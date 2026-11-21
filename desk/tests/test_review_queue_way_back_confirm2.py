"""Second confirmation of the ticket queue return path. All records are synthetic.

Adopted from the reviewer's file (archived in the review history as
test_queue_way_back_round1_confirm2_original.py). Three cases are adapted to the behaviour the
resolution documents (findings 3, 5 and 6), each marked at its assertion.
"""

import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from django.urls import reverse

from core import changelog, preferences
from core.models import AuditEvent, UserPreference
from desk import services, views
from desk.models import SavedView, Status

from .test_contract import new_request
from .test_preferences_in_session import committed, edition, post, same_session
from .test_review_queue_way_back import (
    NEW,
    OLD,
    QUEUE,
    bulk_url,
    controls,
    detail,
    fields,
    lookup_view,
    pref,
    row_ids,
    set_pref,
    settings_shown,
)
from .test_review_queue_way_back_verify import named

pytestmark = pytest.mark.django_db


def shell(page):
    html = page.content.decode()
    payload = re.search(r'<script id="vr-preferences" type="application/json">(.*?)</script>', html)
    assert payload and 'aria-label="Primary"' in html and "vr-topbar-btn vr-whats-new" in html
    return (
        'class="vr-shell vr-nav-collapsed"' in html,
        'data-vr-nav-toggle aria-controls="vr-nav" aria-expanded="false"' in html,
        json.loads(payload[1]),
        'data-vr-unread="1"' in html,
    )


def post_context(page):
    return next(
        c
        for c in page.context
        if c.get("request") is page.wsgi_request and c.get("bulk_form") is not None
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("prior_read", [False, True], ids=["ordinary-post", "cached-earlier"])
def test_refused_rows_sidebar_layout_and_release_badge_use_the_same_read(
    client, agent, category, monkeypatch, prior_read
):
    editions = [edition(20), edition(19)]
    monkeypatch.setattr(changelog, "entries", lambda: editions)
    UserPreference.objects.create(
        user=agent, key=preferences.WHATS_NEW_KEY, value=changelog.seen_marker(editions[1:])
    )
    set_pref(agent, OLD)
    tickets = [new_request(agent, category, subject=f"Shell {s}") for s in "CAB"]
    ticket = tickets[0]
    client.force_login(agent)
    other = same_session(client)
    assert post(client, "nav.collapsed", False).status_code == 204
    layout_key = "grid:desk.requests:v3"
    layout = {"order": ["number", "subject"], "hidden": ["status"]}
    url = reverse(QUEUE) + "?view=all&sort=number&sort=&per_page=25&per_page="
    opened = client.get(url)
    assert shell(opened) == (False, False, {"nav.collapsed": False}, True)
    assert settings_shown(opened) == (OLD["sort"], OLD["per_page"])

    def update_in_other_tab():
        assert other.get(reverse(QUEUE), NEW).status_code == 200
        assert post(other, "nav.collapsed", True).status_code == 204
        assert post(other, layout_key, layout).status_code == 204
        assert other.get(reverse("whats-new")).status_code == 200
        updated = other.post(
            reverse("desk:request-entry", args=[ticket.pk]),
            {"expected_revision": ticket.revision, "kind": "note", "body": "Second tab update."},
        )
        assert updated.status_code == 302

    apply = views._apply_bulk

    def apply_after_update(request, form):
        if prior_read:
            assert preferences.for_request(request)["nav.collapsed"] is False
        committed(update_in_other_tab)
        return apply(request, form)

    monkeypatch.setattr(views, "_apply_bulk", apply_after_update)
    result = client.post(
        url,
        {
            "action": "status",
            "status": Status.WAITING_CLIENT,
            "requests": f"{ticket.pk}:{ticket.revision}",
        },
    )
    assert result.status_code == 200 and post_context(result)["bulk_form"].non_field_errors()
    fresh = client.get(url)
    assert shell(fresh) == (True, True, {"nav.collapsed": True, layout_key: layout}, False)
    assert settings_shown(fresh) == (NEW["sort"], NEW["per_page"])
    assert row_ids(fresh) == [t.pk for t in reversed(tickets)]
    ctx = post_context(result)
    page = ctx["page_obj"]
    assert pref(agent) == NEW
    assert (shell(result), ctx["sort"], page.paginator.per_page, [t.pk for t in page]) == (
        shell(fresh),
        *settings_shown(fresh),
        row_ids(fresh),
    )


@pytest.mark.parametrize("unknown", [False, True], ids=["success", "unknown-view"])
def test_redirect_drops_last_blank_and_keeps_repeated_choices(client, agent, category, unknown):
    ticket = new_request(agent, category)
    set_pref(agent, OLD)
    client.force_login(agent)
    view = "obsolete" if unknown else "all"
    target = reverse(QUEUE) + f"?view={view}&sort=number&sort=&per_page=25&per_page=100&x=&x=two"
    result = client.post(
        target,
        {
            "action": "status",
            "status": Status.WAITING_CLIENT,
            "requests": f"{ticket.pk}:{ticket.revision}",
        },
    )
    assert result.status_code == 302
    ticket.refresh_from_db()
    assert ticket.status == (Status.OPEN if unknown else Status.WAITING_CLIENT)
    assert pref(agent) == OLD
    assert not UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY).exists()
    assert parse_qs(urlsplit(result.url).query, keep_blank_values=True) == {
        **({} if unknown else {"view": ["all"]}),
        "per_page": ["25", "100"],
        "x": ["", "two"],
    }
    assert settings_shown(client.get(result.url)) == (OLD["sort"], 100)


