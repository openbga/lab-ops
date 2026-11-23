"""A password marked as compromised, which forces a change.

Marking (mark_compromised, from the account's page in the admin console): a superuser who owns the
account administration marks another account's password, with a reason. In one transaction it
takes the session-ending locks of both accounts (accounts.session_list.lock_endings, as a sign-in,
a password saved alone and the other endings take them), holds the superuser's account
(accounts.acting.hold_actor) and locks the account's row, then decides from the account as it
stands: refused for the scheduled jobs' account, the superuser's own account (they change it
themselves), a shared demonstration account (its password is published on purpose), an account
without a usable password, one already marked, and one whose password changed after the page was
opened (the page carries a keyed fingerprint of the password it was drawn with, drawn_password).
It records the mark (accounts.models.CompromisedPassword, with a copy of the stored hash so that
the same password is refused as a new one), ends every open session of the account
(Ending.MARKED_COMPROMISED) and records one audit event, `account.password_marked_compromised`,
with the reason, the mark and the sessions ended: never the password or its hash.

Signing in while marked: the password alone no longer opens a session. A sign-in that proved only
the password (Django's and allauth's authentication succeed, as for any password) has its session
recorded as already ended when it starts (refused_at_sign_in, from accounts.sessions.start_session
under the account's session-ending lock), so its browser is signed out at its next request, with a
notice saying what to do. A sign-in that also proved a second factor or a code emailed to the
account's address (allauth's record of the sign-in's steps, PROOFS) opens a session that reaches
only Choose a new password (PasswordChangeMiddleware), and the sign-in out and the session's own
status, until a new password is saved. That middleware checks the sign-in's steps again at each
request, and ends a session of a marked account that proved only the password (one a server
process of an earlier release opened while a deployment replaces it, or after a rollback). A
session that must first add a second factor the laboratory requires (accounts.enrolment) does
that first.

Lifting: any password chosen for the account afterwards lifts the mark in the same save
(accounts.models.User.save): on Choose a new password, through an emailed reset link, or set by a
superuser on the account's password page. The marked password itself is refused as the new one
(accounts.passwords.MarkedPasswordValidator). A hash upgraded when the person signs in is not a
password chosen, and lifts nothing.

Nothing here emails the person: the superuser tells them. The page that marks it says what they
must do, which depends on whether the account has a second factor.

Basis: NIST SP 800-63B-4 §3.1.1.2 (verifiers force a change of password where there is evidence of
compromise; compromised passwords are refused when one is chosen) and §4.3 (a compromised
authenticator is suspended or invalidated promptly); 45 CFR §164.308(a)(5)(ii)(D) (password
management, addressable). Which evidence counts, and who decides, is the laboratory's.
"""

from dataclasses import dataclass

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from .forms import PasswordGuidance
from .models import AccountSession, CompromisedPassword, User

ACTION = "account.password_marked_compromised"
CHANGED_ACTION = "account.password_changed"
NEW_PASSWORD_ROUTE = "account-new-password"
# The page the sign-in was going to, kept in the browser's session while it waits for a new
# password (as accounts.enrolment keeps its own).
NEXT_KEY = "_new_password_next"
# The steps of a sign-in, as allauth records them, that prove more than the password: an
# authenticator's code or a recovery code, and a code emailed to the account's address.
PROOFS = frozenset({"mfa", "code"})
# What a session waiting for a new password may reach: the page itself; signing out; the session
# status, Stay signed in and keeping what was typed (accounts.sessions); the laboratory's logo;
# and the health checks.
ALLOWED_VIEWS = frozenset(
    {
        NEW_PASSWORD_ROUTE,
        "account_logout",
        "account-session",
        "account-session-continue",
        "account-session-keep",
        "lab-logo-image",
        "health",
        "ready",
    }
)
_DRAWN_SALT = "accounts.compromised.drawn_password"
REASON_LENGTH = 1000

