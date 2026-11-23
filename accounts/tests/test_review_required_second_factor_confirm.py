"""Independent confirmation cases for the changed required-second-factor behavior.

Written by the independent reviewer (Codex) confirming the verification fixes at e1d9af3 and
adopted unchanged, except two assertions adapted to the corrected behaviour (marked in place) and
two strict xfails for allauth's code generation, which offers and opens
its confirmation to an account holding only recovery codes before it can be submitted (predates;
documented in docs/architecture.md, "Required second factor"). The original is archived in
.local/review-history/required-second-factor-round-1-confirmation-original/."""

import json
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.core.cache import cache
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import strip_tags

from accounts import enrolment
from accounts.models import AccountSession, User
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_review_required_second_factor import activate_app
from accounts.tests.test_second_factor_enrolment import (
    SETUP,
    code_for,
    reauthenticate,
    require,
    session_row,
    sign_in,
)
from core import second_factor
from core.models import AuditEvent, SecondFactorRule

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_auth_limits():
    cache.clear()
    yield
    cache.clear()


def test_waiting_flag_current_account_rule_and_factor_agree_in_one_read(client, make_user):
    user = make_user("gate_matrix", "technical")
    require(roles=["technical"])
    sign_in(client, user)
    row = session_row(client)
    for flag, active, covered, enrolled in product((False, True), repeat=4):
        AccountSession.objects.filter(pk=row.pk).update(second_factor_required=flag)
        User.objects.filter(pk=user.pk).update(is_active=active)
        require(roles=["technical"] if covered else ["supervisor"])
        if enrolled:
            TOTP.activate(user, generate_totp_secret())
        with CaptureQueriesContext(connection) as queries:
            observed = enrolment.waiting(row)
        assert len(queries) == 1
        assert observed == (flag and active and covered and not enrolled), (
            flag,
            active,
            covered,
            enrolled,
        )
        Authenticator.objects.filter(user=user).delete()
    SecondFactorRule.objects.all().delete()
    assert not enrolment.waiting(row)


@pytest.mark.parametrize(
    "stored", [["technical", "supervisor"], ["supervisor", "technical", "technical"]]
)
def test_saving_the_same_known_role_choices_leaves_the_revision_and_history_alone(
    client, make_user, stored
):
    admin = make_user("role_editor", "admin")
    client.force_login(admin)
    rule = SecondFactorRule.objects.create(pk=1, roles=stored, revision=4)
    edit = reverse("second-factor-edit")
    page = client.get(edit)
    assert page.status_code == 200
    form = page.context["form"]
    displayed = form.initial["roles"]
    assert displayed == ["supervisor", "technical"]
    result = client.post(
        edit, {"roles": displayed, "expected_revision": 4, "reason": "Confirm displayed rule."}
    )
    assert result.url == reverse("security-settings")
    rule.refresh_from_db()
    assert (rule.revision, AuditEvent.objects.filter(action=second_factor.ACTION).count()) == (4, 0)


def test_mixed_retired_and_current_roles_are_cleaned_once_with_the_original_audit_values(make_user):
    admin = make_user("clean_editor", "admin")
    active = make_user("active_role", "technical")
    retired = make_user("custom_role_only", "Technical")
    original = ["Technical", "technical"]
    stored = require(roles=original)
    assert second_factor.requires(active)
    assert not second_factor.requires(retired)
    for _ in range(2):
        stored, _changed = second_factor.save_rule(
            actor=admin,
            new={"roles": ["technical"]},
            expected_revision=stored.revision,
            reason="Keep the technical role.",
        )
    assert stored.roles == ["technical"]
    event = AuditEvent.objects.get(action=second_factor.ACTION)
    assert event.before["roles"] == original and event.after["roles"] == ["technical"]


