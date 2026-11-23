"""Independent verification of the required-second-factor fixes on synthetic accounts.

Written by the independent reviewer (Codex) verifying round 1 at 6dfd23e and adopted unchanged,
except three strict xfails for behaviour of allauth's own sign-in steps that predates this change
and is documented in docs/architecture.md ("Required second factor"): a stored credential of a type
the deployment does not offer, an account without a password whose recovery codes are all used,
and the help shown on a setup form resumed after a re-authentication. The original is archived in
.local/review-history/required-second-factor-round-1-verification-original/."""

import json
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations, product
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from allauth.account.internal.flows.reauthentication import get_reauthentication_flows
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, TOTP, generate_totp_secret
from django.conf import settings as django_settings
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.db import connection, connections
from django.test import Client as Browser
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import strip_tags

from accounts.enrolment import NEXT_KEY
from accounts.roles import ROLE_LABELS, ROLES, effective_roles, has_capability
from accounts.tests.conftest import PASSWORD
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_second_factor_enrolment import SETUP, code_for, require, sign_in
from clients.models import Client
from core import second_factor as policy
from core.models import SecondFactorRule
from core.security_settings import _second_factor_counts, in_force

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_sign_in_limits():
    cache.clear()
    yield
    cache.clear()


def setup_help():
    path = Path(django_settings.BASE_DIR) / "docs/pages/accounts/second-factor-required.json"
    return json.loads(path.read_text())


def test_all_rule_combinations_agree_with_account_roles_grants_and_staff_status(make_user):
    organization = Client.objects.create(name="Example verification clinic")
    ordinary = make_user("two_roles", "supervisor", "technical", is_staff=True)
    arbitrary = make_user("custom_group", "Technical")
    portal = make_user("clinic", "admin", client=organization, is_staff=True)
    admin = make_user("expanded", "admin")
    inactive = make_user("inactive", "admin", is_active=False, is_staff=True)
    users = [
        ordinary,
        arbitrary,
        portal,
        admin,
        inactive,
        make_user("owner", is_superuser=True),
        make_user("account", "account_manager"),
        make_user("services", "client_services"),
        make_user("inactive_clinic", client=organization, is_staff=True, is_active=False),
    ]
    grant = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    ordinary.user_permissions.add(grant)
    portal.user_permissions.add(grant)
    Group.objects.get(name="Technical").permissions.add(grant)
    TOTP.activate(ordinary, generate_totp_secret())
    RecoveryCodes.activate(arbitrary)
    enrolled = {ordinary.pk}
    held = {u.pk: effective_roles(u) for u in users}
    patient = {u.pk for u in users if has_capability(u, "view_patient_information")}

    def expected(user, rule):
        if not user.is_active:
            return []
        labels = [ROLE_LABELS[r] for r in rule.roles if r in held[user.pk]]
        role = (
            (", ".join(labels[:-1]) + " or " + labels[-1]) if len(labels) > 1 else "".join(labels)
        )
        return [
            reason
            for match, reason in (
                (rule.staff and user.client_id is None, "every staff account"),
                (bool(role), f"the {role} role"),
                (
                    rule.patient_information and user.pk in patient,
                    "staff who can view patient information",
                ),
                (rule.admin_console and user.is_staff, "accounts that can open the admin console"),
                (rule.portal and user.client_id is not None, "client portal accounts"),
            )
            if match
        ]

    for bits in product((False, True), repeat=4):
        for n in range(len(ROLES) + 1):
            for roles in combinations(ROLES, n):
                rule = require(**dict(zip(policy.FLAGS, bits, strict=True)), roles=list(roles))
                by_user = {u.pk: expected(u, rule) for u in users}
                covered = {pk for pk, reasons in by_user.items() if reasons}
                desired = (len(covered), len(covered - enrolled))
                assert policy.counts() == policy.counts(rule) == desired, (bits, roles)
                for user in users:
                    assert policy.reasons(user) == policy.reasons(user, rule) == by_user[user.pk]
                    assert (
                        policy.requires(user)
                        == policy.requires(user, rule)
                        == bool(by_user[user.pk])
                    )


