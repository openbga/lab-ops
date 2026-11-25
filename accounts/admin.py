"""Account administration: the supported way to manage staff and client users.

Boundaries enforced here, because the Django admin is otherwise a path around the domain
rules:
- Only superusers (the deployment owner) can open user or group admin. A superuser has no
  laboratory role and no capability by that status alone (see accounts.roles).
- A client user can hold no staff flag, role, or permission, so a client link can never
  coexist with staff access.
- Users are deactivated, never deleted; the last active superuser with staff status (who can
  open account administration) cannot be removed (the scheduled jobs' account, which nobody
  signs in as, does not count).
- The scheduled jobs' account (jobs.actor) cannot be edited here: it stays inactive, with its
  name and undeliverable address, and no staff flag, role or permission.
- The built-in role groups keep their names. Every account and group change, and every
  password change, writes an attributed audit event; password values are never recorded.
- A new account opened from a client organization's page ("Add portal account") arrives with
  that organization chosen (Django admin takes initial values from the query string).
- The identity library's own admin pages are replaced. Authenticator secrets and recovery
  codes are never shown or filterable; a superuser may only remove a lost authenticator, which
  is audited and emailed to the account (accounts.security_notices).
  Authentication email addresses are read only here.
- An account's page shows how many of its sessions are open, and End sessions ends them all with
  a reason (accounts.session_list.end_account_sessions), audited. Their browsers and addresses
  are shown only to the account itself, on Where you are signed in.
- An account's page shows whether its password is marked as compromised, and Mark as compromised
  marks it, with a reason (accounts.compromised.mark_compromised), audited: its sessions end and
  the person must choose a new password before anything else.
- Every save here that changes an account or a role (adding either, their pages, an account's
  password page, Mark as compromised, removing an authenticator) asks the superuser to confirm it
  is them, unless they did in the last few minutes (accounts.confirmation). Ending sessions does
  not ask.
"""

import hashlib
import json

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminPasswordChangeForm as AdminSitePasswordChangeForm
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import (
    AdminPasswordChangeForm,
    AdminUserCreationForm,
    ReadOnlyPasswordHashWidget,
    UserChangeForm,
)
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, router, transaction
from django.db.models import Count, Prefetch
from django.db.models.constants import LOOKUP_SEP
from django.forms.models import model_to_dict
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import formats, timezone
from django.utils.html import format_html

from core.audit import record_event

from .confirmation import ConfirmIdentity
from .forms import PasswordGuidance
from .models import User
from .roles import CAPABILITIES, ROLES
from .setup import account_snapshot, group_snapshot


