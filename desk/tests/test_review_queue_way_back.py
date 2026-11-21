"""Independent review of the staff queue's return links. All accounts and tickets are synthetic.

Adopted from the reviewer's round-1 file (archived in the review history as
test_queue_way_back_round1_original.py) with these changes, each explained at the test: F9 and F11
assert the documented behaviour (the reworded changelog entry and queue help), and F8's dialog case
moved to the browser journeys (e2e/test_preferences.py), adapted to the reworded Cancel help.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import pytest
from django.test import Client as Browser
from django.urls import reverse

from accounts.roles import ROLES
from core.models import UserPreference
from desk import services
from desk.lists import DEFAULT_SORT
from desk.models import ComplaintDecision, EntryKind, SavedView, ServiceRequest, Status

from .test_contract import new_request
from .test_review_preferences_in_session_confirm import _list_controls

pytestmark = pytest.mark.django_db

QUEUE = "desk:request-list"
KEY = f"page:{QUEUE}"
OLD = {"sort": "subject", "per_page": 50}
NEW = {"sort": "-number", "per_page": 100}
CHOICES = {"view": "all", "q": "Collection", "page": "1"}


class Navigation(HTMLParser):
    """Read the links inside a named navigation landmark, excluding the sidebar and footer."""

    def __init__(self, page, label):
        super().__init__()
        self.label = label
        self.inside = False
        self.links = []
        self.feed(page.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self.inside = attrs.get("aria-label") == self.label
        if self.inside and tag == "a":
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "nav":
            self.inside = False


def detail(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def controls(page):
    return _list_controls(page.content.decode())


def link(page, path):
    return next(a["href"] for a in controls(page).links if urlsplit(a["href"]).path == path)


def fields(page, *, action=None):
    return next(
        dict(f["hidden"])
        for f in controls(page).forms
        if "list" in dict(f["hidden"]) and (action is None or f["attrs"].get("action") == action)
    )


def bulk_url(page):
    return next(
        f["attrs"]["action"] for f in controls(page).forms if f["attrs"].get("id") == "bulk-form"
    )


def query(url):
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def queue(client, params=None):
    page = client.get(reverse(QUEUE), params or {})
    assert page.status_code == 200 and page.context["filter_form"].is_valid()
    return page


def row_ids(page):
    return [t.pk for t in page.context["page_obj"]]


def settings_shown(page):
    return (page.context["sort"], page.context["page_obj"].paginator.per_page)


def later_tab(user):
    other = Browser()
    other.force_login(user)
    assert settings_shown(queue(other, NEW)) == (NEW["sort"], NEW["per_page"])
    assert pref(user) == NEW
    return other


def pref(user):
    return UserPreference.objects.filter(user=user, key=KEY).values_list("value", flat=True).first()


def set_pref(user, value):
    UserPreference.objects.create(user=user, key=KEY, value=value)


def breadcrumb(page):
    url = Navigation(page, "Breadcrumb").links[0]
    assert urlsplit(url).path == reverse(QUEUE)
    return url


def neighbours(page):
    return [urlsplit(href).path for href in Navigation(page, "Tickets in this list").links]


def lookup_view(client, saved):
    return next(
        a["href"]
        for a in controls(queue(client)).links
        if query(a["href"]).get("saved") == str(saved.pk)
    )


def save_form(client, listed):
    url = link(listed, reverse("desk:view-create"))
    page = client.get(url)
    assert page.status_code == 200
    return url, page


def save(client, url, form):
    result = client.post(
        url, {**fields(form), "name": "Review collection", "shared_with": "private"}
    )
    assert result.status_code == 302
    return result


@pytest.fixture
def trio(agent, category):
    tickets = [new_request(agent, category, subject=f"Collection {c}") for c in "ABC"]
    ServiceRequest.objects.filter(pk__in=[t.pk for t in tickets]).update(assignee=agent)
    return tickets


@pytest.mark.parametrize(
    "source",
    ["default", "saved-sort", "chosen", "unsupported", "typed-order", "saved-view", "second-page"],
)
def test_displayed_neighbours_and_reopened_view(client, agent, category, trio, source):
    """Compare neighbour links and the saved view's reopened rows with the displayed queue."""
    client.force_login(agent)
    params = {"view": "all", "q": "Collection", "per_page": "25"}
    if source == "default":
        params = {}
    elif source == "saved-sort":
        set_pref(agent, OLD)
    elif source == "chosen":
        params["sort"] = "-subject"
    elif source == "unsupported":
        params["sort"] = "retired_column"
    elif source == "typed-order":
        params["order"] = "subject"
    elif source == "saved-view":
        named = SavedView.objects.create(
            owner=agent, name="By number", filters={"view": "all", "q": "Collection"}, sort="number"
        )
        params = query(lookup_view(client, named))
    elif source == "second-page":
        params["sort"] = "subject"
        for i in range(24):
            new_request(agent, category, subject=f"Collection extra {i:02}")
    listed = queue(client, params)
    ids = row_ids(listed)
    sort = listed.context["sort"]
    if source == "second-page":
        assert listed.context["page_obj"].has_next()
        page_two = next(
            a["href"] for a in controls(listed).links if query(a["href"]).get("page") == "2"
        )
        listed = client.get(urljoin(reverse(QUEUE), page_two))
        params = query(page_two)
        assert listed.context["page_obj"].number == 2
        ids += row_ids(listed)
        middle = ids.index(row_ids(listed)[0])
    else:
        middle = 1
    paths = [reverse("desk:request-detail", args=[pk]) for pk in ids]
    page = client.get(link(listed, paths[middle]))
    assert neighbours(page) == [paths[middle - 1], paths[middle + 1]]
    for offset, href in zip((-1, 1), Navigation(page, "Tickets in this list").links, strict=True):
        neighbour = client.get(href)
        assert neighbour.context["req"].pk == ids[middle + offset]
        assert (
            urlsplit(neighbour.context["next_url" if offset == -1 else "previous_url"]).path
            == paths[middle]
        )
        assert query(breadcrumb(neighbour)) == {k: v for k, v in params.items() if k != "order"}
    url, form = save_form(client, listed)
    result = save(client, url, form)
    saved = SavedView.objects.get(owner=agent, name="Review collection")
    assert saved.sort == sort
    assert saved.filters == {k: v for k, v in params.items() if k in ("q", "view")}
    later_tab(agent)
    reopened = client.get(result.url)
    assert reopened.context["sort"] == sort and row_ids(reopened) == ids
    assert reopened.context["page_obj"].paginator.per_page == NEW["per_page"]


