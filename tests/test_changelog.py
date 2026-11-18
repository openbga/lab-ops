"""What's new: changelog.d/ entries, the generated CHANGELOG.md and the page with its unread
indicator (core/changelog.py, core/views.whats_new, core/templatetags/whats_new.py)."""

import datetime
import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from accounts.models import User
from clients.models import Client
from core import changelog
from core.changelog import ChangelogError, Entry, group_entries, parse_entry, render_markdown
from core.models import UserPreference
from core.preferences import WHATS_NEW_KEY, clean

GOOD = """---
date: 2026-11-16
type: added
impact: high
title: Add contact roles to client organizations
area: Clients
link: clients:client-list
pr: 15
highlight: true
---
Client services can record each contact's role.

Roles are free text.

## Technical notes

Adds a migration in clients.
"""


def write(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def entry(day, title, impact="medium", type="added", id=None, **extra):
    date = datetime.date.fromisoformat(day)
    slug = id or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Entry(
        id=f"{day}-{slug}",
        date=date,
        type=type,
        impact=impact,
        title=title,
        summary="Synthetic summary.",
        **extra,
    )


# ------------------------------------------------------------------ parsing


def test_entry_parses_front_matter_summary_and_technical_notes(tmp_path):
    parsed = parse_entry(write(tmp_path, "2026-11-16-contact-roles.md", GOOD))
    assert parsed.id == "2026-11-16-contact-roles"
    assert parsed.date == datetime.date(2026, 11, 16)
    assert (parsed.type, parsed.type_label, parsed.impact) == ("added", "Added", "high")
    assert (parsed.area, parsed.link, parsed.pr) == ("Clients", "clients:client-list", 15)
    assert parsed.highlight
    assert (
        parsed.summary == "Client services can record each contact's role.\n\nRoles are free text."
    )
    assert parsed.notes == "Adds a migration in clients."


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("type: added", "type: improved"), "key 'type' must be one of"),
        (("impact: high", "impact: critical"), "key 'impact' must be one of"),
        (("highlight: true", "highlight: maybe"), "key 'highlight' must be true or false"),
        (("date: 2026-11-16", "date: 2026-02-30"), "key 'date' must be a date"),
        (("date: 2026-11-16", "date: 16/11/2026"), "key 'date' must be a date"),
        (("date: 2026-11-16\n", ""), "missing required key 'date'"),
        (
            ("title: Add contact roles to client organizations\n", ""),
            "missing required key 'title'",
        ),
        (("impact: high", "impact:"), "missing required key 'impact'"),
        (("pr: 15", "pr: #15"), "key 'pr' must be a pull request number"),
        (("area: Clients", "owner: Clients"), "unknown key 'owner'"),
        (("area: Clients", "area: Clients\narea: Sales"), "key 'area' appears twice"),
        (("client organizations\n", "client organizations.\n"), "without a trailing period"),
        (("link: clients:client-list", "link: /clients/"), "key 'link' must be a route name"),
        (
            ("Client services can record each contact's role.\n\nRoles are free text.\n", ""),
            "summary",
        ),
    ],
)
def test_invalid_entries_are_rejected_naming_file_and_key(tmp_path, change, message):
    path = write(tmp_path, "2026-11-16-contact-roles.md", GOOD.replace(*change))
    with pytest.raises(ChangelogError) as error:
        parse_entry(path)
    assert str(error.value).startswith("2026-11-16-contact-roles.md: ")
    assert message in str(error.value)


@pytest.mark.parametrize("name", ["contact-roles.md", "2026-11-16_Roles.md", "2026-11-16.md"])
def test_file_names_follow_the_date_slug_pattern(tmp_path, name):
    with pytest.raises(ChangelogError, match="YYYY-MM-DD-short-slug.md"):
        parse_entry(write(tmp_path, name, GOOD))


def test_front_matter_must_open_and_close(tmp_path):
    with pytest.raises(ChangelogError, match="must start with a '---'"):
        parse_entry(write(tmp_path, "2026-11-16-a.md", GOOD.removeprefix("---\n")))
    with pytest.raises(ChangelogError, match="no closing '---'"):
        parse_entry(
            write(
                tmp_path,
                "2026-11-16-a.md",
                GOOD.replace("\n---\nClient services", "\nClient services"),
            )
        )


