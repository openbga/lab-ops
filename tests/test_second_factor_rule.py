"""Who must have a second sign-in factor (core.second_factor): the admin role sets the rule on
Security settings with a reason, against the revision the form showed, with one audit event; the
page lists it, counts the accounts it covers, and its history names each change."""

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts.models import User
from clients.models import Client
from core import second_factor
from core.models import AuditEvent, SecondFactorRule
from core.security_settings import in_force

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"
EDIT = "/lab/security/second-factor/"


def make_user(name, *roles, **extra):
    user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def form_data(revision=0, reason="Required by the laboratory's access policy.", **parts):
    data = {"expected_revision": revision, "reason": reason}
    for name in second_factor.FLAGS:
        if parts.get(name):
            data[name] = "on"
    data["roles"] = parts.get("roles", [])
    return data


def fact(key):
    for group in in_force():
        for item in group.facts:
            if item.key == key:
                return item
    raise AssertionError(key)


# --- the service --------------------------------------------------------------------------------


def test_nothing_is_required_until_the_rule_is_saved():
    rule = second_factor.rule()
    assert rule.pk == 1 and rule._state.adding and rule.revision == 0
    assert not second_factor.requires_any(rule)
    assert second_factor.counts() == (0, 0)
    assert second_factor.requires(make_user("anyone", "admin", is_staff=True)) is False
    assert not SecondFactorRule.objects.exists()


def test_saving_records_the_rule_before_and_after_with_the_reason():
    admin = make_user("rule_admin", "admin")
    rule, changed = second_factor.save_rule(
        actor=admin,
        new={"staff": False, "roles": ["technical", "supervisor"], "portal": True},
        expected_revision=0,
        reason="  Clinic contracts ask for it.  ",
    )
    assert changed and rule.revision == 1
    stored = SecondFactorRule.objects.get(pk=1)
    assert stored.roles == ["supervisor", "technical"]  # in ROLES order
    assert stored.portal and not stored.staff
    event = AuditEvent.objects.get(action=second_factor.ACTION)
    assert event.actor == admin and event.reason == "Clinic contracts ask for it."
    assert event.before == {
        "staff": False,
        "roles": [],
        "patient_information": False,
        "admin_console": False,
        "portal": False,
    }
    assert event.after == {
        "staff": False,
        "roles": ["supervisor", "technical"],
        "patient_information": False,
        "admin_console": False,
        "portal": True,
    }
    # The same values again change nothing and record nothing.
    _, changed = second_factor.save_rule(
        actor=admin,
        new=second_factor.values(stored),
        expected_revision=1,
        reason="Again.",
    )
    assert not changed
    assert AuditEvent.objects.filter(action=second_factor.ACTION).count() == 1
    assert SecondFactorRule.objects.get(pk=1).revision == 1


def test_a_rule_changed_after_the_form_opened_is_not_overwritten():
    admin = make_user("stale_admin", "admin")
    second_factor.save_rule(actor=admin, new={"staff": True}, expected_revision=0, reason="One.")
    with pytest.raises(ValidationError, match="changed after you opened this form"):
        second_factor.save_rule(
            actor=admin, new={"portal": True}, expected_revision=0, reason="Two."
        )
    with pytest.raises(ValidationError, match="changed after you opened this form"):
        second_factor.save_rule(
            actor=admin, new={"portal": True}, expected_revision=None, reason="Two."
        )
    stored = SecondFactorRule.objects.get(pk=1)
    assert stored.staff and not stored.portal and stored.revision == 1


@pytest.mark.parametrize(
    ("roles", "extra"),
    [(("supervisor",), {}), ((), {"is_staff": True, "is_superuser": True}), (("admin",), {})],
)
def test_only_an_active_administrator_saves_the_rule(roles, extra):
    actor = make_user("not_allowed", *roles, **extra)
    if "admin" in roles:
        actor.is_active = False
        actor.save()
    with pytest.raises(PermissionDenied):
        second_factor.save_rule(actor=actor, new={"staff": True}, expected_revision=0, reason="x")
    assert not SecondFactorRule.objects.exists()


def test_a_portal_account_in_the_admin_group_cannot_save_it():
    organization = Client.objects.create(name="Example Clinic")
    actor = make_user("portal_admin", "admin", client=organization)
    with pytest.raises(PermissionDenied):
        second_factor.save_rule(actor=actor, new={"staff": True}, expected_revision=0, reason="x")


@pytest.mark.parametrize(
    ("new", "field"),
    [
        ({"roles": ["owner"]}, "roles"),
        ({"roles": "admin"}, "roles"),
        ({"staff": "yes"}, "staff"),
    ],
)
def test_unknown_parts_are_refused(new, field):
    admin = make_user("strict_admin", "admin")
    with pytest.raises(ValidationError) as caught:
        second_factor.save_rule(actor=admin, new=new, expected_revision=0, reason="x")
    assert field in caught.value.error_dict


def test_a_reason_is_required():
    admin = make_user("reasonless_admin", "admin")
    with pytest.raises(ValidationError) as caught:
        second_factor.save_rule(actor=admin, new={"staff": True}, expected_revision=0, reason=" ")
    assert "reason" in caught.value.error_dict


