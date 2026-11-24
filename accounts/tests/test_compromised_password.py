"""A password marked as compromised (accounts.compromised): marking it from the account
administration, what a sign-in with it can reach, choosing a new one, and what lifts the mark."""

import json

import pytest
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import compromised, sessions
from accounts.models import AccountSession, CompromisedPassword, User
from accounts.passwords import guidance
from core.models import AuditEvent

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db

Ending = AccountSession.Ending
NEW_PAGE = "/account/new-password/"
NEW_PASSWORD = "copper lantern orchard seventeen"
OTHER_PASSWORD = "quiet harbour violet ninety"


@pytest.fixture(autouse=True)
def fresh_limits(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(db):
    return User.objects.create_superuser("mark-owner", "mark-owner@example.invalid", "x-pass-81!")


@pytest.fixture
def person(make_user):
    return make_user("marked-person", "client_services")


def admin_browser(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def signed_in(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def row_of(browser):
    return AccountSession.objects.get(pk=browser.session[sessions.SESSION_KEY])


def mark_url(user):
    return reverse("admin:accounts_user_compromised", args=[user.pk])


def mark(owner, user, reason="Typed into a page posing as the laboratory's"):
    """Mark through the admin page, as a superuser does: the page is drawn, then saved."""
    browser = admin_browser(owner)
    page = browser.get(mark_url(user))
    assert page.status_code == 200, page.status_code
    drawn = page.context["form"].initial["drawn"]
    response = browser.post(mark_url(user), {"reason": reason, "drawn": drawn})
    return browser, response


def sign_in(browser, user, password=PASSWORD, next_page=None):
    url = reverse("account_login") + (f"?next={next_page}" if next_page else "")
    return browser.post(url, {"login": user.username, "password": password})


def code_for(secret):
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def choose(browser, person, password, confirmation=None):
    """Post Choose a new password as the browser does: with the mark the page is drawn for."""
    latest = CompromisedPassword.objects.filter(user_id=person.pk).order_by("-pk").first()
    return browser.post(
        NEW_PAGE,
        {
            "new_password1": password,
            "new_password2": password if confirmation is None else confirmation,
            "mark": str(latest.pk) if latest else "",
        },
    )


def notices(browser):
    return [str(m) for m in browser.get(reverse("account_login")).context["messages"]]


def sign_in_with_code(browser, user, secret, next_page=None):
    response = sign_in(browser, user, next_page=next_page)
    assert response.url == reverse("mfa_authenticate")
    return browser.post(response.url, {"code": code_for(secret)})


# --- Marking ----------------------------------------------------------------------------------


def test_the_account_page_shows_whether_the_password_is_marked_and_links_to_mark_it(owner, person):
    browser = admin_browser(owner)
    page = browser.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert "Not marked." in page and mark_url(person) in page
    mark(owner, person)
    page = browser.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert "Marked on" in page and "by mark-owner" in page and mark_url(person) not in page


def test_a_superuser_marks_a_password_with_a_reason(owner, person):
    one, two = signed_in(person), signed_in(person)
    browser, response = mark(owner, person)
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:accounts_user_change", args=[person.pk])
    entry = CompromisedPassword.objects.get(user=person)
    person.refresh_from_db()
    assert entry.marked_by == owner and entry.lifted_at is None
    assert entry.password == person.password  # the copy refuses it as a new password
    assert entry.reason == "Typed into a page posing as the laboratory's"
    for b in (one, two):
        assert row_of(b).ending == Ending.MARKED_COMPROMISED
    event = AuditEvent.objects.get(action=compromised.ACTION)
    assert event.actor == owner and event.object_id == str(person.pk)
    assert event.reason == entry.reason
    assert event.after == {
        "mark": entry.pk,
        "sessions": sorted([str(row_of(one).pk), str(row_of(two).pk)]),
        "count": 2,
    }
    assert person.password not in json.dumps(event.after) + json.dumps(event.before)
    assert LogEntry.objects.filter(
        object_id=str(person.pk), change_message="Marked the password as compromised."
    ).exists()
    text = [str(m) for m in browser.get(response["Location"]).context["messages"]]
    assert any("2 sessions ended." in t and "Forgot your password?" in t for t in text)
    # The superuser's own session is not touched.
    assert row_of(browser).ended_at is None
    assert browser.get(reverse("admin:index")).status_code == 200
    # Each ended browser is told why at its next request.
    assert one.get("/")["Location"].startswith(reverse("account_login"))
    assert notices(one) == [sessions.ENDED_BY_MESSAGES[Ending.MARKED_COMPROMISED]]
    assert mail.outbox == []  # nobody is emailed


def test_marking_needs_a_reason(owner, person):
    browser = admin_browser(owner)
    drawn = browser.get(mark_url(person)).context["form"].initial["drawn"]
    for reason in ("", "   "):
        refused = browser.post(mark_url(person), {"reason": reason, "drawn": drawn})
        assert refused.status_code == 200 and refused.context["form"].errors["reason"]
    assert not CompromisedPassword.objects.exists()
    assert not AuditEvent.objects.filter(action=compromised.ACTION).exists()


@pytest.mark.parametrize("case", ["own", "shared", "unusable", "already"])
def test_passwords_that_cannot_be_marked_are_refused(owner, person, case):
    target = person
    if case == "own":
        target = owner
    elif case == "shared":
        User.objects.filter(pk=person.pk).update(credentials_locked=True)
    elif case == "unusable":
        person.set_unusable_password()
        person.save(update_fields=["password"])
    elif case == "already":
        mark(owner, person)
    expected = {
        "own": compromised.REFUSALS["own"],
        "shared": compromised.REFUSALS["shared"],
        "unusable": compromised.REFUSALS["unusable"],
        "already": compromised.ALREADY_MARKED,
    }[case]
    count = CompromisedPassword.objects.count()
    browser = admin_browser(owner)
    page = browser.get(mark_url(target))
    assert page.status_code == 200 and page.context["refused"] == expected
    assert 'name="reason"' not in page.content.decode()
    # A save sent anyway (a page drawn before the change) is refused before its form is checked
    # (so no confirmation is asked for); the service refuses it too when it saves.
    drawn = compromised.drawn_password(User.objects.get(pk=target.pk))
    response = browser.post(mark_url(target), {"reason": "Leaked", "drawn": drawn})
    assert response.status_code == 200
    assert response.context["refused"] == expected
    assert 'name="reason"' not in response.content.decode()
    assert CompromisedPassword.objects.count() == count
    with pytest.raises(ValidationError) as refused, transaction.atomic():
        compromised.mark_compromised(
            actor=owner, account_id=target.pk, reason="Leaked", drawn=drawn
        )
    assert expected in refused.value.messages
    assert CompromisedPassword.objects.count() == count


def test_the_scheduled_jobs_account_has_no_mark_page(owner):
    from jobs.actor import system_account

    jobs = system_account()
    browser = admin_browser(owner)
    assert browser.get(mark_url(jobs)).status_code == 403
    page = browser.get(reverse("admin:accounts_user_change", args=[jobs.pk])).content.decode()
    assert mark_url(jobs) not in page


def test_only_a_superuser_with_staff_status_marks_a_password(owner, person, make_user):
    lab_admin = make_user("lab-admin", "admin")
    User.objects.filter(pk=lab_admin.pk).update(is_staff=True)
    assert admin_browser(lab_admin).get(mark_url(person)).status_code in (302, 403)
    with pytest.raises(PermissionDenied):
        compromised.mark_compromised(
            actor=lab_admin,
            account_id=person.pk,
            reason="Leaked",
            drawn=compromised.drawn_password(person),
        )
    assert not CompromisedPassword.objects.exists()


def test_a_superuser_who_stepped_down_after_opening_the_page_cannot_mark(owner, person, make_user):
    make_user("second-owner", is_superuser=True, is_staff=True)
    browser = admin_browser(owner)
    drawn = browser.get(mark_url(person)).context["form"].initial["drawn"]
    User.objects.filter(pk=owner.pk).update(is_superuser=False)
    response = browser.post(mark_url(person), {"reason": "Leaked", "drawn": drawn})
    assert response.status_code in (302, 403)
    assert not CompromisedPassword.objects.exists()


def test_a_password_changed_after_the_page_opened_is_not_marked(owner, person):
    browser = admin_browser(owner)
    drawn = browser.get(mark_url(person)).context["form"].initial["drawn"]
    # The person, told of the leak, changes it before the superuser saves.
    person.set_password(NEW_PASSWORD)
    person.save(update_fields=["password"])
    response = browser.post(mark_url(person), {"reason": "Leaked", "drawn": drawn})
    assert response.status_code == 200
    assert compromised.CHANGED_SINCE_DRAWN in [
        str(e) for e in response.context["form"].non_field_errors()
    ]
    assert not CompromisedPassword.objects.exists()


def test_a_forged_or_missing_fingerprint_marks_nothing(owner, person):
    browser = admin_browser(owner)
    for drawn in ("0" * 64, "x"):
        response = browser.post(mark_url(person), {"reason": "Leaked", "drawn": drawn})
        assert response.status_code == 200
    response = browser.post(mark_url(person), {"reason": "Leaked"})
    assert response.status_code == 200 and response.context["form"].errors
    assert not CompromisedPassword.objects.exists()


def test_the_fingerprint_reveals_nothing_of_the_password(person):
    drawn = compromised.drawn_password(person)
    assert person.password not in drawn and len(drawn) == 64
    assert drawn != person.get_session_auth_hash()


def test_the_page_says_what_the_person_must_do(owner, person, settings):
    browser = admin_browser(owner)
    page = browser.get(mark_url(person)).content.decode()
    assert "has no second factor" in page and "you set one on the account" in page
    TOTP.activate(person, generate_totp_secret())
    page = browser.get(mark_url(person)).content.decode()
    assert "has an authenticator app" in page
    marker, response = mark(owner, person)
    text = [str(m) for m in marker.get(response["Location"]).context["messages"]]
    assert any("authenticator code" in t and "No session was open." in t for t in text)


# --- Signing in while marked ------------------------------------------------------------------


def test_the_password_alone_no_longer_signs_in(owner, person):
    mark(owner, person)
    last_login = User.objects.get(pk=person.pk).last_login
    browser = BrowserClient()
    response = sign_in(browser, person)
    assert response.status_code == 302
    row = row_of(browser)
    assert row.ending == Ending.MARKED_COMPROMISED and row.ended_at is not None
    landed = browser.get("/")
    assert landed["Location"].startswith(reverse("account_login"))
    messages = notices(browser)
    assert messages == [sessions.ENDED_BY_MESSAGES[Ending.MARKED_COMPROMISED]]
    assert User.objects.get(pk=person.pk).last_login == last_login
    # Not even the page the sign-in would have opened next.
    assert browser.get(NEW_PAGE)["Location"].startswith(reverse("account_login"))


def test_a_wrong_password_is_refused_as_usual(owner, person):
    mark(owner, person)
    browser = BrowserClient()
    response = sign_in(browser, person, password="not the password at all")
    assert response.status_code == 200
    assert not AccountSession.objects.filter(user=person).exists()


def test_a_sign_in_with_a_second_factor_reaches_only_the_new_password_page(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mark(owner, person)
    browser = BrowserClient()
    response = sign_in_with_code(browser, person, secret, next_page="/requests/")
    assert response.status_code == 302
    assert row_of(browser).ended_at is None
    held = browser.get("/requests/")
    assert held.status_code == 302 and held["Location"] == NEW_PAGE
    page = browser.get(NEW_PAGE)
    assert page.status_code == 200 and page.context["password_change_pending"] is True
    text = page.content.decode()
    assert 'id="vr-nav"' not in text and 'class="vr-thumbbar"' not in text
    for route in ("account-sessions", "account_change_password", "mfa_index", "whats-new"):
        assert f'href="{reverse(route)}"' not in text
    # A script, a partial update and the form dialog are told where to go.
    script = browser.get("/requests/", headers={"Sec-Fetch-Dest": "empty"})
    assert script.status_code == 403
    assert json.loads(script.content) == {"password_change_required": True, "location": NEW_PAGE}
    partial = browser.get("/requests/", headers={"HX-Request": "true"})
    assert partial.status_code == 204 and partial["HX-Redirect"] == NEW_PAGE
    dialog = browser.get(reverse("desk:request-create"), headers={"X-VR-Form": "1"})
    assert dialog.status_code == 204 and dialog["HX-Redirect"] == NEW_PAGE
    # A form posted without script is sent there too, and changes nothing.
    posted = browser.post(reverse("desk:request-create"), {"subject": "x"})
    assert posted.status_code == 302 and posted["Location"] == NEW_PAGE
    # Signing out and the session's own status still work.
    assert browser.get(reverse("account-session")).status_code == 200
    assert browser.get(reverse("mfa_index"))["Location"] == NEW_PAGE
    assert browser.get(reverse("account_change_password"))["Location"] == NEW_PAGE
    assert browser.get(reverse("account-sessions"))["Location"] == NEW_PAGE


def test_choosing_a_new_password_lifts_the_mark_and_goes_on(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    other = signed_in(person)  # ended by the mark below
    mark(owner, person)
    browser = BrowserClient()
    sign_in_with_code(browser, person, secret)
    browser.get("/requests/")  # remembered for after
    short = choose(browser, person, "short")
    assert short.status_code == 200 and short.context["form"].errors
    same = choose(browser, person, PASSWORD)
    assert same.status_code == 200
    assert "marked as compromised" in str(same.context["form"].errors)
    saved = choose(browser, person, NEW_PASSWORD)
    assert saved.status_code == 302 and saved["Location"] == "/requests/"
    entry = CompromisedPassword.objects.get(user=person)
    assert entry.lifted_at is not None
    person.refresh_from_db()
    assert person.check_password(NEW_PASSWORD)
    # This session goes on; the page no longer holds it.
    assert browser.get("/requests/").status_code == 200
    assert browser.get(NEW_PAGE)["Location"] == "/"
    assert row_of(other).ending == Ending.MARKED_COMPROMISED
    event = AuditEvent.objects.get(action=compromised.CHANGED_ACTION, object_id=str(person.pk))
    assert event.actor == person and event.after == {"compromised_mark": entry.pk}
    assert NEW_PASSWORD not in json.dumps(event.after)
    # The new password signs in again, with the account's second factor as usual.
    again = BrowserClient()
    assert sign_in(again, person, password=NEW_PASSWORD).url == reverse("mfa_authenticate")


def test_a_sign_in_code_emailed_to_the_account_reaches_the_new_password_page(
    owner, person, settings
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark(owner, person)
    browser = BrowserClient()
    request_code(browser, person.email)
    response = browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert response.status_code == 302
    assert row_of(browser).ended_at is None
    assert browser.get("/")["Location"] == NEW_PAGE
    saved = choose(browser, person, NEW_PASSWORD)
    assert saved.status_code == 302
    assert CompromisedPassword.objects.get(user=person).lifted_at is not None


def test_a_mark_made_during_a_sign_in_holds_the_session_that_proved_a_second_factor(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    browser = BrowserClient()
    response = sign_in(browser, person)
    assert response.url == reverse("mfa_authenticate")
    mark(owner, person)  # between the password and the code
    browser.post(response.url, {"code": code_for(secret)})
    assert row_of(browser).ended_at is None
    assert browser.get("/")["Location"] == NEW_PAGE


def test_a_sign_in_that_started_before_the_mark_with_the_password_alone_starts_ended(
    owner, person, rf
):
    """The sign-in's password check passed before the mark; its session starts after it."""
    mark(owner, person)
    request = rf.get("/")
    request.session = BrowserClient().session
    sessions.start_session(None, request, person)
    row = AccountSession.objects.get(user=person, pk=request.session[sessions.SESSION_KEY])
    assert row.ending == Ending.MARKED_COMPROMISED


def test_a_session_waiting_for_a_required_second_factor_sets_it_up_first(owner, person, settings):
    from core.models import SecondFactorRule

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark(owner, person)
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["client_services"]})
    browser = BrowserClient()
    request_code(browser, person.email)
    browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert browser.get("/")["Location"] == "/account/second-factor/"
    assert browser.get(NEW_PAGE)["Location"] == "/account/second-factor/"
    TOTP.activate(person, generate_totp_secret())
    assert browser.get("/")["Location"] == NEW_PAGE


def test_a_portal_account_is_held_in_its_own_shell(owner, make_user):
    from clients.models import Client

    organization = Client.objects.create(name="Example Clinic")
    portal = make_user("marked-portal", client=organization)
    secret = generate_totp_secret()
    TOTP.activate(portal, secret)
    mark(owner, portal)
    browser = BrowserClient()
    sign_in_with_code(browser, portal, secret)
    assert browser.get(reverse("portal:home"))["Location"] == NEW_PAGE
    page = browser.get(NEW_PAGE)
    assert page.status_code == 200 and 'class="vr-portal-nav"' not in page.content.decode()


def test_a_shared_demonstration_account_held_by_a_mark_cannot_change_its_password(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mark(owner, person)
    User.objects.filter(pk=person.pk).update(credentials_locked=True)
    browser = BrowserClient()
    sign_in_with_code(browser, person, secret)
    page = browser.get(NEW_PAGE)
    assert page.status_code == 200 and page.context["form"] is None
    choose(browser, person, NEW_PASSWORD)
    assert not User.objects.get(pk=person.pk).check_password(NEW_PASSWORD)
    assert CompromisedPassword.objects.get(user=person).lifted_at is None


# --- What lifts the mark ----------------------------------------------------------------------


def test_a_password_a_superuser_sets_lifts_the_mark(owner, person):
    browser, _ = mark(owner, person)
    url = reverse("admin:auth_user_password_change", args=[person.pk])
    response = browser.post(
        url,
        {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD, "usable_password": "true"},
    )
    assert response.status_code == 302, response.content[:400]
    assert CompromisedPassword.objects.get(user=person).lifted_at is not None
    assert sign_in(BrowserClient(), person, password=NEW_PASSWORD).status_code == 302


def test_a_reset_through_an_emailed_link_lifts_the_mark(owner, person):
    mark(owner, person)
    browser = BrowserClient()
    browser.post(reverse("account_reset_password"), {"email": person.email})
    link = next(
        line for line in mail.outbox[-1].body.splitlines() if "/password/reset/key/" in line
    )
    path = link.strip()[link.strip().index("/accounts/") :]
    form_page = browser.get(path, follow=True)
    action = form_page.redirect_chain[-1][0] if form_page.redirect_chain else path
    response = browser.post(action, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert response.status_code == 302, response.content[:400]
    assert CompromisedPassword.objects.get(user=person).lifted_at is not None
    refused = BrowserClient()
    response = sign_in(refused, person, password=NEW_PASSWORD)
    assert row_of(refused).ended_at is None


def test_a_hash_upgraded_at_sign_in_lifts_nothing(owner, person):
    mark(owner, person)
    stored = User.objects.get(pk=person.pk)
    # As Django does when a sign-in finds an outdated hash: same password, new hash.
    stored.check_password(PASSWORD)
    stored.set_password(PASSWORD)
    stored._password = None
    stored.save(update_fields=["password"])
    assert CompromisedPassword.objects.get(user=person).lifted_at is None


def test_an_unusable_password_lifts_nothing(owner, person):
    mark(owner, person)
    person.refresh_from_db()
    person.set_unusable_password()
    person.save(update_fields=["password"])
    assert CompromisedPassword.objects.get(user=person).lifted_at is None


def test_a_password_can_be_marked_again_after_a_new_one(owner, person):
    mark(owner, person)
    person.refresh_from_db()
    person.set_password(NEW_PASSWORD)
    person.save(update_fields=["password"])
    mark(owner, person, reason="Leaked again")
    assert CompromisedPassword.objects.filter(user=person).count() == 2
    assert CompromisedPassword.objects.filter(user=person, lifted_at__isnull=True).count() == 1


# --- The rule for a new password --------------------------------------------------------------


def test_every_marked_password_of_the_account_is_refused_as_a_new_one(owner, person, make_user):
    mark(owner, person)
    person.refresh_from_db()
    person.set_password(NEW_PASSWORD)
    person.save(update_fields=["password"])
    with pytest.raises(ValidationError) as refused:
        validate_password(PASSWORD, person)
    assert [e.code for e in refused.value.error_list] == ["password_marked_compromised"]
    validate_password(OTHER_PASSWORD, person)
    # Another account is not held to this one's marks.
    validate_password(PASSWORD, make_user("unrelated-person"))


def test_the_guidance_names_the_marked_password_only_where_there_is_one(owner, person, make_user):
    line = "Your password can’t be one that was marked as compromised for your account."
    assert line not in guidance(person, own=True)
    mark(owner, person)
    assert line in guidance(person, own=True)
    assert "The password can’t be one that was marked as compromised for the account." in (
        guidance(person, own=False)
    )
    assert line not in guidance(make_user("someone-else"), own=True)


def test_the_marked_hash_is_not_in_the_admin_console(owner, person):
    mark(owner, person)
    entry = CompromisedPassword.objects.get(user=person)
    browser = admin_browser(owner)
    page = browser.get(reverse("admin:accounts_user_change", args=[person.pk])).content.decode()
    assert entry.password not in page
    from django.contrib import admin

    assert CompromisedPassword not in admin.site._registry


def test_the_security_settings_page_states_who_marks_a_password(owner):
    from core.security_settings import in_force

    facts = {fact.key: fact for group in in_force() for fact in group.facts}
    fact = facts["compromised-password"]
    assert fact.value.startswith("A superuser") and fact.source == "accounts"
    assert "marked as compromised for the account" in facts["passwords"].note


def test_a_lab_role_cannot_reach_the_service_through_a_group(owner, person):
    group = Group.objects.create(name="Not a superuser")
    person.groups.add(group)
    with pytest.raises(PermissionDenied):
        compromised.mark_compromised(
            actor=person,
            account_id=owner.pk,
            reason="Leaked",
            drawn=compromised.drawn_password(owner),
        )


def test_the_new_password_page_sets_nothing_for_an_account_that_is_not_marked(person):
    browser = signed_in(person)
    assert browser.get(NEW_PAGE)["Location"] == "/"
    response = browser.post(
        NEW_PAGE, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.check_password(PASSWORD) and not person.check_password(NEW_PASSWORD)
    assert not AuditEvent.objects.filter(action=compromised.CHANGED_ACTION).exists()


def test_a_page_drawn_before_the_mark_was_lifted_elsewhere_saves_nothing(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mark(owner, person)
    browser = BrowserClient()
    sign_in_with_code(browser, person, secret)
    assert browser.get(NEW_PAGE).status_code == 200
    # A superuser sets a new password meanwhile: the mark is lifted and this session ends.
    stored = User.objects.get(pk=person.pk)
    stored.set_password(OTHER_PASSWORD)
    stored.save(update_fields=["password"])
    response = choose(browser, person, NEW_PASSWORD)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    stored.refresh_from_db()
    assert stored.check_password(OTHER_PASSWORD)


def test_a_marked_superuser_is_held_out_of_the_admin_console(owner, make_user):
    other = make_user("other-owner", is_superuser=True, is_staff=True)
    secret = generate_totp_secret()
    TOTP.activate(other, secret)
    mark(owner, other)
    browser = BrowserClient()
    sign_in_with_code(browser, other, secret)
    assert browser.get(reverse("admin:index"))["Location"] == NEW_PAGE
    assert browser.get(mark_url(owner))["Location"] == NEW_PAGE
    assert not CompromisedPassword.objects.filter(user=owner).exists()


def test_a_change_under_way_when_the_mark_ends_its_session_saves_nothing(
    owner, person, monkeypatch
):
    """Change password checked the old password before the mark; the mark ends the session;
    the save that follows must not lift the mark for whoever held that session."""
    from accounts.adapter import ClosedSignupAdapter

    browser = signed_in(person)
    original = ClosedSignupAdapter.set_password
    marked = []

    def mark_then_save(self, user, password):
        marked.append(
            compromised.mark_compromised(
                actor=owner,
                account_id=user.pk,
                reason="Leaked",
                drawn=compromised.drawn_password(User.objects.get(pk=user.pk)),
            )
        )
        return original(self, user, password)

    monkeypatch.setattr(ClosedSignupAdapter, "set_password", mark_then_save)
    response = browser.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert marked and response.status_code == 302
    assert response["Location"].startswith(reverse("account_login"))
    stored = User.objects.get(pk=person.pk)
    assert stored.check_password(PASSWORD) and not stored.check_password(NEW_PASSWORD)
    assert CompromisedPassword.objects.get(user=person).lifted_at is None


def test_an_own_change_from_an_open_session_of_an_unmarked_account_is_unaffected(person):
    browser = signed_in(person)
    response = browser.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302
    assert User.objects.get(pk=person.pk).check_password(NEW_PASSWORD)
    assert browser.get("/").status_code == 200


def test_a_page_drawn_for_an_earlier_mark_saves_nothing(owner, person):
    secret = generate_totp_secret()
    TOTP.activate(person, secret)
    mark(owner, person)
    browser = BrowserClient()
    sign_in_with_code(browser, person, secret)
    page = browser.get(NEW_PAGE)
    drawn = page.context["form"]["mark"].value()
    assert str(drawn) == str(CompromisedPassword.objects.get(user=person).pk)
    response = browser.post(
        NEW_PAGE,
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD, "mark": str(drawn + 99)},
    )
    assert response.status_code == 302 and response["Location"] == NEW_PAGE
    assert [str(m) for m in browser.get(NEW_PAGE).context["messages"]] == [
        compromised.NOT_THE_DRAWN_MARK
    ]
    assert CompromisedPassword.objects.get(user=person).lifted_at is None
    assert not User.objects.get(pk=person.pk).check_password(NEW_PASSWORD)
    missing = browser.post(NEW_PAGE, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert missing.status_code == 200 and missing.context["form"].errors
    assert not User.objects.get(pk=person.pk).check_password(NEW_PASSWORD)


def test_the_notice_fits_a_deployment_without_email(owner, person, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    one = signed_in(person)
    marker, response = mark(owner, person)
    text = [str(m) for m in marker.get(response["Location"]).context["messages"]]
    assert any("sends no email" in t and "Forgot your password?" not in t for t in text)
    one.get("/")
    assert notices(one) == [sessions.COMPROMISED_WITHOUT_MAIL]


@pytest.mark.parametrize("enrolment", [False, True])
def test_a_password_only_session_an_earlier_release_opened_is_ended_at_its_next_request(
    owner, person, enrolment
):
    """A process of an earlier release knows no marks: its sign-in with the password alone opens
    the session (simulated by reopening the row this release started ended). This release ends it
    at its next request, before either hold, so it can neither choose the new password nor add a
    second factor first."""
    from core.models import SecondFactorRule

    if enrolment:
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["client_services"]})
    mark(owner, person)
    first = None
    for path in (NEW_PAGE, "/account/second-factor/", reverse("mfa_activate_totp"), "/"):
        browser = signed_in(person)
        row = AccountSession.objects.filter(pk=browser.session[sessions.SESSION_KEY])
        row.update(ended_at=None, ending="")
        response = browser.get(path)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login")), path
        assert row.get().ending == Ending.MARKED_COMPROMISED
        first = first or browser
    assert notices(first) == [sessions.ENDED_BY_MESSAGES[Ending.MARKED_COMPROMISED]]
    browser = signed_in(person)
    AccountSession.objects.filter(pk=browser.session[sessions.SESSION_KEY]).update(
        ended_at=None, ending=""
    )
    choose(browser, person, NEW_PASSWORD)
    assert not User.objects.get(pk=person.pk).check_password(NEW_PASSWORD)
    assert CompromisedPassword.objects.get(user=person).lifted_at is None
