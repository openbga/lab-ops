"""Second confirmation for New ticket, using synthetic records and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib import messages
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import transaction
from django.db.models.functions import Now
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from accounts import kept, sessions
from accounts.models import AccountSession, KeptDraft
from request_forms import intake
from request_forms.models import RequestTemplate
from request_forms.services import FORM_CHANGED
from tests import test_review_kept_typing_new_ticket as r
from tests import test_review_kept_typing_new_ticket_confirm as c

pytestmark = pytest.mark.django_db
agent = r.agent
make_user = r.make_user
group = r.group
logistics = r.logistics
pickup_category = r.pickup_category
catalog = r.catalog
pickup = r.pickup
clinic = r.clinic
other_org = r.other_org


def worker(change):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(r.in_worker, change).result(timeout=8)


def after_validation(monkeypatch, change):
    validate = intake.Intake.is_valid

    def edited(self):
        valid = validate(self)
        worker(change)
        return valid

    monkeypatch.setattr(intake.Intake, "is_valid", edited)


def submission(browser, pickup, **changes):
    drawn = r.section(browser, pickup)
    return r.draft_values(
        pickup,
        subject="",
        category_terms=r.hidden(drawn, "category_terms"),
        form_definition=r.hidden(drawn, "form_definition"),
        **changes,
    )


@pytest.mark.django_db(transaction=True)
def test_deleting_selected_template_before_bound_recheck_returns_validation(
    agent, pickup, monkeypatch
):
    spare = c.alternate(pickup)
    browser = r.pages.browser_for(agent)
    values = submission(browser, pickup)
    browser.raise_request_exception = False

    def remove():
        with transaction.atomic():
            pickup.fields.all().delete()
            RequestTemplate.objects.filter(pk=pickup.pk).delete()

    after_validation(monkeypatch, remove)
    response = browser.post(r.PAGE, values)
    assert response.status_code == 200
    assert response.context["form"].has_error("subject")
    assert c.options(response) == {str(spare.pk): spare.name}
    assert response.context["intake_drawn"][0].has_error("request_form")
    assert 'name="form_definition"' not in response.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("surface", ["section", "refused"])
def test_changing_template_on_every_read_clears_selection_and_definition_together(
    agent, pickup, monkeypatch, surface
):
    browser = r.pages.browser_for(agent)
    values = submission(browser, pickup)
    target = RequestTemplate.objects.filter(pk=pickup.pk)
    reads = []
    original = intake.definition.read

    def read(*args, **kwargs):
        defn = original(*args, **kwargs)
        reads.append(defn.template.name)
        worker(lambda: target.update(name=f"Pickup revision {len(reads)}"))
        return defn

    monkeypatch.setattr(intake.definition, "read", read)
    page = r.section(browser, pickup) if surface == "section" else browser.post(r.PAGE, values)
    assert page.status_code == 200
    assert len(reads) <= 4  # three reads or refreshes, plus the bound form's initial read
    chosen = r.selected(page, "request_form")
    token = 'name="form_definition"' in page.text
    assert (bool(chosen), token, 'name="field-courier"' in page.text) == (False, False, False)
    assert str(pickup.pk) in c.options(page)


@pytest.mark.django_db(transaction=True)
def test_single_transient_definition_disagreement_retries_the_explicit_choice(
    agent, pickup, monkeypatch
):
    browser = r.pages.browser_for(agent)
    target = RequestTemplate.objects.filter(pk=pickup.pk)
    original = intake.definition.read
    reads = []

    def read(*args, **kwargs):
        reads.append(True)
        if len(reads) == 1:
            worker(lambda: target.update(name="Temporary booking"))
            defn = original(*args, **kwargs)
            worker(lambda: target.update(name=pickup.name))
            return defn
        return original(*args, **kwargs)

    monkeypatch.setattr(intake.definition, "read", read)
    response = r.section(browser, pickup)
    assert response.status_code == 200 and c.options(response) == {str(pickup.pk): pickup.name}
    assert r.selected(response, "request_form") == str(pickup.pk), reads
    assert 'name="form_definition"' in response.text


@pytest.mark.django_db(transaction=True)
def test_bound_refresh_explains_answers_cleared_by_a_late_rename(agent, pickup, monkeypatch):
    browser = r.pages.browser_for(agent)
    values = submission(browser, pickup)
    after_validation(
        monkeypatch,
        lambda: RequestTemplate.objects.filter(pk=pickup.pk).update(name="New dispatch label"),
    )
    response = browser.post(r.PAGE, values)
    assert response.status_code == 200 and response.context["form"].has_error("subject")
    assert r.ROUTE in response.text or FORM_CHANGED in response.text, (
        "After the redraw the posted courier answer and the form-change notice were both absent"
    )


@pytest.mark.django_db(transaction=True)
def test_bound_refresh_uses_the_category_it_signed_on_the_redrawn_page(agent, pickup, monkeypatch):
    browser = r.pages.browser_for(agent)
    category = pickup.category
    before = "Collect the demonstration cartons at reception."
    after = "Collect the demonstration cartons at dispatch."
    type(category).objects.filter(pk=category.pk).update(guidance=before)
    values = submission(browser, pickup)

    def edit():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=pickup.pk).update(name="Dispatch collection")
            type(category).objects.filter(pk=category.pk).update(guidance=after, is_restricted=True)

    after_validation(monkeypatch, edit)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200
    _, fields = page.context["intake_drawn"]
    assert fields is not None
    signed = fields.definition.category
    assert (page.context["category"].is_restricted, page.context["category"].guidance) == (
        signed.is_restricted,
        signed.guidance,
    )
    assert signed.guidance in page.text


@pytest.mark.django_db(transaction=True)
def test_refused_page_lists_alternatives_and_instructions_from_a_common_reading(
    agent, pickup, monkeypatch
):
    before, after = "Pack at reception.", "Pack at dispatch."
    RequestTemplate.objects.filter(pk=pickup.pk).update(instructions=before)
    spare = c.alternate(pickup, active=False)
    browser = r.pages.browser_for(agent)
    values = submission(browser, pickup)

    def edit():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=pickup.pk).update(instructions=after)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    after_validation(monkeypatch, edit)
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200
    assert (str(spare.pk) in c.options(page)) == (after in page.text)
    assert (before in page.text) != (after in page.text)


@pytest.mark.parametrize("variant", ["held", "empty", "unmarked"])
def test_refusal_reports_actual_keep_and_leaves_other_response_fields_intact(
    agent, pickup, variant
):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    values = {} if variant == "empty" else r.draft_values(pickup)
    if variant != "unmarked":
        values[kept.FIELD] = token
    response = browser.post(
        r.PAGE,
        values,
        headers={
            "HX-Request": "true",
            "X-VR-Form": "1",
            "HX-Current-URL": "http://testserver/requests/",
        },
    )
    assert response.status_code == 401
    reply = response.json()
    assert reply["kept"] is (variant == "held") and reply["signed_in"] is False
    assert reply["sign_in"] == response["HX-Redirect"]
    assert KeptDraft.objects.exists() == (variant == "held")


def test_refusal_preserves_consumption_of_earlier_messages(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    factory = RequestFactory()
    req = factory.post(r.PAGE, {**r.draft_values(pickup), kept.FIELD: token})
    req.COOKIES.update({name: cookie.value for name, cookie in browser.cookies.items()})
    req._messages = CookieStorage(req)
    old = "Previous collection notice."
    fresh = "New collection notice."
    messages.info(req, old)
    assert [str(msg) for msg in messages.get_messages(req)] == [old]
    messages.info(req, fresh)
    assert sessions._keep_refused(req)
    response = HttpResponse()
    req._messages.update(response)
    next_page = factory.get(reverse("account_login"))
    next_page.COOKIES.update({name: cookie.value for name, cookie in response.cookies.items()})
    assert [str(msg) for msg in CookieStorage(next_page)] == [fresh, kept.KEPT_MESSAGE]


def test_held_refusal_completes_when_message_storage_is_unavailable(agent, pickup):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    r.pages.end_open(agent)
    request = RequestFactory().post(r.PAGE, {**r.draft_values(pickup), kept.FIELD: token})
    request.COOKIES.update({key: cookie.value for key, cookie in browser.cookies.items()})
    assert sessions._keep_refused(request)
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT


@pytest.mark.parametrize("organization", ["none", "other"])
def test_bound_picker_drops_a_restricted_form_for_an_ineligible_organization(
    agent, pickup, clinic, other_org, organization
):

    pickup.clients.set([clinic])
    browser = r.pages.browser_for(agent)
    # The section initially has no organization selected, so restricted forms can be seen.
    # A submitted empty organization is a definite choice and may only use public offers.
    values = submission(
        browser, pickup, client=str(other_org.pk) if organization == "other" else ""
    )
    page = browser.post(r.PAGE, values)
    assert page.status_code == 200
    picker, fields = page.context["intake_drawn"]
    assert picker.has_error("request_form") and intake.NOT_OFFERED in picker.errors["request_form"]
    assert (r.selected(page, "request_form"), fields is None, r.ROUTE in page.text) == (
        "",
        True,
        False,
    )
