"""The rule a new password must meet (accounts.passwords): its length by whether the laboratory
requires a second sign-in factor of the account, common and expected passwords refused as a
whole, no composition rule, and the guidance each password form shows for its account."""

import time

import pytest
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse

from accounts.models import User
from accounts.passwords import guidance, minimum_length
from clients.models import Client
from core.models import Lab, SecondFactorRule

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

# Fifteen characters with nothing common, expected or personal in them.
FIFTEEN = "vT4 quiet ember"
FOURTEEN = FIFTEEN[:-1]
EIGHT = "vT4 quie"
SEVEN = EIGHT[:-1]


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


def refusal(password, user=None):
    with pytest.raises(ValidationError) as caught:
        validate_password(password, user)
    return caught.value.messages


# --- length --------------------------------------------------------------------------------------


def test_a_password_alone_needs_fifteen_characters(make_user):
    person = make_user("tech_one", "technical")
    assert minimum_length(person) == 15
    assert refusal(FOURTEEN, person) == [
        "This password is too short. It must contain at least 15 characters."
    ]
    validate_password(FIFTEEN, person)


@pytest.mark.parametrize(
    ("parts", "roles", "extra"),
    [
        ({"staff": True}, ("technical",), {}),
        ({"roles": ["supervisor"]}, ("supervisor",), {}),
        ({"roles": ["technical"]}, ("admin",), {}),  # the admin role does every role's work
        ({"admin_console": True}, (), {"is_staff": True}),
    ],
)
def test_eight_characters_where_the_rule_requires_a_second_factor(make_user, parts, roles, extra):
    person = make_user("covered", *roles, **extra)
    require(**parts)
    assert minimum_length(person) == 8
    assert refusal(SEVEN, person) == [
        "This password is too short. It must contain at least 8 characters."
    ]
    validate_password(EIGHT, person)


def test_portal_accounts_under_the_portal_part_need_eight(make_user):
    organization = Client.objects.create(name="Example Clinic")
    person = make_user("portal_person", client=organization)
    assert minimum_length(person) == 15
    require(portal=True)
    assert minimum_length(person) == 8
    require(staff=True)
    assert minimum_length(person) == 15


def test_the_patient_information_part_counts_a_grant_through_a_group(make_user):
    from django.contrib.auth.models import Permission

    person = make_user("reader", "technical")
    require(patient_information=True)
    assert minimum_length(person) == 15
    group = Group.objects.get(name="technical")
    group.permissions.add(Permission.objects.get(codename="view_patient_information"))
    assert minimum_length(person) == 8


def test_the_length_follows_the_rule_and_account_as_they_stand_when_checked(make_user):
    person = make_user("changing", "technical")
    require(staff=True)
    validate_password(EIGHT, person)
    require()  # the rule stops covering the account
    assert refusal(EIGHT, person) == [
        "This password is too short. It must contain at least 15 characters."
    ]
    require(staff=True)
    User.objects.filter(pk=person.pk).update(is_active=False)
    assert minimum_length(person) == 15  # the rule covers active accounts only


def test_an_account_not_yet_saved_or_unknown_needs_fifteen():
    require(staff=True, admin_console=True)
    unsaved = User(username="newcomer", email="newcomer@example.invalid", is_staff=True)
    assert minimum_length(unsaved) == 15
    assert minimum_length(None) == 15
    assert "at least 15 characters" in refusal(FOURTEEN, unsaved)[0]
    assert "at least 15 characters" in refusal(FOURTEEN)[0]


def test_each_code_point_counts_once_and_spaces_count():
    validate_password("ünïcødé wörds ok")  # 16 code points
    validate_password("  vT4 quiet emb ")  # spaces at either end are characters too
    assert len("ünïcødé wörds o") == 15
    validate_password("ünïcødé wörds o")


def test_no_composition_rule_and_no_upper_limit():
    validate_password("839201746258391")  # digits only
    validate_password("quiet ember lantern")  # lower-case letters and spaces only
    validate_password(
        "a long passphrase about a quiet ember that glows near the lantern by the old mill, "
        "written out in full so that it runs well past sixty-four characters"
    )


