"""Demo seed ownership, baseline records and published permissions."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User
from clients.models import ClientContact, ClientSite
from clients.services import set_portal_access
from core import demo
from core.management.commands import reset_demo, seed_demo
from core.models import Announcement, Lab
from desk.selectors import staff_requests
from links.models import ExternalSystem
from teams.models import Team, TeamMembership
from teams.services import add_member

pytestmark = pytest.mark.django_db

SHARED = "Synthetic-shared-fixture"


def check():
    reset_demo.Command()._require_demo_laboratory()


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")


def refused_announcement(captured_name):
    with pytest.raises(CommandError, match="REFUSING TO PUBLISH") as refused:
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()
    assert captured_name in str(refused.value)
    return str(refused.value)


# the seal needs an empty start


def test_seed_cannot_turn_unrelated_accounts_into_reset_eligible_data(demo_env, capsys):
    account = User.objects.create_user("unrelated_fixture", email="fixture@example.invalid")
    assert not Lab.objects.exists()
    with pytest.raises(CommandError):
        check()
    call_command("seed_demo", verbosity=0)
    assert "without its seal" in capsys.readouterr().err
    assert User.objects.filter(pk=account.pk).exists()
    assert not demo.is_sealed(Lab.objects.get())
    with pytest.raises(CommandError, match="not created by the demonstration seed"):
        check()
    with pytest.raises(CommandError, match="not created by the demonstration seed"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)


def test_seed_on_other_leftover_records_is_not_sealed(demo_env):
    Team.objects.create(slug="synthetic-bench", name="Synthetic bench")
    call_command("seed_demo", verbosity=0)
    assert not demo.is_sealed(Lab.objects.get())


def test_seed_on_an_empty_database_is_sealed_and_says_nothing(demo_env, capsys):
    call_command("seed_demo", verbosity=0)
    assert demo.is_sealed(Lab.objects.get())
    assert capsys.readouterr().err == ""
    check()


# starter rows count only as the migration made them


def test_changed_migration_starter_is_business_data():
    system = ExternalSystem.objects.get(key="lis")
    system.base_url = "https://synthetic.example.invalid"
    system.revision += 1
    system.save()
    assert not Lab.objects.exists() and not User.objects.exists()
    with pytest.raises(CommandError, match="links_externalsystem"):
        check()


def test_renamed_default_team_is_business_data():
    Team.objects.filter(slug="client-services").update(name="Synthetic front desk")
    assert demo.nonempty_tables() == ["teams_team"]


def test_removed_starter_rows_still_count_as_empty():
    ExternalSystem.objects.all().delete()
    assert demo.nonempty_tables() == []


def test_the_starters_match_what_the_migrations_create():
    assert ExternalSystem.objects.count() == 1 and Team.objects.count() == 1
    for starter in demo.migration_starters():
        stored = type(starter).objects.get()
        assert demo._content(stored) == demo._content(starter)


# everything that decides what an announced account can read


def test_announcement_checks_advertised_portal_sites(demo_env):
    call_command("seed_demo", verbosity=0)
    user = User.objects.get(username="demo_client_water")
    grant = user.portal_access
    set_portal_access(
        actor=User.objects.get(username="demo_admin"),
        user_id=user.pk,
        expected_client_id=user.client_id,
        expected_revision=grant.revision,
        contact=grant.contact,
        whole_organization=True,
        reason="Synthetic scope change",
    )
    message = refused_announcement("demo_client_water")
    assert "portal" in message


def test_announcement_checks_another_site_and_contact(demo_env):
    call_command("seed_demo", verbosity=0)
    user = User.objects.get(username="demo_client_water")
    grant = user.portal_access
    others = ClientSite.objects.filter(client_id=user.client_id).exclude(
        pk__in=grant.sites.values("pk")
    )
    contact = (
        ClientContact.objects.filter(client_id=user.client_id).exclude(pk=grant.contact_id).first()
    )
    assert others.exists() and contact is not None
    set_portal_access(
        actor=User.objects.get(username="demo_admin"),
        user_id=user.pk,
        expected_client_id=user.client_id,
        expected_revision=grant.revision,
        contact=contact,
        whole_organization=False,
        sites=[*grant.sites.all(), others.first()],
        reason="Synthetic scope change",
    )
    refused_announcement("demo_client_water")


def test_announcement_checks_restricted_team_membership(demo_env):
    call_command("seed_demo", verbosity=0)
    user = User.objects.get(username="demo_technical")
    assert not staff_requests(user).filter(category__is_restricted=True).exists()
    add_member(
        actor=User.objects.get(username="demo_admin"),
        team=Team.objects.get(slug="quality"),
        user=user,
    )
    assert staff_requests(user).filter(category__is_restricted=True).exists()
    message = refused_announcement("demo_technical")
    assert "quality" in message


def test_announcement_checks_team_lead_status(demo_env):
    call_command("seed_demo", verbosity=0)
    TeamMembership.objects.filter(user__username="demo_client_services").update(is_lead=True)
    refused_announcement("demo_client_services")


def test_announced_authority_is_derived_from_the_seed():
    water = seed_demo.seeded_authority("demo_client_water")
    assert water["portal"] == ("EXWATER", "sampler@water.example.invalid", frozenset({"NORTH"}))
    assert water["roles"] == set() and water["teams"] == set()
    assert seed_demo.seeded_authority("demo_client")["portal"][2] is None
    technical = seed_demo.seeded_authority("demo_technical")
    assert technical == {
        "roles": {"technical"},
        "capabilities": set(),
        "teams": {("technical", True)},
        "portal": None,
    }
    assert seed_demo.seeded_authority("demo_supervisor")["capabilities"] == {
        "view_patient_information"
    }


def test_the_clean_seed_holds_exactly_what_it_announces(demo_env):
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    for name, _ in seed_demo.ANNOUNCED_ACCOUNTS:
        user = User.objects.get(username=name)
        assert seed_demo.held_authority(user) == seed_demo.seeded_authority(name)
    assert Announcement.objects.exists()
