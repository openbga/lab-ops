"""Independent confirmation review. All accounts, changes and submitted values are synthetic."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal import auth as totp
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.db import connections, transaction
from django.test import Client
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from accounts import compromised
from accounts.models import CompromisedPassword, SignInEvent, User
from accounts.roles import ROLES
from clients.models import Client as Organization
from core.models import AuditEvent, SecondFactorRule, SessionLimit

from .conftest import PASSWORD

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db]


@pytest.fixture
def review_owner(make_user):
    return make_user("form-owner", "admin", is_staff=True, is_superuser=True)


def review_form_case(client, case, target):
    """Open the named form and return the business fields for one ordinary edit."""
    option = {}
    headers = {"HTTP_X_VR_FORM": "1"} if case.endswith("-dialog") else {}
    if case.startswith("edit-account"):
        route, args = "admin:accounts_user_change", [target.pk]
        button = case.removeprefix("edit-account-")
        option = {"_" + button: "1", "first_name": "Morgan"}
    elif case.startswith("add-account"):
        route, args = "admin:accounts_user_add", []
        option = {
            "username": "review-new",
            "email": "review-new@example.invalid",
            "usable_password": "true",
            "password1": "candle maple quartz gallery 381",
            "password2": "candle maple quartz gallery 381",
        }
    elif case in ("set-password", "unset-password"):
        route, args = "admin:auth_user_password_change", [target.pk]
        option = {case: "1", "usable_password": "false"}
        if case == "set-password":
            new = "candle maple quartz gallery 381"
            option.update(usable_password="true", password1=new, password2=new)
    elif case == "mark":
        route, args = "admin:accounts_user_compromised", [target.pk]
        option = {"reason": "Synthetic credential rotation."}
    elif case == "remove":
        record = TOTP.activate(target, generate_totp_secret()).instance
        route, args = "admin:mfa_authenticator_delete", [record.pk]
        option = {"post": "yes"}
    elif case == "add-role":
        route, args = "admin:auth_group_add", []
        option = {"name": "Review group"}
    elif case == "edit-role":
        role = Group.objects.create(name="Review group")
        route, args = "admin:auth_group_change", [role.pk]
        permission = Permission.objects.get(codename="view_patient_information")
        option = {"name": role.name, "permissions": [permission.pk]}
    elif case.startswith("limits"):
        route, args = "session-limits-edit", ["portal" if "portal" in case else "staff"]
        option = {"inactivity_minutes": 14, "maximum_hours": 7, "reason": "Review shift."}
    else:
        assert case.startswith("rule")
        route, args = "second-factor-edit", []
        option = {"portal": "on", "reason": "Review enrolment."}
    url = reverse(route, args=args)
    if case.endswith("-popup"):
        url += "?_popup=1"
        option["_popup"] = "1"
    opened = client.get(url, **headers)
    assert opened.status_code == 200
    if case.startswith("edit-account"):
        form = opened.context["adminform"].form
        option.update(
            drawn=form["drawn"].value(),
            username=target.username,
            email=target.email,
            is_active="on",
            groups=list(target.groups.values_list("pk", flat=True)),
        )
    elif case == "mark":
        option["drawn"] = opened.context["form"]["drawn"].value()
    elif case.startswith(("limits", "rule")):
        option["expected_revision"] = opened.context["form"]["expected_revision"].value()
    return url, headers, option


def review_snapshot():
    """Persisted business records and both change histories for the fixtures."""
    models = [
        User,
        User.groups.through,
        User.user_permissions.through,
        Group,
        Group.permissions.through,
        Authenticator,
        CompromisedPassword,
        SecondFactorRule,
        SessionLimit,
        LogEntry,
        AuditEvent,
    ]
    return {model: list(model.objects.order_by("pk").values()) for model in models}


@pytest.mark.parametrize(
    "case",
    [
        "edit-account-save",
        "edit-account-continue",
        "edit-account-addanother",
        "add-account",
        "add-account-popup",
        "set-password",
        "unset-password",
        "mark",
        "remove",
        "add-role",
        "edit-role",
        "limits-full",
        "limits-dialog",
        "limits-portal-full",
        "limits-portal-dialog",
        "rule-full",
        "rule-dialog",
    ],
)
def test_covered_form_confirmations(client, make_user, review_owner, case, settings):
    """Refusals leave business data intact. The same edit succeeds after confirmation."""
    settings.ACCOUNT_RATE_LIMITS = {"reauthenticate": "1/m/user"}
    target = make_user("form-subject", "technical")
    client.force_login(review_owner)
    url, headers, data = review_form_case(client, case, target)
    before = review_snapshot()
    for credentials, error in [
        ({}, "Enter your password to save this change."),
        ({"confirm_password": "wrong synthetic entry"}, "Incorrect password."),
        ({"confirm_password": PASSWORD}, "Too many attempts to confirm it is you."),
    ]:
        response = client.post(url, {**data, **credentials}, **headers)
        assert response.status_code == 200
        assert error.encode() in response.content
        assert review_snapshot() == before
    assert list(
        SignInEvent.objects.filter(kind="reauth_failed").values_list("user_id", "factor")
    ) == [(review_owner.pk, "password")]
    cache.clear()
    saved = client.post(url, {**data, "confirm_password": PASSWORD}, **headers)
    expected = 204 if headers else 200 if case.endswith("-popup") else 302
    assert saved.status_code == expected
    if headers:
        assert saved["HX-Redirect"] == reverse("security-settings")
    if case.endswith("-popup"):
        assert b"data-popup-response" in saved.content
    assert AuditEvent.objects.count() == len(before[AuditEvent]) + 1
    assert LogEntry.objects.count() == len(before[LogEntry]) + int(
        not case.startswith(("limits", "rule"))
    )
    event = AuditEvent.objects.order_by("-occurred_at").first()
    assert event.actor_id == review_owner.pk
    target.refresh_from_db()
    if case.startswith("edit-account"):
        assert target.first_name == "Morgan"
    elif case.startswith("add-account"):
        assert User.objects.get(username=data["username"]).check_password(data["password1"])
    elif case == "set-password":
        assert target.check_password(data["password1"])
    elif case == "unset-password":
        assert not target.has_usable_password()
    elif case == "mark":
        assert CompromisedPassword.objects.get(user=target).reason == data["reason"]
    elif case == "remove":
        assert not Authenticator.objects.filter(user=target).exists()
    elif case == "add-role":
        assert Group.objects.filter(name=data["name"]).exists()
    elif case == "edit-role":
        assert (
            list(Group.objects.get(name=data["name"]).permissions.values_list("pk", flat=True))
            == data["permissions"]
        )
    elif case.startswith("limits"):
        assert (
            SessionLimit.objects.get(
                audience="portal" if "portal" in case else "staff"
            ).inactivity_minutes
            == 14
        )
    else:
        assert SecondFactorRule.objects.get().portal


@pytest.mark.parametrize("method", ["get", "head", "options", "trace", "put", "patch", "delete"])
def test_nonpost_removal_preserves_record_and_histories(client, review_owner, make_user, method):
    target = make_user("method-subject")
    auth = TOTP.activate(target, generate_totp_secret()).instance
    client.force_login(review_owner)
    url = reverse("admin:mfa_authenticator_delete", args=[auth.pk])
    before = review_snapshot()
    response = getattr(client, method)(url)
    assert response.status_code in {200, 405}
    assert review_snapshot() == before


def test_refused_confirmation_does_not_echo_credentials(client, review_owner, caplog):
    TOTP.activate(review_owner, generate_totp_secret())
    client.force_login(review_owner)
    url = reverse("session-limits-edit", args=["staff"])
    code = "synthetic-review-code"
    password = "synthetic-review-password"
    response = client.post(
        url, {"confirm_password": password, "confirm_code": code, "reason": "Keep ordinary input"}
    )
    assert response.status_code == 200
    report_data = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
    assert report_data["reason"] == "Keep ordinary input"
    assert (
        report_data["confirm_password"]
        == report_data["confirm_code"]
        == SafeExceptionReporterFilter.cleansed_substitute
    )
    form = response.context["form"]
    assert {"confirm_password", "confirm_code"}.isdisjoint(form.changed_data)
    stored = (
        repr(list(AuditEvent.objects.values()))
        + repr(list(LogEntry.objects.values()))
        + repr(list(SignInEvent.objects.values()))
        + repr(dict(client.session))
    )
    for value in (password, code):
        assert value not in stored
        assert value not in caplog.text
        assert value.encode() not in client.get(url).content
        assert value.encode() not in response.content


def test_password_button_conflict_has_no_change_or_change_history(client, review_owner, make_user):
    """The two password buttons are both available to a browser with JavaScript turned off."""
    person = make_user("button-subject")
    client.force_login(review_owner)
    url = reverse("admin:auth_user_password_change", args=[person.pk])
    original = person.password
    response = client.post(
        url, {"usable_password": "false", "set-password": "1", "confirm_password": PASSWORD}
    )
    assert response.status_code == 302 and response.url == url
    person.refresh_from_db()
    assert person.password == original
    assert not LogEntry.objects.filter(object_id=str(person.pk)).exists()
    assert not AuditEvent.objects.filter(
        action="account.password_changed", object_id=str(person.pk)
    ).exists()


def test_authenticator_removal_preserves_popup_context(client, review_owner, make_user):
    person = make_user("popup-subject")
    factor = TOTP.activate(person, generate_totp_secret()).instance
    client.force_login(review_owner)
    url = reverse("admin:mfa_authenticator_delete", args=[factor.pk])
    page = client.get(url, {"_popup": "1"})
    assert page.status_code == 200
    assert page.context["is_popup"]
    assert b'name="_popup"' in page.content


@pytest.mark.django_db(transaction=True)
def test_two_confirmed_editors_of_one_limits_revision_save_once(make_user):
    """Two independent browser sessions submit legitimate edits based on the same revision."""
    actors = [make_user(f"shift-manager-{i}", "admin") for i in (1, 2)]
    clients = [Client(), Client()]
    url = reverse("session-limits-edit", args=["staff"])
    revisions = []
    for browser, actor in zip(clients, actors, strict=True):
        browser.force_login(actor)
        form = browser.get(url).context["form"]
        revisions.append(form["expected_revision"].value())
    assert revisions[0] == revisions[1]
    start = Barrier(2, timeout=8)

    def submit(index):
        try:
            start.wait()
            return clients[index].post(
                url,
                {
                    "inactivity_minutes": 16 + index,
                    "maximum_hours": 8,
                    "expected_revision": revisions[index],
                    "reason": f"Shift {index}",
                    "confirm_password": PASSWORD,
                },
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 302]
    winner = next(i for i, r in enumerate(responses) if r.status_code == 302)
    refused = responses[1 - winner]
    assert b"were changed after you opened this form" in refused.content
    assert b'name="confirm_password"' not in refused.content
    assert refused.context["form"]["reason"].value() == f"Shift {1 - winner}"
    audit = AuditEvent.objects.get(action="lab.session_limits_changed")
    assert audit.actor_id == actors[winner].pk
    assert SessionLimit.objects.get(audience="staff").inactivity_minutes == 16 + winner


@pytest.fixture
def factor_actor(make_user):
    return make_user("review-factor-actor", is_staff=True, is_superuser=True)


def factor_browser(actor):
    browser = Client()
    browser.force_login(actor)
    return browser


def factor_app(actor):
    secret = totp.generate_totp_secret()
    return secret, totp.TOTP.activate(actor, secret).instance


def factor_code(secret):
    import time

    counter = int(time.time()) // mfa_settings.TOTP_PERIOD
    return totp.format_hotp_value(totp.hotp_value(secret, counter))


def factor_prompt(browser):
    page = browser.get(reverse("admin:auth_group_add"))
    assert page.status_code == 200
    return {
        name
        for name in ("confirm_password", "confirm_code")
        if f'name="{name}"'.encode() in page.content
    }


def factor_check(browser, **credentials):
    # The ordinary required role name stays empty, so this only validates the form.
    response = browser.post(reverse("admin:auth_group_add"), {"name": "", **credentials})
    assert response.status_code == 200
    return response


def factor_proofs(browser):
    return [
        item["method"]
        for item in browser.session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
        if item.get("reauthenticated")
    ]


def factor_failures():
    return list(
        SignInEvent.objects.exclude(kind__in=("signed_in", "signed_out"))
        .order_by("pk")
        .values_list("user_id", "kind", "factor")
    )


@pytest.mark.parametrize(
    "password,app,recovery,used,expected",
    [
        (True, False, False, "password", ["password"]),
        (True, False, True, "password", ["password"]),
        (True, True, False, "app", ["password", "mfa"]),
        (True, True, True, "recovery", ["password", "mfa"]),
        (False, True, False, "app", ["mfa"]),
        (False, False, False, "none", []),
        (False, False, True, "none", []),
    ],
)
def test_factor_prompt_and_receipt_match_enrolled_methods(
    factor_actor, password, app, recovery, used, expected
):
    if not password:
        factor_actor.set_unusable_password()
        factor_actor.save(update_fields=["password"])
    credentials = {"confirm_password": PASSWORD} if password else {}
    if app:
        secret, _ = factor_app(factor_actor)
        if used == "app":
            credentials["confirm_code"] = factor_code(secret)
    if recovery:
        codes = RecoveryCodes.activate(factor_actor)
        if used == "recovery":
            credentials["confirm_code"] = codes.get_unused_codes()[0]

    browser = factor_browser(factor_actor)
    assert factor_prompt(browser) == set(credentials)
    response = factor_check(browser, **credentials)
    if used == "none":
        assert b"no password to confirm" in response.content
    assert factor_proofs(browser) == expected
    assert factor_prompt(browser) == set()
    assert factor_failures() == []


@pytest.mark.parametrize("order", [("mfa", "password"), ("password", "mfa")])
def test_factor_allauth_pages_combine_only_in_their_own_browser(factor_actor, order):
    secret, _ = factor_app(factor_actor)
    browser = factor_browser(factor_actor)
    other = factor_browser(factor_actor)
    steps = {
        "mfa": ("mfa_reauthenticate", {"code": factor_code(secret)}),
        "password": ("account_reauthenticate", {"password": PASSWORD}),
    }
    for step in order:
        assert factor_prompt(browser) == {"confirm_password", "confirm_code"}
        route, data = steps[step]
        assert browser.post(reverse(route), data).status_code == 302
    assert factor_proofs(browser) == list(order)
    assert factor_prompt(browser) == set()
    assert factor_prompt(other) == {"confirm_password", "confirm_code"}


def test_factor_form_tracks_a_new_authenticator_after_removal_and_reenrolment(factor_actor):
    secret, first = factor_app(factor_actor)
    browser = factor_browser(factor_actor)
    factor_check(browser, confirm_password=PASSWORD, confirm_code=factor_code(secret))
    assert factor_proofs(browser) == ["password", "mfa"]
    first.delete()
    assert factor_prompt(browser) == set()  # only the already-proved password remains
    _, replacement = factor_app(factor_actor)
    proof = browser.session[AUTHENTICATION_METHODS_SESSION_KEY][-1]
    assert proof["id"] != replacement.pk
    assert "confirm_code" in factor_prompt(browser)


def test_factor_password_change_in_kept_browser_draws_prompt_for_its_new_password(factor_actor):
    browser = factor_browser(factor_actor)
    assert (
        browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    )
    assert factor_prompt(browser) == set()
    new = "orchard river birch firelight 317"
    change = browser.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": new, "password2": new},
    )
    assert change.status_code == 302
    factor_actor.refresh_from_db()
    assert factor_actor.check_password(new)
    assert "confirm_password" in factor_prompt(browser)


@pytest.mark.parametrize("reason", ["changed", "compromised"])
def test_factor_existing_session_ends_after_external_password_action(
    factor_actor, make_user, reason
):
    browser = factor_browser(factor_actor)
    assert (
        browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    )
    assert factor_proofs(browser) == ["password"]
    if reason == "changed":
        factor_actor.set_password("orchard water paper autumn 328")
        factor_actor.save(update_fields=["password"])
    else:
        owner = make_user("review-mark-owner", is_staff=True, is_superuser=True)
        compromised.mark_compromised(
            actor=owner,
            account_id=factor_actor.pk,
            drawn=compromised.drawn_password(factor_actor),
            reason="Synthetic session lifecycle check.",
        )
    page = browser.get(reverse("admin:auth_group_add"))
    assert page.status_code == 302
    assert not page.wsgi_request.user.is_authenticated
    assert factor_proofs(browser) == []


def test_factor_another_tab_and_a_later_account_in_the_same_browser(factor_actor, make_user):
    first = factor_browser(factor_actor)
    # Two tabs share the authenticated cookie. The second browser in the allauth test does not.
    tab = Client()
    tab.cookies.update(first.cookies)
    assert factor_prompt(tab) == {"confirm_password"}
    assert first.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    assert factor_prompt(tab) == set()
    factor_check(tab, confirm_password="stale typed value")
    assert factor_failures() == []
    next_actor = make_user("review-factor-next", is_staff=True, is_superuser=True)
    tab.force_login(next_actor)
    assert factor_prompt(tab) == {"confirm_password"}
    assert factor_proofs(tab) == []


def test_factor_totp_is_single_use_across_two_confirmations(factor_actor):
    secret, _ = factor_app(factor_actor)
    first = factor_browser(factor_actor)
    second = factor_browser(factor_actor)
    values = {"confirm_password": PASSWORD, "confirm_code": factor_code(secret)}
    factor_check(first, **values)
    assert factor_proofs(first) == ["password", "mfa"]
    refused = factor_check(second, **values)
    assert b"Incorrect code" in refused.content
    assert factor_proofs(second) == []
    assert factor_failures() == [(factor_actor.pk, "reauth_failed", "second_factor")]


@pytest.mark.parametrize("factor_name", ["password", "second_factor"])
def test_factor_failed_signin_limit_writes_one_refusal_for_the_account(
    factor_actor, settings, factor_name
):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key", "reauthenticate": "12/m/user"}
    credentials = {"confirm_password": "incorrect synthetic password"}
    if factor_name == "second_factor":
        factor_app(factor_actor)
        credentials = {
            "confirm_password": PASSWORD,
            "confirm_code": "invalid synthetic code",
        }
    browser = factor_browser(factor_actor)
    factor_check(browser, **credentials)
    for _ in range(2):
        limited = factor_check(browser, **credentials)
        assert b"Too many failed" in limited.content
    assert factor_failures() == [
        (factor_actor.pk, "reauth_failed", factor_name),
        (factor_actor.pk, "locked_out", factor_name),
    ]
    assert factor_proofs(browser) == []


def test_factor_reauthentication_request_limit_is_shared_across_browsers(factor_actor, settings):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "8/m/key", "reauthenticate": "1/m/user"}
    first = factor_browser(factor_actor)
    second = factor_browser(factor_actor)
    factor_check(first, confirm_password="incorrect synthetic password")
    limited = factor_check(second, confirm_password=PASSWORD)
    assert b"Too many attempts to confirm" in limited.content
    assert factor_failures() == [(factor_actor.pk, "reauth_failed", "password")]
    assert factor_proofs(first) == factor_proofs(second) == []


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        "supervisor",
        "client_services",
        "account_manager",
        "technical",
        "staff_status",
        "admin",
        "superuser_only",
        "portal",
        "deactivated",
        "shared",
    ],
)
def test_authz_refusals_precede_password_and_code_checks(client, make_user, monkeypatch, audience):
    """The normal role/console gate rejects both opening and submitting each unavailable form."""
    target = make_user("authz-target", "technical")
    factor = TOTP.activate(target, generate_totp_secret()).instance
    group = Group.objects.get(name="technical")
    staff_flag = audience in {*ROLES, "staff_status", "superuser_only", "deactivated"}
    if audience != "anonymous":
        roles = [audience] if audience in ROLES else []
        if audience == "shared":
            roles = ["technical"]  # An account published by the demonstration seed.
        actor = make_user(
            "authz-actor",
            *roles,
            is_staff=staff_flag,
            is_superuser=audience in {"superuser_only", "deactivated"},
            is_active=audience != "deactivated",
            credentials_locked=audience == "shared",
            client=Organization.objects.create(name="Synthetic client")
            if audience == "portal"
            else None,
        )
        client.force_login(actor)

    password_check = Mock(return_value=False)
    code_check = Mock(return_value=False)
    monkeypatch.setattr("accounts.adapter.ClosedSignupAdapter.reauthenticate", password_check)
    monkeypatch.setattr("accounts.forms.MFAReauthenticateForm.is_valid", code_check)
    anonymous = audience in {"anonymous", "deactivated"}
    refused_routes = []
    if audience != "superuser_only":
        console_status = 403 if staff_flag and not anonymous else 302
        for route, args in [
            ("admin:accounts_user_add", []),
            ("admin:accounts_user_change", [target.pk]),
            ("admin:auth_user_password_change", [target.pk]),
            ("admin:accounts_user_compromised", [target.pk]),
            ("admin:auth_group_add", []),
            ("admin:auth_group_change", [group.pk]),
            ("admin:mfa_authenticator_delete", [factor.pk]),
        ]:
            refused_routes.append((reverse(route, args=args), {}, console_status, "admin:login"))
        refused_routes.append(
            (reverse("admin:accounts_user_add") + "?_popup=1", {}, console_status, "admin:login")
        )
    if audience != "admin":
        for url in [
            reverse("session-limits-edit", args=["staff"]),
            reverse("session-limits-edit", args=["portal"]),
            reverse("second-factor-edit"),
        ]:
            refused_routes.append((url, {}, 302 if anonymous else 403, "account_login"))
            refused_routes.append(
                (url, {"HTTP_X_VR_FORM": "1"}, 204 if anonymous else 403, "account_login")
            )

    entries = LogEntry.objects.count()
    audits = AuditEvent.objects.count()
    failures = SignInEvent.objects.filter(
        kind__in=[SignInEvent.Kind.REAUTHENTICATION_FAILED, SignInEvent.Kind.LOCKED_OUT]
    )
    failures_before = failures.count()
    for url, headers, status, sign_in in refused_routes:
        for method in ("get", "post"):
            if method == "get":
                response = client.get(url, **headers)
            else:
                response = client.post(
                    url, {"confirm_password": "sample input", "confirm_code": "000000"}, **headers
                )
            assert response.status_code == status, (audience, method, url)
            if status in {204, 302}:
                header = "HX-Redirect" if status == 204 else "Location"
                assert urlsplit(response[header]).path == reverse(sign_in)
            assert b'name="confirm_password"' not in response.content, (audience, url)
            assert b'name="confirm_code"' not in response.content, (audience, url)
            password_check.assert_not_called()
            code_check.assert_not_called()
    assert failures.count() == failures_before
    assert LogEntry.objects.count() == entries
    assert AuditEvent.objects.count() == audits


def test_authz_withdrawn_mark_action_does_not_check_credential(client, make_user, monkeypatch):
    """A withdrawn action is refused before checking identity."""
    actor = make_user("authz-owner", is_staff=True, is_superuser=True)
    target = make_user("authz-mark")
    client.force_login(actor)
    url = reverse("admin:accounts_user_compromised", args=[target.pk])
    page = client.get(url)
    assert page.status_code == 200
    state = page.context["form"].initial["drawn"]

    target.set_unusable_password()
    target.save(update_fields=["password"])
    check = Mock(return_value=False)
    monkeypatch.setattr("accounts.adapter.ClosedSignupAdapter.reauthenticate", check)
    response = client.post(
        url, {"drawn": state, "reason": "Synthetic review.", "confirm_password": "sample input"}
    )

    assert response.status_code == 200
    assert compromised.REFUSALS["unusable"] in response.content.decode()
    assert b'name="confirm_password"' not in response.content
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()
    assert not LogEntry.objects.exists()
    check.assert_not_called()


def test_redaction_survives_loss_of_editor_role(client, make_user, settings):
    """The production reporter's POST table also masks a form that lost permission to save."""
    settings.DEBUG = False
    actor = make_user("review-former-editor", "admin")
    factor_app(actor)
    client.force_login(actor)
    url = reverse("session-limits-edit", args=["staff"])
    opened = client.get(url)
    assert opened.status_code == 200
    for name in ("confirm_password", "confirm_code"):
        assert f'name="{name}"'.encode() in opened.content
    actor.groups.clear()
    response = client.post(
        url, {"confirm_password": "synthetic form value", "confirm_code": "synthetic code value"}
    )
    assert response.status_code == 403
    reporter = SafeExceptionReporterFilter()
    filtered = reporter.get_post_parameters(response.wsgi_request)
    assert all(
        filtered[name] == reporter.cleansed_substitute
        for name in ("confirm_password", "confirm_code")
    )


