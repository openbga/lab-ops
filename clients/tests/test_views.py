"""Client pages for staff: every staff role reads, the manager roles change."""

import pytest
from django.urls import reverse

from clients.models import Client, ClientContact
from core.models import AuditEvent

from .conftest import MANAGERS, READ_ONLY, READERS

pytestmark = pytest.mark.django_db


def urls(clinic, contact):
    return {
        "list": reverse("clients:client-list"),
        "detail": reverse("clients:client-detail", args=[clinic.pk]),
        "create": reverse("clients:client-create"),
        "edit": reverse("clients:client-edit", args=[clinic.pk]),
        "contact-create": reverse("clients:contact-create", args=[clinic.pk]),
        "contact-edit": reverse("clients:contact-edit", args=[clinic.pk, contact.pk]),
    }


def edit_data(client, **changes):
    data = {
        "name": client.name,
        "account_number": client.account_number,
        "email": client.email,
        "phone": client.phone,
        "address_line1": client.address_line1,
        "address_line2": client.address_line2,
        "city": client.city,
        "state": client.state,
        "postal_code": client.postal_code,
        "notes": client.notes,
        "is_active": "on",
        "expected_revision": client.revision,
        "reason": "Corrected on request",
    }
    return data | changes


def contact_data(contact, **changes):
    data = {
        "name": contact.name,
        "role": contact.role,
        "email": contact.email,
        "phone": contact.phone,
        "is_active": "on",
        "expected_revision": contact.revision,
        "reason": "Corrected on request",
    }
    return data | changes


@pytest.mark.parametrize("role", READERS)
def test_every_staff_role_reads_the_list_and_the_record(client, make_user, clinic, pat, role):
    client.force_login(make_user(f"reader-{role}", role))
    page = urls(clinic, pat)
    listing = client.get(page["list"])
    assert listing.status_code == 200 and b"Example Clinic" in listing.content
    detail = client.get(page["detail"])
    assert detail.status_code == 200
    assert b"Pat Example" in detail.content and b"EXCLIN" in detail.content
    assert b"Internal: pays late." in detail.content  # staff see the internal notes
    controls = {
        "new": f'href="{page["create"]}"',
        "edit": f'href="{page["edit"]}"',
        "add-contact": f'href="{page["contact-create"]}"',
        "edit-contact": f'href="{page["contact-edit"]}"',
    }
    shown = {
        name: needle.encode() in listing.content + detail.content
        for name, needle in controls.items()
    }
    expected = role in MANAGERS
    assert set(shown.values()) == {expected}, (role, shown)


def test_portal_accounts_and_visitors_are_refused_every_staff_page(client, make_user, clinic, pat):
    page = urls(clinic, pat)
    portal = make_user("portal", client=clinic)
    client.force_login(portal)
    for name, url in page.items():
        assert client.get(url).status_code == 403, name
    for name in ("create", "edit", "contact-create", "contact-edit"):
        assert client.post(page[name], {"name": "Refused"}).status_code == 403, name
    client.logout()
    for url in page.values():
        response = client.get(url)
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
        assert client.post(url, {}).status_code == 302
    assert Client.objects.count() == 1 and ClientContact.objects.count() == 1
    assert not AuditEvent.objects.exists()


def test_an_account_without_a_lab_role_is_refused(client, make_user, clinic):
    owner = make_user("owner", is_staff=True, is_superuser=True)
    client.force_login(owner)
    assert client.get(reverse("clients:client-list")).status_code == 403
    assert client.get(reverse("clients:client-detail", args=[clinic.pk])).status_code == 403


