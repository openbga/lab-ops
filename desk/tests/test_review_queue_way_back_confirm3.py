"""Third confirmation of assignment choices and empty-queue feedback, using synthetic records.

Adopted from the reviewer's file (archived in the review history as
test_queue_way_back_round1_confirm3_original.py). Four cases are adapted to the behaviour the
resolution documents (findings 3, 6, 8 and 9), each marked at its assertion.
"""

import pytest
from django.urls import reverse

from desk import forms, services, views
from desk.models import Status
from teams import services as team_services
from teams.models import Team

from .test_contract import new_request
from .test_preferences_in_session import committed

pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("draw", ["get", "refusal"])
def test_a_team_and_its_existing_member_appear_together_when_reactivated(
    client, make_user, quality, agent, category, monkeypatch, draw
):
    """The permanent member is eligible in every committed state that offers this team."""
    manager = make_user("team_editor", "admin")
    team_services.add_member(actor=manager, team=quality, user=agent)
    inactive = team_services.update_team(
        actor=manager,
        team_id=quality.pk,
        expected_revision=quality.revision,
        reason="Paused",
        is_active=False,
    )
    ticket = new_request(manager, category)
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    option = f"{quality.pk}:{agent.pk}"
    team_option = f"{quality.pk}:"
    initial = client.get(target).context["bulk_form"].fields["assign_to"]
    assert not initial.valid_value(team_option) and not initial.valid_value(option)
    choices, team_filter = forms.assign_choices, Team.objects.filter
    reading = False
    reads = 0
    resumed = False

    def on_read(*args, **kwargs):
        nonlocal reads, reading
        reads += 1
        reading = reads == (1 if draw == "get" else 2)
        try:
            return choices(*args, **kwargs)
        finally:
            reading = False

    def after_member_query(*args, **kwargs):
        nonlocal resumed
        if reading and not resumed and kwargs == {"is_active": True}:
            resumed = True
            committed(
                lambda: team_services.update_team(
                    actor=manager,
                    team_id=inactive.pk,
                    expected_revision=inactive.revision,
                    reason="Reopened",
                    is_active=True,
                )
            )
        return team_filter(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(forms, "assign_choices", on_read)
        patch.setattr(Team.objects, "filter", after_member_query)
        result = (
            client.get(target)
            if draw == "get"
            else client.post(
                target, {"action": "status", "requests": f"{ticket.pk}:{ticket.revision}"}
            )
        )
    assert result.status_code == 200 and resumed
    form = result.context["bulk_form"]
    assert set(form.errors) == (set() if draw == "get" else {"status"})
    shown = form.fields["assign_to"]
    fresh = client.get(target).context["bulk_form"].fields["assign_to"]
    assert fresh.valid_value(team_option) and fresh.valid_value(option)
    assert (shown.valid_value(team_option), shown.valid_value(option)) in (
        (False, False),
        (True, True),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "reason", ["no-assignment", "missing-revision", "close-open", "missing-ticket"]
)
def test_no_assignment_posted_keeps_the_original_refusal_after_a_team_changes(
    client, make_user, agent, category, quality, monkeypatch, reason
):
    manager = make_user("roster_admin", "admin")
    ticket = new_request(manager, category)
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    action = "assign" if reason == "no-assignment" else "close"
    selected = {
        "missing-revision": str(ticket.pk),
        "missing-ticket": f"{ticket.pk + 1000000}:1",
    }.get(reason, f"{ticket.pk}:{ticket.revision}")
    original = views._apply_bulk
    errors = {}

    def change_unrelated_team(request, form):
        original(request, form)
        assert form.errors
        errors.update({k: list(v) for k, v in form.errors.items()})
        committed(lambda: team_services.add_member(actor=manager, team=quality, user=agent))

    monkeypatch.setattr(views, "_apply_bulk", change_unrelated_team)
    result = client.post(target, {"action": action, "requests": selected})
    assert result.status_code == 200
    form = result.context["bulk_form"]
    assert {k: list(v) for k, v in form.errors.items()} == errors
    assert ("assign_to" in errors) == (reason == "no-assignment")
    assert not form["assign_to"].value()
    assert form.fields["assign_to"].valid_value(f"{quality.pk}:{agent.pk}")
    ticket.refresh_from_db()
    assert ticket.status == Status.OPEN and ticket.assignee_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("outcome", ["success", "target-left", "no-selection", "field-error"])
def test_legacy_bulk_redirect_preserves_its_reason_and_draws_fresh_choices(
    client, agent, category, make_user, quality, monkeypatch, outcome
):
    manager = make_user("legacy_supervisor", "supervisor")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    option = f"{quality.pk}:{agent.pk}"
    assert client.get(target).context["bulk_form"].fields["assign_to"].valid_value(option)
    data = {
        "action": "assign",
        "assign_to": option,
        "next": target,
        "requests": f"{ticket.pk}:{ticket.revision}",
    }
    if outcome == "target-left":
        original = services.bulk_assign

        def leave_before_save(**kwargs):
            committed(
                lambda: team_services.remove_member(
                    actor=manager, membership=membership, reason="Rotation ended"
                )
            )
            return original(**kwargs)

        monkeypatch.setattr(services, "bulk_assign", leave_before_save)
    elif outcome == "no-selection":
        data.pop("requests")
    elif outcome == "field-error":
        data.pop("assign_to")
    assert client.get(reverse("desk:request-bulk")).status_code == 405
    result = client.post(reverse("desk:request-bulk"), data)
    assert result.status_code == 302 and result.url == target
    landing = client.get(result.url)
    form = landing.context["bulk_form"]
    assert not form.is_bound and not form.errors and not _not_changed_body(landing)
    assert form.fields["assign_to"].valid_value(option) == (outcome != "target-left")
    messages = [str(message) for message in landing.context["messages"]]
    expected = {
        "target-left": "active member",
        "no-selection": "Select at least one ticket",
        "field-error": "Choose a team or a person",
        "success": "1 ticket changed",
    }
    assert any(expected[outcome] in message for message in messages), messages
    ticket.refresh_from_db()
    assert ticket.assignee_id == (agent.pk if outcome == "success" else None)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("width", [390, 1366], ids=["phone", "desktop"])
@pytest.mark.parametrize("refusal", ["field", "stale"])
def test_visible_empty_notice_after_a_real_bulk_submission(
    live_server, client, agent, category, settings, width, refusal
):
    api = pytest.importorskip("playwright.sync_api")
    ticket = _waiting_ticket(agent, category, "Synthetic browser subject QE129")
    client.force_login(agent)
    cookie = settings.SESSION_COOKIE_NAME
    url = reverse("desk:request-list") + "?view=waiting-client"
    with api.sync_playwright() as driver:
        browser = driver.chromium.launch()
        context = browser.new_context(
            base_url=live_server.url,
            viewport={"width": width, "height": 844},
            color_scheme="dark" if refusal == "stale" else "light",
            reduced_motion="reduce",
        )
        context.add_cookies(
            [{"name": cookie, "value": client.cookies[cookie].value, "url": live_server.url}]
        )
        page = context.new_page()
        page.set_default_timeout(4000)
        assert page.goto(url).status == 200
        page.get_by_role("checkbox", name=f"Select {ticket.number}", exact=True).check()
        if width == 390:
            page.get_by_role("button", name="Change selected tickets").click()
        apply = page.get_by_role("button", name="Apply to selected")
        notice = page.get_by_role("alert").filter(has_text="Not changed")
        apply.click()  # Field refusal while the selected row is still on the page.
        api.expect(page.locator("#bulk_assign_to_error")).to_be_visible()
        api.expect(page.locator('input[name="requests"]')).to_be_checked()
        api.expect(notice).to_have_count(0)
        if refusal == "stale":
            page.locator("#bulk_action").select_option("close")
        committed(
            lambda: services.change_status(
                actor=agent, request=ticket, expected_revision=ticket.revision, status=Status.OPEN
            )
        )
        apply.click()
        api.expect(page.locator("#bulk-form")).to_have_count(0)
        if refusal == "stale":
            api.expect(
                page.get_by_role("alert").filter(has_text="Changed since you opened")
            ).to_be_visible()
        api.expect(notice).to_be_visible(timeout=1200)
        if refusal == "field":
            api.expect(notice).to_contain_text("Assign to: Choose a team or a person.")
        text = notice.inner_text()
        assert ticket.subject not in text and ticket.number not in text
        box = notice.bounding_box()
        assert box and 0 <= box["x"] and box["x"] + box["width"] <= width
        browser.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("selection", "reason"),
    [("person", "stale_revision"), ("team", "empty_selection"), ("person", "missing_status")],
)
def test_unrelated_member_joining_keeps_an_offered_assignment(
    client, agent, category, quality, accessioning, make_user, monkeypatch, selection, reason
):

    manager = make_user("desk_scheduler", "admin")
    colleague = make_user("new_accessioner", "technical")
    team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    choice = f"{quality.pk}:{agent.pk}" if selection == "person" else f"{quality.pk}:"
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    assert client.get(target).context["bulk_form"].fields["assign_to"].valid_value(choice)
    data = {"action": "assign", "assign_to": choice}
    if reason == "stale_revision":
        data["requests"] = f"{ticket.pk}:{ticket.revision - 1}"
    elif reason == "missing_status":
        data.update(action="status", requests=f"{ticket.pk}:{ticket.revision}")
    original = views._apply_bulk
    refusal = {}

    def after_decision(request, form):
        original(request, form)
        assert form.errors and "assign_to" not in form.errors
        refusal.update({key: list(errors) for key, errors in form.errors.items()})
        committed(
            lambda: team_services.add_member(actor=manager, team=accessioning, user=colleague)
        )

    monkeypatch.setattr(views, "_apply_bulk", after_decision)
    response = client.post(target, data)
    assert response.status_code == 200
    form = response.context["bulk_form"]
    assert all(list(form.errors[key]) == errors for key, errors in refusal.items())
    assert form.fields["assign_to"].valid_value(f"{accessioning.pk}:{colleague.pk}")
    assert form.fields["assign_to"].valid_value(choice)
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    assert f'<option value="{choice}" selected' in form["assign_to"].as_widget()
    assert "assign_to" not in form.errors


