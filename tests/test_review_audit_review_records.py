"""Independent regression cases for signed audit reviews. All accounts and records are synthetic."""

import csv
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, date, datetime, timedelta
from html import unescape
from importlib.util import find_spec
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, get_ident
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import urljoin

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.views.debug import SafeExceptionReporterFilter

from accounts import confirmation
from accounts.models import SignInEvent
from accounts.roles import ROLES
from audit_review.models import Source, UseKind, WorkspaceUse
from audit_review.tests.conftest import (
    PASSWORD,
    review_permission,
)
from audit_review.tests.conftest import (
    clinic as clinic,
)
from audit_review.tests.conftest import (
    make_user as make_user,
)
from audit_review.tests.conftest import (
    reviewer as reviewer,
)
from core.models import AuditEvent
from jobs.models import Notice

# The base checkout runs the shared Workspace use controls from this same file.
if find_spec("audit_review.reviews") is not None:
    from audit_review import review_views, reviews
    from audit_review.models import EXAMINED, Review, ReviewNote, ReviewSchedule
    from audit_review.review_forms import IntervalForm, ReviewForm
    from audit_review.tests.test_reviews import form_data, sign


def route(name, *args):
    return reverse("audit_review:" + name, args=args)


def form_requests(record):
    return [
        (route("review-create"), form_data()),
        (route("review-note", record.pk), {"text": "Fixture note", "expected_revision": 1}),
        (
            route("review-interval"),
            {"reason": "Fixture interval", "interval_days": 5, "expected_revision": 0},
        ),
    ]


@pytest.mark.parametrize(
    "audience", [*ROLES, "anonymous", "superuser", "grant_only", "portal", "inactive"]
)
def test_direct_addresses_require_an_active_lab_role_and_explicit_grant(
    reviewer, client, make_user, clinic, audience
):
    record = sign(reviewer, findings="Synthetic detail marker")
    reviews.add_note(
        actor=reviewer, review=record, text="Synthetic note marker", expected_revision=1
    )
    if audience != "anonymous":
        roles = [audience] if audience in ROLES else []
        account = make_user(
            "audience",
            *roles,
            is_staff=audience != "portal",
            is_superuser=audience == "superuser",
            client=clinic if audience == "portal" else None,
        )
        if audience in ("grant_only", "portal", "inactive"):
            account.user_permissions.add(review_permission())
        if audience == "inactive":
            account.groups.add(Group.objects.get(name="supervisor"))
            account.is_active = False
            account.save(update_fields=["is_active"])
        client.force_login(account)
    forms = form_requests(record)
    reads = [
        route("reviews"),
        route("reviews-export"),
        route("review", record.pk),
        route("review", record.pk + 100),
        route("review-note", record.pk + 100),
    ]
    responses = [client.get(path) for path in reads + [path for path, _ in forms]]
    responses.extend(client.post(path, data) for path, data in forms)
    for response in responses:
        if audience in ("anonymous", "inactive"):
            assert response.status_code == 302
        else:
            assert response.status_code in (403, 404)
        assert all(
            value not in response.content
            for value in (
                record.number.encode(),
                b"Synthetic detail marker",
                b"Synthetic note marker",
            )
        )
    assert responses[2].status_code == responses[3].status_code
    assert responses[4].status_code == responses[len(reads) + 1].status_code
    assert Review.objects.count() == ReviewNote.objects.count() == 1
    assert not ReviewSchedule.objects.exists()
    assert not WorkspaceUse.objects.exists()


def test_role_group_grant_works_on_each_address(client, make_user, reviewer):
    account = make_user("group-reviewer", "technical")
    account.groups.get().permissions.add(review_permission())
    record = sign(reviewer)
    client.force_login(account)
    for path in (route("reviews"), route("reviews-export"), route("review", record.pk)):
        assert client.get(path).status_code == 200
    for path, data in form_requests(record):
        assert client.get(path).status_code == 200
        assert client.post(path, data).status_code == 302
    assert Review.objects.filter(signed_by=account, signer_roles="Technical staff").count() == 1
    assert record.notes.get().author_id == account.pk
    assert ReviewSchedule.objects.get().interval_days == 5


@pytest.mark.parametrize("change", ["grant", "role", "password", "inactive"])
def test_a_form_opened_before_account_changes_is_decided_at_submission(client, reviewer, change):
    record = sign(reviewer)
    forms = form_requests(record)
    client.force_login(reviewer)
    assert all(client.get(path).status_code == 200 for path, _ in forms)
    if change == "grant":
        reviewer.user_permissions.remove(review_permission())
    elif change == "role":
        reviewer.groups.clear()
    elif change == "inactive":
        reviewer.is_active = False
        reviewer.save(update_fields=["is_active"])
    else:
        reviewer.set_password("replacement synthetic password")
        reviewer.save(update_fields=["password"])
    for path, data in forms:
        reply = client.post(path, data)
        assert reply.status_code in (302, 403)
        if reply.status_code == 302:
            assert "login" in reply.url
    assert Review.objects.count() == 1
    assert not record.notes.exists()
    assert not ReviewSchedule.objects.exists()


@pytest.mark.confirmation
def test_signing_neither_creates_nor_consumes_a_recent_confirmation(reviewer):
    first, second = Client(), Client()
    for http in (first, second):
        http.force_login(reviewer)
    create = route("review-create")
    assert first.post(create, form_data()).status_code == 302
    assert not confirmation.current(first.get(create).wsgi_request)
    assert second.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    assert confirmation.current(second.get(create).wsgi_request)
    for http in (first, second):
        for password in ("", "unaccepted synthetic password"):
            reply = http.post(create, form_data(confirm_password=password))
            assert "confirm_password" in reply.context["form"].errors
            assert 'value="' not in reply.context["form"].password.as_widget()
            assert Review.objects.count() == 1
    assert SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").count() == 2
    assert second.post(create, form_data()).status_code == 302
    assert confirmation.current(second.get(create).wsgi_request)


@pytest.mark.parametrize(
    ("limit", "message", "locked"),
    [
        ({"reauthenticate": "10/m/user", "login_failed": "1/m/key"}, "Too many failed", 1),
        ({"reauthenticate": "1/m/user", "login_failed": "10/m/key"}, "Too many attempts", 0),
    ],
)
def test_only_checked_passwords_count_and_the_configured_limit_refuses_signing(
    client, reviewer, settings, limit, message, locked
):
    settings.ACCOUNT_RATE_LIMITS = limit
    cache.clear()
    client.force_login(reviewer)
    create = route("review-create")
    for data in (form_data(examined=[], confirm_password="wrong"), form_data(confirm_password="")):
        assert client.post(create, data).context["form"].errors
    assert not SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").exists()
    failed = client.post(create, form_data(confirm_password="wrong"))
    assert "Incorrect password" in str(failed.context["form"].errors)
    limited = client.post(create, form_data())
    assert message in str(limited.context["form"].errors)
    assert not Review.objects.exists()
    assert SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").count() == 1
    assert SignInEvent.objects.filter(user=reviewer, kind="locked_out").count() == locked


def test_unusable_password_does_not_spend_a_checked_attempt(client, reviewer):
    reviewer.set_unusable_password()
    reviewer.save(update_fields=["password"])
    client.force_login(reviewer)
    result = client.post(route("review-create"), form_data())
    assert "no password" in str(result.context["form"].errors)
    assert not Review.objects.exists()
    assert not SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").exists()


