"""Fixtures for request form tests: staff of each kind, a portal account, a category tree, and a
pickup form with one field of each audience and kind that matters. All data is synthetic."""

import pytest
from django.contrib.auth.models import Group, Permission

from accounts.models import User
from clients.models import Client, ClientSite, PortalAccess
from request_forms.models import Audience, Catalog, CatalogItem, FieldKind, RequestTemplate
from taxonomy.models import RequestCategory
from teams.models import Team

PASSWORD = "test-password-729!"
PATIENT_SENTINEL = "PT-SENTINEL-77123"


def revision_of(template):
    """The template's current revision (setup's stale check)."""
    from request_forms.models import RequestTemplate

    return RequestTemplate.objects.values_list("revision", flat=True).get(pk=template.pk)


def definition_of(obj):
    """The definition token a page drawn now carries (request_forms.definition), for a template
    or for a request raised with one (its form's template)."""
    from request_forms import definition
    from request_forms.models import FilledForm, RequestTemplate

    if not isinstance(obj, RequestTemplate):
        obj = FilledForm.objects.select_related("template").get(request_id=obj.pk).template
    return definition.token(obj)


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def admin(make_user):
    return make_user("forms_admin", "admin")


@pytest.fixture
def agent(make_user):
    return make_user("forms_agent", "client_services")


@pytest.fixture
def capable(make_user):
    user = make_user("forms_capable", "supervisor")
    user.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )
    return user


@pytest.fixture
def logistics(db):
    return Team.objects.create(slug="logistics", name="Logistics")


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="supplies-logistics", name="Supplies and logistics")


@pytest.fixture
def pickup_category(group, logistics):
    return RequestCategory.objects.create(
        key="pickup",
        name="Pickup or missed pickup",
        parent=group,
        default_team=logistics,
        client_may_raise=True,
    )


@pytest.fixture
def staff_category(group):
    return RequestCategory.objects.create(
        key="internal-check", name="Internal check", parent=group, client_may_raise=False
    )


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Clinic", account_number="EXCLIN")


@pytest.fixture
def other_org(db):
    return Client.objects.create(name="Example Water Authority", account_number="EXWATER")


@pytest.fixture
def clinic_site(clinic):
    return ClientSite.objects.create(client=clinic, code="MAIN", name="Main office")


@pytest.fixture
def portal_user(make_user, clinic):
    user = make_user("clinic_portal", client=clinic)
    PortalAccess.objects.create(user=user, client=clinic, whole_organization=True)
    return user


@pytest.fixture
def colleague(make_user, clinic):
    """Another portal account of the same organization, seeing the whole organization."""
    user = make_user("clinic_colleague", client=clinic)
    PortalAccess.objects.create(user=user, client=clinic, whole_organization=True)
    return user


@pytest.fixture
def outsider(make_user, other_org):
    user = make_user("water_portal", client=other_org)
    PortalAccess.objects.create(user=user, client=other_org, whole_organization=True)
    return user


@pytest.fixture
def catalog(db):
    catalog = Catalog.objects.create(key="supplies", name="Supplies")
    CatalogItem.objects.create(
        catalog=catalog, name="Transport bags", code="TEST-BAG", unit="pack", position=10
    )
    CatalogItem.objects.create(catalog=catalog, name="Shipping box", max_quantity=5, position=20)
    CatalogItem.objects.create(catalog=catalog, name="Retired item", is_active=False)
    return catalog


@pytest.fixture
def pickup(pickup_category, catalog):
    """A pickup form: a required client number, a client date and time, a staff-only text, a
    shown text, an inactive-by-default patient field switched on, and an item list."""
    template = RequestTemplate.objects.create(
        key="pickup",
        name="Pickup",
        client_label="Request a pickup",
        category=pickup_category,
        offered_in_portal=True,
        featured=True,
        position=10,
    )
    fields = [
        ("count", "Number of coolers", FieldKind.NUMBER, True, Audience.CLIENT, False),
        ("ready-at", "Ready at", FieldKind.DATETIME, False, Audience.CLIENT, False),
        ("courier", "Courier", FieldKind.SHORT_TEXT, False, Audience.STAFF, False),
        ("tracking", "Tracking", FieldKind.SHORT_TEXT, False, Audience.SHOWN, False),
        ("patient", "Patient identifier", FieldKind.SHORT_TEXT, False, Audience.CLIENT, True),
        ("items", "Items", FieldKind.ITEMS, False, Audience.CLIENT, False),
        ("handling", "Handling", FieldKind.CHOICE, False, Audience.CLIENT, False),
    ]
    for position, (key, label, kind, required, audience, patient) in enumerate(fields, 1):
        template.fields.create(
            key=key,
            label=label,
            kind=kind,
            required=required,
            audience=audience,
            holds_patient_information=patient,
            position=position * 10,
            min_value=1 if kind == FieldKind.NUMBER else None,
            catalog=catalog if kind == FieldKind.ITEMS else None,
            choices=["Room temperature", "Frozen"] if kind == FieldKind.CHOICE else [],
        )
    return template


@pytest.fixture
def staff_template(staff_category):
    template = RequestTemplate.objects.create(
        key="internal",
        name="Internal check form",
        category=staff_category,
        offered_in_portal=False,
    )
    template.fields.create(key="note", label="Note", kind=FieldKind.SHORT_TEXT)
    return template
