"""Changing the scheduled job settings: administrators only, validated, audited, checked
against concurrent changes. Synthetic data."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from core.models import AuditEvent
from jobs.models import JobSettings
from jobs.services import STALE, update_settings

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(make_user):
    return make_user("job_admin", "admin")


def save(actor, value, revision=1):
    return update_settings(actor=actor, expected_revision=revision, close_resolved_after_days=value)


def events():
    return AuditEvent.objects.filter(action="jobs.settings_changed")


@pytest.mark.parametrize("value", [1, 30, 365, "7"])
def test_admin_sets_a_number_of_days(admin, value):
    assert save(admin, value) is True
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == int(value)


def test_admin_clears_the_period(admin):
    save(admin, 30)
    assert save(admin, None, revision=2) is True
    row = JobSettings.objects.get(pk=1)
    assert row.close_resolved_after_days is None
    assert row.revision == 3


@pytest.mark.parametrize("value", [0, 366, -1, "abc", "0", "400"])
def test_values_outside_the_range_are_refused(admin, value):
    with pytest.raises(ValidationError) as error:
        save(admin, value)
    assert "close_resolved_after_days" in error.value.message_dict
    row = JobSettings.objects.filter(pk=1).first()
    assert row is None or (row.close_resolved_after_days is None and row.revision == 1)
    assert not events().exists()


@pytest.mark.parametrize("role", ["supervisor", "client_services", "technical"])
def test_other_staff_roles_are_refused(make_user, role):
    user = make_user("someone_else", role)
    with pytest.raises(PermissionDenied):
        save(user, 30)
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()


def test_a_portal_account_is_refused(clinic_portal):
    with pytest.raises(PermissionDenied):
        save(clinic_portal, 30)
    assert not events().exists()


def test_an_admin_deactivated_after_loading_is_refused(admin):
    User.objects.filter(pk=admin.pk).update(is_active=False)
    # `admin` still says active in memory, as a request that loaded it earlier would.
    assert admin.is_active
    with pytest.raises(PermissionDenied):
        save(admin, 30)
    assert not events().exists()
    assert not JobSettings.objects.filter(close_resolved_after_days__isnull=False).exists()


def test_an_admin_demoted_after_loading_is_refused(admin):
    admin.groups.clear()
    with pytest.raises(PermissionDenied):
        save(admin, 30)
    assert not events().exists()


def test_a_stale_revision_is_refused(admin):
    save(admin, 30)
    with pytest.raises(ValidationError) as error:
        save(admin, 60, revision=1)
    assert STALE in error.value.messages
    assert JobSettings.objects.get(pk=1).close_resolved_after_days == 30
    assert events().count() == 1


def test_a_missing_revision_is_refused(admin):
    with pytest.raises(ValidationError):
        save(admin, 30, revision=None)


def test_a_change_is_audited_and_advances_the_revision(admin):
    save(admin, 30)
    save(admin, 60, revision=2)
    first, second = events().order_by("occurred_at")
    assert first.actor_id == admin.pk
    assert first.before == {"close_resolved_after_days": None}
    assert first.after == {"close_resolved_after_days": 30}
    assert second.before == {"close_resolved_after_days": 30}
    assert second.after == {"close_resolved_after_days": 60}
    assert second.object_type == "jobs.jobsettings" and second.object_id == "1"
    assert JobSettings.objects.get(pk=1).revision == 3


def test_saving_the_same_value_changes_nothing(admin):
    assert save(admin, None) is False
    assert save(admin, 30) is True
    assert save(admin, 30, revision=2) is False
    assert events().count() == 1
    assert JobSettings.objects.get(pk=1).revision == 2