def test_unsaved_supplied_rule_and_absent_or_different_live_rule(make_user):
    user = make_user("explicit", "account_manager")
    supplied = SecondFactorRule(roles=["account_manager"])
    for saved in (None, {"roles": ["technical"]}):
        if saved is not None:
            require(**saved)
        assert (policy.requires(user), policy.reasons(user), policy.counts()) == (False, [], (0, 0))
        assert policy.requires(user, supplied)
        assert policy.reasons(user, supplied) == ["the Account manager role"]
        assert policy.counts(supplied) == (1, 1)


def test_absent_and_supplied_active_rules_each_use_one_statement(make_user):
    user = make_user("query_count", "technical")
    for selected in (None, SecondFactorRule(roles=["technical"], staff=True)):
        for read, args in (
            (policy.requires, (user,)),
            (policy.reasons, (user,)),
            (policy.counts, ()),
        ):
            with CaptureQueriesContext(connection) as queries:
                read(*args, current=selected)
            assert len(queries) == 1, (read.__name__, selected)


def test_retired_role_in_stored_rule_is_ignored_like_the_same_supplied_values(client, make_user):
    person = make_user("custom_role", "Technical")
    # An unrecognised name may survive a configuration import or role rename.
    rule = require(roles=["Technical"])
    assert policy.values(rule)["roles"] == []
    supplied = (policy.requires(person, rule), policy.reasons(person, rule), policy.counts(rule))
    live = (policy.requires(person), policy.reasons(person), policy.counts())
    assert supplied == (False, [], (0, 0))
    client.force_login(make_user("display_reader", "admin"))
    panel = client.get("/lab/security/")
    assert panel.status_code == 200
    fact = next(
        f for group in panel.context["groups"] for f in group.facts if f.key == "second-factor"
    )
    assert fact.value == "Optional" and panel.context["second_factor_covered"] == 0
    member = Browser()
    sign_in(member, person)
    destination = member.get(SETUP)
    assert (live, destination.status_code) == (supplied, 302)


def test_an_admin_can_clear_a_stored_rule_containing_only_a_retired_role(make_user):
    editor = make_user("rule_editor", "admin")
    person = make_user("retired_member", "former_role")
    stored = require(roles=["former_role"])
    policy.save_rule(
        actor=editor, new={}, expected_revision=stored.revision, reason="Clear the old selection."
    )
    assert not policy.requires(person)


@pytest.mark.parametrize(
    "headers",
    [{"HTTP_SEC_FETCH_DEST": "empty"}, {"HTTP_HX_REQUEST": "true"}, {"HTTP_X_VR_FORM": "1"}],
    ids=["fetch", "partial", "dialog"],
)
def test_background_setup_read_preserves_the_first_window_destination(client, make_user, headers):
    user = make_user("window_first", "technical")
    require(staff=True)
    sign_in(client, user)
    assert client.get(SETUP, **headers).status_code == 200
    destination = "/requests/?view=all"
    assert client.get(destination).url == SETUP
    TOTP.activate(user, generate_totp_secret())
    assert client.get(SETUP).url == destination


def test_background_setup_read_after_activation_preserves_continue_for_the_window(
    client, make_user
):
    user = make_user("window_continue", "technical")
    require(staff=True)
    sign_in(client, user)
    destination = "/requests/?view=all"
    assert client.get(destination).url == SETUP
    assert client.session[NEXT_KEY] == destination
    TOTP.activate(user, generate_totp_secret())
    client.get(SETUP, HTTP_SEC_FETCH_DEST="empty")
    assert client.get(SETUP).url == destination


@pytest.mark.parametrize("stored_type", ["webauthn", "totp", "recovery_codes"])
def test_live_gate_and_inventory_after_credential_is_stored(
    client, make_user, settings, stored_type
):
    user = make_user("inventory", "technical")
    require(staff=True)
    sign_in(client, user)
    Authenticator.objects.create(user=user, type=stored_type, data={})
    for supported in (["totp", "recovery_codes"], ["totp", "webauthn", "recovery_codes"]):
        settings.MFA_SUPPORTED_TYPES = supported
        enrolled = stored_type in supported and stored_type != "recovery_codes"
        assert _second_factor_counts() == (int(enrolled), 1, 0, 0)
        assert policy.counts() == (1, int(not enrolled))
        assert policy.has_second_factor(user) == enrolled
        response = client.get("/requests/")
        if enrolled:
            assert response.status_code == 200
        else:
            assert response.url == SETUP


