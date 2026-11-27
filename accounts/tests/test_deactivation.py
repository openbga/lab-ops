"""What an account held when it was deactivated (accounts.deactivation): the record every
deactivating save writes, the Deactivate page of the admin console and the account page's
Deactivation line. Synthetic accounts only."""

import json
import threading
from datetime import timedelta

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as BrowserClient
from django.urls import reverse
from django.utils import timezone

from accounts import deactivation
from accounts.models import AccountSession, CapabilityEnd, Deactivation, User
from clients.models import Client, ClientSite, PortalAccess
from core.models import AuditEvent
from desk.models import ServiceRequest
from desk.tests.test_portal import raise_staff
from taxonomy.models import RequestCategory
from teams.models import Team, TeamMembership

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def make_owner(name="dar-owner"):
    return User.objects.create_superuser(name, f"{name}@example.invalid", PASSWORD)


@pytest.fixture
def owner(db):
    return make_owner()


def capability(codename):
    return Permission.objects.get(content_type__app_label="accounts", codename=codename)


@pytest.fixture
def team(db):
    return Team.objects.create(slug="dar-bench", name="Bench chemistry")


@pytest.fixture
def person(db, team):
    """A staff account with a role, a capability given directly with a last day, a capability
    through its role and a team it leads."""
    user = User.objects.create_user("dar-person", "dar-person@example.invalid", PASSWORD)
    supervisor, _ = Group.objects.get_or_create(name="supervisor")
    supervisor.permissions.add(capability("review_audit_records"))
    user.groups.add(supervisor)
    user.user_permissions.add(capability("view_patient_information"))
    with transaction.atomic():  # setting an end takes the account's access gate
        CapabilityEnd.objects.create(
            user=user,
            permission=capability("view_patient_information"),
            ends_at=timezone.now() + timedelta(days=30),
        )
    TeamMembership.objects.create(team=team, user=user, is_lead=True)
    return user


@pytest.fixture
def assigned(person, team):
    group = RequestCategory.objects.create(key="dar-group", name="Status and results")
    category = RequestCategory.objects.create(
        key="dar-missing", name="Missing result", parent=group, default_team=team
    )
    organization = Client.objects.create(name="Dar Example Clinic", account_number="DARCLIN")
    return raise_staff(person, category, organization, team=team, assignee=person)


