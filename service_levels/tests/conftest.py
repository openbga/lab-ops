"""Fixtures for clock and service-level tests. All data is synthetic.

`frozen` sets the time every service sees (django.utils.timezone.now, which auto_now fields use
too). Status changes by the desk are driven by `set_status`, which
does what those services do: change the status in a transaction and call the clock hook.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from clients.models import Client
from service_levels import hooks
from taxonomy.models import RequestCategory
from teams.models import Team

PASSWORD = "test-password-729!"
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.TIME_ZONE = "America/New_York"


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
    return make_user("setup_admin", "admin")


@pytest.fixture
def agent(make_user):
    return make_user("agent", "client_services")


@pytest.fixture
def frozen(monkeypatch):
    """frozen(datetime) sets the current time; frozen.now is it. Starts Monday 2026-11-16 08:00."""

    class Clock:
        now = datetime(2026, 11, 16, 8, tzinfo=NY)

        def __call__(self, moment):
            self.now = moment
            return moment

    clock = Clock()
    monkeypatch.setattr(timezone, "now", lambda: clock.now)
    return clock


@pytest.fixture
def team(db):
    return Team.objects.filter(is_default=True).first() or Team.objects.create(
        slug="client-services", name="Client services", is_default=True
    )


@pytest.fixture
def group(db):
    return RequestCategory.objects.create(key="status-results", name="Status and results")


@pytest.fixture
def category(group):
    return RequestCategory.objects.create(
        key="missing-result", name="Missing result or report", parent=group, client_may_raise=True
    )


@pytest.fixture
def other_category(group):
    return RequestCategory.objects.create(
        key="result-question", name="Result or report question", parent=group
    )


@pytest.fixture
def client_org(db):
    return Client.objects.create(name="Example Family Clinic", account_number="EXCLIN")


@pytest.fixture
def portal_user(make_user, client_org):
    return make_user("portal", client=client_org)


@pytest.fixture
def new_request(agent, category, team):
    from desk.services import create_request

    def make(*, channel="email", actor=None, priority=None, category_=None, **extra):
        return create_request(
            actor=actor or agent,
            category=category_ or category,
            subject="Example missing result",
            first_entry_kind="note",
            first_entry_body="Synthetic test request.",
            channel=channel,
            priority=priority,
            **extra,
        )

    return make


@pytest.fixture
def set_status():
    """Change a request's status, and tell the clock."""
    from desk.models import ServiceRequest

    def change(request, status):
        with transaction.atomic():
            locked = ServiceRequest.objects.select_for_update().get(pk=request.pk)
            old = locked.status
            locked.status = status
            if status in ("resolved", "closed") and locked.resolved_at is None:
                locked.resolved_at = timezone.now()
            if status == "closed":
                locked.closed_at = timezone.now()
            if status not in ("resolved", "closed"):
                locked.resolved_at = None
            locked.save()
            hooks.request_changed(locked, previous={"status": old})
        return locked

    return change


@pytest.fixture
def change_property():
    from desk.models import ServiceRequest

    def change(request, **values):
        with transaction.atomic():
            locked = ServiceRequest.objects.select_for_update().get(pk=request.pk)
            previous = {}
            for name, value in values.items():
                field = "category_id" if name == "category" else name
                previous[field] = getattr(locked, field)
                setattr(locked, name, value)
            locked.save()
            hooks.request_changed(locked, previous=previous)
        return locked

    return change
