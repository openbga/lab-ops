"""The top bar's What's new link with its unread indicator.

  {% load whats_new %}{% whats_new_button %}

Shown to every signed-in staff account (with or without a lab role); client accounts have no What's
new. The count is the entries in changelog.d/ newer than what this user saw when they last opened
the page (core.changelog.unread and the whats_new.seen preference), so it needs no query of its own.
An account without a seen mark yet (a new account) has nothing new: the first shell it sees records
the newest entries as seen and shows no count.
"""

from django import template
from django.urls import reverse

from core import changelog, preferences

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
        if seen is not None:
            try:
                preferences.save(request, preferences.WHATS_NEW_KEY, seen)
            except ValueError:
                pass  # too many saved preferences: the next page tries again
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