@pytest.mark.parametrize("initial", [None, OLD], ids=["implicit-default", "inherited-sort"])
def test_saving_a_view_preserves_another_tabs_queue_sort(client, agent, trio, initial):
    """Check the ordinary queue after following the redirect that creates a named view."""
    client.force_login(agent)
    if initial:
        set_pref(agent, initial)
    listed = queue(client, {"view": "all"})
    url, form = save_form(client, listed)
    later_tab(agent)
    result = save(client, url, form)
    assert pref(agent) == NEW  # the POST alone
    assert client.get(result.url).status_code == 200
    ordinary = queue(client)
    assert (settings_shown(ordinary), pref(agent)) == ((NEW["sort"], NEW["per_page"]), NEW)


@pytest.mark.parametrize("way", ["ticket", "cancel-save", "bulk-refusal", "bulk-success"])
def test_search_alone_keeps_later_first_time_grid_choices(client, agent, trio, way):
    """Change only Search in the rendered filter form, then set preferences in another tab."""
    client.force_login(agent)
    start = queue(client)
    toolbar = next(
        f
        for f in controls(start).forms
        if f["attrs"].get("role") == "search" and "vr-grid-toolbar" in f["attrs"].get("class", "")
    )
    params = {**dict(toolbar["hidden"]), "q": "Collection"}
    listed = client.get(toolbar["attrs"]["action"], params)
    assert row_ids(listed) and pref(agent) is None
    if way == "ticket":
        target = breadcrumb(client.get(link(listed, detail(trio[1]))))
    elif way == "cancel-save":
        _, form = save_form(client, listed)
        target = form.context["cancel_url"]
    else:
        target = bulk_url(listed)
    later_tab(agent)
    if way.startswith("bulk"):
        body = {"requests": f"{trio[0].pk}:{trio[0].revision}"}
        if way == "bulk-success":
            body.update(action="status", status=Status.WAITING_CLIENT)
        response = client.post(target, body)
        if way == "bulk-success":
            assert response.status_code == 302
            response = client.get(response.url)
        else:
            assert response.status_code == 200 and response.context["bulk_form"].errors
    else:
        response = client.get(target)
    assert response.context["q"] == "Collection" and pref(agent) == NEW
    assert settings_shown(response) == (NEW["sort"], NEW["per_page"])


@pytest.mark.parametrize("surface", ["ticket", "save-view"])
def test_accepted_queue_at_the_configured_field_limit_opens_its_own_links(
    client, agent, trio, settings, surface
):
    """Exercise Django's normal field-count boundary with six ordinary queue controls."""
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 6
    client.force_login(agent)
    params = {
        "view": "all",
        "q": "Collection",
        "status": "open",
        "page": 1,
        "per_page": 25,
        "sort": "subject",
    }
    listed = queue(client, params)
    assert row_ids(listed) == [t.pk for t in trio]
    path = detail(trio[1]) if surface == "ticket" else reverse("desk:view-create")
    opened = client.get(link(listed, path))
    assert opened.status_code == 200
    if surface == "ticket":
        assert neighbours(opened) == [detail(trio[0]), detail(trio[2])]
    else:
        assert save(client, link(listed, path), opened).status_code == 302


