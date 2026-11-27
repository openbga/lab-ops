"""Emergency access (desk.emergency, audit_review.emergency): staff the laboratory names reveal the
patient information on one ticket for a short fixed time after giving a reason; supervisors and
reviewers are told; someone else reviews each use. Synthetic data only."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from audit_review import emergency as review
from core.models import AccessRecord, AuditEvent
from desk import emergency
from desk.models import EmergencyAccess, EmergencyReview
from jobs.models import Notice
from links.services import add_link
from links.tests.conftest import PATIENT_KEY
from links.tests.conftest import lis as lis
from request_forms.models import FieldValue
from request_forms.tests import conftest as fixtures
from request_forms.tests.conftest import PATIENT_SENTINEL
from request_forms.tests.test_pages import raised

pytestmark = pytest.mark.django_db

make_user = fixtures.make_user
catalog = fixtures.catalog
group = fixtures.group
logistics = fixtures.logistics
pickup_category = fixtures.pickup_category
pickup = fixtures.pickup
clinic = fixtures.clinic
portal_user = fixtures.portal_user
capable = fixtures.capable

EXPLANATION = "Dr Example of the synthetic clinic called about a critical potassium value."


def grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(codename=codename, content_type__app_label="accounts")
    )


@pytest.fixture
def patient_ticket(portal_user, pickup, capable, lis):
    ticket = raised(portal_user, pickup, {"count": 1, "patient": PATIENT_SENTINEL})
    row = FieldValue.objects.get(filled_form__request=ticket, key="patient")
    link, _ = add_link(
        actor=capable,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    return ticket, row, link


@pytest.fixture
def eligible(make_user):
    user = make_user("ea_agent", "client_services")
    grant(user, "use_emergency_access")
    return user


@pytest.fixture
def reviewer(make_user):
    user = make_user("ea_reviewer", "technical")
    grant(user, "review_audit_records")
    return user


@pytest.fixture
def supervisor(make_user):
    return make_user("ea_supervisor", "supervisor")


def start_url(ticket):
    return reverse("desk:request-emergency", args=[ticket.pk])


def reveal_urls(row, link):
    return (
        reverse("request_details:reveal", args=[row.pk]),
        reverse("link_records:attachment-reveal", args=[link.pk]),
    )


def started(user, ticket, **extra):
    return emergency.start(
        actor=user,
        ticket_id=ticket.pk,
        reason=extra.get("reason", "critical_result"),
        explanation=extra.get("explanation", EXPLANATION),
    )


def ended_use(user, ticket, minutes_ago=5):
    """A use whose end has passed, written as rows are (the table refuses updates)."""
    now = timezone.now()
    return EmergencyAccess.objects.create(
        user=user,
        user_name=user.username,
        user_username=user.username,
        user_roles="Client services",
        ticket=ticket,
        kind="patient_information",
        reason="other",
        explanation="A synthetic emergency.",
        started_at=now - timedelta(minutes=emergency.MINUTES + minutes_ago),
        ends_at=now - timedelta(minutes=minutes_ago),
    )


def test_an_eligible_person_starts_it_and_reveals_on_that_ticket_only(
    client, eligible, patient_ticket, portal_user, pickup, supervisor, reviewer
):
    ticket, row, link = patient_ticket
    other = raised(portal_user, pickup, {"count": 2, "patient": "PATIENT-OTHER-TEST"})
    client.force_login(eligible)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    assert "Use emergency access" in page and PATIENT_SENTINEL not in page
    for url in reveal_urls(row, link):
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 404

    form = client.get(start_url(ticket))
    assert form.status_code == 200 and "Start emergency access" in form.content.decode()
    response = client.post(
        start_url(ticket), {"reason": "critical_result", "explanation": EXPLANATION}
    )
    assert response.status_code == 302
    use = EmergencyAccess.objects.get()
    assert (use.user_id, use.ticket_id, use.reason) == (eligible.pk, ticket.pk, "critical_result")
    assert use.ends_at - use.started_at == timedelta(minutes=emergency.MINUTES)

    page = client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    assert "You can reveal the patient information on this ticket until" in page
    for url, secret in zip(reveal_urls(row, link), (PATIENT_SENTINEL, PATIENT_KEY), strict=True):
        answer = client.post(url, HTTP_HX_REQUEST="true")
        assert answer.status_code == 200 and secret in answer.content.decode()
    events = AuditEvent.objects.filter(action__in=review.REVEAL_ACTIONS, actor=eligible)
    assert sorted(e.after["emergency"] for e in events) == [use.pk, use.pk]
    assert (
        AccessRecord.objects.filter(account_id=eligible.pk, ticket_id=ticket.pk)
        .filter(kind__in=["patient_answer", "person_key"])
        .count()
        == 2
    )

    other_row = FieldValue.objects.get(filled_form__request=other, key="patient")
    reveal_other = reverse("request_details:reveal", args=[other_row.pk])
    assert client.post(reveal_other, HTTP_HX_REQUEST="true").status_code == 404

    started_event = AuditEvent.objects.get(action="emergency_access.started")
    assert EXPLANATION not in str(started_event.after) and not started_event.reason
    told = set(Notice.objects.filter(topic=emergency.TOPIC).values_list("recipient_id", flat=True))
    assert supervisor.pk in told and reviewer.pk in told and eligible.pk not in told


def test_without_the_capability_the_page_and_the_start_are_refused(
    client, make_user, patient_ticket
):
    ticket, _, _ = patient_ticket
    agent = make_user("ea_plain", "client_services")
    client.force_login(agent)
    assert client.get(start_url(ticket)).status_code == 403
    response = client.post(start_url(ticket), {"reason": "other", "explanation": "x"})
    assert response.status_code == 403
    assert not EmergencyAccess.objects.exists()
    page = client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    assert start_url(ticket) not in page  # no offer (the page help may name the action)


def test_a_holder_of_the_patient_capability_needs_none(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    grant(eligible, "view_patient_information")
    client.force_login(eligible)
    assert "without emergency access" in client.get(start_url(ticket)).content.decode()
    response = client.post(start_url(ticket), {"reason": "other", "explanation": "x"})
    assert response.status_code == 200 and not EmergencyAccess.objects.exists()


def test_a_second_start_while_one_is_in_force_starts_nothing(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    started(eligible, ticket)
    client.force_login(eligible)
    response = client.post(start_url(ticket), {"reason": "other", "explanation": "again"})
    assert response.status_code == 302 and EmergencyAccess.objects.count() == 1
    assert "in force until" in client.get(start_url(ticket)).content.decode()


def test_an_ended_use_and_a_removed_capability_give_nothing(client, eligible, patient_ticket):
    ticket, row, link = patient_ticket
    ended_use(eligible, ticket)
    client.force_login(eligible)
    for url in reveal_urls(row, link):
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 404
    started(eligible, ticket)
    eligible.user_permissions.clear()
    for url in reveal_urls(row, link):
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 404
    assert not AuditEvent.objects.filter(action__in=review.REVEAL_ACTIONS).exists()


def test_a_ticket_the_person_cannot_read_answers_404(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    category = ticket.category
    category.is_restricted = True
    category.save()
    client.force_login(eligible)
    assert client.get(start_url(ticket)).status_code == 404
    assert (
        client.post(start_url(ticket), {"reason": "other", "explanation": "x"}).status_code == 404
    )


def test_the_notice_links_reviewers_to_the_use_and_supervisors_to_the_ticket(
    eligible, patient_ticket, supervisor, reviewer
):
    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    notices = {n.recipient_id: n for n in Notice.objects.filter(topic=emergency.TOPIC)}
    for_reviewer = emergency.prepare_emergency_notice(notices[reviewer.pk])
    for_supervisor = emergency.prepare_emergency_notice(notices[supervisor.pk])
    assert reverse("access_review:emergency-use", args=[use.pk]).replace("/", "%2F") in (
        for_reviewer.body
    )
    assert reverse("desk:request-access", args=[ticket.pk]).replace("/", "%2F") in (
        for_supervisor.body
    )
    for prepared in (for_reviewer, for_supervisor):
        assert ticket.number in prepared.subject
        assert EXPLANATION not in prepared.body and eligible.username not in prepared.body
        assert PATIENT_SENTINEL not in prepared.body
    supervisor.groups.clear()
    assert emergency.prepare_emergency_notice(notices[supervisor.pk]) is None


def test_a_reviewer_records_a_review_once_the_use_has_ended(
    client, eligible, patient_ticket, reviewer
):
    ticket, _, _ = patient_ticket
    in_force = started(eligible, ticket)
    client.force_login(reviewer)
    listing = client.get(reverse("access_review:emergency"))
    assert listing.status_code == 200 and in_force.number in listing.content.decode()
    page = client.get(reverse("access_review:emergency-use", args=[in_force.pk])).content.decode()
    assert EXPLANATION in page and 'name="outcome"' not in page
    refused = client.post(
        reverse("access_review:emergency-use", args=[in_force.pk]),
        {"outcome": "appropriate", "note": "Checked with the caller."},
    )
    assert refused.status_code == 200 and review.IN_FORCE in refused.content.decode()

    past = ended_use(eligible, ticket)
    page = client.get(reverse("access_review:emergency-use", args=[past.pk])).content.decode()
    assert 'name="outcome"' in page
    response = client.post(
        reverse("access_review:emergency-use", args=[past.pk]),
        {"outcome": "appropriate", "note": "Checked with the caller."},
    )
    assert response.status_code == 302
    recorded = EmergencyReview.objects.get(use=past)
    assert (recorded.reviewed_by_id, recorded.outcome) == (reviewer.pk, "appropriate")
    again = client.post(
        reverse("access_review:emergency-use", args=[past.pk]),
        {"outcome": "not_appropriate", "note": "Second opinion."},
    )
    assert again.status_code == 200 and EmergencyReview.objects.count() == 1


def test_the_person_who_used_it_cannot_review_it(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    grant(eligible, "review_audit_records")
    past = ended_use(eligible, ticket)
    client.force_login(eligible)
    page = client.get(reverse("access_review:emergency-use", args=[past.pk])).content.decode()
    assert "someone else reviews it" in page
    response = client.post(
        reverse("access_review:emergency-use", args=[past.pk]),
        {"outcome": "appropriate", "note": "Mine."},
    )
    assert response.status_code == 200 and not EmergencyReview.objects.exists()


def test_the_tab_is_for_reviewers_only(client, eligible, supervisor, patient_ticket):
    ticket, _, _ = patient_ticket
    past = ended_use(eligible, ticket)
    for account in (eligible, supervisor):
        client.force_login(account)
        assert client.get(reverse("access_review:emergency")).status_code == 403
        url = reverse("access_review:emergency-use", args=[past.pk])
        assert client.get(url).status_code == 403
        assert client.post(url, {"outcome": "appropriate", "note": "x"}).status_code == 403
    assert not EmergencyReview.objects.exists()


def test_the_database_keeps_uses_and_reviews_as_written(eligible, patient_ticket, reviewer):
    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    with pytest.raises(DatabaseError), transaction.atomic():
        EmergencyAccess.objects.filter(pk=use.pk).update(explanation="changed")
    with pytest.raises(DatabaseError), transaction.atomic():
        EmergencyAccess.objects.filter(pk=use.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        EmergencyReview.objects.create(
            use=use,
            reviewed_by=reviewer,
            reviewer_name="r",
            reviewer_username="r",
            reviewer_roles="",
            outcome="appropriate",
            note="still in force",
            reviewed_at=timezone.now(),
        )
    past = ended_use(eligible, ticket)
    with pytest.raises(DatabaseError), transaction.atomic():
        EmergencyReview.objects.create(
            use=past,
            reviewed_by=eligible,
            reviewer_name="self",
            reviewer_username="self",
            reviewer_roles="",
            outcome="appropriate",
            note="own use",
            reviewed_at=timezone.now(),
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    assert not EmergencyReview.objects.exists()


def test_a_portal_account_never_has_emergency_access(client, portal_user, patient_ticket):
    ticket, _, _ = patient_ticket
    client.force_login(portal_user)
    assert client.get(start_url(ticket)).status_code in (403, 404)
    assert not emergency.is_eligible(portal_user)


def test_supervisors_are_told_through_the_admin_role_too(make_user, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    admin = make_user("ea_admin", "admin")
    use = started(eligible, ticket)
    assert admin.pk in {account.pk for account in emergency.told(use)}
    admin.groups.set([Group.objects.get_or_create(name="technical")[0]])
    assert admin.pk not in {account.pk for account in emergency.told(use)}


def test_a_ticket_without_hidden_patient_information_offers_and_starts_nothing(
    client, eligible, portal_user, pickup
):
    plain = raised(portal_user, pickup, {"count": 3})
    client.force_login(eligible)
    page = client.get(reverse("desk:request-detail", args=[plain.pk])).content.decode()
    assert start_url(plain) not in page
    drawn = client.get(start_url(plain)).content.decode()
    assert "Nothing to reveal" in drawn and 'name="explanation"' not in drawn
    refused = client.post(start_url(plain), {"reason": "other", "explanation": EXPLANATION})
    assert refused.status_code == 200 and not EmergencyAccess.objects.exists()
    with pytest.raises(ValidationError):
        started(eligible, plain)


@pytest.mark.confirmation
def test_access_is_refused_before_any_password_is_checked(client, make_user, patient_ticket):
    from accounts.models import SignInEvent

    ticket, _, _ = patient_ticket
    agent = make_user("ea_plain_confirm", "client_services")
    client.force_login(agent)
    before = SignInEvent.objects.count()
    response = client.post(
        start_url(ticket),
        {"reason": "other", "explanation": "x", "confirm_password": "not-the-password-1"},
    )
    assert response.status_code == 403
    assert SignInEvent.objects.count() == before  # no failed re-authentication recorded


def test_the_links_panel_under_emergency_access_offers_only_reveal(
    client, eligible, patient_ticket, capable, lis
):
    from links.models import LinkAttachment
    from links.services import remove_link

    ticket, _, first = patient_ticket
    ticket.refresh_from_db()
    remove_link(
        actor=capable,
        attachment_id=first.pk,
        expected_revision=ticket.revision,
        reason_code="entered_in_error",
        note="A synthetic removal note.",
    )
    current, _ = add_link(
        actor=capable,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier="PT-TEST-CURRENT-2",
    )
    started(eligible, ticket)
    client.force_login(eligible)
    page = client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    attachment = LinkAttachment.objects.get(pk=current.pk)
    assert reverse("link_records:attachment-reveal", args=[current.pk]) in page
    assert reverse("link_records:link-detail", args=[attachment.link_id]) not in page
    assert reverse("link_records:attachment-remove", args=[current.pk]) not in page
    assert "A synthetic removal note." not in page
    answer = client.post(
        reverse("link_records:attachment-reveal", args=[current.pk]), HTTP_HX_REQUEST="true"
    )
    assert answer.status_code == 200 and "PT-TEST-CURRENT-2" in answer.content.decode()


def test_the_scheduled_jobs_page_names_the_ticket_of_a_failed_emergency_notice(
    client, make_user, eligible, patient_ticket, supervisor
):
    from jobs.models import NoticeStatus

    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    Notice.objects.filter(topic=emergency.TOPIC).update(
        status=NoticeStatus.FAILED, attempts=1, last_error="Synthetic failure."
    )
    admin = make_user("ea_jobs_admin", "admin")
    client.force_login(admin)
    page = client.get(reverse("jobs:overview")).content.decode()
    assert ticket.number in page and "Emergency access used" in page
    assert use.explanation not in page


def test_the_workspace_use_and_changes_lists_name_the_use(
    client, eligible, patient_ticket, reviewer
):
    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    client.force_login(reviewer)
    client.get(reverse("access_review:emergency"), {"status": "in_force"})
    client.get(reverse("access_review:emergency-use", args=[use.pk]))
    uses = client.get(reverse("audit_review:uses")).content.decode()
    assert "Status: In force" in uses and f"Emergency access {use.number}" in uses
    changes = client.get(
        reverse("audit_review:changes"), {"ticket": ticket.number}
    ).content.decode()
    assert f"Emergency access {use.number} on {ticket.number}" in changes
