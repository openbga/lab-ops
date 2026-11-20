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
from core.forms import StyledForm
from core.models import UserPreference

from .notifications import OPT_OUT_KEY, lock_notice_setting, wants_notices


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
            # The setting's own lock first: the breach job decides whom to tell holding it shared
            # for everyone it may tell, so this change either comes before (and the job reads it)
            # or waits for the job, and a job run meanwhile leaves the ticket for its next run.
            lock_notice_setting(request.user.pk)
            # A notice being sent to this account holds the account's row (jobs.outbox): the
            # change waits for it, and every later attempt reads the new choice. FOR NO KEY
            # UPDATE, so the person's own changes under way, which hold the account FOR KEY
            # SHARE (accounts.acting), do not hold it up: it takes no access away.
            User.objects.select_for_update(no_key=True).filter(pk=request.user.pk).first()
            # Written straight to the stored preference: not compared with what this request
            # read (another tab may have changed it), and never refused by the limit on saved
            # page layouts, so turning mail off always works.
            stored = UserPreference.objects.filter(user=request.user, key=OPT_OUT_KEY)
            if enabled:
                stored.delete()
            else:
                UserPreference.objects.update_or_create(
                    user=request.user, key=OPT_OUT_KEY, defaults={"value": False}
                )
        messages.success(request, "Email notice preference saved.")
        return redirect("email-notices")
    return render(
        request,
        "desk/email/preferences.html",
        {"page_title": "Email notices", "form": form},
    )
