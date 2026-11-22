"""Synthetic New ticket submissions, redraws and receipts for the third confirmation, written by
the independent reviewer and adopted; cases changed on adoption say so ("Adopted:")."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import transaction
from django.test import RequestFactory
from django.urls import reverse

from accounts import kept, sessions
from accounts.models import KeptDraft
from desk.models import ServiceRequest
from request_forms import intake
from request_forms.models import RequestTemplate
from tests import test_review_kept_typing_new_ticket as r
from tests import test_review_kept_typing_new_ticket_confirm as c
from tests import test_review_kept_typing_new_ticket_confirm2 as prior

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


def change_during_refresh(monkeypatch, change):
    """Commit setup after the refresh's list, immediately before its real definition read."""
    refresh = intake.Intake.refresh
    read = intake.definition.read
    in_refresh = False
    changes = []

    def refreshing(self):
        nonlocal in_refresh
        in_refresh = True
        try:
            return refresh(self)
        finally:
            in_refresh = False

    def reading(*args, **kwargs):
        if in_refresh and not changes:
            prior.worker(change)
            changes.append(True)
        return read(*args, **kwargs)

    monkeypatch.setattr(intake.Intake, "refresh", refreshing)
    monkeypatch.setattr(intake.definition, "read", reading)
    return changes


@pytest.mark.parametrize("edit", ["rename", "delete", "retire_and_replace"])
def test_stale_submission_refresh_keeps_its_list_and_definition_together(
    agent, pickup, monkeypatch, edit
):
    spare = c.alternate(pickup, active=edit != "retire_and_replace")
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup)
    browser.raise_request_exception = False
    selected = RequestTemplate.objects.filter(pk=pickup.pk)
    selected.update(instructions="Review the revised collection instructions.")  # force refresh

    def change():
        with transaction.atomic():
            if edit == "rename":
                selected.update(name="Revised collection", instructions="Use revised crate labels.")
            elif edit == "delete":
                pickup.fields.all().delete()
                selected.delete()
            else:
                selected.update(is_active=False)
                RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    changed = change_during_refresh(monkeypatch, change)
    response = browser.post(r.PAGE, values)
    assert changed and response.status_code == 200
    choices = c.options(response)
    picker, fields = response.context["intake_drawn"]
    if edit == "rename":
        assert fields is not None
        assert choices[str(pickup.pk)] == fields.definition.template.name
        assert fields.definition.template.instructions in response.text
    else:
        assert fields is None and r.selected(response, "request_form") == ""
        assert intake.NO_LONGER in picker.errors["request_form"]
        assert choices == {str(spare.pk): spare.name}


@pytest.mark.parametrize("edit", ["retirement", "organization"])
def test_section_retries_a_transient_loss_of_eligibility(
    agent, pickup, clinic, other_org, monkeypatch, edit
):
    browser = r.pages.browser_for(agent)
    pickup.clients.set([clinic])
    original = intake.definition.read
    reads = []

    def set_available(available):
        if edit == "retirement":
            RequestTemplate.objects.filter(pk=pickup.pk).update(is_active=available)
        else:
            pickup.clients.set([clinic if available else other_org])

    def read(*args, **kwargs):
        if reads:
            return original(*args, **kwargs)
        prior.worker(lambda: set_available(False))
        defn = original(*args, **kwargs)
        reads.append(defn)
        prior.worker(lambda: set_available(True))
        return defn

    monkeypatch.setattr(intake.definition, "read", read)
    page = r.section(browser, pickup, client=clinic.pk)
    assert reads and page.status_code == 200 and str(pickup.pk) in c.options(page)
    assert (r.selected(page, "request_form"), 'name="form_definition"' in page.text) == (
        str(pickup.pk),
        True,
    )


def test_temporary_picker_miss_does_not_leave_a_selected_form_without_fields(
    agent, pickup, monkeypatch
):
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup)
    before, called = intake.RequestFormPicker.is_valid, []
    row = RequestTemplate.objects.filter(pk=pickup.pk)

    def valid(picker):
        if not picker.is_bound or called:
            return before(picker)
        called.append(True)
        prior.worker(lambda: row.update(is_active=False))
        result = before(picker)
        prior.worker(lambda: row.update(is_active=True))
        return result

    monkeypatch.setattr(intake.RequestFormPicker, "is_valid", valid)
    page = browser.post(r.PAGE, values)
    assert called and page.status_code == 200 and str(pickup.pk) in c.options(page)
    selection = r.selected(page, "request_form")
    fields = page.context["intake_drawn"][1]
    assert bool(selection) == (fields is not None), (selection, fields)


def test_category_and_alternatives_on_refusal_existed_together(agent, pickup, monkeypatch):
    browser = r.pages.browser_for(agent)
    spare = c.alternate(pickup, active=False)
    category = type(pickup.category).objects.filter(pk=pickup.category_id)
    old, new = "Give the cartons to reception.", "Give the cartons to dispatch."
    category.update(guidance=old)
    values = prior.submission(browser, pickup)
    before = intake.bound_intake

    def edit():
        with transaction.atomic():
            category.update(guidance=new)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    def read(category, data, user):
        prior.worker(edit)  # the view has read category, but Intake has not read the picker
        return before(category, data, user)

    monkeypatch.setattr(intake, "bound_intake", read)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200 and (old in page.text) != (new in page.text)
    assert (str(spare.pk) in c.options(page)) == (new in page.text)


