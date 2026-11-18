"""The client portal's request pages (requirements P1 to P4): the request list, raising a request
(and a follow-up to a closed one), the request page with its client-visible conversation, and the
reply. Every read starts from desk.portal_scope.portal_requests; a request outside it is a 404,
a staff account is refused (403, clients.portal.client_required).

The conversation comes only from desk.visibility.client_visible plus status-change events;
links only where staff chose to show them and never person-level ones. Internal notes, logged
contacts, other events, team members, assignee, priority, service clocks and complaint records
never reach these templates.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, CharField, IntegerField, Value, When
from django.db.models.functions import Extract
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from attachments.selectors import with_files
from attachments.services import add_entry_with_files, create_request_with_files
from clients.portal import client_required
from core.lists import list_response, return_list_params
from taxonomy import terms
from taxonomy.choices import Priority

from .models import AuthorCapacity, EntryKind, ServiceRequest, Status
from .portal_forms import (
    CLIENT_STATUS,
    OPEN_STATUSES,
    REPLYABLE,
    PortalRequestFilter,
    RaiseRequestForm,
    ReplyForm,
    client_status,
)
from .portal_scope import portal_requests
from .visibility import client_visible

SIGNATURE_LIMIT = 3  # follow-up chain shown on the page


def _ordered_for_queue(queryset):
    """Waiting for you first, then the other open requests (with the laboratory, with a third
    party), then resolved, then closed; most recently updated first within each."""
    rank = Case(
        When(status=Status.WAITING_CLIENT, then=Value(0)),
        When(status__in=OPEN_STATUSES, then=Value(1)),
        When(status=Status.RESOLVED, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )

    tone = Case(
        *(When(status=code, then=Value(pair[1])) for code, pair in CLIENT_STATUS.items()),
        default=Value("neutral"),
        output_field=CharField(),
    )
    return queryset.annotate(
        queue=rank * 10_000_000_000 - Extract("updated_at", "epoch"),
        client_status_tone=tone,
    )


PORTAL_COLUMNS = {
    "number": "pk",
    "subject": "subject",
    "status": "queue",  # waiting for you, other open, resolved, closed; newest update first
    "updated": "updated_at",
    "created": "created_at",
}


def _refine(queryset, cleaned):
    status = cleaned.get("status")
    if status == "active":
        return queryset.filter(status__in=OPEN_STATUSES)
    if status:
        return queryset.filter(status=status)
    return queryset


@client_required
def request_list(request):
    queryset = _ordered_for_queue(portal_requests(request.user).select_related("category", "site"))
    return list_response(
        request,
        queryset=queryset,
        columns=PORTAL_COLUMNS,
        default_sort="status",
        form_class=PortalRequestFilter,
        search_fields=("number", "subject", "client_reference"),
        filter_fields={},
        template="desk/portal/request_list.html",
        title="Tickets",
        placeholder="Number, subject or your reference",
        inline=("status",),
        refine=_refine,
    )


def _shown_links(service_request):
    """Links staff chose to show the client, never person-level ones. Empty until the links
    app provides them."""
    try:
        from links.models import LinkAttachment
    except ImportError:  # pragma: no cover - the links app is part of the contract
        return []
    fields = {f.name for f in LinkAttachment._meta.get_fields()}
    if not {"shown_to_client", "is_person", "removed_at", "request"} <= fields:
        return []
    return list(
        LinkAttachment.objects.filter(
            request=service_request,
            shown_to_client=True,
            is_person=False,
            removed_at__isnull=True,
            link__is_person=False,
        )
        .select_related("link", "link__system")
        .order_by("added_at", "pk")
    )


def _conversation(service_request):
    """Client-visible replies and status changes, oldest first. Nothing else. A reply's files
    are listed with it, never a withdrawn one (attachments)."""
    replies = client_visible(service_request.entries.all())
    statuses = service_request.entries.filter(kind=EntryKind.EVENT, event_action="status_changed")
    entries = with_files(
        (replies | statuses).select_related("author", "author__client").order_by("created_at", "pk")
    )
    items = []
    for entry in entries:
        if entry.kind == EntryKind.EVENT:
            new = (entry.event_data or {}).get("new")
            if new not in Status.values:
                continue
            items.append({"kind": "status", "at": entry.created_at, "status": client_status(new)})
        else:
            items.append(
                {
                    "kind": "reply",
                    "at": entry.created_at,
                    "body": entry.body,
                    # Recorded when the entry was written; never re-derived from the account.
                    "from_client": entry.author_capacity == AuthorCapacity.CLIENT,
                    "author": entry.author,
                    "files": [f for f in entry.attachments.all() if f.withdrawn_at is None],
                }
            )
    return items


def _signature(service_request):
    """How staff replies are signed in the portal: the team and the laboratory, never a person."""
    from core.models import Lab

    lab = Lab.objects.filter(pk=1).values_list("name", flat=True).first() or "The laboratory"
    return f"{service_request.team.name}, {lab}"


def _portal_request(user, number):
    return get_object_or_404(
        portal_requests(user).select_related("category", "site", "team", "follow_up_of"),
        number=number,
    )


@never_cache  # form details may hold the client's patient information (request_forms)
@client_required
def request_detail(request, number, reply_form=None):
    service_request = _portal_request(request.user, number)
    follow_up_of = service_request.follow_up_of
    if (
        follow_up_of is not None
        and not portal_requests(request.user).filter(pk=follow_up_of.pk).exists()
    ):
        follow_up_of = None  # never name a request the account cannot see
    list_params = return_list_params(request, PortalRequestFilter)
    can_reply = service_request.status in REPLYABLE
    if reply_form is None and can_reply:
        reply_form = ReplyForm(initial={"expected_revision": service_request.revision})
    return render(
        request,
        "desk/portal/request_detail.html",
        {
            "page_title": f"{service_request.number} {service_request.subject}",
            "service_request": service_request,
            "status": client_status(service_request.status),
            "conversation": _conversation(service_request),
            "links": _shown_links(service_request),
            "follow_up_of": follow_up_of,
            "follow_ups": portal_requests(request.user)
            .filter(follow_up_of=service_request)
            .order_by("pk")[:SIGNATURE_LIMIT],
            "signature": _signature(service_request),
            "can_reply": can_reply,
            "reply_form": reply_form,
            "list_url": reverse("portal:request-list", query=list_params),
            "reply_url": reverse(
                "portal:request-reply",
                args=[number],
                query={"list": urlencode(list_params)} if list_params else None,
            ),
            "is_closed": service_request.status == Status.CLOSED,
        },
    )


@client_required
@require_POST
def request_reply(request, number):
    service_request = _portal_request(request.user, number)
    list_params = return_list_params(request, PortalRequestFilter)
    detail_url = reverse(
        "portal:request-detail",
        args=[number],
        query={"list": urlencode(list_params)} if list_params else None,
    )
    form = ReplyForm(request.POST, request.FILES)
    if service_request.status not in REPLYABLE:
        messages.error(request, "This ticket is closed. Open a follow-up ticket instead.")
        return redirect(detail_url)
    if form.is_valid():
        try:
            add_entry_with_files(
                files=form.cleaned_data["files"],
                actor=request.user,
                request=service_request,
                kind=EntryKind.REPLY,
                body=form.cleaned_data["body"],
                expected_revision=form.cleaned_data["expected_revision"],
            )
        except ServiceRequest.DoesNotExist:
            raise Http404("No such ticket.") from None
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(request, "Reply sent to the laboratory.")
            return redirect(f"{detail_url}#conversation")
    return request_detail(request, number, reply_form=form)


def _form_errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


@client_required
def request_create(request):
    follow_up_of = None
    follow_up_number = request.GET.get("follow_up") or request.POST.get("follow_up") or ""
    if follow_up_number:
        follow_up_of = _portal_request(request.user, follow_up_number)
        if follow_up_of.status != Status.CLOSED:
            raise Http404("Only a closed ticket takes a follow-up.")
    form = RaiseRequestForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        initial={"follow_up": follow_up_of.number if follow_up_of else ""},
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            created = create_request_with_files(
                files=data["files"],
                actor=request.user,
                category=data["category"],
                subject=data["subject"],
                first_entry_kind=EntryKind.REPLY,
                first_entry_body=data["description"],
                channel="portal",
                site=data.get("site"),
                client_reference=data["client_reference"],
                priority=Priority.HIGH if data["urgent"] else None,
                follow_up_of=follow_up_of,
                # The topics' settings when the page was drawn.
                drawn_terms=request.POST.get(terms.FIELD, ""),
            )
        except PermissionDenied:
            raise
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(
                request, f"Ticket {created.number} sent. It is now with the laboratory."
            )
            return redirect("portal:request-detail", number=created.number)
    return render(
        request,
        "desk/portal/request_form.html",
        {
            "page_title": "New follow-up ticket" if follow_up_of else "New ticket",
            "form": form,
            "follow_up_of": follow_up_of,
            "category_terms": terms.drawn(form.fields["category"].queryset),
        },
    )


def portal_queue(user):
    """The portal home's open requests and the count waiting for the client (P4). Both come
    from portal_requests, so they match what the request list shows."""
    open_requests = portal_requests(user).filter(status__in=OPEN_STATUSES)
    waiting = open_requests.filter(status=Status.WAITING_CLIENT).count()
    rows = _ordered_for_queue(open_requests.select_related("category", "site")).order_by(
        "queue", "pk"
    )[:10]
    return {
        "open_requests": rows,
        "open_count": open_requests.count(),
        "waiting_count": waiting,
    }
