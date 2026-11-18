"""The demonstration story (DM1, desk/story.py): what a visitor sees after a reset, whenever the
reset runs, and that two resets in a row tell the same story."""

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.core.management import call_command

from accounts.models import User
from accounts.roles import CAPABILITIES, has_capability
from desk.models import Channel, EntryKind, ServiceRequest, Status
from desk.portal_scope import portal_requests
from desk.selectors import staff_requests
from service_levels.selectors import annotate_clock_state
from taxonomy.choices import Priority

PASSWORD = "synthetic-test-password-only"
CLOCK_STATES = {"overdue", "due_soon", "on_track", "paused", "met", "missed"}
# The daily reset runs at 08:00 UTC (render.demo.yaml): a weekday, a Saturday and a Sunday.
RESETS = {
    "wednesday": datetime(2026, 11, 18, 8, 0, tzinfo=UTC),
    "saturday": datetime(2026, 11, 21, 8, 0, tzinfo=UTC),
    "sunday": datetime(2026, 11, 22, 8, 0, tzinfo=UTC),
}


def seed(monkeypatch, moment=None):
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    if moment is None:
        call_command("seed_demo", verbosity=0)
        return
    with mock.patch("django.utils.timezone.now", return_value=moment):
        call_command("seed_demo", verbosity=0)


def states(moment=None):
    return {r.clock_state for r in annotate_clock_state(ServiceRequest.objects.all(), now=moment)}


@pytest.mark.django_db
@pytest.mark.parametrize("day", list(RESETS))
def test_every_clock_state_whenever_the_reset_runs(monkeypatch, day):
    moment = RESETS[day]
    seed(monkeypatch, moment)
    assert states(moment) >= CLOCK_STATES, day
    # Later in the day a pickup is still due soon (their due times are staggered).
    assert "due_soon" in states(moment + timedelta(hours=10)), day


@pytest.mark.django_db
def test_the_story_covers_what_a_visitor_should_see(monkeypatch):
    seed(monkeypatch)
    requests = ServiceRequest.objects.all()
    assert set(requests.values_list("status", flat=True)) == set(Status.values)
    assert set(requests.values_list("priority", flat=True)) == set(Priority.values)
    assert set(requests.values_list("channel", flat=True)) == set(Channel.values)
    # Complaints: one awaiting its independent review, one reviewed and closed.
    complaints = requests.filter(is_complaint=True)
    assert complaints.filter(
        complaint_review__reviewed_at__isnull=True, complaint_review__decision="investigate"
    ).exists()
    assert complaints.filter(
        status=Status.CLOSED, complaint_review__reviewed_at__isnull=False
    ).exists()
    # A withdrawn reply, and one synthetic patient link that only the supervisor may see.
    assert requests.filter(entries__withdrawn_at__isnull=False).exists()
    from links.models import LinkAttachment

    patient = LinkAttachment.objects.get(link__record_type="patient")
    assert patient.link.identifier.startswith("PT-TEST-") and not patient.link.url
    assert LinkAttachment.objects.filter(link__record_type="order").exists()
    assert LinkAttachment.objects.filter(link__record_type="sample").exists()
    assert LinkAttachment.objects.filter(link__record_type="report").count() >= 2
    holders = {
        user.username
        for user in User.objects.all()
        for code in CAPABILITIES
        if has_capability(user, code)
    }
    assert holders == {"demo_supervisor"}


