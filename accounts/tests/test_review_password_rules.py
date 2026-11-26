"""Independent password-policy, page, provisioning and cache contracts on synthetic data.

Adopted from the independent review of the password rules (round 1). The strict xfails are
behaviours the review proved and the project accepts, each documented where it is described:
Django's createsuperuser without prompts, the rule read when the password is checked rather than
again at the save (accounts.passwords), allauth's give-back of equal timestamps and its clearing of
an address's failures after a completed second factor (Security settings, Failed sign-ins), the
null character Django's form fields refuse, and the sign-in form keeping what was typed in its
username field."""

import base64
import logging
import pickletools
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from time import time
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.auth.password_validation import validate_password
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection, connections, transaction
from django.test import Client as Browser
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from accounts.roles import ROLES
from clients.models import Client
from core import security_settings
from core.models import AuditEvent, Lab, SecondFactorRule
from jobs.actor import system_account

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db
PHRASE = "Copper sails beside a frosted orchard"
EIGHT = "vT4 quie"
TABLE = "lab_ops_cache"


def rule(**parts):
    SecondFactorRule.objects.update_or_create(pk=1, defaults=parts)


def reset_form(browser, person):
    from allauth.account.forms import default_token_generator
    from allauth.account.utils import user_pk_to_url_str

    url = reverse(
        "account_reset_password_from_key",
        args=[user_pk_to_url_str(person), default_token_generator.make_token(person)],
    )
    page = browser.get(url, follow=True)
    assert page.status_code == 200 and page.context["form"].fields.get("password1")
    return page.redirect_chain[-1][0]


def effective_minimum(person):
    try:
        validate_password("quartz", person)
    except ValidationError as exc:
        return next(
            e.params["min_length"] for e in exc.error_list if e.code == "password_too_short"
        )
    pytest.fail("The configured validators gave no length refusal")


@pytest.mark.parametrize("existing", [False, True], ids=["first-setup", "renaming-at-setup"])
def test_setup_checks_the_laboratory_name_being_saved(monkeypatch, existing):
    name = "Tern Orchard Laboratory"
    if existing:
        Lab.objects.create(pk=1, name="First Example Laboratory")
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", name + " 2048")
    with pytest.raises(CommandError, match="name of this laboratory"):
        call_command(
            "setup_lab",
            lab_name=name,
            update_lab=existing,
            admin_username="operator",
            admin_email="operator@example.invalid",
            stdout=StringIO(),
        )
    assert not User.objects.filter(username="operator").exists()
    assert not Lab.objects.filter(name=name).exists()


def test_three_letter_laboratory_name_is_compared_as_a_whole():
    Lab.objects.create(pk=1, name="Elm")
    with pytest.raises(ValidationError):
        validate_password("Elm 1048 - 2976")
    validate_password("The elm beside the copper orchard")


def test_repeating_a_long_passage_isnt_a_short_text_repeat():
    passage = (
        "Herons lifted from the silver marsh as the cart passed the far end of the orchard. "
        "By sundown the ferry had reached the west bank and the driver folded the map. "
    )
    validate_password(passage * 2)


def test_empty_optional_account_details_and_edge_spaces_round_trip(make_user, client):
    person = make_user("verifier")
    User.objects.filter(pk=person.pk).update(email="", first_name="", last_name="")
    person.refresh_from_db()
    client.force_login(person)
    chosen = "  桜 quartz élan  "
    result = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": chosen, "password2": chosen},
    )
    assert result.status_code == 302
    person.refresh_from_db()
    assert person.check_password(chosen) and not person.check_password(chosen.strip())


def test_current_direct_grant_status_and_scheduled_identity(make_user):
    person = make_user("direct-reader")
    grant = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    rule(patient_information=True)
    person.user_permissions.add(grant)
    assert effective_minimum(person) == 8
    person.user_permissions.remove(grant)
    assert effective_minimum(person) == 15
    rule(admin_console=True, patient_information=False)
    User.objects.filter(pk=person.pk).update(is_staff=True)
    assert effective_minimum(person) == 8
    User.objects.filter(pk=person.pk).update(is_active=False)
    assert effective_minimum(person) == 15
    rule(staff=True)
    job = system_account()
    assert not job.is_active and not job.has_usable_password()
    assert effective_minimum(job) == 15


