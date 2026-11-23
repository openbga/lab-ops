"""Second confirmation of review round 1 (candidate c5af6c0, earlier 9e33573). All records and
files are synthetic. Adopted from the reviewer; findings S1 to S7 were resolved in the next
commit (assignee-group-deadlocks-round-1-confirmation-2-resolution.md in the review history).
Adapted to that resolution: S2 (adding a contact takes the directory lock, so the file and the
contact wait there), S3 (organizations held FOR NO KEY UPDATE), S7 (taking an organization out
of its group waits for an import that changes that organization's own records, as the help now
says). The folded-gate-order case is replaced by the third confirmation's stronger one, which
follows the seed past its own lock to the actor gates."""

import io

import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse

from accounts.models import User
from clients import services as directory
from clients.models import Client, ClientContact, ClientSite
from core.models import AuditEvent
from dataimport import importing

from .test_review_acting_account_until_commit import Gate, gated
from .test_review_assignment_lock_order_verify3 import Held, Session, _blocked_by, _statement

pytestmark = pytest.mark.django_db(transaction=True)


def _observe(worker, holder):
    state = worker.waiting_or_ended(seconds=5)
    if state == "waiting":
        assert _blocked_by(worker.pid, holder.pid), _statement(worker.pid)
        return state, _statement(worker.pid)
    assert state == "ended", (state, worker.error)
    return state, ""


@pytest.mark.parametrize("kind", ["sites", "contacts"])
def test_leaving_a_group_during_an_update_only_file_for_the_same_organization(
    kind, make_user, agent, clinic, clinic_site, clinic_contact
):
    manager = make_user("second_confirmation_manager", "account_manager")
    group = Client.objects.create(name="Regional health group")
    member = directory.update_client(
        actor=manager,
        client_id=clinic.pk,
        expected_revision=clinic.revision,
        parent=group,
        reason="Joined regional group",
    )
    if kind == "sites":
        content = b"account_code,site_code,phone\nEXCLIN,MAIN,555-0196\n"
    else:
        content = b"account_code,email,phone\nEXCLIN,pat@clinic.example.invalid,555-0196\n"
    spec = importing.BY_SLUG[kind]
    args = dict(actor=agent, filename="changes.csv", data=content)
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts["update"] == 1
    imported = Held(
        lambda: importing.commit_file(
            spec, **args, state_token=preview.state_token, reason="Directory phone change"
        )
    )
    detach = Session(
        lambda: directory.update_client(
            actor=manager,
            client_id=member.pk,
            expected_revision=member.revision,
            parent=None,
            reason="Left regional group",
        )
    ).start()
    try:
        state, sql = _observe(detach, imported.session)
    finally:
        assert imported.release() == "done"
        detach.join()
    assert detach.outcome == "done", detach.error
    member.refresh_from_db()
    changed = clinic_site if kind == "sites" else clinic_contact
    changed.refresh_from_db()
    assert member.parent_id is None and changed.phone == "555-0196"
    # Adapted (S7): the upload help now says that other changes to an organization, such as
    # taking it out of its group, wait for an import that changes that organization's own
    # records; it waits at the organization's row, not the directory lock.
    assert state == "waiting" and '"clients_client"' in sql, sql


