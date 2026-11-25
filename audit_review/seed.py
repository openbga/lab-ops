"""Demonstration seed for review records (core.seeding): a review interval of 31 days and one review
of last month's activity, signed by the demonstration's reviewer (desk.story.REVIEW_HOLDER, who
holds "Can review access and audit records"), with a note added a few days later. Written through
the audited services at moments in the past (nonconformance.seed.happened_at); the seed signs
without a password, which only a person signing on the form is asked for. Synthetic data only."""

from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from desk.story import REVIEW_HOLDER
from nonconformance.seed import happened_at

from . import reviews
from .models import Review, ReviewSchedule

INTERVAL_DAYS = 31


def seed_setup(ctx):
    """Nothing to set up: the reviewer gets the capability with the desk's records (desk.story),
    so the interval is set with the records."""


def _interval(reviewer):
    """Set the interval once. A laboratory that has set one since keeps its choice."""
    if ReviewSchedule.objects.exists():
        return
    reviews.set_interval(
        actor=reviewer,
        interval_days=INTERVAL_DAYS,
        expected_revision=0,
        reason="Demonstration: the example laboratory reviews its activity records each month.",
    )


def _local(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def seed_records(ctx):
    """One signed review of the previous calendar month, and a note on what its follow-up came
    to, once."""
    reviewer = User.objects.get(username=REVIEW_HOLDER)
    _interval(reviewer)
    if Review.objects.exists():
        return
    now = timezone.now()
    today = timezone.localdate(now)
    last_day = today.replace(day=1) - timedelta(days=1)
    first_day = last_day.replace(day=1)
    # Signed on the third of this month, or an hour ago early in a month.
    signed = min(_local(today.replace(day=1) + timedelta(days=2), 10), now - timedelta(hours=1))
    with transaction.atomic(), happened_at(signed):
        review = reviews.sign_review(
            actor=reviewer,
            period_from=first_day,
            period_to=last_day,
            examined=["changes", "access", "sign_ins", "people"],
            outcome=Review.Outcome.FOLLOW_UP,
            findings="One client portal account had five failed sign-ins in a row on one "
            "evening, with no sign-in after them. Every other count matched normal working "
            "hours and the on-call rota.",
            follow_up="Client services to ask the organization whether the person still needs "
            "the account.",
        )
    noted = min(signed + timedelta(days=2), now - timedelta(minutes=30))
    with transaction.atomic(), happened_at(noted):
        reviews.add_note(
            actor=reviewer,
            review=review,
            text="The organization confirmed the person has left. Their portal access was ended "
            "the same day.",
            expected_revision=1,
        )
