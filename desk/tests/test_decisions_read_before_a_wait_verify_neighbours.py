"""Neighbours of the verification findings on "decisions read before a wait" (V1 to V10 of the
verification of review round 1): the cases next to each one the verification proved."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse
from django.utils import timezone

from accounts import admin as account_admin
from accounts.models import User
from clients import services as directory
from clients.models import Client, PortalAccess
from clients.tests.test_views import edit_data
from core.models import AuditEvent
from desk import services
from desk.models import EntryKind, RequestEntry, Status
from jobs.models import JobSettings
from jobs.services import update_settings
from service_levels import jobs as clock_jobs
from service_levels.models import RequestClock, ServiceTarget

from .test_contract import new_request
from .test_decisions_read_before_a_wait import deactivate
from .test_review_acting_account_until_commit import Gate, _admin_change_data, gated
from .test_review_assignment_lock_order_verify3 import Held, Session

pytestmark = pytest.mark.django_db(transaction=True)

ACCOUNT_URL = "admin:accounts_user_change"


@pytest.fixture
def owner(make_user):
    return make_user("verify_neighbour_admin", "admin", is_staff=True, is_superuser=True)


def _capability():
    return Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )


def _save_with_change_after_check(client, account, data, change):
    """Post the account page; `change` commits in another session once the page's check has held
    its reading, before the page saves its roles."""
    checked, gates = Gate(), {}
    with gated(account_admin.AccessRules, "_check_drawn", gates, before=False):
        save = Session(lambda: client.post(reverse(ACCOUNT_URL, args=[account.pk]), data))
        gates[save.thread] = checked
        save.start()
        try:
            assert checked.arrived.wait(timeout=5), save.error
            other = Session(change).start()
            other.waiting_or_ended(seconds=5)
        finally:
            checked.go.set()
            save.join()
            other.join()
    assert (save.outcome, other.outcome) == ("done", "done"), (save.error, other.error)
    return save.result


# V1: the audit records what this save itself added -----------------------------------------------


def test_a_capability_both_the_page_and_another_change_add_is_not_credited_to_the_page(
    client, owner, make_user
):
    account = make_user("both_add_capability", "technical")
    client.force_login(owner)
    data = _admin_change_data(
        client, account, user_permissions=[str(_capability().pk)], first_name="Sam"
    )
    response = _save_with_change_after_check(
        client,
        account,
        data,
        lambda: User.objects.get(pk=account.pk).user_permissions.add(_capability()),
    )
    assert response.status_code == 302
    assert User.objects.get(pk=account.pk).user_permissions.filter(pk=_capability().pk).exists()
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert event.before["permissions"] == event.after["permissions"] == []
    assert (event.before["first_name"], event.after["first_name"]) == ("", "Sam")


def test_the_page_adds_one_role_while_another_change_adds_a_different_one(client, owner, make_user):
    account = make_user("both_add_roles", "technical")
    supervisor = Group.objects.get_or_create(name="supervisor")[0]
    planning = Group.objects.create(name="Example planning")
    client.force_login(owner)
    current = [str(pk) for pk in account.groups.values_list("pk", flat=True)]
    data = _admin_change_data(client, account, groups=[*current, str(supervisor.pk)])
    response = _save_with_change_after_check(
        client, account, data, lambda: User.objects.get(pk=account.pk).groups.add(planning)
    )
    assert response.status_code == 302
    assert sorted(User.objects.get(pk=account.pk).groups.values_list("name", flat=True)) == [
        "Example planning",
        "supervisor",
        "technical",
    ]
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert (event.before["groups"], event.after["groups"]) == (
        ["technical"],
        ["supervisor", "technical"],
    )


# V2, V3: role names -------------------------------------------------------------------------------


def test_renaming_a_role_the_account_does_not_hold_leaves_its_page_valid(client, owner, make_user):
    account = make_user("other_role_renamed", "technical")
    unrelated = Group.objects.create(name="Example Thursday rota")
    client.force_login(owner)
    data = _admin_change_data(client, account, first_name="Lee")
    unrelated.name = "Example Friday rota"
    unrelated.save()
    assert client.post(reverse(ACCOUNT_URL, args=[account.pk]), data).status_code == 302
    assert User.objects.get(pk=account.pk).first_name == "Lee"


def test_a_page_drawn_after_a_role_rename_saves_and_names_it_once(client, owner, make_user):
    account = make_user("role_renamed_before", "technical")
    rota = Group.objects.create(name="Example Thursday rota")
    account.groups.add(rota)
    rota.name = "Example Friday rota"
    rota.save()
    client.force_login(owner)
    data = _admin_change_data(client, account, last_name="Example")
    assert client.post(reverse(ACCOUNT_URL, args=[account.pk]), data).status_code == 302
    event = AuditEvent.objects.get(action="account.updated", object_id=str(account.pk))
    assert event.before["groups"] == event.after["groups"] == ["Example Friday rota", "technical"]


# V4: a stale organization form is told to reload ----------------------------------------------


def test_a_stale_organization_form_refused_on_another_field_also_says_reload(client, agent, clinic):
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    data = edit_data(clinic, email="not an address")
    directory.update_client(
        actor=agent,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        reason="Phone",
        phone="555-0181",
    )
    response = client.post(url, data)
    assert response.status_code == 200
    form = response.context["form"]
    assert directory.CHANGED in form.non_field_errors()
    assert form.errors["email"]


def test_a_current_organization_form_refused_on_a_field_does_not_say_reload(client, agent, clinic):
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    response = client.post(url, edit_data(clinic, email="not an address"))
    assert response.status_code == 200
    assert directory.CHANGED not in response.context["form"].non_field_errors()


# V6: the owner list and field ---------------------------------------------------------------------


def test_the_owner_lookup_offers_only_active_staff_with_a_role(client, agent, make_user, clinic):
    make_user("lookup_technician", "technical", first_name="Lookup")
    make_user("lookup_roleless", first_name="Lookup")
    inactive = make_user("lookup_inactive", "technical", first_name="Lookup")
    deactivate(inactive, agent)()
    make_user("lookup_portal", client=clinic, first_name="Lookup")
    client.force_login(agent)
    found = client.get(reverse("lookup", args=["role-holders"]), {"q": "Lookup"}).json()
    usernames = {User.objects.get(pk=row["value"]).username for row in found["results"]}
    assert usernames == {"lookup_technician"}


def test_the_owner_lookup_and_field_refuse_a_portal_account_and_a_roleless_owner(
    client, agent, make_user, clinic, clinic_portal
):
    client.force_login(clinic_portal)
    assert client.get(reverse("lookup", args=["role-holders"])).status_code in (403, 404)
    roleless = make_user("roleless_choice")
    client.force_login(agent)
    url = reverse("clients:client-edit", args=[clinic.pk])
    response = client.post(url, edit_data(clinic, account_owner=roleless.pk))
    assert response.status_code == 200 and response.context["form"].errors["account_owner"]
    assert Client.objects.get(pk=clinic.pk).account_owner_id is None


# V7: the first breach run -------------------------------------------------------------------------


def test_a_first_run_deferred_by_the_settings_creates_the_account_at_the_next(
    agent, make_user, category, accessioning, join, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    lead = make_user("first_run_lead", "technical")
    join(lead, accessioning, lead=True)
    ServiceTarget.objects.create(name="First run target", resolution_minutes=60)
    ticket = new_request(agent, category, team=accessioning)
    now = timezone.now() + timedelta(hours=2)
    admin = make_user("first_run_admin", "admin")
    row = JobSettings.objects.get_or_create(pk=1)[0]
    assert row.system_account_id is None
    saving = Held(
        lambda: update_settings(
            actor=admin, expected_revision=row.revision, close_resolved_after_days=9
        )
    )
    try:
        assert clock_jobs.notice_breaches(now=now) == {"breaches": 0, "notices": 0, "busy": 1}
    finally:
        assert saving.release() == "done", saving.session.error
    assert clock_jobs.notice_breaches(now=now)["notices"] == 1
    assert JobSettings.objects.get(pk=1).system_account_id is not None
    assert RequestClock.objects.get(request=ticket).resolution_breach_noticed_at


# V10: a refused reply on a ticket still open ---------------------------------------------------


def test_a_reply_refused_as_changed_is_sent_by_sending_it_again(
    client, agent, sample_category, clinic, clinic_portal
):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, sample_category, client=clinic)
    client.force_login(clinic_portal)
    page = client.get(reverse("portal:request-detail", args=[ticket.number]))
    drawn = page.context["reply_form"]["expected_revision"].value()
    url = page.context["reply_url"]
    services.add_entry(
        actor=agent, request=ticket, kind=EntryKind.REPLY, body="Checked.", expected_revision=drawn
    )
    body = "Order ORD-TEST-000870 is the one."
    refused = client.post(url, {"body": body, "expected_revision": drawn})
    assert refused.status_code == 200 and refused.context["reply_form"].errors
    assert b"Checked." in refused.content  # the page shows the ticket as it is now
    form = refused.context["reply_form"]
    assert form["body"].value() == body
    again = {"body": form["body"].value(), "expected_revision": form["expected_revision"].value()}
    # A further change before sending again refuses it again.
    ticket.refresh_from_db()
    services.add_entry(
        actor=agent,
        request=ticket,
        kind=EntryKind.REPLY,
        body="One more thing.",
        expected_revision=ticket.revision,
    )
    second = client.post(url, again)
    assert second.status_code == 200 and second.context["reply_form"].errors
    assert not RequestEntry.objects.filter(request=ticket, body=body).exists()
    form = second.context["reply_form"]
    sent = client.post(
        url, {"body": form["body"].value(), "expected_revision": form["expected_revision"].value()}
    )
    assert sent.status_code == 302
    assert RequestEntry.objects.filter(request=ticket, body=body).count() == 1
    ticket.refresh_from_db()
    assert ticket.status == Status.OPEN


def test_a_reply_refused_for_a_missing_text_keeps_the_revision_it_was_drawn_with(
    client, agent, sample_category, clinic, clinic_portal
):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    ticket = new_request(agent, sample_category, client=clinic)
    client.force_login(clinic_portal)
    url = reverse("portal:request-reply", args=[ticket.number])
    refused = client.post(url, {"body": "", "expected_revision": ticket.revision})
    assert refused.status_code == 200
    assert str(refused.context["reply_form"]["expected_revision"].value()) == str(ticket.revision)
