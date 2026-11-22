"""Regression checks for session-limited nonconformance form journeys on synthetic records.

Adopted from the independent review of the nonconformance kept typing (round 1). One case is a
strict xfail: the owner picker offers an account whose last staff role ended, which predates this
change (the fresh picker offers it too, and the service refuses the save)."""

import hashlib
import json
import logging
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django import forms
from django.contrib.messages import get_messages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from accounts.roles import ROLES
from core.models import AuditEvent
from desk import services as tickets
from desk.tests.test_kept_typing import browser_for, end_open
from nonconformance import services, views
from nonconformance.models import Nonconformance, Status
from nonconformance.tests import test_review_round1 as original

pytestmark = pytest.mark.django_db
owned = original.owned
decided = original.decided
url = original.nc_url

DRAFT = "Check the handwriting on the synthetic dispatch sheet."
CHANGED = "data-vr-kept-changed"
TICKET_STALE = "This ticket changed. Reload it and review the latest entries."


class PostedControls(HTMLParser):
    """Read the inputs, single selects, radios and textareas from the dedicated POST form."""

    def __init__(self, page):
        super().__init__()
        assert page.status_code == 200
        self.inside = False
        self.forms = 0
        self.values = {}
        self.select = None
        self.textarea = None
        self.keep_names = []
        self.feed(page.text)
        self.close()
        assert self.forms == 1, "Expected one dedicated editing form"

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        name = attrs.get("name")
        if tag == "form":
            self.inside = "vr-form" in attrs.get("class", "").split() and attrs["method"] == "post"
            self.forms += self.inside
        if not self.inside or "disabled" in attrs:
            return
        if tag == "input" and name:
            if name == kept.FIELD:
                self.keep_names = json.loads(attrs["data-vr-kept-fields"])
            if attrs.get("type") not in ("radio", "checkbox") or "checked" in attrs:
                self.values[name] = attrs.get("value", "")
        elif tag == "select":
            self.select = name
        elif (
            tag == "option"
            and self.select
            and ("selected" in attrs or self.select not in self.values)
        ):
            self.values[self.select] = attrs["value"]
        elif tag == "textarea" and name:
            self.textarea = name
            self.values[name] = ""

    def handle_data(self, data):
        if self.textarea:
            self.values[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self.select = None
        elif tag == "textarea" and self.textarea:
            self.values[self.textarea] = self.values[self.textarea].removeprefix("\n")
            self.textarea = None


def receipt(browser, token, values):
    response = browser.post(
        reverse("account-session-keep"),
        {"forms": [{"token": token, "values": values}]},
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()


def keep_page(browser, user, page, typed):
    controls = PostedControls(page)
    fields = controls.values
    token = fields[kept.FIELD]
    names = controls.keep_names
    assert typed.keys() <= set(names)
    end_open(user)
    values = {name: fields.get(name, "") for name in names}
    assert receipt(browser, token, {**values, **typed}) == {"kept": 1, "held": [True]}
    browser.force_login(user)
    return fields


def pending(user, address, typed):
    browser = browser_for(user)
    original_fields = keep_page(browser, user, browser.get(address), typed)
    return browser, original_fields


def investigation(nc, actor, text):
    nc.refresh_from_db()
    return services.record_investigation(
        actor=actor,
        nc=nc,
        expected_revision=nc.revision,
        containment=nc.containment,
        root_cause=text,
        similar_cases=nc.similar_cases,
    )


def ready_to_close(nc, actor):
    nc = services.change_status(
        actor=actor, nc=nc, expected_revision=nc.revision, status=Status.VERIFICATION
    )
    nc = services.record_impact(
        actor=actor,
        nc=nc,
        expected_revision=nc.revision,
        impact="none",
        impact_assessment="Checked the example dispatch sheet.",
    )
    return services.record_effectiveness(
        actor=actor,
        nc=nc,
        expected_revision=nc.revision,
        result="no_action_needed",
        note="Check complete.",
    )


SUCCESS = {
    "edit": "Details saved.",
    "owner": "Owner saved.",
    "status": "Status changed.",
    "investigation": "Investigation saved.",
    "impact": "Impact on reported results saved.",
    "effectiveness": "Effectiveness check recorded.",
    "close": "Nonconformance closed.",
    "reopen": "Nonconformance reopened.",
    "action-add": "Action added.",
    "action-edit": "Action saved.",
    "action-done": "Action marked done.",
    "action-cancel": "Action cancelled.",
    "ticket-link": "Ticket linked.",
    "ticket-unlink": "Linked ticket removed.",
}


@pytest.mark.parametrize("restored", [False, True], ids=["ordinary", "restored"])
@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "changed"])
@pytest.mark.parametrize("name", SUCCESS)
def test_record_form_posts_its_rendered_revision(
    name, changed, restored, owned, agent, lead, tech, plain_ticket
):
    ids = [owned.pk]
    due = timezone.localdate().isoformat()
    action_values = {"description": DRAFT, "assignee": str(tech.pk), "due_on": due}
    if name in {"action-done", "action-cancel", "action-edit"}:
        action = services.add_action(
            actor=agent,
            nc=owned,
            expected_revision=owned.revision,
            description="Read the sheet.",
            assignee=agent,
            due_on=timezone.localdate(),
        )
        ids.append(action.pk)
    if name == "ticket-unlink":
        link = services.link_ticket(
            actor=lead, nc=owned, expected_revision=owned.revision, request=plain_ticket
        )
        ids.append(link.pk)
    if name in {"effectiveness", "close", "reopen"}:
        owned = ready_to_close(owned, agent)
        if name == "reopen":
            owned = services.close(
                actor=lead, nc=owned, expected_revision=owned.revision, note="Finished."
            )
    typed = {
        "edit": {"title": DRAFT, "reason": "Clarify the issue."},
        "owner": {"owner": str(tech.pk), "reason": DRAFT},
        "status": {"status": Status.INVESTIGATING.value, "reason": DRAFT},
        "investigation": {"similar_cases": DRAFT},
        "impact": {"impact": "none", "impact_assessment": DRAFT},
        "effectiveness": {"result": "effective", "note": DRAFT},
        "close": {"note": DRAFT},
        "reopen": {"reason": DRAFT},
        "action-add": action_values,
        "action-edit": {**action_values, "reason": "Clarify the action."},
        "action-done": {"note": DRAFT},
        "action-cancel": {"note": DRAFT},
        "ticket-link": {"ticket": plain_ticket.number},
        "ticket-unlink": {"reason": DRAFT},
    }[name]
    address = url(name, *ids)
    owned.refresh_from_db()
    old = str(owned.revision)
    user = lead if name in {"owner", "close", "reopen"} else agent
    browser = browser_for(user)
    page = browser.get(address)
    if restored:
        keep_page(browser, user, page, typed)
    if changed:
        if name == "reopen":
            services.reopen(
                actor=lead, nc=owned, expected_revision=owned.revision, reason="New information."
            )
        else:
            investigation(owned, lead, "The second reader checked the current copy.")
    owned.refresh_from_db()
    before = owned.revision
    if restored:
        page = browser.get(address)
        fields = PostedControls(page).values
        assert {key: fields.get(key) for key in typed} == typed
        assert (CHANGED in page.text) == changed
    else:
        fields = {**PostedControls(page).values, **typed}
    assert fields["expected_revision"] == old
    assert "no-store" in page["Cache-Control"]
    event_count = AuditEvent.objects.count()
    response = browser.post(address, fields)
    owned.refresh_from_db()
    if changed:
        assert (
            response.status_code == 200
            and services.STALE in response.context["form"].non_field_errors()
        )
        again = PostedControls(response).values
        assert {key: again.get(key) for key in typed} == typed
        assert owned.revision == before and AuditEvent.objects.count() == event_count
    else:
        assert response.status_code == 302 and response.url == url("detail", owned.pk)
        assert SUCCESS[name] in [str(message) for message in get_messages(response.wsgi_request)]
        assert owned.revision == before + 1
        assert AuditEvent.objects.count() > event_count


