"""Independent review, round 1, of the nonconformance module: the reviewer's contracts, kept as
regression tests (.local/review-history/nonconformance-round-1.md). Where the fix changed the
design instead of the behaviour the reviewer proposed, the test states the resolved contract:

- NC-03, NC-07, NC-08: the complaint record's corrective action no longer depends on the module
  switch. A record opened from the complaint takes the reference's place; opening it advances the
  ticket's revision, so a page drawn before cannot sign or save over it. The complaint review
  shows the record's number only, which never changes.
- NC-17: history keeps what it recorded: a ticket's timeline and complaint keep the number of a
  record as text while the module is off; no link, panel or action of the module shows.
- NC-12: the reviewer's search-URL test is not kept. Lists search by GET across the application
  (bookmarkable, back button); the request log records the route pattern only (tested below) and
  record text must not hold patient identifiers (the forms say so).
- NC-14: a form posted from an earlier reading is answered as stale with the input kept, even if
  this reading no longer lets the poster act; a current reading still answers 403.

All records and accounts are synthetic."""

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import OperationalError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core import demo, modules
from core.models import AuditEvent, Lab, LabModule
from desk import services as desk_services
from desk.models import ComplaintReview
from nonconformance import selectors, services, summary
from nonconformance.models import CorrectiveAction, Nonconformance, TicketLink
from nonconformance.urls import urlpatterns
from teams.models import TeamMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def synthetic_password_hasher(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def nc_url(name, *ids):
    return reverse(f"nonconformance:{name}", args=ids)


@pytest.fixture
def owned(module_on, lead, agent):
    return services.raise_nonconformance(
        actor=lead,
        owner=agent,
        title="Example label review",
        description="Example batch.",
        source="internal",
        severity="minor",
    )


@pytest.fixture
def action(owned, tech, agent):
    item = services.add_action(
        actor=agent,
        nc=owned,
        expected_revision=owned.revision,
        description="Check the label",
        assignee=tech,
        due_on=timezone.localdate(),
    )
    owned.refresh_from_db()
    return item


@pytest.fixture
def linked(owned, plain_ticket, lead):
    item = services.link_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        request=plain_ticket,
    )
    owned.refresh_from_db()
    return item


@pytest.fixture
def other(make_user):
    return make_user("elsewhere", "account_manager")


@pytest.fixture
def decided(complaint, agent):
    desk_services.decide_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        decision="investigate",
        rationale="Investigate synthetic delay",
    )
    complaint.refresh_from_db()
    desk_services.update_complaint(
        actor=agent,
        request=complaint,
        expected_revision=complaint.revision,
        corrective_action_reference="CA-SYN-0042",
    )
    complaint.refresh_from_db()
    return complaint


AUDIENCES = ("admin", "lead", "agent", "tech", "other", "no_role", "portal", "anonymous")
STAFF = set(AUDIENCES[:5])
SUPERVISORS = {"admin", "lead"}
WORKERS = SUPERVISORS | {"agent"}


def login(client, request, who):
    if who != "anonymous":
        client.force_login(request.getfixturevalue(who))


def routes(owned, action, linked, complaint):
    targets = {
        "list": ((), STAFF),
        "action-list": ((), STAFF),
        "summary": ((), STAFF),
        "create": ((), STAFF),
        "detail": ((owned.pk,), STAFF),
        "from-ticket": ((complaint.pk,), WORKERS),
        "action-edit": ((owned.pk, action.pk), WORKERS),
        "action-cancel": ((owned.pk, action.pk), WORKERS),
        "action-done": ((owned.pk, action.pk), WORKERS | {"tech"}),
        "ticket-unlink": ((owned.pk, linked.pk), WORKERS),
    }
    for name in ("owner", "close", "reopen"):
        targets[name] = ((owned.pk,), SUPERVISORS)
    for name in (
        "edit",
        "status",
        "investigation",
        "impact",
        "effectiveness",
        "action-add",
        "ticket-link",
    ):
        targets[name] = ((owned.pk,), WORKERS)
    assert set(targets) == {route.name for route in urlpatterns}
    return targets


