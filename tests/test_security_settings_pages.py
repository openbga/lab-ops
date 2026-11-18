"""Security settings and the contact form: only the admin role opens either, the page lists every
group of facts and both contacts, the form saves through core.lab_profile.save_contact and keeps
what was typed when it refuses, and the laboratory profile shows the contacts to every staff
role."""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from accounts.models import User
from accounts.roles import ROLES
from clients.models import Client
from core import lab_profile, security_settings
from core.models import AuditEvent, Lab, LabContact

pytestmark = pytest.mark.django_db

OPERATIONAL = [role for role in ROLES if role != "admin"]


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def lab():
    return Lab.objects.create(name="Example Reference Laboratory")


@pytest.fixture
def admin(staff):
    return staff("security-admin", "admin")


@pytest.fixture
def portal_account(db):
    return User.objects.create_user(
        "security-client",
        "security-client@example.invalid",
        "test-password-729!",
        client=Client.objects.create(name="Example Clinic"),
    )


def contact_data(**changes):
    data = {
        "name": "Example Privacy Office",
        "title": "Privacy official",
        "email": "privacy@example.invalid",
        "phone": "555-0144",
        "reason": "Named by the laboratory director.",
        "expected_revision": "0",
    }
    return data | changes


def record(admin, kind="security", **values):
    contact = next(row for row in lab_profile.contacts() if row.kind == kind)
    saved, _ = lab_profile.save_contact(
        actor=admin,
        kind=kind,
        values={"name": "Example Security Office", **values},
        expected_revision=contact.revision,
        reason="Synthetic reason for the test.",
    )
    return saved


def routes():
    page = reverse("security-settings")
    form = reverse("lab-contact-edit", args=["privacy"])
    return [page, form, form + "?from=security"]


# Authorization: every audience, by direct address, GET and POST.


def test_anonymous_visitors_are_sent_to_sign_in(client, lab):
    for url in routes():
        for response in (client.get(url), client.post(url, contact_data())):
            assert response.status_code == 302, url
            assert response["Location"].startswith(reverse("account_login")), url
    assert not LabContact.objects.exists()


@pytest.mark.parametrize("role", OPERATIONAL)
def test_operational_roles_are_refused(client, lab, staff, role):
    client.force_login(staff(f"security-{role}", role))
    for url in routes():
        assert client.get(url).status_code == 403, url
        assert client.post(url, contact_data()).status_code == 403, url
    assert not LabContact.objects.exists() and not AuditEvent.objects.exists()


def test_a_portal_account_is_refused_even_in_the_admin_group(client, lab, portal_account):
    portal_account.groups.add(Group.objects.get_or_create(name="admin")[0])
    portal_account.is_staff = portal_account.is_superuser = True
    portal_account.save()
    client.force_login(portal_account)
    for url in routes():
        assert client.get(url).status_code == 403, url
        assert client.post(url, contact_data()).status_code == 403, url
    assert not LabContact.objects.exists()


def test_a_superuser_without_a_role_is_refused(client, lab):
    owner = User.objects.create_user(
        "security-owner", "owner@example.invalid", is_staff=True, is_superuser=True
    )
    client.force_login(owner)
    for url in routes():
        assert client.get(url).status_code == 403, url
        assert client.post(url, contact_data()).status_code == 403, url
    assert not LabContact.objects.exists()


def test_an_inactive_admin_is_signed_out(client, lab, admin):
    client.force_login(admin)
    admin.is_active = False
    admin.save()
    for url in routes():
        for response in (client.get(url), client.post(url, contact_data())):
            assert response.status_code == 302, url
            assert response["Location"].startswith(reverse("account_login")), url
    assert not LabContact.objects.exists()


def test_an_unknown_contact_is_not_found(client, lab, admin, staff):
    url = reverse("lab-contact-edit", args=["billing"])
    client.force_login(admin)
    assert client.get(url).status_code == 404
    assert client.post(url, contact_data()).status_code == 404
    client.force_login(staff("security-unknown-tech", "technical"))
    assert client.get(url).status_code == 403  # the role is checked first


# The page.


