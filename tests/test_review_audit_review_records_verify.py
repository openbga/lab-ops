"""Verification of the revised audit review records through synthetic, ordinary workflows."""

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from queue import Queue
from textwrap import dedent
from threading import Event, local
from time import monotonic, sleep

import pytest
from allauth.core import context
from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher, identify_hasher
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.views.debug import get_exception_reporter_filter

from accounts import confirmation
from accounts.models import AccountSession, SignInEvent, User
from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from core.models import AuditEvent, SessionLimit
from nonconformance.seed import happened_at

# The feature-independent flows in this file also run on the released base revision.
if find_spec("audit_review.reviews") is not None:
    from audit_review import review_views, reviews
    from audit_review.models import Review, ReviewNote, ReviewSchedule
    from audit_review.review_forms import ReviewForm
    from audit_review.tests.test_reviews import form_data, sign


if find_spec("tests.test_review_audit_review_records") is not None:
    from tests import test_review_audit_review_records as round_one


def password_request(flow, password):
    if flow == "signing":
        return reverse("audit_review:review-create"), form_data(confirm_password=password)
    if flow == "sign-in":
        return reverse("account_login"), {"login": "verification-admin", "password": password}
    if flow == "reauthentication":
        return reverse("account_reauthenticate"), {"password": password}
    assert flow == "confirmation"
    return reverse("session-limits-edit", args=["staff"]), {
        "inactivity_minutes": "16",
        "maximum_hours": "8",
        "reason": "Synthetic verification",
        "expected_revision": "0",
        "confirm_password": password,
    }


@pytest.mark.confirmation
@pytest.mark.parametrize("flow", ["sign-in", "reauthentication", "confirmation", "signing"])
def test_password_flows_still_refuse_then_accept_and_record_the_right_effect(
    client, make_user, flow
):
    user = make_user("verification-admin", "admin")
    user.user_permissions.add(review_permission())
    if flow != "sign-in":
        client.force_login(user)
    path, wrong = password_request(flow, "wrong synthetic input")
    refusal = client.post(path, wrong)
    assert refusal.status_code == 200
    assert "wrong synthetic input" not in refusal.content.decode()
    failure = (
        SignInEvent.Kind.FAILED if flow == "sign-in" else SignInEvent.Kind.REAUTHENTICATION_FAILED
    )
    assert SignInEvent.objects.filter(user=user, kind=failure).count() == 1
    path, right = password_request(flow, PASSWORD)
    accepted = client.post(path, right)
    assert accepted.status_code == 302
    if flow == "sign-in":
        assert int(client.session["_auth_user_id"]) == user.pk
        assert SignInEvent.objects.filter(user=user, kind=SignInEvent.Kind.SIGNED_IN).count() == 1
    elif flow == "confirmation":
        assert SessionLimit.objects.get(audience="staff").inactivity_minutes == 16
        assert confirmation.current(client.get(path).wsgi_request)
    elif flow == "reauthentication":
        assert confirmation.current(client.get(reverse("dashboard")).wsgi_request)
    else:
        assert accepted.url == reverse("audit_review:review", args=[Review.objects.get().pk])
        assert not confirmation.current(client.get(path).wsgi_request)


