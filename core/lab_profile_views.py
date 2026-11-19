"""Laboratory profile pages: the profile every staff role reads, and the admin role's forms.

Who may change what, and every rule on a change, live in core.lab_profile; these views only
collect input, show the service's errors on the form and redirect to the profile.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.handlers.exception import response_for_exception
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_GET, require_POST

from accounts.roles import require_role, require_staff

from . import lab_profile as service
from . import modules
from .concurrency import one_reading, protect_form, record_token
from .lab_profile_forms import (
    LabCertificateForm,
    LabContactForm,
    LabIdentityForm,
    LabLogoForm,
    ModuleSwitchForm,
)
from .models import AuditEvent, Lab, LabCertificate, LabContact

CERTIFICATE_ACTIONS = (
    "lab.certificate_added",
    "lab.certificate_changed",
    "lab.certificate_removed",
)
MODULE_ACTIONS = ("module.enabled", "module.disabled")
CONTACT_ACTION = "lab.contact_changed"
HISTORY_LIMIT = 20
# A contact form opened from another page returns there: the fixed marker in ?from= names a
# route, so no address is ever taken from the request.
CONTACT_RETURNS = {"security": "security-settings"}


def _lab():
    return Lab.objects.filter(pk=1).first()


def _service_errors(form, exc):
    """Show a service ValidationError on the form, under the field it names where it names one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            for error in errors:
                form.add_error(name if name in form.fields else None, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def _saved(request, changed, message):
    if changed:
        messages.success(request, message)
    else:
        messages.info(request, "No changes to save.")
    return redirect("lab-profile")


def _history():
    """The newest profile changes and module switches, newest first."""
    switches = (
        AuditEvent.objects.filter(action__in=MODULE_ACTIONS)
        .select_related("actor")
        .order_by("-occurred_at")[:HISTORY_LIMIT]
    )
    events = sorted(
        [*service.history(HISTORY_LIMIT), *switches],
        key=lambda event: event.occurred_at,
        reverse=True,
    )[:HISTORY_LIMIT]
    for event in events:
        if event.action in CERTIFICATE_ACTIONS:
            values = event.after or event.before or {}
            event.detail = " ".join(
                str(values[key]) for key in ("authority", "number") if values.get(key)
            )
        elif event.action in MODULE_ACTIONS:
            key = (event.after or {}).get("key", "")
            event.detail = modules.MODULES.get(key, ("",))[0]
        elif event.action == CONTACT_ACTION:
            event.detail = contact_label(event)
    return events


def contact_label(event):
    """Which contact a lab.contact_changed event changed and who it names, from the event alone:
    "Privacy contact: Compliance Office", "...: Compliance Office, now Example Privacy Desk",
    "...: Compliance Office, cleared"."""
    before, after = event.before or {}, event.after or {}
    label = dict(LabContact.Kind.choices).get(after.get("kind") or before.get("kind", ""), "")
    old, new = before.get("name", ""), after.get("name", "")
    if old and not new:
        names = f"{old}, cleared"
    elif old and new and old != new:
        names = f"{old}, now {new}"
    else:
        names = new
    return ": ".join(part for part in (label, names) if part)


def contact_history(limit=HISTORY_LIMIT):
    """The newest changes to the security and privacy contacts, newest first, each with `detail`
    naming the contact."""
    events = list(
        AuditEvent.objects.filter(action=CONTACT_ACTION)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )
    for event in events:
        event.detail = contact_label(event)
    return events


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
def lab_profile(request):
    # One reading for the access check and the whole page: the panels and the history agree.
    # The page is drawn after the reading, so what drawing writes is not done inside it.
    with one_reading():
        require_staff(request.user)
        lab = _lab()
        can_manage = lab is not None and service.can_manage(request.user)
        certificates = service.certificates_with_state(timezone.localdate()) if lab else []
        for row in certificates:
            row.state_label, row.state_tone = service.CERTIFICATE_STATES[row.state]
            row.label = f"{row.authority} {row.number}"
            row.token = record_token(row) if can_manage else ""
        context = {
            "page_title": "Laboratory profile",
            "lab": lab,
            "logo": service.logo_facts(lab),
            "certificates": certificates,
            "can_manage": can_manage,
            "lab_token": record_token(lab) if can_manage else "",
            "contacts": service.contacts() if lab else [],
            "history": _history() if lab else [],
            "modules": modules.states(),
            "can_manage_modules": modules.can_manage(request.user),
        }
    return render(request, "core/lab_profile.html", context)


def _module_title(enable, name):
    return f"Turn {'on' if enable else 'off'} {name[:1].lower()}{name[1:]}"


@login_required
def lab_module(request, key):
    """Turn one optional module on or off (the admin role), with a reason. The form carries the
    state it asks for and the switch's revision it showed, both from one reading of the switch."""
    require_role(request.user, *modules.MODULE_ROLES)
    state = next((row for row in modules.states() if row["key"] == key), None)
    if state is None:
        raise Http404("No such module.")
    form = ModuleSwitchForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_revision": state["revision"],
            "enabled": "0" if state["is_enabled"] else "1",
        },
    )
    enable = not state["is_enabled"]
    if request.method == "POST":
        if form.is_valid():
            enable = form.cleaned_data["enabled"]
            try:
                modules.set_enabled(
                    actor=request.user,
                    key=key,
                    enabled=enable,
                    expected_revision=form.cleaned_data["expected_revision"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                messages.success(request, "Module turned on." if enable else "Module turned off.")
                return redirect("lab-profile")
        else:
            # The title follows what the posted form asks for, never a second reading.
            enable = form["enabled"].value() == "1"
    title = _module_title(enable, state["name"])
    response = render(
        request,
        "core/lab_module_form.html",
        {
            "page_title": title,
            "form": form,
            "module": state,
            "enable": enable,
            "submit_label": "Turn on" if enable else "Turn off",
        },
    )
    add_never_cache_headers(response)  # a refused form shows the reason typed again
    return response


def _never_stored(view):
    """Every answer of the view, its refusals included (a 403, a 404, the sign-in redirect), is
    marked never to be stored by the browser or a proxy: a refused form shows what was typed."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except (Http404, PermissionDenied) as exc:
            response = response_for_exception(request, exc)
        add_never_cache_headers(response)
        return response

    return wrapped


@_never_stored
@login_required
def lab_contact_edit(request, kind):
    """Record, change or clear the security contact or the privacy contact (the admin role), with
    a reason. The title, the initial values and the revision the form carries all come from one
    reading of the contact; a change saved in between is refused with the input kept."""
    require_role(request.user, *service.PROFILE_ROLES)
    if kind not in LabContact.Kind.values:
        raise Http404("No such contact.")
    origin = request.GET.get("from", "")
    back = CONTACT_RETURNS.get(origin, "lab-profile")
    contact = next(row for row in service.contacts() if row.kind == kind)
    label = contact.get_kind_display()
    form = None
    if _lab() is not None:
        form = LabContactForm(
            request.POST if request.method == "POST" else None,
            initial={**service.contact_values(contact), "expected_revision": contact.revision},
        )
        if request.method == "POST" and form.is_valid():
            try:
                _, changed = service.save_contact(
                    actor=request.user,
                    kind=kind,
                    values={name: form.cleaned_data[name] for name in service.CONTACT_FIELDS},
                    expected_revision=form.cleaned_data["expected_revision"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                if changed:
                    messages.success(request, f"{label} saved.")
                else:
                    messages.info(request, "No changes to save.")
                return redirect(back)
    response = render(
        request,
        "core/lab_contact_form.html",
        {
            "page_title": f"Edit {label.lower()}",
            "form": form,
            "contact_label": label,
            "cancel_url": reverse(back),
        },
    )
    return response


def _form_page(request, form, *, title, submit_label, multipart=False, notes=()):
    return render(
        request,
        "core/lab_profile_form.html",
        {
            "page_title": title,
            "form": form,
            "submit_label": submit_label,
            "multipart": multipart,
            "notes": notes,
        },
    )


@login_required
def lab_profile_edit(request):
    require_role(request.user, *service.PROFILE_ROLES)
    lab = _lab()
    form = None
    if lab is not None:
        form = LabIdentityForm(request.POST if request.method == "POST" else None, instance=lab)
        protect_form(form, lab)
        if request.method == "POST" and form.is_valid():
            try:
                _, changed = service.save_identity(
                    actor=request.user,
                    values={name: form.cleaned_data[name] for name in service.IDENTITY_FIELDS},
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(request, changed, "Laboratory profile saved.")
    return _form_page(
        request,
        form,
        title="Edit laboratory profile",
        submit_label="Save changes",
        notes=(
            "Staff see these details on the profile and client portal accounts on their portal "
            "home. The laboratory name is set when the application is installed and is not "
            "changed here.",
        ),
    )


@login_required
def lab_logo_edit(request):
    require_role(request.user, *service.PROFILE_ROLES)
    lab = _lab()
    form = None
    if lab is not None:
        posted = request.method == "POST"
        form = LabLogoForm(request.POST if posted else None, request.FILES if posted else None)
        protect_form(form, lab)
        if posted and form.is_valid():
            try:
                _, changed = service.set_logo(
                    actor=request.user,
                    uploaded=form.cleaned_data["logo"],
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(request, changed, "Logo saved.")
    replacing = lab is not None and lab.logo_id is not None
    return _form_page(
        request,
        form,
        title="Replace logo" if replacing else "Upload logo",
        submit_label="Replace logo" if replacing else "Upload logo",
        multipart=True,
        notes=(
            "The logo is shown on the profile and on the client portal home. Earlier logos stay "
            "stored, so the profile history can show them.",
        ),
    )


@login_required
@require_POST
def lab_logo_remove(request):
    require_role(request.user, *service.PROFILE_ROLES)
    try:
        # The page's confirmation always sends the token; a request without one is refused.
        _, changed = service.remove_logo(
            actor=request.user, expected_state=request.POST.get("original_state", "")
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("lab-profile")
    return _saved(request, changed, "Logo removed.")


@login_required
@require_GET
def lab_logo_image(request, sha256):
    """Staff may open any stored logo (the profile history shows earlier ones); a client portal
    account only the laboratory's current logo, which its portal home shows."""
    if request.user.client_id is not None:
        current = service.logo_facts(_lab())
        if not request.user.is_active or not current or current["sha256"] != sha256:
            raise Http404("No such logo.")
    else:
        require_staff(request.user)
    found = service.logo_image(sha256)
    if found is None:
        raise Http404("No such logo.")
    data, content_type = found
    response = HttpResponse(data, content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    # Content-addressed: the bytes behind a hash never change.
    response["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@login_required
def lab_certificate_edit(request, pk=None):
    require_role(request.user, *service.PROFILE_ROLES)
    instance = get_object_or_404(LabCertificate, pk=pk) if pk else None
    lab = _lab()
    form = None
    if lab is not None:
        form = LabCertificateForm(
            request.POST if request.method == "POST" else None, instance=instance
        )
        protect_form(form, instance)
        if request.method == "POST" and form.is_valid():
            try:
                _, changed = service.save_certificate(
                    actor=request.user,
                    instance=form.save(commit=False),
                    expected_state=form.cleaned_data.get("original_state"),
                )
            except ValidationError as exc:
                _service_errors(form, exc)
            else:
                return _saved(
                    request, changed, "Certificate saved." if instance else "Certificate added."
                )
    return _form_page(
        request,
        form,
        title=f"Edit {instance}" if instance else "Add certificate",
        submit_label="Save changes" if instance else "Add certificate",
        notes=(
            "Record the certificate as issued. It is listed as recorded and nothing is checked "
            "against it.",
        ),
    )


@login_required
@require_POST
def lab_certificate_remove(request, pk):
    require_role(request.user, *service.PROFILE_ROLES)
    try:
        removed = service.remove_certificate(
            actor=request.user, pk=pk, expected_state=request.POST.get("original_state", "")
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Removed certificate {removed['authority']} {removed['number']}.",
        )
    return redirect("lab-profile")
