"""Second confirmation of the session decisions, messages and response types."""

import logging
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib import admin, messages
from django.contrib.auth import SESSION_KEY as AUTH
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.templatetags.static import static
from django.test import RequestFactory
from django.urls import reverse

from accounts import sessions
from accounts.models import User
from core.models import AuditEvent, SessionLimit
from tests.test_review_session_limits_confirm import (
    HEADERS,
    HOUR,
    MIN,
    clock,
    during_wait,
    login,
    state,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(staff):
    return staff("confirm2-staff", "admin")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("hours,expected", [(2, "expired"), (6, "open")])
def test_maximum_changed_during_settlement_wait_uses_the_committed_hours(operator, hours, expected):
    _, row = login(operator)
    clock(row, 3 * HOUR, MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=30, maximum_hours=1)
    reading = state(row, operator)
    settled = during_wait(
        row,
        lambda: sessions._settle_expiry(reading),
        lambda: SessionLimit.objects.filter(pk=policy.pk).update(maximum_hours=hours),
    )
    row.refresh_from_db()
    assert settled[0] == expected
    if expected == "expired":
        assert (row.ending, row.ended_at) == ("maximum", row.started_at + hours * HOUR)
    else:
        assert row.ended_at is None and settled[1].maximum == hours * HOUR


@pytest.mark.django_db(transaction=True)
def test_guard_uses_longer_maximum_after_wait(operator):
    _, row = login(operator)
    clock(row, 3 * HOUR, MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=30, maximum_hours=2)

    def decide():
        token = sessions._current.set((operator.pk, str(row.pk)))
        try:
            with transaction.atomic():
                return sessions.hold_acting_session(operator.pk)
        finally:
            sessions._current.reset(token)

    assert during_wait(
        row, decide, lambda: SessionLimit.objects.filter(pk=policy.pk).update(maximum_hours=6)
    )


@pytest.mark.parametrize("remaining", [2, 0, -2])
def test_settlement_uses_later_clock_for_the_decision_and_remaining_time(operator, remaining):
    _, row = login(operator)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
    clock(row, HOUR, 10 * MIN)
    reading = state(row, operator)
    # The first reading is later than the clock at settlement, as after a clock correction.
    # Put its time two minutes inside, exactly on, or two minutes outside the deadline.
    later = replace(reading, now=row.last_active_at + (30 - remaining) * MIN)
    SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30)
    settled = sessions._settle_expiry(later)
    if remaining > 0:
        assert settled[0] == "open"
        assert settled[1].inactivity_remaining == remaining * 60
    else:
        row.refresh_from_db()
        assert settled[0] == "expired" and row.ended_at == row.last_active_at + 30 * MIN


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("field", ["inactivity_minutes", "maximum_hours"])
def test_waiting_stay_answers_with_the_policy_in_force_when_activity_is_recorded(operator, field):
    browser, row = login(operator)
    clock(row, HOUR, MIN)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=10, maximum_hours=5)

    def press():
        return browser.post(reverse("account-session-continue")).json()

    answer = during_wait(
        row,
        press,
        lambda: SessionLimit.objects.filter(pk=policy.pk).update(
            **{field: 25 if field == "inactivity_minutes" else 9}
        ),
    )
    after = browser.get(reverse("account-session")).json()
    assert answer["signed_in"] and after["signed_in"]
    key = "inactivity_limit_seconds" if field == "inactivity_minutes" else "maximum_seconds"
    assert abs(answer[key] - after[key]) <= 2, (key, answer[key], after[key])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("winner", ["inactivity", "maximum"])
