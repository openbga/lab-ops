"""Verification of the round-one fixes, using the application's forms, commands and cache.

Adopted from the independent review's verification pass. One strict xfail is accepted behaviour:
a reset link of an account that stopped being shared works again until it expires."""

import os
import re
import select
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import pytest
from allauth.account.forms import EmailAwarePasswordResetTokenGenerator, default_token_generator
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from accounts.admin import OwnPasswordForm
from accounts.forms import PasswordGuidance, SetPasswordForm
from accounts.models import AccountSession, User
from accounts.passwords import (
    CHECKED_MINIMUM,
    SINGLE_FACTOR_MINIMUM,
    AccountSimilarityValidator,
    ExpectedPasswordValidator,
    context_names,
    current_account,
    laboratory_named,
    minimum_length,
)
from clients.models import Client as Organization
from core.models import AuditEvent, Lab, SecondFactorRule
from jobs.actor import system_account

from .conftest import PASSWORD
from .test_review_password_rules import PHRASE, reset_form, rule

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("route", ["change", "set", "reset", "admin-own", "admin-target", "add"])
def test_submitted_page_displays_the_minimum_that_refused_the_password(
    make_user, client, monkeypatch, route
):
    """Medium, #3 survives. The help in the HTTP response must agree with the error."""
    owner = make_user("verify-editor", is_staff=True, is_superuser=True)
    target = make_user("verify-target")
    data = {"password1": "poem", "password2": "poem", "usable_password": "true"}
    if route == "reset":
        url = reset_form(client, target)
    else:
        if route == "set":
            target.set_unusable_password()
            target.save(update_fields=["password"])
        client.force_login(owner if route in {"add", "admin-own", "admin-target"} else target)
        if route == "admin-own":
            url = reverse("admin:password_change")
            data = {"old_password": PASSWORD, "new_password1": "poem", "new_password2": "poem"}
        elif route == "admin-target":
            url = reverse("admin:auth_user_password_change", args=[target.pk])
        elif route == "add":
            url = reverse("admin:accounts_user_add")
            data.update(username="verify-new", email="new@example.invalid")
        elif route == "set":
            url = reverse("account_set_password")
        else:
            url = reverse("account_change_password")
            data["oldpassword"] = PASSWORD

    rule(staff=True)
    original = PasswordGuidance.draw_guidance
    drawn = []

    def draw_then_change(form, minimum=None):
        original(form, minimum)
        if form.is_bound and minimum is None:
            drawn.append(str(form.fields[form.guidance_field].help_text))
            # A synchronous change between constructing this form and checking it.
            # Both policies refuse this input.
            SecondFactorRule.objects.filter(pk=1).update(staff=False)

    monkeypatch.setattr(PasswordGuidance, "draw_guidance", draw_then_change)
    response = client.post(url, data)
    html = response.content.decode()
    assert response.status_code == 200
    assert len(drawn) == 1 and f"at least {15 if route == 'add' else 8} characters" in drawn[0]
    assert "It must contain at least 15 characters" in html
    assert not User.objects.filter(username="verify-new").exists()
    assert re.findall(r"(?:Your|The) password must contain at least (\d+) characters", html) == [
        "15"
    ]


@pytest.mark.parametrize("mode", ["empty", "mismatch"])
def test_reclean_resets_the_length_left_on_a_lazy_account(make_user, mode):
    person = make_user("verify-lazy")
    lazy_user = SimpleLazyObject(lambda: person)
    data = {"old_password": PASSWORD, "new_password1": PHRASE, "new_password2": PHRASE}
    form = OwnPasswordForm(lazy_user, data=data)
    form.full_clean()
    assert not form.errors and getattr(person, CHECKED_MINIMUM) == 15
    data["new_password2"] = "" if mode == "empty" else "Different synthetic phrase"
    form.full_clean()
    assert "new_password2" in form.errors and not hasattr(person, CHECKED_MINIMUM)
    rule(staff=True)
    data["new_password1"] = data["new_password2"] = "poem"
    form.full_clean()
    assert getattr(person, CHECKED_MINIMUM) == 8 and "at least 8" in str(form.errors)
    # Medium, #3 survives a second validation even with the correct account marker.
    assert "at least 8" in str(form[form.guidance_field].help_text)


