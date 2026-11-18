"""Fixtures for request tests: a team, a category tree, organizations with sites and contacts,
staff of each role and portal accounts. All data is synthetic."""

import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from clients.models import Client, ClientContact, ClientSite
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

PASSWORD = "test-password-729!"


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
def agent(make_user):
    return make_user("agent", "client_services")


@pytest.fixture
def accessioning(db):
    return Team.objects.create(slug="accessioning", name="Accessioning")


@pytest.fixture
def quality(db):
    return Team.objects.create(slug="quality", name="Quality")


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="status-and-results", name="Status and results")


@pytest.fixture
def category(group):
    """An ordinary category with no default team: requests go to the default team."""
    return RequestCategory.objects.create(
        key="missing-result", name="Missing result or report", parent=group, client_may_raise=True
    )


@pytest.fixture
def sample_category(group, accessioning):
    return RequestCategory.objects.create(
        key="sample-problem",
        name="Sample problem or recollection",
        parent=group,
        default_team=accessioning,
        default_priority="high",
        client_may_raise=True,
    )


@pytest.fixture
def privacy_category(group, quality):
    return RequestCategory.objects.create(
        key="privacy-request",
        name="Privacy request",
        parent=group,
        default_team=quality,
        is_restricted=True,
    )


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Clinic", account_number="EXCLIN")


@pytest.fixture
def water(db):
    return Client.objects.create(name="Example Water Authority", account_number="EXWATER")


@pytest.fixture
def clinic_site(clinic):
    return ClientSite.objects.create(client=clinic, code="MAIN", name="Main office")


@pytest.fixture
def water_site(water):
    return ClientSite.objects.create(client=water, code="NORTH", name="North plant")


@pytest.fixture
def clinic_contact(clinic):
    return ClientContact.objects.create(
        client=clinic, name="Pat Example", email="pat@clinic.example.invalid"
    )


@pytest.fixture
def clinic_portal(make_user, clinic):
    return make_user("clinic_portal", client=clinic)


@pytest.fixture
def join():
    def add(user, team, lead=False):
        return TeamMembership.objects.create(team=team, user=user, is_lead=lead)

    return add