# ------------------------------------------------------------------ ordering and the document


def test_groups_are_months_newest_first_with_impact_sections_and_date_ranges():
    entries = [
        entry("2026-10-03", "October fix", "low", "fixed"),
        entry("2026-11-02", "Early high", "high"),
        entry("2026-11-17", "Beta medium"),
        entry("2026-11-17", "Alpha medium"),
        entry("2026-11-09", "Later medium"),
        entry("2026-11-17", "Late high", "high"),
    ]
    groups = group_entries(entries)
    assert [(g.label, g.start, g.end, g.count) for g in groups] == [
        ("November 2026", datetime.date(2026, 11, 2), datetime.date(2026, 11, 17), 5),
        ("October 2026", datetime.date(2026, 10, 3), datetime.date(2026, 10, 3), 1),
    ]
    november = {impact: [e.title for e in items] for impact, _, items in groups[0].sections}
    assert list(november) == ["high", "medium"]  # empty "low" left out
    assert november["high"] == ["Late high", "Early high"]
    assert november["medium"] == ["Alpha medium", "Beta medium", "Later medium"]


def test_markdown_lists_groups_impacts_and_entries_in_order():
    entries = [
        entry("2026-11-16", "Contact roles", "high", pr=15, area="Clients", notes="Migration."),
        entry("2026-11-17", "Due dates", "medium", "changed"),
        entry("2026-10-31", "Spacing", "low", "fixed"),
    ]
    text = render_markdown(entries, repository="https://example.invalid/repo")
    assert text.startswith("<!-- Generated from changelog.d/")
    assert "Do not edit this file" in text.splitlines()[0]
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Changelog",
        "## November 2026 (2026-11-16 to 2026-11-17)",
        "### High impact",
        "### Medium impact",
        "## October 2026 (2026-10-31)",
        "### Low impact",
    ]
    assert (
        "- **Added:** Contact roles (2026-11-16, Clients, "
        "[#15](https://example.invalid/repo/pull/15))" in text
    )
    assert "  <details><summary>Technical notes</summary>\n\n  Migration.\n\n  </details>" in text
    assert "- **Changed:** Due dates (2026-11-17)" in text


def test_check_mode_detects_stale_output_and_regenerates(tmp_path, monkeypatch, settings):
    entries_dir = tmp_path / "changelog.d"
    entries_dir.mkdir()
    write(entries_dir, "2026-11-16-contact-roles.md", GOOD)
    output = tmp_path / "CHANGELOG.md"
    monkeypatch.setattr(changelog, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(changelog, "CHANGELOG", output)

    with pytest.raises(CommandError, match="out of date"):  # missing
        call_command("changelog", "--check", stdout=StringIO())
    call_command("changelog", stdout=StringIO())
    call_command("changelog", "--check", stdout=StringIO())

    write(entries_dir, "2026-11-17-due-dates.md", GOOD.replace("contact roles", "due dates"))
    with pytest.raises(CommandError, match="out of date"):  # a new entry
        call_command("changelog", "--check", stdout=StringIO())
    call_command("changelog", stdout=StringIO())
    output.write_text(output.read_text() + "\nA hand edit.\n")
    with pytest.raises(CommandError, match="out of date"):  # a hand edit
        call_command("changelog", "--check", stdout=StringIO())

    write(entries_dir, "2026-11-18-bad.md", GOOD.replace("type: added", "type: new"))
    with pytest.raises(CommandError, match=r"2026-11-18-bad.md: key 'type'"):
        call_command("changelog", "--check", stdout=StringIO())
    (entries_dir / "2026-11-18-bad.md").unlink()
    write(entries_dir, "2026-11-18-link.md", GOOD.replace("clients:client-list", "nope:list"))
    with pytest.raises(CommandError, match=r"2026-11-18-link.md: key 'link' names no page"):
        call_command("changelog", "--check", stdout=StringIO())


def test_the_repository_entries_are_valid_and_the_committed_document_is_current():
    call_command("changelog", "--check", stdout=StringIO())


# ------------------------------------------------------------------ unread logic


def test_unread_counts_entries_newer_than_the_seen_marker():
    old = entry("2026-11-10", "Old")
    seen_day = entry("2026-11-16", "Seen that day")
    entries = [old, seen_day]
    assert changelog.unread(entries, None) == entries  # never opened: everything is new
    marker = changelog.seen_marker(entries)
    assert marker == {"date": "2026-11-16", "ids": [seen_day.id, old.id]}
    assert changelog.unread(entries, marker) == []

    newer = entry("2026-11-17", "Newer")
    same_day = entry("2026-11-16", "Merged later the same day")
    backdated = entry("2026-11-12", "Merged late with an earlier date")
    ancient = entry("2026-08-01", "Older than the window")
    later = [newer, same_day, backdated, ancient, *entries]
    assert changelog.unread(later, marker) == [newer, same_day, backdated]
    assert changelog.unread(later, changelog.seen_marker(later)) == []
    assert changelog.seen_marker([]) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-11-16",
        {"date": "2026-11-16"},
        {"date": "yesterday", "ids": []},
        {"date": "2026-11-16", "ids": ["../etc"]},
        {"date": "2026-11-16", "ids": [], "extra": 1},
    ],
)
def test_a_malformed_seen_preference_is_ignored(value):
    assert clean(WHATS_NEW_KEY, value) is None
    assert changelog.unread([entry("2026-11-16", "A")], value) == [entry("2026-11-16", "A")]


