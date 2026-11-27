"""Verification of the round-one fixes using real account pages and synthetic records."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from html import escape

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models.functions import Now
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from test_review_deactivation import (
    DIRECT,
    DrawnInput,
    account_data,
    account_events,
    backend_pid,
    edit_to_post,
    holds_access_gate,
    label_payload,
    other_connection,
    submit,
    token,
)

from accounts import deactivation, grant_ends
from accounts.admin import DeactivateForm
from accounts.models import AccountSession, CapabilityEnd, Deactivation, SignInEvent, User
from accounts.roles import has_capability
from accounts.tests.test_confirm_identity import page_data
from accounts.tests.test_deactivation import (
    PASSWORD,
    browser_for,
    capability,
    change_url,
    make_owner,
    page_url,
)
from accounts.tests.test_deactivation import owner as owner
from accounts.tests.test_deactivation import person as person
from accounts.tests.test_deactivation import team as team
from clients.models import PortalAccess
from clients.portal import current_access
from core.page_docs import document_for_route
from teams.models import TeamMembership

pytestmark = pytest.mark.django_db
REASON = "Synthetic rotation <completed>"
SECOND = "review_audit_records"


def committed_post(http, url, data):
    """The other browser commits on its own connection before the interrupted request resumes."""

    def post():
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            return http.post(url, data)

    with ThreadPoolExecutor(max_workers=1) as pool:
        answer = pool.submit(other_connection, post).result(timeout=10)
    assert answer.status_code == 302


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
@pytest.mark.parametrize("refusal", ["reason", "confirmation"])
@pytest.mark.parametrize("edit", ["deactivate", "rename"])
def test_refused_form_redraws_the_current_target_status_and_username(
    owner, person, monkeypatch, refusal, edit
):
    reviewer, editor = browser_for(owner), browser_for(make_owner("other-editor"))
    original = reviewer.get(page_url(person))
    changes = {"is_active": ""} if edit == "deactivate" else {"username": "new-review-name"}
    edit_data = account_data(editor, person, confirm_password=PASSWORD, **changes)
    validate = DeactivateForm.is_valid

    def finish_validation(form):
        valid = validate(form)
        assert not valid
        committed_post(editor, change_url(person), edit_data)
        return valid

    monkeypatch.setattr(DeactivateForm, "is_valid", finish_validation)
    response = submit(
        reviewer,
        person,
        original,
        reason="" if refusal == "reason" else REASON,
        confirm_password="" if refusal == "confirmation" else PASSWORD,
    )
    fresh = reviewer.get(page_url(person))
    assert response.status_code == fresh.status_code == 200
    if edit == "deactivate":
        assert Deactivation.objects.get(user=person).deactivated_by_id != owner.pk
        assert fresh.context["refused"] == deactivation.REFUSALS["inactive"]
        assert (response.context["refused"], DrawnInput(response).tokens) == (
            fresh.context["refused"],
            [],
        )
    else:
        assert not Deactivation.objects.filter(user=person).exists()
        assert "What <strong>new-review-name</strong> holds now:" in fresh.content.decode()
        assert token(response) == token(fresh)
        assert "What <strong>new-review-name</strong> holds now:" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_refusal_notice_tracks_a_target_restored_before_redraw(owner, person, monkeypatch):
    reviewer, editor = browser_for(owner), browser_for(make_owner("restoring-owner"))
    preview = reviewer.get(page_url(person))
    assert submit(editor, person, editor.get(page_url(person))).status_code == 302
    restore = account_data(editor, person, is_active="on")
    refuse = deactivation.refusal
    changes = []

    def restore_after_decision(account, actor):
        why = refuse(account, actor)
        if why == deactivation.REFUSALS["inactive"] and not changes:
            committed_post(editor, change_url(person), restore)
            changes.append(True)
        return why

    monkeypatch.setattr(deactivation, "refusal", restore_after_decision)
    rejected = submit(reviewer, person, preview)
    fresh = reviewer.get(page_url(person))
    assert changes == [True] and User.objects.get(pk=person.pk).is_active
    assert Deactivation.objects.filter(user=person).count() == 1
    assert rejected.status_code == 200 and token(fresh)
    assert (
        "This account is already deactivated."
        not in rejected.content.decode().split('id="content-main"', 1)[1].split("</main>", 1)[0]
    )
    assert token(rejected) == token(fresh)


def test_stale_save_builds_the_replacement_list_once_after_the_service_refuses(
    owner, person, monkeypatch
):
    reviewer = browser_for(owner)
    preview = reviewer.get(page_url(person))
    person.groups.add(Group.objects.get_or_create(name="technical")[0])
    after_refusal = False
    reads = []
    save, holdings = deactivation.deactivate, deactivation.holdings

    def observe_save(**kwargs):
        nonlocal after_refusal
        try:
            return save(**kwargs)
        except ValidationError:
            after_refusal = True
            raise

    def observe_read(pk):
        with CaptureQueriesContext(connection) as queries:
            held = holdings(pk)
        if after_refusal:
            reads.append(len(queries))
        return held

    monkeypatch.setattr(deactivation, "deactivate", observe_save)
    monkeypatch.setattr(deactivation, "holdings", observe_read)
    rejected = submit(reviewer, person, preview)
    assert rejected.status_code == 200 and "Technical staff" in label_payload(rejected)
    assert token(preview) != token(rejected) and not Deactivation.objects.exists()
    assert len(reads) <= 1, f"SQL statements in each holdings read after the refusal: {reads}"


@pytest.mark.parametrize("case", ["own", "empty reason", "long reason", "valid"])
def test_service_refusal_uses_current_owner_status_even_with_a_stale_actor(owner, person, case):
    earlier = User.objects.get(pk=owner.pk)
    owner.is_staff = False
    owner.save(update_fields=["is_staff"])
    reason = {"empty reason": "", "long reason": "x" * 1001}.get(case, REASON)
    with pytest.raises(PermissionDenied):
        deactivation.deactivate(
            actor=earlier,
            account_id=owner.pk if case == "own" else person.pk,
            reason=reason,
            drawn=deactivation.fingerprint(deactivation.holdings(person.pk)),
        )
    assert not Deactivation.objects.exists()


def test_service_accepts_a_current_owner_with_an_actor_loaded_before_promotion(owner, person):
    target = User.objects.create_user(
        "new-console-owner", "admin-review@example.invalid", PASSWORD, is_staff=True
    )
    earlier = User.objects.get(pk=target.pk)
    admin = browser_for(owner)
    saved = admin.post(change_url(target), account_data(admin, target, is_superuser="on"))
    assert saved.status_code == 302
    assert deactivation.owns_administration(User.objects.get(pk=target.pk))
    record = deactivation.deactivate(
        actor=earlier, account_id=person.pk, reason=REASON, drawn=token(admin.get(page_url(person)))
    )
    assert record.user_id == person.pk and record.deactivated_by_id == target.pk


@pytest.mark.parametrize("ends", ["both", "none"])
def test_entire_event_matches_the_edited_account_without_its_email(owner, person, ends, settings):
    settings.TIME_ZONE = "America/New_York"
    admin = browser_for(owner)
    perm = capability(SECOND)
    person.user_permissions.add(perm)
    person.first_name = "Before"
    person.is_staff = True
    person.save(update_fields=["first_name", "is_staff"])
    expected = {
        "username": person.username,
        "first_name": "Before",
        "last_name": "",
        "is_active": True,
        "is_staff": True,
        "is_superuser": False,
        "client": None,
        "groups": ["supervisor"],
        "permissions": sorted([f"accounts.{DIRECT}", f"accounts.{SECOND}"]),
    }
    CapabilityEnd.objects.filter(user=person).delete()
    if ends == "both":
        expected["capability_ends"] = {}
        for code, delta in [(DIRECT, -2), (SECOND, 7)]:
            moment = grant_ends.end_of(grant_ends.today() + timedelta(days=delta))
            CapabilityEnd.objects.create(user=person, permission=capability(code), ends_at=moment)
            expected["capability_ends"][f"accounts.{code}"] = moment.astimezone(UTC).isoformat()
    result = submit(admin, person, admin.get(page_url(person)), reason=REASON)
    assert result.status_code == 302
    event = account_events(person).get()
    assert (event.before, event.after) == (expected, {**expected, "is_active": False})
    assert event.reason == REASON and event.actor_id == owner.pk


@pytest.mark.xfail(
    strict=True,
    reason="By design: the account change event names the account by its username, as every "
    "account change event does, so a username chosen as an address is kept as the username; the "
    "event has no email field and the record holds neither.",
)
def test_address_shaped_login_is_displayed_but_the_event_contains_no_email_address(owner):
    address = "rotating-owner@example.invalid"
    person = User.objects.create_user(address, address, PASSWORD)
    admin = browser_for(owner)
    preview = admin.get(page_url(person))
    assert f"What <strong>{address}</strong> holds now:" in preview.content.decode()
    assert submit(admin, person, preview).status_code == 302
    row = Deactivation.objects.get(user=person)
    event = account_events(person).get()
    assert (
        address not in json.dumps(row.held)
        and "email" not in event.before
        and "email" not in event.after
    )
    assert address not in json.dumps([event.before, event.after])


@pytest.mark.parametrize("reason_given", [True, False], ids=["reason", "active-box"])
def test_restored_owner_and_other_owner_get_their_own_history_wording_on_get_and_form_error(
    owner, reason_given
):
    target = make_owner("returning-owner")
    other_http = browser_for(owner)
    if reason_given:
        saved = submit(other_http, target, other_http.get(page_url(target)), reason=REASON)
    else:
        saved = other_http.post(change_url(target), account_data(other_http, target, is_active=""))
    assert saved.status_code == 302
    assert (
        other_http.post(
            change_url(target), account_data(other_http, target, is_active="on")
        ).status_code
        == 302
    )
    target.refresh_from_db()
    self_http = browser_for(target)
    for http, own in [(self_http, True), (other_http, False)]:
        url = change_url(target)
        for response in (
            http.get(url + "?_popup=1"),
            http.post(url, account_data(http, target, username="")),
        ):
            assert response.status_code == 200
            line = (
                response.content.decode()
                .split('<div class="vr-deactivation">', 1)[1]
                .split("</div>", 1)[0]
            )
            assert "Made active again since" in line
            assert (escape(REASON) in line) == (reason_given and not own)
            assert ("It is not shown on your own account" in line) == (own and reason_given)
            assert ("No reason was recorded" in line) == (not reason_given)


@pytest.mark.django_db(transaction=True)
def test_two_deactivations_in_one_transaction_have_distinct_database_write_times(person):
    times = []
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        (started,) = cursor.fetchone()
        for _ in range(2):
            cursor.execute("SELECT clock_timestamp()")
            (immediately_before,) = cursor.fetchone()
            person.is_active = False
            person.save(update_fields=["is_active"])
            cursor.execute("SELECT clock_timestamp()")
            (immediately_after,) = cursor.fetchone()
            written = deactivation.latest(person.pk).deactivated_at
            assert started < immediately_before <= written <= immediately_after
            times.append(written)
            person.is_active = True
            person.save(update_fields=["is_active"])
    assert times[0] < times[1]
    assert (
        list(
            Deactivation.objects.filter(user=person)
            .order_by("pk")
            .values_list("deactivated_at", flat=True)
        )
        == times
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["team add", "team lead", "group capability"])
def test_operator_help_qualifies_its_wait_promise_for_ungated_page_edits(owner, person, team, kind):
    owner.groups.add(Group.objects.get_or_create(name="admin")[0])
    editor, reviewer = browser_for(owner), browser_for(make_owner("preview-owner"))
    if kind == "team lead":
        TeamMembership.objects.filter(user=person, team=team).update(is_lead=False)
    target, url, fields = edit_to_post(kind, editor, person, team, None)
    if kind == "team lead":
        fields["is_lead"] = "1"
    before = reviewer.get(page_url(target))
    with transaction.atomic():
        saved = editor.post(url, fields)
        assert saved.status_code == 302
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(
                other_connection, holds_access_gate, backend_pid(), target.pk
            ).result(10)
    refused = submit(reviewer, target, before)
    assert refused.status_code == 200 and "changed after you opened" in refused.content.decode()
    assert token(refused) != token(before)
    help_doc = document_for_route("admin:accounts_user_deactivate")
    rules = next(
        section["paragraphs"]
        for section in help_doc["sections"]
        if section["title"] == "Rules and records"
    )
    unconditional = (
        "Your save waits for a change of the account's roles, capabilities, teams or "
        "portal access that is being saved on the application's pages at that moment; "
        "if that change alters the list, your save is refused, so the record matches the list "
        "you saw."
    )
    assert not any(paragraph.startswith(unconditional) for paragraph in rules)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("when", ["before comparison", "between readings", "after record reading"])
def test_a_saved_record_uses_the_team_name_on_the_accepted_preview(
    owner, person, team, monkeypatch, when
):
    """Change only a display label. The account and both users' permissions stay the same."""
    manager = make_owner("team-editor")
    manager.groups.add(Group.objects.get_or_create(name="admin")[0])
    editor, reviewer = browser_for(manager), browser_for(owner)
    preview = reviewer.get(page_url(person))
    shown = team.name
    assert shown in label_payload(preview)
    url = reverse("teams:team-edit", args=[team.pk])
    fields = page_data(
        editor.get(url).context["form"],
        name="Renamed verification bench",
        reason="Label correction",
    )
    read_record = deactivation.record_held

    def read_at_boundary(user):
        if when == "between readings":
            committed_post(editor, url, fields)
        reading = read_record(user)
        if when == "after record reading":
            committed_post(editor, url, fields)
        return reading

    if when == "before comparison":
        committed_post(editor, url, fields)
    else:
        monkeypatch.setattr(deactivation, "record_held", read_at_boundary)
    result = submit(reviewer, person, preview)
    team.refresh_from_db()
    assert team.name == fields["name"]
    if result.status_code == 200:
        assert "changed after you opened" in result.content.decode()
        assert team.name in label_payload(result)
        assert (
            User.objects.get(pk=person.pk).is_active
            and not Deactivation.objects.filter(user=person).exists()
        )
    else:
        assert result.status_code == 302 and result.url == change_url(person)
        assert Deactivation.objects.get(user=person).held["teams"] == [
            {"text": shown, "note": "Lead"}
        ]


