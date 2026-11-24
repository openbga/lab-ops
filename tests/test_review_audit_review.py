"""Independent regression cases for the audit review workspace, using synthetic records.

Adopted from the independent review (round 1, .local/review-history/audit-review-round-1.md).
Adapted: the reading is held by the newest id where ids follow the writing order
(audit_review.sources.newest), so the cases that patched `now_marker` patch `newest`; two limits
documented in docs/architecture.md ("Audit review workspace") are strict xfails."""

import csv
import html
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from importlib.util import find_spec
from threading import Event
from urllib.parse import urljoin

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import IntegrityError, close_old_connections, connection, connections
from django.http import HttpResponse
from django.test import Client as BrowserClient
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts.models import SignInEvent, User
from core import access
from core.models import AccessRecord, AuditEvent

# The same file can be collected in the pre-workspace checkout for the shared behavior.
if find_spec("audit_review"):
    from audit_review import sources
    from audit_review.forms import ReviewFilter
    from audit_review.models import Source, UseKind, WorkspaceUse
    from audit_review.tests.conftest import agent as agent
    from audit_review.tests.conftest import category as category
    from audit_review.tests.conftest import clinic as clinic
    from audit_review.tests.conftest import make_ticket as make_ticket
    from audit_review.tests.conftest import make_user as make_user
    from audit_review.tests.conftest import reviewer as reviewer
    from audit_review.tests.conftest import water as water


def _review_csv(response):
    import codecs

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert "no-store" in response["Cache-Control"]
    assert response.content.startswith(codecs.BOM_UTF8)
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


# Recording and test discovery


def test_default_unit_collection_includes_app_regressions(pytestconfig):
    """The repository and CI use pytest without an explicit list of unit files."""
    roots = [pytestconfig.rootpath / path for path in pytestconfig.getini("testpaths")]
    omitted = [
        str(path.relative_to(pytestconfig.rootpath))
        for path in sorted(pytestconfig.rootpath.glob("*/tests/test_*.py"))
        if not any(path.is_relative_to(root) for root in roots)
    ]
    assert omitted == []


@pytest.mark.django_db(transaction=True)
def test_access_export_matches_page_when_a_read_commits_before_the_marker(
    client, reviewer, agent, monkeypatch
):
    """A writer starts after the page has its snapshot and finishes before its cutoff."""
    from audit_review import views

    account_id = agent.pk

    def new_read(ref):
        return AccessRecord.objects.create(
            account_id=account_id,
            capacity="laboratory",
            kind="ticket",
            ticket_id=7,
            request_ref=ref,
        )

    def write_in_other_connection():
        try:
            assert connection.get_autocommit()
            new_read("second-read")
        finally:
            connections.close_all()

    new_read("first-read")
    client.force_login(reviewer)
    with ThreadPoolExecutor(max_workers=1) as writer, monkeypatch.context() as patch:
        marker = views.newest  # adapted: the reading is held by the newest id, read in it

        def marker_after_commit(source):
            writer.submit(write_in_other_connection).result(timeout=10)
            return marker(source)

        patch.setattr(views, "newest", marker_after_commit)
        page = client.get(reverse("audit_review:access"))
    assert page.status_code == 200
    assert page.context["total"] == 1
    shown = [row.cells[-1].text for row in page.context["page_obj"]]
    assert shown == ["first-read"]
    exported = client.get(page.context["export_url"])
    assert exported.status_code == 200
    rows = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
    assert [row["Request ID"] for row in rows] == shown


@pytest.mark.parametrize("fail_second_write", [False, True])
def test_mixed_history_response_inserts_once_per_table_or_rolls_back(
    rf, reviewer, fail_second_write
):

    def records(request):
        for ticket_id in (1, 2):
            access.note(request, "ticket", ticket_id=ticket_id)
            access.note(request, "ticket", ticket_id=ticket_id)
            access.keep(
                request,
                WorkspaceUse(
                    account_id=reviewer.pk,
                    source="access",
                    kind="invalid" if fail_second_write else "viewed",
                    rows=2,
                    request_ref=request.request_id,
                ),
            )
        return HttpResponse("Synthetic page")

    request = rf.get("/")
    request.user = reviewer
    request.request_id = "mixed-read"
    binder = access.AccessBindMiddleware(records)
    binder.process_view(request, records, (), {})
    middleware = access.AccessRecordMiddleware(binder)
    with CaptureQueriesContext(connection) as queries:
        if fail_second_write:
            with pytest.raises(IntegrityError):
                middleware(request)
        else:
            assert middleware(request).status_code == 200
    inserts = [item["sql"] for item in queries if item["sql"].startswith("INSERT")]
    assert len(inserts) == 2
    assert sum('"core_accessrecord"' in sql for sql in inserts) == 1
    assert sum('"audit_review_workspaceuse"' in sql for sql in inserts) == 1
    for model in (AccessRecord, WorkspaceUse):
        assert model.objects.filter(account=reviewer, request_ref="mixed-read").count() == (
            0 if fail_second_write else 2
        )


@pytest.mark.parametrize("outcome", ["replaced", "server-error", "raised", "head"])
def test_middleware_discards_a_use_if_the_answer_is_lost(rf, reviewer, outcome):

    def view(request):
        access.keep(request, WorkspaceUse(account=reviewer, source="access", kind="viewed", rows=0))
        return HttpResponse("Synthetic page")

    binder = access.AccessBindMiddleware(view)

    def inner(request):
        if outcome == "raised":
            view(request)
            binder.process_exception(request, RuntimeError("synthetic render failure"))
        response = binder(request) if outcome != "raised" else HttpResponse(status=500)
        if outcome == "replaced":
            return HttpResponse(status=302)
        if outcome == "server-error":
            response.status_code = 503
        return response

    request = rf.head("/") if outcome == "head" else rf.get("/")
    request.user = reviewer
    binder.process_view(request, view, (), {})
    access.AccessRecordMiddleware(inner)(request)
    assert not WorkspaceUse.objects.exists()


