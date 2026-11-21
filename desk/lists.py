"""The request list: its filters, system views, columns and order, shared by the list page, the
previous/next links of the request page, saved views and the dashboard counts.

Every query starts from desk.selectors.staff_requests(user); filters only narrow it.
"""

from datetime import timedelta

from django import forms
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone

from clients.models import Client, ClientSite
from core.forms import SearchFilter, StaffChoiceField
from core.grids import GridRequest
from core.lookups import RemoteSelect
from service_levels.selectors import CLOCK_STATES, annotate_clock_state, filter_clock_state
from taxonomy.choices import Priority
from taxonomy.models import RequestCategory
from teams.models import Team

from .models import EntryKind, RequestEntry, ServiceRequest, Status
from .permissions import is_supervisor
from .selectors import staff_requests

ACTIVE = (Status.OPEN, Status.WAITING_CLIENT, Status.WAITING_THIRD_PARTY)
RECENTLY_CLOSED_DAYS = 7
DEFAULT_SORT = "-updated_at"
COLUMNS = {
    "number": "pk",
    "subject": "subject",
    "client": "client__name",
    "category": "category__name",
    "status": "status",
    "priority": "priority_rank",
    "team": "team__name",
    "assignee": "assignee__first_name",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "clock": "clock_due_at",  # annotate_clock_state: the earliest running due time
}
SEARCH_FIELDS = (
    "number",
    "subject",
    "client__name",
    "client__account_number",
    "contact__name",
    "reported_by",
    "client_reference",
)


def my_teams(user):
    return Team.objects.filter(memberships__user=user)


def _assignment_events():
    return RequestEntry.objects.filter(
        request=OuterRef("pk"),
        kind=EntryKind.EVENT,
        event_action__in=("created", "assigned"),
    )


def needs_review():
    """Complaints without a review that counts: never reviewed, or reviewed by someone who has
    been the request's assignee or has recorded or revised its decision
    (desk.services.review_counts, in SQL; the stored decision author never is the reviewer, by
    the complaint_review_independent constraint)."""
    reviewer_assigned = (
        _assignment_events()
        .annotate(assignee_ref=Cast(KeyTextTransform("assignee_id", "event_data"), IntegerField()))
        .filter(assignee_ref=OuterRef("complaint_review__reviewed_by"))
    )
    reviewer_decided = RequestEntry.objects.filter(
        request=OuterRef("pk"),
        kind=EntryKind.EVENT,
        event_action="complaint_decided",
        author=OuterRef("complaint_review__reviewed_by"),
    )
    return Q(is_complaint=True) & (
        Q(complaint_review__reviewed_at__isnull=True)
        | Q(assignee=F("complaint_review__reviewed_by"))
        | Q(Exists(reviewer_assigned))
        | Q(Exists(reviewer_decided))
    )


def awaiting_review_by(user):
    """Complaints whose decision is recorded and whose review this user may give: they are a
    supervisor, never recorded or revised the decision and have never been its assignee
    (desk.services.review_refusal, in SQL). Nothing for anyone else."""
    if not is_supervisor(user):
        return Q(pk__in=[])
    was_assignee = _assignment_events().filter(event_data__assignee_id=user.pk)
    decided = RequestEntry.objects.filter(
        request=OuterRef("pk"),
        kind=EntryKind.EVENT,
        event_action="complaint_decided",
        author=user,
    )
    return (
        needs_review()
        & ~Q(complaint_review__decision="")
        & Q(complaint_review__isnull=False)
        & ~Q(complaint_review__decided_by=user)
        & ~Q(assignee=user)
        & ~Q(Exists(was_assignee))
        & ~Q(Exists(decided))
    )


def _view_conditions(user):
    now = timezone.now()
    return {
        "my-open": Q(assignee=user, status__in=ACTIVE),
        "unassigned": Q(team__in=my_teams(user), assignee__isnull=True, status__in=ACTIVE),
        "my-teams": Q(team__in=my_teams(user), status__in=ACTIVE),
        "waiting-client": Q(status=Status.WAITING_CLIENT),
        "waiting-third-party": Q(status=Status.WAITING_THIRD_PARTY),
        "overdue": Q(status__in=ACTIVE),  # narrowed by the clock filter below
        "complaints-review": needs_review() & ~Q(status=Status.CLOSED),
        "resolved": Q(status=Status.RESOLVED),
        "recently-closed": Q(
            status=Status.CLOSED, closed_at__gte=now - timedelta(days=RECENTLY_CLOSED_DAYS)
        ),
        "all": Q(),
    }