@pytest.mark.parametrize("existing", [False, True], ids=["create", "update"])
def test_contact_file_rechecks_its_matching_email_after_waiting_for_the_organization(
    existing, agent, make_user, clinic
):
    editor = make_user("second_confirmation_editor", "account_manager")
    email = "coord@example.invalid"
    if existing:
        original = directory.add_contact(
            actor=agent, client_id=clinic.pk, name="File coordinator", email=email, phone="555-0101"
        )
    args = dict(
        actor=agent,
        filename="coordinators.csv",
        data=(
            f"account_code,email,name,phone\nEXCLIN,{email},File coordinator,555-0191\n"
        ).encode(),
    )
    spec = importing.CONTACTS
    preview = importing.preview_file(spec, **args)
    action = "update" if existing else "create"
    assert preview.ok and preview.counts[action] == 1, [r.errors for r in preview.rows]
    first = Held(
        lambda: directory.add_contact(
            actor=editor,
            client_id=clinic.pk,
            name="Office coordinator",
            email=email,
            phone="555-0147",
        )
    )
    confirm = Session(
        lambda: importing.commit_file(
            spec, **args, state_token=preview.state_token, reason="Current coordinator"
        )
    ).start()
    try:
        state, sql = _observe(confirm, first.session)
    finally:
        assert first.release() == "done"
        confirm.join()
    matches = ClientContact.objects.filter(client=clinic, email=email)
    summaries = AuditEvent.objects.filter(action=importing.SUMMARY_ACTION).count()
    if state == "ended":
        # Both overlapping saves may succeed if the import finishes first.
        assert confirm.outcome == "done" and summaries == 1, confirm.error
        return
    # Adapted (S2): adding a contact takes the directory lock, so the import waits for it
    # before reading the file, not at the organization's row.
    assert "pg_advisory_xact_lock" in sql, sql
    fresh = importing.preview_file(spec, **args)
    assert (confirm.outcome, matches.count(), summaries) == ("refused", 1 + int(existing), 0), (
        sql,
        list(matches.values_list("name", "phone")),
        fresh.counts,
        [r.errors for r in fresh.rows],
    )
    if existing:
        original.refresh_from_db()
        assert original.phone == "555-0101"


@pytest.mark.parametrize("action", ["add contact", "edit contact", "add site"])
def test_directory_service_holds_the_actor_before_its_first_record_wait(
    action, agent, clinic, clinic_contact
):
    def hold_record():
        if action == "edit contact":
            ClientContact.objects.select_for_update().get(pk=clinic_contact.pk)
        else:
            Client.objects.select_for_update().get(pk=clinic.pk)

    def save():
        if action == "add site":
            return directory.create_site(actor=agent, client_id=clinic.pk, code="ANN", name="Annex")
        if action == "add contact":
            return directory.add_contact(
                actor=agent, client_id=clinic.pk, name="Sam Example", phone="555-0135"
            )
        return directory.update_contact(
            actor=agent,
            contact_id=clinic_contact.pk,
            expected_revision=clinic_contact.revision,
            reason="Corrected phone",
            phone="555-0135",
        )

    def account_edit():
        person = User.objects.get(pk=agent.pk)
        person.first_name = "Charlie"
        person.save()

    first = Held(hold_record)
    write = Session(save).start()
    edit = None
    try:
        assert _observe(write, first.session)[0] == "waiting"
        edit = Session(account_edit).start()
        state, sql = _observe(edit, write)
    finally:
        assert first.release() == "done"
        write.join()
        if edit is not None:
            edit.join()
    assert write.outcome == edit.outcome == "done", (write.error, edit.error)
    assert User.objects.get(pk=agent.pk).first_name == "Charlie"
    assert state == "waiting" and "pg_advisory_xact_lock(" in sql, sql


@pytest.mark.parametrize("selected", [True, False], ids=["retired-selection", "left-empty"])
def test_edit_form_preserves_the_submitted_choice_when_the_last_site_is_retired(
    selected, client, agent, make_user, clinic, clinic_contact, clinic_site
):
    manager = make_user("confirmation_contact_form_manager", "account_manager")
    client.force_login(agent)
    url = reverse("clients:contact-edit", args=[clinic.pk, clinic_contact.pk])
    form = client.get(url).context["form"]
    assert form["sites"].field.queryset.filter(pk=clinic_site.pk).exists()
    retire = Session(
        lambda: directory.update_site(
            actor=manager,
            site_id=clinic_site.pk,
            expected_revision=clinic_site.revision,
            reason="Office closed",
            is_active=False,
        )
    ).start()
    assert retire.join() == "done", retire.error
    old_name = clinic_contact.name
    data = {
        "name": "Change of contact name",
        "email": clinic_contact.email,
        "reason": "Coordinator change",
        "expected_revision": clinic_contact.revision,
        "is_active": "on",
    }
    if selected:
        data["sites"] = [clinic_site.pk]
    response = client.post(url, data)
    clinic_contact.refresh_from_db()
    events = AuditEvent.objects.filter(action="client_contact.updated", actor=agent).count()
    assert not clinic_contact.sites.exists()
    if selected:
        assert (response.status_code, clinic_contact.name, events) == (200, old_name, 0)
        bound = response.context["form"]
        assert "sites" in bound.errors and bound["reason"].value() == data["reason"]
    else:
        assert (response.status_code, clinic_contact.name, events) == (302, data["name"], 1)


