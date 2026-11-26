"""End dates of grants (accounts.grant_ends) in Access review: the access report shows a grant's
last day, an ended grant as ended and counts it as held by no filter; a signed access review lists
only grants in force, with their last day in the grant's words, so a change of an end, or an end
passing, between the form and the signing refuses the signing; and an ended grant decided for
removal is no longer held. All data is synthetic."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts import grant_ends
from accounts.models import CapabilityEnd
from audit_review import access_report, access_reviews
from audit_review.models import AccessReview, AccessScope, Decision, GrantKind
from clients.models import ClientSite, PortalAccess

from .conftest import review_permission
from .test_access_reviews import patient_permission, sign

pytestmark = pytest.mark.django_db


def rows_for(**filters):
    return {
        row.username: row for row in access_report.describe(list(access_report.accounts(filters)))
    }


def end(user, permission, ends_at):
    CapabilityEnd.objects.update_or_create(
        user=user, permission=permission, defaults={"ends_at": ends_at}
    )


def past():
    return timezone.now() - timedelta(hours=1)


def day_ahead(days=10):
    return grant_ends.today() + timedelta(days=days)


def test_the_report_shows_a_last_day_and_an_ended_grant_as_ended(make_user, reviewer):
    ending = make_user("ending", "technical")
    ending.user_permissions.add(patient_permission())
    end(ending, patient_permission(), grant_ends.end_of(day_ahead()))
    ended = make_user("ended", "technical")
    ended.user_permissions.add(patient_permission())
    end(ended, patient_permission(), past())
    rows = rows_for()
    item = rows["ending"].capabilities[0]
    assert item.text == "Can view patient information"
    assert item.note.startswith(f"Directly, last day {day_ahead().isoformat()}")
    item = rows["ended"].capabilities[0]
    assert item.text == "Can view patient information (ended)"
    assert ", gives nothing" in item.note
    plain = access_report.describe(list(access_report.accounts({})), history=False)
    assert {row.username: row for row in plain}["ended"].capabilities[0].missing
    held = rows_for(capability="view_patient_information")
    assert "ending" in held and "ended" not in held


def test_the_report_shows_a_portal_accesss_last_day_and_what_it_sees_once_ended(
    make_user, reviewer, clinic
):
    north = ClientSite.objects.create(client=clinic, name="North site", code="N")
    sited = make_user("sited", client=clinic)
    access = PortalAccess.objects.create(
        user=sited, client=clinic, ends_at=grant_ends.end_of(day_ahead())
    )
    access.sites.add(north)
    whole = make_user("whole", client=clinic)
    PortalAccess.objects.create(user=whole, client=clinic, whole_organization=True, ends_at=past())
    rows = rows_for()
    assert rows["sited"].portal == (
        f"Tickets of North site, and those it raised, last day {day_ahead().isoformat()}"
    )
    over = grant_ends.last_day_of(PortalAccess.objects.get(user=whole).ends_at).isoformat()
    assert rows["whole"].portal == f"Only the tickets it raised (access ended after {over})"


def test_a_review_lists_only_grants_in_force_with_their_last_day(make_user, reviewer, clinic):
    ending = make_user("ending", "technical")
    ending.user_permissions.add(patient_permission())
    end(ending, patient_permission(), grant_ends.end_of(day_ahead()))
    ended = make_user("ended")  # no role: its only grant has ended, so it holds nothing
    ended.user_permissions.add(patient_permission())
    end(ended, patient_permission(), past())
    staff = access_reviews.reading(AccessScope.STAFF)
    words = {
        (grant.account_username, grant.kind, grant.code): grant.detail for grant in staff.grants
    }
    assert words[("ending", GrantKind.CAPABILITY, "view_patient_information")] == (
        f"Given directly, last day {day_ahead().isoformat()}"
    )
    assert "ended" not in {grant.account_username for grant in staff.grants}
    portal = make_user("portal", client=clinic)
    PortalAccess.objects.create(
        user=portal, client=clinic, whole_organization=True, ends_at=grant_ends.end_of(day_ahead())
    )
    lapsed = make_user("lapsed", client=clinic)
    PortalAccess.objects.create(user=lapsed, client=clinic, whole_organization=True, ends_at=past())
    portals = {
        grant.account_username: (grant.code, grant.detail)
        for grant in access_reviews.reading(AccessScope.PORTAL).grants
    }
    assert portals["portal"] == (
        f"{clinic.pk}:all",
        f"Its organization's tickets, last day {day_ahead().isoformat()}",
    )
    assert portals["lapsed"] == (f"{clinic.pk}:none", "Only the tickets it raised")


@pytest.mark.parametrize(
    "change",
    ["an end set", "an end moved", "an end cleared", "an end passed", "a portal end set"],
)
def test_an_end_changed_or_passed_after_the_form_was_drawn_refuses_the_signing(
    make_user, reviewer, clinic, change
):
    holder = make_user("holder", "technical")
    holder.user_permissions.add(patient_permission())
    if change != "an end set":
        end(holder, patient_permission(), grant_ends.end_of(day_ahead()))
    portal = make_user("portal", client=clinic)
    PortalAccess.objects.create(user=portal, client=clinic, whole_organization=True)
    scope = AccessScope.PORTAL if change.startswith("a portal") else AccessScope.STAFF
    current = access_reviews.reading(scope)
    if change == "an end set":
        end(holder, patient_permission(), grant_ends.end_of(day_ahead()))
    elif change == "an end moved":
        end(holder, patient_permission(), grant_ends.end_of(day_ahead(3)))
    elif change == "an end cleared":
        CapabilityEnd.objects.filter(user=holder).delete()
    elif change == "an end passed":
        CapabilityEnd.objects.filter(user=holder).update(ends_at=past())
    else:
        PortalAccess.objects.filter(user=portal).update(ends_at=grant_ends.end_of(day_ahead()))
    with pytest.raises(ValidationError) as refused, transaction.atomic():
        access_reviews.sign_access_review(
            actor=reviewer,
            scope=scope,
            decisions={grant.key: Decision.KEEP for grant in current.grants},
            expected=current.fingerprint,
        )
    assert access_reviews.STALE in refused.value.messages
    assert not AccessReview.objects.exists()


def test_a_grant_decided_for_removal_is_not_held_once_its_end_has_passed(make_user, reviewer):
    holder = make_user("holder", "technical")
    holder.user_permissions.add(patient_permission(), review_permission())
    end(holder, patient_permission(), grant_ends.end_of(day_ahead()))
    remove = {
        f"{holder.pk}:{GrantKind.CAPABILITY}:view_patient_information",
        f"{holder.pk}:{GrantKind.CAPABILITY}:review_audit_records",
    }
    review = sign(reviewer, remove=remove)
    items = list(review.items.filter(decision=Decision.REMOVE))
    assert set(access_reviews.still_held(items).values()) == {True}
    CapabilityEnd.objects.filter(user=holder).update(ends_at=past())
    held = {item.code: access_reviews.still_held(items)[item.pk] for item in items}
    assert held == {"view_patient_information": False, "review_audit_records": True}
