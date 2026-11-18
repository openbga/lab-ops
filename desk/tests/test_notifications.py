"""Notifications (N1): the allow-list (sentinel test over every template and event), who is told,
sending after commit, failures logged, staff opt-out, and the dummy backend. Synthetic data."""

import logging

import pytest
from django.core import mail
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from clients.models import ClientContact, ClientSite, PortalAccess
from core.models import Lab, UserPreference
from desk import notifications
from desk.models import EntryKind, ServiceRequest, Status
from desk.services import add_entry, create_request
from taxonomy.models import RequestCategory
from teams.models import Team

pytestmark = pytest.mark.django_db

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def S(field):
    """A sentinel string for one field; none may appear in a notice."""
    return f"SENTINEL{field.upper().replace('_', '')}X"


@pytest.fixture(autouse=True)
def lab(db):
    return Lab.objects.update_or_create(pk=1, defaults={"name": "Example Reference Laboratory"})[0]


@pytest.fixture(autouse=True)
def locmem(settings):
    settings.EMAIL_BACKEND = LOCMEM
    settings.PUBLIC_URL = "https://lab-ops.example.invalid"


_capture = {}


@pytest.fixture(autouse=True)
def on_commit(django_capture_on_commit_callbacks):
    _capture["fn"] = django_capture_on_commit_callbacks


def run(fn):
    """Run `fn`, then its on-commit callbacks, as a committed request would."""
    with _capture["fn"](execute=True):
        with transaction.atomic():
            result = fn()
    return result


@pytest.fixture
def portal_account(make_user, clinic):
    user = make_user("clinic_raiser", client=clinic)
    PortalAccess.objects.create(user=user, client_id=user.client_id, whole_organization=True)
    return user


def staff_request(agent, category, clinic, **extra):
    return create_request(
        actor=agent,
        category=category,
        subject="Logged",
        first_entry_kind=EntryKind.NOTE,
        first_entry_body="Internal start.",
        channel="phone",
        client=clinic,
        **extra,
    )


def reply(actor, request, body="Answer", **extra):
    request.refresh_from_db()
    return add_entry(
        actor=actor,
        request=request,
        kind=extra.pop("kind", EntryKind.REPLY),
        body=body,
        expected_revision=request.revision,
        **extra,
    )


# Allow-list ---------------------------------------------------------------------------------


@pytest.fixture
def sentinel_request(make_user, join, clinic, group):
    """A request whose every related text field holds a sentinel."""
    clinic.name, clinic.account_number, clinic.email = S("org"), S("code"), "o@s.example.invalid"
    clinic.notes = S("notes")
    clinic.save()
    site = ClientSite.objects.create(
        client=clinic, code=S("sitecode"), name=S("site"), instructions=S("instructions")
    )
    contact = ClientContact.objects.create(
        client=clinic, name=S("contact"), role=S("role"), email="c@s.example.invalid"
    )
    group.name = S("group")
    group.save()
    category = RequestCategory.objects.create(
        key="sentinel",
        name=S("category"),
        client_label=S("label"),
        guidance=S("guidance"),
        parent=group,
        client_may_raise=True,
    )
    team = Team.objects.create(slug="sentinel", name=S("team"), description=S("teamdesc"))
    agent = make_user(
        "sentinel_agent", "client_services", first_name=S("first"), last_name=S("last")
    )
    join(agent, team)
    raiser = make_user("sentinel_raiser", client=clinic, first_name=S("rfirst"))
    PortalAccess.objects.create(
        user=raiser, client_id=raiser.client_id, whole_organization=True, contact=contact
    )
    request = create_request(
        actor=agent,
        category=category,
        subject=S("subject"),
        first_entry_kind=EntryKind.REPLY,
        first_entry_body=S("body"),
        channel="phone",
        client=clinic,
        site=site,
        contact=contact,
        reported_by=S("reporter"),
        team=team,
        assignee=agent,
        client_reference=S("reference"),
    )
    return request, agent, raiser