@pytest.mark.parametrize("who", AUDIENCES)
@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_direct_quality_pages(client, request, who, enabled, owned, action, linked, complaint):
    login(client, request, who)
    if not enabled:
        LabModule.objects.update(is_enabled=False)
    # Use a real form marker so even an otherwise empty POST is bound. The service suite
    # independently supplies valid mutations for allowed and disallowed roles.
    before = (owned.revision, AuditEvent.objects.count(), TicketLink.objects.count())
    wrong = []
    for name, (args, allowed) in routes(owned, action, linked, complaint).items():
        url = nc_url(name, *args)
        for method in ("get", "post"):
            if not enabled:
                expected = 404
            elif who == "anonymous":
                expected = 302
            elif who not in allowed:
                expected = 403
            elif method == "get" and name in {"close", "reopen", "effectiveness"}:
                expected = 302  # The open record is still outside Verification.
            else:
                expected = 200
            # The revision the route's form carries: the ticket's for opening from a complaint.
            revision = complaint.revision if name == "from-ticket" else owned.revision
            response = (
                client.get(url)
                if method == "get"
                else client.post(url, {"expected_revision": revision, "review_probe": "synthetic"})
            )
            if response.status_code != expected:
                wrong.append((method, name, expected, response.status_code))
            elif who == "anonymous" and enabled:
                assert response["Location"].startswith(reverse("account_login"))
    owned.refresh_from_db()
    assert before == (owned.revision, AuditEvent.objects.count(), TicketLink.objects.count())
    assert not wrong, wrong


@pytest.mark.parametrize("who", AUDIENCES)
def test_desk_and_switch_access(client, request, who, owned, decided):
    login(client, request, who)
    staff = who in STAFF
    supervisor = who in SUPERVISORS
    denied = 302 if who == "anonymous" else 403
    switch = reverse("lab-module", args=["nonconformance"])
    for name, url, allowed in (
        ("profile", reverse("lab-profile"), staff),
        ("switch", switch, who == "admin"),
        ("ticket", reverse("desk:request-detail", args=[decided.pk]), staff),
        ("record", reverse("desk:complaint-record", args=[decided.pk]), who in WORKERS),
        ("review", reverse("desk:complaint-review", args=[decided.pk]), supervisor),
    ):
        page = client.get(url)
        assert page.status_code == (200 if allowed else denied), name
        if name == "profile" and allowed:
            assert (f'href="{switch}"'.encode() in page.content) == (who == "admin")
        if name == "ticket" and allowed:
            assert (nc_url("from-ticket", decided.pk).encode() in page.content) == (who in WORKERS)
        if name == "record" and allowed:  # no record opened from it: the reference stays
            assert b'name="corrective_action_reference"' in page.content
        if name == "switch":
            post = client.post(url, {"enabled": "0", "expected_revision": 1, "reason": ""})
            assert post.status_code == (200 if allowed else denied)
        if name in {"review", "record"}:
            values = (
                {"note": "Reviewed synthetic record"}
                if name == "review"
                else {"form_shape": "nonconformance", "findings": "Synthetic finding"}
            )
            post = client.post(url, {"expected_revision": decided.revision, **values})
            can_save = supervisor if name == "review" else who in WORKERS
            assert post.status_code == (302 if can_save else 200 if allowed else denied)
            decided.refresh_from_db()
            review = ComplaintReview.objects.get(request=decided)
            if name == "record":
                assert bool(review.findings) == can_save
            else:
                assert review.is_reviewed == can_save


