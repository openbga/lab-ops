"""Independent contracts for the security summary, contacts and their upgrade path.

Adopted from the round 1 review (original: .local/review-history/test_security_round1_original.py,
report security-settings-round-1.md). Three cases the report listed as conflicts with its brief
(C-01 to C-03) assert the documented decision instead; SS-01 asserts the corrected statement.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from allauth.account.adapter import get_adapter
from allauth.core.internal.ratelimit import truncate_ip
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import SESSION_KEY, password_validation
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from django.urls import reverse

from accounts.models import User
from accounts.roles import ROLES, has_capability
from core import demo, lab_profile, security_settings
from core.management.commands.seed_demo import DEMO_LAB_NAME
from core.models import AuditEvent, Lab, LabContact
from request_forms.selectors import may_see_patient_information

pytestmark = pytest.mark.django_db

ACTION = "lab.contact_changed"
PASSWORD = "test-password-729!"
CONTACT = {
    "name": "Synthetic Review Office",
    "title": "Synthetic Review Liaison",
    "email": "review-inbox@example.invalid",
    "phone": "555-0176",
}
REASON = "Synthetic appointment reference ZR41."
SAFEGUARDS = Path(settings.BASE_DIR, "docs/safeguards.md")


@pytest.fixture
def lab():
    return Lab.objects.create(name="Synthetic Review Laboratory")


@pytest.fixture
def admin(staff):
    return staff("review-administrator", "admin")


def contact_url(kind="privacy"):
    return reverse("lab-contact-edit", args=[kind])


def record(actor, values=CONTACT, revision=0, reason=REASON):
    return lab_profile.save_contact(
        actor=actor, kind="privacy", values=values, expected_revision=revision, reason=reason
    )


def fact(key):
    return next(f for group in security_settings.in_force() for f in group.facts if f.key == key)


def assert_changed(key, *names):
    """The fact is the changed-configuration fact: it names exactly `names` and states nothing
    else. Returns everything the fact says, lowered, for the absence checks of the caller."""
    item = fact(key)
    assert item.value == security_settings.CHANGED
    assert item.source == "configuration"
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    assert item.note.endswith(f"Check {listed} in config/settings.py."), item.note
    return f"{item.value} {item.note}".lower()


def wording(key):
    item = fact(key)
    return f"{item.value} {item.note}".lower()


def password_sign_in(client, user, **options):
    response = client.post(
        reverse("account_login"), {"login": user.username, "password": PASSWORD, **options}
    )
    assert response.status_code == 302
    assert client.session[SESSION_KEY] == str(user.pk)
    return response


class Links(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.urls = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs if key in ("href", "action") and value)


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
    """Either the other writer commits during the read or a serializing lock delays it."""
    try:
        second_connection(action)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != "55P03":
            raise
        return False
    return True


@pytest.mark.parametrize(
    "audience", [*ROLES, "roleless", "superuser", "inactive", "portal", "anon"]
)
def test_direct_routes_and_error_help(client, staff, portal_user, client_org, lab, admin, audience):
    record(admin)
    if audience in ROLES:
        user = admin if audience == "admin" else staff("review-reader", audience)
    elif audience == "portal":
        user = portal_user(client_org)
    elif audience == "inactive":
        user = admin
    elif audience == "anon":
        user = None
    else:
        user = User.objects.create_user(
            "review-owner",
            "review-owner@example.invalid",
            is_staff=True,
            is_superuser=audience == "superuser",
        )
    if user is not None:
        client.force_login(user)
    if audience == "inactive":
        user.is_active = False
        user.save(update_fields=["is_active"])

    unauthenticated = audience in {"anon", "inactive"}
    refusal = 302 if unauthenticated else 403
    for url in [reverse("security-settings"), contact_url(), contact_url("unknown-kind")]:
        for method in (client.get, client.post):
            response = method(url)
            if audience == "admin":
                assert response.status_code == (404 if "unknown-kind" in url else 200)
            else:
                assert response.status_code == refusal
                if unauthenticated:
                    assert response.url.split("?")[0] == reverse("account_login")
                else:
                    assert (
                        b'data-vr-documentation-id="core-security-settings"' not in response.content
                    )
                    assert (
                        b'data-vr-documentation-id="core-lab-contact-form"' not in response.content
                    )
                assert CONTACT["name"].encode() not in response.content
                assert REASON.encode() not in response.content
    profile = client.get(reverse("lab-profile"))
    assert profile.status_code == (200 if audience in ROLES else refusal)
    assert (CONTACT["name"].encode() in profile.content) == (audience in ROLES)
    if audience in ROLES and audience != "admin":
        assert REASON.encode() in profile.content
        assert contact_url() not in Links(profile).urls
    if audience in {*ROLES, "roleless", "superuser"}:
        news = client.get(reverse("whats-new"))
        assert news.status_code == 200 and b"Review security settings" in news.content
        assert (reverse("security-settings") in Links(news).urls) == (audience == "admin")
    assert list(LabContact.objects.values_list("revision", flat=True)) == [1]
    assert AuditEvent.objects.filter(action=ACTION).count() == 1


def test_settings_body_contains_no_credentials_or_unrelated_identities(
    client, admin, staff, settings
):
    other = staff("synthetic-enrolled-person", "technical")
    Authenticator.objects.create(user=other, type="totp", data={"secret": "SYNTHETIC-MFA-MARKER"})
    settings.SECRET_KEY = "synthetic-deployment-marker"
    settings.EMAIL_HOST = "synthetic-mail-host.example.invalid"
    settings.EMAIL_HOST_USER = "synthetic-mail-login"
    settings.EMAIL_HOST_PASSWORD = "synthetic-mail-marker"
    settings.CONTENT_SECURITY_POLICY = (
        "default-src 'self'; img-src https://synthetic-csp.example.invalid"
    )
    client.force_login(admin)
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    body = unescape(response.content.decode())
    for marker in [
        other.username,
        other.email,
        other.password,
        "SYNTHETIC-MFA-MARKER",
        settings.SECRET_KEY,
        settings.EMAIL_HOST,
        settings.EMAIL_HOST_USER,
        settings.EMAIL_HOST_PASSWORD,
        settings.CONTENT_SECURITY_POLICY,
    ]:
        assert marker not in body
    assert "CONTENT_SECURITY_POLICY" in body and "EMAIL_BACKEND" in body


def test_only_the_contacts_and_their_history_name_people(client, lab, admin, staff):
    """C-01, decided: the contacts panel names the contact and the history its editor, as the
    page help says; the facts about protections name no one."""
    actor = staff("review-previous-editor", "admin")
    actor.first_name, actor.last_name = "Synthetic", "Prior-Editor"
    actor.save()
    record(actor, CONTACT | {"name": "Synthetic Named-Contact"})
    client.force_login(admin)
    body = client.get(reverse("security-settings")).content.decode()
    assert "Synthetic Named-Contact" in body and "Prior-Editor" in body
    facts = " ".join(
        f"{item.label} {item.value} {item.note}"
        for group in security_settings.in_force()
        for item in group.facts
    )
    for name in (actor.username, actor.get_full_name(), admin.username, "Synthetic Named-Contact"):
        assert name not in facts


@pytest.mark.parametrize("route", ["lab-profile", "security-settings"])
def test_contact_details_stay_out_of_link_destinations(client, lab, admin, route):
    record(admin)
    client.force_login(admin)
    urls = Links(client.get(reverse(route))).urls
    # C-02, decided: the contact's email opens the mail program (page help); nothing else carries
    # a contact detail or the reason in an address.
    leaking = [url for url in urls if any(value in url for value in [*CONTACT.values(), REASON])]
    assert leaking == [f"mailto:{CONTACT['email']}"]


def test_contact_redirects_and_request_logs(client, lab, admin, caplog):
    client.force_login(admin)
    caplog.set_level(logging.INFO, logger="lab_ops.request")
    url = contact_url() + "?from=unrecognised"
    form = client.get(url)
    assert form.context["cancel_url"] == reverse("lab-profile")
    response = client.post(url, CONTACT | {"reason": REASON, "expected_revision": 0})
    assert response.status_code == 302 and response.url == reverse("lab-profile")
    stale = client.post(url, CONTACT | {"reason": REASON, "expected_revision": 0})
    assert stale.status_code == 200 and "no-store" in stale["Cache-Control"]
    dialog = client.post(
        contact_url() + "?from=security",
        CONTACT | {"reason": REASON, "expected_revision": 1},
        headers={"X-VR-Form": "1"},
    )
    assert dialog.status_code == 204 and dialog["HX-Redirect"] == reverse("security-settings")
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "lab_ops.request")
    assert "route=lab/contacts/<slug:kind>/" in logs
    assert not any(value in logs for value in [*CONTACT.values(), REASON, "unrecognised"])


def test_audit_insert_failure_rolls_back_contact(lab, admin, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("synthetic audit outage")

    monkeypatch.setattr(lab_profile, "record_event", unavailable)
    with pytest.raises(RuntimeError, match="synthetic audit outage"):
        record(admin)
    assert not LabContact.objects.exists()
    assert not AuditEvent.objects.filter(action=ACTION).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("next_values", [{}, {"name": "Synthetic replacement office"}])
def test_drawn_contact_survives_intervening_save(
    client, lab, admin, staff, monkeypatch, next_values
):
    colleague = staff("review-colleague", "admin")
    record(admin)
    client.force_login(admin)
    original = lab_profile.contacts

    def drawn_then_changed():
        drawn = original()
        concurrent_change(lambda: record(colleague, next_values, 1, "Second decision."))
        return drawn

    with monkeypatch.context() as patch:
        patch.setattr(lab_profile, "contacts", drawn_then_changed)
        page = client.get(contact_url())
    assert page.context["page_title"] == "Edit privacy contact"
    form = page.context["form"]
    assert form["expected_revision"].value() == 1
    assert {key: form[key].value() for key in CONTACT} == CONTACT
    if LabContact.objects.get().revision == 1:
        second_connection(lambda: record(colleague, next_values, 1, "Second decision."))
    draft = CONTACT | {"name": "Typed Review Office", "reason": "Typed review reason."}
    refused = client.post(
        contact_url(), draft | {"expected_revision": form["expected_revision"].value()}
    )
    assert refused.status_code == 200 and "no-store" in refused["Cache-Control"]
    assert all(refused.context["form"][key].value() == value for key, value in draft.items())
    assert refused.context["form"].non_field_errors()
    assert LabContact.objects.get().name == next_values.get("name", "")
    assert AuditEvent.objects.filter(action=ACTION).count() == 2


@pytest.mark.parametrize("change", ["role removed", "deactivated"])
def test_refused_full_page_post_is_uncacheable_after_access_changes(client, lab, admin, change):
    client.force_login(admin)
    form = client.get(contact_url()).context["form"]
    if change == "role removed":
        admin.groups.clear()
    else:
        admin.is_active = False
        admin.save(update_fields=["is_active"])
    response = client.post(
        contact_url(),
        CONTACT | {"reason": REASON, "expected_revision": form["expected_revision"].value()},
    )
    assert response.status_code == (403 if change == "role removed" else 302)
    assert not LabContact.objects.exists() and not AuditEvent.objects.filter(action=ACTION).exists()
    assert "no-store" in response.get("Cache-Control", "")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["security-settings", "lab-profile"])
def test_contact_and_history_agree_at_first_appointment(
    client, lab, admin, staff, monkeypatch, route
):
    colleague = staff("review-appointed-by", "admin")
    client.force_login(admin)
    original = lab_profile.contacts

    def appointment_after_read():
        result = original()
        concurrent_change(lambda: record(colleague))
        return result

    monkeypatch.setattr(lab_profile, "contacts", appointment_after_read)
    body = client.get(reverse(route)).content.decode()
    assert (CONTACT["name"] in body) == (REASON in body)


@pytest.mark.django_db(transaction=True)
def test_enrolment_total_and_numerator_describe_one_population(admin):
    Authenticator.objects.create(user=admin, type="totp", data={})
    changed = False

    def deactivate_after_first_count(execute, sql, params, many, context):
        nonlocal changed
        result = execute(sql, params, many, context)
        if not changed and "COUNT(" in sql.upper() and '"mfa_authenticator"' in sql:
            changed = True
            concurrent_change(lambda: User.objects.filter(pk=admin.pk).update(is_active=False))
        return result

    with connection.execute_wrapper(deactivate_after_first_count):
        note = wording("second-factor")
    enrolled, total = re.search(r"staff accounts with one: (\d+) of (\d+)", note).groups()
    assert int(enrolled) <= int(total), note


def test_deactivation_is_described_as_refusal_and_reactivation_restores_sessions(client, admin):
    """SS-01: deactivation refuses the account's next request but keeps its sessions, and the
    page says exactly that (ending them at once is the roadmap's automatic sign-out item)."""
    password_sign_in(client, admin)
    key = client.session.session_key
    admin.is_active = False
    admin.save(update_fields=["is_active"])
    response = client.get(reverse("security-settings"))
    assert response.status_code == 302
    remaining = Session.objects.filter(session_key=key).first()
    assert remaining is not None and SESSION_KEY in remaining.get_decoded()
    admin.is_active = True
    admin.save(update_fields=["is_active"])
    assert client.get(reverse("security-settings")).status_code == 200
    text = wording("deactivation")
    assert "refused at its next request" in text
    assert "not deleted" in text and "signed in again" in text
    assert "signed out" not in text
    row = reference_row(
        "Authorization and supervision, workforce clearance, termination procedures"
        " (Addressable), (a)(3)(ii)(A)-(C)"
    )
    assert "not deleted" in row


def test_sign_in_record_description_accounts_for_database_records(client, admin):
    assert admin.last_login is None
    password_sign_in(client, admin)
    admin.refresh_from_db()
    assert admin.last_login is not None
    stored = Session.objects.get(session_key=client.session.session_key).get_decoded()
    assert any(event["method"] == "password" for event in stored["account_authentication_methods"])
    assert "application log only" not in wording("sign-in-records")


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
def test_password_rule_includes_account_forms_minimum():
    with pytest.raises(ValidationError, match="at least 6"):
        get_adapter().clean_password("abcde")
    stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "6" not in stated and "characters" not in stated


def test_password_rule_describes_the_attributes_actually_compared(settings):
    settings.AUTH_PASSWORD_VALIDATORS = [
        {
            "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
            "OPTIONS": {"user_attributes": ["last_name"]},
        }
    ]
    user = User(username="review-account", last_name="SyntheticFamilyMarker")
    get_adapter().clean_password(user.username, user=user)
    with pytest.raises(ValidationError, match="last name"):
        get_adapter().clean_password(user.last_name, user=user)
    text = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "username" not in text and "last name" not in text and "close to" not in text


def test_failed_password_limit_names_the_sign_in_identifier(admin, rf):
    # Compare the two normal identifiers without making any failed sign-in attempts.
    request = rf.post(reverse("account_login"), HTTP_HOST="testserver")
    key = get_adapter()._get_login_attempts_cache_key
    assert key(request, username=admin.username) != key(request, email=admin.email)
    assert "for one account" not in wording("failed-sign-ins")
    assert "username or email address" in wording("failed-sign-ins")


def test_failed_password_limit_describes_the_ipv6_counting_unit():
    assert truncate_ip("2001:db8:1::1") == truncate_ip("2001:db8:1::2")
    text = wording("failed-sign-ins")
    assert "ipv6" in text or "prefix" in text or "subnet" in text


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
def test_failed_sign_in_summary_requires_a_counter_that_retains_failures():
    from django.core.cache import cache

    cache.set("review-counter", [1], timeout=60)
    assert cache.get("review-counter") is None
    assert "refused for a time" not in assert_changed("failed-sign-ins", "CACHES")


@override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "3/0.5s/ip"})
def test_subsecond_failure_window_is_not_reported_as_zero():
    stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "within 0 seconds" not in stated and "seconds" not in stated


