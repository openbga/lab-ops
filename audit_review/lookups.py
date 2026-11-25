"""The Person filter's remote option list (core.lookups): every account, staff or client portal,
active or not, for holders of the review capability only."""

from django.contrib.auth import get_user_model

from core.lookups import Lookup, register

from .permissions import require_reviewer


def _label(account):
    name = account.get_full_name()
    text = f"{name} ({account.username})" if name else account.username
    if account.client_id is not None:
        text += f" · {account.client.name}"
    if not account.is_active:
        text += " · inactive"
    return text


def _accounts(request):
    require_reviewer(request.user)
    return get_user_model().objects.select_related("client")


register(
    Lookup(
        "review-accounts",
        _accounts,
        ("username", "first_name", "last_name", "email"),
        ("first_name", "last_name", "username", "pk"),
        label=_label,
    )
)
