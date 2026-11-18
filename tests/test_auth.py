import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from accounts.models import User
from accounts.roles import effective_roles, has_capability, has_role
from clients.models import Client


@pytest.mark.django_db
def test_anonymous_redirect_and_registration_closed(client):
    assert client.get(reverse("clients:client-list")).status_code == 302
    response = client.post(
        reverse("account_signup"),
        {
            "username": "outsider",
            "email": "outside@example.invalid",
            "password1": "a-long-test-password",
            "password2": "a-long-test-password",
        },
    )
    assert response.status_code in (200, 302, 403)
    assert not User.objects.filter(username="outsider").exists()


@pytest.mark.django_db
def test_real_login_and_logout_require_post(client, staff):
    user = staff("technician", "technical")
    response = client.post(
        reverse("account_login"), {"login": user.username, "password": "test-password-729!"}
    )
    assert response.status_code == 302
    assert str(client.session["_auth_user_id"]) == str(user.pk)
    client.get(reverse("account_logout"))
    assert "_auth_user_id" in client.session
    client.post(reverse("account_logout"))
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["account_manager", "admin"])
def test_client_cannot_read_or_mutate_staff_routes_even_with_group(client, role):
    customer = Client.objects.create(name="Example Clinic")
    user = User.objects.create_user(
        "customer", "customer@example.invalid", "long-test-password", client=customer
    )
    group, _ = Group.objects.get_or_create(name=role)
    user.groups.add(group)
    assert not has_role(user, "account_manager")
    client.force_login(user)
    for route in ["client-list", "client-create"]:
        assert client.get(reverse(f"clients:{route}")).status_code == 403
        assert client.post(reverse(f"clients:{route}"), {}).status_code == 403


@pytest.mark.django_db
def test_admin_has_operational_roles_without_membership_or_capabilities(client, staff):
    admin = staff("admin", "admin")
    assert not admin.is_staff and not admin.is_superuser
    assert effective_roles(admin) == {
        "admin",
        "supervisor",
        "client_services",
        "account_manager",
        "technical",
    }
    assert not has_role(admin, "unrecognized_role")
    assert not has_capability(admin, "view_patient_information")
    client.force_login(admin)
    page = client.get(reverse("dashboard"))
    assert page.context["user_roles"] == effective_roles(admin)
    assert page.context["can_manage_clients"]
    assert set(admin.groups.values_list("name", flat=True)) == {"admin"}
    admin.groups.clear()
    assert not effective_roles(admin)  # role revocation is not hidden by a persistent cache


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind", ["anonymous", "inactive_admin", "client_admin", "superuser", "technical"]
)
def test_operational_admin_access_does_not_escape_internal_active_role_boundary(staff, kind):
    from clients.services import create_client

    if kind == "anonymous":
        user = AnonymousUser()
    elif kind == "superuser":
        user = User.objects.create_superuser("root", "root@example.invalid", "test-password")
    elif kind == "client_admin":  # a client account is one for life: created as one
        user = User.objects.create_user(
            "client_admin",
            "client_admin@example.invalid",
            "test-password",
            client=Client.objects.create(name="Synthetic portal owner"),
        )
        user.groups.add(Group.objects.get_or_create(name="admin")[0])
    else:
        user = staff(kind, "technical" if kind == "technical" else "admin")
        if kind == "inactive_admin":
            user.is_active = False
        user.save()
    assert not has_role(user, "account_manager", "client_services")
    assert not has_capability(user, "view_patient_information")
    with pytest.raises(PermissionDenied):
        create_client(actor=user, name="Unauthorized client", reason="Test")
    assert not Client.objects.filter(name="Unauthorized client").exists()


@pytest.mark.django_db
def test_csrf_required_for_mutations(staff):
    from django.test import Client as Browser

    browser = Browser(enforce_csrf_checks=True)
    browser.force_login(staff("technician", "technical"))
    assert browser.post(reverse("clients:client-create"), {}).status_code == 403


@pytest.mark.django_db
def test_capability_is_an_explicit_grant_never_implied_by_a_role(staff):
    from django.contrib.auth.models import Permission

    from accounts.roles import require_capability

    supervisor = staff("supervisor", "supervisor")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )
    assert not has_capability(supervisor, "view_patient_information")
    with pytest.raises(PermissionDenied):
        require_capability(supervisor, "view_patient_information")
    supervisor.user_permissions.add(permission)
    supervisor = User.objects.get(pk=supervisor.pk)  # permission caches live on the instance
    assert has_capability(supervisor, "view_patient_information")
    require_capability(supervisor, "view_patient_information")
    assert has_role(supervisor, "supervisor")
    supervisor.user_permissions.remove(permission)
    assert not has_capability(User.objects.get(pk=supervisor.pk), "view_patient_information")
    with pytest.raises(ValueError):
        has_capability(supervisor, "authorize_release")


@pytest.mark.django_db
def test_capability_can_come_from_a_group(staff):
    from django.contrib.auth.models import Permission

    technician = staff("technician", "technical")
    technician.groups.get().permissions.add(
        Permission.objects.get(
            content_type__app_label="accounts", codename="view_patient_information"
        )
    )
    assert has_capability(technician, "view_patient_information")


@pytest.mark.django_db
def test_admin_and_superuser_status_imply_no_capability(staff):
    admin = staff("admin", "admin")
    admin.is_staff = admin.is_superuser = True
    admin.save()
    assert not has_capability(admin, "view_patient_information")


@pytest.mark.django_db
def test_capability_does_not_override_client_boundary(db):
    from django.contrib.auth.models import Permission

    customer = Client.objects.create(name="Example Clinic")
    user = User.objects.create_user("customer", "customer@example.invalid", client=customer)
    user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="accounts", codename="view_patient_information"
        )
    )
    assert not has_capability(user, "view_patient_information")


@pytest.mark.django_db
def test_client_root_routes_to_scoped_portal(client):
    customer = Client.objects.create(name="Example Clinic")
    user = User.objects.create_user("portal_user", "portal@example.invalid", client=customer)
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302
    assert response.url == reverse("portal:home")
