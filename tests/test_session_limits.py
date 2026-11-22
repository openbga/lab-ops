"""Session limits: every request to a signed-in session is checked against the laboratory's
limits (accounts.sessions), the admin role sets them on Security settings (core.session_limits),
and a deactivation or a new password ends sessions at once. Times are moved by changing the
session rows in the database; the limits are compared with the database's clock."""

import logging
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.functions import Now
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import sessions
from accounts.models import AccountSession, User
from accounts.roles import ROLES
from core import session_limits
from core.models import AuditEvent, SessionLimit
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db


class StrongerHasher(PBKDF2PasswordHasher):
    algorithm = "pbkdf2_stronger"
    iterations = PBKDF2PasswordHasher.iterations + 1


NEW_PASSWORD = "another-test-password-418!"
OPERATIONAL = [role for role in ROLES if role != "admin"]
Ending = AccountSession.Ending


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin(staff):
    return staff("limits-admin", "admin")


@pytest.fixture
def agent(staff):
    return staff("limits-agent", "client_services")


@pytest.fixture
def portal(client_org, portal_user):
    return portal_user(client_org, "limits-portal")


def browser_for(user):
    browser = BrowserClient()
    browser.force_login(user)
    return browser


def open_row(user):
    return AccountSession.objects.get(user=user, ended_at__isnull=True)


def age(user, *, idle=None, since=None):
    """Move the account's open session back in time: last activity `idle` ago, sign-in `since`
    ago."""
    changes = {}
    if idle is not None:
        changes["last_active_at"] = Now() - idle
    if since is not None:
        changes["started_at"] = Now() - since
    assert AccountSession.objects.filter(user=user, ended_at__isnull=True).update(**changes) == 1


def signed_out_to(response, page):
    return response.status_code == 302 and response["Location"] == (
        reverse("account_login") + "?next=" + page
    )


def notice(browser):
    page = browser.get(reverse("account_login"))
    return [str(message) for message in page.context["messages"]]


# signing in and out


def test_signing_in_opens_a_session_the_browser_carries(agent):
    browser = browser_for(agent)
    row = open_row(agent)
    assert browser.session[sessions.SESSION_KEY] == str(row.pk)
    assert row.ending == "" and row.ended_at is None
    assert browser.get("/").status_code == 200


def test_sign_out_ends_the_session_as_signed_out(agent):
    browser = browser_for(agent)
    row = open_row(agent)
    assert browser.post(reverse("account_logout")).status_code == 302
    row.refresh_from_db()
    assert row.ending == Ending.SIGNED_OUT and row.ended_at is not None


def test_signing_in_again_in_the_same_browser_ends_the_earlier_session(agent):
    browser = browser_for(agent)
    first = open_row(agent)
    browser.force_login(agent)
    first.refresh_from_db()
    assert first.ending == Ending.SIGNED_IN_AGAIN
    second = open_row(agent)
    assert browser.session[sessions.SESSION_KEY] == str(second.pk)
    assert browser.get("/").status_code == 200


def test_a_password_sign_in_opens_a_session_too(agent):
    browser = BrowserClient()
    response = browser.post(
        reverse("account_login"), {"login": agent.username, "password": PASSWORD}
    )
    assert response.status_code == 302
    assert browser.session[sessions.SESSION_KEY] == str(open_row(agent).pk)


def test_a_session_from_before_this_release_is_signed_out(agent):
    browser = browser_for(agent)
    session = browser.session
    del session[sessions.SESSION_KEY]
    session.save()
    assert signed_out_to(browser.get("/"), "/")
    assert notice(browser) == [sessions.ENDED_MESSAGE]


def test_a_session_carrying_another_accounts_row_is_signed_out(agent, staff):
    other = staff("limits-other", "client_services")
    browser_for(other)
    browser = browser_for(agent)
    session = browser.session
    session[sessions.SESSION_KEY] = str(open_row(other).pk)
    session.save()
    assert signed_out_to(browser.get("/"), "/")
    assert open_row(other).ended_at is None  # the other account's session is not touched


def test_a_session_carrying_no_valid_id_is_signed_out(agent):
    browser = browser_for(agent)
    session = browser.session
    session[sessions.SESSION_KEY] = "not-an-id"
    session.save()
    assert signed_out_to(browser.get("/"), "/")
    assert open_row(agent).ended_at is None