# key: (label, description for page help and the view picker)
SYSTEM_VIEWS = {
    "my-open": ("My open", "Assigned to you, not resolved or closed."),
    "unassigned": ("Unassigned in my teams", "In your teams, with nobody assigned."),
    "my-teams": ("My teams", "Everything open or waiting in your teams."),
    "waiting-client": ("Waiting on client", "The client owes the next step."),
    "waiting-third-party": ("Waiting on third party", "An outsider owes the next step."),
    "overdue": ("Overdue", "Open or waiting tickets with a service clock past due."),
    "complaints-review": (
        "Complaints awaiting review",
        "Complaints whose independent review is still to come.",
    ),
    "resolved": ("Resolved not closed", "Resolved; close them when nothing more is owed."),
    "recently-closed": ("Recently closed", f"Closed in the last {RECENTLY_CLOSED_DAYS} days."),
    "all": ("All tickets", "Every ticket you may read."),
}
DEFAULT_VIEW = "my-open"

STATUS_FILTERS = [
    ("", "Any status"),
    ("active", "Open or waiting"),
    *Status.choices,
]
ASSIGNEE_FILTERS = [("", "Anyone"), ("me", "Me"), ("none", "Nobody")]
COMPLAINT_FILTERS = [
    ("", "Any"),
    ("yes", "Complaints"),
    ("no", "Not complaints"),
    ("review", "Awaiting review"),
]
CLOCK_LABELS = {
    "overdue": "Overdue",
    "due_soon": "Due soon",
    "paused": "Paused",
    "on_track": "On track",
    "met": "Met",
    "missed": "Missed",
    "none": "No clock",
}