def test_long_valid_search_preserves_the_displayed_ticket_order(client, agent, category):
    """An accepted search and filters whose old full address fits in 2,000 characters."""
    client.force_login(agent)
    set_pref(agent, {"sort": "subject"})
    params = {
        "view": "all",
        "status": "active",
        "priority": "normal",
        "complaint": "no",
        "created_from": "2000-01-01",
        "created_to": "2099-01-01",
    }
    # Make the chosen address 1,987 characters. With '&sort=subject' it was exactly 2,000.
    rest = 1987 - len(urlencode({**params, "q": ""}))
    chars, suffix = divmod(rest, 12)
    term = "🧪" * chars + "a" * suffix
    assert len(term) <= 160
    params["q"] = term
    assert len(urlencode(params)) == 1987
    tickets = [
        new_request(agent, category, subject=f"Collection {c}", reported_by=term) for c in "ABC"
    ]
    listed = queue(client, params)
    assert row_ids(listed) == [t.pk for t in tickets]
    page = client.get(link(listed, detail(tickets[1])))
    assert neighbours(page) == [detail(tickets[0]), detail(tickets[2])]


@pytest.mark.parametrize("surface", ["ticket", "save-view"])
def test_bookmark_with_an_old_ignored_order_key_keeps_its_old_sort(client, agent, trio, surface):
    """An old queue could carry both its offered sort and an ignored order parameter."""
    client.force_login(agent)
    legacy = "view=all&order=-subject&sort=subject&per_page=50"
    if surface == "ticket":
        page = client.get(detail(trio[1]), {"list": legacy})
        assert neighbours(page) == [detail(trio[0]), detail(trio[2])]
    else:
        url = reverse("desk:view-create") + "?" + urlencode({"list": legacy})
        result = save(client, url, client.get(url))
        assert row_ids(client.get(result.url)) == [t.pk for t in trio]


@pytest.mark.parametrize("given", ["view=all", "view=all&sort=subject&per_page=50"])
def test_legacy_page_bookmark_and_session(client, agent, trio, given):
    client.force_login(agent)
    session = client.session
    session["vr_preferences"] = {"user": agent.pk, "prefs": {KEY: OLD}}
    session.save()
    set_pref(agent, NEW)
    assert settings_shown(queue(client)) == (NEW["sort"], NEW["per_page"])
    page = client.get(detail(trio[1]), {"list": given})
    has_sort = "sort=" in given
    assert neighbours(page) == (
        [detail(trio[0]), detail(trio[2])] if has_sort else [detail(trio[2]), detail(trio[0])]
    )
    assert breadcrumb(page) == reverse(QUEUE) + "?" + given
    url = reverse("desk:view-create") + "?" + urlencode({"list": given})
    form = client.get(url)
    assert fields(form)["list"] == given
    done = save(client, url, form)
    named = SavedView.objects.get(owner=agent, name="Review collection")
    assert named.filters == {"view": "all"} and named.sort == ("subject" if has_sort else "")
    assert client.get(done.url).context["sort"] == ("subject" if has_sort else NEW["sort"])


@pytest.fixture
def action_ticket(make_user, agent, category, accessioning, join):
    supervisor = make_user("queue_review_supervisor", "supervisor")
    join(supervisor, accessioning)
    tickets = [
        new_request(
            agent,
            category,
            subject=f"Collection {c}",
            team=accessioning,
            first_entry_kind=EntryKind.REPLY,
        )
        for c in "ABC"
    ]
    middle = tickets[1]
    decider = make_user("queue_review_decider", "supervisor")
    services.set_complaint(
        actor=decider,
        request=middle,
        expected_revision=middle.revision,
        is_complaint=True,
        reason="Review the collection.",
    )
    middle.refresh_from_db()
    services.decide_complaint(
        actor=decider,
        request=middle,
        expected_revision=middle.revision,
        decision=ComplaintDecision.INVESTIGATE,
        rationale="Check the collection time.",
    )
    middle.refresh_from_db()
    return supervisor, tickets


ACTIONS = [
    "request-assign",
    "request-status",
    "request-properties",
    "complaint-mark",
    "complaint-decision",
    "complaint-record",
    "complaint-review",
    "entry-withdraw",
    "request-history",
]


def action_path(name, middle):
    args = [middle.pk]
    if name == "entry-withdraw":
        args.append(middle.entries.get(kind=EntryKind.REPLY).pk)
    return reverse(f"desk:{name}", args=args)


def action_data(name, user, middle):
    return {
        "request-assign": {"assign_to": f"{middle.team_id}:{user.pk}"},
        "request-status": {"status": Status.WAITING_CLIENT},
        "request-properties": {"category": middle.category_id, "priority": "high"},
        "complaint-mark": {"reason": "Resolved categorization question."},
        "complaint-decision": {
            "decision": ComplaintDecision.NO_INVESTIGATION,
            "rationale": "Collection confirmed.",
        },
        "complaint-record": {"findings": "Collection confirmed."},
        "complaint-review": {"note": "Checked the collection."},
        "entry-withdraw": {"reason": "Correcting a synthetic reply."},
    }[name]