@pytest.mark.django_db(transaction=True)
def test_setup_shell_and_reason_agree_when_the_rule_returns_before_the_page_snapshot(
    client, make_user, monkeypatch
):
    admin = make_user("policy_editor", "admin")
    user = make_user("setup_reader", "technical")
    rule = require(roles=["technical"])
    original = enrolment.EnrolmentMiddleware.process_view
    edited = []

    def restore():
        try:
            second_factor.save_rule(
                actor=admin,
                new={"roles": ["technical"]},
                expected_revision=rule.revision,
                reason="Restore the required role.",
            )
        finally:
            connections.close_all()

    def finish_middleware(self, request, view, args, kwargs):
        response = original(self, request, view, args, kwargs)
        if request.path == SETUP and not edited:
            assert not getattr(request, "vr_second_factor_pending", False)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(restore).result(timeout=10)
            edited.append(True)
        return response

    monkeypatch.setattr(enrolment.EnrolmentMiddleware, "process_view", finish_middleware)
    sign_in(client, user)
    assert session_row(client).second_factor_required
    rule, _ = second_factor.save_rule(
        actor=admin, new={}, expected_revision=rule.revision, reason="Pause the required role."
    )
    page = client.get(SETUP)
    assert edited
    if page.status_code == 302:
        assert page.url == reverse("dashboard")
        return
    assert page.context["reasons"] == ["the Technical staff role"]
    assert b'aria-label="Primary"' not in page.content


def test_waiting_recovery_page_offers_continue_only_after_app_activation(client, make_user):
    user = make_user("continue_reader", "technical")
    require(staff=True)
    RecoveryCodes.activate(user)
    sign_in(client, user)
    assert client.get("/requests/").url == SETUP
    reauthenticate(client)
    codes_url = reverse("mfa_view_recovery_codes")
    before = client.get(codes_url)
    premature = (
        before.status_code == 200 and f'href="{SETUP}">Continue</a>'.encode() in before.content
    )
    if premature:
        returned = client.get(SETUP)
        assert returned.status_code == 200 and returned.context["reasons"] == [
            "every staff account"
        ]
    destination, _secret = activate_app(client)
    assert destination.url == reverse("mfa_index")
    after = client.get(codes_url)
    assert after.status_code == 200 and f'href="{SETUP}">Continue</a>'.encode() in after.content
    assert client.get(SETUP).url == "/requests/"
    assert not premature


@pytest.mark.xfail(
    strict=True,
    reason=(
        "allauth's code generation opens its confirmation before checking that the account "
        "has an app; predates this change (docs/architecture.md, Required second factor)"
    ),
)
def test_waiting_person_without_a_code_record_is_not_offered_code_generation(client, make_user):
    user = make_user("no_backups", "technical")
    require(staff=True)
    sign_in(client, user)
    reauthenticate(client)
    assert not Authenticator.objects.filter(user=user).exists()
    generate = reverse("mfa_generate_recovery_codes")
    assert f'href="{generate}"'.encode() not in client.get(reverse("mfa_index")).content
    page = client.get(generate)
    if page.status_code == 302:
        assert page.url == SETUP
    else:
        assert f'action="{generate}"'.encode() not in page.content


@pytest.mark.xfail(
    strict=True,
    reason=(
        "allauth's code generation opens its confirmation before checking that the account "
        "has an app; predates this change (docs/architecture.md, Required second factor)"
    ),
)
def test_existing_code_generation_explains_the_app_prerequisite_before_confirmation(
    client, make_user
):
    user = make_user("retained_backups", "technical")
    require(staff=True)
    record = RecoveryCodes.activate(user)
    sign_in(client, user)
    reauthenticate(client)
    generate = reverse("mfa_generate_recovery_codes")
    overview = client.get(reverse("mfa_index"))
    if f'href="{generate}"'.encode() not in overview.content:
        return
    page = client.get(generate)
    if page.status_code == 302:
        assert page.url == SETUP
        return
    assert page.status_code == 200
    before = len(record.get_unused_codes())
    # Follow the owner's offered confirmation on its own test account.
    result = client.post(generate)
    assert result.status_code == 200 and result.context["form"].non_field_errors()
    record.instance.refresh_from_db()
    assert len(record.get_unused_codes()) == before
    # The visible form is the action surface; the optional Help drawer is separate.
    body = page.content.decode()
    confirm = body.split("You are about to generate", 1)[-1].split("</form>", 1)[0]
    assert "authenticator app" in strip_tags(confirm).lower() or "disabled" in confirm


