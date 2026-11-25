"""Confirmation of the queue fixes on synthetic Django/PostgreSQL and browser journeys.

Adopted from the reviewer's file (archived in the review history as
test_queue_way_back_round1_confirm_original.py). Five cases are adapted to the documented
behaviour of the confirmation's resolution (C5, C6, C7), each marked at its assertion. The phone
journey drives Chromium and is skipped where Playwright is not installed (the application job of
continuous integration); the restorable flag it relies on is covered by the request tests.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client as Browser
from django.urls import reverse

from core.models import AuditEvent
from desk import services, views
from desk.models import ComplaintDecision, SavedView, Status
from teams import services as team_services

from . import test_review_queue_way_back as round_one
from .test_contract import new_request
from .test_preferences_in_session import committed
from .test_review_queue_way_back import (
    ACTIONS,
    NEW,
    OLD,
    QUEUE,
    action_path,
    breadcrumb,
    bulk_url,
    controls,
    detail,
    fields,
    link,
    lookup_view,
    pref,
    query,
    row_ids,
    set_pref,
    settings_shown,
)

queue_action_ticket = round_one.action_ticket
pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "outcome,sort,chosen",
    [
        ("refused", None, {}),
        ("refused", "retired_column", {}),
        ("refused", "-subject", {}),
        ("refused", "deleted-during-post", {}),
        ("refused", None, {"sort": "number"}),
        ("refused", None, {"per_page": "25"}),
        ("refused", None, {"sort": "number", "per_page": "25"}),
        ("success", "-subject", {}),
        ("success", None, {"sort": "number"}),
    ],
    ids=[
        "plain",
        "retired-view",
        "named-view",
        "deleted-view",
        "explicit-sort",
        "explicit-size",
        "both-explicit",
        "successful-named-return",
        "successful-explicit-return",
    ],
)
def test_bulk_page_uses_settings_current_when_the_rows_are_redrawn(
    client,
    agent,
    category,
    monkeypatch,
    outcome,
    sort,
    chosen,
):
    client.force_login(agent)
    other = Browser()
    other.force_login(agent)
    set_pref(agent, OLD)
    tickets = [new_request(agent, category, subject=f"Timing {s}") for s in "CAB"]
    selected = tickets[0]
    filters = {"view": "all", "q": "Timing", **chosen}
    if sort:
        view = SavedView.objects.create(
            owner=agent, name="Timing view", filters={"view": "all", "q": "Timing"}, sort=sort
        )
        filters["saved"] = view.pk
    listed = client.get(reverse(QUEUE), filters)
    target = bulk_url(listed)
    payload = {
        "requests": f"{selected.pk}:{selected.revision}",
        "action": "status",
        "status": Status.WAITING_CLIENT,
    }

    def other_tab():
        assert other.get(reverse(QUEUE), NEW).status_code == 200
        if sort == "deleted-during-post":
            deleted = other.post(
                reverse("desk:view-delete", args=[view.pk]), {"expected_revision": view.revision}
            )
            assert deleted.status_code == 302
        if outcome == "refused":
            updated = other.post(
                reverse("desk:request-entry", args=[selected.pk]),
                {
                    "expected_revision": selected.revision,
                    "kind": "note",
                    "body": "Timing note.",
                },
            )
            assert updated.status_code == 302

    apply = views._apply_bulk

    def apply_after_commit(request, form):
        committed(other_tab)
        return apply(request, form)

    monkeypatch.setattr(views, "_apply_bulk", apply_after_commit)
    response = client.post(target, payload)
    selected.refresh_from_db()
    assert pref(agent) == NEW
    if outcome == "success":
        assert response.status_code == 302 and selected.status == Status.WAITING_CLIENT
        assert query(response.url) == query(target)
        shown_context = client.get(response.url).context
    else:
        assert response.status_code == 200 and selected.status == Status.OPEN
        # Django's test client also captures the other session's rendered GET.
        shown_context = next(
            c
            for c in response.context
            if c.get("request") is response.wsgi_request and c.get("bulk_form") is not None
        )
        assert shown_context["bulk_form"].non_field_errors()
        assert (
            next(t for t in shown_context["page_obj"] if t.pk == selected.pk).revision
            == selected.revision
        )
    fresh = client.get(target)
    assert settings_shown(fresh) == (
        chosen.get("sort", "-subject" if sort == "-subject" else NEW["sort"]),
        int(chosen.get("per_page", NEW["per_page"])),
    )
    painted = (
        shown_context["sort"],
        shown_context["page_obj"].paginator.per_page,
        [t.pk for t in shown_context["page_obj"]],
    )
    assert painted == (*settings_shown(fresh), row_ids(fresh))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("check", ["choice", "picker"])
def test_removed_team_membership_is_current_on_a_refused_bulk_page(
    client,
    agent,
    category,
    make_user,
    quality,
    join,
    monkeypatch,
    check,
):
    manager = make_user("assignments_supervisor", "supervisor")
    membership = join(agent, quality)
    ticket = new_request(manager, category, team=quality)
    view = SavedView.objects.create(
        owner=manager,
        name="Team collection",
        filters={"view": "all"},
        sort="number",
        team=quality,
        shared_with="team",
    )
    client.force_login(agent)
    target = lookup_view(client, view)
    opened = client.get(target)
    option = f"{quality.pk}:{agent.pk}"
    assert option in opened.context["bulk_form"]["assign_to"].as_widget()

    apply = views._apply_bulk

    def apply_after_removal(request, form):
        committed(
            lambda: team_services.remove_member(
                actor=manager, membership=membership, reason="Rotating team duty"
            )
        )
        return apply(request, form)

    monkeypatch.setattr(views, "_apply_bulk", apply_after_removal)
    page = client.post(
        bulk_url(opened),
        {"requests": f"{ticket.pk}:{ticket.revision}", "action": "assign", "assign_to": option},
    )
    assert page.status_code == 200 and page.context["bulk_form"].errors
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    fresh = client.get(target)
    if check == "choice":
        unavailable = f'value="{option}"'
        assert unavailable not in fresh.context["bulk_form"]["assign_to"].as_widget()
        assert unavailable not in page.context["bulk_form"]["assign_to"].as_widget()
    else:
        assert (page.context["page_title"], page.context["clear_url"]) == (
            fresh.context["page_title"],
            fresh.context["clear_url"],
        )
        assert page.context["page_title"] == "All tickets"


@pytest.mark.parametrize(
    "method,outcome",
    [("direct", "stale"), ("direct", "success"), ("legacy", "stale"), ("legacy", "success")],
)
def test_unknown_system_view_is_redirected_before_an_addressed_bulk_post(
    client,
    agent,
    category,
    method,
    outcome,
):
    client.force_login(agent)
    ticket = new_request(agent, category)
    revision = ticket.revision
    set_pref(agent, NEW)
    old = reverse(QUEUE) + "?view=retired-system-view&q=Example&sort=&per_page="
    target = old if method == "direct" else reverse("desk:request-bulk")
    result = client.post(
        target,
        {
            "requests": f"{ticket.pk}:{revision if outcome == 'success' else 0}",
            "action": "status",
            "status": Status.WAITING_CLIENT,
            "next": old,
        },
    )
    assert result.status_code == 302
    ticket.refresh_from_db()
    changed = method == "legacy" and outcome == "success"
    assert (ticket.status, ticket.revision) == (
        Status.WAITING_CLIENT if changed else Status.OPEN,
        revision + int(changed),
    )
    if method == "legacy":
        assert result.url == old
        result = client.get(result.url)
    assert query(result.url) == {"q": "Example"}
    assert settings_shown(client.get(result.url)) == (NEW["sort"], NEW["per_page"])
    assert pref(agent) == NEW


@pytest.mark.parametrize("surface", ["ticket", "save-view"])
@pytest.mark.parametrize(
    "order",
    ["order=subject", "order=subject&order=subject", "order=number&order=-subject"],
    ids=["one", "same-twice", "different-twice"],
)
def test_pre_marker_bookmark_with_an_offered_sort_keeps_its_queue(
    client, agent, category, surface, order
):
    """Earlier queue links carried the entire query, including repeated formerly ignored keys."""
    client.force_login(agent)
    tickets = [new_request(agent, category, subject=f"Upgrade {s}") for s in "ACB"]
    value = f"view=all&q=Upgrade&sort=number&{order}"
    if surface == "ticket":
        page = client.get(detail(tickets[1]), {"list": value})
        assert page.status_code == 200
        assert [urlsplit(page.context[k]).path for k in ("previous_url", "next_url")] == [
            detail(tickets[0]),
            detail(tickets[2]),
        ]
        back = client.get(breadcrumb(page))
        assert row_ids(back) == [t.pk for t in tickets]
    else:
        path = reverse("desk:view-create")
        form = client.get(path, {"list": value})
        assert form.status_code == 200
        result = client.post(
            path, {**fields(form), "shared_with": "private", "name": "Old ticket order"}
        )
        assert result.status_code == 302
        assert row_ids(client.get(result.url)) == [t.pk for t in tickets]
        assert SavedView.objects.get(name="Old ticket order").sort == "number"


def bounded_address(mode):
    # Eight chosen fields and one order. Percent escapes count in the carried string.
    value = "view=all&q=Collection&status=open&page=1&label=a%26b&pad=&x=&x=%2B&%6frder=subject"
    value = value.replace("pad=", "pad=" + "y" * (4000 - len(value)))
    assert len(value) == 4000
    if mode == "over-length":
        value = value.replace("pad=", "pad=y", 1)
    if mode == "over-fields":
        value = value.replace("pad=" + "y" * 20, "other=v&pad=")
    if mode == "duplicate-order":
        value = value.replace("label=a%26b", "order=other")
    return value


@pytest.mark.parametrize("mode", ["exact", "over-fields", "over-length", "duplicate-order"])
def test_ticket_controls_directly_opened_or_submitted_at_both_bounds(
    client, settings, queue_action_ticket, mode
):
    reader, tickets = queue_action_ticket
    ticket = tickets[1]
    client.force_login(reader)
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 8
    value = bounded_address(mode)
    # Adapted (C5): an address from before the order was carried may repeat `order`; only the
    # other parameters count against the limit, so the duplicate-order address is carried too.
    accepted = mode in ("exact", "duplicate-order")
    carried = value if accepted else ""
    for name in ACTIONS:
        path = action_path(name, ticket)
        page = client.get(path, {"list": value})
        assert page.status_code == 200
        assert parse_qs(urlsplit(link(page, detail(ticket))).query).get("list", [""]) == [carried]
        if name != "request-history":
            assert fields(page)["list"] == carried
            refused = client.post(path, {"list": value, "expected_revision": 0})
            assert refused.status_code == 200 and refused.context["form"].errors
            assert fields(refused)["list"] == carried
            page = refused
        back = breadcrumb(page)
        assert client.get(back).status_code == 200
        assert query(back).get("label") == ("a&b" if mode == "exact" else None)
    entry = action_path("request-entry", ticket)
    refused_note = client.post(
        entry, {"list": value, "expected_revision": ticket.revision, "kind": "note", "body": ""}
    )
    assert refused_note.status_code == 400 and refused_note.context["composer"].errors
    assert fields(refused_note, action=entry)["list"] == carried
    for name in ("request-entry", "request-take", "request-status"):
        path = action_path(name, ticket)
        payload = {
            "request-entry": {"kind": "note", "body": "Synthetic boundary note."},
            "request-status": {"status": Status.WAITING_CLIENT},
            "request-take": {},
        }[name]
        previous = ticket.revision
        result = client.post(path, {"list": value, "expected_revision": previous, **payload})
        ticket.refresh_from_db()
        assert result.status_code == 302 and ticket.revision == previous + 1
        assert parse_qs(urlsplit(result.url).query).get("list", [""]) == [carried]


def test_long_save_view_get_post_and_reopen_use_the_whole_generated_address(
    client, agent, category
):
    """Exercise the V6 wording without allowing a refusal on the overlong GET."""
    client.force_login(agent)
    tickets = [new_request(agent, category, subject=f"Bound {s}") for s in "CAB"]
    filters = {
        "view": "all",
        "padding": "x" * 4000,
        "q": "Bound",
        "status": "open",
        "sort": "subject",
    }
    queue = client.get(reverse(QUEUE), filters)
    url = link(queue, reverse("desk:view-create"))
    value = parse_qs(urlsplit(url).query)["list"][0]
    assert len(value) > 4000
    assert client.get(link(queue, detail(tickets[0]))).context["list_query"] == ""
    form = client.get(url)
    assert form.status_code == 200 and fields(form)["list"] == value
    refused = client.post(url, {**fields(form), "name": "", "shared_with": "private"})
    assert refused.status_code == 200 and refused.context["form"].errors
    assert fields(refused)["list"] == value and query(refused.context["cancel_url"]) == filters
    saved = client.post(url, {**fields(refused), "name": "Long form", "shared_with": "private"})
    assert saved.status_code == 302
    view = SavedView.objects.get(owner=agent, name="Long form")
    assert (view.filters, view.sort) == ({"view": "all", "q": "Bound", "status": "open"}, "subject")
    assert row_ids(client.get(saved.url)) == [tickets[i].pk for i in (1, 2, 0)]


@pytest.mark.parametrize("way", ["ticket", "cancel", "bulk"])
def test_unqualified_architecture_claim_about_every_return_to_a_saved_view(
    client, agent, category, way
):
    """Architecture says every way back to the view applies its order again."""
    client.force_login(agent)
    tickets = [new_request(agent, category, subject=f"Manual order {s}") for s in "BCA"]
    view = SavedView.objects.create(
        owner=agent, name="Named", filters={"view": "all"}, sort="-subject"
    )
    page = client.get(lookup_view(client, view))
    number_heading = next(
        a["href"]
        for a in controls(page).links
        if a.get("class") == "vr-sort" and query(a["href"]).get("sort") == "-number"
    )
    # Sort links are relative to the queue.
    from urllib.parse import urljoin

    ordered = client.get(urljoin(reverse(QUEUE), number_heading))
    assert ordered.context["sort"] == "-number"
    if way == "ticket":
        target = breadcrumb(client.get(link(ordered, detail(tickets[0]))))
    elif way == "cancel":
        target = client.get(link(ordered, reverse("desk:view-create"))).context["cancel_url"]
    else:
        result = client.post(
            bulk_url(ordered),
            {
                "requests": f"{tickets[0].pk}:{tickets[0].revision}",
                "action": "status",
                "status": Status.WAITING_CLIENT,
            },
        )
        assert result.status_code == 302
        target = result.url
    back = client.get(target)
    assert back.context["page_title"] == view.name and query(target)["sort"] == "-number"
    # Adapted (C6): docs/architecture.md now says a way back applies the view's order again
    # unless the person chose a sort there; the chosen heading is kept.
    assert back.context["sort"] == "-number"


def test_saved_view_sharing_and_moves_audit_the_change_and_keep_the_live_search(
    client, make_user, quality
):
    """Sharing does not edit a saved query. Refusals and a move at the end add no events."""
    from desk import saved_views

    owner = make_user("saved_history_owner", "supervisor")
    marker = "Synthetic caller QWB-427"
    saved_views.create_view(actor=owner, name="First", shared_with="private", params={})
    view = saved_views.create_view(
        actor=owner,
        name="Second",
        shared_with="private",
        params={"view": "all", "q": marker, "status": "open"},
        sort="-subject",
    )
    client.force_login(owner)
    edit = reverse("desk:view-edit", args=[view.pk])
    move = reverse("desk:view-move", args=[view.pk])
    delete = reverse("desk:view-delete", args=[view.pk])
    events = AuditEvent.objects.filter(object_type="desk.savedview", object_id=str(view.pk))

    unchanged = dict(expected_revision=view.revision, name=view.name, shared_with="private")
    refused = client.post(edit, unchanged)
    assert refused.status_code == 200 and refused.context["form"].non_field_errors()
    sharing = {**unchanged, "shared_with": "team", "team": quality.pk}
    assert client.post(edit, sharing).status_code == 302
    stale = client.post(edit, {**sharing, "name": "Stale rename"})
    assert stale.status_code == 200 and stale.context["form"].non_field_errors()
    assert client.post(move, {"direction": "up"}).status_code == 302
    assert client.post(move, {"direction": "up"}).status_code == 302

    view.refresh_from_db()
    assert view.position == 1 and view.shared_with == "team" and view.team_id == quality.pk
    assert view.filters == {"view": "all", "status": "open", "q": marker}
    assert client.post(delete, {"expected_revision": view.revision}).status_code == 302
    assert not SavedView.objects.filter(pk=view.pk).exists()
    history = list(events.order_by("occurred_at"))
    assert [e.action for e in history] == [
        "saved_view.created",
        "saved_view.updated",
        "saved_view.moved",
        "saved_view.deleted",
    ]
    assert all(e.actor_id == owner.pk for e in history)
    created, changed, moved, removed = history
    assert changed.before["shared_with"] == "private"
    assert changed.after["team"] == quality.pk
    assert (moved.before, moved.after) == ({}, {"direction": "up"})
    for entry in [created.after, changed.before, changed.after, removed.before]:
        assert entry["sort"] == "-subject"
        assert entry["filters"] == {
            "view": "all",
            "status": "open",
            "q": "(search text not recorded)",
        }


def test_ticket_history_and_admin_do_not_expose_saved_view_audit_rows(
    client, agent, make_user, category
):
    """Use the same integer id for a ticket and a view with a pre-upgrade audit row."""
    from django.contrib import admin

    from core.audit import snapshot
    from desk.tests.test_contract import new_request

    supervisor = make_user("audit_scope_reader", "supervisor")
    ticket = new_request(agent, category)
    marker = "Synthetic history subject QWB-81"
    view = SavedView.objects.create(
        pk=ticket.pk, owner=agent, name="Private older search", filters={"q": marker}
    )
    old = AuditEvent.objects.create(
        actor=agent,
        action="saved_view.created",
        object_type="desk.savedview",
        object_id=str(view.pk),
        after=snapshot(view),
    )
    client.force_login(supervisor)
    page = client.get(reverse("desk:request-history", args=[ticket.pk]))
    assert page.status_code == 200
    assert any(e.object_type == "desk.servicerequest" for e in page.context["events"])
    assert old.pk not in [e.pk for e in page.context["events"]]
    assert marker.encode() not in page.content
    assert not admin.site.is_registered(AuditEvent)
    assert not admin.site.is_registered(SavedView)


def test_saved_view_history_wording_for_pre_upgrade_records_after_rename_and_delete(client, agent):
    """Exercise the release note's history claim on an operator export limited to audit events."""
    import io

    from django.core.management import call_command

    from core.audit import snapshot

    marker = "Synthetic caller QWB-93"
    view = SavedView.objects.create(
        owner=agent, name="Earlier private search", filters={"q": marker}
    )
    # The exact creation snapshot the earlier release stored. Historical rows are append-only.
    old = AuditEvent.objects.create(
        actor=agent,
        action="saved_view.created",
        object_type="desk.savedview",
        object_id=str(view.pk),
        after=snapshot(view),
    )
    client.force_login(agent)
    assert (
        client.post(
            reverse("desk:view-edit", args=[view.pk]),
            {
                "expected_revision": view.revision,
                "name": "Current view",
                "shared_with": "private",
            },
        ).status_code
        == 302
    )
    view.refresh_from_db()
    assert (
        client.post(
            reverse("desk:view-delete", args=[view.pk]), {"expected_revision": view.revision}
        ).status_code
        == 302
    )
    assert not SavedView.objects.filter(pk=view.pk).exists()
    out = io.StringIO()
    call_command("dumpdata", "core.AuditEvent", pks=str(old.pk), stdout=out)
    exported = json.loads(out.getvalue())
    assert len(exported) == 1 and exported[0]["pk"] == str(old.pk)
    # Adapted (C7): history is not rewritten, and the release note and help now say events
    # recorded before this change keep their search text; the new rename and delete events do not.
    assert marker in exported[0]["fields"]["after"]["filters"]["q"]
    later = AuditEvent.objects.filter(object_type="desk.savedview", object_id=str(view.pk)).exclude(
        pk=old.pk
    )
    assert later.count() == 2 and marker not in str(list(later.values("before", "after")))