@pytest.mark.django_db(transaction=True)
def test_refused_queue_count_matches_its_rows_after_a_status_change(
    client, agent, category, monkeypatch
):
    first, second = [new_request(agent, category, subject=s) for s in ("Waiting", "Incoming")]

    def wait_for_reply(ticket):
        services.change_status(
            actor=agent,
            request=ticket,
            expected_revision=ticket.revision,
            status=Status.WAITING_CLIENT,
        )

    wait_for_reply(first)
    client.force_login(agent)
    url = reverse(QUEUE) + "?view=waiting-client"
    assert row_ids(client.get(url)) == [first.pk]
    picker = views._view_picker

    def picker_then_commit(*args):
        counts = picker(*args)
        committed(lambda: wait_for_reply(second))
        return counts

    with monkeypatch.context() as patch:
        patch.setattr(views, "_view_picker", picker_then_commit)
        refused = client.post(url, {})
    assert refused.status_code == 200
    ctx = post_context(refused)
    assert ctx["bulk_form"].errors
    assert {t.pk for t in ctx["page_obj"]} == {first.pk, second.pk}
    fresh = client.get(url)

    def current_count(ctx):
        return next(item["count"] for item in ctx["views"] if item["current"])

    assert current_count(fresh.context) == fresh.context["page_obj"].paginator.count == 2
    # Adapted (finding 3, documented bound in the queue help's Views): a view's count is read as
    # the page is drawn, so a ticket changed at that moment is in the table only, until the next
    # page.
    assert current_count(ctx) == 1


@pytest.mark.django_db(transaction=True)
def test_returning_team_member_is_not_listed_as_both_available_and_invalid(
    client, agent, make_user, category, quality, monkeypatch
):

    from teams import services as team_services

    manager = make_user("assignment_scheduler", "supervisor")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    page = client.get(reverse("desk:request-list"), {"view": "all"})
    option = f"{quality.pk}:{agent.pk}"
    assert f'value="{option}"'.encode() in page.content
    target = bulk_url(page)
    data = {"action": "assign", "assign_to": option, "requests": f"{ticket.pk}:{ticket.revision}"}
    team_services.remove_member(actor=manager, membership=membership, reason="Staff rotation")
    apply = views._apply_bulk

    def apply_while_member_returns(request, form):
        committed(lambda: team_services.add_member(actor=manager, team=quality, user=agent))
        return apply(request, form)

    with monkeypatch.context() as patch:
        patch.setattr(views, "_apply_bulk", apply_while_member_returns)
        refused = client.post(target, data)
    if refused.status_code == 302:
        ticket.refresh_from_db()
        assert ticket.assignee_id == agent.pk
        return
    assert refused.status_code == 200 and refused.context["bulk_form"].errors
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    html = refused.content.decode()
    available = re.search(rf'<option\b[^>]*value="{re.escape(option)}"[^>]*>', html)
    invalid = f"{option} is not one of the available choices" in html
    retry = client.post(target, data)
    ticket.refresh_from_db()
    assert retry.status_code == 302 and ticket.assignee_id == agent.pk
    assert not (available and invalid), "The displayed Assign to option is also called unavailable."


