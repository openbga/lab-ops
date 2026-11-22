"""Independent checks of the session release. All accounts and work records are synthetic.

Written by the independent reviewer (round 1, .local/review-history/session-limits-round-1.md);
adopted unchanged except where a test says it was adapted."""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.utils import parsedate_to_datetime
from queue import Queue
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.signals import user_logged_in
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import call_command
from django.db import close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.functions import Now
from django.templatetags.static import static
from django.test import Client
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent, Lab, LabLogo
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
NEW_PASSWORD = "review-example-passphrase-852!"
FORM_DATA = {
    "inactivity_minutes": "15",
    "maximum_hours": "8",
    "reason": "Example front desk hours",
    "expected_revision": "0",
}


@pytest.fixture
def person(staff):
    return staff("review-staff", "client_services")


@pytest.fixture
def admin(staff):
    return staff("review-admin", "admin")


@pytest.fixture
def owner():
    return User.objects.create_superuser("review-owner", "owner@example.invalid", PASSWORD)


def signed_in(user):
    browser = Client()
    browser.force_login(user)
    return browser


def rows(user):
    return user.account_sessions.all()


def login(browser, user, **extra):
    response = browser.post(
        reverse("account_login"), {"login": user.username, "password": PASSWORD, **extra}
    )
    assert response.status_code == 302
    return response


def set_idle(user, minutes):
    assert (
        rows(user)
        .filter(ended_at__isnull=True)
        .update(last_active_at=Now() - timedelta(minutes=minutes))
        == 1
    )


@pytest.mark.parametrize("path,code", [("/requests", 301), ("/accounts/", 302)])
def test_final_redirect_for_signed_in_person_has_no_store(person, path, code):
    response = signed_in(person).get(path)
    assert response.status_code == code
    assert "no-store" in response.get("Cache-Control", "")


def test_session_save_error_with_the_account_shell_has_no_store(person, monkeypatch, settings):
    # Exercise the real error handler, using a session-store failure at the final save.
    settings.DEBUG = False
    browser = Client()
    login(browser, person)
    save = SessionStore.save

    def fail_update(store, must_create=False):
        if must_create:
            return save(store, must_create=True)
        raise UpdateError

    monkeypatch.setattr(SessionStore, "save", fail_update)
    response = browser.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert response.status_code == 400
    assert person.username.encode() in response.content
    assert "no-store" in response.get("Cache-Control", "")