class PageTags(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.tags = []
        self.feed(response.content.decode())

    def attributes(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def input(self, name):
        return next(a.get("value", "") for a in self.attributes("input") if a.get("name") == name)


def form_revision(page):
    assert page.status_code == 200
    return int(PageTags(page).input("expected_revision"))


def form_values(page):
    """The values of our simple complaint form as a browser resubmits it."""
    return {field.name: field.value() or "" for field in page.context["form"]}


@pytest.mark.parametrize("opening", [False, True], ids=["linked", "from-complaint"])
def test_ticket_keeps_the_number_as_text_while_the_module_is_off(
    client, owned, agent, lead, complaint, opening
):
    if opening:
        nc = services.open_from_complaint(
            actor=agent,
            request=complaint,
            expected_revision=complaint.revision,
            title="Another example",
            description="Example delayed batch",
            severity="minor",
        )
    else:
        nc = owned
        services.link_ticket(
            actor=lead,
            nc=nc,
            request=complaint,
            expected_revision=nc.revision,
        )
    client.force_login(agent)
    url = reverse("desk:request-detail", args=[complaint.pk])
    on = client.get(url)
    assert nc.number.encode() in on.content
    assert any(a.get("href", "").startswith("/quality/") for a in PageTags(on).attributes("a"))
    LabModule.objects.update(is_enabled=False)
    off = client.get(url)
    assert off.status_code == 200
    assert nc.number.encode() in off.content  # history keeps what it recorded
    assert not any(a.get("href", "").startswith("/quality/") for a in PageTags(off).attributes("a"))
    assert "nonconformances" not in off.context or not off.context["nonconformances"]


def test_disabled_module_has_no_navigation_or_release_destination(client, owned, agent, complaint):
    client.force_login(agent)
    LabModule.objects.update(is_enabled=False)
    for url in (
        reverse("whats-new"),
        reverse("dashboard"),
        reverse("desk:request-detail", args=[complaint.pk]),
        reverse("desk:complaint-record", args=[complaint.pk]),
    ):
        page = client.get(url)
        assert page.status_code == 200
        assert not any(
            a.get("href", "").startswith("/quality/") for a in PageTags(page).attributes("a")
        )


def test_restricted_link_history_is_scoped_like_the_link(
    client, owned, agent, lead, restricted_ticket
):
    link = services.link_ticket(
        actor=lead,
        nc=owned,
        expected_revision=owned.revision,
        request=restricted_ticket,
    )
    owned.refresh_from_db()
    marker = "Example reason for withdrawing this link"
    services.unlink_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, link=link, reason=marker
    )
    url = nc_url("detail", owned.pk)
    client.force_login(lead)
    assert marker.encode() in client.get(url).content
    client.force_login(agent)
    page = client.get(url)
    assert not page.context["links"]
    assert restricted_ticket.number.encode() not in page.content
    traces = [
        event["title"]
        for event in page.context["history"]
        if event["title"] in {"Ticket linked", "Linked ticket removed"} or event["reason"] == marker
    ]
    assert (traces, marker.encode() in page.content) == ([], False)


def test_intervening_complaint_opening_keeps_the_second_draft(
    client, module_on, complaint, agent, lead
):
    client.force_login(agent)
    url = nc_url("from-ticket", complaint.pk)
    rev = form_revision(client.get(url))
    services.open_from_complaint(
        actor=lead,
        request=complaint,
        expected_revision=rev,
        title="First report",
        description="Supervisor's account.",
        severity="minor",
    )
    draft = "Independent observation from the second operator"
    post = client.post(
        url,
        {
            "expected_revision": rev,
            "title": "Second report",
            "description": draft,
            "severity": "major",
        },
    )
    assert Nonconformance.objects.filter(source_request=complaint).count() == 1
    assert complaint.entries.filter(event_action="nonconformance_opened").count() == 1
    assert post.status_code == 200 and draft.encode() in post.content


def test_losing_owner_role_keeps_the_refused_draft(client, owned, agent, lead, tech):
    client.force_login(agent)
    url = nc_url("investigation", owned.pk)
    rev = form_revision(client.get(url))
    services.set_owner(actor=lead, nc=owned, expected_revision=rev, owner=tech)
    draft = "Example stopped batch"
    post = client.post(url, {"expected_revision": rev, "containment": draft})
    owned.refresh_from_db()
    assert owned.containment == "" and owned.owner == tech
    assert draft.encode() in post.content


def test_supervisor_create_form_refuses_a_lost_owner_selection(client, lead, agent, module_on):
    client.force_login(lead)
    url = nc_url("create")
    assert "owner" in client.get(url).context["form"].fields
    lead.groups.set([Group.objects.get_or_create(name="technical")[0]])
    response = client.post(
        url,
        {
            "owner": agent.pk,
            "title": "Example observation",
            "description": "Example batch",
            "source": "internal",
            "severity": "minor",
        },
    )
    assert not Nonconformance.objects.exists()
    assert response.status_code == 200 and b"Example observation" in response.content


def test_switching_the_module_off_does_not_reshape_the_complaint_form(
    client, module_on, agent, decided
):
    client.force_login(agent)
    url = reverse("desk:complaint-record", args=[decided.pk])
    initial = form_values(client.get(url))
    assert initial["corrective_action_reference"] == "CA-SYN-0042"
    LabModule.objects.update(is_enabled=False)
    assert form_values(client.get(url)).keys() == initial.keys()
    saved = client.post(url, {**initial, "findings": "Example revised findings"})
    assert saved.status_code == 302
    review = ComplaintReview.objects.get(request=decided)
    assert review.findings == "Example revised findings"
    assert review.corrective_action_reference == "CA-SYN-0042"


def test_a_form_drawn_before_a_record_was_opened_cannot_change_the_reference(
    client, module_on, agent, lead, decided
):
    client.force_login(agent)
    url = reverse("desk:complaint-record", args=[decided.pk])
    drawn = form_values(client.get(url))
    services.open_from_complaint(
        actor=lead,
        request=decided,
        expected_revision=decided.revision,
        title="Opened meanwhile",
        description="Example",
        severity="minor",
    )
    refused = client.post(url, {**drawn, "corrective_action_reference": "CA-SYN-0099"})
    assert refused.status_code == 200
    # The typed reference is kept on the refused form (verification V-05), and not saved.
    assert form_values(refused)["corrective_action_reference"] == "CA-SYN-0099"
    review = ComplaintReview.objects.get(request=decided)
    assert review.corrective_action_reference == "CA-SYN-0042"
    decided.refresh_from_db()
    with pytest.raises(ValidationError):
        desk_services.update_complaint(
            actor=agent,
            request=decided,
            expected_revision=decided.revision,
            corrective_action_reference="CA-SYN-0100",
        )


def test_review_that_lost_independence_reopens_the_ticket_entry_point(
    client, module_on, decided, lead, agent
):
    desk_services.review_complaint(
        actor=lead, request=decided, expected_revision=decided.revision, note="Example review"
    )
    decided.refresh_from_db()
    TeamMembership.objects.get_or_create(team=decided.team, user=lead)
    decided = desk_services.assign(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        team=decided.team,
        assignee=lead,
        reason="Resume investigation",
    )
    client.force_login(lead)
    available = nc_url("from-ticket", decided.pk)
    assert client.get(available).status_code == 200
    page = client.get(reverse("desk:request-detail", args=[decided.pk]))
    assert page.context["review_counts"] is False
    assert available in [a.get("href") for a in PageTags(page).attributes("a")]


def in_second_connection(change):
    """A committed unit on another PostgreSQL connection, or False if the reader holds its lock."""

    def work():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '400ms'")
                change()
        except OperationalError as error:
            if getattr(error.__cause__, "sqlstate", None) != "55P03":
                raise
            return False
        finally:
            connections.close_all()
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(work).result(timeout=8)


@contextmanager
def at_read(matches, change, *, before=False):
    """Schedule a second transaction immediately before a statement or after it has its result."""
    outcomes = []

    def wrapper(execute, sql, params, many, context):
        selected = not outcomes and matches(sql)
        if selected and before:
            outcomes.append(in_second_connection(change))
        result = execute(sql, params, many, context)
        if selected and not before:
            outcomes.append(in_second_connection(change))
        return result

    with connection.execute_wrapper(wrapper):
        yield outcomes
    assert len(outcomes) == 1, "The scheduled read did not occur"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["deactivated", "role_removed"])
