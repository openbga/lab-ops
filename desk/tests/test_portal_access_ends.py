"""The last day of a client portal account's portal access (clients.models.PortalAccess.ends_at,
accounts.grant_ends): set with the scope on Set access, checked whenever the portal reads what the
account may see, shown on the organization page, and taken away with the access on a move."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts import grant_ends
from accounts.models import User
from clients.models import ClientSite, PortalAccess
from clients.portal import current_access, portal_contacts
from clients.services import set_portal_access
from core.models import AuditEvent
from desk.models import Channel
from desk.portal_scope import portal_requests, portal_sites, site_required

from .test_portal import raise_staff

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(make_user):
    return make_user("access_manager", "account_manager")


@pytest.fixture
def portal(make_user, clinic):
    return make_user("clinic_reader", client=clinic)


def grant(manager, portal, clinic, last_day=None, **scope):
    access = PortalAccess.objects.filter(user=portal).first()
    return set_portal_access(
        actor=manager,
        user_id=portal.pk,
        expected_client_id=clinic.pk,
        whole_organization=scope.get("whole_organization", True),
        sites=scope.get("sites", ()),
        last_day=last_day,
        expected_revision=access.revision if access else None,
        reason="Synthetic test change" if access else "",
    )


def end_now(portal):
    PortalAccess.objects.filter(user=portal).update(ends_at=timezone.now() - timedelta(hours=1))


def test_access_holds_until_its_last_day_and_then_only_raised_tickets_show(
    agent, category, clinic, clinic_site, manager, portal, client
):
    staff_ticket = raise_staff(agent, category, clinic, site=clinic_site)
    day = grant_ends.today() + timedelta(days=7)
    access = grant(manager, portal, clinic, last_day=day)
    assert access.ends_at == grant_ends.end_of(day)
    assert current_access(portal) is not None
    assert portal_requests(portal).filter(pk=staff_ticket.pk).exists()
    assert list(portal_sites(portal)) == [clinic_site]
    end_now(portal)
    assert current_access(portal) is None
    assert not portal_requests(portal).filter(pk=staff_ticket.pk).exists()
    assert not portal_sites(portal).exists() and not site_required(portal)
    client.force_login(portal)
    assert (
        client.get(reverse("portal:request-detail", args=[staff_ticket.number])).status_code == 404
    )


def test_an_ended_access_still_shows_what_the_account_raised(
    agent, category, clinic, manager, portal
):
    from desk.services import create_request

    own = create_request(
        actor=portal,
        category=category,
        subject="Raised in the portal",
        first_entry_kind="reply",
        first_entry_body="Synthetic.",
        channel=Channel.PORTAL,
        client=clinic,
    )
    grant(manager, portal, clinic, last_day=grant_ends.today() + timedelta(days=7))
    end_now(portal)
    assert list(portal_requests(portal)) == [own]


def test_contacts_follow_the_end(clinic, manager, portal):
    from clients.models import ClientContact

    site = ClientSite.objects.create(client=clinic, name="North", code="N1")
    site_contact = ClientContact.objects.create(client=clinic, name="Site Contact", phone="555")
    site_contact.sites.add(site)
    grant(manager, portal, clinic, last_day=grant_ends.today() + timedelta(days=7))
    assert site_contact in portal_contacts(portal)
    end_now(portal)
    assert site_contact not in portal_contacts(portal)


def test_a_new_last_day_in_the_past_is_refused_a_kept_one_is_not(clinic, manager, portal):
    yesterday = grant_ends.today() - timedelta(days=1)
    with pytest.raises(ValidationError) as refused:
        grant(manager, portal, clinic, last_day=yesterday)
    assert refused.value.message_dict == {"last_day": [grant_ends.PORTAL_PAST_DAY]}
    assert not PortalAccess.objects.exists()
    grant(manager, portal, clinic, last_day=grant_ends.today())  # today is allowed
    PortalAccess.objects.filter(user=portal).update(ends_at=grant_ends.end_of(yesterday))
    contact = clinic.contacts.create(name="Kept Day", phone="555")
    access = PortalAccess.objects.get(user=portal)
    set_portal_access(
        actor=manager,
        user_id=portal.pk,
        expected_client_id=clinic.pk,
        contact=contact,
        whole_organization=True,
        last_day=yesterday,
        expected_revision=access.revision,
        reason="Linked contact",
    )
    access.refresh_from_db()
    assert access.contact == contact and access.ends_at == grant_ends.end_of(yesterday)
    assert current_access(portal) is None


def test_an_end_only_change_is_a_change_and_clearing_it_gives_access_again(clinic, manager, portal):
    grant(manager, portal, clinic)
    day = grant_ends.today() + timedelta(days=3)
    grant(manager, portal, clinic, last_day=day)
    end_now(portal)
    assert current_access(portal) is None
    access = grant(manager, portal, clinic, last_day=None)
    assert access.ends_at is None and current_access(portal) is not None
    events = list(AuditEvent.objects.filter(action="portal_access.set").order_by("occurred_at"))
    assert [event.after["ends_at"] for event in events].count(None) == 2
    assert grant_ends.iso(grant_ends.end_of(day)) in [event.after["ends_at"] for event in events]
    with pytest.raises(ValidationError, match="Change at least one detail"):
        grant(manager, portal, clinic, last_day=None)


def test_a_move_to_another_organization_takes_the_end_away_with_the_access(
    clinic, water, manager, portal, make_user
):
    grant(manager, portal, clinic, last_day=grant_ends.today() + timedelta(days=3))
    portal.client = water
    portal.changed_by = manager  # a move through the application records the reset
    portal.save(update_fields=["client"])
    access = PortalAccess.objects.get(user=portal)
    assert access.ends_at is None and access.client_id == water.pk
    event = AuditEvent.objects.get(action="portal_access.reset")
    assert event.before["ends_at"] is not None and event.after["ends_at"] is None
    # A move made without a save (queryset.update): the database's move trigger empties the end
    # with the rest of the access.
    other = make_user("other_reader", client=clinic)
    grant(manager, other, clinic, last_day=grant_ends.today() + timedelta(days=3))
    User.objects.filter(pk=other.pk).update(client=water)
    access = PortalAccess.objects.get(user=other)
    assert access.ends_at is None and not access.whole_organization


def test_the_organization_page_and_set_access_say_when_access_ends(clinic, manager, portal, client):
    day = grant_ends.today() + timedelta(days=5)
    grant(manager, portal, clinic, last_day=day)
    client.force_login(manager)
    page = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    assert f"Last day {day.isoformat()}" in page
    edit = reverse("clients:portal-access-edit", args=[clinic.pk, portal.pk])
    response = client.get(edit)
    assert response.context["form"].initial["last_day"] == day
    notice = "The last day of this access has passed"  # the notice's own words, not the help's
    assert notice not in response.content.decode()
    end_now(portal)
    page = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    ended_day = grant_ends.last_day_of(PortalAccess.objects.get(user=portal).ends_at)
    assert f"Only tickets it raised (access ended after {ended_day.isoformat()})" in page
    assert notice in client.get(edit).content.decode()


def test_set_access_saves_a_last_day_and_refuses_a_past_one(clinic, manager, portal, client):
    client.force_login(manager)
    edit = reverse("clients:portal-access-edit", args=[clinic.pk, portal.pk])
    yesterday = grant_ends.today() - timedelta(days=1)
    form = {"scope": "organization", "expected_revision": "0"}
    response = client.post(edit, {**form, "last_day": yesterday.isoformat()})
    assert response.status_code == 200
    assert grant_ends.PORTAL_PAST_DAY in response.context["form"].errors["last_day"]
    assert not PortalAccess.objects.exists()
    day = grant_ends.today() + timedelta(days=30)
    response = client.post(edit, {**form, "last_day": day.isoformat()})
    assert response.status_code == 302
    assert PortalAccess.objects.get(user=portal).ends_at == grant_ends.end_of(day)


def test_staff_without_a_client_role_cannot_set_access(clinic, portal, make_user, client):
    reader = make_user("reader", "technical")
    client.force_login(reader)
    edit = reverse("clients:portal-access-edit", args=[clinic.pk, portal.pk])
    day = grant_ends.today() + timedelta(days=30)
    response = client.post(
        edit, {"scope": "organization", "expected_revision": "0", "last_day": day.isoformat()}
    )
    assert response.status_code == 403
    assert not PortalAccess.objects.exists()
    client.force_login(portal)
    assert client.post(edit, {"scope": "organization"}).status_code == 403


def test_an_end_left_from_another_organization_is_never_kept(clinic, water, manager, portal):
    """Access granted for another organization (a supported older record) shows no day; saving
    without one leaves no end, and a day chosen is a new choice."""
    old = PortalAccess.objects.create(
        user=portal,
        client=water,
        whole_organization=True,
        ends_at=grant_ends.end_of(grant_ends.today() + timedelta(days=5)),
    )
    set_portal_access(
        actor=manager,
        user_id=portal.pk,
        expected_client_id=clinic.pk,
        whole_organization=True,
        shown_last_day=None,
        expected_revision=old.revision,
        reason="First decision for this organization",
    )
    old.refresh_from_db()
    assert old.client_id == clinic.pk and old.ends_at is None


def test_a_changed_day_from_the_page_is_a_new_choice_and_a_shown_one_is_kept(
    clinic, manager, portal, client
):
    yesterday = grant_ends.today() - timedelta(days=1)
    grant(manager, portal, clinic)
    PortalAccess.objects.filter(user=portal).update(ends_at=grant_ends.end_of(yesterday))
    client.force_login(manager)
    edit = reverse("clients:portal-access-edit", args=[clinic.pk, portal.pk])
    form = client.get(edit).context["form"]
    assert form["shown_last_day"].value() == yesterday
    data = {
        "scope": "organization",
        "shown_last_day": yesterday.isoformat(),
        "expected_revision": form["expected_revision"].value(),
        "reason": "Synthetic change",
    }
    response = client.post(edit, {**data, "last_day": (yesterday - timedelta(days=1)).isoformat()})
    assert response.context["form"].errors["last_day"] == [grant_ends.PORTAL_PAST_DAY]
    contact = clinic.contacts.create(name="Kept Day Contact", phone="555")
    response = client.post(edit, {**data, "last_day": yesterday.isoformat(), "contact": contact.pk})
    assert response.status_code == 302
    access = PortalAccess.objects.get(user=portal)
    assert access.contact == contact and access.ends_at == grant_ends.end_of(yesterday)
