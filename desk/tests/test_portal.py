"""Client portal requests (P1 to P4): scope by organization, site and raiser; raising and
following up; what the request page shows and never shows; replies; the home page counts.
Direct URLs are tested, not only lists. All data is synthetic."""

import pytest
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSite, PortalAccess
from desk.models import EntryKind, RequestEntry, ServiceRequest, Status
from desk.portal_scope import portal_requests
from desk.services import add_entry, create_request
from taxonomy.models import RequestCategory
from taxonomy.terms import drawn

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

SECRET = "SENTINEL-INTERNAL-7731"


@pytest.fixture
def west_site(clinic):
    return ClientSite.objects.create(client=clinic, code="WEST", name="Westside office")


@pytest.fixture
def whole(make_user, clinic):
    user = make_user("whole_portal", client=clinic)
    PortalAccess.objects.create(user=user, client_id=user.client_id, whole_organization=True)
    return user


@pytest.fixture
def scoped(make_user, clinic, clinic_site):
    user = make_user("site_portal", client=clinic)
    access = PortalAccess.objects.create(
        user=user, client_id=user.client_id, whole_organization=False
    )
    access.sites.add(clinic_site)
    return user


@pytest.fixture
def no_access(make_user, clinic):
    return make_user("new_portal", client=clinic)


@pytest.fixture
def water_portal(make_user, water):
    user = make_user("water_portal", client=water)
    PortalAccess.objects.create(user=user, client_id=user.client_id, whole_organization=True)
    return user


def raise_staff(agent, category, client=None, site=None, subject="Staff logged", **extra):
    return create_request(
        actor=agent,
        category=category,
        subject=subject,
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Logged by the laboratory.",
        channel="phone",
        client=client,
        site=site,
        **extra,
    )


def set_status(request, status):
    now = timezone.now()
    fields = {"status": status, "resolved_at": None, "closed_at": None}
    if status in (Status.RESOLVED, Status.CLOSED):
        fields["resolved_at"] = now
    if status == Status.CLOSED:
        fields["closed_at"] = now
    ServiceRequest.objects.filter(pk=request.pk).update(**fields)
    request.refresh_from_db()
    return request


def main(response):
    """The page's own content (<main>), without the page-help drawer's text."""
    html = response.content.decode()
    return html[html.index("<main") : html.index("</main>")]


def login(client, user):
    assert client.login(username=user.username, password=PASSWORD)


# P1 -----------------------------------------------------------------------------------------


def test_site_scoped_account_sees_its_sites_and_its_own(
    client, agent, category, clinic, clinic_site, west_site, water_site, water, scoped
):
    main = raise_staff(agent, category, clinic, clinic_site, "Main office request")
    west = raise_staff(agent, category, clinic, west_site, "Westside request")
    nosite = raise_staff(agent, category, clinic, None, "Organization request")
    other = raise_staff(agent, category, water, water_site, "Water request")
    own = create_request(
        actor=scoped,
        category=category,
        subject="Raised by the site account",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Please help.",
        channel="portal",
        site=clinic_site,
    )
    # Its own request stays visible after staff move it to a site outside its scope.
    ServiceRequest.objects.filter(pk=own.pk).update(site=west_site)
    assert set(portal_requests(scoped)) == {main, own}
    login(client, scoped)
    page = client.get(reverse("portal:request-list")).content.decode()
    assert main.number in page and own.number in page
    for hidden in (west, nosite, other):
        assert hidden.number not in page
        url = reverse("portal:request-detail", args=[hidden.number])
        assert client.get(url).status_code == 404
        reply = reverse("portal:request-reply", args=[hidden.number])
        assert client.post(reply, {"body": "x", "expected_revision": 1}).status_code == 404
        follow = reverse("portal:request-create") + f"?follow_up={hidden.number}"
        assert client.get(follow).status_code == 404


def test_whole_organization_sees_all_of_its_organization_only(
    client, agent, category, clinic, water, clinic_site, water_site, whole
):
    main = raise_staff(agent, category, clinic, clinic_site)
    nosite = raise_staff(agent, category, clinic)
    other = raise_staff(agent, category, water, water_site)
    assert set(portal_requests(whole)) == {main, nosite}
    login(client, whole)
    assert client.get(reverse("portal:request-detail", args=[main.number])).status_code == 200
    assert client.get(reverse("portal:request-detail", args=[other.number])).status_code == 404


