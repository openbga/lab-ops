"""Email notices to the account holder about changes to how their account signs in
(accounts.security_notices): recorded in the change's transaction, worded at sending from the
account and the laboratory's security contact as they are then. All data is synthetic."""

import time

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    SECRET_SESSION_KEY,
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client as HttpClient
from django.urls import resolve, reverse

from accounts import compromised, mfa_views, security_notices
from accounts.models import User
from core.models import AuditEvent, Lab, LabContact, UserPreference
from desk.notifications import OPT_OUT_KEY
from jobs import outbox
from jobs.models import Notice, NoticeStatus, NotSentReason

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
DUMMY = "django.core.mail.backends.dummy.EmailBackend"
LAB = "Example Reference Laboratory"
SITE = "https://lab-ops.example.invalid"
SUBJECT_ADDED = f"{LAB}: an authenticator app was added to your account"


@pytest.fixture(autouse=True)
def email_on(settings):
    """Email on (Django's in-memory backend), the laboratory's name and a public address."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.EMAIL_BACKEND = LOCMEM
    settings.PUBLIC_URL = SITE
    Lab.objects.update_or_create(pk=1, defaults={"name": LAB})
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(make_user):
    return make_user("notice_person", "technical")


@pytest.fixture
def owner(make_user):
    return make_user("notice_owner", is_staff=True, is_superuser=True)


@pytest.fixture
def security_contact(db):
    return LabContact.objects.create(
        kind=LabContact.Kind.SECURITY,
        name="Information Security Office",
        email="security@example.invalid",
        phone="555-0100",
    )


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    """Run what a change registers for after its commit (the outbox's first attempt, allauth's
    messages), as a real commit would."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def code_for(secret):
    """The app's current code, with at least 5 seconds of its period left."""
    left = 30 - time.time() % 30
    if left < 5:
        time.sleep(left + 0.2)
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def browser_for(user, *, reauthenticated=True):
    """A signed-in browser; re-authenticated with the password just now unless told otherwise."""
    browser = HttpClient()
    browser.force_login(user)
    if reauthenticated:
        response = browser.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        assert response.status_code == 302, response.content[:300]
    return browser


def enrol(user):
    """An authenticator app and recovery codes, as adding an app leaves them."""
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    codes = RecoveryCodes.activate(user)
    return secret, codes.get_unused_codes()


def notice_of(user):
    notices = list(Notice.objects.filter(topic=security_notices.TOPIC, recipient=user))
    assert len(notices) == 1, notices
    return notices[0]


def only_mail():
    assert len(mail.outbox) == 1, [m.subject for m in mail.outbox]
    return mail.outbox[0]


def assert_nothing_secret(text, *secrets):
    for secret in secrets:
        assert secret not in text
    assert "127.0.0.1" not in text  # the test client's address
    assert "Mozilla" not in text and "user agent" not in text.lower()


def assert_data_is_codes_only(notice):
    assert set(notice.data) <= {"at", "by", "types", "mark"}
    assert notice.reference == "Account"
    assert notice.object_id == notice.recipient_id


def activate(browser, committed):
    page = browser.get(reverse("mfa_activate_totp"))
    assert page.status_code == 200
    secret = browser.session[SECRET_SESSION_KEY]
    with committed():
        response = browser.post(reverse("mfa_activate_totp"), {"code": code_for(secret)})
    return secret, response


# --- the routes ---------------------------------------------------------------------------------


def test_the_pages_that_change_factors_are_served_here_at_allauths_paths():
    expected = {
        "mfa_activate_totp": ("/accounts/2fa/totp/activate/", mfa_views.activate_totp),
        "mfa_deactivate_totp": ("/accounts/2fa/totp/deactivate/", mfa_views.deactivate_totp),
        "mfa_generate_recovery_codes": (
            "/accounts/2fa/recovery-codes/generate/",
            mfa_views.generate_recovery_codes,
        ),
    }
    for name, (path, view) in expected.items():
        assert reverse(name) == path
        assert resolve(path).func is view
    # The other two-factor pages stay allauth's.
    assert resolve(reverse("mfa_view_recovery_codes")).func.__module__.startswith("allauth.")


