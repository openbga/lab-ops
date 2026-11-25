"""Shared form dialogs keep page context, input, keyboard access and real POST semantics."""

import io
import re

import pytest
from conftest import NARROW
from django.urls import reverse
from helpers import field, sign_in
from PIL import Image
from playwright.sync_api import Locator, Page, expect

from core.form_surfaces import FORM_SURFACES

pytestmark = pytest.mark.django_db(transaction=True)


def open_client_form(page: Page, sign_in_as) -> tuple[Locator, Locator, str]:
    sign_in_as("account_manager")
    page.goto(reverse("clients:client-list"))
    origin = page.url
    trigger = page.get_by_role("link", name="New organization", exact=True).first
    trigger.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New client organization")
    expect(field(dialog, "Organization name")).to_be_focused()
    return dialog, trigger, origin


def no_document_overflow(page: Page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    ), "The dialog must not introduce horizontal document scrolling."


def test_first_edit_waits_for_slow_form_initialization_without_losing_input(page: Page, sign_in_as):
    """A delayed fragment must finish initializing before its first field accepts typing."""
    from clients.models import Client

    sign_in_as("account_manager")
    page.goto(reverse("clients:client-list"))
    page.evaluate("htmx.config.defaultSettleDelay = 1000")
    page.get_by_role("link", name="New organization", exact=True).first.click()
    dialog = page.locator("#vr-form-dialog")
    code = field(dialog, "Account code")
    expect(code).to_be_disabled()
    # Start with a field other than the autofocus target. Late focus must not steal typing.
    code.fill("SLOW-1")
    expect(code).to_have_value("SLOW-1")
    expect(code).to_be_focused()
    field(dialog, "Organization name").fill("E2E slow dialog initialization")
    dialog.get_by_role("button", name="Create organization", exact=True).click()
    expect(dialog).to_be_hidden()
    assert Client.objects.get(name="E2E slow dialog initialization").account_number == "SLOW-1"


def test_link_preserves_context_and_desktop_dialog_maximizes_restores_and_returns_focus(
    page: Page, sign_in_as
):
    dialog, trigger, origin = open_client_form(page, sign_in_as)
    expect(page).to_have_url(origin)
    frame = dialog.locator(".modal-content")
    normal = frame.bounding_box()
    viewport = page.viewport_size
    assert normal and viewport
    assert normal["x"] >= 23 and normal["y"] >= 23, normal
    assert normal["width"] <= viewport["width"] - 47, normal
    assert normal["height"] <= viewport["height"] - 47, normal
    no_document_overflow(page)

    maximize = dialog.locator("[data-vr-form-maximize]")
    maximize.click()
    expect(dialog.locator(".modal-dialog")).to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    full = frame.bounding_box()
    assert full
    assert full["x"] == pytest.approx(0, abs=1)
    assert full["y"] == pytest.approx(0, abs=1)
    assert full["width"] == pytest.approx(viewport["width"], abs=1)
    assert full["height"] == pytest.approx(viewport["height"], abs=1)
    maximize.click()
    expect(dialog.locator(".modal-dialog")).not_to_have_class(
        re.compile(r"(?:^|\s)modal-fullscreen(?:\s|$)")
    )
    restored = frame.bounding_box()
    assert restored
    assert restored["width"] == pytest.approx(normal["width"], abs=1)
    assert restored["x"] == pytest.approx(normal["x"], abs=1)

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(origin)
    expect(trigger).to_be_focused()
    assert page.console_errors == []


