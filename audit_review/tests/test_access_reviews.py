"""Access reviews: what a review lists for each choice of accounts, signing it with a decision for
each grant and the password entered again, the checksum of what was signed, refusing a signing when
the grants changed after the form was drawn, whether each grant decided for removal is still held,
who may do what, what is recorded, and that the database keeps signed reviews as written. All data
is synthetic."""

import csv
import io
import re
import threading

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    DatabaseError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.urls import reverse

from accounts.models import SignInEvent
from audit_review import access_reviews
from audit_review.models import (
    AccessReview,
    AccessReviewItem,
    AccessScope,
    Decision,
    Source,
    UseKind,
    WorkspaceUse,
)
from audit_review.sources import describe_filters
from clients.models import ClientSite, PortalAccess
from core.models import AuditEvent

from .conftest import PASSWORD, review_permission

FINDINGS = "agent's Client services role is no longer needed: moved to the front desk."


def patient_permission():
    return Permission.objects.get(
        content_type__app_label="accounts", codename="view_patient_information"
    )


def sign(actor, scope=AccessScope.STAFF, organization=None, remove=(), **extra):
    """Sign a review of `scope` as it stands: Remove for the grants whose key is in `remove`, Keep
    for the others."""
    current = access_reviews.reading(scope, organization)
    decisions = {
        grant.key: Decision.REMOVE if grant.key in remove else Decision.KEEP
        for grant in current.grants
    }
    values = {"decisions": decisions, "expected": current.fingerprint}
    values.update(extra)
    with transaction.atomic():
        return access_reviews.sign_access_review(
            actor=actor, scope=scope, organization=organization, **values
        )


def key(account, kind, code):
    return f"{account.pk}:{kind}:{code}"


@pytest.fixture
def team(reviewer, supervisor, agent, make_user):
    """Staff with something of each kind: roles, a direct capability, admin console access, a
    group that is not a role and gives a capability; one account that holds nothing, and one
    deactivated."""
    supervisor.user_permissions.add(patient_permission())
    agent.is_staff = True
    agent.save()
    pharmacy = Group.objects.create(name="Results release")
    pharmacy.permissions.add(patient_permission())
    agent.groups.add(pharmacy)
    make_user("nobody", first_name="Nell", last_name="Nothing")
    make_user("gone", "technical", is_active=False)
    return {"group": pharmacy}


# ------------------------------------------------------------------------------ what is listed


def test_staff_accounts_list_every_grant_in_order_and_leave_out_what_holds_nothing(
    reviewer, supervisor, agent, team
):
    current = access_reviews.reading(AccessScope.STAFF)
    rows = [
        (grant.account_username, grant.kind, grant.label, grant.detail) for grant in current.grants
    ]
    group = team["group"]
    assert rows == [
        ("agent", "console", "Admin console", ""),
        ("agent", "group", "Client services role", ""),
        ("agent", "group", "Group “Results release”", "Gives Can view patient information"),
        ("reviewer", "group", "Supervisor role", ""),
        ("reviewer", "capability", "Can review access and audit records", "Given directly"),
        ("supervisor", "group", "Supervisor role", ""),
        ("supervisor", "capability", "Can view patient information", "Given directly"),
    ]
    assert current.accounts == 3
    assert any(grant.code == str(group.pk) for grant in current.grants)
    # Nobody holding nothing, and no deactivated account.
    names = {grant.account_username for grant in current.grants}
    assert "nobody" not in names and "gone" not in names


def test_the_scheduled_jobs_account_is_not_reviewed(reviewer, monkeypatch):
    from audit_review import access_report

    monkeypatch.setattr(access_report, "system_account_id", lambda: reviewer.pk)
    assert all(
        grant.account_id != reviewer.pk
        for grant in access_reviews.reading(AccessScope.STAFF).grants
    )


def test_portal_accounts_list_what_each_may_see(reviewer, make_user, clinic, water):
    whole = make_user("whole", client=clinic)
    sited = make_user("sited", client=clinic)
    make_user("raised", client=water)
    make_user("off", client=clinic, is_active=False)
    north = ClientSite.objects.create(client=clinic, name="North draw station", code="N")
    south = ClientSite.objects.create(client=clinic, name="South draw station", code="S")
    PortalAccess.objects.filter(user=whole).update(whole_organization=True)
    access, _ = PortalAccess.objects.get_or_create(user=sited, defaults={"client": clinic})
    access.client = clinic
    access.whole_organization = False
    access.save()
    access.sites.set([south, north])
    portal = access_reviews.reading(AccessScope.PORTAL)
    words = {grant.account_username: (grant.label, grant.detail) for grant in portal.grants}
    assert words["raised"] == ("Portal account", "Only the tickets it raised")
    assert words["sited"] == (
        "Portal account",
        "Tickets of North draw station, South draw station, and those it raised",
    )
    assert "off" not in words
    assert "reviewer" not in words
    one = access_reviews.reading(AccessScope.PORTAL, clinic)
    assert {grant.account_username for grant in one.grants} == {"whole", "sited"}
    assert {grant.organization_name for grant in one.grants} == {"Example Clinic"}