@pytest.mark.parametrize(
    "prior_hold", ["different gate", "same folded gate", "earlier missing account"]
)
def test_actor_hold_reacquires_its_own_row_when_an_earlier_gate_survives_rollback(
    prior_hold, make_user
):
    """A surviving gate may have been taken before the hold whose account row rolled back."""
    from accounts.acting import INT4, hold_actor

    def row_probe():
        def probe():
            with transaction.atomic():
                User.objects.select_for_update(nowait=True).get(pk=person.pk)

        session = Session(probe).start()
        result = session.join()
        assert result in {"55P03", "done"}, session.error
        return result

    if prior_hold == "earlier missing account":
        # A normal new account uses its database-assigned pk. Its creating session has not
        # committed yet, so the first hold sees no row and returns None after taking the gate.
        created = []
        creator = Held(
            lambda: created.append(
                User.objects.create_user(
                    "later_visible", email="later_visible@example.invalid", first_name="Original"
                )
            )
        )
        person = created[0]
        prior = person
    else:
        creator = None
        prior = make_user("gate_neighbor", "client_services")
        extra = {"pk": prior.pk + INT4} if prior_hold == "same folded gate" else {}
        person = make_user(
            "reading_under_review", "client_services", first_name="Original", **extra
        )

    def rename():
        current = User.objects.get(pk=person.pk)
        current.first_name = "Latest"
        current.save(update_fields=["first_name"])

    with transaction.atomic():
        try:
            outer = hold_actor(prior)
            assert (outer is None) == (prior_hold == "earlier missing account")
        finally:
            if creator is not None:
                assert creator.release() == "done", creator.session.error
        with transaction.atomic():
            assert hold_actor(person).first_name == "Original"
            assert row_probe() == "55P03"
            transaction.set_rollback(True)
        assert row_probe() == "done"

        editor = Session(rename).start()
        assert editor.join() == "done", editor.error
        current = hold_actor(person)
        assert (current.first_name, row_probe()) == ("Latest", "55P03"), prior_hold


@pytest.mark.parametrize("other_lock", ["peer shared gate", "own exclusive gate"])
def test_actor_hold_ignores_a_different_backend_or_mode_after_rollback(agent, other_lock):
    """A pg_locks row for this advisory key must also match its backend and shared mode."""
    from accounts.acting import close_access, hold_actor

    def rename():
        changed = User.objects.get(pk=agent.pk)
        changed.first_name = "Current"
        changed.save(update_fields=["first_name"])

    with transaction.atomic():
        if other_lock == "own exclusive gate":
            close_access([agent.pk])
        with transaction.atomic():
            first = hold_actor(agent)
            transaction.set_rollback(True)
        editor = Session(rename).start()
        assert editor.join() == "done", editor.error
        peer = Held(lambda: hold_actor(agent)) if other_lock == "peer shared gate" else None
        try:
            again = hold_actor(agent)
            assert again.first_name != first.first_name
            assert again.first_name == "Current"
        finally:
            if peer is not None:
                assert peer.release() == "done", peer.session.error

        def check_row():
            with transaction.atomic():
                User.objects.select_for_update(nowait=True).get(pk=agent.pk)

        probe = Session(check_row).start()
        assert probe.join() == "55P03", probe.error


