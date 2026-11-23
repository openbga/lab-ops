"""Second confirmation of the seed, validator, form and cache changes at 2f2abcc.

Adopted from the independent review's second confirmation; three strict xfails are accepted
behaviour, documented where they are described."""

from collections import Counter
from datetime import timedelta
from html import unescape

import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.functional import SimpleLazyObject, lazy

from accounts.forms import SetPasswordForm
from accounts.models import User
from accounts.passwords import current_account, minimum_length
from core import demo
from core.management.commands import reset_demo
from core.models import Lab

from .test_review_password_rules import EIGHT, PHRASE, rule
from .test_review_password_rules_confirm import ADMIN_PHRASE, seed
from .test_review_password_rules_verify import cache_clock, stored

pytestmark = pytest.mark.django_db


def configured(validator):
    return override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            *settings.AUTH_PASSWORD_VALIDATORS,
            {"NAME": f"{validator.__module__}.{validator.__name__}"},
        ]
    )


@pytest.fixture
def demo_settings(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", PHRASE)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_PHRASE)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")


class DeploymentCheck:
    def get_help_text(self):
        return "Deployment advice."

    def validate(self, password, user=None):
        pass


class PositionalDefault(DeploymentCheck):
    def help_text_for(self, user, minimum=None, /, *, own):
        return self.get_help_text()


class VariadicPositional(DeploymentCheck):
    def help_text_for(self, user, *minimum, own):
        return self.get_help_text()


class DisabledAccountHook(DeploymentCheck):
    help_text_for = False


@pytest.mark.parametrize("validator", [PositionalDefault, VariadicPositional, DisabledAccountHook])
def test_legacy_help_hooks_still_open_the_password_form(make_user, validator):
    """Low, introduced at 2f2abcc. A parameter named minimum need not accept that keyword."""
    user = make_user("second-confirmation-help")
    with configured(validator):
        field = SetPasswordForm(user=user)["password1"]
        assert "Deployment advice." in str(field.help_text)


class KeywordHelp(DeploymentCheck):
    def help_text_for(self, user, *, own, **options):
        settings.CONFIRM_HELP_CALLS.append((own, options["minimum"]))
        return "Use the lab's <guide>."


def test_keyword_help_remains_escaped_and_follows_the_retained_field(make_user, settings):
    settings.CONFIRM_HELP_CALLS = []
    user = make_user("second-confirmation-keyword")
    with configured(KeywordHelp):
        form = SetPasswordForm(user=user, data={"password1": "poem", "password2": "poem"})
        bound = form["password1"]
        html = str(bound.as_field_group())
        assert bound is form["password1"]
        assert settings.CONFIRM_HELP_CALLS == [(True, None), (True, 15)]
        assert "<guide>" not in html and "Use the lab's <guide>." in unescape(html)
        assert "at least 15" in str(bound.help_text)


def test_revalidated_password_subwidget_keeps_the_error_association(make_user):
    """Low, introduced at 2f2abcc. Iterating a revalidated field must render current ARIA."""
    user = make_user("second-confirmation-widget")
    rule(staff=True)
    form = SetPasswordForm(user=user, data={"password1": EIGHT, "password2": EIGHT})
    assert form.is_valid()
    assert "aria-invalid" not in str(form["password1"][0])
    rule(staff=False)
    form.full_clean()
    assert "at least 15" in str(form.errors["password1"])
    assert 'aria-invalid="true"' in str(form["password1"])
    widget = str(form["password1"][0])
    assert 'aria-invalid="true"' in widget and "id_password1_error" in widget


def test_lazy_account_lookup_distinguishes_new_deleted_and_resaved(make_user):
    original = make_user("second-confirmation-identity")
    new = User(username="unsaved-clone", pk=original.pk)
    wrapped = SimpleLazyObject(lambda: new)
    assert current_account(wrapped).username == "unsaved-clone"
    assert minimum_length(wrapped) == 15
    other_reference = User.objects.get(pk=original.pk)
    original.delete()
    assert current_account(SimpleLazyObject(lambda: original)) is None
    assert current_account(other_reference) is None
    original.save()
    assert current_account(original).pk == original.pk