@override_settings(SESSION_EXPIRE_AT_BROWSER_CLOSE=True, ACCOUNT_SESSION_REMEMBER=None)
def test_remember_me_matches_cookie_sent_by_password_sign_in(client, admin):
    response = password_sign_in(client, admin, remember="on")
    assert response.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == settings.SESSION_COOKIE_AGE
    stated = assert_changed("browser-close", "SESSION_EXPIRE_AT_BROWSER_CLOSE")
    assert "ends" not in stated.replace("changed in", "") and "remember me" not in stated


def test_emailed_code_session_without_remember_me_matches_page(client, admin):
    assert client.get(reverse("account_request_login_code")).status_code == 200
    sent = client.post(reverse("account_request_login_code"), {"email": admin.email})
    assert sent.url == reverse("account_confirm_login_code")
    assert mail.outbox[-1].to == [admin.email]
    code = re.search(r"browser window\.\s+(\S+)", mail.outbox[-1].body)[1]
    confirmed = client.post(sent.url, {"code": code})
    assert client.session[SESSION_KEY] == str(admin.pk)
    cookie = confirmed.cookies[settings.SESSION_COOKIE_NAME]
    if "unless remember me" in wording("browser-close") and "code" not in wording("browser-close"):
        assert not cookie["max-age"], "Emailed-code sign-in issued a persistent cookie"


