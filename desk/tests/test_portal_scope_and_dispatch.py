"""Portal access boundaries, historical authorship and notification recipients."""

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.forms import MultiWidget
from django.urls import reverse

from accounts.models import User
from clients.forms import PortalAccessForm
from clients.models import ClientContact, ClientSite, PortalAccess
from clients.portal import portal_contacts
from clients.services import set_portal_access
from desk import notifications
from desk.models import EntryKind, ServiceRequest
from desk.portal_forms import RaiseRequestForm
from desk.portal_scope import check_portal_site, portal_requests
from desk.portal_views import _conversation
from desk.services import create_request
from taxonomy.models import RequestCategory

from .test_portal import raise_staff

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(make_user):
    return make_user("review_owner", "admin", is_staff=True, is_superuser=True)


@pytest.fixture
def manager(make_user):
    return make_user("review_manager", "account_manager")


def admin_post(http, account, **changes):
    """The account administration's change form, posted with its current values changed."""
    url = reverse("admin:accounts_user_change", args=[account.pk])
    form = http.get(url).context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        value = form[name].value()
        if field.disabled or value is None or value is False:
            continue
        if isinstance(field.widget, MultiWidget):
            for index, part in enumerate(field.widget.decompress(value)):
                data[f"{name}_{index}"] = part
        elif isinstance(value, (list, tuple)):
            data[name] = [getattr(v, "pk", v) for v in value]
        else:
            data[name] = "on" if value is True else getattr(value, "pk", value)
    data.update(changes)
    return http.post(url, data)


# a whole-organization grant does not travel with a moved account


def test_scope_attached_to_granting_org(
    client, owner, manager, agent, category, clinic_portal, clinic_contact, water
):
    raise_staff(agent, category, water)
    set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        contact=clinic_contact,
        whole_organization=True,
    )
    client.force_login(owner)
    assert admin_post(client, clinic_portal, client=water.pk).status_code == 302
    clinic_portal.refresh_from_db()
    grants = PortalAccess.objects.filter(user=clinic_portal, whole_organization=True)
    assert (grants.exists(), portal_requests(clinic_portal).count()) == (False, 0)


# No conversion between staff and portal accounts


@pytest.mark.parametrize("direction", ["staff-to-portal", "portal-to-staff"])
def test_account_kind_is_fixed_on_every_path(
    client, owner, agent, clinic_portal, clinic, direction
):
    account, value = (agent, clinic) if direction == "staff-to-portal" else (clinic_portal, None)
    client.force_login(owner)
    response = admin_post(client, account, client=value.pk if value else "")
    assert response.status_code == 200
    assert "Create a new account" in str(response.context["adminform"].form.errors["client"])
    account.client = value
    account.changed_by = owner
    with pytest.raises(ValidationError):
        account.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=account.pk).update(client=value)
    stored = User.objects.get(pk=account.pk)
    assert (stored.client_id is None) == (direction == "staff-to-portal")


# the raiser exception needs portal provenance


def test_raiser_exception_has_portal_provenance(clinic_portal, agent, group, clinic):
    restricted = RequestCategory.objects.create(
        key="limited", name="Limited", parent=group, is_restricted=True
    )
    record = raise_staff(agent, restricted, clinic)
    assert record.channel == "phone"
    # Even attributed to the portal account, a request it did not raise in the portal is not its
    # own: neither the restricted exception nor the "raised" scope applies.
    ServiceRequest.objects.filter(pk=record.pk).update(created_by=clinic_portal)
    assert not portal_requests(clinic_portal).exists()
    assert notifications.client_recipients(ServiceRequest.objects.get(pk=record.pk)) == []
    # Raised in the portal, it is.
    own = create_request(
        actor=clinic_portal,
        category=RequestCategory.objects.create(
            key="limited-portal",
            name="Limited, portal",
            parent=group,
            is_restricted=True,
            client_may_raise=True,
        ),
        subject="Mine",
        first_entry_kind=EntryKind.REPLY,
        first_entry_body="Please.",
        channel="portal",
    )
    assert list(portal_requests(clinic_portal)) == [own]


# the contact directory follows site scope


@pytest.mark.parametrize("surface", ["home", "lookup"])
def test_listed_site_contact_directory(
    client, manager, clinic_portal, clinic_site, clinic_contact, clinic, surface
):
    other_site = ClientSite.objects.create(client=clinic, name="Other office")
    clinic_contact.sites.add(other_site)
    serving = ClientContact.objects.create(client=clinic, name="Serving Example", phone="555-0170")
    serving.sites.add(clinic_site)
    everyone = ClientContact.objects.create(client=clinic, name="Whole Example", phone="555-0171")
    set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        sites=[clinic_site],
    )
    client.force_login(clinic_portal)
    if surface == "home":
        response = client.get(reverse("portal:home"))
    else:
        response = client.get(reverse("lookup", args=["portal-contacts"]))
    assert response.status_code == 200
    body = response.content.decode()
    assert clinic_contact.name not in body
    assert serving.name in body and everyone.name in body


def test_whole_organization_and_no_grant_directories(
    manager, make_user, clinic, clinic_site, clinic_contact
):
    clinic_contact.sites.add(clinic_site)
    everyone = ClientContact.objects.create(client=clinic, name="Whole Example", phone="555")
    whole = make_user("whole_dir", client=clinic)
    set_portal_access(
        actor=manager, user_id=whole.pk, expected_client_id=whole.client_id, whole_organization=True
    )
    assert set(portal_contacts(whole)) == {clinic_contact, everyone}
    no_grant = make_user("no_grant_dir", client=clinic)
    assert list(portal_contacts(no_grant)) == [everyone]