@pytest.mark.parametrize(
    ("route", "source"),
    [("changes", "changes"), ("access", "access"), ("sign-ins", "sign_ins"), ("uses", "uses")],
)
def test_each_filtered_answer_records_its_own_count_account_and_request_id(
    client, reviewer, agent, clinic, make_ticket, route, source
):

    ticket = make_ticket(clinic)
    client.force_login(agent)
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 200
    client.force_login(reviewer)
    # Establish a use for this person before opening the self-referential list.
    assert client.get(reverse("audit_review:access")).status_code == 200
    person = reviewer if route in ("uses", "sign-ins") else agent
    filters = {"person": str(person.pk)}
    previous = set(WorkspaceUse.objects.values_list("pk", flat=True))
    page = client.get(reverse(f"audit_review:{route}"), filters)
    assert page.status_code == 200 and page.context["total"] > 0
    response = client.get(page.context["export_url"])
    assert response.status_code == 200
    assert "no-store" in page["Cache-Control"] and "no-store" in response["Cache-Control"]
    uses = list(WorkspaceUse.objects.exclude(pk__in=previous).order_by("pk"))
    assert len(uses) == 2
    for answer, use, kind in ((page, uses[0], "viewed"), (response, uses[1], "exported")):
        assert (use.account_id, use.kind, use.source) == (reviewer.pk, kind, source)
        assert use.request_ref == answer["X-Request-ID"]
        assert use.rows == page.context["total"]
        assert use.filters == (dict(filters, page="1") if kind == "viewed" else filters)
    assert uses[0].request_ref != uses[1].request_ref


def test_workspace_use_pager_follows_rendered_link_without_repeating_its_own_visits(
    client, reviewer
):
    from html.parser import HTMLParser

    class PageLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hrefs = []

        def handle_starttag(self, tag, attrs):
            href = dict(attrs).get("href", "")
            if tag == "a" and "page=2" in href:
                self.hrefs.append(href)

    WorkspaceUse.objects.bulk_create(
        [
            WorkspaceUse(
                account=reviewer, source="changes", kind="viewed", rows=n, request_ref=f"old-{n}"
            )
            for n in range(52)
        ]
    )
    client.force_login(reviewer)
    first = client.get(reverse("audit_review:uses"))
    assert first.status_code == 200 and first.context["total"] == 52
    initial = [row.cells[-1].text for row in first.context["page_obj"]]
    parser = PageLinks()
    parser.feed(first.content.decode())
    assert parser.hrefs
    second = client.get(urljoin(first.wsgi_request.path, parser.hrefs[0]))
    assert second.status_code == 200 and second.context["total"] == 52
    remaining = [row.cells[-1].text for row in second.context["page_obj"]]
    assert initial + remaining == [f"old-{n}" for n in reversed(range(52))]
    assert WorkspaceUse.objects.count() == 54
    assert WorkspaceUse.objects.get(request_ref=second["X-Request-ID"]).filters == {"page": "2"}


# Access and navigation


def _review_grant():
    return Permission.objects.get(
        content_type__app_label="accounts", codename="review_audit_records"
    )


def _review_addresses():
    for name in ("changes", "access", "sign-ins", "uses"):
        yield reverse(f"audit_review:{name}")
        yield reverse(f"audit_review:{name}-export")
    yield reverse("lookup", args=["review-accounts"])


@pytest.mark.parametrize(
    "audience",
    [
        "anonymous",
        "technical",
        "account_manager",
        "client_services",
        "supervisor",
        "admin",
        "superuser-no-role",
        "superuser-with-role",
        "granted-no-role",
        "portal",
        "deactivated",
        "group-named-like-grant",
    ],
)
def test_ordinary_addresses_require_a_current_role_and_grant(client, make_user, clinic, audience):

    expected = 403
    if audience == "anonymous":
        expected = 302
    else:
        roles = {
            "superuser-no-role": (),
            "superuser-with-role": ("admin",),
            "granted-no-role": (),
            "portal": ("admin",),
            "deactivated": ("supervisor",),
            "group-named-like-grant": ("technical", "review_audit_records"),
        }
        user = make_user(
            "access-contract",
            *roles.get(audience, (audience,)),
            client=clinic if audience == "portal" else None,
            is_staff=audience.startswith("superuser"),
            is_superuser=audience.startswith("superuser"),
        )
        if audience in {"portal", "deactivated", "granted-no-role"}:
            user.user_permissions.add(_review_grant())
        client.force_login(user)
        if audience == "deactivated":
            user.is_active = False
            user.save(update_fields=["is_active"])
            expected = 302
    for address in _review_addresses():
        response = client.get(address)
        assert response.status_code == expected, (audience, address)
        if expected == 302:
            assert reverse("account_login") in response.url
        assert "text/csv" not in response["Content-Type"]
    assert not WorkspaceUse.objects.exists()


@pytest.mark.parametrize("removed", ["role", "grant"])
def test_saved_exports_and_person_lookup_follow_revocation_in_both_browsers(reviewer, removed):
    from core.models import AccessRecord

    AccessRecord.objects.create(
        account_id=reviewer.pk, ticket_id=1234, kind="ticket", capacity="laboratory"
    )
    browsers = [BrowserClient(), BrowserClient()]
    exports = []
    for browser in browsers:
        browser.force_login(reviewer)
        response = browser.get(reverse("audit_review:access"))
        assert response.status_code == 200
        exports.append(response.context["export_url"])
    if removed == "grant":
        reviewer.user_permissions.remove(_review_grant())
    else:
        reviewer.groups.remove(Group.objects.get(name="supervisor"))
    for browser, export in zip(browsers, exports, strict=True):
        assert browser.get(export).status_code == 403
        assert browser.get(reverse("lookup", args=["review-accounts"])).status_code == 403
    assert list(WorkspaceUse.objects.values_list("kind", flat=True)) == ["viewed", "viewed"]


def test_group_grant_opens_all_addresses_and_counts_one_holder(client, make_user, clinic):
    from core.security_settings import review_capability_holders

    lab_group = Group.objects.get_or_create(name="technical")[0]
    lab_group.permissions.add(_review_grant())
    active = make_user("case-technician", "technical")
    active.user_permissions.add(_review_grant())
    make_user("case-client", "technical", client=clinic)
    make_user("case-inactive", "technical", is_active=False)
    roleless = make_user("case-roleless")
    roleless.user_permissions.add(_review_grant())
    inherited = make_user("case-inherited", "technical")
    assert review_capability_holders() == 2
    client.force_login(inherited)
    for address in _review_addresses():
        response = client.get(address)
        assert response.status_code == 200, address
        if address == reverse("lookup", args=["review-accounts"]):
            labels = [row["text"] for row in response.json()["results"]]
            assert "case-inactive · inactive" in labels
            assert f"case-client · {clinic.name}" in labels