ALREADY_MARKED = (
    "This account's password is already marked as compromised, so nothing changed. The person "
    "must choose a new one before anything else."
)
NOT_THE_DRAWN_MARK = (
    "Nothing was saved: your password's mark changed after this page was opened. Choose a new "
    "password again if you are still asked to."
)
CHANGED_SINCE_DRAWN = (
    "The password of this account changed after you opened this page, so it was not marked. Open "
    "the page again to mark the password it has now."
)
REFUSALS = {
    "system": "Nobody signs in as the scheduled jobs' account: it has no password to mark.",
    "own": "This is your own account. Change your own password instead.",
    "shared": "This is a shared demonstration account. Its password is published on purpose, so it "
    "cannot be marked as compromised.",
    "unusable": "This account has no password, so there is nothing to mark.",
}


def drawn_password(user):
    """A keyed fingerprint of the account's password as stored, for the page that marks it: it
    tells the save whether the password changed since the page was drawn, and reveals nothing of
    the password or its hash without the deployment's secret key."""
    value = f"{user.pk}:{user.password}"
    return salted_hmac(_DRAWN_SALT, value, algorithm="sha256").hexdigest()


def open_mark(user_id):
    """The account's open mark (not lifted), or None."""
    return CompromisedPassword.objects.filter(user_id=user_id, lifted_at__isnull=True).first()


def is_marked(user_id):
    return CompromisedPassword.objects.filter(user_id=user_id, lifted_at__isnull=True).exists()


def refusal(account, actor):
    """Why `account`'s password cannot be marked by `actor`, as REFUSALS words it, or None."""
    from jobs.actor import is_system_account

    if is_system_account(account):
        return REFUSALS["system"]
    if actor is not None and account.pk == actor.pk:
        return REFUSALS["own"]
    if account.credentials_locked:
        return REFUSALS["shared"]
    if not account.has_usable_password():
        return REFUSALS["unusable"]
    return None


def can_mark(user):
    """Whether the account may mark other accounts' passwords: the account administration's
    owner (an active superuser with staff status, who opens it)."""
    from .session_list import can_end_sessions

    return can_end_sessions(user)


@dataclass(frozen=True)
class Marked:
    """What marking did: the mark, the ids of the sessions it ended, and whether the account had
    a second factor then (what the person must do next depends on it)."""

    mark: CompromisedPassword
    sessions: list
    second_factor: bool


def mark_compromised(*, actor, account_id, reason, drawn):
    """Mark the account's password as compromised (see the module docstring). Raises
    PermissionDenied when `actor` may not, User.DoesNotExist when the account does not exist, and
    ValidationError with the reason when this account's password cannot be marked now."""
    from core.audit import record_event
    from core.second_factor import has_second_factor

    from .acting import hold_actor
    from .session_list import lock_endings
    from .sessions import end_session_ids

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required."})
    if len(reason) > REASON_LENGTH:
        raise ValidationError({"reason": f"Keep the reason to {REASON_LENGTH:,} characters."})
    with transaction.atomic():
        lock_endings(getattr(actor, "pk", None), account_id)
        held = hold_actor(actor)
        if held is None or not can_mark(held):
            raise PermissionDenied("Only a superuser can mark a password as compromised.")
        # The account's row, after the acting account's: a password saved with the whole account
        # (setup, a command) locks it too, so the reading below is the password as it stands.
        account = User.objects.select_for_update(no_key=True).filter(pk=account_id).first()
        if account is None:
            raise User.DoesNotExist
        refused = refusal(account, held)
        if refused:
            raise ValidationError(refused)
        if is_marked(account.pk):
            raise ValidationError(ALREADY_MARKED)
        if not constant_time_compare(str(drawn or ""), drawn_password(account)):
            raise ValidationError(CHANGED_SINCE_DRAWN)
        mark = CompromisedPassword.objects.create(
            user=account, marked_by=held, reason=reason, password=account.password
        )
        ended = end_session_ids(account, AccountSession.Ending.MARKED_COMPROMISED)
        record_event(
            actor=held,
            action=ACTION,
            obj=account,
            after={"mark": mark.pk, "sessions": ended, "count": len(ended)},
            reason=reason,
        )
        return Marked(mark, ended, has_second_factor(account))