@override_settings(ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=True)
def test_self_password_change_description_matches_configured_sign_out(client, admin):
    password_sign_in(client, admin)
    new_password = "Example-New-9327-Password"
    response = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": PASSWORD,
            "password1": new_password,
            "password2": new_password,
        },
    )
    assert response.status_code == 302
    assert SESSION_KEY not in client.session
    stated = assert_changed("new-password", "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
    assert "except the one that set it" not in stated and "stays signed in" not in stated


def test_active_capability_count_agrees_with_the_patient_value_gate(admin):
    grant = Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )
    roleless = User.objects.create_user("review-roleless", "review-roleless@example.invalid")
    roleless.user_permissions.add(grant)
    admin.user_permissions.add(grant)
    assert has_capability(roleless, grant.codename)
    effective = sum(may_see_patient_information(person) for person in (roleless, admin))
    assert effective == 1
    assert fact("patient-capability").value == "1 active staff account"


@override_settings(MFA_SUPPORTED_TYPES=["totp", "recovery_codes", "webauthn"])
def test_help_count_of_authenticator_apps_excludes_key_only_enrolments(admin, client):
    Authenticator.objects.create(user=admin, type=Authenticator.Type.WEBAUTHN, data={})
    assert not Authenticator.objects.filter(user=admin, type=Authenticator.Type.TOTP).exists()
    client.force_login(admin)
    response = client.get(reverse("security-settings"))
    body = response.content.decode()
    stated = assert_changed("second-factor", "MFA_SUPPORTED_TYPES")
    assert "authenticator app" not in stated and "with one" not in stated
    assert "Active staff accounts with one: 1 of 1." not in body
    assert "Active staff accounts with one: 0 of 1." not in body


