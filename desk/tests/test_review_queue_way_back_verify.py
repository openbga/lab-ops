"""Independent verification of the first queue review's resolutions, on synthetic data.

Adopted from the reviewer's file (archived in the review history as
test_queue_way_back_round1_verify_original.py). Five tests assert wording or bounds that the
resolution of this verification documents instead of changing (V3, V6, V10, V11, V12); each is
adapted to the documented behaviour and says so at the assertion.
"""

import json
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pytest
from django.test import Client as Browser
from django.urls import reverse

from accounts.roles import ROLES
from clients.models import PortalAccess
from core.models import AuditEvent, LabModule, UserPreference
from desk import services
from desk.models import ComplaintDecision, EntryKind, SavedView, Status

from .test_contract import new_request
from .test_preferences_in_session import committed
from .test_review_preferences_in_session_confirm import _list_controls, _size_form

pytestmark = pytest.mark.django_db
QUEUE = "desk:request-list"
KEY = "page:desk:request-list"
PERSONAL = {"sort": "number", "per_page": 25}
NEWER = {"sort": "subject", "per_page": 50}
FILTERS = {"view": "all", "q": "Collection"}
ORDER = "-subject"


def controls(page):
    return _list_controls(page.content.decode())


def params(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class NamedLinks(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.links = []
        self.href = None
        self.label = []
        self.feed(page.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append(("".join(self.label).strip(), self.href))
            self.href = None


def named(page, label):
    return next(url for text, url in NamedLinks(page).links if text == label)


def detail(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def ids(page):
    assert page.status_code == 200
    return [t.pk for t in page.context["page_obj"]]


def picker(client, view):
    listed = client.get(reverse(QUEUE))
    return next(
        a["href"]
        for a in controls(listed).links
        if params(a["href"]).get("saved") == [str(view.pk)]
    )


def hidden(page, action=None):
    return next(
        dict(f["hidden"])
        for f in controls(page).forms
        if "list" in dict(f["hidden"]) and (action is None or action == f["attrs"].get("action"))
    )


def bulk(page):
    return next(
        f["attrs"]["action"] for f in controls(page).forms if f["attrs"].get("id") == "bulk-form"
    )


def set_pref(user, values):
    return UserPreference.objects.create(user=user, key=KEY, value=values)


def later(user):
    tab = Browser()
    tab.force_login(user)
    assert tab.get(reverse(QUEUE), NEWER).context["sort"] == NEWER["sort"]
    return tab


def view_for(user, **kwargs):
    return SavedView.objects.create(owner=user, name="Collection order", filters=FILTERS, **kwargs)


@pytest.fixture
def records(agent, category):
    return [new_request(agent, category, subject=f"Collection {c}") for c in "BAC"]


@pytest.mark.parametrize(
    "way",
    [
        "picker",
        "clear",
        "ticket",
        "previous",
        "next",
        "cancel",
        "refused-save",
        "bulk-success",
        "bulk-refusal",
        "chip",
        "apply",
        "size",
        "heading",
        "htmx",
        "htmx-bulk",
    ],
)
def test_named_order_and_personal_settings_through_rendered_controls(client, agent, records, way):
    client.force_login(agent)
    row = set_pref(agent, PERSONAL)
    view = view_for(agent, sort=ORDER)
    url = picker(client, view)
    opened = client.get(url)
    order = [records[2].pk, records[0].pk, records[1].pk]
    assert ids(opened) == order
    middle = records[0]
    later(agent)
    expected_pref = NEWER.copy()
    expected_sort = ORDER
    target = url
    if way in {"ticket", "previous", "next"}:
        record = client.get(named(opened, middle.number))
        if way != "ticket":
            turn = named(record, "Previous in list" if way == "previous" else "Next in list")
            record = client.get(turn)
            assert record.context["req"].pk == order[0 if way == "previous" else 2]
        target = record.context["list_url"]
        assert target in [a["href"] for a in controls(record).links]
    elif way == "clear":
        narrowed = client.get(url + "&status=open&sort=priority")
        later(agent)
        target = named(narrowed, "Clear all")
    elif way in {"cancel", "refused-save"}:
        form_url = named(opened, "Save view")
        form = client.get(form_url)
        if way == "refused-save":
            form = client.post(form_url, {**hidden(form), "name": "", "shared_with": "private"})
            assert form.context["form"].errors
        target = named(form, "Cancel")
    elif way in {"bulk-success", "bulk-refusal", "htmx-bulk"}:
        body = {"requests": f"{middle.pk}:{middle.revision}"}
        if way != "bulk-refusal":
            body.update(action="status", status=Status.WAITING_CLIENT)
        headers = {"HX-Request": "true", "X-VR-Form": "1"} if way == "htmx-bulk" else {}
        result = client.post(bulk(opened), body, headers=headers)
        if way == "bulk-refusal":
            assert result.status_code == 200 and result.context["bulk_form"].errors
            assert (
                result.context["sort"] == ORDER and result.context["per_page"] == NEWER["per_page"]
            )
            target = bulk(result)
        elif way == "htmx-bulk":
            assert result.status_code == 204
            target = result["HX-Redirect"]
        else:
            assert result.status_code == 302
            target = result.url
        middle.refresh_from_db()
        assert middle.status == (Status.OPEN if way == "bulk-refusal" else Status.WAITING_CLIENT)
    elif way == "chip":
        filtered = client.get(url + "&status=open")
        target = next(
            a["href"]
            for a in controls(filtered).links
            if a.get("aria-label", "").startswith("Remove filter Status")
        )
        assert "status" not in params(target)
    elif way == "apply":
        form = next(
            f for f in controls(opened).forms if f["attrs"].get("aria-label") == "Filter tickets"
        )
        target = (
            form["attrs"]["action"]
            + "?"
            + urlencode([*form["hidden"], ("q", "Collection"), ("status", "open")])
        )
    elif way == "size":
        f = _size_form(controls(opened))
        target = f["attrs"]["action"] + "?" + urlencode([*f["hidden"], ("per_page", 100)])
        expected_pref["per_page"] = 100
    elif way == "heading":
        target = next(
            a["href"]
            for a in controls(opened).links
            if a.get("class") == "vr-sort" and params(a["href"]).get("sort") == ["-number"]
        )
        expected_pref["sort"] = expected_sort = "-number"
    headers = {"HX-Request": "true", "HX-Target": "queue"} if way == "htmx" else {}
    back = client.get(urljoin(reverse(QUEUE), target), headers=headers)
    assert back.status_code == 200 and back.context["page_title"] == view.name
    assert (back.context["sort"], back.context["per_page"]) == (
        expected_sort,
        expected_pref["per_page"],
    )
    assert ids(back) == (order if expected_sort == ORDER else [t.pk for t in reversed(records)])
    row.refresh_from_db()
    assert row.value == expected_pref


def test_pager_keeps_named_order_across_the_page_boundary(client, agent, category):
    client.force_login(agent)
    view = view_for(agent, sort=ORDER)
    tickets = [new_request(agent, category, subject=f"Collection {i:02}") for i in range(27)]
    set_pref(agent, PERSONAL)
    first = client.get(picker(client, view))
    following = next(a["href"] for a in controls(first).links if a.get("rel") == "next")
    other = Browser()
    other.force_login(agent)
    other.get(reverse(QUEUE), {"sort": "subject", "per_page": 25})
    second = client.get(urljoin(reverse(QUEUE), following))
    assert ids(first) + ids(second) == [t.pk for t in reversed(tickets)]
    ticket = client.get(named(second, tickets[1].number))
    assert urlsplit(named(ticket, "Previous in list")).path == detail(tickets[2])
    assert client.get(ticket.context["list_url"]).context["page_obj"].number == 2
    assert UserPreference.objects.get(user=agent, key=KEY).value == {
        "sort": "subject",
        "per_page": 25,
    }


@pytest.mark.parametrize("change", ["rename", "delete"])
def test_old_links_resolve_the_current_named_view_but_ticket_steps_keep_drawn_order(
    client, agent, records, change
):
    client.force_login(agent)
    view = view_for(agent, sort=ORDER)
    row = set_pref(agent, PERSONAL)
    url = picker(client, view)
    queue = client.get(url)
    record_url = named(queue, records[0].number)
    original_save = named(queue, "Save view")
    tab = later(agent)
    if change == "rename":
        edit = reverse("desk:view-edit", args=[view.pk])
        changed = tab.post(
            edit,
            {
                "expected_revision": view.revision,
                "name": "Renamed collection",
                "shared_with": "private",
            },
        )
    else:
        changed = tab.post(
            reverse("desk:view-delete", args=[view.pk]), {"expected_revision": view.revision}
        )
    assert changed.status_code == 302
    record = client.get(record_url)
    assert urlsplit(named(record, "Previous in list")).path == detail(records[2])
    assert urlsplit(named(record, "Next in list")).path == detail(records[1])
    cancel = named(client.get(original_save), "Cancel")
    for method, target in [
        (client.get, url),
        (client.get, record.context["list_url"]),
        (client.get, cancel),
        (client.post, bulk(queue)),
    ]:
        back = method(target)
        assert back.status_code == 200
        assert back.context["page_title"] == (
            "Renamed collection" if change == "rename" else "All tickets"
        )
        assert back.context["q"] == "Collection"
        assert back.context["sort"] == (ORDER if change == "rename" else NEWER["sort"])
    row.refresh_from_db()
    assert row.value == NEWER


@pytest.mark.parametrize(
    "sharing,member", [("private", False), ("everyone", False), ("team", False), ("team", True)]
)
def test_only_a_visible_view_supplies_the_readers_sort(
    client, agent, records, make_user, quality, join, privacy_category, sharing, member
):
    owner = make_user("ordering_owner", "supervisor")
    view = view_for(
        owner, sort=ORDER, shared_with=sharing, team=quality if sharing == "team" else None
    )
    private = new_request(owner, privacy_category, subject="Collection BB")
    if member:
        join(agent, quality)
    client.force_login(agent)
    row = set_pref(agent, PERSONAL)
    available = sharing == "everyone" or member
    chosen = {**FILTERS, "saved": str(view.pk), "sort": ""}
    page = client.get(reverse(QUEUE), chosen)
    assert page.context["page_title"] == (view.name if available else "All tickets")
    assert page.context["sort"] == (ORDER if available else PERSONAL["sort"])
    visible = ids(page)
    assert (private.pk in visible) == member
    for t in records:
        r = client.get(named(page, t.number))
        index = visible.index(t.pk)
        expected = (
            visible[index - 1] if index else None,
            visible[index + 1] if index + 1 < len(visible) else None,
        )
        actual = tuple(urlsplit(r.context[k]).path for k in ("previous_url", "next_url"))
        assert actual == tuple(
            reverse("desk:request-detail", args=[pk]) if pk else "" for pk in expected
        )
    row.refresh_from_db()
    assert row.value == PERSONAL


@pytest.mark.parametrize("source", ["direct", "picker"])
def test_retired_named_order_follows_the_person_when_they_chose_no_sort(
    client, agent, records, source
):
    client.force_login(agent)
    view = view_for(agent, sort="retired_column")
    row = set_pref(agent, PERSONAL)
    target = (
        picker(client, view)
        if source == "picker"
        else reverse(QUEUE) + "?" + urlencode({**FILTERS, "saved": view.pk})
    )
    page = client.get(target)
    row.refresh_from_db()
    assert row.value == PERSONAL
    assert (page.context["sort"], ids(page)) == ("number", [t.pk for t in records])


@pytest.mark.parametrize("old_sort", ["", "number"])
def test_prior_saved_view_bookmark_differentiates_blank_and_explicit_sort(
    client, agent, records, old_sort
):
    client.force_login(agent)
    row = set_pref(agent, NEWER)
    view = view_for(agent, sort=ORDER)
    page = client.get(reverse(QUEUE), {**FILTERS, "saved": view.pk, "sort": old_sort})
    assert page.context["sort"] == (old_sort or ORDER)
    row.refresh_from_db()
    assert row.value == {**NEWER, "sort": old_sort or NEWER["sort"]}


LISTS = [
    ("clients:client-list", "name"),
    ("desk:request-list", "number"),
    ("nonconformance:list", "title"),
    ("nonconformance:action-list", "description"),
    ("service_levels:target-list", "resolution"),
    ("service_levels:calendar-list", "name"),
    ("links:system-list", "kind"),
    ("announcement-list", "title"),
    ("portal:request-list", "subject"),
]


@pytest.mark.parametrize("route,column", LISTS)
def test_blank_grid_fields_on_each_list_follow_last_value_semantics(
    client, make_user, clinic_portal, clinic, route, column
):
    LabModule.objects.update_or_create(key="nonconformance", defaults={"is_enabled": True})
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    user = clinic_portal if route.startswith("portal:") else make_user("list_reader", "admin")
    client.force_login(user)
    path = reverse(route)
    key = f"page:{route}"
    stored = {"sort": "-" + column, "per_page": 50}
    row = UserPreference.objects.create(user=user, key=key, value=stored)
    for qs, expected in [
        ("sort=&per_page=", stored),
        (f"sort={column}&sort=&per_page=100&per_page=", stored),
        (f"sort=&sort={column}&per_page=&per_page=100", {"sort": column, "per_page": 100}),
    ]:
        page = client.get(f"{path}?{qs}")
        assert page.status_code == 200
        assert page.context["filter_form"].is_valid()
        assert (page.context["sort"], page.context["page_obj"].paginator.per_page) == (
            expected["sort"],
            expected["per_page"],
        )
        row.refresh_from_db()
        assert row.value == expected


def decided(ticket, decider):
    services.set_complaint(
        actor=decider,
        request=ticket,
        expected_revision=ticket.revision,
        is_complaint=True,
        reason="Synthetic collection review.",
    )
    ticket.refresh_from_db()
    services.decide_complaint(
        actor=decider,
        request=ticket,
        expected_revision=ticket.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Review collection timing.",
    )
    ticket.refresh_from_db()


@pytest.fixture
def review_ticket(agent, category, make_user, accessioning, join):
    reader = make_user("independent_reviewer", "supervisor")
    join(reader, accessioning)
    ticket = new_request(
        agent, category, subject="Collection", team=accessioning, first_entry_kind=EntryKind.REPLY
    )
    decided(ticket, make_user("collection_decider", "supervisor"))
    return reader, ticket


def carried_by(page):
    """Every nested list in the links and hidden form fields actually rendered."""
    parsed = controls(page)
    return [v for a in parsed.links for v in params(a["href"]).get("list", [])] + [
        v for f in parsed.forms for k, v in f["hidden"] if k == "list"
    ]


def limit_value(size):
    start, end = "view=all&pad=", "&status=open&order=subject"
    return start + "y" * (size - len(start + end)) + end


@pytest.mark.parametrize(
    "bound,size",
    [
        ("characters", 3999),
        ("characters", 4000),
        ("characters", 4001),
        ("fields", 7),
        ("fields", 8),
        ("fields", 9),
    ],
)
def test_boundary_through_all_ticket_actions_history_and_refused_forms(
    client, review_ticket, settings, size, bound
):
    reader, ticket = review_ticket
    client.force_login(reader)
    if bound == "characters":
        value = limit_value(size)
        maximum = 4000
    else:
        settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 7
        parts = [
            "view=all",
            "status=open",
            "order=subject",
            "q=Collection",
            "page=1",
            "x=",
            "x=two",
            "sort=subject",
            "per_page=25",
        ]
        value = "&".join(parts[:size])
        maximum = settings.DATA_UPLOAD_MAX_NUMBER_FIELDS + 1
    kept = value if size <= maximum else ""
    page = client.get(detail(ticket), {"list": value})
    assert page.status_code == 200
    urls = {urlsplit(a["href"]).path: a["href"] for a in controls(page).links}
    mismatches = []
    for route in [
        "request-assign",
        "request-status",
        "request-properties",
        "complaint-mark",
        "complaint-decision",
        "complaint-record",
        "complaint-review",
        "entry-withdraw",
        "request-history",
    ]:
        args = [ticket.pk]
        if route == "entry-withdraw":
            args.append(ticket.entries.get(kind=EntryKind.REPLY).pk)
        action = urls[reverse("desk:" + route, args=args)]
        if params(action).get("list", [""]) != [kept]:
            mismatches.append((route, "link from ticket"))
        form = client.get(action)
        assert form.status_code == 200
        if route != "request-history":
            body = hidden(form)
            if body["list"] != kept:
                mismatches.append((route, "hidden value"))
            refused = client.post(action, {**body, "expected_revision": 0})
            assert refused.status_code == 200 and refused.context["form"].errors
            if hidden(refused)["list"] != kept:
                mismatches.append((route, "refused form"))
            form = refused
        back = (
            form.context["list_url"] if route == "request-history" else form.context["crumbs"][0][1]
        )
        assert params(back).get("status") == (["open"] if kept else None)
        assert "order" not in params(back)
        assert client.get(back).status_code == 200
        if any(v != kept for v in carried_by(form)):
            mismatches.append((route, "return to ticket"))
    assert not mismatches
    assert all(v == kept for v in carried_by(page))


@pytest.mark.parametrize(
    "surface", ["detail", "history", "action", "refusal", "success", "entry", "take"]
)
def test_ticket_drops_an_over_field_limit_value_from_every_return_control(
    client, review_ticket, settings, surface
):
    reader, ticket = review_ticket
    client.force_login(reader)
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 6
    value = (
        "view=all&q=Collection&sort=number&per_page=25&status=open"
        "&page=1&priority=normal&order=number"
    )
    action = reverse("desk:request-status", args=[ticket.pk])
    if surface in ["success", "entry", "take"]:
        paths = {
            "success": action,
            "entry": reverse("desk:request-entry", args=[ticket.pk]),
            "take": reverse("desk:request-take", args=[ticket.pk]),
        }
        extra = {
            "entry": {"kind": "note", "body": "Synthetic note."},
            "take": {},
            "success": {"status": Status.WAITING_CLIENT},
        }
        result = client.post(
            paths[surface], {"list": value, "expected_revision": ticket.revision, **extra[surface]}
        )
        assert result.status_code == 302
        ticket.refresh_from_db()
        if surface == "take":
            assert ticket.assignee_id == reader.pk
        assert result.url == detail(ticket)
    else:
        if surface == "refusal":
            result = client.post(action, {"list": value, "expected_revision": ticket.revision})
            assert result.status_code == 200 and result.context["form"].errors
        else:
            path = {
                "detail": detail(ticket),
                "history": reverse("desk:request-history", args=[ticket.pk]),
                "action": action,
            }[surface]
            result = client.get(path, {"list": value})
        assert result.status_code == 200
        assert not any(carried_by(result))


@pytest.mark.parametrize("surface", ["breadcrumb", "cancel"])
@pytest.mark.parametrize("metadata", [False, True])
def test_the_extra_field_slot_produces_a_return_address_the_queue_can_accept(
    client, agent, records, settings, surface, metadata
):
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 4
    client.force_login(agent)
    value = "view=all&q=Collection&x=&x=two&" + ("order=subject" if metadata else "status=open")
    path = reverse("desk:view-create") if surface == "cancel" else detail(records[0])
    page = client.get(path, {"list": value})
    if page.status_code == 400 and not metadata:
        return
    assert page.status_code == 200
    back = named(page, "Cancel") if surface == "cancel" else page.context["list_url"]
    if metadata:
        assert params(back)["x"] == ["", "two"]
    assert client.get(back).status_code == 200


@pytest.mark.parametrize("size", [4000, 4001])
def test_save_view_obeys_the_same_length_bound_as_the_ticket_it_sits_beside(
    client, agent, records, size
):
    client.force_login(agent)
    fields = {"view": "all", "pad": "", "status": "open", "sort": "subject"}
    fields["pad"] = "y" * (size - len(urlencode({**fields, "order": "subject"})))
    queue = client.get(reverse(QUEUE), fields)
    form_url = named(queue, "Save view")
    nested = params(form_url).get("list", [""])[0]
    assert len(nested) in ({size} if size == 4000 else {0, size})
    form = client.get(form_url)
    if form.status_code == 400 and size > 4000:
        return
    assert form.status_code == 200
    cancel = named(form, "Cancel")
    result = client.post(form_url, {**hidden(form), "name": "Bounded", "shared_with": "private"})
    if size <= 4000:
        assert params(cancel)["status"] == ["open"]
        assert result.status_code == 302
        assert SavedView.objects.get(name="Bounded").filters["status"] == "open"
    else:
        # Adapted (V6, documented in docs/architecture.md): Save view takes the whole value and
        # stores only valid filters; only a ticket page leaves out a value over 4,000 characters.
        assert result.status_code == 302 and params(cancel)["status"] == ["open"]
        assert SavedView.objects.get(name="Bounded").filters["status"] == "open"


@pytest.mark.parametrize("origin", ["older-form", "rendered"])
def test_successfully_saved_view_at_the_field_limit_has_a_working_picker_address(
    client, agent, settings, origin
):
    client.force_login(agent)
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 5
    filters = {
        "view": "all",
        "q": "Collection",
        "status": "open",
        "complaint": "no",
        "priority": "normal",
    }
    if origin == "rendered":
        url = named(client.get(reverse(QUEUE), filters), "Save view")
    else:
        url = reverse("desk:view-create") + "?" + urlencode({"list": urlencode(filters)})
    form = client.get(url)
    assert form.status_code == 200
    result = client.post(url, {**hidden(form), "name": "At the limit", "shared_with": "private"})
    assert result.status_code == 302
    assert SavedView.objects.get(name="At the limit").filters == filters
    # Adapted (V3, documented bound in docs/architecture.md): the queue's own links assume
    # Django's limit of 1,000 parameters, which this project does not change; a cap below the
    # queue's filters plus the view's id refuses them. At the project's setting the view opens.
    assert client.get(result.url).status_code == 400
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
    opened = client.get(result.url)
    assert opened.status_code == 200 and opened.context["page_title"] == "At the limit"


@pytest.mark.parametrize("change", ["sort-only", "remove-last-chip"])
def test_clear_all_can_restore_a_named_view_when_no_filters_remain(client, agent, records, change):
    client.force_login(agent)
    view = SavedView.objects.create(
        owner=agent,
        name="Baseline",
        sort=ORDER,
        filters={"view": "all", **({"status": "open"} if change == "remove-last-chip" else {})},
    )
    page = client.get(picker(client, view))
    if change == "remove-last-chip":
        target = next(
            a["href"]
            for a in controls(page).links
            if a.get("aria-label", "").startswith("Remove filter Status")
        )
    else:
        target = next(
            a["href"]
            for a in controls(page).links
            if a.get("class") == "vr-sort" and params(a["href"]).get("sort") == ["-number"]
        )
    changed = client.get(urljoin(reverse(QUEUE), target))
    assert ids(changed)
    clear = [url for text, url in NamedLinks(changed).links if text == "Clear all"]
    assert clear, "The saved view differs from the list on screen, but there is no Clear all link."
    back = client.get(clear[0])
    assert back.context["page_title"] == view.name and back.context["sort"] == ORDER
    assert back.context["filter_form"].cleaned_data["status"] == view.filters.get("status", "")


@pytest.mark.parametrize(
    "earlier,expected",
    [
        ("order=subject", "subject"),
        ("sort=retired&order=subject", "subject"),
        ("sort=-subject&order=subject", "-subject"),
        ("sort=-subject&sort=number&order=subject", "number"),
    ],
)
def test_save_view_from_an_older_address_reopens_in_the_resolved_order(
    client, agent, records, earlier, expected
):
    client.force_login(agent)
    path = reverse("desk:view-create")
    form = client.get(path, {"list": "view=all&" + earlier})
    saved = client.post(path, {**hidden(form), "name": "Older address", "shared_with": "private"})
    assert saved.status_code == 302
    assert SavedView.objects.get(name="Older address").sort == expected
    assert client.get(saved.url).context["sort"] == expected


@pytest.mark.parametrize("role", ROLES)
def test_review_rows_chip_saved_view_dashboard_and_ticket_controls_agree(
    client, make_user, agent, category, role
):
    reader = make_user("queue_reader", role)
    decider = make_user("other_decider", "supervisor")
    eligible = [new_request(agent, category) for _ in range(2)]
    for ticket in eligible:
        decided(ticket, decider)
    undecided = new_request(agent, category)
    services.set_complaint(
        actor=decider,
        request=undecided,
        expected_revision=undecided.revision,
        is_complaint=True,
        reason="Awaiting a decision.",
    )
    filters = {"view": "complaints-review", "complaint": "review"}
    view = SavedView.objects.create(
        owner=decider,
        name="Eligible for review",
        filters=filters,
        sort="number",
        shared_with="everyone",
    )
    client.force_login(reader)
    dashboard = client.get(reverse("dashboard"))
    cards = [
        card
        for card in dashboard.context["queues"]
        if card["label"] == "Complaints awaiting my review"
    ]
    may_review = role in ("admin", "supervisor")
    assert len(cards) == int(may_review)
    targets = [reverse(QUEUE) + "?" + urlencode(filters), picker(client, view)]
    if cards:
        assert cards[0]["count"] == 2
        targets.append(cards[0]["url"])
    for url in targets:
        page = client.get(url)
        assert set(ids(page)) == ({t.pk for t in eligible} if may_review else set())
        chip = next(
            a["href"]
            for a in controls(page).links
            if a.get("aria-label") == "Remove filter Complaints: Awaiting review"
        )
        assert set(ids(client.get(urljoin(reverse(QUEUE), chip)))) == {
            t.pk for t in [*eligible, undecided]
        }
        broad = next(
            item
            for item in page.context["views"]
            if params(item["url"]).get("view") == ["complaints-review"]
        )
        assert broad["count"] == 3
    for ticket in eligible:
        form = reverse("desk:complaint-review", args=[ticket.pk])
        assert client.get(form).status_code == (200 if may_review else 403)
        record = client.get(detail(ticket), {"list": urlencode({**filters, "order": "number"})})
        neighbours = [u for u in (record.context["previous_url"], record.context["next_url"]) if u]
        assert [urlsplit(u).path for u in neighbours] == (
            [detail(next(t for t in eligible if t != ticket))] if may_review else []
        )


@pytest.mark.parametrize("surface", ["queue", "dashboard", "all-tickets"])
def test_review_count_accepts_a_valid_account_id_in_assignment_history(
    client, agent, category, make_user, join, accessioning, surface
):
    assignee = make_user("assigned_account", "technical", pk=2**31 + 1)
    join(assignee, accessioning)
    decider = make_user("decided_by", "supervisor")
    reviewer = make_user("reviewed_by", "supervisor")
    ticket = new_request(agent, category, team=accessioning, assignee=assignee)
    decided(ticket, decider)
    services.review_complaint(
        actor=reviewer,
        request=ticket,
        expected_revision=ticket.revision,
        note="Checked independently.",
    )
    client.force_login(agent if surface == "all-tickets" else reviewer)
    if surface == "dashboard":
        page = client.get(reverse("dashboard"))
        assert page.status_code == 200
        card = next(
            c for c in page.context["queues"] if c["label"] == "Complaints awaiting my review"
        )
        assert card["count"] == 0
    elif surface == "all-tickets":
        assert ids(client.get(reverse(QUEUE), {"view": "all"})) == [ticket.pk]
    else:
        assert (
            ids(client.get(reverse(QUEUE), {"view": "complaints-review", "complaint": "review"}))
            == []
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("aspect", ["view", "order"])
@pytest.mark.parametrize("change", ["rename", "delete"])
def test_refused_bulk_redraw_sees_view_change_committed_before_the_refusal(
    client, agent, records, monkeypatch, change, aspect
):
    from desk import views

    client.force_login(agent)
    tab = Browser()
    tab.force_login(agent)
    view = view_for(agent, sort=ORDER)
    row = set_pref(agent, PERSONAL)
    queue = client.get(reverse(QUEUE), {**FILTERS, "saved": view.pk})
    ticket = records[0]
    displayed_revision = ticket.revision

    def other_tab():
        if change == "rename":
            saved = tab.post(
                reverse("desk:view-edit", args=[view.pk]),
                {
                    "name": "Changed while applying",
                    "expected_revision": view.revision,
                    "shared_with": "private",
                },
            )
        else:
            saved = tab.post(
                reverse("desk:view-delete", args=[view.pk]), {"expected_revision": view.revision}
            )
        assert saved.status_code == 302
        update = tab.post(
            reverse("desk:request-entry", args=[ticket.pk]),
            {
                "expected_revision": displayed_revision,
                "kind": "note",
                "body": "A later collection note.",
            },
        )
        assert update.status_code == 302

    apply = views._apply_bulk

    def after_other_tab(request, form):
        committed(other_tab)
        return apply(request, form)

    monkeypatch.setattr(views, "_apply_bulk", after_other_tab)
    redraw = client.post(
        bulk(queue),
        {
            "requests": f"{ticket.pk}:{displayed_revision}",
            "action": "status",
            "status": Status.WAITING_CLIENT,
        },
    )
    assert redraw.status_code == 200 and redraw.context["bulk_form"].non_field_errors()
    actual = next(t for t in redraw.context["page_obj"] if t.pk == ticket.pk)
    assert actual.revision > displayed_revision and actual.status == Status.OPEN
    fresh = client.get(bulk(queue))
    assert row.value == UserPreference.objects.get(pk=row.pk).value
    if aspect == "order":
        assert (redraw.context["sort"], ids(redraw)) == (fresh.context["sort"], ids(fresh))
    else:
        keys = ("page_title", "clear_url")
        assert [redraw.context[k] for k in keys] == [fresh.context[k] for k in keys]


@pytest.mark.parametrize("transition", ["create", "rename", "delete"])
def test_private_free_text_search_stays_out_of_saved_view_audit_payload(client, agent, transition):
    client.force_login(agent)
    marker = "example.contact@sample.invalid"
    listed = client.get(reverse(QUEUE), {"view": "all", "q": marker})
    address = named(listed, "Save view")
    form = client.get(address)
    result = client.post(
        address, {**hidden(form), "name": "Private search", "shared_with": "private"}
    )
    assert result.status_code == 302
    view = SavedView.objects.get(owner=agent, name="Private search")
    assert view.filters["q"] == marker
    action = "created"
    if transition == "rename":
        result = client.post(
            reverse("desk:view-edit", args=[view.pk]),
            {
                "name": "Renamed search",
                "shared_with": "private",
                "expected_revision": view.revision,
            },
        )
        action = "updated"
    elif transition == "delete":
        result = client.post(
            reverse("desk:view-delete", args=[view.pk]), {"expected_revision": view.revision}
        )
        action = "deleted"
    assert result.status_code == 302
    event = AuditEvent.objects.get(action=f"saved_view.{action}", object_id=str(view.pk))
    assert marker not in json.dumps([event.before, event.after, event.reason])


def test_navigation_keeps_search_out_of_request_logs_shared_responses_and_ticket_audit(
    client, agent, records, caplog
):
    client.force_login(agent)
    ticket = records[0]
    marker = "case-contact@example.invalid"
    value = urlencode({"view": "all", "q": marker, "order": "number"})
    args = [ticket.pk]
    caplog.set_level("INFO", logger="lab_ops.request")
    pages = [
        client.get(reverse(QUEUE), {"view": "all", "q": marker}),
        client.get(detail(ticket), {"list": value}),
        client.get(reverse("desk:request-status", args=args), {"list": value}),
        client.get(reverse("desk:view-create"), {"list": value}, headers={"X-VR-Form": "1"}),
    ]
    assert all(p.status_code == 200 for p in pages)
    for page in pages:
        vary = {s.strip().lower() for s in page.get("Vary", "").split(",")}
        assert "private" in page.get("Cache-Control", "") or "cookie" in vary
    assert "private" in pages[1]["Cache-Control"]
    assert "X-VR-Form" in pages[-1]["Vary"]
    result = client.post(
        reverse("desk:request-status", args=args),
        {"list": value, "expected_revision": ticket.revision, "status": Status.WAITING_CLIENT},
    )
    assert result.status_code == 302
    events = AuditEvent.objects.filter(
        object_id=str(ticket.pk), object_type=ticket._meta.label_lower
    )
    assert events.count() >= 2
    assert marker not in json.dumps(list(events.values("before", "after", "reason")))
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "lab_ops.request" or r.name.startswith("django.request")
    ]
    assert len(messages) >= 5 and all(marker not in m and "case-contact" not in m for m in messages)


@pytest.mark.parametrize("return_to", ["ticket", "cancel", "bulk"])
def test_return_help_says_unselected_sort_follows_current_personal_settings(
    client, agent, records, return_to
):
    """Literal return-help expectation, distinct from the implemented named-view precedence."""
    client.force_login(agent)
    set_pref(agent, PERSONAL)
    view = view_for(agent, sort=ORDER)
    page = client.get(picker(client, view))
    if return_to == "ticket":
        target = client.get(named(page, records[0].number)).context["list_url"]
    elif return_to == "cancel":
        target = named(client.get(named(page, "Save view")), "Cancel")
    else:
        submitted = client.post(
            bulk(page),
            {
                "requests": f"{records[0].pk}:{records[0].revision}",
                "action": "status",
                "status": Status.WAITING_CLIENT,
            },
        )
        assert submitted.status_code == 302
        target = submitted.url
    later(agent)
    back = client.get(target)
    assert back.context["per_page"] == NEWER["per_page"]
    # Adapted (V10): the help now says that in a saved view its own order applies.
    assert back.context["sort"] == ORDER


@pytest.mark.parametrize("kind", ["char-at", "char-over", "field-at", "field-over"])
def test_leaving_scope_at_a_bound_returns_to_a_usable_queue(
    client, agent, category, privacy_category, settings, kind
):
    client.force_login(agent)
    if kind.startswith("char"):
        value = limit_value(4000 if kind == "char-at" else 4001)
    else:
        settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 5
        value = "view=all&q=Collection&status=open&x=1&x=2&order=subject"
        if kind == "field-over":
            value += "&x=3"
    ticket = new_request(agent, category)
    result = client.post(
        reverse("desk:request-properties", args=[ticket.pk]),
        {
            "expected_revision": ticket.revision,
            "category": privacy_category.pk,
            "priority": "normal",
            "list": value,
        },
    )
    assert result.status_code == 302
    assert params(result.url).get("status") == (["open"] if kind.endswith("at") else None)
    assert "order" not in params(result.url)
    assert client.get(result.url).status_code == 200
    assert client.get(detail(ticket)).status_code == 404


@pytest.mark.parametrize("limit", [1000, None])
def test_many_short_ordinary_fields_do_not_create_a_dead_end_queue_link(
    client, agent, records, settings, limit
):
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = limit
    client.force_login(agent)
    text = "view=all&" + "&".join(["x="] * 1000)
    assert len(text) < 4000
    form = client.get(reverse("desk:view-create"), {"list": text})
    if form.status_code == 400 and limit is not None:
        return
    assert form.status_code == 200
    assert client.get(named(form, "Cancel")).status_code == 200


def test_independence_after_reassignment_and_a_decision_revision(
    client, agent, category, make_user, join, accessioning
):
    first = make_user("original_decider", "supervisor")
    prior = make_user("former_assignee", "supervisor")
    current = make_user("current_decider", "supervisor")
    independent = make_user("eligible_reviewer", "supervisor")
    join(prior, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=prior)
    services.assign(
        actor=current,
        request=ticket,
        expected_revision=ticket.revision,
        team=accessioning,
        assignee=None,
    )
    ticket.refresh_from_db()
    decided(ticket, first)
    services.decide_complaint(
        actor=current,
        request=ticket,
        expected_revision=ticket.revision,
        decision=ComplaintDecision.NO_INVESTIGATION,
        rationale="Independent evidence.",
    )
    ticket.refresh_from_db()
    for who in (prior, first, current, independent):
        client.force_login(who)
        eligible = who == independent
        card = next(
            c
            for c in client.get(reverse("dashboard")).context["queues"]
            if c["label"] == "Complaints awaiting my review"
        )
        assert card["count"] == int(eligible)
        assert ids(client.get(card["url"])) == ([ticket.pk] if eligible else [])
        assert client.get(reverse("desk:complaint-review", args=[ticket.pk])).status_code == (
            200 if eligible else 403
        )


def test_old_picker_bookmark_against_the_unqualified_help_about_personal_sort(
    client, agent, records
):
    """The previous picker automatically added the stored sort to its bookmark."""
    client.force_login(agent)
    view = view_for(agent, sort=ORDER)
    old_address = reverse(QUEUE) + "?" + urlencode({**FILTERS, "saved": view.pk, "sort": ORDER})
    row = set_pref(agent, NEWER)
    opened = client.get(old_address)
    assert opened.context["sort"] == ORDER
    row.refresh_from_db()
    # Adapted (V11): the help and changelog now say a view link saved earlier carries its sort,
    # which is a choice and is saved, as it was before this change.
    assert row.value == {**NEWER, "sort": ORDER}


def test_older_address_with_an_unoffered_sort_against_architecture_wording(client, agent, records):
    """Architecture says an older address with order keeps its sort unless it had none."""
    client.force_login(agent)
    base = "view=all&sort=retired"
    ordinary = client.get(reverse(QUEUE) + "?" + base)
    assert ordinary.context["sort"] != "subject"
    form_url = reverse("desk:view-create")
    form = client.get(form_url, {"list": base + "&order=subject"})
    result = client.post(
        form_url, {**hidden(form), "name": "Upgraded order", "shared_with": "private"}
    )
    assert result.status_code == 302
    # Adapted (V12): docs/architecture.md now says an older address with a typed `order` and a
    # missing or retired sort follows the `order`.
    assert client.get(result.url).context["sort"] == "subject"


@pytest.mark.parametrize("audience", ["portal", "no-role", "anonymous"])
def test_dashboard_and_direct_review_routes_for_nonstaff_audiences(
    client, clinic_portal, make_user, review_ticket, audience
):
    _, ticket = review_ticket
    if audience != "anonymous":
        user = clinic_portal if audience == "portal" else make_user("no_desk_role", is_staff=True)
        client.force_login(user)
    dashboard = client.get(reverse("dashboard"))
    assert dashboard.status_code == (403 if audience == "no-role" else 302)
    if audience == "portal":
        assert dashboard.url == reverse("portal:home")
    filters = {"view": "complaints-review", "complaint": "review"}
    denied = 302 if audience == "anonymous" else 403
    for url, query in [
        (reverse(QUEUE), filters),
        (reverse("desk:complaint-review", args=[ticket.pk]), {"list": urlencode(filters)}),
        (reverse("desk:view-create"), {"list": urlencode(filters)}),
    ]:
        assert client.get(url, query).status_code == denied