# activity and the two limits


def test_a_request_is_activity_and_the_status_question_is_not(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=10))
    before = open_row(agent).last_active_at
    answer = browser.get(reverse("account-session"), HTTP_ACCEPT="application/json").json()
    assert answer["signed_in"] is True
    assert 19 * 60 < answer["inactivity_seconds"] <= 20 * 60
    assert 11 * 3600 < answer["maximum_seconds"] <= 12 * 3600
    assert answer["warning_seconds"] == sessions.WARNING_SECONDS
    assert open_row(agent).last_active_at == before
    assert browser.get("/").status_code == 200
    assert open_row(agent).last_active_at > before


def test_stay_signed_in_is_activity_and_needs_the_csrf_token(agent):
    browser = BrowserClient(enforce_csrf_checks=True)
    browser.force_login(agent)
    age(agent, idle=timedelta(minutes=29))
    assert browser.post(reverse("account-session-continue")).status_code == 403
    page = browser.get("/")  # activity too, and gives the token
    age(agent, idle=timedelta(minutes=29))
    token = page.cookies["csrftoken"].value if "csrftoken" in page.cookies else None
    token = token or browser.cookies["csrftoken"].value
    answer = browser.post(reverse("account-session-continue"), HTTP_X_CSRFTOKEN=token).json()
    assert answer["signed_in"] is True and answer["inactivity_seconds"] > 29 * 60
    assert browser.get(reverse("account-session-continue")).status_code == 405


def test_no_activity_for_the_limit_signs_out_and_returns_to_the_page(agent, caplog):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=30, seconds=1))
    row = open_row(agent)
    page = reverse("desk:request-list") + "?view=all"
    with caplog.at_level(logging.INFO, logger="lab_ops.signin"):
        response = browser.get(page)
    assert response.status_code == 302
    assert response["Location"] == reverse("account_login") + "?next=/requests/%3Fview%3Dall"
    row.refresh_from_db()
    assert row.ending == Ending.INACTIVITY and row.ended_at is not None
    assert notice(browser) == [
        "You were signed out after 30 minutes without activity. Sign in again to continue."
    ]
    assert f"signout.inactivity user={agent.username} sessions=1" in caplog.text
    # Signing in again returns to the page, with a new session.
    signed_in = browser.post(response["Location"], {"login": agent.username, "password": PASSWORD})
    assert signed_in.status_code == 302 and signed_in["Location"] == page
    assert open_row(agent).pk != row.pk


def test_activity_just_inside_the_limit_keeps_the_session(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=29, seconds=50))
    assert browser.get("/").status_code == 200
    assert open_row(agent).ending == ""


def test_the_longest_session_ends_whatever_the_activity(agent):
    browser = browser_for(agent)
    age(agent, since=timedelta(hours=12, seconds=1))
    row = open_row(agent)
    assert signed_out_to(browser.get("/"), "/")
    row.refresh_from_db()
    assert row.ending == Ending.MAXIMUM
    assert notice(browser) == [
        "Your session reached its longest length of 12 hours. Sign in again to continue."
    ]


def test_staff_and_portal_accounts_follow_their_own_limits(agent, portal):
    SessionLimit.objects.create(audience="portal", inactivity_minutes=5, maximum_hours=2)
    SessionLimit.objects.create(audience="staff", inactivity_minutes=60, maximum_hours=24)
    staff_browser, portal_browser = browser_for(agent), browser_for(portal)
    age(agent, idle=timedelta(minutes=45), since=timedelta(hours=20))
    age(portal, idle=timedelta(minutes=6))
    assert staff_browser.get("/").status_code == 200
    home = reverse("portal:home")
    assert signed_out_to(portal_browser.get(home), home)
    assert notice(portal_browser) == [
        "You were signed out after 5 minutes without activity. Sign in again to continue."
    ]


def test_a_shortened_limit_applies_from_the_next_request(admin, agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=10))
    session_limits.save_limits(
        actor=admin,
        audience="staff",
        new={"inactivity_minutes": 5, "maximum_hours": 12},
        expected_revision=0,
        reason="Shared workstations at the front desk",
    )
    assert signed_out_to(browser.get("/"), "/")
    assert AccountSession.objects.get(user=agent).ending == Ending.INACTIVITY


