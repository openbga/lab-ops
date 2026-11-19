"""Security settings in the browser: the administrator edits a contact through the form dialog and
sees it on the page and the profile; the page fits a phone in both themes; other staff read the
contacts on the profile and have no Security settings menu item."""

import re

import pytest
from conftest import DESKTOP, NARROW
from helpers import field, sign_in, sign_out
from playwright.sync_api import Page, expect

from core.models import AuditEvent, LabContact

pytestmark = pytest.mark.django_db(transaction=True)

OVERFLOW = "() => document.documentElement.scrollWidth - window.innerWidth"


def test_admin_edits_a_contact_and_staff_read_it_on_the_profile(page: Page, demo):
    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="Security settings", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Security settings")
    main = page.get_by_role("main")
    for title in ("Sign-in", "Sessions", "Patient information", "Connection"):
        expect(main.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(main).to_contain_text("Information Security Office")

    # The privacy contact, in the form dialog opened from this page.
    main.get_by_role("link", name="Privacy contact", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_role("dialog", name="Edit privacy contact")).to_be_visible()
    expect(field(dialog, "Name or office")).to_have_value("Compliance Office")
    field(dialog, "Name or office").fill("Example Privacy Desk")
    field(dialog, "Email").fill("privacy-desk@example.invalid")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(dialog).to_contain_text("This field is required.")  # the reason
    expect(field(dialog, "Name or office")).to_have_value("Example Privacy Desk")
    field(dialog, "Reason").fill("The privacy desk took over from the compliance office.")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Privacy contact saved.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/lab/security/$"))
    row = main.get_by_role("row", name=re.compile("^Privacy contact"))
    expect(row).to_contain_text("Example Privacy Desk")
    expect(row).to_contain_text("privacy-desk@example.invalid")
    expect(main).to_contain_text("The privacy desk took over from the compliance office.")
    assert LabContact.objects.get(kind="privacy").name == "Example Privacy Desk"
    event = AuditEvent.objects.filter(action="lab.contact_changed").latest("occurred_at")
    assert event.actor.username == "demo_admin" and event.after["kind"] == "privacy"

    page.goto("/lab/profile/")
    expect(page.get_by_role("main")).to_contain_text("Example Privacy Desk")
    assert page.console_errors == []

    # A phone, in both themes: nothing scrolls sideways.
    page.set_viewport_size(NARROW)
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        page.goto("/lab/security/")
        expect(page.get_by_role("heading", level=1)).to_have_text("Security settings")
        assert page.evaluate(OVERFLOW) <= 0, theme
    page.set_viewport_size(DESKTOP)
    page.emulate_media(color_scheme="light")
    sign_out(page)

    # Other staff read the contacts on the profile, without the form's links and the menu item.
    sign_in(page, "demo_supervisor", demo.password)
    page.goto("/lab/profile/")
    profile = page.get_by_role("main")
    contacts = profile.get_by_role("region", name="Security and privacy contacts")
    expect(contacts).to_contain_text("Information Security Office")
    expect(contacts).to_contain_text("Example Privacy Desk")
    expect(contacts.get_by_role("link", name="Privacy contact", exact=True)).to_have_count(0)
    expect(contacts.get_by_role("link", name="Security contact", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Security settings", exact=True)).to_have_count(0)
    assert page.goto("/lab/security/").status == 403
