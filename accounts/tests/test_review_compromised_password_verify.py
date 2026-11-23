"""Independent round-one verification using synthetic accounts and deployment settings."""

import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.core.cache import cache
from django.db import connection, connections
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import AccountSession, User
from accounts.sessions import SESSION_KEY
from core.models import AuditEvent, SecondFactorRule

from .conftest import PASSWORD
from .test_login import mailed_code, request_code

pytestmark = pytest.mark.django_db
FRESH = "Linen bound the autumn notebook"


# Accepted in review (see .local/review-history/compromised-password-round-1-resolution.md).
STAGED_PARTIAL_SAVE = (
    "Accepted (accounts.models.User.save): a password set, then a save of other columns, then a "
    "save of the password lifts nothing and Django tells the validators at the first save; no "
    "path of the application saves that way."
)
EARLY_ERROR_PAGE = (
    "Accepted (docs/operations.md): a Not found or CSRF page, drawn before any view, shows no "
    "record; the session ends at its next request for a page."
)
OUTER_ROLLBACK = (
    "Accepted (predates): the sign-in log writes a completed sign-in once its session has "
    "started; the application opens no transaction around sign-in that could roll it back."
)
MIXED_PASSWORDS = (
    "Accepted (core.templatetags.ui.password_fields): no form has a password field that keeps its "
    "value beside one drawn empty."
)


@pytest.fixture(autouse=True)
def clear_limits(db):
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def feature():
    return pytest.importorskip("accounts.compromised")


@pytest.fixture
def owner(make_user):
    return make_user("verify_owner", is_staff=True, is_superuser=True)


@pytest.fixture
def person(make_user):
    return make_user("verify_reader", "technical")


def mark(feature, owner, person):
    return feature.mark_compromised(
        actor=owner, account_id=person.pk, drawn=feature.drawn_password(person), reason="Review"
    ).mark


def email_login(browser, person):
    assert request_code(browser, person.email).url == reverse("account_confirm_login_code")
    result = browser.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert result.status_code == 302
    return AccountSession.objects.get(pk=browser.session[SESSION_KEY])


def drawn(browser):
    response = browser.get(reverse("account-new-password"))
    assert response.status_code == 200
    field = response.context["form"]["mark"]
    assert field.is_hidden and field.field.required
    assert f'value="{field.value()}"' in field.as_widget()
    return str(field.value())


