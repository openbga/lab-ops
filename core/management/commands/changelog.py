"""Write CHANGELOG.md from the entry files in changelog.d/ (core.changelog).

    uv run python manage.py changelog          regenerate CHANGELOG.md
    uv run python manage.py changelog --check  exit non-zero when an entry is invalid, a link
                                               names no page, or CHANGELOG.md is stale

The skill .agents/skills/changelog/SKILL.md says when an entry is needed and how to write it.
"""

from django.core.management.base import BaseCommand, CommandError
from django.urls import NoReverseMatch, reverse

from core import changelog


class Command(BaseCommand):
    help = "Generate CHANGELOG.md from changelog.d/, or check that it is current (--check)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Change nothing; fail when CHANGELOG.md does not match changelog.d/.",
        )

    def handle(self, *args, check=False, **options):
        try:
            entries = changelog.read_entries()
        except changelog.ChangelogError as error:
            raise CommandError(f"changelog.d/{error}") from None
        for entry in entries:
            if entry.link:
                try:
                    reverse(entry.link)
                except NoReverseMatch:
                    raise CommandError(
                        f"changelog.d/{entry.id}.md: key 'link' names no page without arguments: "
                        f"{entry.link}"
                    ) from None
        if check:
            if changelog.stale(entries):
                raise CommandError(
                    "CHANGELOG.md is out of date with changelog.d/. "
                    "Run `uv run python manage.py changelog` and commit the result."
                )
            self.stdout.write(f"CHANGELOG.md is current ({len(entries)} entries).")
            return
        changelog.CHANGELOG.write_text(changelog.render_markdown(entries), encoding="utf-8")
        self.stdout.write(f"Wrote CHANGELOG.md ({len(entries)} entries).")
