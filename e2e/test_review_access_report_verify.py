"""Both Access review tables retain distinct group names and legible dates at each viewport."""

import re

import pytest
from django.contrib.auth.models import Group, Permission
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from accounts.roles import REVIEW_CAPABILITY

pytestmark = pytest.mark.django_db(transaction=True)


def test_groups_and_timestamp_parts_remain_readable_on_both_tabs(page, view, demo):
    member = User.objects.get(username="former_technician")
    capability = Permission.objects.get(
        content_type__app_label="accounts", codename=REVIEW_CAPABILITY
    )
    names = ["QA North", "qa north"]
    for name in names:
        group = Group.objects.create(name=name)
        group.permissions.add(capability)
        member.groups.add(group)
    sign_in(page, "demo_supervisor", demo.password)
    for path, column in [("accounts", "Capabilities"), ("unused", "Roles and capabilities")]:
        page.goto(f"/access-review/{path}/")
        expect(page.locator("html")).to_have_attribute("data-bs-theme", view.theme)
        row = page.locator("table tbody tr").filter(has_text=member.username)
        cell = row.locator(f'td[data-vr-label="{column}"]')
        expect(cell).to_be_visible()
        boxes = []
        for name in names:
            description = cell.get_by_text(re.compile(re.escape(f"“{name}”")))
            expect(description).to_be_visible()
            boxes.append(description.bounding_box())
        top, bottom = sorted(boxes, key=lambda box: box["y"])
        assert bottom["y"] >= top["y"] + top["height"] - 1
        assert cell.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        stamp = row.locator('td[data-vr-label="Last sign-in"] time')
        expect(stamp).to_have_text(re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}$"))
        # Count text lines for each date/time token, independently of the markup around them.
        lines = stamp.evaluate("""element => {
            const tokens = [];
            const walk = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
            while (walk.nextNode()) {
                for (const match of walk.currentNode.textContent.matchAll(/\\S+/g)) {
                    const range = document.createRange();
                    range.setStart(walk.currentNode, match.index);
                    range.setEnd(walk.currentNode, match.index + match[0].length);
                    tokens.push(range.getClientRects().length);
                }
            }
            return tokens;
        }""")
        assert lines == [1, 1]
    assert page.console_errors == []
