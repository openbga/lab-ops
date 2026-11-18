"""Small shared helpers for the browser suite. Fixture data goes through the domain services."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Group
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

LAB_TZ = ZoneInfo("America/New_York")


def local_input(delta: timedelta) -> str:
    """Value for a datetime-local input, in laboratory local time."""
    return (datetime.now(LAB_TZ) + delta).strftime("%Y-%m-%dT%H:%M")


def field(scope, name: str):
    """A form control by exact label text; the required-field marker may follow it. An enhanced
    select or suggestion input also labels its menu (listbox): the control comes first. Controls
    behind an open dialog are inert and never match."""
    page = scope if isinstance(scope, Page) else scope.page
    labelled = scope.get_by_label(re.compile(rf"^{re.escape(name)}\s*\*?$"))
    # form-dialog.js renames the ids of the inert page behind a dialog to "vr-background-…".
    return labelled.and_(page.locator(":not([id^='vr-background-'])")).first


def native_select(control):
    """The element holding the value a form submits for a labelled control: the <select> behind
    a Tom Select, the hidden server-format input of a date field (static/js/controls.js), or the
    control itself. It first waits until controls.js has enhanced the field, because a labelled
    locator finds the native element before that and the enhanced control after it: without the
    wait the answer depends on how fast the page is (an HTMX error re-render, a slow CI runner).
    The element is resolved now and marked, so a later locator for the label cannot drift."""
    handle = control.first.element_handle()
    control.page.wait_for_function(
        """el => {
            if (!el.isConnected || el.closest('.ts-wrapper')) return true;
            const enhanced = el.classList.contains('tomselected');
            if (el.matches('select:not([data-vr-native])')) return enhanced;
            if (el.matches('input[data-vr-date]')) return Boolean(el.vrDateField);
            if (el.matches('input[data-vr-suggest]')) return enhanced;
            return true;
        }""",
        arg=handle,
    )
    mark = handle.evaluate(
        """el => {
            const wrapper = el.closest('.ts-wrapper');
            const target = wrapper ? wrapper.previousElementSibling
                : el.vrDateField ? el.vrDateField.hidden : el;
            target.dataset.e2eNative = target.dataset.e2eNative || String(Math.random()).slice(2);
            return target.dataset.e2eNative;
        }"""
    )
    return control.page.locator(f"[data-e2e-native='{mark}']")


def pick(control, value=None, *, label=None, index=None) -> None:
    """Choose options the way a user does: open the select's menu, type into its search box when
    it has one, and click the option. `control` is the labelled combobox (field(), get_by_label)
    or the <select>; options are given as for select_option: value(s), label or index.
    A form that re-renders meanwhile (a dependent field refreshing it) is found again once."""
    try:
        _pick(control, value, label, index, timeout=4_000)
    except PlaywrightError:
        _pick(control, value, label, index, timeout=None)


def _pick(control, value, label, index, timeout) -> None:
    select = native_select(control)
    page = select.page
    wrapper = select.locator("xpath=following-sibling::div[contains(@class, 'ts-wrapper')][1]")
    try:  # static/js/controls.js enhances every select once its form is ready
        expect(select).to_have_class(re.compile(r"\btomselected\b"))
    except AssertionError:  # not enhanced: the browser's own select
        select.select_option(value, label=label, index=index)
        return
    combobox = wrapper.locator("[role=combobox]").first
    combobox.wait_for(timeout=timeout)
    if label is not None:
        labels = [label] if isinstance(label, str) else list(label)
    else:
        if index is not None:
            options = [select.locator("option").nth(index)]
        else:
            values = [value] if isinstance(value, str) else list(value)
            options = [select.locator(f"option[value='{v}']") for v in values]
        labels = [" ".join(option.text_content().split()) for option in options]
    menu = wrapper.locator(".ts-dropdown-content")
    search = wrapper.locator("input.dropdown-input")
    for text in labels:
        if not menu.is_visible():
            combobox.click(timeout=timeout)
        words = text.split()[:1]  # the first word narrows the menu; the click picks the option
        if search.count():
            search.fill(" ".join(words))
        elif combobox.evaluate("el => el.tagName === 'INPUT'"):
            combobox.fill(" ".join(words))  # multi-select: its input filters the menu
        menu.get_by_role("option", name=text, exact=True).click(timeout=timeout)
    # Close a menu that stays open (multi-select) only while Tom Select still has it open, so the
    # Escape never reaches the dialog around it.
    if wrapper.evaluate("w => w.classList.contains('dropdown-active')"):
        page.keyboard.press("Escape")


def choose(select, value: str) -> None:
    """Enter `value` in a list-or-other control: a listed option, else "Other" with the text."""
    select = native_select(select)
    if value in select.locator("option").all_text_contents():
        pick(select, label=value)
    else:
        pick(select, value="__other__")
        select.page.locator(f"#{select.get_attribute('id')}_other").fill(value)


def expect_chosen(select, value: str) -> None:
    """The list-or-other control holds `value` ("" for nothing chosen)."""
    select = native_select(select)
    if not value or value in select.locator("option").all_text_contents():
        expect(select).to_have_value(value)
    else:
        expect(select).to_have_value("__other__")
        other = select.page.locator(f"#{select.get_attribute('id')}_other")
        expect(other).to_be_visible()
        expect(other).to_have_value(value)


def described_text(page: Page, field_id: str) -> str:
    """Text of the elements an invalid field points at through aria-describedby."""
    control = page.locator(f"#{field_id}")
    expect(control).to_have_attribute("aria-invalid", "true")
    return control.evaluate(
        "el => el.getAttribute('aria-describedby').split(' ')"
        ".map(id => document.getElementById(id).innerText).join(' ')"
    )


def sign_in(page: Page, username: str, password: str, *, visit: bool = True) -> None:
    """Fill the sign-in form; ``visit=False`` keeps the current page (and its ``next``)."""
    if visit:
        page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)"), exact=False).fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def sign_out(page: Page, who: str = r"^(Demo |E2E )") -> None:
    """Sign out through the user menu; ``who`` matches the menu button (the user's display name)."""
    page.get_by_role("button", name=re.compile(who)).click()
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()


def make_user(username: str, password: str, *roles: str, client=None):
    from accounts.models import User

    user = User.objects.create_user(
        username, f"{username}@example.invalid", password, client=client
    )
    user.first_name = "E2E"
    user.last_name = username
    user.save(update_fields=["first_name", "last_name"])
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def make_clients(count: int, *, prefix: str = "Synthetic Organization") -> int:
    """Add ``count`` client organizations through the domain service; returns how many exist.

    Names sort by number ("Synthetic Organization 001" ...), so the order is predictable.
    """
    from accounts.models import User
    from clients.models import Client
    from clients.services import create_client

    actor = User.objects.get(username="demo_admin")
    for number in range(1, count + 1):
        create_client(
            actor=actor,
            name=f"{prefix} {number:03d}",
            account_number=f"SYN{number:03d}",
            city="Exampleville",
            state="NY",
        )
    return Client.objects.count()


def column_values(page: Page, header: str) -> list[str]:
    """Visible text of one table column, located by its header text."""
    return page.locator("table.vr-table").first.evaluate(
        """(table, header) => {
            const heads = [...table.tHead.rows[0].cells];
            // textContent: innerText would follow the headers' small-capitals label style.
            const label = th => th.textContent.replace(/\\s+/g, " ").trim();
            const index = heads.findIndex(th => label(th) === header);
            if (index < 0) return null;
            return [...table.tBodies[0].rows].map(row => row.cells[index].innerText.trim());
        }""",
        header,
    )


def column_headers(page: Page) -> list[str]:
    return page.locator("table.vr-table").first.evaluate(
        "table => [...table.tHead.rows[0].cells]"
        ".map(th => th.textContent.replace(/\\s+/g, ' ').trim())"
    )


def contrast_ratio(foreground, background) -> float:
    """WCAG 2.x contrast ratio between two sRGB triples (0-255)."""

    def luminance(rgb):
        def channel(value):
            value /= 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(v) for v in rgb[:3])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