@pytest.mark.confirmation
@pytest.mark.parametrize("flow", ["reauthentication", "confirmation", "signing"])
def test_credential_filter_covers_the_workflow_around_the_adapter(
    client, make_user, settings, flow
):
    """Inspect only the production filter's result during successful synthetic requests.

    Sample at real calls made during form and account checks. Keep failing variable names only.
    """
    settings.DEBUG = False
    user = make_user("verification-admin", "admin")
    user.user_permissions.add(review_permission())
    client.force_login(user)
    path, data = password_request(flow, PASSWORD)
    seen = set()
    visible = set()
    checkpoints = {
        ("accounts.adapter", "reauthenticate"),
        ("accounts.adapter", "authenticate"),
        ("audit_review.reviews", "check_period"),
    }
    relevant_modules = {
        "accounts.adapter",
        "accounts.confirmation",
        "audit_review.review_forms",
        "audit_review.review_views",
        "allauth.account.forms",
    }

    def sample(frame, event, _arg):
        if event != "call":
            return
        key = frame.f_globals.get("__name__"), frame.f_code.co_name
        if key not in checkpoints:
            return
        seen.add(key)
        request = context.request
        production = get_exception_reporter_filter(request)
        while frame is not None:
            module = frame.f_globals.get("__name__")
            if module in relevant_modules:
                for name, value in production.get_traceback_frame_variables(request, frame):
                    if isinstance(value, str):
                        contains = value == PASSWORD
                    elif type(value) is dict:
                        contains = PASSWORD in value.values()
                    else:
                        contains = False
                    if contains:
                        visible.add((module, frame.f_code.co_name, name))
            frame = frame.f_back

    previous = sys.getprofile()
    try:
        sys.setprofile(sample)
        response = client.post(path, data)
    finally:
        sys.setprofile(previous)
    assert response.status_code == 302
    assert ("accounts.adapter", "reauthenticate") in seen
    assert ("accounts.adapter", "authenticate") in seen
    if flow == "signing":
        assert ("audit_review.reviews", "check_period") in seen
    assert not visible, sorted(visible)


@pytest.mark.confirmation
def test_signing_uses_the_upgraded_hash_and_commits_its_review_once(client, reviewer, monkeypatch):
    hasher = PBKDF2PasswordHasher()
    legacy = hasher.encode(PASSWORD, hasher.salt(), iterations=100)
    User.objects.filter(pk=reviewer.pk).update(password=legacy)
    reviewer.refresh_from_db()
    client.force_login(reviewer)
    service = reviews.sign_review
    matches = []

    def observe(**kwargs):
        stored = User.objects.values_list("password", flat=True).get(pk=reviewer.pk)
        matches.append(kwargs["password_hash"] == stored and stored != legacy)
        return service(**kwargs)

    monkeypatch.setattr(reviews, "sign_review", observe)
    response = client.post(reverse("audit_review:review-create"), form_data())
    assert response.status_code == 302
    assert matches == [True]
    reviewer.refresh_from_db()
    assert not identify_hasher(reviewer.password).must_update(reviewer.password)
    review = Review.objects.get(signed_by=reviewer)
    assert response.url == reverse("audit_review:review", args=[review.pk])
    assert reviews.intact(review)
    assert AuditEvent.objects.filter(action=reviews.SIGNED).count() == 1
    # The release's session policy treats maintenance of the stored hash as a hash change.
    assert AccountSession.objects.get(user=reviewer).ending == AccountSession.Ending.NEW_PASSWORD


@pytest.mark.confirmation
def test_an_old_credential_receipt_is_refused_even_with_the_signers_current_session(
    client, reviewer
):
    """A completed own-password change keeps the browser signed in. Isolate the service guard
    from middleware sign-out, using both actual form receipts and the real change-password form.
    """
    client.force_login(reviewer)
    url = reverse("audit_review:review-create")

    def receipt(password):
        request = client.get(url).wsgi_request
        with context.request_context(request):
            form = ReviewForm(form_data(confirm_password=password), request=request)
            assert form.check_password()
            assert confirmation.PASSWORD_FIELD not in form.cleaned_data
            return form.checked_hash

    original = receipt(PASSWORD)
    changed = "birch telescope pepper fixture"
    assert (
        client.post(
            reverse("account_change_password"),
            {"oldpassword": PASSWORD, "password1": changed, "password2": changed},
        ).status_code
        == 302
    )
    assert client.get(url).status_code == 200
    current = receipt(changed)
    assert original and current and original != current
    with pytest.raises(ValidationError) as refused:
        sign(reviewer, password_hash=original)
    assert "confirm_password" in refused.value.message_dict
    assert not Review.objects.exists()
    assert not AuditEvent.objects.filter(action=reviews.SIGNED).exists()
    assert reviews.intact(sign(reviewer, password_hash=current))


