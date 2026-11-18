"""Staff pages of the nonconformance module: the two lists, the record page and its action forms,
and opening a record from a complaint ticket.

Every view starts from nonconformance.selectors (staff only; 404 while the module is off) and
every change goes through nonconformance.services, which checks the role, the record's state and
its revision again under lock. A page and its form are drawn from one reading of the record: the
hidden expected_revision is the revision of the record the page shows, and a refused save shows
the service's message with the input kept. Tickets are read only through
desk.selectors.staff_requests, so a ticket the reader may not open is never shown or linked.
Messages are fixed text, and saving redirects to the record's address only: nothing typed into a
record reaches a message or a redirect. The words typed into a list's search go into its address,
as on every list (docs/product.md, "Nonconformance and corrective action").
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers

from core.ids import whole_number
from core.lists import list_response
from core.models import AuditEvent
from core.modules import ModuleOff
from desk import services as desk_services
from desk.forms import person_name
from desk.models import NUMBER_PREFIX as TICKET_PREFIX
from desk.models import ComplaintReview
from desk.permissions import is_supervisor
from desk.selectors import staff_requests

from . import selectors, services
from .forms import (
    NO_PATIENT_DATA,
    SHOW_CLOSED,
    SHOW_MINE,
    SHOW_OPEN,
    SHOW_OVERDUE,
    ActionFilter,
    ActionForm,
    CloseForm,
    DetailsForm,
    EffectivenessForm,
    FinishActionForm,
    ImpactForm,
    InvestigationForm,
    LinkTicketForm,
    NonconformanceCreateForm,
    NonconformanceFilter,
    OpenFromComplaintForm,
    OwnerForm,
    ReasonForm,
    StatusForm,
)
from .models import (
    CLOSABLE_RESULTS,
    ActionStatus,
    ClientNotice,
    CorrectiveAction,
    Effectiveness,
    Impact,
    Severity,
    Source,
    Status,
)

LIST_TITLE = "Nonconformances"
ACTIONS_TITLE = "Corrective actions"
NO_TICKET = "No ticket with that number you can open."
STATUS_ORDER = [Status.OPEN, Status.INVESTIGATING, Status.CORRECTIVE_ACTION]
STATUS_ORDER += [Status.VERIFICATION, Status.CLOSED]
SEVERITY_ORDER = [Severity.MINOR, Severity.MAJOR, Severity.CRITICAL]


def _rank(field, order):
    return Case(
        *[When(**{field: value}, then=Value(rank)) for rank, value in enumerate(order)],
        default=Value(len(order)),
        output_field=IntegerField(),
    )


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _record_or_404(user, pk, with_action_counts=False):
    """The record, read once for the page and its form (404 while the module is off). With
    `with_action_counts`, its open, done and cancelled actions are counted in the same statement
    (open_action_count, done_action_count, cancelled_action_count)."""
    records = selectors.staff_nonconformances(user)
    if with_action_counts:
        records = records.annotate(
            **{
                f"{status}_action_count": Count("actions", filter=Q(actions__status=status))
                for status in ActionStatus.values
            }
        )
    record = (
        records.select_related(
            "owner",
            "raised_by",
            "closed_by",
            "impact_recorded_by",
            "client_told_by",
            "effectiveness_checked_by",
        )
        .filter(pk=pk)
        .first()
    )
    if record is None:
        raise Http404("No such nonconformance.")
    return record


def _record_url(nc):
    return reverse("nonconformance:detail", args=[nc.pk])


def _crumbs(nc, title):
    return [
        (LIST_TITLE, reverse("nonconformance:list")),
        (nc.number, _record_url(nc)),
        (title, None),
    ]


WORK_REFUSAL = "The owner or a supervisor works this nonconformance."


def _form_response(
    request,
    nc,
    form,
    *,
    title,
    submit_label,
    apply,
    success,
    destructive=False,
    notes=(),
    template="nonconformance/action_form.html",
    extra=None,
    unavailable="",
    allowed=True,
    refusal="",
    gone="",
):
    """Render and handle one action form of the record page (the shared form dialog; a page
    without script). `nc` is the one reading the form was drawn from; a refused save shows the
    service's message on the form with the input kept. `unavailable` is why the record's current
    state makes the change impossible (from the same reading): the form is then not offered, and
    opening it returns to the record with that reason (a POST is still decided by the service).

    `allowed` is whether the reader may make this change on this reading; otherwise `refusal` is
    the 403's reason. A form posted from an earlier reading of the record (a revision below this
    one) is shown again with the stale-record message and the input kept, even when this reading
    no longer allows the change: whoever drew it may have been the owner then. Any other posted
    revision (none, not a number, a later one) is decided here. `gone` is the message when the
    service finds what the form concerns no longer there for the reader (a linked ticket out of
    reach): the form is shown again with that message and nothing of it. `extra` may be a
    callable, evaluated when the form is drawn, so a refusal draws only what the reader may still
    read. A service refusing the reader as a staff member (deactivated, no role left) answers 403;
    the module switched off answers 404."""
    if not allowed:
        if _posted_earlier(request, nc.revision):
            form.is_valid()
            form.add_error(None, services.STALE)
            return _render_form(
                request, nc, form, title, submit_label, destructive, notes, template, extra
            )
        raise PermissionDenied(refusal)
    if unavailable and request.method != "POST":
        messages.info(request, unavailable)
        return redirect(_record_url(nc))
    if request.method == "POST" and form.is_valid():
        try:
            apply(form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        except PermissionDenied as exc:  # the record changed hands since the page was drawn
            if not _still_staff(request.user):
                raise
            form.add_error(None, str(exc))
        except ModuleOff:
            raise
        except Http404:
            if not gone:
                raise
            # What the form concerned is out of the reader's reach now: say so, keep the input,
            # and draw nothing of it again.
            form.add_error(None, gone)
            extra = {}
        else:
            messages.success(request, success)
            return redirect(_record_url(nc))
    return _render_form(request, nc, form, title, submit_label, destructive, notes, template, extra)


def _posted_earlier(request, current):
    """Whether a POST carries the revision of an earlier reading: a whole number below `current`."""
    if request.method != "POST":
        return False
    posted = whole_number(request.POST.get("expected_revision"))
    return posted is not None and 1 <= posted < current  # revisions start at 1


def _still_staff(user):
    """Whether the account, read now, is still an active staff account with a role."""
    from accounts.models import User
    from accounts.roles import ROLES, has_role

    current = User.objects.filter(pk=user.pk, is_active=True, client__isnull=True).first()
    return current is not None and has_role(current, *ROLES)


def _render_form(request, nc, form, title, submit_label, destructive, notes, template, extra):
    if callable(extra):
        extra = extra()
    response = render(
        request,
        template,
        {
            "page_title": title,
            "nc": nc,
            "form": form,
            "submit_label": submit_label,
            "destructive": destructive,
            "notes": notes,
            "cancel_url": _record_url(nc),
            "crumbs": _crumbs(nc, title),
            **(extra or {}),
        },
    )
    add_never_cache_headers(response)  # a refused page shows what was typed again
    return response


def _revision(nc):
    return {"expected_revision": nc.revision}


def _closed(nc):
    """Why an open record's change is unavailable now: "" unless it is closed."""
    return services.CLOSED if nc.is_closed else ""


