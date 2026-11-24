"""Verification round for reveal confirmation and factor notices. All accounts are synthetic."""

import time
from types import SimpleNamespace

import pytest
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils.html import strip_tags

from accounts import compromised, confirmation, sessions
from accounts.models import AccountSession, SignInEvent, User
from core.models import AuditEvent
from jobs import outbox
from jobs.models import Notice, NoticeStatus, NotSentReason
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]

REVEAL_MARK = "_reveal_confirmed"
REVEAL_EVENTS = ("request_form.value_revealed", "external_link.person_revealed")
MAIL_ON = "django.core.mail.backends.locmem.EmailBackend"
MAIL_OFF = "django.core.mail.backends.dummy.EmailBackend"


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def rule():
    from core.models import RevealConfirmationRule

    return RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)


@pytest.fixture
def operator(staff):
    return staff("verify-operator", "technical")


@pytest.fixture
def admin_browser():
    owner = User.objects.create_superuser("verify-owner", "owner@example.invalid", PASSWORD)
    browser = Client()
    browser.force_login(owner)
    return browser


def confirm_url():
    return reverse("account-confirm-reveal")


def sign_in(browser, user):
    return browser.post(reverse("account_login"), {"login": user.username, "password": PASSWORD})


def confirm_password(browser):
    response = browser.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert response.status_code == 302


def reveal_history():
    return AuditEvent.objects.filter(action__in=REVEAL_EVENTS)


def current_row(browser):
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def age_confirmation_clock(monkeypatch):
    later = time.time() + confirmation.window() + 1
    monkeypatch.setattr(confirmation, "time", SimpleNamespace(time=lambda: later))


@pytest.mark.parametrize("recent", [True, False], ids=["recent-proof", "expired-proof"])
def test_upgrade_of_an_existing_scalar_mark_uses_the_independent_confirmation(
    client, operator, rule, recent, monkeypatch
):
    client.force_login(operator)
    confirm_password(client)
    if not recent:
        age_confirmation_clock(monkeypatch)
    session = client.session
    session[REVEAL_MARK] = operator.pk  # the scalar written by the preceding feature revision
    session.save()

    page = client.get(confirm_url(), {"next": reverse("account-sign-ins")})
    if recent:
        assert page.status_code == 302 and page.url == reverse("account-sign-ins")
    else:
        assert page.status_code == 200 and "confirm_password" in page.context["form"].fields
        saved = client.post(confirm_url(), {"confirm_password": PASSWORD})
        assert saved.status_code == 302
    assert client.session[REVEAL_MARK] == [operator.pk, str(current_row(client).pk)]
    assert not reveal_history().exists()


