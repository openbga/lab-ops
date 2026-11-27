"""End dates of capabilities given directly (accounts.grant_ends, accounts.models.CapabilityEnd):
every check treats an ended grant as gone, the end goes with its grant, and the account's page in
the admin console sets it with the grant, checked against the page as drawn and audited."""

from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import grant_ends
from accounts.admin import CHANGED_SINCE_DRAWN, NOT_DIRECT, AccountChangeForm, end_field
from accounts.models import CapabilityEnd, User
from accounts.roles import CAPABILITIES, direct_grants, has_capability, roles_and_capabilities
from accounts.tests.test_confirm_identity import page_data
from core.models import AuditEvent

pytestmark = pytest.mark.django_db
PATIENT = "view_patient_information"
REVIEW = "review_audit_records"


def capability(codename):
    return Permission.objects.get(content_type__app_label="accounts", codename=codename)


def give(user, codename, ends_at=None):
    user.user_permissions.add(capability(codename))
    if ends_at is not None:
        CapabilityEnd.objects.create(user=user, permission=capability(codename), ends_at=ends_at)


def past():
    return timezone.now() - timedelta(hours=1)


def future():
    return timezone.now() + timedelta(days=3)


@pytest.fixture
def owner(make_user):
    return make_user("owner", "admin", is_staff=True, is_superuser=True)


# --- every check -------------------------------------------------------------------------------


def test_a_grant_with_an_end_to_come_holds_and_an_ended_one_gives_nothing(make_user):
    holder = make_user("holder", "technical")
    give(holder, PATIENT, future())
    assert has_capability(holder, PATIENT)
    assert roles_and_capabilities(holder)[1] == {PATIENT}
    CapabilityEnd.objects.filter(user=holder).update(ends_at=past())
    assert not has_capability(holder, PATIENT)
    assert roles_and_capabilities(holder) == ({"technical"}, set())
    # Still stored on the account, giving nothing, until removed.
    assert holder.user_permissions.filter(codename=PATIENT).exists()
    assert not direct_grants(user_id=holder.pk).exists()


def test_an_end_applies_to_the_direct_grant_only_not_a_roles(make_user):
    holder = make_user("member", "supervisor")
    Group.objects.get(name="supervisor").permissions.add(capability(PATIENT))
    give(holder, PATIENT, past())
    assert has_capability(holder, PATIENT)  # through the role
    assert roles_and_capabilities(holder)[1] == {PATIENT}


def test_ending_one_capability_leaves_the_other(make_user):
    holder = make_user("both", "supervisor")
    give(holder, PATIENT, past())
    give(holder, REVIEW)
    assert roles_and_capabilities(holder)[1] == {REVIEW}
    assert has_capability(holder, REVIEW) and not has_capability(holder, PATIENT)


def test_the_second_factor_rule_and_security_settings_count_only_grants_in_force(make_user):
    from core import second_factor
    from core.models import SecondFactorRule
    from core.security_settings import patient_capability_holders

    ended = make_user("ended", "technical")
    give(ended, PATIENT, past())
    ending = make_user("ending", "technical")
    give(ending, PATIENT, future())
    SecondFactorRule.objects.update_or_create(pk=1, defaults={"patient_information": True})
    assert set(second_factor.covered_among([ended.pk, ending.pk])) == {ending.pk}
    assert patient_capability_holders() == 1


def test_the_audit_review_workspace_follows_the_end(make_user, client):
    reviewer = make_user("reviewer", "supervisor")
    give(reviewer, REVIEW, future())
    client.force_login(reviewer)
    assert client.get(reverse("audit_review:changes")).status_code == 200
    CapabilityEnd.objects.filter(user=reviewer).update(ends_at=past())
    assert client.get(reverse("audit_review:changes")).status_code == 403