@pytest.mark.parametrize("route", ["dashboard", "lab-profile", "security-settings", "whats-new"])
def test_oversight_and_whats_new_follow_grant_on_normal_and_snapshot_pages(
    client, make_user, route
):
    user = make_user("menu-admin", "admin", "review_audit_records")
    client.force_login(user)
    grant = _review_grant()
    url = reverse("audit_review:changes")
    for granted in (False, True, False):
        if granted:
            user.user_permissions.add(grant)
        else:
            user.user_permissions.remove(grant)
        response = client.get(reverse(route))
        assert response.status_code == 200
        body = response.content.decode()
        nav = body.split('<nav class="vr-sidenav', 1)[1].split("</nav>", 1)[0]
        assert ('id="vr-nav-oversight"' in nav) == granted
        assert (f'href="{url}"' in nav) == granted
        if route == "whats-new":
            assert (f'<a href="{url}">Go to Audit review</a>' in body) == granted


def _can_review_another_sign_in(client, staff):
    """Compare the release's actual review page with the assurances its UI makes."""
    reviewer = staff("history-reader", "admin")
    client.force_login(reviewer)
    try:
        address = reverse("audit_review:sign-ins")
    except NoReverseMatch:
        return False
    subject = staff("history-subject", "technical")
    SignInEvent.objects.create(user=subject, kind="failed", factor="password")
    reviewer.user_permissions.add(_review_grant())
    response = client.get(address, {"person": subject.pk, "action": "failed"})
    assert response.status_code == 200
    return [r.username for r in response.context["page_obj"]] == [subject.username]


@pytest.mark.parametrize("route", ["account-sign-ins", "security-settings"])
def test_personal_sign_in_visibility_claim_agrees_with_reviewer_page(client, staff, route):
    review_available = _can_review_another_sign_in(client, staff)
    response = client.get(reverse(route))
    assert response.status_code == 200
    body = html.unescape(response.content.decode())
    obsolete_assurances = (
        "Only you see your own history",
        "Nobody else's history is shown to anyone in this release",
        "no page shows another person's",
    )
    assert not (review_available and any(assurance in body for assurance in obsolete_assurances)), (
        route,
        [assurance for assurance in obsolete_assurances if assurance in body],
    )


def test_security_settings_help_explains_its_displayed_review_holder_count(client, staff):
    client.force_login(staff("settings-reader", "admin"))
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    facts = [fact for group in response.context["groups"] for fact in group.facts]
    help_text = json.dumps(response.context["page_documentation"]).casefold()
    for fact in facts:
        if fact.key == "review-capability":
            assert (
                "audit" in help_text
                and "review" in help_text
                and ("review access and audit records" in help_text or "audit review" in help_text)
            ), f"The Help reference omits the displayed fact {fact.label!r}."


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("export", [False, True])
def test_authorized_change_row_and_access_check_share_a_snapshot(
    client, reviewer, monkeypatch, export
):
    from audit_review import views

    client.force_login(reviewer)
    address = reverse("audit_review:changes-export" if export else "audit_review:changes")
    token = "review-read-consistency"
    original_check = views.require_reviewer

    def commit_new_event():
        try:
            AuditEvent.objects.create(
                actor_id=reviewer.pk,
                action="account.updated",
                object_type="accounts.user",
                object_id=str(reviewer.pk),
                request_ref=token,
            )
        finally:
            connections.close_all()

    def checked_before_concurrent_event(user):
        original_check(user)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(commit_new_event).result(timeout=10)

    with monkeypatch.context() as patch:
        patch.setattr(views, "require_reviewer", checked_before_concurrent_event)
        first = client.get(address)
    assert first.status_code == 200 and token.encode() not in first.content
    second = client.get(address)
    assert second.status_code == 200 and token.encode() in second.content


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("lookup_name", ["review-accounts", "staff"])
def test_person_options_and_more_flag_describe_the_same_directory_page(
    client, staff, monkeypatch, lookup_name
):
    from core.lookups import LOOKUPS, Paginator

    if lookup_name not in LOOKUPS:
        pytest.skip("This release has no review account lookup")
    user = staff("directory-reader", "technical")
    if lookup_name == "review-accounts":
        user.user_permissions.add(_review_grant())
    client.force_login(user)

    def option(index):
        name = f"option-review-{index:02d}"
        return User(username=name, email=f"{name}@example.invalid")

    initial = User.objects.bulk_create([option(i) for i in range(1, 21)])
    initial_ids = [str(row.pk) for row in initial]

    def publish_first_choice():
        try:
            new = option(0)
            new.save()
            return str(new.pk)
        finally:
            connections.close_all()

    original_page = Paginator.page
    added = []

    def page_during_directory_change(paginator, number):
        page = original_page(paginator, number)
        with ThreadPoolExecutor(max_workers=1) as executor:
            added.append(executor.submit(publish_first_choice).result(timeout=10))
        return page

    with monkeypatch.context() as patch:
        patch.setattr(Paginator, "page", page_during_directory_change)
        result = client.get(reverse("lookup", args=[lookup_name]), {"q": "option-review"}).json()
    [new_id] = added
    ids = [row["value"] for row in result["results"]]
    assert (ids, result["more"]) in [(initial_ids, False), ([new_id, *initial_ids[:-1]], True)], (
        "The first page includes the newly added option but says there is no second page."
    )


def test_a_custom_permission_group_can_supply_the_grant_to_a_role_holder(client, make_user):
    reader = make_user("quality-reader", "technical")
    custom = Group.objects.create(name="Quality review committee")
    custom.permissions.add(_review_grant())
    reader.groups.add(custom)
    client.force_login(reader)
    response = client.get(reverse("audit_review:changes"))
    assert response.status_code == 200
    assert 'id="vr-nav-oversight"' in response.content.decode()


# Internal tickets


@pytest.mark.parametrize("name", ["changes", "changes-export", "access", "access-export"])
def test_review_lists_internal_ticket_with_no_organization(client, reviewer, make_ticket, name):
    """Staff can log an internal problem without naming a client organization."""

    ticket = make_ticket(None, subject="Synthetic internal problem")
    client.force_login(reviewer)
    # Opening the real ticket records a sensitive read for the Access records list.
    assert client.get(reverse("desk:request-detail", args=[ticket.pk])).status_code == 200
    client.raise_request_exception = False
    response = client.get(reverse(f"audit_review:{name}"))
    assert response.status_code == 200
    assert ticket.number.encode() in response.content


