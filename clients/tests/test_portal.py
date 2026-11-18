"""The client portal: an account sees its own organization and nothing of any other."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from clients.models import Client, ClientContact
from clients.portal import client_scope
from core import lab_profile
from core.models import Lab

from .conftest import READERS

pytestmark = pytest.mark.django_db


@pytest.fixture
def lab():
    return Lab.objects.create(name="Example Reference Laboratory", phone="555-0142")


@pytest.fixture
def contacts(clinic, water):
    return {
        "active": ClientContact.objects.create(
            client=clinic, name="Pat Example", role="Billing", email="pat@clinic.example.invalid"
        ),
        "left": ClientContact.objects.create(
            client=clinic, name="Former Person", phone="555-0199", is_active=False
        ),
        "other": ClientContact.objects.create(
            client=water, name="Wendy Waters", email="wendy@water.example.invalid"
        ),
    }


@pytest.fixture
def portal(client, make_user, clinic):
    client.force_login(make_user("portal-clinic", client=clinic))
    return client


def logo_file(size=(120, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return SimpleUploadedFile("logo.png", buffer.getvalue())


def test_a_portal_account_sees_its_own_organization_and_active_contacts(
    portal, lab, clinic, water, contacts
):
    response = portal.get(reverse("portal:home"))
    assert response.status_code == 200
    page = response.content.decode()
    assert response.context["client"] == clinic
    assert [c.name for c in response.context["contacts"]] == ["Pat Example"]
    assert "Example Clinic" in page and 'href="mailto:frontdesk@clinic.example.invalid"' in page
    assert "Pat Example" in page and 'href="mailto:pat@clinic.example.invalid"' in page
    assert 'href="tel:555-0100"' in page and 'href="tel:555-0142"' in page
    assert "Example Reference Laboratory" in page
    assert "Former Person" not in page and "555-0199" not in page  # inactive contact
    assert "Internal: pays late." not in page  # internal notes stay internal


def test_a_portal_account_never_sees_another_organization(portal, lab, clinic, water, contacts):
    for url in (reverse("portal:home"), reverse("dashboard")):
        page = portal.get(url, follow=True).content.decode()
        assert "Example Water Authority" not in page and "EXWATER" not in page
        assert "Wendy Waters" not in page and "wendy@water.example.invalid" not in page
        assert "Sampletown" not in page


def test_the_other_organizations_account_sees_only_its_own(client, make_user, lab, water, contacts):
    client.force_login(make_user("portal-water", client=water))
    response = client.get(reverse("portal:home"))
    page = response.content.decode()
    assert response.context["client"] == water
    assert "Wendy Waters" in page
    assert "Example Clinic" not in page and "Pat Example" not in page
    assert "pat@clinic.example.invalid" not in page


def test_client_scope_never_broadens(clinic, water, contacts, make_user):
    user = make_user("scoped", client=clinic)
    scoped = client_scope(ClientContact.objects.all(), user)
    assert {c.name for c in scoped} == {"Pat Example", "Former Person"}
    assert list(client_scope(Client.objects.all(), user, field="pk")) == [clinic]


def test_the_portal_home_is_where_the_dashboard_sends_a_portal_account(portal):
    response = portal.get(reverse("dashboard"))
    assert response.status_code == 302 and response.url == reverse("portal:home")


@pytest.mark.parametrize("role", READERS)
def test_staff_accounts_are_refused_the_portal(client, make_user, clinic, role):
    client.force_login(make_user(f"staff-{role}", role))
    assert client.get(reverse("portal:home")).status_code == 403


def test_an_account_with_neither_client_nor_role_and_visitors_are_refused(
    client, make_user, clinic
):
    client.force_login(make_user("owner", is_staff=True, is_superuser=True))
    assert client.get(reverse("portal:home")).status_code == 403
    client.logout()
    response = client.get(reverse("portal:home"))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]


def test_an_inactive_portal_account_gets_nothing(client, make_user, clinic):
    user = make_user("portal-gone", client=clinic, is_active=False)
    client.force_login(user)  # an inactive account cannot hold a session
    response = client.get(reverse("portal:home"))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]


def test_a_portal_account_has_no_access_to_the_staff_workspace(portal, clinic, contacts):
    for url in (
        reverse("clients:client-list"),
        reverse("clients:client-detail", args=[clinic.pk]),
        reverse("whats-new"),
        reverse("record-retention"),
        reverse("lab-profile"),
    ):
        assert portal.get(url).status_code == 403, url
    assert portal.get(reverse("lookup", args=["clients"])).status_code == 403
    assert portal.get(reverse("lookup", args=["portal-contacts"])).status_code == 200


def test_the_portal_contacts_lookup_is_scoped_like_the_page(portal, clinic, contacts):
    results = portal.get(reverse("lookup", args=["portal-contacts"])).json()["results"]
    assert [row["text"] for row in results] == ["Pat Example"]


def test_the_lab_logo_shows_on_the_portal_and_only_the_current_one_can_be_fetched(
    portal, lab, make_user, clinic
):
    admin = make_user("logo-admin", "admin")
    lab_profile.set_logo(actor=admin, uploaded=logo_file())
    lab.refresh_from_db()
    first = lab_profile.logo_facts(lab)["sha256"]
    page = portal.get(reverse("portal:home")).content.decode()
    assert f'src="{reverse("lab-logo-image", args=[first])}"' in page
    assert portal.get(reverse("lab-logo-image", args=[first])).status_code == 200

    lab_profile.set_logo(actor=admin, uploaded=logo_file(size=(90, 90)))
    lab.refresh_from_db()
    current = lab_profile.logo_facts(lab)["sha256"]
    assert current != first
    assert portal.get(reverse("lab-logo-image", args=[first])).status_code == 404
    assert portal.get(reverse("lab-logo-image", args=[current])).status_code == 200
    assert portal.get(reverse("lab-logo-image", args=["0" * 64])).status_code == 404
    lab_profile.remove_logo(actor=admin)
    assert portal.get(reverse("lab-logo-image", args=[current])).status_code == 404


def test_the_logo_url_is_refused_to_visitors(client, lab, make_user):
    lab_profile.set_logo(actor=make_user("logo-admin", "admin"), uploaded=logo_file())
    lab.refresh_from_db()
    url = reverse("lab-logo-image", args=[lab_profile.logo_facts(lab)["sha256"]])
    response = client.get(url)
    assert response.status_code == 302 and reverse("account_login") in response["Location"]