class SuperuserOnly:
    """Account and group administration is deployment-owner work."""

    def _allowed(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return False


def capability_names(user):
    """Capability labels the account holds, from prefetched permissions; as accounts.roles."""
    if user.client_id is not None or not user.is_active:
        return []
    direct = {(p.content_type.app_label, p.codename) for p in user.user_permissions.all()}
    grouped = {
        (p.content_type.app_label, p.codename)
        for g in user.groups.all()
        for p in g.permissions.all()
    }
    held = {code for app, code in direct | grouped if app == "accounts"}
    return [label for code, label in CAPABILITIES.items() if code in held]


class AccountTypeFilter(admin.SimpleListFilter):
    title = "account type"
    parameter_name = "account_type"

    def lookups(self, request, model_admin):
        return [("staff", "Laboratory staff"), ("client", "Client portal user")]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(client__isnull=self.value() == "staff")
        return queryset


CHANGED_SINCE_DRAWN = (
    "Someone else saved this account after you opened this page, so nothing was saved. Open the "
    "account again to see its current details, then make your change."
)


# What the account page shows and saves: names, email, organization, flags, roles and
# capabilities (not the password, the sign-in time or the date joined, which it does not edit).
PAGE_FIELDS = (
    "username",
    "first_name",
    "last_name",
    "email",
    "client",
    "groups",
    "is_active",
    "is_staff",
    "is_superuser",
    "user_permissions",
)
RELATIONS = ("groups", "user_permissions")
COLUMNS = tuple(name for name in PAGE_FIELDS if name not in RELATIONS)


def _plain(value):
    if hasattr(value, "pk"):
        return value.pk
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
        return value[0]  # a role's (id, name) of an account_reading: the membership is its id
    if isinstance(value, (list, tuple, set, frozenset)) or hasattr(value, "values_list"):
        return sorted(_plain(item) for item in value)
    return value


def _digest(values):
    state = {name: _plain(values.get(name)) for name in PAGE_FIELDS}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def account_reading(user):
    """The page's fields of `user` as stored: its columns from this instance, its roles as
    (id, name) pairs and its capability grants as ids, each read once, sorted. The fingerprint
    takes the roles' ids: a role renamed on its own page changes no account."""
    reading = model_to_dict(user, fields=COLUMNS)
    reading["groups"] = sorted(user.groups.values_list("pk", "name"))
    reading["user_permissions"] = sorted(user.user_permissions.values_list("pk", flat=True))
    return reading


def _group_ids(reading):
    return {pk for pk, _name in reading["groups"]}


def drawn_state(user):
    """A fingerprint of the page's fields of `user` as stored (account_reading)."""
    return _digest(account_reading(user))


def _snapshot(reading, group_names, permission_labels):
    """account_snapshot's shape (the audit's) of an account_reading whose roles are ids, with
    the names and labels given, so that before and after name a role the same way."""
    return {
        "username": reading["username"],
        "email": reading["email"],
        "first_name": reading["first_name"],
        "last_name": reading["last_name"],
        "is_active": reading["is_active"],
        "is_staff": reading["is_staff"],
        "is_superuser": reading["is_superuser"],
        "client": reading["client"],
        "groups": sorted(group_names[pk] for pk in reading["groups"]),
        "permissions": sorted(permission_labels[pk] for pk in reading["user_permissions"]),
    }


def _labels(permission_ids):
    permissions = Permission.objects.filter(pk__in=permission_ids)
    return {
        pk: f"{app}.{code}"
        for pk, app, code in permissions.values_list("pk", "content_type__app_label", "codename")
    }


def _role_names(group_ids):
    """{id: name} of these groups, read and held FOR SHARE until the transaction ends: a rename
    on the role group page committed before is read, and one started now waits for this save to
    commit, so a change recorded with these names comes before the rename in the history (the
    access report, audit_review.access_report, follows renames in that order)."""
    table = connection.ops.quote_name(Group._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, name FROM {table} WHERE id = ANY(%s) ORDER BY id FOR SHARE",
            [sorted(group_ids)],
        )
        return dict(cursor.fetchall())


def _owns_administration(user):
    return user.is_superuser and user.is_active and user.is_staff


class DrawnStateField(forms.CharField):
    """The account as the page was drawn (drawn_state), sent back with the form. Never a change
    of its own."""

    widget = forms.HiddenInput

    def has_changed(self, initial, data):
        return False


class AccessRules:
    """Shared validation for the add and change forms."""

    def clean(self):
        from jobs.actor import is_system_account, system_account_id

        data = super().clean()
        if self.instance.pk and is_system_account(self.instance) and self.has_changed():
            # Its name labels every change the jobs made (the timelines read it), and its
            # address must stay undeliverable: it is not edited at all.
            raise forms.ValidationError(
                "This account records what the scheduled jobs do and nobody signs in as it. It "
                "cannot be changed: it stays inactive, with its name and an address that receives "
                "no mail, and no staff access, role or permission."
            )
        if self.instance.pk and "client" in data:
            stored = User.objects.filter(pk=self.instance.pk).values_list("client_id", flat=True)
            was_client = stored.first() is not None
            if was_client != (data["client"] is not None):
                self.add_error(
                    "client",
                    "An account stays a laboratory account or a client portal account for "
                    "life. Create a new account instead. A portal account may move to another "
                    "client organization.",
                )
                return data
        if data.get("client"):
            privileged = (
                data.get("is_staff")
                or data.get("is_superuser")
                or data.get("groups")
                or data.get("user_permissions")
            )
            if privileged:
                raise forms.ValidationError(
                    "A client user cannot be staff or superuser or hold roles or permissions. "
                    "Clear the client to create a laboratory account."
                )
        if self.instance.pk and connection.in_atomic_block:
            # The account's access gate before any other lock this save takes (the owner rows
            # below, then the account): the save waits for the person's changes under way, which
            # may lock an owner's row themselves (accounts.acting).
            from .acting import close_access

            close_access([self.instance.pk])
        # An owner opens account administration: active, superuser and staff.
        owner = _owns_administration(self.instance)
        last_owner = False
        if self.instance.pk and owner:
            leaving = not all(
                data.get(flag, True) for flag in ("is_superuser", "is_active", "is_staff")
            )
            owners = User.objects.filter(is_superuser=True, is_active=True, is_staff=True)
            if leaving and connection.in_atomic_block:
                # Every owner row held until this save commits (the admin saves in one
                # transaction): two owners stepping down at once take turns, and the second sees
                # the first has gone. FOR NO KEY UPDATE, so that an owner's own change elsewhere
                # (a ticket saved naming them as its author or actor) does not wait for this one
                # while this one waits for that ticket.
                list(owners.select_for_update(no_key=True).order_by("pk"))
            others = owners.exclude(pk__in=[self.instance.pk, system_account_id()])
            last_owner = leaving and not others.exists()
        if self.instance.pk and "drawn" in self.fields:
            self._check_drawn(data, owner)
        if last_owner:
            raise forms.ValidationError(
                "Keep at least one active superuser with staff status, so someone can still "
                "open account administration."
            )
        return data

    def _check_drawn(self, data, owner):
        """Refuse the save when the account is no longer as the page was drawn. The account's row
        is locked FOR NO KEY UPDATE first, as its save will lock it (after the owners' rows
        above, the order two owners stepping down take them in), so any other change to it, an
        email corrected on its own included, has committed and is read here, or waits for this
        save. Every field the page posts is the value it was drawn with unless the person
        changed it, so saving over a newer value would put the old one back. The decision on
        the owners above, taken from the account read before the locks, must also rest on this
        reading."""
        rows = User.objects.filter(pk=self.instance.pk)
        if connection.in_atomic_block:
            rows = rows.select_for_update(no_key=True)
        current = rows.first()
        reading = account_reading(current) if current is not None else None
        if (
            reading is None
            or data.get("drawn") != _digest(reading)
            or _owns_administration(current) != owner
        ):
            raise forms.ValidationError(CHANGED_SINCE_DRAWN)
        # What the save starts from: the audit's before values, and the roles and grants it
        # changes by difference (AccountAdmin.save_related), which names the roles as they stand
        # when it records the change (_role_names).
        self.held_reading = reading


class ReservedPasswordWidget(ReadOnlyPasswordHashWidget):
    """The password status of an account whose password cannot be set, with the reason and
    without the link to the password form, which refuses it."""

    template_name = "admin/accounts/reserved_password.html"
    explanation = "Nobody signs in as this account: it has no password, and none can be set."

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["explanation"] = self.explanation
        return context


class AccountChangeForm(ConfirmIdentity, AccessRules, UserChangeForm):
    # The account as drawn: a save is refused when it has changed since (AccessRules).
    drawn = DrawnStateField(required=False)

    def __init__(self, *args, **kwargs):
        from jobs.actor import is_system_account

        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            # The values this form draws, not a second reading of the account: a role added
            # between the two would otherwise be in the fingerprint and missing from the page.
            drawn = {name: self.initial[name] for name in PAGE_FIELDS if name in self.initial}
            self.initial["drawn"] = _digest({**account_reading(self.instance), **drawn})
        password = self.fields.get("password")
        if password is not None and self.instance.pk and is_system_account(self.instance):
            # Its page is read only and its password form refuses it: offer neither. (A read-only
            # page shows the widget, not the field's help text, so the widget gives the reason.)
            password.widget = ReservedPasswordWidget()
            password.help_text = ReservedPasswordWidget.explanation


class AccountCreationForm(ConfirmIdentity, PasswordGuidance, AccessRules, AdminUserCreationForm):
    # A new account's first password: the rule for an account not yet saved.
    guidance_own = False

    def guided_account(self):
        return self.instance


class EndSessionsForm(forms.Form):
    reason = forms.CharField(
        # The page's one field: focused when it opens, and again when a save is refused.
        widget=forms.Textarea(attrs={"rows": 3, "cols": 60, "autofocus": True}),
        max_length=1000,
        help_text="Recorded in the audit history with the sessions ended, for example the person "
        "has left or a device was lost. Do not include patient information.",
    )


class MarkCompromisedForm(ConfirmIdentity, forms.Form):
    reason = forms.CharField(
        # The page's one field: focused when it opens, and again when a save is refused.
        widget=forms.Textarea(attrs={"rows": 3, "cols": 60, "autofocus": True}),
        max_length=1000,
        help_text="Recorded in the audit history with the mark, for example the person typed it "
        "into a page posing as the laboratory's, or it was found in a published breach. The "
        "person does not see it. Do not include patient information.",
    )
    # The password as the page was drawn (accounts.compromised.drawn_password): a password changed
    # since is not marked.
    drawn = forms.CharField(widget=forms.HiddenInput)


class AccountPasswordForm(ConfirmIdentity, PasswordGuidance, AdminPasswordChangeForm):
    """The account's password page. It saves the password only: the account was read when the
    request began, and a full save would put back what changed since, such as a deactivation
    committed while the new password was being checked and hashed."""

    guidance_own = False

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save(update_fields=["password"])
        return user


class OwnPasswordForm(PasswordGuidance, AdminSitePasswordChangeForm):
    """The admin console's own Change password page (for the signed-in account). It saves the
    password only, as AccountPasswordForm does for an account's password page: the account was
    read when the request began, and a full save would put back what changed since."""

    guidance_field = "new_password1"

    def clean_old_password(self):
        # A current password not accepted: recorded in the account's sign-in history.
        try:
            return super().clean_old_password()
        except ValidationError:
            from .forms import record_current_password_refused

            record_current_password_refused(self.user)
            raise

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save(update_fields=["password"])
        return user


def own_password_change(request, extra_context=None):
    """The admin console's own Change password page, as AdminSite.password_change draws it, with
    OwnPasswordForm: Django's form saves the whole account."""
    from django.contrib.auth.views import PasswordChangeView
    from django.urls import reverse

    site = admin.site
    defaults = {
        "form_class": OwnPasswordForm,
        "success_url": reverse("admin:password_change_done", current_app=site.name),
        "extra_context": {**site.each_context(request), **(extra_context or {})},
    }
    if site.password_change_template is not None:
        defaults["template_name"] = site.password_change_template
    request.current_app = site.name
    return PasswordChangeView.as_view(**defaults)(request)


admin.site.password_change = own_password_change


@admin.register(User)
class AccountAdmin(SuperuserOnly, UserAdmin):
    form = AccountChangeForm
    add_form = AccountCreationForm
    change_password_form = AccountPasswordForm
    # Django's own templates, with Confirm it is you (accounts.confirmation) before the buttons.
    add_form_template = "admin/accounts/user/add_form.html"
    change_user_password_template = "admin/accounts/user/change_password.html"

    def has_change_permission(self, request, obj=None):
        # The scheduled jobs' account is shown, never changed (its password page included).
        from jobs.actor import is_system_account

        if obj is not None and is_system_account(obj):
            return False
        return super().has_change_permission(request, obj)

    list_display = (
        "username",
        "full_name",
        "email",
        "roles",
        "capabilities",
        "client",
        "is_active",
        "is_superuser",
        "last_login",
    )
    list_filter = (
        AccountTypeFilter,
        "is_active",
        "is_superuser",
        "groups",
        "client",
    )
    ordering = ("username",)

    def get_queryset(self, request):
        permissions = Permission.objects.select_related("content_type")
        return (
            super()
            .get_queryset(request)
            .select_related("client")
            .prefetch_related(
                "groups",
                Prefetch("user_permissions", queryset=permissions),
                Prefetch("groups__permissions", queryset=permissions),
            )
        )

    @admin.display(description="Name", ordering="last_name")
    def full_name(self, user):
        return user.get_full_name() or "—"

    @admin.display(description="Roles")
    def roles(self, user):
        if user.client_id is not None:
            return "Client portal"
        return ", ".join(sorted(g.name for g in user.groups.all())) or "None"

    @admin.display(description="Capabilities")
    def capabilities(self, user):
        return ", ".join(capability_names(user)) or "—"

    actions = None
    # Recorded by the application: shown, never edited (a save would put back the time drawn).
    readonly_fields = ("last_login", "date_joined", "open_sessions", "compromised_password")
    fieldsets = (
        (None, {"fields": ("username", "password", "compromised_password", "drawn")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        (
            "Client access",
            {
                "fields": ("client",),
                "description": "Set for a client portal user, who sees only this "
                "organization's workspace. Leave blank for laboratory staff. Client users cannot "
                "have roles.",
            },
        ),
        (
            "Laboratory roles",
            {
                "fields": ("groups",),
                "description": "Staff roles are the groups: "
                + ", ".join(ROLES)
                + ". Each group's name is fixed.",
            },
        ),
        (
            "Account status and technical access",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "user_permissions"),
                "description": "Staff and superuser open this admin console only. They give "
                "no laboratory role and no capability. A capability is an explicit permission "
                "under accounts (for example accounts | user | Can view patient information), "
                "given here or through a group. Untick Active instead of deleting a user.",
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
        (
            "Sessions",
            {
                "fields": ("open_sessions",),
                "description": "Sessions open now, counted when this page opened. End sessions "
                "signs every browser of the account out at its next request, with a reason (on "
                "your own account, every one but the browser you are using). Deactivating the "
                "account or setting a new password also ends them.",
            },
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "usable_password", "password1", "password2"),
            },
        ),
        ("Client access", {"fields": ("client",)}),
        ("Laboratory roles", {"fields": ("groups",)}),
        ("Technical access", {"fields": ("is_staff", "is_superuser")}),
    )

    def save_model(self, request, obj, form, change):
        # The audit's before values are the reading the form's check held under the account's row
        # lock (AccessRules._check_drawn): the account as the page was drawn (save_related).
        obj._audit_before = {}
        obj.changed_by = request.user  # a change of organization resets portal access (clients)
        if not change:
            super().save_model(request, obj, form, change)
            return
        # Only the columns the page edits. The others (the password, the last sign-in) keep what
        # is stored now: this copy of the account was read before the save waited for its locks,
        # and a password the person changed meanwhile would otherwise be put back.
        edited = [
            field.name
            for field in User._meta.concrete_fields
            if field.name in form.fields and not form.fields[field.name].disabled
        ]
        obj.save(update_fields=edited)

    def save_related(self, request, form, formsets, change):
        obj = form.instance
        if not change:
            super().save_related(request, form, formsets, change)
            after = account_snapshot(obj)
            changed = after != obj._audit_before
        else:
            # Roles and capability grants by difference from the reading the check held. A role
            # added by other means after that reading (nothing locks an addition) stays, and the
            # audit records what this save itself added or removed: not that addition, nor one
            # this page asked for that the other change had made already.
            held = dict(form.held_reading, groups=_group_ids(form.held_reading))
            before = dict(held)
            after = dict(held)
            for name in RELATIONS:
                if name not in form.cleaned_data:
                    continue
                kept = set(held[name])
                posted = {item.pk for item in form.cleaned_data[name]}
                manager = getattr(obj, name)
                present = set(manager.values_list("pk", flat=True))
                removed, added = (kept - posted) & present, (posted - kept) - present
                if removed:
                    manager.remove(*removed)
                if added:
                    manager.add(*added)
                after[name] = (kept - removed) | added
            for formset in formsets:
                self.save_formset(request, form, formset, change=change)
            after.update(model_to_dict(obj, fields=COLUMNS))
            # One reading of the names for both, so a role renamed on the role group page
            # (recorded there) does not show as a change of this account; read as this save
            # records the change, so the event names each role as it is named then (_role_names).
            names = _role_names(set(before["groups"]) | set(after["groups"]))
            labels = _labels(set(before["user_permissions"]) | set(after["user_permissions"]))
            # Whether anything changed is decided on the values themselves (role ids), not on
            # their names: two roles renamed meanwhile could otherwise hide a change.
            changed = _digest(before) != _digest(after)
            obj._audit_before = _snapshot(before, names, labels)
            after = _snapshot(after, names, labels)
        before = obj._audit_before
        if changed:
            record_event(
                actor=request.user,
                action="account.updated" if change else "account.created",
                obj=obj,
                before=before,
                after=after,
                reason="Django admin",
            )
        # A deactivated person stays nobody's assignee: the save returned their open requests to
        # their teams' queues in this transaction (desk.signals).
        released = getattr(obj, "released_assignments", 0)
        if released:
            self.message_user(
                request,
                f"{released} ticket{'s' if released != 1 else ''} returned to their team queues.",
            )

    @admin.display(description="Open sessions")
    def open_sessions(self, user):
        from jobs.actor import is_system_account

        from .session_list import open_count

        if user.pk is None:
            return "—"
        count = open_count(user)
        text = f"{count} open session{'s' if count != 1 else ''}"
        if is_system_account(user):
            return text
        url = reverse("admin:accounts_user_sessions", args=[user.pk])
        return format_html('{} <a href="{}">End sessions</a>', text, url)

    @admin.display(description="Compromised password")
    def compromised_password(self, user):
        """Whether the password is marked as compromised now, and the way to mark it."""
        from .compromised import REFUSALS, open_mark, refusal

        if user.pk is None:
            return "—"
        mark = open_mark(user.pk)
        if mark is not None:
            when = formats.localize(timezone.localtime(mark.marked_at))
            return (
                f"Marked on {when} by {mark.marked_by.get_username()}. The person must choose a "
                "new password before anything else; any new password lifts the mark."
            )
        if user.pk == getattr(user, "_vr_admin_viewer", None):
            return REFUSALS["own"]
        refused = refusal(user, None)
        if refused == REFUSALS["system"]:
            return "—"
        if refused:
            return refused
        url = reverse("admin:accounts_user_compromised", args=[user.pk])
        return format_html('Not marked. <a href="{}">Mark as compromised</a>', url)

    def get_object(self, request, object_id, from_field=None):
        user = super().get_object(request, object_id, from_field)
        if user is not None:
            # Who is looking, for the Compromised password field (one's own cannot be marked).
            user._vr_admin_viewer = request.user.pk
        return user

    def get_urls(self):
        return [
            path(
                "<id>/sessions/",
                self.admin_site.admin_view(self.end_sessions_view),
                name="accounts_user_sessions",
            ),
            path(
                "<id>/compromised/",
                self.admin_site.admin_view(self.mark_compromised_view),
                name="accounts_user_compromised",
            ),
            *super().get_urls(),
        ]

    def mark_compromised_view(self, request, id):
        """Mark the account's password as compromised, with a reason. Decided and recorded by the
        service, from the account as it stands when it is saved (accounts.compromised)."""
        from allauth.account import app_settings as account_settings

        from core.second_factor import has_second_factor
        from jobs.outbox import mail_enabled

        from .compromised import (
            ALREADY_MARKED,
            can_mark,
            drawn_password,
            mark_compromised,
            open_mark,
            refusal,
        )
        from .session_list import open_count

        user = self.get_object(request, unquote(id))
        if user is None:
            raise Http404("No such account.")
        if not self.has_change_permission(request, user) or not can_mark(request.user):
            raise PermissionDenied
        sign_in_codes = mail_enabled() and account_settings.LOGIN_BY_CODE_ENABLED
        if request.method == "POST":
            form = MarkCompromisedForm(request.POST)
        else:
            form = MarkCompromisedForm(initial={"drawn": drawn_password(user)})
        # Whether the mark can be offered for this account at all, before the form (and the
        # confirmation of who is making it) is checked: a mark refused for the account is not a
        # reason to ask for a password. The service decides again when it saves. A mark already
        # open is left to the service on a save, under its locks: the person may be choosing a new
        # password at this moment, which lifts it, and the save waits for that.
        refused = refusal(user, request.user)
        if request.method == "POST" and refused is None and form.is_valid():
            try:
                with transaction.atomic():
                    marked = mark_compromised(
                        actor=request.user,
                        account_id=user.pk,
                        reason=form.cleaned_data["reason"],
                        drawn=form.cleaned_data["drawn"],
                    )
                    # Named as the service read it, not as this request first read it.
                    user = User.objects.get(pk=user.pk)
                    self.log_change(request, user, "Marked the password as compromised.")
            except ValidationError as error:
                form.add_error(None, error)
                # Refused by the service as it saved: the page is drawn from the account as it is
                # now, not as this request first read it (its name, and whether it can be marked).
                user = User.objects.filter(pk=user.pk).first()
                if user is None:
                    raise Http404("No such account.") from None
                user._vr_admin_viewer = request.user.pk
                refused = refusal(user, request.user)
                if refused is None and open_mark(user.pk) is not None:
                    refused = ALREADY_MARKED
            except User.DoesNotExist:
                raise Http404("No such account.") from None
            else:
                count = len(marked.sessions)
                ended = (
                    f"{count} session{'s' if count != 1 else ''} ended."
                    if count
                    else "No session was open."
                )
                if not user.is_active:
                    then = (
                        "The account is deactivated, so it cannot sign in until it is made "
                        "active again, and then must choose a new password."
                    )
                elif marked.second_factor:
                    then = (
                        "Tell the person to sign in with their password and an authenticator "
                        "code: they will be asked to choose a new password."
                    )
                elif mail_enabled() and user.email:
                    by_code = (
                        "or to sign in with a code emailed to them and choose a new password, "
                        if sign_in_codes
                        else ""
                    )
                    then = (
                        "The account has no second factor, so its password no longer signs it "
                        "in. Tell the person to use Forgot your password? on the sign-in page, "
                        f"{by_code}or set a new password for them on the account's password page."
                    )
                elif mail_enabled():
                    then = (
                        "The account has no second factor and no email address, so set a new "
                        "password for them on the account's password page."
                    )
                else:
                    then = (
                        "The account has no second factor and this deployment sends no email, so "
                        "set a new password for them on the account's password page."
                    )
                self.message_user(
                    request,
                    f"Marked the password of {user.get_username()} as compromised. {ended} {then}",
                )
                return HttpResponseRedirect(reverse("admin:accounts_user_change", args=[user.pk]))
        if refused is None and open_mark(user.pk) is not None:
            refused = ALREADY_MARKED  # drawn, not saved: the page offers no form for it
        context = {
            **self.admin_site.each_context(request),
            "title": f"Mark password as compromised: {user.get_username()}",
            "subtitle": None,
            "opts": self.opts,
            "original": user,
            "form": form,
            "refused": refused,
            "second_factor": has_second_factor(user),
            "sign_in_codes": sign_in_codes,
            # Whether the mark's email notice would be sent, as accounts.security_notices decides
            # at sending: email on, the account active and with an address.
            "notice_sent": mail_enabled() and user.is_active and bool(user.email),
            # Whether the person can choose a new password by email: email on and an address.
            "email_recovery": mail_enabled() and bool(user.email),
            "active": user.is_active,
            "open_count": open_count(user),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(request, "admin/accounts/user/mark_compromised.html", context)

    def end_sessions_view(self, request, id):
        """End every open session of the account, with a reason (on one's own account, every one
        but the session making the change). Decided and recorded by the service, from the
        sessions as they stand when it is saved."""
        from .session_list import can_end_sessions, end_account_sessions, open_sessions

        user = self.get_object(request, unquote(id))
        if user is None:
            raise Http404("No such account.")
        if not self.has_change_permission(request, user) or not can_end_sessions(request.user):
            raise PermissionDenied
        own = user.pk == request.user.pk
        state = getattr(request, "account_session", None)
        form = EndSessionsForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    ended = end_account_sessions(
                        actor=request.user,
                        account_id=user.pk,
                        reason=form.cleaned_data["reason"],
                        current=state.id if state is not None else None,
                    )
                    if ended:
                        self.log_change(
                            request,
                            user,
                            f"Ended {len(ended)} session{'s' if len(ended) != 1 else ''}.",
                        )
            except ValidationError as error:
                form.add_error("reason", error.messages[0])
            except User.DoesNotExist:
                raise Http404("No such account.") from None
            else:
                name = user.get_username()
                if not ended and own:
                    text = (
                        "None of your other sessions was open, so none was ended. The session you "
                        "are using stays signed in."
                    )
                elif not ended:
                    text = f"No session of {name} was open, so none was ended."
                elif own:
                    text = (
                        f"Ended {len(ended)} of your other session{'s' if len(ended) != 1 else ''}"
                        ". This one stays signed in."
                    )
                else:
                    text = (
                        f"Ended {len(ended)} session{'s' if len(ended) != 1 else ''} of {name}. "
                        "Each browser goes to the sign-in page at its next request."
                    )
                self.message_user(request, text)
                return HttpResponseRedirect(reverse("admin:accounts_user_change", args=[user.pk]))
        rows = open_sessions(user)
        if own and state is not None:
            rows = [row for row in rows if row.id != state.id]
        context = {
            **self.admin_site.each_context(request),
            "title": f"End sessions: {user.get_username()}",
            "subtitle": None,
            "opts": self.opts,
            "original": user,
            "form": form,
            "own_account": own,
            "open_count": len(rows),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(request, "admin/accounts/user/end_sessions.html", context)

    def user_change_password(self, request, id, form_url=""):
        with transaction.atomic():
            response = super().user_change_password(request, id, form_url)
            # Saved: Django sends a saved password to the account's page. A refused submission
            # (both buttons sent, as without script) comes back to this page, unsaved.
            saved = request.method == "POST" and response.status_code == 302
            if saved:
                user = self.get_object(request, unquote(id))
                saved = response["Location"] == reverse(
                    "admin:accounts_user_change", args=[user.pk]
                )
            if saved:
                record_event(
                    actor=request.user,
                    action="account.password_changed",
                    obj=user,
                    reason="Django admin; password value not recorded",
                )
            return response


admin.site.unregister(Group)


class RoleGroupForm(ConfirmIdentity, forms.ModelForm):
    """A role's page: saving asks the superuser to confirm it is them (accounts.confirmation)."""


@admin.register(Group)
class RoleGroupAdmin(SuperuserOnly, GroupAdmin):
    """Group permissions grant capabilities, so edits are audited and role names fixed."""

    form = RoleGroupForm
    actions = None
    list_display = ("name", "members", "grants")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(member_count=Count("user", distinct=True))
            .prefetch_related("permissions__content_type")
        )

    @admin.display(description="Members", ordering="member_count")
    def members(self, group):
        return group.member_count

    @admin.display(description="Capabilities granted")
    def grants(self, group):
        held = {
            p.codename for p in group.permissions.all() if p.content_type.app_label == "accounts"
        }
        return ", ".join(label for code, label in CAPABILITIES.items() if code in held) or "—"

    def get_readonly_fields(self, request, obj=None):
        return ("name",) if obj is not None and obj.name in ROLES else ()

    def save_model(self, request, obj, form, change):
        chosen = form.cleaned_data.get("permissions")
        stored = set(obj.permissions.values_list("pk", flat=True)) if change else set()
        if change and chosen is not None and stored - {p.pk for p in chosen}:
            # A permission taken away: every member's access gate before the group's row, so the
            # removal waits for their changes under way (accounts.acting). Adding waits for
            # nothing.
            from .acting import close_access, role_members

            close_access(role_members([obj.pk]))
        if change:
            # Then the group's row, held until this save commits, before its values are read for
            # the audit: two saves of the page take turns, and the second records what it changed
            # from the first's result, not from the page it was drawn with. An account save naming
            # the group holds it shared (_role_names), so a rename waits for it.
            obj._audit_before = group_snapshot(
                Group.objects.select_for_update(no_key=True).get(pk=obj.pk)
            )
        else:
            obj._audit_before = {}
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        after = group_snapshot(obj)
        if after != obj._audit_before:
            record_event(
                actor=request.user,
                action="group.updated" if change else "group.created",
                obj=obj,
                before=obj._audit_before,
                after=after,
                reason="Django admin",
            )


class AuthenticatorRemovalForm(ConfirmIdentity, forms.Form):
    """Removing an authenticator: the superuser confirms it is them (accounts.confirmation)."""


admin.site.unregister(Authenticator)


@admin.register(Authenticator)
class AuthenticatorAdmin(SuperuserOnly, admin.ModelAdmin):
    """Read-only factor records without their secrets; removal is the only change.

    Removing a lost authenticator is the account-recovery path, so a superuser may delete one
    record at a time (no bulk action) and the removal is audited against the account in the
    same transaction. Enrollment stays on the user's own two-factor pages.
    """

    fields = ("user", "type", "created_at", "last_used_at")  # never "data": secrets, codes
    readonly_fields = fields
    list_display = fields
    list_filter = ("type", "created_at", "last_used_at")
    list_select_related = ("user",)
    actions = None

    def lookup_allowed(self, lookup, value, request=None):
        # As UserAdmin does for password: a changelist filter on the stored secret, such as
        # ?data__secret__startswith=<prefix>, would reveal it one character per request.
        if lookup.split(LOOKUP_SEP, 1)[0] == "data":
            return False
        return super().lookup_allowed(lookup, value, request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)

    # Django's confirmation page, with Confirm it is you (accounts.confirmation) in its form.
    delete_confirmation_template = "admin/mfa/authenticator/delete_confirmation.html"

    def delete_view(self, request, object_id, extra_context=None):
        """Django's delete view, with the superuser's confirmation (AuthenticatorRemovalForm)
        checked after the access and the record, and before anything is removed."""
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return self._removal_view(request, object_id, extra_context)
        with transaction.atomic(using=router.db_for_write(self.model)):
            return self._removal_view(request, object_id, extra_context)

    def _removal_view(self, request, object_id, extra_context):
        obj = self.get_object(request, unquote(object_id))
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied
        if obj is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)
        deleted_objects, model_count, perms_needed, protected = self.get_deleted_objects(
            [obj], request
        )
        posted = request.method == "POST"
        if posted and not protected and perms_needed:
            raise PermissionDenied  # before the confirmation is checked, as Django refuses it
        form = AuthenticatorRemovalForm(request.POST if posted else None)
        if posted and not protected and form.is_valid():
            obj_display = str(obj)
            obj_id = obj.pk
            self.log_deletions(request, [obj])
            self.delete_model(request, obj)
            return self.response_delete(request, obj_display, obj_id)
        object_name = str(self.opts.verbose_name)
        context = {
            **self.admin_site.each_context(request),
            "title": "Cannot delete " + object_name if perms_needed or protected else "Delete",
            "subtitle": None,
            "object_name": object_name,
            "object": obj,
            "deleted_objects": deleted_objects,
            "model_count": dict(model_count).items(),
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": self.opts,
            "app_label": self.opts.app_label,
            "preserved_filters": self.get_preserved_filters(request),
            "is_popup": IS_POPUP_VAR in request.POST or IS_POPUP_VAR in request.GET,
            "to_field": None,
            "form": form,
            **(extra_context or {}),
        }
        return self.render_delete_form(request, context)

    def delete_model(self, request, obj):
        """Remove it, audited, and record the email notice to the account
        (accounts.security_notices), in the delete view's transaction."""
        from . import security_notices

        user = obj.user
        before = {
            "authenticator_id": obj.pk,
            "type": obj.type,
            "created_at": obj.created_at.isoformat(),
            "last_used_at": obj.last_used_at.isoformat() if obj.last_used_at else None,
        }
        super().delete_model(request, obj)
        record_event(
            actor=request.user,
            action="account.authenticator_removed",
            obj=user,
            before=before,
            reason="Django admin; authenticator secret not recorded",
        )
        security_notices.record(
            user, [security_notices.REMOVED], by="administrator", types=[obj.type]
        )


admin.site.unregister(EmailAddress)


@admin.register(EmailAddress)
class EmailAddressAdmin(SuperuserOnly, admin.ModelAdmin):
    """Authentication email addresses, read only. Users manage their own addresses."""

    fields = ("user", "email", "verified", "primary")
    readonly_fields = fields
    list_display = ("email", "user", "primary", "verified")
    list_filter = ("primary", "verified")
    list_select_related = ("user",)
    search_fields = (
        "email",
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