# Filters, exports and pagination


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit: the organization filter reads which accounts belong to the "
    "organization at each page and export (docs/architecture.md, Audit review workspace).",
)
@pytest.mark.parametrize("name", ["sign-ins", "changes"])
def test_organization_export_keeps_the_accounts_records_reviewed_before_a_move(
    client, reviewer, make_user, clinic, water, name
):
    from desk.tests.test_account_move import admin_change

    portal = make_user("portal-transfer", client=clinic)
    admin = make_user("move-admin", "admin", is_superuser=True, is_staff=True)
    administration = BrowserClient()
    administration.force_login(admin)
    client.force_login(reviewer)
    if name == "sign-ins":
        action = SignInEvent.Kind.FAILED
        SignInEvent.objects.create(user=portal, kind=action, factor="password")
    else:
        action = "account.updated"
        AuditEvent.objects.create(
            actor=admin,
            action=action,
            object_type=portal._meta.label_lower,
            object_id=str(portal.pk),
        )

    address = reverse(f"audit_review:{name}")
    query = {"organization": clinic.pk, "action": action}
    page = client.get(address, query)
    assert page.context["total"] == 1
    export_url = page.context["export_url"]

    assert admin_change(administration, portal, client=water.pk).status_code == 302
    portal.refresh_from_db()
    assert portal.client_id == water.pk
    assert client.get(address, query).context["total"] == 0
    rows = _review_csv(client.get(export_url))
    assert len(rows) == 1


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit: Changes are held by the application's clock, and a change saved by "
    "a process whose clock runs behind can appear in later pages (docs/architecture.md).",
)
def test_changes_export_uses_write_order_when_the_writer_clock_is_behind(
    client, reviewer, monkeypatch
):
    def record(reference):
        return AuditEvent.objects.create(
            actor=reviewer,
            action="account.updated",
            object_type=reviewer._meta.label_lower,
            object_id=str(reviewer.pk),
            request_ref=reference,
        )

    record("original-change")
    client.force_login(reviewer)
    first = client.get(reverse("audit_review:changes"), {"action": "account.updated"})
    assert first.context["total"] == 1
    cutoff = sources.from_marker(int(first.context["request"].GET["upto"]))
    with monkeypatch.context() as writer_clock:
        writer_clock.setattr(timezone, "now", lambda: cutoff - timedelta(seconds=1))
        later = record("later-change")
    assert later.occurred_at < cutoff
    assert (
        client.get(reverse("audit_review:changes"), {"action": "account.updated"}).context["total"]
        == 2
    )
    exported = _review_csv(client.get(first.context["export_url"]))
    assert [row["Request ID"] for row in exported] == ["original-change"]


@pytest.mark.parametrize(
    ("field", "day", "zone"),
    [("date_to", date.max, "America/New_York"), ("date_from", date.min, "Etc/GMT-14")],
)
def test_valid_calendar_range_bound_includes_an_ordinary_record(reviewer, field, day, zone):
    use = WorkspaceUse.objects.create(
        account=reviewer, kind=UseKind.VIEWED, source=Source.ACCESS, rows=0
    )
    with timezone.override(zone):
        form = ReviewFilter(Source.USES, {field: day.isoformat()})
        assert form.is_valid(), form.errors
        matching = sources.rows_for(Source.USES, form.cleaned_data, sources.newest(Source.USES))
        assert list(matching.values_list("pk", flat=True)) == [use.pk]


@pytest.mark.parametrize(
    ("day", "start", "duration_hours"),
    [("2025-03-09", "2025-03-09T05:00:00", 23), ("2025-11-02", "2025-11-02T04:00:00", 25)],
)
def test_date_range_matches_both_boundaries_of_the_local_day_across_dst(
    reviewer, client, day, start, duration_hours
):
    midnight = datetime.fromisoformat(start).replace(tzinfo=UTC)
    next_day = midnight + timedelta(hours=duration_hours)
    before = timedelta(microseconds=1)
    records = {
        "previous-day": midnight - before,
        "first": midnight,
        "last": next_day - before,
        "next-day": next_day,
    }
    WorkspaceUse.objects.bulk_create(
        [
            WorkspaceUse(
                occurred_at=moment,
                account=reviewer,
                source=Source.ACCESS,
                kind=UseKind.EXPORTED,
                rows=0,
                request_ref=label,
            )
            for label, moment in records.items()
        ]
    )
    client.force_login(reviewer)
    query = {"date_from": day, "date_to": day, "action": UseKind.EXPORTED}
    with timezone.override("America/New_York"):
        page = client.get(reverse("audit_review:uses"), query)
        assert [row.cells[-1].text for row in page.context["page_obj"]] == ["last", "first"]
        rows = _review_csv(client.get(page.context["export_url"]))
        assert [row["Request ID"] for row in rows] == ["last", "first"]
        assert {row["When"][:10] for row in rows} == {day}
        assert len({row["When"][-6:] for row in rows}) == 2


def test_unrecognised_ticket_numbers_are_rejected_and_a_valid_one_is_recorded_by_id(
    client, reviewer, clinic, make_ticket
):
    ticket = make_ticket(clinic)
    url = reverse("audit_review:changes")
    client.force_login(reviewer)
    for value in ("NC-000001", "REQ-²", "REQ-" + "8" * 40, "REQ--1", "REQ-０", "0"):
        page = client.get(url, {"ticket": value})
        use = WorkspaceUse.objects.get(request_ref=page["X-Request-ID"])
        assert page.context["filter_form"].has_error("ticket"), value
        assert (use.filters, use.rows) == ({"invalid": True}, 0)
    page = client.get(url, {"ticket": f"   {ticket.number.lower()}  "})
    assert page.context["total"] > 0
    assert WorkspaceUse.objects.get(request_ref=page["X-Request-ID"]).filters == {
        "ticket": str(ticket.pk),
        "page": "1",
    }


def test_upto_ignores_unrecognised_digits_and_limits_future_dates(client, reviewer):
    WorkspaceUse.objects.create(
        account=reviewer, source=Source.ACCESS, kind=UseKind.EXPORTED, rows=0
    )
    client.force_login(reviewer)
    address = reverse("audit_review:uses")
    for value in ("-9", "٣", "0" * 40, "9" * 19):
        newest = sources.newest(Source.USES)  # adapted: the reading is held by the newest id
        page = client.get(address, {"upto": value, "action": UseKind.EXPORTED})
        assert page.context["total"] == 1
        assert int(page.context["request"].GET["upto"]) == newest
    assert client.get(address, {"upto": "0"}).context["total"] == 0