def test_admin_sees_every_group_both_contacts_and_edit_links(client, lab, admin):
    record(admin, email="security@example.invalid")
    client.force_login(admin)
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    text = response.content.decode()
    groups = security_settings.in_force()
    assert [group.title for group in response.context["groups"]] == [g.title for g in groups]
    for group in groups:
        assert f'id="security-{group.key}-heading"' in text
        for fact in group.facts:
            assert fact.label.replace("'", "&#x27;") in text, fact.key
            assert fact.source_label.replace("&", "&amp;") in text
            if fact.setting:
                assert f"<code>{fact.setting}</code>" in text
    assert "Example Security Office" in text and "security@example.invalid" in text
    assert "Privacy contact" in text and "Not recorded" in text
    form = reverse("lab-contact-edit", args=["security"])
    assert f'href="{form}?from=security"' in text
    assert f'href="{reverse("lab-contact-edit", args=["privacy"])}?from=security"' in text


def test_page_reads_the_contacts_once(client, lab, admin):
    record(admin)
    client.force_login(admin)
    client.get(reverse("security-settings"))  # warm the session and caches
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        client.get(reverse("security-settings"))
    reads = [q["sql"] for q in queries if 'FROM "core_labcontact"' in q["sql"]]
    assert len(reads) == 1


def test_page_without_a_laboratory_offers_no_edit(client, admin):
    client.force_login(admin)
    response = client.get(reverse("security-settings"))
    assert response.status_code == 200
    assert reverse("lab-contact-edit", args=["security"]) not in response.content.decode()
    form = client.post(reverse("lab-contact-edit", args=["security"]), contact_data())
    assert form.status_code == 200 and b"No laboratory configured" in form.content
    assert not LabContact.objects.exists()


def test_history_shows_contact_changes_with_reason_newest_first(client, lab, admin):
    record(admin)
    record(admin, kind="privacy", name="Example Privacy Office")
    client.force_login(admin)
    response = client.get(reverse("security-settings"))
    events = response.context["history"]
    assert [event.detail for event in events] == ["Privacy contact", "Security contact"]
    text = response.content.decode()
    assert "Synthetic reason for the test." in text
    assert "Lab contact changed" in text


def test_history_holds_at_most_twenty_changes(client, lab, admin):
    for number in range(22):
        record(admin, name=f"Example Office {number}")
    client.force_login(admin)
    events = client.get(reverse("security-settings")).context["history"]
    assert len(events) == 20 and events[0].after["name"] == "Example Office 21"


# The contact form.


def test_form_saves_and_returns_to_the_profile(client, lab, admin):
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["privacy"])
    page = client.get(url)
    assert page.status_code == 200 and page.context["page_title"] == "Edit privacy contact"
    assert page.context["form"]["expected_revision"].value() == 0
    response = client.post(url, contact_data(), follow=True)
    assert response.redirect_chain == [(reverse("lab-profile"), 302)]
    assert "Privacy contact saved." in response.content.decode()
    contact = LabContact.objects.get(kind="privacy")
    assert (contact.name, contact.email, contact.revision) == (
        "Example Privacy Office",
        "privacy@example.invalid",
        1,
    )
    event = AuditEvent.objects.get(action="lab.contact_changed")
    assert event.actor == admin and event.reason == "Named by the laboratory director."


def test_form_opened_from_security_settings_returns_there(client, lab, admin):
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["security"]) + "?from=security"
    page = client.get(url)
    assert page.context["cancel_url"] == reverse("security-settings")
    assert page.context["page_title"] == "Edit security contact"
    response = client.post(url, contact_data(name="Example Security Office"))
    assert response.status_code == 302 and response["Location"] == reverse("security-settings")
    # Any other marker returns to the profile; no address is taken from the request.
    other = reverse("lab-contact-edit", args=["security"]) + "?from=https://example.invalid/"
    response = client.post(other, contact_data(expected_revision="1", phone="555-0199"))
    assert response.status_code == 302 and response["Location"] == reverse("lab-profile")


def test_form_shows_the_current_values_and_revision(client, lab, admin):
    record(admin, title="Security official")
    client.force_login(admin)
    form = client.get(reverse("lab-contact-edit", args=["security"])).context["form"]
    assert form["name"].value() == "Example Security Office"
    assert form["title"].value() == "Security official"
    assert form["expected_revision"].value() == 1
    assert form.fields["phone"].widget.input_type == "tel"
    assert form.fields["email"].widget.input_type == "email"