@pytest.mark.parametrize("reviewed", [False, True], ids=["unchanged", "review-now-counts"])
def test_complaint_opening_uses_the_rendered_ticket_revision(
    decided, module_on, agent, lead, reviewed
):
    address = url("from-ticket", decided.pk)
    typed = {"title": DRAFT, "description": "Courier dispatch review.", "severity": "minor"}
    browser, original_fields = pending(agent, address, typed)
    if reviewed:
        tickets.review_complaint(
            actor=lead, request=decided, expected_revision=decided.revision, note="Reviewed."
        )
    page = browser.get(address)
    fields = PostedControls(page).values
    assert fields["expected_revision"] == original_fields["expected_revision"]
    assert {key: fields.get(key) for key in typed} == typed
    assert (CHANGED in page.text) == reviewed
    saved = browser.post(address, fields)
    if reviewed:
        assert saved.status_code == 200 and TICKET_STALE in saved.context["form"].non_field_errors()
        assert (
            PostedControls(saved).values["title"] == DRAFT and not Nonconformance.objects.exists()
        )
        redirected = browser.get(address)
        assert redirected.status_code == 302 and redirected.url == reverse(
            "desk:request-detail", args=[decided.pk]
        )
    else:
        assert saved.status_code == 302
        assert Nonconformance.objects.get(source_request=decided).title == DRAFT