@pytest.mark.parametrize("operation", ["sign", "note", "interval"])
def test_empty_submission_binds_and_identifies_every_missing_required_field(
    client, reviewer, operation
):
    record = sign(reviewer)
    client.force_login(reviewer)
    if operation == "sign":
        path = reverse("audit_review:review-create")
        missing = {"period_from", "period_to", "outcome", "examined", "confirm_password"}
    # Adapted (confirmation C3): a missing revision is repaired on the redraw and said in words,
    # as the form's own error, beside the visible fields' errors.
    elif operation == "note":
        path = reverse("audit_review:review-note", args=[record.pk])
        missing = {"text", "__all__"}
    else:
        path = reverse("audit_review:review-interval")
        missing = {"reason", "__all__"}
    response = client.post(path, {})
    assert response.status_code == 200
    form = response.context["form"]
    assert form.is_bound and set(form.errors) == missing
    assert "no-store" in response["Cache-Control"]
    assert not record.notes.exists() and Review.objects.count() == 1
    assert not ReviewSchedule.objects.exists()
    assert not SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").exists()


@pytest.mark.parametrize("operation", ["note", "interval"])
def test_a_field_error_redraw_carries_the_revision_of_the_values_it_shows(
    client, reviewer, operation
):
    review = sign(reviewer)
    second = Client()
    for browser_client in (client, second):
        browser_client.force_login(reviewer)
    if operation == "note":
        url = reverse("audit_review:review-note", args=[review.pk])
        changed = {"text": "First observation"}
        invalid = {"text": ""}
        filled = {"text": "Corrected"}
        displayed, missing = b"First observation", "text"
    else:
        url = reverse("audit_review:review-interval")
        changed = {"interval_days": 14, "reason": "Fortnightly"}
        invalid = {"interval_days": 21, "reason": ""}
        filled = {"interval_days": 21, "reason": "Compared to the current fortnightly interval."}
        displayed, missing = b"Now: every 14 days", "reason"
    old = client.get(url).context["form"]["expected_revision"].value()
    assert second.post(url, {**changed, "expected_revision": old}).status_code == 302
    response = client.post(url, {**invalid, "expected_revision": old})
    assert response.status_code == 200 and displayed in response.content
    form = response.context["form"]
    assert set(form.errors) == {missing}
    corrected = client.post(url, {**filled, "expected_revision": form["expected_revision"].value()})
    assert corrected.status_code == 302
    if operation == "note":
        assert list(review.notes.order_by("pk").values_list("text", flat=True)) == [
            "First observation",
            "Corrected",
        ]
    else:
        assert reviews.schedule().interval_days == 21


def test_migration_has_unlimited_width_for_both_snapshotted_names(db):
    with connection.cursor() as cursor:
        for model, column in [(Review, "signer_name"), (ReviewNote, "author_name")]:
            cursor.execute(
                "SELECT data_type, character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                [model._meta.db_table, column],
            )
            assert cursor.fetchone() == ("text", None)


def test_seed_records_accept_no_credential_receipt_and_do_not_claim_a_password_check(
    reviewer, client
):
    from audit_review import seed

    reviewer.username = seed.REVIEW_HOLDER
    reviewer.save(update_fields=["username"])
    seed.seed_records(None)
    record = Review.objects.get()
    assert reviews.intact(record) and record.notes.count() == 1
    assert ReviewSchedule.objects.get().interval_days == 31
    assert not SignInEvent.objects.filter(user=reviewer).exists()
    seed.seed_records(None)
    assert Review.objects.get().checksum == record.checksum
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:review", args=[record.pk]))
    assert response.status_code == 200 and b"Record a review asks" in response.content


