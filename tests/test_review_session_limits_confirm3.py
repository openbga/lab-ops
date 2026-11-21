"""Third confirmation of the Stay receipt and the ending recorded after a refusal."""

import logging
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib import messages
from django.contrib.auth import SESSION_KEY as AUTH
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection, transaction
from django.templatetags.static import static
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts import sessions
from core.models import SessionLimit
from tests.test_review_session_limits_confirm import HOUR, MIN, clock, during_wait, login, state

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def operator(staff):
    return staff("confirm3-operator", "admin")


@pytest.mark.parametrize("minutes", [5, 20])
def test_waiting_stay_when_idle_limit_is_shortened(operator, minutes):
    browser, row = login(operator)
    clock(row, HOUR, 10 * MIN)
    last = row.last_active_at
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=30, maximum_hours=12)
    receipt = during_wait(
        row,
        lambda: browser.post(reverse("account-session-continue")).json(),
        lambda: SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=minutes),
    )
    row.refresh_from_db()
    if minutes == 5:
        assert (row.last_active_at, row.ended_at, row.ending) == (
            last,
            last + 5 * MIN,
            "inactivity",
        )
        assert receipt["signed_in"] is False
    else:
        assert row.last_active_at > last and row.ended_at is None
        assert receipt["signed_in"] and receipt["inactivity_limit_seconds"] == 20 * 60


def refused_request(operator):
    browser, row = login(operator)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
    req = RequestFactory().post("/")
    req.user, req.session = operator, browser.session
    req._messages = FallbackStorage(req)
    req.account_session = state(row, operator)
    clock(row, HOUR, 10 * MIN)
    token = sessions._current.set((operator.pk, str(row.pk)))
    try:
        with transaction.atomic():
            assert not sessions.hold_acting_session(operator.pk)
    finally:
        sessions._current.reset(token)
    SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30)
    return req, row, policy


def finish(req):
    response = sessions.SessionLimitMiddleware(lambda request: None).process_exception(
        req, sessions.SessionEnded()
    )
    assert response.status_code == 302 and AUTH not in req.session
    return [str(m) for m in messages.get_messages(req)]


def test_recovered_refusal_records_reason_only_on_the_refused_session(operator):
    _, other = login(operator)
    req, row, _ = refused_request(operator)
    assert finish(req) == [sessions.ENDED_MESSAGE]
    row.refresh_from_db()
    other.refresh_from_db()
    assert other.ended_at is None
    assert row.ending == "change_refused" and row.ended_at >= row.last_active_at


def test_refusal_notice_matches_timeout_committed_after_open_settlement(
    operator, monkeypatch, caplog
):
    req, row, policy = refused_request(operator)
    settle = sessions._settle_expiry

    def record_timeout():
        SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=5)
        sessions.close_expired(operator)

    def settle_then_schedule_cleanup(reading):
        result = settle(reading)
        if result[0] == "open":
            # Earliest commit boundary at which a cleanup could win the session row.
            transaction.on_commit(record_timeout)
        return result

    monkeypatch.setattr(sessions, "_settle_expiry", settle_then_schedule_cleanup)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        notices = finish(req)
    row.refresh_from_db()
    if row.ending == "change_refused":
        assert notices == [sessions.ENDED_MESSAGE]
    else:
        assert (row.ending, row.ended_at) == ("inactivity", row.last_active_at + 5 * MIN)
        assert [
            r.getMessage() for r in caplog.records if r.getMessage().startswith("signout.")
        ] == [f"signout.inactivity user={operator.username} sessions=1"]
        assert notices == [sessions.ENDED_MESSAGES["inactivity"].format(inactivity="5 minutes")]


def test_refused_ending_cannot_predate_the_last_recorded_activity(operator, monkeypatch):
    req, row, _ = refused_request(operator)
    end = sessions.end_sessions

    def end_while_latest_activity_commits(user, ending, **kwargs):
        if connection.in_atomic_block or ending != "change_refused":
            return end(user, ending, **kwargs)

        def record_activity():
            assert sessions._touch(state(row, operator))

        return during_wait(row, lambda: end(user, ending, **kwargs), record_activity)

    monkeypatch.setattr(sessions, "end_sessions", end_while_latest_activity_commits)
    finish(req)
    row.refresh_from_db()
    assert row.ending == "change_refused"
    assert row.ended_at >= row.last_active_at, (row.ended_at, row.last_active_at)


def test_both_countdowns_use_the_clock_at_which_the_maximum_was_settled(operator):
    _, row = login(operator)
    clock(row, HOUR + 10 * MIN, 3 * MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=30, maximum_hours=1)
    reading = state(row, operator)
    # The first clock is ahead. A longer maximum makes the session live at that later reading.
    at = row.started_at + 2 * HOUR - 2 * MIN
    SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=60, maximum_hours=2)
    outcome, settled = sessions._settle_expiry(replace(reading, now=at))
    assert outcome == "open"
    assert settled.maximum_remaining == 120
    assert settled.inactivity_remaining == int((row.last_active_at + 60 * MIN - at).total_seconds())


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Dest": "document"}, {"HX-Request": "true"}])
def test_head_refusal_preserves_the_intended_page_and_query(operator, headers):
    req, _, _ = refused_request(operator)
    parent = reverse("admin:index") + "?q=previous"
    target = reverse("admin:auth_group_changelist") + "?q=two%20words&o=1"
    head = RequestFactory().head(
        target, headers={"Referer": "http://testserver" + parent, **headers}
    )
    head.user, head.session, head.account_session = operator, req.session, req.account_session
    answer = sessions.SessionLimitMiddleware(lambda request: None).process_exception(
        head, sessions.SessionEnded()
    )
    partial = "HX-Request" in headers
    assert answer.status_code == (401 if partial else 302)
    assert "no-store" in answer["Cache-Control"]
    redirect = urlsplit(answer["HX-Redirect" if partial else "Location"])
    assert redirect.path == reverse("account_login")
    assert parse_qs(redirect.query)["next"] == [parent if partial else target]


@pytest.mark.parametrize("cookie", [False, True])
def test_static_method_error_depends_on_presence_of_the_session_cookie(settings, cookie):
    browser = Client()
    if cookie:
        browser.cookies[settings.SESSION_COOKIE_NAME] = "synthetic-expired-cookie"
    response = browser.options(static("js/app.js"))
    try:
        assert response.status_code == 405
        assert ("no-store" in response.get("Cache-Control", "")) is cookie
    finally:
        response.close()