@pytest.mark.parametrize("restored", [False, True], ids=["ordinary", "restored"])
def test_invalid_stale_complaint_opening_explains_why_completing_it_cannot_save(
    module_on, complaint, agent, lead, restored
):
    browser = browser_for(agent)
    address = url("from-ticket", complaint.pk)
    page = browser.get(address)
    incomplete = {"title": DRAFT, "description": "", "severity": "minor"}
    fields = {**PostedControls(page).values, **incomplete}
    if restored:
        keep_page(browser, agent, page, incomplete)
    tickets.add_entry(
        actor=lead,
        request=complaint,
        expected_revision=complaint.revision,
        kind="note",
        body="Checked delivery.",
    )
    if restored:
        returned = browser.get(address)
        assert CHANGED in returned.text
        fields = PostedControls(returned).values
    page = browser.post(address, fields)
    assert page.status_code == 200 and page.context["form"].has_error("description", "required")
    assert PostedControls(page).values["title"] == DRAFT and not Nonconformance.objects.exists()
    assert TICKET_STALE in page.context["form"].non_field_errors()


@pytest.mark.parametrize("first", ["old", "new"])
def test_two_tabs_with_equal_text_keep_only_the_first_revision(owned, agent, lead, first):
    address = url("investigation", owned.pk)
    browser = browser_for(agent)
    old = PostedControls(browser.get(address)).values
    updated = investigation(owned, lead, "Confirm this edition of the dispatch sheet.")
    new = PostedControls(browser.get(address)).values
    assert old["expected_revision"] != new["expected_revision"] == str(updated.revision)
    first_tab, second_tab = (old, new) if first == "old" else (new, old)
    end_open(agent)
    values = {"similar_cases": DRAFT}
    assert receipt(browser, first_tab[kept.FIELD], values) == {"kept": 1, "held": [True]}
    assert receipt(browser, second_tab[kept.FIELD], values) == {"kept": 1, "held": [False]}
    browser.force_login(agent)
    returned = PostedControls(browser.get(address)).values
    assert returned["expected_revision"] == first_tab["expected_revision"]
    assert returned["similar_cases"] == DRAFT


@pytest.mark.parametrize("try_save", [False, True], ids=["restored-page", "refused-save"])
def test_second_timeout_keeps_text_and_original_revision(owned, lead, agent, try_save):
    address = url("investigation", owned.pk)
    browser, initial = pending(agent, address, {"similar_cases": DRAFT})
    investigation(owned, lead, "Independent entry.")
    restored = browser.get(address)
    if try_save:
        restored = browser.post(address, PostedControls(restored).values)
        assert services.STALE in restored.context["form"].non_field_errors()
    amended = DRAFT + " Checked again."
    keep_page(browser, agent, restored, {"similar_cases": amended})
    again = browser.get(address)
    fields = PostedControls(again).values
    assert (
        fields["similar_cases"] == amended
        and fields["expected_revision"] == initial["expected_revision"]
    )
    assert (
        CHANGED in again.text
        and services.STALE in browser.post(address, fields).context["form"].non_field_errors()
    )
    owned.refresh_from_db()
    assert owned.root_cause == "Independent entry." and owned.similar_cases == ""