def test_export_refreshes_only_note_counts_in_the_original_reading(client, reviewer):
    # The first and last of 51 reviews are on different pages. Change both counts and add
    # another review after capturing the offered CSV, with different text and checksum.
    records = [sign(reviewer, findings=f"Observation {i}") for i in range(51)]
    client.force_login(reviewer)
    first = client.get(reverse("audit_review:reviews"))
    second_url = round_one.review_next_link(first)
    export_url = first.context["export_url"]
    original = round_one.review_csv(client.get(export_url))
    assert [row["Review"] for row in original] == [r.number for r in reversed(records)]
    assert {row["Notes"] for row in original} == {"0"}
    reviews.add_note(actor=reviewer, review=records[-1], text="Later.", expected_revision=1)
    reviews.add_note(actor=reviewer, review=records[0], text="Later.", expected_revision=1)
    reviews.add_note(actor=reviewer, review=records[0], text="Later still.", expected_revision=2)
    newest = sign(reviewer, findings="Outside the original reading.")

    refreshed = round_one.review_csv(client.get(export_url))
    counts = {records[-1].number: "1", records[0].number: "2"}
    expected = [dict(row, Notes=counts.get(row["Review"], "0")) for row in original]
    assert refreshed == expected
    second = client.get(second_url)
    [only] = list(second.context["page_obj"])
    assert (only.number, only.note_count) == (records[0].number, 2)
    assert second.context["state"].last == newest


def test_other_signing_errors_are_not_reported_as_the_documented_note_count_limit(tmp_path):
    # Adapted (verification V11): the adopted test was renamed when its expected failure became an
    # assertion of the documented limit.
    # Exercise pytest's classification of the actual adopted test. No Django database access
    # or setup is needed in the child: the synthetic unrelated failure happens on its first line.
    case = tmp_path / "test_note_count_control.py"
    case.write_text(
        dedent("""\
        import pytest
        from tests import test_review_audit_review_records as original

        test_export = original.test_an_export_keeps_its_reviews_and_counts_notes_as_it_is_made

        @pytest.fixture
        def reviewer():
            return object()

        @pytest.fixture
        def client():
            return object()

        @pytest.fixture(autouse=True)
        def synthetic_storage_error(monkeypatch):
            def unavailable(*args, **kwargs):
                print("Synthetic review storage outage")
                raise RuntimeError("Synthetic review storage outage")
            monkeypatch.setattr(original, "sign", unavailable)
        """)
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pyproject.toml",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rx",
            "-s",
            str(case),
        ],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "Synthetic review storage outage" in result.stdout
    assert result.returncode == 1 and "1 failed" in result.stdout, result.stdout


@pytest.fixture
def clock_adjusted_signings(reviewer):
    # Five real minutes of backwards adjustment cross the laboratory's local midnight.
    after_midnight = datetime(2026, 3, 9, 4, 2, tzinfo=UTC)
    before_midnight = datetime(2026, 3, 9, 3, 57, tzinfo=UTC)
    historical = {"period_from": date(2026, 2, 1), "period_to": date(2026, 2, 15)}
    with timezone.override("America/New_York"):
        reviews.set_interval(actor=reviewer, interval_days=2, expected_revision=0, reason="Trial.")
        with happened_at(after_midnight):
            prior = sign(reviewer, **historical)
        with happened_at(before_midnight):
            latest = sign(reviewer, **historical)
        assert latest.pk > prior.pk and latest.signed_at < prior.signed_at
        yield prior, latest


def test_order_and_due_day_follow_last_committed_signing_after_clock_adjustment(
    client, reviewer, clock_adjusted_signings
):
    prior, latest = clock_adjusted_signings
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:reviews"))
    assert round_one.page_numbers(response) == [latest.number, prior.number]
    rows = round_one.review_csv(client.get(response.context["export_url"]))
    assert [row["Review"] for row in rows] == [latest.number, prior.number]
    state = response.context["state"]
    assert state.last == latest
    assert state.due == date(2026, 3, 10)
    assert reviews.status(today=state.due).state == "today"
    assert reviews.status(today=state.due + timedelta(days=1)).state == "overdue"


