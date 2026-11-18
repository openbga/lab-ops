"""Fixtures for team tests: staff of each role, a few teams and members. All data is synthetic.
(The desk's fixtures in desk/tests/conftest.py are not shared across apps by pytest.)"""

import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from clients.models import Client
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership, ensure_default_team

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
def admin(make_user):
    return make_user("admin_user", "admin")


@pytest.fixture
def supervisor(make_user):
    return make_user("supervisor_user", "supervisor")


@pytest.fixture
def agent(make_user):
    return make_user("agent", "client_services")


@pytest.fixture
def technician(make_user):
    return make_user("technician", "technical")


@pytest.fixture
def portal_account(make_user, db):
    return make_user("portal_user", client=Client.objects.create(name="Example Clinic"))


@pytest.fixture
def default_team(db):
    return ensure_default_team()


@pytest.fixture
def quality(db):
    return Team.objects.create(slug="quality", name="Quality")


@pytest.fixture
def join():
    def add(user, team, lead=False):
        return TeamMembership.objects.create(team=team, user=user, is_lead=lead)

    return add


@pytest.fixture
def category(db):
    group = RequestCategory.objects.create(key="status-and-results", name="Status and results")
    return RequestCategory.objects.create(key="missing-result", name="Missing result", parent=group)
