"""Whether staff confirm it is them before the first reveal of patient information in a session
(core.reveal_confirmation): the admin role turns it on or off on Security settings with a reason,
against the revision the form showed, with one audit event and its own confirmation; the page lists
it and its history names each change."""

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts import confirmation
from accounts.models import User
from clients.models import Client
from core import reveal_confirmation
from core.models import AuditEvent, RevealConfirmationRule
from core.security_settings import in_force

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-729!"
EDIT = "/lab/security/reveal-confirmation/"


def make_user(name, *roles, **extra):
    user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def form_data(revision=0, reason="Patient-linked tickets need it.", on=True):
    data = {"expected_revision": revision, "reason": reason}
    if on:
        data["first_reveal"] = "on"
    return data


def fact(key):
    for group in in_force():
        for item in group.facts:
            if item.key == key:
                return item
    raise AssertionError(key)


def test_nothing_is_asked_until_the_rule_is_saved():
    rule = reveal_confirmation.rule()
    assert rule.pk == 1 and rule._state.adding and not rule.first_reveal
    assert not RevealConfirmationRule.objects.exists()
    assert fact("patient-reveal-confirmation").value == "Not asked"


def test_saving_records_the_rule_before_and_after_with_the_reason():
    admin = make_user("reveal_admin", "admin")
    rule, changed = reveal_confirmation.save_rule(
        actor=admin, first_reveal=True, expected_revision=0, reason="  Shared workstations.  "
    )
    assert changed and rule.revision == 1 and RevealConfirmationRule.objects.get().first_reveal
    event = AuditEvent.objects.get(action=reveal_confirmation.ACTION)
    assert event.actor == admin and event.reason == "Shared workstations."
    assert event.before == {"first_reveal": False} and event.after == {"first_reveal": True}
    # The same value again changes nothing and records nothing.
    _, changed = reveal_confirmation.save_rule(
        actor=admin, first_reveal=True, expected_revision=1, reason="Again."
    )
    assert not changed and AuditEvent.objects.filter(action=reveal_confirmation.ACTION).count() == 1
    fact_now = fact("patient-reveal-confirmation")
    assert fact_now.value == "Asked once in each session"
    assert fact_now.source_label == "Revealing patient information, above"


def test_a_rule_changed_after_the_form_opened_is_not_overwritten():
    admin = make_user("stale_reveal_admin", "admin")
    reveal_confirmation.save_rule(actor=admin, first_reveal=True, expected_revision=0, reason="1")
    for revision in (0, None):
        with pytest.raises(ValidationError, match="changed after you opened this form"):
            reveal_confirmation.save_rule(
                actor=admin, first_reveal=False, expected_revision=revision, reason="2"
            )
    stored = RevealConfirmationRule.objects.get(pk=1)
    assert stored.first_reveal and stored.revision == 1


@pytest.mark.parametrize(
    ("roles", "extra"),
    [(("supervisor",), {}), ((), {"is_staff": True, "is_superuser": True}), (("admin",), {})],
)
def test_only_an_active_administrator_saves_the_rule(roles, extra):
    actor = make_user("not_allowed", *roles, **extra)
    if "admin" in roles:
        actor.is_active = False
        actor.save()
    with pytest.raises(PermissionDenied):
        reveal_confirmation.save_rule(
            actor=actor, first_reveal=True, expected_revision=0, reason="x"
        )
    assert not RevealConfirmationRule.objects.exists()


def test_a_portal_account_in_the_admin_group_cannot_save_it():
    organization = Client.objects.create(name="Example Clinic")
    actor = make_user("portal_admin", "admin", client=organization)
    with pytest.raises(PermissionDenied):
        reveal_confirmation.save_rule(
            actor=actor, first_reveal=True, expected_revision=0, reason="x"
        )


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_only_yes_or_no_is_saved(value):
    admin = make_user("strict_reveal_admin", "admin")
    with pytest.raises(ValidationError) as caught:
        reveal_confirmation.save_rule(
            actor=admin, first_reveal=value, expected_revision=0, reason="x"
        )
    assert "first_reveal" in caught.value.error_dict


def test_a_reason_is_required():
    admin = make_user("reasonless_reveal_admin", "admin")
    with pytest.raises(ValidationError) as caught:
        reveal_confirmation.save_rule(
            actor=admin, first_reveal=True, expected_revision=0, reason=" "
        )
    assert "reason" in caught.value.error_dict