def test_an_organization_is_chosen_only_for_portal_accounts(reviewer, clinic):
    with pytest.raises(ValidationError), transaction.atomic():
        access_reviews.sign_access_review(
            actor=reviewer, scope=AccessScope.STAFF, organization=clinic, decisions={}, expected=""
        )


def test_more_than_the_most_accounts_or_grants_is_refused(reviewer, supervisor, monkeypatch):
    monkeypatch.setattr(access_reviews, "MOST_ACCOUNTS", 1)
    current = access_reviews.reading(AccessScope.STAFF)
    assert current.too_many and not current.grants
    with pytest.raises(ValidationError) as refused:
        sign(reviewer)
    assert access_reviews.TOO_MANY in refused.value.messages
    monkeypatch.setattr(access_reviews, "MOST_ACCOUNTS", 500)
    monkeypatch.setattr(access_reviews, "MOST_GRANTS", 2)
    assert access_reviews.reading(AccessScope.STAFF).too_many


# ------------------------------------------------------------------------------ signing


def test_a_signed_review_keeps_every_decision_the_signer_and_a_checksum(
    reviewer, supervisor, agent, team
):
    remove = {key(agent, "group", Group.objects.get(name="client_services").pk)}
    review = sign(reviewer, remove=remove, findings=FINDINGS, follow_up="Superuser this week.")
    assert review.number == f"AC-{review.pk:06d}"
    assert (review.accounts, review.grants, review.removals) == (3, 7, 1)
    assert review.signer_name == "Rae Viewer"
    assert review.signer_username == "reviewer"
    assert review.signer_roles == "Supervisor"
    assert review.meaning == "reviewed"
    items = list(review.items.order_by("position"))
    assert [item.position for item in items] == list(range(1, 8))
    assert [item.decision for item in items].count("remove") == 1
    removed = next(item for item in items if item.decision == "remove")
    assert (removed.account_username, removed.label) == ("agent", "Client services role")
    assert access_reviews.intact(AccessReview.objects.get(pk=review.pk))
    event = AuditEvent.objects.get(action=access_reviews.SIGNED)
    assert event.after["checksum"] == review.checksum
    assert event.after["removed"] == [
        {"account": "agent", "grant": "Client services role", "detail": ""}
    ]
    assert "@" not in str(event.after)
    # A later change of a name or a grant does not change what was signed.
    agent.first_name = "Renamed"
    agent.save()
    agent.groups.clear()
    assert access_reviews.intact(AccessReview.objects.get(pk=review.pk))


def test_the_checksum_covers_each_decision_and_the_sign_off(reviewer, supervisor):
    review = sign(reviewer)
    items = list(review.items.order_by("position"))
    assert access_reviews.intact(review, items)
    items[0].decision = "remove"
    assert not access_reviews.intact(review, items)
    items[0].decision = "keep"
    review.signer_roles = "Administrator"
    assert not access_reviews.intact(review, items)


def test_the_database_refuses_to_change_or_delete_a_review_or_a_decision(reviewer):
    review = sign(reviewer)
    item = review.items.first()
    for table, pk in (
        ("audit_review_accessreview", review.pk),
        ("audit_review_accessreviewitem", item.pk),
    ):
        column = "findings" if table.endswith("accessreview") else "decision"
        for statement in (
            f"UPDATE {table} SET {column} = 'remove' WHERE id = %s",
            f"DELETE FROM {table} WHERE id = %s",
        ):
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(statement, [pk])


def test_only_a_reviewer_signs(supervisor, agent, make_user, clinic):
    portal = make_user("portal", client=clinic)
    for account in (supervisor, agent, portal):
        with pytest.raises(PermissionDenied):
            sign(account)
    lone = make_user("lone")
    lone.user_permissions.add(review_permission())
    with pytest.raises(PermissionDenied):
        sign(lone)
    assert not AccessReview.objects.exists()