def test_locked_demo_account_cannot_choose_to_enrol(client, staff):
    shared = staff("synthetic-shared-account", "technical")
    shared.credentials_locked = True
    shared.save(update_fields=["credentials_locked"])
    client.force_login(shared)
    response = client.get(reverse("mfa_activate_totp"))
    assert response.status_code == 302 and response.url == reverse("dashboard")
    assert "each person can add" not in wording("second-factor")


@pytest.mark.parametrize("secure", [True, False])
def test_cookie_summary_matches_independent_browser_flags(client, admin, settings, secure):
    settings.DEBUG = False
    settings.SESSION_COOKIE_SECURE = secure
    settings.SESSION_COOKIE_HTTPONLY = False
    response = password_sign_in(client, admin)
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert not cookie["httponly"] and bool(cookie["secure"]) == secure
    note = fact("session-cookie").note
    assert fact("session-cookie").value == security_settings.CHANGED
    assert "SESSION_COOKIE_HTTPONLY" in note and "hidden from page scripts" not in wording(
        "session-cookie"
    )


@override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False)
def test_no_https_redirect_does_not_mean_debug_is_on(client):
    assert client.get(reverse("health")).status_code == 200
    assert "debug mode is on" not in wording("https")


def test_https_names_the_setting_that_controls_the_redirect(settings):
    settings.SECURE_HSTS_SECONDS = 0
    settings.SECURE_SSL_REDIRECT = False
    assert Client().get(reverse("health")).status_code == 200
    settings.SECURE_SSL_REDIRECT = True
    assert Client().get(reverse("health")).status_code == 301
    assert fact("https").setting != "SECURE_HSTS_SECONDS" or "DEBUG" in fact("https").note