def test_nothing_still_fits_keeps_the_revision_of_the_copy(owned, agent, lead, monkeypatch):
    """A field retired between releases is the only nonempty field in the original copy."""

    class EarlierForm(views.InvestigationForm):
        dispatch_note = forms.CharField(required=False)

    address = url("investigation", owned.pk)
    with monkeypatch.context() as earlier:
        earlier.setattr(views, "InvestigationForm", EarlierForm)
        browser, initial = pending(agent, address, {"dispatch_note": DRAFT})
    current = investigation(owned, lead, "Independent entry.")
    page = browser.get(address)
    assert "none of it fits" in page.text and DRAFT not in page.text
    fields = PostedControls(page).values
    assert fields["root_cause"] == current.root_cause
    assert (fields["expected_revision"], kept._read(fields[kept.FIELD])["v"]) == (
        initial["expected_revision"],
        int(initial["expected_revision"]),
    )


def test_revision_recognized_for_storage_fits_the_draft_column():
    """Check the form/column boundary without writing a draft."""
    from nonconformance.forms import InvestigationForm

    largest = connection.ops.integer_field_range("PositiveIntegerField")[1]
    form = InvestigationForm({"expected_revision": str(largest)})
    assert kept.drawn_revision(form) == largest
    outside_column = InvestigationForm({"expected_revision": str(largest + 1)})
    assert kept.drawn_revision(outside_column) is None


@pytest.mark.parametrize("role", [*ROLES, "roleless", "portal"])
def test_switching_accounts_in_the_same_browser_leaves_the_creators_copy(
    module_on, agent, make_user, clinic, role
):
    address = url("create")
    browser, _ = pending(agent, address, {"title": DRAFT})
    if role == "portal":
        next_user = make_user("next_person", client=clinic)
    elif role == "roleless":
        next_user = make_user("next_person")
    else:
        next_user = make_user("next_person", role)
    browser.force_login(next_user)
    page = browser.get(address)
    assert page.status_code == (200 if role in ROLES else 403)
    assert DRAFT not in page.text and "no-store" in page["Cache-Control"]
    assert KeptDraft.objects.get().values["title"] == DRAFT
    browser.force_login(agent)
    assert PostedControls(browser.get(address)).values["title"] == DRAFT


def test_only_the_original_browser_form_and_record_take_the_copy(owned, agent, lead):
    address = url("investigation", owned.pk)
    browser, _ = pending(agent, address, {"similar_cases": DRAFT})
    second = browser_for(agent)
    other = services.raise_nonconformance(
        actor=lead,
        owner=agent,
        title="Another dispatch",
        description="A different example.",
        severity="minor",
        source="internal",
    )
    responses = [
        second.get(address),
        browser.get(url("edit", owned.pk)),
        browser.get(url("investigation", other.pk)),
        browser.head(address),
        browser.post(address, {"expected_revision": "", "similar_cases": "Another unsent note."}),
    ]
    for headers in (
        {"HX-Request": "true"},
        {"X-VR-Form": "1"},
        {"Sec-Purpose": "prefetch"},
        {"Purpose": "prefetch"},
    ):
        responses.append(browser.get(address, headers=headers))
    for page in responses:
        assert page.status_code == 200 and DRAFT not in page.text
        assert "no-store" in page["Cache-Control"]
    assert KeptDraft.objects.get().taken_at is None
    assert PostedControls(browser.get(address)).values["similar_cases"] == DRAFT
    assert PostedControls(browser.get(address)).values["similar_cases"] == ""


def test_link_outside_current_team_scope_leaves_the_copy_until_access_returns(
    owned, agent, lead, quality_team, restricted_ticket
):
    from teams.models import TeamMembership

    membership = TeamMembership.objects.create(team=quality_team, user=agent)
    link = services.link_ticket(
        actor=lead, nc=owned, expected_revision=owned.revision, request=restricted_ticket
    )
    address = url("ticket-unlink", owned.pk, link.pk)
    browser, _ = pending(agent, address, {"reason": DRAFT})
    membership.delete()
    page = browser.get(address)
    assert page.status_code == 404 and "no-store" in page["Cache-Control"]
    assert all(
        text not in page.text
        for text in [DRAFT, restricted_ticket.subject, restricted_ticket.number]
    )
    assert KeptDraft.objects.get().values["reason"] == DRAFT
    TeamMembership.objects.create(team=quality_team, user=agent)
    assert PostedControls(browser.get(address)).values["reason"] == DRAFT


