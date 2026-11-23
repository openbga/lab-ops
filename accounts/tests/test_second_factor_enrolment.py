"""A second sign-in factor the laboratory requires (accounts.enrolment, core.second_factor): a
session the rule covered at sign-in reaches only the pages that set one up while its account has
none, whatever kind of request it sends, and goes on once one is added."""

import json

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    SECRET_SESSION_KEY,
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.urls import reverse

from accounts.enrolment import ALLOWED_VIEWS, NEXT_KEY
from accounts.models import AccountSession
from clients.models import Client
from core.models import SecondFactorRule

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db

SETUP = "/account/second-factor/"


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


def require(**parts):
    rule = SecondFactorRule.objects.filter(pk=1).first() or SecondFactorRule(pk=1)
    for name in ("staff", "patient_information", "admin_console", "portal"):
        setattr(rule, name, parts.get(name, False))
    rule.roles = parts.get("roles", [])
    rule.save()
    return rule


def sign_in(client, user, next_page=None):
    url = reverse("account_login") + (f"?next={next_page}" if next_page else "")
    return client.post(url, {"login": user.username, "password": PASSWORD})


def code_for(secret):
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def reauthenticate(client):
    """Confirm the password again, as adding or removing an authenticator asks first."""
    response = client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert response.status_code == 302, response.content[:300]


def session_row(client):
    return AccountSession.objects.get(pk=client.session["_account_session"])


def portal_account(make_user):
    organization = Client.objects.create(name="Example Clinic")
    return make_user("portal_person", client=organization)


# --- the rule's parts, as sign-in records them ---------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "roles", "extra", "covered"),
    [
        ({"staff": True}, ("technical",), {}, True),
        ({"staff": True}, (), {}, True),  # a staff account without a role
        ({"roles": ["supervisor"]}, ("supervisor",), {}, True),
        ({"roles": ["supervisor"]}, ("admin",), {}, True),  # admin does every role's work
        ({"roles": ["supervisor"]}, ("technical",), {}, False),
        ({"roles": ["admin"]}, ("supervisor",), {}, False),
        ({"admin_console": True}, ("technical",), {"is_staff": True}, True),
        ({"admin_console": True}, ("technical",), {}, False),
        ({"portal": True}, ("technical",), {}, False),
        ({}, ("admin",), {"is_staff": True, "is_superuser": True}, False),
    ],
)
def test_sign_in_records_whether_the_rule_covers_the_account(
    client, make_user, parts, roles, extra, covered
):
    require(**parts)
    user = make_user("covered_person", *roles, **extra)
    sign_in(client, user)
    assert session_row(client).second_factor_required is covered
    response = client.get("/")
    if covered:
        assert response.status_code == 302 and response.url == SETUP
    else:
        assert response.status_code == 200


def test_the_patient_information_part_counts_a_grant_to_the_account_or_its_group(client, make_user):
    require(patient_information=True)
    grant = Permission.objects.get(
        codename="view_patient_information", content_type__app_label="accounts"
    )
    direct = make_user("direct_grant", "technical")
    direct.user_permissions.add(grant)
    by_group = make_user("group_grant", "supervisor")
    Group.objects.get(name="supervisor").permissions.add(grant)
    without = make_user("no_grant", "technical")
    for user, covered in ((direct, True), (by_group, True), (without, False)):
        client.logout()
        sign_in(client, user)
        assert session_row(client).second_factor_required is covered, user.username


def test_a_portal_account_is_covered_only_by_the_portal_part(client, make_user):
    require(staff=True, roles=list(("admin", "supervisor")), admin_console=True)
    person = portal_account(make_user)
    sign_in(client, person)
    assert session_row(client).second_factor_required is False
    client.logout()
    require(portal=True)
    sign_in(client, person)
    assert session_row(client).second_factor_required is True
    response = client.get(reverse("portal:home"))
    assert response.status_code == 302 and response.url == SETUP
    page = client.get(SETUP)
    assert page.status_code == 200
    html = page.content.decode()
    assert "client portal accounts" in html
    assert 'aria-label="Portal"' not in html  # the portal navigation is left out


def test_nothing_is_required_until_an_administrator_saves_the_rule(client, make_user):
    user = make_user("nobody_required", "admin", is_staff=True, is_superuser=True)
    sign_in(client, user)
    assert session_row(client).second_factor_required is False
    assert client.get("/").status_code == 200
    # The setup page itself sends a session that is not waiting on.
    assert client.get(SETUP).url == reverse("dashboard")