def test_every_notice_renders_only_the_allow_list(sentinel_request):
    request, agent, raiser = sentinel_request
    sentinels = [
        value
        for value in (
            S("org"),
            S("code"),
            S("notes"),
            S("sitecode"),
            S("site"),
            S("instructions"),
            S("contact"),
            S("role"),
            S("group"),
            S("category"),
            S("label"),
            S("guidance"),
            S("team"),
            S("teamdesc"),
            S("first"),
            S("last"),
            S("rfirst"),
            S("subject"),
            S("body"),
            S("reporter"),
            S("reference"),
        )
    ]
    messages = []
    for event in notifications.PHRASES:
        for portal in (True, False):
            context = notifications.notice_context(
                number=request.number,
                event=event,
                url=notifications.sign_in_url(
                    notifications.request_path(request.number, request.pk, portal=portal)
                ),
                laboratory=notifications.lab_name(),
            )
            assert set(context) == {"number", "event_phrase", "lab_name", "sign_in_url"}
            subject, body = notifications.render_notice(context)
            messages.append(subject + "\n" + body)
            assert request.number in subject and request.number in body
            assert "Example Reference Laboratory" in body
    # Real sends through the hooks, headers included.
    mail.outbox.clear()
    run(lambda: reply(agent, request, body=S("body")))
    run(lambda: reply(raiser, request, body=S("body")))
    assert len(mail.outbox) == 2
    messages += [m.message().as_string() for m in mail.outbox]
    for text in messages:
        for sentinel in sentinels:
            assert sentinel not in text, sentinel