def _finished(nc, action):
    if nc.is_closed:
        return services.CLOSED
    return "" if action.status == ActionStatus.OPEN else "This action is already finished."


# ------------------------------------------------------------------------------ lists


NC_COLUMNS = {
    "number": "pk",
    "title": "title",
    "status": "status_rank",
    "severity": "severity_rank",
    "source": "source",
    "owner": "owner__first_name",
    "raised_at": "raised_at",
    "overdue": "has_overdue_actions",
}


def _nc_refine(user):
    def refine(queryset, cleaned):
        show = cleaned.get("show") or SHOW_OPEN
        if show == SHOW_OPEN:
            return selectors.active(queryset)
        if show == SHOW_MINE:
            return selectors.mine(selectors.active(queryset), user)
        if show == SHOW_CLOSED:
            return queryset.filter(status=Status.CLOSED)
        return queryset

    return refine


@login_required
def nonconformance_list(request):
    user = request.user
    records = (
        selectors.with_action_counts(selectors.staff_nonconformances(user))
        .select_related("owner")
        .annotate(
            status_rank=_rank("status", STATUS_ORDER),
            severity_rank=_rank("severity", SEVERITY_ORDER),
        )
    )
    return list_response(
        request,
        queryset=records,
        columns=NC_COLUMNS,
        default_sort="-raised_at",
        form_class=NonconformanceFilter,
        search_fields=("number", "title", "area"),
        filter_fields={"status": "status", "severity": "severity", "source": "source"},
        template="nonconformance/nonconformance_list.html",
        title=LIST_TITLE,
        inline=("show", "status", "severity"),
        more=("source",),
        placeholder="Number, title or area",
        refine=_nc_refine(user),
    )


