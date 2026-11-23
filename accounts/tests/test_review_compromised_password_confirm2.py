"""Regression checks for validator notifications and committed sign-in attribution."""

import logging

import pytest
from django.contrib.auth import password_validation
from django.contrib.auth.signals import user_logged_in
from django.db import transaction

from accounts.models import User


@pytest.mark.django_db
def test_disabling_a_pending_choice_before_the_first_save_sends_no_change_notice(monkeypatch):
    person = User.objects.create_user(
        "confirm2_reader", "reader@example.invalid", "Paper labels for a winter garden"
    )
    assert person.has_usable_password()
    discarded = "A linen cover for the orchard notebook"
    notices = []

    class Observer:
        def password_changed(self, value, user):
            notices.append(value == discarded)

    monkeypatch.setattr(
        password_validation, "get_default_password_validators", lambda: [Observer()]
    )
    person.set_password(discarded)
    person.set_unusable_password()
    person.save(update_fields=["password"])

    assert not User.objects.get(pk=person.pk).has_usable_password()
    assert not notices, "A validator received a change notice for a discarded choice"


@pytest.mark.django_db(transaction=True)
def test_sessionless_success_uses_the_stored_username_at_signin(rf, caplog):
    person = User.objects.create_user("confirm2_operator", "operator@example.invalid")
    stored_name = person.username
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    with transaction.atomic():
        user_logged_in.send(sender=User, request=rf.get("/"), user=person)
        person.username = "unsubmitted_profile_draft"

    saved = User.objects.get(pk=person.pk)
    assert saved.username == stored_name and saved.last_login is not None
    successes = [
        record.getMessage()
        for record in caplog.records
        if record.name == "lab_ops.signin" and record.getMessage().startswith("signin.success ")
    ]
    assert successes == [f"signin.success user={stored_name}"]
