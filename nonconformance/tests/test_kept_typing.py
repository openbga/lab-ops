"""What a staff member was typing in a nonconformance form, kept when a session limit signs them
out (accounts.kept, nonconformance.views._key) and given back at the next sign-in in the same
browser, with the revision the form was drawn with: a page giving the copy back posts that
revision, so its save is refused as changed since, with the typing kept, when the record (or the
complaint ticket) changed after it. A page that is not drawn after all leaves the copy. All data
is synthetic."""

import html
import json
import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse
from django.utils import timezone

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from desk import services as desk_services
from nonconformance import services
from nonconformance.models import ActionStatus, CorrectiveAction, Nonconformance, Status

pytestmark = pytest.mark.django_db

STALE = services.STALE
CHANGED = "This record changed after you typed it, so saving this form is refused."
PUT_BACK = "is back in the form"


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


def browser_for(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def end_open(user):
    """End the account's open session as the limit without activity would have."""
    row = AccountSession.objects.get(user=user, ended_at__isnull=True)
    AccountSession.objects.filter(pk=row.pk).update(
        ended_at=Now(), ending=AccountSession.Ending.INACTIVITY
    )
    return row


def marker(response):
    """(token contents, field names, attributes) of the page's keep marker, or None."""
    text = response.content.decode()
    found = re.search(r'<input type="hidden" name="_vr_kept" value="([^"]+)"([^>]*)>', text)
    if found is None:
        return None
    attrs = found.group(2)
    names = re.search(r'data-vr-kept-fields="([^"]*)"', attrs)
    return found.group(1), json.loads(html.unescape(names.group(1))), attrs


def drawn_revision(response):
    found = re.search(r'name="expected_revision" value="(\d+)"', response.content.decode())
    return int(found.group(1)) if found else None


def send(browser, token, values):
    response = browser.post(
        reverse("account-session-keep"),
        data=json.dumps({"forms": [{"token": token, "values": values}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["kept"]


def kept_then_signed_in(user, url, values):
    """The form at `url` drawn for `user`, a limit ending the session, what was typed kept; then
    the same browser signed in again. Returns (browser, the token's contents)."""
    browser = browser_for(user)
    page = browser.get(url)
    assert page.status_code == 200, url
    token, names, _attrs = marker(page)
    assert set(values) <= set(names), (set(values) - set(names), names)
    end_open(user)
    assert send(browser, token, {name: values.get(name, "") for name in names}) == 1
    browser.force_login(user)
    return browser, kept._read(token)


def url(name, *args):
    return reverse(f"nonconformance:{name}", args=args)


def to_verification(nc, actor):
    nc = fresh(nc)
    for status in (Status.INVESTIGATING, Status.CORRECTIVE_ACTION, Status.VERIFICATION):
        services.change_status(
            actor=actor, nc=nc, expected_revision=nc.revision, status=status, reason=""
        )
        nc = fresh(nc)
    return nc


@pytest.fixture
def record(module_on, lead, agent):
    """An open record owned by `agent`, raised by a supervisor."""
    return services.raise_nonconformance(
        actor=lead,
        title="Label printer misprints",
        description="Labels on ORD-TEST-000101 were unreadable.",
        source="internal",
        severity="minor",
        area="Accessioning",
        owner=agent,
    )


@pytest.fixture
def action(record, agent, tech):
    nc = fresh(record)
    services.add_action(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        description="Replace the print head.",
        assignee=tech,
        due_on=timezone.localdate() + timedelta(days=7),
    )
    return CorrectiveAction.objects.get(nonconformance=record)


@pytest.fixture
def link(record, agent, plain_ticket):
    nc = fresh(record)
    services.link_ticket(actor=agent, nc=nc, expected_revision=nc.revision, request=plain_ticket)
    return fresh(record).ticket_links.get()


# ------------------------------------------------------------------------------ marked forms


def test_every_record_form_is_marked_with_its_key_and_the_revision_it_shows(
    client, record, action, link, agent, lead
):
    nc = fresh(record)
    pages = [
        (agent, url("edit", nc.pk), f"nc-{nc.pk}-edit"),
        (lead, url("owner", nc.pk), f"nc-{nc.pk}-owner"),
        (agent, url("status", nc.pk), f"nc-{nc.pk}-status"),
        (agent, url("investigation", nc.pk), f"nc-{nc.pk}-investigation"),
        (agent, url("impact", nc.pk), f"nc-{nc.pk}-impact"),
        (agent, url("action-add", nc.pk), f"nc-{nc.pk}-action-add"),
        (agent, url("action-edit", nc.pk, action.pk), f"nc-{nc.pk}-action-{action.pk}-edit"),
        (agent, url("action-done", nc.pk, action.pk), f"nc-{nc.pk}-action-{action.pk}-done"),
        (agent, url("action-cancel", nc.pk, action.pk), f"nc-{nc.pk}-action-{action.pk}-cancel"),
        (agent, url("ticket-link", nc.pk), f"nc-{nc.pk}-ticket-link"),
        (agent, url("ticket-unlink", nc.pk, link.pk), f"nc-{nc.pk}-link-{link.pk}-remove"),
    ]
    for user, address, key in pages:
        client.force_login(user)
        page = client.get(address)
        assert page.status_code == 200, address
        token, names, _attrs = marker(page)
        data = kept._read(token)
        assert data["k"] == key and data["v"] == nc.revision == drawn_revision(page), address
        # Never the revision itself, the token or anything hidden.
        assert "expected_revision" not in names and "_vr_kept" not in names, address
    # A form's fetched person is kept by id.
    client.force_login(agent)
    assert "assignee" in marker(client.get(url("action-add", nc.pk)))[1]
    assert "client_told_by" in marker(client.get(url("impact", nc.pk)))[1]


def test_effectiveness_close_and_reopen_are_marked(client, record, agent, lead, lead2):
    nc = to_verification(record, agent)
    client.force_login(agent)
    page = client.get(url("effectiveness", nc.pk))
    assert kept._read(marker(page)[0])["k"] == f"nc-{nc.pk}-effectiveness"
    services.record_impact(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="ORD-TEST-000101 reviewed; none reported.",
    )
    nc = fresh(nc)
    services.record_effectiveness(
        actor=agent,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Checked the next labels.",
    )
    nc = fresh(nc)
    client.force_login(lead2)
    page = client.get(url("close", nc.pk))
    data = kept._read(marker(page)[0])
    assert data["k"] == f"nc-{nc.pk}-close" and data["v"] == nc.revision
    services.close(actor=lead2, nc=nc, expected_revision=nc.revision, note="Done.")
    nc = fresh(nc)
    page = client.get(url("reopen", nc.pk))
    data = kept._read(marker(page)[0])
    assert data["k"] == f"nc-{nc.pk}-reopen" and data["v"] == nc.revision


def test_new_and_from_complaint_forms_are_marked(client, module_on, agent, complaint):
    client.force_login(agent)
    data = kept._read(marker(client.get(url("create")))[0])
    assert data["k"] == "nc-new" and data["v"] is None  # nothing to check a save against
    page = client.get(url("from-ticket", complaint.pk))
    data = kept._read(marker(page)[0])
    assert data["k"] == f"nc-from-ticket-{complaint.pk}"
    assert data["v"] == fresh(complaint).revision == drawn_revision(page)


# ------------------------------------------------------------------------------ given back


def test_details_come_back_with_their_revision_and_save(record, agent):
    nc = fresh(record)
    typed = {
        "title": "Label printer misprints on bench 2",
        "description": "Labels on ORD-TEST-000101 and ORD-TEST-000102 were unreadable.",
        "source": "equipment",
        "severity": "major",
        "area": "Accessioning",
        "reason": "A second order was affected.",
    }
    browser, data = kept_then_signed_in(agent, url("edit", nc.pk), typed)
    assert data["v"] == nc.revision
    page = browser.get(url("edit", nc.pk))
    text = page.content.decode()
    assert page.status_code == 200 and PUT_BACK in text and CHANGED not in text
    assert "ORD-TEST-000102" in text and drawn_revision(page) == nc.revision
    assert KeptDraft.objects.get().taken_at is not None
    saved = browser.post(url("edit", nc.pk), {**typed, "expected_revision": nc.revision})
    assert saved.status_code == 302
    nc = fresh(nc)
    assert nc.title == typed["title"] and nc.severity == "major" and nc.source == "equipment"


def test_a_record_changed_after_the_typing_refuses_the_save_and_keeps_the_typing(
    record, agent, lead
):
    nc = fresh(record)
    typed = {"containment": "", "root_cause": "A worn print head.", "similar_cases": ""}
    browser, data = kept_then_signed_in(agent, url("investigation", nc.pk), typed)
    services.change_status(
        actor=lead, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING, reason=""
    )
    current = fresh(nc)
    assert current.revision > data["v"]

    page = browser.get(url("investigation", nc.pk))
    text = page.content.decode()
    assert page.status_code == 200 and CHANGED in text and "A worn print head." in text
    assert drawn_revision(page) == data["v"]  # the revision typed against, not the current one
    # The page's own token carries that revision too, so a second sign-out keeps it.
    assert kept._read(marker(page)[0])["v"] == data["v"]

    refused = browser.post(
        url("investigation", nc.pk), {**typed, "expected_revision": drawn_revision(page)}
    )
    text = refused.content.decode()
    assert refused.status_code == 200 and html.escape(STALE) in text
    assert "A worn print head." in text
    assert fresh(nc).root_cause == "" and fresh(nc).revision == current.revision


def test_a_form_no_longer_offered_still_gives_back_typing_from_before(record, action, agent, tech):
    """The action was finished after the typing: Edit action is no longer offered, yet the copy
    typed against the earlier revision is drawn, and its save is refused as changed since."""
    nc = fresh(record)
    typed = {
        "description": "Replace the print head and the platen roller.",
        "assignee": str(tech.pk),
        "due_on": action.due_on.isoformat(),
        "reason": "The roller is worn too.",
    }
    browser, data = kept_then_signed_in(agent, url("action-edit", nc.pk, action.pk), typed)
    services.finish_action(
        actor=tech, nc=nc, expected_revision=nc.revision, action=action, done=True, note="Done."
    )
    page = browser.get(url("action-edit", nc.pk, action.pk))
    text = page.content.decode()
    assert page.status_code == 200 and CHANGED in text and "platen roller" in text
    assert drawn_revision(page) == data["v"]
    refused = browser.post(
        url("action-edit", nc.pk, action.pk), {**typed, "expected_revision": data["v"]}
    )
    assert refused.status_code == 200 and html.escape(STALE) in refused.content.decode()
    assert "platen roller" in refused.content.decode()
    assert fresh(action).status == ActionStatus.DONE
    assert "platen" not in fresh(action).description


def test_a_reader_no_longer_allowed_gets_typing_from_before_back(record, agent, lead, lead2):
    """The owner changed after the typing: the former owner may no longer work the record, yet
    gets the typing back with the earlier revision, and its save is refused as changed since,
    not answered 403."""
    nc = fresh(record)
    typed = {"containment": "Labels reprinted.", "root_cause": "", "similar_cases": ""}
    browser, data = kept_then_signed_in(agent, url("investigation", nc.pk), typed)
    services.set_owner(actor=lead, nc=nc, expected_revision=nc.revision, owner=lead2)
    page = browser.get(url("investigation", nc.pk))
    assert page.status_code == 200 and "Labels reprinted." in page.content.decode()
    assert CHANGED in page.content.decode()
    refused = browser.post(url("investigation", nc.pk), {**typed, "expected_revision": data["v"]})
    assert refused.status_code == 200 and html.escape(STALE) in refused.content.decode()
    assert fresh(nc).containment == ""


def test_a_refusal_at_the_same_revision_leaves_the_copy(record, lead):
    """A supervisor's role removed after the typing (the record unchanged): the owner form is
    refused, and the copy waits, untaken."""
    nc = fresh(record)
    typed = {"owner": str(lead.pk), "reason": "Taking it over."}
    browser, _data = kept_then_signed_in(lead, url("owner", nc.pk), typed)
    lead.groups.clear()
    lead.groups.add(Group.objects.get_or_create(name="client_services")[0])
    assert browser.get(url("owner", nc.pk)).status_code == 403
    copy = KeptDraft.objects.get()
    assert copy.taken_at is None and copy.values["reason"] == "Taking it over."


def test_a_form_not_offered_at_the_same_revision_leaves_the_copy(record, action, agent, tech):
    """A copy whose revision is the record's own while the form is not offered (made here by
    moving the copy's revision forward): the page sends the reader back to the record and the
    copy waits, untaken."""
    nc = fresh(record)
    typed = {"description": "Replace the print head.", "assignee": str(tech.pk),
             "due_on": action.due_on.isoformat(), "reason": "Clarified."}  # fmt: skip
    browser, _data = kept_then_signed_in(agent, url("action-edit", nc.pk, action.pk), typed)
    services.finish_action(
        actor=tech, nc=nc, expected_revision=nc.revision, action=action, done=True, note="Done."
    )
    KeptDraft.objects.update(revision=fresh(nc).revision)
    page = browser.get(url("action-edit", nc.pk, action.pk))
    assert page.status_code == 302 and page.url == url("detail", nc.pk)
    assert KeptDraft.objects.get().taken_at is None


def test_the_dialog_gives_nothing_back(record, agent):
    """The form dialog loads the form (htmx): nothing is given back there; signing in again
    opens the form's own page, where it is."""
    nc = fresh(record)
    typed = {"containment": "Labels reprinted.", "root_cause": "", "similar_cases": ""}
    browser, _data = kept_then_signed_in(agent, url("investigation", nc.pk), typed)
    loaded = browser.get(url("investigation", nc.pk), HTTP_HX_REQUEST="true")
    assert loaded.status_code == 200 and PUT_BACK not in loaded.content.decode()
    assert KeptDraft.objects.get().taken_at is None
    page = browser.get(url("investigation", nc.pk))
    assert PUT_BACK in page.content.decode() and "Labels reprinted." in page.content.decode()


def test_a_person_chosen_comes_back_by_id_while_still_active(record, agent, tech, make_user):
    nc = fresh(record)
    due = (timezone.localdate() + timedelta(days=3)).isoformat()
    typed = {"description": "Clean the rollers.", "assignee": str(tech.pk), "due_on": due}
    browser, _data = kept_then_signed_in(agent, url("action-add", nc.pk), typed)
    page = browser.get(url("action-add", nc.pk))
    given = page.wsgi_request.vr_kept_given[f"nc-{nc.pk}-action-add"]
    assert given["assignee"] == str(tech.pk) and given["due_on"] == due
    assert f'value="{due}"' in page.content.decode()
    assert re.search(rf'<option value="{tech.pk}" selected', page.content.decode())

    # Deactivated meanwhile: the rest comes back, and the page says something did not.
    end_open(agent)  # the first browser's session
    browser, _data = kept_then_signed_in(agent, url("action-add", nc.pk), typed)
    type(tech).objects.filter(pk=tech.pk).update(is_active=False)
    page = browser.get(url("action-add", nc.pk))
    text = page.content.decode()
    assert "Clean the rollers." in text
    assert re.search(r"data-vr-kept-missed(?![^>]*hidden)", text)
    assert not re.search(rf'<option value="{tech.pk}" selected', text)


def test_a_new_record_comes_back(module_on, tech):
    typed = {
        "title": "Centrifuge 3 overheated",
        "description": "Stopped mid-run on bench 4.",
        "source": "equipment",
        "severity": "major",
        "area": "Core lab",
        "containment": "Runs moved to centrifuge 2.",
    }
    browser, data = kept_then_signed_in(tech, url("create"), typed)
    assert data["v"] is None
    page = browser.get(url("create"))
    text = page.content.decode()
    assert PUT_BACK in text and CHANGED not in text and "Runs moved to centrifuge 2." in text
    saved = browser.post(url("create"), typed)
    assert saved.status_code == 302
    assert Nonconformance.objects.get().title == "Centrifuge 3 overheated"


def test_a_complaint_changed_after_the_typing_refuses_the_save(agent, complaint, module_on):
    typed = {
        "title": "Late results for one practice",
        "description": "Three late reports in a week.",
        "severity": "minor",
        "area": "Reporting",
        "containment": "",
    }
    address = url("from-ticket", complaint.pk)
    browser, data = kept_then_signed_in(agent, address, typed)
    ticket = fresh(complaint)
    desk_services.add_entry(
        actor=agent,
        request=ticket,
        kind="note",
        body="Called the practice back.",
        expected_revision=ticket.revision,
    )
    page = browser.get(address)
    text = page.content.decode()
    assert page.status_code == 200 and CHANGED in text and "Three late reports" in text
    assert drawn_revision(page) == data["v"] < fresh(complaint).revision
    refused = browser.post(address, {**typed, "expected_revision": data["v"]})
    assert refused.status_code == 200 and "Three late reports" in refused.content.decode()
    assert not Nonconformance.objects.exists()


def test_a_complaint_opened_meanwhile_still_gives_the_typing_back(
    agent, lead, complaint, module_on
):
    """Another person opened the complaint's record after the typing: the page is drawn with the
    typing instead of sending the reader to that record, and its save is refused."""
    typed = {"title": "Late results", "description": "Three late reports.", "severity": "minor",
             "area": "", "containment": ""}  # fmt: skip
    address = url("from-ticket", complaint.pk)
    browser, data = kept_then_signed_in(agent, address, typed)
    ticket = fresh(complaint)
    services.open_from_complaint(
        actor=lead,
        request=ticket,
        expected_revision=ticket.revision,
        title="Late results (supervisor)",
        description="Opened by the supervisor.",
        severity="minor",
    )
    page = browser.get(address)
    assert page.status_code == 200 and "Three late reports." in page.content.decode()
    refused = browser.post(address, {**typed, "expected_revision": data["v"]})
    assert refused.status_code == 200 and "Three late reports." in refused.content.decode()
    assert Nonconformance.objects.count() == 1
    # Without a copy, the page sends the reader to that record, as before.
    assert browser.get(address).status_code == 302


def test_a_link_removed_after_the_typing_still_gives_the_reason_back(record, link, agent, lead):
    nc = fresh(record)
    address = url("ticket-unlink", nc.pk, link.pk)
    browser, data = kept_then_signed_in(agent, address, {"reason": "Linked by mistake."})
    services.unlink_ticket(
        actor=lead, nc=nc, expected_revision=nc.revision, link=link, reason="Not related."
    )
    page = browser.get(address)
    assert page.status_code == 200 and "Linked by mistake." in page.content.decode()
    refused = browser.post(
        address, {"reason": "Linked by mistake.", "expected_revision": data["v"]}
    )
    assert refused.status_code == 200 and "Linked by mistake." in refused.content.decode()
    # Without a copy, a removed link's form is not found, as before.
    assert browser.get(address).status_code == 404


# ------------------------------------------------------------------------------ kept from a post


def test_a_form_posted_after_the_session_ended_keeps_the_posted_revision(record, agent):
    nc = fresh(record)
    browser = browser_for(agent)
    page = browser.get(url("investigation", nc.pk))
    token = marker(page)[0]
    end_open(agent)
    posted = browser.post(
        url("investigation", nc.pk),
        {
            "expected_revision": nc.revision,
            "containment": "",
            "root_cause": "Worn print head.",
            "similar_cases": "",
            kept.FIELD: token,
        },
        HTTP_X_VR_FORM="true",
    )
    assert posted.status_code == 401 and posted.json()["kept"] is True
    copy = KeptDraft.objects.get()
    assert copy.revision == nc.revision and copy.values["root_cause"] == "Worn print head."
    assert fresh(nc).root_cause == ""


def test_a_refused_save_is_marked_with_the_revision_it_posted(record, agent, lead):
    nc = fresh(record)
    old = nc.revision
    services.change_status(
        actor=lead, nc=nc, expected_revision=nc.revision, status=Status.INVESTIGATING, reason=""
    )
    browser = browser_for(agent)
    refused = browser.post(
        url("investigation", nc.pk),
        {"expected_revision": old, "containment": "", "root_cause": "Worn.", "similar_cases": ""},
    )
    assert refused.status_code == 200 and html.escape(STALE) in refused.content.decode()
    assert kept._read(marker(refused)[0])["v"] == old
    # A revision that is no number marks nothing: what a save is checked against is not known.
    garbled = browser.post(
        url("investigation", nc.pk),
        {"expected_revision": "x", "containment": "", "root_cause": "Worn.", "similar_cases": ""},
    )
    assert garbled.status_code == 200 and marker(garbled) is None


def test_a_copy_without_a_revision_is_never_saved_over_the_record(record, agent):
    """A copy of a revision form that holds no revision (never kept so: made here directly) is
    drawn with revision 0, which no record has, so its save is refused."""
    nc = fresh(record)
    typed = {"containment": "Labels reprinted.", "root_cause": "", "similar_cases": ""}
    browser, _data = kept_then_signed_in(agent, url("investigation", nc.pk), typed)
    KeptDraft.objects.update(revision=None)
    page = browser.get(url("investigation", nc.pk))
    assert drawn_revision(page) == 0 and CHANGED in page.content.decode()
    refused = browser.post(url("investigation", nc.pk), {**typed, "expected_revision": 0})
    assert refused.status_code == 200 and html.escape(STALE) in refused.content.decode()
    assert fresh(nc).containment == ""


def test_the_same_text_against_another_revision_is_another_copy():
    stored = ({"note": "Checked."}, {"note": "text"}, False, 4)
    assert kept._same_copy(stored, ({"note": "Checked."}, {"note": "text"}, False, 4))
    assert not kept._same_copy(stored, ({"note": "Checked."}, {"note": "text"}, False, 5))
    assert not kept._same_copy(stored, ({"note": "Checked."}, {"note": "text"}, False, None))


def test_a_token_naming_a_revision_that_is_no_whole_number_is_refused(agent):
    from django.core import signing

    base = {"u": agent.pk, "s": "00000000-0000-0000-0000-000000000001", "k": "nc-1-edit",
            "f": ["note"], "t": ["note"], "w": {"note": "text"}}  # fmt: skip
    for bad in (0, -1, "3", True, 1.5):
        token = signing.dumps({**base, "v": bad}, salt=kept.SALT, compress=True)
        assert kept._read(token) is None, bad
    token = signing.dumps({**base, "v": 3}, salt=kept.SALT, compress=True)
    assert kept._read(token)["v"] == 3