@pytest.mark.parametrize("mode", ["set", "reset", "admin-own", "admin-target"])
def test_forms_read_changed_coverage_on_submission(make_user, client, mode):
    owner = make_user("console-owner", is_staff=True, is_superuser=True)
    person = owner if mode == "admin-own" else make_user("orchard-reader")
    grant = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    if mode == "set":
        person.set_unusable_password()
        person.save(update_fields=["password"])
        client.force_login(person)
        url = reverse("account_set_password")
    elif mode == "reset":
        url = reset_form(client, person)
    else:
        client.force_login(owner)
        url = (
            reverse("admin:password_change")
            if mode == "admin-own"
            else reverse("admin:auth_user_password_change", args=[person.pk])
        )
    rule(patient_information=True)
    person.user_permissions.add(grant)
    assert b"at least 8 characters" in client.get(url).content
    person.user_permissions.remove(grant)

    def submit(value):
        if mode == "admin-own":
            data = {"old_password": PASSWORD, "new_password1": value, "new_password2": value}
        else:
            data = {"password1": value, "password2": value, "usable_password": "true"}
        return client.post(url, data)

    previous = person.password
    refused = submit(EIGHT)
    assert refused.status_code == 200
    assert b"It must contain at least 15 characters" in refused.content
    assert b"password must contain at least 15 characters" in refused.content
    assert EIGHT.encode() not in refused.content
    person.refresh_from_db()
    assert person.password == previous
    assert submit(PHRASE).status_code == 302
    person.refresh_from_db()
    assert person.check_password(PHRASE)


@pytest.mark.parametrize("audience", ["anonymous", "portal", "no-role", *ROLES, "owner", "locked"])
def test_direct_account_and_console_pages(make_user, client, audience):
    person = None
    target = make_user("account-holder")
    if audience != "anonymous":
        flags = {}
        if audience == "portal":
            flags["client"] = Client.objects.create(name="Example Studio")
        if audience in {"owner", "admin"}:
            flags["is_staff"] = True
        if audience == "owner":
            flags["is_superuser"] = True
        if audience == "locked":
            flags["credentials_locked"] = True
        roles = [audience] if audience in ROLES else []
        person = make_user("visitor", *roles, **flags)
        client.force_login(person)
    has_account = person is not None and audience != "locked"
    for name, args, available in [
        ("account_change_password", [], has_account),
        ("admin:password_change", [], audience in {"owner", "admin"}),
        ("admin:auth_user_password_change", [target.pk], audience == "owner"),
        ("admin:accounts_user_add", [], audience == "owner"),
        ("security-settings", [], audience == "admin"),
        ("account_set_password", [], False),  # this account already has a usable password
    ]:
        response = client.get(reverse(name, args=args))
        if available:
            assert response.status_code == 200, name
        else:
            assert response.status_code in (302, 403), name
        if name == "account_set_password" and has_account:
            assert response.url == reverse("account_change_password")
    if has_account:
        person.set_unusable_password()
        person.save(update_fields=["password"])
        client.force_login(person)
        assert client.get(reverse("account_set_password")).status_code == 200

    if audience == "locked":
        url = reverse("account_reset_password_from_key", args=["1", "invalid"])
        assert client.get(url).url == reverse("dashboard")
    else:
        # The emailed link is its own entitlement, including for a signed-out visitor.
        url = reset_form(client, person or target)
        assert b'name="password1"' in client.get(url).content


def test_private_password_does_not_leave_the_change_page(make_user, client, caplog):
    person = make_user("recorder")
    client.force_login(person)
    url = reverse("account_change_password")
    marker = "OrchardInstrument293"
    with caplog.at_level(logging.INFO):
        mismatch = client.post(
            url, {"oldpassword": PASSWORD, "password1": marker, "password2": marker + "x"}
        )
        assert mismatch.status_code == 200 and "no-store" in mismatch["Cache-Control"]
        success = client.post(
            url,
            {"oldpassword": PASSWORD, "password1": marker, "password2": marker},
            follow=True,
        )
        assert person.__class__.objects.get(pk=person.pk).check_password(marker)
    audit = list(AuditEvent.objects.values("reason", "before", "after"))
    for surface in [
        mismatch.content.decode(),
        success.content.decode(),
        str(success.redirect_chain),
        str(list(client.session.items())),
        repr(audit),
        str([(message.subject, message.body) for message in mail.outbox]),
        caplog.text,
    ]:
        assert "OrchardInstrument" not in surface