def test_two_organization_sites_file_and_form_setup_can_both_commit(
    make_user, agent, clinic, water, category
):
    """Take the file's second normal lock with NOWAIT while a form's FK checks run."""

    from request_forms.models import RequestTemplate
    from request_forms.services import create_template

    admin = make_user("confirm_office_form_admin", "admin")
    codes = {client.pk: client.account_number for client in (clinic, water)}
    offices = [
        ClientSite.objects.create(client=c, code="WEST", name="Office") for c in (clinic, water)
    ]
    saved = Gate()
    file_started = Gate()
    references = []
    unavailable = []

    def make_form():
        with transaction.atomic():
            form = create_template(
                actor=admin,
                category=category,
                key="office-update",
                name="Office update",
                clients=[clinic, water],
            )
            links = RequestTemplate.clients.through.objects.filter(requesttemplate_id=form.pk)
            references.extend(links.order_by("pk").values_list("client_id", flat=True))
            saved.stop()
            return form.pk

    def try_import_lock(execute, sql, params, many, context):
        if sql.endswith("FOR UPDATE") and 'FROM "clients_client"' in sql:
            if not file_started.arrived.is_set():
                result = execute(sql, params, many, context)
                file_started.stop()
                return result
            try:
                return execute(sql + " NOWAIT", params, many, context)
            except DatabaseError as exc:
                unavailable.append(getattr(exc.__cause__, "sqlstate", None))
                raise
        if (
            sql.endswith("FOR UPDATE")
            and 'FROM "clients_clientsite"' in sql
            and not file_started.arrived.is_set()
        ):
            # The previous revision takes no organization lock for these update-only rows.
            file_started.stop()
        return execute(sql, params, many, context)

    def commit_sites(args, token):
        with connection.execute_wrapper(try_import_lock):
            return importing.commit_file(
                importing.SITES, **args, state_token=token, reason="Renamed offices"
            )

    template = Session(make_form).start()
    writer = None
    state, statement = "", ""
    try:
        assert saved.arrived.wait(timeout=5), template.error
        assert set(references) == set(codes)
        # Reverse the actual order in which this form saved its organization references.
        csv = "account_code,site_code,name\n" + "".join(
            f"{codes[pk]},WEST,West reception\n" for pk in reversed(references)
        )
        args = dict(actor=agent, filename="offices.csv", data=csv.encode())
        preview = importing.preview_file(importing.SITES, **args)
        assert preview.ok and preview.counts["update"] == 2
        writer = Session(lambda: commit_sites(args, preview.state_token)).start()
        assert file_started.arrived.wait(timeout=5), writer.error
        saved.go.set()
        state = template.waiting_or_ended(seconds=4)
        if state == "waiting":
            assert _blocked_by(template.pid, writer.pid)
            statement = _statement(template.pid)
    finally:
        saved.go.set()
        file_started.go.set()
        template.join()
        if writer is not None:
            writer.join()
    assert template.outcome == "done", template.error
    assert set(
        RequestTemplate.objects.get(pk=template.result).clients.values_list("pk", flat=True)
    ) == set(codes)
    renamed = ClientSite.objects.filter(
        pk__in=[site.pk for site in offices], name="West reception"
    ).count()
    summaries = AuditEvent.objects.filter(actor=agent, action=importing.SUMMARY_ACTION).count()
    assert (unavailable, writer.outcome, renamed, summaries) == ([], "done", 2, 1), (
        state,
        statement,
        unavailable,
        writer.error,
    )


