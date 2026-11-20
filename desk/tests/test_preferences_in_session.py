"""Preferences read for each request, and the limit on a person's saved preferences, under races.

Roadmap item "Review follow-up: preferences kept in the session". Its acceptance cases are the
review's V2 and V3 tests (round-1 verification of "the jobs' account's first use and a page drawn
across a change"), adopted below with the changes their comments give; the rest are their
neighbours. All records are synthetic.

V2: each signed-in session kept a copy of the person's preferences, and two tabs answering at the
same moment each saved their own copy, so the last one put back an older value. Preferences are
now read from the database for each request and nothing is kept in the session.

V3: two additions at the same moment each counted the person's preferences before either added
its row, so both could take the last place. An addition now counts and inserts holding a lock of
that person's additions (core.preferences.PREFERENCE_ADDITIONS).

The races run each party in its own PostgreSQL session (desk.tests.test_review_assignment_lock_
order_verify3); a party holds its transaction open, or is paused at a named point, until the other
waits for a lock or has finished.
"""

import json
from datetime import date
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client as Browser
from django.urls import reverse

from core import changelog, preferences
from core.models import UserPreference
from desk.notifications import OPT_OUT_KEY

from .test_contract import new_request
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, paused

pytestmark = pytest.mark.django_db(transaction=True)

LAYOUT = {"order": ["name", "account_number"], "hidden": ["state"]}
OTHER_LAYOUT = {"order": ["account_number", "name"], "hidden": []}


@pytest.fixture(autouse=True)
def local_mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def ticket(agent, category, clinic):
    return new_request(agent, category, client=clinic)