@pytest.mark.django_db(transaction=True)
def test_unrelated_roster_change_keeps_the_reason_an_assignee_was_refused(
    client, agent, category, quality, accessioning, make_user, monkeypatch
):

    manager = make_user("assignment_operator", "admin")
    other = make_user("other_rota_member", "technical")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    url = reverse("desk:request-list") + "?view=all"
    choice = f"{quality.pk}:{agent.pk}"
    assert client.get(url).context["bulk_form"].fields["assign_to"].valid_value(choice)
    team_services.remove_member(actor=manager, membership=membership, reason="Rotation ended")
    original = views._apply_bulk
    reasons = []

    def after_decision(request, form):
        original(request, form)
        assert set(form.errors) == {"assign_to"}
        reasons.extend(form.errors["assign_to"])
        committed(lambda: team_services.add_member(actor=manager, team=accessioning, user=other))

    monkeypatch.setattr(views, "_apply_bulk", after_decision)
    response = client.post(
        url,
        {
            "requests": f"{ticket.pk}:{ticket.revision}",
            "action": "assign",
            "assign_to": choice,
        },
    )
    assert response.status_code == 200
    form = response.context["bulk_form"]
    assert not form.fields["assign_to"].valid_value(choice)
    assert form.fields["assign_to"].valid_value(f"{accessioning.pk}:{other.pk}")
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    # Adapted (finding 6): the refusal no longer repeats the posted value in its wording.
    original_reason = next(error for error in reasons if "options offered" in error)
    assert (
        original_reason in form.errors["assign_to"] and original_reason.encode() in response.content
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "ending", ["removed", "account-inactive", "team-inactive", "last-role-removed"]
)
def test_ending_the_chosen_members_eligibility_preserves_the_assignment_refusal(
    client, agent, category, quality, make_user, monkeypatch, ending
):

    manager = make_user("roster_scheduler", "admin")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    choice = f"{quality.pk}:{agent.pk}"
    initial = client.get(target).context["bulk_form"]
    assert not initial.is_bound and initial.fields["assign_to"].valid_value(choice)

    def end_eligibility():
        if ending == "removed":
            team_services.remove_member(
                actor=manager, membership=membership, reason="End of rotation"
            )
        elif ending == "account-inactive":
            agent.is_active = False
            agent.changed_by = manager
            agent.save()
        elif ending == "team-inactive":
            team_services.update_team(
                actor=manager,
                team_id=quality.pk,
                expected_revision=quality.revision,
                reason="End of service",
                is_active=False,
            )
        else:
            agent.groups.clear()

    original = services.bulk_assign

    def assign_after_commit(**kwargs):
        committed(end_eligibility)
        return original(**kwargs)

    monkeypatch.setattr(services, "bulk_assign", assign_after_commit)
    response = client.post(
        target,
        {
            "requests": f"{ticket.pk}:{ticket.revision}",
            "action": "assign",
            "assign_to": choice,
        },
    )
    assert response.status_code == 200
    shown = response.context["bulk_form"]
    assert shown.non_field_errors()
    assert any(
        "inactive" in error or "active member" in error for error in shown.non_field_errors()
    )
    fresh = client.get(target).context["bulk_form"]
    assert not fresh.is_bound and not fresh.errors
    assert list(shown.fields["assign_to"].choices) == list(fresh.fields["assign_to"].choices)
    assert not shown.fields["assign_to"].valid_value(choice)
    assert f'value="{choice}"' not in shown["assign_to"].as_widget()
    ticket.refresh_from_db()
    assert ticket.assignee_id is None


