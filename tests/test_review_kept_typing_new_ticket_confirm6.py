"""Sixth confirmation of the changed receipt and intake rules on synthetic PostgreSQL data."""

import re

import pytest
from django.db import connection, transaction

from accounts import kept
from accounts.models import KeptDraft
from desk.models import ServiceRequest
from request_forms import intake
from request_forms.models import FieldKind, FieldValue, RequestTemplate
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
clinic_site = r.clinic_site
other_org = r.other_org


@pytest.mark.parametrize("correction", ["eligible", "from-site", "ineligible"])
@pytest.mark.parametrize("with_alternative", [False, True], ids=["one-form", "two-forms"])
def test_correction_uses_the_returned_choice_or_explains_its_withdrawal(
    agent, pickup, clinic, clinic_site, other_org, correction, with_alternative
):
    retired = type(clinic).objects.create(name="Example former clinic", account_number="R6OLD")
    pickup.clients.set([clinic])
    if with_alternative:
        c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup, client=str(retired.pk))
    values["subject"] = r.SUBJECT
    type(clinic).objects.filter(pk=retired.pk).update(is_active=False)
    first = browser.post(r.PAGE, values)
    assert first.status_code == 200 and first.context["form"].has_error("client")
    assert not ServiceRequest.objects.exists()
    picker, fields = first.context["intake_drawn"]
    assert picker is not None and fields is not None
    assert not picker.has_error("request_form")
    selected = r.selected(first, "request_form")
    assert selected == str(pickup.pk) and r.ROUTE in first.text
    values.update(request_form=selected, form_definition=r.hidden(first, "form_definition"))
    if correction == "from-site":
        values.update(client="", site=str(clinic_site.pk))
    else:
        values["client"] = str((clinic if correction == "eligible" else other_org).pk)
    corrected = browser.post(r.PAGE, values)
    if correction == "ineligible":
        assert corrected.status_code == 200 and not ServiceRequest.objects.exists()
        picker, fields = corrected.context["intake_drawn"]
        assert fields is None and not r.selected(corrected, "request_form")
        assert intake.NOT_OFFERED in picker.errors["request_form"]
    else:
        assert corrected.status_code == 302
        ticket = ServiceRequest.objects.get(subject=r.SUBJECT, client=clinic)
        assert FieldValue.objects.get(filled_form__request=ticket, key="courier").value == r.ROUTE


@pytest.mark.parametrize("newer_first", [False, True])
def test_an_unanswered_optional_choice_without_a_default_keeps_the_same_receipts(
    agent, pickup, newer_first
):
    browser = r.pages.browser_for(agent)
    older = r.drawn_marker(r.section(browser, pickup))[0]
    pickup.fields.create(
        key="dock",
        label="Optional dock",
        kind=FieldKind.CHOICE,
        choices=["North", "South"],
    )
    later = r.section(browser, pickup)
    newer = r.drawn_marker(later)[0]
    radios = re.findall(r'<input\b[^>]*name="field-dock"[^>]*>', later.text)
    assert len(radios) >= 2 and not any(" checked" in radio for radio in radios)
    values = r.draft_values(pickup)
    old, new = (older, values), (newer, {**values, "field-dock": ""})
    first, second = (new, old) if newer_first else (old, new)
    r.pages.end_open(agent)
    assert fourth.batch(browser, fourth.item(*first))["held"] == [True]
    refusal = fourth.refused(browser, *second)
    keep = fourth.batch(browser, fourth.item(*second))
    browser.force_login(agent)
    page = browser.get(r.PAGE)
    assert not page.context["intake_drawn"][1]["field-dock"].value() and not r.missed(page)
    assert r.SUBJECT in page.text and r.ROUTE in page.text
    assert (refusal, keep) == (True, {"kept": 1, "held": [True]})


@pytest.mark.parametrize("over", [False, True], ids=["at-total-limit", "one-over"])
def test_form_first_counts_the_same_normalized_characters_as_json(agent, pickup, over):
    browser = r.pages.browser_for(agent)
    pickup.fields.create(key="notes", label="Routing notes", kind=FieldKind.LONG_TEXT)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    field = "d" * (kept.MAX_VALUE - 2) + "\nx"
    values = {"body": field, "category": str(pickup.category_id), "request_form": str(pickup.pk)}
    left = kept.MAX_TOTAL - sum(map(len, values.values()))
    notes = "r" * (left - 2) + "\ny"
    values.update({"field-notes": notes, "subject": "x" if over else ""})
    assert sum(map(len, values.values())) == kept.MAX_TOTAL + int(over)
    native = {name: value.replace("\n", "\r\n") for name, value in values.items()}
    post = fourth.refused(browser, token, native)
    result = fourth.batch(browser, fourth.item(token, values))
    if over:
        assert (post, result, KeptDraft.objects.count()) == (False, {"kept": 0, "held": [False]}, 0)
    else:
        saved = KeptDraft.objects.get().values
        assert (post, result) == (True, {"kept": 1, "held": [True]})
        assert saved["body"] == field and saved["field-notes"] == notes