def test_notice_for_recorded_timeout_uses_the_duration_of_that_ending(
    operator, monkeypatch, winner, caplog
):
    browser, row = login(operator)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=10, maximum_hours=2)
    clock(row, 3 * HOUR if winner == "maximum" else HOUR, MIN if winner == "maximum" else 20 * MIN)
    read = sessions._read

    def read_then_finish(*args):
        value = read(*args)
        assert sessions.close_expired(operator) == 1
        SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30, maximum_hours=6)
        return value

    monkeypatch.setattr(sessions, "_read", read_then_finish)
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        result = browser.get(reverse("account-session"))
    assert result.json()["signed_in"] is False
    row.refresh_from_db()
    assert row.ending == winner and row.ended_at == (
        row.last_active_at + 10 * MIN if winner == "inactivity" else row.started_at + 2 * HOUR
    )
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("signout.")] == [
        f"signout.{winner} user={operator.username} sessions=1"
    ]
    assert [str(m) for m in messages.get_messages(result.wsgi_request)] == [
        sessions.ENDED_MESSAGES[winner].format(inactivity="10 minutes", maximum="2 hours")
    ]


def test_guard_refusal_and_recovered_session_are_consistent_after_exception_settlement(operator):
    browser, row = login(operator)
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=5, maximum_hours=12)
    clock(row, HOUR, 10 * MIN)
    req = RequestFactory().post("/")
    req.user, req.session, req.account_session = operator, browser.session, state(row, operator)
    token = sessions._current.set((operator.pk, str(row.pk)))
    try:
        with transaction.atomic():
            assert not sessions.hold_acting_session(operator.pk)
        SessionLimit.objects.filter(pk=policy.pk).update(inactivity_minutes=30)
        response = sessions.SessionLimitMiddleware(lambda r: None).process_exception(
            req, sessions.SessionEnded()
        )
    finally:
        sessions._current.reset(token)
    row.refresh_from_db()
    assert response.status_code == 302
    # A definitive browser sign-out and an open tracking record should not coexist.
    assert (AUTH in req.session) is (row.ended_at is None)


@pytest.mark.parametrize("transport", [*HEADERS, "navigation"])
def test_exception_escaping_the_admin_preserves_the_correct_return_page(monkeypatch, transport):
    owner = User.objects.create_superuser(
        "confirm2-owner", "owner@example.invalid", "synthetic-password-482"
    )
    browser, _ = login(owner)
    parent = reverse("admin:index")
    target = reverse("admin:auth_group_changelist") + "?q=example"
    touched = []

    def refuse(request):
        touched.append(request.path)
        raise sessions.SessionEnded()

    monkeypatch.setattr(admin.site.get_model_admin(Group), "get_queryset", refuse)
    headers = {"Referer": "http://testserver" + parent, **HEADERS.get(transport, {})}
    result = (browser.get if transport == "navigation" else browser.post)(target, headers=headers)
    assert touched and AUTH not in browser.session and "no-store" in result["Cache-Control"]
    full = transport in ("page", "navigation")
    assert result.status_code == (302 if full else 401)
    redirect = urlsplit(result["Location" if full else "HX-Redirect"])
    assert redirect.path == reverse("account_login")
    assert parse_qs(redirect.query)["next"] == [target if transport == "navigation" else parent]


@pytest.mark.parametrize("which", ["role", "session"])
def test_import_preview_distinguishes_a_role_refusal_from_a_session_refusal(
    operator, monkeypatch, which
):
    from dataimport.importing import ORGANIZATIONS

    browser, _ = login(operator)
    parent = reverse("dataimport:index")
    target = reverse("dataimport:upload", args=[ORGANIZATIONS.slug])
    calls = []

    def decision(pk):
        calls.append(pk)
        if which == "role":
            raise PermissionDenied("Role refused")
        return False

    monkeypatch.setattr(sessions, "hold_acting_session", decision)
    result = browser.post(
        target,
        {
            "step": "preview",
            "file": SimpleUploadedFile("example.csv", b"account_code,name\nEX,Example\n"),
        },
        headers={"Referer": "http://testserver" + parent, "HX-Request": "true"},
    )
    assert calls == [operator.pk] and not AuditEvent.objects.exists()
    if which == "role":
        assert result.status_code == 403 and AUTH in browser.session
    else:
        assert result.status_code == 401 and result["HX-Redirect"] == sessions.sign_in_url(parent)
        assert AUTH not in browser.session