def _not_changed_body(page):
    """Text inside the named notice only, excluding the separate Django message and list."""
    import re
    from html import unescape

    from django.utils.html import strip_tags

    found = re.search(r"Not changed</span>\s*<div>(.*?)</div>", page.content.decode(), re.S)
    return unescape(strip_tags(found[1])).strip() if found else ""


def _waiting_ticket(user, category, subject):

    ticket = new_request(user, category, subject=subject)
    services.change_status(
        actor=user, request=ticket, expected_revision=ticket.revision, status=Status.WAITING_CLIENT
    )
    ticket.refresh_from_db()
    return ticket


def test_help_notice_when_only_an_unselected_ticket_remains(client, agent, category):
    """Queue help promises the notice when none of the selected tickets is on the redraw."""

    from .test_review_queue_way_back import bulk_url, row_ids

    picked = _waiting_ticket(agent, category, "Synthetic selected")
    remaining = _waiting_ticket(agent, category, "Synthetic unselected")
    client.force_login(agent)
    url = reverse("desk:request-list") + "?view=waiting-client"
    opened = client.get(url)
    assert set(row_ids(opened)) == {picked.pk, remaining.pk}
    expected = picked.revision
    services.change_status(
        actor=agent, request=picked, expected_revision=expected, status=Status.OPEN
    )
    refused = client.post(
        bulk_url(opened), {"action": "status", "requests": f"{picked.pk}:{expected}"}
    )
    assert refused.status_code == 200 and row_ids(refused) == [remaining.pk]
    assert b"Choose a status." in refused.content
    # Adapted (finding 8): the help now says the notice appears when the list drawn again has no
    # tickets; with one left, the reason is in the form, next to the field.
    assert _not_changed_body(refused) == ""


