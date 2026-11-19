"""Fifth confirmation of the Security settings fixes on synthetic accounts and modules."""

import base64
import hashlib
import py_compile
import sys
import time
from importlib import import_module, invalidate_caches
from importlib.util import source_hash
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from allauth.account import app_settings as account_settings
from allauth.account.internal.flows import code_verification
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.totp.internal import auth as totp
from django.contrib.auth import SESSION_KEY
from django.test import Client, override_settings
from django.urls import reverse

from accounts import signin_log
from accounts.tests.test_login import mailed_code, request_code
from core import security_settings
from tests.test_review_security_round1 import PASSWORD, assert_changed, fact, password_sign_in
from tests.test_review_security_round1_confirm3 import handed_off, record_descriptions
from tests.test_review_security_round1_signoff import ROOT, probe


@pytest.mark.django_db
def test_record_distinguishes_expired_authenticator_and_emailed_codes(client, staff, monkeypatch):
    user = staff("fifth-clock-reader", "technical")
    secret = base64.b32encode(b"fifth synthetic clock fixture").decode("ascii")
    totp.TOTP.activate(user, secret)
    counter = 56_000_000
    period = mfa_settings.TOTP_PERIOD
    clock = SimpleNamespace(time=lambda: counter * period)
    monkeypatch.setattr(totp, "time", clock)
    issued = totp.format_hotp_value(totp.hotp_value(secret, counter))
    assert totp.validate_totp_code(secret, issued)
    challenge = client.post(
        reverse("account_login"), {"login": user.username, "password": PASSWORD}
    )
    assert challenge.url == reverse("mfa_authenticate")

    # Move only the authenticator clock beyond every accepted interval.
    clock.time = lambda: (counter + mfa_settings.TOTP_TOLERANCE + 1) * period
    assert not totp.validate_totp_code(secret, issued)
    with patch.object(signin_log.logger, "info") as recorder:
        refused = client.post(challenge.url, {"code": issued})
    assert refused.status_code == 200 and refused.context["form"].has_error("code")
    assert SESSION_KEY not in client.session
    if handed_off(recorder, "signin.failed", user):
        # The generic exclusion covers both sorts of expiring sign-in code.
        blanket = [
            name
            for name, text in record_descriptions().items()
            if any(
                "an expired code," in sentence
                and "authenticator" not in sentence
                and "emailed code" not in sentence
                for sentence in text.lower().split(". ")
            )
        ]
        assert not blanket, blanket


@pytest.mark.django_db
@pytest.mark.parametrize("past_deadline", [False, True], ids=["at-deadline", "one-second-late"])
def test_issued_email_code_at_the_expiry_boundary(client, staff, monkeypatch, past_deadline):
    account = staff("fifth-email-reader", "technical")
    start = time.time()
    clock = SimpleNamespace(time=lambda: start)
    monkeypatch.setattr(code_verification, "time", clock)
    challenge = reverse("account_confirm_login_code")
    assert request_code(client, account.email).url == challenge
    code = mailed_code()
    deadline = start + account_settings.LOGIN_BY_CODE_TIMEOUT
    clock.time = lambda: deadline + (1 if past_deadline else 0)
    with patch.object(signin_log.logger, "info") as recorder:
        result = client.post(challenge, {"code": code})
    if past_deadline:
        assert result.url == reverse("account_request_login_code")
        assert SESSION_KEY not in client.session and not recorder.called
    else:
        assert client.session[SESSION_KEY] == str(account.pk)
        assert handed_off(recorder, "signin.success", account)


