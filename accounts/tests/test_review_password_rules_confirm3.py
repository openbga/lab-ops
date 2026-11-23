"""Third confirmation of the reset hand-off, announcement and form changes in 7b24e1e.

Adopted from the independent review's third confirmation; three strict xfails are accepted
behaviour of the reset's hand-off to the seed, documented in core/management/commands/seed_demo.py
(published_passwords and Command.stealth_options)."""

import pytest
from django.core.management import CommandError, call_command, get_commands
from django.db import connection

from accounts.forms import SetPasswordForm
from accounts.models import User
from core import demo, second_factor
from core.management.commands import reset_demo, seed_demo
from core.models import Announcement, Lab

from .test_review_password_rules import EIGHT, PHRASE, rule
from .test_review_password_rules_confirm import ADMIN_PHRASE, seed
from .test_review_password_rules_confirm2 import DeploymentCheck, configured


@pytest.fixture
def demo_env(monkeypatch):
    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setenv("DEMO_PASSWORD", PHRASE)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_PHRASE)


@pytest.mark.xfail(
    strict=True,
    reason="accepted: checked_passwords is reset_demo's hand-off, reachable through Django's "
    "Python API only; a caller passing it is taken to have checked the passwords",
)
@pytest.mark.django_db
def test_standalone_seed_keeps_the_reset_handoff_out_of_its_public_options(demo_env, monkeypatch):
    """Medium, introduced at 7b24e1e. The reset hands plain values to a general command option."""
    with pytest.raises(TypeError, match="Unknown option") as error:
        call_command("seed_demo", unknown_review_option=True)
    valid_options = str(error.value).partition("Valid options are: ")[2].rstrip(".").split(", ")
    assert "announce_accounts" in valid_options
    raw_options = []

    def observe_dispatch(command, *args, **options):
        # Only observe a genuine reset. Record option names, never the values in transit.
        for name, value in options.items():
            if name in valid_options and type(value) is tuple and value == (PHRASE, ADMIN_PHRASE):
                raw_options.append(name)
        return call_command(command, *args, **options)

    monkeypatch.setattr(reset_demo, "call_command", observe_dispatch)
    seed("reset_demo", interactive=False)
    assert not raw_options, f"The standalone command accepts the plain pair via {raw_options}"


@pytest.mark.xfail(
    strict=True,
    reason="accepted: a deployment's own seed_demo command must accept reset_demo's "
    "checked_passwords (documented on Command.stealth_options)",
)
@pytest.mark.django_db
def test_reset_with_a_seed_subclass_that_declares_its_own_command_options(demo_env, monkeypatch):
    """Low, introduced at 7b24e1e. A previously compatible registered seed can no longer reset."""

    class DeploymentSeed(seed_demo.Command):
        stealth_options = ("batch_label",)

    monkeypatch.setitem(get_commands(), "seed_demo", DeploymentSeed())
    seed()
    original = User.objects.get(username="demo_admin").password
    erasures = []
    erase = reset_demo.Command._erase

    def observe(command):
        erasures.append(True)
        return erase(command)

    monkeypatch.setattr(reset_demo.Command, "_erase", observe)
    try:
        seed("reset_demo", interactive=False)
    except TypeError as error:
        assert User.objects.get(username="demo_admin").password == original
        pytest.fail(f"Registered seed failed after {len(erasures)} erasure(s): {error}")
    assert User.objects.get(username="demo_admin").check_password(ADMIN_PHRASE)


@pytest.mark.django_db(transaction=True)
def test_reset_checks_unsaved_identities_under_the_gate_and_standalone_seed_checks_afresh(
    demo_env, monkeypatch
):
    observed = []
    locks = []

    def record(self, password, user=None):
        if not observed:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND locktype = 'advisory' AND classid = %s AND objid = %s "
                    "AND objsubid = 2 AND granted), EXISTS (SELECT 1 FROM pg_locks WHERE "
                    "pid = pg_backend_pid() AND relation = %s::regclass "
                    "AND mode = 'ShareRowExclusiveLock' AND granted)",
                    [*demo.SEED_LOCK, Lab._meta.db_table],
                )
                locks.append(cursor.fetchone())
        assert user.pk is None and user.client_id is None and user.client is None
        observed.append((user.username, user.first_name, user.last_name, user.email))

    monkeypatch.setattr(DeploymentCheck, "validate", record)
    with configured(DeploymentCheck):
        seed("reset_demo", interactive=False)
        actual = sorted(
            User.objects.filter(is_active=True).values_list(
                "username", "first_name", "last_name", "email"
            )
        )
        assert len(actual) == 7 and sorted(observed) == actual
        observed.clear()
        seed()
        assert sorted(observed) == actual
    assert locks == [(True, True), (False, False)]


