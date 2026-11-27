"""Regression coverage for emergency access to the patient information on one synthetic ticket."""

from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse

from accounts.roles import ROLES
from audit_review import emergency as review
from core.models import AccessRecord, AuditEvent
from desk import emergency
from desk.models import EmergencyAccess, EmergencyReview
from desk.tests import test_emergency_access as cases
from desk.tests.test_emergency_access import ended_use, grant, reveal_urls, start_url, started
from jobs.actor import system_account
from links.services import reveal_person_key
from links.tests.conftest import PATIENT_KEY
from request_forms.services import reveal_value
from request_forms.tests.conftest import PASSWORD, PATIENT_SENTINEL

pytestmark = pytest.mark.django_db

# Reuse the feature suite's synthetic ticket, form, link and staff fixtures.
make_user = cases.make_user
catalog = cases.catalog
group = cases.group
logistics = cases.logistics
pickup_category = cases.pickup_category
pickup = cases.pickup
clinic = cases.clinic
portal_user = cases.portal_user
capable = cases.capable
lis = cases.lis
patient_ticket = cases.patient_ticket
eligible = cases.eligible
reviewer = cases.reviewer
supervisor = cases.supervisor

START_FIELDS = {"reason": "other", "explanation": "Synthetic caller verification."}
REVIEW_FIELDS = {"outcome": "appropriate", "note": "Verified the synthetic handover."}


# Audiences and permissions


def _check_http(client, method, url, status, fields=None):
    response = client.post(url, fields or {}) if method == "post" else getattr(client, method)(url)
    assert response.status_code == status, (method.upper(), url, response.status_code)
    if method == "head":
        assert not response.content
    return response


def _staff_routes(ticket, row, attachment, past):
    return {
        "start": start_url(ticket),
        "answer": reveal_urls(row, attachment)[0],
        "key": reveal_urls(row, attachment)[1],
        "list": reverse("access_review:emergency"),
        "use": reverse("access_review:emergency-use", args=[past.pk]),
        "link": reverse("link_records:link-detail", args=[attachment.link_id]),
        "edit": reverse("link_records:link-edit", args=[attachment.link_id]),
        "remove": reverse("link_records:attachment-remove", args=[attachment.pk]),
        "search": reverse("link_records:person-search"),
    }


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("granted", [False, True], ids=["role-only", "emergency-grant"])
def test_staff_roles_and_the_emergency_grant_at_each_address(
    client, make_user, eligible, patient_ticket, role, granted
):
    ticket, row, attachment = patient_ticket
    actor = make_user("review_operator", role)
    if granted:
        grant(actor, "use_emergency_access")
    urls = _staff_routes(ticket, row, attachment, ended_use(eligible, ticket))
    client.force_login(actor)
    for method in ("get", "head"):
        _check_http(client, method, urls["start"], 200 if granted else 403)
    _check_http(client, "post", urls["start"], 302 if granted else 403, START_FIELDS)
    assert EmergencyAccess.objects.filter(user=actor).count() == int(granted)
    for name, value in (("answer", PATIENT_SENTINEL), ("key", PATIENT_KEY)):
        for method in ("get", "head"):
            _check_http(client, method, urls[name], 405)
        response = _check_http(client, "post", urls[name], 200 if granted else 404)
        assert (value.encode() in response.content) == granted
    for method in ("get", "head", "post"):
        for name in ("link", "edit", "remove"):
            _check_http(client, method, urls[name], 404)
        _check_http(client, method, urls["use"], 403, REVIEW_FIELDS)
        _check_http(client, method, urls["list"], 403 if method == "get" else 405)
        response = _check_http(client, method, urls["search"], 403 if method == "post" else 200)
        assert b'name="key"' not in response.content
    assert not EmergencyReview.objects.exists()


@pytest.mark.parametrize("audience", ["anonymous", "no-role", "portal", "inactive", "jobs"])
def test_staff_addresses_are_closed_outside_active_laboratory_roles(
    client, eligible, portal_user, make_user, patient_ticket, audience
):
    ticket, row, attachment = patient_ticket
    urls = _staff_routes(ticket, row, attachment, ended_use(eligible, ticket))
    if audience != "anonymous":
        if audience == "no-role":
            actor = make_user("review_no_role")
        elif audience == "inactive":
            actor = make_user("review_inactive", "technical", is_active=False)
        elif audience == "portal":
            actor = portal_user
        else:
            actor = system_account()
        if audience != "jobs":
            for code in (
                "use_emergency_access",
                "review_audit_records",
                "view_patient_information",
            ):
                grant(actor, code)
        client.force_login(actor)
    for name, url in urls.items():
        for method in ("get", "head", "post"):
            if audience in {"anonymous", "jobs", "inactive"}:
                expected = 302
            elif (name in {"answer", "key"} and method != "post") or (
                name == "list" and method != "get"
            ):
                expected = 405
            else:
                expected = 403
            response = _check_http(client, method, url, expected, START_FIELDS)
            if expected == 302:
                assert urlsplit(response["Location"]).path == reverse("account_login")
            assert PATIENT_SENTINEL.encode() not in response.content
            assert PATIENT_KEY.encode() not in response.content
    assert EmergencyAccess.objects.count() == 1
    assert not EmergencyReview.objects.exists()
    assert not AuditEvent.objects.filter(action__in=review.REVEAL_ACTIONS).exists()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("granted", [False, True], ids=["ungranted", "granted"])
def test_direct_services_use_separate_emergency_patient_and_review_permissions(
    role, granted, make_user, eligible, patient_ticket
):
    ticket, row, attachment = patient_ticket
    actor = make_user("review_service_operator", role)
    past = ended_use(eligible, ticket)
    if granted:
        grant(actor, "use_emergency_access")
        use = started(actor, ticket)
        assert reveal_value(actor=actor, value_id=row.pk).value == PATIENT_SENTINEL
        assert reveal_person_key(actor=actor, attachment_id=attachment.pk).identifier == PATIENT_KEY
        assert (
            AuditEvent.objects.filter(
                actor=actor, action__in=review.REVEAL_ACTIONS, after__emergency=use.pk
            ).count()
            == 2
        )
    else:
        with pytest.raises(PermissionDenied):
            started(actor, ticket)
        with pytest.raises(ObjectDoesNotExist):
            reveal_value(actor=actor, value_id=row.pk)
        with pytest.raises(PermissionDenied):
            reveal_person_key(actor=actor, attachment_id=attachment.pk)
    with pytest.raises(PermissionDenied):
        review.record_review(actor=actor, use_id=past.pk, **REVIEW_FIELDS)
    grant(actor, "review_audit_records")
    recorded = review.record_review(actor=actor, use_id=past.pk, **REVIEW_FIELDS)
    assert (recorded.use_id, recorded.reviewed_by_id) == (past.pk, actor.pk)
    grant(actor, "view_patient_information")
    with pytest.raises((PermissionDenied, ValidationError)):
        started(actor, ticket)
    assert reveal_value(actor=actor, value_id=row.pk).value == PATIENT_SENTINEL
    assert reveal_person_key(actor=actor, attachment_id=attachment.pk).identifier == PATIENT_KEY
    ordinary = AuditEvent.objects.filter(actor=actor, action__in=review.REVEAL_ACTIONS).exclude(
        after__has_key="emergency"
    )
    assert ordinary.count() == 2


@pytest.mark.parametrize("audience", ["anonymous", "no-role", "portal", "inactive", "jobs"])
def test_ineligible_actors_receive_permission_refusals_from_the_services(
    make_user, portal_user, eligible, patient_ticket, audience
):
    ticket, row, attachment = patient_ticket
    past = ended_use(eligible, ticket)
    if audience == "anonymous":
        actor = AnonymousUser()
    elif audience == "jobs":
        actor = system_account()
    else:
        if audience == "portal":
            actor = portal_user
        elif audience == "inactive":
            actor = make_user("review_service_inactive", "admin", is_active=False)
        else:
            actor = make_user("review_service_unassigned")
        for code in ("use_emergency_access", "review_audit_records", "view_patient_information"):
            grant(actor, code)
    with pytest.raises(PermissionDenied):
        review.record_review(actor=actor, use_id=past.pk, **REVIEW_FIELDS)
    with pytest.raises(PermissionDenied):
        reveal_value(actor=actor, value_id=row.pk)
    with pytest.raises(PermissionDenied):
        reveal_person_key(actor=actor, attachment_id=attachment.pk)
    with pytest.raises(PermissionDenied):
        started(actor, ticket)


def test_patient_grant_opens_links_and_search_independently_of_emergency_access(
    client, capable, eligible, patient_ticket
):
    ticket, row, attachment = patient_ticket
    urls = _staff_routes(ticket, row, attachment, ended_use(eligible, ticket))
    client.force_login(capable)
    for method in ("get", "head", "post"):
        _check_http(client, method, urls["start"], 403, START_FIELDS)
        for name in ("link", "edit", "remove"):
            _check_http(client, method, urls[name], 200)
        result = _check_http(
            client, method, urls["search"], 200, {"key": PATIENT_KEY} if method == "post" else {}
        )
        if method == "post":
            assert ticket.number.encode() in result.content
    grant(capable, "use_emergency_access")
    for method in ("get", "head", "post"):
        response = _check_http(client, method, urls["start"], 200, START_FIELDS)
        if method != "head":
            assert b"without emergency access" in response.content
            assert b'name="explanation"' not in response.content
    assert not EmergencyAccess.objects.filter(user=capable).exists()