@pytest.mark.django_db(transaction=True)
def test_factor_reading_observes_a_committed_change_to_password_availability(make_user):
    """Read the factor inventory while a separate connection updates password availability."""
    from accounts.confirmation import factors

    actor = make_user("factor-inventory")
    actor.set_unusable_password()
    actor.save(update_fields=["password"])

    def set_first_password():
        try:
            writer = User.objects.get(pk=actor.pk)
            writer.set_password("synthetic quartz hazel river 518")
            writer.save(update_fields=["password"])
        finally:
            connections.close_all()

    with transaction.atomic(), ThreadPoolExecutor(max_workers=1) as pool:
        reader = User.objects.get(pk=actor.pk)
        assert factors(reader) == (False, False)
        pool.submit(set_first_password).result(timeout=8)
        assert factors(User.objects.get(pk=reader.pk)) == (True, False)
        assert factors(reader) == (True, False)


def test_separately_timed_allauth_proofs_expire_at_the_older_proof(factor_actor, monkeypatch):
    """Each receipt has its own five-minute window. Both came from the real allauth forms."""
    from accounts import confirmation

    browser = factor_browser(factor_actor)
    secret, _ = factor_app(factor_actor)
    assert (
        browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    )
    assert (
        browser.post(reverse("mfa_reauthenticate"), {"code": factor_code(secret)}).status_code
        == 302
    )
    session = browser.session
    receipts = session[AUTHENTICATION_METHODS_SESSION_KEY]
    later = receipts[-1]["at"]
    receipts[0]["at"] = later - 240
    session[AUTHENTICATION_METHODS_SESSION_KEY] = receipts
    session.save()
    monkeypatch.setattr(confirmation, "time", SimpleNamespace(time=lambda: later + 59))
    assert factor_prompt(browser) == set()
    monkeypatch.setattr(confirmation, "time", SimpleNamespace(time=lambda: later + 60))
    assert factor_prompt(browser) == {"confirm_password", "confirm_code"}


def test_accepted_factors_stay_out_of_change_history_and_session(client, review_owner, caplog):
    """An ordinary successful change carries only the evidence that the two checks succeeded."""
    import logging

    factor_app(review_owner)
    code = RecoveryCodes.activate(review_owner).get_unused_codes()[0]
    client.force_login(review_owner)
    url = reverse("admin:auth_group_add")
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            url,
            {
                "name": "Evening rotation",
                "confirm_password": PASSWORD,
                "confirm_code": code,
            },
        )
        assert response.status_code == 302
        role = Group.objects.get(name="Evening rotation")
        assert AuditEvent.objects.get(action="group.created").actor_id == review_owner.pk
        entry = LogEntry.objects.get(object_id=str(role.pk))
        assert "confirm" not in entry.get_change_message().lower()
        assert factor_proofs(client) == ["password", "mfa"]
        next_page = client.get(reverse("admin:auth_group_change", args=[role.pk]))
    rendered = [next_page.content.decode(), caplog.text, repr(dict(client.session))]
    for model in (AuditEvent, LogEntry, SignInEvent, User):
        rendered.append(repr(list(model.objects.values())))
    for secret in (PASSWORD, code):
        assert all(secret not in value for value in rendered)
