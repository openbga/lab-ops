"""Confirmation round for sign-in bookkeeping, factor-change messages and operator guidance."""

import re

import pytest
from allauth.account.internal.flows.login import get_authentication_records
from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, TOTP, generate_totp_secret
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.db.models.signals import post_delete
from django.test import Client
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin
from django.utils.html import strip_tags

from accounts import compromised, confirmation, sessions
from accounts.models import AccountSession, SignInEvent, User
from accounts.tests.test_login import mailed_code, request_code
from accounts.tests.test_second_factor_enrolment import code_for, require
from core.models import AuditEvent
from jobs.models import Notice
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]
MAIL_ON = "django.core.mail.backends.locmem.EmailBackend"
MAIL_OFF = "django.core.mail.backends.dummy.EmailBackend"
REMOVED = "Authenticator app deactivated."
INFO = "The synthetic laboratory schedule has changed."


@pytest.fixture(autouse=True)
def environment(settings):
    settings.EMAIL_BACKEND = MAIL_ON
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(staff):
    return staff("confirm-operator", "technical")


@pytest.fixture
def owner():
    return User.objects.create_superuser("confirm-owner", "owner@example.invalid", PASSWORD)


def primary_signin(browser, user, method):
    if method == "code":
        assert request_code(browser, user.email).url == reverse("account_confirm_login_code")
        return browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    return browser.post(reverse("account_login"), {"login": user.username, "password": PASSWORD})


def session_row(browser):
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def steps(browser, user):
    request = browser.get(reverse("mfa_index")).wsgi_request
    return [
        (r["method"], r.get("type"), bool(r.get("reauthenticated")))
        for r in get_authentication_records(request, user)
    ]


def confirm_password(browser, password=PASSWORD):
    assert (
        browser.post(reverse("account_reauthenticate"), {"password": password}).status_code == 302
    )


@pytest.mark.parametrize("primary", ["password", "code"])
@pytest.mark.parametrize("second", [None, "totp", "recovery_codes"])
def test_completed_signin_keeps_each_primary_step_and_its_history(client, person, primary, second):
    if second:
        secret = generate_totp_secret()
        TOTP.activate(person, secret)
        recovery = RecoveryCodes.activate(person).get_unused_codes()
    response = primary_signin(client, person, primary)
    expected_steps = [(primary, None, False)]
    factor = primary
    if second:
        assert response.url == reverse("mfa_authenticate")
        assert "_auth_user_id" not in client.session
        assert not AccountSession.objects.filter(user=person).exists()
        value = code_for(secret) if second == "totp" else recovery[0]
        response = client.post(response.url, {"code": value})
        expected_steps.append(("mfa", second, False))
        factor += "_totp" if second == "totp" else "_recovery"
    assert response.status_code == 302
    row = session_row(client)
    assert row.user_id == person.pk and row.factor == factor
    assert SignInEvent.objects.get(session=row.pk, kind=SignInEvent.Kind.SIGNED_IN).factor == factor
    assert steps(client, person) == expected_steps
    req = client.get(reverse("mfa_index")).wsgi_request
    assert not did_recently_reauthenticate(req)
    assert not confirmation.current(req)


