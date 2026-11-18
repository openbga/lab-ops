"""Client pages with tickets: sites (C2), contact sites (C3), portal access by site
(C4), the organization page (C5), the per-client timeline (C6) and the Email notices page (N1).
Kept with the desk tests because they need its fixtures. Synthetic data only."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from clients.models import ClientContact, ClientSite, PortalAccess
from clients.services import (
    add_contact,
    create_site,
    set_portal_access,
    update_contact,
    update_site,
)
from clients.timeline import timeline_page
from core.models import AuditEvent, UserPreference
from desk.models import EntryKind, ServiceRequest
from desk.notifications import OPT_OUT_KEY
from desk.services import add_entry, create_request

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(make_user):
    return make_user("manager", "account_manager")


def log(agent, category, client, site=None, subject="Logged", **extra):
    return create_request(
        actor=agent,
        category=category,
        subject=subject,
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Internal start.",
        channel="phone",
        client=client,
        site=site,
        **extra,
    )


# C2 sites -----------------------------------------------------------------------------------


def test_site_code_unique_per_organization_ignoring_case(manager, clinic, water):
    create_site(actor=manager, client_id=clinic.pk, code="MAIN", name="Main office")
    with pytest.raises(ValidationError):
        create_site(actor=manager, client_id=clinic.pk, code="main", name="Another")
    # Free across organizations, and blank codes never clash.
    create_site(actor=manager, client_id=water.pk, code="main", name="Main pump house")
    create_site(actor=manager, client_id=clinic.pk, name="No code one")
    create_site(actor=manager, client_id=clinic.pk, name="No code two")
    with pytest.raises(IntegrityError), transaction.atomic():
        ClientSite.objects.create(client=clinic, code="Main", name="Bypass")


def test_site_changes_need_a_reason_are_audited_and_refuse_stale(manager, clinic):
    site = create_site(actor=manager, client_id=clinic.pk, code="MAIN", name="Main office")
    with pytest.raises(ValidationError):
        update_site(actor=manager, site_id=site.pk, expected_revision=1, reason="", name="X")
    update_site(actor=manager, site_id=site.pk, expected_revision=1, reason="Renamed", name="HQ")
    with pytest.raises(ValidationError, match="changed"):
        update_site(actor=manager, site_id=site.pk, expected_revision=1, reason="x", name="Y")
    actions = list(
        AuditEvent.objects.filter(object_type="clients.clientsite").values_list("action", "reason")
    )
    assert sorted(actions) == [("site.created", ""), ("site.updated", "Renamed")]


def test_deactivating_a_site_keeps_its_requests(manager, agent, category, clinic, clinic_site):
    request = log(agent, category, clinic, clinic_site)
    update_site(
        actor=manager,
        site_id=clinic_site.pk,
        expected_revision=clinic_site.revision,
        reason="Office closed",
        is_active=False,
    )
    request.refresh_from_db()
    assert request.site == clinic_site
    assert not ClientSite.objects.get(pk=clinic_site.pk).is_active


def test_site_pages(client, make_user, manager, clinic, water):
    client.force_login(make_user("tech", "technical"))
    assert client.get(reverse("clients:site-create", args=[clinic.pk])).status_code == 403
    client.force_login(manager)
    response = client.post(
        reverse("clients:site-create", args=[clinic.pk]),
        {"name": "Westside office", "code": "WEST", "kind": "office", "phone": "555-0105"},
    )
    assert response.status_code == 302
    site = ClientSite.objects.get(code="WEST")
    # Another organization's URL for this site is a 404.
    assert client.get(reverse("clients:site-edit", args=[water.pk, site.pk])).status_code == 404
    page = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    assert "Westside office" in page and 'href="tel:555-0105"' in page
    response = client.post(
        reverse("clients:site-create", args=[clinic.pk]),
        {"name": "Duplicate", "code": "west", "kind": "office"},
    )
    assert response.status_code == 200 and "already uses this code" in response.content.decode()


# C3 contact sites ---------------------------------------------------------------------------


def test_contact_cannot_serve_another_organizations_site(manager, clinic, clinic_site, water_site):
    with pytest.raises(ValidationError) as exc:
        add_contact(
            actor=manager,
            client_id=clinic.pk,
            name="Example Courier",
            phone="555-0106",
            sites=[water_site],
        )
    assert "sites" in exc.value.message_dict
    contact = add_contact(
        actor=manager,
        client_id=clinic.pk,
        name="Example Courier",
        phone="555-0106",
        sites=[clinic_site],
    )
    assert list(contact.sites.all()) == [clinic_site]
    with pytest.raises(ValidationError):
        update_contact(
            actor=manager,
            contact_id=contact.pk,
            expected_revision=1,
            reason="x",
            sites=[water_site],
        )
    update_contact(
        actor=manager, contact_id=contact.pk, expected_revision=1, reason="Whole org", sites=[]
    )
    event = AuditEvent.objects.get(action="client_contact.updated")
    assert event.before["sites"] == [clinic_site.pk] and event.after["sites"] == []


def test_contact_form_offers_only_this_organizations_sites(
    client, manager, clinic, clinic_site, water_site
):
    client.force_login(manager)
    url = reverse("clients:contact-create", args=[clinic.pk])
    form = client.get(url).context["form"]
    assert set(form.fields["sites"].queryset) == {clinic_site}
    response = client.post(
        url, {"name": "Example Liaison", "phone": "555-0107", "sites": [water_site.pk]}
    )
    assert (
        response.status_code == 200
        and not ClientContact.objects.filter(name="Example Liaison").exists()
    )
    response = client.post(
        url, {"name": "Example Liaison", "phone": "555-0107", "sites": [clinic_site.pk]}
    )
    assert response.status_code == 302


# C4 portal access by site -------------------------------------------------------------------


def test_portal_access_by_site(manager, make_user, clinic, clinic_site, water_site):
    account = make_user("portal", client=clinic)
    with pytest.raises(ValidationError) as exc:
        set_portal_access(
            actor=manager,
            user_id=account.pk,
            expected_client_id=account.client_id,
            sites=[water_site],
        )
    assert "sites" in exc.value.message_dict
    with pytest.raises(ValidationError):  # listed sites need at least one
        set_portal_access(
            actor=manager, user_id=account.pk, expected_client_id=account.client_id, sites=[]
        )
    access = set_portal_access(
        actor=manager, user_id=account.pk, expected_client_id=account.client_id, sites=[clinic_site]
    )
    assert list(access.sites.all()) == [clinic_site] and not access.whole_organization
    access = set_portal_access(
        actor=manager,
        user_id=account.pk,
        expected_client_id=account.client_id,
        whole_organization=True,
        sites=[clinic_site],
        expected_revision=1,
        reason="Whole organization now",
    )
    assert access.whole_organization and not access.sites.exists()
    event = AuditEvent.objects.filter(action="portal_access.set").latest("occurred_at")
    assert event.before["sites"] == [clinic_site.pk] and event.after["sites"] == []


def test_portal_access_form_with_sites(client, manager, make_user, clinic, clinic_site):
    account = make_user("portal", client=clinic)
    client.force_login(manager)
    url = reverse("clients:portal-access-edit", args=[clinic.pk, account.pk])
    response = client.post(url, {"scope": "sites", "expected_revision": 0})
    assert response.status_code == 200 and "sites" in response.context["form"].errors
    response = client.post(
        url, {"scope": "sites", "sites": [clinic_site.pk], "expected_revision": 0}
    )
    assert response.status_code == 302
    assert list(PortalAccess.objects.get(user=account).sites.all()) == [clinic_site]
    page = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    assert "Main office" in page


# C5 organization page -----------------------------------------------------------------------


def test_organization_counts_honour_restricted_categories(
    client, make_user, agent, category, privacy_category, clinic, quality, join
):
    log(agent, category, clinic, subject="Ordinary question")
    log(agent, privacy_category, clinic, subject="Privacy matter")
    reader = make_user("reader", "technical")
    client.force_login(reader)
    response = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    assert response.context["open_count"] == 1
    html = response.content.decode()
    assert "Ordinary question" in html and "Privacy matter" not in html
    supervisor = make_user("boss", "supervisor")
    client.force_login(supervisor)
    response = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    assert response.context["open_count"] == 2
    member = make_user("member", "technical")
    join(member, quality)
    client.force_login(member)
    assert client.get(reverse("clients:client-detail", args=[clinic.pk])).context["open_count"] == 2


def test_show_all_open_tickets_uses_the_authorized_organization_queue(
    client, make_user, agent, category, privacy_category, clinic, water
):
    visible = [log(agent, category, clinic) for _ in range(9)]
    log(agent, privacy_category, clinic)
    log(agent, category, water)
    client.force_login(make_user("reader", "technical"))
    response = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    assert b"Showing the 8 most recently updated of 9 open tickets" in response.content
    assert len(response.context["open_requests"]) == 8
    queue = client.get(response.context["open_tickets_url"])
    assert set(queue.context["page_obj"]) == set(visible)


def test_new_request_for_this_organization_prefills(client, agent, clinic):
    from clients.views import new_request_url

    client.force_login(agent)
    html = client.get(reverse("clients:client-detail", args=[clinic.pk])).content.decode()
    url = new_request_url(clinic)
    assert f"?client={clinic.pk}" in url and url in html
    response = client.get(url)
    assert response.status_code == 200
    assert response.context["form"].initial.get("client") in (clinic, clinic.pk, str(clinic.pk))


def test_organization_page_query_budget(client, agent, category, clinic, make_user):
    for index in range(12):
        site = ClientSite.objects.create(client=clinic, name=f"Site {index}")
        ClientContact.objects.create(client=clinic, name=f"Contact {index}", phone="555-0100")
        log(agent, category, clinic, site, subject=f"Request {index}")
        account = make_user(f"portal{index}", client=clinic)
        PortalAccess.objects.create(
            user=account, client_id=account.client_id, whole_organization=index % 2 == 0
        )
    client.force_login(agent)
    url = reverse("clients:client-detail", args=[clinic.pk])
    client.get(url)
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200
    assert len(queries) <= 30, len(queries)


# C6 timeline --------------------------------------------------------------------------------


def test_timeline_merges_entries_and_changes_newest_first(
    manager, agent, category, clinic, clinic_site
):
    request = log(agent, category, clinic, clinic_site, subject="First")
    add_entry(
        actor=agent,
        request=request,
        kind=EntryKind.REPLY,
        body="We are on it.",
        expected_revision=request.revision,
    )
    contact = add_contact(actor=manager, client_id=clinic.pk, name="New person", phone="555")
    page = timeline_page(agent, clinic)
    kinds = [item["kind"] for item in page.object_list]
    assert kinds[0] == "organization"  # the contact, newest
    assert kinds[1:] == ["reply", "note", "opened"]
    times = [item["at"] for item in page.object_list]
    assert times == sorted(times, reverse=True)
    assert page.object_list[0]["event"].object_id == str(contact.pk)


def test_timeline_filters_by_kind_and_site(
    manager, agent, category, clinic, clinic_site, water, water_site
):
    other_site = ClientSite.objects.create(client=clinic, name="Elsewhere")
    log(agent, category, clinic, clinic_site, subject="At main")
    log(agent, category, clinic, other_site, subject="Elsewhere")
    log(agent, category, water, water_site, subject="Other organization")
    opened = timeline_page(agent, clinic, kinds=["opened"]).object_list
    assert {item["entry"].request.subject for item in opened} == {"At main", "Elsewhere"}
    at_main = timeline_page(agent, clinic, site=clinic_site).object_list
    assert {item["entry"].request.subject for item in at_main if "entry" in item} == {"At main"}
    notes = timeline_page(agent, clinic, kinds=["note"]).object_list
    assert all(item["entry"].kind == "note" for item in notes) and len(notes) == 2


def test_timeline_hides_restricted_requests_from_non_members(
    client, make_user, agent, privacy_category, category, clinic, quality, join
):
    log(agent, privacy_category, clinic, subject="Privacy matter")
    log(agent, category, clinic, subject="Ordinary")
    reader = make_user("reader", "technical")
    subjects = {
        item["entry"].request.subject
        for item in timeline_page(reader, clinic).object_list
        if "entry" in item
    }
    assert subjects == {"Ordinary"}
    join(reader, quality)
    subjects = {
        item["entry"].request.subject
        for item in timeline_page(reader, clinic).object_list
        if "entry" in item
    }
    assert subjects == {"Ordinary", "Privacy matter"}
    client.force_login(make_user("other_reader", "technical"))
    html = client.get(reverse("clients:client-timeline", args=[clinic.pk])).content.decode()
    assert "Ordinary" in html and "Privacy matter" not in html


def test_timeline_pagination_is_stable(agent, category, clinic):
    for index in range(30):
        log(agent, category, clinic, subject=f"Request {index}")
    seen = []
    page_number = 1
    while True:
        page = timeline_page(agent, clinic, page=page_number, per_page=25)
        seen += [(item["at"], item["entry"].pk) for item in page.object_list]
        if not page.has_next():
            break
        page_number += 1
    assert len(seen) == len(set(seen)) == 60  # "created" event and first note per request
    assert seen == sorted(seen, reverse=True)


def test_timeline_page_access(client, make_user, clinic):
    url = reverse("clients:client-timeline", args=[clinic.pk])
    portal = make_user("portal", client=clinic)
    client.force_login(portal)
    assert client.get(url).status_code == 403
    client.force_login(make_user("tech", "technical"))
    assert client.get(url).status_code == 200
    assert client.get(url, {"kind": "nonsense"}).context["page_obj"].object_list == []


# N1 email notices page ----------------------------------------------------------------------


def test_email_notices_page(client, make_user, clinic, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    staff = make_user("staffer", "technical")
    client.force_login(staff)
    url = reverse("email-notices")
    assert client.get(url).context["form"].initial["enabled"] is True
    client.post(url, {})
    assert UserPreference.objects.get(user=staff, key=OPT_OUT_KEY).value is False
    saved = client.post(url, {"enabled": "on"}, follow=True)
    assert b"Email is off" in saved.content
    assert b"Email notice preference saved" in saved.content
    assert not UserPreference.objects.filter(user=staff, key=OPT_OUT_KEY).exists()
    client.force_login(make_user("portal", client=clinic))
    assert client.get(url).status_code == 403


def test_portal_cannot_list_requests_of_inactive_scope(
    make_user, clinic, clinic_site, agent, category
):
    """A request stays readable to a site-scoped account after its site is deactivated."""
    account = make_user("portal", client=clinic)
    access = PortalAccess.objects.create(user=account, client_id=account.client_id)
    access.sites.add(clinic_site)
    request = log(agent, category, clinic, clinic_site)
    ClientSite.objects.filter(pk=clinic_site.pk).update(is_active=False)
    from desk.portal_scope import portal_requests

    assert list(portal_requests(account)) == [request]
    assert ServiceRequest.objects.count() == 1