# --- added by the person ------------------------------------------------------------------------


def test_adding_an_app_records_one_notice_with_both_events_and_sends_it(
    person, security_contact, committed
):
    browser = browser_for(person)
    secret, response = activate(browser, committed)
    # allauth's flow, redirect and message are unchanged.
    assert response.status_code == 302 and response.url == reverse("mfa_view_recovery_codes")
    totp = Authenticator.objects.get(user=person, type=Authenticator.Type.TOTP)
    codes = Authenticator.objects.get(user=person, type=Authenticator.Type.RECOVERY_CODES)

    notice = notice_of(person)
    assert notice.events == ["authenticator_added", "recovery_codes_replaced"]
    assert notice.data["by"] == "self"
    assert notice.data["types"] == ["recovery_codes", "totp"]
    assert_data_is_codes_only(notice)
    assert notice.status == NoticeStatus.SENT

    sent = only_mail()
    assert sent.to == [person.email]
    assert sent.subject == SUBJECT_ADDED
    body = sent.body
    assert f"An authenticator app was added to your account {person.username} on " in body
    assert " UTC." in body
    assert "New recovery codes were issued with it." in body
    assert (
        "contact the laboratory's security contact, Information Security Office "
        "(security@example.invalid, 555-0100) straight away." in body
    )
    assert LAB in body and SITE in body
    assert_nothing_secret(
        sent.subject + body + str(notice.data),
        secret,
        totp.data["secret"],
        *codes.wrap().get_unused_codes(),
    )

    event = AuditEvent.objects.get(action="account.authenticator_added")
    assert event.actor == person
    assert (event.object_type, event.object_id) == ("accounts.user", str(person.pk))
    added = event.after["authenticators"]
    assert [(a["authenticator_id"], a["type"]) for a in added] == [
        (totp.pk, "totp"),
        (codes.pk, "recovery_codes"),
    ]
    assert_nothing_secret(
        str(event.before) + str(event.after) + event.reason,
        secret,
        totp.data["secret"],
        *codes.wrap().get_unused_codes(),
    )


@pytest.mark.django_db(transaction=True)
def test_with_real_commits_allauths_message_shows_and_the_notice_is_sent(make_user):
    """No test transaction around the request: the change commits when the view's block ends, so
    allauth's after-commit message and the outbox's first attempt run then, in the request."""
    Lab.objects.update_or_create(pk=1, defaults={"name": LAB})
    person = make_user("committing_person", "technical")
    browser = browser_for(person)
    browser.get(reverse("mfa_activate_totp"))
    secret = browser.session[SECRET_SESSION_KEY]
    response = browser.post(reverse("mfa_activate_totp"), {"code": code_for(secret)})
    assert response.status_code == 302 and response.url == reverse("mfa_view_recovery_codes")
    shown = [str(m) for m in browser.get(response.url).context["messages"]]
    assert any("recovery codes" in m.lower() for m in shown), shown
    assert notice_of(person).status == NoticeStatus.SENT
    assert only_mail().subject == SUBJECT_ADDED


