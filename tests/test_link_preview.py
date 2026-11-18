"""Link preview: a shared link shows the product's lock-up image from an absolute URL, and never
breaks a page (the 400 page for a disallowed host is covered in test_core_error_pages)."""

import re

import pytest

pytestmark = pytest.mark.django_db


def preview_image(client):
    page = client.get("/accounts/login/").content.decode()
    match = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return match and match.group(1)


def test_sign_in_page_names_the_product_and_an_absolute_preview_image(client):
    page = client.get("/accounts/login/").content.decode()
    assert '<meta property="og:title" content="OpenBGA Lab Ops">' in page
    assert re.match(r"http://testserver/static/img/link-preview.*\.png$", preview_image(client))


def test_preview_image_uses_the_public_url_when_set(client, settings):
    settings.PUBLIC_URL = "https://lab-ops.example.invalid"
    assert preview_image(client).startswith("https://lab-ops.example.invalid/static/img/")