# Adopted as a strict xfail: documented in docs/architecture.md. After three readings that each
# saw a setup change, the page draws no form and shows the last reading's list with the category
# as read at its start.
@pytest.mark.xfail(strict=True, reason="readings that never settle may pair two moments")
def test_exhausted_team_and_list_readings_do_not_render_a_pair_that_never_existed(
    agent, pickup, logistics, monkeypatch
):
    browser = r.pages.browser_for(agent)
    spare = c.alternate(pickup)
    values = prior.submission(browser, pickup)
    read = intake.RequestFormPicker.is_valid
    revisions = []

    def change_during_choice(picker):
        valid = read(picker)
        if picker.is_bound and picker.data.get("request_form") and len(revisions) < intake.READINGS:
            revision = len(revisions) + 1

            def change():
                with transaction.atomic():
                    type(logistics).objects.filter(pk=logistics.pk).update(
                        name=f"Dispatch {revision}"
                    )
                    RequestTemplate.objects.filter(pk=spare.pk).update(
                        name=f"Collection {revision}"
                    )

            prior.worker(change)
            revisions.append(revision)
        return valid

    monkeypatch.setattr(intake.RequestFormPicker, "is_valid", change_during_choice)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and len(revisions) == intake.READINGS
    picker, fields = page.context["intake_drawn"]
    assert fields is None and not r.selected(page, "request_form")
    assert intake.FORM_CHANGED in picker.errors["request_form"]
    shown = c.options(page).get(str(spare.pk))
    if shown:
        coherent = [(spare.name, logistics.name)] + [
            (f"Collection {n}", f"Dispatch {n}") for n in revisions
        ]
        guidance = next(team for _, team in coherent if f"Goes to {team} at" in page.text)
        assert (shown, guidance) in coherent


def test_an_extra_restricted_choice_on_refusal_is_marked_and_rechecked_on_save(
    agent, pickup, clinic, other_org
):
    spare = c.alternate(pickup)
    spare.clients.set([other_org])
    browser = r.pages.browser_for(agent)
    offered = r.section(browser, pickup, client=clinic.pk)
    assert set(c.options(offered)) == {str(pickup.pk)}
    values = prior.submission(browser, pickup, client=str(clinic.pk))
    refused = browser.post(r.PAGE, values)
    assert refused.status_code == 200 and refused.context["form"].has_error("subject")
    assert c.options(refused).get(str(spare.pk)) == f"{spare.name} (selected organizations only)"
    # The operator draws that form with no organization selected and then selects the clinic.
    alternate = r.section(browser, spare)
    values.update(
        subject=r.SUBJECT,
        request_form=str(spare.pk),
        form_definition=r.hidden(alternate, "form_definition"),
    )
    corrected = browser.post(r.PAGE, values)
    assert corrected.status_code == 200 and not ServiceRequest.objects.exists()
    picker, fields = corrected.context["intake_drawn"]
    assert fields is None and r.selected(corrected, "request_form") == ""
    assert intake.NOT_OFFERED in picker.errors["request_form"]


@pytest.mark.parametrize("initial_team", [False, True], ids=["assigned", "cleared"])
def test_reading_compares_the_presence_of_the_categorys_default_team(
    agent, pickup, logistics, monkeypatch, initial_team
):
    category = type(pickup.category).objects.filter(pk=pickup.category_id)
    category.update(default_team=logistics if initial_team else None)
    browser = r.pages.browser_for(agent)
    spare = c.alternate(pickup, active=False)
    values = prior.submission(browser, pickup)

    original = intake.Intake.__init__
    committed = []

    def at_list(execute, sql, params, many, context):
        if not committed and 'AS "offered_to_some"' in sql:

            def edit():
                with transaction.atomic():
                    category.update(default_team=None if initial_team else logistics)
                    RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

            prior.worker(edit)
            committed.append(True)
        return execute(sql, params, many, context)

    def read(*args, **kwargs):
        with connection.execute_wrapper(at_list):
            return original(*args, **kwargs)

    monkeypatch.setattr(intake.Intake, "__init__", read)
    page = browser.post(r.PAGE, values)
    assert committed and page.status_code == 200
    showing_team = f"Goes to {logistics.name} at" in page.text
    assert showing_team != ("Goes to the default team at" in page.text)
    newer_list = str(spare.pk) in c.options(page)
    assert showing_team == (not initial_team if newer_list else initial_team)