def test_earlier_coherent_refusal_is_checked_again_on_the_next_submit(
    agent, pickup, clinic, other_org, monkeypatch
):
    pickup.clients.set([clinic])
    spare = c.alternate(pickup, active=False)
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup, client=str(clinic.pk))

    def edit():
        with transaction.atomic():
            pickup.clients.set([other_org])
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    with monkeypatch.context() as patch:
        prior.after_validation(patch, edit)
        page = browser.post(r.PAGE, values)
    assert page.status_code == 200
    assert r.selected(page, "request_form") == str(pickup.pk) and r.ROUTE in page.text
    assert str(spare.pk) not in c.options(page)
    values.update(subject=r.SUBJECT, form_definition=r.hidden(page, "form_definition"))
    again = browser.post(r.PAGE, values)
    assert (
        again.status_code == 200 and not ServiceRequest.objects.filter(subject=r.SUBJECT).exists()
    )
    picker, fields = again.context["intake_drawn"]
    assert fields is None and intake.NOT_OFFERED in picker.errors["request_form"]
    assert str(spare.pk) in c.options(again) and r.ROUTE not in again.text


def test_concurrent_refusals_acknowledge_only_the_inserted_subject(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    cookies = {name: cookie.value for name, cookie in browser.cookies.items()}
    r.pages.end_open(agent)
    ready = Barrier(2)

    def refuse(subject):
        request = RequestFactory().post(
            r.PAGE,
            {**r.draft_values(pickup, subject=subject), kept.FIELD: token},
            headers={"HX-Request": "true", "X-VR-Form": "1"},
        )
        request.COOKIES.update(cookies)
        request._messages = CookieStorage(request)
        ready.wait(timeout=8)
        response = sessions._refusal(request)
        assert response.status_code == 401
        return subject, json.loads(response.content)["kept"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = [
            pool.submit(r.in_worker, lambda subject=subject: refuse(subject))
            for subject in ("Example morning pickup", "Example afternoon pickup")
        ]
        acknowledged = [
            subject for subject, held in (job.result(timeout=12) for job in replies) if held
        ]
    assert acknowledged == [KeptDraft.objects.get().values["subject"]]


def test_delayed_repeat_after_display_does_not_repeat_the_same_signout_notice(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    values = r.draft_values(pickup)
    assert r.pages.send(browser, token, values) == 1
    login = reverse("account_login")
    assert browser.get(login).text.count(kept.KEPT_MESSAGE) == 1
    repeat = browser.post(
        r.PAGE,
        {**values, kept.FIELD: token},
        headers={"X-VR-Form": "1", "HX-Current-URL": "http://testserver/requests/"},
    )
    assert repeat.status_code == 401 and KeptDraft.objects.count() == 1
    assert kept.KEPT_MESSAGE not in browser.get(login).text


# Adopted as a strict xfail: a documented limitation (docs/architecture.md). Two setup changes
# that change and then restore the ticket forms between the section's two list reads cannot be
# told apart from no change; only the display can mix them, and the next submit is checked against
# the current definition.
@pytest.mark.xfail(strict=True, reason="a change and its reversal between two reads (documented)")
def test_section_never_pairs_an_alternative_with_instructions_from_its_absence(
    agent, pickup, monkeypatch
):
    spare = c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    before, during = "Use the reception desk.", "Use the dispatch desk."
    chosen = RequestTemplate.objects.filter(pk=pickup.pk)
    alternative = RequestTemplate.objects.filter(pk=spare.pk)
    chosen.update(instructions=before)
    original, sampled = intake.definition.read, []

    def change(instructions, active):
        with transaction.atomic():
            chosen.update(instructions=instructions)
            alternative.update(is_active=active)

    def read(*args, **kwargs):
        if sampled:
            return original(*args, **kwargs)
        prior.worker(lambda: change(during, False))
        defn = original(*args, **kwargs)
        sampled.append(defn.template.instructions)
        prior.worker(lambda: change(before, True))
        return defn

    monkeypatch.setattr(intake.definition, "read", read)
    page = r.section(browser, pickup)
    assert page.status_code == 200 and sampled == [during]
    assert (before in page.text) != (during in page.text)
    assert (str(spare.pk) in c.options(page)) == (before in page.text)


def test_refusal_removes_the_explicitly_ineligible_choice_from_the_named_clients_list(
    agent, pickup, clinic, other_org
):
    pickup.clients.set([other_org])
    spare = c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    values = prior.submission(browser, pickup, client=str(clinic.pk))
    page = browser.post(r.PAGE, values)
    picker, fields = page.context["intake_drawn"]
    assert page.status_code == 200 and fields is None and r.selected(page, "request_form") == ""
    assert intake.NOT_OFFERED in picker.errors["request_form"]
    assert c.options(page) == {str(spare.pk): spare.name}


def test_insert_receipt_and_existing_copy_receipt_keep_their_distinct_meanings(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    headers = {"X-VR-Form": "1", "HX-Current-URL": "http://testserver/requests/"}
    receipts = []
    for subject in ("First collection", "First collection", "Later collection"):
        values = r.draft_values(pickup, subject=subject)
        page = browser.post(r.PAGE, {**values, kept.FIELD: token}, headers=headers)
        assert page.status_code == 401 and page.json()["sign_in"] == page["HX-Redirect"]
        receipts.append(page.json()["kept"])
    delivery = {"forms": [{"token": token, "values": values}]}
    answer = browser.post(
        reverse("account-session-keep"), delivery, content_type="application/json"
    )
    # Adopted: one meaning for both receipts, as the round's two findings ask (another tab's text
    # must not release a guard; an identical delivery may): what this delivery sent is the copy
    # held. "Later collection" is not, so the keep address says a copy is kept (the count) but
    # not this form's (`held`), and only the identical retransmission is a receipt.
    assert answer.status_code == 200 and answer.json() == {"kept": 1, "held": [False]}
    assert KeptDraft.objects.get().values["subject"] == "First collection"
    assert receipts == [True, True, False]