def test_record_lock_validates_current_actor_before_business_validation(owned, agent, state):
    # A no-change investigation cannot write. Its refusal distinguishes authorization from
    # ordinary field validation without ever submitting an unauthorized change of data.
    def change():
        if state == "deactivated":
            User.objects.filter(pk=agent.pk).update(is_active=False)
        else:
            User.objects.get(pk=agent.pk).groups.clear()

    def record_lock(sql):
        return 'FROM "nonconformance_nonconformance"' in sql and "FOR UPDATE" in sql

    refusal = None
    with at_read(record_lock, change, before=True) as concurrent:
        try:
            services.record_investigation(
                actor=agent,
                nc=owned,
                expected_revision=owned.revision,
                containment="",
                root_cause="",
            )
        except (ValidationError, PermissionDenied) as error:
            refusal = error
    assert owned.revision == Nonconformance.objects.get(pk=owned.pk).revision
    if concurrent == [True]:
        assert isinstance(refusal, PermissionDenied), str(refusal)
    else:
        assert isinstance(refusal, ValidationError)


@pytest.mark.django_db(transaction=True)
def test_switch_rechecks_admin_before_deciding_no_change(module_on, admin):
    def remove_admin():
        User.objects.get(pk=admin.pk).groups.clear()

    def switch_lock(sql):
        return 'FROM "core_labmodule"' in sql and "FOR UPDATE" in sql

    with at_read(switch_lock, remove_admin, before=True) as concurrent:
        try:
            modules.set_enabled(
                actor=admin,
                key="nonconformance",
                enabled=True,
                expected_revision=module_on.revision,
                reason="Still enabled",
            )
        except (PermissionDenied, ValidationError) as error:
            refusal = error
    assert LabModule.objects.get(pk=module_on.pk).revision == module_on.revision
    if concurrent == [True]:
        assert isinstance(refusal, PermissionDenied), str(refusal)
    else:
        assert isinstance(refusal, ValidationError)


def test_staff_assignment_requires_a_named_operational_role(owned, no_role):
    # Groups may grant capabilities independently of the five operational roles.
    no_role.groups.add(Group.objects.create(name="Synthetic capability holders"))
    with pytest.raises(ValidationError), transaction.atomic():
        services._staff_account(no_role, "owner", "the owner")


@pytest.mark.django_db(transaction=True)
def test_existing_module_write_finishes_before_switch_off(owned, admin):
    def switch_off():
        modules.set_enabled(
            actor=admin, key="nonconformance", enabled=False, expected_revision=1, reason="Pause"
        )

    with transaction.atomic():
        modules.lock_enabled("nonconformance")
        assert not in_second_connection(switch_off)
    assert in_second_connection(switch_off)