def test_a_change_that_rolls_back_leaves_no_notice(person, committed, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(mfa_views, "record_event", fail)
    browser = browser_for(person)
    browser.raise_request_exception = False
    _secret, response = activate(browser, committed)
    assert response.status_code == 500
    assert not Authenticator.objects.filter(user=person).exists()
    assert not Notice.objects.exists() and mail.outbox == []


def test_an_app_added_meanwhile_is_not_added_again(person, rf, committed):
    # Added in another tab after allauth checked, before this save took the account's lock.
    TOTP.activate(person, generate_totp_secret())
    view = mfa_views.ActivateTOTPView()
    view.request = rf.post(reverse("mfa_activate_totp"))
    view.request.user = person
    with committed():
        response = view.form_valid(form=None)  # decided under the lock, before allauth's flow
    assert response.url == reverse("mfa_deactivate_totp")
    assert Authenticator.objects.filter(user=person).count() == 1
    assert not Notice.objects.exists() and not AuditEvent.objects.exists()


# --- new recovery codes -------------------------------------------------------------------------


def test_new_recovery_codes_record_a_notice_and_an_audit_event(person, committed):
    secret, old_codes = enrol(person)
    old = Authenticator.objects.get(user=person, type=Authenticator.Type.RECOVERY_CODES)
    browser = browser_for(person)
    with committed():
        response = browser.post(reverse("mfa_generate_recovery_codes"))
    assert response.status_code == 302 and response.url == reverse("mfa_view_recovery_codes")
    new = Authenticator.objects.get(user=person, type=Authenticator.Type.RECOVERY_CODES)
    assert new.pk != old.pk
    new_codes = new.wrap().get_unused_codes()

    notice = notice_of(person)
    assert notice.events == ["recovery_codes_replaced"]
    assert notice.data["types"] == ["recovery_codes"] and notice.data["by"] == "self"
    assert_data_is_codes_only(notice)

    sent = only_mail()
    assert sent.subject == f"{LAB}: new recovery codes were issued for your account"
    assert f"New recovery codes were issued for your account {person.username} on " in sent.body
    assert "Any earlier recovery codes no longer work." in sent.body
    assert "your laboratory's administrator" in sent.body  # no security contact recorded
    assert_nothing_secret(sent.body, secret, *old_codes, *new_codes)

    event = AuditEvent.objects.get(action="account.recovery_codes_replaced")
    assert event.actor == person and event.object_id == str(person.pk)
    assert [a["authenticator_id"] for a in event.before["authenticators"]] == [old.pk]
    assert [a["authenticator_id"] for a in event.after["authenticators"]] == [new.pk]
    assert_nothing_secret(str(event.before) + str(event.after), secret, *old_codes, *new_codes)


def test_new_codes_are_refused_once_the_app_is_gone(person, committed):
    enrol(person)
    browser = browser_for(person)
    browser.get(reverse("mfa_generate_recovery_codes"))
    Authenticator.objects.filter(user=person, type=Authenticator.Type.TOTP).delete()
    with committed():
        response = browser.post(reverse("mfa_generate_recovery_codes"))
    assert response.status_code == 200
    assert not Notice.objects.exists() and not AuditEvent.objects.exists()


# --- removed by the person ----------------------------------------------------------------------


def test_removing_ones_own_app_removes_its_codes_and_records_one_notice(person, committed):
    secret, codes = enrol(person)
    ids = set(Authenticator.objects.filter(user=person).values_list("pk", flat=True))
    browser = browser_for(person)
    with committed():
        response = browser.post(reverse("mfa_deactivate_totp"))
    assert response.status_code == 302 and response.url == reverse("mfa_index")
    assert not Authenticator.objects.filter(user=person).exists()

    notice = notice_of(person)
    assert notice.events == ["authenticator_removed"]
    assert notice.data["types"] == ["recovery_codes", "totp"] and notice.data["by"] == "self"
    assert_data_is_codes_only(notice)

    sent = only_mail()
    assert sent.subject == f"{LAB}: an authenticator app was removed from your account"
    assert (
        f"The authenticator app and its recovery codes were removed from your account "
        f"{person.username} on " in sent.body
    )
    assert_nothing_secret(sent.body, secret, *codes)

    event = AuditEvent.objects.get(action="account.authenticator_removed")
    assert event.actor == person and event.object_id == str(person.pk)
    assert {a["authenticator_id"] for a in event.before["authenticators"]} == ids
    assert_nothing_secret(str(event.before) + str(event.after), secret, *codes)


# --- allauth's reauthentication still guards the three pages ------------------------------------


@pytest.mark.parametrize(
    "name", ["mfa_activate_totp", "mfa_deactivate_totp", "mfa_generate_recovery_codes"]
)
def test_a_post_without_reauthentication_changes_nothing(person, committed, name):
    if name != "mfa_activate_totp":
        enrol(person)
    before = set(Authenticator.objects.filter(user=person).values_list("pk", "type"))
    browser = browser_for(person, reauthenticated=False)
    session = browser.session
    # Signed in long ago: no authentication within allauth's reauthentication timeout.
    session[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time() - 3600, "username": person.username}
    ]
    session.save()
    with committed():
        response = browser.post(reverse(name), {"code": "123456"})
    assert response.status_code == 302
    assert "reauthenticate" in response.url
    assert set(Authenticator.objects.filter(user=person).values_list("pk", "type")) == before
    assert not Notice.objects.exists() and not AuditEvent.objects.exists()
    assert mail.outbox == []