@pytest.mark.parametrize("membership", ["direct", "role-group", "staff-role"])
def test_restoring_eligibility_reactivates_the_original_use(
    client, make_user, patient_ticket, membership
):
    from django.contrib.auth.models import Permission

    ticket, row, attachment = patient_ticket
    operator = make_user("review_restored", "technical")
    role = operator.groups.get()
    permission = Permission.objects.get(
        codename="use_emergency_access", content_type__app_label="accounts"
    )
    assignment = role.permissions if membership == "role-group" else operator.user_permissions
    assignment.add(permission)
    use = started(operator, ticket)
    client.force_login(operator)
    if membership == "staff-role":
        operator.groups.remove(role)
    else:
        assignment.remove(permission)
    for url in reveal_urls(row, attachment):
        _check_http(client, "post", url, 403 if membership == "staff-role" else 404)
    assert not AuditEvent.objects.filter(actor=operator, action__in=review.REVEAL_ACTIONS).exists()
    if membership == "staff-role":
        operator.groups.add(role)
    else:
        assignment.add(permission)
    for url, value in zip(
        reveal_urls(row, attachment), (PATIENT_SENTINEL, PATIENT_KEY), strict=True
    ):
        assert value.encode() in _check_http(client, "post", url, 200).content
    assert (
        AuditEvent.objects.filter(
            actor=operator, action__in=review.REVEAL_ACTIONS, after__emergency=use.pk
        ).count()
        == 2
    )
    assert EmergencyAccess.objects.filter(user=operator).get().ends_at == use.ends_at


@pytest.mark.parametrize("role", ROLES)
def test_staff_with_review_capability_can_record_a_review_via_the_tab(
    client, make_user, eligible, patient_ticket, role
):
    from audit_review.models import WorkspaceUse

    operator = make_user("review_tab_reader", role)
    grant(operator, "review_audit_records")
    use = ended_use(eligible, patient_ticket[0])
    client.force_login(operator)
    url = reverse("access_review:emergency-use", args=[use.pk])
    index = reverse("access_review:emergency")
    _check_http(client, "head", url, 200)
    assert not WorkspaceUse.objects.filter(account=operator).exists()
    for method in ("head", "post"):
        _check_http(client, method, index, 405)
    assert use.number.encode() in _check_http(client, "get", index, 200).content
    assert b'name="outcome"' in _check_http(client, "get", url, 200).content
    _check_http(client, "post", url, 302, REVIEW_FIELDS)
    assert EmergencyReview.objects.get(use=use).reviewed_by_id == operator.pk


# Start form, confirmation and stale submissions


@pytest.mark.confirmation
@pytest.mark.parametrize("change", ["emergency-grant-removed", "ticket-left-scope"])
def test_start_refuses_lost_access_before_checking_the_password(
    client, eligible, patient_ticket, change, monkeypatch
):
    from accounts import confirmation
    from accounts.models import SignInEvent

    ticket, _, _ = patient_ticket
    client.force_login(eligible)
    url = start_url(ticket)
    assert b'name="confirm_password"' in client.get(url).content
    if change == "ticket-left-scope":
        ticket.category.is_restricted = True
        ticket.category.save(update_fields=["is_restricted"])
        status = 404
    else:
        eligible.user_permissions.clear()
        status = 403

    def unexpected_password_check(*args, **kwargs):
        pytest.fail("A refused start reached password verification")

    monkeypatch.setattr(confirmation.ConfirmIdentity, "_confirm", unexpected_password_check)
    before = SignInEvent.objects.count()
    _check_http(client, "post", url, status, {**START_FIELDS, "confirm_password": PASSWORD})
    assert SignInEvent.objects.count() == before
    assert not EmergencyAccess.objects.exists()