@pytest.mark.django_db(transaction=True)
def test_admin_self_service_password_form_preserves_an_updated_first_name(owner, monkeypatch):
    # The name edit commits on a second connection after the password form has read its user.
    browser = signed_in(owner)
    save = PasswordChangeForm.save

    def change_name():
        try:
            return User.objects.filter(pk=owner.pk).update(first_name="Updated name")
        finally:
            connections.close_all()

    def save_after_name_edit(form, *args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(change_name).result(timeout=10) == 1
        return save(form, *args, **kwargs)

    monkeypatch.setattr(PasswordChangeForm, "save", save_after_name_edit)
    response = browser.post(
        reverse("admin:password_change"),
        {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    )
    assert response.status_code == 302
    owner.refresh_from_db()
    assert owner.check_password(NEW_PASSWORD)
    assert owner.first_name == "Updated name"


@pytest.mark.parametrize("signed_in_for_reset", [False, True], ids=["anonymous", "signed-in"])
def test_email_reset_closes_all_account_rows(person, mailoutbox, signed_in_for_reset):
    sibling = signed_in(person)
    current = signed_in(person)
    requester = Client()
    sent = requester.post(reverse("account_reset_password"), {"email": person.email})
    assert sent.status_code == 302 and len(mailoutbox) == 1
    url = re.search(r"https?://[^\s<>]+/password/reset/key/[^\s<>]+", mailoutbox[0].body)
    assert url
    browser = current if signed_in_for_reset else requester
    start = browser.get(urlsplit(url[0]).path)
    assert start.status_code == 302
    response = browser.post(start.url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)
    endings_when_reset = list(rows(person).values_list("ending", flat=True))
    for old_browser in (sibling, current):
        assert old_browser.get("/").url == reverse("account_login") + "?next=/"
    assert endings_when_reset == ["new_password"] * 2


@pytest.mark.parametrize("how", ["new-password", "deactivated", "other-tab-logout"])
def test_closed_browser_session_sends_dialog_to_its_parent_page(person, how):
    browser = signed_in(person)
    if how == "other-tab-logout":
        tab = Client()
        tab.cookies = browser.cookies.copy()
        assert tab.post(reverse("account_logout")).status_code == 302
    elif how == "deactivated":
        person.is_active = False
        person.save(update_fields=["is_active"])
    else:
        changer = signed_in(person)
        changed = changer.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
        assert changed.status_code == 302
    page = reverse("desk:request-list")
    response = browser.get(
        reverse("desk:request-create"),
        headers={"X-VR-Form": "1", "Referer": "http://testserver" + page},
    )
    assert (response.status_code, response.get("HX-Redirect")) == (
        401,
        reverse("account_login") + "?next=" + page,
    )


def test_remembered_cookie_follows_the_last_save_and_the_longest_session_follows_sign_in(
    person, monkeypatch, settings
):
    """Adapted (finding 12 was resolved by correcting the Security settings wording): a session
    saved again (confirming the password) renews its cookie, as the page now says, and the
    longest session still ends counted from sign-in, whatever the cookie says."""
    from django.contrib.sessions import middleware

    from core import security_settings

    browser = Client()
    first = login(browser, person, remember="on")
    cookie = settings.SESSION_COOKIE_NAME
    first_expiry = parsedate_to_datetime(first.cookies[cookie]["expires"])
    clock = middleware.time.time()
    monkeypatch.setattr(middleware, "time", SimpleNamespace(time=lambda: clock + 600))
    response = browser.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert response.status_code == 302
    assert parsedate_to_datetime(response.cookies[cookie]["expires"]) > first_expiry
    note = next(
        fact.note
        for group in security_settings.in_force()
        for fact in group.facts
        if fact.key == "session-length"
    )
    assert "after the session was last saved" in note and "after sign-in at the latest" not in note
    rows(person).update(started_at=Now() - timedelta(hours=12, seconds=1))
    assert browser.get("/").url == reverse("account_login") + "?next=/"
    assert rows(person).get().ending == "maximum"


@pytest.mark.parametrize(
    "field,value,text",
    [
        ("inactivity_minutes", "4", "Choose from 5 to 60 minutes."),
        ("maximum_hours", "25", "Choose from 1 to 24 hours."),
    ],
)
def test_form_reports_range_as_described_in_its_help(admin, field, value, text):
    browser = signed_in(admin)
    response = browser.post(
        reverse("session-limits-edit", args=["staff"]), FORM_DATA | {field: value}
    )
    assert response.status_code == 200
    assert not AuditEvent.objects.filter(action="lab.session_limits_changed").exists()
    assert text in response.context["form"].errors[field]


@pytest.mark.parametrize(
    "field,value", [("inactivity_minutes", ""), ("maximum_hours", "8.5"), ("reason", "")]
)
def test_invalid_form_preserves_the_input_and_history(admin, field, value):
    browser = signed_in(admin)
    response = browser.post(
        reverse("session-limits-edit", args=["portal"]), FORM_DATA | {field: value}
    )
    form = response.context["form"]
    assert response.status_code == 200 and field in form.errors
    assert form[field].value() == value
    assert not AuditEvent.objects.filter(action="lab.session_limits_changed").exists()


def test_two_editors_use_the_revisions_their_forms_actually_rendered(admin, staff):
    from core.models import SessionLimit

    first, second = signed_in(admin), signed_in(staff("review-second", "admin"))
    url = reverse("session-limits-edit", args=["staff"])
    old = second.get(url).context["form"]["expected_revision"].value()
    revision = first.get(url).context["form"]["expected_revision"].value()
    assert first.post(url, FORM_DATA | {"expected_revision": revision}).status_code == 302
    response = second.post(url, FORM_DATA | {"expected_revision": old, "inactivity_minutes": "21"})
    form = response.context["form"]
    assert form["inactivity_minutes"].value() == "21" and form.non_field_errors()
    assert SessionLimit.objects.get().inactivity_minutes == 15
    assert AuditEvent.objects.filter(action="lab.session_limits_changed").count() == 1


@pytest.mark.parametrize("who", ["anonymous", "unassigned", "superuser", "portal"])
def test_direct_limits_addresses_for_accounts_without_a_lab_role(
    who, owner, client_org, portal_user
):
    if who == "anonymous":
        browser = Client()
    else:
        user = {
            "unassigned": lambda: User.objects.create_user("review-unassigned"),
            "superuser": lambda: owner,
            "portal": lambda: portal_user(client_org),
        }[who]()
        browser = signed_in(user)
    for audience in ("staff", "portal", "unknown"):
        address = reverse("session-limits-edit", args=[audience])
        for response in (browser.get(address), browser.post(address, FORM_DATA)):
            assert response.status_code == (302 if who == "anonymous" else 403)
    assert browser.get(reverse("security-settings")).status_code == (
        302 if who == "anonymous" else 403
    )
    assert not AuditEvent.objects.filter(action="lab.session_limits_changed").exists()


@pytest.mark.parametrize("which", ["inactivity", "maximum"])
@pytest.mark.parametrize("offset_us", [-1, 0, 1])
def test_exact_time_boundaries(person, monkeypatch, which, offset_us):
    """Adapted after verification (finding 4): a row found expired is locked and read again
    before it is ended, by its values as they stand, as of the moment the request was read. So
    the row's times are set in the database and the reading's clock is placed `offset_us`
    microseconds before the limit's deadline (after it when negative)."""
    from accounts import sessions

    browser = signed_in(person)
    if which == "maximum":
        rows(person).update(
            started_at=Now() - timedelta(hours=12) + timedelta(minutes=10), last_active_at=Now()
        )
    else:
        rows(person).update(last_active_at=Now() - timedelta(minutes=10))
    original = sessions._read

    def reading(session_id, user):
        uid, start, last, end, now, minutes, hours = original(session_id, user)
        deadline = (
            start + timedelta(hours=hours)
            if which == "maximum"
            else last + timedelta(minutes=minutes)
        )
        return uid, start, last, end, deadline - timedelta(microseconds=offset_us), minutes, hours

    monkeypatch.setattr(sessions, "_read", reading)
    answer = browser.get(reverse("account-session")).json()
    assert answer["signed_in"] is (offset_us > 0)
    assert rows(person).get().ending == ("" if offset_us > 0 else which)


@pytest.mark.parametrize("audience", ["staff", "portal"])
def test_existing_session_uses_longer_limit_for_its_current_audience(
    person, client_org, portal_user, audience
):
    from core.models import SessionLimit

    user = person if audience == "staff" else portal_user(client_org)
    browser = signed_in(user)
    row = rows(user).get()
    set_idle(user, 25)
    other = "portal" if audience == "staff" else "staff"
    SessionLimit.objects.create(audience=other, inactivity_minutes=5, maximum_hours=1)
    SessionLimit.objects.create(audience=audience, inactivity_minutes=45, maximum_hours=12)
    status = reverse("account-session")
    assert 1190 < browser.get(status).json()["inactivity_seconds"] < 1210
    SessionLimit.objects.filter(audience=audience).update(inactivity_minutes=5)
    assert browser.get(status).json()["signed_in"] is False
    row.refresh_from_db()
    assert row.ending == "inactivity"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "same_session", [True, False], ids=["one-session-two-tabs", "two-sessions"]
)
def test_simultaneous_expiries_count_each_ended_session_once(
    person, caplog, monkeypatch, same_session
):
    from accounts import sessions

    first = signed_in(person)
    if same_session:
        second = Client()
        second.cookies = first.cookies.copy()
    else:
        second = signed_in(person)
    count = 1 if same_session else 2
    assert rows(person).update(last_active_at=Now() - timedelta(minutes=31)) == count
    both_read = Barrier(2, timeout=10)
    original = sessions._read

    def reading(session_id, user):
        result = original(session_id, user)
        both_read.wait()
        return result

    def status(browser):
        close_old_connections()
        try:
            return browser.get(reverse("account-session")).json()["signed_in"]
        finally:
            connections.close_all()

    monkeypatch.setattr(sessions, "_read", reading)
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        assert list(pool.map(status, [first, second])) == [False, False]
    assert rows(person).filter(ending="inactivity").count() == count
    messages = [r.getMessage() for r in caplog.records if r.name == "lab_ops.signin"]
    assert messages.count(f"signout.inactivity user={person.username} sessions=1") == count


def test_password_rollback_restores_both_open_sessions(
    person, caplog, django_capture_on_commit_callbacks
):
    for _ in range(2):
        signed_in(person)
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        with transaction.atomic():
            person.set_password(NEW_PASSWORD)
            person.save(update_fields=["password"])
            assert rows(person).filter(ending="new_password").count() == 2
            transaction.set_rollback(True)
    person.refresh_from_db()
    assert (
        person.check_password(PASSWORD) and rows(person).filter(ended_at__isnull=True).count() == 2
    )
    assert "signout.new_password" not in caplog.text


def test_allauth_set_first_password_keeps_the_acting_session(person):
    from accounts.sessions import SESSION_KEY

    person.set_unusable_password()
    person.save(update_fields=["password"])
    current = signed_in(person)
    own = current.session[SESSION_KEY]
    signed_in(person)
    changed = current.post(
        reverse("account_set_password"), {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    )
    assert changed.status_code == 302
    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)
    assert (
        rows(person).get(pk=own).ending == ""
        and rows(person).exclude(pk=own).get().ending == "new_password"
    )
    assert current.get(reverse("dashboard")).status_code == 200


def test_demo_reset_and_rerun_handle_open_sessions_and_saved_limits(monkeypatch):
    from core import demo, session_limits
    from core.models import SessionLimit

    monkeypatch.setenv("DEMO_PASSWORD", NEW_PASSWORD)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", verbosity=0)
    user = User.objects.get(username="demo_admin")
    signed_in(user)
    session_limits.save_limits(
        actor=user,
        audience="staff",
        new={"inactivity_minutes": 20, "maximum_hours": 9},
        expected_revision=0,
        reason="Example reception hours",
    )
    assert {rows(user).model._meta.db_table, SessionLimit._meta.db_table} <= set(
        demo.nonempty_tables()
    )
    call_command("seed_demo", verbosity=0)
    assert rows(user).get().ending == "" and SessionLimit.objects.get().revision == 1
    call_command("reset_demo", interactive=False, verbosity=0)
    assert not SessionLimit.objects.exists() and not rows(user).model.objects.exists()
    assert User.objects.filter(username="demo_admin").exists()


def test_upgrade_from_previous_schema_retires_legacy_login(person):
    from accounts import sessions
    from accounts.models import AccountSession
    from core.models import SessionLimit

    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    old = [("accounts", "0002_ticket_permission_labels"), ("core", "0004_labcontact")]
    executor.migrate(old)
    try:
        # At the previous schema the login signal only had the pre-release receivers.
        uid = "lab_ops_account_session_start"
        assert user_logged_in.disconnect(dispatch_uid=uid)
        try:
            browser = signed_in(person)
        finally:
            user_logged_in.connect(sessions.start_session, dispatch_uid=uid)
        assert browser.session.get("_auth_user_id") == str(person.pk)
        assert sessions.SESSION_KEY not in browser.session
    finally:
        MigrationExecutor(connection).migrate(targets)
    assert not AccountSession.objects.exists() and not SessionLimit.objects.exists()
    first = browser.get(reverse("dashboard"), follow=True)
    assert first.redirect_chain == [(reverse("account_login") + "?next=/", 302)]
    assert [str(msg) for msg in first.context["messages"]] == [sessions.ENDED_MESSAGE]
    login(browser, person)
    assert browser.get(reverse("dashboard")).status_code == 200 and rows(person).count() == 1


def test_logo_static_and_signed_in_partial_keep_their_expected_cache_policy(admin):
    from core import lab_profile
    from tests.test_lab_profile import image_file

    Lab.objects.create(name="Example Laboratory")
    browser = signed_in(admin)
    lab_profile.set_logo(actor=admin, uploaded=image_file())
    logo = browser.get(reverse("lab-logo-image", args=[LabLogo.objects.get().sha256]))
    assert (
        logo.status_code == 200 and logo["Cache-Control"] == "private, max-age=31536000, immutable"
    )
    asset = browser.get(static("js/app.js"))
    assert (
        asset.status_code == 200
        and "max-age" in asset["Cache-Control"]
        and "no-store" not in asset["Cache-Control"]
    )
    partial = browser.get(
        reverse("session-limits-edit", args=["staff"]), headers={"X-VR-Form": "1"}
    )
    assert partial.status_code == 200 and "no-store" in partial["Cache-Control"]


def test_declining_unchanged_limits_records_nothing_and_shows_notice(admin):
    from core.models import SessionLimit

    response = signed_in(admin).post(
        reverse("session-limits-edit", args=["staff"]),
        FORM_DATA | {"inactivity_minutes": "30", "maximum_hours": "12"},
        follow=True,
    )
    assert response.redirect_chain == [(reverse("security-settings"), 302)]
    assert "No changes to save." in [str(message) for message in response.context["messages"]]
    assert (
        not SessionLimit.objects.exists()
        and not AuditEvent.objects.filter(action="lab.session_limits_changed").exists()
    )


@pytest.mark.django_db(transaction=True)
def test_recording_activity_keeps_the_latest_committed_timestamp(person, monkeypatch):
    """Both activities fall comfortably inside the limits. Only their recorded order matters.

    Restored to the reviewer's original after verification: Stay signed in waits for its row
    again (verification finding 3), so the blocked update this test observes exists again."""
    from accounts import sessions

    browser = signed_in(person)
    row = rows(person).get()
    worker_pid = Queue()
    original = sessions._touch

    def touch(state):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            worker_pid.put(cursor.fetchone()[0])
        return original(state)

    def continue_session():
        try:
            return browser.post(reverse("account-session-continue")).json()
        finally:
            connections.close_all()

    monkeypatch.setattr(sessions, "_touch", touch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cursor:
            rows(person).select_for_update().get()
            result = pool.submit(continue_session)
            pid = worker_pid.get(timeout=10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("The activity update never reached the held session row")
            rows(person).update(last_active_at=Now())
            row.refresh_from_db()
            latest = row.last_active_at
        assert result.result(timeout=10)["signed_in"] is True
    row.refresh_from_db()
    assert row.last_active_at >= latest


def test_storage_expiry_and_new_login_record_the_old_session_ending(person):
    from django.contrib.sessions.models import Session

    browser = Client()
    login(browser, person, remember="on")
    original = rows(person).get()
    rows(person).update(
        started_at=Now() - timedelta(days=2), last_active_at=Now() - timedelta(days=2)
    )
    Session.objects.filter(pk=browser.session.session_key).update(
        expire_date=Now() - timedelta(days=1)
    )
    call_command("clearsessions")
    # Same person returns and completes a normal new password sign-in.
    login(browser, person, remember="on")
    assert rows(person).count() == 2
    original.refresh_from_db()
    assert original.ended_at is not None and original.ending in {"maximum", "inactivity"}


def test_operator_setup_rerun_preserves_the_original_session():
    from accounts.setup import setup_lab

    options = {
        "lab_name": "Example Laboratory",
        "username": "example-owner",
        "email": "owner@example.invalid",
    }
    setup_lab(**options, password=PASSWORD)
    user = User.objects.get(username=options["username"])
    browser = signed_in(user)
    assert not setup_lab(**options, password=NEW_PASSWORD).changed
    assert browser.get(reverse("dashboard")).status_code == 200 and rows(user).get().ending == ""


def test_admin_deactivation_after_limits_form_opens_preserves_history(owner, admin):
    from desk.tests.test_account_move import admin_change

    form = reverse("session-limits-edit", args=["staff"])
    first, second = signed_in(admin), signed_in(admin)
    revision = first.get(form).context["form"]["expected_revision"].value()
    console = signed_in(owner)
    assert admin_change(console, admin, is_active="").status_code == 302
    admin.refresh_from_db()
    assert not admin.is_active
    assert list(rows(admin).values_list("ending", flat=True)) == ["deactivated"] * 2
    assert first.post(form, FORM_DATA | {"expected_revision": revision}).status_code == 302
    assert not AuditEvent.objects.filter(action="lab.session_limits_changed").exists()
    assert admin_change(console, admin, is_active="on").status_code == 302
    assert second.get(form).status_code == 302


@pytest.mark.parametrize("route", ["admin:auth_user_password_change", "admin:password_change"])
def test_both_console_password_forms_keep_only_the_acting_session(owner, route):
    from accounts.sessions import SESSION_KEY

    browser = signed_in(owner)
    current_pk = browser.session[SESSION_KEY]
    signed_in(owner)
    if route == "admin:password_change":
        url = reverse(route)
        data = {
            "old_password": PASSWORD,
            "new_password1": NEW_PASSWORD,
            "new_password2": NEW_PASSWORD,
        }
    else:
        url = reverse(route, args=[owner.pk])
        data = {"usable_password": "true", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
    assert browser.post(url, data).status_code == 302
    assert rows(owner).get(pk=current_pk).ending == ""
    assert rows(owner).exclude(pk=current_pk).get().ending == "new_password"
    assert browser.get(reverse("admin:index")).status_code == 200


def test_timed_out_ticket_form_keeps_ticket_text_out_of_session_metadata(
    person, client_org, caplog
):
    from desk.models import ServiceRequest
    from taxonomy.models import RequestCategory
    from taxonomy.terms import drawn

    group = RequestCategory.objects.create(key="review", name="Review")
    category = RequestCategory.objects.create(key="collection", name="Collection", parent=group)
    form = reverse("desk:request-create")
    browser = signed_in(person)
    page = browser.get(form)
    assert page.status_code == 200
    marker = "Synthetic ticket detail R731"
    data = {
        "category": category.pk,
        "category_terms": drawn([category]),
        "client": client_org.pk,
        "channel": "fax",
        "subject": marker,
        "first_entry_kind": "note",
        "body": marker,
    }
    set_idle(person, 31)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        refused = browser.post(form, data, headers={"Referer": "http://testserver" + form})
    assert refused.status_code == 302 and refused.url == reverse("account_login") + "?next=" + form
    assert not ServiceRequest.objects.exists() and not AuditEvent.objects.exists()
    assert marker not in caplog.text
    notice = browser.get(reverse("account_login")).context["messages"]
    assert [str(m) for m in notice] == [
        "You were signed out after 30 minutes without activity. Sign in again to continue."
    ]
    # The same input is valid when the operator signs in again and submits it deliberately.
    login(browser, person)
    assert browser.post(form, data).status_code == 302
    ticket = ServiceRequest.objects.get(subject=marker)
    html = browser.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    assert marker in html
    config = re.search(r"<div\b[^>]*\bdata-vr-session\s[^>]*>", html)[0]
    assert marker not in config and person.username not in config
    assert set(re.findall(r"\s(data-[\w-]+)(?=[\s=>])", config)) == {
        "data-vr-session",
        "data-status-url",
        "data-continue-url",
        # Adopted (kept typing, 2026-11-22): the keep address, an address like the others.
        "data-keep-url",
        "data-sign-in-url",
        "data-inactivity-seconds",
        "data-maximum-seconds",
        "data-warning-seconds",
        "data-longest-warning-seconds",
    }
    status = browser.get(reverse("account-session")).json()
    assert all(isinstance(value, (bool, int)) for value in status.values())


@pytest.mark.parametrize("route", ["admin:index", "account_login", "account_logout"])
def test_expiry_on_entry_pages_shows_notice_and_no_stored_response(owner, route):
    browser = signed_in(owner)
    set_idle(owner, 31)
    page = browser.get(reverse(route))
    assert "no-store" in page.get("Cache-Control", "")
    if page.status_code == 302:
        page = browser.get(page.url, follow=True)
    assert page.status_code == 200 and page.request["PATH_INFO"] == reverse("account_login")
    assert any("without activity" in str(m) for m in page.context["messages"])
    assert rows(owner).get().ending == "inactivity"


def test_a_direct_offsite_next_finishes_at_a_local_page(person):
    requested = reverse("account_login") + "?next=https%3A%2F%2Fexample.invalid%2F"
    response = Client().post(requested, {"login": person.username, "password": PASSWORD})
    assert response.status_code == 302
    assert response.url.startswith("/") and not urlsplit(response.url).netloc


def test_settings_panel_qualifies_the_warning_claim_for_the_console(owner, admin):
    console = signed_in(owner).get(reverse("admin:index"))
    assert console.status_code == 200 and b"data-vr-session-warning" not in console.content
    assert "no-store" in console.get("Cache-Control", "")
    page = signed_in(admin).get(reverse("security-settings"))
    claims = re.findall(r"<p[^>]*>[^<]*Every page warns[^<]*</p>", page.content.decode())
    for claim in claims:
        assert "admin console" in claim.lower(), claim


def test_timed_out_preference_request_uses_the_real_json_body(person):
    browser = signed_in(person)
    data = {"key": "nav.collapsed", "value": True}
    options = {"content_type": "application/json", "headers": {"Sec-Fetch-Dest": "empty"}}
    address = reverse("preference")
    set_idle(person, 31)
    assert browser.post(address, data, **options).status_code == 401
    assert not person.preferences.filter(key=data["key"]).exists()
    login(browser, person)
    assert browser.post(address, data, **options).status_code == 204
    assert person.preferences.get(key=data["key"]).value is True


def test_stay_signed_in_validates_the_submitted_token_before_recording_activity(
    person, monkeypatch
):
    """Observe only an accepted same-origin POST, including its real token."""
    from django.middleware.csrf import CsrfViewMiddleware

    browser = Client(enforce_csrf_checks=True)
    browser.force_login(person)
    assert browser.get("/").status_code == 200
    token = browser.cookies["csrftoken"].value
    steps = []
    accept = CsrfViewMiddleware._accept

    def observe_csrf(middleware, request):
        steps.append("token accepted")
        return accept(middleware, request)

    def observe_sql(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        normalized = sql.replace('"', "").lower()
        # Adapted: the activity statement is now one WITH ... UPDATE (accounts.sessions._touch).
        if "accounts_accountsession" in normalized and "set last_active_at" in normalized:
            steps.append("activity recorded")
        return result

    monkeypatch.setattr(CsrfViewMiddleware, "_accept", observe_csrf)
    with connection.execute_wrapper(observe_sql):
        response = browser.post(reverse("account-session-continue"), headers={"X-CSRFToken": token})
    assert response.status_code == 200 and response.json()["signed_in"]
    assert steps == ["token accepted", "activity recorded"]


@pytest.mark.django_db(transaction=True)
def test_limits_save_checks_its_acting_session_in_the_transaction_that_records_the_change(admin):
    """Trace a successful save by a valid administrator on an open session."""
    browser = signed_in(admin)
    session_checks = []
    audit_writes = []

    def observe(execute, sql, params, many, context):
        normalized = sql.replace('"', "").lower()
        # Keep the outer atomic block object. Two separate transactions must not compare equal.
        block = connection.atomic_blocks[0] if connection.in_atomic_block else None
        if "accounts_accountsession" in normalized and normalized.startswith(("select", "update")):
            session_checks.append(block)
        if normalized.startswith("insert into core_auditevent "):
            audit_writes.append(block)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        response = browser.post(reverse("session-limits-edit", args=["staff"]), FORM_DATA)
    assert response.status_code == 302
    assert AuditEvent.objects.get(action="lab.session_limits_changed").actor == admin
    assert session_checks and len(audit_writes) == 1 and audit_writes[0] is not None
    assert any(block is audit_writes[0] for block in session_checks), (
        "Every session check completed before the transaction that saved and audited these limits"
    )
