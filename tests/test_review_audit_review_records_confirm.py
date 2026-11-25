"""Confirmation of signed-review changes through synthetic application workflows."""

import re
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from importlib.util import find_spec
from pathlib import Path
from queue import Queue
from threading import Event
from time import monotonic, sleep
from urllib.parse import urlsplit

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import Client
from django.urls import reverse
from django.views.debug import get_exception_reporter_filter
from psycopg import sql

from accounts import confirmation
from accounts.models import SignInEvent
from audit_review.tests.conftest import PASSWORD, review_permission
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from core import security_settings
from core.models import AuditEvent

# The account workflows in this file also run on the base release, before signed reviews.
if find_spec("audit_review.reviews") is not None:
    from audit_review import review_views, reviews
    from audit_review.models import Review
    from audit_review.tests.test_reviews import form_data, sign
    from tests import test_review_audit_review_records as round_one


def test_security_page_matches_the_running_password_form_and_its_release_pins(
    client, make_user, settings
):
    client.force_login(make_user("settings-reader", "admin"))
    path = reverse("security-settings")
    release = settings.ACCOUNT_FORMS.copy()
    expected_form = "accounts.forms.PasswordReauthenticateForm"
    assert release["reauthenticate"] == expected_form
    assert security_settings.RELEASE["ACCOUNT_FORMS"][1] == release
    source = Path(settings.BASE_DIR) / "config/settings.py"
    assert sha256(source.read_bytes()).hexdigest() == security_settings.RELEASE_SETTINGS_SHA256
    form = client.get(reverse("account_reauthenticate")).context["form"]
    assert f"{type(form).__module__}.{type(form).__name__}" == expected_form

    def page_facts():
        response = client.get(path)
        assert response.status_code == 200
        return {fact.key: fact for group in response.context["groups"] for fact in group.facts}

    baseline = page_facts()
    assert "characters" in baseline["passwords"].value
    assert "Kept in the database" in baseline["sign-in-history"].value
    # A deployment can select the upstream form by removing just this one override.
    settings.ACCOUNT_FORMS = {
        name: value for name, value in release.items() if name != "reauthenticate"
    }
    fallback = client.get(reverse("account_reauthenticate")).context["form"]
    assert type(fallback).__module__ == "allauth.account.forms"
    changed = page_facts()
    assert {key for key, value in changed.items() if baseline[key] != value} == {
        "passwords",
        "sign-in-history",
    }
    for key in ("passwords", "sign-in-history"):
        assert changed[key].value == security_settings.CHANGED
        assert changed[key].note.endswith("Check ACCOUNT_FORMS in config/settings.py.")


@pytest.mark.confirmation
def test_password_challenge_returns_to_its_requested_page_after_correction(client, make_user):
    person = make_user("challenge-reader", "technical")
    client.force_login(person)
    target = reverse("mfa_activate_totp")
    prompt = client.get(target)
    assert prompt.status_code == 302
    assert urlsplit(prompt.url).path == reverse("account_reauthenticate")
    assert client.get(prompt.url).status_code == 200

    for value, error in [("", "required"), ("incorrect synthetic entry", "incorrect_password")]:
        response = client.post(prompt.url, {"password": value})
        assert response.status_code == 200
        assert response.context["form"].has_error("password", code=error)
        assert not confirmation.current(response.wsgi_request)
    assert SignInEvent.objects.filter(user=person, kind="reauth_failed").count() == 1

    accepted = client.post(prompt.url, {"password": PASSWORD}, follow=True)
    assert accepted.status_code == 200 and accepted.wsgi_request.path == target
    assert confirmation.current(accepted.wsgi_request)
    assert SignInEvent.objects.filter(user=person, kind="reauth_failed").count() == 1


