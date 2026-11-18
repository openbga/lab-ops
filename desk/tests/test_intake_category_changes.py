"""Changes to category routing and protection while an intake page is open."""

import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from clients.models import PortalAccess
from desk import services
from desk.models import ServiceRequest
from desk.selectors import staff_requests
from taxonomy import terms
from taxonomy.models import RequestCategory
from taxonomy.services import update_category

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

RESTRICTED_NOTE = b"only its team's members and supervisors see these tickets"


@pytest.fixture
def admin(make_user):
    return make_user("setup_admin", "admin")


def setup_change(admin, category, **values):
    category.refresh_from_db()
    return update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=category.revision,
        reason="Synthetic setup change",
        **values,
    )


def drawn_terms(page):
    match = re.search(r'name="category_terms" value="([^"]*)"', page.content.decode())
    assert match, "the page carries no category terms"
    return match.group(1)


def guidance(client, category):
    return client.get(reverse("desk:category-guidance"), {"category": category.pk})


def log(client, category, carried, **extra):
    data = {
        "category": category.pk,
        "channel": "phone",
        "subject": "Synthetic call",
        "first_entry_kind": "note",
        "body": "Caller asks about order ORD-TEST-000006.",
    }
    if carried is not None:
        data["category_terms"] = carried
    data.update(extra)
    return client.post(reverse("desk:request-create"), data)


def test_cleared_restriction_refuses_a_page_that_promised_it(
    client, agent, admin, privacy_category
):
    """The page said the request would be restricted;
    the flag was cleared before the save; nothing is written, the page says why and shows the
    category as it is now, and submitting that page again is accepted."""
    client.force_login(agent)
    page = guidance(client, privacy_category)
    assert RESTRICTED_NOTE in page.content
    carried = drawn_terms(page)
    setup_change(admin, privacy_category, is_restricted=False)

    refused = log(client, privacy_category, carried)
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert escape(terms.CHANGED) in refused.content.decode()
    assert RESTRICTED_NOTE not in refused.content
    assert "Caller asks about order ORD-TEST-000006." in refused.content.decode()  # kept

    accepted = log(client, privacy_category, drawn_terms(refused))
    assert accepted.status_code == 302
    created = ServiceRequest.objects.get()
    assert not created.category.is_restricted
    assert staff_requests(agent).filter(pk=created.pk).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"is_restricted": True},
        {"is_complaint": True},
        {"default_team": None},
        {"default_priority": "low"},
    ],
    ids=["restricted", "complaint", "team", "priority"],
)
def test_any_protection_or_routing_change_refuses(client, agent, admin, sample_category, change):
    client.force_login(agent)
    carried = drawn_terms(guidance(client, sample_category))
    setup_change(admin, sample_category, **change)
    assert log(client, sample_category, carried).status_code == 200
    assert not ServiceRequest.objects.exists()


def test_wording_changes_do_not_refuse(client, agent, admin, privacy_category):
    client.force_login(agent)
    carried = drawn_terms(guidance(client, privacy_category))
    setup_change(admin, privacy_category, name="Privacy or data request", guidance="Ask why.")
    assert log(client, privacy_category, carried).status_code == 302
    assert ServiceRequest.objects.get().category.is_restricted


def test_a_post_without_the_drawn_terms_is_refused(client, agent, category):
    """Nothing to compare counts as changed: a page drawn before this release, or a hand-made
    post, cannot write past a promise it did not carry."""
    client.force_login(agent)
    for carried in (None, "", f"{category.pk}:0000", "garbage"):
        response = log(client, category, carried)
        assert response.status_code == 200 and not ServiceRequest.objects.exists()
        assert escape(terms.CHANGED) in response.content.decode()


def test_terms_of_another_category_do_not_stand_in(client, agent, category, sample_category):
    client.force_login(agent)
    carried = drawn_terms(guidance(client, sample_category))
    assert log(client, category, carried).status_code == 200
    assert not ServiceRequest.objects.exists()


def test_the_check_reads_the_category_under_its_row_lock(agent, category):
    """update_category locks the row FOR UPDATE, so it serializes with this read: a setup change
    either commits first (and the comparison sees it) or waits for the creation."""
    with CaptureQueriesContext(connection) as statements:
        services.create_request(
            actor=agent,
            category=category,
            subject="Locked",
            first_entry_kind="note",
            first_entry_body="Text",
            channel="phone",
            drawn_terms=terms.drawn([category]),
        )
    reads = [
        q["sql"].upper()
        for q in statements
        if q["sql"].upper().startswith("SELECT")
        and f'FROM "{RequestCategory._meta.db_table}"'.upper() in q["sql"].upper()
    ]
    assert reads and "FOR NO KEY UPDATE" in reads[0]


# The portal's generic raise page


@pytest.fixture
def portal_client(client, clinic_portal, clinic):
    PortalAccess.objects.create(user=clinic_portal, client=clinic, whole_organization=True)
    client.login(username=clinic_portal.username, password=PASSWORD)
    return client


def raise_request(client, category, carried):
    return client.post(
        reverse("portal:request-create"),
        {
            "category": category.pk,
            "category_terms": carried,
            "subject": "Report not received",
            "description": "Synthetic portal request.",
        },
    )


def test_portal_page_refuses_a_topic_changed_since_it_was_drawn(
    portal_client, admin, category, sample_category
):
    page = portal_client.get(reverse("portal:request-create"))
    carried = drawn_terms(page)
    setup_change(admin, category, is_restricted=True)

    refused = raise_request(portal_client, category, carried)
    assert refused.status_code == 200 and not ServiceRequest.objects.exists()
    assert escape(terms.CHANGED_FOR_CLIENT) in refused.content.decode()

    # Another topic on the same page is unchanged and goes through; the redrawn page carries the
    # changed topic's current settings.
    assert raise_request(portal_client, sample_category, carried).status_code == 302
    assert raise_request(portal_client, category, drawn_terms(refused)).status_code == 302
    assert ServiceRequest.objects.count() == 2


def test_portal_terms_reveal_no_settings(portal_client, sample_category):
    """The value is a keyed digest per topic: no team, priority or visibility in the page."""
    carried = drawn_terms(portal_client.get(reverse("portal:request-create")))
    entries = dict(entry.split(":") for entry in carried.split())
    assert str(sample_category.pk) in entries
    assert all(re.fullmatch(r"[0-9a-f]{32}", digest) for digest in entries.values())
