"""Second confirmation of the factor-change views, marking guidance and Security settings fact."""

from urllib.parse import urlsplit

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, TOTP, generate_totp_secret
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.db.models.signals import post_delete, post_save
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.html import strip_tags

from accounts.models import User
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_second_factor_enrolment import code_for, require
from core.models import AuditEvent
from jobs.models import Notice, NoticeStatus
from tests.conftest import PASSWORD

pytestmark = pytest.mark.confirmation
ACTIONS = {
    "mfa_activate_totp": (
        "authenticator_added",
        "Authenticator app activated.",
        "mfa_view_recovery_codes",
    ),
    "mfa_deactivate_totp": ("authenticator_removed", "Authenticator app deactivated.", "mfa_index"),
    "mfa_generate_recovery_codes": (
        "recovery_codes_replaced",
        "A new set of recovery codes has been generated.",
        "mfa_view_recovery_codes",
    ),
}


@pytest.fixture(autouse=True)
def environment(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    settings.DEBUG = False
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(params=[False, True], ids=["view-transaction", "request-transaction"])
def request_atomic(monkeypatch, request):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", request.param)


@pytest.fixture
def person(staff):
    return staff("second-confirmation", "technical")


@pytest.fixture
def admin_client(client):
    owner = User.objects.create_superuser("second-confirm-admin", "owner@example.invalid", PASSWORD)
    client.force_login(owner)
    return client


def factors(person):
    return dict(Authenticator.objects.filter(user=person).values_list("type", "pk"))


def factor_form(client, person, route, *, app_secret=None):
    if route != "mfa_activate_totp":
        TOTP.activate(person, app_secret or generate_totp_secret())
        RecoveryCodes.activate(person)
    client.force_login(person)
    assert client.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    assert client.get(reverse(route)).status_code == 200
    if route == "mfa_activate_totp":
        return {"code": code_for(client.session[SECRET_SESSION_KEY])}
    return {}


def expired_submission(client, route, data):
    # Expire a real password confirmation after the form was drawn. Restore the normal
    # timeout before following the prompt, so the new confirmation has its ordinary lifetime.
    with override_settings(ACCOUNT_REAUTHENTICATION_TIMEOUT=0):
        pending = client.post(reverse(route), data)
    assert pending.status_code == 302
    assert urlsplit(pending.url).path == reverse("account_reauthenticate")
    return pending.url


def assert_no_change(person, before):
    assert factors(person) == before
    assert not AuditEvent.objects.filter(
        actor_id=person.pk, action__in=[f"account.{action}" for action, _, _ in ACTIONS.values()]
    ).exists()
    assert not Notice.objects.filter(recipient=person, topic="accounts.security").exists()
    assert not mail.outbox


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ACTIONS)
@pytest.mark.parametrize("resume", [False, True], ids=["current-confirmation", "confirm-at-submit"])
def test_factor_change_commits_once_with_its_notice_and_message(
    client, person, route, resume, request_atomic
):
    data = factor_form(client, person, route)
    before = factors(person)
    if resume:
        prompt = expired_submission(client, route, data)
        assert_no_change(person, before)
        assert client.get(prompt).status_code == 200
        response = client.post(prompt, {"password": PASSWORD})
    else:
        response = client.post(reverse(route), data)
    action, message, destination = ACTIONS[route]
    assert response.status_code == 302 and response.url == reverse(destination)
    after = factors(person)
    if route == "mfa_activate_totp":
        assert not before and set(after) == {"totp", "recovery_codes"}
    elif route == "mfa_deactivate_totp":
        assert set(before) == {"totp", "recovery_codes"} and not after
    else:
        assert (
            after["totp"] == before["totp"] and after["recovery_codes"] != before["recovery_codes"]
        )
    assert AuditEvent.objects.filter(actor=person, action=f"account.{action}").count() == 1
    notice = Notice.objects.get(recipient=person, topic="accounts.security")
    assert notice.events[0] == action and notice.status == NoticeStatus.SENT
    assert [m.to for m in mail.outbox] == [[person.email]]
    page = client.get(response.url)
    assert page.status_code == 200 and strip_tags(page.content.decode()).count(message) == 1
    assert message not in strip_tags(client.get(reverse("mfa_index")).content.decode())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ACTIONS)
