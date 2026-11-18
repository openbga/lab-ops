"""Staff request pages: the list with its views and bulk actions, Find, the new-request form, the
request page with its composer and action forms, and the request's audit history.

Every read starts from desk.selectors.staff_requests (or desk.lists, which does), so a request
the user may not read answers 404 on every route. Every change goes through desk.services.
"""

from urllib.parse import urlencode

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.roles import require_staff
from attachments.models import Attachment
from attachments.selectors import with_files
from attachments.services import add_entry_with_files, create_request_with_files
from clients.models import Client
from core.ids import ascii_digits, whole_number
from core.lists import list_response
from core.models import AuditEvent
from links.search import request_ids_for_identifier
from taxonomy import terms
from taxonomy.models import RequestCategory

from . import saved_views, services
from .forms import (
    AssignForm,
    BulkForm,
    ComplaintDecisionForm,
    ComplaintRecordForm,
    ComplaintReviewForm,
    EntryForm,
    PropertiesForm,
    ReasonForm,
    RequestCreateForm,
    SavedViewForm,
    StatusForm,
    WithdrawForm,
    person_name,
)
from .lists import (
    COLUMNS,
    DEFAULT_SORT,
    DEFAULT_VIEW,
    FILTER_FIELDS,
    INLINE,
    MORE,
    SAVED_FILTERS,
    SEARCH_FIELDS,
    SYSTEM_VIEWS,
    RequestFilter,
    base_queryset,
    ordered_requests,
    refine,
    view_count,
)
from .models import ComplaintReview, EntryKind, RequestEntry, SavedView, ServiceRequest, Status
from .permissions import is_supervisor
from .selectors import portal_audience, staff_requests
from .timeline import audit_changes, describe

NEIGHBOUR_WINDOW = 2000  # requests of the list scanned for previous and next


def _errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _request_or_404(user, pk):
    try:
        return (
            staff_requests(user)
            .select_related(
                "client", "site", "contact", "category__parent", "team", "assignee", "created_by"
            )
            .get(pk=pk)
        )
    except ServiceRequest.DoesNotExist:
        raise Http404("No such ticket.") from None


def _record_url(request_obj, list_query=""):
    url = reverse("desk:request-detail", args=[request_obj.pk])
    return f"{url}?{urlencode({'list': list_query})}" if list_query else url


def _back(request):
    """The list query string the request page was opened from (kept through its forms)."""
    return (request.GET.get("list") or request.POST.get("list") or "")[:2000]


# ------------------------------------------------------------------------------ the list


def _view_picker(user, current_view, current_saved):
    views = [
        {
            "label": label,
            "url": f"{reverse('desk:request-list')}?view={key}",
            "count": view_count(user, key),
            "current": not current_saved and key == current_view,
        }
        for key, (label, _) in SYSTEM_VIEWS.items()
    ]
    saved = [
        {
            "label": view.name,
            "url": _saved_url(view),
            "shared": view.get_shared_with_display() if view.shared_with != "private" else "",
            "own": view.owner_id == user.pk,
            "current": current_saved == str(view.pk),
            "view": view,
        }
        for view in saved_views.visible_views(user).select_related("team")
    ]
    return views, saved


def _saved_url(view):
    params = {"view": "all", **view.filters, "saved": view.pk}  # stored view, else all
    if view.sort:
        params["sort"] = view.sort
    return f"{reverse('desk:request-list')}?{urlencode(params)}"


