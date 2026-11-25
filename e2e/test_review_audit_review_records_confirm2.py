"""The entire identity in the review signing statement stays readable on a phone."""

import pytest
from helpers import field, sign_in
from playwright.sync_api import expect

from accounts.models import User
from desk.story import REVIEW_HOLDER

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["full-name", "username"])
def signer(request, demo):
    if request.param == "full-name":
        username = "example-signer"
        names = {"first_name": "A" * 150, "last_name": "B" * 150}
    else:
        username = "u" * 150
        names = {}
    user = User.objects.create_user(username, "signer@example.invalid", demo.password, **names)
    holder = User.objects.get(username=REVIEW_HOLDER)
    user.groups.set(holder.groups.all())
    user.user_permissions.set(holder.user_permissions.all())
    return user


def check_name_lines(surface, name):
    paragraph = surface.get_by_role("group", name="Sign-off").locator("p")
    expect(paragraph).to_contain_text(name)
    # Measure the actual glyphs in the document, independent of the template's choice of wrapper.
    lines = paragraph.evaluate(
        """(paragraph, name) => {
        const nodes = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        while (nodes.nextNode()) {
            const node = nodes.currentNode;
            const start = node.data.indexOf(name);
            if (start < 0) continue;
            const range = document.createRange();
            range.setStart(node, start);
            range.setEnd(node, start + name.length);
            const bounds = paragraph.getBoundingClientRect();
            return Array.from(range.getClientRects(), r => [
                r.left - bounds.left, bounds.right - r.right,
            ]);
        }
        return [];
    }""",
        name,
    )
    assert len(lines) > 1
    assert all(left >= -1 and right >= -1 for left, right in lines), lines


@pytest.mark.parametrize("standalone", [False, True], ids=["dialog", "standalone"])
def test_signing_statement_fits_each_phone_theme_before_and_after_validation(
    new_context, signer, demo, standalone
):
    page = new_context(is_mobile=True, has_touch=True).new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    if standalone:
        # Keep the server-rendered form on its page with theme selection available. A browser
        # with all scripts off always gets the document's default light theme.
        page.route("**/js/form-dialog.js", lambda route: route.abort())
    sign_in(page, signer.username, demo.password)
    name = signer.get_full_name() or signer.username
    for theme in ("light", "dark"):
        page.emulate_media(color_scheme=theme)
        page.goto("/audit-review/reviews/new/")
        expect(page.locator("html")).to_have_attribute("data-bs-theme", theme)
        surface = page.get_by_role("main") if standalone else page.get_by_role("dialog")
        check_name_lines(surface, name)
        surface.get_by_role("checkbox").first.check()
        surface.get_by_label("Nothing that needs follow-up").check()
        field(surface, "Findings").fill("Synthetic review.")
        surface.get_by_role("button", name="Sign review", exact=True).click()
        expect(field(surface, "Your password")).to_have_attribute("aria-invalid", "true")
        expect(field(surface, "Findings")).to_have_value("Synthetic review.")
        check_name_lines(surface, name)