def test_a_former_supervisors_older_copy_can_be_read_then_refused(owned, lead, lead2, tech):
    address = url("owner", owned.pk)
    browser, _ = pending(lead, address, {"owner": str(tech.pk), "reason": DRAFT})
    investigation(owned, lead2, "Follow-up recorded.")
    lead.groups.set(tech.groups.all())
    page = browser.get(address)
    fields = PostedControls(page).values
    assert CHANGED in page.text and fields["reason"] == DRAFT
    result = browser.post(address, fields)
    assert result.status_code == 200 and services.STALE in result.context["form"].non_field_errors()
    owned.refresh_from_db()
    assert owned.owner_id != tech.pk and browser.get(address).status_code == 403


@pytest.mark.xfail(
    strict=True,
    reason="Predates this change: the staff picker offers every active account that is not a "
    "portal account, also on a fresh form, while the service requires a staff role and refuses "
    "the save with the input kept; a follow-up aligns the picker with the service.",
)
def test_staff_picker_leaves_out_a_choice_whose_last_staff_role_ended(owned, lead, tech):
    address = url("owner", owned.pk)
    browser, _ = pending(lead, address, {"owner": str(tech.pk), "reason": DRAFT})
    tech.groups.clear()
    page = browser.get(address)
    fields = PostedControls(page).values
    assert fields["reason"] == DRAFT and page.status_code == 200
    if fields["owner"] == str(tech.pk):
        refused = browser.post(address, fields)
        assert refused.status_code == 200 and refused.context["form"].has_error("owner")
        owned.refresh_from_db()
        assert owned.owner_id != tech.pk
    assert fields["owner"] != str(tech.pk), (
        "The restored form still selects someone the service cannot assign"
    )


@pytest.mark.parametrize("dialog", [False, True], ids=["page", "dialog"])
def test_expired_post_returns_to_the_form_and_keeps_its_text_private(
    owned, agent, caplog, mailoutbox, dialog
):
    from datetime import timedelta

    from django.db.models.functions import Now

    from jobs.models import Notice

    address = url("investigation", owned.pk)
    browser = browser_for(agent)
    fields = PostedControls(browser.get(address)).values
    before = AuditEvent.objects.count(), Notice.objects.count(), len(mailoutbox)
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    headers = (
        {"X-VR-Form": "1", "HX-Current-URL": "http://testserver" + url("detail", owned.pk)}
        if dialog
        else {}
    )
    with caplog.at_level(logging.INFO):
        refused = browser.post(address, {**fields, "similar_cases": DRAFT}, headers=headers)
        assert refused.status_code == (401 if dialog else 302)
        target = urlsplit(refused["HX-Redirect" if dialog else "Location"])
        assert not target.netloc and target.path == reverse("account_login")
        assert parse_qs(target.query) == {"next": [address]}
        browser.force_login(agent)
        page = browser.get(address)
    assert PostedControls(page).values["similar_cases"] == DRAFT
    assert {"lab_ops.signin", "lab_ops.request"} <= {entry.name for entry in caplog.records}
    for response in (refused, page):
        assert "no-store" in response["Cache-Control"]
        assert DRAFT not in str(response.headers) + response.cookies.output()
        assert DRAFT not in str([str(message) for message in get_messages(response.wsgi_request)])
    assert DRAFT not in caplog.text + browser.cookies.output()
    assert (AuditEvent.objects.count(), Notice.objects.count(), len(mailoutbox)) == before
    owned.refresh_from_db()
    assert owned.similar_cases == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_a_change_committed_between_reading_and_take_is_seen_on_the_following_page(
    owned, agent, lead, monkeypatch, atomic_requests
):
    from desk.tests.test_review_jobs_account_and_drawn_page import during_page

    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    address = url("investigation", owned.pk)
    browser, initial = pending(agent, address, {"similar_cases": DRAFT})
    later = "Checked on a separate connection."
    page = during_page(
        views, "_take", lambda: investigation(owned, lead, later), lambda: browser.get(address)
    )
    fields = PostedControls(page).values
    assert fields["expected_revision"] == initial["expected_revision"]
    assert fields["similar_cases"] == DRAFT and CHANGED not in page.text
    result = browser.post(address, fields)
    assert services.STALE in result.context["form"].non_field_errors()
    fresh = PostedControls(browser.get(address)).values
    assert fresh["root_cause"] == later and fresh["similar_cases"] == ""
    assert int(fresh["expected_revision"]) == int(initial["expected_revision"]) + 1