# --- what a waiting session reaches -----------------------------------------------------------


def test_a_waiting_session_reaches_only_the_setup_pages(client, make_user):
    require(staff=True)
    user = make_user("waiting_person", "supervisor", is_staff=True)
    response = sign_in(client, user, next_page="/requests/")
    assert response.url == "/requests/"
    response = client.get("/requests/")
    assert response.status_code == 302 and response.url == SETUP
    assert client.session[NEXT_KEY] == "/requests/"

    page = client.get(SETUP)
    assert page.status_code == 200
    html = page.content.decode()
    assert "Set up a second sign-in factor" in html
    assert "every staff account" in html
    assert reverse("mfa_activate_totp") in html
    assert 'aria-label="Primary"' not in html  # no staff navigation
    assert 'aria-label="Quick actions"' not in html  # no phone thumb bar
    assert "no-store" in page["Cache-Control"]

    for name in ("mfa_index", "account_reauthenticate", "account-session"):
        assert client.get(reverse(name)).status_code == 200, name
    # Adding an authenticator asks for the password again first (allauth's re-authentication).
    response = client.get(reverse("mfa_activate_totp"))
    assert response.url.startswith(reverse("account_reauthenticate"))
    reauthenticate(client)
    assert client.get(reverse("mfa_activate_totp")).status_code == 200

    # Everything else is sent to the setup page: pages, the admin console, files, forms.
    for path in (
        "/",
        "/requests/",
        "/lab/profile/",
        "/admin/",
        "/account/sessions/",
        "/whats-new/",
    ):
        response = client.get(path)
        assert response.status_code == 302 and response.url == SETUP, path
    assert client.get(reverse("account_change_password")).url == SETUP
    assert client.get(reverse("account_email")).url == SETUP
    # The page remembered is the first one the sign-in was opening.
    assert client.session[NEXT_KEY] == "/requests/"


def test_requests_other_than_pages_are_refused_with_where_to_go(client, make_user):
    require(staff=True)
    sign_in(client, make_user("waiting_scripts", "client_services"))
    # A preference saved by a page's script.
    response = client.post(
        reverse("preference"), {"key": "nav.collapsed", "value": "1"}, HTTP_SEC_FETCH_DEST="empty"
    )
    assert response.status_code == 403
    assert json.loads(response.content) == {"second_factor_required": True, "location": SETUP}
    # A partial update.
    response = client.get("/requests/", HTTP_HX_REQUEST="true")
    assert response.status_code == 204 and response["HX-Redirect"] == SETUP
    # The shared form dialog: told to load the setup page in the whole window.
    response = client.get(reverse("desk:request-create"), HTTP_X_VR_FORM="1")
    assert response.status_code == 204 and response["HX-Redirect"] == SETUP
    # A form posted without script is sent there too, and nothing is remembered for it.
    response = client.post(reverse("desk:request-create"), {"subject": "x"})
    assert response.status_code == 302 and response.url == SETUP
    assert NEXT_KEY not in client.session
    # A lookup a page's control asks.
    response = client.get(reverse("lookup", args=["clients"]), HTTP_SEC_FETCH_DEST="empty")
    assert response.status_code == 403