def test_the_export_offered_at_ten_thousand_survives_one_new_matching_record(
    reviewer, client, agent
):
    def recorded(n):
        return WorkspaceUse(
            account=agent, kind=UseKind.EXPORTED, source=Source.ACCESS, request_ref=str(n), rows=0
        )

    WorkspaceUse.objects.bulk_create([recorded(n) for n in range(10_000)], batch_size=500)
    client.force_login(reviewer)
    url = reverse("audit_review:uses")
    query = {"person": agent.pk, "action": UseKind.EXPORTED}
    page = client.get(url, query)
    assert page.context["total"] == 10_000
    assert page.context["exportable"] and b"Export CSV" in page.content
    recorded(10_000).save()
    rows = _review_csv(client.get(page.context["export_url"]))
    assert [row["Request ID"] for row in rows] == [str(n) for n in reversed(range(10_000))]
    newer = client.get(url, query)
    assert newer.context["total"] == 10_001
    assert not newer.context["exportable"] and b">Export CSV</a>" not in newer.content
    previous = WorkspaceUse.objects.count()
    assert client.get(newer.context["export_url"]).status_code == 302
    assert WorkspaceUse.objects.count() == previous


@pytest.mark.django_db(transaction=True)
def test_a_page_keeps_its_count_and_rows_from_one_reading(client, reviewer):
    first_use = WorkspaceUse.objects.create(
        account=reviewer, kind=UseKind.EXPORTED, source=Source.ACCESS, rows=0, request_ref="first"
    )
    after_count = Event()
    committed = Event()

    def insert_between_count_and_rows():
        close_old_connections()
        try:
            assert after_count.wait(10)
            # Giving both records the same event time isolates database snapshot consistency
            # from the independent cutoff comparison, and also exercises the pk tie-break.
            WorkspaceUse.objects.create(
                occurred_at=first_use.occurred_at,
                account_id=reviewer.pk,
                kind=UseKind.EXPORTED,
                source=Source.ACCESS,
                rows=0,
                request_ref="second",
            )
        finally:
            connections.close_all()
            committed.set()

    def wait_after_count(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if "COUNT(*)" in sql and '"audit_review_workspaceuse"' in sql:
            after_count.set()
            assert committed.wait(10)
        return result

    client.force_login(reviewer)
    address = reverse("audit_review:uses")
    query = {"action": UseKind.EXPORTED}
    with ThreadPoolExecutor(max_workers=1) as threads:
        writer = threads.submit(insert_between_count_and_rows)
        with connections["default"].execute_wrapper(wait_after_count):
            page = client.get(address, query)
        writer.result(timeout=10)
    assert page.context["total"] == 1
    assert [row.cells[-1].text for row in page.context["page_obj"]] == ["first"]
    fresh = client.get(address, query)
    assert [row.cells[-1].text for row in fresh.context["page_obj"]] == ["second", "first"]


@pytest.mark.parametrize("field", ["person", "organization"])
def test_the_rejected_lookup_option_still_renders_its_filter_control(db, field):
    form = ReviewFilter(Source.ACCESS, {field: "unselected"})
    assert form.has_error(field, "invalid_choice")
    html = form[field].as_widget()
    assert "<select" in html and 'aria-invalid="true"' in html


def test_rejected_option_still_renders_in_an_existing_organization_control(db):
    from clients.forms import ClientForm

    form = ClientForm(data={"parent": "unselected"})
    assert form.has_error("parent", "invalid_choice")
    html = form["parent"].as_widget()
    assert "<select" in html and 'aria-invalid="true"' in html


def test_ticket_and_organization_filter_select_every_documented_part_and_only_its_owner(
    client, reviewer, agent, category, make_ticket, make_user, clinic, water
):
    from attachments.models import Attachment
    from clients.models import ClientContact, ClientSite, PortalAccess
    from desk.models import ComplaintReview
    from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
    from request_forms.models import FilledForm, RequestTemplate

    system = ExternalSystem.objects.create(key="operations", name="Operations")
    template = RequestTemplate.objects.create(key="courier", name="Courier", category=category)
    link = ExternalRecordLink.objects.create(
        system=system,
        record_type="shipment",
        identifier="box",
        display_label="Box",
        created_by=agent,
    )
    client.force_login(reviewer)

    def refs(organization, objects):
        result = set()
        for obj in objects:
            label = obj._meta.model_name
            reference = f"{organization.pk}-{label}-{obj.pk}"
            AuditEvent.objects.create(
                actor=agent,
                action="review.fixture",
                object_type=obj._meta.label_lower,
                object_id=str(obj.pk),
                request_ref=reference,
            )
            result.add(reference)
        return result

    cases = []
    for organization in (clinic, water):
        ticket = make_ticket(organization)
        entry = ticket.entries.get(kind="note")
        portal = make_user(f"reader-{organization.pk}", client=organization)
        ticket_objects = [
            ticket,
            entry,
            ComplaintReview.objects.create(request=ticket),
            Attachment.objects.create(
                request=ticket,
                entry=entry,
                uploaded_by=agent,
                original_name="contents.txt",
                extension="txt",
                content_type="text/plain",
                size=1,
                sha256="0" * 64,
            ),
            LinkAttachment.objects.create(link=link, request=ticket, added_by=agent),
            FilledForm.objects.create(
                request=ticket,
                template=template,
                template_key=template.key,
                template_name=template.name,
                created_by=agent,
            ),
        ]
        directory_objects = [
            organization,
            portal,
            ClientContact.objects.create(client=organization, name="Dispatch", phone="100"),
            ClientSite.objects.create(client=organization, name="Office"),
            PortalAccess.objects.create(client=organization, user=portal),
            LinkAttachment.objects.create(link=link, client=organization, added_by=agent),
        ]

        ticket_refs = refs(organization, ticket_objects)
        org_refs = ticket_refs | refs(organization, directory_objects)
        cases.extend(
            [
                ({"ticket": ticket.number}, ticket_refs),
                ({"organization": organization.pk}, org_refs),
            ]
        )
    for filters, expected in cases:
        page = client.get(reverse("audit_review:changes"), {**filters, "action": "review.fixture"})
        assert {row.cells[-1].text for row in page.context["page_obj"]} == expected
        assert {
            row["Request ID"] for row in _review_csv(client.get(page.context["export_url"]))
        } == expected


# Row content and CSV format


@pytest.mark.django_db(transaction=True)
def test_sign_ins_export_link_keeps_its_rows_after_an_abandoned_session_is_closed(
    client, reviewer, agent
):
    from datetime import timedelta

    from django.utils import timezone

    from accounts.models import AccountSession, SignInEvent
    from accounts.sessions import close_expired

    old = timezone.now() - timedelta(days=7)
    AccountSession.objects.create(user=agent, started_at=old, last_active_at=old)
    client.force_login(reviewer)
    page = client.get(reverse("audit_review:sign-ins"), {"person": agent.pk})
    assert page.status_code == 200 and page.context["total"] == 1
    export = page.context["export_url"]
    before = _review_csv(client.get(export))
    assert [row["Event"] for row in before] == ["Signed in"]

    # No write was in progress when the reviewer opened the first page.
    assert close_expired(agent) == 1
    assert SignInEvent.objects.filter(user=agent, kind="signed_out").exists()
    assert _review_csv(client.get(export)) == before


def test_unknown_account_device_columns_do_not_consult_its_sign_in_metadata(monkeypatch):
    from accounts.models import SignInEvent
    from audit_review import sources

    # A sign-in outlives the account even when its shared-account status cannot be recovered.
    # Empty device fields suffice to check whether the presentation tries to format either one.
    attempted = []

    def device_formatter(value):
        attempted.append(value)
        return "Not recorded"

    monkeypatch.setattr(sources, "browser_label", device_formatter)
    monkeypatch.setattr(sources.signin_history, "address_text", device_formatter)
    event = SignInEvent(user_id=123, kind="signed_in", user_agent="", address=None)
    [cells] = sources._sign_ins([event], {})
    assert attempted == [], "Sharing status is unavailable for this retained entry"
    assert all(cell.text for cell in cells[1:3])


def test_csv_prefixes_each_formula_marker_already_recognized_by_the_directory_importer():
    from audit_review.views import _cell
    from dataimport.importing import FORMULA_STARTS, formula_warning

    # Inert punctuation only. No expression is supplied or evaluated.
    examples = (*FORMULA_STARTS, "  +")
    recognized = [mark for mark in examples if formula_warning("Name", mark)]
    missing = [mark for mark in recognized if _cell(mark) != "'" + mark]
    assert missing == []


def test_workspace_use_organization_filter_matches_the_product_description(client, reviewer):
    from pathlib import Path

    from django.conf import settings

    client.force_login(reviewer)
    page = client.get(reverse("audit_review:uses"))
    assert page.status_code == 200
    product = " ".join((Path(settings.BASE_DIR) / "docs/product.md").read_text().split())
    promises_filter = "Each is filtered by person (any account), client organization" in product
    assert not promises_filter or "organization" in page.context["filter_form"].fields


def test_a_clock_breach_row_names_the_owning_organization_and_is_found_by_ticket(
    client, reviewer, agent, clinic, make_ticket
):

    from core.audit import record_event
    from service_levels.models import RequestClock

    ticket = make_ticket(clinic)
    clock, _ = RequestClock.objects.get_or_create(request=ticket)
    record_event(
        actor=agent, action="clock.breach_noticed", obj=clock, after={"request_id": ticket.pk}
    )
    client.force_login(reviewer)
    address = reverse("audit_review:changes")
    filtered = {"action": "clock.breach_noticed"}
    page = client.get(address, filtered)
    assert page.status_code == 200 and page.context["total"] == 1
    [event] = page.context["page_obj"]
    ticket_page = client.get(address, {**filtered, "ticket": ticket.number})
    org_page = client.get(address, {**filtered, "organization": clinic.pk})
    assert (event.cells[1].note, ticket_page.context["total"], org_page.context["total"]) == (
        clinic.name,
        1,
        1,
    )


def test_restricted_ticket_history_uses_identifiers_for_its_parts_on_page_and_in_csv(
    client, reviewer, agent, clinic, category, make_ticket
):
    import re
    from html import unescape

    from django.utils import timezone

    from attachments.models import Attachment
    from core.audit import record_event
    from core.models import ACCESS_OBJECTS, AccessKind, AccessRecord
    from desk.models import ComplaintReview
    from links.models import ExternalRecordLink, ExternalSystem, LinkAttachment
    from request_forms.models import FieldValue, FilledForm, RequestTemplate, TemplateField

    ticket = make_ticket(clinic, "Synthetic private subject")
    category.is_restricted = True
    category.save(update_fields=["is_restricted"])
    entry = ticket.entries.get(body="Synthetic first note.")
    complaint = ComplaintReview.objects.create(
        request=ticket, findings="Synthetic private findings"
    )
    attachment = Attachment.objects.create(
        request=ticket,
        entry=entry,
        original_name="synthetic-original.txt",
        extension="txt",
        content_type="text/plain",
        size=1,
        sha256="0" * 64,
        uploaded_by=agent,
        withdrawn_at=timezone.now(),
        withdrawn_by=reviewer,
        withdrawal_reason="synthetic-file-reason",
    )
    template = RequestTemplate.objects.create(
        key="review", name="Synthetic form", category=category
    )
    filled = FilledForm.objects.create(
        request=ticket, template=template, template_name=template.name, created_by=agent
    )
    field = TemplateField.objects.create(
        template=template, key="code", kind="short_text", label="Panel label"
    )
    field_data = dict(
        filled_form=filled,
        field=field,
        key=field.key,
        kind=field.kind,
        audience="staff",
        label="Panel label",
        holds_patient_information=True,
        position=0,
        entered_by=agent,
    )
    earlier = FieldValue.objects.create(
        **field_data, value="synthetic-earlier", superseded_at=timezone.now()
    )
    FieldValue.objects.create(
        **field_data, value="synthetic-current", replaces=earlier, reason="synthetic-correction"
    )
    system = ExternalSystem.objects.create(key="synthetic", name="Synthetic system")
    link = ExternalRecordLink.objects.create(
        system=system,
        record_type="patient",
        identifier="synthetic-person-key",
        display_label="synthetic-masked-label",
        is_person=True,
        created_by=agent,
    )
    attached = LinkAttachment.objects.create(
        request=ticket, link=link, is_person=True, added_by=agent
    )
    parts = [ticket, entry, complaint, attachment, filled, attached]
    for part in parts:
        record_event(
            actor=agent,
            action="review.fixture",
            obj=part,
            before={"value": "synthetic-before"},
            after={"value": "synthetic-after"},
            reason="synthetic-reason",
        )
    details = {
        AccessKind.FILE: attachment,
        AccessKind.PATIENT_ANSWER: earlier,
        AccessKind.PERSON_KEY: attached,
    }
    for kind in AccessKind:
        obj = details.get(kind)
        AccessRecord.objects.create(
            account=agent,
            capacity="laboratory",
            ticket_id=ticket.pk,
            kind=kind,
            object_type=ACCESS_OBJECTS.get(kind, ""),
            object_id=str(obj.pk) if obj else "",
        )

    clinic.name = '+Clinique, "Érable"'
    clinic.save(update_fields=["name"])
    agent.first_name = "Émile"
    agent.last_name = "Révision"
    agent.save(update_fields=["first_name", "last_name"])
    client.force_login(reviewer)
    hidden = [
        ticket.subject,
        entry.body,
        complaint.findings,
        attachment.original_name,
        attachment.withdrawal_reason,
        "synthetic-before",
        "synthetic-after",
        "synthetic-reason",
        "synthetic-earlier",
        "synthetic-current",
        "synthetic-correction",
        link.identifier,
        link.display_label,
    ]
    for tab, filters, count in [
        ("changes", {"action": "review.fixture"}, len(parts)),
        ("access", {}, len(AccessKind)),
    ]:
        page = client.get(reverse(f"audit_review:{tab}"), {**filters, "ticket": ticket.number})
        assert page.status_code == 200 and page.context["total"] == count
        response = client.get(page.context["export_url"])
        assert re.fullmatch(
            rf'attachment; filename="audit-review-{tab}-\d{{8}}-\d{{6}}\.csv"',
            response["Content-Disposition"],
        )
        rows = _review_csv(response)
        assert len(rows) == count
        body = unescape(page.content.decode())
        assert ticket.number in body and clinic.name in body and agent.get_full_name() in body
        for row in rows:
            assert row["Person"] == agent.get_full_name() and row["Username"] == agent.username
            assert row["Client organization"] == "'" + clinic.name
            assert ticket.number in row.get("Record", row.get("Ticket", ""))
        for value in hidden:
            assert value not in body and value not in response.content.decode()
        if tab == "access":
            by_action = {row["What"]: row["Detail"] for row in rows}
            for kind, detail in [
                (AccessKind.FILE, attachment.download_name),
                (AccessKind.PATIENT_ANSWER, earlier.label),
                (AccessKind.PERSON_KEY, f"Link {attached.pk}"),
            ]:
                assert by_action[kind.label] == detail and detail in body


# Shared demonstration sign-ins


def test_every_shared_account_event_keeps_the_same_device_labels_on_page_and_in_export(
    client, reviewer, clinic, make_user
):

    from accounts.models import SignInEvent

    shared = make_user("shared-history", client=clinic, credentials_locked=True)
    kinds = list(SignInEvent.Kind)
    for kind in kinds:
        SignInEvent.objects.create(user=shared, kind=kind)
    client.force_login(reviewer)
    for chosen in [None, *kinds]:
        query = {"person": shared.pk, "organization": clinic.pk}
        if chosen is not None:
            query["action"] = chosen.value
        page = client.get(reverse("audit_review:sign-ins"), query)
        expected = {chosen.label} if chosen else {kind.label for kind in kinds}
        assert page.status_code == 200 and page.context["total"] == len(expected)
        assert page.content.decode().count("Not shown: shared demonstration account") >= 2 * len(
            expected
        )
        rows = _review_csv(client.get(page.context["export_url"]))
        assert {row["Event"] for row in rows} == expected
        assert all(
            row["Browser"] == row["Address"] == "Not shown: shared demonstration account"
            for row in rows
        )


# Upgrade and reset


@pytest.mark.django_db(transaction=True)
def test_reversing_workspace_migrations_removes_grants_and_preserves_prior_history():
    from django.contrib.auth.models import Group, Permission
    from django.core.management import call_command
    from django.db import connection

    from accounts.models import SignInEvent, User
    from core.models import AccessRecord, AuditEvent

    def migrate(*target):
        call_command("migrate", *target, verbosity=0, interactive=False)

    def review_relations():
        names = [
            "audit_event_recent",
            "access_record_recent",
            "sign_in_event_all_recent",
            "audit_review_workspaceuse",
        ]
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(n) FROM unnest(%s::text[]) AS n", [names])
            return [row[0] is not None for row in cursor]

    account = User.objects.create_user("review-migration-fixture")
    group = Group.objects.create(name="Review migration fixture")
    changes = AuditEvent.objects.create(
        actor=account, object_type="accounts.user", object_id=str(account.pk), action="test.saved"
    )
    read = AccessRecord.objects.create(
        account=account, capacity="laboratory", kind="ticket", ticket_id=17
    )
    sign_in = SignInEvent.objects.create(user=account, kind="failed", factor="password")
    permission = Permission.objects.get(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="review_audit_records",
    )
    account.user_permissions.add(permission)
    group.permissions.add(permission)
    WorkspaceUse.objects.create(account=account, kind="viewed", source="changes", rows=1)
    try:
        migrate("audit_review", "zero")
        migrate("core", "0009_access_records")
        migrate("accounts", "0009_sign_in_history")
        assert review_relations() == [False] * 4
        assert AuditEvent.objects.get(pk=changes.pk).action == "test.saved"
        assert AccessRecord.objects.get(pk=read.pk).ticket_id == 17
        assert SignInEvent.objects.get(pk=sign_in.pk).kind == "failed"
        retained_grants = (
            account.user_permissions.filter(pk=permission.pk).count(),
            group.permissions.filter(pk=permission.pk).count(),
        )
    finally:
        migrate()
    assert review_relations() == [True] * 4
    assert WorkspaceUse.objects.count() == 0
    assert retained_grants == (0, 0), "rollback retained direct and group review grants"


@pytest.mark.django_db
def test_demo_reset_truncates_workspace_use_and_announces_only_the_seeded_review_holder(
    monkeypatch,
):
    from django.contrib.auth.models import Group, Permission
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from accounts.models import User
    from audit_review.permissions import may_review
    from core.models import Announcement, AuditEvent

    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-shared-fixture")
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "Synthetic-private-fixture")
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    call_command("seed_demo", verbosity=0)
    supervisor = User.objects.get(username="demo_supervisor")
    WorkspaceUse.objects.create(account=supervisor, source="changes", kind="viewed", rows=0)
    permission = Permission.objects.get(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="review_audit_records",
    )
    Group.objects.get(name="technical").permissions.add(permission)
    with pytest.raises(CommandError, match="demo_technical"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.filter(title="Demo accounts").exists()
    call_command("reset_demo", interactive=False, announce_accounts=True, verbosity=0)
    assert not WorkspaceUse.objects.exists()
    holders = [user.username for user in User.objects.all() if may_review(user)]
    assert holders == ["demo_supervisor"]
    assert (
        AuditEvent.objects.filter(
            action="account.capability_granted",
            object_id=str(User.objects.get(username="demo_supervisor").pk),
            after={"capability": "review_audit_records"},
        ).count()
        == 1
    )
    notice = Announcement.objects.get(title="Demo accounts", show_on_sign_in=True)
    assert any(
        line.startswith("demo_supervisor:") and "audit review" in line
        for line in notice.body.splitlines()
    )


def test_every_workspace_list_offers_its_column_chooser(client, reviewer):
    """The list toolbar lets the reviewer choose the visible columns."""
    from html.parser import HTMLParser

    class ColumnButtons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.grids = set()

        def handle_starttag(self, tag, attrs):
            if tag == "button" and (grid := dict(attrs).get("data-vr-columns-for")):
                self.grids.add(grid)

    client.force_login(reviewer)
    missing = []
    for route in ("changes", "access", "sign-ins", "uses"):
        page = client.get(reverse(f"audit_review:{route}"))
        assert page.status_code == 200
        buttons = ColumnButtons()
        buttons.feed(page.content.decode())
        if not buttons.grids:
            missing.append(route)
    assert not missing, f"Lists with no Columns button: {missing}"


def test_workspace_lists_are_present_in_the_phone_sweep(settings):
    """Keep new list pages in the width and touch-target sweep required by UI standards."""
    import ast

    sweep = settings.BASE_DIR / "e2e/test_phone_layout.py"
    tree = ast.parse(sweep.read_text())
    lists = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(name, ast.Name) and name.id == "LISTS" for name in node.targets)
    )
    workspace = {
        reverse(f"audit_review:{route}") for route in ("changes", "access", "sign-ins", "uses")
    }
    assert workspace.issubset(lists), sorted(workspace.difference(lists))


