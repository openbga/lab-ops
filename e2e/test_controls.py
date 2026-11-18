"""Shared form controls (static/js/controls.js) through the real UI: no browser-default control.

A long and a short select in a dialog and in a filter bar, a list-or-other control, the date
pickers (typed entry, shortcut, keyboard, inside a dialog) and a file chooser. Each submission goes
to the server unchanged.
"""

import io
import re
from datetime import date, datetime

import pytest
from django.urls import reverse
from helpers import LAB_TZ, choose, expect_chosen, field, pick
from PIL import Image
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)


def menu_of(combobox):
    return combobox.page.locator(f"[id='{combobox.get_attribute('aria-controls')}']")


def open_contact_dialog(page: Page, name: str = "Example Family Clinic"):
    from clients.models import Client

    client = Client.objects.get(name=name)
    page.goto(reverse("clients:client-detail", args=[client.pk]))
    page.get_by_role("link", name="Add contact", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    return dialog, client


def test_dialog_select_searches_long_lists(page: Page, sign_in_as):
    from clients.models import Client

    sign_in_as("account_manager")
    page.goto(reverse("clients:client-list"))
    page.get_by_role("link", name="New organization", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Organization name").fill("E2E Controls Client")

    # A long list (states): the menu opens with a search box; Enter takes the first match.
    state = dialog.get_by_role("combobox", name="State")
    state.click()
    search = dialog.locator("input.dropdown-input:visible")
    expect(search).to_be_focused()
    search.fill("new york")
    expect(menu_of(state).get_by_role("option")).to_have_count(1)
    page.keyboard.press("Enter")
    expect(state).to_contain_text("New York")

    dialog.get_by_role("button", name="Create organization", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Client.objects.get(name="E2E Controls Client").state == "NY"


def test_dialog_short_list_types_ahead_and_accepts_other(page: Page, sign_in_as):
    from clients.models import ClientContact

    sign_in_as("client_services")
    dialog, client = open_contact_dialog(page)
    field(dialog, "Name").fill("E2E Controls Contact")
    field(dialog, "Email").fill("controls-contact@example.invalid")

    # A short list (contact roles): no search box; typing jumps, Space opens, Escape closes the
    # menu but not the dialog.
    role = dialog.get_by_role("combobox", name="Role")
    role.focus()
    page.keyboard.type("bill")
    expect(role).to_contain_text("Billing")
    page.wait_for_timeout(800)  # typed letters clear after a pause; then Space opens the menu
    page.keyboard.press("Space")
    expect(menu_of(role)).to_be_visible()
    expect(dialog.locator("input.dropdown-input:visible")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(menu_of(role)).to_be_hidden()
    expect(dialog).to_be_visible()

    # "Other" reveals a text input; what is typed there is stored as the role.
    choose(role, "Laboratory liaison")
    expect_chosen(role, "Laboratory liaison")
    dialog.get_by_role("button", name="Add contact", exact=True).click()
    expect(dialog).to_be_hidden()
    contact = ClientContact.objects.get(client=client, name="E2E Controls Contact")
    assert contact.role == "Laboratory liaison"


def test_filter_bar_select_applies_the_chosen_option(page: Page, sign_in_as):
    sign_in_as("technical")
    page.goto(reverse("clients:client-list"))
    page.get_by_role("button", name="More filters").click()
    state = page.get_by_role("combobox", name="State")
    # Django's blank choice is named from the label: "Any state".
    expect(state).to_contain_text("Any state")
    state.click()
    menu = menu_of(state)
    expect(menu.get_by_role("option", name="Any state")).to_be_visible()
    page.keyboard.type("new y")
    menu.get_by_role("option", name="New York", exact=True).click()
    expect(menu).to_be_hidden()
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]state=NY\b"))
    expect(page.get_by_role("combobox", name="State")).to_contain_text("New York")
    # The browser's own select never shows: the native element is the hidden submitted value.
    assert page.locator("select#id_state").evaluate("el => el.classList.contains('tomselected')")


def test_date_time_picker_typed_entry_keyboard_shortcut_and_validation_in_a_dialog(
    page: Page, sign_in_as
):
    from core.models import Announcement

    sign_in_as("admin")
    page.goto(reverse("announcement-list"))
    page.get_by_role("link", name=re.compile("^New announcement")).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Title").fill("E2E controls notice")
    field(dialog, "Message").fill("Synthetic announcement for the date picker journey.")
    dialog.get_by_label("Staff dashboard").check()

    starts = field(dialog, "Show from")
    until = field(dialog, "Show until")
    starts_value = dialog.locator("input[name=starts_at]")
    until_value = dialog.locator("input[name=ends_at]")
    expect(starts_value).to_have_attribute("type", "hidden")
    expect(dialog).to_contain_text(re.compile(r"Laboratory time, E[SD]T"))

    # Typed entry: a US date and a compact time become the server's own format.
    starts.fill("11/13/2026 0930")
    starts.press("Tab")
    expect(starts).to_have_value("2026-11-13 09:30")
    expect(starts_value).to_have_value("2026-11-13T09:30")

    # Keyboard: Alt+Down opens the calendar on a day; arrows move; Enter picks; the time follows.
    until.click()
    until.fill("2026-11-20 08:00")
    until.press("Alt+ArrowDown")
    calendar = dialog.get_by_role("dialog", name="Choose a date and time")
    expect(calendar).to_be_visible()
    expect(calendar.get_by_role("button", name="November 20, 2026")).to_be_focused()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    time = calendar.get_by_label("Time")
    expect(time).to_be_focused()
    time.fill("17:45")
    time.press("Enter")
    expect(calendar).to_be_hidden()
    expect(until).to_be_focused()
    expect(until_value).to_have_value("2026-11-21T17:45")

    # Escape closes the calendar only, and returns focus to the field.
    until.press("Alt+ArrowDown")
    expect(calendar).to_be_visible()
    page.keyboard.press("Escape")
    expect(calendar).to_be_hidden()
    expect(dialog).to_be_visible()
    expect(until).to_be_focused()

    # Shortcut: Now uses the laboratory's clock.
    dialog.locator(".vr-date-input:has(input[name=starts_at]) .vr-date-icon").click()
    calendar.get_by_role("button", name="Now", exact=True).click()
    lab_today = datetime.now(LAB_TZ).strftime("%Y-%m-%d")
    expect(starts_value).to_have_value(re.compile(rf"^{lab_today}T\d\d:\d\d$"))

    # An impossible date is refused beside the field and nothing is submitted.
    until.fill("2026-02-30 10:00")
    dialog.get_by_role("button", name=re.compile("^(Post|Save|Publish)")).click()
    expect(dialog.get_by_text("Enter a date and time as YYYY-MM-DD HH:MM.")).to_be_visible()
    expect(until).to_have_attribute("aria-invalid", "true")
    assert not Announcement.objects.filter(title="E2E controls notice").exists()

    until.fill("2026-12-01 18:00")
    dialog.get_by_role("button", name=re.compile("^(Post|Save|Publish)")).click()
    expect(dialog).to_be_hidden()
    saved = Announcement.objects.get(title="E2E controls notice")
    assert saved.ends_at.astimezone(LAB_TZ).strftime("%Y-%m-%d %H:%M") == "2026-12-01 18:00"


def test_date_picker_shortcut_and_typed_dates_in_the_certificate_dialog(page: Page, sign_in_as):
    from core.models import LabCertificate

    sign_in_as("admin")
    page.goto(reverse("lab-profile"))
    page.get_by_role("link", name="Add certificate", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Issuing authority").fill("Example State Health Department")
    field(dialog, "Certificate or laboratory ID").fill("EX-DATE-0001")

    valid_from = field(dialog, "Valid from")
    valid_until = field(dialog, "Valid until")
    until_value = dialog.locator("input[name=valid_until]")
    # A date-only field offers Today, not Now, and fills the laboratory's current date.
    valid_from.press("Alt+ArrowDown")
    calendar = dialog.get_by_role("dialog", name="Choose a date")
    calendar.get_by_role("button", name="Today", exact=True).click()
    expect(valid_from).to_have_value(datetime.now(LAB_TZ).strftime("%Y-%m-%d"))

    valid_until.fill("12/31/2099")
    valid_until.press("Tab")
    expect(valid_until).to_have_value("2099-12-31")
    expect(until_value).to_have_value("2099-12-31")
    valid_until.fill("2099-02-30")
    dialog.get_by_role("button", name="Add certificate", exact=True).click()
    expect(valid_until).to_have_attribute("aria-invalid", "true")
    assert not LabCertificate.objects.filter(number="EX-DATE-0001").exists()

    valid_until.fill("2099-12-31")
    dialog.get_by_role("button", name="Add certificate", exact=True).click()
    expect(dialog).to_be_hidden()
    saved = LabCertificate.objects.get(number="EX-DATE-0001")
    assert saved.valid_until == date(2099, 12, 31)
    assert saved.valid_from == datetime.now(LAB_TZ).date()


def png(size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_file_component_names_the_chosen_file_and_accepts_a_drop(page: Page, sign_in_as):
    from core.models import Lab as LabProfile

    sign_in_as("admin")
    page.goto(reverse("lab-logo-edit"))
    dialog = page.locator("#vr-form-dialog")
    form = dialog if dialog.is_visible() else page
    zone = form.locator("[data-vr-file]")
    expect(zone.locator(".vr-drop")).to_contain_text("Choose a file")
    expect(zone.locator(".vr-drop-hint")).to_have_text("PNG or JPEG files")
    native = zone.locator("input[type=file]")
    native.focus()  # visually hidden, still the keyboard target; the zone shows the focus
    expect(native).to_be_focused()

    native.set_input_files(
        {"name": "first.png", "mimeType": "image/png", "buffer": png((60, 40), (20, 90, 140))}
    )
    expect(zone.locator("[data-vr-file-name]")).to_have_text(
        re.compile(r"^first\.png \(\d+ bytes\)$")
    )

    # Dropping a file onto the zone fills the same input.
    zone.locator(".vr-drop").evaluate(
        """(drop, bytes) => {
            const transfer = new DataTransfer();
            const file = new File([new Uint8Array(bytes)], "dropped.png", {type: "image/png"});
            transfer.items.add(file);
            drop.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true}));
        }""",
        list(png((80, 50), (140, 90, 20))),
    )
    expect(zone.locator("[data-vr-file-name]")).to_contain_text("dropped.png")
    form.get_by_role("button", name="Replace logo", exact=True).click()
    expect(page.get_by_text("Logo saved.")).to_be_visible()
    assert LabProfile.objects.get().logo.width == 80


def test_clear_button_and_keys_reset_a_filter_and_an_optional_dialog_field(page: Page, sign_in_as):
    sign_in_as("account_manager")
    # A filter select: the x (shown on hover) and Delete return it to its "Any ..." choice; the
    # filter still waits for Apply.
    page.goto(reverse("clients:client-list"))
    page.get_by_role("button", name="More filters").click()
    state = page.get_by_role("combobox", name="State")
    clear = page.get_by_role("button", name="Clear state")
    expect(clear).to_be_hidden()  # nothing to clear yet
    pick(state, label="Ohio")
    state.hover()
    expect(clear).to_be_visible()
    clear.click()
    expect(state).to_contain_text("Any state")
    expect(state).to_be_focused()
    expect(clear).to_be_hidden()
    expect(page).not_to_have_url(re.compile("state="))  # not submitted by clearing
    pick(state, label="Texas")
    page.keyboard.press("Delete")
    expect(state).to_contain_text("Any state")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).not_to_have_url(re.compile(r"state=\w"))

    # An optional field in a dialog: the x is a keyboard stop of its own; Backspace also clears.
    dialog, _client = open_contact_dialog(page)
    role = dialog.get_by_role("combobox", name="Role")
    pick(role, label="Billing")
    role.focus()
    page.keyboard.press("Tab")
    clear_role = dialog.get_by_role("button", name="Clear role")
    expect(clear_role).to_be_focused()
    page.keyboard.press("Enter")
    expect(role).to_contain_text("Not recorded")
    expect(role).to_be_focused()
    pick(role, label="Technical or quality")
    role.focus()
    page.keyboard.press("Backspace")
    expect(role).to_contain_text("Not recorded")
    # A changed field is a real change: closing the dialog now asks before discarding it.
    pick(role, label="Billing")
    dialog.locator("[data-vr-form-close]").click()
    expect(page.get_by_role("dialog", name="Discard unsaved changes?")).to_be_visible()