@pytest.mark.parametrize("name", ACTIONS)
def test_ticket_actions_return_through_the_record_and_direct_queue_breadcrumb(
    client, action_ticket, name
):
    user, tickets = action_ticket
    middle = tickets[1]
    client.force_login(user)
    set_pref(user, OLD)
    listed = queue(client, CHOICES)
    page = client.get(link(listed, detail(middle)))
    form_url = link(page, action_path(name, middle))
    form = client.get(form_url)
    assert form.status_code == 200
    direct = breadcrumb(form)
    cancel_or_back = link(form, detail(middle))
    later_tab(user)
    if name != "request-history":
        assert form.context["cancel_url"] == cancel_or_back
        body = {**fields(form), **action_data(name, user, middle)}
        changed = client.post(form_url, body)
        assert changed.status_code == 302, (
            changed.context["form"].errors if changed.context else None
        )
        assert changed.url == cancel_or_back
        before = middle.revision
        middle.refresh_from_db()
        assert middle.revision == before + 1
    back_record = client.get(cancel_or_back)
    assert neighbours(back_record) == [detail(tickets[0]), detail(tickets[2])]
    via_record = client.get(breadcrumb(back_record))
    assert via_record.context["q"] == "Collection"
    # Exercise the actual fallback page breadcrumb too. The user's search must work both ways.
    direct_list = client.get(direct)
    assert (query(direct), direct_list.context["current_view"], direct_list.context["q"]) == (
        CHOICES,
        "all",
        "Collection",
    )
    assert settings_shown(direct_list) == (NEW["sort"], NEW["per_page"]) and pref(user) == NEW


@pytest.mark.parametrize(
    "mode", ["take", "entry", "invalid-entry", "stale-entry", "stale-status", "refused-save-view"]
)
def test_inline_and_refused_forms_keep_the_drawn_list(client, action_ticket, mode):
    user, tickets = action_ticket
    middle = tickets[1]
    client.force_login(user)
    set_pref(user, OLD)
    listed = queue(client, CHOICES)
    page = client.get(link(listed, detail(middle)))
    if mode == "refused-save-view":
        url, form = save_form(client, listed)
        later_tab(user)
        refused = client.post(url, {**fields(form), "name": "", "shared_with": "private"})
        assert refused.status_code == 200 and refused.context["form"].errors
        assert not SavedView.objects.filter(owner=user).exists()
        assert fields(refused)["list"] == fields(form)["list"]
        back = refused.context["cancel_url"]
        assert back == breadcrumb(refused)
    else:
        name = "request-take" if mode == "take" else "request-entry"
        if mode == "stale-status":
            name = "request-status"
            url = link(page, action_path(name, middle))
            payload = {**fields(client.get(url)), "status": Status.WAITING_CLIENT}
        else:
            url = action_path(name, middle)
            payload = fields(page, action=url)
            if name == "request-entry":
                payload.update(
                    kind=EntryKind.NOTE, body="" if mode == "invalid-entry" else "Collection draft."
                )
        other = later_tab(user)
        if mode.startswith("stale"):
            other_page = other.get(detail(middle))
            composer = action_path("request-entry", middle)
            advanced = other.post(
                composer,
                {
                    **fields(other_page, action=composer),
                    "kind": EntryKind.NOTE,
                    "body": "Confirmed from another tab.",
                },
            )
            assert advanced.status_code == 302
        response = client.post(url, payload)
        if mode == "stale-status":
            assert response.status_code == 200 and "changed" in str(response.context["form"].errors)
            assert fields(response)["expected_revision"] == str(middle.revision)
            assert response.context["form"]["status"].value() == Status.WAITING_CLIENT
            returned = client.get(response.context["cancel_url"])
        elif mode in ("invalid-entry", "stale-entry"):
            assert response.status_code == 400 and response.context["composer"].errors
            if mode == "stale-entry":
                assert "changed" in str(response.context["composer"].errors)
                assert response.context["composer"]["body"].value() == "Collection draft."
                assert fields(response, action=url)["expected_revision"] == str(middle.revision)
            returned = response
        else:
            assert response.status_code == 302
            returned = client.get(response.url)
        before = middle.revision
        middle.refresh_from_db()
        assert middle.revision == before + int(mode != "invalid-entry")
        if mode == "stale-entry":
            assert not middle.entries.filter(body="Collection draft.").exists()
        assert neighbours(returned) == [detail(tickets[0]), detail(tickets[2])]
        back = breadcrumb(returned)
    reopened = client.get(back)
    assert (query(back), settings_shown(reopened), pref(user)) == (
        CHOICES,
        (NEW["sort"], NEW["per_page"]),
        NEW,
    )


