"""Operator guidance for historical refusals and failures involving overlapping account names.

Adopted from the third confirmation of review round 1
(.local/review-history/sign-in-history-round-1-confirmation-3.md; the original file is kept beside
it)."""

from pathlib import Path

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.functions import Now
from django.urls import reverse

from accounts import signin_history
from accounts.models import AccountSession, SignInEvent, SignInFactor
from accounts.tests.test_sign_in_history import sign_in

pytestmark = pytest.mark.django_db
OPERATIONS = Path(__file__).resolve().parents[2] / "docs/operations.md"


def test_rollout_guidance_includes_refusals_written_by_the_session_trigger(make_user):
    user = make_user("confirm3-rolling")
    apps = (
        MigrationExecutor(connection)
        .loader.project_state([("accounts", "0008_compromised_password")])
        .apps
    )
    old_session = apps.get_model("accounts", "AccountSession")
    # The pre-history release's _record_session insert when it refuses a marked password.
    # The deployment has already applied 0009, even though this writer omits the factor column.
    row = old_session.objects.create(
        user_id=user.pk, ending=AccountSession.Ending.MARKED_COMPROMISED, ended_at=Now()
    )
    entry = SignInEvent.objects.get(user=user)
    assert (entry.session, entry.kind, entry.ending, entry.factor) == (
        row.pk,
        SignInEvent.Kind.REFUSED,
        AccountSession.Ending.MARKED_COMPROMISED,
        "",
    )
    guide = " ".join(OPERATIONS.read_text().split())
    assert (
        "Failed and refused attempts that the earlier release's processes handle while the "
        "deployment replaces them are not recorded" not in guide
    ), "The old writer's refused session produced a history entry after migration 0009."


def test_operations_scopes_the_ambiguous_name_exception_to_signin(client, make_user):
    person = make_user("confirm3-reader@example.invalid")
    other = make_user("confirm3-other")
    other.email = person.username
    other.save(update_fields=["email"])
    assert signin_history.account_named({"username": person.username}) is None

    wrong = "incorrect synthetic value"
    response = sign_in(client, person.username, wrong)
    assert response.status_code == 200 and response.context["form"].errors
    assert not SignInEvent.objects.filter(user__in=[person, other]).exists()

    client.force_login(person)
    response = client.post(reverse("account_reauthenticate"), {"password": wrong})
    assert response.context["form"].has_error("password", code="incorrect_password")
    failure = SignInEvent.objects.get(kind=SignInEvent.Kind.REAUTHENTICATION_FAILED)
    assert (failure.user_id, failure.factor) == (person.pk, SignInFactor.PASSWORD)
    assert not SignInEvent.objects.filter(user=other).exists()

    guide = " ".join(OPERATIONS.read_text().split())
    assert (
        "A wrong password or code that is checked adds one entry to the history of the account "
        "it is recorded for (a wrong password whose name matches no account, or several, adds "
        "none): at sign-in the failed sign-in limit" not in guide
    ), "The name-matching exception applies at sign-in. A rejected re-authentication is recorded."
