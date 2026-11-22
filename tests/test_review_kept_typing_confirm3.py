"""Regression checks for the changes between the second and third confirmations."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.db import transaction
from django.urls import reverse

from accounts.models import KeptDraft
from clients.models import ClientSite
from clients.services import update_site
from desk.portal_forms import RaiseRequestForm
from desk.tests import test_kept_typing as pages
from request_forms.forms import PortalRaiseForm
from request_forms.models import FieldKind
from request_forms.services import update_field
from tests import test_review_kept_typing_verify as previous
from tests.test_review_session_list_verify import in_worker, wait_for_lock

case = previous.case
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("also_chose", [False, True], ids=["only-retired-text", "with-choice"])
def test_retiring_the_only_populated_text_field_still_explains_the_omission(
    case, staff, also_chose
):
    choice = previous.add_field(case, "packaging", FieldKind.CHOICE, choices=["Tray", "Box"])
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    field = case.template.fields.get(key="route")
    update_field(
        actor=staff("retire_route", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        is_active=False,
        reason="Retire the route question.",
    )
    content = {"field-route": "Morning collection"}
    if also_chose:
        content[f"field-{choice.key}"] = "Tray"
    count = pages.send(browser, token, content)
    browser.force_login(case.portal)
    page = browser.get(url)
    assert page.status_code == 200
    assert "Morning collection" not in page.text
    selected = page.context["form"][f"field-{choice.key}"].value()
    if also_chose:
        assert selected == "Tray", f"The still-offered choice disappeared after kept={count}"
    assert previous.missed(page), f"No omitted-answer notice following kept={count}"


@pytest.mark.django_db(transaction=True)
def test_keep_answers_normally_when_template_deletion_commits_during_the_recheck(case):
    browser, token = pages.typed_then_signed_out(case.portal, previous.raise_page(case))
    browser.raise_request_exception = False
    pids = Queue()

    def deliver():
        return pages.send(browser, token, {"description": "Collect the return trays"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            case.template.fields.all().delete()
            case.template.delete()
            delivery = pool.submit(in_worker, deliver, pids)
            wait_for_lock(delivery, pids)
        assert delivery.result(timeout=10) == 0
    assert not KeptDraft.objects.exists()


@pytest.mark.parametrize("which", ["general", "template"])
@pytest.mark.django_db(transaction=True)
def test_portal_never_draws_a_site_label_that_only_existed_after_deactivation(
    case, staff, monkeypatch, which
):
    site = ClientSite.objects.create(client=case.portal.client, name="Receiving desk", code="R")
    manager = staff("directory_editor", "admin")
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    form_type = RaiseRequestForm if which == "general" else PortalRaiseForm
    construct = form_type.__init__
    browser = pages.browser_for(case.portal)

    def close_desk():
        return update_site(
            actor=manager,
            site_id=site.pk,
            expected_revision=site.revision,
            reason="Close the receiving desk.",
            name="Closed receiving desk",
            code="ARCHIVE",
            is_active=False,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:

        def construct_then_close(form, *args, **kwargs):
            construct(form, *args, **kwargs)
            assert form.fields["site"].queryset.filter(pk=site.pk).exists()
            assert not pool.submit(in_worker, close_desk).result(timeout=10).is_active

        with monkeypatch.context() as patch:
            patch.setattr(form_type, "__init__", construct_then_close)
            page = browser.get(url)
    assert page.status_code == 200
    assert "Closed receiving desk (ARCHIVE)" not in page.text


@pytest.mark.parametrize("first_omitted", [False, True])
def test_only_the_first_accepted_delivery_decides_the_dropped_notice(case, staff, first_omitted):
    url = previous.raise_page(case)
    browser, token = pages.typed_then_signed_out(case.portal, url)
    field = case.template.fields.get(key="route")
    update_field(
        actor=staff("retire_before_delivery", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        is_active=False,
        reason="Remove an optional route question.",
    )
    for description, omitted in (("First collection", first_omitted), ("Retry", not first_omitted)):
        content = {"description": description, "field-route": "Side door" if omitted else " "}
        assert pages.send(browser, token, content) == 1
    row = KeptDraft.objects.get()
    assert row.dropped == first_omitted
    assert "field-route" not in row.values
    browser.force_login(case.portal)
    page = browser.get(url)
    assert page.context["form"]["description"].value() == "First collection"
    assert previous.missed(page) == first_omitted


@pytest.mark.parametrize("which", ["general", "template"])
def test_stale_site_message_only_instructs_a_choice_when_there_is_a_selector(case, which):
    site = ClientSite.objects.create(client=case.portal.client, name="Receiving desk")
    url = reverse("portal:request-create") if which == "general" else previous.raise_page(case)
    browser = pages.browser_for(case.portal)
    opened = browser.get(url)
    form = opened.context["form"]
    assert form.fields["site"].queryset.filter(pk=site.pk).exists()
    data = {item.html_name: item.value() or "" for item in form.hidden_fields()}
    data.update(subject="Desk pickup", description="Collect trays", site=site.pk)
    if which == "general":
        data.update(category=case.category.pk, category_terms=opened.context["category_terms"])
    site.is_active = False
    site.save(update_fields=["is_active"])
    response = browser.post(url, data)
    assert response.status_code == 200
    refused = response.context["form"]
    assert refused.non_field_errors() and "site" not in refused.fields
    assert refused["description"].value() == "Collect trays"
    assert not type(case.ticket).objects.filter(subject=data["subject"]).exists()
    # Submit just the fields the user still sees. This is now an organization-wide request.
    del data["site"]
    assert browser.post(url, data).status_code == 302
    assert type(case.ticket).objects.get(subject=data["subject"]).site_id is None
    assert "Choose again." not in str(refused.non_field_errors()), (
        "The refusal asks for another site, but the form has no site selector."
    )
