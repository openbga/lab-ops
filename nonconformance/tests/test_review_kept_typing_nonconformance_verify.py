"""Follow-up checks for restored revisions, repeat session endings and recovery instructions."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from django import forms
from django.core import signing
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from accounts import kept
from accounts.models import KeptDraft
from core.models import AuditEvent
from desk import services as tickets
from nonconformance import services, views
from nonconformance.forms import InvestigationForm
from nonconformance.models import Nonconformance
from nonconformance.tests import test_review_kept_typing_nonconformance as prior

pytestmark = pytest.mark.django_db
owned = prior.owned
decided = prior.decided
action = prior.original.action
linked = prior.original.linked

FORM_FIELDS = {
    "edit": "title",
    "owner": "reason",
    "status": "reason",
    "investigation": "similar_cases",
    "impact": "impact_assessment",
    "effectiveness": "note",
    "close": "note",
    "reopen": "reason",
    "action-add": "description",
    "action-edit": "description",
    "action-done": "note",
    "action-cancel": "note",
    "ticket-link": "ticket",
    "ticket-unlink": "reason",
    "from-ticket": "title",
}
MAX_REVISION = 2_147_483_647
UNKNOWN = "This form was kept without the version of the record"
CHANGED = "This record changed after you typed it, so saving this form is refused."
SECOND = "Follow up the afternoon dispatch."
DISCARD = "data-vr-kept-discard"


@pytest.fixture
def address(request, name, owned, lead, complaint):
    if name == "from-ticket":
        return prior.url(name, complaint.pk)
    if name == "ticket-unlink":
        return prior.url(name, owned.pk, request.getfixturevalue("linked").pk)
    if name in {"action-done", "action-edit", "action-cancel"}:
        return prior.url(name, owned.pk, request.getfixturevalue("action").pk)
    if name in {"close", "reopen", "effectiveness"}:
        record = prior.ready_to_close(owned, lead)
        if name == "reopen":
            services.close(
                actor=lead, nc=record, expected_revision=record.revision, note="Checked."
            )
    else:
        prior.investigation(owned, lead, "The original sheet is available.")
    return prior.url(name, owned.pk)


def assert_restored(page, revision, current, *, nothing):
    fields = prior.PostedControls(page).values
    assert fields["expected_revision"] == str(revision)
    assert kept._read(fields[kept.FIELD])["v"] == revision
    assert ("none of it fits" in page.text) is nothing
    assert (UNKNOWN in page.text) == (revision == 0)
    assert (CHANGED in page.text) == (0 < revision < current)
    assert (DISCARD in page.text) == (not nothing or revision != current)
    return fields


@pytest.mark.parametrize("name", FORM_FIELDS)
@pytest.mark.parametrize("vintage", ["same", "earlier", "zero", "legacy-null"])
@pytest.mark.parametrize("nothing", [False, True], ids=["drawable", "retired-field-only"])
def test_every_revision_form_through_two_session_endings(name, address, vintage, nothing, lead):
    field = FORM_FIELDS[name]
    first_text, second_text = (
        ("REQ-000017", "REQ-000018") if name == "ticket-link" else (prior.DRAFT, SECOND)
    )
    browser, fresh = prior.pending(lead, address, {field: first_text})
    current = int(fresh["expected_revision"])
    assert current > 1
    revision = {"same": current, "earlier": current - 1, "zero": 0, "legacy-null": None}[vintage]
    draft = KeptDraft.objects.get()
    # Represent the persisted form at the given vintage. In the retired-layout variant its
    # only answer belonged to a field that is no longer on the page.
    draft.revision = revision
    if nothing:
        draft.values = {"retired_dispatch_note": prior.DRAFT}
        draft.controls = {"retired_dispatch_note": "text"}
    draft.save(update_fields=["revision", "values", "controls"])

    first = browser.get(address)
    fields = assert_restored(first, revision or 0, current, nothing=nothing)
    assert (fields[field] == first_text) is (not nothing)
    assert kept._read(fields[kept.FIELD])["s"] != kept._read(fresh[kept.FIELD])["s"]
    prior.keep_page(browser, lead, first, {field: second_text})
    next_draft = KeptDraft.objects.get(taken_at__isnull=True)
    assert next_draft.revision == (revision or 0) and next_draft.values[field] == second_text
    second = browser.get(address)
    again = assert_restored(second, revision or 0, current, nothing=False)
    assert again[field] == second_text
    assert kept._read(again[kept.FIELD])["s"] != kept._read(fields[kept.FIELD])["s"]


@pytest.mark.parametrize("nothing", [False, True], ids=["drawable", "retired-field-only"])
def test_new_record_stays_revisionless_through_two_session_endings(module_on, agent, nothing):
    address = prior.url("create")
    browser, _ = prior.pending(agent, address, {"title": prior.DRAFT})
    if nothing:
        KeptDraft.objects.update(
            values={"retired_note": prior.DRAFT}, controls={"retired_note": "text"}
        )
    for expected in [None if nothing else prior.DRAFT, SECOND]:
        page = browser.get(address)
        fields = prior.PostedControls(page).values
        data = signing.loads(fields[kept.FIELD], salt=kept.SALT)
        assert "expected_revision" not in fields and "v" not in data
        assert (fields["title"] or None) == expected
        assert ("none of it fits" in page.text) == (expected is None)
        assert prior.CHANGED not in page.text
        if expected != SECOND:
            prior.keep_page(browser, agent, page, {"title": SECOND})


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        ("0", 0),
        ("00", 0),
        (1, 1),
        (str(MAX_REVISION), MAX_REVISION),
        (MAX_REVISION + 1, None),
        (str(MAX_REVISION + 1), None),
        (-1, None),
        (False, None),
        (0.0, None),
        ("", None),
        (None, None),
        ("０", None),
        ("0.0", None),
    ],
)
def test_revision_read_from_prefixed_bound_and_unbound_forms(raw, expected):
    assert kept._revision(raw) == expected
    bound = InvestigationForm(
        {"copy-expected_revision": raw}, initial={"expected_revision": 7}, prefix="copy"
    )
    unbound = InvestigationForm(initial={"expected_revision": raw}, prefix="copy")
    assert kept.drawn_revision(bound) == kept.drawn_revision(unbound) == expected


def test_drawn_revision_contract_accounts_for_zero():
    # A positive minimum in the helper's contract must distinguish the explicit unknown value.
    assert kept.drawn_revision(InvestigationForm({"expected_revision": "0"})) == 0
    contract = " ".join(kept.drawn_revision.__doc__.split())
    if "at least 1" in contract:
        assert re.search(r"\b(?:zero|0)\b", contract, flags=re.I), contract


@pytest.mark.parametrize("posted", ["0", str(MAX_REVISION), str(MAX_REVISION + 1)])
def test_bound_refusal_emits_only_a_storable_revision(owned, agent, posted):
    address = prior.url("investigation", owned.pk)
    browser = prior.browser_for(agent)
    fields = prior.PostedControls(browser.get(address)).values
    counts = (owned.revision, AuditEvent.objects.count())
    page = browser.post(
        address, {**fields, "expected_revision": posted, "similar_cases": prior.DRAFT}
    )
    rendered = prior.PostedControls(page).values
    assert rendered["expected_revision"] == posted and rendered["similar_cases"] == prior.DRAFT
    if int(posted) <= MAX_REVISION:
        token = rendered[kept.FIELD]
        assert kept._read(token)["v"] == int(posted)
        prior.end_open(agent)
        assert prior.receipt(browser, token, {"similar_cases": prior.DRAFT})["held"] == [True]
        assert KeptDraft.objects.get().revision == int(posted)
    else:
        assert kept.FIELD not in rendered
    owned.refresh_from_db()
    assert (owned.revision, AuditEvent.objects.count()) == counts


@pytest.mark.parametrize(
    "wire,valid",
    [
        (0, True),
        (MAX_REVISION, True),
        (MAX_REVISION + 1, False),
        (-1, False),
        ("0", False),
        (False, False),
        (0.0, False),
        (1.5, False),
    ],
)
def test_reader_accepts_only_integer_revisions_in_the_signed_marker(owned, agent, wire, valid):
    page = prior.browser_for(agent).get(prior.url("investigation", owned.pk))
    data = signing.loads(prior.PostedControls(page).values[kept.FIELD], salt=kept.SALT)
    test_marker = signing.dumps({**data, "v": wire}, salt=kept.SALT)
    assert (kept._read(test_marker) is not None) is valid


@pytest.mark.parametrize("gate", ["other-owner", "removed-link", "closed", "reviewed-complaint"])
def test_unknown_version_leaves_the_copy_at_an_unavailable_form(
    request, gate, owned, agent, lead, decided
):
    record_url = prior.url("detail", owned.pk)
    address = prior.url("investigation", owned.pk)
    field = "similar_cases"
    if gate == "removed-link":
        link = request.getfixturevalue("linked")
        address = prior.url("ticket-unlink", owned.pk, link.pk)
        field = "reason"
    elif gate == "closed":
        owned = prior.ready_to_close(owned, agent)
    elif gate == "reviewed-complaint":
        address = prior.url("from-ticket", decided.pk)
        field = "title"
    browser, fresh = prior.pending(agent, address, {field: prior.DRAFT})
    KeptDraft.objects.update(revision=0)
    if gate == "other-owner":
        services.set_owner(
            actor=lead,
            nc=owned,
            expected_revision=owned.revision,
            owner=lead,
            reason="Review reassigned.",
        )
    elif gate == "removed-link":
        services.unlink_ticket(
            actor=lead, nc=owned, expected_revision=owned.revision, link=link, reason="Checked."
        )
    elif gate == "closed":
        services.close(actor=lead, nc=owned, expected_revision=owned.revision, note="Reviewed.")
    else:
        tickets.review_complaint(
            actor=lead, request=decided, expected_revision=decided.revision, note="Checked."
        )
    response = browser.get(address)
    if gate in {"other-owner", "removed-link"}:
        status = 403 if gate == "other-owner" else 404
        assert response.status_code == status
        assert (
            browser.post(
                address, {**fresh, "expected_revision": "0", field: prior.DRAFT}
            ).status_code
            == status
        )
    else:
        destination = (
            record_url if gate == "closed" else reverse("desk:request-detail", args=[decided.pk])
        )
        assert response.status_code == 302 and response.url == destination
    assert prior.DRAFT not in response.text
    draft = KeptDraft.objects.get()
    assert draft.taken_at is None and draft.values[field] == prior.DRAFT


@pytest.mark.parametrize(
    "change,status", [("unassigned", 403), ("reclassified", 404), ("opened", 302)]
)
def test_zero_on_an_unavailable_complaint_matches_a_fresh_opening(
    module_on, complaint, agent, lead, change, status
):
    address = prior.url("from-ticket", complaint.pk)
    browser, original = prior.pending(agent, address, {"title": prior.DRAFT})
    KeptDraft.objects.update(revision=0)
    if change == "unassigned":
        tickets.assign(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            team=complaint.team,
            assignee=None,
            reason="Return to the team.",
        )
    elif change == "reclassified":
        tickets.set_complaint(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            is_complaint=False,
            reason="Corrected the classification.",
        )
    else:
        existing = services.open_from_complaint(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            title="A courier delay",
            severity="minor",
            description="Reviewed the dispatch.",
        )
    fresh = prior.browser_for(agent).get(address)
    page = browser.get(address)
    assert page.status_code == fresh.status_code == status
    if change == "opened":
        assert page.url == fresh.url == prior.url("detail", existing.pk)
    else:
        posted = browser.post(address, {**original, "expected_revision": "0", "title": prior.DRAFT})
        assert posted.status_code == status and prior.DRAFT not in posted.text
    draft = KeptDraft.objects.get()
    assert draft.taken_at is None and draft.revision == 0 and draft.values["title"] == prior.DRAFT


@pytest.mark.parametrize(
    "version,stale",
    [
        ("earlier", True),
        ("same", False),
        ("future", False),
        ("0", False),
        ("", False),
        ("invalid", False),
    ],
)
def test_incomplete_complaint_only_adds_stale_for_an_earlier_reading(
    module_on, complaint, agent, version, stale
):
    current = complaint.revision
    assert current > 1
    posted = {"earlier": current - 1, "same": current, "future": current + 1}.get(version, version)
    address = prior.url("from-ticket", complaint.pk)
    result = prior.browser_for(agent).post(
        address, {"expected_revision": str(posted), "title": prior.DRAFT}
    )
    assert result.status_code == 200
    form = result.context["form"]
    assert form.has_error("description", "required") and form.has_error("severity", "required")
    assert list(form.non_field_errors()) == ([prior.TICKET_STALE] if stale else [])
    assert prior.PostedControls(result).values["title"] == prior.DRAFT
    assert not Nonconformance.objects.filter(source_request=complaint).exists()


@pytest.mark.parametrize("version", ["earlier", "current", "future", "zero"])
def test_complete_complaint_uses_the_posted_ticket_revision(module_on, complaint, agent, version):
    address = prior.url("from-ticket", complaint.pk)
    browser = prior.browser_for(agent)
    fields = prior.PostedControls(browser.get(address)).values
    revision = int(fields["expected_revision"])
    posted = {"earlier": revision - 1, "current": revision, "future": revision + 1, "zero": 0}[
        version
    ]
    result = browser.post(
        address,
        {
            **fields,
            "expected_revision": str(posted),
            "title": prior.DRAFT,
            "severity": "minor",
            "description": "Check the dispatch.",
        },
    )
    records = Nonconformance.objects.filter(source_request=complaint)
    complaint.refresh_from_db()
    if version == "current":
        assert result.status_code == 302 and result.url == prior.url("detail", records.get().pk)
        assert complaint.revision == revision + 1
    else:
        assert result.status_code == 200
        assert list(result.context["form"].non_field_errors()) == [prior.TICKET_STALE]
        returned = prior.PostedControls(result).values
        assert returned["expected_revision"] == str(posted) and returned["title"] == prior.DRAFT
        assert not records.exists() and complaint.revision == revision


@pytest.mark.parametrize("same_text", [True, False], ids=["identical", "different"])
def test_repeat_delivery_of_an_unknown_version_during_upgrade(owned, agent, same_text):
    address = prior.url("investigation", owned.pk)
    browser, _ = prior.pending(agent, address, {"similar_cases": prior.DRAFT})
    KeptDraft.objects.update(revision=None)
    page = browser.get(address)
    fields = prior.PostedControls(page).values
    assert fields["expected_revision"] == "0"
    token = fields[kept.FIELD]
    data = kept._read(token)
    assert data["v"] == 0
    values = {name: fields.get(name, "") for name in data["f"]}
    ended = prior.end_open(agent)
    # An older worker handles the first delivery during this second timeout. Its model
    # inserts into the migrated table without a revision column in the INSERT.
    historical = MigrationExecutor(connection).loader.project_state(
        [("accounts", "0005_kept_draft")]
    )
    historical.apps.get_model("accounts", "KeptDraft").objects.create(
        user_id=agent.pk,
        session_id=ended.pk,
        form=data["k"],
        browser=hashlib.sha256(browser.cookies[kept.BROWSER_COOKIE].value.encode()).hexdigest(),
        values=values,
        controls=data["w"],
    )
    repeated = values if same_text else {**values, "similar_cases": SECOND}
    receipt = prior.receipt(browser, token, repeated)
    browser.force_login(agent)
    returned = prior.PostedControls(browser.get(address)).values
    assert returned["similar_cases"] == prior.DRAFT and returned["expected_revision"] == "0"
    assert receipt == {"kept": 1, "held": [same_text]}


def test_notice_identifies_a_restored_clear_on_the_same_revision(owned, agent, monkeypatch):
    class EarlierInvestigation(views.InvestigationForm):
        dispatch_note = forms.CharField(required=False)
        similar_cases = forms.MultipleChoiceField(
            choices=[("am", "Morning dispatch"), ("pm", "Evening dispatch")],
            widget=forms.CheckboxSelectMultiple,
            required=False,
        )

    address = prior.url("investigation", owned.pk)
    saved = prior.investigation(owned, agent, "The original dispatch was checked.")
    with monkeypatch.context() as earlier:
        earlier.setattr(views, "InvestigationForm", EarlierInvestigation)
        browser, _ = prior.pending(
            agent,
            address,
            {"root_cause": "", "dispatch_note": prior.DRAFT, "similar_cases": ["am", "pm"]},
        )
    restored = browser.get(address)
    fields = prior.PostedControls(restored).values
    assert fields["expected_revision"] == str(saved.revision)
    # A "none ... could be put back" screen must agree with the current form. If the clear was
    # restored, identify it as a restored edit and give the operator the usual Discard action.
    fresh = prior.PostedControls(browser.get(address)).values
    assert fresh["root_cause"] == saved.root_cause
    assert prior.DRAFT not in restored.text
    if "none of it fits" in restored.text:
        result = browser.post(address, fields)
        saved.refresh_from_db()
        assert saved.root_cause == fresh["root_cause"], (
            f"Saving the form labelled Not put back returned {result.status_code} "
            "and changed the stored root cause."
        )
    if fields["root_cause"] != fresh["root_cause"]:
        assert "none of it fits" not in restored.text and DISCARD in restored.text


@pytest.mark.parametrize("case", ["removed-link", "complaint-opened"])
def test_refused_form_help_only_offers_reachable_recovery(
    request, owned, complaint, agent, lead, case
):
    if case == "removed-link":
        linked = request.getfixturevalue("linked")
        address = prior.url("ticket-unlink", owned.pk, linked.pk)
        parent = prior.url("detail", owned.pk)
        key, field = "ticket-unlink", "reason"
        browser, _ = prior.pending(agent, address, {field: prior.DRAFT})
        services.unlink_ticket(
            actor=lead,
            nc=owned,
            expected_revision=owned.revision,
            link=linked,
            reason="Reconciled.",
        )
    else:
        address = prior.url("from-ticket", complaint.pk)
        parent = reverse("desk:request-detail", args=[complaint.pk])
        key, field = "from-ticket", "title"
        browser, _ = prior.pending(
            agent,
            address,
            {field: prior.DRAFT, "severity": "minor", "description": "Courier dispatch."},
        )
        existing = services.open_from_complaint(
            actor=lead,
            request=complaint,
            expected_revision=complaint.revision,
            title="Courier delay",
            description="Reviewed the dispatch.",
            severity="minor",
        )
    restored = browser.get(address)
    refused = browser.post(address, prior.PostedControls(restored).values)
    assert refused.status_code == 200
    assert prior.PostedControls(refused).values[field] == prior.DRAFT
    assert refused.context["form"].non_field_errors()
    parent_page = browser.get(parent)
    assert parent_page.status_code == 200
    direct = browser.get(address)
    if case == "removed-link":
        assert direct.status_code == 404
    else:
        assert direct.status_code == 302 and direct.url == prior.url("detail", existing.pk)

    help_path = Path(__file__).resolve().parents[2] / "docs/pages/nonconformance" / f"{key}.json"
    help_data = json.loads(help_path.read_text())
    guidance = next(
        item["description"]
        for section in help_data["sections"]
        for item in section.get("items", [])
        if item["label"] == "Signed out by a time limit"
    )
    instruction = re.search(
        r"after a refused save,? open the form again from the "
        r"(?:record|ticket)\b(?! if| when| while)",
        guidance,
        flags=re.I,
    )
    if instruction:
        assert f'href="{address}"' in parent_page.text, (
            f'Help says "{instruction.group()}". The parent has no link to {address}.'
        )
