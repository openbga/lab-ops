"""Confirmation of the resolved compromised-password findings on synthetic accounts."""

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import PBKDF2PasswordHasher, check_password
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.deprecation import RemovedInDjango60Warning

from accounts import enrolment, sessions
from accounts.models import AccountSession, User
from core.models import SecondFactorRule

from .conftest import PASSWORD
from .test_login import mailed_code, request_code
from .test_review_password_rules import reset_form
from .test_second_factor_enrolment import code_for, session_row, sign_in

pytestmark = pytest.mark.django_db
FIRST = "Walnut shelves beside the garden window"
LAST = "Linen curtains beside a quiet orchard"


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
def signin_settings(settings):
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    reverse("account_request_login_code")  # allauth constructs these patterns once
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(make_user):
    return make_user("confirm_reader", "technical")


@pytest.fixture
def owner(make_user):
    return make_user("confirm_owner", is_superuser=True, is_staff=True)


def mark(owner, person):
    from accounts import compromised

    return compromised.mark_compromised(
        actor=owner, account_id=person.pk, reason="Review", drawn=compromised.drawn_password(person)
    ).mark


def watch_changes(monkeypatch):
    observations = []

    def changed(value, user):
        saved = User.objects.get(pk=user.pk)
        observations.append((value == LAST, check_password(value, saved.password)))

    monkeypatch.setattr(password_validation, "password_changed", changed)
    return observations


@pytest.mark.parametrize("write", ["positional", "keyword", "whole", "retry"])
@pytest.mark.xfail(strict=True, reason=STAGED_PARTIAL_SAVE)
def test_a_second_choice_survives_partial_writes_and_notifies_once(
    owner, person, monkeypatch, write
):
    entry = mark(owner, person)
    seen = watch_changes(monkeypatch)
    person.set_password(FIRST)
    person.first_name = "Revised"
    with pytest.warns(RemovedInDjango60Warning):
        person.save(False, False, None, iter(["first_name"]))
    person.set_password(LAST)
    person.save(update_fields=iter([]))
    entry.refresh_from_db()
    assert not seen and entry.lifted_at is None
    assert User.objects.get(pk=person.pk).check_password(PASSWORD)
    if write == "retry":
        with pytest.raises(ValueError):
            person.save(update_fields=["password", "no_such_field"])
        assert not seen
    if write == "positional":
        with pytest.warns(RemovedInDjango60Warning):
            person.save(False, False, None, iter(["password", "email"]))
    elif write == "whole":
        person.save()
    else:
        person.save(update_fields=iter(["password"]))
    person.save(update_fields=["password"])
    entry.refresh_from_db()
    assert entry.lifted_at is not None and seen == [(True, True)]


@pytest.mark.parametrize("discard", ["unusable", "none", "reload"])
def test_discarding_a_staged_value_never_announces_it_as_the_saved_password(
    person, monkeypatch, discard
):
    seen = watch_changes(monkeypatch)
    person.set_password(FIRST)
    person.save(update_fields=["first_name"])
    seen.clear()  # isolate notifications during the write after the choice was discarded
    if discard == "unusable":
        person.set_unusable_password()
    elif discard == "none":
        person.set_password(None)
    else:
        person.refresh_from_db(fields=["password"])
    person.save(update_fields=["password"])
    saved = User.objects.get(pk=person.pk)
    assert saved.has_usable_password() == (discard == "reload")
    assert not seen, seen


def test_reloading_a_password_before_a_hash_upgrade_keeps_the_upgrade_out_of_validator_history(
    person, monkeypatch
):
    hasher = PBKDF2PasswordHasher()
    old = hasher.encode(PASSWORD, hasher.salt(), iterations=100)
    User.objects.filter(pk=person.pk).update(password=old)
    seen = watch_changes(monkeypatch)
    person.set_password(FIRST)
    person.save(update_fields=["first_name"])
    seen.clear()
    person.refresh_from_db(fields=["password"])
    chosen = []
    original = getattr(User, "_check_chosen_while_marked", None)

    def observe_choice(instance):
        chosen.append(instance.pk)
        return original(instance)

    if original is not None:
        monkeypatch.setattr(User, "_check_chosen_while_marked", observe_choice)
    assert person.check_password(PASSWORD)
    encoded = User.objects.values_list("password", flat=True).get(pk=person.pk)
    assert encoded != old and not hasher.must_update(encoded)
    assert (len(chosen), seen) == (0, [])


