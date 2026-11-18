"""The optional-module switch (core.modules): who switches, what it refuses, what it audits and
how a write holds the switch."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404

from accounts.models import User
from clients.models import Client
from core import modules
from core.models import AuditEvent, LabModule

pytestmark = pytest.mark.django_db

KEY = "nonconformance"


@pytest.fixture
def make(db):
    from django.contrib.auth.models import Group

    def make_user(name, *roles, **extra):
        user = User.objects.create_user(
            name, f"{name}@example.invalid", "test-password-729!", **extra
        )
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make_user


@pytest.fixture
def admin(make):
    return make("admin", "admin")


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def switch(actor, *, enabled=True, revision=0, reason="Laboratory decision."):
    return modules.set_enabled(
        actor=actor, key=KEY, enabled=enabled, expected_revision=revision, reason=reason
    )


def test_a_module_without_a_row_is_off():
    assert modules.is_enabled(KEY) is False
    assert not LabModule.objects.exists()


def test_an_unknown_module_key_is_a_programming_error():
    with pytest.raises(ValueError):
        modules.is_enabled("no_such_module")


def test_admin_switches_a_module_on(admin):
    module = switch(admin)
    assert module.is_enabled is True and module.revision == 1
    assert modules.is_enabled(KEY) is True


def test_admin_switches_a_module_off_again(admin):
    switch(admin)
    switch(admin, enabled=False, revision=1)
    assert modules.is_enabled(KEY) is False
    assert fresh(LabModule.objects.get(key=KEY)).revision == 2


@pytest.mark.parametrize("role", ["supervisor", "client_services", "technical"])
def test_other_staff_roles_cannot_switch_a_module(make, role):
    with pytest.raises(PermissionDenied):
        switch(make("someone", role))
    assert not LabModule.objects.exists()
    assert not AuditEvent.objects.exists()


def test_a_portal_account_cannot_switch_a_module(make):
    clinic = Client.objects.create(name="Example Clinic", account_number="EXCLIN")
    with pytest.raises(PermissionDenied):
        switch(make("portal", client=clinic))
    assert not AuditEvent.objects.exists()


def test_an_account_without_a_role_cannot_switch_a_module(make):
    with pytest.raises(PermissionDenied):
        switch(make("norole"))
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_switch_needs_a_reason(admin, reason):
    with pytest.raises(ValidationError):
        switch(admin, reason=reason)
    assert not LabModule.objects.filter(is_enabled=True).exists()
    assert not AuditEvent.objects.exists()


def test_a_stale_revision_refuses_the_switch(admin):
    switch(admin)
    with pytest.raises(ValidationError):
        switch(admin, enabled=False, revision=0)
    assert modules.is_enabled(KEY) is True
    assert AuditEvent.objects.count() == 1


def test_a_missing_revision_refuses_the_switch(admin):
    with pytest.raises(ValidationError):
        switch(admin, revision=None)
    assert not AuditEvent.objects.exists()


def test_switching_to_the_state_it_already_has_is_refused(admin):
    switch(admin)
    with pytest.raises(ValidationError, match="Nothing to change"):
        switch(admin, enabled=True, revision=1)
    assert fresh(LabModule.objects.get(key=KEY)).revision == 1
    assert AuditEvent.objects.count() == 1


def test_switching_off_a_module_that_was_never_on_is_refused(admin):
    with pytest.raises(ValidationError, match="Nothing to change"):
        switch(admin, enabled=False, revision=0)
    assert not AuditEvent.objects.exists()


def test_switching_on_writes_one_audit_event_with_the_reason(admin):
    module = switch(admin, reason="Quality system go-live.")
    event = AuditEvent.objects.get()
    assert event.action == "module.enabled"
    assert event.actor == admin
    assert event.object_id == str(module.pk)
    assert event.reason == "Quality system go-live."
    assert event.before == {"key": KEY, "is_enabled": False}
    assert event.after == {"key": KEY, "is_enabled": True}


def test_switching_off_writes_one_audit_event_with_the_reason(admin):
    switch(admin)
    switch(admin, enabled=False, revision=1, reason="Paused for review.")
    event = AuditEvent.objects.filter(action="module.disabled").get()
    assert AuditEvent.objects.count() == 2
    assert event.reason == "Paused for review."
    assert event.before == {"key": KEY, "is_enabled": True}
    assert event.after == {"key": KEY, "is_enabled": False}


def test_a_switch_has_no_other_side_effect(admin):
    switch(admin)
    assert LabModule.objects.count() == 1
    assert AuditEvent.objects.count() == 1
    assert User.objects.count() == 1


def test_states_lists_a_module_with_revision_zero_before_its_first_switch():
    (state,) = modules.states()
    assert state["key"] == KEY
    assert state["is_enabled"] is False and state["revision"] == 0
    assert state["name"] and state["description"]


def test_states_follows_the_switch(admin):
    switch(admin)
    (state,) = modules.states()
    assert state["is_enabled"] is True and state["revision"] == 1


def test_can_manage_is_admin_only(make):
    assert modules.can_manage(make("a", "admin")) is True
    assert modules.can_manage(make("s", "supervisor")) is False
    assert modules.can_manage(make("n")) is False


def test_require_enabled_is_a_404_when_the_module_is_off():
    with pytest.raises(Http404):
        modules.require_enabled(KEY)


def test_require_enabled_is_a_404_after_it_was_switched_off(admin):
    switch(admin)
    switch(admin, enabled=False, revision=1)
    with pytest.raises(Http404):
        modules.require_enabled(KEY)


def test_require_enabled_passes_when_the_module_is_on(admin):
    switch(admin)
    modules.require_enabled(KEY)


def test_lock_enabled_is_a_404_when_there_is_no_row():
    with pytest.raises(Http404), transaction.atomic():
        modules.lock_enabled(KEY)


def test_lock_enabled_is_a_404_when_the_module_is_off(admin):
    switch(admin)
    switch(admin, enabled=False, revision=1)
    with pytest.raises(Http404), transaction.atomic():
        modules.lock_enabled(KEY)


def test_lock_enabled_passes_when_the_module_is_on(admin):
    switch(admin)
    with transaction.atomic():
        modules.lock_enabled(KEY)


@pytest.mark.django_db(transaction=True)
def test_lock_enabled_without_an_enclosing_transaction_raises_runtime_error():
    with pytest.raises(RuntimeError):
        modules.lock_enabled(KEY)


def test_lock_enabled_rejects_an_unknown_key():
    with pytest.raises(ValueError), transaction.atomic():
        modules.lock_enabled("no_such_module")