@pytest.mark.parametrize(
    "example", ["drawn", "missing", "malformed", "unknown", "other", "superseded"]
)
def test_replacement_uses_the_mark_from_the_rendered_form(
    feature, owner, person, client, make_user, settings, example
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    first = mark(feature, owner, person)
    email_login(client, person)
    seen = drawn(client)
    assert seen == str(first.pk)
    data = {"new_password1": FRESH, "new_password2": FRESH, "mark": seen}
    expected = first
    if example == "missing":
        del data["mark"]
    elif example == "malformed":
        data["mark"] = "no mark number"
    elif example == "unknown":
        data["mark"] = str(first.pk + 1000)
    elif example == "other":
        other = make_user("verify_other")
        data["mark"] = str(mark(feature, owner, other).pk)
    elif example == "superseded":
        # Keep the first form. After the administrator legitimately replaces the password,
        # the original session ends. A new, independently verified sign-in sees the next mark.
        person.set_password("Field notes about glass and linen")
        person.save(update_fields=["password"])
        expected = mark(feature, owner, person)
        client = Client()
        email_login(client, person)
        assert drawn(client) == str(expected.pk) != seen
    previous_encoding = User.objects.values_list("password", flat=True).get(pk=person.pk)
    result = client.post(reverse("account-new-password"), data)
    person.refresh_from_db()
    expected.refresh_from_db()
    if example == "drawn":
        assert result.status_code == 302 and person.check_password(FRESH)
        assert expected.lifted_at is not None
        assert AuditEvent.objects.get(action=feature.CHANGED_ACTION).after == {
            "compromised_mark": first.pk
        }
    else:
        assert person.password == previous_encoding and expected.lifted_at is None
        assert not AuditEvent.objects.filter(action=feature.CHANGED_ACTION).exists()
        if example in {"missing", "malformed"}:
            assert result.status_code == 200 and result.context["form"].has_error("mark")
        else:
            assert result.url == reverse("account-new-password")
            again = client.get(result.url)
            assert feature.NOT_THE_DRAWN_MARK in [str(m) for m in again.context["messages"]]
            assert str(again.context["form"]["mark"].value()) == str(expected.pk)


STAGED = pytest.mark.xfail(
    strict=True,
    reason="Accepted (accounts.models.User.save): a password set, then a save of other columns, "
    "then a save of the password lifts nothing; no path of the application saves that way.",
)


@pytest.mark.parametrize(
    "shape",
    [
        "whole",
        "password",
        "both",
        pytest.param("staged-twice", marks=STAGED),
        pytest.param("empty-then-save", marks=STAGED),
    ],
)
def test_only_the_real_chosen_password_write_lifts_a_mark(feature, owner, person, shape):
    entry = mark(feature, owner, person)
    old = person.password
    person.set_password(FRESH)
    person.first_name = "Updated"
    if shape in ("staged-twice", "empty-then-save"):
        partials = [["first_name"], ["email"]] if shape == "staged-twice" else [[]]
        for columns in partials:
            person.save(update_fields=columns)
            assert User.objects.values_list("password", flat=True).get(pk=person.pk) == old
            entry.refresh_from_db()
            assert entry.lifted_at is None
    if shape == "whole":
        person.save()
    else:
        person.save(update_fields=["password", "first_name"] if shape == "both" else ["password"])
    entry.refresh_from_db()
    assert entry.lifted_at is not None and User.objects.get(pk=person.pk).check_password(FRESH)


def test_first_insert_with_chosen_password_does_not_touch_another_accounts_mark(
    feature, owner, person
):
    entry = mark(feature, owner, person)
    added = User(username="verify_new", email="verify_new@example.invalid")
    added.set_password(FRESH)
    added.save(force_insert=True)
    entry.refresh_from_db()
    assert added.check_password(FRESH) and added._password is None
    assert entry.lifted_at is None and not feature.is_marked(added.pk)


@pytest.mark.parametrize("staged", [False, True])
def test_disabling_password_signin_preserves_the_open_mark(feature, owner, person, staged):
    entry = mark(feature, owner, person)
    if staged:
        person.set_password(FRESH)
        person.first_name = "Updated"
        person.save(update_fields=["first_name"])
    person.set_unusable_password()
    person.save(update_fields=["password"])
    assert not User.objects.get(pk=person.pk).has_usable_password()
    entry.refresh_from_db()
    assert entry.lifted_at is None, "Disabling sign-in was recorded as a chosen replacement"


@pytest.mark.xfail(strict=True, reason=STAGED_PARTIAL_SAVE)
def test_password_validators_receive_one_change_after_the_password_is_persisted(
    person, monkeypatch
):
    observed = []

    def changed(value, account):
        observed.append(User.objects.get(pk=account.pk).check_password(value))

    monkeypatch.setattr(password_validation, "password_changed", changed)
    person.set_password(FRESH)
    person.first_name = "Updated"
    person.save(update_fields=["first_name"])
    person.save(update_fields=["email"])
    person.save(update_fields=["password"])
    assert observed == [True], "Each callback should describe a password actually stored"


def test_a_profile_save_using_djangos_positional_update_fields_keeps_the_session(person, client):
    from django.utils.deprecation import RemovedInDjango60Warning

    client.force_login(person)
    row = AccountSession.objects.get(pk=client.session[SESSION_KEY])
    person.set_password(FRESH)
    person.first_name = "Updated"
    with pytest.warns(RemovedInDjango60Warning):
        person.save(False, False, None, ["first_name"])
    saved = User.objects.get(pk=person.pk)
    assert saved.first_name == "Updated" and saved.check_password(PASSWORD)
    row.refresh_from_db()
    assert row.ended_at is None, "The only persisted change was a profile field"


def test_one_shot_update_fields_persists_the_named_profile_column(person):
    person.first_name = "Updated"
    person.save(update_fields=iter(["first_name"]))
    person.refresh_from_db()
    assert person.first_name == "Updated"


def test_a_real_hash_upgrade_leaves_the_chosen_password_hook_quiet(
    feature, owner, person, monkeypatch, client
):
    hasher = PBKDF2PasswordHasher()
    old = hasher.encode(PASSWORD, hasher.salt(), iterations=100)
    User.objects.filter(pk=person.pk).update(password=old)
    person.refresh_from_db()
    entry = mark(feature, owner, person)
    changed = []
    monkeypatch.setattr(password_validation, "password_changed", lambda *a: changed.append(True))
    client.post(reverse("account_login"), {"login": person.username, "password": PASSWORD})
    entry.refresh_from_db()
    person.refresh_from_db()
    assert entry.password == old != person.password and entry.lifted_at is None
    assert person.check_password(PASSWORD) and not changed


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "path",
    ["allauth-own", "admin-own", "signed-in-reset", "anonymous-reset", "admin-target", "command"],
)
def test_change_already_authorized_when_administrator_marks_the_account(
    feature, owner, make_user, monkeypatch, path
):
    from io import StringIO

    from django.core.management import call_command

    from accounts.password_command import Command

    from .test_review_password_rules import reset_form

    person = make_user("verify_inflight", is_staff=True, is_superuser=True)
    writer = Client()
    if path not in ("anonymous-reset", "command"):
        writer.force_login(owner if path == "admin-target" else person)
    if path.endswith("reset"):
        url = reset_form(writer, person)
    elif path == "admin-target":
        url = reverse("admin:auth_user_password_change", args=[person.pk])
    elif path != "command":
        url = reverse(
            "account_change_password" if path == "allauth-own" else "admin:password_change"
        )

    saved_marks = []
    original = User.set_password

    def mark_on_its_own_connection():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '3s'")
                cursor.execute("SET statement_timeout = '8s'")
            return mark(feature, owner, User.objects.get(pk=person.pk)).pk
        finally:
            connections.close_all()

    def when_chosen(account, value):
        if account.pk == person.pk and not saved_marks:
            with ThreadPoolExecutor(max_workers=1) as pool:
                saved_marks.append(pool.submit(mark_on_its_own_connection).result(timeout=12))
        original(account, value)

    monkeypatch.setattr(User, "set_password", when_chosen)
    if path == "command":
        monkeypatch.setattr(Command, "_get_pass", lambda *args: FRESH)
        call_command("changepassword", person.username, stdout=StringIO())
    else:
        values = {"password1": FRESH, "password2": FRESH}
        if path == "admin-own":
            values = {"old_password": PASSWORD, "new_password1": FRESH, "new_password2": FRESH}
        elif path == "allauth-own":
            values["oldpassword"] = PASSWORD
        elif path == "admin-target":
            values["usable_password"] = "true"
        answer = writer.post(url, values)
        assert answer.status_code == 302
    assert len(saved_marks) == 1
    entry = feature.CompromisedPassword.objects.get(pk=saved_marks[0])
    person.refresh_from_db()
    if path in ("allauth-own", "admin-own"):
        assert entry.lifted_at is None and person.check_password(PASSWORD)
        assert answer.url.startswith(reverse("account_login"))
    else:
        assert (
            entry.lifted_at is not None,
            person.check_password(FRESH),
            person.check_password(PASSWORD),
        ) == (True, True, False), path