def test_start_form_open_in_two_browsers_creates_one_use(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    url = start_url(ticket)
    second = Client()
    for session in (client, second):
        session.force_login(eligible)
        assert b'name="explanation"' in session.get(url).content
    for session, note in ((client, "First synthetic handover."), (second, "Another handover.")):
        _check_http(session, "post", url, 302, {**START_FIELDS, "explanation": note})
    assert EmergencyAccess.objects.get().explanation == "First synthetic handover."
    assert AuditEvent.objects.filter(action="emergency_access.started").count() == 1


def test_start_redraws_not_needed_when_patient_grant_arrives(client, eligible, patient_ticket):
    ticket, _, _ = patient_ticket
    client.force_login(eligible)
    url = start_url(ticket)
    assert b'name="explanation"' in client.get(url).content
    grant(eligible, "view_patient_information")
    refused = _check_http(client, "post", url, 200, START_FIELDS)
    assert b"without emergency access" in refused.content
    assert b'name="explanation"' not in refused.content
    assert not EmergencyAccess.objects.exists()


def test_start_redraws_nothing_to_reveal_after_last_person_link_is_removed(
    client, eligible, portal_user, pickup, capable, lis
):
    from links.services import add_link, remove_link
    from request_forms.tests.test_pages import raised

    ticket = raised(portal_user, pickup, {"count": 1})
    attachment, _ = add_link(
        actor=capable,
        request_id=ticket.pk,
        system=lis,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    client.force_login(eligible)
    url = start_url(ticket)
    assert b'name="explanation"' in client.get(url).content
    ticket.refresh_from_db()
    remove_link(
        actor=capable,
        attachment_id=attachment.pk,
        expected_revision=ticket.revision,
        reason_code="entered_in_error",
        note="Synthetic record was linked in error.",
    )
    refused = _check_http(client, "post", url, 200, START_FIELDS)
    assert b"Nothing to reveal" in refused.content and b'name="explanation"' not in refused.content
    assert not EmergencyAccess.objects.exists()


@pytest.mark.confirmation
def test_start_confirmation_counts_for_reveals_only_in_the_browser_that_gave_it(
    client, eligible, patient_ticket, make_user
):
    from core.reveal_confirmation import save_rule

    ticket, row, attachment = patient_ticket
    manager = make_user("review_rule_manager", "admin")
    save_rule(actor=manager, first_reveal=True, expected_revision=0, reason="Synthetic drill.")
    second = Client()
    for session in (client, second):
        session.force_login(eligible)
    _check_http(
        client, "post", start_url(ticket), 302, {**START_FIELDS, "confirm_password": PASSWORD}
    )
    urls = reveal_urls(row, attachment)
    for url in urls:
        held = _check_http(second, "post", url, 302)
        assert urlsplit(held["Location"]).path == reverse("account-confirm-reveal")
    assert not AuditEvent.objects.filter(action__in=review.REVEAL_ACTIONS).exists()
    for url, value in zip(urls, (PATIENT_SENTINEL, PATIENT_KEY), strict=True):
        assert value.encode() in _check_http(client, "post", url, 200).content
    assert AuditEvent.objects.filter(action__in=review.REVEAL_ACTIONS, actor=eligible).count() == 2
    assert (
        AccessRecord.objects.filter(
            account_id=eligible.pk, kind__in=["patient_answer", "person_key"]
        ).count()
        == 2
    )


def test_start_and_review_validate_reason_outcome_and_trim_their_free_text(
    client, eligible, reviewer, patient_ticket
):
    ticket = patient_ticket[0]
    client.force_login(eligible)
    url = start_url(ticket)
    for changes, field_name in (
        ({"reason": "retired_reason"}, "reason"),
        ({"explanation": "  "}, "explanation"),
        ({"explanation": "x" * 1001}, "explanation"),
    ):
        fields = {**START_FIELDS, **changes}
        refused = _check_http(client, "post", url, 200, fields)
        assert refused.context["form"].has_error(field_name)
        with pytest.raises(ValidationError) as error:
            emergency.start(actor=eligible, ticket_id=ticket.pk, **fields)
        assert field_name in error.value.message_dict
    assert not EmergencyAccess.objects.exists()
    assert not AuditEvent.objects.filter(action="emergency_access.started").exists()
    _check_http(client, "post", url, 302, {**START_FIELDS, "explanation": " " + "x" * 1000 + " "})
    assert EmergencyAccess.objects.get().explanation == "x" * 1000

    use = ended_use(eligible, ticket)
    client.force_login(reviewer)
    url = reverse("access_review:emergency-use", args=[use.pk])
    for changes, field_name in (
        ({"outcome": "retired_outcome"}, "outcome"),
        ({"note": "  "}, "note"),
        ({"note": "x" * 2001}, "note"),
    ):
        fields = {**REVIEW_FIELDS, **changes}
        refused = _check_http(client, "post", url, 200, fields)
        assert refused.context["form"].has_error(field_name)
        with pytest.raises(ValidationError) as error:
            review.record_review(actor=reviewer, use_id=use.pk, **fields)
        assert field_name in error.value.message_dict
    assert not EmergencyReview.objects.exists()
    assert not AuditEvent.objects.filter(action="emergency_access.reviewed").exists()
    _check_http(client, "post", url, 302, {**REVIEW_FIELDS, "note": " " + "x" * 2000 + " "})
    assert EmergencyReview.objects.get().note == "x" * 2000


# Ticket scope and correction history


def test_scope_cleared_answer_leaves_nothing_to_offer_or_start(
    client, eligible, capable, pickup, make_user
):
    """Clearing an ordinary field, then marking it, exercises the browser workflow. An
    already marked field left blank in the correction form retains its old answer."""
    from jobs.models import Notice
    from request_forms.models import FieldValue
    from request_forms.services import update_field
    from request_forms.tests.test_pages import raised

    ticket = raised(capable, pickup, {"count": 1, "tracking": "Example delivery"})
    edit = reverse("request_details:edit", args=[ticket.number])
    client.force_login(capable)
    form = client.get(edit).context["form"]
    data = {control.name: control.value() or "" for control in form.hidden_fields()}
    cleared = client.post(edit, {**data, "field-tracking": "", "reason": "Delivery cancelled."})
    assert cleared.status_code == 302
    row = FieldValue.objects.get(filled_form__request=ticket, key="tracking", superseded_at=None)
    assert row.value is None
    field = row.field
    update_field(
        actor=make_user("example_form_admin", "admin"),
        field_id=field.pk,
        expected_revision=field.revision,
        reason="The laboratory now treats this field as patient information.",
        holds_patient_information=True,
    )
    client.force_login(eligible)
    detail = client.get(reverse("desk:request-detail", args=[ticket.pk]))
    drawn = client.get(start_url(ticket))
    submitted = client.post(start_url(ticket), START_FIELDS)
    assert (detail.status_code, drawn.status_code) == (200, 200)
    actual = {
        "offer": start_url(ticket) in detail.content.decode(),
        "start_form": 'name="explanation"' in drawn.content.decode(),
        "start_status": submitted.status_code,
        "uses": EmergencyAccess.objects.filter(ticket=ticket).count(),
        "notices": Notice.objects.filter(topic=emergency.TOPIC, reference=ticket.number).count(),
    }
    assert actual == {
        "offer": False,
        "start_form": False,
        "start_status": 200,
        "uses": 0,
        "notices": 0,
    }


def _scope_correct_patient(actor, ticket, old_row, value, note):
    from request_forms.services import correct_values
    from request_forms.tests.conftest import definition_of

    return correct_values(
        actor=actor,
        request=ticket,
        expected_definition=definition_of(ticket),
        expected={"patient": old_row.pk},
        values={"patient": value},
        reason="",
        reason_code="typing",
        patient_note=note,
    )


def test_scope_earlier_corrections_show_metadata_only_and_patient_controls_stay_absent(
    client, eligible, capable, patient_ticket
):
    from request_forms.models import FieldValue

    ticket, old_row, _ = patient_ticket
    note = "Confirmed with the synthetic clinic."
    replacement = "EXAMPLE-CORRECTED"
    assert _scope_correct_patient(capable, ticket, old_row, replacement, note) == ["patient"]
    current = FieldValue.objects.get(filled_form__request=ticket, key="patient", superseded_at=None)
    client.force_login(eligible)
    edit = reverse("request_details:edit", args=[ticket.number])
    before = client.get(edit)
    assert before.status_code == 200
    assert note not in before.content.decode()

    started(eligible, ticket)
    after = client.get(edit)
    html = after.content.decode()
    assert after.status_code == 200
    assert "Typing error" in html and note in html and capable.username in html
    assert all(value not in html for value in (old_row.value, replacement))
    assert 'name="field-patient"' not in html and 'name="patient_note"' not in html
    with pytest.raises(PermissionDenied):
        _scope_correct_patient(eligible, ticket, current, "EXAMPLE-OTHER", "Second check.")
    assert client.post(reverse("request_details:reveal", args=[old_row.pk])).status_code == 404
    assert not AuditEvent.objects.filter(
        actor=eligible, action="request_form.value_revealed"
    ).exists()
    received = client.post(reverse("request_details:reveal", args=[current.pk]))
    assert received.status_code == 200 and replacement in received.content.decode()


def test_scope_correction_note_guidance_describes_its_emergency_readers(
    client, eligible, capable, patient_ticket
):
    """Compare the guidance shown to the author with who can subsequently read the note."""
    ticket, old_row, _ = patient_ticket
    edit = reverse("request_details:edit", args=[ticket.number])
    client.force_login(capable)
    drawing = client.get(edit)
    assert drawing.status_code == 200
    help_text = drawing.context["form"].fields["patient_note"].help_text
    assert help_text in drawing.content.decode()

    note = "The example clinic confirmed the correction."
    _scope_correct_patient(capable, ticket, old_row, "EXAMPLE-UPDATED", note)
    started(eligible, ticket)
    reader = Client()
    reader.force_login(eligible)
    shown = reader.get(edit)
    assert shown.status_code == 200 and note in shown.content.decode()
    assert "emergency access" in help_text.lower(), help_text


def test_scope_field_setup_explains_emergency_reveals_in_the_control_and_reference(
    client, eligible, patient_ticket, make_user
):
    ticket, row, _ = patient_ticket
    client.force_login(make_user("example_setup_reader", "admin"))
    setting = client.get(reverse("request_forms:field-edit", args=[row.field_id]))
    assert setting.status_code == 200
    field = setting.context["form"].fields["holds_patient_information"]
    reference = next(
        item["description"]
        for section in setting.context["page_documentation"]["sections"]
        for item in section["items"]
        if item["label"] == field.label
    )
    reader = Client()
    reader.force_login(eligible)
    started(eligible, ticket)
    shown = reader.post(reverse("request_details:reveal", args=[row.pk]))
    assert shown.status_code == 200 and row.value in shown.content.decode()
    guidance = {"field control": field.help_text, "field reference": reference}
    missing = [
        source for source, wording in guidance.items() if "emergency access" not in wording.lower()
    ]
    assert not missing, missing


def test_scope_non_javascript_reveals_on_closed_ticket_return_only_the_selected_item(
    client, eligible, capable, patient_ticket
):
    from desk.services import change_status

    ticket, row, attachment = patient_ticket
    started(eligible, ticket)
    ticket.refresh_from_db()
    for status in ("resolved", "closed"):
        ticket = change_status(
            actor=capable,
            request=ticket,
            expected_revision=ticket.revision,
            status=status,
            summary="Synthetic request complete.",
        )
    client.force_login(eligible)
    ticket_url = reverse("desk:request-detail", args=[ticket.pk])
    details = client.get(ticket_url)
    assert details.status_code == 200
    assert reverse("request_details:edit", args=[ticket.number]) not in details.content.decode()
    for url, expected, hidden in zip(
        reveal_urls(row, attachment),
        (PATIENT_SENTINEL, PATIENT_KEY),
        (PATIENT_KEY, PATIENT_SENTINEL),
        strict=True,
    ):
        assert url in details.content.decode() and hidden not in details.content.decode()
        response = client.post(url)
        html = response.content.decode()
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
        assert expected in html and hidden not in html
        assert "Back to the ticket" in html and f'href="{ticket_url}#' in html


@pytest.mark.parametrize("scope_change", ["restrict", "reassign"])
def test_scope_ticket_must_still_be_readable_for_each_reveal(
    scope_change, client, eligible, capable, patient_ticket, group
):
    from desk.services import assign, update_properties
    from taxonomy.models import RequestCategory
    from teams.models import Team, TeamMembership

    ticket, row, attachment = patient_ticket
    restricted = RequestCategory.objects.create(
        key="staff-check", name="Staff check", parent=group, is_restricted=True
    )
    target_team = Team.objects.create(slug="second-lab-team", name="Second lab team")
    ticket.refresh_from_db()
    if scope_change == "reassign":
        TeamMembership.objects.create(team=ticket.team, user=eligible)
        ticket = update_properties(
            actor=capable,
            request=ticket,
            expected_revision=ticket.revision,
            category=restricted,
            priority=ticket.priority,
        )
    started(eligible, ticket)
    client.force_login(eligible)
    url = reverse("desk:request-detail", args=[ticket.pk])
    before = client.get(url)
    assert before.status_code == 200
    for reveal_url in reveal_urls(row, attachment):
        assert reveal_url in before.content.decode()
    if scope_change == "restrict":
        update_properties(
            actor=capable,
            request=ticket,
            expected_revision=ticket.revision,
            category=restricted,
            priority=ticket.priority,
        )
    else:
        assign(
            actor=capable,
            request=ticket,
            expected_revision=ticket.revision,
            team=target_team,
            reason="Second team takes over.",
        )
    assert client.get(url).status_code == 404
    for reveal_url in reveal_urls(row, attachment):
        assert client.post(reveal_url).status_code == 404
    assert not AuditEvent.objects.filter(actor=eligible, action__in=review.REVEAL_ACTIONS).exists()
    assert not AccessRecord.objects.filter(
        account_id=eligible.pk, ticket_id=ticket.pk, kind__in=["patient_answer", "person_key"]
    ).exists()


def test_scope_shared_person_link_stays_specific_to_reader_and_current_attachment(
    client, eligible, capable, patient_ticket, portal_user, pickup, lis, make_user
):
    from links.services import add_link, remove_link
    from request_forms.tests.test_pages import raised

    ticket, row, first = patient_ticket
    other = raised(portal_user, pickup, {"count": 2})
    second, _ = add_link(
        actor=capable,
        system=lis,
        request_id=other.pk,
        record_type="patient",
        identifier=PATIENT_KEY,
    )
    assert second.link_id == first.link_id
    use = started(eligible, ticket)
    client.force_login(eligible)
    first_url = reverse("link_records:attachment-reveal", args=[first.pk])
    second_url = reverse("link_records:attachment-reveal", args=[second.pk])
    assert client.post(first_url).status_code == 200
    assert client.post(second_url).status_code == 404
    other_reader = make_user("second_lab_reader", "technical")
    grant(other_reader, "use_emergency_access")
    other_client = Client()
    other_client.force_login(other_reader)
    for url in reveal_urls(row, first):
        assert other_client.post(url).status_code == 404

    ticket.refresh_from_db()
    remove_link(
        actor=capable,
        attachment_id=first.pk,
        expected_revision=ticket.revision,
        reason_code="entered_in_error",
        note="Synthetic removal details.",
    )
    assert client.post(first_url).status_code == 404
    assert client.post(second_url).status_code == 404
    html = client.get(reverse("desk:request-detail", args=[ticket.pk])).content.decode()
    assert "Removed links" in html and "Patient linked" in html
    assert first_url not in html and "Synthetic removal details." not in html
    assert not AuditEvent.objects.filter(
        actor=other_reader, action__in=review.REVEAL_ACTIONS
    ).exists()
    assert AuditEvent.objects.filter(
        actor=eligible, action__in=review.REVEAL_ACTIONS
    ).get().after == {"emergency": use.pk, "request": ticket.pk, "attachment": first.pk}


def test_scope_general_search_and_organization_and_portal_timelines(
    client, eligible, patient_ticket, portal_user
):
    ticket, row, attachment = patient_ticket
    use = started(eligible, ticket)
    client.force_login(eligible)
    for url in reveal_urls(row, attachment):
        assert client.post(url).status_code == 200
    for term in (PATIENT_KEY, PATIENT_SENTINEL):
        result = client.get(reverse("desk:request-find"), {"q": term})
        assert result.status_code == 200
        assert not result.context["results"] and not result.context["identifier_ids"]
    timeline = client.get(reverse("clients:client-timeline", args=[ticket.client_id]))
    assert timeline.status_code == 200
    assert ticket.number in timeline.content.decode()
    assert all(
        value not in timeline.content.decode()
        for value in (PATIENT_KEY, PATIENT_SENTINEL, use.explanation)
    )

    portal = Client()
    portal.force_login(portal_user)
    detail = portal.get(reverse("portal:request-detail", args=[ticket.number]))
    assert detail.status_code == 200 and PATIENT_SENTINEL in detail.content.decode()
    assert all(
        value not in detail.content.decode()
        for value in (PATIENT_KEY, use.explanation, start_url(ticket))
    )
    assert all(url not in detail.content.decode() for url in reveal_urls(row, attachment))


@pytest.mark.django_db(transaction=True)
def test_scope_reveal_after_end_rechecks_database_clock_on_both_addresses(
    client, eligible, patient_ticket
):
    from django.db import transaction

    ticket, row, attachment = patient_ticket
    client.force_login(eligible)
    url = reverse("desk:request-detail", args=[ticket.pk])
    with transaction.atomic(), connection.cursor() as cursor:
        # A normal sixty-minute use in its last microsecond at this transaction's clock.
        # Exiting this reading advances the database clock for each independent reveal.
        cursor.execute("SELECT transaction_timestamp() + interval '1 microsecond'")
        ends_at = cursor.fetchone()[0]
        EmergencyAccess.objects.create(
            user=eligible,
            user_name=eligible.username,
            user_username=eligible.username,
            user_roles="Client services",
            ticket=ticket,
            kind="patient_information",
            reason="other",
            explanation="Synthetic handover.",
            ends_at=ends_at,
            started_at=ends_at - timedelta(minutes=60),
        )
        before = client.get(url)
        assert before.status_code == 200
        assert all(
            reveal_url in before.content.decode() for reveal_url in reveal_urls(row, attachment)
        )
    for reveal_url in reveal_urls(row, attachment):
        assert client.post(reveal_url).status_code == 404
    after = client.get(url)
    assert start_url(ticket) in after.content.decode()
    assert not any(
        reveal_url in after.content.decode() for reveal_url in reveal_urls(row, attachment)
    )
    assert not AuditEvent.objects.filter(actor=eligible, action__in=review.REVEAL_ACTIONS).exists()
    assert not AccessRecord.objects.filter(
        account_id=eligible.pk, ticket_id=ticket.pk, kind__in=["patient_answer", "person_key"]
    ).exists()


def test_emergency_access_only_grants_read_operations_on_a_person_link(
    eligible, patient_ticket, lis
):
    from links.models import LinkAttachment
    from links.services import add_link, remove_link, update_link

    ticket, _, attachment = patient_ticket
    started(eligible, ticket)
    with pytest.raises(PermissionDenied):
        add_link(
            actor=eligible,
            request_id=ticket.pk,
            system=lis,
            record_type="patient",
            identifier="SYNTHETIC-SECOND",
        )
    with pytest.raises((PermissionDenied, ObjectDoesNotExist)):
        remove_link(actor=eligible, attachment_id=attachment.pk, reason_code="entered_in_error")
    with pytest.raises((PermissionDenied, ObjectDoesNotExist)):
        update_link(
            actor=eligible,
            link_id=attachment.link_id,
            expected_revision=attachment.link.revision,
            display_label="Synthetic renamed label",
            reason_code="entered_in_error",
        )
    assert LinkAttachment.objects.filter(request=ticket, removed_at=None).get().pk == attachment.pk
    assert not AuditEvent.objects.filter(
        actor=eligible, action__startswith="external_link."
    ).exists()


# Concurrent changes and page readings


def _commit_while_emergency_operation_waits(change, operation, expected_lock):
    """Check that PostgreSQL queued the worker on this transaction's specified advisory lock."""
    from concurrent.futures import Future, ThreadPoolExecutor

    from django.db import transaction

    from tests.test_review_access_signoff import on_database_connection, waits_for_this_transaction

    worker_pid = Future()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change()
            pending = pool.submit(on_database_connection, worker_pid, operation)
            pid = worker_pid.result(timeout=10)
            assert waits_for_this_transaction(pid, pending)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT classid, objid FROM pg_locks WHERE pid = %s "
                    "AND locktype = 'advisory' AND NOT granted",
                    [pid],
                )
                assert cursor.fetchone() == expected_lock
        return pending.result(timeout=10)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["remove-capability", "deactivate"])
