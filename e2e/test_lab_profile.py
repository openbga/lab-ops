"""The laboratory profile in the browser: the administrator's dialogs, and where the changes show
(the profile and the client portal home)."""

import io
import re

import pytest
from helpers import field, sign_in, sign_out
from PIL import Image
from playwright.sync_api import Page, expect

from core.models import AuditEvent, Lab, LabLogo

pytestmark = pytest.mark.django_db(transaction=True)


def png(size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_profile_changes_show_on_the_profile_and_the_client_portal_home(page: Page, demo):
    seeded = Lab.objects.get().logo
    assert seeded is not None

    sign_in(page, "demo_admin", demo.password)
    page.get_by_role("link", name="Laboratory profile", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Laboratory profile")
    main = page.get_by_role("main")
    expect(main).to_contain_text("200 Example Way")
    expect(main.locator("img.vr-lab-logo")).to_be_visible()

    # Contact details, in the form dialog.
    main.get_by_role("link", name="Edit", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    field(dialog, "Phone").fill("555-0188")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Laboratory profile saved.")).to_be_visible()
    expect(main).to_contain_text("555-0188")

    # A file upload works inside the dialog: a file that is not an image is refused there and
    # the dialog stays open; a PNG replaces the logo.
    main.get_by_role("link", name="Replace logo", exact=True).click()
    expect(dialog).to_be_visible()
    upload = dialog.locator("input[type=file]")
    upload.set_input_files({"name": "logo.png", "mimeType": "image/png", "buffer": b"not an image"})
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(dialog).to_contain_text("This file could not be read.")
    assert Lab.objects.get().logo_id == seeded.pk
    upload.set_input_files(
        {"name": "wide.png", "mimeType": "image/png", "buffer": png((300, 100), (20, 90, 140))}
    )
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(page.get_by_text("Logo saved.")).to_be_visible()
    preview = main.locator("img.vr-lab-logo")
    expect(preview).to_be_visible()
    assert preview.evaluate("el => el.complete && el.naturalWidth") == 300

    # A certificate whose recorded validity has ended. The application prints no documents, so the
    # dialog has no choice about printing it.
    main.get_by_role("link", name="Add certificate", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Print on reports")).to_have_count(0)
    field(dialog, "Issuing authority").fill("Example County Health Office")
    field(dialog, "Certificate or laboratory ID").fill("EX-OLD-0000")
    field(dialog, "Valid until").fill("2025-12-31")
    dialog.get_by_role("button", name="Add certificate", exact=True).click()
    expect(page.get_by_text("Certificate added.")).to_be_visible()
    row = main.get_by_role("row", name=re.compile("Example County Health Office"))
    expect(row).to_contain_text("Expired")
    assert list(
        AuditEvent.objects.filter(actor__username="demo_admin", action__startswith="lab.")
        .order_by("occurred_at")
        .values_list("action", flat=True)
    )[-3:] == ["lab.profile_changed", "lab.logo_changed", "lab.certificate_added"]
    assert page.console_errors == []
    sign_out(page)

    # The client portal home shows the new logo and phone, and the laboratory's address.
    sign_in(page, "demo_client", demo.password)
    portal = page.get_by_role("main")
    expect(portal).to_contain_text("555-0188")
    expect(portal).to_contain_text("200 Example Way")
    logo = portal.get_by_role("img", name=re.compile(r" logo$"))
    expect(logo).to_be_visible()
    assert logo.evaluate("el => el.complete && el.naturalWidth") == 300
    current = LabLogo.objects.get(pk=Lab.objects.get().logo_id)
    assert page.request.get(f"/lab/profile/logo/{current.sha256}/").status == 200
    # An earlier logo is not the portal's to read, and neither is the staff profile.
    assert page.request.get(f"/lab/profile/logo/{seeded.sha256}/").status == 404
    assert page.console_errors == []
    assert page.goto("/lab/profile/").status == 403


def test_profile_without_a_logo_shows_the_name_only_and_other_roles_cannot_change_it(
    page: Page, sign_in_as
):
    from accounts.models import User
    from core import lab_profile

    lab_profile.remove_logo(actor=User.objects.get(username="demo_admin"))
    sign_in_as("supervisor")
    page.goto("/lab/profile/")
    main = page.get_by_role("main")
    expect(main.get_by_text("No logo", exact=True)).to_be_visible()
    expect(main.locator("img.vr-lab-logo")).to_have_count(0)
    # Every staff role reads the profile; only the admin role sees the change controls.
    for name in ("Edit", "Upload logo", "Replace logo", "Add certificate"):
        expect(main.get_by_role("link", name=name, exact=True)).to_have_count(0)
    for path in ("/lab/profile/edit/", "/lab/profile/logo/", "/lab/profile/certificates/new/"):
        assert page.goto(path).status == 403, path
