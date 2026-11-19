"""Confirmation of the assignment lock order after the third verification (candidate b9e2ce3).
All records are synthetic.

C-01 (Low). add_link reads the acting account again right after the ticket's lock and again after
the link's, but between them it waits for the system's lock and then decides something about the
ticket ("already has a person-level link") on the account as it stood before that wait. Rule:
docs/architecture.md "who asks is answered before anything about the ticket"; links.services
add_link "Who asks is answered before anything about the ticket (closed, its links)";
links.services.person_link_readable "The capability alone opens nothing". Predates b9e2ce3 (the
same order at 9fbf6c0, 791976b and db1fc14); next to V3-01, which b9e2ce3 fixed for the ticket's
wait only.

The races reuse the verification file's sessions (each party its own PostgreSQL session with a
bounded lock_timeout).
"""

import pytest
from django.contrib.auth.models import Permission

from accounts.models import User
from links import services as link_services
from links.models import ExternalSystem, LinkAttachment

from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import (
    Held,
    Session,
    _blocked_by,
    _demote,
    _race,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def lis(db):
    return ExternalSystem.objects.get_or_create(
        key="lis", defaults={"name": "Laboratory information system", "kind": "lis"}
    )[0]


@pytest.fixture
def restricted_ticket(agent, privacy_category):
    """A ticket in "Privacy request" (restricted, routed to Quality)."""
    return new_request(agent, privacy_category, client=None)


def _capability():
    return Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )


def _revoke(user):
    def run():
        User.objects.get(pk=user.pk).user_permissions.remove(_capability())

    return run


@pytest.fixture
def capable_lead(make_user):
    """A supervisor in no team, with the patient information capability."""
    user = make_user("confirm_capable_lead", "supervisor")
    user.user_permissions.add(_capability())
    return user


def _add_second_person_link(capable_lead, ticket, lis):
    """The system held by a colleague's change, and the addition that waits for it."""

    def hold_system():
        ExternalSystem.objects.select_for_update().get(pk=lis.pk)

    def work():
        link_services.add_link(
            actor=capable_lead,
            request_id=ticket.pk,
            system=lis,
            record_type="patient",
            identifier="PAT-TEST-000902",
        )

    return hold_system, work


@pytest.fixture
def carrying_ticket(capable_lead, restricted_ticket, lis):
    """The restricted ticket, already carrying one person-level link."""
    link_services.add_link(
        actor=capable_lead,
        request_id=restricted_ticket.pk,
        system=lis,
        record_type="patient",
        identifier="PAT-TEST-000901",
    )
    return restricted_ticket


def test_a_person_link_addition_whose_actor_was_deactivated_at_the_system_is_not_told_its_links(
    capable_lead, carrying_ticket, lis
):
    """C-01. A supervisor in no team, holding the patient information capability, adds a
    person-level link to a restricted ticket (Privacy request, Quality) that already carries one.
    The ticket is free, so the change locks it and reads the account again; then it waits for the
    system's lock, which a colleague's link addition (or an external identifier import, or a system
    edit) holds. During that wait the supervisor is deactivated, by a path that bypasses
    User.save (a queryset update), which the hold of accounts.acting does not make wait.

    Who asks is answered before anything about the ticket: the addition reads the account again
    after its locks and is refused as an account that may no longer do this (PermissionDenied) or
    a ticket they cannot open (DoesNotExist, the page's 404). It does not say "This ticket
    already has a person-level link", which tells someone who may no longer open the ticket that
    it carries a person-level link."""
    hold_system, work = _add_second_person_link(capable_lead, carrying_ticket, lis)

    def deactivate():
        User.objects.filter(pk=capable_lead.pk).update(is_active=False)

    state, statement, session = _race(hold_system, work, deactivate)
    assert state == "waiting", state
    assert "links_externalsystem" in statement, statement  # the system's lock, not the ticket's
    assert session.outcome in ("not found", "denied"), (session.outcome, repr(session.error))
    assert LinkAttachment.objects.filter(request=carrying_ticket).count() == 1


@pytest.mark.parametrize("meanwhile", ["demoted", "revoked"])
def test_a_demotion_or_revocation_waits_for_a_person_link_addition_waiting_for_the_system(
    meanwhile, capable_lead, carrying_ticket, lis
):
    """C-01. The same addition, but the supervisor is demoted to technical staff, or loses the
    capability, while it waits for the system's lock. The addition holds the account's roles and
    grants until it commits (accounts.acting), so the change waits behind it. The addition is
    decided by an account that still may open the ticket and see person-level links, so it is
    refused on the merits ("This ticket already has a person-level link"), and nothing is
    attached. The demotion or revocation commits after it."""
    hold_system, work = _add_second_person_link(capable_lead, carrying_ticket, lis)
    access_change = {"demoted": _demote(capable_lead), "revoked": _revoke(capable_lead)}[meanwhile]
    holder = Held(hold_system)
    session = Session(work).start()
    try:
        state = session.waiting_or_ended()
        access = Session(access_change).start()
        access_state = access.waiting_or_ended()
        behind_addition = access.pid is not None and _blocked_by(access.pid, session.pid)
    finally:
        assert holder.release() == "done"
    session.join()
    access.join()
    assert state == "waiting", state  # for the system
    assert (access_state, behind_addition) == ("waiting", True)
    assert access.outcome == "done", repr(access.error)
    assert session.outcome == "refused", (session.outcome, repr(session.error))
    assert "already has a person-level link" in str(session.error)
    assert LinkAttachment.objects.filter(request=carrying_ticket).count() == 1
    stored = User.objects.get(pk=capable_lead.pk)
    if meanwhile == "demoted":
        assert not stored.groups.filter(name="supervisor").exists()
    else:
        assert not stored.user_permissions.exists()


@pytest.mark.parametrize("meanwhile", ["demoted", "revoked"])
def test_a_person_link_addition_whose_actor_lost_the_ticket_before_it_began_is_not_told_its_links(
    meanwhile, capable_lead, carrying_ticket, lis
):
    """C-01. The supervisor was demoted to technical staff, or lost the capability, and the
    change committed before the addition began, while the caller still holds the account as it
    was. Who asks is answered before anything about the ticket: the addition is refused as a
    ticket they cannot open (DoesNotExist, the page's 404) or an account that may no longer do
    this (PermissionDenied). It does not say "This ticket already has a person-level link"."""
    hold_system, work = _add_second_person_link(capable_lead, carrying_ticket, lis)
    {"demoted": _demote(capable_lead), "revoked": _revoke(capable_lead)}[meanwhile]()
    _state, _statement, session = _race(hold_system, work, lambda: None)
    assert session.outcome in ("not found", "denied"), (session.outcome, repr(session.error))
    assert "person-level link" not in str(session.error)
    assert LinkAttachment.objects.filter(request=carrying_ticket).count() == 1