def test_an_existing_second_browser_after_an_ordinary_change(make_user, client):
    person = make_user("two-browsers")
    client.force_login(person)
    other = Browser()
    other.force_login(person)
    url = reverse("account_change_password")
    assert client.get(url).status_code == other.get(url).status_code == 200
    assert (
        client.post(
            url, {"oldpassword": PASSWORD, "password1": PHRASE, "password2": PHRASE}
        ).status_code
        == 302
    )
    assert client.get(url).status_code == 200
    assert other.get(url).status_code == 302
    assert "_auth_user_id" not in other.session


class RecordingValidator:
    """Observe the account a normal provisioning command supplies to configured validators."""

    def validate(self, password, user=None):
        pass

    def get_help_text(self):
        return "Review provisioning check."


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "createsuperuser",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Django's createsuperuser checks the rule only when it asks for the "
                "password (accounts.passwords docstring); setup_lab is the documented way",
            ),
        ),
        "seed_demo",
    ],
)
def test_new_cli_passwords_run_the_configured_validators(monkeypatch, command):
    seen = []
    monkeypatch.setattr(
        RecordingValidator,
        "validate",
        lambda self, pw, user=None: seen.append(user.username if user is not None else None),
    )
    rules = [*settings.AUTH_PASSWORD_VALIDATORS, {"NAME": f"{__name__}.RecordingValidator"}]
    with override_settings(AUTH_PASSWORD_VALIDATORS=rules):
        if command == "createsuperuser":
            monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", PHRASE)
            call_command(
                command,
                username="new-owner",
                email="owner@example.invalid",
                interactive=False,
                verbosity=0,
                stdout=StringIO(),
            )
        else:
            monkeypatch.setenv("DEMO_PASSWORD", PHRASE)
            monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Rows of yellow birches at the lake")
            call_command(command, verbosity=0, stdout=StringIO())
    # Accounts given a password (adapted with Access review: the seed's example of an unused
    # account, audit_review.seed, has none, so no password of it is checked).
    created = {
        user.username for user in User.objects.filter(is_active=True) if user.has_usable_password()
    }
    assert created and created <= set(seen), (created, seen)


@pytest.mark.django_db(transaction=True)
def test_changepassword_preserves_a_name_correction_while_the_operator_types(
    make_user, monkeypatch
):
    from django.contrib.auth.management.commands.changepassword import Command

    person = make_user("command-person", first_name="Original")

    def correct():
        try:
            User.objects.filter(pk=person.pk).update(first_name="Corrected")
        finally:
            connections.close_all()

    replies = iter([None, PHRASE])

    def prompt(self, *args):
        if next(replies) is None:
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(correct).result(timeout=10)
        return PHRASE

    monkeypatch.setattr(Command, "_get_pass", prompt)
    call_command("changepassword", person.username, stdout=StringIO())
    person.refresh_from_db()
    assert person.check_password(PHRASE)
    assert person.first_name == "Corrected"


def db_cache_only():
    if TABLE not in connection.introspection.table_names():
        pytest.skip("This revision has no database cache table")


def test_stored_limit_values_contain_only_timestamps(client):
    db_cache_only()
    cache.clear()
    client.post(reverse("account_login"), {"login": "AccidentalPrivateText", "password": "typo"})
    with connection.cursor() as cursor:
        cursor.execute("SELECT cache_key, value FROM lab_ops_cache")
        rows = cursor.fetchall()
    assert rows
    for key, value in rows:
        assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
        # Inspect stored scalar types without executing a serialized object.
        args = [arg for _op, arg, _position in pickletools.genops(base64.b64decode(value))]
        assert any(isinstance(arg, float) for arg in args)
        assert not any(isinstance(arg, (str, bytes)) for arg in args)


