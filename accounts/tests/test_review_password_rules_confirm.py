"""Confirmation of the password verification fixes on synthetic forms, seed data and cache keys.

Adopted from the independent review's confirmation pass; three strict xfails are accepted
behaviour, each documented where it is described."""

import html
import os
import re
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.forms import SetPasswordForm
from accounts.models import User
from accounts.passwords import (
    AccountSimilarityValidator,
    ExpectedPasswordValidator,
    LengthByFactorValidator,
    context_names,
    current_account,
    laboratory_named,
    minimum_length,
)
from core import demo
from core.management.commands import seed_demo
from core.models import Lab

from .test_review_password_rules import PHRASE, rule
from .test_review_password_rules_verify import cache_clock, stored

pytestmark = pytest.mark.django_db
ADMIN_PHRASE = "Ochre sketch beside a copper tray"


def configured(validator):
    return override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            *settings.AUTH_PASSWORD_VALIDATORS,
            {"NAME": f"{__name__}.{validator.__name__}"},
        ]
    )


def seed(command="seed_demo", **options):
    return call_command(command, verbosity=0, stdout=StringIO(), stderr=StringIO(), **options)


@pytest.fixture
def seed_settings(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", PHRASE)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_PHRASE)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")


class LengthWithAccountHelp(LengthByFactorValidator):
    def help_text_for(self, user, *, own):
        return "The deployment's length advice."


class LengthAwareHelp:
    def get_help_text(self):
        return "The deployment's length advice."

    def help_text_for(self, user, *, own, minimum):
        return self.get_help_text()

    def validate(self, password, user=None):
        pass


@pytest.mark.parametrize("validator", [LengthWithAccountHelp, LengthAwareHelp])
def test_account_specific_help_draws_for_configured_validators(make_user, validator):
    """Medium. The subclass still fails; the unrelated length-aware hook regressed at 83f4f7c."""
    user = make_user("confirmation-help")
    with override_settings(AUTH_PASSWORD_VALIDATORS=[{"NAME": f"{__name__}.{validator.__name__}"}]):
        field = SetPasswordForm(user=user)["password1"]
        # Adopted with one change: the help is HTML, so the apostrophe arrives escaped.
        assert "The deployment's length advice." in html.unescape(str(field.help_text))


def test_a_field_group_that_triggers_validation_renders_the_checked_length(make_user):
    """Low, existing guidance defect partly fixed. A retained bound field renders its own help."""
    user = make_user("confirmation-render")
    rule(staff=True)
    form = SetPasswordForm(user=user, data={"password1": "poem", "password2": "poem"})
    field = form["password1"]
    assert "at least 8" in str(field.help_text)
    rule(staff=False)
    html = str(field.as_field_group())
    assert "It must contain at least 15 characters" in html
    assert "Your password must contain at least 15 characters" in str(form["password1"].help_text)
    rendered = re.search(r"Your password must contain at least (\d+) characters", html)
    assert rendered and rendered[1] == "15"


@pytest.mark.parametrize("check", ["context", "similarity"])
def test_deleting_the_same_model_instance_discards_its_former_names(make_user, check):
    """Low, survives 83f4f7c. Both references to a deleted account have the same lookup result."""
    user = make_user("confirmation-deleted", first_name="Ultramarine")
    other_reference = User.objects.get(pk=user.pk)
    user.delete()
    assert current_account(other_reference) is None
    assert minimum_length(user) == 15
    if check == "context":
        assert "Ultramarine" not in context_names(user)
    else:
        AccountSimilarityValidator(user_attributes=["first_name"]).validate("Ultramarine", user)


def test_nested_laboratory_names_restore_the_saved_name_switch():
    Lab.objects.create(pk=1, name="Existing laboratory")
    with laboratory_named("Proposed outer"):
        assert "Existing laboratory" in context_names(None)
        with pytest.raises(ValueError), laboratory_named("Known seed lab", read_saved=False):
            with laboratory_named("Proposed inner"), CaptureQueriesContext(connection) as captured:
                names = context_names(User(pk=17, username="new-account"))
            assert len(captured) == 0
            assert {"Proposed outer", "Known seed lab", "Proposed inner", "new-account"} <= set(
                names
            )
            assert "Existing laboratory" not in names
            raise ValueError("synthetic early exit")
        assert "Existing laboratory" in context_names(None)
        assert "Known seed lab" not in context_names(None)
    assert "Proposed outer" not in context_names(None)


class ReadLaboratorySetting:
    def get_help_text(self):
        return "The laboratory's configured advice."

    def validate(self, password, user=None):
        Lab.objects.filter(pk=1).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("deployment_validator", [False, True], ids=["release", "orm-validator"])
