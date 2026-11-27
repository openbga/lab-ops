import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.models import User
from accounts.roles import CAPABILITIES, ROLES, has_capability, has_role
from audit_review.seed import UNUSED_USERNAME
from clients.models import Client, ClientContact
from core import lab_profile
from core.announcements import announcements_for, withdraw_announcement
from core.models import Announcement, AuditEvent, Lab, LabCertificate

SHARED = "synthetic-test-password-only"
ADMIN_ONLY = "separate-admin-password-only"


@pytest.mark.django_db
@pytest.mark.parametrize("existing_group", [False, True])
def test_demo_seed_creates_synthetic_records_and_preserves_existing_data(
    monkeypatch, existing_group
):
    if existing_group:
        Group.objects.create(name="supervisor")
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    call_command("seed_demo", verbosity=0)

    assert Lab.objects.get().name == "Example Reference Laboratory"
    assert set(Group.objects.values_list("name", flat=True)) == set(ROLES)
    for role in ROLES:
        user = User.objects.get(username=f"demo_{role}")
        assert has_role(user, role) and user.email.endswith("@example.invalid")
        assert user.client_id is None
    # The demonstration grants one capability, to one account: "Can view patient information"
    # to demo_supervisor (desk/story.py), so the difference shows. No role holds one.
    assert not Group.objects.filter(permissions__isnull=False).exists()
    holders = {
        (user.username, code)
        for user in User.objects.all()
        for code in CAPABILITIES
        if has_capability(user, code)
    }
    assert holders == {
        ("demo_supervisor", "view_patient_information"),
        ("demo_supervisor", "review_audit_records"),
    }

    organizations = {c.account_number: c for c in Client.objects.all()}
    assert set(organizations) == {
        "EXCLIN",
        "EXHS",
        "EXGEN",
        "EXREC",
        "EXFRT",
        "EXWATER",
        "EXMFG",
        "EXRES",
        "EXOLD",
    }
    assert [c.account_number for c in Client.objects.filter(is_active=False)] == ["EXOLD"]
    assert organizations["EXGEN"].parent == organizations["EXHS"]
    assert {code: client.contacts.count() for code, client in organizations.items()} == {
        "EXCLIN": 2,
        "EXHS": 0,
        "EXGEN": 2,
        "EXREC": 1,
        "EXFRT": 2,
        "EXWATER": 2,
        "EXMFG": 0,
        "EXRES": 1,
        "EXOLD": 0,
    }
    assert organizations["EXWATER"].sites.count() == 3
    assert all(
        contact.email.endswith(".example.invalid") or not contact.email
        for contact in ClientContact.objects.all()
    )
    portal = {
        u.username: u.client.account_number for u in User.objects.filter(client__isnull=False)
    }
    assert portal == {"demo_client": "EXCLIN", "demo_client_water": "EXWATER"}
    # Staff tell the two apart by name ("Logged by ..."): each is named after its client.
    names = {u.username: u.get_full_name() for u in User.objects.filter(client__isnull=False)}
    assert names == {
        "demo_client": "Demo Family Clinic",
        "demo_client_water": "Demo Water Authority",
    }
    # One portal account sees its whole organization, the other one site.
    whole = User.objects.get(username="demo_client").portal_access
    narrow = User.objects.get(username="demo_client_water").portal_access
    assert whole.whole_organization and not whole.sites.exists()
    assert not narrow.whole_organization
    assert [site.code for site in narrow.sites.all()] == ["NORTH"]
    # Time-limited grants (accounts.grant_ends): the site access and the supervisor's patient
    # information access each have a last day 90 days after the seed's day; nothing else does.
    from datetime import timedelta

    from accounts import grant_ends
    from accounts.models import CapabilityEnd

    last_day = grant_ends.today() + timedelta(days=90)
    assert not whole.ends_at and narrow.ends_at == grant_ends.end_of(last_day)
    assert list(
        CapabilityEnd.objects.values_list("user__username", "permission__codename", "ends_at")
    ) == [("demo_supervisor", "view_patient_information", grant_ends.end_of(last_day))]
    end_set = AuditEvent.objects.get(action="account.capability_end_set")
    assert end_set.after == {
        "capability": "view_patient_information",
        "ends_at": grant_ends.iso(grant_ends.end_of(last_day)),
    }
    assert not any(has_role(u, *ROLES) for u in User.objects.filter(client__isnull=False))
    # Every record went through the audited services, as the account manager.
    created = AuditEvent.objects.filter(action="client.created")
    assert created.count() == 9
    assert {e.actor.username for e in created} == {"demo_account_manager"}
    assert AuditEvent.objects.filter(action="client.updated", reason__contains="seed").count() == 1
    assert AuditEvent.objects.filter(action="client_contact.added").count() == 10

    password_hash = User.objects.get(username="demo_supervisor").password
    organization = organizations["EXCLIN"]
    organization.notes = "Edited by the laboratory"
    organization.save()
    extra = Client.objects.create(name="Example Added Later")
    monkeypatch.setenv("DEMO_PASSWORD", "different-synthetic-test-password")
    call_command("seed_demo", verbosity=0)
    assert User.objects.get(username="demo_supervisor").password == password_hash
    organization.refresh_from_db()
    assert organization.notes == "Edited by the laboratory"
    assert set(Client.objects.values_list("pk", flat=True)) == {
        *(c.pk for c in organizations.values()),
        extra.pk,
    }
    assert ClientContact.objects.count() == 10
    assert AuditEvent.objects.filter(action="client.created").count() == 9