def test_the_complaint_save_does_not_depend_on_the_module_switch(client, decided, agent):
    """NC-07: the save's shape is decided by the record opened from the complaint (guarded by the
    ticket's revision), so the module switch is not read at all and cannot change between the
    page and the save."""
    url = reverse("desk:complaint-record", args=[decided.pk])
    client.force_login(agent)
    data = form_values(client.get(url))
    data["corrective_action_reference"] = "CA-SYN-0052"
    statements = []

    def record(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        response = client.post(url, data)
    assert response.status_code == 302
    assert not any('"core_labmodule"' in sql for sql in statements)
    saved = ComplaintReview.objects.get(request=decided)
    assert saved.corrective_action_reference == "CA-SYN-0052"


@pytest.mark.django_db(transaction=True)
def test_core_ticket_survives_a_module_switch_during_render(
    client, module_on, complaint, agent, admin
):
    client.force_login(agent)

    def disable():
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=1,
            reason="Pause checks",
        )

    with at_read(lambda sql: 'FROM "core_labmodule"' in sql, disable):
        page = client.get(reverse("desk:request-detail", args=[complaint.pk]))
    assert page.status_code == 200 and complaint.number.encode() in page.content


@pytest.mark.django_db(transaction=True)
def test_action_edit_renders_the_revision_that_owns_its_displayed_text(
    client,
    owned,
    action,
    agent,
    lead,
):
    client.force_login(agent)
    original = (owned.revision, action.description)
    updated = "Check both synthetic labels"

    def edit():
        services.update_action(
            actor=lead,
            nc=owned,
            expected_revision=original[0],
            action=action,
            description=updated,
            assignee=action.assignee,
            due_on=action.due_on,
            reason="Include second label",
        )

    with at_read(lambda sql: 'FROM "nonconformance_nonconformance"' in sql, edit):
        page = client.get(nc_url("action-edit", owned.pk, action.pk))
    observed = (form_revision(page), page.context["form"]["description"].value())
    assert observed in [original, (original[0] + 1, updated)]


@pytest.mark.django_db(transaction=True)
def test_summary_tables_count_the_same_records(owned, lead):
    def add_another():
        services.raise_nonconformance(
            actor=lead,
            title="Second example",
            description="Example",
            source="internal",
            severity="minor",
            area="Second area",
        )

    with at_read(lambda sql: "DATE_TRUNC" in sql and "COUNT(" in sql, add_another):
        counts = summary.counts(selectors.staff_nonconformances(lead))
    assert sum(area["count"] for area in counts["areas"]) == counts["total"]


@pytest.mark.parametrize(
    "route",
    [
        "detail",
        "list",
        "action-list",
        "summary",
        # The ticket page is left out by design: it is restored from the browser's back-forward
        # cache, which scrubs a revealed patient key (e2e/test_links_journey.py); a no-store page
        # would not be. The planned item on sessions marks every signed-in page.
        "complaint-record",
        "complaint-review",
        "lab-module",
    ],
)
def test_pages_with_record_text_or_unsaved_input_send_no_store(
    client, owned, action, admin, decided, route
):
    client.force_login(admin)
    if route.startswith("complaint-"):
        url = reverse(f"desk:{route}", args=[decided.pk])
        # A stale form displays the operator's own free text again.
        key = "findings" if route == "complaint-record" else "note"
        marker = "Synthetic draft for cache check"
        page = client.post(
            url,
            {
                key: marker,
                "expected_revision": decided.revision - 1,
                "form_shape": "nonconformance",
            },
        )
    elif route == "ticket":
        services.link_ticket(
            actor=admin, nc=owned, expected_revision=owned.revision, request=decided
        )
        marker = owned.title
        page = client.get(reverse("desk:request-detail", args=[decided.pk]))
    elif route == "lab-module":
        marker = "Synthetic stale switch reason"
        page = client.post(
            reverse("lab-module", args=["nonconformance"]),
            {"reason": marker, "enabled": "0", "expected_revision": 0},
        )
    elif route == "summary":
        marker = "Synthetic area for cache check"
        Nonconformance.objects.filter(pk=owned.pk).update(area=marker)
        page = client.get(nc_url(route))
    else:
        marker = action.description if route == "action-list" else owned.title
        page = client.get(nc_url(route, owned.pk) if route == "detail" else nc_url(route))
    assert page.status_code == 200 and marker.encode() in page.content
    assert "no-store" in page.get("Cache-Control", "").lower()


def test_record_fields_stay_out_of_ticket_events_redirects_logs_and_email(
    client, owned, lead, plain_ticket, caplog, django_capture_on_commit_callbacks
):
    client.force_login(lead)
    caplog.clear()
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.request"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        result = client.post(
            nc_url("ticket-link", owned.pk),
            {"expected_revision": owned.revision, "ticket": plain_ticket.number},
        )
    assert result.status_code == 302
    entry = plain_ticket.entries.get(event_action="nonconformance_linked")
    assert entry.event_data == {"nonconformance_id": owned.pk, "number": owned.number}
    assert not mail.outbox
    assert "route=" in caplog.text
    for value in (owned.title, owned.description, plain_ticket.subject):
        assert value not in caplog.text and value not in result["Location"]


