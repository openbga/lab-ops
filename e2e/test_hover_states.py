"""One hover cue per element (docs/ui-standards.md section 8, Interaction states).

The pointer hovers one of every kind of link, button, disclosure, checkbox and radio on the main
staff and portal pages. Whatever answers on a surface (its own fill, or the fill of the box it
covers) never also takes an underline, and everything that can be clicked answers somehow. A second
test names the kinds: a text link underlines, a link styled as a button and a tile do not, the
quiet button takes a fill, and a checkbox answers from its label.
"""

import pytest
from helpers import sign_in
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db(transaction=True)

# One sample of every kind on the page: the element's classes and the component it sits in. The
# sample's own transition is switched off (through its style object, which the content security
# policy allows) so the hover look is there at once.
COLLECT = """
() => {
  const CONTEXTS = ['.dropdown-menu', '.vr-topbar', '.vr-sidenav', '.vr-portal-nav', '.breadcrumb',
    '.pagination', '.vr-chips', '.vr-page-toc', '.vr-table thead', '.vr-table tbody',
    '.card-header', '.vr-form-actions', '.vr-facts', '.alert', '.modal', 'footer', 'p', 'main'];
  const STATE = /^(active|show|collapsed|disabled)$/;
  const found = [];
  let n = 0;
  for (const el of document.querySelectorAll('a[href], button, summary, input.form-check-input')) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!box.width || !box.height || el.disabled || el.closest('[inert]')) continue;
    if (style.visibility === 'hidden' || style.pointerEvents === 'none') continue;
    const classes = [...el.classList].filter(name => !STATE.test(name)).sort().join('.');
    const context = CONTEXTS.find(selector => el.parentElement.closest(selector)) || '';
    const current = el.matches('[aria-current], .active') ? ' (current)' : '';
    const checked = el.matches(':checked') ? ' (checked)' : '';
    el.dataset.hoverProbe = String(n++);
    for (let node = el, i = 0; node && i < 3; node = node.parentElement, i++) {
      node.style.transition = 'none';
    }
    const tag = el.tagName.toLowerCase() + (classes ? '.' + classes : '');
    found.push({
      id: el.dataset.hoverProbe,
      kind: tag + current + checked + ' in ' + context,
      label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40),
      exempt: el.matches('.vr-brand, .vr-entrance-brand, [aria-current], .active, :focus'),
    });
  }
  return found;
}
"""

# What the eye sees of an element: its own look, the colour of what it holds, and the fill of the
# boxes around it. A table row's fill is a reading aid, not the element's surface (section 8).
LOOK = """
id => {
  const el = document.querySelector('[data-hover-probe="' + id + '"]');
  if (!el) return null;
  const css = node => getComputedStyle(node);
  const own = css(el);
  const box = el.getBoundingClientRect();
  const around = [], row = [];
  for (let node = el.parentElement, i = 0; node && i < 3; node = node.parentElement, i++) {
    const fill = css(node).backgroundColor + css(node).borderTopColor;
    (node.matches('tr, th, td') ? row : around).push(fill);
  }
  const point = [box.left + box.width / 2, box.top + box.height / 2];
  const top = document.elementFromPoint(point[0], point[1]);
  return {
    point,
    reachable: !!top && (top === el || el.contains(top) || top.contains(el)),
    underlined: own.textDecorationLine.includes('underline'),
    fill: own.backgroundColor,
    filled: !/, 0\\)$|^transparent$/.test(own.backgroundColor),
    look: [own.color, own.borderTopColor, own.boxShadow, own.fontWeight].join(' '),
    inside: [...el.querySelectorAll('*')].slice(0, 8)
      .map(node => css(node).color + css(node).opacity).join(' '),
    around: around.join(' '),
    row: row.join(' '),
  };
}
"""


def hover(page: Page, probe: str):
    """The element's look at rest and under the pointer, or None when it cannot be reached."""
    page.mouse.move(0, 0)
    target = page.locator(f'[data-hover-probe="{probe}"]').first
    target.scroll_into_view_if_needed()
    rest = page.evaluate(LOOK, probe)
    if not rest or not rest["reachable"]:
        return None
    page.mouse.move(*rest["point"])
    under = page.evaluate(LOOK, probe)
    page.mouse.move(0, 0)
    return rest, under


def sweep(page: Page, where: str, seen: set, problems: list) -> None:
    for item in page.evaluate(COLLECT):
        if item["kind"] in seen:
            continue
        looks = hover(page, item["id"])
        if looks is None:
            continue
        seen.add(item["kind"])
        rest, under = looks
        name = f"{where}: {item['kind']} ({item['label']!r})"
        on_surface = rest["filled"] or under["filled"] or rest["around"] != under["around"]
        if under["underlined"] and not rest["underlined"] and on_surface:
            problems.append(f"{name} answers on its surface and also underlines")
        answers = any(rest[key] != under[key] for key in rest if key not in ("point", "reachable"))
        if not answers and not item["exempt"]:
            problems.append(f"{name} does not answer the pointer")


