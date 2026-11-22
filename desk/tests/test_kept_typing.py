"""What a person was typing, kept when a session limit signs them out (accounts.kept): which
endings keep a copy, who gets it back and where, what a form posted after the sign-out keeps,
and what Sign out and the scheduled job remove. All data is synthetic."""

import html
import json
import re
from datetime import timedelta

import pytest
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from clients.models import PortalAccess
from desk.models import EntryKind, ServiceRequest, Status
from desk.services import create_request

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

Ending = AccountSession.Ending
TYPED = "Called the courier; the cooler leaves at four."
COMPOSER_FIELDS = [
    "kind",
    "body",
    "contact_direction",
    "contact_channel",
    "contact_with",
    "contact_outcome",
    "new_status",
]


def composer(**values):
    """A composer copy as kept: every field the composer keeps, "" where nothing was given."""
    return {name: values.get(name, "") for name in COMPOSER_FIELDS}


@pytest.fixture
def ticket(agent, category, clinic):
    return create_request(
        actor=agent,
        category=category,
        subject="Missing report",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Logged by the laboratory.",
        channel="phone",
        client=clinic,
    )


@pytest.fixture
def whole(make_user, clinic):
    user = make_user("whole_portal", client=clinic)
    PortalAccess.objects.create(user=user, client_id=user.client_id, whole_organization=True)
    return user


