"""The People tab of the audit review workspace: over a period, what each account did that a review
looks at first, counted from the records the workspace's lists hold (audit_review.sources).

For each account with any of it: its sign-ins, its failed or refused sign-in attempts, the tickets
it opened, the files it downloaded, the patient information it was shown (patient-marked answers
and person-level keys), the changes it made, and how many of those happened outside the opening
hours of one of the laboratory's business calendars (audit_review.hours). With "Only outside
business hours" every count holds only what happened outside them.

Every count is drawn as of the moment the first page was, each list by its own marker (`upto`
for Changes, the newest id for Access records and Sign-ins), and opens that list filtered to the
very rows it counted: the person, the period, the marker, the kind of event or read, and the
business hours when only activity outside them is counted. Counts only: never what was shown.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.urls import reverse

from accounts.models import SignInEvent
from core.models import AccessKind, AccessRecord, AuditEvent

from . import hours
from .models import Source
from .sources import (
    FAILED_ATTEMPTS,
    GROUPS,
    PATIENT_INFORMATION,
    _accounts,
    _day_start,
    _person,
    from_marker,
)

PAGE_SIZE = 50


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    # The list holding the rows it counts, and the Event or What choice that selects them there.
    source: Source
    action: str = ""


SIGNED_IN = SignInEvent.Kind.SIGNED_IN
MEASURES = (
    Measure("sign_ins", "Sign-ins", Source.SIGN_INS, SIGNED_IN),
    Measure("failed", "Failed or refused sign-ins", Source.SIGN_INS, FAILED_ATTEMPTS),
    Measure("tickets", "Tickets opened", Source.ACCESS, AccessKind.TICKET),
    Measure("downloads", "Files downloaded", Source.ACCESS, AccessKind.FILE),
    Measure("shown", "Patient information shown", Source.ACCESS, PATIENT_INFORMATION),
    Measure("changes", "Changes made", Source.CHANGES),
)
OUTSIDE_LABEL = "Outside business hours"

LISTS = {
    Source.CHANGES: "audit_review:changes",
    Source.ACCESS: "audit_review:access",
    Source.SIGN_INS: "audit_review:sign-ins",
}

# The order the People tab can be sorted in: by person, A to Z or back, or by a count.
DEFAULT_SORT = "person"
SORTS = {"person": "person"} | {measure.key: measure.label for measure in MEASURES}
SORTS["outside"] = OUTSIDE_LABEL.lower()


def sort_value(raw):
    """The sort the address asks for, if it is one of SORTS (with "-" for the reverse order)."""
    raw = raw or ""
    return raw if raw.removeprefix("-") in SORTS else DEFAULT_SORT


def sort_label(sort):
    """A recorded sort in words (sources.describe_filters)."""
    key = sort.removeprefix("-")
    label = SORTS.get(key, key).lower()
    if key == "person":
        return "person, Z to A" if sort.startswith("-") else "person, A to Z"
    return f"{label}, most first" if sort.startswith("-") else f"{label}, fewest first"


@dataclass
class Person:
    account_id: int
    name: str
    username: str
    organization: str
    # Whether the account still exists: a count of one that no longer does opens no list, whose
    # Person filter cannot choose it.
    exists: bool
    # Measure key -> how many, as the page shows them (all activity, or only outside the hours).
    counts: dict = field(default_factory=dict)
    # How many of the counted records fell outside the business hours; None when not judged.
    outside: int | None = None
    # What the page shows after the person, in column order (link): each count with the address
    # of the list holding the rows it counted, then the count outside the hours, which opens this
    # person's activity outside them.
    cells: list = field(default_factory=list)


@dataclass(frozen=True)
class CountCell:
    value: int
    label: str
    url: str = ""


@dataclass
class Summary:
    people: list
    # Measure key -> the sum over every person; "outside" too when judged.
    totals: dict
    judged: bool

    @property
    def measure_totals(self):
        """The totals in column order, without the count outside the hours."""
        return [self.totals[measure.key] for measure in MEASURES]


def markers(source_newest):
    """The moment each list is drawn as of, from `source_newest(source)` (sources.newest)."""
    return {source: source_newest(source) for source in LISTS}


def summarize(effective, marks):
    """The summary for `effective` (PeopleFilter.effective) as of `marks` (markers), unsorted."""
    start = _day_start(effective["date_from"])
    end = (
        _day_start(effective["date_to"] + timedelta(days=1))
        if effective["date_to"] < date.max
        else None
    )
    calendar = effective["calendar"]
    opening = (
        hours.open_hours(calendar, effective["date_from"], effective["date_to"])
        if calendar is not None
        else None
    )

    def scoped(records, account_column):
        records = records.filter(occurred_at__gte=start)
        if end is not None:
            records = records.filter(occurred_at__lt=end)
        if person := effective["person"]:
            records = records.filter(**{account_column: person.pk})
        if organization := effective["organization"]:
            records = records.filter(
                **{
                    f"{account_column}__in": get_user_model()
                    .objects.filter(client=organization)
                    .values("pk")
                }
            )
        if opening is not None:
            records = records.alias(outside=hours.Outside("occurred_at", opening))
        return records

    def counted(records, account_column, conditions):
        """Per account, each measure in `conditions` (key -> Q), and the same outside the hours."""
        totals = {key: Count("pk", filter=condition) for key, condition in conditions.items()}
        if opening is not None:
            totals |= {
                f"{key}_outside": Count("pk", filter=condition & Q(outside=True))
                for key, condition in conditions.items()
            }
        return records.values(account_column).annotate(**totals).order_by()

    failed = GROUPS[Source.SIGN_INS][FAILED_ATTEMPTS][1]
    shown = GROUPS[Source.ACCESS][PATIENT_INFORMATION][1]
    rows = []
    rows += [
        (row.pop("user_id"), row)
        for row in counted(
            scoped(
                SignInEvent.objects.filter(
                    pk__lte=marks[Source.SIGN_INS], kind__in=[SIGNED_IN, *failed]
                ),
                "user_id",
            ),
            "user_id",
            {"sign_ins": Q(kind=SIGNED_IN), "failed": Q(kind__in=failed)},
        )
    ]
    rows += [
        (row.pop("account_id"), row)
        for row in counted(
            scoped(
                AccessRecord.objects.filter(
                    pk__lte=marks[Source.ACCESS],
                    kind__in=[AccessKind.TICKET, AccessKind.FILE, *shown],
                ),
                "account_id",
            ),
            "account_id",
            {
                "tickets": Q(kind=AccessKind.TICKET),
                "downloads": Q(kind=AccessKind.FILE),
                "shown": Q(kind__in=shown),
            },
        )
    ]
    rows += [
        (row.pop("actor_id"), row)
        for row in counted(
            scoped(
                AuditEvent.objects.filter(occurred_at__lte=from_marker(marks[Source.CHANGES])),
                "actor_id",
            ),
            "actor_id",
            {"changes": Q()},
        )
    ]

    found = {}
    for account_id, counts in rows:
        found.setdefault(account_id, {}).update(counts)
    accounts = _accounts(set(found))
    people = []
    totals = {measure.key: 0 for measure in MEASURES}
    if opening is not None:
        totals["outside"] = 0
    for account_id, counts in found.items():
        every = {measure.key: counts.get(measure.key, 0) for measure in MEASURES}
        beyond = {measure.key: counts.get(f"{measure.key}_outside", 0) for measure in MEASURES}
        shown_counts = beyond if effective["outside_only"] else every
        if not any(shown_counts.values()):
            continue
        account = accounts.get(account_id)
        name, username = _person(accounts, account_id)
        person = Person(
            account_id=account_id,
            name=name,
            username=username,
            organization=account.client.name if account is not None and account.client else "",
            exists=account is not None,
            counts=shown_counts,
            outside=sum(beyond.values()) if opening is not None else None,
        )
        for key, value in shown_counts.items():
            totals[key] += value
        if person.outside is not None:
            totals["outside"] += person.outside
        people.append(person)
    return Summary(people, totals, judged=opening is not None)


def ordered(people, sort):
    """`people` in the order `sort` (sort_value) asks for; by name, then username, for ties."""

    def by_name(person):
        return (person.name.casefold(), person.username.casefold(), person.account_id)

    people = sorted(people, key=by_name)
    key = sort.removeprefix("-")
    if key == "person":
        return people[::-1] if sort.startswith("-") else people

    def count(person):
        return (person.outside or 0) if key == "outside" else person.counts[key]

    # Most first for "-key". A stable sort, reversed or not, keeps ties in name order.
    return sorted(people, key=count, reverse=sort.startswith("-"))


def link(people, effective, marks, people_url):
    """Give each person on a page its cells (Person.cells): a count of a person who no longer
    exists, and a count of none, opens nothing."""
    start, end = effective["date_from"].isoformat(), effective["date_to"].isoformat()
    calendar = effective["calendar"]
    for person in people:
        person.cells = []
        for measure in MEASURES:
            value = person.counts[measure.key]
            url = ""
            if value and person.exists:
                params = {
                    "person": person.account_id,
                    "date_from": start,
                    "date_to": end,
                    "upto": marks[measure.source],
                }
                if measure.action:
                    params["action"] = measure.action
                if effective["outside_only"]:
                    params["outside_hours"] = calendar.pk
                url = f"{reverse(LISTS[measure.source])}?{urlencode(params)}"
            person.cells.append(CountCell(value, measure.label, url))
        if person.outside is not None:
            url = ""
            if person.outside and person.exists and not effective["outside_only"]:
                params = {
                    "person": person.account_id,
                    "date_from": start,
                    "date_to": end,
                    "calendar": calendar.pk,
                    "activity": "outside",
                    **marker_params(marks),
                }
                url = f"{people_url}?{urlencode(params)}"
            person.cells.append(CountCell(person.outside, OUTSIDE_LABEL, url))


# The People tab's own links carry each list's marker under its own name.
MARKER_PARAMS = {
    Source.CHANGES: "upto_changes",
    Source.ACCESS: "upto_access",
    Source.SIGN_INS: "upto_sign_ins",
}


def marker_params(marks):
    return {name: str(marks[source]) for source, name in MARKER_PARAMS.items()}