def test_database_keeps_only_recent_failure_times(client):
    db_cache_only()
    from django.core.cache.backends import db

    cache.clear()
    earlier = timezone.now() - timedelta(days=14)
    with (
        patch.object(db, "tz_now", return_value=earlier),
        patch("time.time", return_value=earlier.timestamp()),
    ):
        client.post(reverse("account_login"), {"login": "before-the-break", "password": "typo"})
    # Other traffic resumes, from a different visitor. The first visitor hasn't returned.
    Browser(REMOTE_ADDR="192.0.2.34").post(
        reverse("account_login"), {"login": "after-the-break", "password": "typo"}
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT value FROM lab_ops_cache WHERE expires < %s", [timezone.now()])
        rows = cursor.fetchall()
    stale_times = [
        arg
        for (payload,) in rows
        for _op, arg, _pos in pickletools.genops(base64.b64decode(payload))
        if arg == earlier.timestamp()
    ]
    assert not stale_times, f"{len(stale_times)} stored histories still carry the old attempt"


def test_active_count_entries_survive_an_unrelated_write_at_capacity(settings):
    # Exercise the configured backend's eviction policy at a small, deterministic capacity.
    settings.CACHES = {"default": {**settings.CACHES["default"], "OPTIONS": {"MAX_ENTRIES": 6}}}
    cache.clear()
    keys = [f"review-tally-{index}" for index in range(10)]
    for key in keys:
        cache.set(key, [time()], timeout=300)
    assert len(cache.get_many(keys)) == len(keys)


def test_record_of_signins_tracks_deployment_storage(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    facts = {f.key: f for group in security_settings.in_force() for f in group.facts}
    assert facts["failed-sign-ins"].value == security_settings.CHANGED
    record = facts["sign-in-records"]
    if record.value != security_settings.CHANGED:
        assert "times of recent failures" not in record.note


def test_cache_joins_the_database_transaction():
    db_cache_only()
    with transaction.atomic():
        cache.set("review-transaction-marker", 17, 60)
        assert cache.get("review-transaction-marker") == 17
        transaction.set_rollback(True)
    assert cache.get("review-transaction-marker") is None


def test_seed_and_reset_preserve_preexisting_cache_record(monkeypatch):
    db_cache_only()
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setenv("DEMO_PASSWORD", PHRASE)
    cache.set("review-reset-marker", 19, timeout=300)
    call_command("seed_demo", verbosity=0, stdout=StringIO())
    assert cache.get("review-reset-marker") == 19
    call_command("reset_demo", interactive=False, verbosity=0, stdout=StringIO())
    assert cache.get("review-reset-marker") == 19
    assert User.objects.filter(username="demo_admin").exists()


def test_admin_guidance_names_the_person_whose_password_is_being_set(make_user, client):
    owner = make_user("console-reader", is_staff=True, is_superuser=True)
    target = make_user("other-person")
    client.force_login(owner)
    page = client.get(reverse("admin:auth_user_password_change", args=[target.pk]))
    help_text = str(page.context["form"].fields["password1"].help_text).lower()
    assert "your account" not in help_text and "your other personal information" not in help_text


@pytest.mark.django_db(transaction=True)
def test_bound_form_guidance_and_error_agree_if_the_rule_changes(make_user, client, monkeypatch):
    import re

    from django.forms import BaseForm

    person = make_user("form-reader")
    client.force_login(person)
    rule(staff=True)
    original = BaseForm.full_clean

    def relax_coverage():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '500ms'")
                SecondFactorRule.objects.filter(pk=1).update(staff=False)
        finally:
            connections.close_all()

    def clean(form):
        if "oldpassword" in form.fields and form.is_bound:
            # The proposal is too short under both policies. Only the feedback is in question.
            with ThreadPoolExecutor(max_workers=1) as worker:
                from django.db import OperationalError

                try:
                    worker.submit(relax_coverage).result(timeout=3)
                except OperationalError as exc:
                    # A fix may hold the policy until its reading is finished.
                    if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                        raise
        return original(form)

    monkeypatch.setattr(BaseForm, "full_clean", clean)
    response = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": "poem", "password2": "poem"},
    )
    assert response.status_code == 200
    form = response.context["form"]
    stated = re.search(r"at least (\d+) characters", str(form.fields["password1"].help_text))
    refused = re.search(r"at least (\d+) characters", str(form.errors["password1"]))
    assert stated and refused and stated[1] == refused[1]


def test_context_check_uses_a_name_corrected_before_validation(make_user):
    from allauth.account.forms import ChangePasswordForm as DefaultForm
    from allauth.utils import get_form_class

    person = make_user("reader", first_name="CedarvaleOrchard")
    former = "CedarvaleOrchard 2048"
    form_class = get_form_class(
        getattr(settings, "ACCOUNT_FORMS", {}), "change_password", DefaultForm
    )
    form = form_class(
        user=person,
        data={"oldpassword": PASSWORD, "password1": former, "password2": former},
    )
    User.objects.filter(pk=person.pk).update(first_name="Ash")
    validate_password(former, User.objects.get(pk=person.pk))
    assert form.is_valid(), form.errors.as_data().keys()


@pytest.mark.django_db(transaction=True)
def test_upgrade_adopts_a_django_created_cache_table():
    from django.db.migrations.executor import MigrationExecutor

    db_cache_only()
    before = [("core", "0006_second_factor_rule")]
    graph = MigrationExecutor(connection)
    latest = graph.loader.graph.leaf_nodes()
    graph.migrate(before)
    try:
        assert TABLE not in connection.introspection.table_names()
        call_command("createcachetable", TABLE, verbosity=0)
        cache.set("review-upgrade-marker", 23, timeout=600)
        MigrationExecutor(connection).migrate(latest)
        assert cache.get("review-upgrade-marker") == 23
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, TABLE)
        assert any(c["primary_key"] and c["columns"] == ["cache_key"] for c in constraints.values())
        assert any(c["index"] and c["columns"] == ["expires"] for c in constraints.values())
    finally:
        MigrationExecutor(connection).migrate(latest)
        cache.delete("review-upgrade-marker")