@pytest.mark.parametrize("role", READ_ONLY)
def test_read_only_roles_cannot_open_or_submit_any_change(client, make_user, clinic, pat, role):
    client.force_login(make_user(f"readonly-{role}", role))
    page = urls(clinic, pat)
    for name in ("create", "edit", "contact-create", "contact-edit"):
        assert client.get(page[name]).status_code == 403, name
        assert client.post(page[name], edit_data(clinic, name="Refused")).status_code == 403, name
    clinic.refresh_from_db()
    pat.refresh_from_db()
    assert clinic.name == "Example Clinic" and clinic.revision == 1 and pat.revision == 1
    assert Client.objects.count() == 1 and ClientContact.objects.count() == 1
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("role", MANAGERS)
def test_managers_create_edit_and_add_contacts_with_audited_history(client, make_user, role):
    actor = make_user(f"manager-{role}", role)
    client.force_login(actor)
    created = client.post(
        reverse("clients:client-create"),
        {"name": "Example Lab Partner", "account_number": "EXPART", "state": "OH"},
    )
    organization = Client.objects.get(account_number="EXPART")
    assert created.status_code == 302
    assert created["Location"] == reverse("clients:client-detail", args=[organization.pk])
    assert client.get(created["Location"]).status_code == 200

    saved = client.post(
        reverse("clients:client-edit", args=[organization.pk]),
        edit_data(organization, phone="555-0142", reason="Phone supplied"),
    )
    assert saved.status_code == 302
    organization.refresh_from_db()
    assert organization.phone == "555-0142" and organization.revision == 2

    added = client.post(
        reverse("clients:contact-create", args=[organization.pk]),
        {"name": "Sam Example", "role": "Billing", "email": "sam@example.invalid"},
    )
    assert added.status_code == 302
    contact = organization.contacts.get()
    assert contact.role == "Billing"
    changed = client.post(
        reverse("clients:contact-edit", args=[organization.pk, contact.pk]),
        contact_data(contact, phone="555-0143", reason="Direct line"),
    )
    assert changed.status_code == 302
    contact.refresh_from_db()
    assert contact.phone == "555-0143" and contact.revision == 2

    events = {e.action: e for e in AuditEvent.objects.order_by("occurred_at")}
    assert set(events) == {
        "client.created",
        "client.updated",
        "client_contact.added",
        "client_contact.updated",
    }
    assert all(e.actor == actor for e in events.values())
    assert events["client.updated"].reason == "Phone supplied"
    assert events["client.updated"].before["phone"] == ""
    assert events["client.updated"].after["phone"] == "555-0142"
    assert events["client_contact.updated"].reason == "Direct line"
    detail = client.get(reverse("clients:client-detail", args=[organization.pk]))
    assert b"Phone supplied" in detail.content  # the history shows the reason


def test_a_stale_edit_is_refused_and_keeps_what_was_typed(client, make_user, clinic):
    client.force_login(make_user("manager", "account_manager"))
    url = reverse("clients:client-edit", args=[clinic.pk])
    form_opened = edit_data(clinic, phone="555-0111", reason="Typed reason")
    client.post(url, edit_data(clinic, phone="555-0100", city="Changed City", reason="First"))
    clinic.refresh_from_db()
    assert clinic.revision == 2

    refused = client.post(url, form_opened)  # based on revision 1
    assert refused.status_code == 200
    assert b"This client organization changed" in refused.content
    assert b"555-0111" in refused.content and b"Typed reason" in refused.content
    clinic.refresh_from_db()
    assert clinic.phone == "555-0100" and clinic.city == "Changed City" and clinic.revision == 2
    assert AuditEvent.objects.filter(action="client.updated").count() == 1
    assert client.get(url).context["form"]["expected_revision"].value() == 2


def test_a_stale_contact_edit_is_refused_and_keeps_what_was_typed(client, make_user, clinic, pat):
    client.force_login(make_user("manager", "client_services"))
    url = reverse("clients:contact-edit", args=[clinic.pk, pat.pk])
    opened = contact_data(pat, role="Results and reports", reason="Typed reason")
    assert client.post(url, contact_data(pat, phone="555-0150", reason="First")).status_code == 302
    refused = client.post(url, opened)
    assert refused.status_code == 200 and b"This contact changed" in refused.content
    assert b"Typed reason" in refused.content
    pat.refresh_from_db()
    assert pat.role == "Billing" and pat.revision == 2


def test_a_reason_is_required_to_edit(client, make_user, clinic, pat):
    client.force_login(make_user("manager", "account_manager"))
    for name, url, data in (
        ("client", reverse("clients:client-edit", args=[clinic.pk]), edit_data(clinic, phone="1")),
        (
            "contact",
            reverse("clients:contact-edit", args=[clinic.pk, pat.pk]),
            contact_data(pat, phone="555-0150"),
        ),
    ):
        for reason in ("", "   "):
            refused = client.post(url, data | {"reason": reason})
            assert refused.status_code == 200, name
            assert "reason" in refused.context["form"].errors, name
    clinic.refresh_from_db()
    pat.refresh_from_db()
    assert clinic.revision == 1 and pat.revision == 1 and not AuditEvent.objects.exists()