# --- removed by a superuser in the account administration ---------------------------------------


@pytest.mark.parametrize(
    ("kind", "subject", "line"),
    [
        (
            "totp",
            "an authenticator app was removed from your account",
            "An administrator removed the authenticator app from your account",
        ),
        (
            "recovery_codes",
            "your recovery codes were removed from your account",
            "An administrator removed the recovery codes from your account",
        ),
    ],
)
def test_a_superuser_removal_is_noticed_as_by_an_administrator(
    owner, person, security_contact, committed, kind, subject, line
):
    secret, codes = enrol(person)
    factor = Authenticator.objects.get(user=person, type=kind)
    browser = HttpClient()
    browser.force_login(owner)
    with committed():
        response = browser.post(
            reverse("admin:mfa_authenticator_delete", args=[factor.pk]), {"post": "yes"}
        )
    assert response.status_code == 302
    notice = notice_of(person)
    assert notice.events == ["authenticator_removed"]
    assert notice.data["types"] == [kind] and notice.data["by"] == "administrator"
    assert_data_is_codes_only(notice)
    sent = only_mail()
    assert sent.to == [person.email]
    assert sent.subject == f"{LAB}: {subject}"
    assert f"{line} {person.username} on " in sent.body
    assert owner.username not in sent.body
    assert_nothing_secret(sent.body, secret, *codes)
    # The existing audit event is unchanged: the superuser's, with the removed record.
    event = AuditEvent.objects.get(action="account.authenticator_removed")
    assert event.actor == owner and event.before["authenticator_id"] == factor.pk


@pytest.mark.confirmation
def test_a_removal_refused_by_the_superusers_confirmation_records_no_notice(
    owner, person, committed
):
    enrol(person)
    factor = Authenticator.objects.get(user=person, type="totp")
    browser = HttpClient()
    browser.force_login(owner)
    with committed():
        response = browser.post(
            reverse("admin:mfa_authenticator_delete", args=[factor.pk]), {"post": "yes"}
        )
    assert response.status_code == 200
    assert Authenticator.objects.filter(pk=factor.pk).exists()
    assert not Notice.objects.exists() and mail.outbox == []


# --- password marked as compromised -------------------------------------------------------------


def mark(owner, person, committed):
    browser = HttpClient()
    browser.force_login(owner)
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    drawn = browser.get(url).context["form"].initial["drawn"]
    with committed():
        response = browser.post(url, {"reason": "Typed into a look-alike page", "drawn": drawn})
    assert response.status_code == 302
    return compromised.open_mark(person.pk)