def test_account_without_access_sees_only_what_it_raised(
    client, agent, category, clinic, no_access
):
    staff_logged = raise_staff(agent, category, clinic)
    own = create_request(
        actor=no_access,
        category=category,
        subject="Mine",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Hello",
        channel="portal",
    )
    assert list(portal_requests(no_access)) == [own]
    login(client, no_access)
    url = reverse("portal:request-detail", args=[staff_logged.number])
    assert client.get(url).status_code == 404


def test_restricted_requests_only_for_the_account_that_raised_them(
    client, agent, privacy_category, clinic, whole, no_access
):
    RequestCategory.objects.filter(pk=privacy_category.pk).update(client_may_raise=True)
    privacy_category.refresh_from_db()
    staff_logged = raise_staff(agent, privacy_category, clinic)
    own = create_request(
        actor=no_access,
        category=privacy_category,
        subject="Privacy",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Hello",
        channel="portal",
    )
    assert list(portal_requests(whole)) == []
    assert list(portal_requests(no_access)) == [own]
    login(client, whole)
    for hidden in (staff_logged, own):
        assert client.get(reverse("portal:request-detail", args=[hidden.number])).status_code == 404


def test_inactive_and_staff_accounts(client, agent, category, clinic, whole):
    request = raise_staff(agent, category, clinic)
    whole.is_active = False
    whole.save()
    assert list(portal_requests(whole)) == []
    client.force_login(agent)
    for name, args in (
        ("portal:request-list", []),
        ("portal:request-create", []),
        ("portal:request-detail", [request.number]),
    ):
        assert client.get(reverse(name, args=args)).status_code == 403
    reply = reverse("portal:request-reply", args=[request.number])
    assert client.post(reply, {"body": "x", "expected_revision": 1}).status_code == 403


def test_list_puts_open_first_and_filters_by_status(client, agent, category, clinic, whole):
    closed = set_status(raise_staff(agent, category, clinic, subject="Closed one"), Status.CLOSED)
    waiting = set_status(
        raise_staff(agent, category, clinic, subject="Waiting one"), Status.WAITING_CLIENT
    )
    resolved = set_status(
        raise_staff(agent, category, clinic, subject="Resolved one"), Status.RESOLVED
    )
    login(client, whole)
    rows = list(client.get(reverse("portal:request-list")).context["page_obj"])
    assert rows == [waiting, resolved, closed]
    filtered = client.get(reverse("portal:request-list"), {"status": "waiting_client"})
    assert list(filtered.context["page_obj"]) == [waiting]
    active = client.get(reverse("portal:request-list"), {"status": "active"})
    assert list(active.context["page_obj"]) == [waiting]
    assert "Waiting for you" in filtered.content.decode()


# P2 -----------------------------------------------------------------------------------------


def raise_post(client, **data):
    """As the page drew it: with the offered topics' settings (taxonomy.terms)."""
    data.setdefault("category_terms", drawn(RequestCategory.objects.filter(pk=data["category"])))
    return client.post(reverse("portal:request-create"), data)


def test_raise_offers_only_active_client_categories(
    client, group, category, privacy_category, whole
):
    hidden_inactive = RequestCategory.objects.create(
        key="inactive", name="Old topic", parent=group, client_may_raise=True, is_active=False
    )
    labelled = RequestCategory.objects.create(
        key="supplies",
        name="Supply order",
        client_label="Order supplies",
        parent=group,
        client_may_raise=True,
    )
    login(client, whole)
    page = client.get(reverse("portal:request-create"))
    offered = set(page.context["form"].fields["category"].queryset)
    assert offered == {category, labelled}
    html = page.content.decode()
    assert "Order supplies" in html and "Supply order" not in html
    assert "do not enter patient names" in html.lower()
    response = raise_post(client, category=hidden_inactive.pk, subject="x", description="y")
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    response = raise_post(client, category=group.pk, subject="x", description="y")
    assert response.status_code == 200 and not ServiceRequest.objects.exists()
    response = raise_post(client, category=privacy_category.pk, subject="x", description="y")
    assert response.status_code == 200 and not ServiceRequest.objects.exists()


def test_site_required_when_scoped(client, category, clinic_site, west_site, scoped):
    login(client, scoped)
    form = client.get(reverse("portal:request-create")).context["form"]
    assert form.fields["site"].required
    assert set(form.fields["site"].queryset) == {clinic_site}
    response = raise_post(client, category=category.pk, subject="x", description="y")
    assert "site" in response.context["form"].errors
    response = raise_post(
        client, category=category.pk, subject="x", description="y", site=west_site.pk
    )
    assert "site" in response.context["form"].errors
    assert not ServiceRequest.objects.exists()