@login_required
def request_list(request):
    user = request.user
    require_staff(user)
    current_view = request.GET.get("view") or DEFAULT_VIEW
    if current_view not in SYSTEM_VIEWS:
        # An unknown view (an old bookmark) opens the default one rather than an empty list.
        params = request.GET.copy()
        params.pop("view")
        return redirect(f"{reverse('desk:request-list')}?{params.urlencode()}")
    current_saved = request.GET.get("saved", "")
    views, saved = _view_picker(user, current_view, current_saved)
    active_view = next((item for item in saved if item["current"]), None)
    title = active_view["label"] if active_view else SYSTEM_VIEWS[current_view][0]
    clear_url = (
        active_view["url"] if active_view else f"{reverse('desk:request-list')}?view={current_view}"
    )
    baseline = active_view["view"].filters if active_view else {}
    refined = any(
        request.GET.get(name, "") != baseline.get(name, "")
        for name in SAVED_FILTERS
        if name != "view"
    )
    bulk_form = BulkForm(request.POST if request.method == "POST" else None, auto_id="bulk_%s")
    selected_rows = set()
    if request.method == "POST":
        _apply_bulk(request, bulk_form)
        if not bulk_form.errors:
            return redirect(request.get_full_path())
        # Row values include the displayed revision. A changed ticket is never silently
        # selected at its new revision. The refreshed list still enforces visibility.
        selected_rows = set(request.POST.getlist("requests"))
        for error in bulk_form.non_field_errors():
            messages.error(request, error)
    return list_response(
        request,
        queryset=base_queryset(user),
        columns=COLUMNS,
        default_sort=DEFAULT_SORT,
        form_class=RequestFilter,
        search_fields=SEARCH_FIELDS,
        filter_fields=FILTER_FIELDS,
        date_fields=(("created", "created_at"),),
        template="desk/request_list.html",
        title=title,
        inline=INLINE,
        more=MORE,
        placeholder="Number, subject, organization, contact or reference",
        refine=refine(user),
        extra={
            "views": views,
            "saved_views": saved,
            "current_view": current_view,
            "view_description": SYSTEM_VIEWS[current_view][1],
            "bulk_form": bulk_form,
            "selected_rows": selected_rows,
            "clear_url": clear_url,
            "refined": refined,
            "list_query": request.GET.urlencode(),
        },
    )


def _safe_next(request, fallback):
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return target
    return fallback