# --- common and expected passwords --------------------------------------------------------------


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("1q2w3e4r5t6y7u8i", "This password is too common."),
        ("Password1234567!", "common password with only digits"),
        ("Dragon 8472 9103 65", "common password with only digits"),
        ("iloveyou iloveyou", "common password with only digits"),
        ("OpenBGA-Lab-Ops-2026", "a name of this laboratory, this application or the account"),
        ("LabOps!2026!2026!", "a name of this laboratory, this application or the account"),
        ("aaaaaaaaaaaaaaaa", "repeats one character or a short text"),
        ("xK9#m xK9#m xK9#m", "repeats one character or a short text"),
        ("Xk9#mQ2!Xk9#mQ2!", "repeats one character or a short text"),
        ("123456789012345", "a run of letters, digits or keyboard keys"),
        ("5432109876543210", "a run of letters, digits or keyboard keys"),
        ("abcd efgh ijkl mnop", "a run of letters, digits or keyboard keys"),
        ("qwertyuiopasdfg", "a run of letters, digits or keyboard keys"),
        ("ZYXWVUTSRQPONML", "a run of letters, digits or keyboard keys"),
    ],
)
def test_common_and_expected_passwords_are_refused(password, code):
    assert any(code in message for message in refusal(password))


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",  # a common word inside a passphrase is not compared alone
        "monkey business at the lake",
        "tr0ub4dor&3-extra-long",
        "839201746258391",
    ],
)
def test_the_whole_password_is_compared_not_a_word_inside_it(password):
    validate_password(password)


def test_the_laboratorys_and_the_accounts_names_are_refused_with_digits_added(make_user):
    Lab.objects.update_or_create(pk=1, defaults={"name": "Riverside Testing Laboratory"})
    person = make_user("mquinlan", "technical", first_name="Marisol", last_name="Quinlan")
    User.objects.filter(pk=person.pk).update(email="mq.lab@example.invalid")
    person.refresh_from_db()
    for password in (
        "Riverside Testing Laboratory 1",
        "MQuinlan-2026-2026",
        "marisol.quinlan!!!2026",
        "QuinlanMarisol 2026",
        "mq.lab 2026 2026 2026",
    ):
        messages = refusal(password, person)
        assert any("a name of this laboratory" in message for message in messages), password
    validate_password("Riverside quiet ember lantern", person)  # the name is not the whole


def test_guidance_states_the_length_for_the_account(make_user):
    person = make_user("guided", "technical")
    assert guidance(person, own=True)[0] == "Your password must contain at least 15 characters."
    require(staff=True)
    assert guidance(person, own=True)[0] == (
        "Your password must contain at least 8 characters, because the laboratory requires a "
        "second sign-in factor of your account."
    )
    assert guidance(person, own=False)[0].endswith("second sign-in factor of this account.")
    assert guidance(None, own=False)[0] == "The password must contain at least 15 characters."
    assert "unrelated words" in guidance(person, own=True)[-1]


def test_a_very_long_password_is_checked_in_proportionate_time():
    started = time.monotonic()
    with pytest.raises(ValidationError):
        validate_password("ab" * 250_000)
    validate_password("ab" * 250_000 + "c")  # no shorter text repeats all of it
    assert time.monotonic() - started < 10


# --- the forms ----------------------------------------------------------------------------------