def test_a_stale_form_keeps_the_input_and_writes_nothing(client, lab, admin):
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["privacy"])
    assert client.post(url, contact_data()).status_code == 302
    before = AuditEvent.objects.count()
    response = client.post(
        url, contact_data(name="Typed Office Name", reason="Typed reason.", expected_revision="0")
    )
    assert response.status_code == 200
    text = response.content.decode()
    assert "was changed after you opened this form" in text
    assert "Typed Office Name" in text and "Typed reason." in text
    assert LabContact.objects.get(kind="privacy").name == "Example Privacy Office"
    assert AuditEvent.objects.count() == before


@pytest.mark.parametrize(
    "changes,field,message",
    [
        ({"name": ""}, "name", "Give the name of the person or office."),
        ({"email": "not-an-address"}, "email", "Enter a valid email address."),
        ({"reason": "  "}, "reason", "This field is required."),
    ],
)
def test_field_errors_show_under_their_field_with_input_kept(
    client, lab, admin, changes, field, message
):
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["privacy"])
    response = client.post(url, contact_data(title="Typed Title", **changes))
    assert response.status_code == 200
    assert message in response.context["form"].errors[field]
    assert "Typed Title" in response.content.decode()
    assert not LabContact.objects.exists() and not AuditEvent.objects.exists()


def test_no_change_says_so(client, lab, admin):
    record(admin, kind="privacy", name="Example Privacy Office")
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["privacy"])
    response = client.post(
        url,
        contact_data(title="", email="", phone="", expected_revision="1"),
        follow=True,
    )
    assert "No changes to save." in response.content.decode()
    assert AuditEvent.objects.filter(action="lab.contact_changed").count() == 1


def test_form_responses_are_never_cached(client, lab, admin):
    client.force_login(admin)
    url = reverse("lab-contact-edit", args=["privacy"])
    for response in (client.get(url), client.post(url, contact_data(email="bad"))):
        assert "no-store" in response["Cache-Control"]


# Navigation, the laboratory profile and the portal.


def nav(response):
    text = response.content.decode()
    start = text.find('<nav class="vr-sidenav')
    return text[start : text.find("</nav>", start)]


def test_only_the_admin_role_has_the_menu_item(client, lab, admin, staff):
    url = reverse("security-settings")
    client.force_login(admin)
    page = client.get(url)
    assert f'href="{url}"' in nav(page) and 'aria-current="page"' in nav(page)
    for role in OPERATIONAL:
        client.force_login(staff(f"security-menu-{role}", role))
        assert f'href="{url}"' not in nav(client.get(reverse("dashboard"))), role


@pytest.mark.parametrize("role", ROLES)
def test_profile_shows_contacts_to_every_staff_role_and_edit_to_admin(client, lab, staff, role):
    admin = staff("security-profile-admin", "admin")
    record(admin, email="security@example.invalid", phone="555-0143")
    client.force_login(staff(f"security-profile-{role}", role) if role != "admin" else admin)
    text = client.get(reverse("lab-profile")).content.decode()
    assert "Security and privacy contacts" in text
    assert "Example Security Office" in text and "555-0143" in text
    assert "Privacy contact" in text and "Not recorded" in text
    edit = reverse("lab-contact-edit", args=["security"])
    assert (f'href="{edit}"' in text) == (role == "admin")


def test_profile_history_names_the_contact(client, lab, admin):
    record(admin, kind="privacy", name="Example Privacy Office")
    client.force_login(admin)
    response = client.get(reverse("lab-profile"))
    event = next(e for e in response.context["history"] if e.action == "lab.contact_changed")
    assert event.detail == "Privacy contact"
    assert "Synthetic reason for the test." in response.content.decode()


def test_portal_home_does_not_show_the_contacts(client, lab, admin, portal_account):
    record(admin, email="security@example.invalid")
    client.force_login(portal_account)
    response = client.get(reverse("portal:home"))
    assert response.status_code == 200
    text = response.content.decode()
    assert "Example Security Office" not in text and "security@example.invalid" not in text
    assert "Security and privacy contacts" not in text
