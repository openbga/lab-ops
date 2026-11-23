"""Neighbouring cases of the round 1 review fixes (commit c21f3df): one reading of the database,
no-store answers, the error help in the form dialog, the seed's lock, the history's contact
label and the facts under configurations other than the shipped one."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import OperationalError, connection, transaction
from django.test import override_settings
from django.urls import reverse

from accounts.models import User
from core import lab_profile, modules, security_settings
from core.concurrency import one_reading
from core.lab_profile_views import contact_label
from core.management.commands.seed_demo import DEMO_LAB_NAME
from core.models import AuditEvent, Lab, LabCertificate, LabContact
from request_forms.forms import PATIENT_HELP
from request_forms.models import RequestTemplate
from taxonomy.models import RequestCategory

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"
CONTACT = {
    "name": "Synthetic Fix Office",
    "title": "Synthetic Liaison",
    "email": "fix-inbox@example.invalid",
    "phone": "555-0177",
}
REASON = "Synthetic reason FX12."


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.SECURE_SSL_REDIRECT = False


@pytest.fixture
def lab():
    return Lab.objects.create(name="Synthetic Fix Laboratory")


@pytest.fixture
def admin(staff):
    return staff("fix-administrator", "admin")


def second_connection(action):
    def run():
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '900ms'")
                return action()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=5)


def concurrent_change(action):
    """Whether the other writer committed; False when a serializing lock delayed it."""
    try:
        second_connection(action)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != "55P03":
            raise
        return False
    return True


def record(actor, values=CONTACT, revision=0, reason=REASON, kind="privacy"):
    return lab_profile.save_contact(
        actor=actor, kind=kind, values=values, expected_revision=revision, reason=reason
    )


def facts_of():
    return {fact.key: fact for group in security_settings.in_force() for fact in group.facts}


def fact(key):
    return facts_of()[key]


def assert_changed(key, *names):
    """The fact is the changed-configuration fact: it names exactly `names` and states nothing
    else. Returns everything the fact says, lowered, for the absence checks of the caller."""
    item = fact(key)
    assert item.value == security_settings.CHANGED
    assert item.source == "configuration"
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    assert item.note == (
        f"This page describes this release's configuration. Check {listed} in config/settings.py."
    ), item.note
    return f"{item.value} {item.note}".lower()


def words(key):
    item = fact(key)
    return f"{item.value} {item.note}"


# 1. one_reading


def test_one_reading_inside_a_transaction_adds_nothing(lab):
    assert connection.in_atomic_block
    savepoints = list(connection.savepoint_ids)
    with one_reading():
        assert connection.in_atomic_block
        assert list(connection.savepoint_ids) == savepoints
        assert Lab.objects.count() == 1
        assert User.objects.count() == 0
    assert Lab.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_one_reading_outside_a_transaction_sees_one_snapshot():
    assert not connection.in_atomic_block
    with one_reading():
        assert connection.in_atomic_block
        before = Lab.objects.count()
        assert concurrent_change(lambda: Lab.objects.create(name="Synthetic Late Laboratory"))
        assert Lab.objects.count() == before == 0
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            assert cursor.fetchone()[0] == "repeatable read"
    assert not connection.in_atomic_block
    assert Lab.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_one_reading_leaves_the_connection_usable_after_an_error():
    with pytest.raises(RuntimeError), one_reading():
        raise RuntimeError("stop")
    assert not connection.in_atomic_block
    with connection.cursor() as cursor:
        cursor.execute("SHOW transaction_isolation")
        assert cursor.fetchone()[0] == "read committed"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["certificate", "identity", "module", "contact"])
def test_profile_panels_and_history_agree_when_a_second_connection_changes_it(
    client, staff, monkeypatch, change
):
    Lab.objects.create(name="Synthetic Fix Laboratory")
    admin = staff("fix-profile-admin", "admin")
    colleague = staff("fix-profile-colleague", "admin")
    client.force_login(admin)
    key = next(iter(modules.MODULES))
    changes = {
        "certificate": lambda: lab_profile.save_certificate(
            actor=colleague,
            instance=LabCertificate(authority="Synthetic Authority", number="FX-90210"),
        ),
        "identity": lambda: lab_profile.save_identity(
            actor=colleague, values={"city": "Synthetic Snapshot City"}
        ),
        "module": lambda: modules.set_enabled(
            actor=colleague, key=key, enabled=True, expected_revision=0, reason=REASON
        ),
        "contact": lambda: record(colleague),
    }
    original = lab_profile.certificates_with_state
    done = []

    def change_after_first_read(on):
        rows = original(on)
        done.append(concurrent_change(changes[change]))
        return rows

    monkeypatch.setattr(lab_profile, "certificates_with_state", change_after_first_read)
    response = client.get(reverse("lab-profile"))
    assert response.status_code == 200 and done == [True]
    context = response.context
    assert context["lab"].city == ""
    assert [c.number for c in context["certificates"]] == []
    assert not any(state["is_enabled"] for state in context["modules"])
    assert [row.name for row in context["contacts"]] == ["", ""]
    assert list(context["history"]) == []
    body = response.content.decode()
    for text in ("FX-90210", "Synthetic Snapshot City", CONTACT["name"], REASON):
        assert text not in body
    monkeypatch.undo()
    later = client.get(reverse("lab-profile"))
    assert len(later.context["history"]) == 1


@pytest.mark.django_db(transaction=True)
def test_security_counts_describe_one_reading_when_an_account_is_deactivated(client, staff):
    Lab.objects.create(name="Synthetic Fix Laboratory")
    admin = staff("fix-security-admin", "admin")
    holder = staff("fix-security-holder", "technical")
    holder.user_permissions.add(
        Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
    )
    Authenticator.objects.create(user=holder, type="totp", data={})
    client.force_login(admin)
    original = security_settings._second_factor_counts

    def deactivate_after_count():
        result = original()
        assert concurrent_change(lambda: User.objects.filter(pk=holder.pk).update(is_active=False))
        return result

    security_settings._second_factor_counts = deactivate_after_count
    try:
        response = client.get(reverse("security-settings"))
    finally:
        security_settings._second_factor_counts = original
    shown = {f.key: f for g in response.context["groups"] for f in g.facts}
    assert "Active staff accounts with one: 1 of 2." in shown["second-factor"].note
    assert shown["patient-capability"].value == "1 active staff account"
    assert fact("patient-capability").value == "0 active staff accounts"


@pytest.mark.django_db(transaction=True)
def test_security_counts_describe_one_reading_when_the_capability_is_granted(client, staff):
    Lab.objects.create(name="Synthetic Fix Laboratory")
    admin = staff("fix-grant-admin", "admin")
    later = staff("fix-grant-later", "technical")
    client.force_login(admin)
    original = security_settings._second_factor_counts

    def grant_after_count():
        result = original()
        grant = Permission.objects.get(
            codename="view_patient_information", content_type__app_label="accounts"
        )
        assert concurrent_change(lambda: later.user_permissions.add(grant))
        return result

    security_settings._second_factor_counts = grant_after_count
    try:
        response = client.get(reverse("security-settings"))
    finally:
        security_settings._second_factor_counts = original
    shown = {f.key: f for g in response.context["groups"] for f in g.facts}
    assert shown["patient-capability"].value == "0 active staff accounts"
    assert fact("patient-capability").value == "1 active staff account"


# 2. every answer of the contact form is never stored


def never_stored(response):
    header = response.get("Cache-Control", "")
    assert "no-store" in header, header


def contact_url(kind="privacy", **query):
    suffix = "?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
    return reverse("lab-contact-edit", args=[kind]) + suffix


def test_the_contact_form_answers_are_never_stored(client, lab, admin, staff):
    supervisor = staff("fix-supervisor", "supervisor")
    never_stored(client.get(contact_url()))  # anonymous: the sign-in redirect
    anonymous = client.get(contact_url())
    assert anonymous.status_code == 302 and anonymous.url.startswith(reverse("account_login"))
    never_stored(anonymous)
    never_stored(client.post(contact_url(), {"name": "x"}))

    client.force_login(supervisor)
    for method in (client.get, client.post):
        response = method(contact_url())
        assert response.status_code == 403
        never_stored(response)

    client.force_login(admin)
    unknown = client.get(contact_url("unknown-kind"))
    assert unknown.status_code == 404
    never_stored(unknown)
    unknown = client.post(contact_url("unknown-kind"), {"name": "x"})
    assert unknown.status_code == 404
    never_stored(unknown)

    page = client.get(contact_url())
    assert page.status_code == 200
    never_stored(page)
    form = {**CONTACT, "reason": REASON, "expected_revision": "0"}
    saved = client.post(contact_url(), form)
    assert saved.status_code == 302
    never_stored(saved)
    # The same form, now stale: redisplayed with the typed input.
    stale = client.post(contact_url(), {**form, "name": "Synthetic Stale Office"})
    assert stale.status_code == 200
    never_stored(stale)
    assert b"Synthetic Stale Office" in stale.content
    # A form with no changes: still a redirect, still never stored.
    unchanged = client.post(contact_url(), {**form, "expected_revision": "1"})
    assert unchanged.status_code == 302
    never_stored(unchanged)


# 3. the error help in the form dialog

DIALOG = {"headers": {"X-VR-Form": "1"}}
ERROR_HELP = b'data-vr-documentation-id="core-unavailable"'


def help_ids(response):
    return set(re.findall(r'data-vr-documentation-id="([^"]+)"', response.content.decode()))


@pytest.mark.parametrize(
    ("url", "own_help"),
    [
        (lambda: reverse("lab-module", args=[next(iter(modules.MODULES))]), "core-lab-module"),
        (lambda: reverse("lab-certificate-create"), "core-lab-certificate-form"),
        (lambda: reverse("lab-contact-edit", args=["privacy"]), "core-lab-contact-form"),
        (lambda: reverse("lab-profile-edit"), "core-lab-profile-form"),
        (lambda: reverse("lab-logo-edit"), "core-lab-logo-form"),
    ],
)
def test_a_refused_dialog_carries_the_error_help_and_an_allowed_one_its_own(
    client, lab, admin, staff, url, own_help
):
    client.force_login(staff("fix-dialog-technical", "technical"))
    for method in (client.get, client.post):
        refused = method(url(), **DIALOG)
        assert refused.status_code == 403
        assert help_ids(refused) == {"core-unavailable"}, help_ids(refused)
    client.force_login(admin)
    allowed = client.get(url(), **DIALOG)
    assert allowed.status_code == 200
    assert own_help in help_ids(allowed)
    assert "core-unavailable" not in help_ids(allowed)


def test_a_missing_target_in_the_dialog_carries_the_error_help(client, lab, admin):
    client.force_login(admin)
    for url in (
        contact_url("unknown-kind"),
        reverse("lab-module", args=["no-such-module"]),
        reverse("lab-certificate-edit", args=[987654]),
    ):
        for method in (client.get, client.post):
            response = method(url, **DIALOG)
            assert response.status_code == 404, url
            assert help_ids(response) == {"core-unavailable"}, (url, help_ids(response))


def test_the_page_form_refusal_still_shows_the_error_panel(client, lab, staff):
    client.force_login(staff("fix-page-technical", "technical"))
    response = client.get(reverse("lab-contact-edit", args=["privacy"]))
    assert response.status_code == 403
    assert "core-lab-contact-form" not in help_ids(response)
    assert ERROR_HELP in response.content


# 4. the seed locks the laboratory row before its check


@pytest.fixture
def demo_environment(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-Fix-Seed-735")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)


def _is_seed_lock(sql):
    upper = sql.upper()
    return '"CORE_LAB"' in upper and "FOR " in upper and "UPDATE" in upper.split("FOR ", 1)[-1]


def _wait_until_blocked(seconds=10):
    deadline = time.monotonic() + seconds
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            # pg_stat_activity is read once per transaction: without this, a session that
            # connected after the first reading in this transaction is never seen.
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
def test_a_profile_edit_committed_before_the_seed_lock_means_no_contacts(demo_environment, staff):
    Lab.objects.create(name=DEMO_LAB_NAME)
    editor = staff("fix-seed-editor", "admin")
    state = {"committed": None}

    def wrapper(execute, sql, params, many, context):
        if state["committed"] is None and _is_seed_lock(sql):
            state["committed"] = concurrent_change(
                lambda: lab_profile.save_identity(
                    actor=editor, values={"city": "Synthetic Local City"}
                )
            )
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        call_command("seed_demo", verbosity=0)
    assert state["committed"] is True, "The seed took no lock on the laboratory row"
    assert Lab.objects.get().city == "Synthetic Local City"
    assert not LabContact.objects.exists()
    assert not AuditEvent.objects.filter(action="lab.contact_changed").exists()
    assert not AuditEvent.objects.filter(
        action="lab.profile_changed", actor__username="demo_admin"
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_a_profile_edit_starting_after_the_seed_lock_waits_and_applies_after_it(
    demo_environment, staff
):
    Lab.objects.create(name=DEMO_LAB_NAME)
    editor = staff("fix-seed-late-editor", "admin")
    outcome, started = {}, []

    def edit():
        try:
            outcome["saved"] = lab_profile.save_identity(
                actor=editor, values={"city": "Synthetic Late City"}
            )
        except Exception as error:  # reported by the assertions
            outcome["error"] = error
        finally:
            connection.close()

    editor_thread = threading.Thread(target=edit)

    def wrapper(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not started and _is_seed_lock(sql):
            started.append(True)
            editor_thread.start()
            outcome["waited"] = _wait_until_blocked()
        return result

    with connection.execute_wrapper(wrapper):
        call_command("seed_demo", verbosity=0)
    editor_thread.join(timeout=30)
    assert started, "The seed took no lock on the laboratory row"
    assert outcome.get("waited") is True and "error" not in outcome
    assert LabContact.objects.count() == 2
    lab = Lab.objects.get()
    assert lab.city == "Synthetic Late City"
    events = list(
        AuditEvent.objects.filter(action="lab.profile_changed").order_by("occurred_at", "pk")
    )
    assert [event.actor.username for event in events] == ["demo_admin", "fix-seed-late-editor"]
    assert events[-1].after["city"] == "Synthetic Late City"
    assert events[0].after["city"] != "Synthetic Late City"


def test_the_seed_records_contacts_on_an_existing_demo_laboratory_without_history(
    demo_environment,
):
    Lab.objects.create(name=DEMO_LAB_NAME)
    assert not AuditEvent.objects.exists()
    call_command("seed_demo", verbosity=0)
    assert LabContact.objects.count() == 2
    assert AuditEvent.objects.filter(action="lab.contact_changed").count() == 2
    assert Lab.objects.get().city != ""


# 5. contact_label


def event(before=None, after=None):
    return SimpleNamespace(before=before, after=after)


def test_contact_label_first_record():
    assert (
        contact_label(event({}, {"kind": "privacy", "name": "Compliance Office"}))
        == "Privacy contact: Compliance Office"
    )
    assert (
        contact_label(event(None, {"kind": "security", "name": "Desk"})) == "Security contact: Desk"
    )


def test_contact_label_replacement_and_clearing():
    before = {"kind": "privacy", "name": "Compliance Office"}
    assert (
        contact_label(event(before, {"kind": "privacy", "name": "Example Privacy Desk"}))
        == "Privacy contact: Compliance Office, now Example Privacy Desk"
    )
    assert (
        contact_label(event(before, {"kind": "privacy", "name": ""}))
        == "Privacy contact: Compliance Office, cleared"
    )


def test_contact_label_for_a_change_of_title_only_names_the_person_once():
    before = {"kind": "privacy", "name": "Compliance Office", "title": "Lead"}
    after = {"kind": "privacy", "name": "Compliance Office", "title": "Director"}
    assert contact_label(event(before, after)) == "Privacy contact: Compliance Office"


def test_contact_label_when_the_kind_is_missing_does_not_crash_or_lead_with_a_colon():
    for before, after in (
        ({}, {"name": "Compliance Office"}),
        ({"name": "Compliance Office"}, {"name": ""}),
        ({"name": "A"}, {"name": "B"}),
        ({}, {}),
        (None, None),
    ):
        label = contact_label(event(before, after))
        assert not label.startswith(":"), label
    # kind only in `before`, as a cleared event written by an older release might carry
    assert contact_label(event({"kind": "security", "name": "Desk"}, {})) == (
        "Security contact: Desk, cleared"
    )


def test_history_names_the_contact_end_to_end(client, lab, admin):
    record(admin)
    record(admin, {"name": "Synthetic Replacement Desk"}, revision=1)
    record(admin, {}, revision=2)
    client.force_login(admin)
    for route in ("lab-profile", "security-settings"):
        body = client.get(reverse(route)).content.decode()
        assert f"Privacy contact: {CONTACT['name']}" in body
        assert f"{CONTACT['name']}, now Synthetic Replacement Desk" in body
        assert "Synthetic Replacement Desk, cleared" in body


# 6. facts under other configurations


def validators(*entries):
    base = "django.contrib.auth.password_validation."
    return [
        {"NAME": base + name, **({"OPTIONS": options} if options else {})}
        for name, options in entries
    ]


class CustomRule:
    def validate(self, password, user=None):
        return None

    def get_help_text(self):
        return "Synthetic custom rule."


def test_similarity_validator_with_other_attributes_is_a_changed_configuration():
    rules = validators(
        ("UserAttributeSimilarityValidator", {"user_attributes": ("username", "phone_number")})
    )
    with override_settings(AUTH_PASSWORD_VALIDATORS=rules):
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "close to" not in stated and "phone number" not in stated


def test_an_unknown_validator_next_to_a_minimum_length_claims_nothing_about_either():
    rules = validators(("MinimumLengthValidator", {"min_length": 10})) + [
        {"NAME": f"{__name__}.CustomRule"}
    ]
    with override_settings(AUTH_PASSWORD_VALIDATORS=rules):
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "10" not in stated and "characters" not in stated and "other check" not in stated


def test_allauth_minimum_never_changes_the_release_statement():
    assert "At least 15 characters" in fact("passwords").value
    with override_settings(ACCOUNT_PASSWORD_MIN_LENGTH=20):  # ignored while validators exist
        assert "At least 15 characters" in fact("passwords").value
    with override_settings(AUTH_PASSWORD_VALIDATORS=[], ACCOUNT_PASSWORD_MIN_LENGTH=20):
        stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "20" not in stated


@pytest.mark.parametrize("rule", [False, {"login_failed": ""}, {"login_failed": None}, {}])
def test_no_failed_sign_in_rule_is_a_changed_configuration_not_a_limit(rule):
    with override_settings(ACCOUNT_RATE_LIMITS=rule, ACCOUNT_LOGIN_ATTEMPTS_LIMIT=0):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "failures" not in stated and "refused" not in stated


def test_a_per_user_rate_is_a_changed_configuration():
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "4/5m/user"}):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "4 failures" not in stated and "one account" not in stated


@pytest.mark.parametrize(
    "bad", ["synthetic-invalid-marker", "x/y/z", "3/10m/key,junk-marker", 12345, ["a"]]
)
def test_an_unreadable_rule_is_never_echoed_and_never_an_error(bad):
    with override_settings(ACCOUNT_RATE_LIMITS={"login_failed": bad}):
        stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "marker" not in stated and "junk" not in stated and "12345" not in stated


def test_a_sliding_session_is_a_changed_configuration(db):
    assert fact("inactivity").value == "30 minutes for staff and client portal accounts"
    assert fact("inactivity").source == "session-limits"
    with override_settings(SESSION_COOKIE_AGE=900, SESSION_SAVE_EVERY_REQUEST=True):
        stated = assert_changed("inactivity", "SESSION_SAVE_EVERY_REQUEST")
        length = fact("session-length")
    assert "after 15" not in stated
    assert length.value == security_settings.CHANGED
    assert "SESSION_COOKIE_AGE" in length.note and "SESSION_SAVE_EVERY_REQUEST" in length.note
    assert "15 minutes" not in length.note


@pytest.fixture
def two_sessions(client, admin):
    def make():
        first, second = client.__class__(), client.__class__()
        for browser in (first, second):
            assert (
                browser.post(
                    reverse("account_login"), {"login": admin.username, "password": PASSWORD}
                ).status_code
                == 302
            )
            assert browser.session[SESSION_KEY] == str(admin.pk)
        return first, second

    return make


def change_password(browser, new="Example-New-9327-Password"):
    return browser.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": new, "password2": new},
    )


@override_settings(ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=True)
def test_logout_on_password_change_is_a_changed_configuration_and_signs_everyone_out(
    two_sessions, admin
):
    stated = assert_changed("new-password", "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
    assert "stays signed in" not in stated and "other sessions" not in stated
    changer, other = two_sessions()
    assert change_password(changer).status_code == 302
    assert SESSION_KEY not in changer.session
    assert SESSION_KEY not in other.session or other.get("/").status_code == 302


def test_the_default_password_change_keeps_only_the_changing_session(two_sessions, admin):
    assert fact("new-password").value == "Ends the account's other sessions at once"
    changer, other = two_sessions()
    assert change_password(changer).status_code == 302
    assert changer.get("/").status_code == 200
    assert other.get("/").status_code == 302


def test_https_wording_never_says_debug_mode_is_on_when_it_is_off():
    """The redirect follows debug mode at startup in this release's configuration; the note says
    so about the configuration and never claims debug mode is on now."""
    for debug in (False, True):
        with override_settings(DEBUG=debug, SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False):
            stated = fact("https")
        assert stated.value == "Not required"
        assert "debug mode is on" not in stated.note.lower()
        assert "In this release's configuration that happens only in debug mode" in stated.note


def test_hsts_zero_says_browsers_are_not_told():
    # A deployment with debug mode off at startup (STARTUP_DEBUG, config/settings.py).
    redirect = {"STARTUP_DEBUG": False, "SECURE_SSL_REDIRECT": True, "SESSION_COOKIE_SECURE": True}
    with override_settings(SECURE_HSTS_SECONDS=0, **redirect):
        stated = fact("https")
    assert stated.value == "Required"
    assert "not told" in stated.note and "only HTTPS for" not in stated.note
    with override_settings(SECURE_HSTS_SECONDS=86400, **redirect):
        assert "only HTTPS for 1 day" in fact("https").note


@pytest.mark.parametrize("policy", ["", "   ", "\n\t "])
def test_an_empty_policy_is_none(policy):
    with override_settings(CONTENT_SECURITY_POLICY=policy):
        stated = fact("content-security-policy")
    assert stated.value == "None" and "anything" in stated.note


@pytest.mark.parametrize(
    ("backend", "extra", "wording"),
    [
        (
            "django.core.mail.backends.filebased.EmailBackend",
            {"EMAIL_FILE_PATH": "/tmp/synthetic-mail"},
            "writes email to files",
        ),
        (
            "django.core.mail.backends.filebased.EmailBackend",
            {"EMAIL_FILE_PATH": None},
            "cannot send email: EMAIL_FILE_PATH is not set",
        ),
        ("django.core.mail.backends.console.EmailBackend", {}, "writes email to its console"),
        ("django.core.mail.backends.locmem.EmailBackend", {}, "keeps email in memory"),
        ("synthetic.mail.Backend", {}, "depends on this deployment's email backend"),
    ],
)
def test_a_backend_that_does_not_send_is_named_in_the_code_note(backend, extra, wording):
    # The deployment's email backend at startup and now (STARTUP_EMAIL_BACKEND, config/settings.py).
    with override_settings(
        STARTUP_EMAIL_BACKEND=backend,
        EMAIL_BACKEND=backend,
        ACCOUNT_LOGIN_BY_CODE_ENABLED=True,
        **extra,
    ):
        assert wording in fact("sign-in-code").note


def test_an_smtp_backend_and_a_disabled_code_carry_no_kept_email_note():
    smtp = "django.core.mail.backends.smtp.EmailBackend"
    with override_settings(
        STARTUP_EMAIL_BACKEND=smtp, EMAIL_BACKEND=smtp, ACCOUNT_LOGIN_BY_CODE_ENABLED=True
    ):
        stated = fact("sign-in-code")
    assert stated.value == "On" and "instead of sending" not in stated.note
    dummy = "django.core.mail.backends.dummy.EmailBackend"
    with override_settings(
        STARTUP_EMAIL_BACKEND=dummy, EMAIL_BACKEND=dummy, ACCOUNT_LOGIN_BY_CODE_ENABLED=False
    ):
        stated = fact("sign-in-code")
    assert stated.value == "Off" and "instead of sending" not in stated.note


def test_webauthn_is_a_changed_configuration_and_the_default_names_no_security_key():
    assert "security key" not in fact("second-factor").note
    with override_settings(MFA_SUPPORTED_TYPES=["totp", "recovery_codes", "webauthn"]):
        stated = assert_changed("second-factor", "MFA_SUPPORTED_TYPES")
    assert "security key" not in stated
    with override_settings(MFA_SUPPORTED_TYPES=["totp", "recovery_codes"]):  # same set, same fact
        assert fact("second-factor").value == "Optional"


# 5. the release configuration is pinned to config/settings.py


def test_the_repositorys_own_settings_are_the_release_configuration():
    assert security_settings.changed_settings(*security_settings.RELEASE) == []
    for item in facts_of().values():
        assert item.value != security_settings.CHANGED, item.key
        assert item.source != "configuration" or "Check " not in item.note, item.key


def test_changed_settings_lists_in_the_order_asked_and_treats_unreadable_as_changed():
    with override_settings(SESSION_COOKIE_AGE=600, SESSION_SAVE_EVERY_REQUEST=True):
        assert security_settings.changed_settings(
            "SESSION_SAVE_EVERY_REQUEST", "SESSION_ENGINE", "SESSION_COOKIE_AGE"
        ) == ["SESSION_SAVE_EVERY_REQUEST", "SESSION_COOKIE_AGE"]
    original = security_settings.RELEASE["SESSION_ENGINE"]
    try:

        def unreadable():
            raise RuntimeError("synthetic")

        security_settings.RELEASE["SESSION_ENGINE"] = (unreadable, original[1])
        assert security_settings.changed_settings("SESSION_ENGINE") == ["SESSION_ENGINE"]
    finally:
        security_settings.RELEASE["SESSION_ENGINE"] = original


NO_MAIL_CACHE = "django.core.cache.backends.dummy.DummyCache"
BOTH_OFF = {"SECURE_SSL_REDIRECT": False, "SESSION_COOKIE_SECURE": False}
CHANGES = [
    (
        "accounts",
        {"ACCOUNT_ADAPTER": "allauth.account.adapter.DefaultAccountAdapter"},
        ["ACCOUNT_ADAPTER"],
    ),
    ("passwords", {"AUTH_PASSWORD_VALIDATORS": []}, ["AUTH_PASSWORD_VALIDATORS"]),
    (
        "second-factor",
        {"MFA_SUPPORTED_TYPES": ["totp", "recovery_codes", "webauthn"]},
        ["MFA_SUPPORTED_TYPES"],
    ),
    (
        "second-factor-changes",
        {"MFA_REAUTHENTICATION_REQUIRED": False},
        ["MFA_REAUTHENTICATION_REQUIRED"],
    ),
    (
        "second-factor-changes",
        {"ACCOUNT_REAUTHENTICATION_TIMEOUT": 900},
        ["ACCOUNT_REAUTHENTICATION_TIMEOUT"],
    ),
    (
        "failed-sign-ins",
        {"ACCOUNT_RATE_LIMITS": {"login_failed": "3/10m/key"}},
        ["ACCOUNT_RATE_LIMITS"],
    ),
    ("failed-sign-ins", {"ALLAUTH_RATE_LIMIT_IPV6_PREFIX": 48}, ["ALLAUTH_RATE_LIMIT_IPV6_PREFIX"]),
    ("failed-sign-ins", {"CACHES": {"default": {"BACKEND": NO_MAIL_CACHE}}}, ["CACHES"]),
    ("sign-in-code", {"ACCOUNT_LOGIN_BY_CODE_TIMEOUT": 600}, ["ACCOUNT_LOGIN_BY_CODE_TIMEOUT"]),
    ("sign-in-code", {"MFA_TRUST_ENABLED": True}, ["MFA_TRUST_ENABLED"]),
    (
        "sign-in-records",
        {"SESSION_ENGINE": "django.contrib.sessions.backends.cache"},
        ["SESSION_ENGINE"],
    ),
    ("session-length", {"SESSION_COOKIE_AGE": 600}, ["SESSION_COOKIE_AGE"]),
    ("session-length", {"ACCOUNT_SESSION_COOKIE_AGE": 600}, ["ACCOUNT_SESSION_COOKIE_AGE"]),
    ("session-length", {"SESSION_SAVE_EVERY_REQUEST": True}, ["SESSION_SAVE_EVERY_REQUEST"]),
    ("inactivity", {"SESSION_SAVE_EVERY_REQUEST": True}, ["SESSION_SAVE_EVERY_REQUEST"]),
    ("browser-close", {"ACCOUNT_SESSION_REMEMBER": True}, ["ACCOUNT_SESSION_REMEMBER"]),
    ("browser-close", {"ACCOUNT_SESSION_REMEMBER": False}, ["ACCOUNT_SESSION_REMEMBER"]),
    (
        "browser-close",
        {"SESSION_EXPIRE_AT_BROWSER_CLOSE": True},
        ["SESSION_EXPIRE_AT_BROWSER_CLOSE"],
    ),
    (
        "deactivation",
        {"AUTHENTICATION_BACKENDS": ["django.contrib.auth.backends.ModelBackend"]},
        ["AUTHENTICATION_BACKENDS"],
    ),
    (
        "new-password",
        {"ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE": True},
        ["ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE"],
    ),
    ("session-cookie", {"SESSION_COOKIE_HTTPONLY": False}, ["SESSION_COOKIE_HTTPONLY"]),
    ("https", {"SECURE_REDIRECT_EXEMPT": ["^health/$"]}, ["SECURE_REDIRECT_EXEMPT"]),
    ("https", {"SECURE_HSTS_SECONDS": 3600, **BOTH_OFF}, ["SECURE_HSTS_SECONDS"]),
    (
        "https",
        {"SECURE_SSL_REDIRECT": True, "SESSION_COOKIE_SECURE": False, "SECURE_HSTS_SECONDS": 0},
        ["SECURE_SSL_REDIRECT"],
    ),
]


@pytest.mark.parametrize(("key", "overrides", "names"), CHANGES, ids=lambda v: str(v)[:40])
def test_changing_one_guarded_setting_yields_changed_naming_exactly_that_setting(
    key, overrides, names
):
    with override_settings(**overrides):
        assert_changed(key, *names)


def test_changing_the_secure_cookie_flag_alone_is_named_by_the_cookie_fact():
    with override_settings(SESSION_COOKIE_SECURE=not settings.SESSION_COOKIE_SECURE):
        assert_changed("session-cookie", "SESSION_COOKIE_SECURE")


def test_two_changed_settings_are_both_named_and_the_other_facts_stay_stated():
    with override_settings(SESSION_COOKIE_AGE=600, SESSION_SAVE_EVERY_REQUEST=True):
        assert_changed("session-length", "SESSION_COOKIE_AGE", "SESSION_SAVE_EVERY_REQUEST")
        assert fact("deactivation").value == "Ends its sessions at once"
        assert fact("passwords").value != security_settings.CHANGED


def test_a_changed_fact_leaves_the_counts_and_the_unguarded_facts_alone():
    with override_settings(AUTH_PASSWORD_VALIDATORS=[], MFA_SUPPORTED_TYPES=["totp", "webauthn"]):
        shown = facts_of()
    assert shown["passwords"].value == security_settings.CHANGED
    assert shown["second-factor"].value == security_settings.CHANGED
    assert shown["patient-capability"].value.endswith("active staff accounts")
    assert shown["patient-fields"].value == "0 active fields"
    assert shown["content-security-policy"].value == "The application's default"


# 7. the patient information mark's help


@pytest.fixture
def template(db):
    category = RequestCategory.objects.create(key="status-and-results", name="Status and results")
    return RequestTemplate.objects.create(key="pickup", name="Pickup", category=category)


def test_the_field_form_says_the_client_sees_values_only_on_fields_clients_see(
    client, admin, template
):
    assert "on fields clients see" in PATIENT_HELP
    client.force_login(admin)
    page = client.get(reverse("request_forms:field-create", args=[template.pk]))
    assert page.status_code == 200
    text = re.sub(r"\s+", " ", page.content.decode())
    assert "on fields clients see, shown to the client who raised or may see the ticket" in text


def test_the_field_form_help_file_states_the_same_condition():
    path = Path(settings.BASE_DIR, "docs/pages/request_forms/field-form.json")
    text = re.sub(r"\s+", " ", path.read_text())
    assert (
        "shown to the client who raised or may see the ticket, when the field is one clients see"
        in text
    )
    assert "on fields clients see" in PATIENT_HELP
