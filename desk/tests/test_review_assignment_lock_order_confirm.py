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
from .test_review_assignment_lock_order_verify3 import _deactivate, _demote, _race

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


@pytest.mark.parametrize("meanwhile", ["demoted", "deactivated", "revoked"])
def test_a_person_link_addition_whose_actor_lost_the_ticket_at_the_system_is_not_told_its_links(
    meanwhile, capable_lead, restricted_ticket, lis
):
    """C-01. A supervisor in no team, holding the patient information capability, adds a
    person-level link to a restricted ticket (Privacy request, Quality) that already carries one.
    The ticket is free, so the change locks it and reads the account again; then it waits for the
    system's lock, which a colleague's link addition (or an external identifier import, or a system
    edit) holds. During that wait the supervisor is demoted to technical staff (the ticket is no
    longer one they may open), deactivated, or loses the capability (they may see nothing of a
    person-level link).

    Who asks is answered before anything about the ticket: the addition is refused as a ticket
    they cannot open (DoesNotExist, the page's 404) or an account that may no longer do this
    (PermissionDenied). It does not say "This ticket already has a person-level link", which tells
    someone who may no longer open the ticket, or may no longer see person-level links, that it
    carries a person-level link."""
    ticket = restricted_ticket
    link_services.add_link(
        actor=capable_lead,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier="PAT-TEST-000901",
    )

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

    change_meanwhile = {
        "demoted": _demote(capable_lead),
        "deactivated": _deactivate(capable_lead),
        "revoked": _revoke(capable_lead),
    }[meanwhile]
    state, statement, session = _race(hold_system, work, change_meanwhile)
    assert state == "waiting", state
    assert "links_externalsystem" in statement, statement  # the system's lock, not the ticket's
    assert session.outcome in ("not found", "denied"), (session.outcome, repr(session.error))
    assert LinkAttachment.objects.filter(request=ticket).count() == 1