@pytest.mark.django_db
def test_who_sees_what(monkeypatch):
    seed(monkeypatch)
    users = User.objects.in_bulk(
        ["demo_technical", "demo_supervisor", "demo_client", "demo_client_water"],
        field_name="username",
    )
    privacy = ServiceRequest.objects.get(category__key="privacy-request")
    assert privacy not in staff_requests(users["demo_technical"])
    assert privacy in staff_requests(users["demo_supervisor"])

    clinic = portal_requests(users["demo_client"])
    assert clinic.exists() and set(clinic.values_list("client__account_number", flat=True)) == {
        "EXCLIN"
    }
    assert clinic.filter(channel=Channel.PORTAL, created_by=users["demo_client"]).exists()

    water = portal_requests(users["demo_client_water"])
    south = ServiceRequest.objects.get(subject="Bottle kits for the south plant")
    assert south not in water  # scoped to the north plant
    assert water.filter(site__code="NORTH").count() >= 2
    # Each portal account has its own conversation: replies from both sides it may read.
    for account in ("demo_client", "demo_client_water"):
        visible = portal_requests(users[account])
        assert visible.filter(
            entries__kind=EntryKind.REPLY, entries__author__client__isnull=True
        ).exists(), account
    assert (
        water.filter(entries__author=users["demo_client_water"], entries__kind="reply").count() >= 2
    )  # raised it and answered the laboratory


def fingerprint():
    from request_forms.models import RequestTemplate

    rows = annotate_clock_state(ServiceRequest.objects.order_by("pk"))
    requests = [
        (r.number, r.subject, r.status, r.priority, r.channel, r.clock_state, r.entries.count())
        for r in rows
    ]
    # Which organizations each request form is offered to (request_forms.seed).
    offers = sorted(
        (template.key, tuple(sorted(template.clients.values_list("account_number", flat=True))))
        for template in RequestTemplate.objects.all()
    )
    return requests, offers


@pytest.mark.django_db(transaction=True)
def test_two_resets_in_a_row_tell_the_same_story(monkeypatch):
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "separate-admin-password-only")
    call_command("reset_demo", interactive=False, verbosity=0)
    first = fingerprint()
    call_command("reset_demo", interactive=False, verbosity=0)
    assert fingerprint() == first and len(first[0]) >= 25
    assert dict(first[1])["material-request"] == ("EXRES",)


@pytest.mark.django_db
def test_requests_raised_with_forms(monkeypatch):
    from request_forms.models import FieldValue, FilledForm
    from request_forms.selectors import staff_values

    seed(monkeypatch)
    forms = FilledForm.objects.select_related("request", "request__created_by")
    keys = set(forms.values_list("template_key", flat=True))
    assert {
        "pickup",
        "urgent-pickup",
        "supply-order",
        "bottle-kit-order",
        "result-question",
        "technical-support",
        "result-status",
        "add-on",
    } <= keys
    # Each portal account raised at least one, so "Request again" has something to copy.
    raisers = {f.request.created_by.username for f in forms if f.request.channel == "portal"}
    assert {"demo_client", "demo_client_water"} <= raisers
    # Items with quantities, staff answers in shown fields, and a correction with its reason.
    assert FieldValue.objects.filter(kind="items", superseded_at__isnull=True).count() >= 2
    assert FieldValue.objects.filter(key="tracking", superseded_at__isnull=True).count() >= 2
    assert FieldValue.objects.filter(superseded_at__isnull=False).exists()
    # The phone call logged with details not known shows what is still needed.
    call = forms.get(template_key="result-status").request
    supervisor = User.objects.get(username="demo_supervisor")
    assert staff_values(call, supervisor).missing


@pytest.mark.django_db
def test_seeded_reviews_and_assignments_respect_team_membership(monkeypatch):
    from desk import services
    from teams.models import TeamMembership

    seed(monkeypatch)
    # Every recorded complaint review still counts: no reviewer was later the assignee.
    for request in ServiceRequest.objects.filter(complaint_review__reviewed_at__isnull=False):
        assert services.review_counts(request, request.complaint_review), request.number
    # Nobody is the assignee of a request in a team they do not belong to.
    for request in ServiceRequest.objects.exclude(assignee=None).exclude(status=Status.CLOSED):
        assert TeamMembership.objects.filter(team=request.team, user=request.assignee).exists(), (
            request.number
        )