def test_an_expired_session_asking_its_status_is_ended_and_told(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=31))
    answer = browser.get(reverse("account-session")).json()
    assert answer == {"signed_in": False, "sign_in": reverse("account_login")}
    assert AccountSession.objects.get(user=agent).ending == Ending.INACTIVITY
    assert notice(browser)[0].startswith("You were signed out after 30 minutes")


def test_a_signed_out_visitor_asking_the_status_is_told_to_sign_in(db):
    answer = BrowserClient().get(reverse("account-session")).json()
    assert answer == {"signed_in": False, "sign_in": reverse("account_login")}


# what a request gets once its session has ended


def test_a_form_sent_after_the_limit_changes_nothing_and_returns_to_its_page(admin):
    browser = browser_for(admin)
    form = reverse("session-limits-edit", args=["staff"])
    assert browser.get(form).status_code == 200
    age(admin, idle=timedelta(minutes=31))
    response = browser.post(
        form,
        {
            "inactivity_minutes": "10",
            "maximum_hours": "8",
            "reason": "Front desk",
            "expected_revision": "0",
        },
        HTTP_REFERER="http://testserver" + form + "?from=security",
    )
    assert response.status_code == 302
    assert response["Location"] == (
        reverse("account_login") + "?next=/lab/security/sessions/staff/%3Ffrom%3Dsecurity"
    )
    assert not SessionLimit.objects.exists() and not AuditEvent.objects.exists()


def test_a_form_sent_from_another_site_is_not_returned_to(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=31))
    response = browser.post(reverse("preference"), HTTP_REFERER="https://example.invalid/phish")
    assert response.status_code == 302 and response["Location"] == reverse("account_login")


def test_a_dialog_or_partial_update_after_the_limit_loads_the_sign_in_page(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=31))
    page = "http://testserver/requests/?view=all"
    for headers in ({"HTTP_HX_REQUEST": "true"}, {"HTTP_X_VR_FORM": "1"}):
        response = browser.get(reverse("desk:request-create"), HTTP_HX_CURRENT_URL=page, **headers)
        assert response.status_code == 401
        sign_in = reverse("account_login") + "?next=/requests/%3Fview%3Dall"
        assert response["HX-Redirect"] == sign_in
        # The refusal also says whether what this form posted is the copy kept (new-ticket batch).
        assert response.json() == {"signed_in": False, "sign_in": sign_in, "kept": False}
        browser.force_login(agent)
        age(agent, idle=timedelta(minutes=31))


def test_a_script_request_after_the_limit_is_refused_not_redirected(agent):
    browser = browser_for(agent)
    age(agent, idle=timedelta(minutes=31))
    response = browser.post(
        reverse("preference"),
        {"key": "nav.collapsed", "value": "true"},
        HTTP_SEC_FETCH_DEST="empty",
    )
    assert response.status_code == 401
    assert not agent.preferences.exists()


def test_a_session_ended_between_its_reading_and_its_activity_is_signed_out(agent, monkeypatch):
    browser = browser_for(agent)
    row = open_row(agent)
    read = sessions._read

    def stale_then_real(session_id, user):
        found = read(session_id, user)
        # Ended (deactivated, a new password) after the reading and before the activity.
        AccountSession.objects.filter(pk=row.pk).update(ended_at=Now(), ending=Ending.DEACTIVATED)
        monkeypatch.setattr(sessions, "_read", read)
        return found

    monkeypatch.setattr(sessions, "_read", stale_then_real)
    assert signed_out_to(browser.get("/"), "/")
    row.refresh_from_db()
    assert row.ending == Ending.DEACTIVATED  # not brought back, nor rewritten
    assert notice(browser) == [sessions.ENDED_MESSAGE]


def test_a_session_expiring_between_its_reading_and_its_activity_ends_for_inactivity(
    agent, monkeypatch
):
    browser = browser_for(agent)
    read = sessions._read

    def just_inside_then_past(session_id, user):
        found = read(session_id, user)
        age(agent, idle=timedelta(minutes=31))
        monkeypatch.setattr(sessions, "_read", read)
        return found

    monkeypatch.setattr(sessions, "_read", just_inside_then_past)
    assert signed_out_to(browser.get("/"), "/")
    assert AccountSession.objects.get(user=agent).ending == Ending.INACTIVITY