@pytest.mark.django_db(transaction=True)
def test_simultaneous_pages_return_the_same_copy_only_once(owned, agent):
    from django.test import Client

    from desk.tests.test_review_jobs_account_and_drawn_page import during_page

    address = url("investigation", owned.pk)
    browser, _ = pending(agent, address, {"similar_cases": DRAFT})
    tab = Client()
    tab.cookies = browser.cookies.copy()  # two tabs using one signed-in browser
    shown_to_other_tab = []

    def read_other_tab():
        page = tab.get(address)
        shown_to_other_tab.append(PostedControls(page).values["similar_cases"])

    page = during_page(views, "_take", read_other_tab, lambda: browser.get(address))
    assert shown_to_other_tab == [DRAFT] and PostedControls(page).values["similar_cases"] == ""
    assert KeptDraft.objects.get().taken_at is not None


@pytest.mark.django_db(transaction=True)
def test_role_granted_during_form_draw_first_appears_on_the_following_get(module_on, agent, lead):
    from desk.tests.test_review_jobs_account_and_drawn_page import during_page

    browser = browser_for(agent)
    address = url("create")
    grant = lead.groups.get(name="supervisor")
    # Pause before the role check that decides if this GET includes the owner picker.
    page = during_page(
        views, "is_supervisor", lambda: agent.groups.add(grant), lambda: browser.get(address)
    )
    assert "owner" not in PostedControls(page).values
    assert "owner" in PostedControls(browser.get(address)).values


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_late_post_error_observes_the_deployments_request_transaction(
    owned, agent, monkeypatch, atomic_requests
):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    browser = browser_for(agent)
    address = url("investigation", owned.pk)
    fields = PostedControls(browser.get(address)).values

    def interrupt(*args, **kwargs):
        assert connection.in_atomic_block == atomic_requests
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            assert cursor.fetchone() == ("read committed",)
        raise RuntimeError("Synthetic response failure after the service returned")

    monkeypatch.setattr(views.messages, "success", interrupt)
    with pytest.raises(RuntimeError, match="Synthetic response failure"):
        browser.post(address, {**fields, "root_cause": DRAFT})
    owned.refresh_from_db()
    changed = AuditEvent.objects.filter(
        action="nonconformance.investigation_recorded", object_id=str(owned.pk)
    ).exists()
    assert changed == (not atomic_requests)
    assert owned.root_cause == ("" if atomic_requests else DRAFT)


@pytest.mark.django_db(transaction=True)
def test_older_ticket_copies_survive_upgrade_and_an_old_writer(
    client, agent, plain_ticket, complaint
):
    before = ("accounts", "0005_kept_draft")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_draft = executor.loader.project_state([before]).apps.get_model("accounts", "KeptDraft")

    def address(ticket):
        return reverse("desk:request-detail", args=[ticket.pk])

    client.force_login(agent)
    assert client.get(address(plain_ticket)).status_code == 200
    digest = hashlib.sha256(client.cookies[kept.BROWSER_COOKIE].value.encode()).hexdigest()
    session = end_open(agent)

    def write_old(ticket, value):
        return old_draft.objects.create(
            session_id=session.pk,
            user_id=agent.pk,
            form=f"ticket-entry-{ticket.pk}",
            browser=digest,
            values={"body": value},
            controls={"body": "text"},
        )

    try:
        executor.migrate([before])
        first = write_old(plain_ticket, "Synthetic text kept before migration.")
    finally:
        MigrationExecutor(connection).migrate(latest)
    second = write_old(complaint, "Synthetic text kept during rollout.")
    client.force_login(agent)
    for ticket, old in ((plain_ticket, first), (complaint, second)):
        draft = KeptDraft.objects.get(pk=old.pk)
        assert draft.revision is None and draft.values == old.values
        page = client.get(address(ticket))
        assert page.status_code == 200 and old.values["body"] in page.text
        draft.refresh_from_db()
        assert draft.values is None and draft.taken_at is not None


