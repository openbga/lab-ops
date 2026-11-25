"""Review records: recording and signing a review with the password entered again, the checksum of
what was signed, notes, the review interval and when the next review is due, who may do what, and
that the database keeps signed reviews and notes as written. All data is synthetic."""

import csv
import io
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import SignInEvent
from audit_review import reviews
from audit_review.models import Review, ReviewNote, ReviewSchedule, Source, UseKind, WorkspaceUse
from core.models import AuditEvent

from .conftest import PASSWORD, review_permission

FINDINGS = "Two after-hours ticket opens by one account, both on the on-call rota."


def sign(actor, **extra):
    today = timezone.localdate()
    values = {
        "period_from": today - timedelta(days=30),
        "period_to": today - timedelta(days=1),
        "examined": ["changes", "access"],
        "outcome": Review.Outcome.NOTHING,
    }
    values.update(extra)
    with transaction.atomic():
        return reviews.sign_review(actor=actor, **values)


def form_data(**extra):
    today = timezone.localdate()
    data = {
        "period_from": (today - timedelta(days=30)).isoformat(),
        "period_to": (today - timedelta(days=1)).isoformat(),
        "examined": ["changes", "sign_ins"],
        "examined_other": "",
        "outcome": "follow_up",
        "findings": FINDINGS,
        "follow_up": "Checked against the rota; nothing further.",
        "confirm_password": PASSWORD,
    }
    data.update(extra)
    return data


# ------------------------------------------------------------------------------ the service


def test_a_signed_review_keeps_the_signer_as_they_stood_and_a_checksum_of_what_was_signed(reviewer):
    review = sign(reviewer, findings="Nothing unusual.")
    assert review.number == f"AR-{review.pk:06d}"
    assert review.signer_name == "Rae Viewer"
    assert review.signer_username == "reviewer"
    assert review.signer_roles == "Supervisor"
    assert review.meaning == "reviewed"
    assert len(review.checksum) == 64
    assert reviews.intact(review)
    stored = Review.objects.get(pk=review.pk)
    assert reviews.intact(stored)
    assert stored.examined == ["changes", "access"]
    event = AuditEvent.objects.get(action=reviews.SIGNED)
    assert event.object_id == str(review.pk)
    assert event.after["checksum"] == review.checksum
    # A later change of the account's name does not change what was signed.
    reviewer.first_name = "Renamed"
    reviewer.save()
    assert reviews.intact(Review.objects.get(pk=review.pk))


def test_the_checksum_covers_every_signed_field(reviewer):
    review = sign(reviewer)
    for name, value in (
        ("findings", "Something else"),
        ("period_to", review.period_to - timedelta(days=1)),
        ("examined", ["changes"]),
        ("signer_roles", "Administrator"),
        ("signer_name", "Someone Else"),
        ("signed_at", review.signed_at + timedelta(microseconds=1)),
    ):
        copy = Review.objects.get(pk=review.pk)
        setattr(copy, name, value)
        assert not reviews.intact(copy), name