def test_patient_notices_no_store_and_no_email_on_the_three_writes(
    client, reviewer, mailoutbox, caplog, settings
):
    record = sign(reviewer)
    client.force_login(reviewer)
    settings.DEBUG = False
    before = Notice.objects.count()
    with caplog.at_level("INFO"):
        for path, data in form_requests(record):
            response = client.get(path)
            assert b"No patient data" in response.content
            assert "no-store" in response["Cache-Control"]
            saved = client.post(path, data)
            assert saved.status_code == 302
            assert "no-store" in saved["Cache-Control"]
            if "confirm_password" in data:
                filtered = SafeExceptionReporterFilter().get_post_parameters(saved.wsgi_request)
                assert (
                    filtered["confirm_password"] == SafeExceptionReporterFilter.cleansed_substitute
                )
    assert not mailoutbox and Notice.objects.count() == before
    assert all(
        text not in caplog.text
        for text in (PASSWORD, "Fixture note", "Fixture interval", form_data()["findings"])
    )
    for model in (Review, ReviewNote, AuditEvent, WorkspaceUse, SignInEvent):
        assert PASSWORD not in str(list(model.objects.values()))


@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("examined_other", "x" * 2000, True),
        ("examined_other", "x" * 2001, False),
        ("findings", "z" * 4000, True),
        ("findings", "z" * 4001, False),
        ("follow_up", "y" * 4000, True),
        ("follow_up", "y" * 4001, False),
        ("outcome", "invented", False),
        ("examined", ["reviews"], False),
        ("examined", ["uses", "changes", "uses"], True),
        ("findings", "   ", False),
    ],
    ids=[
        "other-limit",
        "other-over",
        "findings-limit",
        "findings-over",
        "followup-limit",
        "followup-over",
        "unknown-outcome",
        "unknown-list",
        "duplicate-list",
        "blank-findings",
    ],
)
def test_field_limits_and_values_agree_between_form_and_service(reviewer, field, value, valid):
    data = form_data(**{field: value})
    form = ReviewForm(data)
    kwargs = {"outcome": "follow_up", "findings": data["findings"], field: value}
    if valid:
        assert form.is_valid(), form.errors
        record = sign(reviewer, **kwargs)
        if field == "examined":
            assert record.examined == ["changes", "uses"]
        else:
            assert getattr(record, field) == value
    else:
        assert field in form.errors
        with pytest.raises(ValidationError) as failure:
            sign(reviewer, **kwargs)
        assert field in failure.value.message_dict
        assert not Review.objects.exists()


@pytest.mark.parametrize(
    ("first", "last", "valid"),
    [
        (date.min, date.min, True),
        (date(2024, 1, 1), date(2024, 12, 31), True),
        (date(2023, 12, 31), date(2024, 12, 31), False),
        (date(2025, 3, 8), date(2025, 3, 8), True),
        (date(2025, 3, 9), date(2025, 3, 9), False),
        (date.max, date.max, False),
    ],
)
def test_period_bounds_use_local_today_and_count_both_ends(reviewer, first, last, valid):
    instant = datetime(2025, 3, 9, 4, 30, tzinfo=UTC)
    with (
        timezone.override("America/New_York"),
        patch("django.utils.timezone.now", return_value=instant),
    ):
        form = ReviewForm(form_data(period_from=first.isoformat(), period_to=last.isoformat()))
        assert form.is_valid() == valid
        if valid:
            record = sign(reviewer, period_from=first, period_to=last)
            assert (record.period_from, record.period_to) == (first, last)
            assert reviews.intact(Review.objects.get(pk=record.pk))
        else:
            assert "period_to" in form.errors
            with pytest.raises(ValidationError) as error:
                sign(reviewer, period_from=first, period_to=last)
            assert "period_to" in error.value.message_dict


def test_checksum_has_an_independent_oracle_for_all_signed_fields(reviewer):
    record = sign(reviewer, findings="Contrôle de démonstration", examined_other="Rota")
    body = {
        "record": "audit review",
        "number": record.number,
        "period_from": record.period_from.isoformat(),
        "period_to": record.period_to.isoformat(),
        "examined": ["changes", "access"],
        "examined_other": "Rota",
        "outcome": "nothing",
        "findings": "Contrôle de démonstration",
        "follow_up": "",
        "meaning": "reviewed",
        "signer": {
            "id": reviewer.pk,
            "name": "Rae Viewer",
            "username": "reviewer",
            "roles": "Supervisor",
        },
        "signed_at": record.signed_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert record.checksum == hashlib.sha256(encoded).hexdigest()
    with timezone.override("Pacific/Auckland"), override_settings(SECRET_KEY="rotated-fixture"):
        stored = Review.objects.get(pk=record.pk)
        # A database copy models a restored row whose append-only guard was absent. Change one
        # stored value per savepoint, then read it through the same model as the detail page.
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TEMP TABLE review_checksum_fixture ON COMMIT DROP AS "
                "SELECT * FROM audit_review_review WHERE id = %s",
                [stored.pk],
            )
        read_copy = "SELECT * FROM pg_temp.review_checksum_fixture"
        assert reviews.intact(Review.objects.raw(read_copy)[0])
        for field, value in {
            "number": "AR-999999",
            "signed_by_id": reviewer.pk + 1,
            "period_from": stored.period_from + timedelta(days=1),
            "period_to": stored.period_to + timedelta(days=1),
            "examined": ["uses"],
            "examined_other": "Other",
            "outcome": "follow_up",
            "findings": "Revised",
            "follow_up": "Call",
            "signer_name": "New name",
            "signer_username": "new-user",
            "signer_roles": "Administrator",
            "meaning": "other",
            "signed_at": stored.signed_at + timedelta(microseconds=1),
        }.items():
            with transaction.atomic(), connection.cursor() as cursor:
                column = connection.ops.quote_name(field)
                cursor.execute(f"UPDATE pg_temp.review_checksum_fixture SET {column} = %s", [value])
                assert not reviews.intact(Review.objects.raw(read_copy)[0]), field
                transaction.set_rollback(True)
        stored.request_ref = "another fixture"
        assert reviews.intact(stored)


def test_detail_rechecks_checksum_against_the_stored_fields(client, reviewer):
    # Insert an internally inconsistent synthetic fixture as it might be read from a backup.
    # Both append-only triggers stay active throughout.
    original = sign(reviewer)
    fixture = Review.objects.get(pk=original.pk)
    fixture.id = None
    fixture.number = "AR-fixture"
    fixture.save(force_insert=True)
    client.force_login(reviewer)
    detail = client.get(route("review", fixture.pk))
    assert detail.status_code == 200
    assert b"Checksum does not match" in detail.content
    assert b"Matches the review as stored." not in detail.content


@pytest.mark.parametrize("operation", ["note", "interval"])
def test_empty_post_reports_required_fields(client, reviewer, operation):
    record = sign(reviewer)
    client.force_login(reviewer)
    dest = route("review-note", record.pk) if operation == "note" else route("review-interval")
    reply = client.post(dest, {})
    assert reply.status_code == 200
    form = reply.context["form"]
    assert ("text" if operation == "note" else "reason") in form.errors
    assert not record.notes.exists() and not ReviewSchedule.objects.exists()