@pytest.mark.django_db
def test_seed_has_no_full_option(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    with pytest.raises(CommandError):
        call_command("seed_demo", "--full", verbosity=0)
    assert not Lab.objects.exists()


@pytest.mark.django_db
def test_fresh_seed_announces_the_demo_accounts_without_the_admin(monkeypatch):
    """The nightly-reset path: an empty database, one command, the notice is back."""
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_ONLY)
    monkeypatch.setenv("DEMO_RESET_NOTE", "Erased every day at 08:00 UTC.")
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    admin = User.objects.get(username="demo_admin")
    assert admin.check_password(ADMIN_ONLY) and not admin.check_password(SHARED)
    notice = Announcement.objects.get()
    assert [a.pk for a in announcements_for(AnonymousUser())] == [notice.pk]
    assert (notice.show_on_sign_in, notice.show_to_staff, notice.show_to_clients) == (
        True,
        False,
        False,
    )
    assert f"Password for each: {SHARED}" in notice.body
    assert notice.body.endswith("Anyone can use these accounts. Erased every day at 08:00 UTC.")
    assert "demo_admin" not in notice.body and ADMIN_ONLY not in notice.body
    listed = {line.split(":")[0] for line in notice.body.splitlines() if line.startswith("demo_")}
    # Every account but the administrator is published, except the Access review's example of an
    # unused account (audit_review.seed), which has no password: nobody signs in as it.
    unused = User.objects.get(username=UNUSED_USERNAME)
    assert not unused.has_usable_password() and not unused.credentials_locked
    assert listed == set(
        User.objects.exclude(pk__in=[admin.pk, unused.pk]).values_list("username", flat=True)
    ) and all(User.objects.get(username=name).check_password(SHARED) for name in listed)
    assert AuditEvent.objects.get(action="announcement.posted").actor == admin
    # Published accounts cannot change their own credentials; the administrator's can.
    assert set(User.objects.filter(credentials_locked=True).values_list("username", flat=True)) == (
        listed
    )

    # A rerun leaves the administrator's later decision alone.
    withdraw_announcement(actor=admin, pk=notice.pk)
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert Announcement.objects.get().withdrawn_at


@pytest.mark.django_db
def test_seed_never_publishes_a_password_that_opens_the_admin_account(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_RESET_NOTE", raising=False)
    call_command("seed_demo", verbosity=0)
    assert not Announcement.objects.exists()  # publishing is opt-in
    with pytest.raises(CommandError, match="also opens demo_admin"):
        call_command("seed_demo", announce_accounts=True, verbosity=0)
    assert not Announcement.objects.exists()

    admin = User.objects.get(username="demo_admin")
    admin.set_password(ADMIN_ONLY)
    admin.save()
    technician = User.objects.get(username="demo_technical")
    technician.set_password("changed-by-someone-else")
    technician.save()
    call_command("seed_demo", announce_accounts=True, verbosity=0)
    body = Announcement.objects.get().body
    assert "demo_account_manager" in body and "demo_client" in body and SHARED in body
    assert "demo_admin" not in body and "demo_technical" not in body  # only accounts it opens
    assert body.endswith("The demonstration can be reset at any time.")

    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "too-short")
    with pytest.raises(CommandError, match="DEMO_ADMIN_PASSWORD"):
        call_command("seed_demo", verbosity=0)


@pytest.mark.django_db
def test_seed_gives_the_demo_laboratory_an_identity_once(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    call_command("seed_demo", verbosity=0)
    lab = Lab.objects.get()
    assert lab.address_line1 == "200 Example Way" and lab.email.endswith("@example.invalid")
    assert lab_profile.logo_facts(lab)["width"] == 240
    assert [c.number for c in LabCertificate.objects.all()] == ["EX-0000000000", "EX-ACC-0000"]
    assert {c.state for c in lab_profile.certificates_with_state(timezone.localdate())} == {
        "current"
    }

    # An administrator's later changes survive the next seed.
    admin = User.objects.get(username="demo_admin")
    lab_profile.remove_logo(actor=admin)
    lab_profile.remove_certificate(actor=admin, pk=LabCertificate.objects.first().pk)
    call_command("seed_demo", verbosity=0)
    lab.refresh_from_db()
    assert lab.logo_id is None and LabCertificate.objects.count() == 1


@pytest.mark.django_db
def test_seed_needs_the_length_of_a_password_used_alone(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "fourteen-chars")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="DEMO_PASSWORD to at least 15 characters"):
        call_command("seed_demo", verbosity=0)
    monkeypatch.setenv("DEMO_PASSWORD", SHARED)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "fourteen-chars")
    with pytest.raises(CommandError, match="DEMO_ADMIN_PASSWORD needs at least 15 characters"):
        call_command("seed_demo", verbosity=0)
    assert not User.objects.exists()