def test_an_edit_that_changes_nothing_is_refused(client, make_user, clinic):
    client.force_login(make_user("manager", "account_manager"))
    refused = client.post(reverse("clients:client-edit", args=[clinic.pk]), edit_data(clinic))
    assert refused.status_code == 200 and b"Change at least one detail" in refused.content
    clinic.refresh_from_db()
    assert clinic.revision == 1


def test_the_account_code_must_be_unique(client, make_user, clinic, water):
    client.force_login(make_user("manager", "account_manager"))
    refused = client.post(
        reverse("clients:client-create"), {"name": "Another Clinic", "account_number": "EXCLIN"}
    )
    assert refused.status_code == 200
    assert b"already uses this account code" in refused.content
    assert b"Another Clinic" in refused.content  # input kept
    assert Client.objects.count() == 2
    refused = client.post(
        reverse("clients:client-edit", args=[water.pk]), edit_data(water, account_number="EXCLIN")
    )
    assert refused.status_code == 200 and b"already uses this account code" in refused.content
    water.refresh_from_db()
    assert water.account_number == "EXWATER" and water.revision == 1


def test_a_contact_needs_an_email_address_or_a_phone_number(client, make_user, clinic, pat):
    client.force_login(make_user("manager", "account_manager"))
    add = reverse("clients:contact-create", args=[clinic.pk])
    refused = client.post(add, {"name": "Sam Example", "role": "Billing"})
    assert refused.status_code == 200
    assert b"Give an email address or a phone number" in refused.content
    assert b"Sam Example" in refused.content
    assert not ClientContact.objects.filter(name="Sam Example").exists()
    assert client.post(add, {"name": "Sam Example", "phone": "555-0101"}).status_code == 302
    edit = reverse("clients:contact-edit", args=[clinic.pk, pat.pk])
    refused = client.post(edit, contact_data(pat, email="", phone=""))
    assert refused.status_code == 200
    pat.refresh_from_db()
    assert pat.email == "pat@clinic.example.invalid" and pat.revision == 1


def test_a_contact_of_another_organization_is_not_found_here(client, make_user, clinic, water, pat):
    client.force_login(make_user("manager", "account_manager"))
    wrong = reverse("clients:contact-edit", args=[water.pk, pat.pk])
    assert client.get(wrong).status_code == 404
    assert client.post(wrong, contact_data(pat, phone="555-0150")).status_code == 404
    assert client.get(reverse("clients:client-detail", args=[999999])).status_code == 404
    pat.refresh_from_db()
    assert pat.revision == 1


def test_an_inactive_organization_is_marked_and_stays_readable(client, make_user, clinic, pat):
    Client.objects.filter(pk=clinic.pk).update(is_active=False)
    client.force_login(make_user("reader", "technical"))
    detail = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    assert detail.status_code == 200
    assert b"This organization is inactive" in detail.content and b"Pat Example" in detail.content


def test_the_record_page_lists_portal_accounts_of_that_organization_only(
    client, make_user, clinic, water
):
    make_user("clinic-portal", client=clinic)
    make_user("water-portal", client=water)
    client.force_login(make_user("reader", "supervisor"))
    page = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    assert [u.username for u in page.context["portal_users"]] == ["clinic-portal"]
    assert b"clinic-portal@example.invalid" in page.content
    assert b"water-portal" not in page.content


@pytest.mark.parametrize(
    "kind, is_superuser, shown",
    [
        ("account_manager", False, False),
        ("admin", False, False),
        ("admin", True, True),
        ("supervisor", True, True),
    ],
)
def test_add_portal_account_is_offered_to_superusers_only(
    client, make_user, clinic, kind, is_superuser, shown
):
    user = make_user(f"viewer-{kind}-{is_superuser}", kind, is_superuser=is_superuser)
    client.force_login(user)
    page = client.get(reverse("clients:client-detail", args=[clinic.pk]))
    link = f"/admin/accounts/user/add/?client={clinic.pk}"
    assert page.status_code == 200
    assert (f'href="{link}"' in page.content.decode()) == shown
    assert page.context["can_invite"] == shown
    assert (b">Add portal account</a>" in page.content) == shown