def test_start_queued_after_account_change_decides_from_committed_account(
    eligible, patient_ticket, change
):
    from accounts.acting import ACCESS_GATE
    from accounts.models import User

    ticket, _, _ = patient_ticket

    def change_account():
        account = User.objects.get(pk=eligible.pk)
        if change == "remove-capability":
            account.user_permissions.clear()
        else:
            account.is_active = False
            account.save(update_fields=["is_active"])

    def start_is_refused():
        with pytest.raises(PermissionDenied):
            started(eligible, ticket)

    _commit_while_emergency_operation_waits(
        change_account, start_is_refused, (ACCESS_GATE, eligible.pk)
    )
    assert not EmergencyAccess.objects.exists()
    assert not AuditEvent.objects.filter(action="emergency_access.started").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("which", ["answer", "key"])
def test_emergency_reveal_queued_after_capability_removal_records_nothing(
    eligible, patient_ticket, client, which
):
    from accounts.acting import ACCESS_GATE

    ticket, row, link = patient_ticket
    started(eligible, ticket)
    client.force_login(eligible)
    url = reveal_urls(row, link)[0 if which == "answer" else 1]
    response = _commit_while_emergency_operation_waits(
        eligible.user_permissions.clear,
        lambda: client.post(url),
        (ACCESS_GATE, eligible.pk),
    )
    assert response.status_code == 404
    assert not AuditEvent.objects.filter(actor=eligible, action__in=review.REVEAL_ACTIONS).exists()
    assert not AccessRecord.objects.filter(
        account_id=eligible.pk, kind__in=["patient_answer", "person_key"]
    ).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