@pytest.mark.xfail(
    strict=True,
    reason=(
        "allauth's sign-in asks for a code whenever any credential is stored, including a type "
        "the deployment does not offer; predates this change (docs/architecture.md, Required "
        "second factor)"
    ),
)
def test_signin_with_only_a_retired_credential_offers_a_usable_method_or_recovery_guidance(
    client,
    make_user,
):
    person = make_user("retired_key", "technical")
    require(staff=True)
    # The release sign-in stage checks the retained row's type only.
    Authenticator.objects.create(user=person, type=Authenticator.Type.WEBAUTHN, data={})
    response = sign_in(client, person)
    landing = client.get(response.url, follow=True)
    if landing.resolver_match.view_name == "account-second-factor":
        assert landing.context["reasons"] == ["every staff account"]
        return
    assert landing.resolver_match.view_name == "mfa_authenticate"
    assert "_auth_user_id" not in client.session
    if landing.context.get("webauthn_form"):
        return
    # If the previous key cannot be used, the challenge must explain the assisted
    # recovery path. Neither a TOTP nor a recovery code exists for this account.
    visible = " ".join(strip_tags(landing.content.decode()).casefold().split())
    assert any(word in visible for word in ("administrator", "contact your", "contact the lab"))


@pytest.mark.parametrize("surface", ["facts", "setup-help", "deactivation-help"])
def test_open_session_explanation_matches_rule_restoration(client, make_user, surface):
    user = make_user("restored", "technical")
    require(roles=["technical"])
    sign_in(client, user)
    require()
    TOTP.activate(user, generate_totp_secret())
    assert client.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    assert client.post(reverse("mfa_deactivate_totp")).status_code == 302
    assert not policy.has_second_factor(user)
    assert client.get("/requests/").status_code == 200
    require(roles=["technical"])
    assert client.get("/requests/").url == SETUP
    if surface == "facts":
        fact = next(f for group in in_force() for f in group.facts if f.key == "second-factor")
        assert "sessions already open are not interrupted" not in fact.note.lower()
    elif surface == "setup-help":
        section = next(s for s in setup_help()["sections"] if s["title"] == "Why you are here")
        assert (
            "a rule saved while you were already signed in applies from your next sign-in"
            not in (" ".join(section["paragraphs"]).lower())
        )
    else:
        doc = json.loads(
            (Path(django_settings.BASE_DIR) / "docs/pages/accounts/mfa-deactivate.json").read_text()
        )
        section = next(s for s in doc["sections"] if s["title"] == "Eligibility and navigation")
        paragraph = next(
            p.lower() for p in section["paragraphs"] if "removing the app" in p.lower()
        )
        assert any(
            s in paragraph
            for s in ("still covers", "still requires", "no longer covers", "continues to cover")
        )


def test_setup_help_qualifies_confirmation_for_an_account_without_a_method(
    client, make_user, settings
):
    require(staff=True)
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("without_method", "technical")
    sign_in_by_code(client, user)
    assert get_reauthentication_flows(user) == []
    assert client.get("/requests/").url == SETUP
    assert client.get(reverse("mfa_activate_totp")).status_code == 200
    section = next(s for s in setup_help()["sections"] if s["title"] == "Fields and actions")
    action = next(item for item in section["items"] if item["label"] == "Set up authenticator app")
    description = action["description"].lower()
    assert any(
        s in description
        for s in (
            "if available",
            "when asked",
            "neither",
            "no method",
            "available method",
            "if you have",
            "may ask",
        )
    )


@pytest.mark.parametrize("audience", ["staff", "portal", "shared", "uncovered", "anonymous"])
@pytest.mark.parametrize("surface", ["csrf", "unknown", "disabled-module"])
def test_early_error_pages_draw_the_correct_shell(make_user, audience, surface):
    browser = Browser(enforce_csrf_checks=True)
    require(roles=["technical"], portal=True)
    if audience != "anonymous":
        client_org = (
            Client.objects.create(name="Example organization") if audience == "portal" else None
        )
        role = "supervisor" if audience == "uncovered" else "technical"
        person = make_user(
            "error_reader", role, client=client_org, credentials_locked=audience == "shared"
        )
        browser.force_login(person)
    if surface == "csrf":
        page = browser.post(reverse("account-session-continue"))
        assert page.status_code == 403
        assert b"This form could not be verified" in page.content
    else:
        path = "/quality/" if surface == "disabled-module" else "/no-such-review-page/"
        page = browser.get(path)
        assert page.status_code == 404
    waiting = audience in {"staff", "portal", "shared"}
    assert bool(page.context["second_factor_pending"]) == waiting
    assert (b'aria-label="Primary"' in page.content) == (audience == "uncovered")
    assert b'aria-label="Portal"' not in page.content
    has_menu_link = f'href="{reverse("account-sessions")}"'.encode() in page.content
    assert has_menu_link == (audience == "uncovered")
    mfa_link = f'href="{reverse("mfa_index")}"'.encode() in page.content
    assert mfa_link == (audience in {"staff", "portal", "uncovered"})
    if audience != "anonymous":
        assert "no-store" in page["Cache-Control"]