def test_account_names_use_the_corrected_record_and_unsaved_input(make_user):
    person = make_user("verify-reader", first_name="Tamarack")
    User.objects.filter(pk=person.pk).update(first_name="BlueHorizon")
    similarity = AccountSimilarityValidator(user_attributes=["first_name"])
    similarity.validate("Tamarack", person)
    with pytest.raises(ValidationError, match="too similar"):
        similarity.validate("BlueHorizon", person)
    assert "Tamarack" not in context_names(person)
    assert "BlueHorizon" in context_names(person)
    new = User(username="unsaved-scribe", first_name="Zircon")
    assert current_account(new) is new and "Zircon" in context_names(new)
    job = system_account()
    assert current_account(job).pk == job.pk and not job.has_usable_password()
    assert job.username in context_names(job)


def test_an_unsaved_instance_with_an_assigned_pk_keeps_its_own_names(make_user):
    """Low, introduced by #4. Unsaved model instances supply their own proposed names."""
    existing = make_user("on-file")
    proposed = User(pk=existing.pk, username="proposed-scribe")
    assert proposed._state.adding
    assert "proposed-scribe" in context_names(proposed)


def test_an_unsaved_instance_with_an_assigned_pk_gets_new_account_guidance(make_user):
    """Low, predates the fixes. An unsaved instance's stated minimum is always 15."""
    existing = make_user("on-file")
    rule(staff=True)
    proposed = User(pk=existing.pk, username="proposed-scribe")
    assert proposed._state.adding and minimum_length(existing) == 8
    assert minimum_length(proposed) == 15


@pytest.mark.parametrize("check", ["context", "similarity"])
def test_a_deleted_accounts_former_names_are_not_current(make_user, check):
    """Low, #4 still compares former names when its second read returns no account."""
    person = make_user("verify-deletion", first_name="FormerNotebook")
    stale = User.objects.get(pk=person.pk)
    person.delete()
    assert not User.objects.filter(pk=stale.pk).exists()
    try:
        if check == "context":
            assert "FormerNotebook" not in context_names(stale)
        else:
            AccountSimilarityValidator(user_attributes=["first_name"]).validate(
                "FormerNotebook", stale
            )
    except User.DoesNotExist:
        pass  # Explicitly refusing a missing account would also avoid comparing its old name.


class DeploymentGuidance:
    """A validator using the account-specific help signature supported before the fixes."""

    def get_help_text(self):
        return "Deployment guidance."

    def help_text_for(self, user, *, own):
        return "Deployment guidance for this account."

    def validate(self, password, user=None):
        pass


def test_existing_account_specific_validator_help_can_still_draw_a_form(make_user, settings):
    """Medium, introduced by #3. Configured validators must keep working on password pages."""
    person = make_user("verify-custom")
    settings.AUTH_PASSWORD_VALIDATORS = [
        *settings.AUTH_PASSWORD_VALIDATORS,
        {"NAME": f"{__name__}.DeploymentGuidance"},
    ]
    form = SetPasswordForm(user=person)
    assert "Deployment guidance for this account" in str(form["password1"].help_text)


@pytest.mark.parametrize(
    "unit,short",
    [
        ("Qzmvtrxbkplhfj", True),
        ("Qzmvtrxbkplhfji", False),
        ("Qzmvtr xb kplhf", False),
        ("Qzmvtrxbkplhfß", True),
    ],
)
def test_repeat_boundary_counts_the_characters_in_the_input(unit, short):
    """Low, #16 incompletely fixed. The 14/15 cutoff counts input code points, including spaces."""
    assert (len(unit) < 15) is short
    if short:
        with pytest.raises(ValidationError, match="repeats"):
            ExpectedPasswordValidator().validate(unit * 2)
    else:
        ExpectedPasswordValidator().validate(unit * 2)


@pytest.mark.parametrize("name", ["I", "Ås", "Elm"])
def test_short_laboratory_names_use_whole_password_comparisons(name):
    Lab.objects.create(pk=1, name=name)
    validator = ExpectedPasswordValidator()
    with pytest.raises(ValidationError) as error:
        validator.validate(f"8391 {name} 0276 / 54")
    assert error.value.code == "password_context_name"
    validator.validate(f"An orchard beyond {name} at sunrise")


def test_ordinary_recovery_links_are_compatible_and_refused_while_locked(make_user):
    person = make_user("verify-recovery")
    legacy = EmailAwarePasswordResetTokenGenerator()
    assert legacy.check_token(person, default_token_generator.make_token(person))
    ordinary = legacy.make_token(person)
    assert default_token_generator.check_token(person, ordinary)
    User.objects.filter(pk=person.pk).update(credentials_locked=True)
    person.refresh_from_db()
    assert not default_token_generator.check_token(person, ordinary)