ACTION_COLUMNS = {
    "number": "nonconformance_id",
    "description": "description",
    "assignee": "assignee__first_name",
    "due_on": "due_on",
    "status": "status",
}


def _action_refine(user):
    def refine(queryset, cleaned):
        show = cleaned.get("show") or SHOW_OPEN
        if show == SHOW_OPEN:
            return queryset.filter(status=ActionStatus.OPEN)
        if show == SHOW_MINE:
            return queryset.filter(status=ActionStatus.OPEN, assignee=user)
        if show == SHOW_OVERDUE:
            return selectors.overdue(queryset)
        return queryset

    return refine


@login_required
def action_list(request):
    user = request.user
    return list_response(
        request,
        queryset=selectors.staff_actions(user).select_related("nonconformance", "assignee"),
        columns=ACTION_COLUMNS,
        default_sort="due_on",
        form_class=ActionFilter,
        search_fields=("nonconformance__number", "description"),
        filter_fields={"status": "status"},
        template="nonconformance/action_list.html",
        title=ACTIONS_TITLE,
        inline=("show", "status"),
        placeholder="Nonconformance number or action",
        refine=_action_refine(user),
        extra={"today": timezone.localdate()},
    )


# ------------------------------------------------------------------------------ the record page

HISTORY_TITLES = {
    "nonconformance.raised": "Raised",
    "nonconformance.updated": "Details edited",
    "nonconformance.owner_changed": "Owner changed",
    "nonconformance.investigation_recorded": "Investigation recorded",
    "nonconformance.impact_recorded": "Impact on reported results recorded",
    "nonconformance.status_changed": "Status changed",
    "nonconformance.effectiveness_recorded": "Effectiveness check recorded",
    "nonconformance.effectiveness_cleared": "Effectiveness check cleared",
    "nonconformance.closed": "Closed",
    "nonconformance.reopened": "Reopened",
    "nonconformance.ticket_linked": "Ticket linked",
    "nonconformance.ticket_unlinked": "Linked ticket removed",
    "corrective_action.added": "Action added",
    "corrective_action.updated": "Action edited",
    "corrective_action.done": "Action marked done",
    "corrective_action.cancelled": "Action cancelled",
}
# Coded values only: free text in the snapshots (what happened, notes) is never shown here.
CODED = {
    "status": ("Status", {**dict(Status.choices), **dict(ActionStatus.choices)}),
    "severity": ("Severity", dict(Severity.choices)),
    "source": ("Source", dict(Source.choices)),
    "impact": ("Decision on reported results", dict(Impact.choices)),
    "client_notice": ("Client told", dict(ClientNotice.choices)),
    "effectiveness": ("Effectiveness", dict(Effectiveness.choices)),
    "due_on": ("Due date", {}),
}
PEOPLE = {"owner": "Owner", "assignee": "Person"}