def _apply_bulk(request, form):
    # Each selected row posts "<id>:<revision>", the revision the list showed;
    # a selection without its revision is refused by the service as out of date.
    selected = {}
    for value in request.POST.getlist("requests"):
        pk, _, revision = value.partition(":")
        selected[pk] = revision or None
    if not form.is_valid():
        return
    data = form.cleaned_data
    try:
        if data["action"] == "assign":
            count = services.bulk_assign(
                actor=request.user,
                selected=selected,
                team=data["team"],
                assignee=data["assignee"],
            )
        elif data["action"] == "status":
            count = services.bulk_status(
                actor=request.user, selected=selected, status=data["status"]
            )
        else:
            count = services.bulk_close(actor=request.user, selected=selected)
    except (ValidationError, PermissionDenied, ValueError) as exc:
        form.add_error(None, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.success(request, f"{count} ticket{'s' if count != 1 else ''} changed.")


@login_required
@require_POST
def request_bulk(request):
    """Accept forms opened before the list itself handled bulk corrections."""
    require_staff(request.user)
    form = BulkForm(request.POST)
    _apply_bulk(request, form)
    for errors in form.errors.values():
        messages.error(request, " ".join(errors))
    return redirect(_safe_next(request, reverse("desk:request-list")))


def _category_terms_refused(exc):
    """Whether create_request refused because the category changed since the page was drawn."""
    return terms.CHANGED in getattr(exc, "message_dict", {}).get("category", [])


def _terms_match(category, value):
    """Whether the posted category terms still describe `category` as it is now."""
    try:
        terms.check(category, value)
    except ValidationError:
        return False
    return True


def _ascii_digits(raw):
    """Whether `raw` is a record id (core.ids.ascii_digits: ASCII digits, bounded; reviews 5
    and 8)."""
    return ascii_digits(raw)


# ------------------------------------------------------------------------------ Find


def _number(term):
    digits = term.upper().removeprefix("REQ-").lstrip("0")
    return whole_number(digits)


@login_required
@require_GET
def request_find(request):
    """One search box (the top bar's lookup and the phone bar's Find): a request number opens it;
    otherwise requests matching the text, the organization or contact, requests carrying a
    matching identifier, and matching client organizations."""
    user = request.user
    term = request.GET.get("q", "").strip()[:200]
    visible = staff_requests(user)
    results, identifier_ids, organizations = [], [], []
    if term:
        organizations = Client.objects.filter(
            Q(name__icontains=term)
            | Q(account_number__icontains=term)
            | Q(email__icontains=term)
            | Q(phone__icontains=term)
            | Q(city__icontains=term)
        ).order_by("-is_active", "name")[:20]
        number = _number(term)
        if number is not None and visible.filter(pk=number).exists():
            return redirect("desk:request-detail", pk=number)
        identifier_ids = [
            pk for pk in request_ids_for_identifier(user, term) if visible.filter(pk=pk).exists()
        ]
        text = Q()
        for field in SEARCH_FIELDS:
            text |= Q(**{f"{field}__icontains": term})
        results = (
            base_queryset(user)
            .filter(text | Q(pk__in=identifier_ids))
            .order_by("-updated_at", "-pk")[:50]
        )
    return render(
        request,
        "desk/find.html",
        {
            "page_title": "Find tickets",
            "q": term,
            "results": results,
            "identifier_ids": set(identifier_ids),
            "organizations": organizations,
        },
    )


# ------------------------------------------------------------------------------ new request


@login_required
def request_create(request):
    require_staff(request.user)
    initial = {}
    if client_id := request.GET.get("client"):
        initial["client"] = client_id
    form = RequestCreateForm(request.POST or None, request.FILES or None, initial=initial)
    # A request form's own fields (request_forms): validated with the form, saved in the
    # creation's transaction through on_created. Imported here: desk models never import it.
    from request_forms.intake import bound_intake, saver
    from request_forms.services import FORM_CHANGED, is_form_changed

    intake = None
    if request.method == "POST":
        posted = RequestCategory.objects.filter(
            pk=int(request.POST["category"]) if _ascii_digits(request.POST.get("category")) else 0,
            parent__isnull=False,
        ).first()
        intake = bound_intake(posted, request.POST, request.user)
    form_valid = request.method == "POST" and form.is_valid()
    if intake is not None and "client" in getattr(form, "cleaned_data", {}):
        # Recheck the offer against the organization the ticket will have, including a site or
        # contact's organization, even when another field prevents the form from saving.
        intake.check_offer(form.cleaned_data["client"])
    intake_valid = intake is None or intake.is_valid()
    form_changed = intake is not None and intake.definition_changed
    if form_valid and intake_valid:
        data = form.cleaned_data
        try:
            # The request form's locks first, in the creating transaction: one lock order for
            # every capture (request_forms.definition).
            with transaction.atomic():
                if intake is not None:
                    intake.lock()
                created = create_request_with_files(
                    files=data.get("files") or [],
                    actor=request.user,
                    category=data["category"],
                    subject=data["subject"],
                    first_entry_kind=data["first_entry_kind"],
                    first_entry_body=data["body"],
                    channel=data["channel"],
                    client=data.get("client"),
                    site=data.get("site"),
                    contact=data.get("contact"),
                    reported_by=data.get("reported_by", ""),
                    priority=data.get("priority") or None,
                    team=data.get("team"),
                    assignee=data.get("assignee"),
                    client_reference=data.get("client_reference", ""),
                    on_created=saver(intake, request.user) if intake is not None else None,
                    # What the category section told the person: refused if the
                    # category's protection or routing changed since; missing counts as changed.
                    drawn_terms=request.POST.get(terms.FIELD, ""),
                )
        except ValidationError as exc:
            if intake is not None and is_form_changed(exc):
                form_changed = True
            elif intake is not None and _category_terms_refused(exc):
                # The desk's own check noticed the category changed: its message stands, and the
                # request form is drawn fresh with it, so the next submit carries current values.
                _errors(form, exc)
                intake.refresh()
            elif intake is not None and hasattr(exc, "message_dict"):
                keys = {f.key for f in intake.template_fields}
                exc_fields = {k: v for k, v in exc.message_dict.items() if k in keys}
                if exc_fields:
                    intake.add_value_errors(ValidationError(exc_fields))
                rest = {k: v for k, v in exc.message_dict.items() if k not in keys}
                if rest:
                    _errors(form, ValidationError(rest))
            else:
                _errors(form, exc)
        else:
            messages.success(request, f"Ticket {created.number} logged.")
            if staff_requests(request.user).filter(pk=created.pk).exists():
                return redirect("desk:request-detail", pk=created.pk)
            return redirect("desk:request-list")  # routed to a restricted team
    if form_changed:
        # The request form changed or was retired since the page was drawn, whichever layer
        # noticed: drawn fresh from its current definition with none of its answers, or not at
        # all when it left the category (request_forms reviews 6 and 7). One message: the
        # desk's own when the category changed, the picker's when the form left it.
        intake.refresh()
        if not _terms_match(intake.category, request.POST.get(terms.FIELD, "")):
            form.add_error("category", terms.CHANGED)
        elif intake.values_form is not None:
            messages.error(request, FORM_CHANGED)
    chosen = form["category"].value()
    category = (
        RequestCategory.objects.filter(pk=chosen, parent__isnull=False)
        .select_related("default_team")
        .first()
        if _ascii_digits(chosen)
        else None
    )
    if intake is not None and category is not None and intake.category.pk == category.pk:
        # One object for the guidance, the category terms and the request form's token, so the
        # page tells one story and a second submit is accepted.
        category = intake.category
    response = render(
        request,
        "desk/request_form.html",
        {
            "page_title": "New ticket",
            "form": form,
            "category": category,
            "category_terms": terms.drawn([category]),
            "data": request.POST if request.method == "POST" else QueryDict(),
            "intake_values": intake,
            "submit_label": "Log ticket",
            "cancel_url": reverse("desk:request-list"),
            "crumbs": [("Tickets", reverse("desk:request-list")), ("New ticket", None)],
        },
    )
    # Never cached, whatever the form: a refused page shows typed values again, patient
    # information included, and whether a field holds it may have changed since it was drawn.
    add_never_cache_headers(response)
    return response


@never_cache  # it carries a request form's controls and token, drawn from one reading
@login_required
@require_GET
def category_guidance(request):
    """HTMX partial: the section after the category on the new-request form (its guidance and
    the request type's own fields), for the chosen category."""
    require_staff(request.user)
    raw = request.GET.get("category", "")
    category = (
        RequestCategory.objects.filter(
            pk=int(raw) if _ascii_digits(raw) else 0, parent__isnull=False
        )
        .select_related("default_team")
        .first()
    )
    return render(
        request,
        "desk/_category_section.html",
        {"category": category, "category_terms": terms.drawn([category]), "data": QueryDict()},
    )


# ------------------------------------------------------------------------------ request page


def _neighbours(user, request_obj, list_query):
    if not list_query:
        return None, None
    ids = list(
        ordered_requests(user, QueryDict(list_query)).values_list("pk", flat=True)[
            :NEIGHBOUR_WINDOW
        ]
    )
    if request_obj.pk not in ids:
        return None, None
    index = ids.index(request_obj.pk)
    previous = ids[index - 1] if index > 0 else None
    following = ids[index + 1] if index + 1 < len(ids) else None
    return previous, following


def _detail_context(request, request_obj, composer=None):
    user = request.user
    list_query = _back(request)
    entries = describe(
        list(
            with_files(
                request_obj.entries.select_related("author", "withdrawn_by").order_by(
                    "-created_at", "-pk"
                )
            )
        )
    )
    audience = portal_audience(request_obj)
    review = getattr(request_obj, "complaint_review", None)
    previous, following = _neighbours(user, request_obj, list_query)
    closed = request_obj.status == Status.CLOSED
    if composer is None:
        composer = EntryForm(
            closed=closed,
            initial={
                "kind": EntryKind.REPLY if audience and not closed else EntryKind.NOTE,
                "expected_revision": request_obj.revision,
            },
        )
    supervisor = is_supervisor(user)
    return {
        **_nonconformance_context(user, request_obj),
        "page_title": f"{request_obj.number} {request_obj.subject}",
        "req": request_obj,
        "entries": entries,
        "audience": audience,
        "composer": composer,
        "review": review,
        "review_counts": services.review_counts(request_obj, review),
        "review_refusal": services.review_refusal(user, request_obj, review)
        if request_obj.is_complaint
        else "",
        # The record can change until a review that still counts (desk.services._complaint_record).
        "can_record_complaint": request_obj.is_complaint
        and (supervisor or user.pk == request_obj.assignee_id)
        and not services.review_counts(request_obj, review),
        "is_supervisor": supervisor,
        "is_member": request_obj.team.memberships.filter(user=user).exists(),
        "is_closed": closed,
        "composer_title": "Add an internal note" if closed else "Reply or add a note",
        "list_query": list_query,
        "list_url": f"{reverse('desk:request-list')}?{list_query}"
        if list_query
        else reverse("desk:request-list"),
        "previous_url": _record_url(ServiceRequest(pk=previous), list_query) if previous else "",
        "next_url": _record_url(ServiceRequest(pk=following), list_query) if following else "",
        "follow_ups": staff_requests(user).filter(follow_up_of=request_obj).order_by("pk"),
        "follow_up_of": staff_requests(user).filter(pk=request_obj.follow_up_of_id).first()
        if request_obj.follow_up_of_id
        else None,
    }


def _nonconformance_context(user, request_obj):
    """The optional nonconformance module on a ticket's pages, from one check of the switch.

    The record opened from a complaint is part of the complaint's record, so its number shows
    whether the module is on or off (as a link only while it is on); opening it advances the
    ticket's revision, so a form drawn before cannot save over it. The records linked to the
    ticket, the Open nonconformance action and every link show only while the module is on."""
    # Imported here: the desk works with every optional module off (core.modules).
    from nonconformance import selectors as nonconformance_selectors
    from nonconformance.models import Nonconformance

    module_on = nonconformance_selectors.module_on_for(user)
    opened = (
        Nonconformance.objects.filter(source_request=request_obj).first()
        if request_obj.is_complaint
        else None
    )
    return {
        "nonconformance_module": module_on,
        "nonconformance_opened": opened,
        "nonconformances": (
            list(nonconformance_selectors.for_ticket(user, request_obj)) if module_on else []
        ),
    }


@login_required
@never_cache
def request_detail(request, pk):
    request_obj = _request_or_404(request.user, pk)
    return render(request, "desk/request_detail.html", _detail_context(request, request_obj))


@login_required
@require_POST
def request_entry(request, pk):
    request_obj = _request_or_404(request.user, pk)
    form = EntryForm(request.POST, request.FILES, closed=request_obj.status == Status.CLOSED)
    if form.is_valid():
        data = form.cleaned_data
        try:
            entry = add_entry_with_files(
                files=data.get("files") or [],
                actor=request.user,
                request=request_obj,
                kind=data["kind"],
                body=data["body"],
                expected_revision=data["expected_revision"],
                contact=form.contact(),
                new_status=data["new_status"] or None,
            )
        except ServiceRequest.DoesNotExist:
            raise Http404("No such ticket.") from None  # left the actor's scope meanwhile
        except ValidationError as exc:
            _errors(form, exc)
        else:
            label = {"reply": "Reply sent", "note": "Note added", "contact": "Contact logged"}
            if entry.kind == EntryKind.REPLY and not portal_audience(request_obj):
                label[entry.kind] = "Reply recorded. No portal account can see this ticket"
            messages.success(request, label[entry.kind] + ".")
            return redirect(_record_url(request_obj, _back(request)))
    # Scope again before anything about the request is rendered: it may have left the actor's
    # scope while the form was validated.
    request_obj = _request_or_404(request.user, pk)
    context = _detail_context(request, request_obj, composer=form)
    return render(request, "desk/request_detail.html", context, status=400)


@login_required
@require_POST
def request_take(request, pk):
    request_obj = _request_or_404(request.user, pk)
    try:
        services.take(
            actor=request.user,
            request=request_obj,
            expected_revision=int(request.POST.get("expected_revision") or 0),
        )
    except ServiceRequest.DoesNotExist:
        raise Http404("No such ticket.") from None
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Assigned to you.")
    return redirect(_record_url(request_obj, _back(request)))


def _action_form(
    request, request_obj, form, *, title, submit_label, apply, template=None, extra=None
):
    """Render and handle one of the request page's action forms (the shared form dialog).
    `extra` adds to the template's context."""
    record_url = _record_url(request_obj, _back(request))
    if request.method == "POST" and form.is_valid():
        try:
            message = apply(form.cleaned_data)
        except ServiceRequest.DoesNotExist:
            raise Http404("No such ticket.") from None  # left the actor's scope meanwhile
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, message)
            if not staff_requests(request.user).filter(pk=request_obj.pk).exists():
                # Moved to a restricted category or team the user is not in.
                messages.info(request, f"{request_obj.number} is now outside what you can see.")
                return redirect("desk:request-list")
            return redirect(record_url)
    if request.method == "POST":
        # Scope again before the refused form shows anything about the request.
        request_obj = _request_or_404(request.user, request_obj.pk)
    response = render(
        request,
        template or "desk/request_action.html",
        {
            "page_title": f"{title}: {request_obj.number}",
            "req": request_obj,
            "form": form,
            "submit_label": submit_label,
            "cancel_url": record_url,
            "list_query": _back(request),
            "crumbs": [
                ("Tickets", reverse("desk:request-list")),
                (request_obj.number, record_url),
                (title, None),
            ],
            **(extra or {}),
        },
    )
    add_never_cache_headers(response)  # a refused form shows what was typed again
    return response