def test_checks_in_one_transaction_agree_on_the_moment(make_user):
    """transaction_timestamp(): the clock at the transaction's start, so a grant read twice in one
    change or page reading is read the same way both times."""
    from django.db import connection

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT transaction_timestamp(), clock_timestamp()")
            start, _clock = cursor.fetchone()
        holder = make_user("moment", "technical")
        give(holder, PATIENT, start + timedelta(microseconds=1))
        assert has_capability(holder, PATIENT)
        assert has_capability(holder, PATIENT)  # still: the moment is the transaction's start


# --- the end goes with its grant ---------------------------------------------------------------


def test_removing_the_grant_removes_its_end_and_a_grant_given_anew_has_none(make_user):
    holder = make_user("again", "technical")
    give(holder, PATIENT, past())
    holder.user_permissions.remove(capability(PATIENT))
    assert not CapabilityEnd.objects.filter(user=holder).exists()
    give(holder, PATIENT, past())
    holder.user_permissions.clear()
    assert not CapabilityEnd.objects.exists()
    give(holder, PATIENT, past())
    capability(PATIENT).user_set.remove(holder)  # from the permission's side
    assert not CapabilityEnd.objects.exists()
    # An end left behind by a path that bypasses the signals does not end a grant given anew.
    CapabilityEnd.objects.create(user=holder, permission=capability(PATIENT), ends_at=past())
    holder.user_permissions.add(capability(PATIENT))
    assert not CapabilityEnd.objects.exists()
    assert has_capability(holder, PATIENT)


def test_setting_an_end_takes_the_accounts_access_gate(make_user, monkeypatch):
    holder = make_user("gated", "technical")
    give(holder, PATIENT)
    closed = []
    monkeypatch.setattr("accounts.acting.close_access", lambda ids: closed.append(list(ids)))
    end = CapabilityEnd.objects.create(
        user=holder, permission=capability(PATIENT), ends_at=future()
    )
    end.ends_at = past()
    end.save()
    assert closed == [[holder.pk], [holder.pk]]


# --- the day and the moment --------------------------------------------------------------------


@override_settings(TIME_ZONE="America/New_York")
def test_a_last_day_ends_at_the_start_of_the_next_day_in_the_laboratorys_zone():
    zone = ZoneInfo("America/New_York")
    for day in (date(2026, 12, 31), date(2027, 3, 13), date(2027, 3, 14), date(2026, 11, 1)):
        moment = grant_ends.end_of(day)
        assert moment.astimezone(zone).date() == day + timedelta(days=1)
        assert moment.astimezone(zone).hour == 0
        assert grant_ends.last_day_of(moment) == day
        # The same moment read back from the database (UTC) gives the same day and ISO form.
        assert grant_ends.last_day_of(moment.astimezone(UTC)) == day
        assert grant_ends.iso(moment) == grant_ends.iso(moment.astimezone(UTC))


def test_a_new_day_must_be_today_or_later_but_a_kept_one_is_never_refused():
    today = grant_ends.today()
    yesterday = today - timedelta(days=1)
    assert grant_ends.check_new_day(today, None) is None
    assert grant_ends.check_new_day(yesterday, None) == grant_ends.PAST_DAY
    assert grant_ends.check_new_day(yesterday, yesterday) is None
    assert grant_ends.check_new_day(None, yesterday) is None


# --- the account page --------------------------------------------------------------------------


def test_every_capability_has_a_last_day_field_on_the_account_page():
    fields = AccountChangeForm.base_fields
    assert {end_field(codename) for codename in CAPABILITIES} <= set(fields)


def change(http, account, **changes):
    url = reverse("admin:accounts_user_change", args=[account.pk])
    form = http.get(url).context["adminform"].form
    return form, http.post(url, page_data(form, **changes))