def test_architecture_describes_the_due_day_when_signing_clocks_disagree(clock_adjusted_signings):
    prior, latest = clock_adjusted_signings
    state = reviews.status()
    architecture = (Path(settings.BASE_DIR) / "docs/architecture.md").read_text()
    if "local date of the newest `signed_at` plus the interval" in architecture:
        described = max((prior, latest), key=lambda review: review.signed_at)
    else:
        # The product's Last review follows the order the two signings committed in.
        described = latest
    assert state.due == timezone.localdate(described.signed_at) + timedelta(days=state.interval)


@pytest.mark.django_db(transaction=True)
def test_adopted_signing_probe_does_not_recognize_a_wait_on_a_different_lock(monkeypatch):
    # PostgreSQL's one-bigint and two-integer advisory locks are different namespaces, even
    # when their high/low words match. Hold only the one-bigint lock on this test database.
    pair = (123456, 789)
    single = (pair[0] << 32) | pair[1]
    pids = Queue()

    def wait_on_single():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
                cursor.execute("SELECT pg_advisory_xact_lock(%s::bigint)", [single])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s::bigint)", [single])
            waiting = pool.submit(wait_on_single)
            pid = pids.get(timeout=10)
            deadline = monotonic() + 10
            while monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    break
                sleep(0.01)
            else:
                pytest.fail("The fixture writer did not reach its one-bigint lock.")
            # Exercise a single iteration of the adopted probe; no signing was launched.
            ticks = iter([0, 1, 20])
            monkeypatch.setattr(round_one, "monotonic", lambda: next(ticks))
            with pytest.raises(AssertionError, match="No signing waited"):
                round_one._wait_for_a_waiter(pair)
        waiting.result(timeout=10)


def _review_worker_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _assert_review_backend_waits_for(waiter, holder):
    deadline = monotonic() + 10
    with connection.cursor() as cursor:
        while monotonic() < deadline:
            cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder, waiter])
            if cursor.fetchone()[0]:
                return
            sleep(0.02)
    pytest.fail("The expected PostgreSQL backend did not wait for the other transaction")


def _review_thread(call, *args):
    try:
        return call(*args)
    finally:
        connections.close_all()


@pytest.mark.django_db(transaction=True)
def test_queued_signing_allows_notes_interval_and_failed_password_accounting(
    reviewer, client, settings, monkeypatch
):
    cache.clear()
    settings.ACCOUNT_RATE_LIMITS = {"reauthenticate": "20/m/user", "login_failed": "1/m/key"}
    create = reverse("audit_review:review-create")
    old_review = sign(reviewer)
    browsers = [Client() for _ in range(3)]
    for http in [client, *browsers]:
        http.force_login(reviewer)

    unblock = Event()
    allocated = Queue()
    reached_signing = Queue()
    phase = local()
    next_id = reviews._next_id
    actual_sign = reviews.sign_review

    def pause_first_after_allocation():
        pk = next_id()
        if getattr(phase, "first", False):
            allocated.put(_review_worker_pid())
            assert unblock.wait(20)
        return pk

    def identify_waiter(**kwargs):
        if kwargs["findings"] == "Second synthetic signing":
            reached_signing.put(_review_worker_pid())
        return actual_sign(**kwargs)

    monkeypatch.setattr(reviews, "_next_id", pause_first_after_allocation)
    monkeypatch.setattr(reviews, "sign_review", identify_waiter)

    def sign_from_session(http, label, first=False):
        phase.first = first
        return http.post(create, form_data(findings=label)).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(
            _review_thread, sign_from_session, browsers[0], "First synthetic signing", True
        )
        try:
            holder = allocated.get(timeout=10)
            second = pool.submit(
                _review_thread, sign_from_session, browsers[1], "Second synthetic signing"
            )
            waiter = reached_signing.get(timeout=10)
            _assert_review_backend_waits_for(waiter, holder)

            def independent_forms():
                # The earlier checks have finished. The third session can spend one failed
                # attempt and see the refusal on its next attempt while both signings remain open.
                failed = browsers[2].post(create, form_data(confirm_password="synthetic-wrong"))
                limited = browsers[2].post(create, form_data())
                note = client.post(
                    reverse("audit_review:review-note", args=[old_review.pk]),
                    {"text": "Independent correction.", "expected_revision": 1},
                )
                interval = client.post(
                    reverse("audit_review:review-interval"),
                    {"interval_days": 17, "reason": "Seventeen-day cycle.", "expected_revision": 0},
                )
                return failed.content, limited.content, note.status_code, interval.status_code

            failed, limited, note, interval = pool.submit(_review_thread, independent_forms).result(
                timeout=10
            )
            assert b"Incorrect password" in failed and b"Too many failed" in limited
            assert (note, interval) == (302, 302)
            assert old_review.notes.get().text == "Independent correction."
            assert reviews.schedule().interval_days == 17
            assert Review.objects.count() == 1
            assert SignInEvent.objects.filter(user=reviewer, kind="reauth_failed").count() == 1
            assert SignInEvent.objects.filter(user=reviewer, kind="locked_out").count() == 1
        finally:
            unblock.set()
        assert (first.result(timeout=10), second.result(timeout=10)) == (302, 302)
    assert list(
        Review.objects.exclude(pk=old_review.pk).order_by("id").values_list("findings", flat=True)
    ) == ["First synthetic signing", "Second synthetic signing"]