class RequestFilter(SearchFilter):
    # Carried by the toolbar's hidden inputs (grid_toolbar hidden=filter_form.hidden_fields), so
    # Apply keeps the system view, the sort and the page size.
    view = forms.ChoiceField(
        required=False,
        choices=[(key, label) for key, (label, _) in SYSTEM_VIEWS.items()],
        widget=forms.HiddenInput,
    )
    sort = forms.CharField(required=False, widget=forms.HiddenInput)
    per_page = forms.CharField(required=False, widget=forms.HiddenInput)
    saved = forms.CharField(required=False, widget=forms.HiddenInput)
    status = forms.ChoiceField(required=False, choices=STATUS_FILTERS)
    team = forms.ModelChoiceField(
        required=False, queryset=Team.objects.all(), empty_label="Any team"
    )
    assignee = forms.ChoiceField(required=False, choices=ASSIGNEE_FILTERS, label="Assigned to")
    person = StaffChoiceField(required=False, label="Person", empty_label="Any person")
    category = forms.ModelChoiceField(
        required=False,
        queryset=RequestCategory.objects.filter(parent__isnull=False),
        empty_label="Any category",
    )
    priority = forms.ChoiceField(required=False, choices=[("", "Any priority"), *Priority.choices])
    client = forms.ModelChoiceField(
        required=False,
        queryset=Client.objects.all(),
        widget=RemoteSelect("clients"),
        label="Organization",
        empty_label="Any organization",
    )
    site = forms.ModelChoiceField(
        required=False,
        queryset=ClientSite.objects.select_related("client"),
        widget=RemoteSelect("desk-sites"),
        empty_label="Any site",
    )
    complaint = forms.ChoiceField(required=False, choices=COMPLAINT_FILTERS, label="Complaints")
    clock = forms.ChoiceField(
        required=False,
        choices=[("", "Any clock"), *((s, CLOCK_LABELS[s]) for s in CLOCK_STATES)],
        label="Service clock",
    )
    created_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    created_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_ranges = ("created",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["created_from"].label = "Created from"
        self.fields["created_to"].label = "Created to"


# Filters a saved view may store: the list's own and its system view, not sort or page size.
SAVED_FILTERS = tuple(
    name for name in RequestFilter.base_fields if name not in ("sort", "per_page", "saved")
)
INLINE = ("status", "assignee")
MORE = (
    "team",
    "person",
    "category",
    "priority",
    "client",
    "site",
    "complaint",
    "clock",
    "created_from",
    "created_to",
)
FILTER_FIELDS = {"team": "team", "person": "assignee", "category": "category", "client": "client"}


def base_queryset(user):
    return (
        staff_requests(user)
        .select_related("client", "site", "contact", "category", "team", "assignee")
        .annotate(
            priority_rank=Case(
                When(priority=Priority.URGENT, then=Value(4)),
                When(priority=Priority.HIGH, then=Value(3)),
                When(priority=Priority.NORMAL, then=Value(2)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
    )


def refine(user):
    """list_response's refine for this user: the system view and the non-trivial filters."""

    def apply(queryset, cleaned):
        view = cleaned.get("view") or DEFAULT_VIEW
        queryset = queryset.filter(_view_conditions(user)[view])
        if view == "overdue":
            queryset = filter_clock_state(queryset, "overdue")
        status = cleaned.get("status")
        if status == "active":
            queryset = queryset.filter(status__in=ACTIVE)
        elif status:
            queryset = queryset.filter(status=status)
        if cleaned.get("assignee") == "me":
            queryset = queryset.filter(assignee=user)
        elif cleaned.get("assignee") == "none":
            queryset = queryset.filter(assignee__isnull=True)
        if cleaned.get("priority"):
            queryset = queryset.filter(priority=cleaned["priority"])
        if cleaned.get("site"):
            queryset = queryset.filter(site=cleaned["site"])
        complaint = cleaned.get("complaint")
        if complaint == "yes":
            queryset = queryset.filter(is_complaint=True)
        elif complaint == "no":
            queryset = queryset.filter(is_complaint=False)
        elif complaint == "review":
            queryset = queryset.filter(awaiting_review_by(user))
        if cleaned.get("clock"):
            queryset = filter_clock_state(queryset, cleaned["clock"])
        return annotate_clock_state(queryset)

    return apply


def ordered_requests(user, params):
    """The requests the list shows for `params` (a QueryDict), in its order, or none when a
    filter is invalid. Used for previous/next on the request page."""
    try:
        grid = GridRequest.parse(params, COLUMNS, DEFAULT_SORT)
    except ValueError:
        return ServiceRequest.objects.none()
    data = params.copy()
    data["q"] = grid.query
    form = RequestFilter(data)
    if not form.is_valid():
        return ServiceRequest.objects.none()
    queryset = base_queryset(user)
    cleaned = form.cleaned_data
    if cleaned["q"]:
        condition = Q()
        for field in SEARCH_FIELDS:
            condition |= Q(**{f"{field}__icontains": cleaned["q"]})
        queryset = queryset.filter(condition)
    for param, field in FILTER_FIELDS.items():
        if cleaned.get(param):
            queryset = queryset.filter(**{field: cleaned[param]})
    for suffix, lookup in (("from", "gte"), ("to", "lte")):
        if value := cleaned.get(f"created_{suffix}"):
            queryset = queryset.filter(**{f"created_at__date__{lookup}": value})
    queryset = refine(user)(queryset, cleaned)
    field = COLUMNS[grid.sort.removeprefix("-")]
    return queryset.order_by(("-" if grid.sort.startswith("-") else "") + field, "pk")


def view_queryset(user, params):
    """The requests a list opened with `params` (a dict of filters) shows this user, unordered:
    the dashboard's queues count exactly what their linked list shows."""
    form = RequestFilter(params)
    if not form.is_valid():
        return ServiceRequest.objects.none()
    queryset = staff_requests(user)
    for param, field in FILTER_FIELDS.items():
        if form.cleaned_data.get(param):
            queryset = queryset.filter(**{field: form.cleaned_data[param]})
    return refine(user)(queryset, form.cleaned_data)


def view_count(user, key):
    """How many requests a system view shows this user (the view picker)."""
    return view_queryset(user, {"view": key}).count()