def _coded(names, value):
    """A coded value as the page names it; a cleared one is "Not recorded"."""
    if value in ("", None):
        return "Not recorded"
    return names.get(value, value)


TICKET_EVENTS = ("nonconformance.ticket_linked", "nonconformance.ticket_unlinked")


def _history(user, nc, actions):
    """The record's audit events and those of its actions, oldest first, as history items. The
    linking or removal of a ticket the reader may not open now is left out entirely, its reason
    included, like the ticket itself (selectors.linked_tickets)."""
    from accounts.models import User

    action_ids = [str(action.pk) for action in actions]
    events = list(
        AuditEvent.objects.filter(
            Q(object_type="nonconformance.nonconformance", object_id=str(nc.pk))
            | Q(object_type="nonconformance.correctiveaction", object_id__in=action_ids)
        )
        .select_related("actor")
        .order_by("occurred_at")
    )
    ticket_ids = {
        (event.after or event.before or {}).get("request_id")
        for event in events
        if event.action in TICKET_EVENTS
    }
    readable = set(
        staff_requests(user)
        .filter(pk__in=[i for i in ticket_ids if i])
        .values_list("pk", flat=True)
    )
    events = [
        event
        for event in events
        if event.action not in TICKET_EVENTS
        or (event.after or event.before or {}).get("request_id") in readable
    ]
    person_ids = {
        data.get(key)
        for event in events
        for data in (event.before or {}, event.after or {})
        for key in PEOPLE
        if data.get(key)
    }
    people = {user.pk: person_name(user) for user in User.objects.filter(pk__in=person_ids)}
    items = []
    for event in events:
        before, after = event.before or {}, event.after or {}
        changes = []
        if before:  # a change: the coded values it moved, before and after
            for key, (label, names) in CODED.items():
                if key in after and before.get(key) != after[key]:
                    changes.append(
                        (label, _coded(names, before.get(key)), _coded(names, after[key]))
                    )
            for key, label in PEOPLE.items():
                if key in after and before.get(key) != after[key]:
                    old = people.get(before.get(key), "Nobody")
                    changes.append((label, old, people.get(after[key], "Nobody")))
        detail = ""
        if event.object_type == "nonconformance.correctiveaction":
            values = after or before
            person = people.get(values.get("assignee"), "")
            detail = f"Action due {values.get('due_on', '')}" + (f", {person}" if person else "")
        items.append(
            {
                "title": HISTORY_TITLES.get(event.action, event.action),
                "at": event.occurred_at,
                "actor": event.actor,
                "reason": event.reason,
                "changes": changes,
                "detail": detail,
            }
        )
    return items


