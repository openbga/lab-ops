"""The service clocks' scheduled job (jobs.registry): tell the people responsible once when a
request passes its first-response or resolution target.

A clock is breached when it is running (not met, and for resolution not paused) and its due time
has passed; at the due time itself it is still on time, as service_levels.rules counts it. For
each breached clock not yet noticed in this run of it, the job records a notice for each person
desk.notifications.breach_recipients names (the assignee and the team's leads, or the whole team
when it has neither), marks the clock noticed and records an audit event, in one transaction with
the request and then its clock locked (the services' order). Who may be told is read after the
access of everyone the rule names is held (accounts.acting.try_hold_access): a person
deactivated, or whose role or team membership was taken away, before then is not told, and such
a change made afterwards waits for the job. A request or clock someone is changing at that
moment, or a person whose access is being changed, is left for the next run. The request itself
does not change, so nobody's open page of it goes stale.

A run of a clock ends when it is met or resolved, or when a recalculation moves its due time
(service_levels.rules): its mark is cleared, a later breach is noticed again, and a notice of the
earlier run that is still waiting no longer applies (desk.notifications.prepare_breach_notice
compares the mark it was recorded with). Clocks already past due when this release was installed
were marked by migration 0003 so the first run does not send a notice for every old breach.
"""

import uuid
from collections import Counter

from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.acting import try_hold_access
from core.audit import record_event
from jobs import outbox
from jobs.actor import system_account
from jobs.locks import is_lock_unavailable, share_lock

from .models import RequestClock
from .rules import STOPPED_STATUSES

# Breached clocks are read in pages of this many until none is left.
BREACH_BATCH = 500
CLOCKS = ("first_response", "resolution")


def breached_filter(now):
    """Clocks running past their due time and not noticed yet in this run of them."""
    first = Q(
        first_response_met_at__isnull=True,
        first_response_due_at__lt=now,
        first_response_breach_noticed_at__isnull=True,
    )
    resolution = Q(
        resolution_met_at__isnull=True,
        paused_since__isnull=True,
        resolution_due_at__lt=now,
        resolution_breach_noticed_at__isnull=True,
    )
    return (first | resolution) & ~Q(request__status__in=STOPPED_STATUSES)


def notice_breaches(now=None):
    now = now or timezone.now()
    counts = Counter({"breaches": 0, "notices": 0})
    actor = None
    after = None  # the last clock looked at: every breached clock gets its turn in each run
    while True:
        page = RequestClock.objects.filter(breached_filter(now))
        if after is not None:  # no lower bound before the first page: any key is a clock's
            page = page.filter(pk__gt=after)
        page = list(page.order_by("pk").values_list("pk", "request_id")[:BREACH_BATCH])
        if not page:
            break
        actor = actor or system_account()
        for clock_id, request_id in page:
            after = clock_id
            try:
                outcome = _notice_request(request_id, actor, now)
            except OperationalError as exc:
                if not is_lock_unavailable(exc):
                    raise
                # A person to tell, or the team, is being changed right now: the transaction
                # let go of everything; the next run decides again.
                outcome = {"busy": 1}
            counts.update(outcome)
    return dict(counts)


def _notice_request(request_id, actor, now):
    """Record the breach notices of one request, in one transaction: the request, then its clock,
    then the people to tell are locked without waiting, so the job never waits for a row while
    holding another a writer may need."""
    from accounts.models import User
    from desk import notifications
    from desk.models import ServiceRequest
    from teams.models import Team, TeamMembership

    counts = Counter()
    with transaction.atomic():
        request = (
            ServiceRequest.objects.select_for_update(of=("self",), skip_locked=True)
            .filter(pk=request_id)
            .exclude(status__in=STOPPED_STATUSES)
            .first()
        )
        if request is None:  # being changed right now, or resolved since: next run
            return {"busy": 1}
        clock = RequestClock.objects.select_for_update(skip_locked=True).filter(request=request)
        clock = clock.first()
        if clock is None:  # being changed right now: next run
            return {"busy": 1}
        # Who is responsible, held while it is decided and recorded: the team and its
        # memberships against a member added or a lead changed (key share: assignments go on),
        # then the access of everyone the rule names, before reading whether each may be told:
        # a deactivation, or a role or membership taken away, either committed before (and the
        # reading sees it) or waits for this transaction. Then the accounts the notices
        # reference. All without waiting.
        share_lock(Team, [request.team_id], nowait=True, mode="key share")
        memberships = TeamMembership.objects.filter(team_id=request.team_id)
        share_lock(
            TeamMembership, memberships.values_list("pk", flat=True), nowait=True, mode="key share"
        )
        candidates = [pk for pk, _role in notifications.breach_roles(request)]
        if not (try_hold_access(candidates) and notifications.try_hold_notice_settings(candidates)):
            # Someone's access or notice setting is being changed right now: next run.
            return {"busy": 1}
        # The accounts the notices and the audit event will name, FOR KEY SHARE (what the
        # database checks at COMMIT, taken now so the COMMIT waits for nothing), before reading
        # them: a sign-in or an assignment to them goes on, an address being changed defers.
        share_lock(User, [*candidates, actor.pk], nowait=True, mode="key share")
        recipients = notifications.breach_recipients(request)
        changed, recorded = [], []
        for which in CLOCKS:
            noticed_field = f"{which}_breach_noticed_at"
            if getattr(clock, noticed_field) is not None:
                continue
            if not notifications.breach_still_running(clock, which, now):
                continue
            due = getattr(clock, f"{which}_due_at")
            run = uuid.uuid4()  # this run of the clock; sending checks it is unchanged
            notices = [
                outbox.record(
                    recipient=user,
                    topic=notifications.BREACH_TOPIC,
                    object_id=request.pk,
                    reference=request.number,
                    events=[notifications.BREACH_EVENTS[which]],
                    data={"clock": which, "role": role, "run": str(run)},
                )
                for user, role in recipients
            ]
            setattr(clock, noticed_field, now)
            setattr(clock, f"{which}_breach_run", run)
            changed += [noticed_field, f"{which}_breach_run"]
            recorded += [notice.pk for notice in notices]
            record_event(
                actor=actor,
                action="clock.breach_noticed",
                obj=clock,
                after={
                    "request_id": request.pk,
                    "clock": which,
                    "due_at": due.isoformat(),
                    "notices": len(notices),
                },
            )
            counts["breaches"] += 1
            counts["notices"] += len(notices)
        if changed:
            clock.save(update_fields=changed)
            outbox.send_after_commit(recorded)
    return counts