@pytest.mark.confirmation
def test_password_challenge_shares_failed_attempts_and_distinguishes_request_limits(
    client, make_user, settings
):
    person = make_user("limited-reader", "technical")
    other_browser = Client()
    for session in (client, other_browser):
        session.force_login(person)
    prompt = reverse("account_reauthenticate")
    cache.clear()
    try:
        settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key", "reauthenticate": "20/m/user"}
        wrong = client.post(prompt, {"password": "incorrect synthetic entry"})
        assert wrong.context["form"].has_error("password", code="incorrect_password")
        for _ in range(2):
            limited = other_browser.post(prompt, {"password": PASSWORD})
            assert limited.status_code == 200
            assert limited.context["form"].has_error("password", code="too_many_login_attempts")
            assert not confirmation.current(limited.wsgi_request)
        assert list(
            SignInEvent.objects.filter(user=person, kind__in=["reauth_failed", "locked_out"])
            .order_by("pk")
            .values_list("kind", "factor")
        ) == [("reauth_failed", "password"), ("locked_out", "password")]

        cache.clear()
        settings.ACCOUNT_RATE_LIMITS = {"login_failed": "5/m/key", "reauthenticate": "1/m/user"}
        assert client.post(prompt, {"password": PASSWORD}).status_code == 302
        refused = other_browser.post(prompt, {"password": PASSWORD})
        assert refused.status_code == 429
        assert not confirmation.current(refused.wsgi_request)
        assert (
            SignInEvent.objects.filter(
                user=person, kind__in=["reauth_failed", "locked_out"]
            ).count()
            == 2
        )
    finally:
        cache.clear()


@pytest.mark.confirmation
@pytest.mark.parametrize("with_app", [False, True], ids=["no-method", "authenticator"])
def test_reauthentication_without_a_usable_password_selects_an_available_method(
    client, make_user, with_app
):
    person = make_user("method-reader", "technical")
    person.set_unusable_password()
    person.save(update_fields=["password"])
    if with_app:
        TOTP.activate(person, generate_totp_secret())
    client.force_login(person)
    prompt = client.get(reverse("account_reauthenticate"))
    if with_app:
        assert prompt.status_code == 302 and urlsplit(prompt.url).path == reverse(
            "mfa_reauthenticate"
        )
        assert "code" in client.get(prompt.url).context["form"].fields
    else:
        assert prompt.status_code == 403
    assert not SignInEvent.objects.filter(user=person, kind="reauth_failed").exists()


@pytest.mark.confirmation
@pytest.mark.parametrize("outcome", ["accepted", "refused", "request-limited"])
def test_password_on_the_reauthentication_page_is_a_protected_request_field(
    client, make_user, settings, outcome
):
    """Use Django's two request-data filters on ordinary responses, retaining only pass/fail."""
    settings.DEBUG = False
    person = make_user("field-reader", "technical")
    client.force_login(person)
    url = reverse("account_reauthenticate")
    settings.ACCOUNT_RATE_LIMITS = {
        "reauthenticate": "1/m/user" if outcome == "request-limited" else "20/m/user"
    }
    value = "incorrect synthetic entry" if outcome == "refused" else PASSWORD
    cache.clear()
    try:
        if outcome == "request-limited":
            assert client.post(url, {"password": ""}).status_code == 200
        response = client.post(url, {"password": value})
        assert (
            response.status_code
            == {"accepted": 302, "refused": 200, "request-limited": 429}[outcome]
        )
        request = response.wsgi_request
        reporter = get_exception_reporter_filter(request)
        unprotected = [
            surface
            for surface, filtered in (
                ("POST parameters", reporter.get_post_parameters(request)),
                (
                    "request data in traceback variables",
                    reporter.cleanse_special_types(request, request.POST),
                ),
            )
            if filtered.get("password") == value
        ]
        assert not unprotected, unprotected
    finally:
        cache.clear()


