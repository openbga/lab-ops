"""Phone use (docs/ui-standards.md section 13) on every frame page: at the narrowest and widest
phone widths there is no horizontal page scrolling and no control under 44px, except links
inside running text (WCAG 2.5.8). Add a page here when you add one to the frame."""

import re

import pytest
from conftest import Lab, View
from helpers import contrast_ratio, grid_search, sign_in
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PHONES = [{"width": 360, "height": 780}, {"width": 430, "height": 900}]
ANONYMOUS = ["/accounts/login/", "/accounts/password/reset/"]
STAFF = [
    "/",
    "/clients/",
    "/clients/?active=yes&contacts=none",
    "/clients/{clinic}/",
    "/clients/new/",
    "/clients/{clinic}/edit/",
    "/clients/{clinic}/contacts/new/",
    "/lab/profile/",
    "/lab/profile/edit/",
    "/lab/profile/logo/",
    "/lab/profile/certificates/new/",
    "/lab/contacts/privacy/",
    "/lab/security/",
    "/lab/announcements/",
    "/lab/announcements/new/",
    "/lab/record-retention/",
    "/lab/scheduled-jobs/",
    "/lab/scheduled-jobs/settings/",
    "/whats-new/",
    "/accounts/password/change/",
    "/accounts/email/",
    "/accounts/2fa/",
]

# Controls a finger must hit. A link is exempt only when it sits in running text.
PROBE = """() => {
  const found = [];
  const inText = el => el.tagName === 'A' && getComputedStyle(el).display === 'inline'
    && !!el.closest('p, td, dd, .vr-facts, .form-text, .vr-form-note, label, .alert, '
      + '.breadcrumb, .vr-change-summary');
  const controls = 'a[href], button, '
    + 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), '
    + 'select:not(.tomselected), textarea, summary, .ts-wrapper, .form-check-label';
  for (const el of document.querySelectorAll(controls)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') continue;
    if (el.closest('[hidden], .visually-hidden, .visually-hidden-focusable, .d-none')) continue;
    if (el.matches('input.tomselected, .ts-control input') || inText(el)) continue;
    if (r.height < 43.5) {
      const name = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
      const tag = `${el.tagName.toLowerCase()}.${el.className}`;
      found.push(`${tag} "${name.slice(0, 40)}" ${Math.round(r.height)}px`);
    }
  }
  return {overflow: document.documentElement.scrollWidth - window.innerWidth, small: found};
}"""


def check(page: Page, where: str, problems: list):
    result = page.evaluate(PROBE)
    if result["overflow"] > 0:
        problems.append(f"{where}: page scrolls sideways by {result['overflow']}px")
    problems.extend(f"{where}: {item}" for item in result["small"])


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_frame_pages_fit_a_phone_with_44px_targets(page: Page, demo: Lab, phone):
    from clients.models import Client

    clinic = Client.objects.get(account_number="EXCLIN").pk
    page.set_viewport_size(phone)
    problems = []
    for url in ANONYMOUS:
        page.goto(url)
        check(page, url, problems)

    sign_in(page, "demo_admin", demo.password)
    for url in STAFF:
        page.goto(url.format(clinic=clinic))
        check(page, url, problems)

    # Surfaces that open over a page: the navigation drawer, page help and a form dialog.
    page.goto("/clients/")
    page.get_by_role("navigation", name="Quick actions").get_by_role("button", name="Menu").click()
    page.locator("#vr-nav.show").wait_for()
    check(page, "navigation drawer", problems)
    page.keyboard.press("Escape")
    page.locator("#vr-nav.show").wait_for(state="detached")
    page.locator("[data-vr-documentation-toggle]").first.click()
    page.locator("#vr-documentation").wait_for()
    check(page, "page help", problems)
    page.get_by_role("button", name="Close documentation").click()
    page.goto(f"/clients/{clinic}/")
    page.get_by_role("link", name="Edit organization").click()
    page.get_by_role("dialog").get_by_role("button", name="Save changes").wait_for()
    check(page, "form dialog", problems)

    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    page.goto("/portal/")
    check(page, "/portal/", problems)

    assert not problems, "\n".join(problems)