def test_sign_in_link_carries_no_token(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    mail.outbox.clear()
    run(lambda: reply(agent, request))
    body = mail.outbox[0].body
    assert (
        f"https://lab-ops.example.invalid/accounts/login/?next=%2Fportal%2Frequests%2F{request.number}%2F"
        in body
    )
    assert "token" not in body.lower() and "key=" not in body


# Recipients ---------------------------------------------------------------------------------


def test_staff_reply_tells_raiser_and_contact_account_in_scope(
    make_user, agent, category, clinic, clinic_contact, clinic_site
):
    raiser = make_user("raiser", client=clinic)
    PortalAccess.objects.create(user=raiser, client_id=raiser.client_id, whole_organization=True)
    linked = make_user("linked", client=clinic)
    PortalAccess.objects.create(
        user=linked, client_id=linked.client_id, whole_organization=True, contact=clinic_contact
    )
    other_site = ClientSite.objects.create(client=clinic, name="Elsewhere")
    linked_out_of_scope = make_user("linked_scoped", client=clinic)
    narrow = PortalAccess.objects.create(
        user=linked_out_of_scope, client_id=linked_out_of_scope.client_id, contact=clinic_contact
    )
    narrow.sites.add(other_site)
    bystander = make_user("bystander", client=clinic)
    PortalAccess.objects.create(
        user=bystander, client_id=bystander.client_id, whole_organization=True
    )
    request = run(
        lambda: create_request(
            actor=raiser,
            category=category,
            subject="Raised",
            first_entry_kind=EntryKind.REPLY,
            first_entry_body="Help",
            channel="portal",
            site=clinic_site,
        )
    )
    ServiceRequest.objects.filter(pk=request.pk).update(contact=clinic_contact)
    request.refresh_from_db()
    mail.outbox.clear()
    run(lambda: reply(agent, request))
    assert sorted(m.to[0] for m in mail.outbox) == sorted([raiser.email, linked.email])


def test_notes_contacts_and_withdrawn_replies_tell_nobody(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    request.refresh_from_db()
    mail.outbox.clear()
    run(lambda: reply(agent, request, kind=EntryKind.NOTE))
    run(
        lambda: reply(
            agent,
            request,
            kind=EntryKind.CONTACT,
            contact={"direction": "to_client", "channel": "phone", "outcome": "spoke_with"},
        )
    )
    assert mail.outbox == []
    # A withdrawn reply is not client-visible: the hook ignores it.
    entry = reply(agent, request)
    mail.outbox.clear()
    entry.withdrawn_at = timezone.now()
    run(lambda: notifications.entry_added(request, entry))
    assert mail.outbox == []


def test_reply_with_waiting_status_sends_one_notice(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    mail.outbox.clear()
    run(lambda: reply(agent, request, new_status=Status.WAITING_CLIENT))
    assert len(mail.outbox) == 1
    assert notifications.PHRASES["waiting_client"] in mail.outbox[0].body


def test_resolved_status_notice(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    # A "resolved" notice goes out only while the ticket is resolved (checked at sending).
    ServiceRequest.objects.filter(pk=request.pk).update(
        created_by=portal_account,
        channel="portal",
        status=Status.RESOLVED,
        resolved_at=timezone.now(),
    )
    request.refresh_from_db()
    mail.outbox.clear()
    run(
        lambda: notifications.status_changed(
            request, old=Status.OPEN, new=Status.RESOLVED, actor=agent
        )
    )
    assert notifications.PHRASES["resolved"] in mail.outbox[0].body
    mail.outbox.clear()
    run(
        lambda: notifications.status_changed(
            request, old=Status.OPEN, new=Status.WAITING_THIRD_PARTY, actor=agent
        )
    )
    assert mail.outbox == []


def test_assignment_and_client_reply_tell_the_assignee(
    make_user, join, agent, category, clinic, portal_account
):
    colleague = make_user("colleague", "technical")
    team = Team.objects.create(slug="t", name="Team T")
    join(agent, team)
    join(colleague, team)
    mail.outbox.clear()
    request = run(lambda: staff_request(agent, category, clinic, team=team, assignee=colleague))
    assert [m.to for m in mail.outbox] == [[colleague.email]]
    assert notifications.PHRASES["assigned"] in mail.outbox[0].body
    assert "/requests/" in mail.outbox[0].body or "/accounts/login/" in mail.outbox[0].body
    # Assigning oneself tells nobody.
    mail.outbox.clear()
    run(lambda: staff_request(agent, category, clinic, team=team, assignee=agent))
    assert mail.outbox == []
    # A client reply tells the assignee, not the client.
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    mail.outbox.clear()
    run(lambda: reply(portal_account, request, body="Client answer"))
    assert [m.to for m in mail.outbox] == [[colleague.email]]
    assert notifications.PHRASES["client_reply"] in mail.outbox[0].body


def test_assignee_who_cannot_read_a_restricted_request_is_not_told(
    make_user, join, agent, privacy_category, clinic, quality
):
    outsider = make_user("outsider", "technical")
    request = staff_request(agent, privacy_category, clinic)
    mail.outbox.clear()
    run(lambda: notifications.assigned(request, assignee=outsider, actor=agent))
    assert mail.outbox == []


def test_staff_opt_out_is_honoured(make_user, join, agent, category, clinic):
    colleague = make_user("colleague", "technical")
    team = Team.objects.create(slug="t", name="Team T")
    join(colleague, team)
    UserPreference.objects.create(user=colleague, key=notifications.OPT_OUT_KEY, value=False)
    mail.outbox.clear()
    run(lambda: staff_request(agent, category, clinic, team=team, assignee=colleague))
    assert mail.outbox == []


# Sending ------------------------------------------------------------------------------------


def test_nothing_is_sent_when_the_change_rolls_back(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    mail.outbox.clear()

    class Abort(Exception):
        pass

    with _capture["fn"](execute=True), pytest.raises(Abort), transaction.atomic():
        reply(agent, request)
        assert mail.outbox == []  # not before commit
        raise Abort
    assert mail.outbox == []
    assert not request.entries.filter(kind=EntryKind.REPLY, body="Answer").exists()


def test_failed_send_is_logged_and_keeps_the_change(
    portal_account, agent, category, clinic, monkeypatch, caplog
):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")

    def broken(self, *args, **kwargs):
        raise ConnectionRefusedError("mail server down for someone@example.invalid")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
    with caplog.at_level(logging.WARNING, logger="lab_ops.notifications"):
        run(lambda: reply(agent, request))
    assert request.entries.filter(kind=EntryKind.REPLY, body="Answer").exists()
    assert "Notice not sent" in caplog.text and request.number in caplog.text
    assert "example.invalid" not in caplog.text  # no addresses in the log


@override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend")
def test_dummy_backend_sends_nothing(portal_account, agent, category, clinic):
    request = staff_request(agent, category, clinic)
    ServiceRequest.objects.filter(pk=request.pk).update(created_by=portal_account, channel="portal")
    mail.outbox.clear()
    run(lambda: reply(agent, request))
    assert mail.outbox == []
    assert request.entries.filter(kind=EntryKind.REPLY).exists()