@pytest.mark.confirmation
@pytest.mark.parametrize("case", ["required-field", "incorrect-password"])
def test_refused_review_still_masks_its_submitted_password_and_preserves_its_other_fields(
    client, make_user, settings, case
):
    settings.DEBUG = False
    person = make_user("review-signatory", "technical")
    person.user_permissions.add(review_permission())
    client.force_login(person)
    entered = PASSWORD if case == "required-field" else "incorrect synthetic entry"
    data = form_data(confirm_password=entered, findings="Synthetic retained observation")
    if case == "required-field":
        data["period_from"] = ""
    response = client.post(reverse("audit_review:review-create"), data)
    assert response.status_code == 200
    form = response.context["form"]
    assert form.has_error("period_from" if case == "required-field" else "confirm_password")
    assert form["findings"].value() == data["findings"]
    assert entered not in response.content.decode()
    assert not Review.objects.exists()
    assert SignInEvent.objects.filter(user=person, kind="reauth_failed").count() == int(
        case == "incorrect-password"
    )
    request = response.wsgi_request
    reporter = get_exception_reporter_filter(request)
    for filtered in (
        reporter.get_post_parameters(request),
        reporter.cleanse_special_types(request, request.POST),
    ):
        protected = filtered.get("confirm_password") == reporter.cleansed_substitute
        assert protected


class _RedrawnInputs(HTMLParser):
    """Read the enabled revision control and any visibly invalid controls from the actual HTML."""

    def __init__(self, response):
        super().__init__()
        assert response.status_code == 200
        self.revisions = []
        self.visible_errors = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"input", "textarea", "select"} or "disabled" in attrs:
            return
        if attrs.get("type") == "hidden":
            if attrs.get("name") == "expected_revision":
                value = attrs.get("value")
                self.revisions.append(int(value) if value and value.isdecimal() else None)
        elif attrs.get("aria-invalid") == "true" or "is-invalid" in attrs.get("class", "").split():
            self.visible_errors.append(attrs.get("name"))

    @property
    def revision(self):
        (value,) = self.revisions
        return value


def _review_editor(make_user):
    person = make_user("redraw-colleague", "technical")
    person.user_permissions.add(review_permission())
    session = Client()
    session.force_login(person)
    return session


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("surface", ["note", "interval"])
@pytest.mark.parametrize("refusal", ["stale", "visible", "no_revision", "bad_revision", "empty"])
def test_intervening_writes_after_a_refusal_keep_the_revision_of_the_state_shown(
    reviewer, make_user, client, monkeypatch, surface, refusal
):
    record = sign(reviewer)
    client.force_login(reviewer)
    editor = _review_editor(make_user)
    if surface == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
        action = reviews.NOTE_ADDED
        proposed = {"text": "Reviewing the final sample count."}
        changes = [{"text": label} for label in ("Initial entry.", "Later entry.", "Last entry.")]
        invalid_visible = {"text": "n" * 4001}
        stale_message = reviews.STALE_NOTE
    else:
        url = reverse("audit_review:review-interval")
        action = reviews.INTERVAL_CHANGED
        proposed = {"interval_days": 21, "reason": "Reviewing the final sample count."}
        # The newest change restores the first value. A value-only check would miss both changes.
        changes = [
            {"interval_days": days, "reason": label}
            for days, label in ((7, "Initial entry."), (14, "Later entry."), (7, "Last entry."))
        ]
        invalid_visible = {"interval_days": 367}
        stale_message = reviews.STALE_INTERVAL

    first_reading = _RedrawnInputs(client.get(url)).revision
    assert editor.post(url, {**changes[0], "expected_revision": first_reading}).status_code == 302
    sent = {**proposed, "expected_revision": first_reading}
    if refusal == "visible":
        sent.update(invalid_visible)
    elif refusal == "no_revision":
        del sent["expected_revision"]
    elif refusal == "bad_revision":
        sent["expected_revision"] = "missing revision"
    elif refusal == "empty":
        sent.clear()

    form_read = Event()
    permit_redraw = Event()
    carry_revision = review_views._carry_revision

    def carry_after_changes(form, revision, *what):
        # The rows for this response are available. Let a second browser commit changes before
        # the hidden input is filled and the actual HTML is generated.
        # Adapted (confirmation C3): _carry_revision also takes what the form shows.
        form_read.set()
        assert permit_redraw.wait(15)
        return carry_revision(form, revision, *what)

    def submit_for_redraw():
        try:
            return client.post(url, sent)
        finally:
            connections.close_all()

    with monkeypatch.context() as scoped, ThreadPoolExecutor(max_workers=1) as pool:
        scoped.setattr(review_views, "_carry_revision", carry_after_changes)
        refused = pool.submit(submit_for_redraw)
        try:
            assert form_read.wait(10)
            for body in changes[1:]:
                actual_revision = _RedrawnInputs(editor.get(url)).revision
                assert (
                    editor.post(url, {**body, "expected_revision": actual_revision}).status_code
                    == 302
                )
        finally:
            permit_redraw.set()
        redrawn = refused.result(timeout=10)

    assert b"Initial entry." in redrawn.content
    assert b"Later entry." not in redrawn.content and b"Last entry." not in redrawn.content
    rendered_revision = _RedrawnInputs(redrawn).revision
    assert rendered_revision == first_reading + 1
    assert AuditEvent.objects.filter(action=action).count() == 3
    for field in ("text", "reason"):
        if field in sent:
            assert escape(sent[field]).encode() in redrawn.content
    if "interval_days" in sent:
        assert f'name="interval_days" value="{sent["interval_days"]}"'.encode() in redrawn.content

    stale = client.post(url, {**proposed, "expected_revision": rendered_revision})
    assert stale.status_code == 200 and stale_message.encode() in stale.content
    for marker in (b"Initial entry.", b"Later entry.", b"Last entry."):
        assert marker in stale.content
    assert AuditEvent.objects.filter(action=action).count() == 3
    latest_revision = _RedrawnInputs(stale).revision
    assert latest_revision == first_reading + 3
    assert client.post(url, {**proposed, "expected_revision": latest_revision}).status_code == 302
    assert AuditEvent.objects.filter(action=action).count() == 4
    if surface == "note":
        assert list(record.notes.values_list("text", flat=True)) == [
            *(change["text"] for change in changes),
            proposed["text"],
        ]
    else:
        assert reviews.schedule().interval_days == 21


