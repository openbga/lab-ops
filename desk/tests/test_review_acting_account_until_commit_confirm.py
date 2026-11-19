"""Confirmation of the verification's resolution (candidate e713e13, previous 3c82dcf, base
cc8d7fa). All records are synthetic.

Adopted: C1. Every save of a portal account's access takes the account's gate, widening included,
as the portal access form's help says; the docs now state it (accounts/acting.py,
docs/architecture.md), and the case asserts that wait.

Each test states intended behaviour from the docs the resolution changed. The races run every
party in its own PostgreSQL session with a bounded lock_timeout; a party is held at a named point
until the others wait for a lock (pg_blocking_pids) or have finished. No helper only the candidate
has, so the file runs on the base too.
"""

import threading
from unittest.mock import patch

import pytest

from accounts.models import User
from clients.models import ClientSite, PortalAccess
from clients.services import set_portal_access
from desk import services
from desk.models import EntryKind

from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import Session, _blocked_by, _fresh

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def locmem_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


class Gate:
    def __init__(self):
        self.arrived, self.go = threading.Event(), threading.Event()


def gated(module, name, gates):
    """Patch module.`name`: a call from a thread listed in `gates` stops once before the call."""
    real = getattr(module, name)
    used = set()

    def wrapper(*args, **kwargs):
        gate = gates.get(threading.current_thread())
        if gate is not None and id(gate) not in used:
            used.add(id(gate))
            gate.arrived.set()
            gate.go.wait(timeout=20)
        return real(*args, **kwargs)

    return patch.object(module, name, wrapper)


# C1. A site granted to a portal account and the account's reply under way --------------------


def test_granting_a_portal_site_waits_for_the_accounts_reply_then_goes_through(
    make_user, agent, category, clinic, clinic_portal
):
    """A clinic's portal account, granted site A, is replying on a ticket for site A (the change
    has held the account and reached its ticket). An account manager grants it site B as well on
    the portal access form; nothing is taken away.

    Intended (the portal access form's help: "If the account is saving a ticket or a reply at
    that moment, the save waits for it"; accounts/acting.py and docs/architecture.md: every save of
    a portal account's access takes the account's gate): the grant waits for the reply, which is
    saved first, and then goes through."""
    site_a = ClientSite.objects.create(client=clinic, code="SA", name="Site A")
    site_b = ClientSite.objects.create(client=clinic, code="SB", name="Site B")
    access = PortalAccess.objects.create(user=clinic_portal, client=clinic)
    access.sites.set([site_a])
    manager = make_user("confirm_manager", "account_manager")
    ticket = new_request(agent, category, client=clinic, site=site_a, subject="Site A")

    def reply():
        seen = _fresh(ticket)
        return services.add_entry(
            actor=User.objects.get(pk=clinic_portal.pk),
            request=seen,
            kind=EntryKind.REPLY,
            body="Any news on order ORD-TEST-000801?",
            expected_revision=seen.revision,
        )

    def widen():
        return set_portal_access(
            actor=manager,
            user_id=clinic_portal.pk,
            sites=[site_a, site_b],
            expected_client_id=clinic.pk,
            expected_revision=PortalAccess.objects.get(pk=access.pk).revision,
            reason="Site B opened",
        )

    first = Session(reply)
    gates = {first.thread: Gate()}
    with gated(services, "_locked_request", gates):
        first.start()
        assert gates[first.thread].arrived.wait(timeout=10), first.error
        grant = Session(widen).start()
        state = grant.waiting_or_ended(seconds=3)
        behind_reply = grant.pid is not None and _blocked_by(grant.pid, first.pid)
        gates[first.thread].go.set()
        first.join()
        grant.join()
    assert (first.outcome, grant.outcome) == ("done", "done"), (first.error, grant.error)
    assert set(PortalAccess.objects.get(pk=access.pk).sites.all()) == {site_a, site_b}
    assert (state, behind_reply) == ("waiting", True), state
