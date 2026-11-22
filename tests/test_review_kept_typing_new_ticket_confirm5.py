"""Fifth confirmation using New ticket, signed keep markers and synthetic PostgreSQL rows."""

import pytest
from django.db import OperationalError, connection, transaction
from django.db.models import F

from accounts import kept
from accounts.models import KeptDraft
from core import preferences
from core.models import UserPreference
from desk import views
from desk.models import ServiceRequest
from request_forms import intake
from request_forms.models import FieldValue, RequestTemplate
from tests import test_review_kept_typing_new_ticket as r
from tests import test_review_kept_typing_new_ticket_confirm as c
from tests import test_review_kept_typing_new_ticket_confirm2 as prior
from tests import test_review_kept_typing_new_ticket_confirm4 as fourth

pytestmark = pytest.mark.django_db(transaction=True)
agent = r.agent
make_user = r.make_user
group = r.group
logistics = r.logistics
pickup_category = r.pickup_category
catalog = r.catalog
pickup = r.pickup
clinic = r.clinic
other_org = r.other_org


@pytest.mark.parametrize("stale", [False, True], ids=["ordinary", "refresh"])
def test_unavailable_organization_keeps_rendered_picker_and_fields_in_agreement(
    agent, pickup, clinic, other_org, stale
):
    pickup.clients.set([other_org])
    spare = c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    # The form was chosen before the organization. The selected organization becomes
    # inactive after the operator chooses it and before the request is submitted.
    values = prior.submission(browser, pickup, client=str(clinic.pk))
    values["subject"] = r.SUBJECT
    type(clinic).objects.filter(pk=clinic.pk).update(is_active=False)
    if stale:
        RequestTemplate.objects.filter(pk=pickup.pk).update(revision=F("revision") + 1)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and page.context["form"].has_error("client")
    assert not ServiceRequest.objects.exists() and str(spare.pk) in c.options(page)
    picker, fields = page.context["intake_drawn"]
    selected = r.selected(page, "request_form")
    if fields is None:
        assert not selected and picker.has_error("request_form")
    else:
        # Correct the organization. Submit the choice actually selected in the returned
        # HTML with the courier answer under the field the operator can see.
        values.update(
            client=str(other_org.pk),
            request_form=selected,
            form_definition=r.hidden(page, "form_definition"),
        )
        assert browser.post(r.PAGE, values).status_code == 302
        ticket = ServiceRequest.objects.get(subject=r.SUBJECT)
        courier = FieldValue.objects.filter(filled_form__request=ticket, key="courier")
        assert (selected, list(courier.values_list("value", flat=True))) == (
            str(fields.definition.template.pk),
            [r.ROUTE],
        )


# Adopted as a strict xfail: predates this change (the reviewer's observation, failing on
# every revision): a category whose forms are none offered to the organization draws no
# picker, yet a submission needs a picker answer. Recorded for a later change.
@pytest.mark.xfail(strict=True, reason="predates: a picker answer required where none was drawn")
def test_category_with_no_offered_form_accepts_the_ticket_its_page_drew(
    agent, pickup, clinic, other_org
):
    pickup.clients.set([other_org])
    browser = r.pages.browser_for(agent)
    page = r.section(browser, pickup, client=clinic.pk)
    assert page.status_code == 200 and 'name="request_form"' not in page.text
    values = {
        "client": str(clinic.pk),
        "category": str(pickup.category_id),
        "category_terms": r.hidden(page, "category_terms"),
        "subject": r.SUBJECT,
        "body": "Pickup at the front entrance.",
        "channel": "email",
        "first_entry_kind": "note",
    }
    saved = browser.post(r.PAGE, values)
    if saved.status_code == 200:
        assert not saved.context["form"].errors
        picker, _ = saved.context["intake_drawn"]
        assert not picker.errors, picker.errors.as_text()
    assert saved.status_code == 302 and ServiceRequest.objects.get().subject == r.SUBJECT


