"""Sign-in history (accounts.signin_history): what is recorded for which account, what never is,
that the database writes every session's start and end and refuses to change an entry, and the
page where each person reads their own."""

import datetime

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.core.cache import cache
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.test import Client as BrowserClient
from django.urls import reverse

from accounts import compromised, sessions, signin_history
from accounts.models import AccountSession, SignInEvent, SignInFactor, User
from accounts.session_list import end_account_sessions, sign_out_others
from accounts.tests.test_confirm_identity import code_for as safe_code_for

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db

Kind = SignInEvent.Kind
Ending = AccountSession.Ending
PAGE = "/account/sign-ins/"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
TYPED_SECRET = "Typed-Secret-Value-41"


@pytest.fixture(autouse=True)
def fresh_limits(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    cache.clear()
    yield
    cache.clear()


def browser():
    return BrowserClient(HTTP_USER_AGENT=AGENT, REMOTE_ADDR="192.0.2.10")


def events(user):
    return list(
        SignInEvent.objects.filter(user=user).order_by("id").values_list("kind", "factor", "ending")
    )


def sign_in(http, login, password=PASSWORD):
    return http.post(reverse("account_login"), {"login": login, "password": password})


def code_for(secret):
    # Taken with time left in its period: a code sent just before the period ends is refused
    # after it (the app accepts only the current period).
    return safe_code_for(secret)


def wrong_code(secret):
    """Six digits no authenticator code of this period or the next accepts."""
    counter = next(yield_hotp_counters_from_time())
    accepted = {hotp_value(secret, counter + step) for step in (-1, 0, 1, 2)}
    return format_hotp_value(next(n for n in range(10**6) if n not in accepted))


def session_row(http):
    return AccountSession.objects.get(pk=http.session[sessions.SESSION_KEY])


def nothing_typed_is_stored():
    """No entry holds anything a person typed: the table has no column for it, and no text
    column holds the typed values used by these tests."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM accounts_signinevent WHERE kind || factor || ending || "
            "user_agent || coalesce(host(address), '') ~* %s",
            [f"{TYPED_SECRET}|{PASSWORD}|wrong"],
        )
        return cursor.fetchone()[0] == 0


# --- Sign-ins and sign-outs --------------------------------------------------------------------


def test_a_password_sign_in_and_sign_out_are_recorded_with_the_session(make_user):
    user = make_user("history-staff", "technical")
    http = browser()
    assert sign_in(http, user.username).status_code == 302
    row = session_row(http)
    http.post(reverse("account_logout"))

    signed_in, signed_out = SignInEvent.objects.filter(user=user).order_by("id")
    assert (signed_in.kind, signed_in.factor, signed_in.ending) == (
        Kind.SIGNED_IN,
        SignInFactor.PASSWORD,
        "",
    )
    assert signed_in.session == row.pk and signed_in.occurred_at == row.started_at
    assert (signed_in.user_agent, signed_in.address) == (AGENT, "192.0.2.10")
    row.refresh_from_db()
    assert (signed_out.kind, signed_out.ending, signed_out.factor) == (
        Kind.SIGNED_OUT,
        Ending.SIGNED_OUT,
        "",
    )
    assert signed_out.session == row.pk and signed_out.occurred_at == row.ended_at
    assert (signed_out.user_agent, signed_out.address) == (AGENT, "192.0.2.10")


def test_an_authenticator_or_recovery_code_is_recorded_as_the_second_factor(make_user):
    user = make_user("history-mfa", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    codes = RecoveryCodes.activate(user).get_unused_codes()

    first = browser()
    assert sign_in(first, user.username).url == reverse("mfa_authenticate")
    first.post(reverse("mfa_authenticate"), {"code": code_for(secret)})
    second = browser()
    sign_in(second, user.email)
    second.post(reverse("mfa_authenticate"), {"code": codes[0]})

    assert events(user) == [
        (Kind.SIGNED_IN, SignInFactor.PASSWORD_AUTHENTICATOR, ""),
        (Kind.SIGNED_IN, SignInFactor.PASSWORD_RECOVERY_CODE, ""),
    ]
    assert session_row(first).factor == SignInFactor.PASSWORD_AUTHENTICATOR


def test_an_emailed_code_sign_in_and_a_wrong_code_are_recorded(make_user, settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("history-code", "technical")
    http = browser()
    request_code(http, user.email)
    code = mailed_code()
    http.post(reverse("account_confirm_login_code"), {"code": TYPED_SECRET})
    http.post(reverse("account_confirm_login_code"), {"code": code})
    assert http.session["_auth_user_id"] == str(user.pk)
    assert events(user) == [
        (Kind.FAILED, SignInFactor.CODE, ""),
        (Kind.SIGNED_IN, SignInFactor.CODE, ""),
    ]
    assert nothing_typed_is_stored()


def test_a_forced_sign_in_without_recorded_steps_has_no_factor(make_user):
    user = make_user("history-forced", "technical")
    BrowserClient().force_login(user)
    assert events(user) == [(Kind.SIGNED_IN, "", "")]


# --- Failures, refusals and lockouts -----------------------------------------------------------


def test_wrong_passwords_are_recorded_for_the_one_account_they_name(make_user):
    user = make_user("history-target", "supervisor")
    EmailAddress.objects.create(user=user, email="alias-history@example.invalid", verified=True)
    other = make_user("history-other", "technical")
    http = browser()
    for identifier in ("HISTORY-TARGET", user.email.upper(), "alias-history@example.invalid"):
        assert sign_in(http, identifier, TYPED_SECRET).status_code == 200
    sign_in(http, "nobody-by-this-name", TYPED_SECRET)
    sign_in(http, TYPED_SECRET, "wrong-password-typed")

    assert events(user) == [(Kind.FAILED, SignInFactor.PASSWORD, "")] * 3
    assert events(other) == []
    assert SignInEvent.objects.count() == 3
    failed = SignInEvent.objects.filter(user=user).first()
    assert (failed.session, failed.user_agent, failed.address) == (None, AGENT, "192.0.2.10")
    assert nothing_typed_is_stored()


def test_an_identifier_naming_two_accounts_records_nothing(make_user):
    first = make_user("shared-name@example.invalid", "technical")
    second = make_user("history-second", "technical")
    User.objects.filter(pk=second.pk).update(email="Shared-Name@Example.invalid")
    sign_in(browser(), "shared-name@example.invalid", TYPED_SECRET)
    assert events(first) == [] and events(second) == []


def test_attempts_refused_by_the_failed_sign_in_limit_are_lockouts(make_user, settings):
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    user = make_user("history-throttled", "technical")
    http = browser()
    for password in (TYPED_SECRET, PASSWORD):
        assert sign_in(http, user.username, password).status_code == 200
    assert "_auth_user_id" not in http.session
    assert events(user) == [
        (Kind.FAILED, SignInFactor.PASSWORD, ""),
        (Kind.LOCKED_OUT, SignInFactor.PASSWORD, ""),
    ]


def test_second_factor_failures_and_lockouts_are_recorded(make_user, settings):
    user = make_user("history-mfa-fail", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    http = browser()
    assert sign_in(http, user.username).url == reverse("mfa_authenticate")
    http.post(reverse("mfa_authenticate"), {"code": wrong_code(secret)})
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    cache.clear()
    http.post(reverse("mfa_authenticate"), {"code": wrong_code(secret)})
    refused = http.post(reverse("mfa_authenticate"), {"code": code_for(secret)})
    assert refused.status_code == 200 and "_auth_user_id" not in http.session
    assert events(user) == [
        (Kind.FAILED, SignInFactor.SECOND_FACTOR, ""),
        (Kind.FAILED, SignInFactor.SECOND_FACTOR, ""),
        (Kind.LOCKED_OUT, SignInFactor.SECOND_FACTOR, ""),
    ]


def test_a_deactivated_account_is_refused_with_its_right_password(make_user):
    user = make_user("history-deactivated", "technical")
    User.objects.filter(pk=user.pk).update(is_active=False)
    http = browser()
    sign_in(http, user.username)
    sign_in(http, user.username, TYPED_SECRET)
    assert "_auth_user_id" not in http.session
    assert events(user) == [
        (Kind.REFUSED, "", Ending.DEACTIVATED),
        (Kind.FAILED, SignInFactor.PASSWORD, ""),
    ]


def test_a_password_marked_as_compromised_ends_sessions_and_refuses_sign_in(make_user):
    owner = User.objects.create_superuser("history-owner", "history-owner@example.invalid", "x-1!")
    user = make_user("history-marked", "client_services")
    open_session = browser()
    sign_in(open_session, user.username)
    opened = session_row(open_session)
    compromised.mark_compromised(
        actor=owner,
        account_id=user.pk,
        reason="Typed into a page posing as the laboratory's",
        drawn=compromised.drawn_password(user),
    )
    http = browser()
    sign_in(http, user.username)
    refused = session_row(http)

    assert events(user) == [
        (Kind.SIGNED_IN, SignInFactor.PASSWORD, ""),
        (Kind.SIGNED_OUT, "", Ending.MARKED_COMPROMISED),
        (Kind.REFUSED, SignInFactor.PASSWORD, Ending.MARKED_COMPROMISED),
    ]
    entries = SignInEvent.objects.filter(user=user).order_by("id")
    assert [entry.session for entry in entries] == [opened.pk, opened.pk, refused.pk]


def test_reauthentication_failures_are_recorded_for_the_signed_in_account(make_user):
    user = make_user("history-reauth", "technical")
    http = browser()
    sign_in(http, user.username)
    http.post(reverse("account_reauthenticate"), {"password": TYPED_SECRET})
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    http.post(reverse("mfa_reauthenticate"), {"code": wrong_code(secret)})
    assert events(user) == [
        (Kind.SIGNED_IN, SignInFactor.PASSWORD, ""),
        (Kind.REAUTHENTICATION_FAILED, SignInFactor.PASSWORD, ""),
        (Kind.REAUTHENTICATION_FAILED, SignInFactor.SECOND_FACTOR, ""),
    ]
    assert nothing_typed_is_stored()


# --- Every way a session ends ------------------------------------------------------------------


def test_every_way_a_session_ends_is_recorded_once(make_user):
    owner = User.objects.create_superuser("history-admin", "history-admin@example.invalid", "x-2!")
    user = make_user("history-endings", "technical")
    browsers = [browser() for _ in range(4)]
    for http in browsers:
        sign_in(http, user.username)
    rows = [session_row(http) for http in browsers]
    # Expired: no activity for longer than the limit; closed when the account next signs in.
    AccountSession.objects.filter(pk=rows[0].pk).update(
        last_active_at=rows[0].last_active_at - datetime.timedelta(days=2),
        started_at=rows[0].started_at - datetime.timedelta(days=2),
    )
    # Signed in again in the same browser (the abandoned session is closed with it).
    browsers[1].force_login(user)
    again = session_row(browsers[1])
    # Signed out from another session, then by an administrator.
    with transaction.atomic():
        sign_out_others(actor=user, current=str(again.pk))
    sign_in(browser(), user.username)
    end_account_sessions(actor=owner, account_id=user.pk, reason="Left the laboratory today")
    # A new password, then a deactivation.
    sign_in(browser(), user.username)
    user.set_password("copper lantern orchard seventeen")
    user.save(update_fields=["password"])
    sign_in(browser(), user.username, "copper lantern orchard seventeen")
    user.is_active = False
    user.save()

    endings = [ending for kind, _, ending in events(user) if kind == Kind.SIGNED_OUT]
    assert sorted(endings) == sorted(
        [
            Ending.INACTIVITY,
            Ending.SIGNED_IN_AGAIN,
            Ending.SIGNED_OUT_ELSEWHERE,
            Ending.SIGNED_OUT_ELSEWHERE,
            Ending.ADMINISTRATOR,
            Ending.ADMINISTRATOR,
            Ending.NEW_PASSWORD,
            Ending.DEACTIVATED,
        ]
    )
    ended = AccountSession.objects.filter(user=user, ended_at__isnull=False)
    assert ended.count() == len(endings)
    for row in ended:
        entry = SignInEvent.objects.get(user=user, kind=Kind.SIGNED_OUT, session=row.pk)
        assert (entry.occurred_at, entry.ending) == (row.ended_at, row.ending)


def test_a_session_ended_twice_is_recorded_once_and_its_later_saves_add_nothing(make_user):
    user = make_user("history-once", "technical")
    http = browser()
    sign_in(http, user.username)
    row = session_row(http)
    sessions.end_sessions(user, Ending.DEACTIVATED)
    sessions.end_sessions(user, Ending.ADMINISTRATOR)
    AccountSession.objects.filter(pk=row.pk).update(ended_at=row.started_at)
    row.refresh_from_db()
    row.save()
    assert events(user) == [
        (Kind.SIGNED_IN, SignInFactor.PASSWORD, ""),
        (Kind.SIGNED_OUT, "", Ending.DEACTIVATED),
    ]


def test_a_session_an_earlier_release_starts_is_recorded_without_its_factor(make_user):
    """A release still running while this one migrates inserts sessions without the factor."""
    user = make_user("history-earlier", "technical")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_accountsession (id, user_id, ended_at, ending) "
            "VALUES (gen_random_uuid(), %s, NULL, '') RETURNING id",
            [user.pk],
        )
        session_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE accounts_accountsession SET ended_at = now(), ending = 'signed_out' "
            "WHERE id = %s",
            [session_id],
        )
    assert events(user) == [(Kind.SIGNED_IN, "", ""), (Kind.SIGNED_OUT, "", Ending.SIGNED_OUT)]


# --- History is never changed ------------------------------------------------------------------


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_the_database_refuses_to_change_or_delete_an_entry(make_user, statement):
    user = make_user("history-immutable", "technical")
    BrowserClient().force_login(user)
    sql = {
        "UPDATE": "UPDATE accounts_signinevent SET kind = 'failed' WHERE user_id = %s",
        "DELETE": "DELETE FROM accounts_signinevent WHERE user_id = %s",
    }[statement]
    with pytest.raises(DatabaseError, match="Sign-in history cannot be changed or deleted"):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [user.pk])
    assert events(user) == [(Kind.SIGNED_IN, "", "")]


def test_deleting_an_account_keeps_its_history(make_user):
    """The application never deletes an account (it is deactivated); a deletion by other means
    leaves the history in place, and the entry keeps the account's id."""
    user = make_user("history-kept", "technical")
    BrowserClient().force_login(user)
    pk = user.pk
    user.delete()
    assert not User.objects.filter(pk=pk).exists()
    assert list(SignInEvent.objects.filter(user_id=pk).values_list("kind", flat=True)) == [
        Kind.SIGNED_IN
    ]


# --- The page ----------------------------------------------------------------------------------


def test_the_page_lists_only_the_persons_own_entries_newest_first(make_user):
    user = make_user("history-reader", "technical")
    other = make_user("history-stranger", "technical")
    sign_in(browser(), other.username, TYPED_SECRET)
    sign_in(browser(), user.username, TYPED_SECRET)
    http = browser()
    sign_in(http, user.username)
    page = http.get(PAGE)
    assert page.status_code == 200
    assert page["Cache-Control"].count("no-store") == 1
    entries = page.context["entries"]
    assert [(entry.kind, entry.details) for entry in entries] == [
        (Kind.SIGNED_IN, "Password"),
        (Kind.FAILED, "Password not accepted"),
    ]
    assert entries[0].this_session and not entries[1].this_session
    assert entries[0].browser == "Firefox on Windows" and entries[0].address == "192.0.2.10"
    body = page.content.decode()
    assert "This browser" in body and "1 attempt listed below was not let through" in body
    assert "history-stranger" not in body and TYPED_SECRET not in body


def test_a_portal_account_reads_its_own_history(make_user):
    from clients.models import Client

    organization = Client.objects.create(name="Example Clinic", account_number="EXCLIN")
    user = make_user("history-portal", client=organization)
    http = browser()
    sign_in(http, user.username)
    page = http.get(PAGE)
    assert page.status_code == 200
    assert [entry.kind for entry in page.context["entries"]] == [Kind.SIGNED_IN]


def test_the_page_needs_a_sign_in(client):
    response = client.get(PAGE)
    assert response.status_code == 302
    assert response.url.startswith(reverse("account_login"))


def test_the_page_shows_ninety_days_and_at_most_a_hundred_entries(make_user):
    user = make_user("history-long", "technical")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_signinevent (user_id, occurred_at, kind, factor, ending, "
            "user_agent) SELECT %s, now() - interval '91 days', 'failed', 'password', '', '' "
            "UNION ALL SELECT %s, now() - (n || ' minutes')::interval, 'failed', 'password', "
            "'', '' FROM generate_series(1, 120) AS n",
            [user.pk, user.pk],
        )
    http = browser()
    sign_in(http, user.username)
    entries = http.get(PAGE).context["entries"]
    assert len(entries) == signin_history.RECENT_LIMIT == 100
    assert entries[0].kind == Kind.SIGNED_IN
    assert all(entry.occurred_at > entries[-1].occurred_at for entry in entries[:-1])


def test_a_shared_account_sees_other_visitors_entries_without_browser_or_address(make_user):
    user = make_user("history-shared", "client_services", credentials_locked=True)
    other_visitor = browser()
    sign_in(other_visitor, user.username)
    sign_in(BrowserClient(HTTP_USER_AGENT=AGENT, REMOTE_ADDR="198.51.100.7"), user.username, "x")
    http = BrowserClient(HTTP_USER_AGENT="curl/8.0", REMOTE_ADDR="203.0.113.5")
    sign_in(http, user.username)
    page = http.get(PAGE)
    body = page.content.decode()
    entries = page.context["entries"]
    assert [entry.this_session for entry in entries] == [True, False, False]
    assert entries[0].address == "203.0.113.5"
    assert [(entry.browser, entry.address) for entry in entries[1:]] == [(None, None)] * 2
    assert "192.0.2.10" not in body and "198.51.100.7" not in body
    table = body.split("<table", 1)[1].split("</table>", 1)[0]
    assert "Firefox" not in table and table.count("Not shown") == 4
    assert "Shared demonstration account" in body
    assert page.context["attention_count"] == 1 and "listed below was not let through" not in body


def test_the_page_is_linked_from_the_user_menu_and_where_you_are_signed_in(make_user):
    user = make_user("history-links", "technical")
    http = browser()
    sign_in(http, user.username)
    assert f'href="{PAGE}"' in http.get(reverse("account-sessions")).content.decode()
    assert f'href="{PAGE}"' in http.get(PAGE).content.decode()


def test_the_page_reads_a_bounded_number_of_queries(make_user, django_assert_max_num_queries):
    user = make_user("history-queries", "technical")
    http = browser()
    for _ in range(4):  # the fifth failure in five minutes would refuse the sign-in
        sign_in(http, user.username, TYPED_SECRET)
    sign_in(http, user.username)
    http.get(PAGE)
    with django_assert_max_num_queries(25):
        assert http.get(PAGE).status_code == 200


# --- Upgrade and reversal ----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_session_open_at_the_upgrade_records_its_sign_out_and_reversal_removes_the_history():
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    before = [("accounts", "0008_compromised_password")]
    executor.migrate(before)
    try:
        then = executor.loader.project_state(before).apps.get_model("accounts", "AccountSession")
        account = User.objects.create_user("history-upgrade")
        opened = then.objects.create(user_id=account.pk)
    finally:
        MigrationExecutor(connection).migrate(latest)

    assert events(account) == []
    sessions.end_sessions(account, Ending.ADMINISTRATOR)
    assert events(account) == [(Kind.SIGNED_OUT, "", Ending.ADMINISTRATOR)]
    assert SignInEvent.objects.get(user=account).session == opened.pk

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        with connection.cursor() as cursor:
            assert "accounts_signinevent" not in connection.introspection.table_names(cursor)
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                "AND tgrelid = 'accounts_accountsession'::regclass"
            )
            assert cursor.fetchone()[0] == 0
        # Sessions start and end as before without the history.
        then = executor.loader.project_state(before).apps.get_model("accounts", "AccountSession")
        row = then.objects.create(user_id=account.pk)
        then.objects.filter(pk=row.pk).update(ended_at=row.started_at, ending="signed_out")
    finally:
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
    with transaction.atomic():
        User.objects.filter(pk=account.pk).update(is_active=False)