def test_action_that_removes_ticket_from_readers_queue_returns_to_chosen_address(
    client, agent, category, privacy_category
):
    ticket = new_request(agent, category, subject="Collection schedule")
    set_pref(agent, OLD)
    client.force_login(agent)
    page = client.get(link(queue(client, CHOICES), detail(ticket)))
    url = link(page, action_path("request-properties", ticket))
    form = client.get(url)
    later_tab(agent)
    moved = client.post(
        url, {**fields(form), "category": privacy_category.pk, "priority": "normal"}
    )
    assert moved.status_code == 302 and urlsplit(moved.url).path == reverse(QUEUE)
    ticket.refresh_from_db()
    assert ticket.category_id == privacy_category.pk
    assert client.get(detail(ticket)).status_code == 404
    back = client.get(moved.url)
    assert ticket.pk not in row_ids(back)
    assert (query(moved.url), settings_shown(back), pref(agent)) == (
        CHOICES,
        (NEW["sort"], NEW["per_page"]),
        NEW,
    )


@pytest.mark.parametrize("outcome", ["success", "refusal"])
@pytest.mark.parametrize("choice", ["no-settings", "inherited", "mixed", "explicit"])
def test_bulk_post_and_following_get_are_separate(client, agent, trio, choice, outcome):
    client.force_login(agent)
    if choice != "no-settings":
        set_pref(agent, OLD)
    overrides = {
        "no-settings": {},
        "inherited": {},
        "mixed": {"per_page": "50"},
        "explicit": {"sort": "number", "per_page": "25"},
    }[choice]
    params = {**CHOICES, **overrides}
    listed = queue(client, params)
    target = bulk_url(listed)
    if choice != "no-settings":
        later_tab(agent)
    before = list(
        UserPreference.objects.filter(user=agent, key=KEY).values_list("value", "updated_at")
    )
    middle = trio[1]
    body = {"requests": f"{middle.pk}:{middle.revision}"}
    if outcome == "success":
        body.update(action="status", status=Status.WAITING_CLIENT)
    submitted = client.post(target, body)
    assert (
        list(UserPreference.objects.filter(user=agent, key=KEY).values_list("value", "updated_at"))
        == before
    )
    middle.refresh_from_db()
    if outcome == "success":
        assert submitted.status_code == 302 and middle.status == Status.WAITING_CLIENT
        back = client.get(submitted.url)
        destination = submitted.url
    else:
        assert submitted.status_code == 200 and submitted.context["bulk_form"].errors
        assert middle.status == Status.OPEN and submitted.context["selected_rows"] == {
            body["requests"]
        }
        back = submitted
        destination = bulk_url(back)
    sort = overrides.get("sort", DEFAULT_SORT if choice == "no-settings" else NEW["sort"])
    size = int(overrides.get("per_page", 25 if choice == "no-settings" else NEW["per_page"]))
    assert (query(target), query(destination), settings_shown(back)) == (
        params,
        params,
        (sort, size),
    )
    page = client.get(link(back, detail(middle)))
    ids = row_ids(back)
    index = ids.index(middle.pk)
    around = ids[max(0, index - 1) : index] + ids[index + 1 : index + 2]
    assert neighbours(page) == [reverse("desk:request-detail", args=[pk]) for pk in around]
    assert query(breadcrumb(page)) == params
    form_url, form = save_form(client, back)
    saved = save(client, form_url, form)
    assert client.get(saved.url).context["sort"] == sort


@pytest.mark.parametrize(
    "legacy,valid", [(True, True), (True, False), (False, True), (False, False)]
)
def test_legacy_bulk_and_unknown_view(client, agent, trio, legacy, valid):
    client.force_login(agent)
    set_pref(agent, OLD)
    target = reverse("desk:request-bulk") if legacy else reverse(QUEUE)
    # Old forms use the local 'next' destination. An unrecognized system view is normalized first.
    params = {"q": "Collection"} if legacy else {"view": "obsolete", "q": "Collection"}
    middle = trio[1]
    local = reverse(QUEUE) + "?view=all&q=Collection"
    body = {"requests": f"{middle.pk}:{middle.revision}", "next": local}
    if valid:
        body.update(action="status", status=Status.WAITING_CLIENT)
    done = client.post(target + "?" + urlencode(params), body)
    assert done.status_code == 302 and pref(agent) == OLD
    middle.refresh_from_db()
    assert middle.status == (Status.WAITING_CLIENT if legacy and valid else Status.OPEN)
    assert urlsplit(done.url).path == reverse(QUEUE)
    assert query(done.url) == (query(local) if legacy else {"q": "Collection"})
    assert settings_shown(client.get(done.url)) == (OLD["sort"], OLD["per_page"])


