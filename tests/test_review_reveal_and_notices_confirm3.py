"""Third confirmation of the recovery instructions for a deactivated, marked account."""

from pathlib import Path

import pytest
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils.html import strip_tags

from accounts.compromised import open_mark
from accounts.models import User
from accounts.tests.test_compromised_password import NEW_PASSWORD, choose
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_second_factor_enrolment import require
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.confirmation, pytest.mark.django_db]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def marked(client, staff, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    cache.clear()
    owner = User.objects.create_superuser("review3-owner", "owner@example.invalid", PASSWORD)
    client.force_login(owner)
    person = staff("review3-person", "technical")
    person.is_active = False
    person.save(update_fields=["is_active"])
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = client.get(url)
    assert page.status_code == 200
    form_text = page.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    fields = {
        "drawn": page.context["form"].initial["drawn"],
        "confirm_password": PASSWORD,
        "reason": "Synthetic training account.",
    }
    response = client.post(url, fields)
    assert response.status_code == 302 and open_mark(person.pk)
    messages = [str(m) for m in client.get(response.url).context["messages"]]
    completion = next(m for m in messages if "as compromised" in m)
    yield person, strip_tags(form_text), completion, page.context["page_documentation"]
    cache.clear()


@pytest.mark.parametrize("state", ["enrolment-required", "enrolment-optional", "already-enrolled"])
def test_self_service_after_reactivation_follows_the_documented_order(marked, state):
    person, preview, completion, help_ = marked
    require(roles=["technical"] if state != "enrolment-optional" else [])
    if state == "already-enrolled":
        TOTP.activate(person, generate_totp_secret())
        recovery = RecoveryCodes.activate(person).get_unused_codes()[0]
    for message in (preview.lower(), completion.lower()):
        assert "cannot sign in until" in message and "active again" in message
        assert "new password" in message and "before anything else" not in message
    steps = next(
        task["steps"] for task in help_["tasks"] if task["title"] == "Tell the person what to do"
    )
    assert "active again" in steps[0] and "sign in" in steps[1]
    detail = next(
        item["description"]
        for section in help_["sections"]
        for item in section.get("items", [])
        if item["label"] == "What marking does"
    )
    product = " ".join((ROOT / "docs/product.md").read_text().split())
    for explanation in (
        detail,
        product.split("A deactivated account that is marked", 1)[1].split("Any new password", 1)[0],
    ):
        assert "setting up a second factor first where the laboratory requires one" in explanation
    browser = Client()
    person.is_active = True
    person.save(update_fields=["is_active"])
    assert request_code(browser, person.email).url == reverse("account_confirm_login_code")
    signed = browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    if state == "already-enrolled":
        assert signed.url == reverse("mfa_authenticate")
        assert browser.post(signed.url, {"code": recovery}).status_code == 302
    assert browser.session["_auth_user_id"] == str(person.pk)
    password_page = reverse("account-new-password")
    if state == "enrolment-required":
        assert browser.get(password_page).url == reverse("account-second-factor")
        assert browser.get(reverse("account-second-factor")).status_code == 200
        TOTP.activate(person, generate_totp_secret())
    assert browser.get(password_page).status_code == 200
    assert choose(browser, person, NEW_PASSWORD).status_code == 302
    assert open_mark(person.pk) is None
    assert browser.get("/").status_code == 200


def test_admin_can_replace_a_marked_password_while_the_account_stays_deactivated(client, marked):
    person, _, _, help_ = marked
    task = next(task for task in help_["tasks"] if task["title"] == "Tell the person what to do")
    assert any("set a new password" in step and "password page" in step for step in task["steps"])
    url = reverse("admin:auth_user_password_change", args=[person.pk])
    assert client.get(url).status_code == 200
    saved = client.post(
        url,
        {
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
            "usable_password": "true",
            "confirm_password": PASSWORD,
        },
    )
    assert saved.status_code == 302
    person.refresh_from_db()
    assert not person.is_active and person.check_password(NEW_PASSWORD)
    assert open_mark(person.pk) is None
    blanket_instruction = "make it active again first, then follow the step for it below"
    assert not any(blanket_instruction in step.lower() for step in task["steps"])


@pytest.mark.parametrize("request_wide", [False, True], ids=["control", "accepted-combination"])
def test_an_unrelated_setup_error_is_reported_in_the_resumed_removal_case(tmp_path, request_wide):
    """Check pytest's classification without running or changing the accepted rollback scenario."""
    import os
    import subprocess
    import sys
    from textwrap import dedent

    probe = tmp_path / "test_marker_scope.py"
    probe.write_text(
        dedent("""
        import os
        from inspect import signature
        from tests import test_review_reveal_and_notices_confirm2 as suite

        def test_form_unavailable(request, monkeypatch):
            def unavailable(*args, **kwargs):
                print("Synthetic form could not open", flush=True)
                raise AssertionError("Synthetic form could not open")

            atomic = os.environ["SYNTHETIC_ATOMIC"] == "1"
            monkeypatch.setitem(suite.connection.settings_dict, "ATOMIC_REQUESTS", atomic)
            monkeypatch.setattr(suite, "factor_form", unavailable)
            case = suite.test_commit_failure_leaves_factors_and_suppresses_success
            arguments = dict(client=None, person=None, route="mfa_deactivate_totp",
                             confirmation_method="password", request_atomic=atomic, request=request)
            case(**{key: arguments[key] for key in signature(case).parameters})
        """)
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-s",
            "-rxf",
            "--tb=short",
            str(probe),
        ],
        cwd=ROOT,
        env={**os.environ, "SYNTHETIC_ATOMIC": str(int(request_wide))},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "Synthetic form could not open" in run.stdout, run.stdout + run.stderr
    assert run.returncode == 1 and "1 failed" in run.stdout, run.stdout + run.stderr