# --- Round 1 review: the cases next to the findings ---------------------------------------------


# Not named "page": pytest-playwright would parametrize it, and pytest would then group every
# browser-parametrized test (transactional ones included) beside it.
@pytest.mark.parametrize("surface", ["account", "admin"])
def test_a_current_password_not_accepted_on_change_password_is_a_failed_reauthentication(
    make_user, surface
):
    user = make_user("history-current", "admin", is_staff=True, is_superuser=(surface == "admin"))
    http = browser()
    sign_in(http, user.username)
    if surface == "account":
        url, fields = reverse("account_change_password"), ("oldpassword", "password1", "password2")
    else:
        url = reverse("admin:password_change")
        fields = ("old_password", "new_password1", "new_password2")
    new = "copper lantern orchard seventeen"
    refused = http.post(url, dict(zip(fields, (TYPED_SECRET, new, new), strict=True)))
    assert refused.status_code == 200
    accepted = http.post(url, dict(zip(fields, (PASSWORD, new, new), strict=True)))
    assert accepted.status_code == 302
    assert events(user) == [
        (Kind.SIGNED_IN, SignInFactor.PASSWORD, ""),
        (Kind.REAUTHENTICATION_FAILED, SignInFactor.PASSWORD, ""),
    ]
    assert nothing_typed_is_stored()