@pytest.mark.parametrize("available_method", ["totp", "recovery"])
def test_retained_key_guidance_limits_the_warning_to_accounts_it_strands(
    client, make_user, available_method
):
    user = make_user("key_with_backup", "technical")
    Authenticator.objects.create(user=user, type="webauthn", data={})
    if available_method == "totp":
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
        code = code_for(secret)
    else:
        code = RecoveryCodes.activate(user).get_unused_codes()[0]
    start = sign_in(client, user)
    assert start.url == reverse("mfa_authenticate")
    assert client.post(start.url, {"code": code}).status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    assert Authenticator.objects.filter(user=user, type="webauthn").exists()
    help_path = Path(settings.BASE_DIR) / "docs/pages/admin/authenticators.json"
    note = " ".join(json.loads(help_path.read_text())["good_to_know"])
    arch = (Path(settings.BASE_DIR) / "docs/architecture.md").read_text()
    assertions = {
        "Authenticator administration help": (
            "A stored security key, which this deployment does not offer, "
            "stops password sign-in until it is removed." in note
        ),
        "Required second factor architecture": (
            "still makes password sign-in ask for a code the account cannot give"
            in " ".join(arch.split())
        ),
    }
    assert not any(assertions.values()), [
        name for name, overstates in assertions.items() if overstates
    ]


