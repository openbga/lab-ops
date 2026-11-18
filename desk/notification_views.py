"""Email notices page: a staff member turns their own request notices on or off (N1). Reached
from the user menu; the setting is the core.preferences key `notifications.email`, which
desk.notifications reads. Client portal accounts cannot turn notices off and are refused."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from accounts.models import User
from accounts.roles import require_staff
from core import preferences
from core.forms import StyledForm

from .notifications import OPT_OUT_KEY, wants_notices


class EmailNoticesForm(StyledForm, forms.Form):
    enabled = forms.BooleanField(
        required=False,
        label="Email me ticket notices",
        help_text="A short notice when a ticket is assigned to you, when a client replies on a "
        "ticket assigned to you, and when a ticket assigned to you, or in a team you lead, passes "
        "a service target. When a ticket has no assignee and its team no lead, every member of "
        "the team is told. It names only the ticket number.",
    )


@login_required
def email_notices(request):
    require_staff(request.user)
    form = EmailNoticesForm(
        request.POST if request.method == "POST" else None,  # unticked posts an empty form
        initial={"enabled": wants_notices(request.user)},
    )
    if request.method == "POST" and form.is_valid():
        enabled = form.cleaned_data["enabled"]
        with transaction.atomic():
            # A notice being sent to this account holds the account's row (jobs.outbox): the
            # change waits for it, and every later attempt reads the new choice.
            User.objects.select_for_update().filter(pk=request.user.pk).first()
            # Compare with what is stored, not this session's copy: another session may have
            # changed it since this one signed in.
            request.session.pop(preferences.SESSION_KEY, None)
            if hasattr(request, "_vr_preferences"):
                del request._vr_preferences
            preferences.save(request, OPT_OUT_KEY, None if enabled else False)
        messages.success(request, "Email notice preference saved.")
        return redirect("email-notices")
    return render(
        request,
        "desk/email/preferences.html",
        {"page_title": "Email notices", "form": form},
    )