def test_refused_bulk_field_error_is_visible_after_last_row_leaves_the_queue(
    client, agent, make_user, category, quality
):

    from teams import services as team_services

    manager = make_user("empty_queue_manager", "supervisor")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    services.change_status(
        actor=manager,
        request=ticket,
        expected_revision=ticket.revision,
        status=Status.WAITING_CLIENT,
    )
    ticket.refresh_from_db()
    client.force_login(manager)
    opened = client.get(reverse("desk:request-list"), {"view": "waiting-client"})
    assert row_ids(opened) == [ticket.pk]
    data = {
        "action": "assign",
        "assign_to": f"{quality.pk}:{agent.pk}",
        "requests": f"{ticket.pk}:{ticket.revision}",
    }
    team_services.remove_member(actor=manager, membership=membership, reason="Staff rotation")
    services.change_status(
        actor=manager, request=ticket, expected_revision=ticket.revision, status=Status.OPEN
    )
    refused = client.post(bulk_url(opened), data)
    assert refused.status_code == 200 and not row_ids(refused)
    errors = refused.context["bulk_form"].errors["assign_to"]
    assert errors and ticket.assignee_id is None
    assert any(error.encode() in refused.content for error in errors), (
        "The bound form refused the submitted assignee, but the queue lost its last row "
        "and no assignment error was displayed."
    )


def test_restoring_removed_view_chip_from_an_empty_result(client, agent, category):

    ticket = new_request(agent, category)
    services.change_status(
        actor=agent, request=ticket, expected_revision=ticket.revision, status=Status.WAITING_CLIENT
    )
    client.force_login(agent)
    view = SavedView.objects.create(
        owner=agent, name="Waiting for reply", filters={"view": "waiting-client"}, sort="subject"
    )
    target = lookup_view(client, view)
    opened = client.get(target)
    assert row_ids(opened) == [ticket.pk]
    chip = next(
        a["href"]
        for a in controls(opened).links
        if a.get("aria-label", "").startswith("Remove filter View:")
    )
    changed = client.get(urljoin(reverse("desk:request-list"), chip))
    assert not row_ids(changed) and changed.context["current_view"] == "my-open"
    assert b"No tickets match" in changed.content
    assert named(changed, "Clear filters") == named(changed, "Clear all") == target
    assert row_ids(client.get(target)) == [ticket.pk]


def test_retired_explicit_sort_can_be_cleared_without_saving_the_grid_fallback(
    client, agent, category
):

    tickets = [new_request(agent, category, subject=s) for s in ("B", "C", "A")]
    set_pref(agent, {"sort": "subject"})
    client.force_login(agent)
    view = SavedView.objects.create(
        owner=agent, name="Old order", filters={"view": "all"}, sort="retired_name"
    )
    target = lookup_view(client, view)
    changed = client.get(target + "&sort=retired_name")
    assert changed.context["sort"] == "-updated_at"
    assert pref(agent) == {"sort": "subject"}
    assert named(changed, "Clear all") == target
    restored = client.get(target)
    assert restored.context["sort"] == "subject"
    assert row_ids(restored) == [tickets[i].pk for i in (2, 0, 1)]


@pytest.mark.parametrize("total", [8, 9, 10, 11])
def test_documented_twice_the_field_limit_on_inner_addresses(
    client, agent, category, settings, total
):
    """C5's documented secondary bound counts total fields, including repeated order."""

    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 4
    client.force_login(agent)
    ticket = new_request(agent, category)
    # Only one ordinary field, safely under both the ordinary count and character limits.
    carried = "&".join(["view=all"] + ["order=number"] * (total - 1))
    record = client.get(detail(ticket), {"list": carried})
    form = client.get(reverse("desk:view-create"), {"list": carried})
    assert record.status_code == 200
    expected = (carried, 200) if total <= 8 else ("", 400)
    assert (record.context["list_query"], form.status_code) == expected