@pytest.mark.parametrize("days", [0, -1, 1, 366, 367, None])
def test_interval_range_clear_and_required_reason(reviewer, days):
    form = IntervalForm(
        {
            "interval_days": days if days is not None else "",
            "reason": "Test procedure",
            "expected_revision": 0,
        }
    )
    if days is None or 1 <= days <= 366:
        assert form.is_valid()
        stored, changed = reviews.set_interval(
            actor=reviewer, interval_days=days, reason="Test procedure", expected_revision=0
        )
        assert stored.interval_days == days and changed == (days is not None)
    else:
        assert "interval_days" in form.errors
        with pytest.raises(ValidationError) as error:
            reviews.set_interval(
                actor=reviewer, interval_days=days, reason="Test", expected_revision=0
            )
        assert "interval_days" in error.value.message_dict
    assert "reason" in IntervalForm({"interval_days": 2, "expected_revision": 0}).errors
    with pytest.raises(ValidationError) as error:
        reviews.set_interval(actor=reviewer, interval_days=2, reason="  ", expected_revision=0)
    assert "reason" in error.value.message_dict


def test_standing_and_suggested_period_across_the_dst_change(client, reviewer):
    client.force_login(reviewer)
    base = datetime(2025, 3, 9, 4, 30, tzinfo=UTC)  # 23:30 March 8 in New York
    with (
        timezone.override("America/New_York"),
        patch("django.utils.timezone.now", return_value=base),
    ):
        reviews.set_interval(actor=reviewer, interval_days=2, expected_revision=0, reason="Test")
        initial = client.get(route("reviews"))
        assert initial.context["state"].state == "none" and b"Now" in initial.content
        start = date(2025, 1, 1)
        for n in range(13):
            day = start + timedelta(days=2 * n)
            sign(reviewer, period_from=day, period_to=day)
        overlap = sign(reviewer, period_from=start, period_to=start + timedelta(days=2))
        state = client.get(route("reviews")).context["state"]
        assert state.last == overlap
        assert state.due == date(2025, 3, 10) and state.days_left == 2
        assert state.gap_count == 11 and len(state.gaps) == 10
        assert state.gaps[0] == (date(2025, 1, 24), date(2025, 1, 24))
        assert state.unreviewed_from == date(2025, 1, 26)
        sign(reviewer, period_from=date(2025, 3, 8), period_to=date(2025, 3, 8))
        assert reviews.status().unreviewed_from is None
        create = client.get(route("review-create")).context["form"]
        assert create.initial["period_from"] == create.initial["period_to"] == date(2025, 3, 8)
    for hour, label in [(4, b"Due today"), (28, b"Overdue")]:
        now = datetime(2025, 3, 10, tzinfo=UTC) + timedelta(hours=hour)
        with (
            timezone.override("America/New_York"),
            patch("django.utils.timezone.now", return_value=now),
        ):
            assert label in client.get(route("reviews")).content


def test_all_examined_links_open_the_covered_period_as_it_stands(client, reviewer):
    today = timezone.localdate()
    record = sign(reviewer, period_from=today, period_to=today, examined=list(EXAMINED))
    client.force_login(reviewer)
    result = client.get(route("review", record.pk))
    links = dict(result.context["examined_links"])
    assert set(links) == {source.label for source in EXAMINED}
    for path in links.values():
        reply = client.get(path)
        assert reply.status_code == 200
        assert reply.context["filter_form"].cleaned_data["date_from"] == today
        assert reply.context["filter_form"].cleaned_data["date_to"] == today
    assert record.number.encode() in client.get(links["Changes"]).content


def test_date_field_help_counts_the_same_days_as_its_validation():
    form = ReviewForm()
    first = date(2024, 1, 1)
    last = first + timedelta(days=366)
    assert reviews.check_period(first, last, date(2025, 1, 1))
    assert "at most 366 days after From" not in form.fields["period_to"].help_text


def review_csv(response):
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def page_numbers(response):
    return [record.number for record in response.context["page_obj"]]


def review_next_link(response):
    match = re.search(r'href="([^"]+)" rel="next"', response.content.decode())
    assert match is not None
    return urljoin(response.wsgi_request.path, unescape(match[1]))


def _wait_for_a_waiter(lock):
    """Until another connection waits for the advisory lock `lock` (reviews.SIGNING_LOCK)."""
    deadline = monotonic() + 15
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s "
                "AND objid = %s AND NOT granted",
                list(lock),
            )
            if cursor.fetchone()[0]:
                return
            sleep(0.02)
    raise AssertionError("No signing waited for the lock.")


@pytest.fixture
def overlapping_signings(client, reviewer, make_user, monkeypatch):
    # Adapted (round 1, M1 and L1): a signing takes reviews.SIGNING_LOCK, held until it commits,
    # before it is given its number. The first signer's request still yields after reserving its
    # number; the second signing, started meanwhile, now waits for it and is numbered after it,
    # so numbers follow the order signings commit in. The list is read while both are in flight.
    writer = make_user("second-reviewer", "supervisor")
    writer.user_permissions.add(review_permission())
    other = Client()
    other.force_login(writer)
    client.force_login(reviewer)
    reader = Client()
    reader.force_login(reviewer)
    for _ in range(51):
        sign(reviewer)

    allocated = Event()
    reading_done = Event()
    main_thread = get_ident()
    held = []
    next_id = reviews._next_id

    def allocate():
        number = next_id()
        if get_ident() != main_thread and not held:
            held.append(number)
            allocated.set()
            assert reading_done.wait(30)
        return number

    monkeypatch.setattr(reviews, "_next_id", allocate)
    create_url = reverse("audit_review:review-create")
    list_url = reverse("audit_review:reviews")

    def post(account_client, findings):
        try:
            return account_client.post(create_url, form_data(findings=findings)).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        waited_for = pool.submit(post, other, "Signed while another waited.")
        try:
            assert allocated.wait(15)
            waiting = pool.submit(post, client, "Signed after it.")
            _wait_for_a_waiter(reviews.SIGNING_LOCK)
            first = reader.get(list_url)
            before = list(Review.objects.order_by("-id").values_list("number", flat=True))
            assert len(before) == 51
            assert page_numbers(first) == before[:50]
        finally:
            reading_done.set()
        assert waited_for.result(timeout=15) == 302
        assert waiting.result(timeout=15) == 302
    earlier = Review.objects.get(signed_by=writer)
    latest = Review.objects.get(findings="Signed after it.")
    assert earlier.pk < latest.pk and earlier.signed_at < latest.signed_at
    return first, before, latest


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("surface", ["pager", "export"])
def test_the_original_reading_excludes_a_review_signed_after_it(
    client, overlapping_signings, surface
):
    first, before, _latest = overlapping_signings
    if surface == "pager":
        later = client.get(review_next_link(first))
        observed = page_numbers(first) + page_numbers(later)
    else:
        observed = [row["Review"] for row in review_csv(client.get(first.context["export_url"]))]
    assert observed == before


@pytest.mark.django_db(transaction=True)
def test_the_most_recent_signing_is_the_first_listed_and_exported(client, overlapping_signings):
    _first, _before, latest = overlapping_signings
    current = client.get(reverse("audit_review:reviews"))
    assert current.context["state"].last == latest
    listed = page_numbers(current)[0]
    exported = review_csv(client.get(current.context["export_url"]))[0]["Review"]
    assert (listed, exported) == (latest.number, latest.number)


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit (round 1, L2): the number of notes is counted as each page or export "
    "is made (reviews.html footer, docs/pages/audit_review/reviews.json).",
)
def test_an_export_keeps_the_note_count_shown_when_its_link_was_offered(client, reviewer):
    review = sign(reviewer)
    client.force_login(reviewer)
    first = client.get(reverse("audit_review:reviews"))
    [(number, count)] = [(row.number, row.note_count) for row in first.context["page_obj"]]
    assert number == review.number and count == 0
    reviews.add_note(
        actor=reviewer, review=review, text="A later observation.", expected_revision=1
    )
    [row] = review_csv(client.get(first.context["export_url"]))
    assert (row["Review"], int(row["Notes"])) == (number, count)