def test_effective_check_is_current_for_newly_completed_corrective_work(owned, agent, lead):
    nc = services.change_status(
        actor=lead, nc=owned, expected_revision=owned.revision, status="verification"
    )
    nc = services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Reviewed example reports",
    )
    nc = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="effective",
        note="Checked initial corrective work",
    )
    action = services.add_action(
        actor=lead,
        nc=nc,
        expected_revision=nc.revision,
        description="Perform another correction",
        assignee=agent,
        due_on=timezone.localdate(),
    )
    nc.refresh_from_db()
    services.finish_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        action=action,
        done=True,
        note="Completed subsequent corrective work",
    )
    nc.refresh_from_db()
    assert AuditEvent.objects.filter(action="nonconformance.effectiveness_recorded").exists()
    assert services.close_refusal(lead, nc, nc.actions.filter(status="open").count())


def test_new_module_tables_participate_in_demo_empty_detection():
    assert demo.nonempty_tables() == []
    assert not modules.is_enabled("nonconformance")
    LabModule.objects.create(key="nonconformance", is_enabled=False)
    assert demo.nonempty_tables() == [LabModule._meta.db_table]


def test_repeat_demo_seed_preserves_quality_records_and_history(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-seed-example")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "separate-synthetic-seed-example")
    call_command("seed_demo", verbosity=0)
    assert demo.is_sealed(Lab.objects.get())
    assert modules.is_enabled("nonconformance")
    assert Nonconformance.objects.count() == 4
    assert CorrectiveAction.objects.count() == 4
    assert TicketLink.objects.count() == 2
    tracked = [LabModule, Nonconformance, CorrectiveAction, TicketLink, AuditEvent]

    def snapshot():
        return [list(model.objects.order_by("pk").values()) for model in tracked]

    first = snapshot()
    call_command("seed_demo", verbosity=0)
    assert snapshot() == first


@pytest.mark.django_db(transaction=True)
def test_populated_upgrade_preserves_complaint_and_audit_history():
    head = MigrationExecutor(connection).loader.graph.leaf_nodes()
    base = [("nonconformance", None), ("core", "0002_history_immutable")]
    try:
        old = MigrationExecutor(connection).migrate(base).apps
        person = old.get_model("accounts", "User").objects.create(username="upgrade-review")
        lab = old.get_model("core", "Lab").objects.create(name="Example upgrade laboratory")
        team = old.get_model("teams", "Team").objects.create(slug="upgrade", name="Example team")
        category = old.get_model("taxonomy", "RequestCategory").objects.create(
            key="upgrade", name="Example complaint"
        )
        ticket = old.get_model("desk", "ServiceRequest").objects.create(
            number="REQ-900001",
            subject="Example upgrade complaint",
            channel="phone",
            team=team,
            category=category,
            created_by=person,
            is_complaint=True,
        )
        review = old.get_model("desk", "ComplaintReview").objects.create(
            request=ticket, corrective_action_reference="CA-EXAMPLE-042"
        )
        event = old.get_model("core", "AuditEvent").objects.create(
            actor=person,
            action="lab.setup",
            object_type="core.lab",
            object_id=str(lab.pk),
            after={"name": lab.name},
            reason="Synthetic fixture",
        )

        def snapshot():
            return [
                type(row).objects.filter(pk=row.pk).values().get()
                for row in (lab, person, ticket, review, event)
            ]

        original = snapshot()
        MigrationExecutor(connection).migrate(head)
        assert snapshot() == original
        assert not modules.is_enabled("nonconformance")
        assert all(
            not model.objects.exists()
            for model in (LabModule, Nonconformance, CorrectiveAction, TicketLink)
        )
    finally:
        MigrationExecutor(connection).migrate(head)


def corrective_action_fact(page):
    html = page.content.decode()
    start = html.index("Corrective action")
    return html[start : html.index("</div>", start)]


@pytest.mark.parametrize("intervening", ["record_status", "disable"])
def test_approval_covers_the_corrective_action_information_the_reviewer_read(
    client, module_on, agent, lead, admin, decided, intervening
):
    """The review page shows the record's number only, which never changes once opened (opening
    it advances the ticket's revision). A change of the record's status or of the module switch
    leaves what the reviewer read, and so what they approve, as it was."""
    nc = services.open_from_complaint(
        actor=agent,
        request=decided,
        expected_revision=decided.revision,
        title="Example review",
        description="Synthetic complaint",
        severity="minor",
    )
    decided.refresh_from_db()
    url = reverse("desk:complaint-review", args=[decided.pk])
    client.force_login(lead)
    before = client.get(url)
    data = form_values(before)
    read = corrective_action_fact(before)
    assert nc.number in read and nc.get_status_display() not in read
    if intervening == "record_status":
        services.change_status(
            actor=admin, nc=nc, expected_revision=nc.revision, status="investigating"
        )
    else:
        modules.set_enabled(
            actor=admin,
            key="nonconformance",
            enabled=False,
            expected_revision=1,
            reason="Suspend optional module",
        )
    assert corrective_action_fact(client.get(url)) == read
    response = client.post(url, {**data, "note": "Checked the corrective action displayed"})
    assert response.status_code == 302
    assert ComplaintReview.objects.get(request=decided).is_reviewed


def test_switching_on_keeps_the_reference_typed_in_the_open_form(client, decided, agent, admin):
    client.force_login(agent)
    url = reverse("desk:complaint-record", args=[decided.pk])
    data = form_values(client.get(url))
    modules.set_enabled(
        actor=admin,
        key="nonconformance",
        enabled=True,
        expected_revision=0,
        reason="Start optional module",
    )
    draft = "CA-SYN-UNSAVED"
    response = client.post(
        url, {**data, "corrective_action_reference": draft, "findings": "Example from draft"}
    )
    assert response.status_code == 302
    assert ComplaintReview.objects.get(request=decided).corrective_action_reference == draft


def test_changed_status_is_reported_as_stale_and_keeps_the_posted_choice(
    client, owned, agent, lead
):
    client.force_login(agent)
    url = nc_url("status", owned.pk)
    revision = form_revision(client.get(url))
    services.change_status(actor=lead, nc=owned, expected_revision=revision, status="investigating")
    response = client.post(
        url,
        {
            "expected_revision": revision,
            "status": "investigating",
            "reason": "Example status draft",
        },
    )
    owned.refresh_from_db()
    assert owned.revision == revision + 1
    assert b"Example status draft" in response.content
    assert services.STALE in response.context["form"].non_field_errors()


def test_remove_form_discards_ticket_preview_after_a_scope_refusal(
    client, owned, linked, agent, plain_ticket, monkeypatch
):
    # Stub the domain's "no longer readable" outcome. Even with a previously authorized
    # preview in memory, the refusal must not draw that preview again.
    from django.http import Http404

    client.force_login(agent)
    url = nc_url("ticket-unlink", owned.pk, linked.pk)
    page = client.get(url)
    rev = form_revision(page)
    assert plain_ticket.subject.encode() in page.content

    def unavailable(**kwargs):
        raise Http404("Ticket unavailable")

    monkeypatch.setattr(services, "unlink_ticket", unavailable)
    post = client.post(url, {"expected_revision": rev, "reason": "Example draft"})
    assert post.status_code in {200, 404}
    assert plain_ticket.subject.encode() not in post.content
    assert plain_ticket.number.encode() not in post.content


def test_complaint_review_and_ticket_read_forms_post_the_revisions_they_drew(
    client, owned, action, linked, decided, lead, agent
):
    client.force_login(lead)
    nc = services.change_status(
        actor=lead, nc=owned, expected_revision=owned.revision, status="verification"
    )
    for name in (
        "edit",
        "status",
        "owner",
        "investigation",
        "impact",
        "effectiveness",
        "action-add",
        "ticket-link",
        "action-edit",
        "action-done",
        "action-cancel",
        "ticket-unlink",
    ):
        ids = [nc.pk]
        if name in {"action-edit", "action-done", "action-cancel"}:
            ids.append(action.pk)
        elif name == "ticket-unlink":
            ids.append(linked.pk)
        assert form_revision(client.get(nc_url(name, *ids))) == nc.revision, name
    for url in (
        nc_url("from-ticket", decided.pk),
        reverse("desk:complaint-review", args=[decided.pk]),
        reverse("desk:complaint-record", args=[decided.pk]),
    ):
        assert form_revision(client.get(url)) == decided.revision

    # The owner finishes an action while the supervisor's edit form is open.
    old = nc.revision
    services.finish_action(
        actor=agent,
        nc=nc,
        expected_revision=old,
        action=action,
        done=True,
        note="Finished normally",
    )
    draft = "Example revision to the description"
    response = client.post(
        nc_url("action-edit", nc.pk, action.pk),
        {
            "expected_revision": old,
            "description": draft,
            "assignee": agent.pk,
            "due_on": action.due_on,
            "reason": "Example revision",
        },
    )
    assert response.status_code == 200 and draft.encode() in response.content
    assert services.STALE in response.context["form"].non_field_errors()
    nc.refresh_from_db()
    nc = services.record_impact(
        actor=lead,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Checked synthetic batch",
    )
    nc = services.record_effectiveness(
        actor=lead,
        nc=nc,
        expected_revision=nc.revision,
        result="effective",
        note="Checked synthetic action",
    )
    assert form_revision(client.get(nc_url("close", nc.pk))) == nc.revision
    nc = services.close(actor=lead, nc=nc, expected_revision=nc.revision, note="Checked")
    assert form_revision(client.get(nc_url("reopen", nc.pk))) == nc.revision


def test_quality_history_uses_append_only_audit_rows(owned):
    from django.db import DatabaseError

    events = AuditEvent.objects.filter(action="nonconformance.raised", object_id=str(owned.pk))
    before = events.values().get()
    for change in (lambda: events.update(reason="Replacement"), events.delete):
        with (
            pytest.raises(DatabaseError, match="cannot be updated or deleted"),
            transaction.atomic(),
        ):
            change()
    assert events.values().get() == before


def test_restricted_ticket_values_are_absent_from_quality_reads_and_staff_lookup(
    client, owned, restricted_ticket, lead, agent
):
    services.link_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, request=restricted_ticket
    )
    client.force_login(agent)
    seen = []
    for url in (
        nc_url("detail", owned.pk),
        nc_url("list"),
        nc_url("action-list"),
        nc_url("summary"),
        nc_url("ticket-link", owned.pk),
        reverse("lookup", args=["staff"]),
    ):
        result = client.get(url)
        assert result.status_code == 200
        seen.append(result.content)
    for marker in (
        restricted_ticket.subject,
        restricted_ticket.number,
        "Synthetic restricted note.",
    ):
        assert all(marker.encode() not in data for data in seen)


