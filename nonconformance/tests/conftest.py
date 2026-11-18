"""Fixtures for nonconformance tests: staff of each role, a portal account, a complaint ticket and
a restricted ticket, and the module switched on. All data is synthetic."""

import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from clients.models import Client
from core.models import LabModule
from desk import services as desk_services
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def quick_password_hasher(settings):
    """Synthetic accounts only: a fast hasher keeps hundreds of fixture accounts quick."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


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
def module_on(db):
    return LabModule.objects.create(key="nonconformance", is_enabled=True, revision=1)


@pytest.fixture
def admin(make_user):
    return make_user("admin", "admin")


@pytest.fixture
def lead(make_user):
    return make_user("lead", "supervisor")


@pytest.fixture
def lead2(make_user):
    return make_user("lead2", "supervisor")


@pytest.fixture
def agent(make_user):
    return make_user("agent", "client_services")


@pytest.fixture
def tech(make_user):
    return make_user("tech", "technical")


@pytest.fixture
def no_role(make_user):
    return make_user("norole")


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Clinic", account_number="EXCLIN")


@pytest.fixture
def portal(make_user, clinic):
    return make_user("clinic_portal", client=clinic)


@pytest.fixture
def quality_team(db):
    return Team.objects.create(slug="quality", name="Quality")


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="service", name="Service")


@pytest.fixture
def complaint_category(group):
    return RequestCategory.objects.create(
        key="complaint", name="Complaint", parent=group, is_complaint=True
    )


@pytest.fixture
def plain_category(group):
    return RequestCategory.objects.create(key="question", name="Question", parent=group)


@pytest.fixture
def restricted_category(group, quality_team):
    return RequestCategory.objects.create(
        key="privacy", name="Privacy", parent=group, default_team=quality_team, is_restricted=True
    )


@pytest.fixture
def complaint(agent, complaint_category, clinic):
    """A complaint ticket assigned to `agent` on the default team."""
    request = desk_services.create_request(
        actor=agent,
        category=complaint_category,
        subject="Results late three times",
        first_entry_kind="note",
        first_entry_body="Practice manager reports late results.",
        channel="phone",
        client=clinic,
    )
    TeamMembership.objects.get_or_create(team=request.team, user=agent)
    return desk_services.assign(
        actor=agent,
        request=request,
        expected_revision=request.revision,
        team=request.team,
        assignee=agent,
        reason="Taking it.",
    )


@pytest.fixture
def plain_ticket(agent, plain_category, clinic):
    return desk_services.create_request(
        actor=agent,
        category=plain_category,
        subject="Question about a report",
        first_entry_kind="note",
        first_entry_body="Caller asked about report timing.",
        channel="phone",
        client=clinic,
    )


@pytest.fixture
def restricted_ticket(lead, restricted_category):
    return desk_services.create_request(
        actor=lead,
        category=restricted_category,
        subject="Privacy matter",
        first_entry_kind="note",
        first_entry_body="Synthetic restricted note.",
        channel="internal",
    )