def test_seed_maps_refusal_codes_without_formatting_validator_messages(demo_settings, monkeypatch):
    def message():
        raise AssertionError("The deployment's private diagnostic was formatted")

    private_message = lazy(message, str)()

    def refuse(self, value, user=None):
        raise ValidationError(
            [
                ValidationError(private_message, code="password_too_short"),
                ValidationError(private_message, code="password_too_short"),
                ValidationError(private_message, code="site_policy"),
                ValidationError(private_message),
            ]
        )

    monkeypatch.setattr(DeploymentCheck, "validate", refuse)
    with configured(DeploymentCheck), pytest.raises(CommandError) as error:
        seed()
    diagnostic = str(error.value)
    assert "DEMO_ADMIN_PASSWORD" in diagnostic and "demo_admin" in diagnostic
    assert diagnostic.count("it is too short") == 1
    assert diagnostic.count("a password check of this deployment refused it") == 1
    assert ADMIN_PHRASE not in diagnostic and not Lab.objects.exists()


@pytest.mark.xfail(
    strict=True,
    reason="accepted: the seed checks each account as it will be created, before it exists, "
    "with no id or organization yet (seed_demo.published_passwords)",
)
@pytest.mark.parametrize("reading", ["foreign_key", "organization"])
def test_seed_validates_the_portal_organizations_it_actually_creates(
    demo_settings, monkeypatch, reading
):
    """Medium. The audience gap survives; 2f2abcc newly supplies incomplete organization data."""
    observed = {}

    def describe(user):
        if reading == "foreign_key":
            return user.client_id is not None
        if user.client is not None:
            return (user.client.account_number, user.client.name, user.client.organization_type)
        return None

    def record(self, password, user=None):
        observed[user.username] = describe(user)

    monkeypatch.setattr(DeploymentCheck, "validate", record)
    with configured(DeploymentCheck):
        seed()
    actual = {
        user.username: describe(user)
        for user in User.objects.filter(is_active=True).select_related("client")
    }
    assert len(actual) == 7 and observed.keys() == actual.keys()
    assert observed == actual


def test_reset_uses_the_settings_approved_before_erasure(demo_settings, monkeypatch):
    """Medium, introduced at 2f2abcc. A reset discards its checked settings and reads them again."""
    seed()
    checked = []
    erase = reset_demo.Command._erase

    def record(self, password, user=None):
        checked.append(user.username)

    def rotate_then_erase(command):
        # Both sets of synthetic values meet the rule. Only snapshot consistency is in question.
        monkeypatch.setenv("DEMO_PASSWORD", "Copper kettle among old papers")
        monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Orange folio beside the window")
        return erase(command)

    monkeypatch.setattr(DeploymentCheck, "validate", record)
    monkeypatch.setattr(reset_demo.Command, "_erase", rotate_then_erase)
    with configured(DeploymentCheck):
        seed("reset_demo", interactive=False)
    admin = User.objects.get(username="demo_admin")
    portal = User.objects.get(username="demo_client")
    checks_per_account = Counter(checked)
    assert len(checks_per_account) == 7
    original_values = (admin.check_password(ADMIN_PHRASE), portal.check_password(PHRASE))
    later_values = (
        admin.check_password("Orange folio beside the window"),
        portal.check_password("Copper kettle among old papers"),
    )
    result = (
        original_values,
        later_values,
        sorted(set(checks_per_account.values())),
    )
    assert result == ((True, True), (False, False), [1])


