"""Remote option lists the request forms and filters use (core.lookups). Staff only; a site or
contact is labelled with its organization so a user can find it by either name. The forms still
check that a site or contact belongs to the request's organization."""

from core.lookups import Lookup, model, register, staff


def _with_client(obj):
    return f"{obj.name} · {obj.client.name}"


register(
    Lookup(
        "desk-sites",
        staff(
            lambda request: (
                model("clients.ClientSite")
                .objects.filter(is_active=True, client__is_active=True)
                .select_related("client")
            )
        ),
        ("name", "code", "client__name", "client__account_number"),
        ("client__name", "name", "pk"),
        label=_with_client,
    )
)
register(
    Lookup(
        "desk-contacts",
        staff(
            lambda request: (
                model("clients.ClientContact")
                .objects.filter(is_active=True, client__is_active=True)
                .select_related("client")
            )
        ),
        ("name", "role", "email", "client__name", "client__account_number"),
        ("client__name", "name", "pk"),
        label=_with_client,
    )
)