def test_queue_post_reads_only_own_list_key(client, agent, make_user, trio):
    other = make_user("queue_review_other", "technical")
    set_pref(other, NEW)
    UserPreference.objects.create(
        user=agent, key="page:clients:client-list", value={"sort": "name", "per_page": 50}
    )
    client.force_login(agent)
    path = reverse(QUEUE)
    # Warm the shell (which separately acknowledges What's new).
    queue(client)
    rows = UserPreference.objects.filter(key__startswith="page:").order_by("pk")
    before = list(rows.values_list("pk", "value", "updated_at"))
    response = client.post(path + "?q=Collection", {})
    assert settings_shown(response) == (DEFAULT_SORT, 25)
    assert not response.wsgi_request.GET.get("sort")
    set_pref(agent, OLD)
    after = list(rows.values_list("pk", "value", "updated_at"))
    assert after[:-1] == before
    response = client.post(path + "?view=all", {})
    assert settings_shown(response) == (OLD["sort"], OLD["per_page"])
    old_endpoint = client.post(reverse("desk:request-bulk"))
    form = client.post(reverse("desk:view-create"), {"name": "", "list": "view=all"})
    for nonlist in [old_endpoint, form]:
        assert "sort" not in nonlist.wsgi_request.GET and "per_page" not in nonlist.wsgi_request.GET
    assert list(rows.values_list("pk", "value", "updated_at")) == after


@pytest.mark.parametrize("role", [*ROLES, "staff-without-role", "portal", "anonymous", "member"])
def test_direct_addresses_respect_the_readers_ticket_scope(
    client, agent, category, privacy_category, quality, join, clinic_portal, make_user, role
):
    public = new_request(agent, category, subject="Collection A")
    hidden = new_request(agent, privacy_category, subject="Collection B")
    last = new_request(agent, category, subject="Collection C")
    named = SavedView.objects.create(
        owner=agent, name="Private review collection", filters={"view": "all"}, sort="subject"
    )
    if role == "portal":
        user = clinic_portal
    elif role == "anonymous":
        user = None
    elif role == "staff-without-role":
        user = make_user("queue_review_norole", is_staff=True)
    elif role == "member":
        user = make_user("queue_review_member", "technical")
        join(user, quality)
    else:
        user = make_user("queue_review_reader", role)
    if user:
        client.force_login(user)
    can_open = role in ROLES or role == "member"
    can_read_restricted = role in ("admin", "supervisor", "member")
    allowed = {public.pk, last.pk}
    if can_read_restricted:
        allowed.add(hidden.pk)
    variants = [
        {"view": "all", "saved": named.pk, "sort": "subject", "order": "subject"},
        {"view": "all", "category": privacy_category.pk, "sort": "number", "order": "number"},
        {"view": "all", "complaint": "review"},
        {"view": "all", "status": "no-such-status"},
        {"view": "all", "q": "search" * 42, "order": "retired_column"},
        {"view": "all", "saved": "unused" * 345, "q": "%ZZ", "order": "-number"},
    ]
    for params in variants:
        carried = {"list": urlencode(params)}
        listed = client.get(reverse(QUEUE), {**params, **carried})
        public_page = client.get(detail(public), carried)
        restricted_page = client.get(detail(hidden), carried)
        form = client.get(reverse("desk:view-create"), carried)
        if not can_open:
            for page in (listed, public_page, restricted_page, form):
                assert page.status_code == (302 if user is None else 403)
                if user is None:
                    assert "next" in query(page.url)
            continue
        assert listed.status_code == public_page.status_code == form.status_code == 200
        assert restricted_page.status_code == (200 if can_read_restricted else 404)
        assert set(row_ids(listed)) <= allowed
        if params.get("saved") == named.pk:
            assert not listed.context["saved_views"] and named.name.encode() not in listed.content
            expected_ids = (
                [public.pk, hidden.pk, last.pk] if can_read_restricted else [public.pk, last.pk]
            )
            assert row_ids(listed) == expected_ids
            assert neighbours(public_page) == [
                reverse("desk:request-detail", args=[expected_ids[1]])
            ]
        if "category" in params:
            assert row_ids(listed) == ([hidden.pk] if can_read_restricted else [])
        for page in [public_page, restricted_page] if can_read_restricted else [public_page]:
            for href in Navigation(page, "Tickets in this list").links:
                assert urlsplit(href).path in {
                    reverse("desk:request-detail", args=[pk]) for pk in allowed
                }
                assert client.get(href).status_code == 200
    if can_open:
        assert (
            client.get(reverse("desk:view-create"), {"list": "view=all&q=%ZZ&order=%"}).status_code
            == 200
        )


@pytest.mark.parametrize(
    "role", ["client_services", "account_manager", "technical", "admin", "supervisor"]
)
def test_awaiting_review_filter_agrees_with_who_can_review(client, action_ticket, make_user, role):
    _, tickets = action_ticket
    ticket = tickets[1]
    reader = make_user("independent_reader", role)
    client.force_login(reader)
    params = {"view": "all", "complaint": "review"}
    listed = queue(client, params)
    direct = client.get(action_path("complaint-review", ticket))
    may_review = role in ("admin", "supervisor")
    assert direct.status_code == (200 if may_review else 403)
    assert row_ids(listed) == ([ticket.pk] if may_review else [])