def test_narrow_form_is_fullscreen_and_keeps_fields_and_actions_inside_viewport(
    page: Page, sign_in_as
):
    page.set_viewport_size(NARROW)
    dialog, _trigger, origin = open_client_form(page, sign_in_as)
    frame = dialog.locator(".modal-content").bounding_box()
    assert frame
    assert frame["x"] == pytest.approx(0, abs=1)
    assert frame["y"] == pytest.approx(0, abs=1)
    assert frame["width"] == pytest.approx(NARROW["width"], abs=1)
    assert frame["height"] == pytest.approx(NARROW["height"], abs=1)
    no_document_overflow(page)
    field(dialog, "Internal notes").fill("Synthetic narrow-screen notes")
    save = dialog.get_by_role("button", name="Create organization", exact=True)
    save.scroll_into_view_if_needed()
    bounds = save.bounding_box()
    assert bounds and bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= NARROW["width"] + 1
    assert bounds["y"] + bounds["height"] <= NARROW["height"] + 1
    expect(page).to_have_url(origin)
    assert page.console_errors == []


def test_dirty_dialog_keeps_edits_until_discard_is_explicit(page: Page, sign_in_as):
    from clients.models import Client

    dialog, trigger, origin = open_client_form(page, sign_in_as)
    name = "E2E unsaved modal organization"
    field(dialog, "Organization name").fill(name)
    dialog.locator("[data-vr-form-close]").click()
    confirm = page.get_by_role("dialog", name="Discard unsaved changes?")
    keep = confirm.get_by_role("button", name="Keep editing", exact=True)
    expect(keep).to_be_focused()  # the safe choice takes the focus
    keep.click()
    expect(confirm).to_be_hidden()
    expect(field(dialog, "Organization name")).to_have_value(name)
    expect(dialog).to_be_visible()

    page.keyboard.press("Escape")
    discard = confirm.get_by_role("button", name="Discard changes", exact=True)
    expect(discard).to_be_visible()
    discard.click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(page).to_have_url(origin)
    assert not Client.objects.filter(name=name).exists()

    trigger.click()
    expect(dialog).to_be_visible()
    expect(field(dialog, "Organization name")).to_have_value("")
    assert page.console_errors == []


def test_invalid_post_preserves_input_then_save_follows_server_redirect(page: Page, sign_in_as):
    from clients.models import Client

    dialog, _trigger, origin = open_client_form(page, sign_in_as)
    name = "E2E validated modal organization"
    field(dialog, "Organization name").fill(name)
    field(dialog, "Account code").fill("MODAL-1")
    field(dialog, "General email").fill("invalid-email")
    dialog.get_by_role("button", name="Create organization", exact=True).click()
    email = field(dialog, "General email")
    expect(email).to_have_attribute("aria-invalid", "true")
    expect(email).to_be_focused()
    expect(field(dialog, "Organization name")).to_have_value(name)
    expect(field(dialog, "Account code")).to_have_value("MODAL-1")
    expect(email).to_have_value("invalid-email")
    expect(page).to_have_url(origin)
    assert not Client.objects.filter(name=name).exists()

    email.fill("modal-organization@example.invalid")
    dialog.get_by_role("button", name="Create organization", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text(f"{name} added.", exact=True)).to_be_visible()
    client = Client.objects.get(name=name)
    expect(page).to_have_url(
        re.compile(re.escape(reverse("clients:client-detail", args=[client.pk])) + r"$")
    )
    assert client.email == "modal-organization@example.invalid"
    assert page.console_errors == []


def test_direct_form_url_opens_dialog_and_cancel_returns_to_its_list(page: Page, sign_in_as):
    sign_in_as("account_manager")
    path = reverse("clients:client-create")
    page.goto(path)
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("New client organization")
    expect(field(dialog, "Organization name")).to_be_visible()
    expect(page).to_have_url(re.compile(re.escape(path) + r"$"))
    dialog.get_by_role("link", name="Cancel", exact=True).click()
    expect(page).to_have_url(re.compile(re.escape(reverse("clients:client-list")) + r"$"))
    expect(
        page.get_by_role("heading", name="Client organizations", exact=True, level=1)
    ).to_be_visible()
    expect(dialog).to_be_hidden()
    assert page.console_errors == []