def test_ordinary_later_signings_stay_out_of_the_pager_and_offered_export(client, reviewer):
    records = [sign(reviewer).number for _ in range(51)][::-1]
    client.force_login(reviewer)
    first = client.get(reverse("audit_review:reviews"))
    second_url = review_next_link(first)
    assert "upto=" in second_url and "upto=" in first.context["export_url"]
    sign(reviewer)
    assert page_numbers(first) + page_numbers(client.get(second_url)) == records
    assert [row["Review"] for row in review_csv(client.get(first.context["export_url"]))] == records


def test_odd_reading_markers_clamp_or_start_a_fresh_reading(client, reviewer):
    old, new = sign(reviewer), sign(reviewer)
    client.force_login(reviewer)
    for value, expected in [
        ("0", []),
        (str(old.pk), [old.number]),
        ("", [new.number, old.number]),
        ("-3", [new.number, old.number]),
        ("9" * 19, [new.number, old.number]),
        ("9" * 20, [new.number, old.number]),
        ("１２", [new.number, old.number]),
    ]:
        response = client.get(reverse("audit_review:reviews"), {"upto": value})
        assert page_numbers(response) == expected, value
        assert [
            row["Review"] for row in review_csv(client.get(response.context["export_url"]))
        ] == expected


def test_review_csv_headers_and_text_cells(client, make_user):
    writer = make_user("@csv-author", "supervisor", first_name="=Synthetic", last_name="Éxample")
    writer.user_permissions.add(review_permission())
    review = sign(writer, examined_other="+fixture", findings="-fixture", follow_up="＠fixture")
    reviews.add_note(
        actor=writer, review=review, text="Only on the review page.", expected_revision=1
    )
    client.force_login(writer)
    response = client.get(reverse("audit_review:reviews-export"))
    [row] = review_csv(response)
    assert list(row) == [
        "Review",
        "From",
        "To",
        "Lists examined",
        "Other records examined",
        "Outcome",
        "Findings",
        "Follow-up",
        "Signed by",
        "Username",
        "Roles",
        "Meaning",
        "Signed at",
        "SHA-256 checksum",
        "Notes",
    ]
    assert row["Review"] == review.number and row["Notes"] == "1"
    assert row["SHA-256 checksum"] == review.checksum
    assert [
        row[name]
        for name in ("Other records examined", "Findings", "Follow-up", "Signed by", "Username")
    ] == ["'+fixture", "'-fixture", "'＠fixture", "'=Synthetic Éxample", "'@csv-author"]
    assert b"Only on the review page." not in response.content
    assert re.fullmatch(
        r'attachment; filename="audit-review-reviews-\d{8}-\d{6}\.csv"',
        response["Content-Disposition"],
    )
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert "no-store" in response["Cache-Control"]


def test_use_records_match_the_responses_that_displayed_each_review_surface(
    client, reviewer, monkeypatch
):
    review = sign(reviewer)
    client.force_login(reviewer)
    root = reverse("audit_review:reviews")
    detail = reverse("audit_review:review", args=[review.pk])
    export = reverse("audit_review:reviews-export")
    uses = WorkspaceUse.objects.filter(source=Source.REVIEWS)
    for url, kind, filters in [
        (root, UseKind.VIEWED, {"page": "1"}),
        (detail, UseKind.VIEWED, {"review": str(review.pk)}),
        (export, UseKind.EXPORTED, {}),
    ]:
        response = client.get(url)
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
        use = uses.get(request_ref=response["X-Request-ID"])
        assert (use.account_id, use.rows, use.kind, use.filters) == (reviewer.pk, 1, kind, filters)
    for url in (root, detail, export):
        for method in (client.head, client.post, client.put, client.options):
            assert method(url).status_code == 405
    for name, args in [
        ("review-create", ()),
        ("review-note", [review.pk]),
        ("review-interval", ()),
    ]:
        response = client.get(reverse("audit_review:" + name, args=args))
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert client.get(reverse("audit_review:review", args=[review.pk + 1])).status_code == 404
    monkeypatch.setattr(review_views, "EXPORT_LIMIT", 0)
    assert client.get(export).status_code == 302
    assert uses.count() == 3
    use_history = client.get(reverse("audit_review:uses")).content
    assert f"Review {review.number}".encode() in use_history
    reviews.set_interval(
        actor=reviewer, interval_days=47, expected_revision=0, reason="Fixture schedule."
    )
    reviews.add_note(actor=reviewer, review=review, text="Fixture note.", expected_revision=1)
    changes = client.get(reverse("audit_review:changes")).content
    assert changes.count(f"Audit review {review.number}".encode()) >= 2
    assert b"Audit review interval" in changes
    assert b"Fixture note." not in changes and b"Fixture schedule." not in changes


def test_review_pages_and_export_fail_closed_if_the_use_cannot_be_written(
    client, reviewer, monkeypatch
):
    review = sign(reviewer, findings="Synthetic private content.")
    client.force_login(reviewer)
    client.raise_request_exception = False

    def unavailable(*args, **kwargs):
        raise DatabaseError("Synthetic recording outage")

    monkeypatch.setattr(WorkspaceUse.objects, "bulk_create", unavailable)
    for url in (
        reverse("audit_review:reviews"),
        reverse("audit_review:review", args=[review.pk]),
        reverse("audit_review:reviews-export"),
    ):
        response = client.get(url)
        assert response.status_code == 500
        assert review.number.encode() not in response.content
        assert b"Synthetic private content." not in response.content
    assert not WorkspaceUse.objects.exists()


def test_a_server_error_answer_discards_the_list_use(client, reviewer, monkeypatch):
    render = review_views.render

    def unavailable(*args, **kwargs):
        response = render(*args, **kwargs)
        response.status_code = 503
        return response

    client.force_login(reviewer)
    monkeypatch.setattr(review_views, "render", unavailable)
    response = client.get(reverse("audit_review:reviews"))
    assert response.status_code == 503
    assert not WorkspaceUse.objects.exists()


def _posted_revision(response):
    assert response.status_code == 200
    return int(re.search(rb'name="expected_revision" value="(\d+)"', response.content)[1])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["note", "interval"])
