"""Remote select lookups (core/lookups.py): every read is authorized on the server."""

import pytest
from django import forms
from django.urls import reverse

from accounts.models import User
from clients.models import Client, ClientContact
from core.lookups import LOOKUPS, RemoteSelect


@pytest.fixture
def clients(db):
    active = Client.objects.create(name="Alpha Water Utility", city="Exampleville")
    inactive = Client.objects.create(name="Beta Closed District", is_active=False)
    other = Client.objects.create(name="Gamma Sewer District")
    return active, inactive, other


def lookup(client, name, **params):
    return client.get(reverse("lookup", args=[name]), params)


def values(response):
    return [row["text"] for row in response.json()["results"]]


def portal_user(client_record):
    return User.objects.create_user(
        "portal", "portal@example.invalid", "test-password-729!", client=client_record
    )


def test_staff_search_and_inactive_records(client, staff, clients):
    client.force_login(staff("technical1", "technical"))
    assert values(lookup(client, "clients")) == [
        "Alpha Water Utility",
        "Beta Closed District",
        "Gamma Sewer District",
    ]
    assert values(lookup(client, "active-clients")) == [
        "Alpha Water Utility",
        "Gamma Sewer District",
    ]
    # Every word must match a search field; case does not matter.
    assert values(lookup(client, "clients", q="district gam")) == ["Gamma Sewer District"]
    assert values(lookup(client, "clients", q="exampleville")) == ["Alpha Water Utility"]
    assert values(lookup(client, "clients", q="nothing")) == []


def test_pagination(client, staff, db):
    Client.objects.bulk_create(Client(name=f"Client {n:02}") for n in range(25))
    client.force_login(staff("technical1", "technical"))
    first = lookup(client, "clients").json()
    assert len(first["results"]) == LOOKUPS["clients"].page_size == 20 and first["more"]
    second = lookup(client, "clients", page=2).json()
    assert [r["text"] for r in second["results"]] == [f"Client {n}" for n in range(20, 25)]
    assert not second["more"]
    assert lookup(client, "clients", page=9).json() == {"results": [], "more": False}


def test_refused_without_staff_role(client, staff, clients):
    assert lookup(client, "clients").status_code == 302  # anonymous: sign in first
    nobody = User.objects.create_user("norole", "norole@example.invalid", "test-password-729!")
    client.force_login(nobody)
    assert lookup(client, "clients").status_code == 403
    technician = staff("former", "technical")
    technician.is_active = False
    technician.save()
    client.force_login(technician)
    assert lookup(client, "clients").status_code in (302, 403)


def test_client_portal_sees_only_its_own_active_contacts(client, staff, clients):
    active, _, other = clients
    ClientContact.objects.create(client=active, name="Pat Example", email="pat@example.invalid")
    ClientContact.objects.create(
        client=active, name="Former Contact", email="former@example.invalid", is_active=False
    )
    ClientContact.objects.create(client=other, name="Sam Other", email="sam@example.invalid")
    client.force_login(portal_user(active))
    assert values(lookup(client, "portal-contacts")) == ["Pat Example"]
    # Staff lookups refuse portal users, and the portal lookup refuses staff.
    assert lookup(client, "clients").status_code == 403
    assert lookup(client, "staff").status_code == 403
    client.force_login(staff("manager1", "account_manager"))
    assert lookup(client, "portal-contacts").status_code == 403
    assert values(lookup(client, "clients", q="sewer")) == ["Gamma Sewer District"]


def test_staff_lookup_lists_active_staff_accounts_only(client, staff, clients):
    technician = staff("technical1", "technical")
    inactive = staff("inactive1", "technical")
    inactive.is_active = False
    inactive.save()
    portal_user(clients[0])
    client.force_login(technician)
    assert values(lookup(client, "staff")) == ["technical1"]


def test_unknown_lookup_and_method(client, staff, db):
    client.force_login(staff("technical2", "technical"))
    assert lookup(client, "secrets").status_code == 404
    assert client.post(reverse("lookup", args=["clients"])).status_code == 405


class ClientChoiceForm(forms.Form):
    client = forms.ModelChoiceField(
        queryset=Client.objects.all(), required=False, widget=RemoteSelect("active-clients")
    )


def test_remote_select_renders_only_the_selected_option(clients):
    active, inactive, _ = clients
    html = " ".join(str(ClientChoiceForm(initial={"client": inactive.pk})["client"]).split())
    assert f'data-vr-lookup="{reverse("lookup", args=["active-clients"])}"' in html
    # The edit form keeps its current value even though the lookup no longer offers it.
    assert f'<option value="{inactive.pk}" selected >Beta Closed District</option>' in html
    assert active.name not in html
    assert html.count("<option") == 2  # the empty choice and the selection


def test_remote_select_validates_with_the_field_queryset(clients):
    active, *_ = clients
    form = ClientChoiceForm({"client": str(active.pk)})
    assert form.is_valid() and form.cleaned_data["client"] == active
    assert not ClientChoiceForm({"client": "999999"}).is_valid()


def test_unknown_lookup_name_fails_at_import():
    with pytest.raises(ValueError):
        RemoteSelect("no-such-lookup")
