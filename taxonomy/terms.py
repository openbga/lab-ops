"""What a request-creating page told the person about a category, carried to the write.

A page that offers categories for a new request (staff intake, the portal's raise page) draws
their protection and routing settings: who may see the requests, whether they are complaints,
which team and priority they go to, whether clients may raise them. The page carries an opaque
digest of those settings per category (`drawn`), and the write compares the chosen category's
current settings with it under the category's row lock (`check`). When they differ, the write is
refused and the page drawn again from the current settings, so protection never ends up weaker
than what the page showed.

The digest is a keyed hash, so a client's page reveals nothing about a category's team or
visibility. A missing or unreadable value is refused like a changed one. `settings_of` is the
shared list of facts, for other pages that carry them in their own token (request_forms).
"""

from django.core.exceptions import ValidationError
from django.utils.crypto import constant_time_compare, salted_hmac

FIELD = "category_terms"
CHANGED = (
    "This category's visibility or routing changed after this page was drawn. Review what the "
    "page says now and submit again."
)
CHANGED_FOR_CLIENT = (
    "The laboratory changed this topic after this page was opened. Check your choice and send "
    "again."
)
_SALT = "openbga.lab_ops.category_terms"


def settings_of(category):
    """The category's protection and routing settings, in a fixed order."""
    return (
        category.pk,
        category.is_restricted,
        category.is_complaint,
        category.default_team_id,
        category.default_priority,
        category.client_may_raise,
        category.is_active,
    )


def _digest(category):
    facts = "|".join(str(value) for value in settings_of(category))
    return salted_hmac(_SALT, facts, algorithm="sha256").hexdigest()[:32]


def drawn(categories):
    """The value a page carries in its FIELD input for the categories it offers."""
    return " ".join(f"{category.pk}:{_digest(category)}" for category in categories if category)


def check(current, value, *, portal=False):
    """Refuse unless `value` (what the page carried) holds the digest of `current`, the category
    as it stands now, read under its row lock."""
    expected = _digest(current)
    for entry in (value or "").split():
        pk, _, digest = entry.partition(":")
        if pk == str(current.pk) and constant_time_compare(digest, expected):
            return
    raise ValidationError({"category": CHANGED_FOR_CLIENT if portal else CHANGED})