def test_the_database_refuses_to_change_or_delete_a_review_or_a_note(reviewer):
    review = sign(reviewer)
    note = reviews.add_note(actor=reviewer, review=review, text="A note.", expected_revision=1)
    for statement in (
        "UPDATE audit_review_review SET findings = 'changed' WHERE id = %s",
        "DELETE FROM audit_review_review WHERE id = %s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, [review.pk])
    for statement in (
        "UPDATE audit_review_reviewnote SET text = 'changed' WHERE id = %s",
        "DELETE FROM audit_review_reviewnote WHERE id = %s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, [note.pk])


def test_only_a_reviewer_signs(supervisor, agent, make_user):
    portal_admin = make_user("portal", client=None)
    for account in (supervisor, agent, portal_admin):
        with pytest.raises(PermissionDenied):
            sign(account)
    # The capability without a laboratory role opens nothing.
    lone = make_user("lone")
    lone.user_permissions.add(review_permission())
    with pytest.raises(PermissionDenied):
        sign(lone)
    assert not Review.objects.exists()


def test_a_capability_removed_before_the_save_refuses_it(reviewer):
    reviewer.user_permissions.remove(review_permission())
    with pytest.raises(PermissionDenied):
        sign(reviewer)


@pytest.mark.parametrize(
    "extra, field",
    [
        ({"period_to": date(2999, 1, 1)}, "period_to"),
        ({"period_from": date(2026, 1, 2), "period_to": date(2026, 1, 1)}, "period_to"),
        ({"period_from": date(2024, 1, 1), "period_to": date(2025, 1, 2)}, "period_to"),
        ({"examined": [], "examined_other": ""}, "examined"),
        ({"examined": ["reviews"]}, "examined"),
        ({"outcome": "follow_up", "findings": "  "}, "findings"),
        ({"outcome": "maybe"}, "outcome"),
        ({"findings": "x" * 4001}, "findings"),
    ],
)
def test_the_service_refuses_what_the_form_refuses(reviewer, extra, field):
    with pytest.raises(ValidationError) as caught:
        sign(reviewer, **extra)
    assert field in caught.value.error_dict


def test_other_records_alone_are_enough(reviewer):
    review = sign(reviewer, examined=[], examined_other="The hosting provider's sign-in log.")
    assert review.examined == []


def test_a_note_is_refused_when_another_was_added_since_the_page_was_read(reviewer):
    review = sign(reviewer)
    reviews.add_note(actor=reviewer, review=review, text="First.", expected_revision=1)
    with pytest.raises(ValidationError):
        reviews.add_note(actor=reviewer, review=review, text="Second.", expected_revision=1)
    reviews.add_note(actor=reviewer, review=review, text="Second.", expected_revision=2)
    assert [note.text for note in review.notes.order_by("pk")] == ["First.", "Second."]
    assert AuditEvent.objects.filter(action=reviews.NOTE_ADDED).count() == 2
    # Notes are not part of what was signed.
    assert reviews.intact(Review.objects.get(pk=review.pk))


def test_the_interval_is_set_with_a_reason_and_a_stale_form_is_refused(reviewer):
    stored, changed = reviews.set_interval(
        actor=reviewer, interval_days=31, expected_revision=0, reason="Monthly review."
    )
    assert changed and stored.interval_days == 31 and stored.revision == 1
    with pytest.raises(ValidationError):
        reviews.set_interval(
            actor=reviewer, interval_days=92, expected_revision=0, reason="Quarterly."
        )
    with pytest.raises(ValidationError):
        reviews.set_interval(actor=reviewer, interval_days=0, expected_revision=1, reason="x")
    stored, changed = reviews.set_interval(
        actor=reviewer, interval_days=None, expected_revision=1, reason="Paused."
    )
    assert changed and stored.interval_days is None
    event = AuditEvent.objects.filter(action=reviews.INTERVAL_CHANGED).order_by("-occurred_at")[0]
    assert event.before == {"interval_days": 31} and event.after == {"interval_days": None}
    assert event.reason == "Paused."


def test_status_counts_the_next_due_day_from_the_last_signing(reviewer):
    today = timezone.localdate()
    assert reviews.status().state == "unset"
    ReviewSchedule.objects.create(pk=1, interval_days=10)
    assert reviews.status().state == "none"
    review = sign(
        reviewer, period_from=today - timedelta(days=40), period_to=today - timedelta(days=31)
    )
    state = reviews.status()
    assert state.last == review
    assert state.due == timezone.localtime(review.signed_at).date() + timedelta(days=10)
    assert state.state == "due" and state.days_left == 10
    assert state.covered_to == today - timedelta(days=31)
    assert state.unreviewed_from == today - timedelta(days=30)
    assert reviews.status(today=today + timedelta(days=10)).state == "today"
    assert reviews.status(today=today + timedelta(days=11)).state == "overdue"


def test_gaps_are_the_days_between_reviewed_periods_that_no_review_covers():
    d = date(2026, 1, 1)
    periods = [
        (d, d + timedelta(days=9)),
        (d + timedelta(days=5), d + timedelta(days=12)),
        (d + timedelta(days=20), d + timedelta(days=25)),
        (d + timedelta(days=26), d + timedelta(days=30)),
        (d + timedelta(days=32), d + timedelta(days=32)),
    ]
    assert reviews.gaps(periods) == [
        (d + timedelta(days=13), d + timedelta(days=19)),
        (d + timedelta(days=31), d + timedelta(days=31)),
    ]
    assert reviews.gaps([]) == []


def test_the_suggested_period_starts_after_the_last_day_reviewed():
    today = date(2026, 11, 25)
    assert reviews.suggested_period(None, today) == (date(2026, 10, 26), date(2026, 11, 24))
    state = reviews.Status(interval=None, today=today, covered_to=date(2026, 10, 31))
    assert reviews.suggested_period(state, today) == (date(2026, 11, 1), date(2026, 11, 24))
    state.covered_to = today
    assert reviews.suggested_period(state, today) == (today, today)
    state.covered_to = date(2020, 1, 1)
    start, end = reviews.suggested_period(state, today)
    assert (end - start).days + 1 == reviews.MOST_DAYS


# ------------------------------------------------------------------------------ the pages


@pytest.mark.parametrize(
    "name, args",
    [
        ("reviews", ()),
        ("reviews-export", ()),
        ("review-create", ()),
        ("review-interval", ()),
    ],
)
def test_pages_refuse_accounts_without_the_capability(client, supervisor, name, args):
    client.force_login(supervisor)
    assert client.get(reverse(f"audit_review:{name}", args=args)).status_code == 403
    if name != "reviews" and name != "reviews-export":
        assert client.post(reverse(f"audit_review:{name}", args=args), {}).status_code == 403


def test_a_review_page_and_its_note_form_refuse_accounts_without_the_capability(
    client, reviewer, supervisor
):
    review = sign(reviewer)
    client.force_login(supervisor)
    for name in ("review", "review-note"):
        assert client.get(reverse(f"audit_review:{name}", args=[review.pk])).status_code == 403
    response = client.post(
        reverse("audit_review:review-note", args=[review.pk]),
        {"text": "Not allowed.", "expected_revision": 1},
    )
    assert response.status_code == 403
    assert not ReviewNote.objects.exists()


def test_recording_a_review_signs_it_with_the_password(client, reviewer):
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:review-create"))
    assert page.status_code == 200
    assert b"Sign-off" in page.content
    response = client.post(reverse("audit_review:review-create"), form_data())
    review = Review.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("audit_review:review", args=[review.pk])
    assert review.findings == FINDINGS
    assert review.examined == ["changes", "sign_ins"]
    page = client.get(response["Location"])
    assert page.status_code == 200
    assert review.checksum.encode() in page.content
    assert b"Matches the review as stored." in page.content
    assert FINDINGS.encode() in page.content
    # Its page is recorded as a use of the workspace.
    use = WorkspaceUse.objects.get(source=Source.REVIEWS)
    assert use.kind == UseKind.VIEWED and use.filters == {"review": str(review.pk)}


def test_a_wrong_password_signs_nothing_keeps_the_typing_and_is_recorded(client, reviewer):
    client.force_login(reviewer)
    response = client.post(
        reverse("audit_review:review-create"), form_data(confirm_password="wrong-password")
    )
    assert response.status_code == 200
    assert not Review.objects.exists()
    assert FINDINGS.encode() in response.content
    assert b"wrong-password" not in response.content
    assert b"Incorrect password." in response.content
    assert SignInEvent.objects.filter(
        user_id=reviewer.pk, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()
    assert not AuditEvent.objects.filter(action=reviews.SIGNED).exists()


def test_an_empty_password_or_a_refused_field_checks_no_password(client, reviewer):
    client.force_login(reviewer)
    client.post(reverse("audit_review:review-create"), form_data(confirm_password=""))
    client.post(reverse("audit_review:review-create"), form_data(examined=[]))
    assert not Review.objects.exists()
    assert not SignInEvent.objects.filter(
        user_id=reviewer.pk, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()


def test_the_password_never_reaches_the_audit_event_or_the_record(client, reviewer):
    client.force_login(reviewer)
    client.post(reverse("audit_review:review-create"), form_data())
    event = AuditEvent.objects.get(action=reviews.SIGNED)
    assert PASSWORD not in str(event.after) and PASSWORD not in str(event.before)
    assert PASSWORD not in str(Review.objects.values().get())


def test_the_list_shows_where_reviews_stand_and_records_its_use(client, reviewer):
    ReviewSchedule.objects.create(pk=1, interval_days=31)
    review = sign(reviewer, findings="Nothing.")
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:reviews"))
    assert page.status_code == 200
    assert review.number.encode() in page.content
    assert b"Every 31 days" in page.content
    use = WorkspaceUse.objects.get(source=Source.REVIEWS)
    assert use.filters == {"page": "1"} and use.rows == 1


def test_the_export_holds_every_review_and_is_recorded(client, reviewer):
    sign(reviewer, findings='=HYPERLINK("x")')
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:reviews-export"))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][0] == "Review" and len(rows) == 2
    assert rows[1][6].startswith("'=")  # formula-safe
    assert WorkspaceUse.objects.filter(source=Source.REVIEWS, kind=UseKind.EXPORTED).exists()


def test_adding_a_note_from_a_stale_page_keeps_the_typing(client, reviewer):
    review = sign(reviewer)
    reviews.add_note(actor=reviewer, review=review, text="Meanwhile.", expected_revision=1)
    client.force_login(reviewer)
    response = client.post(
        reverse("audit_review:review-note", args=[review.pk]),
        {"text": "My late note.", "expected_revision": 1},
    )
    assert response.status_code == 200
    assert b"My late note." in response.content
    assert ReviewNote.objects.count() == 1
    # Drawn again with the notes as they stand and the revision they make.
    assert b"Meanwhile." in response.content
    assert b'name="expected_revision" value="2"' in response.content
    response = client.post(
        reverse("audit_review:review-note", args=[review.pk]),
        {"text": "My late note.", "expected_revision": 2},
    )
    assert response.status_code == 302
    assert ReviewNote.objects.count() == 2


def test_the_interval_form_saves_with_a_reason(client, reviewer):
    client.force_login(reviewer)
    assert client.get(reverse("audit_review:review-interval")).status_code == 200
    response = client.post(
        reverse("audit_review:review-interval"),
        {"interval_days": "92", "reason": "Quarterly, per our procedure.", "expected_revision": 0},
    )
    assert response.status_code == 302
    assert ReviewSchedule.objects.get().interval_days == 92


def test_a_review_event_is_named_by_its_number_on_the_changes_list(client, reviewer):
    review = sign(reviewer)
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:changes"))
    assert f"Audit review {review.number}".encode() in page.content
    assert b"Audit review signed" in page.content


def test_a_portal_account_is_refused(client, make_user, clinic):
    portal = make_user("portal-user", client=clinic)
    portal.user_permissions.add(review_permission())
    client.force_login(portal)
    assert client.get(reverse("audit_review:reviews")).status_code in (403, 404)


def test_the_admin_role_alone_does_not_open_reviews(client, make_user):
    admin = make_user("admin-only", "admin")
    client.force_login(admin)
    assert client.get(reverse("audit_review:reviews")).status_code == 403
    assert Group.objects.filter(name="admin").exists()


def test_a_stale_interval_form_is_drawn_again_with_the_interval_as_it_stands(client, reviewer):
    reviews.set_interval(actor=reviewer, interval_days=31, expected_revision=0, reason="Monthly.")
    client.force_login(reviewer)
    response = client.post(
        reverse("audit_review:review-interval"),
        {"interval_days": "92", "reason": "Quarterly.", "expected_revision": 0},
    )
    assert response.status_code == 200
    assert ReviewSchedule.objects.get().interval_days == 31
    assert b"every 31 days" in response.content
    assert b'name="expected_revision" value="1"' in response.content
    assert b"Quarterly." in response.content
    response = client.post(
        reverse("audit_review:review-interval"),
        {"interval_days": "92", "reason": "Quarterly.", "expected_revision": 1},
    )
    assert response.status_code == 302
    assert ReviewSchedule.objects.get().interval_days == 92


def test_security_settings_lists_the_review_interval(client, reviewer, make_user):
    admin = make_user("admin-reader", "admin")
    client.force_login(admin)
    assert b"Activity review interval" in client.get(reverse("security-settings")).content
    reviews.set_interval(actor=reviewer, interval_days=31, expected_revision=0, reason="Monthly.")
    page = client.get(reverse("security-settings")).content
    assert b"Every 31 days" in page


def test_reversing_the_migration_is_refused_while_a_review_exists(reviewer):
    from django.db.migrations.executor import MigrationExecutor

    sign(reviewer)
    executor = MigrationExecutor(connection)
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            executor.migrate([("audit_review", "0002_people_summary")])