@pytest.mark.parametrize("stored_order", ["", "retired_column"], ids=["unconfigured", "retired"])
def test_clear_all_releases_a_sort_chosen_in_a_view_without_a_usable_order(
    client, agent, category, stored_order
):
    """The new help promises Clear all whenever a saved view's order was changed."""
    import re
    from html import unescape
    from urllib.parse import parse_qs, urljoin, urlsplit

    from django.test import Client
    from django.urls import reverse

    from desk.models import SavedView

    from .test_contract import new_request
    from .test_review_queue_way_back import controls, lookup_view, pref, set_pref

    path = reverse("desk:request-list")
    tickets = [new_request(agent, category, subject=f"Sheet {s}") for s in "CAB"]
    view = SavedView.objects.create(
        owner=agent, name="My view", filters={"view": "all"}, sort=stored_order
    )
    set_pref(agent, {"sort": "number"})
    client.force_login(agent)
    baseline = lookup_view(client, view)
    opened = client.get(baseline)
    heading = next(
        a["href"]
        for a in controls(opened).links
        if a.get("class") == "vr-sort"
        and parse_qs(urlsplit(a["href"]).query).get("sort") == ["subject"]
    )
    changed = client.get(urljoin(path, heading))
    assert [t.pk for t in changed.context["page_obj"]] == [tickets[i].pk for i in (1, 2, 0)]
    assert pref(agent)["sort"] == "subject"

    other = Client()
    other.force_login(agent)
    assert other.get(path, {"sort": "-number"}).context["sort"] == "-number"
    clear = re.search(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>Clear all</a>', changed.content.decode())
    assert clear, "No Clear all after choosing a sort in this saved view"
    assert unescape(clear.group(1)) == baseline
    restored = client.get(unescape(clear.group(1)))
    assert restored.context["sort"] == "-number"
    assert [t.pk for t in restored.context["page_obj"]] == [t.pk for t in reversed(tickets)]
    assert pref(agent)["sort"] == "-number"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("start", ["sort-only", "invalid-date-range"])
def test_phone_filter_sheet_restores_saved_rows_and_sort(
    live_server, client, agent, category, settings, start
):
    from urllib.parse import parse_qs, urlsplit

    from django.urls import reverse

    sync_api = pytest.importorskip("playwright.sync_api")
    expect, sync_playwright = sync_api.expect, sync_api.sync_playwright

    from desk.models import SavedView

    from .test_contract import new_request
    from .test_review_queue_way_back import lookup_view, pref, set_pref

    tickets = [new_request(agent, category, subject=f"Phone {c}") for c in "CAB"]
    view = SavedView.objects.create(
        owner=agent, name="Phone queue", filters={"view": "all"}, sort="-subject"
    )
    set_pref(agent, {"sort": "subject", "per_page": 50})
    client.force_login(agent)
    address = lookup_view(client, view)
    displayed = [tickets[i].number for i in (0, 2, 1)]
    cookie = settings.SESSION_COOKIE_NAME
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        context = browser.new_context(
            base_url=live_server.url,
            viewport={"width": 390, "height": 844},
            reduced_motion="reduce",
        )
        context.add_cookies(
            [{"name": cookie, "value": client.cookies[cookie].value, "url": live_server.url}]
        )
        page = context.new_page()
        page.set_default_timeout(5000)
        suffix = (
            "&created_from=2026-11-03&created_to=2026-11-01"
            if start == "invalid-date-range"
            else ""
        )
        assert page.goto(address + suffix).status == 200
        rows = page.locator('table[data-vr-grid="desk.requests"] tbody .vr-row-link')
        button = page.get_by_role("button", name="Filter and sort", exact=True)
        sheet = page.get_by_role("dialog", name="Filter and sort")
        if start == "sort-only":
            expect(rows).to_have_text(displayed)
            button.click()
            expect(sheet).to_be_visible()
            expect(sheet.get_by_role("link", name="Clear all", exact=True)).to_have_count(0)
            sheet.locator('[data-vr-sort-slot] a[href*="sort=-number"]').click()
            expect(rows).to_have_text([t.number for t in reversed(tickets)])
        else:
            expect(page.get_by_text("These filters cannot be applied", exact=True)).to_be_visible()
            expect(rows).to_have_count(0)
        # The button counts valid chips. Invalid filters may still supply a chip.
        if start == "invalid-date-range":
            # Adapted (audit review, round 1): a refused filter opens the sheet at its field.
            expect(sheet).to_be_visible()
        else:
            page.locator("button.vr-filter-button").click()
        expect(sheet).to_be_visible()
        clear = sheet.get_by_role("link", name="Clear all", exact=True)
        expect(clear).to_be_visible(timeout=2500)
        clear.click()
        expect(rows).to_have_text(displayed)
        assert urlsplit(page.url).path == reverse("desk:request-list")
        assert parse_qs(urlsplit(page.url).query) == {"view": ["all"], "saved": [str(view.pk)]}
        expect(sheet).to_be_hidden()
        browser.close()
    assert pref(agent) == {"sort": "-number" if start == "sort-only" else "subject", "per_page": 50}


@pytest.mark.parametrize(
    "reviewer_is_former_assignee", [False, True], ids=["independent", "subsequently-assigned"]
)
def test_review_counts_at_the_largest_schema_account_id(
    client, agent, category, accessioning, join, make_user, reviewer_is_former_assignee
):
    reviewer = make_user("reviewer_at_range_limit", "supervisor", pk=2**63 - 1)
    decider = make_user("decision_owner", "supervisor")
    control = make_user("independent_reader", "supervisor")
    join(reviewer, accessioning)
    join(decider, accessioning)
    ticket = new_request(agent, category, team=accessioning)
    services.set_complaint(
        actor=decider,
        request=ticket,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Collection review.",
    )
    ticket.refresh_from_db()
    services.decide_complaint(
        actor=decider,
        request=ticket,
        expected_revision=ticket.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Review the collection.",
    )
    ticket.refresh_from_db()
    services.review_complaint(
        actor=reviewer,
        request=ticket,
        expected_revision=ticket.revision,
        note="Independently checked.",
    )
    for assignee in [reviewer if reviewer_is_former_assignee else decider, None]:
        ticket.refresh_from_db()
        services.assign(
            actor=decider,
            request=ticket,
            expected_revision=ticket.revision,
            team=accessioning,
            assignee=assignee,
        )
    for reader, eligible in [(control, reviewer_is_former_assignee), (reviewer, False)]:
        client.force_login(reader)
        dashboard = client.get(reverse("dashboard"))
        assert dashboard.status_code == 200
        card = next(
            c for c in dashboard.context["queues"] if c["label"] == "Complaints awaiting my review"
        )
        assert card["count"] == int(eligible)
        assert row_ids(client.get(card["url"])) == ([ticket.pk] if eligible else [])
        assert client.get(reverse("desk:complaint-review", args=[ticket.pk])).status_code == (
            200 if eligible else 403
        )


@pytest.mark.parametrize("name", [name for name in ACTIONS if name != "request-history"])
@pytest.mark.parametrize("mode", ["exact", "over-fields", "over-length"])
def test_successful_action_returns_to_the_correct_bounded_ticket(
    client, settings, queue_action_ticket, name, mode
):
    reader, tickets = queue_action_ticket
    ticket = tickets[1]
    client.force_login(reader)
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 8
    value = bounded_address(mode)
    path = action_path(name, ticket)
    before = ticket.revision
    data = {
        **round_one.action_data(name, reader, ticket),
        "expected_revision": before,
        "list": value,
    }
    result = client.post(path, data)
    assert result.status_code == 302
    ticket.refresh_from_db()
    assert ticket.revision == before + 1 and urlsplit(result.url).path == detail(ticket)
    accepted = mode == "exact"
    assert parse_qs(urlsplit(result.url).query).get("list", [""]) == [value if accepted else ""]
    back = breadcrumb(client.get(result.url))
    assert query(back).get("q") == ("Collection" if accepted else None)
    assert client.get(back).status_code == 200


def test_clear_all_after_removing_the_saved_system_view_chip(
    client, agent, category, accessioning, join
):
    from urllib.parse import urljoin

    client.force_login(agent)
    join(agent, accessioning)
    mine = new_request(agent, category, team=accessioning, assignee=agent, subject="My ticket")
    waiting = new_request(agent, category, subject="Other team ticket")
    services.change_status(
        actor=agent,
        request=waiting,
        expected_revision=waiting.revision,
        status=Status.WAITING_CLIENT,
    )
    view = SavedView.objects.create(
        owner=agent,
        name="Waiting for collection",
        filters={"view": "waiting-client"},
        sort="number",
    )
    baseline = lookup_view(client, view)
    opened = client.get(baseline)
    assert row_ids(opened) == [waiting.pk]
    chip = next(
        a["href"]
        for a in controls(opened).links
        if a.get("aria-label", "").startswith("Remove filter View:")
    )
    changed = client.get(urljoin(reverse(QUEUE), chip))
    assert changed.context["page_title"] == view.name and row_ids(changed) == [mine.pk]
    assert changed.context["current_view"] == "my-open"
    import re
    from html import unescape

    restore = re.search(r'<a[^>]+href="([^"]+)"[^>]*>Clear all</a>', changed.content.decode())
    assert restore, "Clearing the last chip changed the tickets' system view and left no Clear all."
    assert unescape(restore.group(1)) == baseline
    assert row_ids(client.get(unescape(restore.group(1)))) == [waiting.pk]


def test_current_queue_collapses_typed_order_keys_before_building_record_links(
    client, agent, category
):
    client.force_login(agent)
    ticket = new_request(agent, category)
    queue = client.get(reverse(QUEUE) + "?view=all&sort=number&order=a&order=b")
    assert row_ids(queue) == [ticket.pk]
    for path in [detail(ticket), reverse("desk:view-create")]:
        target = link(queue, path)
        folded = parse_qs(urlsplit(target).query)["list"][0]
        assert parse_qs(folded)["order"] == ["number"]
        opened = client.get(target)
        assert opened.status_code == 200
        assert query(breadcrumb(opened)) == {"view": "all", "sort": "number"}


def test_clear_all_restores_a_view_from_a_single_invalid_filter_without_valid_chips(
    client, agent, category
):
    client.force_login(agent)
    ticket = new_request(agent, category)
    view = SavedView.objects.create(
        owner=agent, name="Catch up", filters={"view": "all"}, sort="number"
    )
    baseline = lookup_view(client, view)
    invalid = client.get(baseline + "&priority=retired-priority")
    assert invalid.status_code == 200 and invalid.context["filter_form"].errors
    assert invalid.context["active_filters"] == [] and row_ids(invalid) == []
    import re
    from html import unescape

    clear = re.search(r'<a[^>]+href="([^"]+)"[^>]*>Clear all</a>', invalid.content.decode())
    assert clear and unescape(clear.group(1)) == baseline
    restored = client.get(unescape(clear.group(1)))
    assert restored.context["sort"] == "number" and row_ids(restored) == [ticket.pk]


@pytest.mark.parametrize(
    "explicit,expected",
    [(None, "number"), ("", "number"), ("retired", "-updated_at"), ("-subject", "-subject")],
)
def test_retired_named_order_and_earlier_explicit_bookmarks(
    client, agent, category, explicit, expected
):
    client.force_login(agent)
    tickets = [new_request(agent, category, subject="Legacy " + s) for s in "ACB"]
    set_pref(agent, {"sort": "number", "per_page": 50})
    view = SavedView.objects.create(
        owner=agent, name="Earlier column", filters={"view": "all"}, sort="retired"
    )
    params = {"saved": view.pk, "view": "all"}
    if explicit is not None:
        params["sort"] = explicit
    shown = client.get(reverse(QUEUE), params)
    assert shown.status_code == 200 and shown.context["page_title"] == view.name
    expected_ids = {
        "number": [t.pk for t in tickets],
        "-updated_at": [t.pk for t in reversed(tickets)],
        "-subject": [tickets[i].pk for i in (1, 2, 0)],
    }
    assert (shown.context["sort"], row_ids(shown)) == (expected, expected_ids[expected])
    assert pref(agent) == {"sort": explicit if explicit == "-subject" else "number", "per_page": 50}


def test_saved_view_list_footer_wording_when_no_heading_was_chosen(client, agent, category):
    client.force_login(agent)
    new_request(agent, category)
    set_pref(agent, {"sort": "number"})
    view = SavedView.objects.create(
        owner=agent, name="Named order", filters={"view": "all"}, sort="subject"
    )
    opened = client.get(lookup_view(client, view))
    assert "sort" not in query(opened.wsgi_request.get_full_path())
    # Adapted (C6): the footer help now says a saved view's own order comes before the saved sort.
    assert opened.context["sort"] == view.sort


@pytest.mark.parametrize(
    "audience",
    [
        "admin",
        "supervisor",
        "client_services",
        "account_manager",
        "technical",
        "team-member",
        "no-role",
        "portal",
        "anonymous",
    ],
)
def test_direct_bulk_post_keeps_ticket_scope_after_the_picker_was_moved(
    client, agent, make_user, category, privacy_category, quality, join, clinic_portal, audience
):
    owner = make_user("named_queue_owner", "supervisor")
    visible = new_request(agent, category, subject="Ordinary collection")
    restricted = new_request(owner, privacy_category, subject="Team-only collection", team=quality)
    view = SavedView.objects.create(
        owner=owner, name="Private queue order", filters={"view": "all"}, sort="-number"
    )
    if audience == "portal":
        reader = clinic_portal
    elif audience == "anonymous":
        reader = None
    elif audience == "no-role":
        reader = make_user("without_operational_role", is_staff=True)
    else:
        reader = make_user("queue_operator", "technical" if audience == "team-member" else audience)
        if audience == "team-member":
            join(reader, quality)
    if reader:
        client.force_login(reader)
    url = reverse(QUEUE) + f"?view=all&saved={view.pk}"
    posted = client.post(
        url,
        {
            "requests": [f"{t.pk}:{t.revision}" for t in (visible, restricted)],
            "action": "status",
            "status": Status.WAITING_CLIENT,
        },
    )
    may_read_both = audience in ("admin", "supervisor", "team-member")
    for ticket in (visible, restricted):
        before = ticket.revision
        ticket.refresh_from_db()
        assert ticket.revision == before + int(may_read_both)
        assert ticket.status == (Status.WAITING_CLIENT if may_read_both else Status.OPEN)
    if audience in ("portal", "no-role"):
        assert posted.status_code == 403
    elif audience == "anonymous":
        assert posted.status_code == 302 and "next" in query(posted.url)
    else:
        if may_read_both:
            assert posted.status_code == 302
            page = client.get(posted.url)
        else:
            assert posted.status_code == 200 and posted.context["bulk_form"].errors
            page = posted
        assert page.context["page_title"] == "All tickets"
        assert set(row_ids(page)) == (
            {visible.pk, restricted.pk} if may_read_both else {visible.pk}
        )
        record = client.get(link(page, detail(visible)))
        neighbours = {
            urlsplit(record.context[k]).path
            for k in ("next_url", "previous_url")
            if record.context[k]
        }
        assert neighbours == ({detail(restricted)} if may_read_both else set())