@pytest.mark.parametrize("audience", ["ordinary", "marked", "enrolment", "both"])
def test_holds_reads_both_states_in_one_sql_statement(
    feature, owner, person, client, settings, audience
):
    from accounts import enrolment

    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    marked = audience in ("marked", "both")
    waiting = audience in ("enrolment", "both")
    if marked:
        mark(feature, owner, person)
    if waiting:
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    row = email_login(client, person)
    with CaptureQueriesContext(connection) as queries:
        flags = enrolment.holds(SimpleNamespace(id=row.pk, user_id=person.pk))
    assert flags == (waiting, marked)
    assert len(queries) == 1
    with CaptureQueriesContext(connection) as anonymous_queries:
        assert enrolment.holds(None) == (False, False)
    assert not anonymous_queries.captured_queries


@pytest.mark.parametrize(
    "transport",
    [
        "page",
        "head",
        "form",
        "dialog",
        "partial",
        "script",
        "file",
        "status",
        "admin:index",
        "account_email",
        "mfa_reauthenticate",
        "account-sessions",
        "desk:request-find",
        "whats-new",
    ],
)
def test_dispatched_requests_consult_the_open_mark_once(
    feature, owner, person, client, settings, transport
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark(feature, owner, person)
    email_login(client, person)
    url = reverse("desk:request-create")
    headers = {
        "dialog": {"X-VR-Form": "1"},
        "partial": {"HX-Request": "true"},
        "script": {"Sec-Fetch-Dest": "empty"},
    }.get(transport, {})
    if transport == "file":
        url = reverse("attachments:download", args=[1])
    elif transport == "status":
        url = reverse("account-session")
    elif transport in {
        "admin:index",
        "account_email",
        "mfa_reauthenticate",
        "account-sessions",
        "desk:request-find",
        "whats-new",
    }:
        url = reverse(transport)
    send = {"head": client.head, "form": client.post}.get(transport, client.get)
    with CaptureQueriesContext(connection) as queries:
        answer = send(url, headers=headers)
    assert answer.status_code == {"dialog": 204, "partial": 204, "script": 403, "status": 200}.get(
        transport, 302
    )
    if transport == "status":
        assert answer.json()["signed_in"]
    elif transport == "script":
        assert answer.json() == {
            "password_change_required": True,
            "location": reverse("account-new-password"),
        }
    else:
        assert answer.get("HX-Redirect", answer.get("Location")) == reverse("account-new-password")
    reads = [q["sql"] for q in queries if "accounts_compromisedpassword" in q["sql"]]
    assert len(reads) == 1 and "accounts_accountsession" in reads[0]


@pytest.mark.parametrize("surface", ["404", "csrf"])
def test_shell_without_a_dispatched_view_reuses_the_same_mark_read(
    feature, owner, person, client, settings, surface
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    mark(feature, owner, person)
    email_login(client, person)
    browser = Client(enforce_csrf_checks=True)
    browser.cookies = client.cookies.copy()
    with CaptureQueriesContext(connection) as queries:
        result = (
            browser.get("/no-such-verification-page/")
            if surface == "404"
            else browser.post(reverse("account-session-continue"))
        )
    assert result.status_code == (404 if surface == "404" else 403)
    assert result.context["session_held"] and result.context["password_change_pending"]
    assert b'aria-label="Primary"' not in result.content
    reads = [q["sql"] for q in queries if "accounts_compromisedpassword" in q["sql"]]
    assert len(reads) == 1 and "accounts_accountsession" in reads[0], len(reads)


def test_sign_in_log_distinguishes_each_attempt_in_the_same_browser(
    feature, owner, person, client, settings, caplog, django_capture_on_commit_callbacks
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    caplog.set_level(logging.INFO, logger="lab_ops.signin")

    def events():
        return [
            record.getMessage().split()[0]
            for record in caplog.records
            if record.name == "lab_ops.signin" and f"user={person.username}" in record.getMessage()
        ]

    login = reverse("account_login")
    credentials = {"login": person.username, "password": PASSWORD}
    client.post(login, credentials)
    assert events() == ["signin.success"]
    client.post(reverse("account_logout"))
    mark(feature, owner, person)
    caplog.clear()
    client.post(login, {**credentials, "password": "synthetic wrong value"})
    assert events() == ["signin.failed"]
    assert not AccountSession.objects.filter(user=person, ended_at__isnull=True).exists()
    caplog.clear()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(login, credentials)
    client.get(login)
    assert events() == ["signout.password_compromised"]
    caplog.clear()
    assert email_login(client, person).ended_at is None
    assert events() == ["signin.success"]


@pytest.mark.parametrize(
    "mode", ["off", "on", "authenticator-off", "authenticator-on", "codes-disabled"]
)
def test_admin_and_ended_browser_describe_available_recovery(
    feature, owner, person, settings, mode
):
    mail_on = mode in ("on", "authenticator-on", "codes-disabled")
    codes = mode in ("on", "authenticator-on")
    # allauth builds its URL patterns once. Load the enabled routes before toggling
    # the deployment setting for the page, as the sign-in suite does.
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    link = reverse("account_request_login_code")
    settings.EMAIL_BACKEND = (
        "django.core.mail.backends." + ("locmem" if mail_on else "dummy") + ".EmailBackend"
    )
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = codes
    if mode.startswith("authenticator"):
        TOTP.activate(person, generate_totp_secret())
    administrator, participant = Client(), Client()
    administrator.force_login(owner)
    participant.force_login(person)
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = administrator.get(url)
    answer = administrator.post(
        url, {"drawn": page.context["form"]["drawn"].value(), "reason": "Review"}
    )
    assert answer.status_code == 302
    confirmation = " ".join(str(m) for m in administrator.get(answer.url).context["messages"])
    end = participant.get(reverse("dashboard"), follow=True)
    notice = " ".join(str(m) for m in end.context["messages"])
    assert "administrator" in notice.lower()
    assert ("Forgot your password?" in notice) == mail_on
    offered = bool(re.search(r'href="' + re.escape(link) + r'(?:\?[^"]*)?"', end.content.decode()))
    assert offered == codes
    if mode.startswith("authenticator"):
        assert "authenticator code" in confirmation
        assert "account has no second factor" not in confirmation
    else:
        assert "account's password page" in confirmation
        assert ("Forgot your password?" in confirmation) == mail_on
        assert ("code emailed" in confirmation) == offered


def test_two_administrators_see_the_action_only_for_each_other(feature, owner, make_user):
    second = make_user("verify_second_admin", is_superuser=True, is_staff=True)
    for viewer, subject in ((owner, second), (second, owner)):
        browser = Client()
        browser.force_login(viewer)
        for target in (viewer, subject, viewer):
            page = browser.get(reverse("admin:accounts_user_change", args=[target.pk]))
            link = reverse("admin:accounts_user_compromised", args=[target.pk])
            assert (f'href="{link}"' in page.content.decode()) == (viewer.pk != target.pk)
            assert (feature.REFUSALS["own"] in page.content.decode()) == (viewer.pk == target.pk)


def test_account_help_explains_the_absent_action_on_ones_own_page(feature, owner, client):
    client.force_login(owner)
    page = client.get(reverse("admin:accounts_user_change", args=[owner.pk]))
    reference = next(
        item["description"]
        for section in page.context["page_documentation"]["sections"]
        for item in section.get("items", [])
        if item.get("label") == "Compromised password"
    )
    assert re.search(r"(your own|own account|yourself)", reference, re.I), reference


def test_own_account_readonly_field_still_identifies_an_existing_mark(
    feature, owner, make_user, rf
):
    from django.contrib import admin

    second = make_user("verify_second_admin", is_superuser=True, is_staff=True)
    display = admin.site._registry[User]
    request = rf.get("/")
    request.user = owner
    subject = display.get_object(request, str(owner.pk))
    # The account page is already being assembled when the second administrator marks it.
    entry = mark(feature, second, owner)
    text = str(display.compromised_password(subject))
    assert feature.open_mark(owner.pk).pk == entry.pk
    assert "href=" not in text
    assert second.username in text and "Marked on" in text, text


SUMMARY = "includes/form_errors.html"


@pytest.mark.parametrize(
    ("form_name", "expected"),
    [
        ("LoginForm", "mixed"),
        ("ReauthenticateForm", "all"),
        ("ChangePasswordForm", "all"),
        ("SetPasswordForm", "all"),
        ("ResetPasswordKeyForm", "all"),
        ("NewPasswordForm", "all"),
        ("ResetPasswordForm", "ordinary"),
        ("RequestLoginCodeForm", "ordinary"),
        ("ConfirmLoginCodeForm", "ordinary"),
        ("AddEmailForm", "ordinary"),
        ("MFAAuthenticateForm", "ordinary"),
        ("MFAReauthenticateForm", "ordinary"),
        ("ReasonForm", "ordinary"),
    ],
)
def test_real_forms_show_what_must_be_reentered(person, form_name, expected):
    from allauth.account import forms as account

    from accounts import forms as local
    from desk.forms import ReasonForm

    if form_name == "NewPasswordForm":
        cls = pytest.importorskip("accounts.compromised").NewPasswordForm
    elif form_name == "ReasonForm":
        cls = ReasonForm
    elif form_name in {
        "ChangePasswordForm",
        "SetPasswordForm",
        "ResetPasswordKeyForm",
        "MFAAuthenticateForm",
        "MFAReauthenticateForm",
    }:
        cls = getattr(local, form_name)
    else:
        cls = getattr(account, form_name)
    no_user = {
        "LoginForm",
        "ResetPasswordForm",
        "RequestLoginCodeForm",
        "ConfirmLoginCodeForm",
        "ReasonForm",
    }
    args = {} if form_name in no_user else {"user": person}
    form = cls(data={}, **args)
    assert form.errors, form_name
    html = render_to_string(SUMMARY, {"form": form})
    messages = {
        "mixed": "Your entries were kept, except the passwords",
        "all": "Type the passwords again.",
        "ordinary": "Your entries were kept.",
    }
    assert messages[expected] in html
    assert sum(text in html for text in messages.values()) == 1


def test_shared_summary_covers_the_declared_fields_of_project_forms(record_property):
    from importlib import import_module
    from inspect import getmembers, isclass

    from django.apps import apps
    from django.forms.utils import ErrorDict
    from django.utils.module_loading import module_has_submodule

    # Feed each class's actual declared widgets to the shared component. Model choices and
    # business-specific constructor arguments do not take part in rendering the summary.
    modules = {
        "accounts.admin",
        "core.admin",
        "core.announcement_forms",
        "core.lab_profile_forms",
        "desk.lists",
        "desk.notification_views",
        "desk.portal_forms",
        "request_forms.intake",
        "allauth.account.forms",
        "allauth.mfa.base.forms",
        "allauth.mfa.totp.forms",
    }
    if module_has_submodule(import_module("accounts"), "compromised"):
        modules.add("accounts.compromised")
    for app in apps.get_app_configs():
        if Path(app.path).parent != Path(__file__).resolve().parents[2]:
            continue
        if not module_has_submodule(app.module, "forms"):
            continue
        modules.add(f"{app.name}.forms")
    examined = set()
    for module_id in sorted(modules):
        module = import_module(module_id)
        for name, cls in getmembers(module, isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, forms.BaseForm):
                continue
            if not cls.base_fields:
                continue
            form = forms.Form(data={})
            form.fields = cls.base_fields.copy()
            form._errors = ErrorDict()
            form.cleaned_data = {}
            form.add_error(next(iter(form.fields)), "Check this field")
            html = render_to_string(SUMMARY, {"form": form})
            visible = form.visible_fields()
            cleared = [
                f
                for f in visible
                if isinstance(f.field.widget, forms.PasswordInput)
                and not f.field.widget.render_value
            ]
            if form.is_multipart():
                assert "Your entries were kept." not in html
            elif not cleared:
                assert "Your entries were kept." in html, name
            elif len(cleared) == len(visible):
                assert "Type the passwords again." in html, name
            else:
                assert "except the passwords" in html, name
            examined.add((module.__name__, name))
    record_property("declared_form_classes", len(examined))
    assert ("desk.forms", "RequestCreateForm") in examined and (
        "clients.forms",
        "ClientForm",
    ) in examined


@pytest.mark.parametrize("widget_case", ["subclass", "retain"])
def test_shared_summary_follows_the_password_widgets_actual_redisplay(settings, widget_case):
    from allauth.account.fields import PasswordField

    class ConfirmationInput(forms.PasswordInput):
        pass

    class Example(forms.Form):
        label = forms.CharField()

    settings.ACCOUNT_PASSWORD_INPUT_RENDER_VALUE = widget_case == "retain"
    form = Example(data={"confirmation": "synthetic field content"})
    form.fields["confirmation"] = (
        PasswordField() if widget_case == "retain" else forms.CharField(widget=ConfirmationInput)
    )
    assert form.has_error("label")
    kept = 'value="synthetic field content"' in form["confirmation"].as_widget()
    html = render_to_string(SUMMARY, {"form": form})
    assert ("Your entries were kept." in html) == kept
    assert ("except the passwords" in html) != kept


@pytest.mark.parametrize(
    "child_password",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted (predates; core.templatetags.ui.password_fields): the summary "
                "looks at the form's own fields only, and no form has password fields in its "
                "child rows.",
            ),
        ),
    ],
)
def test_shared_summary_accounts_for_child_rows(child_password):
    class Header(forms.Form):
        name = forms.CharField()

    class Child(forms.Form):
        value = forms.CharField(widget=forms.PasswordInput if child_password else forms.TextInput)

    rows = forms.formset_factory(Child, min_num=1, validate_min=True)(
        data={
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-0-value": "synthetic field content",
        }
    )
    form = Header(data={})
    assert form.errors and rows.is_valid()
    kept = 'value="synthetic field content"' in rows[0]["value"].as_widget()
    html = render_to_string(SUMMARY, {"form": form, "formset": rows})
    assert ("Your entries were kept." in html) == kept