@pytest.mark.django_db
def test_release_seed_can_export_its_complete_change_history(client, monkeypatch):
    from collections import Counter

    from django.core.management import call_command

    monkeypatch.setenv("DEMO_PASSWORD", "Synthetic-review-demo-fixture")
    call_command("seed_demo", verbosity=0)
    reviewer = User.objects.get(username="demo_supervisor")
    recorded = list(AuditEvent.objects.order_by("-occurred_at", "-pk"))
    assert len({event.object_type for event in recorded}) > 10
    # Compare a complete export to all the actual event types the demonstration produces.
    from core.templatetags.ui import action_label

    expected = Counter(action_label(event.action) for event in recorded)
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:changes-export"))
    assert Counter(row["Action"] for row in _review_csv(response)) == expected


def test_batch_file_descriptions_use_the_access_records_own_ticket(agent, clinic, make_ticket):
    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files
    from desk import opened
    from desk.models import EntryKind

    first, second = make_ticket(clinic), make_ticket(clinic)
    entry = add_entry_with_files(
        actor=agent,
        request=second,
        kind=EntryKind.NOTE,
        expected_revision=second.revision,
        body="Synthetic attachment",
        files=[inspect_bytes("synthetic.txt", b"Sample")],
    )
    file = entry.attachments.get()
    observations = [
        AccessRecord.objects.create(
            account=agent,
            capacity="laboratory",
            ticket_id=ticket.pk,
            kind="file",
            object_type="attachments.attachment",
            object_id=str(file.pk),
        )
        for ticket in (first, second)
    ]
    assert [row.detail for row in opened.describe(observations)] == [
        f"File {file.pk}",
        file.download_name,
    ]
    assert opened.page(first, 1).object_list[0].detail == f"File {file.pk}"
    assert opened.page(second, 1).object_list[0].detail == file.download_name