@pytest.mark.parametrize("identity", ["anonymous", "same", "different"])
def test_valid_reset_link_replaces_only_its_target_when_marked_during_submission(
    owner, person, make_user, client, identity, monkeypatch
):
    neighbor = make_user("confirm_neighbor")
    if identity != "anonymous":
        client.force_login(person if identity == "same" else neighbor)
    url = reset_form(client, person)
    original = User.set_password
    entries = []

    def mark_before_write(account, value):
        if account.pk == person.pk and not entries:
            entries.append(mark(owner, person))
        original(account, value)

    monkeypatch.setattr(User, "set_password", mark_before_write)
    answer = client.post(url, {"password1": LAST, "password2": LAST})
    assert answer.status_code == 302 and len(entries) == 1
    entries[0].refresh_from_db()
    assert entries[0].lifted_at is not None
    assert User.objects.get(pk=person.pk).check_password(LAST)
    assert User.objects.get(pk=neighbor.pk).check_password(PASSWORD)


def real_signin(client, person, method):
    if method == "email-code":
        assert request_code(client, person.email).url == reverse("account_confirm_login_code")
        return client.post(reverse("account_confirm_login_code"), {"code": mailed_code()})
    if method in ("totp", "recovery"):
        secret = generate_totp_secret()
        TOTP.activate(person, secret)
        token = (
            RecoveryCodes.activate(person).get_unused_codes()[0]
            if method == "recovery"
            else code_for(secret)
        )
        first = sign_in(client, person)
        assert first.url == reverse("mfa_authenticate")
        return client.post(first.url, {"code": token})
    login = person.email if method == "address" else person.username
    return client.post(reverse("account_login"), {"login": login, "password": PASSWORD})


def log_events(caplog, user):
    return [
        r.getMessage().split()[0]
        for r in caplog.records
        if r.name == "lab_ops.signin" and f"user={user.username}" in r.getMessage().split()
    ]


@pytest.fixture(params=["ordinary-order", "starter-last"])
def receiver_order(request):
    # Reproduce the supported receiver reorder and restore the dispatcher's exact original order.
    if request.param == "ordinary-order":
        yield
        return
    signal = user_logged_in
    saved = signal.receivers.copy()
    uid = "lab_ops_account_session_start"
    try:
        assert signal.disconnect(dispatch_uid=uid)
        signal.connect(sessions.start_session, dispatch_uid=uid)
        yield
    finally:
        with signal.lock:
            signal.receivers[:] = saved
            signal.sender_receivers_cache.clear()


@pytest.mark.parametrize(
    ("method", "marked"),
    [
        ("username", False),
        ("address", False),
        ("username", True),
        ("email-code", False),
        ("email-code", True),
        ("totp", False),
        ("totp", True),
        ("recovery", False),
        ("recovery", True),
    ],
)
def test_log_and_session_agree_after_completed_or_refused_authentication(
    owner,
    person,
    client,
    receiver_order,
    method,
    marked,
    caplog,
    django_capture_on_commit_callbacks,
):
    if marked:
        mark(owner, person)
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    with django_capture_on_commit_callbacks(execute=True):
        answer = real_signin(client, person, method)
    assert answer.status_code == 302
    row = session_row(client)
    refused = marked and method == "username"
    assert (row.ended_at is not None) == refused
    assert log_events(caplog, person) == [
        "signout.password_compromised" if refused else "signin.success"
    ]
    if marked and not refused:
        assert client.get(reverse("account-new-password")).status_code == 200
        row.refresh_from_db()
        assert row.ended_at is None


