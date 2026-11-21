"""The top bar's What's new link with its unread indicator.

  {% load whats_new %}{% whats_new_button %}

Shown to every signed-in staff account (with or without a lab role); client accounts have no What's
new. The count is the entries in changelog.d/ newer than what this user saw when they last opened
the page (core.changelog.unread and the whats_new.seen preference), so it needs no query of its own.
An account without a seen mark yet (a new account) has nothing new: the first shell it sees records
the newest entries as seen and shows no count. A page drawn inside one reading of the database
(core.concurrency.one_reading) records the mark after that reading ends, so the page writes
nothing inside it.
"""

from django import template
from django.db import IntegrityError
from django.urls import reverse

from core import changelog, preferences
from core.concurrency import after_reading

register = template.Library()


@register.inclusion_tag("includes/whats_new_button.html", takes_context=True)
def whats_new_button(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.client_id is not None:
        return {"show": False}
    entries = changelog.entries()
    marker = preferences.for_request(request).get(preferences.WHATS_NEW_KEY)
    if marker is None:
        count = 0
        seen = changelog.seen_marker(entries)
        if seen is not None and request.method != "HEAD":  # a HEAD response carries no page
            drawn_for = user.pk

            def record():
                # Only for the account the page was drawn for: if the request's account changed
                # before the page's reading ended, nobody saw this page as the new one.
                if getattr(request.user, "pk", None) != drawn_for:
                    return
                # Only while there is none: a mark saved meanwhile (another tab's first page, or
                # opening What's new) is newer than this page's reading and stays.
                try:
                    preferences.save_if_absent(request, preferences.WHATS_NEW_KEY, seen)
                except ValueError:
                    pass  # too many saved preferences: the next page tries again
                except IntegrityError as error:
                    # The account was removed while the page was drawn: nobody to record it for.
                    # Any other failure, also of another reference, is raised.
                    if not preferences.account_removed(error, drawn_for):
                        raise

            after_reading(record)
    else:
        count = len(changelog.unread(entries, marker))
    match = getattr(request, "resolver_match", None)
    return {
        "show": True,
        "url": reverse("whats-new"),
        "count": count,
        "badge": "99+" if count > 99 else str(count),
        "current": match is not None and match.view_name == "whats-new",
    }