def test_default_image_policy_description_includes_the_data_scheme(client):
    response = client.get(reverse("health"))
    assert "img-src 'self' data:" in response["Content-Security-Policy"]
    text = wording("content-security-policy")
    assert "images only from the application" not in text or "data:" in text


@override_settings(CONTENT_SECURITY_POLICY="")
def test_empty_csp_is_identified_as_unprotected(client):
    # A blank policy sends no header at all (an empty header value is not a policy).
    assert "Content-Security-Policy" not in client.get(reverse("health"))
    with override_settings(CONTENT_SECURITY_POLICY="\n"):
        assert "Content-Security-Policy" not in client.get(reverse("health"))
    text = wording("content-security-policy")
    assert fact("content-security-policy").value == "None"
    assert any(word in text for word in ("empty", "no policy", "disabled", "off", "no restriction"))


@pytest.fixture
def demo_environment(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-Review-Seed-735")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)


def test_seed_records_contacts_only_once_and_preserves_seal(demo_environment):
    assert demo.nonempty_tables() == []
    call_command("seed_demo", verbosity=0)
    assert demo.is_sealed(Lab.objects.get())
    contacts = list(LabContact.objects.order_by("kind").values())
    assert [(c["kind"], c["revision"]) for c in contacts] == [("privacy", 1), ("security", 1)]
    assert all(c["email"].endswith("@example.invalid") for c in contacts)
    events = list(AuditEvent.objects.filter(action=ACTION).values_list("pk", flat=True))
    assert len(events) == 2
    call_command("seed_demo", verbosity=0)
    assert demo.is_sealed(Lab.objects.get())
    assert list(LabContact.objects.order_by("kind").values()) == contacts
    assert list(AuditEvent.objects.filter(action=ACTION).values_list("pk", flat=True)) == events


def test_seed_respects_preexisting_edited_profile(admin, demo_environment):
    lab = Lab.objects.create(name=DEMO_LAB_NAME)
    lab_profile.save_identity(actor=admin, values={"city": "Synthetic Existing City"})
    call_command("seed_demo", verbosity=0)
    lab.refresh_from_db()
    assert lab.city == "Synthetic Existing City"
    assert not LabContact.objects.exists()
    assert not AuditEvent.objects.filter(action=ACTION).exists()
    assert not demo.is_sealed(lab)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("already_sealed", [False, True])
def test_populated_lab_survives_contact_migration(already_sealed):
    previous, candidate = ("core", "0003_labmodule"), ("core", "0004_labcontact")
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    assert connection.settings_dict["NAME"].startswith("test_")
    executor.migrate([previous])
    try:
        apps = executor.loader.project_state([previous]).apps
        old_lab = apps.get_model("core", "Lab").objects.create(name="Synthetic Existing Lab")
        actor = apps.get_model("accounts", "User").objects.create(
            username="migration-editor", email="migration-editor@example.invalid"
        )
        old_event = apps.get_model("core", "AuditEvent").objects.create(
            actor_id=actor.pk,
            action="lab.profile_changed",
            object_type="core.lab",
            object_id=str(old_lab.pk),
            after={"city": "Synthetic Existing City"},
        )
        if already_sealed:
            apps.get_model("core", "AuditEvent").objects.create(
                actor_id=actor.pk,
                action=demo.SEAL_ACTION,
                object_type="core.lab",
                object_id=str(old_lab.pk),
                after={"seal": demo.seal_for(old_lab, actor)},
            )
        MigrationExecutor(connection).migrate([candidate])
        assert Lab.objects.get(pk=old_lab.pk).name == old_lab.name
        assert AuditEvent.objects.get(pk=old_event.pk).after == old_event.after
        assert not LabContact.objects.exists()
        assert demo.is_sealed(Lab.objects.get()) is already_sealed
        assert LabContact._meta.db_table in demo.business_tables()
        assert LabContact._meta.db_table not in demo.nonempty_tables()
        LabContact.objects.create(kind="security", name="Synthetic Existing Office")
        assert LabContact._meta.db_table in demo.nonempty_tables()
    finally:
        MigrationExecutor(connection).migrate(leaves)


def test_denied_contact_dialog_uses_error_help(rf, staff):
    from django.template.loader import render_to_string
    from django.urls import resolve

    request = rf.get(contact_url(), headers={"X-VR-Form": "1"})
    request.user = staff("review-form-reader", "technical")
    request.resolver_match = resolve(request.path)
    body = render_to_string("403.html", request=request)
    assert 'data-vr-documentation-id="core-lab-contact-form"' not in body


@pytest.mark.parametrize("change", ["role removed", "deactivated"])
def test_full_page_does_not_echo_the_draft_after_access_is_lost(client, lab, admin, change):
    """C-03, decided: a form is shown again only to someone who may still open it
    (docs/architecture.md); a refused post writes nothing and echoes nothing."""
    client.force_login(admin)
    revision = client.get(contact_url()).context["form"]["expected_revision"].value()
    if change == "role removed":
        admin.groups.clear()
    else:
        admin.is_active = False
        admin.save(update_fields=["is_active"])
    submitted = CONTACT | {"reason": REASON, "expected_revision": revision}
    refused = client.post(contact_url(), submitted, follow=True)
    assert not LabContact.objects.exists() and not AuditEvent.objects.filter(action=ACTION).exists()
    body = refused.content.decode()
    assert CONTACT["name"] not in body and REASON not in body


def test_profile_history_can_identify_the_previous_contact_after_clearing(client, lab, admin):
    record(admin)
    record(admin, {}, 1, "Synthetic removal.")
    client.force_login(admin)
    page = client.get(reverse("lab-profile"))
    events = page.context["history"]
    assert events[0].before["name"] == CONTACT["name"] and not events[0].after["name"]
    assert CONTACT["name"] in page.content.decode()


def test_unknown_or_inapplicable_password_rule_is_not_reported_as_absent(settings):
    # A normal Django validator, with the same public validator interface as a site's own.
    settings.AUTH_PASSWORD_VALIDATORS = [{"NAME": f"{__name__}.SyntheticPasswordRule"}]
    with pytest.raises(ValidationError):
        password_validation.validate_password("tiny")
    stated = assert_changed("passwords", "AUTH_PASSWORD_VALIDATORS")
    assert "no rule" not in stated and "any password" not in stated


class SyntheticPasswordRule:
    def validate(self, password, user=None):
        if len(password) < 13:
            raise ValidationError("Use thirteen or more characters.")

    def get_help_text(self):
        return "Use thirteen or more characters."


@override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "synthetic-invalid-config-marker"})
def test_unreadable_rate_configuration_is_not_echoed_as_an_enforced_rule():
    stated = assert_changed("failed-sign-ins", "ACCOUNT_RATE_LIMITS")
    assert "synthetic-invalid-config-marker" not in stated