def test_sign_out_works_from_a_waiting_session(client, make_user):
    require(staff=True)
    sign_in(client, make_user("waiting_sign_out", "technical"))
    row_id = client.session["_account_session"]
    response = client.post(reverse("account_logout"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session
    assert AccountSession.objects.get(pk=row_id).ending == AccountSession.Ending.SIGNED_OUT


def test_adding_an_authenticator_opens_the_rest_and_continue_goes_on_once(client, make_user):
    require(roles=["client_services"])
    user = make_user("enrolling_person", "client_services")
    sign_in(client, user, next_page="/requests/?view=all")
    assert client.get("/requests/?view=all").url == SETUP

    reauthenticate(client)
    page = client.get(reverse("mfa_activate_totp"))
    assert page.status_code == 200
    secret = client.session[SECRET_SESSION_KEY]
    response = client.post(reverse("mfa_activate_totp"), {"code": code_for(secret)})
    assert response.status_code == 302
    assert response.url == reverse("mfa_view_recovery_codes")
    assert Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()

    codes = client.get(response.url)
    assert codes.status_code == 200
    assert 'href="/account/second-factor/"' in codes.content.decode()  # Continue
    assert 'aria-label="Primary"' in codes.content.decode()  # the navigation is back

    response = client.get(SETUP)
    assert response.status_code == 302 and response.url == "/requests/?view=all"
    assert NEXT_KEY not in client.session
    assert client.get("/requests/?view=all").status_code == 200
    # Continue is taken once; afterwards the setup page leads to the start page.
    assert client.get(SETUP).url == reverse("dashboard")
    assert session_row(client).second_factor_required is True


def test_an_enrolled_account_signs_in_with_its_factor_and_is_not_held(client, make_user):
    require(staff=True)
    user = make_user("enrolled_person", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    response = sign_in(client, user)
    assert response.url == reverse("mfa_authenticate")
    client.post(response.url, {"code": code_for(secret)})
    assert session_row(client).second_factor_required is True
    assert client.get("/").status_code == 200
    assert client.get(SETUP).url == reverse("dashboard")


def test_recovery_codes_alone_are_not_a_second_factor(client, make_user):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    require(staff=True)
    user = make_user("codes_only", "technical")
    RecoveryCodes.activate(user)
    sign_in(client, user)
    assert client.get("/").url == SETUP


def test_an_emailed_code_sign_in_is_held_too(client, make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    require(staff=True)
    user = make_user("emailed_code_person", "technical")
    request_code(client, user.email)
    client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert client.session["_auth_user_id"] == str(user.pk)
    assert session_row(client).second_factor_required is True
    assert client.get("/").url == SETUP


# --- the rule or the account changing while a session is open --------------------------------


def test_a_rule_saved_after_sign_in_applies_from_the_next_sign_in(client, make_user):
    user = make_user("signed_in_before", "technical")
    sign_in(client, user)
    require(staff=True)
    assert client.get("/").status_code == 200  # this session is not interrupted
    client.logout()
    sign_in(client, user)
    assert client.get("/").url == SETUP


def test_a_waiting_session_goes_on_when_the_rule_no_longer_covers_the_account(client, make_user):
    rule = require(roles=["technical"])
    user = make_user("released_by_rule", "technical")
    sign_in(client, user)
    assert client.get("/").url == SETUP
    rule.roles = ["supervisor"]
    rule.save()
    assert client.get("/").status_code == 200
    # Covered again while the session is still open: held again, as it was at sign-in.
    rule.roles = ["technical"]
    rule.save()
    assert client.get("/").url == SETUP
    user.groups.clear()
    assert client.get("/").status_code == 403  # no longer held; a staff page needs a role


def test_removing_the_authenticator_sends_the_session_back_to_setup(client, make_user):
    require(staff=True)
    user = make_user("removes_factor", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    response = sign_in(client, user)
    client.post(response.url, {"code": code_for(secret)})
    assert client.get("/").status_code == 200
    reauthenticate(client)
    response = client.post(reverse("mfa_deactivate_totp"))
    assert response.status_code == 302
    assert not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    assert client.get("/").url == SETUP
    assert client.get(reverse("mfa_activate_totp")).status_code == 200


def test_a_session_not_covered_at_sign_in_keeps_going_after_removing_its_factor(client, make_user):
    user = make_user("not_covered_removes", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    response = sign_in(client, user)
    client.post(response.url, {"code": code_for(secret)})
    require(staff=True)
    reauthenticate(client)
    client.post(reverse("mfa_deactivate_totp"))
    assert not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    assert client.get("/").status_code == 200  # from its next sign-in


def test_a_shared_demonstration_account_cannot_add_one_and_is_told_so(client, make_user):
    require(staff=True)
    user = make_user("shared_demo", "client_services", credentials_locked=True)
    sign_in(client, user)
    page = client.get(SETUP)
    html = page.content.decode()
    assert page.status_code == 200
    assert "This shared demonstration account cannot add a second factor" in html
    assert reverse("mfa_activate_totp") not in html
    # Its two-factor pages are refused (accounts.middleware), which ends on the setup page.
    response = client.get(reverse("mfa_activate_totp"), follow=True)
    assert response.redirect_chain[-1][0] == SETUP
    assert response.status_code == 200


def test_the_allowed_pages_are_the_setup_ones():
    assert ALLOWED_VIEWS == {
        "account-second-factor",
        "mfa_index",
        "mfa_activate_totp",
        "mfa_view_recovery_codes",
        "mfa_download_recovery_codes",
        "mfa_generate_recovery_codes",
        "account_reauthenticate",
        "mfa_reauthenticate",
        "account_login",
        "account_logout",
        "account-session",
        "account-session-continue",
        "account-session-keep",
        "lab-logo-image",
        "health",
        "ready",
    }


def test_continue_never_leaves_the_site(client, make_user):
    require(staff=True)
    user = make_user("continue_elsewhere", "technical")
    sign_in(client, user)
    session = client.session
    session[NEXT_KEY] = "//elsewhere.example/path"
    session.save()
    TOTP.activate(user, generate_totp_secret())
    assert client.get(SETUP).url == reverse("dashboard")


# --- review round 1: neighbours of the fixed cases ----------------------------------------------


def test_each_decision_reads_the_rule_and_the_account_in_one_statement(make_user):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from core import second_factor

    require(roles=["technical"], patient_information=True, portal=True)
    user = make_user("one_statement", "technical")
    for read in (
        lambda: second_factor.requires(user),
        lambda: second_factor.reasons(user),
        lambda: second_factor.counts(),
    ):
        with CaptureQueriesContext(connection) as queries:
            read()
        assert len(queries) == 1, [query["sql"] for query in queries]


def test_an_account_without_a_password_confirms_with_a_recovery_code(client, make_user, settings):
    from allauth.account.models import EmailAddress
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    require(staff=True)
    user = make_user("codes_confirm", "technical")
    user.set_unusable_password()
    user.save(update_fields=["password"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    codes = RecoveryCodes.activate(user)
    request_code(client, user.email)
    client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert client.get("/").url == SETUP
    prompt = client.get(reverse("mfa_activate_totp"), follow=True)
    assert prompt.resolver_match.view_name == "mfa_reauthenticate"
    # Still waiting: the confirmation page opens, nothing else does.
    assert client.get("/requests/").url == SETUP
    response = client.post(prompt.request["PATH_INFO"], {"code": codes.get_unused_codes()[0]})
    assert response.status_code == 302
    assert client.get(reverse("mfa_activate_totp")).status_code == 200


def test_a_page_not_found_keeps_the_shell_of_the_session(client, make_user):
    require(roles=["technical"])
    waiting = make_user("missing_waiting", "technical")
    sign_in(client, waiting)
    html = client.get("/no-such-page/").content.decode()
    assert 'aria-label="Primary"' not in html
    other = make_user("missing_not_waiting", "client_services")
    second = type(client)()
    sign_in(second, other)
    html = second.get("/no-such-page/").content.decode()
    assert 'aria-label="Primary"' in html


def test_the_account_menu_offers_only_two_factor_and_sign_out_while_waiting(client, make_user):
    require(staff=True)
    sign_in(client, make_user("menu_waiting", "technical"))
    html = client.get(SETUP).content.decode()
    assert reverse("mfa_index") in html and reverse("account_logout") in html
    for name in ("account_change_password", "account_email", "account-sessions", "email-notices"):
        assert f'href="{reverse(name)}"' not in html, name
    TOTP.activate(User := session_row(client).user, generate_totp_secret())  # noqa: N806
    html = client.get(reverse("mfa_index")).content.decode()
    for name in ("account_change_password", "account_email", "account-sessions"):
        assert f'href="{reverse(name)}"' in html, name
    assert User.username == "menu_waiting"


def test_a_credential_type_the_deployment_does_not_offer_is_not_a_second_factor(make_user):
    from core import second_factor

    require(staff=True)
    user = make_user("webauthn_only", "technical")
    Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
    assert second_factor.has_second_factor(user) is False
    assert second_factor.counts() == (1, 1)


def test_a_head_request_neither_remembers_a_page_nor_takes_one(client, make_user):
    require(staff=True)
    user = make_user("head_requests", "technical")
    sign_in(client, user)
    assert client.head("/whats-new/").url == SETUP
    assert NEXT_KEY not in client.session
    assert client.get("/requests/").url == SETUP
    TOTP.activate(user, generate_totp_secret())
    assert client.head(SETUP).url == "/requests/"
    assert client.session[NEXT_KEY] == "/requests/"
    assert client.get(SETUP).url == "/requests/"


def test_opening_the_setup_page_first_remembers_the_start_page(client, make_user):
    require(staff=True)
    user = make_user("setup_first", "technical")
    sign_in(client, user, next_page=SETUP)
    assert client.get(SETUP).status_code == 200
    assert client.session[NEXT_KEY] == reverse("dashboard")
    assert client.get("/requests/").url == SETUP
    assert client.session[NEXT_KEY] == reverse("dashboard")  # the first page stays


# --- review verification: neighbours of the fixed cases -------------------------------------------


def test_the_gate_reads_the_flag_the_factor_and_the_rule_in_one_statement(client, make_user):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from accounts import enrolment

    require(staff=True)
    sign_in(client, make_user("gate_one_statement", "technical"))
    request = client.get(SETUP).wsgi_request
    with CaptureQueriesContext(connection) as queries:
        assert enrolment.pending(request) is True
    assert len(queries) == 1


def test_a_stored_name_that_is_not_a_role_neither_covers_nor_survives_a_save(make_user):
    from django.contrib.auth.models import Group

    from core import second_factor
    from core.models import AuditEvent

    member = make_user("stray_group_member")
    member.groups.add(Group.objects.create(name="Technical"))  # not the role group "technical"
    rule = require()
    SecondFactorRule.objects.filter(pk=1).update(roles=["Technical"])
    assert second_factor.requires(member) is False
    assert second_factor.counts() == (0, 0)
    admin = make_user("stray_admin", "admin")
    _, changed = second_factor.save_rule(
        actor=admin, new={}, expected_revision=rule.revision, reason="Clear the stored names."
    )
    assert changed and SecondFactorRule.objects.get(pk=1).roles == []
    event = AuditEvent.objects.get(action=second_factor.ACTION)
    assert event.before["roles"] == ["Technical"] and event.after["roles"] == []


def test_a_waiting_session_opens_its_existing_codes_but_stays_held(client, make_user):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    require(staff=True)
    user = make_user("codes_while_waiting", "technical")
    RecoveryCodes.activate(user)
    sign_in(client, user)
    client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert client.get(reverse("mfa_view_recovery_codes")).status_code == 200
    assert client.get(reverse("mfa_download_recovery_codes")).status_code == 200
    client.get(reverse("mfa_generate_recovery_codes"))
    assert client.get("/requests/").url == SETUP  # codes are not a second factor


def test_the_setup_page_words_used_up_codes(client, make_user):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    require(staff=True)
    user = make_user("used_up_codes", "technical")
    codes = RecoveryCodes.activate(user)
    for code in codes.get_unused_codes():
        codes.validate_code(code)
    sign_in(client, user)
    html = client.get(SETUP).content.decode()
    assert "recovery codes are all used" in html and "Generate" in html


# --- review confirmation: neighbours of the fixed cases -------------------------------------------


def test_only_a_window_navigation_to_an_allowed_page_is_remembered(client, make_user):
    require(staff=True)
    sign_in(client, make_user("allowed_first", "technical"), next_page=reverse("mfa_index"))
    client.get(reverse("mfa_index"), HTTP_SEC_FETCH_DEST="empty")
    client.get(reverse("mfa_index"), HTTP_HX_REQUEST="true")
    assert NEXT_KEY not in client.session
    assert client.get(reverse("mfa_index")).status_code == 200
    assert client.session[NEXT_KEY] == reverse("mfa_index")
    client.get(SETUP)
    assert client.session[NEXT_KEY] == reverse("mfa_index")


def test_the_overview_offers_continue_only_once_the_session_is_released(client, make_user):
    require(staff=True)
    user = make_user("overview_continue", "technical")
    sign_in(client, user)
    assert client.get("/requests/").url == SETUP
    TOTP.activate(user, generate_totp_secret())
    require(staff=True, roles=["technical"])  # still covered; the factor releases the session
    assert f'href="{SETUP}">Continue</a>' in client.get(reverse("mfa_index")).content.decode()
    Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).delete()
    TOTP.activate(user, generate_totp_secret())
    html = client.get(reverse("mfa_index")).content.decode()
    assert f'href="{SETUP}">Continue</a>' in html


def test_a_waiting_overview_does_not_offer_continue(client, make_user):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    require(staff=True)
    user = make_user("overview_waiting", "technical")
    RecoveryCodes.activate(user)
    sign_in(client, user)
    assert client.get("/requests/").url == SETUP
    client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    for name in ("mfa_index", "mfa_view_recovery_codes"):
        assert f'href="{SETUP}">Continue</a>' not in client.get(reverse(name)).content.decode()
