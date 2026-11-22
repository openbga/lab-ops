"""Confirmation of the New ticket fixes, with synthetic forms and ordinary requests."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import unescape

import pytest
from django.db import transaction
from django.db.models.functions import Now
from django.urls import reverse

from accounts import kept
from accounts.models import AccountSession, KeptDraft
from request_forms import intake
from request_forms.models import RequestTemplate
from request_forms.selectors import kept_eligible_fields
from tests import test_review_kept_typing_new_ticket as r

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
capable = r.capable


def options(page):
    select = re.search(r'<select\b[^>]*name="request_form"[^>]*>(.*?)</select>', page.text, re.S)
    if not select:
        return {}
    return {
        key: unescape(label.strip())
        for key, label in re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', select[1])
    }


def after_list(monkeypatch, edit):
    original = intake.definition.read
    calls = []

    def changed(*args, **kwargs):
        if not calls:
            calls.append(True)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(r.in_worker, edit).result(timeout=8)
        return original(*args, **kwargs)

    monkeypatch.setattr(intake.definition, "read", changed)
    return calls


def alternate(pickup, active=True):
    return RequestTemplate.objects.create(
        key="archive", name="Archive collections", category=pickup.category, is_active=active
    )


@pytest.mark.django_db(transaction=True)
def test_a_requested_form_deleted_after_the_list_leaves_the_other_choice(
    agent, pickup, monkeypatch
):
    spare = alternate(pickup)
    browser = r.pages.browser_for(agent)
    browser.raise_request_exception = False

    def remove():
        with transaction.atomic():
            pickup.fields.all().delete()
            RequestTemplate.objects.filter(pk=pickup.pk).delete()

    edited = after_list(monkeypatch, remove)
    response = r.section(browser, pickup)
    assert edited and response.status_code == 200
    assert options(response) == {str(spare.pk): spare.name}
    assert 'name="form_definition"' not in response.text


@pytest.mark.django_db(transaction=True)
def test_picker_remains_available_when_one_sole_offer_replaces_another(agent, pickup, monkeypatch):
    spare = alternate(pickup, active=False)
    browser = r.pages.browser_for(agent)

    def replace():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=pickup.pk).update(is_active=False)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    edited = after_list(monkeypatch, replace)
    response = browser.get(
        r.SECTION, {"category": pickup.category_id}, headers={"HX-Request": "true"}
    )
    offered = options(response)
    assert edited and response.status_code == 200
    assert offered in ({str(pickup.pk): pickup.name}, {str(spare.pk): spare.name})
    assert r.selected(response, "request_form") in offered


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("bound", [False, True], ids=["section", "validation"])
def test_two_distinct_ticket_forms_keep_distinct_labels(agent, pickup, monkeypatch, bound):
    spare = alternate(pickup)
    browser = r.pages.browser_for(agent)
    drawn = r.section(browser, pickup)
    values = r.draft_values(
        pickup,
        subject="",
        category_terms=r.hidden(drawn, "category_terms"),
        form_definition=r.hidden(drawn, "form_definition"),
    )

    def rename():
        with transaction.atomic():
            RequestTemplate.objects.filter(pk=spare.pk).update(name="Retained transport")
            RequestTemplate.objects.filter(pk=pickup.pk).update(name=spare.name)
            RequestTemplate.objects.filter(pk=spare.pk).update(name=pickup.name)

    if bound:
        original = intake.Intake.is_valid

        def after_validation(self):
            answer = original(self)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(r.in_worker, rename).result(timeout=8)
            return answer

        monkeypatch.setattr(intake.Intake, "is_valid", after_validation)
        page = browser.post(r.PAGE, values)
    else:
        changed = after_list(monkeypatch, rename)
        page = r.section(browser, pickup)
        assert changed
    assert page.status_code == 200
    labels = options(page)
    assert set(labels) == {str(pickup.pk), str(spare.pk)}
    assert set(labels.values()) == {pickup.name, spare.name}, labels


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["move_offer", "retire"])
def test_refused_post_explains_a_late_change_to_the_selected_form(
    agent, pickup, other_org, clinic, monkeypatch, change
):
    spare = alternate(pickup, active=False)
    browser = r.pages.browser_for(agent)
    drawn = r.section(browser, pickup, client=clinic.pk)
    values = r.draft_values(
        pickup,
        subject="",
        client=str(clinic.pk),
        category_terms=r.hidden(drawn, "category_terms"),
        form_definition=r.hidden(drawn, "form_definition"),
    )
    original = intake.Intake.is_valid

    def change_offers():
        with transaction.atomic():
            if change == "move_offer":
                pickup.clients.set([other_org])
            else:
                RequestTemplate.objects.filter(pk=pickup.pk).update(is_active=False)
            RequestTemplate.objects.filter(pk=spare.pk).update(is_active=True)

    def finish_validation(self):
        valid = original(self)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(r.in_worker, change_offers).result(timeout=8)
        return valid

    monkeypatch.setattr(intake.Intake, "is_valid", finish_validation)
    response = browser.post(r.PAGE, values)
    assert response.status_code == 200 and response.context["form"].has_error("subject")
    if str(spare.pk) not in options(response):
        # A coherent reading of the old options also retains the original answers.
        assert r.selected(response, "request_form") == str(pickup.pk) and r.ROUTE in response.text
        return
    if change == "move_offer":
        assert r.selected(response, "request_form") != str(pickup.pk), options(response)
    assert response.context["intake_drawn"][0].has_error("request_form"), (
        "The form disappeared during this refused save, without a picker message"
    )
    assert 'name="field-count"' not in response.text


@pytest.mark.parametrize("first", ["post", "keep"])
def test_duplicate_acknowledgments_leave_one_notice_and_preserve_the_ending(agent, pickup, first):
    browser = r.pages.browser_for(agent)
    token = r.drawn_marker(r.section(browser, pickup))[0]
    values = r.draft_values(pickup)
    AccountSession.objects.filter(user=agent, ended_at__isnull=True).update(
        last_active_at=Now() - timedelta(minutes=31)
    )
    assert browser.get(reverse("account-session")).json()["signed_in"] is False

    def posted():
        response = browser.post(
            r.PAGE,
            {**values, kept.FIELD: token},
            headers={"X-VR-Form": "1", "HX-Current-URL": "http://testserver/requests/"},
        )
        assert response.status_code == 401

    def acknowledged():
        assert r.pages.send(browser, token, values) == 1

    for action in (posted, acknowledged) if first == "post" else (acknowledged, posted):
        action()
    assert KeptDraft.objects.get().values["subject"] == r.SUBJECT
    notices = [
        str(message) for message in browser.get(reverse("account_login")).context["messages"]
    ]
    assert any("without activity" in message for message in notices)
    assert notices.count(kept.KEPT_MESSAGE) == 1, notices
    assert kept.KEPT_MESSAGE not in browser.get(reverse("account_login")).text


@pytest.mark.parametrize("user_fixture", ["agent", "capable", "portal"])
def test_box_recheck_matches_the_field_drawn_for_a_hidden_requirement(
    request, pickup, clinic, portal_user, user_fixture
):
    user = (
        portal_user(clinic) if user_fixture == "portal" else request.getfixturevalue(user_fixture)
    )
    pickup.fields.update(required=False)
    patient = pickup.fields.get(key="patient")
    pickup.fields.filter(pk=patient.pk).update(required=True)
    browser = r.pages.browser_for(user)
    page = (
        browser.get(reverse("portal_forms:raise", args=[pickup.key]))
        if user_fixture == "portal"
        else r.section(browser, pickup)
    )
    assert page.status_code == 200
    eligible = kept_eligible_fields(pickup.pk, user.pk)
    visible_box = 'name="details_not_known"' in page.text
    assert ("details_not_known" in eligible) == visible_box == (user_fixture != "portal")
    assert "field-patient" not in eligible


def test_empty_post_and_head_leave_pending_copy_for_a_get(agent, pickup):
    browser = r.pending(agent, pickup)
    for response in (browser.post(r.PAGE, {}), browser.head(r.PAGE)):
        assert response.status_code == 200 and r.SUBJECT not in response.content.decode()
    assert KeptDraft.objects.get().taken_at is None
    assert r.SUBJECT in browser.get(r.PAGE).text