def test_two_reviewer_forms_refuse_one_concurrent_save_and_allow_that_input_again(
    reviewer, make_user, operation
):
    colleague = make_user("second-reviewer", "technical", first_name="Ty", last_name="Lane")
    colleague.user_permissions.add(review_permission())
    users = [reviewer, colleague]
    clients = [Client(), Client()]
    for client, user in zip(clients, users, strict=True):
        client.force_login(user)

    review = sign(reviewer)
    if operation == "note":
        url = reverse("audit_review:review-note", args=[review.pk])
        bodies = [{"text": "Verified count A."}, {"text": "Verified count B."}]
        marker = "text"
        current_revision = 1
        audit_action = reviews.NOTE_ADDED
    else:
        url = reverse("audit_review:review-interval")
        bodies = [
            {"interval_days": 7, "reason": "Weekly procedure."},
            {"interval_days": 14, "reason": "Fortnightly procedure."},
        ]
        marker = "reason"
        current_revision = 0
        audit_action = reviews.INTERVAL_CHANGED

    for client, body in zip(clients, bodies, strict=True):
        body["expected_revision"] = _posted_revision(client.get(url))
        assert body["expected_revision"] == current_revision

    # Both POSTs have validated and hold their own account before either locks the shared record.
    both_at_save = Barrier(2, timeout=10)
    reviewer_at_save = reviews._reviewer
    pids = set()

    def meet_after_account_hold(actor):
        current = reviewer_at_save(actor)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids.add(cursor.fetchone()[0])
        both_at_save.wait()
        return current

    def post_from_session(index):
        try:
            return clients[index].post(url, bodies[index])
        finally:
            connections.close_all()

    with patch.object(reviews, "_reviewer", meet_after_account_hold), ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(post_from_session, range(2)))

    assert len(pids) == 2
    assert sorted(r.status_code for r in responses) == [200, 302]
    loser = next(i for i, response in enumerate(responses) if response.status_code == 200)
    winner = 1 - loser
    refused = responses[loser]
    assert bodies[loser][marker].encode() in refused.content
    assert bodies[winner][marker].encode() in refused.content
    assert _posted_revision(refused) == current_revision + 1
    first_event = AuditEvent.objects.get(action=audit_action)
    assert first_event.actor_id == users[winner].pk
    if operation == "note":
        note = review.notes.get()
        assert note.text == bodies[winner]["text"]
        assert note.author_id == users[winner].pk
    else:
        stored = ReviewSchedule.objects.get()
        assert (stored.revision, stored.interval_days) == (1, bodies[winner]["interval_days"])
        assert f"every {stored.interval_days} days".encode() in refused.content
        assert first_event.before == {"interval_days": None}
        assert first_event.after == {"interval_days": stored.interval_days}

    bodies[loser]["expected_revision"] = _posted_revision(refused)
    retry = clients[loser].post(url, bodies[loser])
    assert retry.status_code == 302
    second_event = AuditEvent.objects.filter(action=audit_action).exclude(pk=first_event.pk).get()
    assert second_event.actor_id == users[loser].pk
    if operation == "note":
        assert list(review.notes.values_list("text", flat=True)) == [
            bodies[winner]["text"],
            bodies[loser]["text"],
        ]
        assert reviews.intact(Review.objects.get(pk=review.pk))
    else:
        stored.refresh_from_db()
        assert (stored.revision, stored.interval_days) == (2, bodies[loser]["interval_days"])
        assert second_event.reason == bodies[loser]["reason"]
        assert second_event.before == first_event.after
        assert second_event.after == {"interval_days": stored.interval_days}


def test_signing_uses_the_name_username_and_remaining_role_at_save(client, reviewer):
    from django.contrib.auth.models import Group

    technical = Group.objects.get_or_create(name="technical")[0]
    reviewer.groups.add(technical)
    client.force_login(reviewer)
    url = reverse("audit_review:review-create")
    opened = client.get(url)
    assert b"Rae Viewer" in opened.content
    reviewer.first_name = "Updated"
    reviewer.username = "updated-reviewer"
    reviewer.save(update_fields=["first_name", "username"])
    reviewer.groups.remove(technical)
    signed = client.post(url, form_data())
    assert signed.status_code == 302
    stored = Review.objects.get()
    assert (
        stored.signed_by_id,
        stored.signer_name,
        stored.signer_username,
        stored.signer_roles,
    ) == (reviewer.pk, "Updated Viewer", "updated-reviewer", "Supervisor")
    assert reviews.intact(stored)


@pytest.mark.django_db(transaction=True)
def test_signer_waiting_for_a_role_edit_keeps_the_roles_and_name_after_it(reviewer):
    from django.contrib.auth.models import Group

    extra_role = Group.objects.get_or_create(name="technical")[0]
    reviewer.groups.add(extra_role)
    worker_pids = Queue()

    def sign_in_another_transaction():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                worker_pids.put(cursor.fetchone()[0])
            return sign(reviewer).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        with transaction.atomic():
            # The reviewer retains their original supervisor role and direct capability.
            reviewer.groups.remove(extra_role)
            reviewer.first_name = "Avery"
            reviewer.save(update_fields=["first_name"])
            job = pool.submit(sign_in_another_transaction)
            pid = worker_pids.get(timeout=10)
            with connection.cursor() as cursor:
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    if cursor.fetchone()[0]:
                        break
                    sleep(0.02)
                else:
                    pytest.fail("The signing transaction never waited for the account edit")
        stored = Review.objects.get(pk=job.result(timeout=10))
    assert (stored.signer_name, stored.signer_roles) == ("Avery Viewer", "Supervisor")
    assert reviews.intact(stored)


def test_note_retains_author_and_application_time_after_the_author_is_renamed(reviewer, make_user):
    review = sign(reviewer)
    colleague = make_user("follow-up-reviewer", "technical", first_name="Ty", last_name="Lane")
    colleague.user_permissions.add(review_permission())
    before = reviews.checksum(review)
    marked_time = datetime(2026, 7, 3, 1, 15, 7, 123456, tzinfo=UTC)
    with patch("django.utils.timezone.now", return_value=marked_time):
        saved = reviews.add_note(
            actor=colleague, review=review, expected_revision=1, text="Recount complete."
        )
    colleague.first_name = "Changed"
    colleague.save(update_fields=["first_name"])
    saved.refresh_from_db()
    assert (saved.author_id, saved.author_name, saved.added_at) == (
        colleague.pk,
        "Ty Lane",
        marked_time,
    )
    review.refresh_from_db()
    assert reviews.intact(review) and review.checksum == before


@pytest.mark.parametrize("operation", ["sign", "note"])
def test_account_name_allowed_by_both_profile_fields_fits_attribution(client, reviewer, operation):
    review = sign(reviewer) if operation == "note" else None
    reviewer.first_name = "A" * reviewer._meta.get_field("first_name").max_length
    reviewer.last_name = "B" * reviewer._meta.get_field("last_name").max_length
    reviewer.full_clean()
    reviewer.save(update_fields=["first_name", "last_name"])
    client.force_login(reviewer)
    if operation == "sign":
        url = reverse("audit_review:review-create")
        assert client.post(url, form_data()).status_code == 302
        assert Review.objects.get().signer_name == reviewer.get_full_name()
    else:
        url = reverse("audit_review:review-note", args=[review.pk])
        body = {"expected_revision": _posted_revision(client.get(url)), "text": "Checked count."}
        assert client.post(url, body).status_code == 302
        assert review.notes.get().author_name == reviewer.get_full_name()


@pytest.mark.parametrize("operation", ["sign", "note", "interval"])
def test_failed_audit_write_rolls_back_each_review_mutation(reviewer, monkeypatch, operation):
    review = sign(reviewer) if operation == "note" else None

    def unavailable(**kwargs):
        raise RuntimeError("Synthetic audit write failure")

    monkeypatch.setattr(reviews, "record_event", unavailable)
    with pytest.raises(RuntimeError, match="Synthetic audit write failure"):
        if operation == "sign":
            sign(reviewer)
        elif operation == "note":
            reviews.add_note(actor=reviewer, review=review, expected_revision=1, text="Checked.")
        else:
            reviews.set_interval(
                actor=reviewer, interval_days=8, expected_revision=0, reason="Trial."
            )
    assert Review.objects.count() == (1 if operation == "note" else 0)
    assert not ReviewNote.objects.exists()
    assert not ReviewSchedule.objects.exists()
    assert list(
        AuditEvent.objects.filter(action__startswith="audit_review.").values_list(
            "action", flat=True
        )
    ) == ([reviews.SIGNED] if operation == "note" else [])