def test_empty_refusal_does_not_claim_tickets_were_selected_when_none_were(client, agent, category):

    from .test_review_queue_way_back import bulk_url, row_ids

    ticket = _waiting_ticket(agent, category, "Synthetic last row")
    client.force_login(agent)
    url = reverse("desk:request-list") + "?view=waiting-client"
    opened = client.get(url)
    assert row_ids(opened) == [ticket.pk]
    services.change_status(
        actor=agent, request=ticket, expected_revision=ticket.revision, status=Status.OPEN
    )
    refused = client.post(bulk_url(opened), {"action": "close"})
    assert refused.status_code == 200 and not row_ids(refused)
    assert "Select at least one ticket." in refused.context["bulk_form"].non_field_errors()
    assert b"Select at least one ticket." in refused.content
    assert "the selected tickets are no longer on this page" not in _not_changed_body(refused)


def test_nonfield_only_refusal_explains_the_stale_revision_inside_the_notice(
    client, agent, category
):

    from .test_review_queue_way_back import bulk_url, row_ids

    ticket = _waiting_ticket(agent, category, "Synthetic timing")
    client.force_login(agent)
    url = reverse("desk:request-list") + "?view=waiting-client"
    opened = client.get(url)
    expected = ticket.revision
    services.change_status(
        actor=agent, request=ticket, expected_revision=expected, status=Status.OPEN
    )
    refused = client.post(
        bulk_url(opened), {"action": "close", "requests": f"{ticket.pk}:{expected}"}
    )
    assert refused.status_code == 200 and not row_ids(refused)
    assert list(refused.context["bulk_form"].errors) == ["__all__"]
    assert b"Changed since you opened the list" in refused.content
    body = _not_changed_body(refused)
    assert ticket.number not in body and ticket.subject not in body
    # Adapted (finding 9): the help now says a reason about the selected tickets themselves is in
    # the message above the notice, which the page shows (asserted above).
    assert "Nothing was changed" in body