def test_stale_note_notice_points_toward_the_open_list_on_the_rendered_form(client, reviewer):
    record = sign(reviewer)
    client.force_login(reviewer)
    colleague = Client()
    colleague.force_login(reviewer)
    url = reverse("audit_review:review-note", args=[record.pk])
    original = _RedrawnInputs(client.get(url)).revision
    entry = "New observation to compare with the proposed correction."
    assert colleague.post(url, {"text": entry, "expected_revision": original}).status_code == 302
    body = {"text": "My pending correction.", "expected_revision": original}
    redraw = client.post(url, body)
    assert redraw.status_code == 200
    (notice,) = redraw.context["form"].non_field_errors()
    html = redraw.content.decode()
    assert re.search(r"<details\b[^>]*\bopen[^>]*>\s*<summary>Notes so far", html)
    assert entry in html and body["text"] in html
    location = "above" if html.index(entry) < html.index(escape(notice)) else "below"
    direction = re.search(r"\bnotes (above|below)\b", notice, re.I)
    assert not direction or direction[1].lower() == location, (
        f"The notice says {direction[1]!r}. The open note list is {location} the notice."
    )


@pytest.mark.parametrize("surface", ["note", "interval"])
def test_a_repaired_hidden_field_does_not_direct_reviewers_to_a_highlighted_visible_field(
    client, reviewer, surface
):
    record = sign(reviewer)
    client.force_login(reviewer)
    if surface == "note":
        url = reverse("audit_review:review-note", args=[record.pk])
        entered = {"text": "Retained correction."}
    else:
        url = reverse("audit_review:review-interval")
        entered = {"interval_days": 21, "reason": "Retained explanation."}
    redrawn = client.post(url, entered)
    controls = _RedrawnInputs(redrawn)
    # Adapted (confirmation C3): the repaired revision is said in words, as the form's own error,
    # not as an error of a field nobody can see.
    assert set(redrawn.context["form"].errors) == {"__all__"}
    assert b"This form is shown again as" in redrawn.content
    assert controls.visible_errors == []
    # The new hidden revision is enough: submitting the same visible input succeeds.
    hidden = controls.revision if controls.revision is not None else ""
    corrected = client.post(url, {**entered, "expected_revision": hidden})
    assert b"Check the highlighted field below." not in redrawn.content, (
        "The error summary refers to a highlighted visible field, but there is none."
    )
    assert corrected.status_code == 302