def test_changes_render_identifiers_for_each_release_model_with_a_retained_event(client, reviewer):
    """Exercise the generic renderer as well as known models synthesized as retained events."""
    from pathlib import Path
    from uuid import UUID

    from django.apps import apps
    from django.conf import settings
    from django.db.models import UUIDField

    action = "review.retained_event"
    markers = ("synthetic-before-review", "synthetic-after-review", "synthetic-reason-review")
    events = []
    for model in apps.get_models():
        app_directory = Path(model._meta.app_config.path)
        if app_directory.parent != settings.BASE_DIR:
            continue
        pk = str(UUID(int=99)) if isinstance(model._meta.pk, UUIDField) else "999999"
        events.append(
            AuditEvent(
                object_type=model._meta.label_lower,
                object_id=pk,
                actor=reviewer,
                action=action,
                before={"value": markers[0]},
                after={"value": markers[1]},
                reason=markers[2],
                request_ref=f"retained-{len(events)}",
            )
        )
    assert len(events) > 50
    AuditEvent.objects.bulk_create(events)
    client.force_login(reviewer)
    address = reverse("audit_review:changes")
    query = {"action": action}
    first = client.get(address, query)
    assert first.status_code == 200 and first.context["total"] == len(events)
    pages = [first]
    for page in range(2, first.context["page_obj"].paginator.num_pages + 1):
        pages.append(
            client.get(
                address, {**query, "page": page, "upto": first.context["request"].GET["upto"]}
            )
        )
    exported = client.get(first.context["export_url"])
    rows = _review_csv(exported)
    assert {row["Request ID"] for row in rows} == {event.request_ref for event in events}
    assert all(row["Record"] for row in rows)
    assert {row.cells[-1].text for page in pages for row in page.context["page_obj"]} == {
        event.request_ref for event in events
    }
    for answer in [*pages, exported]:
        assert all(marker.encode() not in answer.content for marker in markers)