def browser_for(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def open_row(user):
    return AccountSession.objects.get(user=user, ended_at__isnull=True)


def end_open(user, ending=Ending.INACTIVITY, ago=timedelta(0)):
    """End the account's open session as a limit (or anything else) would have."""
    row = open_row(user)
    AccountSession.objects.filter(pk=row.pk).update(ended_at=Now() - ago, ending=ending)
    return row


def marker(page):
    """(token, field names, values JSON id or None) of the page's kept-typing marker."""
    text = page.content.decode()
    found = re.search(r'<input type="hidden" name="_vr_kept" value="([^"]+)"([^>]*)>', text)
    if found is None:
        return None
    attrs = found.group(2)
    names = re.search(r'data-vr-kept-fields="([^"]*)"', attrs)
    values = re.search(r'data-vr-kept-values="([^"]*)"', attrs)
    return (
        found.group(1),
        json.loads(html.unescape(names.group(1))),
        values.group(1) if values else None,
    )


def given_back(page):
    """The values a page gives back (its JSON), or None."""
    found = marker(page)
    if found is None or found[2] is None:
        return None
    text = page.content.decode()
    script = re.search(
        rf'<script id="{re.escape(found[2])}" type="application/json">(.*?)</script>', text, re.S
    )
    return json.loads(script.group(1))


def send(browser, token, values):
    response = browser.post(
        reverse("account-session-keep"),
        data=json.dumps({"forms": [{"token": token, "values": values}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["kept"]


def ticket_page(ticket):
    return reverse("desk:request-detail", args=[ticket.pk])


def typed_then_signed_out(user, page, ending=Ending.INACTIVITY):
    """Open the page, end its session, return the browser and the page's token."""
    browser = browser_for(user)
    token, _names, _values = marker(browser.get(page))
    end_open(user, ending)
    return browser, token


# the form's marker


def test_the_composer_names_what_may_be_kept_and_never_files_or_the_revision(agent, ticket):
    page = browser_for(agent).get(ticket_page(ticket))
    token, names, values = marker(page)
    assert names == COMPOSER_FIELDS and values is None
    data = kept._read(token)
    assert data["u"] == agent.pk and data["s"] == str(open_row(agent).pk)
    assert data["k"] == f"ticket-entry-{ticket.pk}" and data["t"] == ["body", "contact_with"]
    # The page's warning knows where to send it.
    assert f'data-keep-url="{reverse("account-session-keep")}"' in page.content.decode()


def test_a_page_drawn_for_a_visitor_carries_no_marker(db):
    page = BrowserClient().get(reverse("account_login"))
    assert marker(page) is None


# what is kept


@pytest.mark.parametrize("ending", [Ending.INACTIVITY, Ending.MAXIMUM, Ending.CHANGE_REFUSED])
def test_typing_is_kept_when_a_limit_ends_the_session_and_given_back_once(agent, ticket, ending):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket), ending)
    assert send(browser, token, {"kind": "note", "body": TYPED, "contact_with": ""}) == 1
    draft = KeptDraft.objects.get()
    assert draft.user == agent and draft.form == f"ticket-entry-{ticket.pk}"
    assert draft.values == composer(kind="note", body=TYPED)
    # Signing in again gives it back on the same form, with the time the session ended.
    browser.force_login(agent)
    page = browser.get(ticket_page(ticket))
    assert given_back(page) == composer(kind="note", body=TYPED)
    text = page.content.decode()
    assert "is back in the form. Check it before you send it." in text
    assert "Files you had chosen were not kept." in text
    draft.refresh_from_db()
    assert draft.values is None and draft.taken_at is not None
    # Once: the next drawing of the page starts empty.
    assert given_back(browser.get(ticket_page(ticket))) is None


def test_the_keep_address_says_so_on_the_sign_in_page(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    notices = [str(m) for m in browser.get(reverse("account_login")).context["messages"]]
    assert kept.KEPT_MESSAGE in notices


@pytest.mark.parametrize(
    "ending",
    [
        Ending.SIGNED_OUT,
        Ending.DEACTIVATED,
        Ending.NEW_PASSWORD,
        Ending.SIGNED_IN_AGAIN,
        Ending.SIGNED_OUT_ELSEWHERE,
        Ending.ADMINISTRATOR,
    ],
)
def test_a_session_ended_any_other_way_keeps_nothing(agent, ticket, ending):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket), ending)
    assert send(browser, token, {"body": TYPED}) == 0
    assert not KeptDraft.objects.exists()


def test_an_open_session_keeps_nothing_and_sending_is_not_activity(agent, ticket):
    browser = browser_for(agent)
    token, _names, _values = marker(browser.get(ticket_page(ticket)))
    AccountSession.objects.filter(pk=open_row(agent).pk).update(
        last_active_at=Now() - timedelta(minutes=10)
    )
    before = open_row(agent).last_active_at
    assert send(browser, token, {"body": TYPED}) == 0
    assert open_row(agent).last_active_at == before and not KeptDraft.objects.exists()


def test_a_copy_more_than_24_hours_after_the_ending_is_refused(agent, ticket):
    browser = browser_for(agent)
    token, _names, _values = marker(browser.get(ticket_page(ticket)))
    end_open(agent, ago=timedelta(hours=24, minutes=1))
    assert send(browser, token, {"body": TYPED}) == 0


def test_a_deactivated_account_keeps_nothing(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    type(agent).objects.filter(pk=agent.pk).update(is_active=False)
    assert send(browser, token, {"body": TYPED}) == 0


def test_the_first_copy_stands(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    assert send(browser, token, {"body": "Something else entirely"}) == 1  # held: answer lost
    assert KeptDraft.objects.get().values == composer(body=TYPED)
    # Given back, a later copy for the same session and form changes nothing.
    browser.force_login(agent)
    browser.get(ticket_page(ticket))
    assert send(BrowserClient(), token, {"body": "Third"}) == 0
    assert KeptDraft.objects.get().values is None


def test_only_the_fields_the_token_names_are_kept_and_nothing_untyped(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"kind": "note", "body": "  ", "contact_with": ""}) == 0
    assert send(browser, token, {"body": TYPED, "expected_revision": "99", "files": "x"}) == 1
    assert set(KeptDraft.objects.get().values) == set(COMPOSER_FIELDS)
    assert KeptDraft.objects.get().values["body"] == TYPED


@pytest.mark.parametrize(
    "values",
    [
        {"body": "x" * (kept.MAX_VALUE + 1)},
        {"body": ["a list"]},
        {"body": 7},
        "not an object",
    ],
)
def test_a_copy_too_large_or_not_text_is_not_kept_at_all(agent, ticket, values):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, values) == 0


def test_a_token_changed_or_of_another_kind_keeps_nothing(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    forged = kept.signing.dumps(
        {"u": agent.pk, "s": str(AccountSession.objects.get().pk), "k": "x", "f": ["body"]},
        salt="another purpose",
    )
    for value in (token[:-2] + "xx", forged, "", None, 12):
        assert send(browser, value, {"body": TYPED}) == 0
    assert not KeptDraft.objects.exists()


def test_the_keep_address_needs_no_session_and_no_csrf_token(agent, ticket):
    _browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    stranger = BrowserClient(enforce_csrf_checks=True)
    assert send(stranger, token, {"body": TYPED}) == 1


def test_the_keep_address_refuses_what_it_cannot_read(db):
    browser = BrowserClient()
    url = reverse("account-session-keep")
    assert browser.post(url, data="{", content_type="application/json").status_code == 400
    assert browser.post(url, data="[]", content_type="application/json").status_code == 400
    assert browser.get(url).status_code == 405
    big = json.dumps({"forms": [], "pad": "x" * 2_500_000})
    response = browser.post(url, data=big, content_type="application/json")
    assert response.status_code == 413
    assert "no-store" in response["Cache-Control"]


# a form sent after the sign-out


def test_a_reply_sent_after_the_limit_is_kept_from_what_it_posted(agent, ticket):
    browser = browser_for(agent)
    token, _names, _values = marker(browser.get(ticket_page(ticket)))
    AccountSession.objects.filter(pk=open_row(agent).pk).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    response = browser.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {
            "kind": "reply",
            "body": TYPED,
            "expected_revision": ticket.revision,
            kept.FIELD: token,
        },
        HTTP_REFERER="http://testserver" + ticket_page(ticket),
    )
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login") + "?next=")
    assert not ticket.entries.filter(body=TYPED).exists()
    assert KeptDraft.objects.get().values["body"] == TYPED
    notices = [str(m) for m in browser.get(reverse("account_login")).context["messages"]]
    assert kept.KEPT_MESSAGE in notices and any("without activity" in n for n in notices)


def test_a_form_sent_without_its_token_after_the_limit_keeps_nothing(agent, ticket):
    browser = browser_for(agent)
    browser.get(ticket_page(ticket))
    AccountSession.objects.filter(pk=open_row(agent).pk).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    browser.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "reply", "body": TYPED, "expected_revision": ticket.revision},
    )
    assert not KeptDraft.objects.exists()


