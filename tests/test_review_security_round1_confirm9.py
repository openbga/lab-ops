"""Ninth confirmation of the source records and the rebased staff page layout."""

import hashlib
import shutil

import pytest
from django.test import override_settings
from django.test.html import parse_html
from django.urls import reverse
from django.utils.html import escape

from core import security_settings
from core.models import Lab
from tests.test_review_security_round1_confirm2 import config_rows
from tests.test_review_security_round1_signoff import CHANGED, ROOT, probe


@pytest.mark.parametrize(
    "changed_files",
    [(), ("settings.py",), ("logging.py",), ("settings.py", "logging.py")],
    ids=["release", "settings", "logging", "both"],
)
def test_startup_digest_matches_each_source_file_byte_for_byte(tmp_path, changed_files):
    directory = tmp_path / "config"
    shutil.copytree(ROOT / "config", directory, ignore=shutil.ignore_patterns("__pycache__"))
    expected = []
    for name in ("settings.py", "logging.py"):
        path = directory / name
        original = path.read_bytes()
        if name in changed_files:
            # Only the line endings change. Python reads the same configuration values.
            published = original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            assert published != original and published.splitlines() == original.splitlines()
            path.write_bytes(published)
        expected.append(hashlib.sha256(path.read_bytes()).hexdigest())

    digests, debug, logging_version, value, note = probe(
        tmp_path,
        """
        fact = https()
        modules = ("config.settings", "config.logging")
        print(json.dumps([[security.module_digest(m) for m in modules], settings.STARTUP_DEBUG,
                          settings.LOGGING["version"], fact.value, fact.note]))
        """,
    )
    assert digests == expected and debug is True and logging_version == 1
    assert value == (CHANGED if changed_files else "Not required")
    for name in ("settings.py", "logging.py"):
        assert (f"config/{name} differs" in note) == (name in changed_files), note
    assert "does not record" not in note and "could not be read" not in note


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("logging_record", "logging_reason"),
    [
        (None, "config/settings.py does not record config/logging.py"),
        ("", "config/logging.py could not be read"),
        (security_settings.RELEASE_LOGGING_SHA256, ""),
    ],
    ids=["missing", "unreadable", "matches"],
)
def test_admin_sees_separate_reasons_when_settings_source_is_unreadable(
    client, staff, logging_record, logging_reason
):
    client.force_login(staff("ninth-records", "admin"))
    with override_settings(SETTINGS_SHA256="", LOGGING_SHA256=logging_record):
        response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    rows = config_rows(response.context["groups"])
    assert {"https", "session-length", "sign-in-records"} <= {row.key for row in rows}
    for row in rows:
        assert row.value == CHANGED
        assert "config/settings.py could not be read" in row.note
        if logging_reason:
            assert row.note.count("config/logging.py") == 1
            assert logging_reason in row.note
        else:
            assert "config/logging.py" not in row.note
        assert escape(row.note).encode() in response.content


@pytest.mark.django_db
def test_contact_pages_keep_their_headings_and_help_after_the_ticket_layout_change(client, staff):
    Lab.objects.create(name="Ninth Example Laboratory")
    client.force_login(staff("ninth-pages", "admin"))
    pages = [
        (reverse("security-settings"), "Security settings", "core-security-settings"),
        (reverse("lab-profile"), "Laboratory profile", "core-lab-profile"),
        (
            reverse("lab-contact-edit", args=["privacy"]),
            "Edit privacy contact",
            "core-lab-contact-form",
        ),
    ]
    for url, heading, help_id in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        markup = response.content.decode()
        document = parse_html(markup)
        assert document.count(parse_html(f'<h1 class="vr-page-title">{heading}</h1>')) == 1, url
        assert f'data-vr-documentation-id="{help_id}"' in markup, url
        assert "vr-ticket-header" not in markup and "vr-record-stacked-lead" not in markup, url


def test_alternate_module_without_digest_records_is_identified_by_its_own_name(tmp_path):
    (tmp_path / "ninth_site.py").write_text(
        "from config.settings import *\ndel SETTINGS_SHA256, LOGGING_SHA256\n"
    )
    loaded, digests, value, note = probe(
        tmp_path,
        """
        row = https()
        names = ("config.settings", "config.logging")
        print(json.dumps([settings.SETTINGS_MODULE, [security.module_digest(n) for n in names],
                          row.value, row.note]))
        """,
        module="ninth_site",
    )
    assert loaded == "ninth_site" and digests == [None, None]
    assert value == CHANGED and loaded in note
    assert "config/settings.py" not in note and "config/logging.py" not in note