@pytest.mark.parametrize("mode", ["ticket", "bulk", "cancel-save"])
def test_unqualified_release_note_about_preserving_later_choices(client, agent, trio, mode):
    """Explicit choices on the list are sent back and saved again (F9: the release note and
    docs/architecture.md now say a way back puts back only what the person chose there)."""
    client.force_login(agent)
    listed = queue(client, {**CHOICES, **OLD})
    if mode == "ticket":
        target = breadcrumb(client.get(link(listed, detail(trio[1]))))
    elif mode == "cancel-save":
        _, form = save_form(client, listed)
        target = form.context["cancel_url"]
    else:
        target = bulk_url(listed)
    later_tab(agent)
    if mode == "bulk":
        changed = client.post(
            target,
            {
                "requests": f"{trio[1].pk}:{trio[1].revision}",
                "action": "status",
                "status": Status.WAITING_CLIENT,
            },
        )
        assert changed.status_code == 302 and pref(agent) == NEW
        target = changed.url
    back = client.get(target)
    assert settings_shown(back) == (
        OLD["sort"],
        OLD["per_page"],
    )  # page help gives explicit choices precedence
    # Adapted (F9): what the person chose on the list is their choice again when it comes back, so
    # it is saved; the reworded release note promises this only for what they did not choose.
    assert pref(agent) == OLD


def test_bulk_stale_selection_is_not_checked_at_new_revision(client, agent, trio):
    client.force_login(agent)
    set_pref(agent, OLD)
    listed = queue(client, CHOICES)
    target = bulk_url(listed)
    middle = trio[1]
    old_row = f"{middle.pk}:{middle.revision}"
    other = later_tab(agent)
    record = other.get(detail(middle))
    composer = action_path("request-entry", middle)
    advanced = other.post(
        composer,
        {
            **fields(record, action=composer),
            "kind": EntryKind.NOTE,
            "body": "Updated while queue was open.",
        },
    )
    assert advanced.status_code == 302
    redraw = client.post(
        target,
        {
            "requests": [old_row, f"{trio[0].pk}:{trio[0].revision}"],
            "action": "status",
            "status": Status.WAITING_CLIENT,
        },
    )
    assert redraw.status_code == 200 and redraw.context["bulk_form"].errors
    assert all(
        status == Status.OPEN for status in ServiceRequest.objects.values_list("status", flat=True)
    )

    # Inspect the actual row controls, including the checkboxes outside the bulk form.
    class Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.checked = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("name") == "requests" and attrs.get("value"):
                self.checked[attrs["value"]] = "checked" in attrs

    rows = Rows()
    rows.feed(redraw.content.decode())
    middle.refresh_from_db()
    assert old_row not in rows.checked
    assert rows.checked[f"{middle.pk}:{middle.revision}"] is False
    assert rows.checked[f"{trio[0].pk}:{trio[0].revision}"] is True
    assert settings_shown(redraw) == (NEW["sort"], NEW["per_page"]) and pref(agent) == NEW


def test_find_and_ticket_link_form_do_not_claim_a_queue(client, agent, trio):
    client.force_login(agent)
    set_pref(agent, OLD)
    ticket = trio[1]
    find = reverse("desk:request-find")
    by_number = client.get(find, {"q": ticket.number})
    results = client.get(find, {"q": "Collection"})
    for url in [by_number.url, link(results, detail(ticket))]:
        assert url == detail(ticket)
        opened = client.get(url)
        assert not neighbours(opened) and breadcrumb(opened) == reverse(QUEUE)
        assert settings_shown(client.get(breadcrumb(opened))) == (OLD["sort"], OLD["per_page"])
    from_queue = client.get(link(queue(client, CHOICES), detail(ticket)))
    add = link(from_queue, reverse("link_records:request-link-add", args=[ticket.pk]))
    form = client.get(add)
    assert form.status_code == 200 and "list" not in query(add)
    assert urlsplit(form.context["cancel_url"]).path == detail(ticket)
    assert "list" not in query(form.context["cancel_url"])


def test_explicit_system_view_is_kept_in_spite_of_all_tickets_help(client, agent, trio, category):
    """A view saved from an explicitly chosen My open keeps it (F11: the queue help now says so)."""
    extra = new_request(agent, category, subject="Collection D")
    client.force_login(agent)
    listed = queue(client, {"view": "my-open"})
    url, form = save_form(client, listed)
    reopened = client.get(save(client, url, form).url)
    assert set(row_ids(listed)) == {t.pk for t in trio}
    assert extra.pk in row_ids(queue(client, {"view": "all"}))
    # No Assigned to Me or Open or waiting filter was set. Adapted (F11): the help now says the
    # view keeps the chosen My open, so the unassigned ticket stays out.
    assert reopened.context["current_view"] == "my-open"
    assert extra.pk not in row_ids(reopened)


def test_changed_ticket_uses_its_current_position_in_the_original_sort(client, agent, trio):
    """A status change moves the middle ticket to newest, even after another tab changes sort."""
    client.force_login(agent)
    listed = queue(client, {"view": "all"})
    middle = trio[1]
    assert row_ids(listed) == [t.pk for t in reversed(trio)]
    page = client.get(link(listed, detail(middle)))
    assert neighbours(page) == [detail(trio[2]), detail(trio[0])]
    url = link(page, action_path("request-status", middle))
    form = client.get(url)
    later_tab(agent)
    done = client.post(url, {**fields(form), "status": Status.WAITING_CLIENT})
    assert done.status_code == 302
    returned = client.get(done.url)
    assert neighbours(returned) == [detail(trio[2])] and not returned.context["previous_url"]
    assert settings_shown(client.get(breadcrumb(returned))) == (NEW["sort"], NEW["per_page"])