@pytest.mark.parametrize(
    "failure",
    [
        "insert",
        pytest.param("outer-rollback", marks=pytest.mark.xfail(strict=True, reason=OUTER_ROLLBACK)),
    ],
)
def test_an_aborted_signin_has_no_completed_event(person, client, caplog, monkeypatch, failure):
    caplog.set_level(logging.INFO, logger="lab_ops.signin")

    class StopSignin(Exception):
        pass

    if failure == "insert":

        def stop(**kwargs):
            raise StopSignin

        monkeypatch.setattr(AccountSession.objects, "create", stop)
    with pytest.raises(StopSignin), transaction.atomic():
        client.force_login(person)
        if failure == "outer-rollback":
            raise StopSignin
    person.refresh_from_db()
    assert person.last_login is None and not AccountSession.objects.filter(user=person).exists()
    assert log_events(caplog, person) == []


def rollout_snapshot(owner, person):
    """Seed an open mark beside a pre-existing synthetic session, as on an upgraded database."""
    from accounts.models import CompromisedPassword

    return CompromisedPassword.objects.create(
        user=person, marked_by=owner, password=person.password, reason="Synthetic rollout fixture"
    )


@pytest.mark.parametrize(
    ("kind", "route"),
    [
        ("page", "account-new-password"),
        ("head", "account-new-password"),
        ("post", "account-new-password"),
        ("partial", "account-new-password"),
        ("dialog", "account-new-password"),
        ("script", "account-new-password"),
        ("file", "attachments:download"),
        ("status", "account-session"),
        ("continue", "account-session-continue"),
        ("keep", "account-session-keep"),
        ("public", "health"),
        ("page", "account-second-factor"),
        ("page", "mfa_index"),
        ("page", "mfa_activate_totp"),
        ("page", "mfa_view_recovery_codes"),
        ("page", "mfa_download_recovery_codes"),
        ("get-only", "mfa_generate_recovery_codes"),
        ("page", "account_reauthenticate"),
        ("page", "mfa_reauthenticate"),
    ],
)
def test_password_only_session_ends_on_dispatch_before_either_hold(
    person, owner, client, kind, route
):
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    assert sign_in(client, person).status_code == 302
    row = session_row(client)
    assert row.ended_at is None and row.second_factor_required
    rollout_snapshot(owner, person)
    url = reverse(route, args=[1] if kind == "file" else [])
    headers = {
        "partial": {"HX-Request": "true"},
        "dialog": {"X-VR-Form": "1"},
        "script": {"Sec-Fetch-Dest": "empty"},
    }.get(kind, {})
    if kind == "keep":
        answer = client.post(url, {"forms": []}, content_type="application/json")
    else:
        send = {"post": client.post, "continue": client.post, "head": client.head}.get(
            kind, client.get
        )
        answer = send(url, headers=headers)
    row.refresh_from_db()
    assert row.ending == AccountSession.Ending.MARKED_COMPROMISED and row.ended_at is not None
    assert "_auth_user_id" not in client.session
    if kind in {"script", "dialog", "partial"}:
        assert answer.status_code == 401 and answer.json()["signed_in"] is False
    elif kind in {"status", "continue"}:
        assert answer.json()["signed_in"] is False
    elif kind == "keep":
        assert answer.json()["kept"] == 0
    elif kind == "public":
        assert answer.status_code == 200
    else:
        assert answer.status_code in {302, 405}
        if answer.status_code == 302:
            assert reverse("account_login") in answer.url