@pytest.mark.xfail(
    strict=True,
    reason="accepted: a reset link does not work while its account is shared (accounts.tokens); "
    "once an account stops being shared, a link mailed before works until it expires, as for "
    "any account",
)
def test_sharing_transition_changes_recovery_state_after_unlock(make_user):
    """Medium, #5 partially fixed. 'Stops working once' requires a lasting state change."""
    person = make_user("verify-sharing")

    def fingerprint():
        person.refresh_from_db()
        return default_token_generator._make_hash_value(person, 123)

    before = fingerprint()
    User.objects.filter(pk=person.pk).update(credentials_locked=True)
    assert fingerprint() != before
    User.objects.filter(pk=person.pk).update(credentials_locked=False)
    # Compare only state for the same synthetic timestamp. No link is created or redeemed.
    changed = fingerprint() != before
    assert changed, "Unlock restored the exact pre-sharing recovery state"


def test_setup_uses_saved_name_and_restores_proposed_names_after_errors(monkeypatch):
    Lab.objects.create(pk=1, name="Silver Ledger")
    with laboratory_named("Outer proposed name"):
        with pytest.raises(ValueError), laboratory_named("Inner proposed name"):
            assert "Inner proposed name" in context_names(None)
            raise ValueError
        assert "Outer proposed name" in context_names(None)
        assert "Inner proposed name" not in context_names(None)
    assert "Outer proposed name" not in context_names(None)
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", "Silver Ledger 8427")
    with pytest.raises(CommandError, match="name of this laboratory"):
        call_command(
            "setup_lab", admin_username="verify-setup", admin_email="setup@example.invalid"
        )
    assert not User.objects.filter(username="verify-setup").exists()
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", PHRASE)
    call_command(
        "setup_lab",
        admin_username="verify-setup",
        admin_email="setup@example.invalid",
        stdout=StringIO(),
    )
    saved = User.objects.get(username="verify-setup").password
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", "too short")
    call_command(
        "setup_lab",
        admin_username="verify-setup",
        lab_name="Renamed Ledger",
        update_lab=True,
        stdout=StringIO(),
    )
    assert Lab.objects.get(pk=1).name == "Renamed Ledger"
    assert User.objects.get(username="verify-setup").password == saved


class RefuseOneAccount:
    def get_help_text(self):
        return "Operator-defined validation."

    def validate(self, password, user=None):
        if user.username == settings.VERIFY_REFUSED_ACCOUNT:
            raise ValidationError("Unacceptable field value: %(value)s", params={"value": password})


def test_default_seed_diagnostic_excludes_a_value_identical_to_an_account_name(monkeypatch):
    """Low, introduced by #8. The release validators must keep even refused values private."""
    synthetic_value = "demo_client_services"
    monkeypatch.setenv("DEMO_PASSWORD", synthetic_value)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", PHRASE)
    with pytest.raises(CommandError, match="DEMO_PASSWORD") as error:
        call_command("seed_demo", verbosity=0, stdout=StringIO(), stderr=StringIO())
    assert not Lab.objects.exists()
    leaks_value = synthetic_value in str(error.value)
    assert not leaks_value, "The account label repeated the complete refused value"


@pytest.mark.parametrize("path", [".env.example", "website/index.html", "README.md"])
def test_demo_setup_instructions_give_the_required_minimum(path):
    """Low, introduced by 4351616. The trial and configuration examples must work as printed."""
    content = (settings.BASE_DIR / path).read_text()
    line = next(
        line
        for line in content.splitlines()
        if "password" in line.lower() and re.search(r"(?:at least|minimum) \d+ characters", line)
    )
    assert (
        int(re.search(r"(?:at least|minimum) (\d+) characters", line)[1]) == SINGLE_FACTOR_MINIMUM
    )


def test_published_portal_password_is_checked_before_application_writes(monkeypatch):
    """Low, new #8 path. The brief calls for refusal before anything is written."""
    # A decorated portal surname is distinct from every other account's name.
    monkeypatch.setenv("DEMO_PASSWORD", "Water Authority 2407")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", PHRASE)
    inserted_tables = set()

    def record_inserts(execute, sql, params, many, context):
        match = re.match(r'INSERT INTO "([^"]+)"', sql)
        if match:
            inserted_tables.add(match[1])
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record_inserts), pytest.raises(CommandError) as error:
        call_command("seed_demo", verbosity=0, stdout=StringIO(), stderr=StringIO())
    assert "demo_client_water" in str(error.value)
    assert not Lab.objects.exists() and not User.objects.exists()
    assert not inserted_tables, sorted(inserted_tables)