# ------------------------------------------------------------------ page and indicator


@pytest.fixture
def entries(monkeypatch):
    items = [
        entry("2026-11-17", "Record due dates", "medium", "changed", link="clients:client-list"),
        entry(
            "2026-11-16",
            "Contact roles",
            "high",
            pr=15,
            link="announcement-list",
            notes="Adds a migration in clients.",
        ),
        entry("2026-10-30", "Tidy spacing", "low", "fixed"),
    ]
    monkeypatch.setattr(changelog, "entries", lambda: tuple(items))
    return items


def indicator(response):
    html = response.content.decode()
    match = re.search(r'<a class="btn btn-sm vr-topbar-btn vr-whats-new"[^>]*>', html)
    assert match, "What's new link expected in the top bar"
    label = re.search(r'aria-label="([^"]*)"', match.group(0)).group(1).replace("&#x27;", "'")
    return label, 'class="vr-topbar-count"' in html


@pytest.mark.django_db
def test_a_first_visit_has_nothing_new_and_records_the_newest_entries_as_seen(
    client, staff, entries
):
    user = staff("wn-new", "technical")
    client.force_login(user)
    assert indicator(client.get(reverse("dashboard"))) == ("What's new", False)
    stored = UserPreference.objects.get(user=user, key=WHATS_NEW_KEY).value
    assert stored == changelog.seen_marker(entries)  # recorded without opening the list
    html = client.get(reverse("whats-new")).content.decode()
    assert "data-new" not in html and "Tidy spacing" in html  # every entry, none New

    other = staff("wn-other", "technical")  # seen is per user: a new account starts clear too
    client.force_login(other)
    assert indicator(client.get(reverse("dashboard"))) == ("What's new", False)


@pytest.mark.django_db
def test_indicator_counts_entries_newer_than_the_mark_and_opening_the_list_clears_it(
    client, staff, entries
):
    user = staff("wn-staff", "technical")
    client.force_login(user)
    client.get(reverse("dashboard"))  # first visit: marks the current entries as seen

    entries.insert(0, entry("2026-11-18", "Brand new change", "high"))
    entries.insert(1, entry("2026-11-18", "Another new change"))
    assert indicator(client.get(reverse("dashboard"))) == ("What's new: 2 new changes", True)
    page = client.get(reverse("whats-new"))
    html = page.content.decode()
    assert "2 new changes since your last visit" in html
    assert html.count("data-new") == 2
    assert indicator(page) == ("What's new", False)  # cleared on the page that opened it
    assert indicator(client.get(reverse("dashboard"))) == ("What's new", False)
    stored = UserPreference.objects.get(user=user, key=WHATS_NEW_KEY).value
    assert stored == changelog.seen_marker(entries)

    entries.insert(0, entry("2026-11-19", "Newest change"))
    assert indicator(client.get(reverse("dashboard"))) == ("What's new: 1 new change", True)