def test_restoring_preserves_live_group_access_alongside_an_ended_direct_grant(owner, person):
    today = grant_ends.database_today()
    end = CapabilityEnd.objects.get(user=person)
    end.ends_at = grant_ends.end_of(today - timedelta(days=1))
    end.save()
    person.groups.get().permissions.add(capability(DIRECT))
    http = browser_for(owner)
    preview = http.get(page_url(person))
    assert "Directly, ended after" in label_payload(preview)
    assert submit(http, person, preview).status_code == 302
    person.refresh_from_db()
    assert not has_capability(person, DIRECT)
    assert (
        http.post(change_url(person), account_data(http, person, is_active="on")).status_code == 302
    )
    person.refresh_from_db()
    assert has_capability(person, DIRECT)
    person.groups.get().permissions.remove(capability(DIRECT))
    assert not has_capability(person, DIRECT)
    assert CapabilityEnd.objects.get(user=person).ends_at == end.ends_at


@pytest.mark.parametrize("last_day_delta", [-1, 0, 1])
def test_portal_reactivation_respects_the_whole_last_day(
    owner, client_org, portal_user, last_day_delta
):
    person = portal_user(client_org)
    last_day = grant_ends.database_today() + timedelta(days=last_day_delta)
    access = PortalAccess.objects.create(
        user=person, client=client_org, whole_organization=True, ends_at=grant_ends.end_of(last_day)
    )
    http = browser_for(owner)
    preview = http.get(page_url(person))
    expected = (
        f"Only the tickets it raised (access ended after {last_day})"
        if last_day_delta < 0
        else f"Its organization's tickets, last day {last_day}"
    )
    assert expected in label_payload(preview)
    assert submit(http, person, preview).status_code == 302
    person.refresh_from_db()
    assert current_access(person) is None
    assert (
        http.post(change_url(person), account_data(http, person, is_active="on")).status_code == 302
    )
    person.refresh_from_db()
    assert (current_access(person) is not None) == (last_day_delta >= 0)
    assert PortalAccess.objects.get(pk=access.pk).ends_at == grant_ends.end_of(last_day)
    assert Deactivation.objects.get(user=person).held["portal"] == expected