@pytest.mark.parametrize("method", ["get", "head", "post"])
@pytest.mark.parametrize("enabled", [True, False], ids=["rule-on", "rule-off"])
def test_current_confirmation_on_visit_returns_without_recording_a_reveal(
    client, operator, rule, method, enabled
):
    # The page is available to ordinary staff without the patient-information grant, too.
    assert not operator.user_permissions.exists()
    rule.first_reveal = enabled
    rule.save(update_fields=["first_reveal"])
    client.force_login(operator)
    confirm_password(client)
    back = reverse("account-sign-ins") + "?tab=recent"
    page = getattr(client, method)(confirm_url(), {"next": back})
    assert page.status_code == 302 and page.url == back
    assert "no-store" in page["Cache-Control"]
    if enabled:
        assert client.session[REVEAL_MARK] == [operator.pk, str(current_row(client).pk)]
    else:
        assert REVEAL_MARK not in client.session
    assert not reveal_history().exists()
    assert not SignInEvent.objects.filter(
        user=operator, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()


def test_pending_form_rechecks_a_confirmation_that_expired_before_submission(
    client, operator, rule, monkeypatch
):
    client.force_login(operator)
    back = reverse("account-sign-ins")
    page = client.get(confirm_url(), {"next": back})
    assert page.status_code == 200 and "confirm_password" in page.context["form"].fields
    confirm_password(client)  # another tab in this browser
    age_confirmation_clock(monkeypatch)
    waiting = client.post(confirm_url(), {"next": back})
    assert waiting.status_code == 200 and waiting.context["form"].has_error(
        "confirm_password", "required"
    )
    assert REVEAL_MARK not in client.session
    accepted = client.post(confirm_url(), {"next": back, "confirm_password": PASSWORD})
    assert accepted.status_code == 302 and accepted.url == back
    assert client.session[REVEAL_MARK] == [operator.pk, str(current_row(client).pk)]
    assert not reveal_history().exists()


def test_a_new_signin_does_not_report_a_confirmation_from_an_earlier_account_session(
    client, operator
):
    """Check short-lived confirmation provenance at Django's sign-in hook, without a reveal."""
    client.force_login(operator)
    confirm_password(client)
    old = current_row(client)
    assert confirmation.current(SimpleNamespace(user=operator, session=client.session))
    client.force_login(operator)
    new = current_row(client)
    old.refresh_from_db()
    assert old.ended_at is not None and old.ending == AccountSession.Ending.SIGNED_IN_AGAIN
    assert new.pk != old.pk and new.ended_at is None
    assert REVEAL_MARK not in client.session
    assert not confirmation.current(SimpleNamespace(user=operator, session=client.session))


def test_logout_and_public_signin_of_same_account_start_unconfirmed(client, operator, rule):
    assert sign_in(client, operator).status_code == 302
    old = current_row(client)
    assert client.post(confirm_url(), {"confirm_password": PASSWORD}).status_code == 302
    assert REVEAL_MARK in client.session
    assert client.post(reverse("account_logout")).status_code == 302
    assert sign_in(client, operator).status_code == 302
    new = current_row(client)
    assert new.pk != old.pk and REVEAL_MARK not in client.session
    prompt = client.get(confirm_url())
    assert prompt.status_code == 200 and "confirm_password" in prompt.context["form"].fields
    assert not reveal_history().exists()


@pytest.mark.parametrize("reason", ["required-app", "marked-password", "marked-with-app"])
def test_new_signin_row_gets_its_required_next_page_and_discards_old_metadata(
    client, operator, rule, reason
):
    from core.models import SecondFactorRule

    client.force_login(operator)
    old = current_row(client)
    assert client.post(confirm_url(), {"confirm_password": PASSWORD}).status_code == 302
    assert REVEAL_MARK in client.session
    if reason == "required-app":
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
        # Django's session-start hook receives the same browser state the previous sign-in used.
        client.force_login(operator)
        expected_page = reverse("account-second-factor")
    else:
        owner = User.objects.create_superuser("verify-marker", "marker@example.invalid", PASSWORD)
        if reason == "marked-with-app":
            TOTP.activate(operator, generate_totp_secret())
            codes = RecoveryCodes.activate(operator).get_unused_codes()
        compromised.mark_compromised(
            actor=owner,
            account_id=operator.pk,
            reason="Synthetic review.",
            drawn=compromised.drawn_password(operator),
        )
        if reason == "marked-password":
            client.force_login(operator)
            expected_page = reverse("account_login")
        else:
            # A real password-and-code sign-in after the earlier sign-in ended.
            ended = client.get(confirm_url())
            assert ended.status_code == 302 and ended.url.startswith(reverse("account_login"))
            stage = sign_in(client, operator)
            assert stage.status_code == 302 and stage.url == reverse("mfa_authenticate")
            assert client.post(stage.url, {"code": codes[0]}).status_code == 302
            expected_page = reverse("account-new-password")
    old.refresh_from_db()
    new = current_row(client)
    assert old.ended_at is not None and old.pk != new.pk
    assert REVEAL_MARK not in client.session
    if reason == "marked-password":
        assert new.ended_at is not None and new.ending == AccountSession.Ending.MARKED_COMPROMISED
    else:
        assert new.ended_at is None and new.second_factor_required == (reason == "required-app")
    response = client.get(confirm_url())
    assert response.status_code == 302 and response.url.split("?", 1)[0] == expected_page
    assert REVEAL_MARK not in client.session and not reveal_history().exists()


def draw_mark(browser, account):
    url = reverse("admin:accounts_user_compromised", args=[account.pk])
    page = browser.get(url)
    assert page.status_code == 200
    fields = {
        "drawn": page.context["form"].initial["drawn"],
        "reason": "Synthetic training account.",
        "confirm_password": PASSWORD,
    }
    form = page.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    return url, fields, strip_tags(form)


@pytest.mark.parametrize(
    ("active", "address", "backend", "wording", "expected"),
    [
        (True, "reader@example.invalid", MAIL_ON, "sent an email notice", NoticeStatus.SENT),
        (
            False,
            "reader@example.invalid",
            MAIL_ON,
            "deactivated or has no email address",
            NoticeStatus.NOT_SENT,
        ),
        (True, "", MAIL_ON, "deactivated or has no email address", NoticeStatus.NOT_SENT),
        (
            True,
            "reader@example.invalid",
            MAIL_OFF,
            "This deployment sends no email",
            NoticeStatus.NOT_SENT,
        ),
    ],
)
def test_mark_form_and_delivery_agree_while_eligibility_stays_the_same(
    operator,
    admin_browser,
    settings,
    active,
    address,
    backend,
    wording,
    expected,
    django_capture_on_commit_callbacks,
):
    settings.EMAIL_BACKEND = backend
    operator.is_active, operator.email = active, address
    operator.save(update_fields=["is_active", "email"])
    url, fields, form = draw_mark(admin_browser, operator)
    assert wording in form
    with django_capture_on_commit_callbacks(execute=True):
        saved = admin_browser.post(url, fields)
    assert saved.status_code == 302
    notice = Notice.objects.get(topic="accounts.security", recipient=operator)
    assert notice.status == expected
    assert [message.to for message in mail.outbox] == (
        [[address]] if expected == NoticeStatus.SENT else []
    )
    if expected == NoticeStatus.NOT_SENT:
        assert notice.not_sent_reason == (
            NotSentReason.MAIL_OFF if backend == MAIL_OFF else NotSentReason.NO_LONGER_APPLIES
        )


@pytest.mark.parametrize("change", ["deactivated", "address-removed", "address-added"])
def test_admin_notice_preview_allows_for_a_changed_account_at_delivery(
    admin_browser, operator, settings, change
):
    """A snapshot can report today's eligibility; a queued message can have a different outcome."""
    settings.EMAIL_BACKEND = MAIL_ON
    if change == "address-added":
        operator.email = ""
        operator.save(update_fields=["email"])
    url, fields, shown = draw_mark(admin_browser, operator)
    # Change one account before saving the open form, the others after recording the notice.
    if change == "deactivated":
        operator.is_active = False
        operator.save(update_fields=["is_active"])
    assert admin_browser.post(url, fields).status_code == 302
    queued = Notice.objects.get(topic="accounts.security", recipient=operator)
    assert queued.status == NoticeStatus.WAITING
    if change != "deactivated":
        operator.email = "" if change == "address-removed" else "new-reader@example.invalid"
        operator.save(update_fields=["email"])
    result = outbox.attempt(queued.pk)
    queued.refresh_from_db()
    if change == "address-added":
        assert result == "sent" and [message.to for message in mail.outbox] == [[operator.email]]
        contradicted = "so no email notice is sent: tell the person yourself."
    else:
        assert result == "not_sent" and queued.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
        assert mail.outbox == []
        contradicted = (
            "The person is sent an email notice that their password was marked, "
            "with what to do next."
        )
    if contradicted in shown:
        assert any(
            word in shown.casefold()
            for word in (
                "currently",
                "at present",
                "as of now",
                "when delivery",
                "at delivery",
                "when the notice is sent",
                "when the notice is delivered",
                "when it is sent",
            )
        ), "The preview predicts delivery without qualifying when eligibility is checked."


def test_mark_completion_for_an_addressless_account_gives_a_usable_recovery_instruction(
    admin_browser, operator, settings, django_capture_on_commit_callbacks
):
    settings.EMAIL_BACKEND = MAIL_ON
    operator.email = ""
    operator.save(update_fields=["email"])
    url, fields, _ = draw_mark(admin_browser, operator)
    with django_capture_on_commit_callbacks(execute=True):
        saved = admin_browser.post(url, fields)
    assert saved.status_code == 302 and compromised.is_marked(operator.pk)
    page = admin_browser.get(saved.url)
    text = next(str(m) for m in page.context["messages"] if "as compromised" in str(m))
    assert "set a new password for them" in text and mail.outbox == []
    assert "Tell the person to use Forgot your password?" not in text


@pytest.fixture
def code_only_admin(client, staff):
    user = staff("verify-code-only", "admin")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    TOTP.activate(user, generate_totp_secret())
    client.force_login(user)
    return user


def test_code_only_admin_gets_accurate_good_to_know_for_the_rule_form(client, code_only_admin):
    page = client.get(reverse("reveal-confirmation-edit"))
    assert page.status_code == 200
    form = page.context["form"]
    assert "confirm_code" in form.fields and "confirm_password" not in form.fields
    guidance = page.context["page_documentation"]["good_to_know"]
    assert not any(
        line.startswith("Saving asks for your password, and a code if") for line in guidance
    )


def test_whats_new_describes_the_code_only_reveal_option(client, code_only_admin, rule):
    from core.changelog import entries

    prompt = client.get(confirm_url())
    assert prompt.status_code == 200
    assert "confirm_code" in prompt.context["form"].fields
    assert "confirm_password" not in prompt.context["form"].fields
    published = next(
        entry for entry in entries() if entry.id == "2026-11-24-confirm-before-revealing"
    )
    page = client.get(reverse("whats-new"))
    assert page.status_code == 200 and published.summary in strip_tags(page.content.decode())
    # The short entry is also displayed on its own. A reader needs the same options as on the form.
    unqualified = (
        "their password, and an authenticator or recovery code where they have an app.",
        "give their password, and a code from their authenticator app or a recovery code "
        "if they have one, before their first reveal",
    )
    assert not any(phrase in published.summary for phrase in unqualified)


def test_a_get_that_accepts_a_recent_confirmation_remembers_it_after_the_window(
    client, rule, operator, monkeypatch
):
    client.force_login(operator)
    confirm_password(client)
    before = client.get(confirm_url(), {"next": reverse("account-sessions")})
    assert before.status_code == 302 and before.url == reverse("account-sessions")
    mark = [operator.pk, str(current_row(client).pk)]
    assert client.session[REVEAL_MARK] == mark
    age_confirmation_clock(monkeypatch)
    assert not confirmation.current(SimpleNamespace(user=operator, session=client.session))
    later = client.get(confirm_url(), {"next": reverse("account-sign-ins")})
    assert later.status_code == 302 and later.url == reverse("account-sign-ins")
    assert client.session[REVEAL_MARK] == mark and not reveal_history().exists()


def test_a_password_only_confirmation_lacks_the_code_of_an_app_added_since(client, rule, operator):
    client.force_login(operator)
    confirm_password(client)
    TOTP.activate(operator, generate_totp_secret())
    page = client.get(confirm_url())
    assert page.status_code == 200
    assert {"confirm_password", "confirm_code"}.issubset(page.context["form"].fields)
    assert REVEAL_MARK not in client.session and not reveal_history().exists()


def test_login_form_visit_by_an_already_signed_in_account_keeps_its_session(client, operator):
    assert sign_in(client, operator).status_code == 302
    before = current_row(client)
    visit = client.get(reverse("account_login"))
    assert visit.status_code == 302 and current_row(client).pk == before.pk
    assert AccountSession.objects.filter(user=operator).count() == 1


def test_ended_row_is_refused_before_the_confirmation_page(client, rule, operator):
    from accounts.session_list import end_account_sessions

    client.force_login(operator)
    before = current_row(client)
    assert client.post(confirm_url(), {"confirm_password": PASSWORD}).status_code == 302
    assert REVEAL_MARK in client.session
    owner = User.objects.create_superuser("ending-owner", "end@example.invalid", PASSWORD)
    end_account_sessions(actor=owner, account_id=operator.pk, reason="Shift complete.")
    before.refresh_from_db()
    assert before.ended_at and before.ending == AccountSession.Ending.ADMINISTRATOR
    stop = client.get(confirm_url())
    assert stop.status_code == 302 and stop.url.split("?", 1)[0] == reverse("account_login")
    assert REVEAL_MARK not in client.session and not reveal_history().exists()


def test_code_only_administrator_gets_matching_panel_fact_field_and_quick_help(
    client, code_only_admin, rule
):
    from core.security_settings import in_force

    setting = next(
        fact
        for group in in_force()
        for fact in group.facts
        if fact.key == "patient-reveal-confirmation"
    )
    assert setting.value == "Asked once in each session"
    panel = client.get(reverse("security-settings"))
    assert panel.status_code == 200
    text = strip_tags(
        panel.content.decode()
        .split('id="reveal-confirmation-heading"', 1)[1]
        .split("</section>", 1)[0]
    )
    edit = client.get(reverse("reveal-confirmation-edit"))
    assert edit.status_code == 200
    field = edit.context["form"].fields["first_reveal"]
    for explanation in (setting.note, text, field.help_text):
        assert "without a password gives the code alone" in explanation
    prompt = client.get(confirm_url())
    assert prompt.status_code == 200
    steps = prompt.context["page_documentation"]["tasks"][0]["steps"]
    assert "or both" in steps[0]


def test_a_rolled_back_removal_does_not_show_the_account_holder_a_success_message(
    client, operator, monkeypatch, settings
):
    from allauth.mfa.models import Authenticator
    from django.template.loader import render_to_string

    from accounts import mfa_views

    settings.DEBUG = False
    settings.EMAIL_BACKEND = MAIL_ON
    TOTP.activate(operator, generate_totp_secret())
    RecoveryCodes.activate(operator)
    factors = Authenticator.objects.filter(user=operator)
    before = set(factors.values_list("pk", flat=True))
    client.force_login(operator)
    confirm_password(client)
    assert client.get(reverse("mfa_deactivate_totp")).status_code == 200

    def unavailable(**kwargs):
        raise RuntimeError("Synthetic audit write failure")

    monkeypatch.setattr(mfa_views, "record_event", unavailable)
    client.raise_request_exception = False
    failed = client.post(reverse("mfa_deactivate_totp"))
    assert failed.status_code == 500
    assert set(factors.values_list("pk", flat=True)) == before
    assert not AuditEvent.objects.filter(
        action="account.authenticator_removed", actor=operator
    ).exists()
    assert not Notice.objects.filter(topic="accounts.security", recipient=operator).exists()
    assert not mail.outbox
    next_page = client.get(reverse("mfa_index"))
    assert next_page.status_code == 200
    success = render_to_string("mfa/messages/totp_deactivated.txt").strip()
    assert success and success not in strip_tags(failed.content.decode())
    assert success not in strip_tags(next_page.content.decode())


def test_invalid_mark_submission_redraws_for_the_current_account(admin_browser, operator, settings):
    settings.EMAIL_BACKEND = MAIL_ON
    url, fields, original = draw_mark(admin_browser, operator)
    assert "sent an email notice" in original
    operator.email = ""
    operator.save(update_fields=["email"])
    invalid = admin_browser.post(url, {**fields, "reason": ""})
    assert invalid.status_code == 200 and invalid.context["form"].has_error("reason", "required")
    form = invalid.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    assert "has no email address" in form
    assert not compromised.is_marked(operator.pk) and not Notice.objects.exists()