@pytest.mark.django_db(transaction=True)
def test_a_rolled_back_signing_releases_the_lock_and_commits_no_partial_record(
    reviewer, monkeypatch
):
    results = Queue()
    arrived = Queue()
    release = Event()
    phase = local()
    actual_record_event = reviews.record_event
    reached_service = reviews._reviewer

    def start_actor(actor):
        current = reached_service(actor)
        if not phase.rollback:
            arrived.put(_review_worker_pid())
        return current

    def interrupt_before_commit(**kwargs):
        event = actual_record_event(**kwargs)
        if phase.rollback:
            results.put((_review_worker_pid(), kwargs["obj"].number))
            assert release.wait(20)
            raise RuntimeError("Synthetic audit transaction rollback")
        return event

    def write(rollback):
        phase.rollback = rollback
        return sign(reviewer, findings="Rolled back" if rollback else "Committed")

    monkeypatch.setattr(reviews, "_reviewer", start_actor)
    monkeypatch.setattr(reviews, "record_event", interrupt_before_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_review_thread, write, True)
        try:
            holder, unused_number = results.get(timeout=10)
            second = pool.submit(_review_thread, write, False)
            waiter = arrived.get(timeout=10)
            _assert_review_backend_waits_for(waiter, holder)
            assert not Review.objects.exists()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="Synthetic audit transaction rollback"):
            first.result(timeout=10)
        committed = second.result(timeout=10)
    assert Review.objects.get().pk == committed.pk
    assert committed.number != unused_number and reviews.intact(committed)
    assert AuditEvent.objects.get(action=reviews.SIGNED).object_id == str(committed.pk)