def _revision(request_obj):
    return {"expected_revision": request_obj.revision}


@login_required
def request_assign(request, pk):
    request_obj = _request_or_404(request.user, pk)
    form = AssignForm(
        request.POST or None,
        initial={
            **_revision(request_obj),
            "assign_to": f"{request_obj.team_id}:{request_obj.assignee_id or ''}",
        },
    )

    def apply(data):
        team, assignee = data["assign_to"]
        services.assign(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            team=team,
            assignee=assignee,
            reason=data["reason"],
        )
        review = ComplaintReview.objects.filter(request=request_obj).first()
        if (
            assignee is not None
            and request_obj.is_complaint
            and review is not None
            and review.reviewed_by_id == assignee.pk
        ):
            messages.warning(
                request,
                f"{person_name(assignee)} reviewed this complaint and is now its assignee, so "
                "that review no longer counts. Another supervisor must review it before it "
                "can close.",
            )
        return "Assignment changed."

    return _action_form(
        request, request_obj, form, title="Assign", submit_label="Assign", apply=apply
    )


@login_required
def request_status(request, pk):
    request_obj = _request_or_404(request.user, pk)
    if request_obj.status == Status.CLOSED:
        raise PermissionDenied("A closed ticket cannot change.")
    form = StatusForm(
        request.POST or None, current=request_obj.status, initial=_revision(request_obj)
    )

    def apply(data):
        services.change_status(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            status=data["status"],
            summary=data["summary"],
            reason=data["reason"],
        )
        return "Status changed."

    return _action_form(
        request, request_obj, form, title="Change status", submit_label="Change status", apply=apply
    )


