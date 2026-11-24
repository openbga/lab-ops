"""Confirm it is you (accounts.confirmation): the password, and the second factor where the
account has one, asked for again before a change to roles, capabilities, security settings or
another account's sign-in factors; not asked again for ACCOUNT_REAUTHENTICATION_TIMEOUT seconds
once confirmed in the same browser session."""

import logging
import time

import pytest
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import Client as HttpClient
from django.urls import reverse

from accounts import confirmation
from accounts.models import SignInEvent, User
from core.models import AuditEvent, SecondFactorRule, SessionLimit

from .conftest import PASSWORD

pytestmark = [pytest.mark.django_db, pytest.mark.confirmation]

USER_KEY = "account_authentication_methods_user"
WRONG = "not-the-password-4471"


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(make_user):
    return make_user("owner", is_staff=True, is_superuser=True)


@pytest.fixture
def lab_admin(make_user):
    return make_user("lab_admin", "admin")


@pytest.fixture
def person(make_user):
    return make_user("person", "technical")


def http_for(user):
    http = HttpClient()
    http.force_login(user)
    return http


def code_for(secret):
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def with_authenticator(user):
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    return secret


def records(http, user, *methods, ago=0, reauthenticated=True):
    """Put re-authentication records in the browser's session, as allauth writes them."""
    session = http.session
    entries = []
    for method in methods:
        entry = {"method": method, "at": time.time() - ago}
        if method == "mfa":
            entry.update(type="totp", id=1)
        if reauthenticated:
            entry["reauthenticated"] = True
        entries.append(entry)
    session[AUTHENTICATION_METHODS_SESSION_KEY] = entries
    session[USER_KEY] = str(user.pk)
    session.save()


def page_data(form, **changes):
    """The values a Django form was drawn with, as the browser posts them, changed."""
    data = {}
    for name, field in form.fields.items():
        if field.disabled or name.startswith("confirm_"):
            continue
        value = form[name].value()
        if value is None or value is False:
            continue
        if isinstance(value, (list, tuple)):
            data[name] = [getattr(v, "pk", v) for v in value]
        elif value is True:
            data[name] = "on"
        else:
            data[name] = getattr(value, "pk", value)
    data.update(changes)
    return data


def account_change(http, account, **changes):
    url = reverse("admin:accounts_user_change", args=[account.pk])
    form = http.get(url).context["adminform"].form
    return http.post(url, page_data(form, **changes))


def roles_of(account):
    return set(account.groups.values_list("name", flat=True))


def sign_in_failures(user):
    """The account's sign-in history entries other than sign-ins and sign-outs."""
    return list(
        SignInEvent.objects.filter(user_id=user.pk)
        .exclude(kind__in=["signed_in", "signed_out"])
        .values_list("kind", "factor")
        .order_by("pk")
    )


# --- whether the session confirmed recently enough ----------------------------------------------


def request_for(http, user):
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.session = SessionStore(session_key=http.session.session_key)
    return request


@pytest.mark.parametrize(
    ("methods", "ago", "reauthenticated", "second_factor", "expected"),
    [
        ((), 0, True, False, False),
        (("password",), 0, True, False, True),
        (("password",), 299, True, False, True),
        (("password",), 301, True, False, False),
        (("password",), 0, False, False, False),  # the sign-in itself does not count
        (("password",), 0, True, True, False),  # an authenticator: the code too
        (("mfa",), 0, True, True, False),  # and the password
        (("password", "mfa"), 0, True, True, True),
        (("password", "mfa"), 301, True, True, False),
        (("password", "mfa"), -60, True, True, False),  # a time ahead of now is not trusted
    ],
)
def test_current_needs_every_factor_the_account_has_within_the_window(
    person, methods, ago, reauthenticated, second_factor, expected
):
    if second_factor:
        with_authenticator(person)
    http = http_for(person)
    records(http, person, *methods, ago=ago, reauthenticated=reauthenticated)
    assert confirmation.current(request_for(http, person)) is expected


def test_records_of_another_account_do_not_count(person, make_user):
    other = make_user("other", "technical")
    http = http_for(person)
    records(http, other, "password")
    assert confirmation.current(request_for(http, person)) is False