def test_dialog_blocks_background_focus_and_traps_keyboard_in_both_directions(
    page: Page, sign_in_as
):
    dialog, trigger, _origin = open_client_form(page, sign_in_as)
    # Check inertness and actual focus without coupling to the shell wrapper IDs.
    assert trigger.evaluate("el => Boolean(el.closest('[inert]'))")
    trigger.evaluate("el => el.focus()")
    expect(trigger).not_to_be_focused()
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )

    controls = dialog.locator(
        'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), '
        'select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'
    ).filter(visible=True)
    last = controls.last
    last.focus()
    page.keyboard.press("Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(last).not_to_be_focused()
    first = controls.first
    first.focus()
    page.keyboard.press("Shift+Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)"), page.evaluate(
        "({tag: document.activeElement.tagName, id: document.activeElement.id})"
    )
    expect(first).not_to_be_focused()
    assert page.console_errors == []


def test_logo_upload_in_dialog_refuses_a_bad_file_and_keeps_the_dialog_open(page: Page, sign_in_as):
    from core.models import Lab

    sign_in_as("admin")
    page.goto(reverse("lab-profile"))
    origin = page.url
    seeded = Lab.objects.get().logo_id
    page.get_by_role("link", name="Replace logo", exact=True).click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    upload = dialog.locator("input[type=file]")
    upload.set_input_files({"name": "logo.png", "mimeType": "image/png", "buffer": b"not an image"})
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(dialog).to_contain_text("This file could not be read.")
    expect(dialog).to_be_visible()
    assert Lab.objects.get().logo_id == seeded

    buffer = io.BytesIO()
    Image.new("RGB", (300, 100), (20, 90, 140)).save(buffer, format="PNG")
    upload.set_input_files(
        {"name": "wide.png", "mimeType": "image/png", "buffer": buffer.getvalue()}
    )
    dialog.get_by_role("button", name="Replace logo", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Logo saved.")).to_be_visible()
    expect(page).to_have_url(origin)
    assert Lab.objects.get().logo_id != seeded
    assert page.console_errors == []


def test_edit_dialog_from_the_record_page_reopens_on_error_and_saves_with_a_reason(
    page: Page, sign_in_as
):
    from clients.models import Client

    sign_in_as("client_services")
    client = Client.objects.get(name="Example Family Clinic")
    path = reverse("clients:client-detail", args=[client.pk])
    page.goto(path)
    trigger = page.get_by_role("link", name="Edit organization", exact=True)
    trigger.click()
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#vr-form-title")).to_have_text("Edit Example Family Clinic")
    field(dialog, "Main phone").fill("555-0199")
    # A change needs its reason; the refused save reopens the dialog with the same input.
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    reason = field(dialog, "Reason for change")
    expect(reason).to_have_attribute("aria-invalid", "true")
    expect(reason).to_be_focused()
    expect(field(dialog, "Main phone")).to_have_value("555-0199")
    client.refresh_from_db()
    assert client.phone != "555-0199"

    reason.fill("Synthetic phone change confirmed with the clinic.")
    dialog.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Changes recorded with their reason.", exact=True)).to_be_visible()
    expect(dialog).to_be_hidden()
    expect(page).to_have_url(re.compile(re.escape(path) + r"$"))
    expect(page.get_by_role("main")).to_contain_text("555-0199")
    client.refresh_from_db()
    assert client.phone == "555-0199"
    assert page.console_errors == []


def file_on(request):
    """A file on a note of `request`, through the attachments service (M2-B)."""
    import io

    from PIL import Image

    from accounts.models import User
    from attachments.inspection import inspect_bytes
    from attachments.services import add_entry_with_files

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    request.refresh_from_db()
    entry = add_entry_with_files(
        actor=User.objects.get(username="demo_client_services"),
        request=request,
        kind="note",
        body="Synthetic.",
        expected_revision=request.revision,
        files=[inspect_bytes("scan.png", buffer.getvalue())],
    )
    return entry.attachments.get()


def _desk_records():
    """Records the request desk's dialogs open on, as demo_admin (admin and supervisor) sees
    them: a request, a complaint it may record (as a supervisor), one it may review, a reply to
    withdraw, its own saved view and a team. Created through the services."""
    from accounts.models import User
    from desk import saved_views, services
    from desk.models import ComplaintDecision, EntryKind, ServiceRequest
    from taxonomy.models import RequestCategory
    from teams.models import Team

    admin = User.objects.get(username="demo_admin")
    category = RequestCategory.objects.filter(parent__isnull=False, is_restricted=False).first()
    made = services.create_request(
        actor=admin,
        category=category,
        subject="E2E dialog request",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Synthetic.",
        channel="phone",
    )
    complaint = services.create_request(
        actor=admin,
        category=category,
        subject="E2E dialog complaint",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Synthetic.",
        channel="phone",
    )
    services.set_complaint(
        actor=admin, request=complaint, expected_revision=1, is_complaint=True, reason="E2E"
    )
    decided = ServiceRequest.objects.filter(
        complaint_review__decision=ComplaintDecision.INVESTIGATE
    )
    review_target = decided.exclude(complaint_review__decided_by=admin).first() or complaint
    reply = made.entries.get(kind=EntryKind.REPLY)
    view = saved_views.create_view(
        actor=admin, name="E2E dialog view", shared_with="private", params={"view": "all"}
    )
    return made, complaint, review_target, reply, view, Team.objects.get(is_default=True)


def nonconformance_surfaces():
    """Form surfaces of the optional nonconformance module (on in the demo), each on a record whose
    state offers that form to the administrator (a supervisor who owns none of them)."""
    from accounts.models import User
    from core.modules import MODULES
    from desk.services import create_request
    from nonconformance import services
    from nonconformance.models import ActionStatus, CorrectiveAction, Nonconformance, Status
    from taxonomy.models import RequestCategory

    admin = User.objects.get(username="demo_admin")
    agent = User.objects.get(username="demo_client_services")
    unowned = Nonconformance.objects.get(status=Status.OPEN, owner__isnull=True)
    closed = Nonconformance.objects.filter(status=Status.CLOSED).first()
    action = CorrectiveAction.objects.filter(status=ActionStatus.OPEN).first()
    ticket = create_request(
        actor=agent,
        category=RequestCategory.objects.get(key="complaint"),
        subject="E2E surface complaint",
        first_entry_kind="note",
        first_entry_body="Synthetic.",
        channel="phone",
    )
    link = services.link_ticket(
        actor=admin, nc=unowned, expected_revision=unowned.revision, request=ticket
    )
    ready = services.raise_nonconformance(
        actor=admin,
        owner=agent,
        title="E2E surface record",
        description="Synthetic.",
        source="internal",
        severity="minor",
    )
    ready = services.change_status(
        actor=agent, nc=ready, expected_revision=ready.revision, status=Status.VERIFICATION
    )
    ready = services.record_impact(
        actor=agent,
        nc=ready,
        expected_revision=ready.revision,
        impact="not_applicable",
        impact_assessment="Synthetic.",
    )
    ready = services.record_effectiveness(
        actor=agent,
        nc=ready,
        expected_revision=ready.revision,
        result="no_action_needed",
        note="Synthetic.",
    )
    assert closed is not None and action is not None
    record = unowned.pk
    on = {
        "edit": record,
        "owner": record,
        "status": record,
        "investigation": record,
        "impact": record,
        "action-add": record,
        "ticket-link": record,
        "effectiveness": ready.pk,
        "close": ready.pk,
        "reopen": closed.pk,
    }
    surfaces = {
        f"nonconformance:{name}": reverse(f"nonconformance:{name}", args=[pk])
        for name, pk in on.items()
    }
    for name in ("action-edit", "action-done", "action-cancel"):
        surfaces[f"nonconformance:{name}"] = reverse(
            f"nonconformance:{name}", args=[action.nonconformance_id, action.pk]
        )
    return {
        **surfaces,
        "nonconformance:create": reverse("nonconformance:create"),
        "nonconformance:from-ticket": reverse("nonconformance:from-ticket", args=[ticket.pk]),
        "nonconformance:ticket-unlink": reverse(
            "nonconformance:ticket-unlink", args=[record, link.pk]
        ),
        "lab-module": reverse("lab-module", args=[next(iter(MODULES))]),
    }


def setup_and_link_surfaces(client):
    """Form surfaces of setup and links, on records the demo seed made."""
    from accounts.models import User
    from desk.services import create_request
    from links.models import LinkAttachment
    from service_levels.models import BusinessCalendar, ServiceTarget
    from taxonomy.models import RequestCategory

    calendar = BusinessCalendar.objects.first()
    attachment = LinkAttachment.objects.filter(client=client).first()
    request = create_request(
        actor=User.objects.get(username="demo_client_services"),
        category=RequestCategory.objects.get(key="missing-result"),
        subject="E2E surface request",
        first_entry_kind="note",
        first_entry_body="Synthetic.",
        channel="email",
    )
    return {
        "taxonomy:category-create": reverse("taxonomy:category-create"),
        "taxonomy:group-create": reverse("taxonomy:group-create"),
        "taxonomy:category-edit": reverse(
            "taxonomy:category-edit", args=[RequestCategory.objects.get(key="pickup").pk]
        ),
        "service_levels:target-create": reverse("service_levels:target-create"),
        "service_levels:target-edit": reverse(
            "service_levels:target-edit", args=[ServiceTarget.objects.first().pk]
        ),
        "service_levels:calendar-create": reverse("service_levels:calendar-create"),
        "service_levels:calendar-edit": reverse("service_levels:calendar-edit", args=[calendar.pk]),
        "service_levels:holiday-create": reverse(
            "service_levels:holiday-create", args=[calendar.pk]
        ),
        "service_levels:holiday-remove": reverse(
            "service_levels:holiday-remove", args=[calendar.holidays.first().pk]
        ),
        "links:system-create": reverse("links:system-create"),
        "links:system-edit": reverse("links:system-edit", args=[attachment.link.system_id]),
        "link_records:request-link-add": reverse(
            "link_records:request-link-add", args=[request.pk]
        ),
        "link_records:client-link-add": reverse("link_records:client-link-add", args=[client.pk]),
        "link_records:attachment-remove": reverse(
            "link_records:attachment-remove", args=[attachment.pk]
        ),
        "link_records:link-edit": reverse("link_records:link-edit", args=[attachment.link_id]),
        "attachments:withdraw": reverse("attachments:withdraw", args=[file_on(request).pk]),
    }


def audit_review_surfaces():
    """Form surfaces of the audit review records, on the review the demonstration seed signed."""
    from audit_review.models import Review

    review = Review.objects.latest("id")
    return {
        "audit_review:review-create": reverse("audit_review:review-create"),
        "audit_review:review-note": reverse("audit_review:review-note", args=[review.pk]),
        "audit_review:review-interval": reverse("audit_review:review-interval"),
    }


def test_every_form_surface_opens_as_a_dialog_and_cancel_leaves_it(page: Page, sign_in_as, demo):
    from accounts.models import User
    from clients.models import Client, ClientContact
    from core.announcements import save_announcement
    from core.models import Announcement, LabCertificate

    sign_in_as("admin")
    client = Client.objects.get(name="Example Family Clinic")
    contact = ClientContact.objects.filter(client=client).first()
    announcement, _changed = save_announcement(
        actor=User.objects.get(username="demo_admin"),
        instance=Announcement(title="E2E surface notice", body="Synthetic.", show_to_staff=True),
    )
    certificate = LabCertificate.objects.first()
    desk_request, complaint, review_target, reply, saved_view, team = _desk_records()
    site = client.sites.first()
    portal_account = User.objects.get(username="demo_client")
    paths = {
        "clients:client-create": reverse("clients:client-create"),
        "clients:client-edit": reverse("clients:client-edit", args=[client.pk]),
        "clients:contact-create": reverse("clients:contact-create", args=[client.pk]),
        "clients:contact-edit": reverse("clients:contact-edit", args=[client.pk, contact.pk]),
        "clients:site-create": reverse("clients:site-create", args=[client.pk]),
        "clients:site-edit": reverse("clients:site-edit", args=[client.pk, site.pk]),
        "clients:portal-access-edit": reverse(
            "clients:portal-access-edit", args=[client.pk, portal_account.pk]
        ),
        "announcement-create": reverse("announcement-create"),
        "announcement-edit": reverse("announcement-edit", args=[announcement.pk]),
        "lab-profile-edit": reverse("lab-profile-edit"),
        "lab-logo-edit": reverse("lab-logo-edit"),
        "lab-certificate-create": reverse("lab-certificate-create"),
        "lab-certificate-edit": reverse("lab-certificate-edit", args=[certificate.pk]),
        "jobs:settings-edit": reverse("jobs:settings-edit"),
        "lab-contact-edit": reverse("lab-contact-edit", args=["privacy"]),
        "session-limits-edit": reverse("session-limits-edit", args=["portal"]),
        "second-factor-edit": reverse("second-factor-edit"),
        "reveal-confirmation-edit": reverse("reveal-confirmation-edit"),
        # Request desk
        "desk:request-create": reverse("desk:request-create"),
        "desk:request-assign": reverse("desk:request-assign", args=[desk_request.pk]),
        "desk:request-status": reverse("desk:request-status", args=[desk_request.pk]),
        "desk:request-properties": reverse("desk:request-properties", args=[desk_request.pk]),
        "desk:complaint-mark": reverse("desk:complaint-mark", args=[desk_request.pk]),
        "desk:complaint-decision": reverse("desk:complaint-decision", args=[complaint.pk]),
        "desk:complaint-record": reverse("desk:complaint-record", args=[complaint.pk]),
        "desk:complaint-review": reverse("desk:complaint-review", args=[review_target.pk]),
        "desk:entry-withdraw": reverse("desk:entry-withdraw", args=[reply.request_id, reply.pk]),
        "desk:view-create": reverse("desk:view-create"),
        "desk:view-edit": reverse("desk:view-edit", args=[saved_view.pk]),
        "teams:team-create": reverse("teams:team-create"),
        "teams:team-edit": reverse("teams:team-edit", args=[team.pk]),
        **setup_and_link_surfaces(client),
        **nonconformance_surfaces(),
        **audit_review_surfaces(),
    }
    assert set(paths) == set(FORM_SURFACES), "A form route was added or removed: update this test."
    dialog = page.locator("#vr-form-dialog")

    def open_and_cancel(surfaces):
        for name, path in surfaces.items():
            page.goto(path)
            expect(dialog, name).to_be_visible()
            expect(dialog.get_by_role("heading", level=1), name).to_have_count(1)
            dialog.get_by_role("link", name="Cancel", exact=True).click()
            expect(dialog, name).to_be_hidden()
            expect(page, name).not_to_have_url(re.compile(re.escape(path) + r"$"))

    reviewer_only = {name: path for name, path in paths.items() if name.startswith("audit_review:")}
    open_and_cancel({name: path for name, path in paths.items() if name not in reviewer_only})
    # The review forms need the capability "Can review access and audit records", which the
    # demonstration gives its supervisor.
    page.context.clear_cookies()
    sign_in(page, "demo_supervisor", demo.password)
    open_and_cancel(reviewer_only)
    assert page.console_errors == []