@pytest.mark.parametrize("kind", ["sites", "contacts"])
def test_file_holds_each_changing_organization_once_in_file_order_before_writing(
    kind, make_user, clinic, water
):
    from dataclasses import replace

    actor = make_user("file_order_manager", "account_manager")
    neutral = Client.objects.create(name="Example unchanged office", account_number="EXUNCH")
    if kind == "sites":
        spec = importing.SITES
        header = "account_code,site_code,name"

        def add_record(client, key):
            ClientSite.objects.create(client=client, code=key, name="Original")
            return key
    else:
        spec = importing.CONTACTS
        header = "account_code,email,name"

        def add_record(client, key):
            email = f"{key.lower()}@example.invalid"
            ClientContact.objects.create(client=client, email=email, name="Original")
            return email

    rows = [
        (neutral, add_record(neutral, "ONE"), "Original"),
        (water, add_record(water, "ONE"), "First change"),
        (clinic, add_record(clinic, "TWO"), "Second change"),
        (water, add_record(water, "THREE"), "Third change"),
    ]
    text = "\n".join([header, *(f"{c.account_number},{key},{label}" for c, key, label in rows)])
    args = dict(actor=actor, filename="order.csv", data=text.encode())
    preview = importing.preview_file(spec, **args)
    assert preview.ok and preview.counts["update"] == 3
    taken = []
    before_writes = []

    def watch_sql(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        # Adapted (S3): the organizations are held FOR NO KEY UPDATE.
        if sql.endswith("FOR NO KEY UPDATE") and 'FROM "clients_client"' in sql:
            taken.append(params[0])
        return result

    def watch_row(*args, **kwargs):
        before_writes.append(tuple(taken))
        return spec.write(*args, **kwargs)

    def confirm():
        with connection.execute_wrapper(watch_sql):
            return importing.commit_file(
                replace(spec, write=watch_row),
                **args,
                state_token=preview.state_token,
                reason="Renamed records",
            )

    writer = Session(confirm).start()
    assert writer.join() == "done", writer.error
    assert writer.result.counts["update"] == 3
    assert before_writes == [(water.pk, clinic.pk)] * 3


@pytest.fixture
def confirmation_seed_options(settings, monkeypatch):

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
    monkeypatch.setenv("DEMO_PASSWORD", "Tern harbor 4182 lantern")
    monkeypatch.delenv("DEMO_ADMIN_PASSWORD", raising=False)
    return {"stdout": io.StringIO(), "stderr": io.StringIO()}


def test_second_seed_leaves_the_first_seeds_portal_setup_gate_available(confirmation_seed_options):
    """Two real seed commands encounter an existing portal account awaiting its first grant."""

    from accounts.acting import ACCESS_GATE, INT4
    from clients.models import PortalAccess

    class GateUnavailable(Exception):
        pass

    call_command("seed_demo", **confirmation_seed_options)
    former = User.objects.get(username="demo_client_water")
    former.username = "former_water_account"
    former.save(update_fields=["username"])
    portal = User.objects.create_user(
        username="demo_client_water", email="new-water@example.invalid", client_id=former.client_id
    )
    assert not PortalAccess.objects.filter(user=portal).exists()
    before_grant = Gate()
    gate_key = (ACCESS_GATE, portal.pk % INT4)

    def probe(execute, sql, params, many, context):
        if (
            "pg_advisory_xact_lock(" in sql
            and tuple(params or ()) == gate_key
            and not before_grant.arrived.is_set()
        ):
            before_grant.stop()
            result = execute(
                sql.replace("pg_advisory_xact_lock(", "pg_try_advisory_xact_lock("),
                params,
                many,
                context,
            )
            if not context["cursor"].fetchone()[0]:
                raise GateUnavailable
            return result
        return execute(sql, params, many, context)

    def first_seed():
        try:
            with connection.execute_wrapper(probe):
                call_command("seed_demo", **confirmation_seed_options)
        except GateUnavailable:
            return "exclusive portal gate unavailable"
        return "saved"

    first = Session(first_seed).start()
    second = None
    try:
        assert before_grant.arrived.wait(timeout=8), first.error
        second = Session(lambda: call_command("seed_demo", **confirmation_seed_options)).start()
        state = second.waiting_or_ended(seconds=5)
        assert state == "waiting" and _blocked_by(second.pid, first.pid), second.error
        sql = _statement(second.pid)
    finally:
        before_grant.go.set()
        first.join()
        if second is not None:
            second.join()
    assert first.outcome == second.outcome == "done", (first.error, second.error)
    assert PortalAccess.objects.filter(user=portal, client_id=former.client_id).count() == 1
    assert first.result == "saved", (first.result, sql)


def test_seed_waiting_for_a_reset_leaves_the_erase_tables_free(
    confirmation_seed_options, monkeypatch
):
    """Observe relation locks while reset is stopped at its erase boundary; never erase the demo."""

    from core import demo
    from core.management.commands.reset_demo import Command
    from core.models import Lab

    class StopBeforeErase(Exception):
        pass

    call_command("seed_demo", **confirmation_seed_options)
    assert demo.is_sealed(Lab.objects.get(pk=1))
    tables = demo.business_tables()
    boundary = Gate()

    def stop_before_erase(_command):
        boundary.stop()
        raise StopBeforeErase

    def preflight():
        with pytest.raises(StopBeforeErase):
            call_command("reset_demo", interactive=False, **confirmation_seed_options)

    monkeypatch.setenv("ALLOW_DEMO_RESET", "true")
    monkeypatch.setattr(Command, "_erase", stop_before_erase)
    reset = Session(preflight).start()
    seed = None
    occupied = []
    sql = ""
    try:
        assert boundary.arrived.wait(timeout=8), reset.error
        seed = Session(lambda: call_command("seed_demo", **confirmation_seed_options)).start()
        state = seed.waiting_or_ended(seconds=5)
        assert state in {"waiting", "ended"}, seed.error
        if state == "waiting":
            assert _blocked_by(seed.pid, reset.pid)
            sql = _statement(seed.pid)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT c.relname FROM pg_locks l JOIN pg_class c ON c.oid=l.relation "
                    "WHERE l.pid=%s AND l.granted AND c.relname=ANY(%s) ORDER BY 1",
                    [seed.pid, tables],
                )
                occupied = [row[0] for row in cursor.fetchall()]
    finally:
        boundary.go.set()
        reset.join()
        if seed is not None:
            seed.join()
    assert reset.outcome == seed.outcome == "done", (reset.error, seed.error)
    assert not occupied, (sql, occupied)