def test_invalid_or_stale_submission_keeps_factors_and_creates_no_notice(
    client, person, route, request_atomic
):
    data = factor_form(client, person, route)
    if route == "mfa_activate_totp":
        data = {"code": ""}
    else:
        # Another tab already removed the app after the form opened.
        Authenticator.objects.filter(user=person).delete()
    before = factors(person)
    response = client.post(reverse(route), data)
    if route == "mfa_deactivate_totp":
        assert response.status_code == 404
    else:
        assert response.status_code == 200 and response.context["form"].errors
    assert_no_change(person, before)
    page = client.get(reverse("mfa_index"))
    assert ACTIONS[route][1] not in strip_tags(response.content.decode() + page.content.decode())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("route", "confirmation_method"),
    [(route, method) for route in ACTIONS for method in ("direct", "password")]
    + [("mfa_deactivate_totp", "authenticator")],
)
def test_commit_failure_leaves_factors_and_suppresses_success(
    client, person, route, confirmation_method, request_atomic, request
):
    atomic = connection.settings_dict.get("ATOMIC_REQUESTS")
    if atomic and route == "mfa_deactivate_totp" and confirmation_method != "direct":
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason="Accepted limit (accounts/mfa_views.py): with ATOMIC_REQUESTS, which this "
                "release does not set, allauth's re-authentication page resumes the removal inside "
                "its own request's transaction, whose commit comes after the removal view returns.",
            )
        )
    app_secret = generate_totp_secret() if confirmation_method == "authenticator" else None
    data = factor_form(client, person, route, app_secret=app_secret)
    before = factors(person)
    submit_url = reverse(route)
    if confirmation_method != "direct":
        submit_url = expired_submission(client, route, data)
        data = {"password": PASSWORD}
    if confirmation_method == "authenticator":
        prompt = client.get(submit_url)
        alternatives = prompt.context["reauthentication_alternatives"]
        submit_url = next(
            item["url"] for item in alternatives if item["id"] == "mfa_reauthenticate"
        )
        data = {"code": code_for(app_secret)}
    assert_no_change(person, before)
    # Fail only when PostgreSQL checks the deferred constraint at commit. Using the
    # authenticator signal also permits the same test on the original allauth route.
    removing = route == "mfa_deactivate_totp"
    signal = post_delete if removing else post_save
    target = "totp" if removing or route == "mfa_activate_totp" else "recovery_codes"
    faults = []
    assert not Group.objects.filter(pk=-1).exists()

    def deferred_failure(sender, instance, using, **kwargs):
        if instance.type == target and (removing or kwargs["created"]):
            User.groups.through.objects.using(using).create(user_id=person.pk, group_id=-1)
            faults.append(instance.type)

    client.raise_request_exception = False
    signal.connect(deferred_failure, sender=Authenticator)
    try:
        result = client.post(submit_url, data)
    finally:
        signal.disconnect(deferred_failure, sender=Authenticator)
    assert faults == [target] and result.status_code == 500
    assert_no_change(person, before)
    assert not User.groups.through.objects.filter(user_id=person.pk, group_id=-1).exists()
    next_page = client.get(reverse("mfa_index"))
    assert next_page.status_code == 200
    banner = ACTIONS[route][1]
    shown_on = [
        name
        for name, page in (("error response", result), ("following overview", next_page))
        if banner in strip_tags(page.content.decode())
    ]
    assert not shown_on, f"{banner} appeared on {shown_on}"


def mark_form(browser, person):
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = browser.get(url)
    assert page.status_code == 200
    fields = {
        "drawn": page.context["form"].initial["drawn"],
        "reason": "Synthetic exercise.",
        "confirm_password": PASSWORD,
    }
    content = page.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    return url, fields, strip_tags(content), page.context["page_documentation"]