@pytest.mark.parametrize("field", ["action", "status", "assign_to"])
def test_empty_notice_keeps_invalid_field_feedback_free_of_posted_ticket_text(
    client, agent, category, field
):

    from .test_review_queue_way_back import row_ids

    ticket = new_request(agent, category, subject="Synthetic collection hours XQ146")
    client.force_login(agent)
    data = {"action": "status", "status": "open", "requests": f"{ticket.pk}:{ticket.revision}"}
    data[field] = ticket.subject
    refused = client.post(reverse("desk:request-list") + "?view=waiting-client", data)
    assert refused.status_code == 200 and not row_ids(refused)
    assert field in refused.context["bulk_form"].errors
    assert ticket.subject not in _not_changed_body(refused)


def test_invalid_filters_and_ordinary_empty_gets_do_not_create_bulk_feedback(
    client, agent, category
):

    from .test_review_queue_way_back import row_ids

    ticket = new_request(agent, category, subject="Synthetic filter update")
    client.force_login(agent)
    url = reverse("desk:request-list")
    for query in ("?view=waiting-client", "?view=all&created_from=unknown"):
        opened = client.get(url + query)
        assert opened.status_code == 200 and not row_ids(opened)
        assert not _not_changed_body(opened)
    refused = client.post(
        url + "?view=all&created_from=unknown",
        {"action": "status", "requests": f"{ticket.pk}:{ticket.revision}"},
    )
    assert refused.status_code == 200 and not row_ids(refused)
    assert refused.context["filter_form"].errors and refused.context["bulk_form"].errors
    assert b"These filters cannot be applied" in refused.content
    assert "Choose a status." in _not_changed_body(refused)
    assert ticket.subject not in _not_changed_body(refused)


@pytest.mark.django_db(transaction=True)
def test_empty_notice_and_bulk_form_do_not_both_appear_after_last_row_leaves(
    client, agent, category, monkeypatch
):
    """The last result leaves after pagination counts it and before the template reads the row."""

    from core.grids import GridRequest

    from .test_review_queue_way_back import bulk_url, row_ids

    ticket = _waiting_ticket(agent, category, "Synthetic count boundary")
    client.force_login(agent)
    url = reverse("desk:request-list") + "?view=waiting-client"
    opened = client.get(url)
    assert row_ids(opened) == [ticket.pk]
    expected = ticket.revision
    paginate = GridRequest.page

    def read_count_then_move(*args, **kwargs):
        page = paginate(*args, **kwargs)
        assert page.paginator.count == 1
        committed(
            lambda: services.change_status(
                actor=agent, request=ticket, expected_revision=expected, status=Status.OPEN
            )
        )
        return page

    with monkeypatch.context() as patch:
        patch.setattr(GridRequest, "page", read_count_then_move)
        refused = client.post(
            bulk_url(opened), {"action": "status", "requests": f"{ticket.pk}:{expected}"}
        )
    assert refused.status_code == 200 and not row_ids(refused)
    assert refused.context["bulk_form"].errors["status"] == ["Choose a status."]
    assert not (
        "Choose a status." in _not_changed_body(refused) and b'id="bulk-form"' in refused.content
    ), "The empty-list notice repeats the field error above a bulk form with zero ticket rows."