@pytest.mark.parametrize(
    "command,account,setting",
    [
        ("seed_demo", "demo_admin", "DEMO_ADMIN_PASSWORD"),
        ("seed_demo", "demo_client_water", "DEMO_PASSWORD"),
        ("reset_demo", "demo_client_water", "DEMO_PASSWORD"),
    ],
)
def test_seed_refusal_rolls_back_and_keeps_the_password_out_of_diagnostics(
    monkeypatch, command, account, setting
):
    """Medium, introduced by #8. A validator's error must not print the supplied setting value."""
    chosen = {"DEMO_ADMIN_PASSWORD": "Synthetic admin orchard phrase", "DEMO_PASSWORD": PHRASE}
    for key, value in chosen.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    options = {}
    if command == "reset_demo":
        call_command("seed_demo", verbosity=0, stdout=StringIO())
        Organization.objects.create(name="Synthetic rollback marker", account_number="MARKER")
        options["interactive"] = False

    def snapshot():
        return (
            list(Lab.objects.order_by("pk").values_list("pk", "name")),
            list(User.objects.order_by("pk").values_list("pk", "username", "password")),
            list(Organization.objects.order_by("pk").values_list("pk", "name")),
            list(AuditEvent.objects.order_by("pk").values_list("pk", flat=True)),
        )

    before = snapshot()
    stdout, stderr = StringIO(), StringIO()
    with (
        override_settings(
            AUTH_PASSWORD_VALIDATORS=[
                *settings.AUTH_PASSWORD_VALIDATORS,
                {"NAME": f"{__name__}.RefuseOneAccount"},
            ],
            VERIFY_REFUSED_ACCOUNT=account,
        ),
        pytest.raises(CommandError) as error,
    ):
        call_command(command, verbosity=0, stdout=stdout, stderr=stderr, **options)
    assert snapshot() == before
    assert account in str(error.value) and setting in str(error.value)
    diagnostic = str(error.value) + stdout.getvalue() + stderr.getvalue()
    leaks_value = any(value in diagnostic for value in chosen.values())
    assert not leaks_value, "The refusal included the synthetic setting value"


@contextmanager
def cache_clock(instant):
    with (
        patch("django.core.cache.backends.db.tz_now", return_value=instant),
        patch("time.time", return_value=instant.timestamp()),
    ):
        yield


def stored(key):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM lab_ops_cache WHERE cache_key = %s", [cache.make_key(key)])
        return cursor.fetchone() is not None


@pytest.mark.parametrize("operation", ["set", "add", "touch"])
def test_write_respects_an_expiry_in_the_current_second(operation):
    """Low, #11 incomplete; inherited add/touch. Reads and writes agree on when a key expires."""
    start = timezone.now().replace(microsecond=0)
    with cache_clock(start):
        cache.set("paper", 1, timeout=1)
        cache.set("read-control", 1, timeout=1)
        cache.set("long-lived", 2, timeout=None)
    with cache_clock(start + timedelta(seconds=1, milliseconds=500)):
        assert cache.get("read-control") is None
        if operation == "add":
            assert cache.add("paper", 3, timeout=10)
            assert cache.get("paper") == 3
        elif operation == "touch":
            assert not cache.touch("paper", timeout=10)
        else:
            cache.set("ink", 4, timeout=10)
            assert not stored("paper"), "set kept an expired row"
        assert cache.get("long-lived") == 2


def test_cache_live_add_touch_expiry_rounding_and_clear():
    start = timezone.now().replace(microsecond=750000)
    with cache_clock(start):
        cache.set("paper", 1, timeout=10)
        assert not cache.add("paper", 2, timeout=20)
        assert cache.get("paper") == 1
        assert cache.touch("paper", timeout=20)
        assert not cache.touch("missing", timeout=20)
        cache.set("persistent", 3, timeout=None)
    with cache_clock(start + timedelta(seconds=20)):
        assert cache.get("paper") == 1
    with cache_clock(start + timedelta(seconds=22)):
        assert cache.add("paper", 4, timeout=20)
        assert cache.get("paper") == 4 and cache.get("persistent") == 3
        cache.clear()
        assert not stored("paper") and not stored("persistent")


def test_zero_default_timeout_disables_caching_immediately(settings):
    """Low, introduced by #14. A configured zero timeout should work like an explicit zero."""
    settings.CACHES = {"default": {**settings.CACHES["default"], "TIMEOUT": 0}}
    with cache_clock(timezone.now().replace(microsecond=750000)):
        cache.set("explicit-zero", "ink", timeout=0)
        assert cache.get("explicit-zero") is None
        cache.set("implicit-zero", "ink")
        assert cache.get("implicit-zero") is None