@pytest.mark.django_db(transaction=True)
def test_parallel_counter_records_every_ordinary_event():
    from threading import Barrier

    from allauth.core.internal import ratelimit
    from django.test import RequestFactory

    db_cache_only()
    gate = Barrier(4)
    config = {"read": "10/m/key"}
    rate = ratelimit.parse_rates(config["read"])[0]

    def record():
        try:
            request = RequestFactory().post("/")
            gate.wait(timeout=10)
            usage = ratelimit.consume(
                request, app="review", action="read", config=config, key="fixture"
            )
            assert usage is not None
            return usage.usage[0]
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=4) as executor:
        events = list(executor.map(lambda _: record(), range(4)))
    key = ratelimit.get_cache_key(
        RequestFactory().post("/"), app="review", action="read", rate=rate, key="fixture"
    )
    try:
        expected = [event.timestamp for event in events]
        assert sorted(cache.get(key)) == sorted(expected)
        events[0].rollback()
        assert sorted(cache.get(key)) == sorted(expected[1:])
    finally:
        cache.delete(key)


@pytest.mark.django_db(transaction=True)
def test_email_and_factor_limits_are_visible_to_another_connection(make_user, settings):
    from allauth.account.adapter import get_adapter
    from allauth.core.internal import ratelimit
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
    from django.test import RequestFactory

    db_cache_only()
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    person = make_user("code-reader")
    cache.clear()
    client = Browser(REMOTE_ADDR="192.0.2.61")
    assert (
        client.post(reverse("account_request_login_code"), {"email": person.email}).status_code
        == 302
    )
    TOTP.activate(person, generate_totp_secret())
    client = Browser(REMOTE_ADDR="192.0.2.61")
    mfa = client.post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    assert mfa.url == reverse("mfa_authenticate")
    assert client.post(mfa.url, {"code": "typing-error"}).status_code == 200
    req = RequestFactory().post("/", REMOTE_ADDR="192.0.2.61")

    def histories():
        try:
            counts = []
            for action, key in [
                ("request_login_code", person.email),
                ("login_failed", f"mfa-auth-user-{person.pk}"),
            ]:
                for rate in ratelimit.get_rates(app="allauth.account", action=action):
                    cache_key = ratelimit.get_cache_key(
                        req, app="allauth.account", action=action, rate=rate, key=key
                    )
                    counts.append(len(cache.get(cache_key, [])))
            return counts
        finally:
            connections.close_all()

    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            observed = worker.submit(histories).result(timeout=5)
        assert observed == [1, 1, 1, 1]
        # The successful password step gave back its own account attempt.
        typed = get_adapter()._get_login_attempts_cache_key(req, username=person.username)
        key_rate = next(
            r
            for r in ratelimit.get_rates(app="allauth.account", action="login_failed")
            if r.per == "key"
        )
        cache_key = ratelimit.get_cache_key(
            req, app="allauth.account", action="login_failed", rate=key_rate, key=typed
        )
        assert cache.get(cache_key) == []
    finally:
        cache.clear()