@pytest.mark.django_db(transaction=True)
def test_a_renamed_team_and_member_with_a_remaining_role_keep_the_choice(
    client, agent, make_user, category, quality, monkeypatch
):
    from django.contrib.auth.models import Group

    manager = make_user("rename_scheduler", "admin")
    secondary_role = Group.objects.get_or_create(name="technical")[0]
    agent.groups.add(secondary_role)
    team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    url = reverse("desk:request-list") + "?view=all"
    choice = f"{quality.pk}:{agent.pk}"
    original = views._apply_bulk

    def update_labels():
        agent.groups.remove(secondary_role)
        agent.first_name = "Avery"
        agent.save(update_fields=["first_name"])
        team_services.update_team(
            actor=manager,
            team_id=quality.pk,
            expected_revision=quality.revision,
            name="Quality services",
            reason="Department renamed",
        )

    def after_decision(request, form):
        original(request, form)
        assert form.non_field_errors() and not form["assign_to"].errors
        committed(update_labels)

    monkeypatch.setattr(views, "_apply_bulk", after_decision)
    response = client.post(
        url,
        {
            "requests": f"{ticket.pk}:{ticket.revision - 1}",
            "action": "assign",
            "assign_to": choice,
        },
    )
    assert response.status_code == 200
    form = response.context["bulk_form"]
    assert form.non_field_errors() and not form["assign_to"].errors
    assert form["assign_to"].value() == choice
    assert "Avery (Quality services)" in form["assign_to"].as_widget()


@pytest.mark.django_db(transaction=True)
def test_returning_member_after_a_service_refusal_is_not_asked_to_choose_an_active_member(
    client, agent, quality, category, make_user, monkeypatch
):
    """Both choice reads offer the same person; they were absent only when the service checked."""
    manager = make_user("membership_supervisor", "supervisor")
    membership = team_services.add_member(actor=manager, team=quality, user=agent)
    ticket = new_request(manager, category, team=quality)
    client.force_login(manager)
    target = reverse("desk:request-list") + "?view=all"
    choice = f"{quality.pk}:{agent.pk}"
    data = {"action": "assign", "assign_to": choice, "requests": f"{ticket.pk}:{ticket.revision}"}
    assign = services.bulk_assign

    def change_during_save(**kwargs):
        committed(
            lambda: team_services.remove_member(
                actor=manager, membership=membership, reason="Shift ended"
            )
        )
        try:
            return assign(**kwargs)
        finally:
            committed(lambda: team_services.add_member(actor=manager, team=quality, user=agent))

    with monkeypatch.context() as patch:
        patch.setattr(services, "bulk_assign", change_during_save)
        refused = client.post(target, data)
    assert refused.status_code == 200
    assert refused.context["bulk_form"].non_field_errors()
    ticket.refresh_from_db()
    assert ticket.assignee_id is None
    retry = client.post(target, data)
    ticket.refresh_from_db()
    assert retry.status_code == 302 and ticket.assignee_id == agent.pk
    selected = f'<option value="{choice}" selected'.encode() in refused.content
    says_choose_active = b"Choose an active member" in refused.content
    # Adapted (finding 3, documented bound in the Assign to help): the refusal reflects the moment
    # of the change, and the form shows the teams as they are now, so the rejoined person is
    # offered and kept while the message gives the reason the change was refused.
    assert selected and says_choose_active


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("endpoint", ["desk:request-list", "desk:request-bulk"])
def test_a_staff_role_lost_before_the_action_does_not_redraw_the_form(
    client, agent, category, monkeypatch, endpoint
):
    ticket = new_request(agent, category)
    client.force_login(agent)
    apply = services.bulk_close

    def role_ended(**kwargs):
        committed(lambda: agent.groups.clear())
        return apply(**kwargs)

    monkeypatch.setattr(services, "bulk_close", role_ended)
    result = client.post(
        reverse(endpoint), {"action": "close", "requests": f"{ticket.pk}:{ticket.revision}"}
    )
    assert result.status_code == 403
    assert b'id="bulk-form"' not in result.content and not _not_changed_body(result)
    ticket.refresh_from_db()
    assert ticket.status == Status.OPEN