def test_a_superuser_sets_a_last_day_with_the_grant_and_it_is_audited(owner, make_user, client):
    person = make_user("temp", "technical")
    client.force_login(owner)
    day = grant_ends.today() + timedelta(days=10)
    _form, response = change(
        client,
        person,
        user_permissions=[capability(PATIENT).pk],
        **{end_field(PATIENT): day.isoformat()},
    )
    assert response.status_code == 302, response.content[:2000]
    end = CapabilityEnd.objects.get(user=person)
    assert end.ends_at == grant_ends.end_of(day)
    assert has_capability(person, PATIENT)
    event = AuditEvent.objects.filter(action="account.updated", object_id=str(person.pk)).get()
    assert event.before["capability_ends"] == {}
    assert event.after["capability_ends"] == {
        f"accounts.{PATIENT}": grant_ends.iso(grant_ends.end_of(day))
    }
    assert event.after["permissions"] == [f"accounts.{PATIENT}"]
    # The page drawn again shows the day.
    form = (
        client.get(reverse("admin:accounts_user_change", args=[person.pk]))
        .context["adminform"]
        .form
    )
    assert form.initial[end_field(PATIENT)] == day


def test_an_end_only_change_is_saved_and_clearing_it_removes_the_end(owner, make_user, client):
    person = make_user("shorten", "technical")
    give(person, PATIENT, grant_ends.end_of(grant_ends.today() + timedelta(days=30)))
    client.force_login(owner)
    day = grant_ends.today() + timedelta(days=2)
    _form, response = change(client, person, **{end_field(PATIENT): day.isoformat()})
    assert response.status_code == 302
    assert CapabilityEnd.objects.get(user=person).ends_at == grant_ends.end_of(day)
    _form, response = change(client, person, **{end_field(PATIENT): ""})
    assert response.status_code == 302
    assert not CapabilityEnd.objects.filter(user=person).exists()
    events = AuditEvent.objects.filter(action="account.updated", object_id=str(person.pk))
    moment = grant_ends.iso(grant_ends.end_of(day))
    assert sorted(
        (
            list(event.before["capability_ends"].values()),
            list(event.after["capability_ends"].values()),
        )
        for event in events
    ) == sorted(
        [
            (
                [grant_ends.iso(grant_ends.end_of(grant_ends.today() + timedelta(days=30)))],
                [moment],
            ),
            ([moment], []),
        ]
    )


def test_a_day_for_a_capability_not_given_directly_is_refused(owner, make_user, client):
    person = make_user("nogrant", "supervisor")
    Group.objects.get(name="supervisor").permissions.add(capability(PATIENT))  # through the role
    client.force_login(owner)
    day = grant_ends.today() + timedelta(days=5)
    _form, response = change(client, person, **{end_field(PATIENT): day.isoformat()})
    assert response.status_code == 200
    assert NOT_DIRECT in response.context["adminform"].form.errors[end_field(PATIENT)]
    assert not CapabilityEnd.objects.exists()
    assert not AuditEvent.objects.filter(action="account.updated").exists()


def test_a_new_day_in_the_past_is_refused_and_a_kept_past_day_is_not(owner, make_user, client):
    person = make_user("pastday", "technical")
    client.force_login(owner)
    yesterday = grant_ends.today() - timedelta(days=1)
    _form, response = change(
        client,
        person,
        user_permissions=[capability(PATIENT).pk],
        **{end_field(PATIENT): yesterday.isoformat()},
    )
    assert response.status_code == 200
    assert grant_ends.PAST_DAY in response.context["adminform"].form.errors[end_field(PATIENT)]
    assert not person.user_permissions.exists()
    # An end already past, kept as it is while another field changes, is saved as it stands.
    give(person, PATIENT, grant_ends.end_of(yesterday))
    form, response = change(client, person, first_name="Renamed")
    assert "Ended" in form.fields[end_field(PATIENT)].help_text
    assert response.status_code == 302
    assert CapabilityEnd.objects.get(user=person).ends_at == grant_ends.end_of(yesterday)
    assert not has_capability(person, PATIENT)