def test_contact_queues_on_organization_after_the_sites_file_writes_its_first_row(
    make_user, agent, clinic, clinic_site
):
    """The contact starts after the first site is updated and before the new site is inserted."""
    from dataclasses import replace

    actor = make_user("order_contact_manager", "account_manager")
    args = dict(
        actor=actor,
        filename="sites.csv",
        data=(
            b"account_code,site_code,name\nEXCLIN,MAIN,Main reception\nEXCLIN,EAST,East reception\n"
        ),
    )
    preview = importing.preview_file(importing.SITES, **args)
    assert preview.ok and preview.counts["create"] == preview.counts["update"] == 1
    wrote_first = Gate()
    unavailable = []

    def after_row(*args, **kwargs):
        result = importing.SITES.write(*args, **kwargs)
        if not wrote_first.arrived.is_set():
            wrote_first.stop()
        return result

    def probe_create_organization(execute, sql, params, many, context):
        if (
            wrote_first.arrived.is_set()
            and 'FROM "clients_client"' in sql
            and sql.endswith("FOR UPDATE")
        ):
            try:
                return execute(sql + " NOWAIT", params, many, context)
            except DatabaseError as exc:
                unavailable.append(getattr(exc.__cause__, "sqlstate", None))
                raise
        return execute(sql, params, many, context)

    def confirm():
        with connection.execute_wrapper(probe_create_organization):
            return importing.commit_file(
                replace(importing.SITES, write=after_row),
                **args,
                reason="Updated offices",
                state_token=preview.state_token,
            )

    writer = Session(confirm).start()
    contact = None
    try:
        assert wrote_first.arrived.wait(timeout=5), writer.error
        contact = Session(
            lambda: directory.add_contact(
                actor=agent,
                client_id=clinic.pk,
                sites=[clinic_site],
                name="Example coordinator",
                phone="555-0113",
            )
        ).start()
        state = contact.waiting_or_ended(seconds=4)
        statement = _statement(contact.pid)
        blocked = state == "waiting" and _blocked_by(contact.pid, writer.pid)
    finally:
        wrote_first.go.set()
        writer.join()
        if contact is not None:
            contact.join()
    assert contact.outcome == "done", contact.error
    assert contact.result.sites.contains(clinic_site)
    # Adapted (S2): adding a contact takes the directory lock first, which the file holds, so
    # the contact waits there rather than at the organization's row.
    assert (blocked, "pg_advisory_xact_lock" in statement, unavailable, writer.outcome) == (
        True,
        True,
        [],
        "done",
    ), (statement, writer.error)
    clinic_site.refresh_from_db()
    assert clinic_site.name == "Main reception"
    assert ClientSite.objects.filter(client=clinic, code="EAST").exists()


