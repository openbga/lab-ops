"""The Security settings page (the admin role): the laboratory's security and privacy contacts,
what protects sign-in, sessions and patient information as read from the running deployment
(core.security_settings), and the recent changes to the contacts.

The page states facts and who sets each one; it changes nothing itself. The contacts are edited
through the laboratory profile's contact form, which returns here when opened from this page.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from accounts.roles import require_role

from . import lab_profile, security_settings
from .lab_profile_views import contact_history
from .models import Lab


@login_required
def security_settings_page(request):
    require_role(request.user, *security_settings.SECURITY_ROLES)
    lab_ready = Lab.objects.filter(pk=1).exists()
    groups = security_settings.in_force()
    toc = [
        ("contacts-heading", "Security and privacy contacts"),
        *[(f"security-{group.key}-heading", group.title) for group in groups],
        ("about-heading", "About this page"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "core/security_settings.html",
        {
            "page_title": "Security settings",
            "contacts": lab_profile.contacts(),
            # The contact form needs the laboratory row; without it there is nothing to edit.
            "can_edit_contacts": lab_ready and lab_profile.can_manage(request.user),
            "groups": groups,
            "toc": toc,
            "history": contact_history(),
        },
    )