def test_site_required_when_the_organization_has_several(
    client, category, clinic_site, west_site, whole
):
    login(client, whole)
    form = client.get(reverse("portal:request-create")).context["form"]
    assert form.fields["site"].required
    assert set(form.fields["site"].queryset) == {clinic_site, west_site}
    response = raise_post(
        client, category=category.pk, subject="x", description="y", site=west_site.pk
    )
    assert response.status_code == 302
    assert ServiceRequest.objects.get().site == west_site


def test_site_optional_with_one_site_and_absent_with_none(client, category, clinic, whole):
    login(client, whole)
    assert "site" not in client.get(reverse("portal:request-create")).context["form"].fields
    site = ClientSite.objects.create(client=clinic, name="Only office")
    form = client.get(reverse("portal:request-create")).context["form"]
    assert not form.fields["site"].required and set(form.fields["site"].queryset) == {site}
    response = raise_post(client, category=category.pk, subject="No site", description="y")
    assert response.status_code == 302


def test_raise_records_portal_reply_reference_and_urgent_as_high(client, category, clinic, whole):
    login(client, whole)
    response = raise_post(
        client,
        category=category.pk,
        subject="Result missing",
        description="Order ORD-TEST-1 has no report.",
        client_reference="ORD-TEST-1",
        urgent="on",
    )
    request = ServiceRequest.objects.get()
    assert response.url == reverse("portal:request-detail", args=[request.number])
    assert (request.channel, request.priority, request.client, request.created_by) == (
        "portal",
        "high",
        clinic,
        whole,
    )
    assert request.client_reference == "ORD-TEST-1"
    first = request.entries.exclude(kind=EntryKind.EVENT).get()
    assert first.kind == EntryKind.REPLY
    raise_post(client, category=category.pk, subject="Not urgent", description="y")
    assert ServiceRequest.objects.latest("pk").priority == category.default_priority


def test_follow_up_of_a_closed_request(client, agent, category, clinic, whole):
    earlier = raise_staff(agent, category, clinic)
    login(client, whole)
    url = reverse("portal:request-create") + f"?follow_up={earlier.number}"
    assert client.get(url).status_code == 404  # still open
    set_status(earlier, Status.CLOSED)
    page = main(client.get(reverse("portal:request-detail", args=[earlier.number])))
    assert "New follow-up ticket" in page and "Send reply" not in page
    assert earlier.number in client.get(url).content.decode()
    response = client.post(
        reverse("portal:request-create"),
        {
            "category": category.pk,
            "category_terms": drawn([category]),
            "subject": "Still wrong",
            "description": "y",
            "follow_up": earlier.number,
        },
    )
    assert response.status_code == 302
    later = ServiceRequest.objects.latest("pk")
    assert later.follow_up_of == earlier
    page = client.get(reverse("portal:request-detail", args=[later.number])).content.decode()
    assert earlier.number in page


# P3 -----------------------------------------------------------------------------------------


def test_request_page_never_renders_internal_material(
    client, make_user, agent, category, clinic, clinic_contact, whole, accessioning, join
):
    join(agent, accessioning)
    accessioning.name = f"Team {SECRET}-team"
    accessioning.save()
    request = raise_staff(
        agent,
        category,
        clinic,
        contact=clinic_contact,
        reported_by=f"{SECRET}-reporter",
        team=accessioning,
        assignee=agent,
        priority="urgent",
    )
    agent.first_name, agent.last_name = f"{SECRET}-assignee", "Person"
    agent.save()
    add_entry(
        actor=agent,
        request=request,
        kind=EntryKind.NOTE,
        body=f"{SECRET}-note",
        expected_revision=request.revision,
    )
    request.refresh_from_db()
    add_entry(
        actor=agent,
        request=request,
        kind=EntryKind.CONTACT,
        body=f"{SECRET}-contact",
        expected_revision=request.revision,
        contact={
            "direction": "to_client",
            "channel": "phone",
            "outcome": "spoke_with",
            "with_whom": f"{SECRET}-with",
        },
    )
    request.refresh_from_db()
    withdrawn = add_entry(
        actor=agent,
        request=request,
        kind=EntryKind.REPLY,
        body=f"{SECRET}-withdrawn",
        expected_revision=request.revision,
    )
    RequestEntry.objects.filter(pk=withdrawn.pk).update(
        withdrawn_at=timezone.now(), withdrawn_by=agent, withdrawal_reason="Wrong request"
    )
    request.refresh_from_db()
    add_entry(
        actor=agent,
        request=request,
        kind=EntryKind.REPLY,
        body="Visible answer",
        expected_revision=request.revision,
    )
    login(client, whole)
    response = client.get(reverse("portal:request-detail", args=[request.number]))
    full, html = response.content.decode(), main(response)
    assert SECRET not in full.replace(f"Team {SECRET}-team", "")
    assert "Visible answer" in html and "Logged by the laboratory." in html
    assert SECRET not in html.replace(f"Team {SECRET}-team", "")  # team only in the signature
    for word in (
        "Urgent",
        "urgent",
        "Priority",
        "Assignee",
        "Internal note",
        "Logged contact",
        "complaint",
        "Due",
        "overdue",
    ):
        assert word not in html, word
    # Staff replies are signed with the team and laboratory, never the person.
    assert f"{SECRET}-assignee" not in html