def test_refusals_by_the_limit_are_recorded_once_a_minute_for_each_address(make_user, settings):
    user = make_user("history-flood", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    first = browser()
    assert sign_in(first, user.username).url == reverse("mfa_authenticate")
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    cache.clear()
    for _ in range(4):
        first.post(reverse("mfa_authenticate"), {"code": wrong_code(secret)})
    elsewhere = BrowserClient(HTTP_USER_AGENT=AGENT, REMOTE_ADDR="198.51.100.20")
    settings.ACCOUNT_RATE_LIMITS = {}
    sign_in(elsewhere, user.username)
    settings.ACCOUNT_RATE_LIMITS = {"login_failed": "1/m/key"}
    elsewhere.post(reverse("mfa_authenticate"), {"code": wrong_code(secret)})
    lockouts = SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT)
    assert sorted(str(entry.address) for entry in lockouts) == ["192.0.2.10", "198.51.100.20"]
    assert all(entry.factor == SignInFactor.SECOND_FACTOR for entry in lockouts)
    # One written more than a minute ago does not hold back the next.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_signinevent (user_id, occurred_at, kind, factor, ending, "
            "user_agent, address) VALUES (%s, now() - interval '2 minutes', 'locked_out', "
            "'password', '', '', NULL)",
            [user.pk],
        )
    signin_history.record(user, Kind.LOCKED_OUT, None, factor=SignInFactor.PASSWORD)
    recent = SignInEvent.objects.filter(user=user, kind=Kind.LOCKED_OUT, factor="password")
    assert recent.count() == 2


