"""The staff queue's address on its way to a ticket and back (roadmap: "Review follow-up: a
ticket's way back to the queue"). A ticket opened from the queue carries what the person chose and
the order the queue showed: the way back sends only the choices, so the queue's saved page size and
sort apply as they stand on arrival; Previous, Next and Save view keep the order shown. Synthetic
accounts and tickets."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client as Browser
from django.urls import reverse

from core.models import UserPreference
from desk.lists import DEFAULT_SORT
from desk.models import SavedView, ServiceRequest, Status

from .test_contract import new_request
from .test_review_preferences_in_session_confirm import _list_controls

pytestmark = pytest.mark.django_db

QUEUE_KEY = "page:desk:request-list"
SAVED = {"sort": "subject", "per_page": 50}


@pytest.fixture
def tickets(agent, category):
    """Three tickets: by subject A, B, C; by last change (the default order) C, B, A."""
    made = [new_request(agent, category, subject=f"Collection {letter}") for letter in "ABC"]
    ServiceRequest.objects.filter(pk__in=[t.pk for t in made]).update(assignee=agent)
    return made


def _detail(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def _queue(client, params=None):
    page = client.get(reverse("desk:request-list"), params or {})
    assert page.status_code == 200
    return page, _list_controls(page.content.decode())


def _link_to(controls, path):
    return next(a["href"] for a in controls.links if urlsplit(a["href"]).path == path)


def _carried(href):
    """The `list` value a link carries, as a dict of lists."""
    return parse_qs(parse_qs(urlsplit(href).query)["list"][0])


def _query(url):
    return parse_qs(urlsplit(url).query)


@pytest.mark.parametrize(
    "saved,chosen,carried,order",
    [
        (SAVED, {}, {"order": ["subject"]}, "ABC"),
        (SAVED, {"q": "Collection"}, {"q": ["Collection"], "order": ["subject"]}, "ABC"),
        (SAVED, {"sort": "-subject"}, {"sort": ["-subject"], "order": ["-subject"]}, "CBA"),
        (None, {}, {"order": [DEFAULT_SORT]}, "CBA"),
        # A sort the queue does not offer shows the default order, which the ticket keeps; the way
        # back sends the address as it was chosen.
        (None, {"sort": "bogus"}, {"sort": ["bogus"], "order": [DEFAULT_SORT]}, "CBA"),
        # An `order` typed into the queue's address is replaced by the order shown.
        (
            None,
            {"order": "subject", "q": "Collection"},
            {"q": ["Collection"], "order": [DEFAULT_SORT]},
            "CBA",
        ),
    ],
    ids=["saved", "saved-and-filter", "chosen-sort", "default", "unknown-sort", "typed-order"],
)
def test_a_ticket_keeps_the_order_shown_and_goes_back_to_the_choices(
    client, agent, tickets, saved, chosen, carried, order
):
    if saved:
        UserPreference.objects.create(user=agent, key=QUEUE_KEY, value=saved)
    client.force_login(agent)
    queue, controls = _queue(client, chosen)
    by_letter = {t.subject[-1]: t for t in tickets}
    assert [t.pk for t in queue.context["page_obj"]] == [by_letter[x].pk for x in order]
    middle = by_letter[order[1]]
    link = _link_to(controls, _detail(middle))
    assert _carried(link) == carried  # no page size, nor a sort, from the saved settings
    page = client.get(link)
    assert page.status_code == 200
    assert [urlsplit(page.context[k]).path for k in ("previous_url", "next_url")] == [
        _detail(by_letter[order[0]]),
        _detail(by_letter[order[2]]),
    ]
    # Previous and Next carry the same value, so the next ticket keeps the order too.
    assert _carried(page.context["next_url"]) == carried
    back = page.context["list_url"]
    assert any(a["href"] == back for a in _list_controls(page.content.decode()).links)
    assert urlsplit(back).path == reverse("desk:request-list")
    expected = {k: [str(v)] for k, v in chosen.items() if k != "order"}
    assert _query(back) == expected


def test_the_way_back_follows_the_saved_settings_as_they_stand_on_arrival(client, agent, tickets):
    row = UserPreference.objects.create(user=agent, key=QUEUE_KEY, value=SAVED)
    client.force_login(agent)
    _, controls = _queue(client, {"view": "all"})
    page = client.get(_link_to(controls, _detail(tickets[1])))
    other = Browser()
    other.force_login(agent)
    newer = {"sort": "-subject", "per_page": 100}
    assert other.get(reverse("desk:request-list"), newer).status_code == 200
    back = client.get(page.context["list_url"])
    assert (back.context["sort"], back.context["per_page"]) == ("-subject", 100)
    assert back.context["current_view"] == "all"
    row.refresh_from_db()
    assert row.value == newer


def test_the_ticket_pages_actions_carry_the_queue_through(client, agent, tickets):
    UserPreference.objects.create(user=agent, key=QUEUE_KEY, value=SAVED)
    client.force_login(agent)
    _, controls = _queue(client, {"q": "Collection"})
    middle = tickets[1]
    page = client.get(_link_to(controls, _detail(middle)))
    status_path = reverse("desk:request-status", args=[middle.pk])
    form_page = client.get(_link_to(_list_controls(page.content.decode()), status_path))
    assert form_page.status_code == 200
    hidden = next(
        dict(f["hidden"])
        for f in _list_controls(form_page.content.decode()).forms
        if "list" in dict(f["hidden"])
    )
    changed = client.post(status_path, {**hidden, "status": Status.WAITING_CLIENT})
    assert changed.status_code == 302
    middle.refresh_from_db()
    assert middle.status == Status.WAITING_CLIENT
    again = client.get(changed.url)
    assert urlsplit(again.context["previous_url"]).path == _detail(tickets[0])
    assert urlsplit(again.context["next_url"]).path == _detail(tickets[2])
    assert _query(again.context["list_url"]) == {"q": ["Collection"]}


def test_a_page_opened_before_the_order_was_carried_keeps_its_full_address(client, agent, tickets):
    client.force_login(agent)
    earlier = "view=all&sort=subject&per_page=50"
    page = client.get(_detail(tickets[1]), {"list": earlier})
    assert [urlsplit(page.context[k]).path for k in ("previous_url", "next_url")] == [
        _detail(tickets[0]),
        _detail(tickets[2]),
    ]
    assert page.context["list_url"] == f"{reverse('desk:request-list')}?{earlier}"


def test_a_ticket_not_opened_from_the_queue_has_no_previous_or_next(client, agent, tickets):
    client.force_login(agent)
    page = client.get(_detail(tickets[1]))
    assert page.context["previous_url"] == page.context["next_url"] == ""
    assert page.context["list_url"] == reverse("desk:request-list")


def test_previous_and_next_from_a_bare_queue_skip_restricted_tickets(
    client, agent, category, make_user, privacy_category
):
    first = new_request(agent, category, subject="Open collection")
    new_request(agent, privacy_category, subject="Copy of records RECTEST")
    last = new_request(agent, category, subject="Later collection")
    outsider = make_user("outsider", "technical")
    client.force_login(outsider)
    queue, controls = _queue(client, {"view": "all", "sort": "number"})
    assert [t.pk for t in queue.context["page_obj"]] == [first.pk, last.pk]
    page = client.get(_link_to(controls, _detail(first)))
    assert urlsplit(page.context["next_url"]).path == _detail(last)
    assert page.context["previous_url"] == ""


@pytest.mark.parametrize(
    "list_value,stored_sort",
    [
        ("order=subject", "subject"),
        ("view=all&q=Collection&order=subject", "subject"),
        ("view=all&sort=-subject&order=-subject", "-subject"),
        ("view=all", ""),  # a link from before the order was carried, as earlier
    ],
)
def test_save_view_stores_the_order_shown_and_cancels_to_the_choices(
    client, agent, list_value, stored_sort
):
    client.force_login(agent)
    path = reverse("desk:view-create")
    form = client.get(path, {"list": list_value})
    assert form.status_code == 200
    cancel = form.context["cancel_url"]
    assert "order" not in _query(cancel) and "per_page" not in _query(cancel)
    assert _query(cancel) == {k: v for k, v in parse_qs(list_value).items() if k != "order"}
    hidden = next(
        dict(f["hidden"])
        for f in _list_controls(form.content.decode()).forms
        if "list" in dict(f["hidden"])
    )
    saved = client.post(path, {**hidden, "name": "Collections", "shared_with": "private"})
    assert saved.status_code == 302
    view = SavedView.objects.get(owner=agent, name="Collections")
    assert view.sort == stored_sort
    assert "order" not in view.filters


def test_a_refused_bulk_change_draws_the_queue_as_its_address_and_saved_settings_stand(
    client, agent, tickets
):
    row = UserPreference.objects.create(user=agent, key=QUEUE_KEY, value=SAVED)
    client.force_login(agent)
    _, controls = _queue(client, {"q": "Collection"})
    action = next(
        f["attrs"]["action"] for f in controls.forms if f["attrs"].get("id") == "bulk-form"
    )
    assert _query(action) == {"q": ["Collection"]}  # not the values from the saved settings
    other = Browser()
    other.force_login(agent)
    newer = {"sort": "-subject", "per_page": 100}
    assert other.get(reverse("desk:request-list"), newer).status_code == 200
    selected = f"{tickets[0].pk}:{tickets[0].revision}"
    redraw = client.post(action, {"requests": selected})  # no action chosen: refused
    assert redraw.status_code == 200 and redraw.context["bulk_form"].errors
    assert (redraw.context["sort"], redraw.context["per_page"]) == ("-subject", 100)
    assert redraw.context["selected_rows"] == {selected}
    again = _list_controls(redraw.content.decode())
    assert _carried(_link_to(again, _detail(tickets[0]))) == {
        "q": ["Collection"],
        "order": ["-subject"],
    }
    redraw_action = next(
        f["attrs"]["action"] for f in again.forms if f["attrs"].get("id") == "bulk-form"
    )
    assert _query(redraw_action) == {"q": ["Collection"]}
    row.refresh_from_db()
    assert row.value == newer  # a post saves nothing


def test_a_bulk_change_returns_to_the_chosen_page_and_sort(client, agent, tickets):
    row = UserPreference.objects.create(user=agent, key=QUEUE_KEY, value=SAVED)
    client.force_login(agent)
    chosen = {"q": "Collection", "sort": "-subject", "page": "1"}
    _, controls = _queue(client, chosen)
    row.refresh_from_db()
    assert row.value == {"sort": "-subject", "per_page": 50}
    action = next(
        f["attrs"]["action"] for f in controls.forms if f["attrs"].get("id") == "bulk-form"
    )
    done = client.post(
        action,
        {
            "action": "status",
            "status": Status.WAITING_CLIENT,
            "requests": [f"{t.pk}:{t.revision}" for t in tickets[:1]],
        },
    )
    assert done.status_code == 302
    assert urlsplit(done.url).path == reverse("desk:request-list")
    assert _query(done.url) == {k: [v] for k, v in chosen.items()}
    back = client.get(done.url)
    assert (back.context["sort"], back.context["per_page"]) == ("-subject", 50)