def proved_beyond_password(request, user):
    """Whether this browser's sign-in of `user` proved more than the password (PROOFS), as allauth
    recorded its steps in the browser's session. The record is the sign-in's own: signing out, and
    being signed out, empties the browser's session."""
    from allauth.account.internal.flows.login import get_authentication_records

    if request is None or getattr(request, "session", None) is None:
        return False
    records = get_authentication_records(request, user)
    return any(isinstance(item, dict) and item.get("method") in PROOFS for item in records)


def refused_at_sign_in(request, user):
    """For a sign-in as it starts (accounts.sessions.start_session, which holds the account's
    session-ending lock): whether the account's password is marked and this sign-in proved
    nothing more than the password, so that its session starts ended."""
    return is_marked(user.pk) and not proved_beyond_password(request, user)


def waiting(state):
    """Whether the signed-in session `state` (accounts.sessions.SessionState) must choose a new
    password before anything else: its account's password is marked. Every session open then was
    ended by the mark, and one that signed in afterwards with only the password started ended; a
    session a process of an earlier release opened with the password alone is ended by
    PasswordChangeMiddleware, which checks the sign-in's steps again at each request."""
    return state is not None and is_marked(state.user_id)


def pending(request):
    return waiting(getattr(request, "account_session", None))


class PasswordChangeMiddleware:
    """After EnrolmentMiddleware: a session waiting for a new password reaches only ALLOWED_VIEWS.
    One that must first add a second factor the laboratory requires is left to that (its own
    pages are the only ones it reaches then)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Decided once a view is reached (process_view); None until then, so that a page drawn
        # without one (Not found) decides for its shell itself (core.context_processors).
        request.vr_password_change_pending = None
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from .enrolment import _refusal

        # Read with the second factor's hold, in the same statement (accounts.enrolment.holds);
        # read here only where that middleware did not run.
        marked = getattr(request, "vr_password_marked", None)
        if marked is None:
            marked = pending(request)
        if marked and not proved_beyond_password(request, request.user):
            # A session of a marked account whose sign-in proved only the password: one a server
            # process of an earlier release opened (it does not know marks) while a deployment
            # replaces it, or after a rollback. Ended here as this release ends such a sign-in,
            # whichever hold would apply, so it can neither choose the new password nor add a
            # second factor first.
            request.vr_password_change_pending = False
            return _end_unproved(request)
        if getattr(request, "vr_second_factor_pending", False):
            request.vr_password_change_pending = False
            return None
        request.vr_password_change_pending = marked
        if not request.vr_password_change_pending:
            return None
        match = request.resolver_match
        if match is not None and match.view_name in ALLOWED_VIEWS:
            return None
        return _refusal(request, NEW_PASSWORD_ROUTE, "password_change_required", NEXT_KEY)


def _end_unproved(request):
    """End this request's session (Ending.MARKED_COMPROMISED) and sign its browser out, with
    the notice; a page opened with GET goes on as a signed-out visitor (to the sign-in page, for a
    page that needs one), anything else is answered at once (accounts.sessions)."""
    from .sessions import Ending, _goes_on, _sign_out, end_sessions
    from .sessions import _refusal as signed_out

    state = request.account_session
    with transaction.atomic():
        end_sessions(request.user, Ending.MARKED_COMPROMISED, session_id=state.id)
    _sign_out(request, Ending.MARKED_COMPROMISED)
    return None if _goes_on(request) else signed_out(request)


class NewPasswordForm(PasswordGuidance, SetPasswordForm):
    """The new password, twice, checked against the rule for the account (the marked password
    refused too); no current password, which is the one marked. It carries the mark the page was
    drawn for, which the save requires to be the open one. Saved by choose_new_password."""

    guidance_field = "new_password1"
    mark = forms.IntegerField(widget=forms.HiddenInput, label="Marked password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("new_password1", "new_password2"):
            self.fields[name].widget.attrs["autocomplete"] = "new-password"
        self.fields["new_password1"].widget.attrs["autofocus"] = True

    def clean(self):
        # As Django's, with the rule's refusals beside New password, where the rule is stated,
        # rather than under the confirmation.
        self.validate_passwords("new_password1", "new_password2")
        self.validate_password_for_user(self.user, "new_password1")
        return forms.Form.clean(self)

    def save(self, commit=True):  # pragma: no cover - the page saves through choose_new_password
        raise NotImplementedError("Save through accounts.compromised.choose_new_password.")


def choose_new_password(*, user, password, mark_id):
    """Save the person's new password while the mark the page was drawn for (`mark_id`, posted
    with the form) is still the account's open one, in one transaction: the account's
    session-ending lock, then the account held (a session that has ended is refused,
    accounts.sessions.SessionEnded), then the mark. The save lifts the mark and ends the account's
    other sessions, keeping this one (accounts.models.User.save). Returns the account as saved, or
    None when that mark is not open (lifted meanwhile, or another account's or unknown), in which
    case nothing is saved."""
    from core.audit import record_event

    from .acting import hold_actor
    from .session_list import lock_endings

    with transaction.atomic():
        lock_endings(user.pk)
        held = hold_actor(user)
        if held is None or not held.is_active or held.credentials_locked:
            raise PermissionDenied("You can no longer do this.")
        mark = (
            CompromisedPassword.objects.select_for_update()
            .filter(pk=mark_id, user_id=held.pk, lifted_at__isnull=True)
            .first()
        )
        if mark is None:
            return None
        held.set_password(password)
        held.save(update_fields=["password"])
        record_event(
            actor=held,
            action=CHANGED_ACTION,
            obj=held,
            after={"compromised_mark": mark.pk},
            reason="New password chosen after the password was marked as compromised; password "
            "value not recorded",
        )
        return held


def _next_page(request):
    """The page the sign-in was going to (NEXT_KEY), taken from the browser's session, if it is on
    this site; else None."""
    path = request.session.pop(NEXT_KEY, None)
    if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
        if url_has_allowed_host_and_scheme(path, allowed_hosts={request.get_host()}):
            return path
    return None


@transaction.non_atomic_requests  # choose_new_password opens its own transaction
@require_http_methods(["GET", "HEAD", "POST"])
@login_required
def new_password_page(request):
    """Choose a new password: why it is needed, and the form. Once the account's password is no
    longer marked (a new password saved here or elsewhere), it goes on to the page the sign-in
    was going to, or the start page."""
    from .enrolment import _is_navigation, _remember

    user = request.user
    mark = open_mark(user.pk)
    # The shell (core.context_processors) follows this reading, not the middleware's earlier one.
    request.vr_password_change_pending = mark is not None
    if mark is None:
        return redirect(_next_page(request) or "dashboard")
    locked = user.credentials_locked
    form = None
    if not locked:
        if request.method == "POST":
            form = NewPasswordForm(user, request.POST)
            if form.is_valid():
                saved = choose_new_password(
                    user=user,
                    password=form.cleaned_data["new_password1"],
                    mark_id=form.cleaned_data["mark"],
                )
                if saved is None:
                    # The mark the page was drawn for is no longer the open one.
                    messages.warning(request, NOT_THE_DRAWN_MARK)
                    return redirect(NEW_PASSWORD_ROUTE)
                # This session stays signed in (accounts.sessions._settle_kept_session).
                update_session_auth_hash(request, saved)
                messages.success(request, "Your new password is saved.")
                return redirect(_next_page(request) or "dashboard")
        else:
            form = NewPasswordForm(user, initial={"mark": mark.pk})
    if request.method == "GET" and _is_navigation(request):
        _remember(request, reverse("dashboard"), key=NEXT_KEY)
    return render(
        request,
        "accounts/new_password.html",
        {
            "page_title": "Choose a new password",
            "form": form,
            "locked": locked,
            "marked_at": mark.marked_at,
        },
    )