def test_quality_capture_and_refused_forms_use_no_store(client, owned, agent, complaint):
    client.force_login(agent)
    form = nc_url("investigation", owned.pk)
    for page in (
        client.get(nc_url("create")),
        client.get(nc_url("from-ticket", complaint.pk)),
        client.get(form),
        client.post(
            form, {"expected_revision": owned.revision - 1, "containment": "Example draft"}
        ),
    ):
        assert page.status_code == 200 and "no-store" in page["Cache-Control"]


def test_closing_uses_the_impact_assessment_the_supervisor_reviewed(client, owned, lead, agent):
    nc = services.change_status(
        actor=agent, nc=owned, expected_revision=owned.revision, status="verification"
    )
    nc = services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Initial review found no impact",
    )
    nc = services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="effective",
        note="Checked initial corrective work",
    )
    client.force_login(lead)
    page = client.get(nc_url("detail", nc.pk))
    assert nc.impact_assessment.encode() in page.content
    close_path = nc_url("close", nc.pk)
    close_href = next(
        a["href"]
        for a in PageTags(page).attributes("a")
        if a.get("href", "").split("?")[0] == close_path
    )

    latest = services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="affected",
        client_notice="not_needed",
        impact_assessment="Later review found affected reports",
    )
    confirm = client.get(close_href)
    if confirm.status_code == 200 and "form" in confirm.context:
        data = form_values(confirm)
        if "note" in data:
            client.post(close_href, {**data, "note": "Reviewed the assessment on the record"})
    latest.refresh_from_db()
    # Either show the changed decision for review, or require the supervisor to reload.
    assert latest.status != "closed" or latest.impact_assessment.encode() in confirm.content


