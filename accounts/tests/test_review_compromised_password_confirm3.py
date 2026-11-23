"""Operational sign-in events must describe completed sign-ins."""

import logging

import pytest
from django.contrib.auth.signals import user_logged_in
from django.db import transaction

from accounts.models import AccountSession, User


@pytest.mark.xfail(
    strict=True,
    reason="Accepted (predates; see test_review_compromised_password_confirm.OUTER_ROLLBACK): the "
    "sign-in log writes a completed sign-in once its session start has finished; the application "
    "opens no transaction around sign-in that could roll it back.",
)
@pytest.mark.django_db(transaction=True)
def test_rolled_back_sessionless_signin_emits_no_success(rf, caplog):
    person = User.objects.create_user("confirm3_operator")
    request = rf.get("/")
    assert not hasattr(request, "session")

    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        with transaction.atomic():
            user_logged_in.send(sender=User, request=request, user=person)
            assert User.objects.get(pk=person.pk).last_login is not None
            transaction.set_rollback(True)

    person.refresh_from_db()
    assert person.last_login is None
    assert not AccountSession.objects.filter(user=person).exists()
    successes = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lab_ops.signin" and record.getMessage().startswith("signin.success ")
    ]
    assert successes == []