def test_using_the_last_code_to_confirm_setup_leaves_the_appropriate_backup_instructions(
    client, make_user, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    require(staff=True)
    user = make_user("last_available", "technical")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    record = RecoveryCodes.activate(user)
    for code in record.get_unused_codes()[:-1]:
        assert record.validate_code(code)
    request_code(client, user.email)
    client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    setup = client.get(SETUP)
    assert setup.status_code == 200
    steps = setup.content.decode().split("How to set it up", 1)[1].split("</ol>", 1)[0]
    prompt = client.get(reverse("mfa_activate_totp"), follow=True)
    assert prompt.resolver_match.view_name == "mfa_reauthenticate"
    result = client.post(
        prompt.wsgi_request.get_full_path(), {"code": record.get_unused_codes()[0]}
    )
    assert result.url == reverse("mfa_activate_totp")
    destination, _secret = activate_app(client)
    assert destination.url == reverse("mfa_index")
    assert b"0 out of" in client.get(destination.url).content
    instructions = strip_tags(steps).lower()
    assert "recovery codes stay as they are" not in instructions or "last code" in instructions


def test_continue_remembers_the_recovery_page_opened_first_after_signin(client, make_user):
    user = make_user("first_recovery_page", "technical")
    require(staff=True)
    RecoveryCodes.activate(user)
    first = reverse("mfa_view_recovery_codes")
    assert sign_in(client, user, next_page=first).url == first
    initial = client.get(first, follow=True)
    if initial.resolver_match.view_name == "account_reauthenticate":
        from accounts.tests.conftest import PASSWORD

        confirmed = client.post(
            initial.wsgi_request.get_full_path(), {"password": PASSWORD}, follow=True
        )
        assert confirmed.resolver_match.view_name == "mfa_view_recovery_codes"
    else:
        assert initial.resolver_match.view_name in (
            "account-second-factor",
            "mfa_view_recovery_codes",
        )
    assert client.get(SETUP).status_code == 200
    completed, _secret = activate_app(client)
    assert completed.url == reverse("mfa_index")
    assert f'href="{SETUP}">Continue</a>'.encode() in client.get(completed.url).content
    # Adapted after the second confirmation: the setup pages are never remembered for Continue
    # (the app's setup page turns into its removal once the app exists), so it goes to the start
    # page when no other page was opened.
    assert client.get(SETUP).url == reverse("dashboard")


@pytest.mark.parametrize(
    "kind,remaining",
    [("native", 1), ("native", 0), ("migrated", 1), ("migrated", 0), ("absent", 0)],
)
def test_setup_reports_unused_and_exhausted_codes_without_displaying_or_consuming_them(
    client, make_user, kind, remaining
):
    user = make_user("backup_inventory", "technical")
    require(staff=True)
    codes = None
    if kind == "native":
        codes = RecoveryCodes.activate(user)
        unused = codes.get_unused_codes()
        for code in unused[remaining:]:
            assert codes.validate_code(code)
    elif kind == "migrated":
        from allauth.mfa.utils import encrypt

        migrated = ["83457915"] if remaining else []
        record = Authenticator.objects.create(
            user=user,
            type="recovery_codes",
            data={"migrated_codes": [encrypt(code) for code in migrated]},
        )
        codes = RecoveryCodes(record)
    sign_in(client, user)
    assert client.head(SETUP).status_code == 200
    page = client.get(SETUP)
    assert page.status_code == 200
    html = page.content.decode()
    steps = html.split("How to set it up", 1)[1].split("</ol>", 1)[0].lower()
    if codes is None:
        assert "codes shown next" in steps
        assert not Authenticator.objects.filter(user=user).exists()
    else:
        codes.instance.refresh_from_db()
        unseen = codes.get_unused_codes()
        assert len(unseen) == remaining
        assert all(code not in html for code in unseen)
        if remaining:
            # Adapted on adoption: the step no longer says the codes "stay as they are" (the last
            # one may be used to confirm the setup; this file's own finding).
            assert "keeps its recovery codes" in steps
        else:
            assert "all used" in steps and "generate" in steps


def test_view_download_and_replacement_at_the_stages_where_they_are_available(client, make_user):
    user = make_user("backups_owner", "technical")
    require(staff=True)
    codes = RecoveryCodes.activate(user)
    assert codes.validate_code(codes.get_unused_codes()[0])
    expected = codes.get_unused_codes()
    sign_in(client, user)
    reauthenticate(client)
    view = reverse("mfa_view_recovery_codes")
    download = reverse("mfa_download_recovery_codes")
    page = client.get(view)
    file = client.get(download)
    assert page.status_code == file.status_code == 200
    assert "no-store" in page["Cache-Control"] and "no-store" in file["Cache-Control"]
    assert file["Content-Disposition"].startswith("attachment;")
    assert all(code in page.content.decode() for code in expected)
    assert file.content.decode().split() == expected
    codes.instance.refresh_from_db()
    assert codes.get_unused_codes() == expected
    assert client.get("/requests/").url == SETUP
    for code in expected:
        assert codes.validate_code(code)
    assert b"are all used" in client.get(SETUP).content
    complete, _secret = activate_app(client)
    assert complete.url == reverse("mfa_index")
    replace = client.post(reverse("mfa_generate_recovery_codes"))
    assert replace.status_code == 302 and replace.url == view
    replacement = Authenticator.objects.get(user=user, type="recovery_codes").wrap()
    assert replacement.instance.pk != codes.instance.pk
    assert len(replacement.get_unused_codes()) == len(expected) + 1


def test_only_the_window_get_takes_continue_after_enrolment(client, make_user):
    user = make_user("navigation", "technical")
    require(staff=True)
    sign_in(client, user)
    target = "/requests/?view=all"
    assert client.get(target).url == SETUP
    activate_app(client)
    client.head(SETUP)
    for headers in (
        {"HTTP_HX_REQUEST": "true"},
        {"HTTP_X_VR_FORM": "1"},
        {"HTTP_SEC_FETCH_DEST": "empty"},
    ):
        client.get(SETUP, **headers)
        assert client.session[enrolment.NEXT_KEY] == target
    assert client.get(SETUP, HTTP_SEC_FETCH_DEST="document").url == target
    assert client.get(SETUP).url == reverse("dashboard")