def test_direct_crumb_from_refused_status_form(client, agent, trio):
    client.force_login(agent)
    set_pref(agent, OLD)
    page = client.get(link(queue(client, CHOICES), detail(trio[1])))
    url = link(page, action_path("request-status", trio[1]))
    form = client.get(url)
    later_tab(agent)
    refused = client.post(url, fields(form))  # no new status
    assert refused.status_code == 200 and refused.context["form"]["status"].errors
    back = client.get(breadcrumb(refused))
    assert (back.context["current_view"], back.context["q"], settings_shown(back)) == (
        "all",
        "Collection",
        (NEW["sort"], NEW["per_page"]),
    )


def test_predeployment_bulk_form_still_uses_its_full_address(client, agent, trio):
    """An already-rendered bulk form cannot distinguish the provenance of its old query values."""
    client.force_login(agent)
    set_pref(agent, OLD)
    old_url = reverse(QUEUE) + "?" + urlencode({**CHOICES, **OLD})
    later_tab(agent)
    middle = trio[1]
    changed = client.post(
        old_url,
        {
            "requests": f"{middle.pk}:{middle.revision}",
            "action": "status",
            "status": Status.WAITING_CLIENT,
        },
    )
    assert changed.status_code == 302 and pref(agent) == NEW
    assert query(changed.url) == query(old_url)
    assert settings_shown(client.get(changed.url)) == (OLD["sort"], OLD["per_page"])
    assert pref(agent) == OLD


def test_invalid_filter_refuses_to_create_view_in_either_format(client, agent):
    client.force_login(agent)
    for carried in ("view=all&status=obsolete", "view=all&status=obsolete&order=subject"):
        path = reverse("desk:view-create")
        form = client.get(path, {"list": carried})
        response = client.post(
            path, {**fields(form), "name": "Collection draft", "shared_with": "private"}
        )
        assert response.status_code == 200 and response.context["form"].errors
        assert not SavedView.objects.filter(owner=agent).exists()
        assert fields(response)["list"] == carried


def test_long_address_retains_the_last_chosen_status_filter(client, agent, category):
    """An ordinary filter after a multibyte search must still narrow the queue from the ticket."""
    client.force_login(agent)
    search = "🧪" * 157 + "集aa"
    params = {
        "view": "all",
        "sort": "number",
        "priority": "normal",
        "complaint": "no",
        "created_from": "2000-01-01",
        "created_to": "2099-01-01",
        "page": "1",
        "q": search,
    }
    assert len(search) == 160 and len(urlencode(params)) == 2000
    tickets = [
        new_request(agent, category, reported_by=search, subject=f"Collection {c}") for c in "ABC"
    ]
    services.change_status(
        actor=agent,
        request=tickets[1],
        expected_revision=tickets[1].revision,
        status=Status.WAITING_CLIENT,
    )
    params["status"] = "open"
    listed = queue(client, params)
    assert row_ids(listed) == [tickets[0].pk, tickets[2].pk]
    opened = client.get(link(listed, detail(tickets[0])))
    back = client.get(breadcrumb(opened))
    assert (neighbours(opened), row_ids(back)) == ([detail(tickets[2])], row_ids(listed))


def test_restricted_ticket_on_a_genuinely_bare_queue(client, agent, category, privacy_category):
    tickets = [
        new_request(agent, cat, subject=f"Collection {c}")
        for cat, c in [(category, "A"), (privacy_category, "B"), (category, "C")]
    ]
    # A synthetic legacy assignment makes the restricted row match My open. Only scope excludes it.
    ServiceRequest.objects.filter(pk__in=[t.pk for t in tickets]).update(assignee=agent)
    client.force_login(agent)
    listed = queue(client)
    assert pref(agent) is None and listed.wsgi_request.META["QUERY_STRING"] == ""
    assert row_ids(listed) == [tickets[2].pk, tickets[0].pk]
    assert client.get(detail(tickets[1])).status_code == 404
    opened = client.get(link(listed, detail(tickets[2])))
    assert neighbours(opened) == [detail(tickets[0])]


def test_first_bulk_refusal_saves_only_the_ordinary_shell_acknowledgment(client, agent):
    from core.preferences import WHATS_NEW_KEY

    client.force_login(agent)
    stored = UserPreference.objects.filter(user=agent)
    assert not stored.exists()
    response = client.post(reverse(QUEUE) + "?sort=subject&per_page=50", {})
    assert response.status_code == 200 and response.context["bulk_form"].errors
    assert settings_shown(response) == ("subject", 50)
    assert set(stored.values_list("key", flat=True)) == {WHATS_NEW_KEY}