@pytest.mark.parametrize(
    "earlier_created_accounts", [False, True], ids=["populated", "concurrent-first-seed"]
)
def test_seed_holds_the_actors_visible_after_it_waits_for_the_directory(
    confirmation_seed_options, earlier_created_accounts
):
    """The first seed commits all seven actors while a second seed waits for its directory."""

    from accounts.acting import ACCESS_GATE, INT4
    from clients.services import DIRECTORY_LOCK, hold_directory
    from links import seed as link_seed

    def command():
        call_command("seed_demo", **confirmation_seed_options)

    if earlier_created_accounts:
        first = Held(command)
    else:
        command()
        first = Held(hold_directory)

    phase = Gate()
    seed = Session(command)
    captured = []
    with gated(link_seed, "seed_records", {seed.thread: phase}):
        seed.start()
        try:
            assert seed.waiting_or_ended(seconds=6) == "waiting", seed.error
            assert _blocked_by(seed.pid, first.session.pid)
            assert first.release() == "done", first.session.error
            assert phase.arrived.wait(timeout=8), seed.error
            # At this point both revisions have taken the directory, created or read the
            # accounts, and completed their organization phase. Each pg_try runs in its own
            # autocommit transaction, so successful probes cannot interfere with the seed.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s,%s)", DIRECTORY_LOCK)
                assert cursor.fetchone()[0] is False
                accounts = User.objects.filter(username__startswith="demo_").order_by("username")
                for pk, name in accounts.values_list("pk", "username"):
                    cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(%s,%s)", [ACCESS_GATE, pk % INT4]
                    )
                    if cursor.fetchone()[0]:
                        captured.append(name)
        finally:
            phase.go.set()
            first.release()
            seed.join()
    assert seed.outcome == "done", seed.error
    assert not captured, ("Demo account gates still available during the link phase", captured)


def test_site_creation_reports_the_unique_conflict_even_after_the_code_is_released(
    client, make_user, agent, clinic, clinic_site
):
    """The competing site is renamed again after PostgreSQL identifies the exact constraint."""
    creator = make_user("second_confirmation_site_creator", "account_manager")
    client.force_login(creator)
    client.raise_request_exception = False
    form_url = reverse("clients:site-create", args=[clinic.pk])
    collision = Gate()

    def observe_insert(execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except IntegrityError as exc:
            if (
                getattr(exc.__cause__, "sqlstate", None) == "23505"
                and exc.__cause__.diag.constraint_name == "client_site_code"
            ):
                collision.stop()
            raise

    def add_site():
        with connection.execute_wrapper(observe_insert):
            return client.post(
                form_url, {"name": "Example annex", "code": "WEST", "kind": "office"}
            )

    def rename(code, revision):
        directory.update_site(
            actor=agent,
            site_id=clinic_site.pk,
            expected_revision=revision,
            code=code,
            reason="Updated office numbering",
        )

    first = Held(lambda: rename("West", clinic_site.revision))
    adding = Session(add_site).start()
    release_code = None
    try:
        try:
            assert _observe(adding, first.session)[0] == "waiting"
        finally:
            assert first.release() == "done", first.session.error
        assert collision.arrived.wait(timeout=5), adding.error
        release_code = Session(lambda: rename("NORTH", clinic_site.revision + 1)).start()
        assert release_code.waiting_or_ended(seconds=5) == "ended", release_code.error
    finally:
        collision.go.set()
        adding.join()
        if release_code is not None:
            release_code.join()
    assert adding.outcome == release_code.outcome == "done", (adding.error, release_code.error)
    assert ClientSite.objects.get(pk=clinic_site.pk).code == "NORTH"
    assert not ClientSite.objects.filter(client=clinic, code__iexact="west").exists()
    assert not AuditEvent.objects.filter(actor=creator, action="site.created").exists()
    assert adding.result.status_code == 200, adding.result.exc_info
    form = adding.result.context["form"]
    assert form["code"].value() == "WEST"
    assert "Another site of this organization already uses this code" in str(form.errors)