@pytest.mark.parametrize("surface", ["unmatched", "csrf"])
@pytest.mark.xfail(strict=True, reason=EARLY_ERROR_PAGE)
def test_early_error_response_settles_the_same_session_snapshot(person, owner, client, surface):
    assert sign_in(client, person).status_code == 302
    row = session_row(client)
    rollout_snapshot(owner, person)
    browser = Client(enforce_csrf_checks=True)
    browser.cookies = client.cookies.copy()
    with CaptureQueriesContext(connection) as queries:
        answer = (
            browser.get("/unknown-confirmation-page/")
            if surface == "unmatched"
            else browser.post(reverse("account-session-continue"))
        )
    assert answer.status_code == (404 if surface == "unmatched" else 403)
    reads = [q for q in queries if "accounts_compromisedpassword" in q["sql"]]
    assert len(reads) == 1
    assert b'aria-label="Primary"' not in answer.content
    row.refresh_from_db()
    assert row.ending == AccountSession.Ending.MARKED_COMPROMISED and row.ended_at is not None


def test_another_accounts_real_code_record_is_disregarded_by_both_guards(
    person, owner, client, rf, monkeypatch
):
    from accounts import compromised

    assert real_signin(client, owner, "email-code").status_code == 302
    request = rf.get(reverse("account-second-factor"))
    request.session = client.session
    request.user = person
    assert compromised.proved_beyond_password(request, owner)
    assert not compromised.proved_beyond_password(request, person)
    request.account_session = SimpleNamespace(user_id=person.pk)
    ended = object()
    monkeypatch.setattr(compromised, "_end_unproved", lambda request: ended)
    monkeypatch.setattr(enrolment, "holds", lambda state: (True, True))
    assert enrolment.EnrolmentMiddleware(None).process_view(request, None, (), {}) is ended
    request.vr_password_marked = request.vr_second_factor_pending = True
    assert compromised.PasswordChangeMiddleware(None).process_view(request, None, (), {}) is ended


@pytest.mark.django_db(transaction=True)
def test_signout_notice_uses_the_ending_already_recorded_at_the_write(
    person, owner, client, monkeypatch
):
    assert sign_in(client, person).status_code == 302
    row = session_row(client)
    rollout_snapshot(owner, person)
    earlier = AccountSession.Ending.ADMINISTRATOR
    read = enrolment.holds
    winners = []

    def administrative_ending():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '3s'")
            return sessions.end_sessions(person, earlier, session_id=row.pk)
        finally:
            connections.close_all()

    def read_then_finish(state):
        result = read(state)
        if result[1] and not winners:
            with ThreadPoolExecutor(max_workers=1) as pool:
                winners.append(pool.submit(administrative_ending).result(timeout=10))
        return result

    monkeypatch.setattr(enrolment, "holds", read_then_finish)
    answer = client.get(reverse("account-session"))
    assert answer.json()["signed_in"] is False and winners == [1]
    row.refresh_from_db()
    assert row.ending == earlier and row.ended_at is not None
    notices = [str(m) for m in client.get(reverse("account_login")).context["messages"]]
    assert sessions.ENDED_BY_MESSAGES[earlier] in notices
    assert sessions.ENDED_BY_MESSAGES[AccountSession.Ending.MARKED_COMPROMISED] not in notices


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("mail-and-code", "mail-only"),
        ("mail-only", "mail-and-code"),
        ("no-mail", "mail-and-code"),
        ("mail-and-code", "no-mail"),
        ("no-mail-with-code-setting", "no-mail-with-code-setting"),
    ],
)
def test_mark_preview_and_confirmation_each_follow_the_current_delivery_choices(
    person, owner, client, settings, before, after
):
    def configure(value):
        settings.EMAIL_BACKEND = (
            "django.core.mail.backends.dummy.EmailBackend"
            if value.startswith("no-mail")
            else "django.core.mail.backends.locmem.EmailBackend"
        )
        settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = value in (
            "mail-and-code",
            "no-mail-with-code-setting",
        )

    configure(before)
    client.force_login(owner)
    url = reverse("admin:accounts_user_compromised", args=[person.pk])
    page = client.get(url)
    preview = (
        page.content.decode().split('<form method="post" id="user_form">')[1].split("</form>")[0]
    )
    assert ("code emailed" in preview) == (before == "mail-and-code")
    assert ("Forgot your password?" in preview) == (not before.startswith("no-mail"))
    configure(after)
    result = client.post(url, {"drawn": page.context["form"]["drawn"].value(), "reason": "Review"})
    notices = [str(m) for m in client.get(result.url).context["messages"]]
    confirmation = next(message for message in notices if "as compromised" in message)
    assert ("code emailed" in confirmation) == (after == "mail-and-code")
    assert ("Forgot your password?" in confirmation) == (not after.startswith("no-mail"))
    assert "account's password page" in confirmation


