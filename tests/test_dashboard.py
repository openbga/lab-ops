import pytest
from django.urls import reverse

from accounts.roles import ROLES
from clients.models import Client, ClientContact

pytestmark = pytest.mark.django_db

QUEUE = "Active organizations without an active contact"
CLIENT_MANAGERS = ("account_manager", "client_services", "admin")


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def labels(response, area="clients"):
    """Queue labels of one dashboard area (the request desk adds its own area, tested in desk)."""
    return [queue["label"] for queue in response.context["queues"] if queue["area"] == area]


def queue(response, label=QUEUE):
    return next(item for item in response.context["queues"] if item["label"] == label)


@pytest.fixture
def organizations(db):
    """Two organizations waiting for a contact, one complete, one inactive, one only-inactive."""
    bare = Client.objects.create(name="Example Bare Clinic", account_number="BARE")
    Client.objects.create(name="Example Second Clinic")
    done = Client.objects.create(name="Example Complete Clinic")
    ClientContact.objects.create(client=done, name="Pat Example", email="pat@example.invalid")
    Client.objects.create(name="Example Closed Clinic", is_active=False)
    former = Client.objects.create(name="Example Former Contact Clinic")
    ClientContact.objects.create(
        client=former, name="Former Contact", email="former@example.invalid", is_active=False
    )
    return bare


def test_queue_is_shown_only_to_client_manager_roles(client, staff, organizations):
    for role in ROLES:
        client.force_login(staff(f"scope-{role}", role))
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200
        expected = [QUEUE] if role in CLIENT_MANAGERS else []
        assert labels(response) == expected, role
        assert bool(response.context["can_manage_clients"]) == (role in CLIENT_MANAGERS)
        assert response.context["recent_clients"]  # every staff role sees the recent panel


def test_count_matches_the_list_it_links_to(client, staff, organizations):
    client.force_login(staff("dash-manager", "account_manager"))
    response = client.get(reverse("dashboard"))
    item = queue(response)
    # Two without any contact, one whose only contact is inactive; inactive organizations out.
    assert item["count"] == 3
    assert b"3 waiting" not in response.content  # organization and ticket counts stay separate
    assert item["url"] == f"{reverse('clients:client-list')}?active=yes&contacts=none"
    assert f'href="{item["url"].replace("&", "&amp;")}"'.encode() in response.content
    target = client.get(item["url"])
    assert target.status_code == 200
    assert target.context["page_obj"].paginator.count == item["count"]
    assert {c.name for c in target.context["page_obj"]} == {
        "Example Bare Clinic",
        "Example Second Clinic",
        "Example Former Contact Clinic",
    }
    assert item["tone"] == "warning" and item["area"] == "clients"
    assert item["oldest_at"] is None and item["status"] == "Incomplete"


def test_all_clear_and_the_queue_still_shows_at_zero(client, staff, db):
    done = Client.objects.create(name="Example Complete Clinic")
    ClientContact.objects.create(client=done, name="Pat Example", email="pat@example.invalid")
    client.force_login(staff("dash-empty", "client_services"))
    response = client.get(reverse("dashboard"))
    assert response.context["queues_clear"] is True
    assert b"Nothing is waiting for you right now." in response.content
    assert queue(response)["count"] == 0
    assert b'aria-label="Service desk queues"' in response.content
    for item in response.context["queues"]:
        assert f'href="{item["url"].replace("&", "&amp;")}"'.encode() in response.content


def test_staff_without_client_queues_still_get_the_dashboard(client, staff, db):
    client.force_login(staff("dash-technical", "technical"))
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert labels(response) == []
    families = {line["family"] for line in response.context["queue_groups"]}
    assert families == {"desk"}  # the request desk's queues, for every staff role


def test_recent_organizations_panel_shows_the_newest_first_and_links_to_each(client, staff, db):
    for number in range(10):
        Client.objects.create(name=f"Example Clinic {number:02}", account_number=f"EX{number:02}")
    newest = Client.objects.get(name="Example Clinic 09")
    client.force_login(staff("dash-recent", "technical"))
    response = client.get(reverse("dashboard"))
    recent = list(response.context["recent_clients"])
    assert len(recent) == 8 and recent[0] == newest
    assert [c.name for c in recent] == [f"Example Clinic {n:02}" for n in range(9, 1, -1)]
    html = response.content.decode()
    assert "Recently added organizations" in html
    assert f'href="{reverse("clients:client-detail", args=[newest.pk])}"' in html
    assert "Example Clinic 01" not in html and "Example Clinic 00" not in html
    assert f'href="{reverse("clients:client-list")}"' in html  # All client organizations
    create = f'href="{reverse("clients:client-create")}"'.encode()
    assert create not in response.content  # a technical account cannot add one


def test_recent_panel_offers_creation_to_managers_and_an_empty_state(client, staff, db):
    client.force_login(staff("dash-new", "account_manager"))
    response = client.get(reverse("dashboard"))
    assert not response.context["recent_clients"]
    assert b"No client organizations yet" in response.content
    assert f'href="{reverse("clients:client-create")}"'.encode() in response.content


def test_portal_accounts_are_redirected_to_their_home(client, client_org, portal_user):
    client.force_login(portal_user(client_org))
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302 and response.url == reverse("portal:home")


def test_anonymous_visitors_are_sent_to_sign_in(client, db):
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302 and reverse("account_login") in response.url


def test_dashboard_query_count_is_bounded_with_more_rows_than_one_page(
    client, staff, django_assert_max_num_queries
):
    Client.objects.bulk_create(Client(name=f"Example Clinic {n:03}") for n in range(60))
    manager = staff("dash-bound", "account_manager")
    client.force_login(manager)
    client.get(reverse("dashboard"))  # warm content-type/permission caches
    with django_assert_max_num_queries(25):
        response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert queue(response)["count"] == 60  # one aggregate counts every row, not just a page
    assert len(response.context["recent_clients"]) == 8
