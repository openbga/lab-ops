"""Emergency access under concurrent requests and with Confirm it is you: two starts by one person
on one ticket make one use; two reviews of one use record one; a start waits for, and is refused
after, the removal of its capability; the start page asks for the password where the session has
not confirmed. Synthetic data only."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.acting import close_access
from audit_review import emergency as review
from desk import emergency
from desk.models import EmergencyAccess, EmergencyReview
from links.tests.conftest import lis as lis
from request_forms.tests import conftest as fixtures
from request_forms.tests.conftest import PASSWORD, PATIENT_SENTINEL
from request_forms.tests.test_pages import raised

make_user = fixtures.make_user
catalog = fixtures.catalog
group = fixtures.group
logistics = fixtures.logistics
pickup_category = fixtures.pickup_category
pickup = fixtures.pickup
clinic = fixtures.clinic
portal_user = fixtures.portal_user

EXPLANATION = "A synthetic caller asked for a critical value at once."


def grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(codename=codename, content_type__app_label="accounts")
    )


@pytest.fixture
def ticket(portal_user, pickup):
    return raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})


@pytest.fixture
def eligible(make_user):
    user = make_user("eac_agent", "client_services")
    grant(user, "use_emergency_access")
    return user


@pytest.fixture
def reviewers(make_user):
    accounts = []
    for name in ("eac_reviewer_a", "eac_reviewer_b"):
        user = make_user(name, "technical")
        grant(user, "review_audit_records")
        accounts.append(user)
    return accounts


def in_thread(work):
    def run():
        try:
            return work()
        finally:
            connection.close()

    return run


def outcome(future):
    try:
        return future.result(timeout=30)
    except Exception as error:  # noqa: BLE001 - the test inspects which refusal it was
        return error


@pytest.mark.django_db(transaction=True)
def test_two_starts_by_one_person_on_one_ticket_make_one_use(eligible, ticket):
    barrier = threading.Barrier(2)

    def start():
        barrier.wait(timeout=10)
        return emergency.start(
            actor=eligible, ticket_id=ticket.pk, reason="other", explanation=EXPLANATION
        )

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(in_thread(start)) for _ in range(2)]
        results = [outcome(future) for future in futures]
    started = [r for r in results if isinstance(r, EmergencyAccess)]
    refused = [r for r in results if isinstance(r, emergency.AlreadyInForce)]
    assert len(started) == 1 and len(refused) == 1, results
    assert EmergencyAccess.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_reviews_of_one_use_record_one(eligible, ticket, reviewers):
    now = timezone.now()
    use = EmergencyAccess.objects.create(
        user=eligible,
        user_name="eac_agent",
        user_username="eac_agent",
        user_roles="Client services",
        ticket=ticket,
        kind="patient_information",
        reason="other",
        explanation=EXPLANATION,
        started_at=now - timedelta(minutes=90),
        ends_at=now - timedelta(minutes=30),
    )
    barrier = threading.Barrier(2)

    def reviewed_by(account):
        def work():
            barrier.wait(timeout=10)
            return review.record_review(
                actor=account, use_id=use.pk, outcome="appropriate", note="Checked."
            )

        return work

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(in_thread(reviewed_by(account))) for account in reviewers]
        results = [outcome(future) for future in futures]
    recorded = [r for r in results if isinstance(r, EmergencyReview)]
    refused = [r for r in results if isinstance(r, ValidationError)]
    assert len(recorded) == 1 and len(refused) == 1, results
    assert review.ALREADY_REVIEWED in refused[0].messages
    assert EmergencyReview.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_start_after_its_capability_is_removed_is_refused(eligible, ticket):
    """The removal holds the account's access gate (accounts.acting.close_access): a start that
    begins meanwhile waits for it, then reads the account without the capability."""
    removal_holds = threading.Event()
    release = threading.Event()

    def remove():
        with transaction.atomic():
            close_access([eligible.pk])
            eligible.user_permissions.clear()
            removal_holds.set()
            release.wait(timeout=10)

    def start():
        removal_holds.wait(timeout=10)
        return emergency.start(
            actor=eligible, ticket_id=ticket.pk, reason="other", explanation=EXPLANATION
        )

    with ThreadPoolExecutor(2) as pool:
        removing = pool.submit(in_thread(remove))
        starting = pool.submit(in_thread(start))
        removal_holds.wait(timeout=10)
        assert not starting.done()  # waits for the removal's gate
        release.set()
        removing.result(timeout=30)
        result = outcome(starting)
    assert isinstance(result, PermissionDenied), result
    assert not EmergencyAccess.objects.exists()


@pytest.mark.django_db
@pytest.mark.confirmation
def test_the_start_asks_for_the_password_and_refuses_a_wrong_one(client, eligible, ticket):
    client.force_login(eligible)
    url = reverse("desk:request-emergency", args=[ticket.pk])
    page = client.get(url).content.decode()
    assert 'name="confirm_password"' in page
    refused = client.post(
        url, {"reason": "other", "explanation": EXPLANATION, "confirm_password": "wrong-one-1"}
    )
    assert refused.status_code == 200 and not EmergencyAccess.objects.exists()
    accepted = client.post(
        url, {"reason": "other", "explanation": EXPLANATION, "confirm_password": PASSWORD}
    )
    assert accepted.status_code == 302 and EmergencyAccess.objects.count() == 1
