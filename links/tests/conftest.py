"""Fixtures for link tests. All data is synthetic: keys carry TEST, addresses .invalid."""

import pytest
from django.contrib.auth.models import Group, Permission

from accounts.models import User
from clients.models import Client
from links.models import ExternalSystem
from links.services import add_link
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

PASSWORD = "test-password-729!"
PATIENT_KEY = "PT-TEST-00004821"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def make_user(db):
    def make(name, *roles, capability=False, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        if capability:
            user.user_permissions.add(
                Permission.objects.get(
                    codename="view_patient_information", content_type__app_label="accounts"
                )
            )
        return user

    return make


@pytest.fixture
def agent(make_user):
    """Staff without the patient capability."""
    return make_user("agent", "client_services")


@pytest.fixture
def holder(make_user):
    """Staff with the patient capability."""
    return make_user("holder", "client_services", capability=True)


@pytest.fixture
def admin(make_user):
    """Admin role, superuser too: neither implies the capability."""
    return make_user("setup_admin", "admin", is_superuser=True, is_staff=True)


@pytest.fixture
def team(db):
    return Team.objects.filter(is_default=True).first() or Team.objects.create(
        slug="client-services", name="Client services", is_default=True
    )


@pytest.fixture
def quality(db):
    return Team.objects.create(slug="quality", name="Quality")


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="status-results", name="Status and results")


@pytest.fixture
def category(group):
    return RequestCategory.objects.create(
        key="missing-result", name="Missing result or report", parent=group, client_may_raise=True
    )


@pytest.fixture
def restricted(group, quality):
    return RequestCategory.objects.create(
        key="privacy-request",
        name="Privacy request",
        parent=group,
        default_team=quality,
        is_restricted=True,
    )


@pytest.fixture
def client_org(db):
    return Client.objects.create(name="Example Family Clinic", account_number="EXCLIN")


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis",
        defaults={"name": "Laboratory information system", "kind": "lis"},
    )[0]


@pytest.fixture
def lims(db):
    return ExternalSystem.objects.create(
        key="lims", name="Example LIMS", kind="lims", base_url="https://lims.example.invalid/app/"
    )


@pytest.fixture
def make_request(agent, category, team, client_org):
    from desk.services import create_request

    def make(*, actor=None, category_=None, **extra):
        extra.setdefault("client", client_org)
        return create_request(
            actor=actor or agent,
            category=category_ or category,
            subject="Example missing result",
            first_entry_kind="note",
            first_entry_body="Synthetic test request.",
            channel="email",
            **extra,
        )

    return make


@pytest.fixture
def service_request(make_request):
    return make_request()


@pytest.fixture
def quality_member(make_user, quality):
    user = make_user("quality_member", "technical", capability=True)
    TeamMembership.objects.create(team=quality, user=user)
    return user


@pytest.fixture
def person(holder, lis, service_request):
    attachment, _ = add_link(
        actor=holder,
        request_id=service_request.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    return attachment