def test_counts_cover_active_accounts_and_count_only_an_authenticator_app():
    admin = make_user("count_admin", "admin")
    second_factor.save_rule(
        actor=admin, new={"roles": ["technical"]}, expected_revision=0, reason="x"
    )
    with_app = make_user("with_app", "technical")
    TOTP.activate(with_app, generate_totp_secret())
    codes_only = make_user("codes_only", "technical")
    RecoveryCodes.activate(codes_only)
    make_user("without", "technical")
    make_user("inactive", "technical", is_active=False)
    make_user("other_role", "client_services")
    # admin counts through the admin role, which does every role's work
    assert second_factor.counts() == (4, 3)
    assert Authenticator.objects.count() == 2


def test_reasons_name_the_parts_that_cover_the_account():
    admin = make_user("reasons_admin", "admin")
    grant = Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )
    second_factor.save_rule(
        actor=admin,
        new={"roles": ["supervisor", "technical"], "patient_information": True},
        expected_revision=0,
        reason="x",
    )
    person = make_user("reasons_person", "supervisor")
    person.user_permissions.add(grant)
    assert second_factor.reasons(person) == [
        "the Supervisor role",
        "staff who can view patient information",
    ]
    assert second_factor.reasons(admin) == ["the Supervisor or Technical staff role"]
    assert second_factor.reasons(make_user("reasons_other", "client_services")) == []


def test_summary_words_each_rule():
    assert second_factor.summary({}) == "Nobody"
    assert (
        second_factor.summary({"staff": True, "roles": ["admin"], "portal": True})
        == "Every staff account; staff with the Administrator role; client portal accounts"
    )


# --- Security settings ------------------------------------------------------------------------


def test_the_page_lists_the_rule_and_its_history(client):
    admin = make_user("page_admin", "admin")
    client.force_login(admin)
    page = client.get(reverse("security-settings"))
    html = page.content.decode()
    assert "Second sign-in factor" in html
    assert html.count('<span class="vr-muted">Not required</span>') == 5
    assert f'href="{EDIT}"' in html
    assert fact("second-factor").value == "Optional"
    assert fact("second-factor").source_label == "Second sign-in factor, above"

    second_factor.save_rule(
        actor=admin,
        new={"roles": ["supervisor"], "admin_console": True},
        expected_revision=0,
        reason="Supervisors see restricted tickets.",
    )
    html = client.get(reverse("security-settings")).content.decode()
    assert html.count('<span class="vr-muted">Not required</span>') == 3
    assert "Required: Supervisor</span>" in html
    assert "Active accounts required to have one: 1; without one yet: 1." in html
    assert (
        "Required of: staff with the Supervisor role; accounts that can open the admin console "
        "(was: nobody)"
    ) in html
    assert "Supervisors see restricted tickets." in html
    assert fact("second-factor").value == (
        "Required of staff with the Supervisor role; accounts that can open the admin console"
    )


def test_other_staff_and_portal_accounts_cannot_open_the_form(client):
    organization = Client.objects.create(name="Example Clinic")
    for user in (
        make_user("form_supervisor", "supervisor"),
        make_user("form_superuser", is_staff=True, is_superuser=True),
        make_user("form_portal", client=organization),
    ):
        client.force_login(user)
        assert client.get(EDIT).status_code == 403, user.username
        assert client.post(EDIT, form_data(staff=True)).status_code == 403, user.username
    client.logout()
    response = client.get(EDIT)
    assert response.status_code == 302 and response.url.startswith(reverse("account_login"))
    assert not SecondFactorRule.objects.exists()


def test_the_form_saves_and_returns_to_security_settings(client):
    admin = make_user("form_admin", "admin")
    client.force_login(admin)
    page = client.get(EDIT)
    assert page.status_code == 200
    assert 'name="expected_revision" value="0"' in page.content.decode()
    response = client.post(EDIT, form_data(staff=True, roles=["admin"]))
    assert response.status_code == 302 and response.url == reverse("security-settings")
    stored = SecondFactorRule.objects.get(pk=1)
    assert stored.staff and stored.roles == ["admin"] and stored.revision == 1


def test_a_stale_form_is_refused_with_the_input_kept(client):
    admin = make_user("stale_form_admin", "admin")
    client.force_login(admin)
    second_factor.save_rule(actor=admin, new={"staff": True}, expected_revision=0, reason="x")
    response = client.post(EDIT, form_data(revision=0, portal=True, reason="Portal too."))
    assert response.status_code == 200
    html = response.content.decode()
    assert "changed after you opened this form" in html
    assert "Portal too." in html
    stored = SecondFactorRule.objects.get(pk=1)
    assert stored.staff and not stored.portal


def test_a_form_without_a_reason_is_refused(client):
    admin = make_user("form_reasonless", "admin")
    client.force_login(admin)
    response = client.post(EDIT, form_data(staff=True, reason=""))
    assert response.status_code == 200
    assert not SecondFactorRule.objects.exists()


def test_an_unknown_role_in_the_form_is_refused(client):
    admin = make_user("form_unknown_role", "admin")
    client.force_login(admin)
    response = client.post(EDIT, form_data(roles=["owner"]))
    assert response.status_code == 200
    assert not SecondFactorRule.objects.exists()