@pytest.mark.parametrize(
    "key,overrides",
    [
        ("session-length", {"SESSION_COOKIE_AGE": 600}),
        ("passwords", {"AUTH_PASSWORD_VALIDATORS": []}),
        ("failed-sign-ins", {"ACCOUNT_RATE_LIMITS": {"login_failed": "2/5m/key"}}),
    ],
)
def test_set_by_distinguishes_runtime_overrides_from_release_changes(key, overrides):
    initial = fact(key).value
    with override_settings(**overrides):
        changed = fact(key)
    assert changed.value != initial
    assert changed.source_label != "Fixed in this release"


def reference_row(label):
    return (
        next(
            line for line in SAFEGUARDS.read_text().splitlines() if line.startswith(f"| {label} |")
        )
        .split("|")[2]
        .lower()
    )


def test_safeguards_reference_qualifies_the_default_password_rule():
    rule = [{"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}]
    with override_settings(AUTH_PASSWORD_VALIDATORS=rule):
        get_adapter().clean_password("short")
    row = reference_row("Password management (Addressable), (a)(5)(ii)(D)")
    assert "at least 8" not in row or "default" in row


@override_settings(ACCOUNT_RATE_LIMITS=False)
def test_safeguards_reference_qualifies_failed_login_limits():
    from allauth.account import app_settings

    assert app_settings.RATE_LIMITS == {}
    row = reference_row("Log-in monitoring (Addressable), (a)(5)(ii)(C)")
    assert "are limited per account" not in row or "default" in row


@override_settings(SESSION_COOKIE_AGE=600)
def test_safeguards_reference_qualifies_the_session_lifetime(client, admin):
    password_sign_in(client, admin, remember="on")
    assert client.session.get_expiry_age() == 600
    row = reference_row("Automatic logoff (Addressable), (a)(2)(iii)")
    assert "two weeks" not in row or "default" in row


@override_settings(MFA_REAUTHENTICATION_REQUIRED=False)
def test_safeguards_reference_qualifies_the_reauthentication_rule():
    assert not get_adapter().is_reauthentication_required(app="allauth.mfa")
    row = reference_row("Person or entity authentication, (d)")
    assert "needs a recent re-authentication" not in row or "default" in row


def test_safeguards_reference_acknowledges_stored_sensitive_reads(demo_environment):
    from request_forms.models import FieldValue
    from request_forms.services import reveal_value

    call_command("seed_demo", verbosity=0)
    actor = User.objects.get(username="demo_supervisor")
    value = FieldValue.objects.get(holds_patient_information=True, superseded_at__isnull=True)
    revealed = reveal_value(actor=actor, value_id=value.pk)
    event = AuditEvent.objects.get(
        action="request_form.value_revealed", after__value_id=revealed.pk
    )
    assert event.actor_id == actor.pk and revealed.value not in str(event.after)
    row = reference_row("Information system activity review (Required), (a)(1)(ii)(D)")
    assert "no stored record of reads" not in row


def test_patient_search_summary_acknowledges_authorized_exact_key_search(demo_environment):
    from links.models import ExternalRecordLink
    from links.services import search_person_key
    from request_forms.models import FieldValue

    call_command("seed_demo", verbosity=0)
    actor = User.objects.get(username="demo_supervisor")
    identifier = FieldValue.objects.get(
        holds_patient_information=True, superseded_at__isnull=True
    ).value
    link = ExternalRecordLink.objects.get(is_person=True, identifier=identifier)
    assert search_person_key(actor=actor, key=link.identifier)
    text = wording("patient-kept-out")
    assert "search" not in text or "exact" in text or "general" in text


def test_portal_routes_exclude_lab_contacts_and_settings_exclude_patient_answers(
    client, demo_environment
):
    from desk.portal_scope import portal_requests
    from request_forms.models import FieldValue
    from request_forms.selectors import portal_templates

    call_command("seed_demo", verbosity=0)
    admin = User.objects.get(username="demo_admin")
    record(admin, revision=1)
    portal = User.objects.get(username="demo_client")
    ticket = portal_requests(portal).filter(status="open").first()
    template = portal_templates(portal).first()
    assert ticket and template
    pages = [
        "portal:home",
        "portal:request-list",
        "portal:request-create",
        "portal_forms:choose",
    ]
    client.force_login(portal)
    responses = [client.get(reverse(name)) for name in pages]
    responses += [
        client.get(reverse("portal:request-detail", args=[ticket.number])),
        client.post(reverse("portal:request-reply", args=[ticket.number]), {}),
        client.get(reverse("portal_forms:raise", args=[template.key])),
    ]
    for response in responses:
        assert response.status_code == 200
        assert not any(value in response.content.decode() for value in [*CONTACT.values(), REASON])
    client.force_login(admin)
    marker = FieldValue.objects.get(
        holds_patient_information=True, superseded_at__isnull=True
    ).value
    for url in [reverse("security-settings"), reverse("lab-profile"), contact_url()]:
        response = client.get(url)
        assert response.status_code == 200
        assert marker not in response.content.decode()


@override_settings(SESSION_COOKIE_AGE=600, SESSION_SAVE_EVERY_REQUEST=True)
def test_sliding_session_expiry_is_described(client, admin):
    password_sign_in(client, admin, remember="on")
    client.get(reverse("dashboard"))
    session = Session.objects.get(session_key=client.session.session_key)
    before = session.expire_date
    client.get(reverse("dashboard"))
    session.refresh_from_db()
    assert session.expire_date > before
    assert fact("inactivity").value == security_settings.CHANGED
    assert fact("inactivity").note.endswith(
        "Check SESSION_SAVE_EVERY_REQUEST in config/settings.py."
    )
    assert "after" not in wording("inactivity").replace("check", "")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    ACCOUNT_LOGIN_BY_CODE_ENABLED=True,
)
def test_sign_in_code_description_distinguishes_console_mail_from_delivery():
    from io import StringIO

    sink = StringIO()
    backend = mail.get_connection(stream=sink)
    mail.EmailMessage(
        "Synthetic delivery check",
        "Review mail body",
        to=["test@example.invalid"],
        connection=backend,
    ).send()
    assert "Review mail body" in sink.getvalue()
    text = wording("sign-in-code")
    assert "console" in text or "log" in text or "local" in text


def test_explicit_capability_on_a_named_role_is_described_as_in_force(staff):
    technician = staff("review-technician", "technical")
    role = technician.groups.get(name="technical")
    grant = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    role.permissions.add(grant)
    assert not technician.user_permissions.exists()
    assert may_see_patient_information(technician)
    text = wording("patient-capability")
    assert "no role includes it" not in text or "default" in text or "automatic" in text


def test_patient_field_control_describes_staff_only_answer_visibility(demo_environment):
    from desk.portal_scope import portal_requests
    from request_forms.forms import PATIENT_HELP
    from request_forms.models import Audience, FieldValue
    from request_forms.selectors import client_values

    call_command("seed_demo", verbosity=0)
    portal = User.objects.get(username="demo_client")
    value = FieldValue.objects.get(holds_patient_information=True, superseded_at__isnull=True)
    ticket = value.filled_form.request
    assert portal_requests(portal).filter(pk=ticket.pk).exists()
    assert any(value.value in row.lines for row in client_values(ticket, portal).rows)
    value.field.audience = Audience.STAFF
    value.field.save(update_fields=["audience"])
    assert all(value.value not in row.lines for row in client_values(ticket, portal).rows)
    text = PATIENT_HELP.lower()
    assert "fields clients see" in text or "client-visible" in text or "audience" in text


@pytest.mark.django_db(transaction=True)
def test_seed_rechecks_profile_before_appointing_contacts(demo_environment, staff, monkeypatch):
    Lab.objects.create(name=DEMO_LAB_NAME)
    staff("demo_admin", "admin")
    editor = staff("review-profile-editor", "admin")
    original_lock = lab_profile._lock_lab
    edit_committed = False
    attempted = False

    def lock_after_profile_edit():
        nonlocal edit_committed, attempted
        if not attempted:
            attempted = True
            edit_committed = concurrent_change(
                lambda: lab_profile.save_identity(
                    actor=editor, values={"city": "Synthetic Local City"}
                )
            )
        return original_lock()

    monkeypatch.setattr(lab_profile, "_lock_lab", lock_after_profile_edit)
    call_command("seed_demo", verbosity=0)
    if edit_committed:
        assert AuditEvent.objects.filter(action="lab.profile_changed", actor=editor).exists()
        assert not LabContact.objects.exists(), (
            "Seed appointed contacts after the local profile edit"
        )
    else:
        # A seed that serializes the whole decision may legitimately be first.
        lab_profile.save_identity(actor=editor, values={"city": "Synthetic Local City"})
    assert Lab.objects.get().city == "Synthetic Local City"


def test_safeguards_describes_the_scope_of_supervisor_ticket_history(client, demo_environment):
    from request_forms.models import FieldValue
    from request_forms.services import reveal_value

    call_command("seed_demo", verbosity=0)
    supervisor = User.objects.get(username="demo_supervisor")
    value = FieldValue.objects.get(holds_patient_information=True, superseded_at__isnull=True)
    reveal_value(actor=supervisor, value_id=value.pk)
    event = AuditEvent.objects.get(action="request_form.value_revealed", after__value_id=value.pk)
    client.force_login(supervisor)
    response = client.get(reverse("desk:request-history", args=[value.filled_form.request_id]))
    assert response.status_code == 200
    visible = {entry.pk for entry in response.context["events"]}
    assert visible and value.value not in response.content.decode()
    if "full history" in reference_row(
        "Information system activity review (Required), (a)(1)(ii)(D)"
    ):
        assert event.pk in visible