@pytest.mark.parametrize(
    "sort,last", [(None, "-subject"), ("retired", "subject"), ("number", "-subject")]
)
def test_repeated_order_preserves_the_offered_sort_or_uses_the_last_marker(
    client, agent, category, sort, last
):

    client.force_login(agent)
    tickets = [new_request(agent, category, subject=f"Order {s}") for s in "CAB"]
    carried = "view=all&x=&x=%2B&order=number&%6frder=" + last
    if sort:
        carried += "&sort=" + sort
    expected_sort = sort if sort == "number" else last
    indexes = {"number": (0, 1, 2), "subject": (1, 2, 0), "-subject": (0, 2, 1)}[expected_sort]
    ordered = [tickets[i] for i in indexes]
    record = client.get(detail(ordered[1]), {"list": carried})
    assert record.status_code == 200
    assert [urlsplit(record.context[k]).path for k in ("previous_url", "next_url")] == [
        detail(ordered[0]),
        detail(ordered[2]),
    ]
    back = parse_qs(urlsplit(record.context["list_url"]).query, keep_blank_values=True)
    assert back == {"view": ["all"], "x": ["", "+"], **({"sort": [sort]} if sort else {})}
    path = reverse("desk:view-create")
    form = client.get(path, {"list": carried})
    assert form.status_code == 200 and fields(form)["list"] == carried
    saved = client.post(path, {**fields(form), "name": "Old link", "shared_with": "private"})
    assert saved.status_code == 302
    assert SavedView.objects.get(owner=agent, name="Old link").sort == expected_sort
    assert row_ids(client.get(saved.url)) == [t.pk for t in ordered]


def test_adopted_history_check_with_a_ticket_that_has_the_same_id(
    client, agent, category, monkeypatch
):
    """Run the adopted check itself alongside a legitimate audit event for a different model."""
    from importlib.util import find_spec

    module = "desk.tests.test_review_queue_way_back_confirm"
    if find_spec(module) is None:
        pytest.skip("This checkout predates adoption of the C7 test.")
    from .test_review_queue_way_back_confirm import (
        test_saved_view_history_wording_for_pre_upgrade_records_after_rename_and_delete as check,
    )

    ticket = new_request(agent, category)
    assert AuditEvent.objects.filter(
        object_type="desk.servicerequest", object_id=str(ticket.pk)
    ).exists()
    create = SavedView.objects.create

    def create_with_matching_id(**values):
        return create(pk=ticket.pk, **values)

    monkeypatch.setattr(SavedView.objects, "create", create_with_matching_id)
    check(client, agent)


def test_architecture_claim_about_inherited_settings_on_a_successful_post(client, agent, category):
    """The architecture says a bulk POST adds the saved page size and sort."""
    ticket = new_request(agent, category)
    set_pref(agent, OLD)
    client.force_login(agent)
    response = client.post(
        reverse(QUEUE) + "?view=all",
        {
            "action": "status",
            "status": Status.WAITING_CLIENT,
            "requests": f"{ticket.pk}:{ticket.revision}",
        },
    )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.url).query) == {"view": ["all"]}
    assert settings_shown(client.get(response.url)) == (OLD["sort"], OLD["per_page"])
    params = response.wsgi_request.GET
    # Adapted (finding 5): docs/architecture.md now says a successful change returns to the
    # address as chosen; the saved settings apply on arrival, not to the post.
    assert (params.get("sort"), params.get("per_page")) == (None, None)


def test_architecture_order_precedence_for_a_retired_heading_on_a_bookmark(client, agent, category):
    """The view has the first usable order in the documented precedence."""

    tickets = [new_request(agent, category, subject=s) for s in ("Alpha", "Charlie", "Bravo")]
    set_pref(agent, {"sort": "number"})
    client.force_login(agent)
    view = SavedView.objects.create(
        owner=agent, name="Named order", filters={"view": "all"}, sort="subject"
    )
    url = lookup_view(client, view)
    expected_ids = [tickets[i].pk for i in (0, 2, 1)]
    assert row_ids(client.get(url)) == expected_ids
    result = client.get(url + "&sort=retired_heading")
    assert result.status_code == 200 and pref(agent) == {"sort": "number"}
    # Adapted (finding 6): docs/architecture.md now says a chosen sort the list does not offer
    # shows the list's default (newest change first).
    newest_first = [t.pk for t in sorted(tickets, key=lambda t: t.updated_at, reverse=True)]
    assert (result.context["sort"], row_ids(result)) == ("-updated_at", newest_first)