@pytest.mark.django_db
def test_record_guard_names_password_logout_alone_and_with_both_existing_guards():
    key = "sign-in-records"
    release = fact(key)
    with override_settings(ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE=True):
        assert_changed(key, "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
        with override_settings(
            SESSION_ENGINE="django.contrib.sessions.backends.cache",
            ACCOUNT_ADAPTER="allauth.account.adapter.DefaultAccountAdapter",
        ):
            changed = assert_changed(
                key, "SESSION_ENGINE", "ACCOUNT_ADAPTER", "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE"
            )
            assert "wrong codes" not in changed and "lab_ops.signin" not in changed
        assert_changed(key, "ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE")
    assert release.value != security_settings.CHANGED and fact(key) == release


@pytest.mark.django_db
def test_reactivated_browser_can_explicitly_sign_out_and_record_it(client, staff):
    user = staff("fifth-session-reader", "technical")
    password_sign_in(client, user)
    original = client.session.session_key
    with patch.object(signin_log.logger, "info") as recorder:
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert client.get(reverse("dashboard")).status_code == 302
        assert not recorder.called

        # A fresh password refusal and refusal of the retained session are different events.
        fresh = Client()
        denied = fresh.post(
            reverse("account_login"), {"login": user.username, "password": PASSWORD}
        )
        assert denied.url == reverse("account_inactive") and SESSION_KEY not in fresh.session
        assert handed_off(recorder, "signin.failed", user)
        recorder.reset_mock()
        user.is_active = True
        user.save(update_fields=["is_active"])
        assert client.get(reverse("dashboard")).status_code == 200
        assert client.session.session_key == original and not recorder.called
        assert client.post(reverse("account_logout")).status_code == 302
        assert handed_off(recorder, "signout", user) and SESSION_KEY not in client.session


def test_reading_a_registered_lazy_settings_module_leaves_initialization_deferred(tmp_path):
    (tmp_path / "fifth_settings.py").write_bytes((ROOT / "config/settings.py").read_bytes())
    value, note, initialized = probe(
        tmp_path,
        """
        import sys
        from importlib.util import LazyLoader, find_spec, module_from_spec
        name = "config.settings"
        assert name not in sys.modules
        spec = find_spec(name)
        spec.loader = LazyLoader(spec.loader)
        module = module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # Looking at the real module dictionary this way does not force the lazy import.
        namespace = object.__getattribute__(module, "__dict__")
        assert "STARTUP_DEBUG" not in namespace
        row = https()
        print(json.dumps([row.value, row.note, "STARTUP_DEBUG" in namespace]))
        """,
        module="fifth_settings",
    )
    assert value == security_settings.CHANGED and "fifth_settings" in note
    assert "could not be read" not in note
    assert not initialized, "Reading a configuration fact initialized the inactive settings."


def test_digest_reads_through_the_module_loader_that_exposes_the_source(tmp_path):
    # A source loader can present archive members under a mounted directory. Its own
    # get_data understands that pathname. The archive reader it delegates to uses member names.
    source = (ROOT / "config/settings.py").read_bytes()
    (tmp_path / "fifth_settings.py").write_bytes(source)
    with ZipFile(tmp_path / "release.zip", "w") as archive:
        archive.writestr("config/settings.py", source)
    digest, value, note = probe(
        tmp_path,
        """
        import hashlib, sys
        from importlib.abc import SourceLoader
        from importlib.util import find_spec, spec_from_loader
        from zipimport import zipimporter

        class ArchiveSource(SourceLoader):
            def __init__(self, root):
                self.root = root
                self.loader = zipimporter(str(Path("release.zip").resolve()))

            def get_filename(self, fullname):
                return str(self.root / (fullname.replace(".", "/") + ".py"))

            def get_data(self, path):
                return self.loader.get_data(Path(path).relative_to(self.root).as_posix())

        class SettingsFinder:
            def find_spec(self, fullname, path, target=None):
                if fullname == "config.settings":
                    return spec_from_loader(fullname, ArchiveSource(Path("mounted").resolve()))

        sys.meta_path.insert(0, SettingsFinder())
        spec = find_spec("config.settings")
        raw = spec.loader.get_data(spec.origin)
        assert spec.loader.get_source(spec.name) == raw.decode()
        row = https()
        assert "config.settings" not in sys.modules
        print(json.dumps([hashlib.sha256(raw).hexdigest(), row.value, row.note]))
        """,
        module="fifth_settings",
    )
    assert digest == security_settings.RELEASE_SETTINGS_SHA256
    assert value == security_settings.CHANGED and "fifth_settings" in note
    assert "config/settings.py could not be read" not in note, note


@pytest.mark.parametrize("value", [17, 18], ids=["matching-revision", "later-revision"])
def test_bytecode_digest_identifies_the_revision_actually_loaded(tmp_path, monkeypatch, value):
    # Adopted: digests are recorded while the configuration loads (confirmation 5 resolution).
    # A module that records its own digest while it runs describes the revision that ran.
    name = f"fifth_source_returned_{value}"
    raw = (
        b"# Synthetic module used only in this test.\r\n"
        b"from config.digest import source_sha256\r\n"
        b"VALUE = 17\r\n"
        b"DIGEST = source_sha256(__file__, __loader__)\r\n"
    )
    source = tmp_path / (name + ".py")
    compiled = source.with_suffix(".pyc")
    source.write_bytes(raw)
    py_compile.compile(
        str(source),
        cfile=str(compiled),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    source.unlink()
    monkeypatch.syspath_prepend(str(tmp_path))
    with patch.dict(sys.modules):
        module = import_module(name)
        assert module.VALUE == 17 and module.__spec__.origin == str(compiled)
        # Loaded from bytecode with no source beside it: nothing to describe.
        assert module.DIGEST == ""
        restored = raw.replace(b"17", str(value).encode())
        source.write_bytes(restored)
        # A checked-hash pyc identifies its input, independently of the application's digest.
        original_hash = compiled.read_bytes()[8:16]
        assert original_hash == source_hash(raw)
        assert (original_hash == source_hash(restored)) == (value == 17)
        # The record made while it loaded does not change when a source appears later.
        assert module.DIGEST == ""
        # A fresh load runs the bytecode when its hash matches the source, the source when
        # not, and records the digest of the revision that ran.
        del sys.modules[name]
        invalidate_caches()
        reloaded = import_module(name)
        assert reloaded.VALUE == value
        assert reloaded.DIGEST == hashlib.sha256(restored).hexdigest()
