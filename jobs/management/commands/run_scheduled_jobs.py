"""Run the scheduled jobs once. A scheduler (cron, a Render cron job, a systemd timer) runs this
every few minutes; docs/operations.md "Scheduled jobs"."""

from django.core.management.base import BaseCommand, CommandError

from jobs import runner
from jobs.models import JobOutcome
from jobs.registry import JOBS


class Command(BaseCommand):
    help = (
        "Run the scheduled jobs once: close resolved tickets after the laboratory's period, tell "
        "the people responsible about breached service targets, and send or retry email notices. "
        "Run it every few minutes from a scheduler. Exits with an error when a job fails."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--job",
            action="append",
            choices=[job.name for job in JOBS],
            dest="jobs",
            help="Run only this job (repeat for several). Default: every job, in order.",
        )

    def handle(self, *args, **options):
        try:
            results = runner.run(only=options["jobs"])
        except runner.Busy:
            self.stdout.write("Another run of the scheduled jobs is in progress; nothing was done.")
            return
        failed = []
        for result in results:
            if result.outcome == JobOutcome.SUCCEEDED:
                self.stdout.write(f"{result.name}: {runner.counts_text(result.counts)}")
            else:
                failed.append(result.name)
                self.stdout.write(self.style.ERROR(f"{result.name}: failed ({result.error})"))
        if failed:
            raise CommandError(f"Scheduled jobs failed: {', '.join(failed)}. See the log.")