# Every list page of the frame (one with a filter toolbar). Add a list here when you add one.
LISTS = [
    "/clients/",
    "/lab/announcements/",
    "/quality/nonconformances/",
    "/quality/actions/",
    "/audit-review/changes/",
    "/audit-review/access/",
    "/audit-review/sign-ins/",
    "/audit-review/people/",
    "/audit-review/uses/",
]
# Lists opened by another account than demo_admin: the audit review needs its capability, which
# the demonstration gives demo_supervisor. They have no search box, and only People sorts.
LIST_ACCOUNTS = {url: "demo_supervisor" for url in LISTS if url.startswith("/audit-review/")}
UNSORTED = set(LIST_ACCOUNTS) - {"/audit-review/people/"}
NO_SEARCH = set(LIST_ACCOUNTS)
THUMBBAR = 64  # the phone bottom bar, --vr-thumbbar-height
SHEET = "Filter and sort"


def filter_button(page: Page):
    return page.get_by_role("button", name=re.compile(rf"^{SHEET}"))


def open_filter_sheet(page: Page):
    filter_button(page).click()
    sheet = page.get_by_role("dialog", name=SHEET)
    expect(sheet).to_be_visible()
    expect(sheet).not_to_have_class(re.compile(r"\bshowing\b"))  # opened; focus has moved in
    expect(sheet).to_be_focused()
    return sheet


def post_announcement():
    """A row for the announcements list (the demo laboratory posts none)."""
    from django.utils import timezone

    from accounts.models import User
    from core.announcements import save_announcement
    from core.models import Announcement

    save_announcement(
        actor=User.objects.get(username="demo_admin"),
        instance=Announcement(
            title="Synthetic maintenance notice",
            body="The portal restarts on Sunday.",
            show_to_staff=True,
            starts_at=timezone.now(),
        ),
    )


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_list_rows_come_first_behind_one_filter_and_sort_button(page: Page, demo: Lab, phone):
    """Owner requirement: on a phone the rows come first. The toolbar and the sort row collapse
    behind one "Filter and sort" button that opens a bottom sheet holding the whole toolbar and
    the sort links."""
    post_announcement()
    page.set_viewport_size(phone)
    signed_in = None
    problems = []
    for url in LISTS:
        account = LIST_ACCOUNTS.get(url, "demo_admin")
        if account != signed_in:
            page.context.clear_cookies()
            sign_in(page, account, demo.password)
            signed_in = account
            if url in LIST_ACCOUNTS:  # a ticket opened, so that Access records has a row
                from desk.models import ServiceRequest

                page.goto(f"/requests/{ServiceRequest.objects.order_by('pk').first().pk}/")
        page.goto(url)
        row = page.locator("table.vr-table tbody tr").first
        expect(row).to_be_visible()
        # The first row starts in the first screen, above the thumb bar, with its title showing.
        top = row.bounding_box()["y"]
        if top > phone["height"] - THUMBBAR - 80:
            problems.append(f"{url}: first row starts at {top:.0f}px of {phone['height']}px")
        check(page, url, problems)

        button = filter_button(page)
        expect(button).to_be_visible()
        expect(button).to_have_attribute("aria-expanded", "false")
        assert button.bounding_box()["height"] >= 44, url
        # The toolbar, its search box and the table's own sort chips are out of the way.
        expect(grid_search(page, re.compile("^Filter"))).to_be_hidden()
        if url not in UNSORTED:  # the head holds the sort chips, which the sheet copies
            expect(page.locator("table.vr-table > thead")).to_be_hidden()
        summary = page.locator(".vr-filter-summary")
        expect(summary).to_contain_text("No filters")

        sheet = open_filter_sheet(page)
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(sheet.get_by_role("button", name="Apply")).to_be_visible()
        box = sheet.bounding_box()
        assert box["y"] >= phone["height"] * 0.1, f"{url}: sheet taller than 85% of the screen"
        assert box["y"] + box["height"] >= phone["height"] - 1, f"{url}: sheet not at the bottom"
        check(page, f"{url} filter sheet", problems)
        if url in UNSORTED:
            expect(sheet.get_by_role("heading", name="Sort by")).to_be_hidden()
            sheet.get_by_role("button", name=f"Close {SHEET}").click()
            expect(sheet).to_be_hidden()
            continue
        if url not in NO_SEARCH:
            expect(sheet.get_by_role("searchbox")).to_be_visible()
        expect(sheet.get_by_role("heading", name="Sort by")).to_be_visible()

        # Choosing a sort reloads the list sorted, and the summary says so.
        chips = sheet.locator(".vr-sort-chips a")
        assert chips.count() >= 2, url
        chip = sheet.locator(".vr-sort-chips a:not([aria-current])").first
        label = chip.inner_text().strip()
        chip.click()
        expect(page).to_have_url(re.compile(r"[?&]sort="))
        expect(page.locator(".vr-filter-summary")).to_contain_text(f"Sorted by {label}")
        expect(page.locator(".vr-filter-summary")).to_contain_text("No filters")
        sheet = open_filter_sheet(page)
        expect(sheet.locator(".vr-sort-chips a[aria-current]")).to_have_text(re.compile(label))

        # Close returns focus to the button, by the Close button and by Escape.
        sheet.get_by_role("button", name=f"Close {SHEET}").click()
        expect(sheet).to_be_hidden()
        expect(filter_button(page)).to_be_focused()
        expect(filter_button(page)).to_have_attribute("aria-expanded", "false")
        open_filter_sheet(page)
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name=SHEET)).to_be_hidden()
        expect(filter_button(page)).to_be_focused()
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("phone", PHONES, ids=lambda size: f"{size['width']}px")
def test_filter_summary_and_count_follow_the_active_filters(page: Page, demo: Lab, phone):
    page.set_viewport_size(phone)
    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/?active=yes&contacts=none")
    button = filter_button(page)
    expect(button).to_contain_text("2 active")
    summary = page.locator(".vr-filter-summary")
    expect(summary).to_contain_text("Status: Active")
    # The chips row gives way to the summary; the sheet keeps Clear all.
    expect(page.locator(".vr-chips")).to_be_hidden()
    sheet = open_filter_sheet(page)
    sheet.get_by_role("link", name="Clear all").click()
    expect(page).to_have_url(re.compile(r"/clients/$"))
    expect(page.locator(".vr-filter-summary")).to_contain_text("No filters")
    # One line, cut with an ellipsis rather than wrapping.
    height = page.locator(".vr-filter-summary").bounding_box()["height"]
    assert height < 30, height