def test_a_reply_sent_while_signed_in_keeps_nothing(agent, ticket):
    browser = browser_for(agent)
    token, _names, _values = marker(browser.get(ticket_page(ticket)))
    response = browser.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": TYPED, "expected_revision": ticket.revision, kept.FIELD: token},
    )
    assert response.status_code == 302 and ticket.entries.filter(body=TYPED).exists()
    assert not KeptDraft.objects.exists()


# who gets it back, and where


def test_never_given_back_to_another_account_or_on_another_form(
    agent, make_user, ticket, category, clinic
):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    colleague = make_user("colleague", "client_services")
    assert given_back(browser_for(colleague).get(ticket_page(ticket))) is None
    other = create_request(
        actor=agent,
        category=category,
        subject="Another",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Logged.",
        channel="phone",
        client=clinic,
    )
    browser.force_login(agent)
    assert given_back(browser.get(ticket_page(other))) is None
    assert KeptDraft.objects.get().taken_at is None
    assert given_back(browser.get(ticket_page(ticket))) == composer(body=TYPED)


def test_given_back_only_in_the_browser_that_kept_it(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    cookie = browser.cookies[kept.BROWSER_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and TYPED not in cookie.value
    assert KeptDraft.objects.get().browser != cookie.value  # only its digest is stored
    # The same account signed in in another browser (a shared account's other visitor) gets
    # nothing, and leaves the copy for the browser that kept it.
    elsewhere = browser_for(agent)
    assert given_back(elsewhere.get(ticket_page(ticket))) is None
    assert KeptDraft.objects.get().taken_at is None
    browser.force_login(agent)
    assert given_back(browser.get(ticket_page(ticket))) == composer(body=TYPED)


def test_a_copy_already_held_for_another_browser_is_not_reported_kept(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(BrowserClient(), token, {"body": "From elsewhere"}) == 1
    assert send(browser, token, {"body": TYPED}) == 0
    assert kept.BROWSER_COOKIE not in browser.cookies


def test_not_given_back_into_a_refused_save_or_a_partial_request(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    browser.force_login(agent)
    refused = browser.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": "note", "body": "", "expected_revision": ticket.revision},
    )
    assert refused.status_code == 400 and given_back(refused) is None
    partial = browser.get(ticket_page(ticket), HTTP_HX_REQUEST="true")
    assert given_back(partial) is None
    head = browser.head(ticket_page(ticket))
    assert head.status_code == 200
    assert KeptDraft.objects.get().taken_at is None


def test_given_back_holds_only_the_fields_the_form_keeps_now(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    KeptDraft.objects.update(values={"body": TYPED, "expected_revision": "1", "_vr_kept": "x"})
    browser.force_login(agent)
    assert given_back(browser.get(ticket_page(ticket))) == {"body": TYPED}


def test_the_newest_copy_is_given_back_and_every_copy_for_the_form_is_taken(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": "Older"}) == 1
    browser.force_login(agent)
    token, _names, _values = marker(browser.get(ticket_page(ticket)))  # takes "Older"
    KeptDraft.objects.update(values={"body": "Older"}, taken_at=None)  # as if not yet taken
    end_open(agent)
    assert send(browser, token, {"body": "Newer"}) == 1
    browser.force_login(agent)
    assert given_back(browser.get(ticket_page(ticket))) == composer(body="Newer")
    assert not KeptDraft.objects.filter(taken_at__isnull=True).exists()


def test_a_ticket_now_out_of_reach_gives_nothing_back(agent, ticket, privacy_category):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    ServiceRequest.objects.filter(pk=ticket.pk).update(category=privacy_category)
    browser.force_login(agent)
    assert browser.get(ticket_page(ticket)).status_code == 404
    assert KeptDraft.objects.get().taken_at is None


def test_a_closed_ticket_gives_the_note_back_with_only_note_offered(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"kind": "reply", "body": TYPED}) == 1
    ServiceRequest.objects.filter(pk=ticket.pk).update(
        status=Status.CLOSED, resolved_at=Now(), closed_at=Now()
    )
    browser.force_login(agent)
    page = browser.get(ticket_page(ticket))
    # Given back as kept; the page offers only a note now, so its script leaves the kind as drawn
    # and shows the sentence saying so (e2e/test_kept_typing.py).
    assert given_back(page) == composer(kind="reply", body=TYPED)
    assert 'value="reply"' not in page.content.decode()
    assert "data-vr-kept-missed hidden" in page.content.decode()


# Sign out and the scheduled job


def test_sign_out_removes_every_copy_and_refuses_later_ones(agent, ticket):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    browser.force_login(agent)
    browser.post(reverse("account_logout"))
    assert not KeptDraft.objects.exists()
    # A page of the earlier session sending later finds that Sign out.
    assert send(BrowserClient(), token, {"body": TYPED}) == 0
    assert not KeptDraft.objects.exists()


def test_the_scheduled_job_removes_copies_24_hours_after_their_session_ended(
    agent, ticket, make_user
):
    browser, token = typed_then_signed_out(agent, ticket_page(ticket))
    assert send(browser, token, {"body": TYPED}) == 1
    other = make_user("other_agent", "client_services")
    other_browser, other_token = typed_then_signed_out(other, ticket_page(ticket))
    assert send(other_browser, other_token, {"body": "Recent"}) == 1
    AccountSession.objects.filter(user=agent).update(ended_at=Now() - timedelta(hours=25))
    assert kept.remove_expired() == {"kept_removed": 1}
    assert list(KeptDraft.objects.values_list("user", flat=True)) == [other.pk]
    # An expired copy is never given back before the job runs.
    AccountSession.objects.filter(user=other).update(ended_at=Now() - timedelta(hours=25))
    other_browser.force_login(other)
    assert given_back(other_browser.get(ticket_page(ticket))) is None


def test_the_job_is_registered(db):
    from jobs.registry import JOBS_BY_NAME

    assert JOBS_BY_NAME["remove_kept_typing"].function is kept.remove_expired


# the client portal


def test_a_portal_reply_is_kept_and_given_back(agent, ticket, whole):
    page = reverse("portal:request-detail", args=[ticket.number])
    browser = browser_for(whole)
    token, names, _values = marker(browser.get(page))
    assert names == ["body"]
    end_open(whole)
    assert send(browser, token, {"body": TYPED}) == 1
    browser.force_login(whole)
    assert given_back(browser.get(page)) == {"body": TYPED}


def test_a_new_portal_ticket_is_kept_and_given_back(whole, category):
    page = reverse("portal:request-create")
    browser = browser_for(whole)
    token, names, _values = marker(browser.get(page))
    assert names == ["category", "subject", "description", "client_reference", "urgent"]
    end_open(whole, Ending.MAXIMUM)
    values = {"category": str(category.pk), "subject": "Cooler", "description": TYPED}
    assert send(browser, token, {**values, "urgent": "on"}) == 1
    browser.force_login(whole)
    assert given_back(browser.get(page)) == {
        **values,
        "client_reference": "",
        "urgent": "on",
    }


def test_the_sign_in_password_is_never_named(db, agent):
    page = BrowserClient().get(reverse("account_login"))
    assert "_vr_kept" not in page.content.decode()
    assert PASSWORD not in page.content.decode()