@pytest.mark.django_db(transaction=True)
def test_correcting_an_invalid_interval_form_submits_the_revision_its_redraw_shows(
    reviewer, make_user, client, monkeypatch
):
    url = reverse("audit_review:review-interval")
    editor = make_user("interval-editor", "supervisor")
    editor.user_permissions.add(review_permission())
    other_client = Client()
    other_client.force_login(editor)
    client.force_login(reviewer)
    revision_pattern = rb'name="expected_revision" value="(\d+)"'
    original_revision = int(re.search(revision_pattern, client.get(url).content)[1])
    assert original_revision == 0

    pending_commit = Queue()
    drawing = Queue()
    release = Event()
    record_event = reviews.record_event
    read_interval = review_views._interval_reading

    def pause_review_change(**kwargs):
        event = record_event(**kwargs)
        if kwargs["action"] == reviews.INTERVAL_CHANGED and kwargs["actor"].pk == editor.pk:
            pending_commit.put(_review_worker_pid())
            assert release.wait(20)
        return event

    def identify_redraw():
        drawing.put(_review_worker_pid())
        return read_interval()

    monkeypatch.setattr(reviews, "record_event", pause_review_change)
    monkeypatch.setattr(review_views, "_interval_reading", identify_redraw)
    with ThreadPoolExecutor(max_workers=2) as pool:
        changed = pool.submit(
            _review_thread,
            other_client.post,
            url,
            {"interval_days": 14, "reason": "Fortnightly.", "expected_revision": original_revision},
        )
        try:
            writer = pending_commit.get(timeout=10)
            # A required field is empty, so this request needs a reading without calling the
            # interval service. Its shared lock must wait for the editor's exclusive one.
            refused = pool.submit(
                _review_thread,
                client.post,
                url,
                {"interval_days": 21, "reason": "", "expected_revision": original_revision},
            )
            reader = drawing.get(timeout=10)
            _assert_review_backend_waits_for(reader, writer)
        finally:
            release.set()
        assert changed.result(timeout=10).status_code == 302
        redrawn = refused.result(timeout=10)
    html = redrawn.content
    assert redrawn.status_code == 200
    assert b"This field is required." in html and b'interval_days" value="21"' in html
    assert b"Now: every 14 days" in html
    assert b"every 14 days" in html.split(b"Changes to the interval (", 1)[1]
    assert b"Fortnightly." in html
    redrawn_revision = int(re.search(revision_pattern, html)[1])
    corrected = client.post(
        url,
        {
            "interval_days": 21,
            "reason": "Compared to 14 days.",
            "expected_revision": redrawn_revision,
        },
    )
    assert corrected.status_code == 302, (
        f"Form showed 14 days at revision 1, posted revision {redrawn_revision} and "
        f"was refused for staleness: {reviews.STALE_INTERVAL.encode() in corrected.content}"
    )
    assert reviews.schedule().interval_days == 21