def test_an_end_changed_after_the_page_was_opened_refuses_the_save(owner, make_user, client):
    person = make_user("stale", "technical")
    give(person, PATIENT, grant_ends.end_of(grant_ends.today() + timedelta(days=30)))
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    form = client.get(url).context["adminform"].form
    with transaction.atomic():
        CapabilityEnd.objects.filter(user=person).update(
            ends_at=grant_ends.end_of(grant_ends.today() + timedelta(days=3))
        )
    response = client.post(url, page_data(form, first_name="Late"))
    assert response.status_code == 200
    assert CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()
    person.refresh_from_db()
    assert person.first_name == ""
    # An end added elsewhere since the page opened refuses it too.
    CapabilityEnd.objects.all().delete()
    form = client.get(url).context["adminform"].form
    CapabilityEnd.objects.create(user=person, permission=capability(PATIENT), ends_at=future())
    response = client.post(url, page_data(form, first_name="Late"))
    assert CHANGED_SINCE_DRAWN in response.context["adminform"].form.non_field_errors()


def test_removing_the_capability_on_the_page_removes_its_end(owner, make_user, client):
    person = make_user("remove", "technical")
    give(person, PATIENT, grant_ends.end_of(grant_ends.today() + timedelta(days=30)))
    client.force_login(owner)
    _form, response = change(client, person, user_permissions=[], **{end_field(PATIENT): ""})
    assert response.status_code == 302
    assert not CapabilityEnd.objects.exists() and not person.user_permissions.exists()
    event = AuditEvent.objects.get(action="account.updated", object_id=str(person.pk))
    assert list(event.before["capability_ends"]) == [f"accounts.{PATIENT}"]
    assert event.after["capability_ends"] == {} and event.after["permissions"] == []


def test_an_account_without_ends_records_no_end_key(owner, make_user, client):
    person = make_user("plain", "technical")
    client.force_login(owner)
    _form, response = change(client, person, first_name="Plain")
    assert response.status_code == 302
    event = AuditEvent.objects.get(action="account.updated", object_id=str(person.pk))
    assert "capability_ends" not in event.before and "capability_ends" not in event.after


def test_the_account_list_leaves_out_an_ended_capability(owner, make_user, client):
    ended = make_user("ended1", "technical")
    give(ended, PATIENT, past())
    ending = make_user("ending1", "technical")
    give(ending, PATIENT, future())
    client.force_login(owner)
    response = client.get(reverse("admin:accounts_user_changelist"))
    rows = {u.username: u for u in response.context["cl"].result_list}
    admin_view = response.context["cl"].model_admin
    assert admin_view.capabilities(rows["ended1"]) == "—"
    assert admin_view.capabilities(rows["ending1"]) == "Can view patient information"
    assert admin_view.capabilities(User.objects.get(pk=ended.pk)) == "—"  # without the prefetch


# --- a day sent back as the page showed it (review round 1) -----------------------------------


def test_a_day_sent_back_as_shown_keeps_the_end_and_a_changed_one_is_new_after_a_zone_change(
    owner, make_user, client
):
    person = make_user("zones", "technical")
    client.force_login(owner)
    day = grant_ends.today() + timedelta(days=40)
    with override_settings(TIME_ZONE="Etc/UTC"):
        give(person, PATIENT, grant_ends.end_of(day))
        stored = CapabilityEnd.objects.get(user=person).ends_at
        url = reverse("admin:accounts_user_change", args=[person.pk])
        form = client.get(url).context["adminform"].form
        kept = page_data(form, first_name="Kept")
        moved = page_data(form, **{end_field(PATIENT): (day + timedelta(days=2)).isoformat()})
    with override_settings(TIME_ZONE="Asia/Tokyo"):
        assert client.post(url, kept).status_code == 302
        assert CapabilityEnd.objects.get(user=person).ends_at == stored
        form = client.get(url).context["adminform"].form  # the page as it now reads
        assert form.initial[end_field(PATIENT)] == day + timedelta(days=1)
        moved["drawn"] = form.initial["drawn"]
        assert client.post(url, moved).status_code == 302
        assert CapabilityEnd.objects.get(user=person).ends_at == grant_ends.end_of(
            day + timedelta(days=2)
        )