@pytest.mark.parametrize("reason", ["changed_database", "announcement"])
def test_reset_refuses_invalid_password_configuration_before_truncating(
    demo_settings, monkeypatch, reason
):
    """Low, incomplete at 2f2abcc. A refused reset should discover the refusal before erasure."""
    seed()
    original = User.objects.get(username="demo_admin").password
    admin_saw_lab = []
    truncations = []

    def check(self, password, user=None):
        if reason == "changed_database" and user.username == "demo_admin":
            exists = Lab.objects.filter(pk=1).exists()
            admin_saw_lab.append(exists)
            if not exists:
                raise ValidationError("Synthetic policy needs the laboratory setting.")

    def observe(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith("TRUNCATE "):
            truncations.append(True)
        return execute(sql, params, many, context)

    if reason == "announcement":
        # On a reset the private and public accounts will be recreated from these settings.
        monkeypatch.delenv("DEMO_ADMIN_PASSWORD")
    monkeypatch.setattr(DeploymentCheck, "validate", check)
    with connection.execute_wrapper(observe), configured(DeploymentCheck):
        if reason == "announcement":
            with pytest.raises(CommandError, match="it also opens demo_admin"):
                seed("reset_demo", interactive=False, announce_accounts=True)
        else:
            # Reusing the successful first validation may also let the reset complete.
            try:
                seed("reset_demo", interactive=False)
            except CommandError as error:
                assert "DEMO_ADMIN_PASSWORD" in str(error)
            else:
                assert admin_saw_lab == [True]
                return
    assert User.objects.get(username="demo_admin").password == original
    assert not truncations, (admin_saw_lab, "Refusal followed a rolled-back TRUNCATE")


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    strict=True,
    reason="accepted: a caller that wraps seed_demo in its own transaction holds what the "
    "password check reads (seed_demo.published_passwords); reset_demo takes the seed lock first",
)
def test_seed_entered_from_a_transaction_reaches_the_gate_without_validator_locks(
    demo_settings, monkeypatch
):
    """Medium, residual lock-order gap when the caller already has an open transaction."""

    class AtGate(Exception):
        pass

    held = []

    def observe(self, password, user=None):
        Lab.objects.exists()

    def gate():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() "
                "AND relation = %s::regclass AND granted",
                [Lab._meta.db_table],
            )
            held.extend(row[0] for row in cursor)
        raise AtGate

    monkeypatch.setattr(DeploymentCheck, "validate", observe)
    monkeypatch.setattr(demo, "hold_seed_lock", gate)
    # Inspect only the current backend. No other session is started or blocked.
    with transaction.atomic(), configured(DeploymentCheck), pytest.raises(AtGate):
        seed()
    assert not held, held


def test_cache_clean_reads_empty_deletes_and_mixed_expiry_cleanup():
    start = timezone.now().replace(microsecond=0) - timedelta(minutes=10)
    with cache_clock(start):
        for key in ["current", "other-current"]:
            cache.set(key, key, timeout=None)
        for key in ["old", "other-old"]:
            cache.set(key, key, timeout=1)
    with CaptureQueriesContext(connection) as clean:
        assert cache.get_many(["current", "absent"]) == {"current": "current"}
        cache.delete_many([])
    assert len(clean) == 1 and clean[0]["sql"].startswith("SELECT")
    assert stored("other-old")
    assert cache.get_many(["current", "old", "absent"]) == {"current": "current"}
    assert not stored("old") and not stored("other-old")
    assert cache.delete("absent") is False and cache.delete("current") is True
    assert cache.get("other-current") == "other-current"


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    strict=True,
    reason="accepted: Django's database cache removes an expired entry it reads; nothing here "
    "reads the cache in a read-only transaction",
)
def test_read_only_lookup_can_treat_an_expired_marker_as_missing():
    """Low, inherited from Django. A stored expired marker makes a lookup require a write."""
    start = timezone.now().replace(microsecond=0) - timedelta(minutes=10)
    with cache_clock(start):
        cache.set("expired-paper", "paper", timeout=1)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            assert cache.get("expired-paper") is None
    finally:
        cache.delete("expired-paper")
