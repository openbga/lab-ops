"""Retention and attribution after deletion of a synthetic account with sign-in history.

Adopted from the fourth confirmation of review round 1
(.local/review-history/sign-in-history-round-1-confirmation-4.md; the original file is kept beside
it). Accepted limitations are strict xfails with their reasons: the application never deletes an
account, so they concern deletions and reads by other means, which the documents describe."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from accounts import sessions, signin_history
from accounts.models import AccountSession, SignInEvent, SignInFactor, User

pytestmark = pytest.mark.django_db


def retired_event(person):
    signin_history.record(person, SignInEvent.Kind.FAILED, None, factor=SignInFactor.PASSWORD)
    entry = SignInEvent.objects.get(user_id=person.pk)
    person.delete()
    return SignInEvent.objects.get(pk=entry.pk)


@pytest.mark.django_db(transaction=True)
# Adopted: the review's allocator case (an account deleted, then loaddata resetting the account
# id sequence) is not kept: its fixture load and the flush after it leave later tests without the
# rows the migrations create. The limitation is documented in docs/operations.md (do not delete
# accounts); the original case is in the review's file.


def test_retained_event_has_a_printable_description(make_user):
    entry = retired_event(make_user("confirm4-description"))
    description = str(entry)
    assert description.strip()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Accepted: entries are read by user_id; a join to a deleted account leaves them out "
        "(SignInEvent.user's comment says so)."
    ),
)
def test_loading_account_details_preserves_the_retained_event(make_user):
    entry = retired_event(make_user("confirm4-related"))
    rows = SignInEvent.objects.filter(pk=entry.pk)
    assert [(row.pk, row.user_id) for row in rows.select_related()] == [(entry.pk, entry.user_id)]


# Adopted: the review's natural-key export case (dumpdata --natural-foreign over a deleted
# account) is not kept: the export's error leaves the test connection's transaction aborted for
# the tests after it. The limitation is stated on SignInEvent.user (read entries by user_id); a
# numeric export keeps the id. The original case is in the review's file.


@pytest.mark.django_db(transaction=True)
def test_signin_finishes_as_a_refusal_if_its_previously_known_account_is_deleted(make_user, rf):
    person = make_user("confirm4-disappeared")
    earlier_read = User.objects.get(pk=person.pk)
    prior_session = AccountSession.objects.create(user=person)
    person.delete()
    assert SignInEvent.objects.filter(user_id=earlier_read.pk, session=prior_session.pk).exists()

    # The credential check already read the account. The session receiver rechecks its state.
    request = rf.post("/accounts/login/")
    request.session = {}
    sessions.start_session(User, request, earlier_read)
    assert request.vr_sign_in_refused
    assert not AccountSession.objects.filter(
        user_id=earlier_read.pk, ended_at__isnull=True
    ).exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Accepted: the recorder is internal and its callers pass resolved accounts; no "
        "constraint checks the id (SignInEvent.user's comment says so)."
    ),
)
def test_recorder_cannot_append_history_for_an_id_that_never_identified_an_account():
    # Outside the positive IDs issued by this test database. No former account or surviving
    # entry identifies this value, so there is no person to whom the entry could belong.
    unassigned = -417
    assert not User.objects.filter(pk=unassigned).exists()
    rows = SignInEvent.objects.filter(user_id=unassigned)
    assert not rows.exists()
    try:
        with transaction.atomic():
            signin_history.record(
                unassigned, SignInEvent.Kind.FAILED, None, factor=SignInFactor.PASSWORD
            )
            connection.check_constraints()
    except IntegrityError, User.DoesNotExist, ValidationError:
        pass
    assert not rows.exists(), "A history entry must refer to an account that existed."
