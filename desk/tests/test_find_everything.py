"""Find (the top bar's lookup and the phone bar's Find) lists matching client organizations as
well as requests; it stays a staff page."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_find_lists_matching_organizations(client, agent, clinic, water):
    client.force_login(agent)
    response = client.get(reverse("desk:request-find"), {"q": "EXWATER"})
    assert list(response.context["organizations"]) == [water]
    assert reverse("clients:client-detail", args=[water.pk]) in response.content.decode()


def test_find_is_refused_to_portal_accounts(client, clinic_portal):
    client.force_login(clinic_portal)
    assert client.get(reverse("desk:request-find"), {"q": "Example"}).status_code == 403


def test_shell_find_points_at_find(client, agent):
    client.force_login(agent)
    page = client.get(reverse("dashboard")).content.decode()
    assert f'action="{reverse("desk:request-find")}"' in page  # the top bar's lookup
    assert f'href="{reverse("desk:request-find")}"' in page  # the phone bar's Find