def test_change_password_page_states_the_rule_and_checks_it_at_the_save(make_user, client):
    person = make_user("changer", "technical")
    client.force_login(person)
    url = reverse("account_change_password")
    page = client.get(url).content.decode()
    assert "Your password must contain at least 15 characters." in page
    assert "unrelated words" in page

    require(staff=True)
    page = client.get(url).content.decode()
    assert "at least 8 characters, because the laboratory requires" in page

    require()  # changed after the page was drawn: the save checks the rule as it stands
    response = client.post(
        url, {"oldpassword": PASSWORD, "password1": EIGHT + "xy", "password2": EIGHT + "xy"}
    )
    assert response.status_code == 200
    assert "It must contain at least 15 characters." in response.content.decode()
    person.refresh_from_db()
    assert person.check_password(PASSWORD)

    response = client.post(
        url, {"oldpassword": PASSWORD, "password1": FIFTEEN, "password2": FIFTEEN}
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(FIFTEEN)


def test_a_covered_account_may_choose_eight(make_user, client):
    from allauth.mfa.models import Authenticator

    person = make_user("covered_changer", "technical")
    Authenticator.objects.create(user=person, type=Authenticator.Type.TOTP, data={})
    require(staff=True)
    client.force_login(person)
    response = client.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": EIGHT, "password2": EIGHT},
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(EIGHT)


def test_a_password_chosen_before_still_signs_in(make_user, client):
    person = make_user("earlier", "technical")
    User.objects.filter(pk=person.pk).update(password="")
    person.set_password("short-1")  # set directly, as before this rule
    person.save(update_fields=["password"])
    response = client.post(reverse("account_login"), {"login": "earlier", "password": "short-1"})
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(person.pk)


def test_reset_by_email_link_states_and_checks_the_rule(make_user, client, settings):
    from allauth.account.forms import default_token_generator
    from allauth.account.utils import user_pk_to_url_str

    person = make_user("resetter", "technical")
    key = default_token_generator.make_token(person)
    url = reverse(
        "account_reset_password_from_key",
        kwargs={"uidb36": user_pk_to_url_str(person), "key": key},
    )
    response = client.get(url, follow=True)
    page = response.content.decode()
    assert "Your password must contain at least 15 characters." in page
    form_url = response.redirect_chain[-1][0] if response.redirect_chain else url
    response = client.post(form_url, {"password1": FOURTEEN, "password2": FOURTEEN})
    assert "It must contain at least 15 characters." in response.content.decode()
    response = client.post(form_url, {"password1": FIFTEEN, "password2": FIFTEEN})
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(FIFTEEN)


@pytest.fixture
def owner(make_user):
    return make_user("owner", is_staff=True, is_superuser=True)


def test_admin_password_page_states_the_rule_for_that_account(owner, make_user, client):
    person = make_user("managed", "technical")
    client.force_login(owner)
    url = reverse("admin:auth_user_password_change", args=[person.pk])
    page = client.get(url).content.decode()
    assert "The password must contain at least 15 characters." in page
    require(staff=True)
    page = client.get(url).content.decode()
    assert (
        "at least 8 characters, because the laboratory requires a second sign-in factor of "
        "this account." in page
    )
    response = client.post(url, {"usable_password": "true", "password1": SEVEN, "password2": SEVEN})
    assert response.status_code == 200
    assert "It must contain at least 8 characters." in response.content.decode()
    response = client.post(url, {"usable_password": "true", "password1": EIGHT, "password2": EIGHT})
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(EIGHT)


def test_admin_own_password_and_new_account_forms_state_the_rule(owner, client):
    client.force_login(owner)
    page = client.get(reverse("admin:password_change")).content.decode()
    assert "Your password must contain at least 15 characters." in page
    require(staff=True, admin_console=True)
    page = client.get(reverse("admin:accounts_user_add")).content.decode()
    assert "The password must contain at least 15 characters." in page
    response = client.post(
        reverse("admin:accounts_user_add"),
        {
            "username": "newcomer",
            "email": "newcomer@example.invalid",
            "usable_password": "true",
            "password1": EIGHT,
            "password2": EIGHT,
        },
    )
    assert response.status_code == 200
    assert "It must contain at least 15 characters." in response.content.decode()
    assert not User.objects.filter(username="newcomer").exists()


def test_setup_lab_holds_the_first_administrator_to_fifteen(monkeypatch):
    from io import StringIO

    from django.core.management import CommandError, call_command

    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", FOURTEEN)
    argv = [
        "--lab-name=Example Laboratory",
        "--admin-username=first_admin",
        "--admin-email=first-admin@example.invalid",
    ]
    with pytest.raises(CommandError, match="at least 15 characters"):
        call_command("setup_lab", *argv, stdout=StringIO())
    assert not User.objects.filter(username="first_admin").exists()
    monkeypatch.setenv("SETUP_ADMIN_PASSWORD", FIFTEEN)
    call_command("setup_lab", *argv, stdout=StringIO())
    assert User.objects.get(username="first_admin").check_password(FIFTEEN)