@pytest.mark.parametrize("source", ["field", "help"])
def test_interval_reason_guidance_names_a_place_that_displays_it(
    client, reviewer, settings, source
):
    reason = "Synthetic choice of weekly review."
    reviews.set_interval(actor=reviewer, interval_days=7, expected_revision=0, reason=reason)
    client.force_login(reviewer)
    displayed_interval = client.get(reverse("audit_review:review-interval"))
    displayed_changes = client.get(reverse("audit_review:changes"))
    assert reason.encode() in displayed_interval.content
    assert b"Audit review interval" in displayed_changes.content
    assert reason.encode() not in displayed_changes.content
    if source == "field":
        guidance = displayed_interval.context["form"].fields["reason"].help_text
    else:
        path = Path(settings.BASE_DIR) / "docs/pages/audit_review/review-interval.json"
        help_items = (
            item
            for section in json.loads(path.read_text())["sections"]
            for item in section.get("items", [])
        )
        guidance = next(item["description"] for item in help_items if item["label"] == "Reason")
    assert "and under Changes" not in guidance, guidance


@pytest.mark.django_db(transaction=True)
def test_interval_history_agrees_with_current_value_and_revision_on_stale_redraw(
    client, reviewer, make_user, monkeypatch
):
    url = reverse("audit_review:review-interval")
    other = make_user("schedule-editor", "supervisor")
    other.user_permissions.add(review_permission())
    editor = Client()
    editor.force_login(other)
    client.force_login(reviewer)
    initial = client.get(url)
    assert b'name="expected_revision" value="0"' in initial.content
    first = {"expected_revision": "0", "interval_days": "7", "reason": "Weekly review."}
    assert editor.post(url, first).status_code == 302
    editor_form = editor.get(url)
    assert b'name="expected_revision" value="1"' in editor_form.content

    # Adapted (round 1, M3): the redraw reads the interval and its history holding the interval's
    # lock shared, so the other reviewer's save waits for it instead of committing between the two
    # readings. The save runs in its own thread, and the history is read once it waits.
    from django.db import connection as reading_connection

    history_requested = Event()
    read_history = reviews.interval_history

    def read_history_while_another_save_waits(*args, **kwargs):
        history_requested.set()
        deadline = monotonic() + 10
        with reading_connection.cursor() as cursor:
            while monotonic() < deadline:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = %s AND NOT granted",
                    [reviews.SCHEDULE_LOCK[0]],
                )
                if cursor.fetchone()[0]:
                    break
                sleep(0.02)
        return read_history(*args, **kwargs)

    monkeypatch.setattr(reviews, "interval_history", read_history_while_another_save_waits)

    def submit_stale_form():
        try:
            return client.post(url, {**first, "interval_days": "9", "reason": "Trial schedule."})
        finally:
            connections.close_all()

    def other_save():
        try:
            return editor.post(
                url, {"expected_revision": "1", "interval_days": "14", "reason": "Fortnightly."}
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as worker:
        stale = worker.submit(submit_stale_form)
        assert history_requested.wait(10)
        changed = worker.submit(other_save)
        refused = stale.result(timeout=20)
        assert changed.result(timeout=20).status_code == 302
    assert refused.status_code == 200
    assert reviews.STALE_INTERVAL.encode() in refused.content
    assert b"Trial schedule." in refused.content
    html = refused.content.decode()
    current_days = int(re.search(r"Now: every (\d+) days", html)[1])
    revision = int(re.search(r'name="expected_revision" value="(\d+)"', html)[1])
    history = html.split("Changes to the interval (", 1)[1]
    latest_history_days = int(re.search(r"every (\d+) days", history)[1])
    # The redraw read before the other save committed: value, revision and newest event agree.
    assert (revision, current_days, latest_history_days) == (1, 7, 7)
    assert reviews.schedule().interval_days == 14


def _review_migration(name):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    target = [("audit_review", name)]
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    return executor.loader.project_state(target).apps


@pytest.mark.django_db(transaction=True)
def test_review_upgrade_keeps_workspace_uses_from_a_running_older_release():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connection, connections, transaction

    from audit_review.models import Review, ReviewSchedule, WorkspaceUse

    previous = "0002_people_summary"
    latest = "0003_review_records"
    old_apps = _review_migration(previous)
    old_use = old_apps.get_model("audit_review", "WorkspaceUse")
    altering = Event()

    def upgrade():
        def observe(execute, sql, params, many, ctx):
            if sql.startswith('ALTER TABLE "audit_review_workspaceuse" DROP CONSTRAINT'):
                altering.set()
            return execute(sql, params, many, ctx)

        try:
            with connection.execute_wrapper(observe):
                _review_migration(latest)
        finally:
            connections.close_all()

    def record(source):
        return old_use.objects.create(
            source=source, kind="viewed", account_id=112, filters={"page": "1"}, rows=3
        )

    try:
        original = [
            record(source) for source in ("access", "changes", "people", "sign_ins", "uses")
        ]
        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                overlapping = record("changes")
                applying = workers.submit(upgrade)
                assert altering.wait(timeout=15), "migration did not reach the workspace table"
            applying.result(timeout=30)
        original.extend([overlapping, record("uses")])  # still the older release's ORM
        actual = WorkspaceUse.objects.in_bulk()
        assert set(actual) == {row.pk for row in original}
        for row in original:
            current = actual[row.pk]
            assert (current.source, current.occurred_at, current.filters, current.rows) == (
                row.source,
                row.occurred_at,
                {"page": "1"},
                3,
            )
        assert not Review.objects.exists()
        assert not ReviewSchedule.objects.exists()
        _review_migration(previous)
        assert set(old_use.objects.values_list("pk", flat=True)) == set(actual)
    finally:
        _review_migration(latest)


@pytest.mark.parametrize("review_exists,use_exists", [(True, False), (False, True), (True, True)])
@pytest.mark.django_db(transaction=True)
def test_rollback_preserves_signed_review_or_reviews_use(review_exists, use_exists):
    from django.contrib.auth.models import Group
    from django.db import DatabaseError
    from django.db.migrations.recorder import MigrationRecorder

    from accounts.models import User
    from audit_review.models import Review, WorkspaceUse
    from audit_review.tests.conftest import review_permission
    from audit_review.tests.test_reviews import sign

    if review_exists:
        user = User.objects.create_user("rollback-reviewer")
        user.groups.add(Group.objects.get_or_create(name="supervisor")[0])
        user.user_permissions.add(review_permission())
        signed = sign(user)
    if use_exists:
        shown = WorkspaceUse.objects.create(kind="viewed", source="reviews", account_id=112, rows=0)
    latest = "0003_review_records"
    try:
        with pytest.raises(DatabaseError) as refused:
            _review_migration("0002_people_summary")
        reason = "Signed reviews exist" if review_exists else "workspace_use_source"
        assert reason in str(refused.value)
        assert MigrationRecorder.Migration.objects.filter(app="audit_review", name=latest).exists()
        if review_exists:
            assert Review.objects.get().checksum == signed.checksum
        if use_exists:
            assert WorkspaceUse.objects.get().pk == shown.pk
    finally:
        _review_migration(latest)


@pytest.mark.django_db(transaction=True)
def test_seed_and_reset_review_records_early_on_the_first_of_the_month(monkeypatch):
    from datetime import UTC, date, datetime
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    from audit_review import reviews
    from audit_review.models import Review, ReviewNote, ReviewSchedule
    from core.models import AuditEvent

    now = datetime(2026, 6, 1, 4, 30, tzinfo=UTC)  # 00:30 in the laboratory
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-seed-only")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setattr(timezone, "now", lambda: now)
    options = {"stdout": StringIO(), "stderr": StringIO(), "verbosity": 0}
    with timezone.override("America/New_York"):
        call_command("seed_demo", **options)
        review = Review.objects.get()
        note = review.notes.get()
        assert (review.period_from, review.period_to) == (date(2026, 5, 1), date(2026, 5, 31))
        assert review.period_to <= timezone.localdate(review.signed_at)
        assert review.signed_at < note.added_at <= now
        assert reviews.intact(review)
        schedule = ReviewSchedule.objects.get()
        assert schedule.interval_days == 31
        reviews.set_interval(
            actor=review.signed_by,
            interval_days=45,
            expected_revision=schedule.revision,
            reason="Synthetic procedure revision",
        )
        call_command("seed_demo", **options)
        assert Review.objects.get().checksum == review.checksum
        assert ReviewNote.objects.get().pk == note.pk
        assert ReviewSchedule.objects.get().interval_days == 45
        assert AuditEvent.objects.filter(action=reviews.SIGNED).count() == 1
        assert AuditEvent.objects.filter(action=reviews.NOTE_ADDED).count() == 1
        call_command("reset_demo", interactive=False, **options)
        assert reviews.intact(Review.objects.get())
        assert ReviewNote.objects.get().review == Review.objects.get()
        assert ReviewSchedule.objects.get().interval_days == 31
        assert AuditEvent.objects.filter(action=reviews.INTERVAL_CHANGED).count() == 1


@pytest.mark.django_db
def test_demonstration_review_page_describes_its_actual_signoff(client, monkeypatch):
    from io import StringIO

    from django.core.management import call_command
    from django.urls import reverse

    from audit_review.models import Review

    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-review-seed-only")
    call_command("seed_demo", stdout=StringIO(), verbosity=0)
    example = Review.objects.get()
    client.force_login(example.signed_by)
    detail = client.get(reverse("audit_review:review", args=[example.pk]))
    assert detail.status_code == 200 and example.checksum.encode() in detail.content
    assert b"Signed with the reviewer&#x27;s password entered at signing" not in detail.content
    assert b"Signed with the reviewer's password entered at signing" not in detail.content


@pytest.mark.parametrize("surface", ["signing", "earlier_reauthentication"])
def test_password_frame_locals_obey_the_production_error_report_filter(
    client, reviewer, settings, surface
):
    """Check the production filter on a successful synthetic request, without emitting a report."""
    import sys

    from allauth.core import context
    from django.views.debug import get_exception_reporter_filter

    settings.DEBUG = False
    client.force_login(reviewer)
    checked = set()
    unredacted = set()

    if surface == "signing":
        expected = {
            ("audit_review.review_views", "review_create"),
            ("audit_review.review_forms", "signing_refusal"),
        }
        path, data = route("review-create"), form_data()
    else:
        expected = {("accounts.adapter", "reauthenticate")}
        path, data = reverse("account_reauthenticate"), {"password": PASSWORD}

    def check_frame(frame, event, arg):
        if event != "return":
            return
        location = (frame.f_globals.get("__name__"), frame.f_code.co_name)
        if location not in expected:
            return
        checked.add(location)
        request = context.request
        reporter_filter = get_exception_reporter_filter(request)
        for name, value in reporter_filter.get_traceback_frame_variables(request, frame):
            if isinstance(value, (str, dict)) and PASSWORD in str(value):
                unredacted.add((frame.f_code.co_name, name))

    previous = sys.getprofile()
    try:
        sys.setprofile(check_frame)
        response = client.post(path, data)
    finally:
        sys.setprofile(previous)
    assert response.status_code == 302
    assert checked == expected
    assert not unredacted


@pytest.mark.parametrize("record_type", ["review", "note"])
def test_mutations_are_refused_by_the_append_only_trigger_itself(reviewer, record_type):
    record = sign(reviewer)
    if record_type == "note":
        record = reviews.add_note(
            actor=reviewer, review=record, expected_revision=1, text="Fixture"
        )
    rows = type(record).objects.filter(pk=record.pk)
    for delete in (False, True):
        with pytest.raises(DatabaseError, match="Historical records cannot be updated or deleted"):
            with transaction.atomic():
                if delete:
                    rows.delete()
                else:
                    rows.update(request_ref="fixture")
    assert rows.exists()


@pytest.mark.parametrize("removed", ["role", "grant"])
@pytest.mark.django_db(transaction=True)
def test_waiting_mutations_recheck_the_last_required_role_or_grant(reviewer, removed):
    from django.core.exceptions import PermissionDenied

    record = sign(reviewer)
    ready = Queue()
    operations = {
        "sign": lambda: sign(reviewer),
        "note": lambda: reviews.add_note(
            actor=reviewer, review=record, text="Follow-up", expected_revision=1
        ),
        "interval": lambda: reviews.set_interval(
            actor=reviewer, interval_days=7, expected_revision=0, reason="Fixture"
        ),
    }

    def guarded_save(name):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                ready.put(cursor.fetchone()[0])
            with pytest.raises(PermissionDenied):
                operations[name]()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(3) as workers:
        with transaction.atomic():
            if removed == "role":
                reviewer.groups.clear()
            else:
                reviewer.user_permissions.remove(review_permission())
            futures = [workers.submit(guarded_save, name) for name in operations]
            pids = [ready.get(timeout=10) for _ in operations]
            with connection.cursor() as cursor:
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    cursor.execute(
                        "SELECT bool_and(pg_backend_pid() = ANY(pg_blocking_pids(pid))) "
                        "FROM unnest(%s::int[]) AS pid",
                        [pids],
                    )
                    if cursor.fetchone()[0]:
                        break
                    sleep(0.02)
                else:
                    pytest.fail("Expected all three writers to wait for the access change")
        for future in futures:
            future.result(timeout=10)
    assert not ReviewNote.objects.exists() and not ReviewSchedule.objects.exists()
    assert list(Review.objects.values_list("pk", flat=True)) == [record.pk]


def test_note_form_shows_the_notes_so_far_and_its_revision_begins_when_it_opens(
    client, reviewer, make_user
):
    # Adapted (round 1, L3): the help now says the revision begins when the note form opens, and
    # the form shows the notes so far, unfolded, so a note added after the review page was opened
    # is in front of the reviewer before they add theirs.
    record = sign(reviewer)
    colleague = make_user("note-colleague", "technical")
    colleague.user_permissions.add(review_permission())
    other = Client()
    other.force_login(colleague)
    client.force_login(reviewer)
    detail = client.get(route("review", record.pk))
    path = re.search(r'href="([^"]+)">Add a note</a>', detail.content.decode())[1]
    assert b"No notes." in detail.content
    other_form = other.get(path)
    assert (
        other.post(
            path, {"expected_revision": _posted_revision(other_form), "text": "Colleague follow-up"}
        ).status_code
        == 302
    )
    opened = client.get(path)
    assert b"Colleague follow-up" in opened.content
    assert b'<details class="vr-form-note vr-disclosure" open>' in opened.content
    from django.conf import settings

    help_text = (Path(settings.BASE_DIR) / "docs/pages/audit_review/review-note.json").read_text()
    assert "after you opened this form" in help_text
    reply = client.post(
        path, {"expected_revision": _posted_revision(opened), "text": "My follow-up"}
    )
    assert reply.status_code == 302
    assert record.notes.count() == 2


def test_earlier_workspace_list_and_export_keep_the_first_reading(client, reviewer):
    """Cross-version control on the unchanged Workspace use surface with no concurrent writer."""
    client.force_login(reviewer)
    use = WorkspaceUse.objects.create(
        account_id=reviewer.pk,
        source="changes",
        kind="viewed",
        rows=4,
        request_ref="fixture-earlier",
    )
    first = client.get(route("uses"))
    offered_export = first.context["export_url"]
    assert "upto=" in offered_export and first.context["total"] == 1
    assert WorkspaceUse.objects.count() == 2
    exported = client.get(offered_export)
    assert exported.status_code == 200 and "no-store" in exported["Cache-Control"]
    assert use.request_ref.encode() in exported.content
    assert first["X-Request-ID"].encode() not in exported.content
    assert WorkspaceUse.objects.get(request_ref=exported["X-Request-ID"]).rows == 1


@pytest.mark.django_db(transaction=True)
def test_earlier_workspace_inflight_exception_is_visible_on_its_offered_export(client, reviewer):
    """The older lists explicitly document this exception (docs/architecture.md, Audit review)."""
    client.force_login(reviewer)
    reserved, finish = Event(), Event()

    def old_write():
        try:
            with transaction.atomic():
                WorkspaceUse.objects.create(
                    account=reviewer,
                    source="changes",
                    kind="viewed",
                    rows=1,
                    request_ref="fixture-inflight",
                )
                reserved.set()
                assert finish.wait(15)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as worker:
        future = worker.submit(old_write)
        try:
            assert reserved.wait(10)
            WorkspaceUse.objects.create(
                account=reviewer,
                source="changes",
                kind="viewed",
                rows=1,
                request_ref="fixture-visible",
            )
            first = client.get(route("uses"))
            assert first.context["total"] == 1 and b"fixture-inflight" not in first.content
        finally:
            finish.set()
        future.result(timeout=10)
    exported = client.get(first.context["export_url"])
    assert b"fixture-inflight" in exported.content and b"fixture-visible" in exported.content


def test_the_password_changed_in_the_same_browser_is_used_at_the_next_signing(client, reviewer):
    client.force_login(reviewer)
    url = route("review-create")
    assert client.get(url).status_code == 200
    current = "copper orchard candle fixture"
    changed = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": current, "password2": current},
    )
    assert changed.status_code == 302
    reviewer.refresh_from_db()
    assert reviewer.check_password(current)
    refused = client.post(url, form_data())
    assert "Incorrect password" in str(refused.context["form"].errors)
    assert not Review.objects.exists()
    assert client.post(url, form_data(confirm_password=current)).status_code == 302
    assert Review.objects.get().signed_by_id == reviewer.pk