def test_mark_help_qualifies_the_reset_advice_when_the_laboratory_sends_no_mail(
    person, owner, client, settings
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    settings.ACCOUNT_LOGIN_BY_CODE_ENABLED = False
    client.force_login(owner)
    page = client.get(reverse("admin:accounts_user_compromised", args=[person.pk]))
    task = next(
        task
        for task in page.context["page_documentation"]["tasks"]
        if task["title"] == "Tell the person what to do"
    )
    advice = next(s for s in task["steps"] if "Forgot your password?" in s)
    reset_clause = advice.split("or sign in")[0].split("or you set")[0].lower()
    assert "sends no email" in page.content.decode()
    assert any(word in reset_clause for word in ("where", "if ", "when", "enabled", "available")), (
        advice
    )


def test_account_field_follows_the_current_mark_then_returns_to_the_own_account_instruction(
    owner, make_user, rf
):
    from django.contrib import admin

    from accounts import compromised

    another = make_user("confirm_second_owner", is_superuser=True, is_staff=True)
    display = admin.site._registry[User]
    request = rf.get("/")
    request.user = owner
    subject = display.get_object(request, str(owner.pk))
    assert display.compromised_password(subject) == compromised.REFUSALS["own"]
    mark(another, owner)
    marked = str(display.compromised_password(subject))
    assert "Marked on" in marked and another.username in marked and "href=" not in marked
    owner.set_password(LAST)
    owner.save(update_fields=["password"])
    assert display.compromised_password(subject) == compromised.REFUSALS["own"]


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("hidden-only", ""),
        ("ordinary-and-hidden", "Your entries were kept."),
        ("two-cleared", "Type the passwords again."),
        ("one-cleared", "except the passwords"),
        ("all-retained", "Your entries were kept."),
    ],
)
def test_summary_classifies_only_visible_fields_that_drop_values(shape, expected):
    from core.templatetags.ui import password_fields

    class CustomPassword(forms.PasswordInput):
        pass

    form = forms.Form(data={"a": "synthetic alpha", "b": "synthetic beta"})
    form.fields["stamp"] = forms.CharField(widget=forms.HiddenInput)
    if shape != "hidden-only":
        form.fields["a"] = forms.CharField(
            widget=(
                forms.TextInput()
                if shape in {"ordinary-and-hidden", "one-cleared"}
                else CustomPassword(render_value=shape == "all-retained")
            )
        )
    if shape in ("two-cleared", "one-cleared", "all-retained"):
        form.fields["b"] = forms.CharField(
            widget=forms.PasswordInput(render_value=shape == "all-retained")
        )
    assert form.has_error("stamp")
    if shape == "hidden-only":
        assert password_fields(form) == ""
    else:
        dropped = [f for f in form.visible_fields() if 'value="synthetic' not in f.as_widget()]
        assert len(dropped) == {"two-cleared": 2, "one-cleared": 1}.get(shape, 0)
        assert expected in render_to_string("includes/form_errors.html", {"form": form})


