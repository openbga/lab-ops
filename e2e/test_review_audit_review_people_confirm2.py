"""Check the independent hidden-column limit on a real organization grid."""

import re

import pytest
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import UserPreference
from core.preferences import clean

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("width", "count", "repeat"),
    [(360, 59, False), (1366, 60, False), (390, 60, True)],
    ids=["one-free-slot", "full", "duplicate-frees-a-slot"],
)
def test_hidden_only_column_ids_use_the_hidden_lists_own_capacity(page, demo, width, count, repeat):
    page.set_viewport_size({"width": width, "height": 850})
    user = User.objects.get(username="demo_supervisor")
    key = "grid:clients.clients:v2"
    # The two saved lists can be full even when their sets of retired IDs are disjoint.
    order_only = [f"old_position_{i}" for i in range(59)]
    hidden_only = [f"old_hidden_{i}" for i in range(count)]
    if repeat:
        hidden_only[1] = hidden_only[0]
    previous = {"order": ["name", *order_only], "hidden": hidden_only}
    assert clean(key, previous) == previous
    pref = UserPreference.objects.create(user=user, key=key, value=previous)
    sign_in(page, user.username, demo.password)
    page.goto("/clients/")
    table = page.locator('table[data-vr-grid="clients.clients"]')
    expect(table.locator("tbody tr").first).to_be_visible()
    if width < 576:
        page.get_by_role("button", name=re.compile("^Filter and sort")).click()
    page.get_by_role("button", name="Columns", exact=True).click()
    dialog = page.get_by_role("dialog", name="Columns")
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/preferences/")
    ) as posted:
        dialog.get_by_role("checkbox", name="City", exact=True).uncheck()
    assert posted.value.status == 204
    saved = posted.value.request.post_data_json["value"]
    pref.refresh_from_db()
    assert saved == pref.value
    # The current edit gets a place, then previous distinct hidden IDs fit in the remaining 59.
    assert saved["hidden"] == ["city", *list(dict.fromkeys(hidden_only))[:59]]
    assert len(saved["order"]) == len(set(saved["order"])) == 60
    assert not set(hidden_only).intersection(saved["order"])
    page.reload()
    expect(table.locator("tbody tr").first).to_be_visible()
    expect(table.locator('th[data-vr-col="city"]')).to_have_count(0)