@login_required
def request_properties(request, pk):
    request_obj = _request_or_404(request.user, pk)
    form = PropertiesForm(
        request.POST or None,
        initial={
            **_revision(request_obj),
            "category": request_obj.category_id,
            "priority": request_obj.priority,
        },
    )

    def apply(data):
        services.update_properties(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            priority=data["priority"],
            category=data["category"],
            reason=data["reason"],
        )
        return "Details changed."

    return _action_form(
        request,
        request_obj,
        form,
        title="Edit category and priority",
        submit_label="Save changes",
        apply=apply,
    )


@login_required
def request_complaint_mark(request, pk):
    request_obj = _request_or_404(request.user, pk)
    marking = not request_obj.is_complaint
    if not marking and not is_supervisor(request.user):
        raise PermissionDenied("Only a supervisor can unmark a complaint.")
    form = ReasonForm(request.POST or None, initial=_revision(request_obj))

    def apply(data):
        services.set_complaint(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            is_complaint=marking,
            reason=data["reason"],
        )
        return "Marked as a complaint." if marking else "No longer a complaint."

    title = "Mark as a complaint" if marking else "Unmark complaint"
    return _action_form(request, request_obj, form, title=title, submit_label=title, apply=apply)


def _complaint_or_404(request, pk):
    request_obj = _request_or_404(request.user, pk)
    if not request_obj.is_complaint:
        raise Http404("This ticket is not a complaint.")
    return request_obj, getattr(request_obj, "complaint_review", None)


