"""Exercise script surfaces outside the workflow journeys under the enforced policy."""

import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from helpers import sign_in
from playwright.sync_api import expect

from accounts.models import User
from core.models import Lab

pytestmark = pytest.mark.django_db(transaction=True)


def assert_policy(response):
    assert response.status == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "frame-ancestors 'none'" in policy
    assert "camera=()" in response.headers["permissions-policy"]


def test_admin_and_identity_widgets_use_local_scripts(page, demo):
    admin = User.objects.get(username="demo_admin")
    admin.is_staff = admin.is_superuser = True
    admin.save(update_fields=["is_staff", "is_superuser"])
    sign_in(page, admin.username, demo.password)
    assert page.evaluate("htmx.config.allowEval") is False
    assert page.evaluate("htmx.config.includeIndicatorStyles") is False
    group = Group.objects.get(name="supervisor")
    routes = [
        reverse("admin:index"),
        reverse("admin:accounts_user_changelist"),
        reverse("admin:accounts_user_add"),
        reverse("admin:accounts_user_change", args=[admin.pk]),
        reverse("admin:auth_group_changelist"),
        reverse("admin:auth_group_change", args=[group.pk]),
        reverse("admin:core_lab_change", args=[Lab.objects.get().pk]),
        reverse("account_email"),
        reverse("mfa_index"),
        reverse("mfa_activate_totp"),
    ]
    for route in routes:
        assert_policy(page.goto(route))
        expect(page.locator("body")).to_be_visible()
        # Admin filter_horizontal widgets are initialized by the external SelectFilter script.
        if route == reverse("admin:auth_group_change", args=[group.pk]):
            expect(page.locator("#id_permissions_from")).to_be_visible()
            page.locator("#id_permissions_input").fill("client")
        assert page.console_errors == [], f"{route}: {page.console_errors}"


def test_staff_and_portal_pages_run_under_the_policy_without_violations(page, demo):
    from clients.models import Client

    clinic = Client.objects.get(name="Example Family Clinic")
    sign_in(page, "demo_account_manager", demo.password)
    for route in (
        reverse("dashboard"),
        reverse("clients:client-list"),
        reverse("clients:client-detail", args=[clinic.pk]),
        reverse("clients:client-create"),
        reverse("clients:client-edit", args=[clinic.pk]),
        reverse("clients:contact-create", args=[clinic.pk]),
        reverse("lab-profile"),
        reverse("whats-new"),
    ):
        assert_policy(page.goto(route))
        assert page.console_errors == [], f"{route}: {page.console_errors}"
    page.context.clear_cookies()
    sign_in(page, "demo_client", demo.password)
    assert_policy(page.goto(reverse("portal:home")))
    # The laboratory logo is a local image under the same policy.
    expect(page.get_by_role("img", name=re.compile(r" logo$"))).to_be_visible()
    assert page.console_errors == []


def test_csrf_failure_page_has_no_inline_policy_violation(page, demo):
    page.goto("/accounts/login/")
    page.locator('[name="csrfmiddlewaretoken"]').evaluate("element => element.remove()")
    with page.expect_response(
        lambda response: response.request.method == "POST" and "/accounts/login/" in response.url
    ) as refused:
        sign_in(page, "demo_admin", demo.password, visit=False)
    assert refused.value.status == 403
    expect(page.get_by_role("heading", name="The form could not be submitted")).to_be_visible()
    expect(page.get_by_text("This form could not be verified.", exact=False)).to_be_visible()
    assert page.locator("style").count() == 0