def test_an_account_deactivated_at_the_second_step_is_refused(make_user):
    user = make_user("history-late-deactivation", "technical")
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    http = browser()
    assert sign_in(http, user.username).url == reverse("mfa_authenticate")
    user.is_active = False
    user.save(update_fields=["is_active"])
    http.post(reverse("mfa_authenticate"), {"code": code_for(secret)})
    assert not AccountSession.objects.filter(user=user, ended_at__isnull=True).exists()
    assert [(kind, ending) for kind, _, ending in events(user)] == [
        (Kind.REFUSED, Ending.DEACTIVATED)
    ]
    assert http.get(PAGE).status_code == 302
    assert not User.objects.get(pk=user.pk).last_login


def test_a_later_sign_in_in_the_same_browser_does_not_take_earlier_steps(make_user):
    user = make_user("history-steps", "technical")
    http = browser()
    sign_in(http, user.username)
    http.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    http.force_login(user)
    assert events(user)[-1] == (Kind.SIGNED_IN, "", "")
    assert session_row(http).factor == ""


@pytest.mark.parametrize("code", ["", "   "])
def test_an_emailed_code_form_sent_without_a_code_is_not_recorded(make_user, settings, code):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    user = make_user("history-blank-code", "technical")
    http = browser()
    request_code(http, user.email)
    http.post(reverse("account_confirm_login_code"), {"code": code})
    assert events(user) == []