# pages are not stored


def test_every_answer_to_a_signed_in_session_is_marked_no_store(agent, admin):
    browser = browser_for(agent)
    for address in ("/", reverse("desk:request-list"), reverse("account-session")):
        response = browser.get(address)
        assert "no-store" in response["Cache-Control"], address
        assert "private" in response["Cache-Control"], address
    signed_out = browser_for(admin)
    age(admin, idle=timedelta(minutes=31))
    assert "no-store" in signed_out.get("/")["Cache-Control"]


def test_signing_in_marks_its_answer_no_store(agent):
    response = BrowserClient().post(
        reverse("account_login"), {"login": agent.username, "password": PASSWORD}
    )
    assert response.status_code == 302 and "no-store" in response["Cache-Control"]


# deactivation and a new password


def test_deactivation_ends_every_session_at_once_and_reactivation_does_not_bring_them_back(
    agent, caplog, django_capture_on_commit_callbacks
):
    first, second = browser_for(agent), browser_for(agent)
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        agent.is_active = False
        agent.save(update_fields=["is_active"])
    assert not AccountSession.objects.filter(user=agent, ended_at__isnull=True).exists()
    assert set(AccountSession.objects.filter(user=agent).values_list("ending", flat=True)) == {
        Ending.DEACTIVATED
    }
    agent.is_active = True
    agent.save()
    for browser in (first, second):
        assert signed_out_to(browser.get("/"), "/")
    assert f"signout.deactivated user={agent.username} sessions=2" in caplog.text


def test_a_save_that_leaves_an_account_active_ends_nothing(agent):
    browser_for(agent)
    agent.first_name = "Example"
    agent.save()
    agent.save(update_fields=["first_name"])
    assert open_row(agent).ended_at is None