@login_required
def nonconformance_detail(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    # Related rows are read after the record: anything newer than the revision this page posts
    # is refused as stale by the service, never saved over.
    actions = list(nc.actions.select_related("assignee", "finished_by"))
    today = timezone.localdate()
    closed = nc.is_closed
    work = not closed and services.can_work(user, nc)
    supervisor = is_supervisor(user)
    open_actions = [action for action in actions if action.status == ActionStatus.OPEN]
    overdue_count = sum(1 for action in open_actions if action.due_on < today)
    for action in actions:
        is_open = not closed and action.status == ActionStatus.OPEN
        action.can_change = is_open and work
        action.can_finish = is_open and services.can_finish_action(user, nc, action)
    links = list(selectors.linked_tickets(user, nc))
    for link in links:
        link.can_remove = work and link.request_id != nc.source_request_id
    opened_from = (
        staff_requests(user).filter(pk=nc.source_request_id).first()
        if nc.source_request_id
        else None
    )
    close_refusal = (
        services.close_refusal(user, nc, len(open_actions)) if supervisor and not closed else ""
    )
    can_close = supervisor and not closed and not close_refusal
    in_verification = nc.status == Status.VERIFICATION
    if closed:
        next_step = "reopen" if supervisor else ""
    elif can_close:
        next_step = "close"
    elif work and in_verification and nc.effectiveness not in CLOSABLE_RESULTS:
        next_step = "effectiveness"
    elif work:
        next_step = "status"
    else:
        next_step = ""
    toc = [
        ("details-heading", "Details"),
        ("investigation-heading", "Investigation"),
        ("impact-heading", "Impact on reported results"),
        ("actions-heading", f"Actions ({len(actions)})"),
        ("tickets-heading", f"Linked tickets ({len(links)})"),
        ("effectiveness-heading", "Effectiveness check"),
        ("closing-heading", "Closing"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "nonconformance/nonconformance_detail.html",
        {
            "page_title": f"{nc.number} {nc.title}",
            "nc": nc,
            "actions": actions,
            "open_count": len(open_actions),
            "overdue_count": overdue_count,
            "overdue_label": (
                f"{overdue_count} overdue action{'s' if overdue_count != 1 else ''}"
                if overdue_count and not closed
                else ""
            ),
            "today": today,
            "links": links,
            "opened_from": opened_from,
            "can_work": work,
            "is_supervisor": supervisor,
            "is_closed": closed,
            "in_verification": in_verification,
            "close_refusal": close_refusal,
            "next_step": next_step,
            "has_own_actions": any(action.can_finish for action in actions),
            "history": _history(user, nc, actions),
            "toc": toc,
        },
    )


# ------------------------------------------------------------------------------ raising a record


@login_required
def nonconformance_create(request):
    user = request.user
    selectors.staff_nonconformances(user)  # staff only; 404 while the module is off
    # A supervisor chooses an owner. A posted owner is kept on the form even if the poster is no
    # longer a supervisor, so the service refuses it with the input kept rather than dropping it.
    can_choose_owner = is_supervisor(user) or bool(request.POST.get("owner"))
    form = NonconformanceCreateForm(request.POST or None, can_choose_owner=can_choose_owner)
    list_url = reverse("nonconformance:list")
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            nc = services.raise_nonconformance(
                actor=user,
                title=data["title"],
                description=data["description"],
                source=data["source"],
                severity=data["severity"],
                area=data["area"],
                containment=data["containment"],
                owner=data.get("owner"),
            )
        except ValidationError as exc:
            _errors(form, exc)
        except PermissionDenied as exc:
            if not _still_staff(user):
                raise
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Nonconformance {nc.number} raised.")
            return redirect(_record_url(nc))
    response = render(
        request,
        "nonconformance/nonconformance_form.html",
        {
            "page_title": "New nonconformance",
            "form": form,
            "submit_label": "Raise nonconformance",
            "cancel_url": list_url,
            "crumbs": [(LIST_TITLE, list_url), ("New nonconformance", None)],
        },
    )
    add_never_cache_headers(response)
    return response


@login_required
def from_ticket(request, pk):
    """Open the nonconformance of a complaint ticket (its assignee or a supervisor). Nothing of the
    ticket is copied: the record is readable by every staff role, the ticket may not be."""
    user = request.user
    records = selectors.staff_nonconformances(user)  # staff only; 404 while the module is off
    ticket = staff_requests(user).filter(pk=pk).first()
    posted = request.method == "POST"
    # A form posted from an earlier reading of the ticket (a lower revision) is answered by the
    # service (stale, already opened, no longer a complaint, no longer yours) with the input kept,
    # while the reader may still open the ticket; this reading decides only the rest.
    stale = ticket is not None and _posted_earlier(request, ticket.revision)
    if ticket is None or not (ticket.is_complaint or stale):
        raise Http404("No such complaint.")
    if not (is_supervisor(user) or user.pk == ticket.assignee_id or stale):
        raise PermissionDenied("The assignee or a supervisor records the complaint.")
    ticket_url = reverse("desk:request-detail", args=[ticket.pk])
    existing = records.filter(source_request=ticket).first()
    if existing is not None and not posted:
        messages.info(request, "This complaint already has a nonconformance.")
        return redirect(_record_url(existing))
    review = ComplaintReview.objects.filter(request=ticket).first()
    if request.method != "POST" and desk_services.review_counts(ticket, review):
        messages.info(
            request,
            "This complaint has been reviewed; its record no longer changes. Raise a "
            "nonconformance and link the ticket instead.",
        )
        return redirect(ticket_url)
    form = OpenFromComplaintForm(
        request.POST or None, initial={"expected_revision": ticket.revision}
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            nc = services.open_from_complaint(
                actor=user,
                request=ticket,
                expected_revision=data["expected_revision"],
                title=data["title"],
                description=data["description"],
                severity=data["severity"],
                area=data["area"],
                containment=data["containment"],
            )
        except ValidationError as exc:
            _errors(form, exc)
        except PermissionDenied as exc:
            if not _still_staff(user):
                raise
            form.add_error(None, str(exc))
        except ModuleOff:
            raise
        except Http404:  # the ticket left the reader's reach meanwhile
            raise Http404("No such complaint.") from None
        else:
            messages.success(request, f"Nonconformance {nc.number} opened.")
            return redirect(_record_url(nc))
    title = "Open nonconformance"
    response = render(
        request,
        "nonconformance/nonconformance_form.html",
        {
            "page_title": title,
            "form": form,
            "ticket": ticket,
            "submit_label": title,
            "cancel_url": ticket_url,
            "crumbs": [
                ("Tickets", reverse("desk:request-list")),
                (ticket.number, ticket_url),
                (title, None),
            ],
        },
    )
    add_never_cache_headers(response)
    return response


# ------------------------------------------------------------------------------ working a record


@login_required
def nonconformance_edit(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = DetailsForm(
        request.POST or None,
        initial={
            **_revision(nc),
            "title": nc.title,
            "description": nc.description,
            "source": nc.source,
            "severity": nc.severity,
            "area": nc.area,
        },
    )

    def apply(data):
        services.update_details(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title=f"Edit {nc.number}",
        submit_label="Save changes",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Details saved.",
        notes=(NO_PATIENT_DATA,),
        unavailable=_closed(nc),
    )


@login_required
def nonconformance_owner(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = OwnerForm(request.POST or None, initial={**_revision(nc), "owner": nc.owner_id})
    title = "Change owner" if nc.owner_id else "Choose owner"

    def apply(data):
        services.set_owner(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title=title,
        submit_label=title,
        apply=apply,
        allowed=is_supervisor(user),
        refusal="A supervisor chooses the owner.",
        success="Owner saved.",
        notes=("The owner works the record with the supervisors and cannot close it.",),
        unavailable=_closed(nc),
    )


@login_required
def nonconformance_status(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = StatusForm(request.POST or None, current=nc.status, initial=_revision(nc))

    def apply(data):
        services.change_status(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Change status",
        submit_label="Change status",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Status changed.",
        extra={"current_status": nc.get_status_display()},
        unavailable=_closed(nc),
    )


@login_required
def nonconformance_investigation(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = InvestigationForm(
        request.POST or None,
        initial={
            **_revision(nc),
            "containment": nc.containment,
            "root_cause": nc.root_cause,
            "similar_cases": nc.similar_cases,
        },
    )

    def apply(data):
        services.record_investigation(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Record investigation",
        submit_label="Save investigation",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Investigation saved.",
        notes=(NO_PATIENT_DATA,),
        unavailable=_closed(nc),
    )


@login_required
def nonconformance_impact(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = ImpactForm(
        request.POST or None,
        initial={
            **_revision(nc),
            "impact": nc.impact,
            "impact_assessment": nc.impact_assessment,
            "client_notice": nc.client_notice,
            "client_told_on": nc.client_told_on,
            "client_told_how": nc.client_told_how,
            "client_told_by": nc.client_told_by_id,
        },
    )

    def apply(data):
        services.record_impact(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Record impact",
        submit_label="Save impact",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Impact on reported results saved.",
        notes=(NO_PATIENT_DATA,),
        template="nonconformance/impact_form.html",
        unavailable=_closed(nc),
    )


@login_required
def nonconformance_effectiveness(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = EffectivenessForm(request.POST or None, initial=_revision(nc))

    def apply(data):
        services.record_effectiveness(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Record effectiveness",
        submit_label="Record effectiveness",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Effectiveness check recorded.",
        notes=(
            "Effective needs every action done or cancelled. Not effective keeps the record "
            "open: add actions or move it back.",
        ),
        unavailable=_closed(nc)
        or (
            ""
            if nc.status == Status.VERIFICATION
            else "Move it to Verification before checking effectiveness."
        ),
    )


@login_required
def nonconformance_close(request, pk):
    user = request.user
    # What the supervisor closes is shown on the form, from the reading whose revision it posts:
    # the record and its action counts in one statement.
    nc = _record_or_404(user, pk, with_action_counts=True)
    form = CloseForm(request.POST or None, initial=_revision(nc))
    open_actions = nc.open_action_count

    def apply(data):
        services.close(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Close nonconformance",
        submit_label="Close nonconformance",
        apply=apply,
        allowed=is_supervisor(user),
        refusal="A supervisor closes a nonconformance.",
        success="Nonconformance closed.",
        extra={"closing": True},
        unavailable=services.close_refusal(user, nc, open_actions),
    )


@login_required
def nonconformance_reopen(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = ReasonForm(request.POST or None, initial=_revision(nc))

    def apply(data):
        services.reopen(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Reopen nonconformance",
        submit_label="Reopen nonconformance",
        apply=apply,
        allowed=is_supervisor(user),
        refusal="A supervisor reopens a nonconformance.",
        success="Nonconformance reopened.",
        notes=(
            "It returns to Investigating. Its closing and effectiveness check stay in the history.",
        ),
        unavailable=services.reopen_refusal(user, nc),
    )


# ------------------------------------------------------------------------------ actions


@login_required
def action_add(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = ActionForm(request.POST or None, initial=_revision(nc))

    def apply(data):
        services.add_action(actor=user, nc=nc, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Add action",
        submit_label="Add action",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Action added.",
        unavailable=_closed(nc),
    )


def _record_and_action(user, pk, action_pk):
    """(record, action) read together in one statement, so the form's revision and the action it
    shows come from the same moment (staff only; 404 while the module is off)."""
    records = selectors.staff_nonconformances(user)
    action = (
        CorrectiveAction.objects.filter(
            pk=action_pk, nonconformance_id=pk, nonconformance__in=records
        )
        .select_related("assignee", "nonconformance", "nonconformance__owner")
        .first()
    )
    if action is None:
        raise Http404("No such action.")
    return action.nonconformance, action


@login_required
def action_edit(request, pk, action_pk):
    user = request.user
    nc, action = _record_and_action(user, pk, action_pk)
    form = ActionForm(
        request.POST or None,
        editing=True,
        initial={
            **_revision(nc),
            "description": action.description,
            "assignee": action.assignee_id,
            "due_on": action.due_on,
        },
    )

    def apply(data):
        services.update_action(actor=user, nc=nc, action=action, **data)

    return _form_response(
        request,
        nc,
        form,
        title="Edit action",
        submit_label="Save changes",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Action saved.",
        notes=(
            "A due date already past may stay as it is; a new one is today or later. Changing "
            "an action clears a recorded effectiveness check.",
        ),
        unavailable=_finished(nc, action),
    )


def _finish(request, pk, action_pk, *, done):
    user = request.user
    nc, action = _record_and_action(user, pk, action_pk)
    form = FinishActionForm(request.POST or None, done=done, initial=_revision(nc))
    title = "Mark done" if done else "Cancel action"

    def apply(data):
        services.finish_action(actor=user, nc=nc, action=action, done=done, **data)

    return _form_response(
        request,
        nc,
        form,
        title=title,
        submit_label=title,
        apply=apply,
        allowed=(
            services.can_finish_action(user, nc, action) if done else services.can_work(user, nc)
        ),
        refusal=(
            "Its assignee, the owner or a supervisor marks an action done."
            if done
            else "The owner or a supervisor cancels an action."
        ),
        success="Action marked done." if done else "Action cancelled.",
        destructive=not done,
        extra={"action": action},
        unavailable=_finished(nc, action),
    )


@login_required
def action_done(request, pk, action_pk):
    return _finish(request, pk, action_pk, done=True)


@login_required
def action_cancel(request, pk, action_pk):
    return _finish(request, pk, action_pk, done=False)


# ------------------------------------------------------------------------------ linked tickets


def _ticket_number(raw):
    """The ticket number typed (REQ-000123, req-123 or 123) in its stored form, or None."""
    digits = (raw or "").strip().upper().removeprefix(TICKET_PREFIX).lstrip("0")
    number = whole_number(digits)
    return f"{TICKET_PREFIX}{number:06d}" if number else None


@login_required
def ticket_link(request, pk):
    user = request.user
    nc = _record_or_404(user, pk)
    form = LinkTicketForm(request.POST or None, initial=_revision(nc))

    def apply(data):
        number = _ticket_number(data["ticket"])
        # Only a ticket the reader may open now; any other number gets the same answer.
        ticket = staff_requests(user).filter(number=number).first() if number else None
        if ticket is None:
            raise ValidationError({"ticket": NO_TICKET})
        try:
            services.link_ticket(
                actor=user, nc=nc, expected_revision=data["expected_revision"], request=ticket
            )
        except ModuleOff:
            raise
        except Http404:  # left the reader's reach since it was found
            raise ValidationError({"ticket": NO_TICKET}) from None

    return _form_response(
        request,
        nc,
        form,
        title="Add ticket",
        submit_label="Add ticket",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        success="Ticket linked.",
        notes=("The ticket's timeline records the link.",),
        unavailable=_closed(nc),
    )


@login_required
def ticket_unlink(request, pk, link_pk):
    user = request.user
    selectors.staff_nonconformances(user)  # staff only; 404 while the module is off
    # The link and its record in one statement: the form's revision and the ticket it names come
    # from the same moment, and only a ticket the reader may open now. A link removed since a
    # form was drawn is still found for that form's POST, so its reason is kept with a message.
    links = selectors.linked_tickets(user, pk, include_removed=request.method == "POST")
    link = (
        links.filter(pk=link_pk).select_related("nonconformance", "nonconformance__owner").first()
    )
    if link is None:
        raise Http404("No such linked ticket.")
    nc = link.nonconformance
    if link.removed_at is not None and not _posted_earlier(request, nc.revision):
        raise Http404("No such linked ticket.")  # only a form drawn before its removal
    form = ReasonForm(request.POST or None, initial=_revision(nc))

    def apply(data):
        if link.removed_at is not None:
            raise ValidationError("This ticket is no longer linked to this record.")
        services.unlink_ticket(actor=user, nc=nc, link=link, **data)

    def preview():
        # Drawn again only while the reader may still open the ticket.
        readable = selectors.linked_tickets(user, pk, include_removed=True).filter(pk=link_pk)
        return {"link": link} if readable.exists() else {}

    return _form_response(
        request,
        nc,
        form,
        title="Remove linked ticket",
        submit_label="Remove ticket",
        apply=apply,
        allowed=services.can_work(user, nc),
        refusal=WORK_REFUSAL,
        gone="That ticket is no longer linked or no longer open to you.",
        success="Linked ticket removed.",
        destructive=True,
        notes=("The link stays in the history with your reason.",),
        extra=preview,
        unavailable=_closed(nc)
        or (
            "The complaint it was opened from stays linked."
            if link.request_id == nc.source_request_id
            else ""
        ),
    )
