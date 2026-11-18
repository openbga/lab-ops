"""Demo reset locking and effective access of published accounts."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections, transaction

from accounts.models import User
from clients.models import Client
from core import demo
from core.management.commands import reset_demo, seed_demo
from core.models import Announcement, Lab
from desk.models import ServiceRequest
from desk.selectors import staff_requests
from taxonomy.models import RequestCategory
from taxonomy.services import update_category
from teams.models import Team

pytestmark = pytest.mark.django_db

SHARED = "Synthetic-shared-fixture"


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")


def other_connection(function):
    def run():
        try:
            return function()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=30)


def try_to_write(username):
    """Another connection's write, giving up after a short wait instead of queueing."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '300ms'")
            User.objects.create_user(username, email=f"{username}@example.invalid")
    except OperationalError:
        return "blocked"
    return "written"


def refused(match):
    with pytest.raises(CommandError, match="REFUSING TO PUBLISH") as raised:
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()
    assert match in str(raised.value)


# the seal and a concurrent writer


@pytest.mark.django_db(transaction=True)
def test_seed_does_not_claim_a_concurrent_unrelated_account(demo_env, monkeypatch):
    """Another connection writes an account right after the seed's scan.
    The seed holds every application table against writers from before the scan until it
    commits, so that write cannot land inside the seal (it waits; here it gives up)."""
    original = demo.nonempty_tables
    attempts = []

    def scan():
        tables = original()
        assert not tables
        attempts.append(other_connection(lambda: try_to_write("independent_user")))
        return tables

    monkeypatch.setattr(demo, "nonempty_tables", scan)
    call_command("seed_demo", verbosity=0)
    assert attempts == ["blocked"]
    assert not User.objects.filter(username="independent_user").exists()
    assert demo.is_sealed(Lab.objects.get())
    reset_demo.Command()._require_demo_laboratory()


@pytest.mark.django_db(transaction=True)
def test_application_tables_stay_locked_until_the_transaction_ends():
    with transaction.atomic():
        demo.lock_application_tables()
        assert other_connection(lambda: try_to_write("write_during_reset")) == "blocked"
        assert other_connection(lambda: Team.objects.count()) >= 0  # reads still pass
    assert other_connection(lambda: try_to_write("write_after_reset")) == "written"


@pytest.mark.django_db(transaction=True)
def test_the_lock_needs_a_transaction():
    with pytest.raises(RuntimeError, match="needs a transaction"):
        demo.lock_application_tables()


def test_reset_decides_again_under_the_lock(monkeypatch, demo_env):
    """A record written between the reset's first check and its erase is not erased: the reset
    checks again once every other writer is shut out."""
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    original = demo.lock_application_tables

    def written_meanwhile():
        Client.objects.create(name="Synthetic late organization", account_number="R6LATE")
        original()

    monkeypatch.setattr(demo, "lock_application_tables", written_meanwhile)
    with pytest.raises(CommandError, match="not empty"):
        call_command("reset_demo", interactive=False, verbosity=0)


# what the announcement's sentences claim


def test_public_technical_scope_after_category_restriction(demo_env):
    call_command("seed_demo", verbosity=0)
    tech = User.objects.get(username="demo_technical")
    admin = User.objects.get(username="demo_admin")
    record = ServiceRequest.objects.filter(
        team__slug="technical", category__is_restricted=False
    ).first()
    assert record is not None
    assert not staff_requests(tech).filter(category__is_restricted=True).exists()
    category = record.category
    update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Synthetic visibility change",
        is_restricted=True,
    )
    assert staff_requests(tech).filter(pk=record.pk, category__is_restricted=True).exists()
    refused("demo_technical can read restricted tickets")


def test_a_restricted_category_routed_to_the_team_refuses_before_a_request_arrives(demo_env):
    call_command("seed_demo", verbosity=0)
    category = RequestCategory.objects.filter(is_restricted=True).first()
    tech = User.objects.get(username="demo_technical")
    RequestCategory.objects.filter(pk=category.pk).update(
        default_team=Team.objects.get(slug="technical")
    )
    assert not staff_requests(tech).filter(category__is_restricted=True).exists()
    refused("demo_technical can read restricted tickets")


def test_patient_information_through_a_role_group_refuses(demo_env):
    call_command("seed_demo", verbosity=0)
    Group.objects.get(name="client_services").permissions.add(
        Permission.objects.get(codename="view_patient_information")
    )
    refused("demo_client_services can read patient information")


def test_a_widened_portal_account_reads_beyond_its_plant(demo_env):
    from clients.models import PortalAccess

    call_command("seed_demo", verbosity=0)
    water = User.objects.get(username="demo_client_water")
    PortalAccess.objects.filter(user=water).update(whole_organization=True)
    expected = seed_demo.seeded_reach("demo_client_water")
    assert seed_demo.held_reach(water)["sites"] != expected["sites"]
    refused("demo_client_water can read sites")


def test_the_clean_seed_reads_exactly_what_it_announces(demo_env):
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    body = Announcement.objects.get().body
    for name, sentence in seed_demo.ANNOUNCED_ACCOUNTS:
        assert f"{name}: {sentence}" in body
        user = User.objects.get(username=name)
        assert seed_demo.held_reach(user) == seed_demo.seeded_reach(name), name
    supervisor = seed_demo.seeded_reach("demo_supervisor")
    assert supervisor == {"patient information": True, "restricted tickets": True}
    assert seed_demo.seeded_reach("demo_technical")["restricted tickets"] is False
    assert seed_demo.seeded_reach("demo_client_water")["sites"] == {"NORTH"}
    whole = seed_demo.seeded_reach("demo_client")
    assert whole["organizations"] == {"EXCLIN"} and len(whole["sites"]) >= 1