def browser_for(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def page_url(user):
    return reverse("admin:accounts_user_deactivate", args=[user.pk])


def change_url(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def drawn_of(response):
    return response.context["form"]["drawn"].value()


# ------------------------------------------------------------------------------ what it holds


def test_holdings_word_what_a_staff_account_holds_as_the_access_report_does(person):
    held = deactivation.holdings(person.pk)
    assert held["kind"] == "Staff" and held["organization"] == "" and held["portal"] == ""
    assert held["console"] == []
    assert held["roles"] == ["Supervisor"]
    texts = [item["text"] for item in held["capabilities"]]
    notes = [item["note"] for item in held["capabilities"]]
    assert "Can view patient information" in texts
    assert any(note.startswith("Directly, last day ") for note in notes)
    assert "Through the Supervisor role" in notes
    assert held["teams"] == [{"text": "Bench chemistry", "note": "Lead"}]
    assert "dar-person@example.invalid" not in json.dumps(held)


def test_holdings_of_a_portal_account_say_what_it_sees():
    organization = Client.objects.create(name="Dar Water Utility", account_number="DARWAT")
    site = ClientSite.objects.create(client=organization, code="N1", name="North plant")
    portal = User.objects.create_user(
        "dar-portal", "dar-portal@example.invalid", PASSWORD, client=organization
    )
    access = PortalAccess.objects.create(user=portal, client=organization)
    access.sites.add(site)
    held = deactivation.holdings(portal.pk)
    assert held["kind"] == "Client portal" and held["organization"] == "Dar Water Utility"
    assert held["portal"] == "Tickets of North plant, and those it raised"
    assert held["roles"] == [] and held["capabilities"] == [] and held["teams"] == []


# ------------------------------------------------------------------------------ the record


def test_any_save_that_deactivates_records_what_it_held_once(person):
    before = deactivation.holdings(person.pk)
    person.is_active = False
    person.save(update_fields=["is_active"])
    record = Deactivation.objects.get(user=person)
    assert record.held == before and record.reason == "" and record.deactivated_by_id is None
    # Saving it again while inactive deactivates nothing: no second record.
    person.first_name = "Renamed"
    person.save()
    person.save(update_fields=["is_active"])
    assert Deactivation.objects.filter(user=person).count() == 1


def test_a_save_that_does_not_write_is_active_records_nothing(person):
    person.is_active = False
    person.save(update_fields=["first_name"])
    assert not Deactivation.objects.exists()
    assert User.objects.get(pk=person.pk).is_active


def test_a_stale_copy_saved_inactive_after_a_reactivation_is_a_deactivation(person):
    stale = User.objects.get(pk=person.pk)
    person.is_active = False
    person.save(update_fields=["is_active"])
    person.is_active = True
    person.save(update_fields=["is_active"])
    stale.is_active = False
    stale.save()
    assert Deactivation.objects.filter(user=person).count() == 2


def test_the_record_counts_the_tickets_returned_to_their_queues(person, assigned):
    person.is_active = False
    person.save(update_fields=["is_active"])
    assert Deactivation.objects.get(user=person).tickets_returned == 1
    assert ServiceRequest.objects.get(pk=assigned.pk).assignee_id is None


def test_records_cannot_be_changed_or_deleted(person):
    person.is_active = False
    person.save(update_fields=["is_active"])
    record = Deactivation.objects.get(user=person)
    with pytest.raises(DatabaseError, match="cannot be changed or deleted"), transaction.atomic():
        Deactivation.objects.filter(pk=record.pk).update(reason="Rewritten")
    with pytest.raises(DatabaseError, match="cannot be changed or deleted"), transaction.atomic():
        Deactivation.objects.filter(pk=record.pk).delete()
    assert Deactivation.objects.get(pk=record.pk).reason == ""


# ------------------------------------------------------------------------------ the page


def test_the_page_shows_what_the_account_holds_and_what_deactivating_does(owner, person, assigned):
    browser_for(person)  # one open session
    response = browser_for(owner).get(page_url(person))
    assert response.status_code == 200
    text = response.content.decode()
    for words in (
        "Roles: Supervisor",
        "Can view patient information (Directly, last day ",
        "Through the Supervisor role",
        "Bench chemistry (lead)",
        "1 session is open now",
        "1 open ticket now",
        "Deactivate</button>",
    ):
        assert words in text, words
    assert "dar-person@example.invalid" not in text


def test_deactivating_records_what_it_held_with_the_reason_in_one_change(owner, person, assigned):
    target = browser_for(person)
    browser = browser_for(owner)
    page = browser.get(page_url(person))
    shown = deactivation.holdings(person.pk)
    response = browser.post(
        page_url(person), {"reason": "Left the laboratory", "drawn": drawn_of(page)}
    )
    assert response.status_code == 302 and response["Location"] == change_url(person)
    person.refresh_from_db()
    assert not person.is_active
    record = Deactivation.objects.get(user=person)
    assert record.held == shown and record.reason == "Left the laboratory"
    assert record.deactivated_by_id == owner.pk and record.tickets_returned == 1
    event = AuditEvent.objects.filter(action="account.updated", object_id=str(person.pk)).get()
    assert event.actor_id == owner.pk and event.reason == "Left the laboratory"
    assert event.before["is_active"] is True and event.after["is_active"] is False
    assert {k: v for k, v in event.before.items() if k != "is_active"} == {
        k: v for k, v in event.after.items() if k != "is_active"
    }
    assert LogEntry.objects.filter(
        object_id=str(person.pk), change_message="Deactivated the account."
    ).exists()
    assert AccountSession.objects.get(pk=target.session["_account_session"]).ended_at is not None
    landed = browser.get(response["Location"])
    text = landed.content.decode()
    assert "Deactivated dar-person. Its sessions ended. 1 open ticket returned" in text
    assert "Reason: Left the laboratory" in text
    assert "What it held when it was deactivated:" in text and "Roles: Supervisor" in text


def test_the_account_page_offers_deactivate_and_says_when_nothing_was_recorded(owner, person):
    browser = browser_for(owner)
    text = browser.get(change_url(person)).content.decode()
    assert page_url(person) in text
    User.objects.filter(pk=person.pk).update(is_active=False)  # a path that records nothing
    text = browser.get(change_url(person)).content.decode()
    assert "What it held then was not recorded" in text and page_url(person) not in text


def test_after_a_reactivation_the_page_shows_the_last_deactivation_and_offers_it_again(
    owner, person
):
    browser = browser_for(owner)
    page = browser.get(page_url(person))
    browser.post(page_url(person), {"reason": "On leave", "drawn": drawn_of(page)})
    person.refresh_from_db()
    person.is_active = True
    person.save(update_fields=["is_active"])
    text = browser.get(change_url(person)).content.decode()
    assert "Made active again since it was deactivated on" in text
    assert "Reason: On leave" in text and page_url(person) in text


def test_clearing_active_on_the_account_page_records_what_it_held_before_that_save(owner, person):
    browser = browser_for(owner)
    shown = deactivation.holdings(person.pk)
    form = browser.get(change_url(person)).context["adminform"].form
    data = {
        name: value
        for name, value in form.initial.items()
        if value is not None and name not in ("password", "groups", "user_permissions")
    }
    data.update(
        groups=[],  # the role removed in the same save
        user_permissions=[capability("view_patient_information").pk],
        is_active="",
        date_joined_0=person.date_joined.strftime("%Y-%m-%d"),
        date_joined_1=person.date_joined.strftime("%H:%M:%S"),
    )
    data = {k: v for k, v in data.items() if k not in ("date_joined", "last_login")}
    response = browser.post(change_url(person), data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    person.refresh_from_db()
    assert not person.is_active and not person.groups.exists()
    record = Deactivation.objects.get(user=person)
    assert record.held == shown and record.held["roles"] == ["Supervisor"]
    assert record.reason == "" and record.deactivated_by_id == owner.pk
    text = browser.get(change_url(person)).content.decode()
    assert "No reason was recorded" in text


def test_a_reason_is_required_and_kept_short(owner, person):
    browser = browser_for(owner)
    drawn = drawn_of(browser.get(page_url(person)))
    for reason in ("   ", "x" * 1001):
        response = browser.post(page_url(person), {"reason": reason, "drawn": drawn})
        assert response.status_code == 200 and response.context["form"].errors
    assert User.objects.get(pk=person.pk).is_active and not Deactivation.objects.exists()
    with pytest.raises(ValidationError):
        deactivation.deactivate(actor=owner, account_id=person.pk, reason=" ", drawn=drawn)


def test_a_page_opened_before_a_change_of_what_it_holds_is_refused_and_shows_it_now(
    owner, person, team
):
    browser = browser_for(owner)
    drawn = drawn_of(browser.get(page_url(person)))
    TeamMembership.objects.filter(team=team, user=person).update(is_lead=False)
    response = browser.post(page_url(person), {"reason": "Left", "drawn": drawn})
    assert response.status_code == 200
    assert User.objects.get(pk=person.pk).is_active and not Deactivation.objects.exists()
    text = response.content.decode()
    assert "changed after you opened this page" in text
    assert "Bench chemistry (lead)" not in text and "Bench chemistry</li>" in text
    assert response.context["form"]["reason"].value() == "Left"
    # Drawn again with the fingerprint of the list it shows now: sent as it is, it saves.
    again = browser.post(page_url(person), {"reason": "Left", "drawn": drawn_of(response)})
    assert again.status_code == 302
    assert Deactivation.objects.get(user=person).held["teams"][0]["note"] == ""


@pytest.mark.parametrize(
    "change",
    [
        lambda user: user.groups.add(Group.objects.get_or_create(name="technical")[0]),
        lambda user: user.user_permissions.remove(capability("view_patient_information")),
        lambda user: CapabilityEnd.objects.filter(user=user).update(
            ends_at=timezone.now() + timedelta(days=90)
        ),
        lambda user: User.objects.filter(pk=user.pk).update(is_staff=True),
    ],
    ids=["role added", "capability removed", "last day moved", "admin console given"],
)
def test_each_kind_of_change_since_the_page_was_opened_is_refused(owner, person, change):
    drawn = deactivation.fingerprint(deactivation.holdings(person.pk))
    change(person)
    with pytest.raises(ValidationError, match="changed after you opened"):
        deactivation.deactivate(actor=owner, account_id=person.pk, reason="Left", drawn=drawn)
    assert User.objects.get(pk=person.pk).is_active and not Deactivation.objects.exists()


def test_own_inactive_and_scheduled_jobs_accounts_are_refused(owner, person):
    from jobs.actor import system_account

    browser = browser_for(owner)
    own = browser.get(page_url(owner))
    assert own.status_code == 200 and "This is your own account" in own.content.decode()
    refused = browser.post(page_url(owner), {"reason": "x", "drawn": "x"})
    assert User.objects.get(pk=owner.pk).is_active and refused.status_code == 200
    with pytest.raises(ValidationError, match="your own account"):
        deactivation.deactivate(actor=owner, account_id=owner.pk, reason="x", drawn="x")
    person.is_active = False
    person.save(update_fields=["is_active"])
    inactive = browser.get(page_url(person))
    assert "already deactivated" in inactive.content.decode() and "<form" not in str(
        inactive.content.decode().split('id="content-main"')[1]
    )
    jobs = system_account()
    assert browser.get(page_url(jobs)).status_code == 403
    assert "Deactivate</a>" not in browser.get(change_url(owner)).content.decode()


@pytest.mark.parametrize("kind", ["staff without superuser", "superuser without staff", "portal"])
def test_only_an_owner_of_the_administration_opens_or_saves_the_page(person, kind):
    if kind == "portal":
        organization = Client.objects.create(name="Dar Clinic", account_number="DARC2")
        actor = User.objects.create_user(
            "dar-p", "dar-p@example.invalid", PASSWORD, client=organization
        )
    elif kind == "staff without superuser":
        actor = User.objects.create_user("dar-s", "dar-s@example.invalid", PASSWORD, is_staff=True)
    else:
        actor = User.objects.create_user(
            "dar-u", "dar-u@example.invalid", PASSWORD, is_superuser=True
        )
    browser = browser_for(actor)
    drawn = deactivation.fingerprint(deactivation.holdings(person.pk))
    for response in (
        browser.get(page_url(person)),
        browser.post(page_url(person), {"reason": "x", "drawn": drawn}),
    ):
        assert response.status_code in (302, 403)
        if response.status_code == 302:
            assert reverse("admin:login") in response["Location"]
    assert User.objects.get(pk=person.pk).is_active
    with pytest.raises(PermissionDenied):
        deactivation.deactivate(actor=actor, account_id=person.pk, reason="x", drawn=drawn)


def test_a_missing_account_is_not_found(owner):
    assert browser_for(owner).get(
        reverse("admin:accounts_user_deactivate", args=[987654])
    ).status_code in (302, 404)
    with pytest.raises(User.DoesNotExist):
        deactivation.deactivate(actor=owner, account_id=987654, reason="x", drawn="x")


@pytest.mark.confirmation
def test_saving_asks_the_owner_to_confirm_it_is_them(owner, person):
    browser = browser_for(owner)
    page = browser.get(page_url(person))
    assert "Your password" in page.content.decode()
    refused = browser.post(page_url(person), {"reason": "Left", "drawn": drawn_of(page)})
    assert refused.status_code == 200 and User.objects.get(pk=person.pk).is_active
    saved = browser.post(
        page_url(person), {"reason": "Left", "drawn": drawn_of(page), "confirm_password": PASSWORD}
    )
    assert saved.status_code == 302 and not User.objects.get(pk=person.pk).is_active


# ------------------------------------------------------------------------------ at the same time


@pytest.mark.django_db(transaction=True)
def test_two_owners_deactivating_each_other_take_turns():
    first, second = make_owner("dar-first"), make_owner("dar-second")
    drawn = {
        user.pk: deactivation.fingerprint(deactivation.holdings(user.pk))
        for user in (first, second)
    }
    barrier = threading.Barrier(2)
    outcomes = {}

    def run(actor, target):
        try:
            barrier.wait(timeout=10)
            deactivation.deactivate(
                actor=actor, account_id=target.pk, reason="Mutual", drawn=drawn[target.pk]
            )
            outcomes[actor.pk] = "deactivated"
        except PermissionDenied:
            outcomes[actor.pk] = "refused"
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=(first, second)),
        threading.Thread(target=run, args=(second, first)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes.values()) == ["deactivated", "refused"]
    assert User.objects.filter(pk__in=[first.pk, second.pk], is_active=True).count() == 1
    assert Deactivation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_role_change_under_way_finishes_first_and_the_page_is_refused(person):
    owner = make_owner()
    drawn = deactivation.fingerprint(deactivation.holdings(person.pk))
    started, release = threading.Event(), threading.Event()
    outcome = {}

    def role_change():
        from accounts.acting import close_access

        try:
            with transaction.atomic():
                close_access([person.pk])  # as the account page takes it
                person.groups.add(Group.objects.get_or_create(name="technical")[0])
                started.set()
                release.wait(timeout=10)
        finally:
            connections.close_all()

    def deactivate():
        try:
            started.wait(timeout=10)
            deactivation.deactivate(actor=owner, account_id=person.pk, reason="Left", drawn=drawn)
            outcome["result"] = "deactivated"
        except ValidationError as error:
            outcome["result"] = error.messages[0]
        finally:
            connections.close_all()

    changer = threading.Thread(target=role_change)
    saver = threading.Thread(target=deactivate)
    changer.start()
    saver.start()
    started.wait(timeout=10)
    # The deactivation waits for the role change's gate.
    saver.join(timeout=1)
    assert saver.is_alive()
    release.set()
    changer.join(timeout=10)
    saver.join(timeout=10)
    assert "changed after you opened" in outcome["result"]
    assert User.objects.get(pk=person.pk).is_active and not Deactivation.objects.exists()


# ------------------------------------------------------------------------------ upgrade


@pytest.mark.django_db(transaction=True)
def test_the_migration_reverses_and_applies_again():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("accounts", "0011_grant_ends")])
        assert "accounts_deactivation" not in connection.introspection.table_names()
        MigrationExecutor(connection).migrate([("accounts", "0012_deactivation")])
        assert "accounts_deactivation" in connection.introspection.table_names()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