@pytest.mark.confirmation
def test_signing_with_an_enrolled_authenticator_requires_only_the_password(client, reviewer):
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret

    TOTP.activate(reviewer, generate_totp_secret())
    client.force_login(reviewer)
    url = route("review-create")
    fields = client.get(url).context["form"].fields
    assert confirmation.PASSWORD_FIELD in fields and confirmation.CODE_FIELD not in fields
    assert client.post(url, form_data()).status_code == 302
    assert not confirmation.current(client.get(url).wsgi_request)
    assert Review.objects.get().signed_by_id == reviewer.pk


def test_database_insert_constraints_identify_the_invalid_field_groups(reviewer):
    from django.db import IntegrityError

    original = sign(reviewer)
    for constraint, changes in {
        "review_period_order": {"period_from": original.period_to + timedelta(days=1)},
        "review_outcome": {"outcome": "other"},
        "review_meaning": {"meaning": "other"},
        "review_findings_with_follow_up": {"outcome": "follow_up", "findings": ""},
        "review_examined": {"examined": [], "examined_other": ""},
        "review_examined_lists": {"examined": ["reviews"]},
        "review_checksum": {"checksum": "invalid"},
    }.items():
        candidate = copy(original)
        candidate.pk = None
        candidate.number = "AR-fixture"
        for name, value in changes.items():
            setattr(candidate, name, value)
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            candidate.save(force_insert=True)
        assert refused.value.__cause__.diag.constraint_name == constraint
    with pytest.raises(IntegrityError, match="review_note_text"), transaction.atomic():
        ReviewNote.objects.create(review=original, text="", author=reviewer, author_name="Fixture")
    for pk, days, constraint in (
        (2, 5, "review_schedule_single"),
        (1, 367, "review_schedule_interval_bounds"),
    ):
        with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
            ReviewSchedule.objects.create(pk=pk, interval_days=days)