@pytest.mark.parametrize("which", ["answer", "key"])
@pytest.mark.parametrize("save_first", [False, True], ids=["reveal-first", "save-first"])
def test_emergency_reveal_and_confirmation_rule_save_serialize_on_the_rule_lock(
    client, make_user, eligible, patient_ticket, which, save_first
):
    from core import reveal_confirmation

    ticket, row, link = patient_ticket
    use = started(eligible, ticket)
    admin = make_user("review_rule_admin", "admin")
    client.force_login(eligible)
    url = reveal_urls(row, link)[0 if which == "answer" else 1]

    def save_rule():
        _, changed = reveal_confirmation.save_rule(
            actor=admin, first_reveal=True, expected_revision=0, reason="Synthetic shift change."
        )
        assert changed

    def reveal():
        return client.post(url, HTTP_HX_REQUEST="true")

    events = AuditEvent.objects.filter(actor=eligible, action__in=review.REVEAL_ACTIONS)
    accesses = AccessRecord.objects.filter(
        account_id=eligible.pk, kind__in=["patient_answer", "person_key"]
    )
    if save_first:
        queued = _commit_while_emergency_operation_waits(
            save_rule, reveal, reveal_confirmation.RULE_LOCK
        )
        assert not events.exists() and not accesses.exists()
    else:

        def reveal_before_save():
            response = reveal()
            assert response.status_code == 200
            assert (
                PATIENT_SENTINEL if which == "answer" else PATIENT_KEY
            ).encode() in response.content

        _commit_while_emergency_operation_waits(
            reveal_before_save, save_rule, reveal_confirmation.RULE_LOCK
        )
        assert events.get().after["emergency"] == use.pk
        assert accesses.get().kind == ("patient_answer" if which == "answer" else "person_key")
        queued = reveal()

    assert queued.status_code == 204 and queued.content == b""
    assert queued["HX-Redirect"] == reverse("account-confirm-reveal")
    assert reveal_confirmation.rule().first_reveal is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("request_atomic", [False, True], ids=["default", "atomic-requests"])