@pytest.mark.parametrize("check", ["warning", "second-timeout"])
def test_new_form_copy_held_by_a_previous_release_worker(owned, agent, check):
    """0005 can insert a copy of a new worker's form without recording its revision."""
    browser = browser_for(agent)
    address = url("investigation", owned.pk)
    fields = PostedControls(browser.get(address)).values
    session = end_open(agent)
    old = MigrationExecutor(connection).loader.project_state([("accounts", "0005_kept_draft")])
    old.apps.get_model("accounts", "KeptDraft").objects.create(
        session_id=session.pk,
        user_id=agent.pk,
        form=f"nc-{owned.pk}-investigation",
        browser=hashlib.sha256(browser.cookies[kept.BROWSER_COOKIE].value.encode()).hexdigest(),
        values={"similar_cases": DRAFT},
        controls={"similar_cases": "text"},
    )
    browser.force_login(agent)
    page = browser.get(address)
    returned = PostedControls(page).values
    assert returned["similar_cases"] == DRAFT and returned["expected_revision"] == "0"
    owned.refresh_from_db()
    assert int(fields["expected_revision"]) == owned.revision
    if check == "warning":
        # It is correct to refuse when the original version is unknown. The reason must be accurate.
        assert (
            "This record changed after you typed it, so saving this form is refused."
            not in page.text
        )
    else:
        end_open(agent)
        amended = DRAFT + " Another dispatch needs checking."
        result = receipt(browser, returned.get(kept.FIELD, ""), {"similar_cases": amended})
        assert result["held"] == [True]
        browser.force_login(agent)
        again = PostedControls(browser.get(address)).values
        assert again["similar_cases"] == amended and again["expected_revision"] == "0"


def test_a_closed_record_returns_the_old_investigation_then_redirects(owned, agent, lead):
    owned = ready_to_close(owned, agent)
    address = url("investigation", owned.pk)
    browser, initial = pending(agent, address, {"root_cause": DRAFT})
    closed = services.close(
        actor=lead, nc=owned, expected_revision=owned.revision, note="All checks complete."
    )
    page = browser.get(address)
    fields = PostedControls(page).values
    assert (
        fields["expected_revision"] == initial["expected_revision"]
        and fields["root_cause"] == DRAFT
    )
    refused = browser.post(address, fields)
    assert services.STALE in refused.context["form"].non_field_errors()
    assert PostedControls(refused).values["root_cause"] == DRAFT
    again = browser.get(address)
    assert again.status_code == 302 and again.url == url("detail", owned.pk)
    assert services.CLOSED in [str(m) for m in get_messages(again.wsgi_request)]
    owned.refresh_from_db()
    assert owned.revision == closed.revision and owned.status == Status.CLOSED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic_requests", [False, True])
def test_a_render_failure_leaves_the_taken_copy_for_retry(
    owned, agent, monkeypatch, atomic_requests
):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", atomic_requests)
    address = url("investigation", owned.pk)
    browser, _ = pending(agent, address, {"root_cause": DRAFT})

    def render_fails(*args, **kwargs):
        assert KeptDraft.objects.get().taken_at is not None
        raise RuntimeError("Interrupted synthetic form render")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(views, "render", render_fails)
        with pytest.raises(RuntimeError, match="Interrupted synthetic form render"):
            browser.get(address)
    assert KeptDraft.objects.get().values["root_cause"] == DRAFT
    assert PostedControls(browser.get(address)).values["root_cause"] == DRAFT


