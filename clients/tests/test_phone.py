"""Phone use (docs/ui-standards.md section 13): the staff thumb bar, the right keyboards and the
stacked-row hooks. The layout itself is checked in the browser (e2e/test_client_journey.py)."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

pytestmark = pytest.mark.django_db

APP_CSS = Path(settings.BASE_DIR, "static", "css", "app.css").read_text()


def test_staff_pages_have_the_thumb_bar_and_the_portal_does_not(client, make_user, clinic):
    client.force_login(make_user("cs", "client_services"))
    page = client.get(reverse("clients:client-list")).content.decode()
    bar = re.search(r'<nav class="vr-thumbbar" aria-label="Quick actions">(.*?)</nav>', page, re.S)
    assert bar, "staff pages carry the thumb bar"
    assert 'data-bs-target="#vr-nav"' in bar.group(1)
    # Find (requests and client organizations), the first phone journey.
    assert f'href="{reverse("desk:request-find")}"' in bar.group(1)

    client.force_login(make_user("portal", client=clinic))
    portal = client.get(reverse("portal:home")).content.decode()
    assert "vr-thumbbar" not in portal


@pytest.mark.parametrize(
    "url_name, args",
    [("clients:client-create", ()), ("clients:contact-create", ("pk",)), ("lab-profile-edit", ())],
)
def test_phone_numbers_open_the_dial_pad(client, make_user, clinic, url_name, args):
    from core.models import Lab

    Lab.objects.get_or_create(pk=1, defaults={"name": "Example Laboratory"})
    client.force_login(make_user("owner", "admin"))
    page = client.get(reverse(url_name, args=[clinic.pk for _ in args])).content.decode()
    phone = re.search(r'<input[^>]*name="phone"[^>]*>', page).group(0)
    assert 'type="tel"' in phone and 'autocomplete="tel"' in phone


def test_tables_stack_on_phones_unless_they_opt_out():
    phones = APP_CSS[APP_CSS.index("/* Stacked rows:") : APP_CSS.index("/* Thumb bar:")]
    assert "@media (max-width: 575.98px)" in phones
    assert "table.vr-table:not(.vr-table-scroll) > tbody > tr" in phones
    assert "attr(data-vr-label)" in phones
    # Sorting survives: a sortable header row becomes "Sort by" chips; others are hidden.
    assert "thead:has(.vr-sort)" in phones and 'content: "Sort by"' in phones
    assert "thead:not(:has(.vr-sort))" in phones
    assert "--vr-touch-target: 2.75rem;" in APP_CSS


def test_touch_screens_get_44px_controls_and_no_hover_only_reveals():
    touch = APP_CSS[APP_CSS.index("@media (max-width: 575.98px), (pointer: coarse)") :]
    assert "--vr-control-height: var(--vr-touch-target);" in touch
    assert "@media (hover: none)" in APP_CSS
    assert ".vr-portal .vr-portal-nav {" in APP_CSS


def test_allauth_hand_written_fields_render_bootstrap_controls(client, make_user):
    user = make_user("staffer", "technical")
    client.force_login(user)
    page = client.get(reverse("account_email")).content.decode()
    radio = re.search(r'<input[^>]*type="radio"[^>]*>', page).group(0)
    assert 'class="form-check-input"' in radio
