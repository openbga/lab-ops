"""Nonconformances in the browser, on the demonstration laboratory (module on, seeded records):
raising a record and working an action in the form dialog, the record and its pages on a phone,
and the administrator switching the module off and on again from the laboratory profile."""

import re

import pytest
from conftest import NARROW, Lab, View
from helpers import field, pick, sign_in
from playwright.sync_api import Page, expect
from test_phone_layout import check

from nonconformance.models import ActionStatus, CorrectiveAction, Nonconformance

pytestmark = pytest.mark.django_db(transaction=True)


def test_raise_a_record_and_finish_an_action(page: Page, demo: Lab, view: View):
    """Every staff role raises a record from the Quality section; an action's assignee marks it
    done from the record page. In {desktop, phone} x {light, dark}."""
    sign_in(page, "demo_client_services", demo.password)
    page.goto("/quality/nonconformances/")
    expect(page.get_by_role("heading", level=1)).to_have_text("Nonconformances")
    page.get_by_role("link", name="New nonconformance").first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Do not enter patient names or identifiers.")
    field(dialog, "Title").fill("Courier box arrived without ice packs")
    field(dialog, "What happened").fill("Box for ORD-TEST-000777 arrived warm at 10:05.")
    pick(field(dialog, "Source"), label="Found internally")
    pick(field(dialog, "Severity"), label="Minor")
    dialog.get_by_role("button", name="Raise nonconformance").click()
    expect(page.get_by_text(re.compile(r"Nonconformance NC-\d{6} raised\."))).to_be_visible()
    raised = Nonconformance.objects.get(title="Courier box arrived without ice packs")
    expect(page).to_have_url(re.compile(rf"/quality/nonconformances/{raised.pk}/$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text(raised.number)
    assert raised.raised_by.username == "demo_client_services" and raised.owner is None
    if view.phone:
        problems = []
        check(page, "record page", problems)
        assert not problems, "\n".join(problems)

    # The technician's overdue action on the seeded refrigerator record.
    page.context.clear_cookies()  # the account menu is in the drawer on a phone
    sign_in(page, "demo_technical", demo.password)
    action = CorrectiveAction.objects.get(
        status=ActionStatus.OPEN, description__startswith="Add a weekly door-seal check"
    )
    page.goto(f"/quality/nonconformances/{action.nonconformance_id}/")
    expect(page.get_by_role("main")).to_contain_text("1 overdue action")
    # The technician owns the record, so the row's Actions menu holds Mark done.
    row = page.get_by_role("row").filter(has_text="Add a weekly door-seal check")
    row.get_by_role("button", name=re.compile("^Actions")).click()
    row.get_by_role("link", name="Mark done").click()
    expect(dialog).to_be_visible()
    field(dialog, "What was done").fill("Added to the weekly storage checklist.")
    dialog.get_by_role("button", name="Mark done").click()
    expect(page.get_by_text("Action marked done.")).to_be_visible()
    expect(page.get_by_role("main")).not_to_contain_text("overdue action")
    action.refresh_from_db()
    assert action.status == ActionStatus.DONE and action.finished_by.username == "demo_technical"


def test_the_quality_pages_fit_a_phone(page: Page, demo: Lab):
    page.set_viewport_size(NARROW)
    sign_in(page, "demo_supervisor", demo.password)
    record = Nonconformance.objects.get(title__startswith="Specimen storage refrigerator")
    problems = []
    for url in (
        "/quality/nonconformances/",
        "/quality/actions/?show=overdue",
        "/quality/summary/",
        f"/quality/nonconformances/{record.pk}/",
    ):
        page.goto(url)
        check(page, url, problems)
    page.get_by_role("link", name="Record impact").click()
    page.locator("#vr-form-dialog").get_by_role("button", name="Save impact").wait_for()
    check(page, "impact dialog", problems)
    assert not problems, "\n".join(problems)


def test_switching_the_module_off_hides_it_and_keeps_its_records(page: Page, demo: Lab):
    count = Nonconformance.objects.count()
    sign_in(page, "demo_admin", demo.password)
    expect(page.get_by_role("link", name="Nonconformances", exact=True)).to_be_visible()
    page.goto("/lab/profile/")
    modules = page.get_by_role("region", name=re.compile("^Optional modules"))
    modules.get_by_role("link", name=re.compile("^Turn off")).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Reason").fill("Synthetic check: the laboratory pauses the module.")
    dialog.get_by_role("button", name=re.compile("^Turn off")).click()
    expect(page.get_by_role("main")).to_contain_text("Off")
    expect(page.get_by_role("link", name="Nonconformances", exact=True)).to_have_count(0)
    response = page.goto("/quality/nonconformances/")
    assert response.status == 404
    assert Nonconformance.objects.count() == count

    page.goto("/lab/profile/")
    modules.get_by_role("link", name=re.compile("^Turn on")).click()
    field(dialog, "Reason").fill("Synthetic check: back in use.")
    dialog.get_by_role("button", name=re.compile("^Turn on")).click()
    expect(page.get_by_role("link", name="Nonconformances", exact=True)).to_be_visible()
    assert page.goto("/quality/nonconformances/").status == 200