@pytest.fixture
def reviews_at_decimal_rollover(reviewer):
    # ALTER SEQUENCE RESTART is transactional. Restore its original value and remove the
    # synthetic records together, even if an assertion fails.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [Review._meta.db_table])
            sequence = sql.Identifier(*cursor.fetchone()[0].split("."))
            cursor.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH 999999").format(sequence))
        previous = sign(reviewer)
        current = sign(reviewer)
        yield previous, current
        transaction.set_rollback(True)


def test_highest_review_number_remains_numeric_at_the_next_decimal_place(
    client, reviewer, reviews_at_decimal_rollover
):
    previous, current = reviews_at_decimal_rollover
    assert (previous.number, current.number) == ("AR-999999", "AR-1000000")
    assert reviews.intact(current)
    client.force_login(reviewer)
    listing = client.get(reverse("audit_review:reviews"))
    assert listing.context["state"].last == current
    assert round_one.page_numbers(listing) == [current.number, previous.number]
    rows = round_one.review_csv(client.get(listing.context["export_url"]))
    assert [row["Review"] for row in rows] == [current.number, previous.number]


def test_architecture_describes_the_width_of_numbers_the_sequence_can_issue(
    reviews_at_decimal_rollover,
):
    previous, current = reviews_at_decimal_rollover
    assert previous.number == "AR-999999" and current.number == "AR-1000000"
    architecture = " ".join((Path(settings.BASE_DIR) / "docs/architecture.md").read_text().split())
    assert "(`AR-` and six digits)" not in architecture


@pytest.mark.django_db(transaction=True)
def test_the_waiting_signer_is_blocked_by_the_first_signers_outer_transaction(reviewer):
    # The first sign_review has returned yet its surrounding transaction hasn't committed.
    # Identify both backends and the lock, then read from a third connection at that instant.
    parked = Queue()
    arriving = Queue()
    commit = Event()
    original = sign(reviewer)

    def pid():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def first_signer():
        try:
            with transaction.atomic():
                first = sign(reviewer)
                parked.put((pid(), first.number))
                assert commit.wait(20)
            return first.number
        finally:
            connections.close_all()

    def second_signer():
        try:
            arriving.put(pid())
            return sign(reviewer).number
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(first_signer)
        try:
            holder, first_number = parked.get(timeout=10)
            second = workers.submit(second_signer)
            waiter = arriving.get(timeout=10)
            deadline = monotonic() + 10
            with connection.cursor() as cursor:
                while monotonic() < deadline:
                    cursor.execute(
                        "SELECT EXISTS (SELECT FROM pg_locks WHERE pid = %s "
                        "AND database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database()) "
                        "AND locktype = 'advisory' AND classid = %s "
                        "AND objid = %s AND objsubid = 2 "
                        "AND NOT granted AND %s = ANY(pg_blocking_pids(pid)))",
                        [waiter, *reviews.SIGNING_LOCK, holder],
                    )
                    if cursor.fetchone()[0]:
                        break
                    sleep(0.01)
                else:
                    pytest.fail("The second signing did not wait for the first transaction.")
            assert reviews.status().last == original
            assert list(Review.objects.values_list("number", flat=True)) == [original.number]
        finally:
            commit.set()
        assert first.result(timeout=10) == first_number
        second_number = second.result(timeout=10)
    assert list(Review.objects.order_by("id").values_list("number", flat=True)) == [
        original.number,
        first_number,
        second_number,
    ]
    assert reviews.status().last.number == second_number