def test_same_account_new_session_keeps_primary_steps_and_restarts_both_confirmation_pages(
    client, person
):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    codes = RecoveryCodes.activate(person).get_unused_codes()
    stage = primary_signin(client, person, "password")
    assert stage.url == reverse("mfa_authenticate")
    assert client.post(stage.url, {"code": code_for(secret)}).status_code == 302
    original = steps(client, person)
    assert original == [("password", None, False), ("mfa", "totp", False)]
    first_row = session_row(client)
    confirm_password(client)
    assert client.post(reverse("mfa_reauthenticate"), {"code": codes[0]}).status_code == 302
    req = client.get(reverse("mfa_index")).wsgi_request
    assert confirmation.current(req) and did_recently_reauthenticate(req)

    # Django preserves this browser's session data on a same-account login.
    client.force_login(person)
    assert session_row(client).pk != first_row.pk
    assert steps(client, person) == original
    req = client.get(reverse("mfa_index")).wsgi_request
    assert not confirmation.current(req) and not did_recently_reauthenticate(req)
    prompt = client.get(reverse("mfa_deactivate_totp"), follow=True)
    assert prompt.resolver_match.view_name in {"account_reauthenticate", "mfa_reauthenticate"}
    confirm_password(client)
    assert not confirmation.current(client.get(reverse("mfa_index")).wsgi_request)
    assert client.post(reverse("mfa_reauthenticate"), {"code": codes[1]}).status_code == 302
    assert confirmation.current(client.get(reverse("mfa_index")).wsgi_request)
    assert client.get(reverse("mfa_deactivate_totp")).status_code == 200


@pytest.mark.parametrize(
    ("primary", "second", "held"),
    [("password", False, False), ("password", True, True), ("code", False, True)],
)
def test_marked_signin_records_what_was_proved_and_its_disposition(
    client, person, owner, primary, second, held
):
    if second:
        secret = generate_totp_secret()
        TOTP.activate(person, secret)
    compromised.mark_compromised(
        actor=owner,
        account_id=person.pk,
        reason="Synthetic exercise.",
        drawn=compromised.drawn_password(person),
    )
    response = primary_signin(client, person, primary)
    if second:
        assert response.url == reverse("mfa_authenticate")
        response = client.post(response.url, {"code": code_for(secret)})
    assert response.status_code == 302
    row = session_row(client)
    kind = SignInEvent.Kind.SIGNED_IN if held else SignInEvent.Kind.REFUSED
    assert SignInEvent.objects.get(session=row.pk, kind=kind).factor == row.factor
    assert row.factor == (primary + "_totp" if second else primary)
    assert (row.ended_at is None) == held
    landing = client.get("/")
    if held:
        assert landing.url == "/account/new-password/"
        request = client.get(landing.url).wsgi_request
        assert not confirmation.current(request)
        assert not did_recently_reauthenticate(request)
    else:
        assert row.ending == AccountSession.Ending.MARKED_COMPROMISED
        assert landing.url.startswith(reverse("account_login"))
        assert "_auth_user_id" not in client.session


def test_setup_adds_a_factor_without_reclassifying_the_emailed_signin(client, person):
    require(roles=["technical"])
    assert primary_signin(client, person, "code").status_code == 302
    before = session_row(client)
    assert client.get("/").url == "/account/second-factor/"
    confirm_password(client)
    assert client.get(reverse("mfa_activate_totp")).status_code == 200
    code = code_for(client.session[SECRET_SESSION_KEY])
    added = client.post(reverse("mfa_activate_totp"), {"code": code})
    assert added.url == reverse("mfa_view_recovery_codes")
    assert client.get("/").status_code == 200
    after = session_row(client)
    assert after.pk == before.pk and after.factor == "code"
    assert (
        SignInEvent.objects.get(session=after.pk, kind=SignInEvent.Kind.SIGNED_IN).factor == "code"
    )
    request = client.get(reverse("mfa_index")).wsgi_request
    assert not confirmation.current(request)
    value = (
        Authenticator.objects.get(user=person, type="recovery_codes").wrap().get_unused_codes()[0]
    )
    assert client.post(reverse("mfa_reauthenticate"), {"code": value}).status_code == 302
    assert confirmation.current(client.get(reverse("mfa_index")).wsgi_request)


def test_own_password_change_keeps_signin_and_reveal_but_requires_the_new_password_for_next_save(
    client, person
):
    from core.models import RevealConfirmationRule
    from core.reveal_confirmation import confirmed

    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    primary_signin(client, person, "password")
    before = session_row(client)
    confirm_password(client)
    assert client.get(reverse("account-confirm-reveal")).status_code == 302
    req = client.get("/").wsgi_request
    assert confirmation.current(req) and confirmed(req)
    new = "Synthetic copper orchard canyon 405"
    response = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": new, "password2": new},
    )
    assert response.status_code == 302
    assert session_row(client).pk == before.pk
    req = client.get("/").wsgi_request
    assert req.user.pk == person.pk and confirmed(req)
    assert not confirmation.current(req)
    confirm_password(client, new)
    assert confirmation.current(client.get("/").wsgi_request)


class IndependentMessageMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method == "POST":
            messages.info(request, INFO)


def factor_ids(person):
    return set(Authenticator.objects.filter(user=person).values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("route", "banner"),
    [
        ("mfa_deactivate_totp", REMOVED),
        ("mfa_activate_totp", "Authenticator app activated."),
        ("mfa_generate_recovery_codes", "A new set of recovery codes has been generated."),
    ],
)
@pytest.mark.parametrize("fails", [False, True], ids=["commit", "audit-failure"])
def test_factor_change_messages_agree_with_database_and_keep_unrelated_message(
    client, person, settings, monkeypatch, route, banner, fails
):
    from accounts import mfa_views

    settings.DEBUG = False
    client.force_login(person)
    confirm_password(client)
    if route != "mfa_activate_totp":
        TOTP.activate(person, generate_totp_secret())
        RecoveryCodes.activate(person)
    url = reverse(route)
    assert client.get(url).status_code == 200
    data = (
        {"code": code_for(client.session[SECRET_SESSION_KEY])}
        if route == "mfa_activate_totp"
        else {}
    )
    before = factor_ids(person)
    settings.MIDDLEWARE = [*settings.MIDDLEWARE, f"{__name__}.IndependentMessageMiddleware"]
    # A fresh test client loads the configured middleware, using the same signed-in browser cookies.
    browser = Client(raise_request_exception=False)
    browser.cookies = client.cookies
    if fails:

        def unavailable(**kwargs):
            raise RuntimeError("Synthetic audit write failure")

        monkeypatch.setattr(mfa_views, "record_event", unavailable)
    response = browser.post(url, data)
    assert response.status_code == (500 if fails else 302)
    assert (factor_ids(person) == before) == fails
    assert Notice.objects.filter(topic="accounts.security", recipient=person).count() == (
        0 if fails else 1
    )
    next_page = browser.get(reverse("mfa_index"))
    assert next_page.status_code == 200
    shown = strip_tags(response.content.decode()) + strip_tags(next_page.content.decode())
    assert shown.count(INFO) == 1
    assert shown.count(banner) == (0 if fails else 1)
    following = browser.get(reverse("mfa_index"))
    assert INFO not in following.content.decode() and banner not in following.content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "request_atomic", [False, True], ids=["view-transaction", "request-transaction"]
)
def test_database_commit_failure_does_not_announce_removal(
    client, person, settings, monkeypatch, request_atomic
):
    """PostgreSQL checks the synthetic, deferred foreign key at the actual outermost commit."""
    settings.DEBUG = False
    TOTP.activate(person, generate_totp_secret())
    RecoveryCodes.activate(person)
    client.force_login(person)
    confirm_password(client)
    before = factor_ids(person)
    assert not Group.objects.filter(pk=-1).exists()

    def fail_at_commit(sender, instance, using, **kwargs):
        if instance.type == Authenticator.Type.TOTP:
            User.groups.through.objects.using(using).create(user_id=person.pk, group_id=-1)

    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", request_atomic)
    client.raise_request_exception = False
    post_delete.connect(fail_at_commit, sender=Authenticator)
    try:
        response = client.post(reverse("mfa_deactivate_totp"))
    finally:
        post_delete.disconnect(fail_at_commit, sender=Authenticator)
    assert response.status_code == 500
    assert factor_ids(person) == before
    assert not User.groups.through.objects.filter(user_id=person.pk, group_id=-1).exists()
    assert not AuditEvent.objects.filter(
        actor=person, action="account.authenticator_removed"
    ).exists()
    assert not Notice.objects.filter(recipient=person, topic="accounts.security").exists()
    assert not mail.outbox
    page = client.get(reverse("mfa_index"))
    assert page.status_code == 200
    assert REMOVED not in strip_tags(response.content.decode() + page.content.decode())