def test_last_picker_miss_during_changing_list_clears_its_posted_choice(agent, pickup, monkeypatch):
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup)
    spare = c.alternate(pickup)
    selected = RequestTemplate.objects.filter(pk=pickup.pk)
    selected.update(is_active=False)
    validate = intake.RequestFormPicker.is_valid
    reads = []

    def changing(picker):
        valid = validate(picker)
        if picker.is_bound and picker.data.get("request_form") and len(reads) < intake.READINGS:
            reads.append(valid)

            def change():
                with transaction.atomic():
                    RequestTemplate.objects.filter(pk=spare.pk).update(name=f"Archive {len(reads)}")
                    if len(reads) == intake.READINGS:
                        selected.update(is_active=True)

            prior.worker(change)
        return valid

    monkeypatch.setattr(intake.RequestFormPicker, "is_valid", changing)
    page = browser.post(r.PAGE, values)
    assert reads == [False] * intake.READINGS and page.status_code == 200
    picker, fields = page.context["intake_drawn"]
    assert str(pickup.pk) in c.options(page) and fields is None and picker.has_error("request_form")
    assert r.selected(page, "request_form") == ""


# Adopted as a strict xfail: documented in docs/architecture.md. A page drawn in its own
# reading makes its small writes (the first What's new visit) after the reading ends, so a
# failure there comes after the copy was taken.
@pytest.mark.xfail(strict=True, reason="a failed write after the reading leaves the copy taken")
@pytest.mark.parametrize("per_request", [False, True], ids=["default", "atomic_requests"])
def test_restored_ticket_survives_a_failed_first_visit_write(
    agent, pickup, monkeypatch, per_request
):
    browser = r.pending(agent, pickup)
    UserPreference.objects.filter(user=agent, key=preferences.WHATS_NEW_KEY).delete()
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", per_request)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(KeptDraft.objects.get().taken_at is not None)
        raise OperationalError("Synthetic failure recording the first visit")

    browser.raise_request_exception = False
    with monkeypatch.context() as patch:
        patch.setattr(preferences, "save_if_absent", unavailable)
        failed = browser.get(r.PAGE)
    assert failed.status_code == 500 and calls and calls[0]
    pending = KeptDraft.objects.get().taken_at is None
    again = browser.get(r.PAGE)
    assert again.status_code == 200
    assert (pending, r.SUBJECT in again.text, r.ROUTE in again.text) == (True, True, True)


def test_late_failure_after_creating_a_ticket_rolls_back_the_atomic_post(
    agent, pickup, monkeypatch
):
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup, details_not_known="on")
    values["subject"] = r.SUBJECT
    values["field-count"] = "3"
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    committed = []

    def interrupt(*args, **kwargs):
        committed.append(ServiceRequest.objects.filter(subject=r.SUBJECT).exists())
        raise RuntimeError("Synthetic failure constructing the success response")

    with monkeypatch.context() as patch:
        patch.setattr(views, "redirect", interrupt)
        with pytest.raises(RuntimeError, match="success response"):
            browser.post(r.PAGE, values)
    assert committed == [True] and not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    assert browser.post(r.PAGE, values).status_code == 302
    assert ServiceRequest.objects.filter(subject=r.SUBJECT).count() == 1


@pytest.mark.parametrize(
    "body, second, expected",
    [
        ("Pickup\n\nAt reception.", "Pickup\r\n\r\nAt reception.", True),
        ("Pickup\nAt reception.", "Pickup\n At reception.", False),
        ("", "\n\n", False),
    ],
    ids=["equivalent-lines", "different-indent", "blank-lines"],
)
def test_receipts_preserve_the_actual_textarea_contents(agent, pickup, body, second, expected):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    first = r.draft_values(pickup, body=body)
    next_values = {**first, "body": second}
    assert fourth.batch(browser, fourth.item(token, first))["held"] == [True]
    post = fourth.refused(browser, token, next_values)
    followup = fourth.batch(browser, fourth.item(token, next_values))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert page.status_code == 200 and page.context["form"]["body"].value() == body
    assert (post, followup["held"]) == (expected, [expected])


