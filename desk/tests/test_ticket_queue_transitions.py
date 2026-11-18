"""Queue changes during deactivation, removal and revision of a complaint decision."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from desk import lists, services
from desk.models import ComplaintDecision, EntryKind, ServiceRequest
from teams.models import ensure_default_team

from .test_contract import new_request, seen, selection

pytestmark = pytest.mark.django_db


def test_deactivating_an_account_returns_its_requests_to_the_queue(
    client, agent, make_user, category, join
):
    owner = make_user("owner", "admin", is_staff=True, is_superuser=True)
    team = ensure_default_team()
    join(agent, team)
    open_request = new_request(agent, category, assignee=agent)
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[agent.pk])
    form = client.get(url).context["adminform"].form
    data = {
        name: value
        for name, value in form.initial.items()
        if value is not None and name not in ("password", "groups", "user_permissions")
    }
    data.update(
        groups=[g.pk for g in agent.groups.all()],
        is_active="",  # cleared: deactivate
        date_joined_0=agent.date_joined.strftime("%Y-%m-%d"),
        date_joined_1=agent.date_joined.strftime("%H:%M:%S"),
    )
    data = {k: v for k, v in data.items() if k not in ("date_joined", "last_login")}
    response = client.post(url, data, follow=True)
    agent.refresh_from_db()
    if agent.is_active:  # the admin form's own fields changed shape: say so plainly
        pytest.fail(f"Deactivation through the admin did not save: {response.status_code}")
    open_request.refresh_from_db()
    assert open_request.assignee is None
    assert "returned to their team queues" in response.content.decode()


def test_removal_dialog_counts_the_requests_that_return(client, agent, make_user, category, join):
    lead = make_user("membership_lead", "supervisor")
    team = ensure_default_team()
    join(agent, team)
    new_request(agent, category, assignee=agent)
    new_request(agent, category, assignee=agent)
    client.force_login(lead)
    page = client.get(reverse("teams:team-detail", args=[team.pk])).content.decode()
    assert "2 ticket(s) assigned to them in this team that are not closed return" in page


def test_an_earlier_decision_author_stays_out_of_the_review_queue(make_user, category):
    first = make_user("first_author", "supervisor")
    second = make_user("second_author", "supervisor")
    third = make_user("independent", "supervisor")
    record = new_request(first, category)
    services.set_complaint(
        actor=first, request=record, expected_revision=seen(record), is_complaint=True, reason="x"
    )
    for author in (first, second):
        services.decide_complaint(
            actor=author,
            request=record,
            expected_revision=seen(record),
            decision=ComplaintDecision.INVESTIGATE,
            rationale="Same decision, revised",
        )
    params = {"view": "complaints-review", "complaint": "review"}
    assert record not in lists.view_queryset(first, params)
    assert record not in lists.view_queryset(second, params)
    assert record in lists.view_queryset(third, params)


def test_stale_bulk_selection_names_the_changed_request(agent, make_user, category):
    colleague = make_user("colleague_b", "technical")
    record = new_request(agent, category)
    picked = selection([record])  # what the list showed
    services.add_entry(
        actor=colleague,
        request=record,
        kind=EntryKind.NOTE,
        body="Later",
        expected_revision=seen(record),
    )
    with pytest.raises(ValidationError, match=record.number):
        services.bulk_status(actor=agent, selected=picked, status="waiting_client")
    with pytest.raises(ValidationError, match="out of date"):
        services.bulk_status(actor=agent, selected={record.pk: None}, status="waiting_client")


def test_refused_action_form_after_scope_loss_answers_404(
    client, agent, make_user, category, privacy_category, monkeypatch
):
    from desk import views

    supervisor = make_user("rerouting_lead", "supervisor")
    record = new_request(agent, category)
    validate = views.PropertiesForm.is_valid

    def lose_scope(form):
        valid = validate(form)
        services.update_properties(
            actor=supervisor,
            request=ServiceRequest.objects.get(pk=record.pk),
            expected_revision=seen(record),
            priority="normal",
            category=privacy_category,
        )
        return valid

    monkeypatch.setattr(views.PropertiesForm, "is_valid", lose_scope)
    client.force_login(agent)
    response = client.post(
        reverse("desk:request-properties", args=[record.pk]),
        {"category": "", "priority": "normal", "expected_revision": seen(record)},
    )
    assert response.status_code == 404


def test_recategorizing_uses_the_current_complaint_flag(
    agent, make_user, category, sample_category
):
    from taxonomy.models import RequestCategory

    record = new_request(agent, category)
    stale = RequestCategory.objects.get(pk=sample_category.pk)  # loaded before the change
    RequestCategory.objects.filter(pk=sample_category.pk).update(is_complaint=True)
    services.update_properties(
        actor=agent,
        request=record,
        expected_revision=seen(record),
        priority=record.priority,
        category=stale,
    )
    record.refresh_from_db()
    assert record.is_complaint