# no access record, no site choice


def test_absent_access_has_no_site_choices(clinic_portal, clinic_site, category):
    field = RaiseRequestForm(user=clinic_portal).fields.get("site")
    offered = [] if field is None else list(field.queryset.values_list("pk", flat=True))
    assert offered == []
    with pytest.raises(ValidationError):
        check_portal_site(clinic_portal, clinic_site)
    with pytest.raises(ValidationError):
        create_request(
            actor=clinic_portal,
            category=category,
            subject="With a site",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Text",
            channel="portal",
            site=clinic_site,
        )


def test_emptied_access_has_no_site_choices(owner, manager, make_user, clinic, water, clinic_site):
    account = make_user("moved_back", client=clinic)
    set_portal_access(
        actor=manager, user_id=account.pk, expected_client_id=account.client_id, sites=[clinic_site]
    )
    for organization in (water, clinic):  # the move resets; moving back restores nothing
        account.client = organization
        account.changed_by = owner
        account.save()
    assert "site" not in RaiseRequestForm(user=account).fields


# editing an existing grant needs its revision


def test_access_edit_requires_version(manager, clinic_portal, clinic_site):
    access = set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        whole_organization=True,
    )
    form = PortalAccessForm(
        {"scope": "sites", "sites": [clinic_site.pk], "reason": "Limit to the main office"},
        account=clinic_portal,
        access=access,
    )
    assert not form.is_valid() and "expected_revision" in form.errors


def test_existing_access_service_requires_revision(manager, clinic_portal, clinic_site):
    grant = set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        whole_organization=True,
    )
    for revision in (None, grant.revision + 1):
        with pytest.raises(ValidationError):
            set_portal_access(
                actor=manager,
                user_id=clinic_portal.pk,
                expected_client_id=clinic_portal.client_id,
                sites=[clinic_site],
                expected_revision=revision,
                reason="Limit scope to the main office",
            )
    grant.refresh_from_db()
    assert grant.whole_organization and grant.revision == 1


def test_access_edit_of_another_organization_is_404(client, manager, clinic_portal, water):
    client.force_login(manager)
    url = reverse("clients:portal-access-edit", args=[water.pk, clinic_portal.pk])
    assert client.get(url, headers={"X-VR-Form": "1"}).status_code == 404
    response = client.post(
        url, {"scope": "organization", "expected_revision": 0}, headers={"X-VR-Form": "1"}
    )
    assert response.status_code == 404
    assert not PortalAccess.objects.filter(user=clinic_portal).exists()


# a laboratory reply keeps its signature


def test_staff_signature_is_historical(
    client, owner, manager, agent, category, clinic, clinic_portal, water
):
    set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        whole_organization=True,
    )
    record = raise_staff(agent, category, clinic)
    assert not _conversation(record)[0]["from_client"]
    client.force_login(owner)
    assert admin_post(client, agent, client=water.pk).status_code == 200  # refused
    agent.is_active = False  # a deactivated author changes nothing either
    agent.save()
    client.force_login(clinic_portal)
    html = client.get(reverse("portal:request-detail", args=[record.number])).content.decode()
    main = html[html.index("<main") : html.index("</main>")]  # without the page help's text
    assert "Sent by " not in main and agent.username not in html
    assert "Reply from the laboratory" in main


# recipients are checked again at dispatch


def test_revocation_before_notice_dispatch(
    django_capture_on_commit_callbacks,
    settings,
    manager,
    agent,
    category,
    clinic,
    clinic_contact,
    clinic_portal,
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        contact=clinic_contact,
        whole_organization=True,
    )
    with django_capture_on_commit_callbacks(execute=True):
        record = raise_staff(agent, category, clinic, contact=clinic_contact)
        clinic_portal.is_active = False
        clinic_portal.save(update_fields=["is_active"])
        assert not notifications.client_recipients(record)
    assert not mail.outbox


def test_narrowed_scope_before_notice_dispatch(
    django_capture_on_commit_callbacks,
    settings,
    manager,
    agent,
    category,
    clinic,
    clinic_contact,
    clinic_portal,
    clinic_site,
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    other = ClientSite.objects.create(client=clinic, name="Other office")
    set_portal_access(
        actor=manager,
        user_id=clinic_portal.pk,
        expected_client_id=clinic_portal.client_id,
        contact=clinic_contact,
        whole_organization=True,
    )
    with django_capture_on_commit_callbacks(execute=True):
        raise_staff(agent, category, clinic, clinic_site, contact=clinic_contact)
        access = PortalAccess.objects.get(user=clinic_portal)
        set_portal_access(
            actor=manager,
            user_id=clinic_portal.pk,
            expected_client_id=clinic_portal.client_id,
            contact=clinic_contact,
            sites=[other],
            expected_revision=access.revision,
            reason="Moved to the other office",
        )
    assert not mail.outbox


def test_multiline_laboratory_name_is_one_subject():
    context = notifications.notice_context(
        number="REQ-000001",
        event="reply",
        url="https://example.invalid/login/",
        laboratory="Example\r\nLaboratory",
    )
    subject, _ = notifications.render_notice(context)
    assert subject.splitlines() == ["REQ-000001: update from Example Laboratory"]