def test_every_grant_needs_a_decision_and_only_the_grants_listed(reviewer, supervisor):
    current = access_reviews.reading(AccessScope.STAFF)
    decisions = {grant.key: Decision.KEEP for grant in current.grants}
    missing = dict(list(decisions.items())[1:])
    for chosen in (missing, {**decisions, current.grants[0].key: "maybe"}):
        with pytest.raises(ValidationError), transaction.atomic():
            access_reviews.sign_access_review(
                actor=reviewer,
                scope=AccessScope.STAFF,
                decisions=chosen,
                expected=current.fingerprint,
            )
    with pytest.raises(ValidationError) as refused, transaction.atomic():
        access_reviews.sign_access_review(
            actor=reviewer,
            scope=AccessScope.STAFF,
            decisions={**decisions, "999999:group:1": Decision.KEEP},
            expected=current.fingerprint,
        )
    assert access_reviews.STALE in refused.value.messages
    assert not AccessReview.objects.exists()


def _add_role(account, role="technical"):
    account.groups.add(Group.objects.get_or_create(name=role)[0])


CHANGES = {
    "a role added": lambda accounts: _add_role(accounts["supervisor"]),
    "a role removed": lambda accounts: accounts["supervisor"].groups.clear(),
    "a capability given": lambda accounts: accounts["agent"].user_permissions.add(
        patient_permission()
    ),
    "a capability given to a role": lambda accounts: Group.objects.get(
        name="client_services"
    ).permissions.add(patient_permission()),
    "a role renamed": lambda accounts: Group.objects.filter(name="client_services").update(
        name="Front desk"
    ),
    "admin console access": lambda accounts: (
        type(accounts["agent"]).objects.filter(pk=accounts["agent"].pk).update(is_staff=True)
    ),
    "an account deactivated": lambda accounts: (
        type(accounts["agent"]).objects.filter(pk=accounts["agent"].pk).update(is_active=False)
    ),
    "a new account": lambda accounts: accounts["make_user"]("newcomer", "technical"),
    "an account renamed": lambda accounts: (
        type(accounts["agent"]).objects.filter(pk=accounts["agent"].pk).update(first_name="Changed")
    ),
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_grants_changed_after_the_form_was_drawn_refuse_the_signing(
    reviewer, supervisor, agent, make_user, change
):
    current = access_reviews.reading(AccessScope.STAFF)
    CHANGES[change]({"supervisor": supervisor, "agent": agent, "make_user": make_user})
    with pytest.raises(ValidationError) as refused, transaction.atomic():
        access_reviews.sign_access_review(
            actor=reviewer,
            scope=AccessScope.STAFF,
            decisions={grant.key: Decision.KEEP for grant in current.grants},
            expected=current.fingerprint,
        )
    assert access_reviews.STALE in refused.value.messages
    assert not AccessReview.objects.exists()
    assert not AuditEvent.objects.filter(action=access_reviews.SIGNED).exists()


def test_a_portal_account_whose_sites_change_refuses_the_signing(reviewer, make_user, clinic):
    portal = make_user("portal", client=clinic)
    site = ClientSite.objects.create(client=clinic, name="North draw station", code="N")
    current = access_reviews.reading(AccessScope.PORTAL)
    access, _ = PortalAccess.objects.get_or_create(user=portal, defaults={"client": clinic})
    access.client = clinic
    access.save()
    access.sites.add(site)
    with pytest.raises(ValidationError), transaction.atomic():
        access_reviews.sign_access_review(
            actor=reviewer,
            scope=AccessScope.PORTAL,
            decisions={grant.key: Decision.KEEP for grant in current.grants},
            expected=current.fingerprint,
        )


def test_nothing_to_review_signs_nothing(reviewer, clinic):
    with pytest.raises(ValidationError) as refused:
        sign(reviewer, scope=AccessScope.PORTAL)
    assert access_reviews.NOTHING in refused.value.messages


def test_a_password_changed_since_it_was_checked_refuses_the_signing(reviewer):
    stale_hash = reviewer.password
    reviewer.set_password("another-synthetic-passphrase-93")
    reviewer.save()
    with pytest.raises(ValidationError):
        sign(reviewer, password_hash=stale_hash)
    review = sign(reviewer, password_hash=type(reviewer).objects.get(pk=reviewer.pk).password)
    assert review.pk


# ------------------------------------------------------------------------------ after signing


def test_still_held_follows_the_accounts_as_they_stand(reviewer, supervisor, agent, team):
    client_services = Group.objects.get(name="client_services")
    remove = {
        key(agent, "group", client_services.pk),
        key(agent, "console", "is_staff"),
        key(supervisor, "capability", "view_patient_information"),
        key(supervisor, "group", Group.objects.get(name="supervisor").pk),
    }
    review = sign(reviewer, remove=remove)
    removals = list(review.items.filter(decision="remove"))
    assert len(removals) == 4
    assert all(access_reviews.still_held(removals).values())
    agent.groups.remove(client_services)
    supervisor.user_permissions.clear()
    held = access_reviews.still_held(removals)
    by_label = {(item.account_username, item.label): held[item.pk] for item in removals}
    assert by_label == {
        ("agent", "Client services role"): False,
        ("agent", "Admin console"): True,
        ("supervisor", "Can view patient information"): False,
        ("supervisor", "Supervisor role"): True,
    }
    type(agent).objects.filter(pk__in=[agent.pk, supervisor.pk]).update(is_active=False)
    assert not any(access_reviews.still_held(removals).values())


def test_a_portal_account_is_held_while_active_in_its_organization(
    reviewer, make_user, clinic, water
):
    portal = make_user("portal", client=clinic)
    review = sign(
        reviewer, scope=AccessScope.PORTAL, remove={key(portal, "portal", f"{clinic.pk}:none")}
    )
    removals = list(review.items.filter(decision="remove"))
    assert access_reviews.still_held(removals) == {removals[0].pk: True}
    type(portal).objects.filter(pk=portal.pk).update(client=water)
    assert access_reviews.still_held(removals) == {removals[0].pk: False}


def test_reversing_the_migration_is_refused_while_an_access_review_exists(reviewer, supervisor):
    from django.db.migrations.executor import MigrationExecutor

    sign(reviewer)
    executor = MigrationExecutor(connection)
    with pytest.raises(DatabaseError), transaction.atomic():
        executor.migrate([("audit_review", "0004_access_report")])


# ------------------------------------------------------------------------------ pages


def form_page(client, query=""):
    response = client.get(reverse("access_review:review-create") + query)
    assert response.status_code == 200
    return response


def posted(response, decide=Decision.KEEP, **extra):
    """The form's hidden fields from `response` and `decide` for every grant shown."""
    body = response.content.decode()
    data = {
        name: re.search(rf'name="{name}" value="([^"]*)"', body).group(1)
        for name in ("scope", "expected", "shown")
    }
    found = re.search(r'<input type="hidden" name="organization" value="([^"]*)"', body)
    data["organization"] = found.group(1) if found else ""
    for name in set(re.findall(r'name="(d-[0-9a-f]+)"', body)):
        data[name] = decide
    data.update({"findings": "", "follow_up": "", "confirm_password": PASSWORD})
    data.update(extra)
    return data


@pytest.mark.parametrize(
    "name, args",
    [
        ("reviews", []),
        ("reviews-export", []),
        ("review-create", []),
    ],
)
def test_pages_refuse_accounts_without_the_capability(client, supervisor, name, args):
    client.force_login(supervisor)
    assert client.get(reverse(f"access_review:{name}", args=args)).status_code == 403
    assert client.post(reverse("access_review:review-create"), {}).status_code == 403


def test_a_review_and_its_export_refuse_accounts_without_the_capability(
    client, reviewer, supervisor, make_user, clinic
):
    review = sign(reviewer)
    for account in (supervisor, make_user("portal", client=clinic)):
        client.force_login(account)
        for name in ("review", "review-export"):
            assert client.get(reverse(f"access_review:{name}", args=[review.pk])).status_code == 403
    client.force_login(reviewer)
    assert client.get(reverse("access_review:review", args=[review.pk + 1])).status_code == 404


def test_the_form_lists_the_grants_and_its_drawing_is_recorded(client, reviewer, supervisor, agent):
    client.force_login(reviewer)
    response = form_page(client)
    assert b"Supervisor role" in response.content
    assert b"Client services role" in response.content
    use = WorkspaceUse.objects.get(source=Source.ACCESS_REVIEWS)
    assert use.kind == UseKind.VIEWED
    assert use.filters == {"form": "new", "scope": "staff"}
    assert use.rows == 4  # the grants listed: two of the reviewer's, one each of the others
    assert describe_filters(use, {}, {}, {}) == "Accounts: Staff accounts; Record an access review"


def test_signing_on_the_form_with_the_password(client, reviewer, supervisor):
    client.force_login(reviewer)
    data = posted(form_page(client), findings=FINDINGS)
    response = client.post(reverse("access_review:review-create"), data)
    review = AccessReview.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("access_review:review", args=[review.pk])
    assert review.findings == FINDINGS
    assert review.grants == 3 and review.removals == 0
    assert PASSWORD not in str(AuditEvent.objects.get(action=access_reviews.SIGNED).after)
    assert PASSWORD not in str(AccessReview.objects.values().get())


def test_missing_decisions_refuse_without_checking_the_password(client, reviewer, supervisor):
    client.force_login(reviewer)
    data = posted(form_page(client))
    first = next(name for name in data if name.startswith("d-"))
    del data[first]
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    assert b"Choose Keep or Remove." in response.content
    assert b"1 grant still needs a decision." in response.content
    assert not AccessReview.objects.exists()
    assert not SignInEvent.objects.filter(
        user_id=reviewer.pk, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()


def test_a_wrong_password_signs_nothing_and_keeps_the_decisions(client, reviewer, supervisor):
    client.force_login(reviewer)
    data = posted(form_page(client), decide=Decision.REMOVE, confirm_password="wrong-password")
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    assert b"Incorrect password." in response.content
    assert b"wrong-password" not in response.content
    body = response.content.decode()
    assert len(re.findall(r'value="remove"[^>]*checked', body)) == 3
    assert not AccessReview.objects.exists()
    assert SignInEvent.objects.filter(
        user_id=reviewer.pk, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()


def test_a_change_after_the_form_was_drawn_draws_it_again_keeping_unchanged_decisions(
    client, reviewer, supervisor
):
    client.force_login(reviewer)
    data = posted(form_page(client), decide=Decision.REMOVE)
    _add_role(supervisor)
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    body = response.content.decode()
    assert "changed after you opened this review" in body
    badge = r'<span class="vr-status[^"]*"[^>]*>(?:(?!</span>).)*New or changed</span>'
    assert len(re.findall(badge, body, re.S)) == 1
    assert len(re.findall(r'value="remove"[^>]*checked', body)) == 3
    assert not AccessReview.objects.exists()
    # Refused before the password is checked: no attempt is counted.
    assert not SignInEvent.objects.filter(
        user_id=reviewer.pk, kind=SignInEvent.Kind.REAUTHENTICATION_FAILED
    ).exists()
    # Sent again as drawn, with the new grant decided, it signs.
    response = client.post(
        reverse("access_review:review-create"), posted(response, decide=Decision.KEEP)
    )
    assert response.status_code == 302
    assert AccessReview.objects.get().grants == 4


def test_a_change_after_the_password_was_checked_refuses_the_signing(
    client, reviewer, supervisor, monkeypatch
):
    from audit_review import access_review_forms

    real = access_review_forms.signing_refusal

    def then_change(request, password):
        refusal = real(request, password)
        _add_role(supervisor)  # saved once the form's own reading has passed
        return refusal

    monkeypatch.setattr(access_review_forms, "signing_refusal", then_change)
    client.force_login(reviewer)
    response = client.post(reverse("access_review:review-create"), posted(form_page(client)))
    assert response.status_code == 200
    assert b"changed after you opened this review" in response.content
    assert b"New or changed" in response.content
    assert not AccessReview.objects.exists()


def test_the_list_the_page_and_the_exports_show_the_review_and_are_recorded(
    client, reviewer, supervisor, agent
):
    remove = {key(agent, "group", Group.objects.get(name="client_services").pk)}
    review = sign(reviewer, remove=remove, findings=FINDINGS)
    client.force_login(reviewer)
    listing = client.get(reverse("access_review:reviews"))
    assert listing.status_code == 200
    assert review.number.encode() in listing.content
    page = client.get(reverse("access_review:review", args=[review.pk]))
    assert page.status_code == 200
    assert b"Still held" in page.content
    assert b"Matches the access review as stored." in page.content
    exported = client.get(reverse("access_review:review-export", args=[review.pk]))
    text = b"".join(exported.streaming_content if exported.streaming else [exported.content])
    header, *lines = list(csv.reader(io.StringIO(text.decode("utf-8-sig"))))
    assert header[:7] == [
        "Access review",
        "Person",
        "Username",
        "Client organization",
        "Grant",
        "Detail",
        "Decision",
    ]
    removed = [line for line in lines if line[6] == "Remove"]
    assert removed == [
        [
            review.number,
            "Ada Agent",
            "agent",
            "",
            "Client services role",
            "",
            "Remove",
            "Yes",
            removed[0][8],
        ]
    ]
    listed = client.get(reverse("access_review:reviews-export"))
    assert review.number in listed.content.decode("utf-8-sig")
    uses = list(WorkspaceUse.objects.filter(source=Source.ACCESS_REVIEWS).order_by("pk"))
    assert [(use.kind, use.filters) for use in uses] == [
        (UseKind.VIEWED, {"page": "1"}),
        (UseKind.VIEWED, {"access_review": str(review.pk)}),
        (UseKind.EXPORTED, {"access_review": str(review.pk)}),
        (UseKind.EXPORTED, {}),
    ]
    assert describe_filters(uses[1], {}, {}, {}) == f"Access review {review.number}"


def test_names_are_escaped_on_every_page(client, reviewer, make_user):
    odd = make_user("odd", first_name="<b>Odd</b>", last_name="Name")
    odd.groups.add(Group.objects.create(name="<script>x</script>"))
    client.force_login(reviewer)
    response = form_page(client)
    assert b"<script>x</script>" not in response.content
    assert b"<b>Odd</b>" not in response.content
    review = sign(reviewer)
    page = client.get(reverse("access_review:review", args=[review.pk]))
    assert b"<script>x</script>" not in page.content


def test_the_signed_review_is_named_by_its_number_on_the_changes_list(client, reviewer):
    review = sign(reviewer)
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:changes"))
    assert response.status_code == 200
    assert AuditEvent.objects.filter(
        action=access_reviews.SIGNED, object_id=str(review.pk)
    ).exists()


# ------------------------------------------------------------------------------ holding the grants


@pytest.mark.django_db(transaction=True)
def test_a_removal_waits_for_a_signing_under_way(reviewer, supervisor):
    """While a signing has read the grants and not yet committed, a change that takes a reviewed
    account's access away (close_access) waits for it."""
    from accounts.acting import close_access
    from audit_review import access_reviews as module

    read = threading.Event()
    release = threading.Event()
    errors = []
    real = module.record_event

    def pause(*args, **kwargs):
        read.set()
        release.wait(10)
        return real(*args, **kwargs)

    def signing():
        try:
            close_old_connections()
            sign(reviewer)
        except Exception as exc:  # reported below
            errors.append(exc)
        finally:
            connection.close()

    module.record_event = pause
    try:
        worker = threading.Thread(target=signing)
        worker.start()
        assert read.wait(10)
        with pytest.raises(OperationalError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '300ms'")
            close_access([supervisor.pk])
        release.set()
        worker.join(10)
    finally:
        module.record_event = real
        release.set()
    assert not errors
    assert AccessReviewItem.objects.filter(account_id=supervisor.pk).exists()


def test_workspace_use_names_the_organization_a_form_was_drawn_for(
    client, reviewer, make_user, clinic
):
    make_user("portal", client=clinic)
    client.force_login(reviewer)
    form_page(client, f"?scope=portal&organization={clinic.pk}")
    use = WorkspaceUse.objects.get(source=Source.ACCESS_REVIEWS)
    assert use.filters == {"form": "new", "scope": "portal", "organization": str(clinic.pk)}
    response = client.get(reverse("audit_review:uses"))
    assert (
        b"Organization: Example Clinic; Accounts: Client portal accounts; Record an access review"
        in response.content
    )


def test_the_changes_list_names_a_signing_by_its_number(client, reviewer, supervisor):
    review = sign(reviewer)
    client.force_login(reviewer)
    response = client.get(reverse("audit_review:changes"))
    assert f"Access review {review.number}".encode() in response.content


def test_a_form_sent_for_accounts_not_accepted_is_drawn_again_for_the_staff_accounts(
    client, reviewer, supervisor, clinic
):
    client.force_login(reviewer)
    data = posted(form_page(client), organization=str(clinic.pk))  # staff with an organization
    response = client.post(reverse("access_review:review-create"), data)
    assert response.status_code == 200
    body = response.content.decode()
    assert "were not accepted, so it now lists the staff accounts" in body
    assert re.search(r'name="scope" value="staff"', body)
    hidden = re.search(r'<input type="hidden" name="organization"[^>]*>', body).group(0)
    assert f'value="{clinic.pk}"' not in hidden
    assert not AccessReview.objects.exists()
    # Sent again as drawn, it signs for the staff accounts.
    response = client.post(reverse("access_review:review-create"), posted(response))
    assert response.status_code == 302
    assert AccessReview.objects.get().scope == "staff"