@pytest.mark.xfail(
    strict=True,
    reason="accepted: reset_demo checks the passwords before it erases, so a validator that reads "
    "the database sees the demonstration being replaced (documented on published_passwords)",
)
@pytest.mark.django_db
def test_reset_validation_sees_the_staff_policy_of_the_demo_it_creates(demo_env, monkeypatch):
    """Medium, introduced at 7b24e1e for validators that consult a resettable lab setting."""
    seed()
    rule(staff=True)
    settings_seen = []

    def observe(self, password, user=None):
        if user.username == "demo_admin":
            settings_seen.append(second_factor.rule().staff)

    # The observer only reads the supported rule. Both synthetic passwords meet the release rule.
    monkeypatch.setattr(DeploymentCheck, "validate", observe)
    with configured(DeploymentCheck):
        seed("reset_demo", interactive=False)
    restored = second_factor.rule().staff
    assert restored is False
    assert restored in settings_seen, "Validation only saw the policy the reset discarded"


@pytest.mark.django_db
@pytest.mark.parametrize("same_setting", [True, False], ids=["identical", "case-distinct"])
def test_announcement_on_an_empty_demo_uses_the_two_checked_values(
    demo_env, monkeypatch, same_setting
):
    private = PHRASE if same_setting else PHRASE.swapcase()
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", private)
    assert not Lab.objects.exists()
    erasures = []
    erase = reset_demo.Command._erase

    def observe(command):
        erasures.append(True)
        return erase(command)

    monkeypatch.setattr(reset_demo.Command, "_erase", observe)
    if same_setting:
        with pytest.raises(CommandError, match="also opens demo_admin") as error:
            seed("reset_demo", interactive=False, announce_accounts=True)
        assert not erasures and not Lab.objects.exists() and not Announcement.objects.exists()
        assert PHRASE not in str(error.value)
    else:
        seed("reset_demo", interactive=False, announce_accounts=True)
        assert erasures == [True]
        assert User.objects.get(username="demo_admin").check_password(private)
        body = Announcement.objects.get(show_on_sign_in=True).body
        assert PHRASE in body and private not in body


class OpaqueHelp:
    @property
    def __signature__(self):
        raise ValueError("This callable does not publish a signature")

    def __call__(self, user, *, own):
        return "Account-specific advice for this deployment."


class LegacyOpaqueValidator(DeploymentCheck):
    help_text_for = OpaqueHelp()


class NonCallableHelp(DeploymentCheck):
    help_text_for = "Use standard help"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("validator", "advice"),
    [
        (LegacyOpaqueValidator, "Account-specific advice for this deployment."),
        (NonCallableHelp, "Deployment advice."),
    ],
)
def test_help_dispatch_handles_opaque_and_noncallable_hooks(make_user, validator, advice):
    user = make_user("third-confirmation-help")
    with configured(validator):
        field = SetPasswordForm(user=user)["password1"]
        assert advice in str(field.help_text)


@pytest.mark.django_db
def test_retained_field_drops_error_attributes_after_a_successful_recheck(make_user):
    user = make_user("third-confirmation-input")
    rule(staff=False)
    form = SetPasswordForm(user=user, data={"password1": EIGHT, "password2": EIGHT})
    field = form["password1"]
    assert not form.is_valid()
    assert 'aria-invalid="true"' in str(field[0]) and "id_password1_error" in str(field[0])
    rule(staff=True)
    form.full_clean()
    assert form.is_valid() and field is form["password1"]
    refreshed = str(next(iter(field)))
    assert "aria-invalid" not in refreshed and "id_password1_error" not in refreshed
    assert "id_password1_helptext" in refreshed and "at least 8" in str(field.help_text)