def test_history_distinguishes_both_admin_actions_and_a_session_already_past_its_limit(
    owner, person
):
    http = browser_for(owner)
    live = browser_for(person).session["_account_session"]
    expired = AccountSession.objects.create(
        user=person,
        started_at=Now() - timedelta(hours=3),
        last_active_at=Now() - timedelta(hours=2),
    ).pk
    preview = http.get(page_url(person))
    assert "1 session is open now" in preview.content.decode()
    assert submit(http, person, preview, reason=REASON).status_code == 302
    event = account_events(person).get()
    assert (event.reason, event.before["is_active"], event.after["is_active"]) == (
        REASON,
        True,
        False,
    )
    changes = LogEntry.objects.filter(object_id=str(person.pk))
    assert changes.get().get_change_message() == "Deactivated the account."
    for session, ending in [
        (live, AccountSession.Ending.DEACTIVATED),
        (expired, AccountSession.Ending.INACTIVITY),
    ]:
        assert AccountSession.objects.get(pk=session).ending == ending
        sign_out = SignInEvent.objects.get(session=session, kind=SignInEvent.Kind.SIGNED_OUT)
        assert sign_out.ending == ending
    assert (
        http.post(change_url(person), account_data(http, person, is_active="on")).status_code == 302
    )
    assert (
        http.post(change_url(person), account_data(http, person, is_active="")).status_code == 302
    )
    assert deactivation.latest(person.pk).reason == ""
    assert "Active" in changes.latest("pk").get_change_message()
    assert account_events(person).order_by("-occurred_at").first().reason == "Django admin"


def test_portal_event_preserves_access_report_attribution_without_copying_contact_details(
    owner, client_org, portal_user
):
    from audit_review.access_report import describe

    account = portal_user(client_org)
    PortalAccess.objects.create(user=account, client=client_org, whole_organization=True)
    http = browser_for(owner)
    assert submit(http, account, http.get(page_url(account)), reason=REASON).status_code == 302
    event = account_events(account).get()
    for snapshot in (event.before, event.after):
        assert snapshot["client"] == client_org.pk
        assert "capability_ends" not in snapshot and "email" not in snapshot
        assert snapshot["groups"] == snapshot["permissions"] == []
    account.refresh_from_db()
    (report,) = describe([account], history=True)
    assert report.status == "Deactivated" and report.organization == client_org.name
    assert (report.deactivated.username, report.deactivated.at) == (
        owner.username,
        event.occurred_at,
    )