STAFF = [
    "/",
    "/clients/",
    "/clients/new/",
    "/requests/",
    "/setup/categories/",
    "/import/organizations/",
]
PORTAL = ["/portal/", "/portal/requests/", "/portal/forms/"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_control_answers_the_pointer_with_one_cue(page: Page, demo, theme):
    page.emulate_media(color_scheme=theme)
    problems: list = []
    for account, pages, record in (
        ("demo_admin", STAFF, "/requests/"),
        ("demo_client", PORTAL, "/portal/requests/"),
    ):
        seen: set = set()
        sign_in(page, account, demo.password)
        page.wait_for_load_state("networkidle")
        for url in pages:
            page.goto(url)
            page.wait_for_load_state("networkidle")
            sweep(page, url, seen, problems)
        # A record page, with its facts, breadcrumbs and (for staff) the composer's radios.
        page.goto(record)
        page.locator("a.vr-row-link").first.click()
        page.wait_for_load_state("networkidle")
        sweep(page, f"{record}<record>", seen, problems)
        # A menu: its items are links and buttons side by side.
        page.locator(".vr-topbar [data-bs-toggle=dropdown]").last.click()
        page.locator(".vr-topbar .dropdown-menu.show").wait_for()
        sweep(page, f"{record}<record> account menu", seen, problems)
        page.keyboard.press("Escape")
        page.context.clear_cookies()
    assert not problems, "\n".join(problems)


def look_on_hover(page: Page, selector: str):
    page.locator(selector).first.wait_for()
    probe = page.evaluate(
        """selector => {
            const el = [...document.querySelectorAll(selector)]
              .find(candidate => candidate.getBoundingClientRect().width);
            for (let node = el, i = 0; node && i < 3; node = node.parentElement, i++) {
              node.style.transition = 'none';
            }
            el.dataset.hoverProbe = 'named';
            return el.dataset.hoverProbe;
        }""",
        selector,
    )
    looks = hover(page, probe)
    assert looks, f"{selector} cannot be reached by the pointer"
    page.evaluate(
        "document.querySelector('[data-hover-probe=named]').removeAttribute('data-hover-probe')"
    )
    return looks


def test_text_links_underline_and_controls_answer_on_their_surface(page: Page, demo):
    sign_in(page, "demo_admin", demo.password)
    page.goto("/clients/")
    for text_link in ("a.vr-row-link", "footer a"):
        rest, under = look_on_hover(page, text_link)
        assert not rest["underlined"] and under["underlined"], text_link

    # The same button as a link and as a <button>: both answer with the fill, neither underlines.
    for button in (
        ".vr-page-actions a.btn-primary",
        ".vr-grid-toolbar button.btn-outline-secondary",
    ):
        rest, under = look_on_hover(page, button)
        assert not under["underlined"] and rest["fill"] != under["fill"], button

    page.locator("a.vr-row-link").first.click()
    rest, under = look_on_hover(page, ".breadcrumb a")
    assert not rest["underlined"] and under["underlined"]

    # The quiet button has no fill at rest and takes one on hover, like every other button.
    page.goto("/import/organizations/")
    rest, under = look_on_hover(page, ".vr-form-actions .btn-link")
    assert not rest["filled"] and under["filled"] and not under["underlined"]

    # A checkbox and its label are one target: the label shows the pointer and marks the box.
    page.context.clear_cookies()
    page.goto("/accounts/login/")
    label = page.locator("label.form-check-label").first
    box = page.locator("input.form-check-input").first
    edge = "el => getComputedStyle(el).borderTopColor"
    page.mouse.move(0, 0)
    at_rest = box.evaluate(edge)
    label.hover()
    assert label.evaluate("el => getComputedStyle(el).cursor") == "pointer"
    page.wait_for_function(
        "([el, rest]) => getComputedStyle(el).borderTopColor !== rest",
        arg=[box.element_handle(), at_rest],
    )

    # The portal's form tiles and its navigation answer with their fill alone.
    sign_in(page, "demo_client", demo.password)
    page.wait_for_load_state("networkidle")
    for control in (".vr-service-tile", ".vr-portal-nav .nav-link:not([aria-current])"):
        rest, under = look_on_hover(page, control)
        assert not under["underlined"] and rest["fill"] != under["fill"], control
    rest, under = look_on_hover(page, ".vr-portal-nav .nav-link[aria-current]")
    assert not under["underlined"]