@pytest.mark.django_db(transaction=True)
def test_two_stale_redraws_can_read_together_and_both_hold_the_writer_until_history_is_read(
    reviewer, monkeypatch
):
    url = reverse("audit_review:review-interval")
    reviews.set_interval(actor=reviewer, interval_days=7, reason="Weekly.", expected_revision=0)
    sessions = [Client() for _ in range(3)]
    for session in sessions:
        session.force_login(reviewer)

    readers = [Queue(), Queue()]
    release = [Event(), Event()]
    writer_started = Queue()
    worker = local()
    real_history = reviews.interval_history
    real_reviewer = reviews._reviewer

    def interval_history_rows(*args, **kwargs):
        # The wait happens at iteration, including if the queryset should become lazy again.
        index = worker.index
        readers[index].put(_review_worker_pid())
        assert release[index].wait(20)
        yield from real_history(*args, **kwargs)

    def identify_writer(actor):
        active = real_reviewer(actor)
        if worker.index == 2:
            writer_started.put(_review_worker_pid())
        return active

    monkeypatch.setattr(reviews, "interval_history", interval_history_rows)
    monkeypatch.setattr(reviews, "_reviewer", identify_writer)

    def submit(index):
        worker.index = index
        return sessions[index].post(
            url,
            {"interval_days": 14, "reason": "Fortnightly.", "expected_revision": int(index == 2)},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        stale_forms = [pool.submit(_review_thread, submit, index) for index in range(2)]
        try:
            first_reader, second_reader = [arrived.get(timeout=10) for arrived in readers]
            assert first_reader != second_reader
            writer = pool.submit(_review_thread, submit, 2)
            writer_pid = writer_started.get(timeout=10)
            _assert_review_backend_waits_for(writer_pid, first_reader)
            _assert_review_backend_waits_for(writer_pid, second_reader)
            release[0].set()
            first = stale_forms[0].result(timeout=10)
            _assert_review_backend_waits_for(writer_pid, second_reader)
        finally:
            for allowed in release:
                allowed.set()
        second = stale_forms[1].result(timeout=10)
        assert writer.result(timeout=10).status_code == 302

    for redraw in [first, second]:
        assert redraw.status_code == 200 and reviews.STALE_INTERVAL.encode() in redraw.content
        assert b"Now: every 7 days" in redraw.content
        assert b'name="expected_revision" value="1"' in redraw.content
        assert b"every 7 days" in redraw.content.split(b"Changes to the interval (", 1)[1]
    assert reviews.schedule().interval_days == 14


@pytest.mark.parametrize("operation", ["note", "interval"])
def test_correcting_the_visible_fields_after_an_empty_post_can_save(client, reviewer, operation):
    from html.parser import HTMLParser

    class RevisionInput(HTMLParser):
        value = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "input" and attributes.get("name") == "expected_revision":
                self.value = attributes.get("value", "")

    record = sign(reviewer)
    client.force_login(reviewer)
    if operation == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
        corrections = {"text": "The reviewer corrected the note field."}
    else:
        url = reverse("audit_review:review-interval")
        corrections = {"interval_days": 12, "reason": "Twelve-day cycle."}
    redraw = client.post(url, {})
    assert redraw.status_code == 200
    hidden = RevisionInput()
    hidden.feed(redraw.content.decode())
    assert hidden.value is not None
    accepted = client.post(url, {**corrections, "expected_revision": hidden.value})
    assert accepted.status_code == 302, list(accepted.context["form"].errors)
    if operation == "note":
        assert record.notes.get().text == corrections["text"]
    else:
        assert reviews.schedule().interval_days == 12


def test_stale_interval_message_matches_the_action_that_preserves_input(client, reviewer):
    client.force_login(reviewer)
    url = reverse("audit_review:review-interval")
    old = client.get(url).context["form"]["expected_revision"].value()
    reviews.set_interval(
        actor=reviewer, interval_days=14, reason="Fortnightly.", expected_revision=old
    )
    entered = {"interval_days": 21, "reason": "Twenty-one days."}
    redraw = client.post(url, {**entered, "expected_revision": old})
    assert redraw.status_code == 200
    form = redraw.context["form"]
    advice = " ".join(form.non_field_errors())
    assert advice and form["reason"].value() == entered["reason"]
    assert b"Now: every 14 days" in redraw.content
    # Opening the address afresh discards the typing. Comparing and saving the returned form works.
    assert not client.get(url).context["form"]["reason"].value()
    assert (
        client.post(
            url, {**entered, "expected_revision": form["expected_revision"].value()}
        ).status_code
        == 302
    )
    assert "reload" not in advice.lower(), advice


def test_architecture_describes_when_a_signing_takes_its_number(reviewer, monkeypatch):
    calls = []
    hold = reviews.hold_actor
    allocate = reviews._next_id

    def hold_account(actor):
        held = hold(actor)
        calls.append("account held")
        return held

    def allocate_number():
        number = allocate()
        calls.append("number allocated")
        return number

    monkeypatch.setattr(reviews, "hold_actor", hold_account)
    monkeypatch.setattr(reviews, "_next_id", allocate_number)
    assert reviews.intact(sign(reviewer))
    assert calls == ["account held", "number allocated"]
    architecture = (Path(settings.BASE_DIR) / "docs/architecture.md").read_text()
    assert "takes the id from the table's sequence first" not in architecture


@pytest.mark.django_db(transaction=True)
def test_adopted_interval_probe_requires_the_other_writer_to_have_started(
    client, reviewer, make_user, monkeypatch
):
    # Withhold the second request until the stale response has finished rendering. Both real
    # saves still run, but there is no overlap or waiting writer for the adopted test to confirm.
    redraw_finished = Event()

    class OrderedRequests(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            def request():
                if fn.__name__ == "other_save":
                    assert redraw_finished.wait(20)
                    return fn(*args, **kwargs)
                try:
                    return fn(*args, **kwargs)
                finally:
                    redraw_finished.set()

            return super().submit(request)

    ticks = iter(range(0, 600, 11))
    monkeypatch.setattr(round_one, "ThreadPoolExecutor", OrderedRequests)
    monkeypatch.setattr(round_one, "monotonic", lambda: next(ticks))
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        round_one.test_interval_history_agrees_with_current_value_and_revision_on_stale_redraw(
            client, reviewer, make_user, monkeypatch
        )