def test_reopen_help_describes_when_reopen_is_unavailable(owned, agent, lead):
    nc = ready_to_close(owned, agent)
    nc = services.close(actor=lead, nc=nc, expected_revision=nc.revision, note="Checked.")
    address = url("reopen", nc.pk)
    browser = browser_for(lead)
    closed_page = browser.get(address)
    assert closed_page.status_code == 200
    services.reopen(
        actor=lead, nc=nc, expected_revision=nc.revision, reason="Another sheet arrived."
    )
    open_page = browser.get(address)
    assert open_page.status_code == 302 and open_page.url == url("detail", nc.pk)
    assert (
        "form is no longer offered, for example after the record was closed" not in closed_page.text
    )


def test_nothing_put_back_cannot_clear_a_later_investigation(owned, agent, lead, monkeypatch):
    """Model a form upgrade that retires a note and replaces a checklist with free text."""

    class EarlierForm(views.InvestigationForm):
        dispatch_note = forms.CharField(required=False)
        similar_cases = forms.MultipleChoiceField(
            choices=[("am", "Morning dispatch"), ("pm", "Evening dispatch")],
            widget=forms.CheckboxSelectMultiple,
            required=False,
        )

    address = url("investigation", owned.pk)
    with monkeypatch.context() as earlier:
        earlier.setattr(views, "InvestigationForm", EarlierForm)
        browser, _ = pending(
            agent, address, {"dispatch_note": DRAFT, "similar_cases": ["am", "pm"]}
        )

    saved = investigation(owned, lead, "Checked the supervisor's dispatch sheet.")
    page = browser.get(address)
    assert "none of it fits" in page.text
    # Submit exactly what the newer form put on screen, without editing any control.
    before = AuditEvent.objects.filter(action="nonconformance.investigation_recorded").count()
    result = browser.post(address, PostedControls(page).values)
    owned.refresh_from_db()
    assert (owned.root_cause, owned.revision, result.status_code) == (
        saved.root_cause,
        saved.revision,
        200,
    )
    assert (
        AuditEvent.objects.filter(action="nonconformance.investigation_recorded").count() == before
    )


def test_new_nonconformance_keeps_the_text_when_the_owner_picker_is_no_longer_offered(
    lead, tech, module_on
):
    address = url("create")
    typed = {
        "title": DRAFT,
        "description": "The example manifest was incomplete.",
        "source": "internal",
        "severity": "minor",
        "owner": str(tech.pk),
    }
    browser, _ = pending(lead, address, typed)
    lead.groups.set(tech.groups.all())
    page = browser.get(address)
    fields = PostedControls(page).values
    assert "owner" not in fields and "expected_revision" not in fields
    assert page.context["kept"]["missed"] and fields["title"] == DRAFT
    result = browser.post(address, fields)
    record = Nonconformance.objects.get()
    assert result.status_code == 302 and result.url == url("detail", record.pk)
    assert record.title == DRAFT and record.owner_id is None


def test_a_complaint_out_of_reach_keeps_the_draft_for_a_later_readable_visit(
    module_on, agent, lead, quality_team, restricted_ticket
):
    from teams.models import TeamMembership

    ticket = tickets.set_complaint(
        actor=lead,
        request=restricted_ticket,
        expected_revision=restricted_ticket.revision,
        is_complaint=True,
        reason="Example classification.",
    )
    membership = TeamMembership.objects.create(team=quality_team, user=agent)
    ticket = tickets.assign(
        actor=lead,
        request=ticket,
        expected_revision=ticket.revision,
        team=quality_team,
        assignee=agent,
        reason="Review the example.",
    )
    address = url("from-ticket", ticket.pk)
    browser, _ = pending(agent, address, {"title": DRAFT})
    ticket = tickets.assign(
        actor=lead,
        request=ticket,
        expected_revision=ticket.revision,
        team=quality_team,
        assignee=None,
        reason="Return to the team.",
    )
    membership.delete()
    unavailable = browser.get(address)
    assert unavailable.status_code == 404
    assert all(text not in unavailable.text for text in (DRAFT, ticket.number, ticket.subject))
    assert KeptDraft.objects.get().values["title"] == DRAFT
    TeamMembership.objects.create(team=quality_team, user=agent)
    page = browser.get(address)
    assert CHANGED in page.text and PostedControls(page).values["title"] == DRAFT