def test_start_page_reads_account_name_and_emergency_use_in_one_snapshot(
    eligible, patient_ticket, client, monkeypatch, request_atomic
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connections, transaction

    from desk import emergency_views

    ticket, _, _ = patient_ticket
    eligible.first_name = "First shift"
    eligible.save(update_fields=["first_name"])
    writer = Client()
    client.force_login(eligible)
    writer.force_login(eligible)
    url = start_url(ticket)
    account_was_read, committed = Event(), Event()
    original = emergency_views.reading_account

    def after_reading_account(request):
        current = original(request)
        account_was_read.set()
        assert committed.wait(12), "The other browser did not finish its start"
        return current

    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", request_atomic)
    monkeypatch.setattr(emergency_views, "reading_account", after_reading_account)

    def draw():
        try:
            return client.get(url)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        drawing = pool.submit(draw)
        try:
            assert account_was_read.wait(12), "The first browser did not reach the page reading"
            with transaction.atomic():
                eligible.first_name = "Second shift"
                eligible.save(update_fields=["first_name"])
                assert writer.post(url, START_FIELDS).status_code == 302
        finally:
            committed.set()
        response = drawing.result(timeout=15)

    assert response.status_code == 200
    visible = (
        b"First shift" in response.content,
        b"Second shift" in response.content,
        b'name="explanation"' in response.content,
        b"is in force until <time" in response.content,
    )
    assert visible in {(True, False, True, False), (False, True, False, True)}
    current = client.get(url)
    assert current.status_code == 200 and b"Second shift" in current.content
    assert (
        b"is in force until <time" in current.content
        and b'name="explanation"' not in current.content
    )


@pytest.mark.django_db(transaction=True)
def test_scope_offer_and_reveal_buttons_share_one_reading_during_second_browser_start(
    client, eligible, patient_ticket, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connections

    ticket, row, attachment = patient_ticket
    url = reverse("desk:request-detail", args=[ticket.pk])
    client.force_login(eligible)
    second_client = Client()
    second_client.force_login(eligible)
    ready = Event()
    committed = Event()
    original = emergency.ticket_state

    def alongside_start(user, ticket):
        state = original(user, ticket)
        ready.set()
        assert committed.wait(15)
        return state

    def start_in_other_browser():
        try:
            assert ready.wait(15)
            response = second_client.post(start_url(ticket), START_FIELDS)
            assert response.status_code == 302
        finally:
            connections.close_all()
            committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        started_elsewhere = pool.submit(start_in_other_browser)
        with monkeypatch.context() as patch:
            patch.setattr(emergency, "ticket_state", alongside_start)
            initial = client.get(url)
        started_elsewhere.result(timeout=20)
    forms = reveal_urls(row, attachment)
    assert initial.status_code == 200 and start_url(ticket) in initial.content.decode()
    assert not any(action in initial.content.decode() for action in forms)
    latest = client.get(url)
    assert latest.status_code == 200 and start_url(ticket) not in latest.content.decode()
    assert "You can reveal the patient information on this ticket until" in latest.content.decode()
    assert all(action in latest.content.decode() for action in forms)


@pytest.mark.django_db(transaction=True)
@pytest.mark.confirmation
def test_start_page_reads_its_confirmation_fields_with_the_account_it_displays(
    eligible, patient_ticket, client, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connections

    from accounts import confirmation
    from tests.test_review_reveal_and_notices_confirm2 import factor_form

    ticket, _, _ = patient_ticket
    url = start_url(ticket)
    eligible.first_name = "Morning roster"
    eligible.save(update_fields=["first_name"])
    client.force_login(eligible)
    writer = Client()
    route = "mfa_activate_totp"
    enrollment = factor_form(writer, eligible, route)
    read_factors, changes_finished = Event(), Event()
    original = confirmation.check

    def after_check(request):
        checked = original(request)
        if request.path == url and not read_factors.is_set():
            read_factors.set()
            assert changes_finished.wait(12), "The second browser did not finish enrollment"
        return checked

    monkeypatch.setattr(confirmation, "check", after_check)

    def draw():
        try:
            return client.get(url)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        drawing = pool.submit(draw)
        try:
            assert read_factors.wait(12)
            enrolled = writer.post(reverse(route), enrollment)
            assert enrolled.status_code == 302 and enrolled.url == reverse(
                "mfa_view_recovery_codes"
            )
            eligible.first_name = "Evening roster"
            eligible.save(update_fields=["first_name"])
        finally:
            changes_finished.set()
        response = drawing.result(timeout=15)

    assert response.status_code == 200 and b'name="confirm_password"' in response.content
    # The new display name was saved only after the account had a second factor.
    displayed = (
        b"Evening roster" in response.content,
        b'name="confirm_code"' in response.content,
    )
    assert displayed != (True, False), "The newer account name appeared with the old factor fields"
    current = client.get(url)
    assert b"Evening roster" in current.content and b'name="confirm_code"' in current.content


# Notices, audit records and review


@pytest.mark.parametrize("mail_on", [True, False], ids=["enabled", "disabled"])
def test_records_emergency_notice_recipients_and_contents(
    eligible,
    reviewer,
    supervisor,
    patient_ticket,
    capable,
    make_user,
    portal_user,
    settings,
    django_capture_on_commit_callbacks,
    mailoutbox,
    mail_on,
):
    from django.contrib.auth.models import Group, Permission

    from core.models import UserPreference
    from desk import notifications
    from jobs import outbox
    from jobs.models import Notice, NoticeStatus, NotSentReason

    ticket, _, _ = patient_ticket
    settings.EMAIL_BACKEND = (
        "django.core.mail.backends.locmem.EmailBackend" if mail_on else outbox.DUMMY_BACKEND
    )
    independent_group = Group.objects.create(name="Emergency reviewers")
    independent_group.permissions.add(
        Permission.objects.get(codename="review_audit_records", content_type__app_label="accounts")
    )
    grouped = make_user("records_group_reviewer", "account_manager")
    grouped.groups.add(independent_group)
    admin = make_user("records_admin", "admin")
    grant(eligible, "review_audit_records")
    for excluded in (make_user("records_no_role"), portal_user):
        grant(excluded, "review_audit_records")
    make_user("records_inactive", "supervisor", is_active=False)
    no_address = make_user("records_no_address", "supervisor")
    no_address.email = ""
    no_address.save(update_fields=["email"])
    jobs_user = system_account()
    jobs_user.groups.add(Group.objects.get(name="supervisor"))
    jobs_user.is_active = True
    jobs_user.save(update_fields=["is_active"])
    for recipient in (reviewer, supervisor, admin, grouped, capable):
        UserPreference.objects.create(user=recipient, key=notifications.OPT_OUT_KEY, value=False)

    with django_capture_on_commit_callbacks(execute=True):
        use = started(eligible, ticket)
    notices = list(Notice.objects.filter(topic="desk.emergency"))
    expected = {recipient.pk for recipient in (reviewer, supervisor, admin, grouped, capable)}
    assert len(notices) == len(expected)
    assert {n.recipient_id for n in notices} == expected
    for item in notices:
        assert (item.object_id, item.events, item.reference, item.data) == (
            use.pk,
            ["emergency_access"],
            ticket.number,
            {"use": use.pk},
        )
    if not mail_on:
        assert all(n.status == NoticeStatus.NOT_SENT for n in notices)
        assert all(n.not_sent_reason == NotSentReason.MAIL_OFF for n in notices)
        assert mailoutbox == []
        return
    assert all(n.status == NoticeStatus.SENT for n in notices)
    assert len(mailoutbox) == len(expected)
    for n in notices:
        [msg] = [m for m in mailoutbox if m.to == [n.recipient.email]]
        path = (
            reverse("access_review:emergency-use", args=[use.pk])
            if n.recipient_id in {reviewer.pk, grouped.pk}
            else reverse("desk:request-access", args=[ticket.pk])
        )
        assert msg.cc == msg.bcc == []
        assert (msg.subject, msg.body) == notifications.render_notice(
            {
                "number": ticket.number,
                "event_phrase": notifications.PHRASES["emergency_access"],
                "sign_in_url": notifications.sign_in_url(path),
                "lab_name": notifications.lab_name(),
            }
        )
        for private in (use.explanation, use.user_name, use.reason, PATIENT_KEY, PATIENT_SENTINEL):
            assert private not in msg.body and private not in msg.subject


def test_records_notice_uses_recipient_role_and_address_at_delivery(
    reviewer,
    supervisor,
    eligible,
    patient_ticket,
):
    from jobs.models import Notice

    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    reviewer_notice = Notice.objects.get(topic="desk.emergency", recipient=reviewer)
    supervisor_notice = Notice.objects.get(topic="desk.emergency", recipient=supervisor)

    reviewer.groups.add(supervisor.groups.get())
    reviewer.user_permissions.clear()
    reviewer.email = "new-review-address@example.invalid"
    reviewer.save(update_fields=["email"])
    prepared = emergency.prepare_emergency_notice(reviewer_notice)
    assert prepared.to == reviewer.email
    assert reverse("desk:request-access", args=[ticket.pk]).replace("/", "%2F") in prepared.body
    grant(supervisor, "review_audit_records")
    prepared = emergency.prepare_emergency_notice(supervisor_notice)
    assert (
        reverse("access_review:emergency-use", args=[use.pk]).replace("/", "%2F") in prepared.body
    )
    reviewer.groups.clear()
    assert emergency.prepare_emergency_notice(reviewer_notice) is None
    supervisor.is_active = False
    supervisor.save(update_fields=["is_active"])
    assert emergency.prepare_emergency_notice(supervisor_notice) is None


@pytest.mark.django_db(transaction=True)
def test_records_notice_defers_while_the_recipient_address_is_being_saved(
    supervisor,
    eligible,
    patient_ticket,
    settings,
    mailoutbox,
    monkeypatch,
):
    from jobs import outbox
    from jobs.models import Notice
    from jobs.tests.test_review_outbox_follow_ups_verify import held_open

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    ticket, _, _ = patient_ticket
    with monkeypatch.context() as change:
        change.setattr(outbox, "send_after_commit", lambda ids: None)
        started(eligible, ticket)
    notice = Notice.objects.get(topic="desk.emergency", recipient=supervisor)

    def change_address():
        supervisor.email = "updated-supervisor@example.invalid"
        supervisor.save(update_fields=["email"])

    with held_open(change_address):
        outcome = outbox.attempt(notice.pk)
    assert (outcome, [msg.to for msg in mailoutbox]) == ("busy", [])
    notice.refresh_from_db()
    assert outbox.attempt(notice.pk, now=notice.next_attempt_at) == "sent"
    assert [msg.to for msg in mailoutbox] == [[supervisor.email]]


def test_records_workspace_identifies_a_rejected_emergency_status(
    client,
    eligible,
    patient_ticket,
    reviewer,
):
    from audit_review.models import WorkspaceUse
    from audit_review.sources import describe_filters

    ticket, _, _ = patient_ticket
    started(eligible, ticket)
    client.force_login(reviewer)
    response = client.get(reverse("access_review:emergency"), {"status": "retired-status"})
    assert response.status_code == 200
    assert response.context["form"].has_error("status")
    recorded = WorkspaceUse.objects.get(request_ref=response["X-Request-ID"])
    assert "A filter was not accepted" in describe_filters(recorded, {}, {}, {})
    assert recorded.rows == 0


def test_records_use_page_names_the_current_field_after_a_field_rename(
    client,
    eligible,
    patient_ticket,
    reviewer,
    make_user,
):
    from request_forms.services import update_field

    ticket, answer, link = patient_ticket
    client.force_login(eligible)
    use = started(eligible, ticket)
    answer_url, _ = reveal_urls(answer, link)
    revealed = client.post(answer_url)
    assert revealed.status_code == 200 and PATIENT_SENTINEL in revealed.content.decode()
    new_label = "Synthetic revised field caption"
    update_field(
        actor=make_user("records_forms_admin", "admin"),
        field_id=answer.field_id,
        expected_revision=answer.field.revision,
        reason="Update the display label.",
        label=new_label,
    )
    client.force_login(reviewer)
    detail = client.get(reverse("access_review:emergency-use", args=[use.pk]))
    assert detail.status_code == 200
    assert [label for _, label in detail.context["reveals"]] == [
        f"Patient-marked answer: {new_label}"
    ]
    assert PATIENT_SENTINEL not in detail.content.decode()


def test_records_review_saved_in_another_browser_keeps_its_attribution_and_note(
    client,
    reviewer,
    make_user,
    eligible,
    patient_ticket,
):
    from django.db import DatabaseError, transaction

    from audit_review.models import WorkspaceUse

    ticket, _, _ = patient_ticket
    use = ended_use(eligible, ticket)
    other = make_user("records_other_reviewer", "supervisor")
    grant(other, "review_audit_records")
    reviewer.first_name = "Original"
    reviewer.last_name = "Reviewer"
    reviewer.save(update_fields=["first_name", "last_name"])
    client.force_login(reviewer)
    second = Client()
    second.force_login(other)
    url = reverse("access_review:emergency-use", args=[use.pk])
    for session in (client, second):
        assert session.get(url).context["form"] is not None
    payload = {"outcome": "not_appropriate", "note": "  Discussed with the synthetic shift lead.  "}
    saved = client.post(url, payload)
    assert saved.status_code == 302
    event = AuditEvent.objects.get(action="emergency_access.reviewed")
    recorded = EmergencyReview.objects.get(use=use)
    assert event.after == {
        "outcome": payload["outcome"],
        "reviewed_at": recorded.reviewed_at.isoformat(),
    }
    assert event.actor_id == reviewer.pk and recorded.reviewed_by_id == reviewer.pk
    assert recorded.request_ref == event.request_ref == saved["X-Request-ID"]
    assert event.object_type == "desk.emergencyaccess" and event.object_id == str(use.pk)
    assert not event.before and not event.reason
    assert recorded.note == payload["note"].strip()
    reviewer.first_name = "Changed"
    reviewer.save(update_fields=["first_name"])
    reviewer.groups.clear()
    assert (recorded.reviewer_name, recorded.reviewer_username, recorded.reviewer_roles) == (
        "Original Reviewer",
        reviewer.username,
        "Technical staff",
    )
    refused = second.post(url, REVIEW_FIELDS)
    assert refused.status_code == 200
    assert review.ALREADY_REVIEWED in refused.content.decode()
    assert (
        recorded.note in refused.content.decode()
        and "Original Reviewer" in refused.content.decode()
    )
    assert not refused.context["form"]
    use_log = WorkspaceUse.objects.get(request_ref=refused["X-Request-ID"])
    assert (use_log.account_id, use_log.rows, use_log.filters) == (
        other.pk,
        0,
        {"emergency_use": str(use.pk)},
    )
    listing = second.get(reverse("access_review:emergency"), {"status": "reviewed"})
    assert list(listing.context["page_obj"]) == [use]
    assert (
        recorded.note not in listing.content.decode()
        and use.explanation not in listing.content.decode()
    )
    for record_page in (listing, refused):
        log = WorkspaceUse.objects.get(request_ref=record_page["X-Request-ID"])
        assert log.source == "emergency" and log.kind == "viewed"
    changes = second.get(reverse("audit_review:changes"), {"ticket": ticket.number})
    assert f"Emergency access {use.number} on {ticket.number}" in changes.content.decode()
    for remove in (False, True):
        with (
            pytest.raises(DatabaseError, match="Historical records cannot be updated or deleted"),
            transaction.atomic(),
        ):
            rows = EmergencyReview.objects.filter(pk=use.pk)
            if remove:
                rows.delete()
            else:
                rows.update(note="Edited review.")
    assert EmergencyReview.objects.get(use=use).note == recorded.note
    assert AuditEvent.objects.filter(action="emergency_access.reviewed").count() == 1


@pytest.mark.django_db(transaction=True)
def test_records_review_becomes_available_after_the_database_end(
    client, eligible, patient_ticket, reviewer
):
    from datetime import timedelta
    from time import sleep

    from django.db import transaction

    ticket, _, _ = patient_ticket
    client.force_login(reviewer)
    # The GET and fixture share a transaction clock. The POST has a new database clock.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        ends = cursor.fetchone()[0] + timedelta(milliseconds=250)
        use = EmergencyAccess.objects.create(
            user=eligible,
            user_name=eligible.username,
            user_username=eligible.username,
            user_roles="Client services",
            ticket=ticket,
            kind="patient_information",
            reason="other",
            explanation="Synthetic handover.",
            ends_at=ends,
            started_at=ends - timedelta(hours=1),
        )
        url = reverse("access_review:emergency-use", args=[use.pk])
        drawn = client.get(url)
        assert drawn.context["status"] == "in_force" and drawn.context["form"] is None
        cursor.execute("SELECT clock_timestamp()")
        remaining = (ends - cursor.fetchone()[0]).total_seconds()
    sleep(max(0, remaining) + 0.01)
    saved = client.post(url, REVIEW_FIELDS)
    assert saved.status_code == 302
    recorded = EmergencyReview.objects.get(use=use)
    assert recorded.reviewed_by_id == reviewer.pk and recorded.reviewed_at >= use.ends_at


@pytest.mark.xfail(
    strict=True,
    reason="Documented limit, as for the audit review lists: the pager holds the newest use id "
    "when the first page was drawn, and a start whose id was given out before then but that "
    "committed after can still take a place on a later page (audit_review.emergency_views).",
)
@pytest.mark.django_db(transaction=True)
def test_records_tab_pager_preserves_membership_with_overlapping_starts(
    client,
    eligible,
    make_user,
    patient_ticket,
    reviewer,
):
    import re
    from html import unescape

    from jobs.tests.test_review_outbox_follow_ups_verify import held_open

    ticket, _, _ = patient_ticket
    other = make_user("records_second_starter", "technical")
    grant(other, "use_emergency_access")
    for index in range(50):
        ended_use(eligible, ticket, minutes_ago=60 * (index + 1))
    client.force_login(reviewer)
    address = reverse("access_review:emergency")
    with held_open(lambda: started(other, ticket)) as saving:
        latest_visible = started(eligible, ticket)
        first = client.get(address)
        first_ids = [u.pk for u in first.context["page_obj"]]
        assert (
            len(first_ids) == 50
            and first_ids[0] == latest_visible.pk
            and saving.pk not in first_ids
        )
        link = re.search(r'href="([^"]+)" rel="next"', first.content.decode())
        assert link
    second = client.get(address + unescape(link[1]))
    assert second.status_code == 200 and second.context["page_obj"].number == 2
    second_ids = [u.pk for u in second.context["page_obj"]]
    assert not (set(first_ids) & set(second_ids)), (first_ids, second_ids)
    assert second.context["total"] == first.context["total"] == 51


def test_records_start_and_each_reveal_match_their_request_and_saved_identity(
    client,
    reviewer,
    eligible,
    patient_ticket,
    portal_user,
    pickup,
):
    from django.db import transaction

    from audit_review.models import WorkspaceUse
    from clients.models import Client as Organization
    from request_forms.tests.test_pages import raised

    ticket, answer, link = patient_ticket
    eligible.first_name = "Original"
    eligible.last_name = "Operator"
    eligible.save(update_fields=["first_name", "last_name"])
    client.force_login(eligible)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT transaction_timestamp()")
        start_clock = cursor.fetchone()[0]
        saved = client.post(start_url(ticket), START_FIELDS)
    assert saved.status_code == 302
    use = EmergencyAccess.objects.get(user=eligible, ticket=ticket)
    event = AuditEvent.objects.get(action="emergency_access.started", actor=eligible)
    assert (use.request_ref, event.request_ref) == (saved["X-Request-ID"],) * 2
    assert use.started_at == start_clock
    assert use.ends_at - use.started_at == timedelta(minutes=60)
    assert event.after == {
        "ticket": ticket.pk,
        "kind": "patient_information",
        "reason": START_FIELDS["reason"],
        "started_at": use.started_at.isoformat(),
        "ends_at": use.ends_at.isoformat(),
    }
    assert event.object_type == "desk.emergencyaccess" and event.object_id == str(use.pk)
    assert not event.reason and not event.before
    assert use.explanation == START_FIELDS["explanation"]
    eligible.first_name = "Updated"
    eligible.save(update_fields=["first_name"])
    expected = (
        (
            "request_form.value_revealed",
            "patient_answer",
            answer,
            {"request_id": ticket.pk, "field": answer.key, "value_id": answer.pk},
        ),
        (
            "external_link.person_revealed",
            "person_key",
            link,
            {"request": ticket.pk, "attachment": link.pk},
        ),
    )
    for url, (action, kind, target, payload) in zip(
        reveal_urls(answer, link), expected, strict=True
    ):
        revealed = client.post(url)
        assert revealed.status_code == 200
        request_ref = revealed["X-Request-ID"]
        event = AuditEvent.objects.get(request_ref=request_ref, action=action, actor=eligible)
        assert event.after == {**payload, "emergency": use.pk}
        record = AccessRecord.objects.get(request_ref=request_ref)
        assert (record.account_id, record.ticket_id, record.capacity, record.kind) == (
            eligible.pk,
            ticket.pk,
            "laboratory",
            kind,
        )
        assert (record.object_type, record.object_id) == (target._meta.label_lower, str(target.pk))

    client.force_login(reviewer)
    detail = client.get(reverse("access_review:emergency-use", args=[use.pk]))
    assert detail.status_code == 200
    assert [what for _, what in detail.context["reveals"]] == [
        f"Patient-marked answer: {answer.label}",
        f"Person-level key: {link.link.get_record_type_display()}",
    ]
    assert use.user_name == "Original Operator" and use.user_username == eligible.username
    assert use.user_roles == "Client services"
    html = detail.content.decode()
    assert use.user_name in html and use.explanation in html
    assert PATIENT_KEY not in html and PATIENT_SENTINEL not in html
    workspace = WorkspaceUse.objects.get(request_ref=detail["X-Request-ID"])
    assert (workspace.account_id, workspace.source, workspace.rows) == (reviewer.pk, "emergency", 2)
    assert workspace.filters == {"emergency_use": str(use.pk)}
    other_ticket = raised(portal_user, pickup, {"count": 3})
    other_org = Organization.objects.create(name="Second example clinic", account_number="EXREC")
    event_label = f"Emergency access {use.number} on {ticket.number}"
    for params, matches in (
        ({"ticket": ticket.number}, True),
        ({"ticket": other_ticket.number}, False),
        ({"organization": ticket.client_id}, True),
        ({"organization": other_org.pk}, False),
    ):
        changes = client.get(reverse("audit_review:changes"), params)
        assert changes.status_code == 200 and changes.context["filter_form"].is_valid()
        assert (event_label in changes.content.decode()) == matches


def test_records_failed_and_retrying_jobs_name_only_readable_tickets(
    client,
    make_user,
    reviewer,
    eligible,
    supervisor,
    patient_ticket,
):
    from jobs.models import Notice, NoticeStatus
    from jobs.selectors import notices_needing_attention

    ticket, _, _ = patient_ticket
    use = started(eligible, ticket)
    by_recipient = {n.recipient_id: n for n in Notice.objects.filter(topic="desk.emergency")}
    for account, status in ((reviewer, NoticeStatus.WAITING), (supervisor, NoticeStatus.FAILED)):
        notice = by_recipient[account.pk]
        notice.status = status
        notice.attempts = 1
        notice.save(update_fields=["status", "attempts"])
    ticket.category.is_restricted = True
    ticket.category.save(update_fields=["is_restricted"])
    admin = make_user("records_jobs_admin", "admin")
    client.force_login(admin)
    overview = client.get(reverse("jobs:overview"))
    assert overview.status_code == 200
    items = [row for row in overview.context["notices"] if row["events"] == "Emergency access used"]
    assert len(items) == 2 and {row["failed"] for row in items} == {True, False}
    assert all((row["ticket_id"], row["reference"]) == (ticket.pk, ticket.number) for row in items)
    assert use.explanation not in overview.content.decode()
    hidden = [n for n in notices_needing_attention(reviewer) if n.topic == "desk.emergency"]
    assert len(hidden) == 2 and all(n.ticket_id is None for n in hidden)


def test_records_start_and_review_roll_back_with_their_history(
    reviewer,
    supervisor,
    eligible,
    patient_ticket,
    monkeypatch,
):
    from core import audit
    from jobs import outbox
    from jobs.models import Notice

    ticket, _, _ = patient_ticket

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic persistence failure.")

    with monkeypatch.context() as patched:
        patched.setattr(outbox, "send_after_commit", fail)
        with pytest.raises(RuntimeError, match="Synthetic persistence failure"):
            started(eligible, ticket)
    assert not EmergencyAccess.objects.exists()
    assert not Notice.objects.filter(topic="desk.emergency").exists()
    assert not AuditEvent.objects.filter(action="emergency_access.started").exists()
    use = ended_use(eligible, ticket)
    with monkeypatch.context() as patched:
        patched.setattr(audit, "record_event", fail)
        with pytest.raises(RuntimeError, match="Synthetic persistence failure"):
            review.record_review(actor=reviewer, use_id=use.pk, **REVIEW_FIELDS)
    assert not EmergencyReview.objects.exists()
    assert not AuditEvent.objects.filter(action="emergency_access.reviewed").exists()


# Migration, demonstration and settings


@pytest.mark.django_db(transaction=True)
def test_emergency_migrations_preserve_earlier_rows_and_remove_only_the_new_grants(
    patient_ticket, capable
):
    from django.contrib.auth.models import Group, Permission
    from django.core.management import call_command
    from django.db import transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    from accounts.models import CapabilityEnd
    from audit_review.models import WorkspaceUse
    from desk.models import ServiceRequest

    previous = [
        # Adapted: the capability's migration follows accounts 0012_deactivation since the merge.
        ("accounts", "0012_deactivation"),
        ("audit_review", "0005_access_reviews"),
        ("desk", "0007_ticket_terminology"),
    ]
    ticket, _, _ = patient_ticket
    earlier = WorkspaceUse.objects.create(
        account=capable, kind="viewed", source="grant_review", rows=3, filters={"page": "1"}
    )
    old_ticket = ServiceRequest.objects.values().get(pk=ticket.pk)
    old_visit = WorkspaceUse.objects.values().get(pk=earlier.pk)
    patient = capable.user_permissions.get(codename="view_patient_information")
    with transaction.atomic():
        prior_end = CapabilityEnd.objects.create(
            user=capable, permission=patient, ends_at=timezone.now() + timedelta(days=10)
        )
    group_grants = Group.objects.create(name="Synthetic migration grants")
    group_grants.permissions.add(patient)
    permission = Permission.objects.filter(
        content_type__app_label="accounts",
        content_type__model="user",
        codename="use_emergency_access",
    )
    try:
        # The management command runs Django's post_migrate hook, which creates the permission.
        MigrationExecutor(connection).migrate(previous)
        assert not permission.exists()
        call_command("migrate", verbosity=0, interactive=False)
        addition = permission.get()
        assert not addition.user_set.exists() and not addition.group_set.exists()
        assert not EmergencyAccess.objects.exists()
        assert not EmergencyReview.objects.exists()
        capable.user_permissions.add(addition)
        group_grants.permissions.add(addition)
        with transaction.atomic():
            CapabilityEnd.objects.create(
                user=capable, permission=addition, ends_at=prior_end.ends_at
            )

        MigrationExecutor(connection).migrate(previous)
        assert not permission.exists()
        assert not CapabilityEnd.objects.filter(permission_id=addition.pk).exists()
        assert list(group_grants.permissions.all()) == [patient]
        assert list(capable.user_permissions.all()) == [patient]
        assert CapabilityEnd.objects.get(pk=prior_end.pk).ends_at == prior_end.ends_at
        tables = connection.introspection.table_names()
        assert "desk_emergencyaccess" not in tables and "desk_emergencyreview" not in tables
    finally:
        call_command("migrate", verbosity=0, interactive=False)

    assert ServiceRequest.objects.values().get(pk=ticket.pk) == old_ticket
    assert WorkspaceUse.objects.values().get(pk=earlier.pk) == old_visit
    restored = permission.get()
    assert not restored.user_set.exists() and not restored.group_set.exists()


@pytest.mark.django_db(transaction=True)
def test_emergency_migrations_refuse_reverse_with_history_and_preserve_rows(
    patient_ticket, eligible, reviewer
):
    from django.core.management import call_command
    from django.db import DatabaseError, IntegrityError
    from django.db.migrations.executor import MigrationExecutor

    from audit_review.models import WorkspaceUse

    use = ended_use(eligible, patient_ticket[0])
    recorded = review.record_review(
        actor=reviewer, use_id=use.pk, outcome="appropriate", note="Synthetic migration review."
    )
    visit = WorkspaceUse.objects.create(account=reviewer, kind="viewed", source="emergency", rows=1)
    try:
        with pytest.raises(DatabaseError, match="desk 0008 cannot be reversed"):
            MigrationExecutor(connection).migrate([("desk", "0007_ticket_terminology")])
        with pytest.raises(IntegrityError, match="workspace_use_source"):
            MigrationExecutor(connection).migrate([("audit_review", "0005_access_reviews")])
        assert ("desk", "0008_emergency_access") in MigrationExecutor(
            connection
        ).loader.applied_migrations
        assert ("audit_review", "0006_emergency_access_uses") in MigrationExecutor(
            connection
        ).loader.applied_migrations
        assert EmergencyAccess.objects.get(pk=use.pk).explanation == use.explanation
        assert EmergencyReview.objects.get(pk=use.pk).note == recorded.note
        assert WorkspaceUse.objects.get(pk=visit.pk).source == "emergency"
    finally:
        call_command("migrate", verbosity=0, interactive=False)


@pytest.fixture
def emergency_demo_settings(monkeypatch, settings):
    from tests.test_demo_seed import ADMIN_ONLY, SHARED

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")


def test_emergency_demo_reset_clears_use_review_and_reseeds_its_advertised_grant(
    emergency_demo_settings,
):
    from io import StringIO

    from django.core.management import call_command

    from accounts.models import User
    from core.models import Announcement
    from desk.models import ServiceRequest

    options = {"stdout": StringIO(), "announce_accounts": True}
    call_command("seed_demo", **options)
    holder = User.objects.get(username="demo_client_services")
    use = ended_use(holder, ServiceRequest.objects.first())
    reviewed = review.record_review(
        actor=User.objects.get(username="demo_supervisor"),
        use_id=use.pk,
        outcome="appropriate",
        note="Synthetic demo reset review.",
    )
    assert reviewed.use_id == use.pk and EmergencyReview.objects.count() == 1
    call_command("reset_demo", interactive=False, **options)
    assert not EmergencyAccess.objects.exists() and not EmergencyReview.objects.exists()
    assert not AuditEvent.objects.filter(action="emergency_access.reviewed").exists()
    holder = User.objects.get(username="demo_client_services")
    assert emergency.is_eligible(holder)
    grant_event = AuditEvent.objects.get(
        action="account.capability_granted", after={"capability": "use_emergency_access"}
    )
    assert grant_event.actor.username == "demo_admin" and grant_event.object_id == str(holder.pk)
    advertised = [
        line for line in Announcement.objects.get().body.splitlines() if "emergency access" in line
    ]
    assert len(advertised) == 1 and advertised[0].startswith("demo_client_services:")


def test_emergency_demo_refuses_to_advertise_a_grant_after_its_last_day(emergency_demo_settings):
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from accounts import grant_ends
    from accounts.models import CapabilityEnd, User
    from core.models import Announcement

    call_command("seed_demo", stdout=StringIO())
    holder = User.objects.get(username="demo_client_services")
    permission = holder.user_permissions.get(codename="use_emergency_access")
    # The stored direct grant outlives its last day, as on an installation left running.
    yesterday = grant_ends.database_today() - timedelta(days=1)
    CapabilityEnd.objects.create(
        user=holder, permission=permission, ends_at=grant_ends.end_of(yesterday)
    )
    assert not emergency.is_eligible(holder)
    assert not Announcement.objects.exists()
    with pytest.raises(CommandError, match="demo_client_services"):
        call_command("seed_demo", announce_accounts=True, stdout=StringIO())
    assert not Announcement.objects.exists()


def test_emergency_settings_count_in_force_grants_once_per_eligible_staff_account(
    make_user, portal_user
):
    from django.contrib.auth.models import Group, Permission

    from accounts import grant_ends
    from accounts.models import CapabilityEnd
    from core import security_settings

    permission = Permission.objects.get(
        codename="use_emergency_access", content_type__app_label="accounts"
    )
    group_grants = Group.objects.create(name="Synthetic emergency grants")
    group_grants.permissions.add(permission)
    direct = make_user("synthetic-direct", "technical")
    both = make_user("synthetic-both", "supervisor", "technical")
    grouped = make_user("synthetic-group", "client_services")
    no_role = make_user("synthetic-no-role")
    inactive = make_user("synthetic-inactive", "technical", is_active=False)
    expired = make_user("synthetic-expired", "account_manager")
    for account in (direct, both, no_role, inactive, portal_user, expired):
        account.user_permissions.add(permission)
    for account in (both, grouped, no_role):
        account.groups.add(group_grants)
    # The same group grants access to both and grouped. Only one has an additional direct grant.
    yesterday = grant_ends.database_today() - timedelta(days=1)
    CapabilityEnd.objects.create(
        user=expired, permission=permission, ends_at=grant_ends.end_of(yesterday)
    )
    facts = {fact.key: fact for group in security_settings.in_force() for fact in group.facts}
    fact = facts["emergency-capability"]
    assert fact.value == "3 active staff accounts"
    assert fact.label == "Can use emergency access" and "60 minutes" in fact.note