def test_a_compromised_mark_is_noticed_with_what_to_do_next(owner, person, committed, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marked = mark(owner, person, committed)
    notice = notice_of(person)
    assert notice.events == ["password_marked_compromised"]
    assert notice.data["mark"] == marked.pk and notice.data["by"] == "administrator"
    assert_data_is_codes_only(notice)
    sent = only_mail()
    assert sent.subject == f"{LAB}: your password was marked as compromised"
    assert (
        f"An administrator marked the password of your account {person.username} as "
        "compromised on " in sent.body
    )
    assert "signed out every session of the account" in sent.body
    assert (
        'Your password no longer signs you in. Choose a new one with "Forgot your password?" on '
        'the sign-in page, or choose "Email me a sign-in code" and then a new password.'
    ) in sent.body
    assert "Typed into a look-alike page" not in sent.body  # the reason stays in the audit
    assert PASSWORD not in sent.body and owner.username not in sent.body


def test_a_marked_account_with_a_second_factor_is_told_to_sign_in_with_it(owner, person, committed):
    enrol(person)
    mark(owner, person, committed)
    body = only_mail().body
    assert (
        "Sign in with your password and your second sign-in factor: you will be asked to choose "
        "a new password before anything else."
    ) in body


def test_a_mark_lifted_before_sending_says_a_new_password_was_chosen(owner, person):
    with transaction.atomic():  # committed; its first attempt is not made
        compromised.mark_compromised(
            actor=owner,
            account_id=person.pk,
            reason="Leaked",
            drawn=compromised.drawn_password(person),
        )
    person.refresh_from_db()
    person.set_password("a long synthetic replacement 8841")
    person.save(update_fields=["password"])
    assert compromised.open_mark(person.pk) is None
    assert outbox.deliver_due()["sent"] == 1
    assert "A new password has been chosen for the account since." in only_mail().body


@pytest.mark.parametrize("case", ["own", "fingerprint"])
def test_a_refused_mark_records_no_notice(owner, person, case):
    target = owner if case == "own" else person
    with pytest.raises(ValidationError), transaction.atomic():
        compromised.mark_compromised(
            actor=owner,
            account_id=target.pk,
            reason="Leaked",
            drawn="" if case == "fingerprint" else compromised.drawn_password(target),
        )
    assert not Notice.objects.exists()


# --- at sending ---------------------------------------------------------------------------------


def recorded(user, events=("authenticator_added",), **kwargs):
    """A notice recorded and committed, its first attempt not yet made."""
    with transaction.atomic():
        notice = security_notices.record(user, list(events), by="self", **kwargs)
    return notice


def test_a_person_who_turned_email_notices_off_still_gets_it(person):
    UserPreference.objects.create(user=person, key=OPT_OUT_KEY, value=False)
    recorded(person, types=["totp"])
    assert outbox.deliver_due()["sent"] == 1
    assert only_mail().to == [person.email]


@pytest.mark.parametrize("change", ["inactive", "no_email"])
def test_a_notice_no_longer_applies_to_an_inactive_account_or_one_without_an_address(
    person, change
):
    notice = recorded(person, types=["totp"])
    if change == "inactive":
        User.objects.filter(pk=person.pk).update(is_active=False)
    else:
        User.objects.filter(pk=person.pk).update(email="")
    assert outbox.deliver_due()["not_sent"] == 1
    notice.refresh_from_db()
    assert notice.status == NoticeStatus.NOT_SENT
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    assert mail.outbox == []


def test_the_contact_is_read_when_the_notice_is_sent(person):
    recorded(person, types=["totp"])
    LabContact.objects.create(kind=LabContact.Kind.SECURITY, name="Laboratory Director")
    outbox.deliver_due()
    assert "contact the laboratory's security contact, Laboratory Director straight away." in (
        only_mail().body
    )


def test_with_email_off_the_notice_is_recorded_as_not_sent(person, settings, committed):
    settings.EMAIL_BACKEND = DUMMY
    browser = browser_for(person)
    activate(browser, committed)
    notice = notice_of(person)
    assert notice.status == NoticeStatus.NOT_SENT
    assert notice.not_sent_reason == NotSentReason.MAIL_OFF
    assert mail.outbox == []


def test_unknown_events_or_codes_are_not_worded(person):
    notice = recorded(person, events=["something_else"])
    assert outbox.deliver_due()["not_sent"] == 1
    notice.refresh_from_db()
    assert notice.not_sent_reason == NotSentReason.NO_LONGER_APPLIES
    with transaction.atomic():
        security_notices.record(person, ["authenticator_removed"], by="self", types=["webauthn"])
    outbox.deliver_due()
    assert "A sign-in factor was removed from your account" in only_mail().body