def test_the_add_portal_account_link_opens_the_admin_form_with_the_organization_chosen(
    client, make_user, clinic
):
    owner = make_user("owner", "admin", is_staff=True, is_superuser=True)
    client.force_login(owner)
    link = f"/admin/accounts/user/add/?client={clinic.pk}"
    assert link == reverse("admin:accounts_user_add") + f"?client={clinic.pk}"
    page = client.get(link)
    assert page.status_code == 200
    assert str(page.context["adminform"].form["client"].value()) == str(clinic.pk)


class TestList:
    @pytest.fixture(autouse=True)
    def organizations(self, db):
        a = Client.objects.create(
            name="Alpha Clinic",
            account_number="ALPHA",
            city="Albany",
            state="NY",
            email="alpha@example.invalid",
        )
        ClientContact.objects.create(client=a, name="Pat Example", phone="555-0100")
        b = Client.objects.create(
            name="Beta Water", account_number="BETA", city="Boston", state="MA"
        )
        ClientContact.objects.create(
            client=b, name="Former Person", phone="555-0101", is_active=False
        )
        Client.objects.create(name="Gamma Works", account_number="GAMMA", state="NY")
        Client.objects.create(
            name="Delta Closed", account_number="DELTA", state="NY", is_active=False
        )

    def names(self, client, **params):
        response = client.get(reverse("clients:client-list"), params)
        assert response.status_code == 200
        return {row.name.split()[0] for row in response.context["page_obj"]}

    @pytest.fixture
    def signed_in(self, client, make_user):
        client.force_login(make_user("reader", "technical"))
        return client

    def test_unfiltered_lists_every_organization_by_name(self, signed_in):
        response = signed_in.get(reverse("clients:client-list"))
        assert [c.name for c in response.context["page_obj"]] == [
            "Alpha Clinic",
            "Beta Water",
            "Delta Closed",
            "Gamma Works",
        ]

    @pytest.mark.parametrize(
        "params, expected",
        [
            ({"q": "alpha"}, {"Alpha"}),
            ({"q": "BETA"}, {"Beta"}),  # case-insensitive, matches the account code too
            ({"q": "boston"}, {"Beta"}),  # city
            ({"q": "alpha@example"}, {"Alpha"}),  # email
            ({"q": "nothing matches"}, set()),
            ({"active": "yes"}, {"Alpha", "Beta", "Gamma"}),
            ({"active": "no"}, {"Delta"}),
            ({"active": "all"}, {"Alpha", "Beta", "Gamma", "Delta"}),
            ({"state": "NY"}, {"Alpha", "Gamma", "Delta"}),
            ({"state": "NY", "active": "yes"}, {"Alpha", "Gamma"}),
            ({"contacts": "none"}, {"Beta", "Gamma", "Delta"}),  # an inactive contact is not one
            ({"contacts": "none", "active": "yes"}, {"Beta", "Gamma"}),
            ({"contacts": "none", "active": "yes", "state": "NY"}, {"Gamma"}),
            ({"contacts": "none", "q": "gamma"}, {"Gamma"}),
        ],
    )
    def test_filters(self, signed_in, params, expected):
        assert self.names(signed_in, **params) == expected

    @pytest.mark.parametrize(
        "params",
        [{"state": "ZZ"}, {"active": "maybe"}, {"contacts": "some"}, {"q": "x" * 201}],
    )
    def test_an_invalid_filter_shows_no_rows_not_all_rows(self, signed_in, params):
        response = signed_in.get(reverse("clients:client-list"), params)
        assert response.status_code == 200
        assert response.context["filter_form"].errors
        assert list(response.context["page_obj"]) == []
        assert b"No organizations are hidden by an invalid filter" in response.content
        assert b"Alpha Clinic" not in response.content

    def test_sort_and_page_size_are_validated(self, signed_in):
        url = reverse("clients:client-list")
        response = signed_in.get(url, {"sort": "-name"})
        assert [c.name for c in response.context["page_obj"]][0] == "Gamma Works"
        unknown = signed_in.get(url, {"sort": "notes"})  # not a column: the default order
        assert [c.name for c in unknown.context["page_obj"]][0] == "Alpha Clinic"
        assert signed_in.get(url, {"per_page": "9"}).status_code == 400
        assert signed_in.get(url, {"per_page": "50"}).context["page_obj"].paginator.per_page == 50