@pytest.mark.parametrize("waiting", [False, True])
def test_verified_email_session_follows_the_required_hold_and_stays_open(
    person, owner, client, waiting
):
    if waiting:
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
    entry = mark(owner, person)
    assert real_signin(client, person, "email-code").status_code == 302
    row = session_row(client)
    target = reverse("account-second-factor" if waiting else "account-new-password")
    assert client.get(reverse("account_change_password")).url == target
    assert client.post(reverse("account_change_password"), {}).url == target
    with CaptureQueriesContext(connection) as queries:
        answer = client.get("/missing-confirmation-resource/")
    assert answer.status_code == 404
    assert answer.context["session_held"]
    assert answer.context["second_factor_pending"] == waiting
    assert answer.context["password_change_pending"] != waiting
    assert len([q for q in queries if "accounts_compromisedpassword" in q["sql"]]) == 1
    row.refresh_from_db()
    entry.refresh_from_db()
    assert row.ended_at is None and entry.lifted_at is None


@pytest.mark.parametrize("channel", ["totp", "email-code"])
def test_intermediate_authentication_has_no_success_line_until_the_last_step(
    owner, person, client, caplog, channel
):
    mark(owner, person)
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    if channel == "totp":
        secret = generate_totp_secret()
        TOTP.activate(person, secret)
        next_step = sign_in(client, person).url
        assert next_step == reverse("mfa_authenticate")
        token = code_for(secret)
    else:
        next_step = request_code(client, person.email).url
        assert next_step == reverse("account_confirm_login_code")
        token = mailed_code()
    assert (
        not log_events(caplog, person) and not AccountSession.objects.filter(user=person).exists()
    )
    assert client.post(next_step, {"code": token}).status_code == 302
    assert session_row(client).ended_at is None and log_events(caplog, person) == ["signin.success"]


def test_password_middleware_settles_a_session_even_without_an_enrolment_decision(
    owner, person, client, settings
):
    settings.MIDDLEWARE = [
        name for name in settings.MIDDLEWARE if name != "accounts.enrolment.EnrolmentMiddleware"
    ]
    assert sign_in(client, person).status_code == 302
    row = session_row(client)
    rollout_snapshot(owner, person)
    assert client.get(reverse("account-session")).json()["signed_in"] is False
    row.refresh_from_db()
    assert row.ending == AccountSession.Ending.MARKED_COMPROMISED


@pytest.mark.xfail(strict=True, reason=MIXED_PASSWORDS)
def test_summary_distinguishes_a_retained_password_from_an_empty_one():
    class MixedPasswordForm(forms.Form):
        retained = forms.CharField(widget=forms.PasswordInput(render_value=True))
        cleared = forms.CharField(widget=forms.PasswordInput)
        required_value = forms.CharField()

    form = MixedPasswordForm(data={"retained": "synthetic alpha", "cleared": "synthetic beta"})
    assert form.has_error("required_value")
    assert 'value="synthetic alpha"' in form["retained"].as_widget()
    assert 'value="synthetic beta"' not in form["cleared"].as_widget()
    summary = render_to_string("includes/form_errors.html", {"form": form})
    assert "Your entries were kept, except the passwords" not in summary
    assert "Your entries were kept." not in summary and "Type the passwords again." not in summary


@pytest.mark.parametrize("entry", ["admin", "enrolment"])
def test_completed_signin_is_logged_once_on_the_admin_and_enrolment_entry_paths(
    owner, person, client, caplog, entry
):
    caplog.set_level(logging.INFO, logger="lab_ops.signin")
    if entry == "admin":
        page = client.get(reverse("admin:index"), follow=True)
        assert page.resolver_match.view_name == "account_login"
        account = owner
        answer = client.post(
            page.request["PATH_INFO"],
            {"login": owner.username, "password": PASSWORD, "next": reverse("admin:index")},
        )
        assert answer.status_code == 302
        assert client.get(reverse("admin:index")).status_code == 200
    else:
        account = person
        SecondFactorRule.objects.update_or_create(pk=1, defaults={"roles": ["technical"]})
        assert sign_in(client, person).status_code == 302
        assert client.get(reverse("dashboard")).url == reverse("account-second-factor")
    assert session_row(client).ended_at is None
    assert log_events(caplog, account) == ["signin.success"]