@pytest.mark.parametrize("kind", ["head", "conditional", "range", "method-error", "range-error"])
def test_static_cache_exemption_is_for_asset_responses(operator, kind):
    browser, _ = login(operator)
    path = static("js/app.js")
    head = browser.head(path)
    assert head.status_code == 200
    if kind == "head":
        answer = head
    elif kind == "conditional":
        answer = browser.get(path, headers={"If-None-Match": head["ETag"]})
    elif kind == "range":
        answer = browser.get(path, headers={"Range": "bytes=0-15"})
    elif kind == "method-error":
        answer = browser.post(path)
    else:
        answer = browser.get(path, headers={"Range": f"bytes={int(head['Content-Length']) + 1}-"})
    try:
        expected = {
            "head": 200,
            "conditional": 304,
            "range": 206,
            "method-error": 405,
            "range-error": 416,
        }[kind]
        assert answer.status_code == expected
        if expected >= 400:
            assert "no-store" in answer.get("Cache-Control", "")
        else:
            assert (
                "max-age" in answer["Cache-Control"] and "no-store" not in answer["Cache-Control"]
            )
    finally:
        answer.close()
        head.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ending", ["inactivity", "maximum", "signed_out"])
def test_notice_when_winner_committed_before_status_read(operator, caplog, ending):
    browser, row = login(operator)
    clock(row, 13 * HOUR if ending == "maximum" else HOUR, MIN if ending == "maximum" else 35 * MIN)
    if ending == "signed_out":
        assert sessions.end_sessions(operator, ending, session_id=row.pk) == 1
    else:
        assert sessions.close_expired(operator) == 1
    row.refresh_from_db()
    assert row.ending == ending
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        answer = browser.get(reverse("account-session"))
    assert not answer.json()["signed_in"] and AUTH not in browser.session
    assert not any(r.getMessage().startswith("signout.") for r in caplog.records)
    expected = sessions.ENDED_MESSAGES.get(ending, sessions.ENDED_MESSAGE).format(
        inactivity="30 minutes", maximum="12 hours"
    )
    assert [str(m) for m in messages.get_messages(answer.wsgi_request)] == [expected]


@pytest.mark.parametrize("final_minutes", [20, 5])
def test_result_after_second_decline_matches_the_last_reading(operator, monkeypatch, final_minutes):
    browser, row = login(operator)
    clock(row, HOUR, 7 * MIN)
    before = row.last_active_at
    policy = SessionLimit.objects.create(audience="staff", inactivity_minutes=15, maximum_hours=10)
    values = SessionLimit.objects.filter(pk=policy.pk)
    touch = sessions._touch
    attempts = []

    def decline(reading):
        values.update(inactivity_minutes=5)
        assert touch(reading) is False
        attempts.append(reading)
        values.update(inactivity_minutes=15 if len(attempts) == 1 else final_minutes)
        return False

    monkeypatch.setattr(sessions, "_touch", decline)
    reply = browser.post(reverse("account-session-continue")).json()
    row.refresh_from_db()
    assert len(attempts) == 2 and row.last_active_at == before
    if final_minutes == 20:
        assert reply["signed_in"] and row.ended_at is None and AUTH in browser.session
        assert reply["inactivity_limit_seconds"] == 20 * 60
        assert 12 * 60 < reply["inactivity_seconds"] <= 13 * 60 + 1
    else:
        assert not reply["signed_in"] and AUTH not in browser.session
        assert (row.ending, row.ended_at) == ("inactivity", before + 5 * MIN)


def test_import_holds_the_session_once_for_the_whole_file(operator, monkeypatch):
    from dataimport.importing import ORGANIZATIONS, preview_file

    _, row = login(operator)
    original = sessions.hold_acting_session
    calls = []

    def count(pk):
        calls.append(pk)
        return original(pk)

    monkeypatch.setattr(sessions, "hold_acting_session", count)
    token = sessions._current.set((operator.pk, str(row.pk)))
    try:
        preview = preview_file(
            ORGANIZATIONS,
            actor=operator,
            data=b"account_code,name\nA,Example One\nB,Example Two\n",
            filename="example.csv",
        )
    finally:
        sessions._current.reset(token)
    assert preview.ok and len(preview.rows) == 2 and calls == [operator.pk]
