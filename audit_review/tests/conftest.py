"""Fixtures for the audit review workspace: a reviewer (a laboratory role and the capability), staff
and portal accounts without it, and tickets of two organizations. All data is synthetic."""

import pytest
from django.contrib.auth.models import Group, Permission

from accounts.models import User
from clients.models import Client
from desk.models import Channel, EntryKind
from desk.services import create_request
from taxonomy.models import RequestCategory

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def review_permission():
    return Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def reviewer(make_user):
    user = make_user("reviewer", "supervisor", first_name="Rae", last_name="Viewer")
    user.user_permissions.add(review_permission())
    return user


@pytest.fixture
def supervisor(make_user):
    return make_user("supervisor", "supervisor", first_name="Sam", last_name="Lead")


@pytest.fixture
def agent(make_user):
    return make_user("agent", "client_services", first_name="Ada", last_name="Agent")


@pytest.fixture
def clinic(db):
    return Client.objects.create(name="Example Clinic", account_number="EXCLIN")


@pytest.fixture
def water(db):
    return Client.objects.create(name="Example Water Authority", account_number="EXWATER")


@pytest.fixture
def category(db):
    group = RequestCategory.objects.create(key="status-and-results", name="Status and results")
    return RequestCategory.objects.create(
        key="missing-result", name="Missing result or report", parent=group
    )


@pytest.fixture
def make_ticket(agent, category):
    def make(client, subject="Example subject"):
        return create_request(
            actor=agent,
            category=category,
            subject=subject,
            first_entry_kind=EntryKind.NOTE,
            first_entry_body="Synthetic first note.",
            channel=Channel.PHONE,
            client=client,
        )

    return make