def test_a_rolled_back_deactivation_ends_nothing(agent, caplog, django_capture_on_commit_callbacks):
    from django.db import transaction

    browser = browser_for(agent)
    with (
        caplog.at_level(logging.INFO, logger="lab_ops.signin"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        with pytest.raises(RuntimeError), transaction.atomic():
            agent.is_active = False
            agent.save()
            raise RuntimeError("cancelled")
    assert browser.get("/").status_code == 200
    assert "signout.deactivated" not in caplog.text


def test_a_new_password_from_the_account_page_keeps_only_that_session(agent):
    changer, other = browser_for(agent), browser_for(agent)
    changer_row = AccountSession.objects.get(pk=changer.session[sessions.SESSION_KEY])
    response = changer.post(
        reverse("account_change_password"),
        {"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert response.status_code == 302
    changer_row.refresh_from_db()
    assert changer_row.ended_at is None
    other_row = AccountSession.objects.get(pk=other.session[sessions.SESSION_KEY])
    assert other_row.ending == Ending.NEW_PASSWORD
    assert changer.get("/").status_code == 200
    assert signed_out_to(other.get("/"), "/")


def test_a_password_set_for_another_account_ends_all_of_its_sessions(agent):
    superuser = User.objects.create_superuser("limits-root", "root@example.invalid", PASSWORD)
    first, second = browser_for(agent), browser_for(agent)
    owner = browser_for(superuser)
    response = owner.post(
        reverse("admin:auth_user_password_change", args=[agent.pk]),
        {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD, "usable_password": "true"},
    )
    assert response.status_code == 302
    assert not AccountSession.objects.filter(user=agent, ended_at__isnull=True).exists()
    for browser in (first, second):
        assert signed_out_to(browser.get("/"), "/")
    assert owner.get("/admin/").status_code == 200  # the owner's own session goes on


def test_saving_the_password_unchanged_ends_nothing(agent):
    browser_for(agent)
    agent.refresh_from_db()
    agent.save()
    agent.save(update_fields=["password"])
    assert open_row(agent).ended_at is None


def test_an_unusable_password_set_for_another_account_ends_its_sessions(agent):
    browser = browser_for(agent)
    agent.set_unusable_password()  # Django keeps no raw value for this one
    agent.save(update_fields=["password"])
    assert AccountSession.objects.get(user=agent).ending == Ending.NEW_PASSWORD
    assert signed_out_to(browser.get("/"), "/")


def test_a_hash_upgraded_at_sign_in_ends_the_sessions_the_new_hash_signs_out(agent, settings):
    """Django checks each session against the stored hash, so an upgraded hash signs the other
    browsers out whatever this release does; their sessions end with it, and the new sign-in
    opens its own."""
    other = browser_for(agent)
    settings.PASSWORD_HASHERS = [
        "tests.test_session_limits.StrongerHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
    browser = BrowserClient()
    signed_in = browser.post(
        reverse("account_login"), {"login": agent.username, "password": PASSWORD}
    )
    assert signed_in.status_code == 302
    agent.refresh_from_db()
    assert agent.password.startswith("pbkdf2_stronger$")
    assert signed_out_to(other.get("/"), "/")
    assert browser.get("/").status_code == 200
    assert set(AccountSession.objects.filter(user=agent).values_list("ending", flat=True)) == {
        Ending.NEW_PASSWORD,
        "",
    }


# the limits: who sets them, within what, and with what record


def save(actor, audience="staff", revision=0, reason="Shared front-desk workstations", **new):
    values = {"inactivity_minutes": 15, "maximum_hours": 8} | new
    return session_limits.save_limits(
        actor=actor, audience=audience, new=values, expected_revision=revision, reason=reason
    )


def test_limits_never_saved_are_the_starting_values_and_reading_writes_nothing():
    staff_limits, portal_limits = session_limits.limits()
    assert (staff_limits.audience, portal_limits.audience) == ("staff", "portal")
    for limit in (staff_limits, portal_limits):
        assert (limit.inactivity_minutes, limit.maximum_hours, limit.revision) == (30, 12, 0)
    assert not SessionLimit.objects.exists()


def test_the_admin_role_saves_limits_with_a_reason_and_an_audit_event(admin):
    limit, changed = save(admin)
    assert changed and limit.revision == 1
    event = AuditEvent.objects.get(action=session_limits.ACTION)
    assert event.actor == admin and event.reason == "Shared front-desk workstations"
    assert event.before == {"audience": "staff", "inactivity_minutes": 30, "maximum_hours": 12}
    assert event.after == {"audience": "staff", "inactivity_minutes": 15, "maximum_hours": 8}
    limit, changed = save(admin, revision=1, inactivity_minutes=20)
    assert changed and limit.revision == 2
    unchanged, changed = save(admin, revision=2, inactivity_minutes=20)
    assert not changed and unchanged.revision == 2
    assert AuditEvent.objects.filter(action=session_limits.ACTION).count() == 2
    assert not SessionLimit.objects.filter(audience="portal").exists()


def test_a_save_based_on_limits_changed_since_is_refused(admin):
    save(admin)
    with pytest.raises(ValidationError, match="changed after you opened this form"):
        save(admin, revision=0, inactivity_minutes=45)
    assert SessionLimit.objects.get().inactivity_minutes == 15


@pytest.mark.parametrize(
    "new",
    [
        {"inactivity_minutes": 4},
        {"inactivity_minutes": 61},
        {"maximum_hours": 0},
        {"maximum_hours": 25},
        {"inactivity_minutes": True},
        {"maximum_hours": "8"},
        {"inactivity_minutes": None},
    ],
)
def test_limits_outside_their_bounds_are_refused(admin, new):
    with pytest.raises(ValidationError):
        save(admin, **new)
    assert not SessionLimit.objects.exists() and not AuditEvent.objects.exists()


def test_a_reason_is_required(admin):
    with pytest.raises(ValidationError, match="A reason is required"):
        save(admin, reason="   ")
    assert not SessionLimit.objects.exists()


@pytest.mark.parametrize("role", OPERATIONAL)
def test_only_the_admin_role_saves_limits(staff, role):
    with pytest.raises(PermissionDenied):
        save(staff(f"limits-{role}", role))
    assert not SessionLimit.objects.exists()


def test_a_portal_account_or_a_superuser_without_the_role_cannot_save_limits(portal):
    superuser = User.objects.create_superuser("limits-owner", "owner@example.invalid", PASSWORD)
    for actor in (portal, superuser):
        with pytest.raises(PermissionDenied):
            save(actor)


def test_the_role_is_decided_as_the_account_stands_when_saving(admin):
    read_earlier = User.objects.get(pk=admin.pk)
    admin.groups.remove(Group.objects.get(name="admin"))
    with pytest.raises(PermissionDenied):
        save(read_earlier)
    admin.groups.add(Group.objects.get(name="admin"))
    User.objects.filter(pk=admin.pk).update(is_active=False)
    with pytest.raises(PermissionDenied):
        save(read_earlier)  # still active as it was read
    assert not SessionLimit.objects.exists()


def test_an_unknown_audience_is_not_a_limit(admin):
    with pytest.raises(ValueError):
        save(admin, audience="everyone")


# the page and its form


def form_data(**changes):
    data = {
        "inactivity_minutes": "15",
        "maximum_hours": "8",
        "reason": "Shared front-desk workstations",
        "expected_revision": "0",
    }
    return data | changes


def test_security_settings_shows_the_limits_and_their_history(admin):
    browser = browser_for(admin)
    page = browser.get(reverse("security-settings"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Session limits" in html and "30 minutes" in html and "12 hours" in html
    assert reverse("session-limits-edit", args=["portal"]) in html
    save(admin, audience="portal")
    page = browser.get(reverse("security-settings"))
    detail = [event.detail for event in page.context["history"]]
    assert detail == [
        "Client portal: 15 minutes without activity (was 30 minutes); longest session 8 hours "
        "(was 12 hours)"
    ]
    facts = {fact.key: fact for group in page.context["groups"] for fact in group.facts}
    assert (
        facts["inactivity"].value == "30 minutes for staff, 15 minutes for client portal accounts"
    )


def test_the_form_saves_and_returns_to_security_settings(admin):
    browser = browser_for(admin)
    form = reverse("session-limits-edit", args=["staff"])
    page = browser.get(form)
    assert page.status_code == 200 and page.context["form"].initial["expected_revision"] == 0
    response = browser.post(form, form_data())
    assert response.status_code == 302 and response["Location"] == reverse("security-settings")
    limit = SessionLimit.objects.get(audience="staff")
    assert (limit.inactivity_minutes, limit.maximum_hours, limit.revision) == (15, 8, 1)


def test_the_form_keeps_what_was_typed_when_refused(admin):
    browser = browser_for(admin)
    form = reverse("session-limits-edit", args=["staff"])
    save(admin)
    response = browser.post(form, form_data(inactivity_minutes="45", reason="Typed reason"))
    assert response.status_code == 200
    assert "changed after you opened this form" in response.content.decode()
    assert response.context["form"]["reason"].value() == "Typed reason"
    response = browser.post(form, form_data(inactivity_minutes="90", expected_revision="1"))
    assert response.status_code == 200 and "inactivity_minutes" in response.context["form"].errors
    assert SessionLimit.objects.get().inactivity_minutes == 15


@pytest.mark.parametrize("role", OPERATIONAL)
def test_the_form_is_refused_to_other_roles_before_anything_about_it(staff, role):
    browser = browser_for(staff(f"limits-page-{role}", role))
    for audience in ("staff", "portal", "everyone"):
        address = reverse("session-limits-edit", args=[audience])
        assert browser.get(address).status_code == 403
        assert browser.post(address, form_data()).status_code == 403
    assert not SessionLimit.objects.exists()


def test_the_form_is_refused_to_a_portal_account(portal):
    browser = browser_for(portal)
    assert browser.get(reverse("session-limits-edit", args=["portal"])).status_code == 403


def test_an_unknown_audience_is_not_found_for_the_admin_role(admin):
    browser = browser_for(admin)
    assert browser.get(reverse("session-limits-edit", args=["everyone"])).status_code == 404


def test_a_role_removed_after_the_form_opened_refuses_the_save(admin):
    browser = browser_for(admin)
    form = reverse("session-limits-edit", args=["staff"])
    assert browser.get(form).status_code == 200
    admin.groups.remove(Group.objects.get(name="admin"))
    assert browser.post(form, form_data()).status_code == 403
    assert not SessionLimit.objects.exists()


def test_a_status_answer_carries_only_times(agent):
    browser = browser_for(agent)
    answer = browser.get(reverse("account-session")).json()
    assert set(answer) == {
        "signed_in",
        "inactivity_seconds",
        "maximum_seconds",
        "inactivity_limit_seconds",
        "warning_seconds",
        "longest_warning_seconds",
    }
    assert str(uuid.UUID(browser.session[sessions.SESSION_KEY])) not in str(answer)