THEME_SAMPLES_JS = (Path(__file__).parent / "theme_samples.js").read_text()


def theme_samples(page: Page) -> dict:
    """Foreground/background pairs of key elements as rendered now, with the minimum ratio."""
    return page.evaluate(THEME_SAMPLES_JS)


def contrast_failures(samples: dict) -> list[str]:
    """Human-readable list of sampled pairs below their WCAG minimum (empty when all pass)."""
    failures = []
    for label, pair in samples["pairs"].items():
        ratios = [contrast_ratio(fg, bg) for fg, bg in pair["candidates"]]
        if max(ratios) < pair["minimum"]:
            failures.append(
                f"{samples['scheme']}: {label} {max(ratios):.2f}:1 < {pair['minimum']}:1"
            )
    return failures


def grid_search(page: Page, label: str):
    """The search box of a list's own filter toolbar (not the finder in the top bar)."""
    return page.get_by_role("search", name=label).get_by_role("searchbox")


def menu_item(page: Page, menu: str, item: str):
    """An item of a header dropdown ({% more_actions %} "More", or a named menu such as "Import
    results"): opens the menu and returns the item, ready to click."""
    page.get_by_role("button", name=menu, exact=True).click()
    return page.locator(".dropdown-menu.show").get_by_role("link", name=item, exact=True)