def mark_page(browser, person):
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = browser.get(url)
    assert page.status_code == 200
    fields = {
        "drawn": page.context["form"].initial["drawn"],
        "reason": "Synthetic review.",
        "confirm_password": PASSWORD,
    }
    form = page.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    return url, fields, strip_tags(form)


@pytest.mark.parametrize("phase", ["preview", "completion"])
@pytest.mark.parametrize(
    ("backend", "address", "codes"),
    [
        (MAIL_ON, "", True),
        (MAIL_ON, "", False),
        (MAIL_ON, "recipient@example.invalid", True),
        (MAIL_ON, "recipient@example.invalid", False),
        (MAIL_OFF, "recipient@example.invalid", True),
    ],
)
def test_marking_guidance_offers_only_available_recovery_methods(
    client, person, owner, settings, phase, backend, address, codes
):
    settings.EMAIL_BACKEND = backend
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = codes
    person.email = address
    person.save(update_fields=["email"])
    client.force_login(owner)
    url, data, shown = mark_page(client, person)
    if phase == "completion":
        response = client.post(url, data)
        assert response.status_code == 302
        shown = " ".join(str(m) for m in client.get(response.url).context["messages"])
        assert "compromised" in shown
    assert "account's password page" in shown
    can_email = backend == MAIL_ON and bool(address)
    offered = ("Forgot your password?" in shown, "code emailed to them" in shown)
    assert offered == (can_email, can_email and codes)


@pytest.mark.parametrize("second", [False, True], ids=["email-recovery", "app-signin"])
def test_completion_names_the_inactive_accounts_signin_limit(client, owner, person, second):
    person.is_active = False
    person.save(update_fields=["is_active"])
    if second:
        TOTP.activate(person, generate_totp_secret())
    client.force_login(owner)
    url, fields, _ = mark_page(client, person)
    result = client.post(url, fields)
    assert result.status_code == 302
    advice = " ".join(str(m) for m in client.get(result.url).context["messages"])
    assert "compromised" in advice
    # Follow the suggested route as the account holder, on a separate browser.
    browser = Client()
    if second:
        response = primary_signin(browser, person, "password")
        assert (
            response.url != reverse("mfa_authenticate") and "_auth_user_id" not in browser.session
        )
    else:
        response = browser.post(reverse("account_reset_password"), {"email": person.email})
        assert response.url == reverse("account_reset_password_done")
        assert all("/password/reset/key/" not in m.body for m in mail.outbox)
    if "Tell the person to sign in" in advice or "Forgot your password?" in advice:
        assert any(word in advice.lower() for word in ("inactive", "deactivated", "reactivat"))


@pytest.mark.parametrize(
    "route",
    [
        "admin:accounts_user_compromised",
        "admin:mfa_authenticator_delete",
        "admin:auth_user_password_change",
        "admin:accounts_user_change",
        "admin:auth_group_change",
        "second-factor-edit",
        "session-limits-edit",
        "reveal-confirmation-edit",
    ],
)
def test_quick_help_matches_the_confirmation_fields_of_a_code_only_administrator(
    client, owner, person, route
):
    owner.set_unusable_password()
    owner.save(update_fields=["password"])
    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    TOTP.activate(owner, generate_totp_secret())
    client.force_login(owner)
    args = []
    if route == "admin:mfa_authenticator_delete":
        args = [TOTP.activate(person, generate_totp_secret()).instance.pk]
    elif route == "admin:auth_group_change":
        args = [person.groups.get().pk]
    elif route.startswith("admin:"):
        args = [person.pk]
    elif route == "session-limits-edit":
        args = ["staff"]
    page = client.get(reverse(route, args=args))
    assert page.status_code == 200
    html = page.content.decode()
    assert 'name="confirm_code"' in html and 'name="confirm_password"' not in html
    doc = page.context["page_documentation"]
    lines = [*doc["good_to_know"], *(line for task in doc["tasks"] for line in task["steps"])]
    # Reject instructions that require the absent field. Conditional descriptions of it are valid.
    unconditional = re.compile(
        r"(?:type (?:your own password in )?your password, and a code|"
        r"asks for your (?:own )?password, and a code if you have)"
    )
    contradicted = [
        line
        for line in lines
        if unconditional.search(line.lower().replace("[[", "").replace("]]", ""))
    ]
    assert not contradicted, contradicted