def test_architecture_describes_the_writes_made_by_the_review_workspace(client, reviewer, settings):
    client.force_login(reviewer)
    assert client.post(route("review-create"), form_data()).status_code == 302
    assert Review.objects.count() == 1 and AuditEvent.objects.filter(action=reviews.SIGNED).exists()
    section = (Path(settings.BASE_DIR) / "docs/architecture.md").read_text()
    section = section.split("## Audit review workspace", 1)[1].split("\n## ", 1)[0]
    assert "writes only the last" not in section


def test_coverage_help_distinguishes_an_internal_gap_from_the_last_day_reviewed(
    client, reviewer, settings
):
    today = timezone.localdate()
    for day in (today - timedelta(days=2), today):
        sign(reviewer, period_from=day, period_to=day)
    client.force_login(reviewer)
    state = client.get(route("reviews")).context["state"]
    assert state.covered_to == today and state.unreviewed_from is None
    assert state.gaps == [(today - timedelta(days=1), today - timedelta(days=1))]
    help_file = Path(settings.BASE_DIR) / "docs/pages/audit_review/reviews.json"
    steps = [step for task in json.loads(help_file.read_text())["tasks"] for step in task["steps"]]
    assert not any(
        "[[Reviewed up to]] for the first day no review covers" in step for step in steps
    )


def test_signing_specification_names_an_existing_form():
    from audit_review import review_forms

    mentioned = re.findall(r"audit_review\.review_forms\.(\w+)", reviews.__doc__)
    assert all(hasattr(review_forms, name) for name in mentioned), mentioned


@pytest.mark.parametrize("operation", ["note", "interval"])
def test_notes_and_interval_reasons_share_their_form_and_service_text_limits(reviewer, operation):
    from audit_review.review_forms import NoteForm

    record = sign(reviewer)
    for length in (4001, 4000):
        text = "x" * length
        if operation == "note":
            form = NoteForm({"text": text, "expected_revision": 1})
            field = "text"
            save = reviews.add_note
            values = {"review": record, "text": text, "expected_revision": 1}
        else:
            form = IntervalForm({"reason": text, "interval_days": 7, "expected_revision": 0})
            field = "reason"
            save = reviews.set_interval
            values = {"interval_days": 7, "reason": text, "expected_revision": 0}
        if length == 4001:
            assert field in form.errors
            with pytest.raises(ValidationError) as rejected:
                save(actor=reviewer, **values)
            assert field in rejected.value.message_dict
        else:
            assert form.is_valid()
            save(actor=reviewer, **values)
            if operation == "note":
                assert record.notes.get().text == text
            else:
                assert AuditEvent.objects.get(action=reviews.INTERVAL_CHANGED).reason == text