def test_ticket_link_and_both_histories_roll_back_as_one(owned, lead, plain_ticket, monkeypatch):
    original = (
        owned.revision,
        plain_ticket.revision,
        plain_ticket.entries.count(),
        AuditEvent.objects.count(),
    )

    def fail_last_save(nc):
        raise RuntimeError("Synthetic interruption")

    monkeypatch.setattr(services, "_save", fail_last_save)
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        services.link_ticket(
            actor=lead, nc=owned, expected_revision=owned.revision, request=plain_ticket
        )
    owned.refresh_from_db()
    plain_ticket.refresh_from_db()
    assert not TicketLink.objects.filter(nonconformance=owned, request=plain_ticket).exists()
    assert (
        owned.revision,
        plain_ticket.revision,
        plain_ticket.entries.count(),
        AuditEvent.objects.count(),
    ) == original


@pytest.mark.django_db(transaction=True)
def test_complaint_openings_serialize_and_write_one_ticket_event(module_on, complaint, agent, lead):
    original = complaint.revision

    def open_record(actor):
        return services.open_from_complaint(
            actor=actor,
            request=complaint,
            expected_revision=original,
            title="Example complaint",
            description="Example batch",
            severity="minor",
        )

    with transaction.atomic():
        first = open_record(agent)
        assert not in_second_connection(lambda: open_record(lead))
    with pytest.raises(ValidationError):
        open_record(lead)
    complaint.refresh_from_db()
    assert complaint.revision == original + 1
    assert list(Nonconformance.objects.filter(source_request=complaint)) == [first]
    assert complaint.entries.filter(event_action="nonconformance_opened").count() == 1
