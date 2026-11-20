"""Neighbours of the round 1 review findings on "decisions read before a wait" (F1 to F13 of the
independent review): the cases next to each one the review proved, so a fix that covers only the
reported case shows here."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import IntegrityError, transaction
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts.models import User
from clients import services as directory
from clients.models import Client
from clients.tests.test_views import edit_data
from core.models import AuditEvent, UserPreference
from dataimport import importing
from desk import notifications
from desk.models import EntryKind, Status
from service_levels import jobs as clock_jobs
from service_levels.models import ServiceTarget

from .test_contract import new_request, set_status
from .test_decisions_read_before_a_wait import deactivate, observe
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session

pytestmark = pytest.mark.django_db(transaction=True)

ACCOUNT_URL = "admin:accounts_user_change"
CAPABILITY = "view_patient_information"


@pytest.fixture
def owner(make_user):
    return make_user("neighbour_admin", "admin", is_staff=True, is_superuser=True)


def _capability():
    return Permission.objects.get(codename=CAPABILITY, content_type__app_label="accounts")


# F1: the fingerprint is of the values the page draws ----------------------------------------------


@pytest.mark.parametrize("change", ["capability", "organization"])
def test_a_change_made_while_the_page_is_drawn_is_in_neither_the_page_nor_its_fingerprint(
    client, owner, make_user, clinic, water, change
):
    """The change commits after the form read the account and before the fingerprint would
    have read it again: the page shows the old values, so its save is refused."""
    if change == "organization":
        account = make_user("drawn_portal", client=clinic)
    else:
        account = make_user("drawn_capability", "technical")
    client.force_login(owner)
    drawing, gates = Gate(), {}
    url = reverse(ACCOUNT_URL, args=[account.pk])

    def commit_change():
        current = User.objects.get(pk=account.pk)
        if change == "capability":
            current.user_permissions.add(_capability())
        else:
            current.client = water
            current.changed_by = owner
            current.save(update_fields=["client"])

    with gated(account_admin, "account_reading", gates, before=True):
        page = Session(lambda: client.get(url))
        gates[page.thread] = drawing
        page.start()
        try:
            assert drawing.arrived.wait(timeout=5), page.error
            commit_change()
        finally:
            drawing.go.set()
            page.join()
    form = page.result.context["adminform"].form
    data = _admin_change_data_from(form, last_name="Example")
    response = client.post(url, data)
    assert response.status_code == 200
    assert (
        account_admin.CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
    )
    stored = User.objects.get(pk=account.pk)
    assert stored.last_name == ""
    if change == "capability":
        assert stored.user_permissions.filter(pk=_capability().pk).exists()
    else:
        assert stored.client_id == water.pk


def _admin_change_data_from(form, **changes):
    """What the browser posts for an account page already drawn (as _admin_change_data)."""
    from django import forms

    data = {}
    for name, field in form.fields.items():
        if field.disabled:
            continue
        value = form[name].value()
        if isinstance(field.widget, forms.MultiWidget):
            for index, part in enumerate(field.widget.decompress(value)):
                data[f"{name}_{index}"] = "" if part is None else str(part)
        elif isinstance(value, (list, tuple)):
            data[name] = [str(getattr(item, "pk", item)) for item in value]
        elif isinstance(value, bool):
            if value:
                data[name] = "on"
        else:
            data[name] = "" if value is None else value
    data.update(changes)
    return data


# F2: roles and grants by difference from the reading held ----------------------------------------


def test_a_capability_granted_after_the_check_is_kept_and_not_recorded_as_this_save(
    client, owner, make_user
):
    account = make_user("granted_after", "technical")
    client.force_login(owner)
    data = _admin_change_data(client, account, last_name="Example")
    checked, gates = Gate(), {}

    def grant():
        User.objects.get(pk=account.pk).user_permissions.add(_capability())

    with gated(account_admin.AccessRules, "_check_drawn", gates, before=False):
        save = Session(lambda: client.post(reverse(ACCOUNT_URL, args=[account.pk]), data))
        gates[save.thread] = checked
        save.start()
        try:
            assert checked.arrived.wait(timeout=5), save.error
            granting = Session(grant).start()
            state, _ = observe(granting, save)
        finally:
            checked.go.set()
            save.join()
            granting.join()
    assert save.outcome == "done", save.error
    assert save.result.status_code == 302
    assert granting.outcome == "done", granting.error
    stored = User.objects.get(pk=account.pk)
    assert stored.last_name == "Example"
    assert stored.user_permissions.filter(pk=_capability().pk).exists()
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert event.before["permissions"] == event.after["permissions"] == []
    assert (event.before["last_name"], event.after["last_name"]) == ("", "Example")
    assert state in ("ended", "waiting")


def test_roles_the_page_removes_and_adds_are_applied_and_recorded(client, owner, make_user):
    account = make_user("roles_changed", "technical")
    supervisor = Group.objects.get_or_create(name="supervisor")[0]
    client.force_login(owner)
    data = _admin_change_data(client, account, groups=[str(supervisor.pk)])
    assert client.post(reverse(ACCOUNT_URL, args=[account.pk]), data).status_code == 302
    assert sorted(User.objects.get(pk=account.pk).groups.values_list("name", flat=True)) == [
        "supervisor"
    ]
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert (event.before["groups"], event.after["groups"]) == (["technical"], ["supervisor"])


# F3: a mark left by a save whose update failed ------------------------------------------------


def test_a_deactivation_whose_update_failed_leaves_no_mark_for_the_next_save(
    owner, make_user, agent, category, accessioning, join
):
    """A deactivation refused by the database (a duplicate email) leaves the person active with
    their work; a later save of the same instance that changes a name releases nothing."""
    person = make_user("failed_deactivation", "technical")
    join(person, accessioning)
    ticket = new_request(agent, category, team=accessioning, assignee=person)
    other = make_user("taken_address", "technical")
    current = User.objects.get(pk=person.pk)
    current.is_active = False
    current.email = other.email
    current.changed_by = owner
    with pytest.raises(IntegrityError), transaction.atomic():
        current.save()
    current.refresh_from_db()
    assert current.is_active
    current.first_name = "Pat"
    current.save(update_fields=["first_name"])
    ticket.refresh_from_db()
    assert ticket.assignee_id == person.pk
    assert (
        not AuditEvent.objects.filter(action="request.assigned", object_id=str(ticket.pk))
        .exclude(reason="")
        .filter(reason__startswith="Account deactivated")
        .exists()
    )


# F4: the organization form keeps a former owner -----------------------------------------------


def test_a_former_owner_is_shown_inactive_and_another_active_owner_can_replace_them(
    client, agent, make_user, clinic
):
    former = make_user("former_owner", "technical")
    successor = make_user("successor_owner", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Owner",
        account_owner=former,
    )
    deactivate(former, agent)()
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    page = client.get(url)
    field = page.context["form"]["account_owner"]
    assert field.value() == former.pk
    assert ", inactive" in page.content.decode()
    data = edit_data(clinic, account_owner=successor.pk)
    assert client.post(url, data).status_code == 302
    assert Client.objects.get(pk=clinic.pk).account_owner_id == successor.pk


def test_another_inactive_account_cannot_be_chosen_as_the_owner(client, agent, make_user, clinic):
    former = make_user("former_owner_two", "technical")
    deactivate(former, agent)()
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    response = client.post(url, edit_data(clinic, account_owner=former.pk))
    assert response.status_code == 200
    assert response.context["form"].errors["account_owner"]
    assert Client.objects.get(pk=clinic.pk).account_owner_id is None


# F5: an import decides owners from the reading it held -----------------------------------------


def test_a_row_decides_its_owner_from_the_reading_held_before_the_rows(
    agent, make_user, clinic, monkeypatch
):
    manager = make_user("rename_importer", "account_manager")
    owner = make_user("renamed_owner", "technical")
    data = f"account_code,account_owner\nEXCLIN,{owner.username}\n".encode()
    Client.objects.filter(pk=clinic.pk).update(account_number="EXCLIN")
    real = importing._hold_named_accounts

    def hold_then_rename(*args):
        held = real(*args)
        # Read again now, the file's username names no account; the row decides from the
        # reading held before the rows instead of looking the username up again.
        User.objects.filter(pk=owner.pk).update(username="renamed_owner_two")
        return held

    monkeypatch.setattr(importing, "_hold_named_accounts", hold_then_rename)
    preview = importing.preview_file(
        importing.ORGANIZATIONS, actor=manager, data=data, filename="owners.csv"
    )
    assert preview.ok, [row.errors for row in preview.rows]
    assert preview.rows[0].data["account_owner"].pk == owner.pk


def test_an_unknown_owner_is_a_row_error_and_holds_nothing(agent, make_user, clinic):
    manager = make_user("unknown_importer", "account_manager")
    Client.objects.filter(pk=clinic.pk).update(account_number="EXCLIN")
    preview = importing.preview_file(
        importing.ORGANIZATIONS,
        actor=manager,
        data=b"account_code,account_owner\nEXCLIN,nobody_by_this_name\n",
        filename="owners.csv",
    )
    assert not preview.ok
    assert "no account with username 'nobody_by_this_name'" in " ".join(preview.rows[0].errors)


# F6, F7, F9: the breach job and its recipients' accounts ----------------------------------------


@pytest.fixture
def breach(make_user, agent, category, accessioning, join, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    lead = make_user("neighbour_lead", "technical")
    join(lead, accessioning, lead=True)
    ServiceTarget.objects.create(name="Neighbour target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    return lead, ticket, timezone.now() + timedelta(hours=2)


def _set_notices(client, person, enabled):
    client.force_login(person)
    return client.post(reverse("email-notices"), {"enabled": "on"} if enabled else {})


def test_a_notice_setting_saved_while_the_job_decides_waits_for_it(client, breach):
    lead, ticket, now = breach
    selected, gates = Gate(), {}
    with gated(notifications, "breach_recipients", gates, before=True):
        job = Session(lambda: clock_jobs.notice_breaches(now=now))
        gates[job.thread] = selected
        job.start()
        try:
            assert selected.arrived.wait(timeout=5), job.error
            turning_off = Session(lambda: _set_notices(HttpClient(), lead, False)).start()
            state, statement = observe(turning_off, job)
        finally:
            selected.go.set()
            job.join()
            turning_off.join()
    assert state == "waiting", statement
    assert (job.outcome, turning_off.outcome) == ("done", "done"), (job.error, turning_off.error)
    assert job.result["notices"] == 1  # decided before the change, which then waited
    assert UserPreference.objects.filter(user=lead, key=notifications.OPT_OUT_KEY).exists()


def test_a_notice_setting_being_saved_leaves_the_breach_for_the_next_run(client, breach):
    lead, ticket, now = breach
    other = Held(lambda: _set_notices(HttpClient(), lead, False))
    try:
        assert clock_jobs.notice_breaches(now=now)["busy"] == 1
    finally:
        assert other.release() == "done", other.session.error
    outcome = clock_jobs.notice_breaches(now=now)
    assert (outcome["breaches"], outcome["notices"]) == (1, 0)


def test_turning_notices_back_on_waits_for_the_job_too(client, breach):
    lead, ticket, now = breach
    UserPreference.objects.create(user=lead, key=notifications.OPT_OUT_KEY, value=False)
    other = Held(lambda: _set_notices(HttpClient(), lead, True))
    try:
        assert clock_jobs.notice_breaches(now=now)["busy"] == 1
    finally:
        assert other.release() == "done", other.session.error
    assert clock_jobs.notice_breaches(now=now)["notices"] == 1


@pytest.mark.parametrize("held", ["recipient", "jobs account"])
def test_an_account_held_for_a_key_change_leaves_the_breach_for_the_next_run(breach, held):
    from jobs.actor import system_account

    lead, ticket, now = breach
    account = lead if held == "recipient" else system_account()
    other = Held(lambda: User.objects.select_for_update().get(pk=account.pk))
    try:
        assert clock_jobs.notice_breaches(now=now)["busy"] == 1
    finally:
        assert other.release() == "done"
    assert clock_jobs.notice_breaches(now=now)["notices"] == 1


# F10: a stale organization form is told to reload ------------------------------------------------


def test_an_ineligible_new_owner_on_an_unchanged_organization_keeps_the_owner_message(
    agent, make_user, clinic
):
    owner = make_user("roleless_owner", "technical")
    with transaction.atomic():
        User.objects.get(pk=owner.pk).groups.clear()
    with pytest.raises(Exception) as refused:
        directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision,
            reason="Owner",
            account_owner=owner,
        )
    assert directory.OWNER_REFUSED in str(refused.value)


def test_a_form_drawn_before_another_change_is_told_to_reload_before_anything_is_held(
    agent, make_user, clinic
):
    owner = make_user("eligible_owner", "technical")
    clinic = directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Phone",
        phone="555-0180",
    )
    with pytest.raises(Exception) as refused:
        directory.update_client(
            actor=agent,
            client_id=clinic.pk,
            expected_revision=clinic.revision - 1,
            reason="Owner",
            account_owner=owner,
        )
    assert directory.CHANGED in str(refused.value)


# F11, F13: the refused reply's page ---------------------------------------------------------------


def test_a_refused_staff_entry_draws_the_ticket_page_with_its_help(client, agent, category):
    ticket = new_request(agent, category)
    client.force_login(agent)
    response = client.post(
        reverse("desk:request-entry", args=[ticket.pk]),
        {"kind": EntryKind.NOTE, "body": "", "expected_revision": ticket.revision},
    )
    assert response.status_code == 400  # the ticket page again, with the composer's error
    assert response.context["page_documentation"]["id"] == "desk-request-detail"


def test_a_refused_reply_on_a_closed_ticket_says_why_with_text_or_without(
    client, agent, sample_category, clinic, clinic_portal
):
    from clients.models import PortalAccess

    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = set_status(new_request(agent, sample_category, client=clinic), Status.CLOSED)
    client.force_login(clinic_portal)
    url = reverse("portal:request-reply", args=[ticket.number])
    for body, shown in (("Order ORD-TEST-000860 too.", True), ("", False)):
        response = client.post(url, {"body": body, "expected_revision": ticket.revision})
        assert response.status_code == 200
        assert b"so nothing was sent" in response.content
        assert (b'id="id_body"' in response.content) is shown
        assert response.context["page_documentation"]["id"] == "portal-request-detail"