def test_portal_menu_returns_on_an_error_after_enrolment(client, make_user):
    org = Client.objects.create(name="Example clinic")
    user = make_user("client_reader", client=org)
    require(portal=True)
    sign_in(client, user)
    TOTP.activate(user, generate_totp_secret())
    page = client.get("/unknown-portal-page/")
    assert page.status_code == 404
    assert b'aria-label="Portal"' in page.content
    assert f'href="{reverse("account-sessions")}"'.encode() in page.content
    assert f'href="{reverse("email-notices")}"'.encode() not in page.content
    assert "no-store" in page["Cache-Control"]


def test_public_error_pages_dont_show_the_waiting_accounts_menu(
    client, make_user, settings, monkeypatch
):
    require(staff=True)
    user = make_user("error_shell", "technical")
    sign_in(client, user)
    settings.DEBUG = False
    bad_host = client.get(SETUP, HTTP_HOST="unrecognized.example.invalid")
    assert bad_host.status_code == 400

    def unavailable(_person):
        raise RuntimeError("Synthetic rendering failure")

    monkeypatch.setattr(policy, "reasons", unavailable)
    client.raise_request_exception = False
    server_error = client.get(SETUP)
    assert server_error.status_code == 500 and b"Something went wrong" in server_error.content
    for page in (bad_host, server_error):
        assert b'aria-label="Primary"' not in page.content
        assert f'href="{reverse("account-sessions")}"'.encode() not in page.content
        assert "no-store" in page["Cache-Control"]


def test_early_plain_refusals_and_redirects_have_no_work_menu(client, make_user, settings):
    require(staff=True)
    person = make_user("early_response", "technical", credentials_locked=True)
    sign_in(client, person)
    locked = client.get(reverse("mfa_index"))
    assert locked.status_code == 302 and locked.url == reverse("dashboard")
    canonical = client.get("/requests")
    assert canonical.status_code == 301 and canonical.url == "/requests/"
    settings.MAX_REQUEST_BODY_BYTES = 1024
    # A fresh handler picks up the new middleware instance's size limit.
    same = Browser()
    same.cookies = client.cookies.copy()
    for length, status in [("invalid", 400), ("1025", 413)]:
        response = same.get("/requests/", CONTENT_LENGTH=length)
        assert response.status_code == status
        assert response["Content-Type"].startswith("text/plain")
        assert "no-store" in response["Cache-Control"]
    settings.SECURE_SSL_REDIRECT = True
    secure = Browser()
    secure.cookies = client.cookies.copy()
    https = secure.get("/requests/")
    assert https.status_code == 301 and https.url.startswith("https://")
    for early in (https, canonical, locked):
        assert b'aria-label="Primary"' not in early.content
        assert "no-store" in early["Cache-Control"]