def test_cache_delivers_the_full_requested_minute():
    from django.core.cache.backends import db

    start = timezone.now().replace(microsecond=750000)
    read = start + timedelta(seconds=59.5)
    with (
        patch.object(db, "tz_now", return_value=start),
        patch("time.time", return_value=start.timestamp()),
    ):
        cache.set("review-minute", 7, timeout=60)
    try:
        with (
            patch.object(db, "tz_now", return_value=read),
            patch("time.time", return_value=read.timestamp()),
        ):
            assert cache.get("review-minute") == 7
    finally:
        cache.delete("review-minute")


@pytest.mark.xfail(
    strict=True,
    reason="allauth gives back an attempt by removing every entry with its time; two attempts "
    "at the same clock reading are not distinguished (allauth, predates this change)",
)
def test_giving_back_one_usage_preserves_another_with_the_same_clock_reading(rf):
    from allauth.core.internal import ratelimit

    request = rf.post("/")
    now = time()
    options = {
        "app": "review",
        "action": "read",
        "config": {"read": "8/m/key"},
        "key": "two-readers",
    }
    with patch("time.time", return_value=now):
        first = ratelimit.consume(request, **options)
        second = ratelimit.consume(request, **options)
    assert first and second
    key = first.usage[0].cache_key
    try:
        assert cache.get(key) == [now, now]
        second.rollback()
        assert cache.get(key) == [now]
    finally:
        cache.delete(key)


@pytest.mark.xfail(
    strict=True,
    reason="allauth clears the address's failure count after a completed second factor; "
    "documented on Security settings (Failed sign-ins); predates this change",
)
def test_successful_second_factor_cleanup_is_scoped_to_the_attempt(make_user, client, monkeypatch):
    from allauth.core.internal import ratelimit
    from allauth.mfa.totp.internal.auth import (
        TOTP,
        format_hotp_value,
        generate_totp_secret,
        hotp_value,
        yield_hotp_counters_from_time,
    )
    from django.test import RequestFactory

    person = make_user("factor-reader")
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mfa = client.post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    assert mfa.url == reverse("mfa_authenticate")
    rate = next(
        r
        for r in ratelimit.get_rates(app="allauth.account", action="login_failed")
        if r.per == "ip"
    )
    address_history = ratelimit.get_cache_key(
        RequestFactory().post("/"), app="allauth.account", action="login_failed", rate=rate
    )
    # Observe only the cache operations of one successful sign-in. No failed attempts are sent.
    deleted_whole_history = []
    delete = cache.delete

    def observe(key, *args, **kwargs):
        if key == address_history:
            deleted_whole_history.append(True)
        return delete(key, *args, **kwargs)

    monkeypatch.setattr(cache, "delete", observe)
    code = format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))
    client.post(mfa.url, {"code": code})
    assert client.session["_auth_user_id"] == str(person.pk)
    assert not deleted_whole_history


def test_reset_email_changes_only_the_recipient_and_the_link_is_then_spent(make_user, client):
    import re
    from urllib.parse import urlsplit

    person = make_user("mail-reader")
    other = make_user("mail-browser")
    client.force_login(other)
    previous = other.password
    sender = Browser()
    assert (
        sender.post(reverse("account_reset_password"), {"email": person.email}).status_code == 302
    )
    message = mail.outbox[-1]
    assert message.to == [person.email] and PHRASE not in message.body
    link = re.search(r"https?://[^\s<>]+/password/reset/key/[^\s<>]+/", message.body)
    assert link is not None
    path = urlsplit(link[0]).path
    displayed = client.get(path, follow=True)
    assert displayed.status_code == 200 and displayed.context["form"].fields["password1"]
    masked = displayed.redirect_chain[-1][0]
    saved = client.post(masked, {"password1": PHRASE, "password2": PHRASE})
    assert saved.status_code == 302
    assert "_auth_user_id" not in client.session
    person.refresh_from_db()
    assert person.check_password(PHRASE)
    other.refresh_from_db()
    assert other.password == previous
    spent = Browser().get(path, follow=True)
    assert b'name="password1"' not in spent.content


