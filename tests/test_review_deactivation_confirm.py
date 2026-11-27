"""Confirm the revised preview, redraw and audit on ordinary synthetic account changes."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from test_review_deactivation import (
    DIRECT,
    account_data,
    account_events,
    label_payload,
    submit,
    token,
    waiting_post,
)
from test_review_deactivation_verify import committed_post

from accounts import deactivation
from accounts.models import Deactivation, User
from accounts.tests.test_confirm_identity import page_data
from accounts.tests.test_deactivation import (
    PASSWORD,
    browser_for,
    capability,
    change_url,
    make_owner,
    page_url,
)
from accounts.tests.test_deactivation import owner as owner
from accounts.tests.test_deactivation import person as person
from accounts.tests.test_deactivation import team as team
from clients.models import ClientSite, PortalAccess

pytestmark = pytest.mark.django_db
REASON = "Synthetic rotation complete"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("when", ["before comparison", "after comparison"])
def test_portal_site_rename_has_a_definite_outcome_on_each_side_of_comparison(
    owner, client_org, portal_user, monkeypatch, when
):
    reviewer = browser_for(owner)
    manager = make_owner("site-editor")
    manager.groups.add(Group.objects.get_or_create(name="admin")[0])
    editor = browser_for(manager)
    target = portal_user(client_org)
    site = ClientSite.objects.create(name="East & West", code="EW", client=client_org)
    other = ClientSite.objects.create(name="North", code="N", client=client_org)
    access = PortalAccess.objects.create(client=client_org, user=target)
    access.sites.add(site, other)
    url = reverse("clients:site-edit", args=[client_org.pk, site.pk])
    new_name = "Central <annex>"
    fields = page_data(editor.get(url).context["form"], name=new_name, reason="Site renamed")

    shown = reviewer.get(page_url(target))
    expected = shown.context["held"]
    assert expected["portal"] == "Tickets of East & West, North, and those it raised"
    assert expected["portal"] in label_payload(shown)
    if when == "before comparison":
        committed_post(editor, url, fields)
    else:
        save = User.save

        def save_after_rename(account, *args, **kwargs):
            if account.pk == target.pk and not account.is_active:
                committed_post(editor, url, fields)
            return save(account, *args, **kwargs)

        monkeypatch.setattr(User, "save", save_after_rename)

    response = submit(reviewer, target, shown)
    site.refresh_from_db()
    assert site.name == new_name
    if when == "before comparison":
        assert response.status_code == 200
        assert deactivation.CHANGED_SINCE_DRAWN in response.content.decode()
        assert not Deactivation.objects.filter(user=target).exists()
        assert not account_events(target).exists()
        expected = response.context["held"]
        assert expected["portal"] == "Tickets of Central <annex>, North, and those it raised"
        assert expected["portal"] in label_payload(response)
        assert token(response) != token(shown)
        response = submit(reviewer, target, response)

    assert response.status_code == 302 and response.url == change_url(target)
    row = Deactivation.objects.get(user=target)
    assert row.held == expected and row.deactivated_by_id == owner.pk
    assert expected["portal"] in label_payload(reviewer.get(change_url(target)))


@pytest.mark.parametrize("later_save", ["Active box", "model"])
def test_rolled_back_preview_and_later_model_saves_keep_independent_readings(
    owner, person, team, later_save
):
    reviewer = browser_for(owner)
    preview = reviewer.get(page_url(person))
    first = preview.context["held"]
    with transaction.atomic():
        assert submit(reviewer, person, preview, reason=REASON).status_code == 302
        assert Deactivation.objects.get(user=person).held == first
        transaction.set_rollback(True)
    assert not Deactivation.objects.exists() and not account_events(person).exists()
    person.refresh_from_db()
    assert person.is_active
    team.name = "Second bench"
    team.save(update_fields=["name"])
    refused = submit(reviewer, person, preview, reason=REASON)
    second = refused.context["held"]
    assert second != first and second["teams"] == [{"text": team.name, "note": "Lead"}]
    assert submit(reviewer, person, refused, reason=REASON).status_code == 302
    accepted = Deactivation.objects.get(user=person)
    assert accepted.held == second and accepted.reason == REASON
    assert (
        reviewer.post(
            change_url(person), account_data(reviewer, person, is_active="on")
        ).status_code
        == 302
    )

    team.name = "Third bench"
    team.save(update_fields=["name"])
    third = deactivation.holdings(person.pk)
    if later_save == "Active box":
        assert (
            reviewer.post(
                change_url(person), account_data(reviewer, person, is_active="")
            ).status_code
            == 302
        )
    else:
        person.is_active = False
        person.save(update_fields=["is_active"])
    latest = Deactivation.objects.exclude(pk=accepted.pk).get(user=person)
    accepted.refresh_from_db()
    assert accepted.held == second and latest.held == third and third != second
    assert latest.reason == ""


@pytest.mark.django_db(transaction=True)
def test_pending_group_grant_removal_invalidates_preview_then_redraws_once(
    owner, person, monkeypatch
):
    reviewer = browser_for(owner)
    editor = browser_for(make_owner("group-editor"))
    role = person.groups.get()
    url = reverse("admin:auth_group_change", args=[role.pk])
    fields = page_data(editor.get(url).context["adminform"].form, permissions=[])
    preview = reviewer.get(page_url(person))
    assert "Through the Supervisor role" in label_payload(preview)
    calls = []
    read = deactivation.holdings

    def observed(pk):
        value = read(pk)
        calls.append(value)
        return value

    monkeypatch.setattr(deactivation, "holdings", observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert editor.post(url, fields).status_code == 302
            result = waiting_post(
                pool, reviewer, page_url(person), {"drawn": token(preview), "reason": REASON}
            )
        redraw = result.result(timeout=10)
    assert redraw.status_code == 200 and "changed after you opened" in redraw.content.decode()
    current = redraw.context["held"]
    assert calls == [current, current]  # Service comparison, then the single redraw.
    assert "Through the Supervisor role" not in label_payload(redraw)
    assert not Deactivation.objects.exists()
    assert token(redraw) == deactivation.fingerprint(current)
    calls.clear()
    assert submit(reviewer, person, redraw, reason=REASON).status_code == 302
    assert calls == [current] and Deactivation.objects.get(user=person).held == current


@pytest.mark.django_db(transaction=True)
def test_two_edits_across_refusal_and_resubmission_each_require_review(owner, person, monkeypatch):
    reviewer = browser_for(owner)
    editor = browser_for(make_owner("account-editor"))
    preview = reviewer.get(page_url(person))
    technical = Group.objects.get_or_create(name="technical")[0]
    url = change_url(person)
    first_edit = account_data(editor, person, username="bench-operator", groups=[technical.pk])
    committed_post(editor, url, first_edit)
    second_edit = account_data(editor, person, username="day-operator", groups=[])
    service = deactivation.deactivate

    def finish_refusal(**kwargs):
        try:
            return service(**kwargs)
        except ValidationError:
            committed_post(editor, url, second_edit)
            raise

    with monkeypatch.context() as scope:
        scope.setattr(deactivation, "deactivate", finish_refusal)
        redraw = submit(reviewer, person, preview, reason=REASON)
    assert redraw.context["original"].username == "day-operator"
    assert redraw.context["held"]["roles"] == [] and token(preview) != token(redraw)
    assert redraw.context["form"]["reason"].value() == REASON
    committed_post(editor, url, account_data(editor, person, groups=[technical.pk]))
    again = submit(reviewer, person, redraw, reason=REASON)
    assert again.status_code == 200 and "Technical staff" in label_payload(again)
    assert token(again) != token(redraw) and not Deactivation.objects.exists()
    assert submit(reviewer, person, again, reason=REASON).status_code == 302
    assert Deactivation.objects.get(user=person).held == again.context["held"]


@pytest.mark.parametrize("actor_kind", ["anonymous", "unsaved", "former superuser"])
def test_service_settles_owner_status_before_validating_a_missing_target(owner, actor_kind):
    if actor_kind == "former superuser":
        actor = User.objects.get(pk=owner.pk)
        owner.is_superuser = False
        owner.save(update_fields=["is_superuser"])
    elif actor_kind == "anonymous":
        actor = AnonymousUser()
    else:
        actor = User(username="unsaved", is_superuser=True, is_staff=True)
    with pytest.raises(PermissionDenied):
        deactivation.deactivate(actor=actor, account_id=owner.pk + 100, drawn="", reason="")
    assert not Deactivation.objects.exists()


@pytest.mark.parametrize("same_address", [True, False], ids=["same", "different"])
def test_address_login_is_kept_as_username_and_mailbox_is_omitted(owner, same_address):
    login = "bench.login@example.invalid"
    mailbox = login if same_address else "rotation-notices@example.invalid"
    target = User.objects.create_user(login, mailbox, PASSWORD)
    reviewer = browser_for(owner)
    preview = reviewer.get(page_url(target))
    assert f"What <strong>{login}</strong> holds now" in preview.content.decode()
    assert submit(reviewer, target, preview, reason=REASON).status_code == 302
    event = account_events(target).get()
    for snapshot in [event.before, event.after]:
        assert snapshot["username"] == login and "email" not in snapshot
        if not same_address:
            assert mailbox not in str(snapshot)
    held = Deactivation.objects.get(user=target).held
    assert held == preview.context["held"] and login not in str(held) and mailbox not in str(held)


@pytest.mark.django_db(transaction=True)
def test_pending_owner_step_down_is_read_again_before_a_valid_save(owner, person):
    reviewer = browser_for(owner)
    editor = browser_for(make_owner("continuing-owner"))
    preview = reviewer.get(page_url(person))
    change = account_data(editor, owner, is_superuser="")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert editor.post(change_url(owner), change).status_code == 302
            future = waiting_post(
                pool, reviewer, page_url(person), {"drawn": token(preview), "reason": REASON}
            )
        result = future.result(timeout=10)
    owner.refresh_from_db()
    person.refresh_from_db()
    assert not owner.is_superuser and result.status_code == 403
    assert person.is_active and not Deactivation.objects.exists()
    assert not account_events(person).exists()


def test_restored_owner_can_save_with_an_earlier_inactive_instance(owner, person):
    other = make_owner("returning-administrator")
    reviewer = browser_for(owner)
    assert submit(reviewer, other, reviewer.get(page_url(other))).status_code == 302
    earlier = User.objects.get(pk=other.pk)
    assert not earlier.is_active
    assert (
        reviewer.post(change_url(other), account_data(reviewer, other, is_active="on")).status_code
        == 302
    )
    preview = reviewer.get(page_url(person))
    saved = deactivation.deactivate(
        actor=earlier, account_id=person.pk, reason=REASON, drawn=token(preview)
    )
    assert saved.deactivated_by_id == other.pk and saved.held == preview.context["held"]
    assert account_events(person).get().actor_id == other.pk


def test_restore_uses_the_current_grants_and_keeps_the_preexisting_session_ended(owner, person):
    from accounts.models import AccountSession
    from accounts.roles import has_capability

    reviewer = browser_for(owner)
    person.groups.clear()
    new_code = "review_audit_records"
    assert not has_capability(person, new_code)
    old_session = browser_for(person).session["_account_session"]
    preview = reviewer.get(page_url(person))
    assert has_capability(person, DIRECT)
    assert submit(reviewer, person, preview).status_code == 302
    old = AccountSession.objects.get(pk=old_session)
    ended = old.ended_at
    assert ended is not None
    # A capability is added while the account is inactive. Its historical list stays fixed.
    grant = capability(new_code)
    person.refresh_from_db()
    person.user_permissions.add(grant)
    assert not has_capability(person, new_code)
    assert (
        reviewer.post(
            change_url(person), account_data(reviewer, person, is_active="on")
        ).status_code
        == 302
    )
    person.refresh_from_db()
    assert has_capability(person, new_code) and has_capability(person, DIRECT)
    old.refresh_from_db()
    assert old.ended_at == ended
    assert Deactivation.objects.get(user=person).held == preview.context["held"]