def test_a_page_that_sends_no_shown_day_is_read_as_showing_the_stored_day(owner, make_user, client):
    person = make_user("noshown", "technical")
    give(person, PATIENT, grant_ends.end_of(grant_ends.today() - timedelta(days=3)))
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    form = client.get(url).context["adminform"].form
    data = page_data(form, first_name="Legacy")
    data.pop(f"shown_last_day_{PATIENT}")
    assert client.post(url, data).status_code == 302  # the past day as stored: not a new choice
    # A different day, from a page that did not send what it showed, is a new choice.
    data = page_data(client.get(url).context["adminform"].form)
    data.pop(f"shown_last_day_{PATIENT}")
    data[end_field(PATIENT)] = (grant_ends.today() - timedelta(days=1)).isoformat()
    response = client.post(url, data)
    assert grant_ends.PAST_DAY in response.context["adminform"].form.errors[end_field(PATIENT)]


def moved_back_at_the_gate(monkeypatch, rows, ends_at):
    """The first time a save takes the access gate, set `rows`' end to `ends_at` just before it:
    a change that moved the end, moved back by another change before the save holds the
    account."""
    from accounts import acting

    gate = acting.close_access
    done = []

    def moved_back(ids):
        if not done:
            done.append(True)
            rows.update(ends_at=ends_at)
        return gate(ids)

    monkeypatch.setattr(acting, "close_access", moved_back)


@pytest.mark.parametrize("posted", ["moved-day", "drawn-day", "cleared"])
def test_a_page_that_sends_no_shown_day_is_read_against_the_end_the_save_holds(
    owner, make_user, client, monkeypatch, posted
):
    """The day such a page is read as showing is the day of the end the save holds after the
    gate, not of the end read when the form was built: an end moved, then moved back, leaves the
    page's fingerprint as drawn, and the day chosen must still be saved (review confirmation
    round, C01)."""
    person = make_user(f"held-{posted}", "technical")
    drawn_day = grant_ends.today() + timedelta(days=21)
    moved_day = drawn_day + timedelta(days=10)
    give(person, PATIENT, grant_ends.end_of(drawn_day))
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    data = page_data(client.get(url).context["adminform"].form, first_name="Held")
    data.pop(f"shown_last_day_{PATIENT}")
    chosen = {"moved-day": moved_day, "drawn-day": drawn_day, "cleared": None}[posted]
    data[end_field(PATIENT)] = chosen.isoformat() if chosen else ""
    ends = CapabilityEnd.objects.filter(user=person)
    ends.update(ends_at=grant_ends.end_of(moved_day))  # as the post is read
    moved_back_at_the_gate(monkeypatch, ends, grant_ends.end_of(drawn_day))
    assert client.post(url, data).status_code == 302
    person.refresh_from_db()
    assert person.first_name == "Held"
    assert [end.ends_at for end in ends] == ([grant_ends.end_of(chosen)] if chosen else [])
    event = AuditEvent.objects.get(action="account.updated", object_id=str(person.pk))
    label = f"accounts.{PATIENT}"
    assert event.before["capability_ends"] == {label: grant_ends.iso(grant_ends.end_of(drawn_day))}
    assert event.after.get("capability_ends", {}).get(label) == (
        grant_ends.iso(grant_ends.end_of(chosen)) if chosen else None
    )


