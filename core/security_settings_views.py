"""The Security settings page (the admin role): the laboratory's security and privacy contacts,
what protects sign-in, sessions and patient information as read from the running deployment
(core.security_settings), and the recent changes to the contacts.

The page states facts and who sets each one; it changes nothing itself. The contacts are edited
through the laboratory profile's contact form, which returns here when opened from this page.
"""

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render

from accounts.roles import require_role

from . import lab_profile, security_settings
from .concurrency import one_reading
from .lab_profile_views import contact_history, reading_account
from .models import Lab


@transaction.non_atomic_requests  # one_reading opens its own transaction (ATOMIC_REQUESTS)
@login_required
def security_settings_page(request):
    # One reading for the role check and the whole page: the access decision, the contacts,
    # their history and the counts describe one state. The page is drawn after the reading, so
    # what drawing writes (What's new, preferences) is not done inside it.
    with one_reading():
        reader = reading_account(request)
        require_role(reader, *security_settings.SECURITY_ROLES)
        lab_ready = Lab.objects.filter(pk=1).exists()
        groups = security_settings.in_force()
        context = {
            "page_title": "Security settings",
            "contacts": lab_profile.contacts(),
            # The contact form needs the laboratory row; without it there is nothing to edit.
            "can_edit_contacts": lab_ready and lab_profile.can_manage(reader),
            "groups": groups,
            "toc": [
                ("contacts-heading", "Security and privacy contacts"),
                *[(f"security-{group.key}-heading", group.title) for group in groups],
                ("about-heading", "About this page"),
                ("history-heading", "History"),
            ],
            "history": contact_history(),
        }
    return render(request, "core/security_settings.html", context)