def test_shown_links_only_and_never_person_level(client, agent, category, clinic, whole):
    from links import models as links

    system = links.ExternalSystem.objects.get(key="lis")
    request = raise_staff(agent, category, clinic)

    def attach(identifier, label, record_type, shown):
        link = links.ExternalRecordLink.objects.create(
            system=system,
            record_type=record_type,
            identifier=identifier,
            display_label=label,
            is_person=record_type == "patient",
            created_by=agent,
        )
        return links.LinkAttachment.objects.create(
            link=link,
            request=request,
            role="related",
            is_person=link.is_person,
            shown_to_client=shown,
            added_by=agent,
        )

    attach("ORD-TEST-000042", "ORD-TEST-000042", "order", True)
    attach("ORD-TEST-000099", "ORD-TEST-000099", "order", False)
    attach("PT-TEST-00004821", "Patient ····4821", "patient", False)
    login(client, whole)
    html = client.get(reverse("portal:request-detail", args=[request.number])).content.decode()
    assert "ORD-TEST-000042" in html
    assert "ORD-TEST-000099" not in html and "4821" not in html


@pytest.mark.parametrize("status", [Status.WAITING_CLIENT, Status.RESOLVED])
def test_client_reply_reopens_waiting_and_resolved(client, agent, category, clinic, whole, status):
    request = set_status(raise_staff(agent, category, clinic), status)
    login(client, whole)
    page = client.get(reverse("portal:request-detail", args=[request.number]))
    assert "Send reply" in page.content.decode()
    response = client.post(
        reverse("portal:request-reply", args=[request.number]),
        {"body": "Here is the information.", "expected_revision": request.revision},
    )
    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == Status.OPEN
    html = client.get(reverse("portal:request-detail", args=[request.number])).content.decode()
    assert "Here is the information." in html and "You wrote" in html
    assert "Status: With the laboratory" in html


def test_closed_request_refuses_replies(client, agent, category, clinic, whole):
    request = set_status(raise_staff(agent, category, clinic), Status.CLOSED)
    login(client, whole)
    response = client.post(
        reverse("portal:request-reply", args=[request.number]),
        {"body": "Late reply", "expected_revision": request.revision},
    )
    assert response.status_code == 302
    assert not request.entries.filter(body="Late reply").exists()


def test_stale_reply_keeps_the_text(client, agent, category, clinic, whole):
    request = raise_staff(agent, category, clinic)
    login(client, whole)
    response = client.post(
        reverse("portal:request-reply", args=[request.number]),
        {"body": "My careful answer", "expected_revision": request.revision + 5},
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "My careful answer" in html and "changed" in html


# P4 -----------------------------------------------------------------------------------------


def test_home_counts_match_the_scope(
    client, agent, category, clinic, water, clinic_site, west_site, water_site, scoped
):
    mine = set_status(raise_staff(agent, category, clinic, clinic_site), Status.WAITING_CLIENT)
    raise_staff(agent, category, clinic, clinic_site)
    set_status(raise_staff(agent, category, clinic, clinic_site), Status.RESOLVED)
    set_status(raise_staff(agent, category, clinic, west_site), Status.WAITING_CLIENT)
    set_status(raise_staff(agent, category, water, water_site), Status.WAITING_CLIENT)
    login(client, scoped)
    response = client.get(reverse("portal:home"))
    assert response.context["open_count"] == 2
    assert response.context["waiting_count"] == 1
    assert [r.pk for r in response.context["open_requests"]][0] == mine.pk
    listed = client.get(reverse("portal:request-list"), {"status": "active"})
    assert listed.context["page_obj"].paginator.count == 2
    waiting = client.get(reverse("portal:request-list"), {"status": "waiting_client"})
    assert waiting.context["page_obj"].paginator.count == 1
    assert "1 waiting for you" in response.content.decode()
