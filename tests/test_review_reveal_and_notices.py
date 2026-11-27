"""Independent functional regressions for reveal confirmation and account notices."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, TOTP, generate_totp_secret
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import Client as Browser
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from accounts import confirmation
from accounts.acting import hold_actor
from accounts.models import SignInEvent, SignInFactor, User
from accounts.roles import ROLES
from accounts.session_list import end_account_sessions
from accounts.tests.test_confirm_identity import code_for as code_with_time_left
from core.models import AuditEvent, LabContact
from jobs import outbox
from jobs.models import Notice, NoticeStatus
from links.services import add_link, remove_link
from links.tests.conftest import PATIENT_KEY
from links.tests.conftest import lis as lis
from request_forms.models import FieldValue
from request_forms.services import correct_values
from request_forms.tests import conftest as fixtures
from request_forms.tests.conftest import PASSWORD, PATIENT_SENTINEL, definition_of
from request_forms.tests.test_pages import raised
from tests.test_session_limits import age

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]

# Register the repository's synthetic form fixtures in this cross-application module.
make_user = fixtures.make_user
catalog = fixtures.catalog
group = fixtures.group
logistics = fixtures.logistics
pickup_category = fixtures.pickup_category
pickup = fixtures.pickup
clinic = fixtures.clinic
portal_user = fixtures.portal_user
capable = fixtures.capable

VALUE_EVENT = "request_form.value_revealed"
LINK_EVENT = "external_link.person_revealed"
REVEAL_EVENTS = (VALUE_EVENT, LINK_EVENT)


@pytest.fixture
def reveal_rule():
    from core.models import RevealConfirmationRule

    return RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)


@pytest.fixture
def isolated_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def patient_ticket(portal_user, pickup, capable, lis):
    ticket = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    row = FieldValue.objects.get(filled_form__request=ticket, key="patient")
    link, _ = add_link(
        actor=capable,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    return ticket, row, link


def review_urls(patient_ticket):
    _, row, link = patient_ticket
    return (
        reverse("request_details:reveal", args=[row.pk]),
        reverse("link_records:attachment-reveal", args=[link.pk]),
    )


def review_confirm(browser, **fields):
    return browser.post(reverse("account-confirm-reveal"), fields)


def review_reveals():
    return AuditEvent.objects.filter(action__in=REVEAL_EVENTS)


def review_grant(user):
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    user.user_permissions.add(permission)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("granted", [False, True], ids=["ordinary", "patient-grant"])
def test_staff_role_reads_both_reveal_types_with_its_current_grant(
    client, make_user, patient_ticket, reveal_rule, isolated_limits, role, granted
):
    actor = make_user("review-reader", role)
    if granted:
        review_grant(actor)
    client.force_login(actor)
    urls = review_urls(patient_ticket)
    for url in urls:
        wait = client.post(url, HTTP_HX_REQUEST="true")
        assert wait.status_code == 204 and wait["HX-Redirect"] == reverse("account-confirm-reveal")
        assert wait.content == b""
    assert review_confirm(client, confirm_password=PASSWORD).status_code == 302
    assert not review_reveals().exists()
    for url, value in zip(urls, (PATIENT_SENTINEL, PATIENT_KEY), strict=True):
        response = client.post(url, HTTP_HX_REQUEST="true")
        assert response.status_code == (200 if granted else 404)
        assert (value.encode() in response.content) == granted
    actual = list(review_reveals().values_list("action", "actor_id"))
    assert sorted(actual) == (
        sorted((event, actor.pk) for event in REVEAL_EVENTS) if granted else []
    )


@pytest.mark.parametrize("audience", ["signed-out", "portal", "roleless-owner", "inactive"])
def test_staff_pages_require_an_active_staff_role(
    client, patient_ticket, reveal_rule, portal_user, make_user, audience
):
    if audience == "portal":
        client.force_login(portal_user)
    elif audience == "roleless-owner":
        client.force_login(make_user("review-owner", is_superuser=True, is_staff=True))
    elif audience == "inactive":
        actor = make_user("review-inactive", "admin")
        client.force_login(actor)
        actor.is_active = False
        actor.save(update_fields=["is_active"])
    expected = 302 if audience in ("signed-out", "inactive") else 403
    paths = [
        (url, method)
        for url in (reverse("account-confirm-reveal"), "/lab/security/reveal-confirmation/")
        for method in ("get", "head", "post")
    ] + [(url, "post") for url in review_urls(patient_ticket)]
    for url, method in paths:
        response = getattr(client, method)(url)
        assert response.status_code == expected, (url, method)
        if expected == 302:
            assert urlsplit(response["Location"]).path == reverse("account_login")
        assert PATIENT_SENTINEL.encode() not in response.content
        assert PATIENT_KEY.encode() not in response.content
    assert "_reveal_confirmed" not in client.session
    assert not review_reveals().exists()
    assert not AuditEvent.objects.filter(action="lab.reveal_confirmation_changed").exists()


def test_confirmation_methods_do_not_change_state_on_read(client, capable, reveal_rule):
    client.force_login(capable)
    url = reverse("account-confirm-reveal")
    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert "no-store" in head["Cache-Control"]
    for method in ("options", "put", "patch", "delete"):
        response = getattr(client, method)(url)
        assert response.status_code == 405 and set(response["Allow"].split(", ")) == {
            "GET",
            "HEAD",
            "POST",
        }
    assert "_reveal_confirmed" not in client.session


def test_a_confirmation_in_one_browser_covers_both_reveals_only_there(
    capable, patient_ticket, reveal_rule, isolated_limits
):
    first, second = Browser(), Browser()
    first.force_login(capable)
    second.force_login(capable)
    assert first.session.session_key != second.session.session_key
    assert review_confirm(first, confirm_password=PASSWORD).status_code == 302
    for url in review_urls(patient_ticket):
        assert first.post(url, HTTP_HX_REQUEST="true").status_code == 200
        assert second.post(url, HTTP_HX_REQUEST="true").status_code == 204
    assert sorted(review_reveals().values_list("action", flat=True)) == sorted(REVEAL_EVENTS)


def test_changing_the_rule_between_the_confirmation_page_and_submit(
    client, capable, patient_ticket, reveal_rule, staff
):
    from core.reveal_confirmation import save_rule

    admin = staff("review-rule-admin", "admin")
    client.force_login(capable)
    url = reverse("account-confirm-reveal")
    ticket, _, _ = patient_ticket
    back = reverse("desk:request-detail", args=[ticket.pk]) + "?tab=details"
    assert client.get(url, {"next": back}).status_code == 200
    save_rule(actor=admin, first_reveal=False, expected_revision=1, reason="Single shift.")
    done = client.post(url, {"next": back})
    assert done.status_code == 302 and done["Location"] == back
    assert "_reveal_confirmed" not in client.session
    assert not review_reveals().exists()
    save_rule(actor=admin, first_reveal=True, expected_revision=2, reason="Later shift.")
    assert client.post(review_urls(patient_ticket)[0], HTTP_HX_REQUEST="true").status_code == 204


def test_ticket_edits_before_reveal_choose_only_the_current_records(
    client, capable, patient_ticket, reveal_rule, isolated_limits
):
    ticket, row, link = patient_ticket
    client.force_login(capable)
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 200
    assert review_confirm(client, confirm_password=PASSWORD).status_code == 302
    correct_values(
        actor=capable,
        request=ticket,
        expected_definition=definition_of(ticket),
        expected={"patient": row.pk},
        values={"patient": "PT-SYNTHETIC-CORRECTED"},
        reason="",
        reason_code="typing",
    )
    remove_link(actor=capable, attachment_id=link.pk, reason_code="no_longer_needed")
    for url in review_urls(patient_ticket):
        old = client.post(url, HTTP_HX_REQUEST="true")
        assert old.status_code == 404
    assert not review_reveals().exists()
    current = FieldValue.objects.get(filled_form__request=ticket, key="patient", superseded_at=None)
    result = client.post(
        reverse("request_details:reveal", args=[current.pk]), HTTP_HX_REQUEST="true"
    )
    assert result.status_code == 200 and current.value.encode() in result.content
    assert review_reveals().get().after["value_id"] == current.pk


def test_reveals_recheck_category_and_capability_after_confirmation(
    client, make_user, patient_ticket, reveal_rule, isolated_limits
):
    actor = make_user("review-technical", "technical")
    review_grant(actor)
    client.force_login(actor)
    ticket, _, _ = patient_ticket
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 200
    assert review_confirm(client, confirm_password=PASSWORD).status_code == 302
    category = ticket.category
    category.is_restricted = True
    category.save(update_fields=["is_restricted"])
    for url in review_urls(patient_ticket):
        assert client.post(url).status_code == 404
    category.is_restricted = False
    category.save(update_fields=["is_restricted"])
    actor.user_permissions.clear()
    for url in review_urls(patient_ticket):
        assert client.post(url).status_code == 404
    assert not review_reveals().exists()


def test_portal_ticket_still_shows_its_own_answer_with_the_rule_on(
    client, portal_user, patient_ticket, reveal_rule
):
    client.force_login(portal_user)
    ticket, _, _ = patient_ticket
    response = client.get(reverse("portal:request-detail", args=[ticket.number]))
    assert response.status_code == 200
    assert (
        PATIENT_SENTINEL.encode() in response.content
        and PATIENT_KEY.encode() not in response.content
    )
    assert "_reveal_confirmed" not in client.session and not review_reveals().exists()


def test_shared_demo_staff_can_confirm_an_existing_password(
    client, capable, patient_ticket, reveal_rule, isolated_limits
):
    capable.credentials_locked = True
    capable.save(update_fields=["credentials_locked"])
    client.force_login(capable)
    result = review_confirm(client, confirm_password=PASSWORD)
    assert result.status_code == 302
    url = review_urls(patient_ticket)[0]
    shown = client.post(url, HTTP_HX_REQUEST="true")
    assert shown.status_code == 200 and PATIENT_SENTINEL.encode() in shown.content
    assert review_reveals().get().actor_id == capable.pk


@pytest.mark.parametrize("password", [True, False], ids=["password-and-code", "code-only"])
def test_confirmation_consumes_one_recovery_code(
    client, capable, reveal_rule, isolated_limits, password
):
    if not password:
        capable.set_unusable_password()
        capable.save(update_fields=["password"])
    TOTP.activate(capable, generate_totp_secret())
    codes = RecoveryCodes.activate(capable).get_unused_codes()
    client.force_login(capable)
    form = client.get(reverse("account-confirm-reveal")).context["form"]
    assert ("confirm_password" in form.fields) == password
    fields = {"confirm_code": codes[0]}
    if password:
        fields["confirm_password"] = PASSWORD
    done = review_confirm(client, **fields)
    assert done.status_code == 302 and client.session["_reveal_confirmed"][0] == capable.pk
    remaining = RecoveryCodes(Authenticator.objects.get(user=capable, type="recovery_codes"))
    assert remaining.get_unused_codes() == codes[1:]
    assert not review_reveals().exists()


@pytest.mark.parametrize("factor", ["password", "second_factor"])
def test_failed_confirmation_records_the_failed_factor_and_redacts_the_input(
    client, capable, reveal_rule, isolated_limits, caplog, factor
):
    typed = "review-unsuccessful-entry-731"
    fields = {"confirm_password": typed}
    if factor == "second_factor":
        TOTP.activate(capable, generate_totp_secret())
        fields = {"confirm_password": PASSWORD, "confirm_code": typed}
    client.force_login(capable)
    failures = SignInEvent.objects.filter(
        user=capable, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    )
    assert not failures.exists()
    refused = review_confirm(client, **fields)
    assert refused.status_code == 200 and failures.get().factor == factor
    assert "_reveal_confirmed" not in client.session
    redacted = SafeExceptionReporterFilter().get_post_parameters(refused.wsgi_request)
    history = client.get(reverse("account-sign-ins"))
    assert history.status_code == 200
    assert "Re-authentication failed" in history.content.decode()
    for surface in (
        refused.content.decode(),
        history.content.decode(),
        caplog.text,
        str(redacted),
        str(list(SignInEvent.objects.filter(user=capable).values())),
        str(list(AuditEvent.objects.filter(actor=capable).values("before", "after", "reason"))),
        str(dict(client.session)),
    ):
        assert typed not in surface
    assert not review_reveals().exists()


def test_failed_signin_limit_and_reauthentication_limit_have_distinct_history(
    client, capable, reveal_rule, isolated_limits
):
    client.force_login(capable)
    for _ in range(5):
        response = review_confirm(client, confirm_password="review-incorrect")
        assert "Incorrect password." in response.content.decode()
    history = SignInEvent.objects.filter(user=capable).exclude(kind=SignInEvent.Kind.SIGNED_IN)
    assert (
        list(history.values_list("kind", "factor"))
        == [(SignInEvent.Kind.REAUTHENTICATION_FAILED, SignInFactor.PASSWORD)] * 5
    )
    for _ in range(5):
        refused = review_confirm(client, confirm_password=PASSWORD)
        assert "Too many failed login attempts" in refused.content.decode()
    assert history.filter(kind=SignInEvent.Kind.LOCKED_OUT).count() == 1
    ids = set(history.values_list("pk", flat=True))
    response = review_confirm(client, confirm_password=PASSWORD)
    assert "Too many attempts to confirm it is you." in response.content.decode()
    assert set(history.values_list("pk", flat=True)) == ids
    assert "_reveal_confirmed" not in client.session


@pytest.mark.parametrize(
    "ending", ["inactivity", "maximum", "password", "deactivation", "administrator"]
)
def test_ended_session_loses_its_reveal_confirmation(
    client, capable, patient_ticket, reveal_rule, isolated_limits, ending
):
    client.force_login(capable)
    assert review_confirm(client, confirm_password=PASSWORD).status_code == 302
    if ending == "inactivity":
        age(capable, idle=timedelta(hours=1))
    elif ending == "maximum":
        age(capable, since=timedelta(days=1))
    elif ending == "password":
        capable.set_password("another example password for this review")
        capable.save(update_fields=["password"])
    elif ending == "deactivation":
        capable.is_active = False
        capable.save(update_fields=["is_active"])
    else:
        owner = User.objects.create_superuser("session-owner", "owner@example.invalid", PASSWORD)
        assert (
            len(end_account_sessions(actor=owner, account_id=capable.pk, reason="New shift.")) == 1
        )
    url = review_urls(patient_ticket)[0]
    answer = client.post(url)
    assert answer.status_code == 302 and urlsplit(answer["Location"]).path == reverse(
        "account_login"
    )
    if ending == "deactivation":
        capable.is_active = True
        capable.save(update_fields=["is_active"])
    fresh = Browser()
    fresh.force_login(capable)
    assert fresh.post(url, HTTP_HX_REQUEST="true").status_code == 204
    assert not review_reveals().exists()


def test_own_password_change_keeps_the_confirmed_browser_only(
    client, capable, patient_ticket, reveal_rule, isolated_limits
):
    other = Browser()
    for browser in (client, other):
        browser.force_login(capable)
        assert review_confirm(browser, confirm_password=PASSWORD).status_code == 302
    replacement = "fresh synthetic passphrase for this review"
    response = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": PASSWORD,
            "password1": replacement,
            "password2": replacement,
        },
    )
    assert response.status_code == 302
    capable.refresh_from_db()
    assert capable.check_password(replacement)
    url = review_urls(patient_ticket)[0]
    assert client.post(url, HTTP_HX_REQUEST="true").status_code == 200
    ended = other.post(url)
    assert ended.status_code == 302 and urlsplit(ended["Location"]).path == reverse("account_login")
    assert review_reveals().get().actor_id == capable.pk


@pytest.mark.confirmation
@pytest.mark.django_db
def test_reloading_pending_reveal_confirmation_after_another_tab_confirmed(client):
    """A tab waiting at confirmation can return once another tab has supplied the password."""
    from django.contrib.auth.models import Group
    from django.urls import reverse

    from accounts.models import User
    from core.models import RevealConfirmationRule

    password = "synthetic long passphrase for review"
    actor = User.objects.create_user("review-tab-confirmer", password=password)
    actor.groups.add(Group.objects.get_or_create(name="technical")[0])
    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    client.force_login(actor)
    confirm = reverse("account-confirm-reveal")
    back = reverse("dashboard")
    waiting = client.get(confirm, {"next": back})
    assert waiting.status_code == 200
    assert 'name="confirm_password"' in waiting.content.decode()

    other_tab = client.post(reverse("account_reauthenticate"), {"password": password})
    assert other_tab.status_code == 302
    reloaded = client.get(confirm, {"next": back})
    assert reloaded.status_code == 302, (
        reloaded.status_code,
        tuple(reloaded.context["form"].fields),
        reloaded.context["form"].confirming,
    )
    assert reloaded.url == back


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
@pytest.mark.parametrize("writer_first", [False, True], ids=["read-first", "save-first"])
def test_rule_save_and_reveal_decision_share_the_commit_boundary(staff, writer_first):
    """Observe PostgreSQL queuing the second operation until the first commits, in both orders."""
    from core import reveal_confirmation

    admin = staff("review-rule-author", "admin")
    reader = staff("review-rule-reader", "technical")
    waiting_pid = Queue(maxsize=1)

    def save():
        _, changed = reveal_confirmation.save_rule(
            actor=admin, first_reveal=True, expected_revision=0, reason="Synthetic review."
        )
        assert changed

    def read():
        user = hold_actor(reader)
        return reveal_confirmation.may_reveal(SimpleNamespace(user=user, session={}))

    def second():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '8s'")
                cursor.execute("SELECT pg_backend_pid()")
                waiting_pid.put(cursor.fetchone()[0])
                return read() if writer_first else save()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cursor:
            if writer_first:
                save()
            else:
                assert read() is True
            pending = pool.submit(second)
            pid = waiting_pid.get(timeout=6)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    break
                assert not pending.done(), "The second operation finished before the commit."
                sleep(0.02)
            else:
                pytest.fail("PostgreSQL did not report the expected queue at the rule lock.")
        if writer_first:
            assert pending.result(timeout=8) is False
        else:
            pending.result(timeout=8)
    rule = reveal_confirmation.rule()
    assert (rule.revision, rule.first_reveal) == (1, True)
    assert AuditEvent.objects.get(action=reveal_confirmation.ACTION).actor == admin


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_two_browsers_replacing_codes_record_a_contiguous_history(staff, settings):
    """Two ordinary Generate submissions produce one replacement and one notice per request."""
    from accounts.tests.conftest import PASSWORD

    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    staff_member = staff("review-code-history", "technical")
    TOTP.activate(staff_member, generate_totp_secret())
    oldest = RecoveryCodes.activate(staff_member).instance.pk
    browsers = [Browser(), Browser()]
    generate = reverse("mfa_generate_recovery_codes")
    for browser in browsers:
        browser.force_login(staff_member)
        assert (
            browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code
            == 302
        )
        assert browser.get(generate).status_code == 200
    assert browsers[0].session.session_key != browsers[1].session.session_key

    start = Barrier(2)

    def replace(browser):
        try:
            start.wait(timeout=6)
            response = browser.post(generate)
            assert (response.status_code, response.url) == (302, reverse("mfa_view_recovery_codes"))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(replace, browser) for browser in browsers]
        for request in requests:
            request.result(timeout=12)

    newest = Authenticator.objects.get(user=staff_member, type="recovery_codes").pk
    history = list(
        AuditEvent.objects.filter(action="account.recovery_codes_replaced").order_by("occurred_at")
    )
    assert len(history) == 2
    ids = [oldest]
    for event in history:
        assert event.actor == staff_member
        assert (event.object_type, event.object_id) == ("accounts.user", str(staff_member.pk))
        assert [factor["authenticator_id"] for factor in event.before["authenticators"]] == [
            ids[-1]
        ]
        (replacement,) = event.after["authenticators"]
        ids.append(replacement["authenticator_id"])
    assert len(set(ids)) == 3 and ids[-1] == newest
    notices = list(Notice.objects.filter(topic="accounts.security").order_by("pk"))
    assert len(notices) == 2
    for notice in notices:
        assert notice.recipient_id == notice.object_id == staff_member.pk
        assert notice.events == ["recovery_codes_replaced"]
        assert notice.data["by"] == "self"


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_admin_app_removal_and_self_code_replacement_agree_with_history(staff, settings):
    """Independent administration and self-service requests each account for their actual change."""
    from accounts.models import User
    from accounts.tests.conftest import PASSWORD

    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    staff_member = staff("review-mixed-changes", "technical")
    admin = User.objects.create_superuser(
        "review-mixed-admin", "review-admin@example.invalid", PASSWORD
    )
    app = TOTP.activate(staff_member, generate_totp_secret()).instance
    oldest = RecoveryCodes.activate(staff_member).instance.pk

    admin_browser, personal_browser = Browser(), Browser()
    admin_browser.force_login(admin)
    personal_browser.force_login(staff_member)
    assert (
        personal_browser.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code
        == 302
    )
    remove = reverse("admin:mfa_authenticator_delete", args=[app.pk])
    generate = reverse("mfa_generate_recovery_codes")
    assert admin_browser.get(remove).status_code == 200
    assert personal_browser.get(generate).status_code == 200
    start = Barrier(2)

    def submit(browser, url, data):
        try:
            start.wait(timeout=6)
            return browser.post(url, data).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        removal = pool.submit(
            submit, admin_browser, remove, {"post": "yes", confirmation.PASSWORD_FIELD: PASSWORD}
        )
        replacement = pool.submit(submit, personal_browser, generate, {})
        assert removal.result(timeout=12) == 302
        replaced = replacement.result(timeout=12)
    assert replaced in (200, 302)
    assert not Authenticator.objects.filter(pk=app.pk).exists()
    removed = AuditEvent.objects.get(action="account.authenticator_removed")
    assert removed.actor_id == admin.pk
    assert removed.object_id == str(staff_member.pk)
    assert removed.before["authenticator_id"] == app.pk

    current = Authenticator.objects.get(user=staff_member, type="recovery_codes")
    changes = AuditEvent.objects.filter(action="account.recovery_codes_replaced")
    expected = {("administrator", "authenticator_removed", "totp")}
    if replaced == 302:
        assert current.pk != oldest
        change = changes.get()
        assert (change.actor_id, change.object_id) == (staff_member.pk, str(staff_member.pk))
        assert [factor["authenticator_id"] for factor in change.before["authenticators"]] == [
            oldest
        ]
        assert [factor["authenticator_id"] for factor in change.after["authenticators"]] == [
            current.pk
        ]
        expected.add(("self", "recovery_codes_replaced", "recovery_codes"))
    else:
        assert current.pk == oldest and not changes.exists()

    notices = list(Notice.objects.filter(topic="accounts.security"))
    assert len(notices) == len(expected)
    actual = set()
    for notice in notices:
        assert notice.recipient_id == notice.object_id == staff_member.pk
        (event,) = notice.events
        (kind,) = notice.data["types"]
        actual.add((notice.data["by"], event, kind))
    assert actual == expected


REVIEW_NOTICE_PASSWORD = "Synthetic-factor-review-9084!"


@pytest.fixture
def notice_person(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.PUBLIC_URL = "https://review.example.invalid"
    return User.objects.create_user(
        "notice_review_person", "recipient@example.invalid", REVIEW_NOTICE_PASSWORD
    )


@pytest.fixture
def notice_admin_browser():
    admin = User.objects.create_superuser(
        "notice_review_admin", "admin@example.invalid", REVIEW_NOTICE_PASSWORD
    )
    browser = Browser()
    browser.force_login(admin)
    return browser


def notice_mark_page(browser, user):
    url = reverse("admin:accounts_user_compromised", args=[user.pk])
    page = browser.get(url)
    assert page.status_code == 200
    payload = {
        "drawn": page.context["form"].initial["drawn"],
        "reason": "Synthetic account holder requested help",
        "confirm_password": REVIEW_NOTICE_PASSWORD,
    }
    return page, url, payload


@pytest.mark.django_db
@pytest.mark.parametrize("condition", ["inactive", "no_address"])
def test_mark_form_only_promises_email_to_an_eligible_account(
    notice_person, notice_admin_browser, condition, django_capture_on_commit_callbacks
):
    if condition == "inactive":
        notice_person.is_active = False
        notice_person.save(update_fields=["is_active"])
    else:
        notice_person.email = ""
        notice_person.save(update_fields=["email"])

    page, url, payload = notice_mark_page(notice_admin_browser, notice_person)
    form_html = page.content.decode().split('id="content-main">', 1)[1].split("</form>", 1)[0]
    with django_capture_on_commit_callbacks(execute=True):
        assert notice_admin_browser.post(url, payload).status_code == 302
    assert AuditEvent.objects.filter(action="account.password_marked_compromised").count() == 1
    assert mail.outbox == []
    assert "The person is sent an email notice" not in form_html


@pytest.mark.django_db
def test_mark_help_agrees_with_the_form_and_successful_delivery(
    notice_person, notice_admin_browser, django_capture_on_commit_callbacks
):
    page, url, payload = notice_mark_page(notice_admin_browser, notice_person)
    with django_capture_on_commit_callbacks(execute=True):
        assert notice_admin_browser.post(url, payload).status_code == 302
    if mail.outbox:
        assert [message.to for message in mail.outbox] == [[notice_person.email]]
        assert "The person is sent an email notice" in page.content.decode()
        help_text = json.dumps(page.context["page_documentation"])
        assert "The page itself still says the person is not emailed" not in help_text
    else:
        # A deployment before notices were introduced told the administrator to make contact.
        assert not Notice.objects.filter(topic="accounts.security").exists()
        assert "The person is not emailed: tell them yourself." in page.content.decode()


def notice_person_browser(user):
    browser = Browser()
    browser.force_login(user)
    response = browser.post(reverse("account_reauthenticate"), {"password": REVIEW_NOTICE_PASSWORD})
    assert response.status_code == 302
    return browser


@pytest.mark.django_db
def test_adding_an_app_with_retained_codes_does_not_promise_new_codes(
    notice_person, notice_admin_browser, django_capture_on_commit_callbacks
):

    app = TOTP.activate(notice_person, generate_totp_secret()).instance
    codes = RecoveryCodes.activate(notice_person).instance
    url = reverse("admin:mfa_authenticator_delete", args=[app.pk])
    with django_capture_on_commit_callbacks(execute=True):
        assert (
            notice_admin_browser.post(
                url, {"post": "yes", "confirm_password": REVIEW_NOTICE_PASSWORD}
            ).status_code
            == 302
        )
    assert Authenticator.objects.filter(user=notice_person, pk=codes.pk).exists()
    mail.outbox.clear()

    browser = notice_person_browser(notice_person)
    url = reverse("mfa_activate_totp")
    page = browser.get(url)
    assert page.status_code == 200
    secret = browser.session[SECRET_SESSION_KEY]
    code = code_with_time_left(secret)
    with django_capture_on_commit_callbacks(execute=True):
        response = browser.post(url, {"code": code})
    assert response.status_code == 302 and response.url == reverse("mfa_index")
    kept = Authenticator.objects.get(user=notice_person, type="recovery_codes")
    assert kept.pk == codes.pk and kept.data == codes.data
    if Notice.objects.filter(topic="accounts.security").exists():
        notice = Notice.objects.get(events=["authenticator_added"], recipient=notice_person)
        assert notice.data["types"] == ["totp"]
        assert len(mail.outbox) == 1 and mail.outbox[0].to == [notice_person.email]
        assert "new recovery codes were issued" not in mail.outbox[0].body.lower()
    help_text = json.dumps(page.context["page_documentation"])
    assert "Adding an app sends one notice, which also says new recovery codes were issued" not in (
        help_text
    )


@pytest.mark.django_db
@pytest.mark.parametrize("contact_change", ["replaced", "cleared"])
def test_retry_uses_current_account_address_username_and_contact(
    notice_person, notice_admin_browser, contact_change, monkeypatch
):
    contact = LabContact.objects.create(
        kind=LabContact.Kind.SECURITY, name="Former Office", email="former-office@example.invalid"
    )
    page, url, payload = notice_mark_page(notice_admin_browser, notice_person)
    assert page.status_code == 200
    assert notice_admin_browser.post(url, payload).status_code == 302
    notice = Notice.objects.get(topic="accounts.security", recipient=notice_person)
    with monkeypatch.context() as patch:
        patch.setattr(outbox.EmailMessage, "send", lambda self: 0)
        assert outbox.attempt(notice.pk) == "retrying"

    notice_person.username = "renamed_review_person"
    notice_person.email = "new-recipient@example.invalid"
    notice_person.save(update_fields=["username", "email"])
    contact.name = "New Desk" if contact_change == "replaced" else ""
    contact.email = "new-desk@example.invalid" if contact_change == "replaced" else ""
    contact.save()
    notice.refresh_from_db()
    assert outbox.attempt(notice.pk, now=notice.next_attempt_at) == "sent"
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [notice_person.email] and sent.cc == [] and sent.bcc == []
    assert notice_person.username in sent.body and "notice_review_person" not in sent.body
    assert "Former Office" not in sent.body and "former-office@" not in sent.body
    expected = (
        "New Desk (new-desk@example.invalid)" if contact.name else "your laboratory's administrator"
    )
    assert expected in sent.body
    assert payload["reason"] not in sent.body and REVIEW_NOTICE_PASSWORD not in sent.body


@pytest.mark.django_db
@pytest.mark.xfail(
    strict=True,
    reason="Accepted limit (accounts/mfa_views.py docstring): allauth's Deactivate sends its "
    "authenticator_removed signals as it removes, now inside the view's transaction, so a "
    "receiver can see a removal that rolls back. Nothing in the application receives it.",
)
def test_rolled_back_app_removal_does_not_announce_a_removal(
    notice_person, django_capture_on_commit_callbacks
):
    from allauth.mfa.signals import authenticator_removed

    TOTP.activate(notice_person, generate_totp_secret())
    RecoveryCodes.activate(notice_person)
    browser = notice_person_browser(notice_person)
    factors = Authenticator.objects.filter(user=notice_person)
    before = set(factors.values_list("pk", flat=True))
    announced = []

    def receive(sender, authenticator, **kwargs):
        announced.append(authenticator.type)

    authenticator_removed.connect(receive)
    try:
        with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            response = browser.post(reverse("mfa_deactivate_totp"))
            assert response.status_code == 302 and response.url == reverse("mfa_index")
            assert not factors.exists()
            transaction.set_rollback(True)
    finally:
        authenticator_removed.disconnect(receive)
    assert set(factors.values_list("pk", flat=True)) == before
    assert not AuditEvent.objects.filter(action="account.authenticator_removed").exists()
    assert not Notice.objects.exists() and mail.outbox == []
    assert announced == []


@pytest.mark.django_db
def test_viewing_and_downloading_the_existing_recovery_set_does_not_send_email(
    notice_person, django_capture_on_commit_callbacks
):
    TOTP.activate(notice_person, generate_totp_secret())
    codes = RecoveryCodes.activate(notice_person)
    unused = codes.get_unused_codes()
    browser = notice_person_browser(notice_person)
    with django_capture_on_commit_callbacks(execute=True):
        viewed = browser.get(reverse("mfa_view_recovery_codes"))
        downloaded = browser.get(reverse("mfa_download_recovery_codes"))
    assert viewed.status_code == downloaded.status_code == 200
    assert "attachment" in downloaded["Content-Disposition"]
    for code in unused:
        assert code in viewed.content.decode() and code in downloaded.content.decode()
    kept = Authenticator.objects.get(user=notice_person, type="recovery_codes")
    assert kept.pk == codes.instance.pk and kept.wrap().get_unused_codes() == unused
    assert not AuditEvent.objects.filter(action="account.recovery_codes_replaced").exists()
    assert not Notice.objects.filter(recipient=notice_person).exists() and not mail.outbox


@pytest.mark.django_db(transaction=True)
def test_upgrade_keeps_old_ticket_notice_deliverable_and_reveal_setting_off(settings):
    """Deliver an assignment queued on the previous schema after applying core.0008."""
    from django.core import mail
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    from core import reveal_confirmation
    from core.models import RevealConfirmationRule
    from jobs import outbox
    from jobs.models import Notice

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.PUBLIC_URL = "https://example.invalid"
    executor = MigrationExecutor(connection)
    head = executor.loader.graph.leaf_nodes()
    previous = [
        ("core", "0007_shared_cache_table") if app == "core" else (app, name) for app, name in head
    ]
    try:
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        assert "core_revealconfirmationrule" not in connection.introspection.table_names()
        user = apps.get_model("accounts", "User").objects.create(
            username="older-assignee", email="assignee@example.invalid", password="!"
        )
        role, _ = apps.get_model("auth", "Group").objects.get_or_create(name="technical")
        user.groups.add(role)
        team = apps.get_model("teams", "Team").objects.create(
            slug="upgrade-team", name="Upgrade Team"
        )
        apps.get_model("teams", "TeamMembership").objects.create(team=team, user=user)
        category = apps.get_model("taxonomy", "RequestCategory").objects.create(
            key="upgrade-category", name="Upgrade category"
        )
        ticket = apps.get_model("desk", "ServiceRequest").objects.create(
            number="REQ-900910",
            subject="Historical synthetic ticket",
            channel="phone",
            team=team,
            category=category,
            created_by=user,
            assignee=user,
        )
        queued = apps.get_model("jobs", "Notice").objects.create(
            recipient=user,
            topic="desk.request",
            object_id=ticket.pk,
            reference=ticket.number,
            events=["assigned"],
            data={"portal": False},
            next_attempt_at=timezone.now(),
        )
    finally:
        MigrationExecutor(connection).migrate(head)

    assert not RevealConfirmationRule.objects.exists()
    assert reveal_confirmation.rule().first_reveal is False
    assert reveal_confirmation.rule().revision == 0
    assert outbox.deliver_due()["sent"] == 1
    delivered = Notice.objects.get(pk=queued.pk)
    assert delivered.status == NoticeStatus.SENT
    assert delivered.attempts == 1
    assert [message.to for message in mail.outbox] == [[user.email]]
    assert ticket.number in mail.outbox[0].body
    assert "A ticket has been assigned to you." in mail.outbox[0].body


@pytest.mark.confirmation
@pytest.mark.django_db
def test_code_only_confirm_help_does_not_instruct_staff_to_type_a_missing_password(client):
    """The quick instructions match the credentials that the form offers and accepts."""
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
    from django.contrib.auth.models import Group
    from django.urls import reverse

    from accounts.models import User
    from core.models import RevealConfirmationRule
    from core.reveal_confirmation import SESSION_KEY

    actor = User.objects.create_user("review-code-only", password=None)
    actor.groups.add(Group.objects.get_or_create(name="technical")[0])
    assert not actor.has_usable_password()
    TOTP.activate(actor, generate_totp_secret())
    codes = RecoveryCodes.activate(actor)
    RevealConfirmationRule.objects.create(pk=1, first_reveal=True, revision=1)
    client.force_login(actor)
    path = reverse("account-confirm-reveal")
    page = client.get(path)
    assert page.status_code == 200
    assert "confirm_password" not in page.context["form"].fields
    assert "confirm_code" in page.context["form"].fields
    accepted = client.post(path, {"confirm_code": codes.get_unused_codes()[0]})
    assert accepted.status_code == 302
    assert client.session.get(SESSION_KEY)[0] == actor.pk

    instructions = page.context["page_documentation"]["tasks"]
    steps = [step for task in instructions for step in task["steps"]]
    assert (
        "Type [[Your password]], and a code in [[Authenticator or recovery code]] "
        "if it asks for one."
    ) not in steps, "The help tells this account to type a password it does not have."


def test_new_account_session_clears_the_previous_reveal_metadata(rf, staff):
    """Each recorded sign-in clears per-session reveal metadata, as the release note promises."""
    from accounts import sessions

    actor = staff("review-session-start", "technical")
    request = rf.get("/")
    request.session = Browser().session
    sessions.start_session(None, request, actor)
    first = request.session[sessions.SESSION_KEY]
    # A harmless sentinel distinguishes a cleared key from copied metadata.
    request.session["_reveal_confirmed"] = "prior-session-metadata"
    sessions.start_session(None, request, actor)
    assert request.session[sessions.SESSION_KEY] != first
    assert "_reveal_confirmed" not in request.session


def test_unchanged_deployment_reveals_and_audits_both_types(client, capable, patient_ticket):
    client.force_login(capable)
    for url, value in zip(
        review_urls(patient_ticket), (PATIENT_SENTINEL, PATIENT_KEY), strict=True
    ):
        response = client.post(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 200 and value.encode() in response.content
        assert "no-store" in response["Cache-Control"]
    assert sorted(review_reveals().values_list("action", flat=True)) == sorted(REVEAL_EVENTS)


def test_waiting_reveal_does_not_query_the_protected_records(
    client, capable, patient_ticket, reveal_rule
):
    from django.test.utils import CaptureQueriesContext

    from links.models import ExternalRecordLink, LinkAttachment

    client.force_login(capable)
    for url in review_urls(patient_ticket):
        with CaptureQueriesContext(connection) as statements:
            response = client.post(url, HTTP_HX_REQUEST="true")
        assert response.status_code == 204 and response.content == b""
        for model in (FieldValue, ExternalRecordLink, LinkAttachment):
            assert not any(
                connection.ops.quote_name(model._meta.db_table) in statement["sql"]
                for statement in statements.captured_queries
            ), model._meta.label
    assert not review_reveals().exists()


@pytest.mark.parametrize(
    "back", ["/requests/?label=two%20words", "/requests/?a=1&a=2", "/requests/?q=%2F"]
)
def test_confirmation_preserves_local_encoded_filter_and_repeated_query_values(
    client, capable, reveal_rule, isolated_limits, back
):
    client.force_login(capable)
    url = reverse("account-confirm-reveal")
    page = client.get(url, {"next": "http://testserver" + back})
    assert page.status_code == 200 and page.context["cancel_url"] == back
    result = client.post(url, {"next": back, "confirm_password": PASSWORD})
    assert result.status_code == 302 and result["Location"] == back


def test_rule_form_can_be_stale_without_its_inputs_or_an_extra_event_being_saved(
    client, staff, isolated_limits
):
    from core import reveal_confirmation

    original, editor = staff("review-form-owner", "admin"), staff("review-form-editor", "admin")
    second = Browser()
    client.force_login(original)
    second.force_login(editor)
    edit = reverse("reveal-confirmation-edit")
    form = client.get(edit).context["form"]
    revision = form.initial["expected_revision"]
    assert revision == 0 and not form.initial["first_reveal"]
    result = second.post(
        edit,
        {
            "expected_revision": revision,
            "first_reveal": "on",
            "reason": "Policy for the new shift.",
            "confirm_password": PASSWORD,
        },
    )
    assert result.status_code == 302
    retained = "The original operator's reason."
    refused = client.post(
        edit, {"expected_revision": revision, "reason": retained, "confirm_password": PASSWORD}
    )
    assert refused.status_code == 200 and "changed after you opened" in refused.content.decode()
    form = refused.context["form"]
    assert form["reason"].value() == retained and not form["first_reveal"].value()
    assert int(form["expected_revision"].value()) == revision
    current = client.get(edit).context["form"].initial
    assert current["expected_revision"] == 1 and current["first_reveal"]
    unchanged = client.post(edit, {"expected_revision": 1, "first_reveal": "on", "reason": "Keep."})
    assert unchanged.status_code == 302
    event = AuditEvent.objects.get(action=reveal_confirmation.ACTION)
    assert event.actor_id == editor.pk and event.reason == "Policy for the new shift."
    assert event.before == {"first_reveal": False} and event.after == {"first_reveal": True}


@pytest.mark.parametrize("role", ROLES)
def test_edit_form_checks_the_role_on_open_and_save(client, staff, reveal_rule, role):
    actor = staff("review-roles-form", role)
    client.force_login(actor)
    edit = reverse("reveal-confirmation-edit")
    allowed = role == "admin"
    assert client.get(edit).status_code == (200 if allowed else 403)
    response = client.post(
        edit,
        {
            "first_reveal": "on",
            "expected_revision": 1,
            "reason": "Retain setting.",
            "confirm_password": PASSWORD,
        },
    )
    assert response.status_code == (302 if allowed else 403)
    assert not AuditEvent.objects.filter(action="lab.reveal_confirmation_changed").exists()


def test_changing_account_in_a_browser_starts_a_distinct_reveal_session(
    client, capable, make_user, patient_ticket, reveal_rule, isolated_limits
):
    client.force_login(capable)
    assert review_confirm(client, confirm_password=PASSWORD).status_code == 302
    assert client.post(reverse("account_logout")).status_code == 302
    colleague = make_user("review-different-reader", "technical")
    review_grant(colleague)
    signed_in = client.post(
        reverse("account_login"), {"login": colleague.username, "password": PASSWORD}
    )
    assert signed_in.status_code == 302
    assert "_reveal_confirmed" not in client.session
    for url in review_urls(patient_ticket):
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 204
    assert not review_reveals().exists()


@pytest.mark.parametrize(
    "route", ["mfa_activate_totp", "mfa_generate_recovery_codes", "mfa_deactivate_totp"]
)
def test_safe_methods_on_factor_forms_preserve_factors_notices_and_history(notice_person, route):
    if route != "mfa_activate_totp":
        TOTP.activate(notice_person, generate_totp_secret())
        RecoveryCodes.activate(notice_person)
    browser = notice_person_browser(notice_person)
    url = reverse(route)
    factors = Authenticator.objects.filter(user=notice_person).order_by("pk")
    before = list(factors.values_list("pk", "type", "data"))
    canonical = browser.get(url.rstrip("/"))
    assert canonical.status_code in (301, 308) and canonical["Location"] == url
    head = browser.head(url)
    assert head.status_code == 200 and not head.content
    options = browser.options(url)
    assert options.status_code == 200 and "POST" in options["Allow"]
    assert list(factors.values_list("pk", "type", "data")) == before
    assert not AuditEvent.objects.filter(
        action__in=(
            "account.authenticator_added",
            "account.recovery_codes_replaced",
            "account.authenticator_removed",
        )
    ).exists()
    assert not Notice.objects.filter(recipient=notice_person).exists() and not mail.outbox


@pytest.mark.parametrize(
    "route", ["mfa_activate_totp", "mfa_generate_recovery_codes", "mfa_deactivate_totp"]
)
def test_shared_demo_accounts_keep_their_published_factors(client, make_user, route):
    actor = make_user("review-shared-credentials", "technical", credentials_locked=True)
    client.force_login(actor)
    for method in [client.get, client.post]:
        response = method(reverse(route))
        assert response.status_code == 302 and response["Location"] == reverse("dashboard")
    assert not Authenticator.objects.filter(user=actor).exists()
    assert not Notice.objects.filter(recipient=actor).exists()


def test_activation_form_keeps_allauths_code_validation_and_rate_limit(
    notice_person, isolated_limits, settings
):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    browser = notice_person_browser(notice_person)
    activate = reverse("mfa_activate_totp")
    assert browser.get(activate).status_code == 200
    for expected in ("incorrect_code", "rate_limited"):
        refused = browser.post(activate, {"code": "invalid-synthetic-code"})
        assert refused.status_code == 200
        assert refused.context["form"].has_error("code", expected)
    assert not Authenticator.objects.filter(user=notice_person).exists()
    assert not AuditEvent.objects.filter(action="account.authenticator_added").exists()
    assert not Notice.objects.filter(recipient=notice_person).exists() and not mail.outbox


def test_setting_opened_as_an_administrator_is_refused_after_the_role_is_removed(
    client, staff, reveal_rule
):
    actor = staff("review-form-role-change", "admin")
    client.force_login(actor)
    edit = reverse("reveal-confirmation-edit")
    drawn = client.get(edit).context["form"].initial
    actor.groups.remove(actor.groups.get(name="admin"))
    refused = client.post(
        edit,
        {
            "expected_revision": drawn["expected_revision"],
            "reason": "Review sample.",
            "confirm_password": PASSWORD,
        },
    )
    assert refused.status_code == 403
    reveal_rule.refresh_from_db()
    assert reveal_rule.first_reveal and reveal_rule.revision == 1
    assert not AuditEvent.objects.filter(action="lab.reveal_confirmation_changed").exists()