@login_required
def complaint_decision(request, pk):
    request_obj, review = _complaint_or_404(request, pk)
    initial = _revision(request_obj)
    if review:
        initial.update(decision=review.decision, rationale=review.rationale)
    form = ComplaintDecisionForm(request.POST or None, initial=initial)

    def apply(data):
        services.decide_complaint(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            decision=data["decision"],
            rationale=data["rationale"],
        )
        return "Decision recorded."

    return _guarded(
        request,
        request_obj,
        form,
        title="Investigation decision",
        submit_label="Record decision",
        apply=apply,
    )


@login_required
def complaint_record(request, pk):
    request_obj, review = _complaint_or_404(request, pk)
    initial = _revision(request_obj)
    if review:
        initial.update(
            findings=review.findings,
            corrective_action_reference=review.corrective_action_reference,
            outcome_communicated_on=review.outcome_communicated_on,
            outcome_communicated_how=review.outcome_communicated_how,
        )
    form = ComplaintRecordForm(request.POST or None, initial=initial)
    if request_obj.nonconformances_opened.exists():
        # The nonconformance opened from the complaint takes the place of a free-text reference
        # (a stored one stays as it is). Opening it advances the ticket's revision, so a form
        # drawn before it was opened, with the field, is refused as stale.
        del form.fields["corrective_action_reference"]

    def apply(data):
        values = dict(data)
        expected = values.pop("expected_revision")
        services.update_complaint(
            actor=request.user, request=request_obj, expected_revision=expected, **values
        )
        return "Complaint record saved."

    return _guarded(
        request,
        request_obj,
        form,
        title="Findings and outcome",
        submit_label="Save record",
        apply=apply,
    )


