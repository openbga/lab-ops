"""Demo reset eligibility and published account details."""

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from accounts.models import User
from accounts.setup import setup_lab
from clients.models import Client
from core import demo
from core.management.commands import reset_demo, seed_demo
from core.models import Announcement, AuditEvent, Lab

pytestmark = pytest.mark.django_db

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


def check():
    reset_demo.Command()._require_demo_laboratory()


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    call_command("seed_demo", verbosity=0)
    return Lab.objects.get()


# what may be erased


def test_a_freshly_migrated_database_counts_as_empty():
    assert demo.nonempty_tables() == []
    check()


def test_reset_preflight_requires_empty_business_tables():
    assert not Lab.objects.exists() and not User.objects.exists()
    Client.objects.create(name="Synthetic independent lab customer", account_number="R4")
    with pytest.raises(CommandError, match="not empty"):
        check()


def test_extra_rows_in_migration_tables_are_business_records():
    from teams.models import Team

    Team.objects.create(slug="bench", name="Bench")
    with pytest.raises(CommandError, match="teams_team"):
        check()


def test_reset_preflight_distinguishes_seed_from_setup_lab():
    setup_lab(
        lab_name=seed_demo.DEMO_LAB_NAME,
        username="synthetic_owner",
        email="owner@example.invalid",
        password="Review-only-fixture-44",
    )
    with pytest.raises(CommandError, match="not created by the demonstration seed"):
        check()


def test_seeding_a_setup_lab_database_does_not_seal_it(monkeypatch):
    setup_lab(
        lab_name=seed_demo.DEMO_LAB_NAME,
        username="synthetic_owner",
        email="owner@example.invalid",
        password="Review-only-fixture-44",
    )
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    call_command("seed_demo", verbosity=0)
    assert not demo.is_sealed(Lab.objects.get())
    with pytest.raises(CommandError):
        check()
    with pytest.raises(CommandError, match="not created by the demonstration seed"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()


def test_the_seed_seals_the_laboratory_it_creates(seeded):
    assert demo.is_sealed(seeded)
    check()
    event = AuditEvent.objects.get(action=demo.SEAL_ACTION)
    assert event.actor.username == "demo_admin" and len(event.after["seal"]) == 64


def test_a_forged_seal_or_a_renamed_laboratory_is_refused(seeded):
    seeded.name = "Example Reference Laboratory (renamed)"
    Lab.objects.filter(pk=seeded.pk).update(name=seeded.name)
    with pytest.raises(CommandError):
        check()
    Lab.objects.filter(pk=seeded.pk).update(name=seed_demo.DEMO_LAB_NAME)
    with override_settings(SECRET_KEY="another-deployment-key-entirely-0123456789"):
        with pytest.raises(CommandError, match="seal"):
            check()


def test_a_rotated_key_keeps_the_demonstration_resettable(seeded, settings):
    old = settings.SECRET_KEY
    with override_settings(
        SECRET_KEY="the-new-deployment-key-0123456789abcdef", SECRET_KEY_FALLBACKS=[old]
    ):
        check()


def test_reset_only_selects_the_projects_numbering_sequences():
    with connection.cursor() as cursor:
        cursor.execute("CREATE SEQUENCE review_auxiliary_numbers")
        cursor.execute("CREATE SEQUENCE desk_example_numbers")
        cursor.execute(reset_demo.UNOWNED_SEQUENCES, [reset_demo.project_sequence_patterns()])
        candidates = {row[0] for row in cursor.fetchall()}
    assert "review_auxiliary_numbers" not in candidates
    assert "desk_example_numbers" in candidates


# what may be published


def test_announcement_matches_current_role(seeded):
    technician = User.objects.get(username="demo_technical")
    technician.groups.add(Group.objects.get(name="admin"))
    with pytest.raises(CommandError, match="REFUSING TO PUBLISH"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()
    assert not User.objects.get(username="demo_technical").credentials_locked  # rolled back


@pytest.mark.parametrize(
    "drift",
    ["capability_on_account", "capability_on_group", "superuser", "client_kind", "missing_role"],
)
def test_any_authority_beyond_the_announcement_refuses(seeded, drift):
    capability = Permission.objects.get(codename="view_patient_information")
    technician = User.objects.get(username="demo_technical")
    if drift == "capability_on_account":
        technician.user_permissions.add(capability)
    elif drift == "capability_on_group":
        Group.objects.get(name="technical").permissions.add(capability)
    elif drift == "superuser":
        User.objects.filter(pk=technician.pk).update(is_superuser=True, is_staff=True)
    elif drift == "client_kind":
        User.objects.filter(username="demo_client").update(credentials_locked=False)
        Group.objects.get(name="account_manager").user_set.add(
            User.objects.get(username="demo_client")
        )
    elif drift == "missing_role":
        technician.groups.clear()
    with pytest.raises(CommandError, match="REFUSING TO PUBLISH"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()


def test_the_clean_seed_announces_exactly_its_accounts(seeded):
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    body = Announcement.objects.get().body
    for name, _ in seed_demo.ANNOUNCED_ACCOUNTS:
        assert name in body
        assert User.objects.get(username=name).credentials_locked
    assert "demo_admin" not in body