@pytest.mark.parametrize("address_added", [True, False], ids=["address-added", "still-addressless"])
def test_notice_preview_and_outbox_when_only_some_eligibility_conditions_change(
    client, owner, person, address_added
):
    from jobs import outbox
    from jobs.models import NoticeStatus, NotSentReason

    person.email = ""
    person.is_active = False
    person.save(update_fields=["email", "is_active"])
    client.force_login(owner)
    url, data, shown = mark_page(client, person)
    assert "at present" in shown and "only if that changes" in shown
    assert client.post(url, data).status_code == 302
    notice = Notice.objects.get(topic="accounts.security", recipient=person)
    assert notice.status == NoticeStatus.WAITING
    person.is_active = True
    if address_added:
        person.email = "confirm-delivery@example.invalid"
    person.save(update_fields=["email", "is_active"])
    outcome = outbox.attempt(notice.pk)
    notice.refresh_from_db()
    if address_added:
        assert outcome == "sent" and notice.status == NoticeStatus.SENT
        assert [m.to for m in mail.outbox] == [[person.email]]
    else:
        assert outcome == "not_sent" and notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
        assert not mail.outbox


@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit (accounts/mfa_views.py DeactivateTOTPView): a removal that fails puts "
    "the message queue back as it was before the removal, so a message another receiver of "
    "allauth's removal signal queued during it goes too. Nothing in this application does.",
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "request_atomic", [False, True], ids=["view-transaction", "request-transaction"]
)
def test_unrelated_message_queued_during_removal_survives_a_later_error(
    client, person, settings, monkeypatch, request_atomic
):
    """An integration queues independent information while allauth is processing this request."""
    from allauth.mfa.signals import authenticator_removed

    settings.DEBUG = False
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", request_atomic)
    TOTP.activate(person, generate_totp_secret())
    RecoveryCodes.activate(person)
    client.force_login(person)
    confirm_password(client)
    before = factor_ids(person)
    account_message = f"You are signed in as {person.username}."
    emitted = []

    def announce_account(sender, request, user, authenticator, **kwargs):
        if authenticator.type == Authenticator.Type.TOTP:
            messages.info(request, account_message)
            emitted.append(user.pk)

    def unavailable(sender, authenticator, **kwargs):
        if authenticator.type == Authenticator.Type.TOTP:
            raise RuntimeError("Synthetic failure in a second signal receiver")

    client.raise_request_exception = False
    authenticator_removed.connect(announce_account)
    authenticator_removed.connect(unavailable)
    try:
        response = client.post(reverse("mfa_deactivate_totp"))
    finally:
        authenticator_removed.disconnect(announce_account)
        authenticator_removed.disconnect(unavailable)
    assert response.status_code == 500
    assert emitted == [person.pk]
    assert factor_ids(person) == before
    assert not AuditEvent.objects.filter(
        actor=person, action="account.authenticator_removed"
    ).exists()
    assert not Notice.objects.filter(recipient=person, topic="accounts.security").exists()
    page = client.get(reverse("mfa_index"))
    assert page.status_code == 200 and page.wsgi_request.user.pk == person.pk
    shown = strip_tags(response.content.decode() + page.content.decode())
    assert REMOVED not in shown
    assert account_message in shown