@login_required
def complaint_review(request, pk):
    request_obj, review = _complaint_or_404(request, pk)
    if refusal := services.review_refusal(request.user, request_obj, review):
        raise PermissionDenied(refusal)
    form = ComplaintReviewForm(request.POST or None, initial=_revision(request_obj))

    def apply(data):
        services.review_complaint(
            actor=request.user,
            request=request_obj,
            expected_revision=data["expected_revision"],
            note=data["note"],
        )
        return "Complaint reviewed. The ticket can now be closed."

    return _action_form(
        request,
        request_obj,
        form,
        title="Review complaint",
        submit_label="Approve review",
        apply=apply,
        template="desk/complaint_review.html",
        extra=_nonconformance_context(request.user, request_obj),
    )


def _guarded(request, request_obj, form, **kwargs):
    """An action form that services refuse with PermissionDenied: shown as a page error."""
    apply = kwargs.pop("apply")

    def checked(data):
        try:
            return apply(data)
        except PermissionDenied as exc:
            raise ValidationError(str(exc)) from exc

    if not (is_supervisor(request.user) or request.user.pk == request_obj.assignee_id):
        raise PermissionDenied("The assignee or a supervisor records the complaint.")
    return _action_form(request, request_obj, form, apply=checked, **kwargs)


@login_required
def entry_withdraw(request, pk, entry_pk):
    request_obj = _request_or_404(request.user, pk)
    if not is_supervisor(request.user):
        raise PermissionDenied("Only a supervisor can withdraw a reply.")
    entry = get_object_or_404(RequestEntry, pk=entry_pk, request=request_obj, kind=EntryKind.REPLY)
    form = WithdrawForm(request.POST or None, initial=_revision(request_obj))

    def apply(data):
        services.withdraw_reply(
            actor=request.user,
            entry=entry,
            reason=data["reason"],
            expected_revision=data["expected_revision"],
        )
        return "Reply withdrawn from client view."

    if entry.withdrawn_at is not None:
        return redirect(_record_url(request_obj, _back(request)))
    return _action_form(
        request,
        request_obj,
        form,
        title="Withdraw reply",
        submit_label="Withdraw from client view",
        apply=apply,
        template="desk/entry_withdraw.html",
    )


@login_required
def request_history(request, pk):
    """The audit trail of a request, its entries, its complaint record and its links."""
    request_obj = _request_or_404(request.user, pk)
    if not is_supervisor(request.user):
        raise PermissionDenied("A ticket's audit history is for supervisors.")
    objects = Q(object_type="desk.servicerequest", object_id=str(request_obj.pk))
    entry_ids = [str(pk) for pk in request_obj.entries.values_list("pk", flat=True)]
    objects |= Q(object_type="desk.requestentry", object_id__in=entry_ids)
    if review := getattr(request_obj, "complaint_review", None):
        objects |= Q(object_type="desk.complaintreview", object_id=str(review.pk))
    try:
        attachment = apps.get_model("links", "LinkAttachment")
    except LookupError:
        attachment = None
    if attachment is not None and any(f.name == "request" for f in attachment._meta.fields):
        ids = attachment.objects.filter(request=request_obj).values_list("pk", flat=True)
        objects |= Q(
            object_type=attachment._meta.label_lower, object_id__in=[str(pk) for pk in ids]
        )
    file_ids = Attachment.objects.filter(request=request_obj).values_list("pk", flat=True)
    objects |= Q(object_type="attachments.attachment", object_id__in=[str(pk) for pk in file_ids])
    events = AuditEvent.objects.filter(objects).select_related("actor").order_by("occurred_at")
    return render(
        request,
        "desk/request_history.html",
        {
            "page_title": f"History of {request_obj.number}",
            "req": request_obj,
            "events": audit_changes(events),
            "record_url": _record_url(request_obj, _back(request)),
        },
    )