def test_seed_holds_no_application_table_lock_when_it_reaches_its_advisory_lock(
    seed_settings, monkeypatch, deployment_validator
):
    """Medium, introduced by 83f4f7c for a configured validator that reads a laboratory setting."""
    held = []

    class ReachedSeedLock(Exception):
        pass

    def inspect_lock():
        # Observe only our own backend before it acquires the gate. No competing writer is needed.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() AND l.granted AND c.relkind IN ('r', 'p') "
                "AND c.relnamespace = current_schema()::regnamespace",
            )
            held.extend(cursor.fetchall())
        raise ReachedSeedLock

    validators = list(settings.AUTH_PASSWORD_VALIDATORS)
    if deployment_validator:
        validators.append({"NAME": f"{__name__}.ReadLaboratorySetting"})
    monkeypatch.setattr(demo, "hold_seed_lock", inspect_lock)
    with override_settings(AUTH_PASSWORD_VALIDATORS=validators), pytest.raises(ReachedSeedLock):
        seed()
    assert not held, held


class ObserveAudience:
    def get_help_text(self):
        return "Account audience."

    def validate(self, password, user=None):
        settings.CONFIRM_SEED_AUDIENCES[user.username] = user.client is not None


@pytest.mark.xfail(
    strict=True,
    reason="accepted: the seed checks each account as it will be created, before it exists, "
    "with no id or organization yet (seed_demo.published_passwords)",
)
def test_seed_validator_sees_the_portal_audience_that_is_saved(seed_settings, settings):
    """Medium, introduced by 83f4f7c. A portal account is a client account during validation."""
    settings.CONFIRM_SEED_AUDIENCES = {}
    with configured(ObserveAudience):
        seed()
    portal_names = set(User.objects.filter(client__isnull=False).values_list("username", flat=True))
    assert portal_names == {"demo_client", "demo_client_water"}
    seen = settings.CONFIRM_SEED_AUDIENCES
    assert set(seen) == set(User.objects.filter(is_active=True).values_list("username", flat=True))
    assert {name for name, portal in seen.items() if portal} == portal_names


@pytest.mark.xfail(
    strict=True,
    reason="accepted: the seed checks both password settings on every run, as it checks "
    "their length",
)
def test_repeat_seed_does_not_validate_an_admin_password_it_will_never_set(
    seed_settings, monkeypatch
):
    """Low, introduced by 83f4f7c. Reconciliation preserves a pre-existing admin's password."""
    seed()
    admin = User.objects.get(username="demo_admin")
    original = admin.password
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "OneNote" * 4)
    seed()
    admin.refresh_from_db()
    assert admin.password == original


def test_seed_saves_the_same_admin_setting_it_checked(seed_settings, monkeypatch):
    """Low, introduced by 83f4f7c. An embedded command should use one settings snapshot."""
    next_setting = "Pale leaves on the far bank"
    observed = []

    def read_setting(key, default=None):
        result = os.environ.get(key, default)
        if key == "DEMO_ADMIN_PASSWORD":
            monkeypatch.setenv(key, next_setting)
        return result

    def record_input(self, password, user=None):
        if user.username == "demo_admin":
            observed.append(password)

    # Simulate the host rotating its setting after a read. Both values meet the release rule.
    environment = SimpleNamespace(get=read_setting)
    monkeypatch.setattr(seed_demo, "os", SimpleNamespace(environ=environment))
    monkeypatch.setattr(ObserveAudience, "validate", record_input)
    with configured(ObserveAudience):
        seed()
    admin = User.objects.get(username="demo_admin")
    assert len(observed) == 1
    agrees = admin.check_password(observed[0])
    assert agrees, "The validation and the write used different setting values"