def test_the_page_lists_the_rule_and_its_history(client):
    admin = make_user("page_reveal_admin", "admin")
    client.force_login(admin)
    html = client.get(reverse("security-settings")).content.decode()
    assert 'id="reveal-confirmation-heading"' in html and f'href="{EDIT}"' in html
    assert '<span class="vr-muted">Not asked</span>' in html
    reveal_confirmation.save_rule(
        actor=admin, first_reveal=True, expected_revision=0, reason="Shared workstations."
    )
    html = client.get(reverse("security-settings")).content.decode()
    assert "Asked once in each session</span>" in html
    assert (
        "Confirm it is you before the first reveal in a session: asked once in each session "
        "(was: not asked)"
    ) in html
    assert "Shared workstations." in html
    # Read-only for other staff who may open Security settings? Only the admin role opens it.
    supervisor = make_user("page_reveal_supervisor", "supervisor")
    client.force_login(supervisor)
    assert client.get(reverse("security-settings")).status_code == 403


def test_other_staff_and_portal_accounts_cannot_open_the_form(client):
    organization = Client.objects.create(name="Example Clinic")
    for user in (
        make_user("form_supervisor", "supervisor"),
        make_user("form_superuser", is_staff=True, is_superuser=True),
        make_user("form_portal", client=organization),
    ):
        client.force_login(user)
        assert client.get(EDIT).status_code == 403, user.username
        assert client.post(EDIT, form_data()).status_code == 403, user.username
    client.logout()
    response = client.get(EDIT)
    assert response.status_code == 302 and response.url.startswith(reverse("account_login"))
    assert not RevealConfirmationRule.objects.exists()


def test_the_form_saves_and_returns_to_security_settings(client):
    admin = make_user("form_reveal_admin", "admin")
    client.force_login(admin)
    page = client.get(EDIT)
    assert page.status_code == 200
    assert 'name="expected_revision" value="0"' in page.content.decode()
    response = client.post(EDIT, form_data())
    assert response.status_code == 302 and response.url == reverse("security-settings")
    assert RevealConfirmationRule.objects.get(pk=1).first_reveal
    # Unticked turns it off again.
    response = client.post(EDIT, form_data(revision=1, on=False, reason="Not needed."))
    assert response.status_code == 302
    stored = RevealConfirmationRule.objects.get(pk=1)
    assert not stored.first_reveal and stored.revision == 2


def test_a_stale_form_is_refused_with_the_input_kept(client):
    admin = make_user("stale_form_reveal_admin", "admin")
    client.force_login(admin)
    reveal_confirmation.save_rule(actor=admin, first_reveal=True, expected_revision=0, reason="x")
    response = client.post(EDIT, form_data(revision=0, on=False, reason="Kept reason 8812."))
    html = response.content.decode()
    assert response.status_code == 200 and "changed after you opened this form" in html
    assert "Kept reason 8812." in html
    assert RevealConfirmationRule.objects.get(pk=1).first_reveal


def test_a_form_without_a_reason_is_refused(client):
    admin = make_user("form_reasonless_reveal", "admin")
    client.force_login(admin)
    response = client.post(EDIT, form_data(reason=""))
    assert response.status_code == 200
    assert not RevealConfirmationRule.objects.exists()


@pytest.mark.confirmation
def test_saving_asks_the_administrator_to_confirm_it_is_them(client):
    admin = make_user("confirming_reveal_admin", "admin")
    client.force_login(admin)
    page = client.get(EDIT).content.decode()
    assert f'name="{confirmation.PASSWORD_FIELD}"' in page
    refused = client.post(EDIT, form_data())
    assert refused.status_code == 200 and not RevealConfirmationRule.objects.exists()
    wrong = client.post(EDIT, {**form_data(), confirmation.PASSWORD_FIELD: "not-it-3381"})
    assert "Incorrect password." in wrong.content.decode()
    assert not RevealConfirmationRule.objects.exists()
    saved = client.post(EDIT, {**form_data(), confirmation.PASSWORD_FIELD: PASSWORD})
    assert saved.status_code == 302 and RevealConfirmationRule.objects.get(pk=1).first_reveal
    event = AuditEvent.objects.get(action=reveal_confirmation.ACTION)
    assert PASSWORD not in str([event.before, event.after, event.reason])