def test_an_account_with_nothing_to_confirm_with_is_never_current(person):
    person.set_unusable_password()
    person.save(update_fields=["password"])
    http = http_for(person)
    records(http, person, "password", "mfa")
    assert confirmation.current(request_for(http, person)) is False


def test_an_error_report_leaves_out_what_was_typed(owner):
    from allauth.core.context import request_context
    from django.test import RequestFactory
    from django.views.debug import SafeExceptionReporterFilter

    from core.lab_profile_forms import SessionLimitsForm

    request = RequestFactory().post(
        "/", {"confirm_password": PASSWORD, "confirm_code": "123456", "reason": "Kept."}
    )
    request.user = owner
    request.session = {}
    with request_context(request):
        SessionLimitsForm(request.POST)
    shown = SafeExceptionReporterFilter().get_post_parameters(request)
    assert shown["reason"] == "Kept."
    assert PASSWORD not in str(dict(shown)) and "123456" not in str(dict(shown))


# --- an account's page ---------------------------------------------------------------------------


def test_an_unconfirmed_account_save_asks_and_changes_nothing(owner, person):
    http = http_for(owner)
    page = http.get(reverse("admin:accounts_user_change", args=[person.pk]))
    html = page.content.decode()
    assert "Confirm it is you" in html and 'name="confirm_password"' in html
    assert 'name="confirm_code"' not in html  # the superuser has no authenticator
    response = account_change(
        http,
        person,
        groups=[
            Group.objects.get(name="technical").pk,
            Group.objects.get_or_create(name="admin")[0].pk,
        ],
    )
    assert response.status_code == 200
    assert "Enter your password to save this change." in response.content.decode()
    person.refresh_from_db()
    assert roles_of(person) == {"technical"}
    assert not AuditEvent.objects.filter(action="account.updated").exists()
    assert not LogEntry.objects.exists()


def test_a_confirmed_account_save_saves_and_records_nothing_typed(owner, person, caplog):
    admin_role = Group.objects.get_or_create(name="admin")[0]
    technical = Group.objects.get(name="technical")
    http = http_for(owner)
    with caplog.at_level(logging.DEBUG):
        response = account_change(
            http, person, groups=[technical.pk, admin_role.pk], confirm_password=PASSWORD
        )
    assert response.status_code == 302, response.content.decode()[:2000]
    assert roles_of(person) == {"technical", "admin"}
    event = AuditEvent.objects.get(action="account.updated")
    assert PASSWORD not in str(event.before) + str(event.after) + str(event.reason)
    entry = LogEntry.objects.get()
    assert "password" not in entry.get_change_message().lower()
    assert PASSWORD not in caplog.text
    # Not asked again within the window.
    page = http.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert 'name="confirm_password"' not in page
    response = account_change(http, person, first_name="Pat")
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.first_name == "Pat"


def test_a_wrong_password_saves_nothing_and_is_a_failed_re_authentication(owner, person):
    http = http_for(owner)
    response = account_change(http, person, first_name="Pat", confirm_password=WRONG)
    assert response.status_code == 200
    assert "Incorrect password." in response.content.decode()
    person.refresh_from_db()
    assert person.first_name == ""
    assert sign_in_failures(owner) == [("reauth_failed", "password")]
    assert sign_in_failures(person) == []
    assert WRONG not in str(list(SignInEvent.objects.values()))