def test_reset_refuses_invalid_seed_settings_before_erasure(seed_settings, monkeypatch):
    """Low, pre-existing reset path. Up-front refusal should precede reset's write as well."""
    seed()
    original = User.objects.get(username="demo_admin").password
    monkeypatch.setenv("DEMO_PASSWORD", "Water Authority 2407")
    truncations = []

    def count_truncate(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith("TRUNCATE "):
            truncations.append(True)
        return execute(sql, params, many, context)

    with (
        connection.execute_wrapper(count_truncate),
        pytest.raises(CommandError, match="DEMO_PASSWORD"),
    ):
        seed("reset_demo", interactive=False)
    assert User.objects.get(username="demo_admin").password == original
    assert not truncations, "The previous demo was truncated and rolled back before refusal"


def test_seed_diagnostic_keeps_a_substring_of_an_account_name_private(seed_settings, monkeypatch):
    """Medium, survives the 83f4f7c redaction. A longer account label can contain the full input."""
    sample = "demo_client_serv"
    monkeypatch.setenv("DEMO_PASSWORD", sample)
    with pytest.raises(CommandError, match="DEMO_PASSWORD") as error:
        seed()
    reveals_sample = sample in str(error.value)
    assert not reveals_sample, "The diagnostic included the complete synthetic setting value"


class QuotedFieldError:
    def get_help_text(self):
        return "Deployment policy."

    def validate(self, password, user=None):
        raise ValidationError("Rejected field value %(value)r", params={"value": password})


def test_seed_diagnostic_keeps_a_validators_quoted_value_private(seed_settings, monkeypatch):
    """Medium, survives 83f4f7c. A quoted diagnostic must keep the private setting opaque."""
    sample = r"Ochre sketch\folio beside the dock"
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", sample)
    with (
        configured(QuotedFieldError),
        pytest.raises(CommandError, match="DEMO_ADMIN_PASSWORD") as error,
    ):
        seed()
    reveals_sample = sample in str(error.value) or repr(sample) in str(error.value)
    assert not reveals_sample, "The diagnostic contains the private setting in reversible form"


@pytest.mark.parametrize("separator", [" ", "\u00a0", "\t"], ids=["space", "no-break-space", "tab"])
def test_two_short_units_with_one_whitespace_separator_are_consistent(separator):
    """Low, introduced by 83f4f7c. Two short copies separated by whitespace previously agreed."""
    unit = "Zan8fQ!"
    with pytest.raises(ValidationError) as refusal:
        ExpectedPasswordValidator().validate(separator.join([unit, unit]))
    assert refusal.value.code == "password_repeated"


def test_repeat_cutoff_counts_a_punctuation_separator_as_typed():
    short = "Qzmvtrxbkplhf"
    long = short + "w"
    validator = ExpectedPasswordValidator()
    assert len(short + "-") == 14 and len(long + "-") == 15
    for trailing in ("", "-"):
        with pytest.raises(ValidationError) as refusal:
            validator.validate(short + "-" + short.upper() + trailing)
        assert refusal.value.code == "password_repeated"
        validator.validate(long + "-" + long.upper() + trailing)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("key,expected", [("stationery", "paper"), ("missing", None)])
def test_read_only_transaction_can_read_a_live_or_absent_cache_key(key, expected):
    """Medium, introduced by 83f4f7c. A live cache hit or miss formerly required only a SELECT."""
    cache.set("stationery", "paper", timeout=None)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            assert cache.get(key) == expected
    finally:
        cache.delete("stationery")


@pytest.mark.xfail(
    strict=True,
    reason="accepted: Django's database cache; nothing here asks whether a key is present",
)
def test_cache_membership_and_value_agree_at_the_exact_expiry():
    """Low, inherited by 83f4f7c. Membership and retrieval disagree at the expiry instant."""
    start = timezone.now().replace(microsecond=0)
    with cache_clock(start):
        cache.set("exact-boundary", "paper", timeout=2)
    with cache_clock(start + timedelta(seconds=2)):
        present = "exact-boundary" in cache
        assert present == (cache.get("exact-boundary") is not None)


@pytest.mark.xfail(
    strict=True,
    reason="accepted: the operations guide now says an expired entry stays until the cache "
    "is next written",
)
def test_the_next_request_removes_idle_rows_as_operations_describes(client):
    """Low, wording introduced by 83f4f7c. Operations says the next request removes idle rows."""
    start = timezone.now().replace(microsecond=0) - timedelta(minutes=10)
    with cache_clock(start):
        cache.set("idle-paper", 1, timeout=2)
    assert stored("idle-paper")
    page = client.get(reverse("account_login"))
    assert page.status_code == 200
    assert not stored("idle-paper"), "A normal sign-in page GET left the expired row on disk"


def test_cache_preserves_positive_fractional_intervals_and_expires_nonpositive_ones(settings):
    settings.CACHES = {"default": {**settings.CACHES["default"], "TIMEOUT": -1}}
    start = timezone.now().replace(microsecond=750000)
    with cache_clock(start):
        cache.set("instant-default", "a")
        cache.set("instant-explicit", "b", timeout=-0.125)
        assert cache.get_many(["instant-default", "instant-explicit"]) == {}
        cache.set("fractional", "c", timeout=0.5)
        cache.set("forever", "d", timeout=None)
    with cache_clock(start + timedelta(milliseconds=500)):
        assert cache.get("fractional") == "c"
    with cache_clock(start + timedelta(days=1)):
        assert cache.get("fractional") is None and cache.get("forever") == "d"