@pytest.mark.parametrize("operation", ["delete", "delete_many"])
def test_deletion_also_removes_previously_expired_rows(operation):
    """Low, #11 documentation added by the fixes says every write removes expired entries."""
    start = timezone.now().replace(microsecond=0)
    with cache_clock(start):
        cache.set("past", 1, timeout=1)
        cache.set("to-remove", 2, timeout=None)
    with cache_clock(start + timedelta(minutes=10)):
        if operation == "delete":
            cache.delete("to-remove")
        else:
            cache.delete_many(["to-remove"])
        assert not stored("to-remove")
        assert not stored("past"), "Deleting another key kept the ten-minute-old row"


@pytest.mark.django_db(transaction=True)
def test_manage_changepassword_prompts_retries_and_closes_both_sessions(make_user):
    """Run the actual operator entry point on this pytest database."""
    person = make_user("verify-command")
    browsers = [Client(), Client()]
    for browser in browsers:
        browser.force_login(person)
    sessions = person.account_sessions
    assert sessions.filter(ended_at__isnull=True).count() == 2
    name = connection.settings_dict["NAME"]
    assert name.startswith("test_")
    parts = urlsplit(os.environ["DATABASE_URL"])
    env = {**os.environ, "DATABASE_URL": urlunsplit(parts._replace(path="/" + name))}
    env.pop("TEST_DATABASE_NAME", None)

    def run(username, replies):
        result = subprocess.run(
            [sys.executable, "manage.py", "changepassword", username],
            input="".join(line + "\n" for line in replies),
            capture_output=True,
            text=True,
            env=env,
            start_new_session=True,
            timeout=25,
        )
        return result.returncode, result.stdout + result.stderr

    code, output = run("missing-verify-command", [])
    assert code and "does not exist" in output and "Password:" not in output
    code, output = run(person.username, [""])
    assert code and "aborted" in output
    code, output = run(person.username, ["poem", "poem"] * 3)
    assert code and "after 3 attempts" in output and output.count("at least 15 characters") == 3
    person.refresh_from_db()
    assert person.check_password(PASSWORD) and sessions.filter(ended_at__isnull=True).count() == 2
    termios = pytest.importorskip("termios")
    master, slave = os.openpty()
    with (
        os.fdopen(master, "wb", buffering=0) as keyboard,
        os.fdopen(slave, "rb", buffering=0) as tty,
    ):
        with subprocess.Popen(
            [sys.executable, "manage.py", "changepassword", person.username],
            stdin=tty,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        ) as process:
            prompts = bytearray()
            try:
                for first, second in [("first", "different"), ("poem", "poem"), (PHRASE, PHRASE)]:
                    for prompt, reply in [(b"Password: ", first), (b"Password (again): ", second)]:
                        while not prompts.endswith(prompt):
                            assert select.select([process.stderr], [], [], 15)[0], (
                                "Prompt timed out"
                            )
                            block = os.read(process.stderr.fileno(), 4096)
                            assert block, "The command exited before the next prompt"
                            prompts.extend(block)
                        assert not termios.tcgetattr(tty)[3] & termios.ECHO
                        keyboard.write((reply + "\n").encode())
                stdout, stderr = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
            code = process.returncode
            output = (stdout + prompts + stderr).decode()
    assert code == 0 and "Passwords do not match" in output and "at least 15" in output
    assert "changed successfully" in output and output.count("Password (again):") == 3
    assert output.count("Password:") == 3 and PHRASE not in output
    person.refresh_from_db()
    assert person.check_password(PHRASE)
    assert (
        sessions.filter(ending=AccountSession.Ending.NEW_PASSWORD, ended_at__isnull=False).count()
        == 2
    )
    assert all(
        browser.get(reverse("account_change_password")).status_code == 302 for browser in browsers
    )


def test_count_queries_on_one_ordinary_login(make_user, client):
    person = make_user("verify-budget")
    for accepted in [False, True]:
        cache.clear()
        with CaptureQueriesContext(connection) as captured:
            response = client.post(
                reverse("account_login"),
                {"login": person.username, "password": PASSWORD if accepted else "typo"},
            )
        assert response.status_code == (302 if accepted else 200)
        sql = [row["sql"] for row in captured if "lab_ops_cache" in row["sql"]]
        counts = sum(query.startswith("SELECT COUNT(*)") for query in sql)
        sweeps = sum(query.startswith("DELETE") and '"expires" <' in query for query in sql)
        print(
            f"accepted={accepted} statements={len(captured)} cache={len(sql)} "
            f"counts={counts} expiration_deletes={sweeps}"
        )