def test_published_common_password_count_matches_the_installed_list():
    import re

    from django.contrib.auth.password_validation import CommonPasswordValidator

    facts = {f.key: f for group in security_settings.in_force() for f in group.facts}
    size = re.search(r"list of ([0-9,]+) common passwords", facts["passwords"].note)
    if size:
        assert int(size[1].replace(",", "")) == len(CommonPasswordValidator().passwords)


@pytest.mark.xfail(
    strict=True,
    reason="Django's form fields refuse the null character; the help says letters of any "
    "language, digits, symbols and spaces",
)
def test_form_accepts_the_same_unicode_characters_as_the_policy(make_user):
    from allauth.account.forms import SetPasswordForm as DefaultForm
    from allauth.utils import get_form_class

    person = make_user("unicode-reader")
    chosen = "A frosted orchard\x00and a bell"
    validate_password(chosen, person)
    form_class = get_form_class(getattr(settings, "ACCOUNT_FORMS", {}), "set_password", DefaultForm)
    form = form_class(user=person, data={"password1": chosen, "password2": chosen})
    assert form.is_valid(), [e.code for e in form.errors.as_data()["password1"]]


@pytest.mark.xfail(
    strict=True,
    reason="the sign-in form keeps what was typed in its username field, to correct a typo, in "
    "the answer to the browser that typed it (predates this change)",
)
def test_misplaced_password_is_absent_from_the_returned_signin_page(make_user, client):
    make_user("typing-slip")
    response = client.post(reverse("account_login"), {"login": PASSWORD, "password": "typing slip"})
    assert response.status_code == 200
    assert PASSWORD.encode() not in response.content


def test_recovery_validation_state_changes_on_a_credential_lock(make_user):
    from allauth.account.forms import default_token_generator

    person = make_user("recovery-state")

    # Compare only the state used for validation. No recovery key is issued or redeemed.
    def state():
        return default_token_generator._make_hash_value(User.objects.get(pk=person.pk), 1)

    before = state()
    User.objects.filter(pk=person.pk).update(credentials_locked=True)
    changed = state() != before
    assert changed, "Locking credentials left the recovery validation state identical"


@pytest.mark.xfail(
    strict=True,
    reason="the rule is read when the password is checked, just before the save; a rule saved "
    "in between applies from the next password (accounts.passwords docstring)",
)
def test_factor_policy_at_form_validation_matches_the_password_write(make_user, monkeypatch):
    from allauth.account.forms import ChangePasswordForm as DefaultForm
    from allauth.utils import get_form_class

    from core.second_factor import requires

    checked = []
    monkeypatch.setattr(
        RecordingValidator,
        "validate",
        lambda self, value, user=None: checked.append(requires(user)),
    )
    person = make_user("policy-reader")
    rules = [*settings.AUTH_PASSWORD_VALIDATORS, {"NAME": f"{__name__}.RecordingValidator"}]
    with override_settings(AUTH_PASSWORD_VALIDATORS=rules):
        form_class = get_form_class(
            getattr(settings, "ACCOUNT_FORMS", {}), "change_password", DefaultForm
        )
        form = form_class(
            user=person, data={"oldpassword": PASSWORD, "password1": PHRASE, "password2": PHRASE}
        )
        assert form.is_valid() and checked == [False]
        # This change lowers the minimum. The chosen phrase satisfies both policies.
        rule(staff=True)
        form.save()
        person.refresh_from_db()
        assert person.check_password(PHRASE)
        assert checked[-1] == requires(person)


def test_password_chosen_while_covered_still_works_after_coverage_ends(make_user, client):
    person = make_user("legacy-reader", "technical")
    client.force_login(person)
    rule(roles=["technical"])
    chosen = EIGHT
    response = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": chosen, "password2": chosen},
    )
    assert response.status_code == 302
    rule(roles=[])
    other = Browser()
    response = other.post(reverse("account_login"), {"login": person.username, "password": chosen})
    assert response.status_code == 302
    assert other.session["_auth_user_id"] == str(person.pk)