@pytest.mark.django_db
def test_list_shows_groups_impacts_entries_and_permitted_links(client, staff, entries):
    user = staff(
        "wn-pm", "account_manager"
    )  # has Client organizations, not Announcements, in the menu
    client.force_login(user)
    html = client.get(reverse("whats-new")).content.decode()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    assert "November 2026 Highlights" in text and "All 2 changes" in text
    assert "October 2026 Highlights" in text and "Change details" in text
    assert text.index("November 2026") < text.index("October 2026")
    november = text[text.index("November 2026") : text.index("October 2026")]
    assert november.index("High impact") < november.index("Medium impact")
    assert "Added Contact roles" in november and "Change #15" in november
    assert "Changed Record due dates" in november
    assert "Fixed Tidy spacing" in text
    assert "Technical notes" in html and "Adds a migration in clients." in html
    assert f'href="{reverse("clients:client-list")}">Go to Client organizations' in html
    assert "data-new" not in html  # a first visit has nothing new
    assert 'aria-current="page"' in html

    # not in this account's menu: no link
    assert f'href="{reverse("announcement-list")}"' not in html

    admin = staff("wn-admin", "admin")  # Announcements are in the admin menu
    client.force_login(admin)
    html = client.get(reverse("whats-new")).content.decode()
    assert f'href="{reverse("announcement-list")}">Go to Announcements' in html


@pytest.mark.django_db
def test_monthly_highlights_prioritize_unread_then_featured_then_impact(client, staff, entries):
    featured = entry("2026-11-14", "Featured change", "low", highlight=True)
    routine = entry("2026-11-19", "Routine change", "low")
    entries.extend([featured, routine])
    client.force_login(staff("wn-highlights", "technical"))
    url = reverse("whats-new")

    def highlighted(page):
        return [row["entry"].title for row in page.context["groups"][0]["highlights"]]

    assert highlighted(client.get(url)) == ["Featured change", "Contact roles", "Record due dates"]
    entries.append(entry("2026-11-20", "Unread change", "low"))
    assert highlighted(client.get(url)) == ["Unread change", "Featured change", "Contact roles"]
    entries.extend(entry("2026-11-21", f"Update {i}") for i in range(4))
    page = client.get(url)
    assert highlighted(page) == ["Update 0", "Update 1", "Update 2"]
    assert "All 9 changes (4 new)" in page.content.decode()


@pytest.mark.django_db
def test_empty_list_has_an_empty_state(client, staff, monkeypatch):
    monkeypatch.setattr(changelog, "entries", lambda: ())
    client.force_login(staff("wn-empty", "technical"))
    response = client.get(reverse("whats-new"))
    assert "No changes recorded yet" in response.content.decode()
    assert indicator(response) == ("What's new", False)
    assert not UserPreference.objects.filter(key=WHATS_NEW_KEY).exists()


@pytest.mark.django_db
def test_staff_without_a_role_may_open_it_and_clients_and_visitors_may_not(client, entries):
    no_role = User.objects.create_user("wn-none", "wn-none@example.invalid", "test-password-729!")
    client.force_login(no_role)
    assert client.get(reverse("whats-new")).status_code == 200

    portal = User.objects.create_user(
        "wn-client",
        "wn-client@example.invalid",
        "test-password-729!",
        client=Client.objects.create(name="Example Clinic"),
    )
    client.force_login(portal)
    assert client.get(reverse("whats-new")).status_code == 403
    assert "vr-whats-new" not in client.get(reverse("portal:home")).content.decode()

    client.logout()
    response = client.get(reverse("whats-new"))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


@pytest.mark.django_db
def test_the_seen_marker_cannot_be_written_through_the_preferences_endpoint(client, staff):
    client.force_login(staff("wn-post", "technical"))
    response = client.post(
        reverse("preference"),
        '{"key": "whats_new.seen", "value": {"date": "2099-01-01", "ids": []}}',
        content_type="application/json",
    )
    assert response.status_code == 400