def completion(browser, url, fields):
    result = browser.post(url, fields)
    assert result.status_code == 302
    messages = [str(m) for m in browser.get(result.url).context["messages"]]
    return next(message for message in messages if "as compromised" in message)


@pytest.mark.django_db
@pytest.mark.parametrize("becomes_active", [True, False], ids=["reactivated", "deactivated"])
@pytest.mark.parametrize("address", ["", "synthetic@example.invalid"], ids=["addressless", "email"])
def test_mark_completion_uses_eligibility_as_of_submission(
    admin_client, person, becomes_active, address
):
    person.is_active = not becomes_active
    person.email = "" if address else "former@example.invalid"
    person.save(update_fields=["email", "is_active"])
    url, fields, preview, _ = mark_form(admin_client, person)
    assert ("cannot sign in until" in preview) == (not person.is_active)
    assert ("Forgot your password?" in preview) == (person.is_active and bool(person.email))
    person.is_active = becomes_active
    person.email = address
    person.save(update_fields=["email", "is_active"])
    advice = completion(admin_client, url, fields)
    assert ("cannot sign in until" in advice) == (not becomes_active)
    assert ("Forgot your password?" in advice) == (becomes_active and bool(address))
    if becomes_active and not address:
        assert "account's password page" in advice


@pytest.mark.django_db
@pytest.mark.parametrize("source", ["preview", "completion", "help"])
def test_reactivation_advice_accounts_for_required_enrolment_before_password_change(
    admin_client, person, source
):
    require(roles=["technical"])
    person.is_active = False
    person.save(update_fields=["is_active"])
    url, fields, preview, doc = mark_form(admin_client, person)
    advice = completion(admin_client, url, fields)
    person.is_active = True
    person.save(update_fields=["is_active"])
    browser = Client()
    assert request_code(browser, person.email).status_code == 302
    assert (
        browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()}).status_code
        == 302
    )
    assert browser.session["_auth_user_id"] == str(person.pk)
    assert browser.get(reverse("account-new-password")).url == reverse("account-second-factor")
    assert browser.get(reverse("account-second-factor")).status_code == 200
    explanations = {
        "preview": [preview],
        "completion": [advice],
        "help": [
            item["description"]
            for section in doc["sections"]
            for item in section.get("items", [])
            if item["label"] in {"What marking does", "Mark as compromised"}
        ],
    }
    assert explanations[source]
    for explanation in explanations[source]:
        # An unqualified password-first instruction contradicts the actual first page.
        if "before anything else" in explanation and "deactivated" in explanation.lower():
            assert any(word in explanation.lower() for word in ("set up", "setting up", "enrol"))


@pytest.mark.django_db
def test_inactive_accounts_quick_help_qualifies_its_signin_instruction(admin_client, person):
    person.is_active = False
    person.save(update_fields=["is_active"])
    TOTP.activate(person, generate_totp_secret())
    _, _, _, doc = mark_form(admin_client, person)
    steps = next(
        task["steps"] for task in doc["tasks"] if task["title"] == "Tell the person what to do"
    )
    browser = Client()
    response = browser.post(
        reverse("account_login"), {"login": person.username, "password": PASSWORD}
    )
    assert response.status_code == 302 and response.url == reverse("account_inactive")
    assert "_auth_user_id" not in browser.session
    help_text = " ".join(steps).lower()
    if "sign in with their password" in help_text:
        assert any(term in help_text for term in ("active", "reactivat", "deactivated"))


@pytest.mark.django_db
def test_settings_fact_reads_the_saved_rule_on_each_read():
    from core.models import RevealConfirmationRule
    from core.security_settings import in_force

    def fact_value():
        return next(
            f.value
            for group in in_force()
            for f in group.facts
            if f.key == "patient-reveal-confirmation"
        )

    assert not RevealConfirmationRule.objects.exists()
    assert fact_value() == "Not asked"
    assert not RevealConfirmationRule.objects.exists()
    rule = RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    assert fact_value() == "Asked once in each session"
    rule.first_reveal = False
    rule.save(update_fields=["first_reveal"])
    assert fact_value() == "Not asked"