def detail(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def post(browser, key, value):
    return browser.post(
        reverse("preference"),
        json.dumps({"key": key, "value": value}),
        content_type="application/json",
    )


def page_prefs(browser, path):
    """The preferences a page hands to its scripts ({% client_preferences %})."""
    html = browser.get(path).content.decode()
    tag = '<script id="vr-preferences" type="application/json">'
    start = html.index(tag) + len(tag)
    return json.loads(html[start : html.index("</script>", start)])


def same_session(browser):
    """A second tab of `browser`: another client sending the same session cookie."""
    tab = Browser()
    tab.cookies = browser.cookies.copy()
    return tab


def edition(day):
    return changelog.Entry(
        id=f"2026-11-{day}-delivery",
        date=date(2026, 11, day),
        type="changed",
        impact="low",
        title="Delivery hours",
        summary="New collection hours.",
    )


def fill(user, count):
    UserPreference.objects.bulk_create(
        UserPreference(user=user, key=f"page:list{n}", value={"per_page": 25}) for n in range(count)
    )


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def committed(work):
    """`work` in another PostgreSQL session, run to its end; its result."""
    writer = Session(work).start()
    assert writer.join() == "done", writer.error
    return writer.result


def waits_for(holder_work, work):
    """`work` (its own session) while another session holds what `holder_work` took, in a
    transaction kept open: whether `work` waited for that session, and its Session once ended."""
    holder = Held(holder_work)
    other = Session(work).start()
    try:
        state = other.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(other.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.error
        other.join()
    return state, blocked, other


# V2 and its neighbours: nothing kept in the session ---------------------------------------------


def test_session_mark_after_first_ticket_and_explicit_visit_overlap(client, agent, ticket):
    """V2, adopted. A second tab acknowledges a release after the first tab's first mark is
    stored, before the first tab's response (and its session) is saved. Adapted: paused after
    save_if_absent, which stores the mark, instead of after the session copy's helper, which no
    longer exists; on the base this is the same moment."""
    client.force_login(agent)
    other = same_session(client)
    editions = [edition(20), edition(19)]
    mark = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)
    visits = []

    def acknowledge():
        assert mark.get().value == changelog.seen_marker(editions[1:])
        with patch.object(changelog, "entries", return_value=editions):
            visits.append(committed(lambda: other.get(reverse("whats-new")).status_code))
        assert mark.get().value == changelog.seen_marker(editions)

    with (
        patch.object(changelog, "entries", return_value=editions[1:]),
        paused(preferences, "save_if_absent", acknowledge, before=False),
    ):
        assert client.get(detail(ticket)).status_code == 200
    assert visits == [200] and mark.get().value == changelog.seen_marker(editions)
    with patch.object(changelog, "entries", return_value=editions):
        next_page = client.get(detail(ticket))
        assert next_page.status_code == 200
        assert b'data-vr-unread="1"' not in next_page.content


def test_a_layout_saved_in_another_tab_while_a_page_is_drawn_shows_on_the_next_page(client, agent):
    client.force_login(agent)
    tab = same_session(client)
    saved = []

    def save_in_the_other_tab():
        saved.append(committed(lambda: post(tab, "grid:clients.clients:v1", LAYOUT).status_code))

    with paused(preferences, "for_request", save_in_the_other_tab, before=False):
        assert client.get(reverse("dashboard")).status_code == 200
    assert saved == [204]
    assert page_prefs(client, reverse("clients:client-list")) == {"grid:clients.clients:v1": LAYOUT}
    assert page_prefs(tab, reverse("clients:client-list")) == {"grid:clients.clients:v1": LAYOUT}


def test_two_tabs_saving_different_preferences_at_the_same_moment_keep_both(client, agent):
    client.force_login(agent)
    tab = same_session(client)
    url = reverse("clients:client-list")
    saved = []

    def collapse_in_the_other_tab():
        saved.append(committed(lambda: post(tab, "nav.collapsed", True).status_code))

    # The first tab saves its page size (the list middleware) while the other tab collapses the
    # sidebar; each request ends after the other's change committed.
    with paused(preferences, "save_combined", collapse_in_the_other_tab, before=False):
        assert client.get(url, {"per_page": 50}).status_code == 200
    assert saved == [204]
    for browser in (client, tab):
        assert page_prefs(browser, url) == {"nav.collapsed": True}
        assert browser.get(url).context["page_obj"].paginator.per_page == 50


def test_a_sort_saved_in_another_tab_is_kept_when_this_tab_saves_its_page_size(client, agent):
    """The list middleware saves the fields its address gives into the list's stored value as it
    stands when it saves, not as the request read it: another tab's sort saved meanwhile stays."""
    UserPreference.objects.create(
        user=agent, key="page:clients:client-list", value={"sort": "name"}
    )
    client.force_login(agent)
    tab = same_session(client)
    url = reverse("clients:client-list")
    sorted_elsewhere = []

    def sort_in_the_other_tab():
        sorted_elsewhere.append(committed(lambda: tab.get(url, {"sort": "-name"}).status_code))

    with paused(preferences, "for_request", sort_in_the_other_tab, before=False):
        assert client.get(url, {"per_page": 50}).status_code == 200
    assert sorted_elsewhere == [200]
    stored = UserPreference.objects.get(user=agent, key="page:clients:client-list").value
    assert stored == {"per_page": 50, "sort": "-name"}


def test_a_change_in_another_browser_shows_from_the_next_page_without_signing_in_again(
    client, agent
):
    client.force_login(agent)
    elsewhere = Browser()
    elsewhere.force_login(agent)  # another workstation: its own session
    assert post(client, "grid:clients.clients:v1", LAYOUT).status_code == 204
    assert page_prefs(client, reverse("dashboard")) == {"grid:clients.clients:v1": LAYOUT}
    assert post(elsewhere, "grid:clients.clients:v1", OTHER_LAYOUT).status_code == 204
    assert post(elsewhere, "nav.collapsed", True).status_code == 204
    expected = {"grid:clients.clients:v1": OTHER_LAYOUT, "nav.collapsed": True}
    assert page_prefs(client, reverse("dashboard")) == expected
    assert post(elsewhere, "grid:clients.clients:v1", None).status_code == 204  # Reset columns
    assert page_prefs(client, reverse("dashboard")) == {"nav.collapsed": True}


def test_opening_whats_new_elsewhere_clears_the_indicator_on_the_next_page(client, agent, ticket):
    editions = [edition(20), edition(19)]
    client.force_login(agent)
    elsewhere = Browser()
    elsewhere.force_login(agent)
    with patch.object(changelog, "entries", return_value=editions[1:]):
        assert client.get(detail(ticket)).status_code == 200  # the first page's mark
    with patch.object(changelog, "entries", return_value=editions):
        assert b'data-vr-unread="1"' in client.get(detail(ticket)).content
        assert elsewhere.get(reverse("whats-new")).status_code == 200
        assert b"data-vr-unread" not in client.get(detail(ticket)).content


def test_a_copy_left_in_a_session_from_before_is_ignored(client, agent):
    """Sessions signed in before this change carry a copy under "vr_preferences"; it is no
    longer read, and nothing new is written there."""
    UserPreference.objects.create(user=agent, key="grid:clients.clients:v1", value=LAYOUT)
    client.force_login(agent)
    session = client.session
    session["vr_preferences"] = {"user": agent.pk, "prefs": {"nav.collapsed": True}}
    session.save()
    assert page_prefs(client, reverse("dashboard")) == {"grid:clients.clients:v1": LAYOUT}
    assert post(client, "nav.collapsed", False).status_code == 204
    assert client.session["vr_preferences"]["prefs"] == {"nav.collapsed": True}  # untouched
    assert page_prefs(client, reverse("dashboard"))["nav.collapsed"] is False


def test_turning_notices_off_in_one_tab_shows_in_the_other(client, agent):
    client.force_login(agent)
    tab = same_session(client)
    assert tab.get(reverse("email-notices")).context["form"]["enabled"].value() is True
    assert client.post(reverse("email-notices"), {}).status_code == 302  # unticked: off
    assert UserPreference.objects.get(user=agent, key=OPT_OUT_KEY).value is False
    assert tab.get(reverse("email-notices")).context["form"]["enabled"].value() is False
    assert "vr_preferences" not in client.session


# V3 and its neighbours: the limit holds under additions at the same moment -----------------------


def test_first_shell_and_another_preference_share_the_last_slot(client, agent, ticket):
    """V3, adopted. With 299 saved, the first page's mark and a sidebar preference saved in
    another tab compete for the last place. Adapted: the first page counts holding the lock of
    the person's additions, so the other tab's addition waits for it (it no longer runs to its
    end between the count and the insert), then counts 300 and is refused."""
    client.force_login(agent)
    tab = Browser()
    tab.force_login(agent)
    prefs = UserPreference.objects.filter(user=agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    here = backend_pid()
    others = []

    def after_count(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not others and "COUNT(" in sql and '"core_userpreference"' in sql:
            other = Session(lambda: post(tab, "nav.collapsed", True).status_code).start()
            state = other.waiting_or_ended()
            others.append((other, state, state == "waiting" and _blocked_by(other.pid, here)))
        return result

    with connection.execute_wrapper(after_count):
        assert client.get(detail(ticket)).status_code == 200
    other, state, blocked = others[0]
    assert (state, blocked) == ("waiting", True)
    assert other.join() == "done" and other.result == 400
    assert prefs.count() == preferences.MAX_PREFERENCES
    assert prefs.filter(key=preferences.WHATS_NEW_KEY).exists()
    assert not prefs.filter(key="nav.collapsed").exists()


def test_a_first_page_waits_for_an_addition_under_way_and_leaves_the_last_slot_to_it(
    client, agent, ticket
):
    client.force_login(agent)
    tab = Browser()
    tab.force_login(agent)
    prefs = UserPreference.objects.filter(user=agent)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    state, blocked, page = waits_for(
        lambda: post(tab, "nav.collapsed", True).status_code,
        lambda: client.get(detail(ticket)).status_code,
    )
    assert (state, blocked) == ("waiting", True)
    assert page.outcome == "done" and page.result == 200
    assert prefs.count() == preferences.MAX_PREFERENCES
    assert prefs.filter(key="nav.collapsed").exists()
    assert not prefs.filter(key=preferences.WHATS_NEW_KEY).exists()
    assert client.get(detail(ticket)).status_code == 200  # tries again, still full
    assert prefs.count() == preferences.MAX_PREFERENCES


def test_two_different_additions_at_the_same_moment_take_the_last_slot_once(client, agent):
    client.force_login(agent)
    tab = same_session(client)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    first = []
    state, blocked, second = waits_for(
        lambda: first.append(post(client, "grid:clients.clients:v1", LAYOUT).status_code),
        lambda: post(tab, "grid:desk.requests:v1", OTHER_LAYOUT).status_code,
    )
    assert (state, blocked) == ("waiting", True)
    assert first == [204] and second.result == 400
    prefs = UserPreference.objects.filter(user=agent)
    assert prefs.count() == preferences.MAX_PREFERENCES
    assert prefs.filter(key="grid:clients.clients:v1").exists()


def test_the_same_addition_from_two_tabs_at_the_same_moment_is_stored_once(client, agent):
    """The second waits for the first, then finds its row and replaces it: not a second row,
    an error, or a refusal at the last place."""
    client.force_login(agent)
    tab = same_session(client)
    fill(agent, preferences.MAX_PREFERENCES - 1)
    first = []
    state, blocked, second = waits_for(
        lambda: first.append(post(client, "nav.collapsed", True).status_code),
        lambda: post(tab, "nav.collapsed", False).status_code,
    )
    assert (state, blocked) == ("waiting", True)
    assert first == [204] and second.result == 204
    prefs = UserPreference.objects.filter(user=agent)
    assert prefs.count() == preferences.MAX_PREFERENCES
    assert prefs.get(key="nav.collapsed").value is False


@pytest.mark.parametrize("first", ["visit", "first page"])
def test_whats_new_and_a_first_page_adding_the_mark_at_the_same_moment_keep_the_visit(
    client, agent, ticket, first
):
    """Whichever adds the What's new mark first, the visit's mark (every entry it showed) is
    kept and the first page's older one is not stored over it, nor added a second time."""
    editions = [edition(20), edition(19)]
    client.force_login(agent)
    tab = same_session(client)
    mark = UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY)

    def visit():
        with patch.object(changelog, "entries", return_value=editions):
            return tab.get(reverse("whats-new")).status_code

    def first_page():
        with patch.object(changelog, "entries", return_value=editions[1:]):
            return client.get(detail(ticket)).status_code

    holding, waiting = (visit, first_page) if first == "visit" else (first_page, visit)
    holder = Held(holding)
    other = Session(waiting).start()
    try:
        state = other.waiting_or_ended()
        blocked = state == "waiting" and _blocked_by(other.pid, holder.session.pid)
    finally:
        assert holder.release() == "done", holder.session.error
        other.join()
    assert (state, blocked) == ("waiting", True)
    assert other.outcome == "done" and other.result == 200
    assert mark.count() == 1 and mark.get().value == changelog.seen_marker(editions)


def test_changing_or_removing_a_stored_preference_does_not_wait_for_an_addition(client, agent):
    UserPreference.objects.create(user=agent, key="nav.collapsed", value=True)
    UserPreference.objects.create(user=agent, key="grid:clients.clients:v1", value=LAYOUT)
    client.force_login(agent)
    tab = same_session(client)
    changes = (("nav.collapsed", False), ("grid:clients.clients:v1", None))
    for version, (key, value) in enumerate(changes, start=1):
        added = []
        state, _blocked, other = waits_for(  # an addition holds the person's additions lock
            lambda v=version, added=added: added.append(
                post(client, f"grid:desk.requests:v{v}", OTHER_LAYOUT).status_code
            ),
            lambda key=key, value=value: post(tab, key, value).status_code,
        )
        assert added == [204] and state == "ended" and other.result == 204
    stored = dict(UserPreference.objects.filter(user=agent).values_list("key", "value"))
    assert stored == {
        "nav.collapsed": False,
        "grid:desk.requests:v1": OTHER_LAYOUT,
        "grid:desk.requests:v2": OTHER_LAYOUT,
    }


def test_another_persons_addition_does_not_wait(client, agent, make_user):
    colleague = make_user("tech-colleague", "technical")
    client.force_login(agent)
    elsewhere = Browser()
    elsewhere.force_login(colleague)
    first = []
    state, _blocked, other = waits_for(
        lambda: first.append(post(client, "nav.collapsed", True).status_code),
        lambda: post(elsewhere, "nav.collapsed", True).status_code,
    )
    assert first == [204] and state == "ended" and other.result == 204
    for person in (agent, colleague):
        assert UserPreference.objects.get(user=person, key="nav.collapsed").value is True


def test_at_the_limit_changes_and_removals_still_save_and_a_removal_frees_a_place(client, agent):
    fill(agent, preferences.MAX_PREFERENCES - 1)
    UserPreference.objects.create(user=agent, key="nav.collapsed", value=True)
    client.force_login(agent)
    assert post(client, "grid:clients.clients:v1", LAYOUT).status_code == 400
    assert post(client, "nav.collapsed", False).status_code == 204
    assert post(client, "nav.collapsed", None).status_code == 204
    assert post(client, "grid:clients.clients:v1", LAYOUT).status_code == 204
    assert UserPreference.objects.filter(user=agent).count() == preferences.MAX_PREFERENCES


def test_a_list_at_the_limit_follows_its_address_without_saving_it(client, agent):
    """The list middleware saves a page size or sort given in the address. At the limit it saves
    nothing and the page still opens with the address's values (before: a server error)."""
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    response = client.get(reverse("clients:client-list"), {"per_page": 50, "sort": "name"})
    assert response.status_code == 200
    assert response.context["page_obj"].paginator.per_page == 50
    assert not UserPreference.objects.filter(user=agent, key="page:clients:client-list").exists()
    assert client.get(reverse("clients:client-list")).context["page_obj"].paginator.per_page == 25


def test_a_first_whats_new_visit_at_the_limit_still_shows_the_list(client, agent):
    fill(agent, preferences.MAX_PREFERENCES)
    client.force_login(agent)
    assert client.get(reverse("whats-new")).status_code == 200
    assert not UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY).exists()


def test_the_additions_lock_needs_a_transaction(agent):
    with pytest.raises(RuntimeError):
        preferences._hold_additions(agent.pk)


def test_pages_and_preferences_do_not_extend_a_session_and_a_password_change_does(client, agent):
    """The Security settings page's Session length: a session ends that long after it was last
    saved, at sign-in and at a few account steps such as a password change; opening pages and
    changing display preferences no longer save it."""
    from datetime import timedelta

    from django.contrib.sessions.models import Session as StoredSession
    from django.utils import timezone

    from .conftest import PASSWORD

    client.force_login(agent)
    key = client.session.session_key
    earlier = timezone.now() + timedelta(days=1)
    StoredSession.objects.filter(session_key=key).update(expire_date=earlier)
    assert client.get(reverse("dashboard")).status_code == 200
    assert post(client, "nav.collapsed", True).status_code == 204
    assert client.get(reverse("clients:client-list"), {"per_page": 50}).status_code == 200
    assert client.get(reverse("whats-new")).status_code == 200
    assert StoredSession.objects.get(session_key=key).expire_date == earlier
    new = "Synthetic-passphrase-4471"
    changed = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": new, "password2": new},
    )
    assert changed.status_code == 302
    current = StoredSession.objects.get(session_key=client.session.session_key)
    assert current.expire_date > earlier + timedelta(days=12)


def test_rows_per_page_form_keeps_a_sort_the_address_gave_and_leaves_out_a_saved_one(
    client, agent, clinic
):
    """V9's neighbour: the form re-sends what the person chose on this page (a sort in the
    address) but not a sort added from their saved settings, which another tab may change."""
    UserPreference.objects.create(
        user=agent, key="page:clients:client-list", value={"sort": "name"}
    )
    client.force_login(agent)
    url = reverse("clients:client-list")

    def page_size_form(response):
        html = response.content.decode()
        start = html.index('<form class="d-flex align-items-center gap-2" method="get"')
        return html[start : html.index("</form>", start)]

    saved = page_size_form(client.get(url, {"q": "Example"}))
    given = page_size_form(client.get(url, {"q": "Example", "sort": "-name"}))
    assert 'name="sort"' not in saved
    assert '<input type="hidden" name="sort" value="-name">' in given
    for form in (saved, given):  # the person's filters always go with it
        assert '<input type="hidden" name="q" value="Example">' in form