def test_receipt_for_already_held_multiline_text_at_the_per_field_limit(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    body = "a" * (kept.MAX_VALUE - 2) + "\nb"
    assert len(body) == kept.MAX_VALUE
    values = r.draft_values(pickup, body=body)
    assert fourth.batch(browser, fourth.item(token, values)) == {"kept": 1, "held": [True]}
    post = fourth.refused(browser, token, {**values, "body": body.replace("\n", "\r\n")})
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert page.status_code == 200 and page.context["form"]["body"].value() == body
    assert post, "The same visible textarea was already stored within the character limit"


@pytest.mark.parametrize("phase", ["bound", "refresh"])
def test_categorys_displayed_team_and_available_options_existed_together(
    agent, pickup, logistics, monkeypatch, phase
):
    browser = r.pages.browser_for(agent)
    spare = c.alternate(pickup, active=False)
    values = prior.submission(browser, pickup)
    if phase == "refresh":
        RequestTemplate.objects.filter(pk=pickup.pk).update(revision=F("revision") + 1)
    old, new = logistics.name, "Evening dispatch"
    original = intake.Intake.__init__ if phase == "bound" else intake.Intake.refresh
    commits = []

    def edit():
        with transaction.atomic():
            type(logistics).objects.filter(pk=logistics.pk).update(name=new)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    def around_read(execute, sql, params, many, context):
        if not commits and 'AS "offered_to_some"' in sql:
            prior.worker(edit)
            commits.append(True)
        return execute(sql, params, many, context)

    def during(*args, **kwargs):
        with connection.execute_wrapper(around_read):
            return original(*args, **kwargs)

    monkeypatch.setattr(intake.Intake, "__init__" if phase == "bound" else "refresh", during)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and commits
    assert (f"Goes to {old} at" in page.text) != (f"Goes to {new} at" in page.text)
    assert (str(spare.pk) in c.options(page)) == (f"Goes to {new} at" in page.text)


@pytest.mark.parametrize(
    "first_has_picker", [False, True], ids=["earlier-page-first", "no-form-first"]
)
def test_an_absent_picker_and_an_explicit_no_form_have_distinct_receipts(
    agent, pickup, first_has_picker
):
    browser = r.pages.browser_for(agent)
    template = RequestTemplate.objects.filter(pk=pickup.pk)
    template.update(is_active=False)
    before = r.section(browser, pickup)
    older = r.drawn_marker(before)[0]
    assert 'name="request_form"' not in before.text
    template.update(is_active=True)
    after = r.section(browser, pickup, request_form="")
    newer = r.drawn_marker(after)[0]
    assert r.selected(after, "request_form") == ""
    common = {
        "subject": r.SUBJECT,
        "category": str(pickup.category_id),
        "body": "Collect the cartons.",
        "channel": "email",
        "first_entry_kind": "note",
    }
    no_picker = (older, common)
    explicit_none = (newer, {**common, "request_form": ""})
    first, second = (explicit_none, no_picker) if first_has_picker else (no_picker, explicit_none)
    r.pages.end_open(agent)
    assert fourth.batch(browser, fourth.item(*first))["held"] == [True]
    post = fourth.refused(browser, *second)
    reply = fourth.batch(browser, fourth.item(*second))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert r.selected(page, "request_form") == ("" if first_has_picker else str(pickup.pk))
    assert ('name="field-courier"' in page.text) == (not first_has_picker)
    assert (post, reply) == (False, {"kept": 1, "held": [False]})


def test_refusal_explains_a_removed_form_when_the_invalid_organization_lists_nothing(
    agent, pickup, clinic, other_org
):
    browser = r.pages.browser_for(agent)
    pickup.clients.set([other_org])
    values = prior.submission(browser, pickup, client=str(clinic.pk))
    values["subject"] = r.SUBJECT
    type(clinic).objects.filter(pk=clinic.pk).update(is_active=False)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and page.context["form"].has_error("client")
    picker, fields = page.context["intake_drawn"]
    still_shown = fields is not None and r.selected(page, "request_form") == str(pickup.pk)
    if still_shown:
        assert r.ROUTE in page.text
    else:
        assert picker is not None and picker.has_error("request_form"), (
            "The courier answer disappeared without a ticket-form explanation"
        )