# ------------------------------------------------------------------------------ saved views


def _filter_summary(params):
    """The filters a new view will store, as (label, value) pairs for the form page."""
    try:
        stored = saved_views.clean_filters(params)
    except ValidationError:
        return []
    form = RequestFilter(stored)
    form.is_valid()
    pairs = []
    for name, value in stored.items():
        field = form.fields[name]
        shown = form.cleaned_data.get(name, value)
        if hasattr(field, "choices") and not hasattr(field, "queryset"):
            shown = dict(field.choices).get(value, value)
        label = "View" if name == "view" else form[name].label
        pairs.append((label, shown))
    return pairs


@login_required
def view_create(request):
    require_staff(request.user)
    params = QueryDict(
        request.GET.get("list", "") if request.method == "GET" else request.POST.get("list", "")
    )
    form = SavedViewForm(request.POST or None)
    if not is_supervisor(request.user):
        form.fields["shared_with"].choices = [("private", "Only me")]
    list_url = f"{reverse('desk:request-list')}?{params.urlencode()}"
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            view = saved_views.create_view(
                actor=request.user,
                name=data["name"],
                shared_with=data["shared_with"],
                team=data["team"],
                params=params,
                sort=params.get("sort", ""),
            )
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied):
                exc = ValidationError(str(exc))
            _errors(form, exc)
        else:
            messages.success(request, f"View “{view.name}” saved.")
            return redirect(_saved_url(view))
    return render(
        request,
        "desk/view_form.html",
        {
            "page_title": "Save view",
            "form": form,
            "list_query": params.urlencode(),
            "filters": _filter_summary(params),
            "cancel_url": list_url,
            "submit_label": "Save view",
            "crumbs": [("Tickets", list_url), ("Save view", None)],
        },
    )


@login_required
def view_edit(request, pk):
    require_staff(request.user)
    view = get_object_or_404(SavedView, pk=pk, owner=request.user)
    if request.method == "GET" and request.GET.get("confirm") == "delete":
        return render(
            request,
            "desk/view_delete.html",
            {
                "page_title": f"Delete view {view.name}?",
                "view": view,
                "cancel_url": _saved_url(view),
            },
        )
    form = SavedViewForm(
        request.POST or None,
        initial={"name": view.name, "shared_with": view.shared_with, "team": view.team_id},
    )
    if not is_supervisor(request.user) and view.shared_with == "private":
        form.fields["shared_with"].choices = [("private", "Only me")]
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            saved_views.update_view(
                actor=request.user,
                view=view,
                expected_revision=int(request.POST.get("expected_revision") or 0),
                name=data["name"],
                shared_with=data["shared_with"],
                team=data["team"],
            )
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied):
                exc = ValidationError(str(exc))
            _errors(form, exc)
        else:
            messages.success(request, "View saved.")
            return redirect(_saved_url(view))
    return render(
        request,
        "desk/view_form.html",
        {
            "page_title": f"Edit view {view.name}",
            "form": form,
            "view": view,
            "cancel_url": _saved_url(view),
            "submit_label": "Save changes",
            "crumbs": [("Tickets", reverse("desk:request-list")), (view.name, None)],
        },
    )


@login_required
@require_POST
def view_delete(request, pk):
    require_staff(request.user)
    view = get_object_or_404(SavedView, pk=pk, owner=request.user)
    try:
        saved_views.delete_view(
            actor=request.user,
            view=view,
            expected_revision=request.POST.get("expected_revision", ""),
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("desk:view-edit", pk=pk)
    messages.success(request, f"View “{view.name}” deleted.")
    return redirect("desk:request-list")


@login_required
@require_POST
def view_move(request, pk):
    require_staff(request.user)
    view = get_object_or_404(SavedView, pk=pk, owner=request.user)
    saved_views.move_view(actor=request.user, view=view, direction=request.POST.get("direction"))
    return redirect(_safe_next(request, _saved_url(view)))