@pytest.mark.parametrize("refused_at", ["after-the-gate", "at-the-gate"])
def test_a_page_that_sent_no_shown_day_is_drawn_again_with_only_what_the_save_held(
    owner, make_user, client, monkeypatch, refused_at
):
    """Drawn again after a refusal, such a page carries the day of the end the refused save held
    under the gate (the page as drawn), never the end read before it; refused at the gate (the
    page no longer matched), it says again that it sent none, and a later save reads the end it
    then holds. Either way the day chosen is saved once the page matches (review second
    confirmation round)."""
    from accounts.admin import UNSENT, shown_field

    person = make_user(f"redrawn-{refused_at}", "technical")
    drawn_day = grant_ends.today() + timedelta(days=21)
    moved_day = drawn_day + timedelta(days=10)
    give(person, PATIENT, grant_ends.end_of(drawn_day))
    client.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[person.pk])
    data = page_data(client.get(url).context["adminform"].form, email="not-an-address")
    data.pop(shown_field(PATIENT))
    data[end_field(PATIENT)] = moved_day.isoformat()  # the day chosen
    ends = CapabilityEnd.objects.filter(user=person)
    ends.update(ends_at=grant_ends.end_of(moved_day))  # as the post is read
    if refused_at == "after-the-gate":
        moved_back_at_the_gate(monkeypatch, ends, grant_ends.end_of(drawn_day))
    refused = client.post(url, data)
    assert refused.status_code == 200
    redrawn = refused.context["adminform"].form
    assert "email" in redrawn.errors
    stale = CHANGED_SINCE_DRAWN in redrawn.non_field_errors()
    assert stale == (refused_at == "at-the-gate")
    marker = redrawn[shown_field(PATIENT)].value()
    assert marker == (drawn_day.isoformat() if refused_at == "after-the-gate" else UNSENT)
    assert str(redrawn[end_field(PATIENT)].value()) == moved_day.isoformat()
    if stale:
        ends.update(ends_at=grant_ends.end_of(drawn_day))  # moved back before the next save
    retry = page_data(redrawn, email="redrawn@example.invalid")
    retry[shown_field(PATIENT)] = marker  # as the page drew it
    assert client.post(url, retry).status_code == 302
    assert ends.get().ends_at == grant_ends.end_of(moved_day)


def test_the_seeds_day_is_the_later_of_the_two_clocks_days(monkeypatch):
    """accounts.grant_ends.later_today: a day after it is to come by the application's clock (which
    checks a new day) and by the database's (which checks the end)."""
    database_day = grant_ends.database_today()
    now = timezone.now()
    for shift in (-400, 0, 91):
        monkeypatch.setattr(grant_ends.timezone, "now", lambda shift=shift: now + timedelta(shift))
        assert grant_ends.later_today() == max(grant_ends.today(), database_day)
        later = grant_ends.later_today() + timedelta(days=90)
        assert grant_ends.check_new_day(later, None) is None
        assert later > database_day


@pytest.mark.parametrize("years", [99, 101])
def test_a_new_day_more_than_a_hundred_years_ahead_is_refused(owner, make_user, client, years):
    person = make_user(f"far{years}", "technical")
    client.force_login(owner)
    day = grant_ends.today() + timedelta(days=365 * years)
    _form, response = change(
        client, person, user_permissions=[capability(PATIENT).pk], **{end_field(PATIENT): day}
    )
    if years == 99:
        assert response.status_code == 302
        assert CapabilityEnd.objects.get(user=person).ends_at == grant_ends.end_of(day)
    else:
        assert response.status_code == 200
        errors = response.context["adminform"].form.errors[end_field(PATIENT)]
        assert grant_ends.TOO_LATE in errors
        assert not person.user_permissions.exists()


def test_the_ended_help_is_drawn_again_on_a_refused_page(owner, make_user, client):
    person = make_user("helpagain", "technical")
    give(person, PATIENT, past())
    client.force_login(owner)
    form, response = change(
        client,
        person,
        **{end_field(REVIEW): (grant_ends.today() + timedelta(days=3)).isoformat()},
    )
    assert response.status_code == 200  # the review capability is not given directly
    refused = response.context["adminform"].form
    assert refused.fields[end_field(PATIENT)].help_text.startswith("Ended:")
    assert not refused.fields[end_field(REVIEW)].help_text.startswith("Ended:")