def colour(page: Page, selector: str, prop: str) -> tuple:
    value = page.locator(selector).first.evaluate("(el, prop) => getComputedStyle(el)[prop]", prop)
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def test_filter_sheet_in_every_view(page: Page, demo: Lab, view: View):
    """The journey in {desktop 1366, phone 390} x {light, dark}: on a desktop the toolbar is
    inline as ever and no sheet is offered; on a phone the sheet opens and stays legible."""
    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/")
    search = grid_search(page, re.compile("^Filter"))
    if not view.phone:
        expect(search).to_be_visible()
        expect(filter_button(page)).to_be_hidden()
        expect(page.locator(".vr-grid-sheet .vr-sheet-header")).to_be_hidden()
        expect(page.locator("table.vr-table > thead")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= view.viewport["width"]
        return
    sheet = open_filter_sheet(page)
    expect(sheet.get_by_role("searchbox")).to_be_visible()
    sheet_bg = colour(page, ".vr-grid-sheet", "backgroundColor")
    for selector, background in (
        (".vr-sheet-title", sheet_bg),
        (".vr-sheet-section-title", sheet_bg),
        (
            ".vr-sort-chips a:not([aria-current])",
            colour(page, ".vr-sort-chips a:not([aria-current])", "backgroundColor"),
        ),
        (
            ".vr-sort-chips a[aria-current]",
            colour(page, ".vr-sort-chips a[aria-current]", "backgroundColor"),
        ),
    ):
        ratio = contrast_ratio(colour(page, selector, "color"), background)
        assert ratio >= 4.5, f"{view.theme}: {selector} {ratio:.2f}:1"
    sheet.get_by_role("button", name=f"Close {SHEET}").click()
    page.goto("/clients/?active=yes&contacts=none")
    summary = contrast_ratio(
        colour(page, ".vr-filter-summary", "color"),
        colour(page, ".vr-table-card", "backgroundColor"),
    )
    assert summary >= 4.5, f"{view.theme}: filter summary {summary:.2f}:1"
    badge = contrast_ratio(
        colour(page, ".vr-filter-count", "color"),
        colour(page, ".vr-filter-count", "backgroundColor"),
    )
    assert badge >= 4.5, f"{view.theme}: count badge {badge:.2f}:1"