def sign_in_by_code(browser, user):
    user.set_unusable_password()
    user.save(update_fields=["password"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    request_code(browser, user.email)
    browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert browser.session["_auth_user_id"] == str(user.pk)


def test_recovery_confirmation_returns_to_setup_and_preserves_the_gate(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    require(staff=True)
    user = make_user("confirm_routes", "technical")
    codes = RecoveryCodes.activate(user)
    sign_in_by_code(client, user)
    activate = reverse("mfa_activate_totp")
    prompt = client.get(activate, follow=True)
    assert prompt.resolver_match.view_name == "mfa_reauthenticate"
    confirmation = prompt.wsgi_request.get_full_path()
    assert client.post(confirmation, {"code": ""}).status_code == 200
    assert client.get("/requests/").url == SETUP
    before = len(codes.get_unused_codes())
    response = client.post(confirmation, {"code": codes.get_unused_codes()[0]})
    assert response.status_code == 302 and response.url == activate
    assert client.get(response.url).context["page_documentation"]["id"] == "accounts-mfa-activate"
    codes.instance.refresh_from_db()
    assert len(codes.get_unused_codes()) == before - 1
    assert not policy.has_second_factor(user)
    assert client.get("/requests/").url == SETUP


def test_waiting_mfa_overview_only_offers_recovery_actions_that_can_open(client, make_user):
    user = make_user("overview", "technical")
    require(staff=True)
    RecoveryCodes.activate(user)
    sign_in(client, user)
    page = client.get(reverse("mfa_index"))
    assert page.status_code == 200
    assert f'href="{reverse("mfa_activate_totp")}"'.encode() in page.content
    actions = (
        "mfa_view_recovery_codes",
        "mfa_download_recovery_codes",
        "mfa_generate_recovery_codes",
    )
    choices = []
    for name in actions:
        url = reverse(name)
        if f'href="{url}"'.encode() in page.content:
            choices.append((name, client.get(url).get("Location")))
    assert all(location != SETUP for _, location in choices), choices


@pytest.mark.xfail(
    strict=True,
    reason=(
        "allauth's re-authentication offers the recovery codes record however many codes are "
        "left; predates this change (docs/architecture.md, Required second factor)"
    ),
)
def test_code_only_account_with_spent_codes_gets_assisted_recovery_guidance(
    client, make_user, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    require(staff=True)
    user = make_user("spent_only", "technical")
    codes = RecoveryCodes.activate(user)
    for code in codes.get_unused_codes():
        assert codes.validate_code(code)
    sign_in_by_code(client, user)
    assert codes.get_unused_codes() == []
    assert client.get("/requests/").url == SETUP
    prompt = client.get(reverse("mfa_activate_totp"), follow=True)
    if prompt.resolver_match.view_name == "mfa_activate_totp":
        return
    assert prompt.resolver_match.view_name == "mfa_reauthenticate"
    visible = " ".join(strip_tags(prompt.content.decode()).casefold().split())
    assert any(word in visible for word in ("administrator", "contact your", "contact the lab"))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "page help follows the route resolved, the re-authentication page's, when allauth resumes "
        "the posted setup form; predates this change (docs/architecture.md, Required second "
        "factor)"
    ),
)
@pytest.mark.parametrize("method", ["password", "recovery"])
def test_resumed_activation_validation_shows_help_for_the_form_on_screen(
    client, make_user, settings, method, monkeypatch
):
    from accounts.enrolment import EnrolmentMiddleware

    visited = []
    original = EnrolmentMiddleware.process_view

    def observe_route(self, request, view, args, kwargs):
        visited.append(request.resolver_match.view_name)
        return original(self, request, view, args, kwargs)

    # Install before the client builds its middleware chain. Trace only the
    # confirmation response below, which draws the permitted activation form.
    monkeypatch.setattr(EnrolmentMiddleware, "process_view", observe_route)
    require(staff=True)
    user = make_user("confirm_help", "technical")
    if method == "password":
        sign_in(client, user)
        confirmation = {"password": PASSWORD}
        expected_prompt = "account_reauthenticate"
    else:
        settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
        codes = RecoveryCodes.activate(user)
        sign_in_by_code(client, user)
        confirmation = {"code": codes.get_unused_codes()[0]}
        expected_prompt = "mfa_reauthenticate"
    # Only the ordinary activation form is involved. A blank field makes its
    # validation error and the accompanying Help drawer observable.
    activation = reverse("mfa_activate_totp")
    prompt = client.post(activation, {"code": ""})
    assert urlsplit(prompt.url).path == reverse(expected_prompt)
    visited.clear()
    page = client.post(prompt.url, confirmation, follow=True)
    confirmation_views = tuple(visited)
    assert "code" in page.context["form"].errors
    assert page.context["form"].__class__.__name__ == "ActivateTOTPForm"
    assert client.get("/requests/").url == SETUP
    assert page.context["page_documentation"]["id"] == "accounts-mfa-activate", confirmation_views


@pytest.mark.django_db(transaction=True)
def test_setup_instructions_match_one_state_during_activation(make_user, monkeypatch):
    user = make_user("two_browsers", "technical")
    require(staff=True)
    reader, editor = Browser(), Browser()
    for browser in (reader, editor):
        sign_in(browser, user)
    activation = reverse("mfa_activate_totp")
    confirm = editor.get(activation)
    assert editor.post(confirm.url, {"password": PASSWORD}).status_code == 302
    assert editor.get(activation).status_code == 200
    assert not Authenticator.objects.filter(user=user).exists()
    original_reasons = policy.reasons

    def activate():
        try:
            code = code_for(editor.session[SECRET_SESSION_KEY])
            response = editor.post(activation, {"code": code})
            assert response.url == reverse("mfa_view_recovery_codes")
        finally:
            connections.close_all()

    def read_reasons_then_activate(*args, **kwargs):
        reasons = original_reasons(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(activate).result(timeout=15)
        return reasons

    monkeypatch.setattr(policy, "reasons", read_reasons_then_activate)
    page = reader.get(SETUP)
    assert set(Authenticator.objects.filter(user=user).values_list("type", flat=True)) == {
        "totp",
        "recovery_codes",
    }
    if page.status_code == 302:
        assert page.url == reverse("dashboard")
    else:
        assert page.status_code == 200
        assert b"Your account's recovery codes stay as they are" not in page.content


def test_setup_guidance_with_no_unused_recovery_codes(client, make_user):
    require(staff=True)
    user = make_user("spent_codes", "technical")
    codes = RecoveryCodes.activate(user)
    for code in codes.get_unused_codes():
        assert codes.validate_code(code)
    assert codes.get_unused_codes() == []
    sign_in(client, user)
    page = client.get(SETUP)
    assert page.status_code == 200
    activate = reverse("mfa_activate_totp")
    confirm = client.get(activate)
    assert client.post(confirm.url, {"password": PASSWORD}).status_code == 302
    assert client.get(activate).status_code == 200
    completed = client.post(activate, {"code": code_for(client.session[SECRET_SESSION_KEY])})
    assert completed.url == reverse("mfa_index")
    overview = client.get(completed.url)
    assert b"0 out of" in overview.content
    # The setup instructions should tell this person how to obtain usable backups.
    instructions = strip_tags(
        page.content.decode().split("How to set it up", 1)[1].split("</ol>", 1)[0]
    )
    assert "generate" in instructions.lower() or "replace" in instructions.lower()


def test_architecture_names_the_credential_types_that_release_the_gate(client, make_user):
    user = make_user("documented_factor", "technical")
    require(staff=True)
    sign_in(client, user)
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    response = client.get("/requests/")
    if response.status_code == 200:
        assert policy.has_second_factor(user)
        return  # On the original feature any non-recovery credential released the gate.
    assert response.url == SETUP
    path = Path(django_settings.BASE_DIR) / "docs/architecture.md"
    section = path.read_text().split("## Required second factor", 1)[1].split("\n## ", 1)[0]
    assert "whether the account has a non-recovery-code authenticator in one statement" not in (
        " ".join(section.split())
    )


@pytest.mark.django_db(transaction=True)
def test_setup_never_demands_an_app_when_no_committed_state_requires_enrolment(client, make_user):
    editor = make_user("rule_author", "admin")
    person = make_user("already_set_up", "technical")
    require(roles=["technical"])
    sign_in(client, person)
    # The session was originally covered. Before opening setup it is released by
    # a rule change. A second browser then adds the app before coverage is restored.
    require()
    assert not policy.requires(person) and not policy.has_second_factor(person)
    other = Browser()
    sign_in(other, person)
    activation = reverse("mfa_activate_totp")
    prompt = other.get(activation)
    assert other.post(prompt.url, {"password": PASSWORD}).status_code == 302
    assert other.get(activation).status_code == 200
    observed = []

    def add_app_then_restore():
        try:
            code = code_for(other.session[SECRET_SESSION_KEY])
            assert other.post(activation, {"code": code}).url == reverse("mfa_view_recovery_codes")
            policy.save_rule(
                actor=editor,
                new={"roles": ["technical"]},
                expected_revision=policy.rule().revision,
                reason="Restore rollout.",
            )
        finally:
            connections.close_all()

    def after_session_and_factor_read(execute, sql, params, many, context):
        value = execute(sql, params, many, context)
        if not observed and "accounts_accountsession" in sql and "mfa_authenticator" in sql:
            observed.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(add_app_then_restore).result(timeout=15)
        return value

    with connection.execute_wrapper(after_session_and_factor_read):
        page = client.get(SETUP)
    assert observed and policy.requires(person) and policy.has_second_factor(person)
    assert page.status_code == 302 and page.url == reverse("dashboard")