@pytest.fixture
def previous_process():
    checkout = Path("/tmp/labops-cpw-base")
    if not (checkout / ".venv/bin/python").is_file():
        pytest.skip("requires the previous release checkout named in the review brief")
    name = connection.settings_dict["NAME"]
    url = urlsplit(os.environ["DATABASE_URL"])
    if not name.startswith("test_labops_cpw_review") or (url.hostname, url.port) != (
        "127.0.0.1",
        55432,
    ):
        pytest.skip("requires a local disposable review database")
    env = dict(
        os.environ,
        DATABASE_URL=urlunsplit(url._replace(path="/" + name)),
        DJANGO_SETTINGS_MODULE="config.settings",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("TEST_DATABASE_NAME", None)

    def run(script, *arguments):
        return subprocess.run(
            [str(checkout / ".venv/bin/python"), "-c", script, *arguments],
            cwd=checkout,
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )

    return run


@pytest.mark.django_db(transaction=True)
def test_temporarily_deactivating_one_account_applies_on_both_releases(
    previous_process, feature, owner, person, make_user
):
    """An existing, reversible operational control for an urgent case during a rollout."""
    control = make_user("verify_control")
    browser = Client()
    browser.force_login(person)
    previous = AccountSession.objects.get(pk=browser.session[SESSION_KEY])
    person.is_active = False
    person.save(update_fields=["is_active"])
    previous.refresh_from_db()
    assert previous.ending == AccountSession.Ending.DEACTIVATED and previous.ended_at is not None
    entry = mark(feature, owner, person)
    from django.contrib.auth import authenticate

    assert authenticate(username=person.username, password=PASSWORD) is None
    assert authenticate(username=control.username, password=PASSWORD).pk == control.pk
    script = """
import sys, django
django.setup()
from django.contrib.auth import authenticate
from accounts.tests.conftest import PASSWORD
blocked, control = sys.argv[1:]
assert authenticate(username=blocked, password=PASSWORD) is None
assert authenticate(username=control, password=PASSWORD).username == control
"""
    previous_process(script, person.username, control.username)
    entry.refresh_from_db()
    assert entry.lifted_at is None
    person.set_password(FRESH)
    person.save(update_fields=["password"])
    person.is_active = True
    person.save(update_fields=["is_active"])
    assert authenticate(username=person.username, password=FRESH).pk == person.pk
    assert not feature.is_marked(person.pk)
    previous.refresh_from_db()
    assert previous.ended_at is not None


@pytest.mark.django_db(transaction=True)
def test_current_worker_can_read_the_factor_evidence_written_by_the_previous_release(
    previous_process, feature, person, make_user
):
    """Inspect two legitimate old-release sign-ins made before either account has a mark."""
    from django.contrib.sessions.models import Session

    second = make_user("verify_code_reader")
    previous_process(
        """
import sys, django
django.setup()
from django.test import Client, override_settings
from django.urls import reverse
from accounts.tests.conftest import PASSWORD
from accounts.tests.test_login import request_code, mailed_code
with override_settings(ALLOWED_HOSTS=["testserver"], ACCOUNT_LOGIN_BY_CODE_ENABLED=True,
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
    by_password, by_code = Client(), Client()
    assert by_password.post(reverse("account_login"),
                            {"login": sys.argv[1], "password": PASSWORD}).status_code == 302
    assert request_code(by_code, sys.argv[2]).url == reverse("account_confirm_login_code")
    response = by_code.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    assert response.status_code == 302
    assert "_auth_user_id" in by_code.session and "_auth_user_id" in by_password.session
""",
        person.username,
        second.email,
    )
    seen = {}
    for stored in Session.objects.all():
        data = stored.get_decoded()
        account = {str(person.pk): person, str(second.pk): second}.get(data.get("_auth_user_id"))
        if account is not None:
            seen[account.pk] = feature.proved_beyond_password(
                SimpleNamespace(session=data), account
            )
    assert seen == {person.pk: False, second.pk: True}


def test_cleaned_form_is_still_bound_to_its_original_mark_at_the_write(feature, owner, person):
    first = mark(feature, owner, person)
    form = feature.NewPasswordForm(
        person, data={"mark": str(first.pk), "new_password1": FRESH, "new_password2": FRESH}
    )
    assert form.is_valid()
    person.set_password("Letters from the western orchard")
    person.save(update_fields=["password"])
    current = mark(feature, owner, person)
    encoding = person.password
    assert (
        feature.choose_new_password(
            user=person,
            password=form.cleaned_data["new_password1"],
            mark_id=form.cleaned_data["mark"],
        )
        is None
    )
    person.refresh_from_db()
    current.refresh_from_db()
    assert person.password == encoding and current.lifted_at is None
    assert not AuditEvent.objects.filter(action=feature.CHANGED_ACTION).exists()


@pytest.mark.xfail(strict=True, reason=STAGED_PARTIAL_SAVE)
def test_deferred_profile_save_and_later_whole_save_lift_only_on_password_write(
    feature, owner, person
):
    entry = mark(feature, owner, person)
    editing = User.objects.only("first_name").get(pk=person.pk)
    editing.set_password(FRESH)
    editing.first_name = "Updated"
    editing.save(update_fields=["first_name"])
    entry.refresh_from_db()
    assert entry.lifted_at is None and User.objects.get(pk=person.pk).check_password(PASSWORD)
    editing.save()
    entry.refresh_from_db()
    assert entry.lifted_at is not None and User.objects.get(pk=person.pk).check_password(FRESH)


def test_security_facts_report_reordered_hashers_even_if_all_can_still_be_read(settings):
    from core.security_settings import CHANGED, in_force

    def relevant():
        return [
            fact
            for group in in_force()
            for fact in group.facts
            if fact.key in {"passwords", "compromised-password"}
        ]

    defaults = list(settings.PASSWORD_HASHERS)
    assert len(relevant()) == 2 and all(fact.value != CHANGED for fact in relevant())
    settings.PASSWORD_HASHERS = [defaults[1], defaults[0], *defaults[2:]]
    assert all(fact.value == CHANGED and "PASSWORD_HASHERS" in fact.note for fact in relevant())
    settings.PASSWORD_HASHERS = defaults
    assert all(fact.value != CHANGED for fact in relevant())


def test_mark_help_lists_the_working_email_option_in_the_task_for_the_administrator(
    feature, settings, owner, person, client
):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    client.force_login(owner)
    page = client.get(reverse("admin:accounts_user_compromised", args=[person.pk]))
    help_page = page.context["page_documentation"]
    task = next(t for t in help_page["tasks"] if t["title"] == "Tell the person what to do")
    steps = " ".join(task["steps"])
    notes = " ".join(help_page["good_to_know"])
    assert "emailed sign-in code" in notes
    assert "Forgot your password?" in steps
    assert re.search(r"email.{0,20}code|code.{0,20}email", steps, re.I), steps