def test_an_authenticator_is_asked_for_too(owner, person):
    secret = with_authenticator(owner)
    http = http_for(owner)
    page = http.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert 'name="confirm_password"' in page and 'name="confirm_code"' in page
    response = account_change(http, person, first_name="Pat", confirm_password=PASSWORD)
    assert response.status_code == 200
    assert "Enter a code from your authenticator app" in response.content.decode()
    response = account_change(
        http, person, first_name="Pat", confirm_password=PASSWORD, confirm_code="000000"
    )
    assert response.status_code == 200 and "Incorrect code." in response.content.decode()
    assert ("reauth_failed", "second_factor") in sign_in_failures(owner)
    person.refresh_from_db()
    assert person.first_name == ""
    response = account_change(
        http, person, first_name="Pat", confirm_password=PASSWORD, confirm_code=code_for(secret)
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.first_name == "Pat"
    kinds = {
        (item["method"], bool(item.get("reauthenticated")))
        for item in http.session[AUTHENTICATION_METHODS_SESSION_KEY]
    }
    assert kinds == {("password", True), ("mfa", True)}


def test_a_wrong_password_does_not_use_up_the_code(owner, person):
    secret = with_authenticator(owner)
    http = http_for(owner)
    code = code_for(secret)
    response = account_change(
        http, person, first_name="Pat", confirm_password=WRONG, confirm_code=code
    )
    assert response.status_code == 200
    response = account_change(
        http, person, first_name="Pat", confirm_password=PASSWORD, confirm_code=code
    )
    assert response.status_code == 302


def test_a_recovery_code_stands_in_for_the_app(owner, person):
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    with_authenticator(owner)
    codes = RecoveryCodes.activate(owner).get_unused_codes()
    http = http_for(owner)
    response = account_change(
        http, person, first_name="Pat", confirm_password=PASSWORD, confirm_code=codes[0]
    )
    assert response.status_code == 302
    assert (
        codes[0]
        not in RecoveryCodes(
            Authenticator.objects.get(user=owner, type="recovery_codes")
        ).get_unused_codes()
    )


def test_a_page_drawn_while_confirmed_asks_when_saved_after_it_lapsed(owner, person):
    http = http_for(owner)
    records(http, owner, "password")
    url = reverse("admin:accounts_user_change", args=[person.pk])
    form = http.get(url).context["adminform"].form
    assert not form.confirming
    records(http, owner, "password", ago=301)
    response = http.post(url, page_data(form, first_name="Typed name"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Enter your password to save this change." in html
    assert 'value="Typed name"' in html  # what was typed is kept
    person.refresh_from_db()
    assert person.first_name == ""


def test_an_authenticator_added_after_confirming_asks_again(owner, person):
    http = http_for(owner)
    assert (
        account_change(http, person, first_name="Pat", confirm_password=PASSWORD).status_code == 302
    )
    with_authenticator(owner)
    response = account_change(http, person, first_name="Sam")
    assert response.status_code == 200
    person.refresh_from_db()
    assert person.first_name == "Pat"


def test_allauth_re_authentication_counts(owner, person):
    http = http_for(owner)
    response = http.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert response.status_code == 302
    assert account_change(http, person, first_name="Pat").status_code == 302


def test_an_account_without_a_password_or_authenticator_cannot_save(owner, person):
    owner.set_unusable_password()
    owner.save(update_fields=["password"])
    http = HttpClient()
    http.force_login(owner)
    response = account_change(http, person, first_name="Pat")
    assert response.status_code == 200
    assert "Your account has no password to confirm it is you" in response.content.decode()
    person.refresh_from_db()
    assert person.first_name == ""


def test_too_many_wrong_passwords_are_refused_by_the_failed_sign_in_limit(owner, person):
    http = http_for(owner)
    for _ in range(5):
        account_change(http, person, first_name="Pat", confirm_password=WRONG)
    response = account_change(http, person, first_name="Pat", confirm_password=PASSWORD)
    assert response.status_code == 200
    assert "Too many failed" in response.content.decode()
    person.refresh_from_db()
    assert person.first_name == ""
    assert ("locked_out", "password") in sign_in_failures(owner)


def test_a_confirmation_accepted_beside_a_refused_field_is_kept(owner, person):
    http = http_for(owner)
    response = account_change(http, person, email="not an address", confirm_password=PASSWORD)
    assert response.status_code == 200
    assert 'name="confirm_password"' not in response.content.decode()
    assert account_change(http, person, first_name="Pat").status_code == 302


# --- the other account administration pages ----------------------------------------------------


def test_adding_an_account_asks(owner):
    http = http_for(owner)
    url = reverse("admin:accounts_user_add")
    assert 'name="confirm_password"' in http.get(url).content.decode()
    data = {
        "username": "newcomer",
        "email": "newcomer@example.invalid",
        "usable_password": "true",
        "password1": "a long synthetic password 5512",
        "password2": "a long synthetic password 5512",
    }
    assert http.post(url, data).status_code == 200
    assert not User.objects.filter(username="newcomer").exists()
    assert http.post(url, {**data, "confirm_password": PASSWORD}).status_code == 302
    assert User.objects.filter(username="newcomer").exists()


def test_another_accounts_password_page_asks(owner, person):
    http = http_for(owner)
    url = reverse("admin:auth_user_password_change", args=[person.pk])
    page = http.get(url).content.decode()
    assert 'name="confirm_password"' in page and "Confirm it is you" in page
    data = {
        "usable_password": "true",
        "password1": "a long synthetic password 5512",
        "password2": "a long synthetic password 5512",
        "set-password": "Change password",
    }
    assert http.post(url, data).status_code == 200
    person.refresh_from_db()
    assert person.check_password(PASSWORD)
    assert not AuditEvent.objects.filter(action="account.password_changed").exists()
    assert http.post(url, {**data, "confirm_password": PASSWORD}).status_code == 302
    person.refresh_from_db()
    assert person.check_password("a long synthetic password 5512")


def test_marking_a_password_compromised_asks(owner, person):
    from accounts.models import CompromisedPassword

    http = http_for(owner)
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = http.get(url)
    assert 'name="confirm_password"' in page.content.decode()
    data = {
        "reason": "Typed into a look-alike page.",
        "drawn": page.context["form"].initial["drawn"],
    }
    assert http.post(url, data).status_code == 200
    assert not CompromisedPassword.objects.exists()
    assert http.post(url, {**data, "confirm_password": PASSWORD}).status_code == 302
    assert CompromisedPassword.objects.filter(user=person, lifted_at=None).exists()


def test_removing_an_authenticator_asks(owner, person):
    with_authenticator(person)
    authenticator = Authenticator.objects.get(user=person, type="totp")
    http = http_for(owner)
    url = reverse("admin:mfa_authenticator_delete", args=[authenticator.pk])
    page = http.get(url).content.decode()
    assert 'name="confirm_password"' in page and "Yes, I’m sure" in page
    response = http.post(url, {"post": "yes"})
    assert response.status_code == 200
    assert "Enter your password to save this change." in response.content.decode()
    assert Authenticator.objects.filter(pk=authenticator.pk).exists()
    assert not AuditEvent.objects.filter(action="account.authenticator_removed").exists()
    response = http.post(url, {"post": "yes", "confirm_password": WRONG})
    assert (
        response.status_code == 200 and Authenticator.objects.filter(pk=authenticator.pk).exists()
    )
    response = http.post(url, {"post": "yes", "confirm_password": PASSWORD})
    assert response.status_code == 302
    assert not Authenticator.objects.filter(pk=authenticator.pk).exists()
    assert AuditEvent.objects.filter(action="account.authenticator_removed").count() == 1


def test_a_role_page_asks(owner):
    group = Group.objects.get_or_create(name="technical")[0]
    permission = Permission.objects.get(codename="view_patient_information")
    http = http_for(owner)
    url = reverse("admin:auth_group_change", args=[group.pk])
    page = http.get(url)
    assert 'name="confirm_password"' in page.content.decode()
    data = page_data(page.context["adminform"].form, permissions=[permission.pk])
    assert http.post(url, data).status_code == 200
    assert not group.permissions.exists()
    assert http.post(url, {**data, "confirm_password": PASSWORD}).status_code == 302
    assert list(group.permissions.all()) == [permission]
    assert "password" not in LogEntry.objects.get().get_change_message().lower()


def test_adding_a_role_asks(owner):
    http = http_for(owner)
    url = reverse("admin:auth_group_add")
    assert http.post(url, {"name": "Night shift"}).status_code == 200
    assert not Group.objects.filter(name="Night shift").exists()
    assert http.post(url, {"name": "Night shift", "confirm_password": PASSWORD}).status_code == 302
    assert Group.objects.filter(name="Night shift").exists()


def test_a_page_drawn_read_only_does_not_ask(owner):
    """The scheduled jobs' account's page saves nothing, so it offers no confirmation either."""
    from jobs.actor import system_account

    http = http_for(owner)
    page = http.get(reverse("admin:accounts_user_change", args=[system_account().pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Confirm it is you" not in html and 'name="confirm_password"' not in html


def test_ending_sessions_does_not_ask(owner, person):
    http = http_for(owner)
    url = reverse("admin:accounts_user_sessions", args=[person.pk])
    assert 'name="confirm_password"' not in http.get(url).content.decode()
    assert http.post(url, {"reason": "Lost phone."}).status_code == 302


# --- Security settings ---------------------------------------------------------------------------


def limits_data(**changes):
    return {
        "inactivity_minutes": "15",
        "maximum_hours": "8",
        "reason": "Front desk.",
        "expected_revision": "0",
        **changes,
    }


def test_session_limits_ask(lab_admin):
    http = http_for(lab_admin)
    url = reverse("session-limits-edit", args=["staff"])
    page = http.get(url).content.decode()
    assert "Confirm it is you" in page and 'name="confirm_password"' in page
    response = http.post(url, limits_data())
    assert response.status_code == 200
    assert not SessionLimit.objects.filter(audience="staff", revision__gt=0).exists()
    response = http.post(url, limits_data(confirm_password=PASSWORD))
    assert response.status_code == 302
    assert SessionLimit.objects.get(audience="staff").inactivity_minutes == 15
    event = AuditEvent.objects.get(action="lab.session_limits_changed")
    assert PASSWORD not in str(event.before) + str(event.after) + str(event.reason)


def test_session_limits_in_the_form_dialog_ask_in_the_dialog(lab_admin):
    http = http_for(lab_admin)
    url = reverse("session-limits-edit", args=["staff"])
    response = http.post(url, limits_data(), HTTP_X_VR_FORM="1")
    assert response.status_code == 200
    assert 'name="confirm_password"' in response.content.decode()
    response = http.post(url, limits_data(confirm_password=PASSWORD), HTTP_X_VR_FORM="1")
    assert response.status_code == 204 and response["HX-Redirect"] == reverse("security-settings")


def test_a_confirmation_beside_a_refused_limit_is_kept(lab_admin):
    http = http_for(lab_admin)
    url = reverse("session-limits-edit", args=["staff"])
    response = http.post(url, limits_data(inactivity_minutes="2", confirm_password=PASSWORD))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Choose from 5 to 60 minutes." in html and 'name="confirm_password"' not in html
    assert http.post(url, limits_data()).status_code == 302


def test_the_second_factor_rule_asks(lab_admin):
    http = http_for(lab_admin)
    url = reverse("second-factor-edit")
    assert 'name="confirm_password"' in http.get(url).content.decode()
    data = {"staff": "on", "reason": "Every staff account.", "expected_revision": "0"}
    assert http.post(url, data).status_code == 200
    assert not SecondFactorRule.objects.filter(staff=True).exists()
    assert http.post(url, {**data, "confirm_password": PASSWORD}).status_code == 302
    assert SecondFactorRule.objects.get(pk=1).staff is True


# --- who is asked ------------------------------------------------------------------------------


def test_no_password_is_checked_for_a_request_that_may_not_make_the_change(person, make_user):
    """A request refused for its access is refused before any confirmation is checked: no failed
    re-authentication is recorded and nothing is drawn."""
    staff_only = make_user("console", is_staff=True)
    target = make_user("target", "technical")
    http = http_for(staff_only)
    response = http.post(
        reverse("admin:accounts_user_change", args=[target.pk]),
        {"username": "target", "confirm_password": WRONG},
    )
    assert response.status_code in (302, 403)
    http = http_for(person)
    response = http.post(
        reverse("session-limits-edit", args=["staff"]), limits_data(confirm_password=WRONG)
    )
    assert response.status_code == 403
    response = http.post(reverse("second-factor-edit"), {"confirm_password": WRONG})
    assert response.status_code == 403
    assert SignInEvent.objects.exclude(kind__in=["signed_in", "signed_out"]).count() == 0
