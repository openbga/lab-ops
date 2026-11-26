"""Confirmation regressions for the signed access review on synthetic records."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.urls import reverse

from accounts.models import User
from audit_review import access_report, access_reviews, seed
from audit_review.models import AccessReview, Decision
from audit_review.tests.conftest import clinic as clinic
from audit_review.tests.conftest import make_user as make_user
from audit_review.tests.conftest import reviewer as reviewer
from audit_review.tests.test_access_report import account_change, http_for
from audit_review.tests.test_access_reviews import form_page, posted
from core.models import AuditEvent
from tests.test_review_access_signoff import _run_signoff_demo


@pytest.mark.django_db
def test_demo_recognizes_seeded_technician_after_creator_is_renamed(make_user, monkeypatch):
    # Start with the demonstration as it existed before seed_demo began signing reviews.
    with monkeypatch.context() as earlier:
        earlier.setattr(seed, "_access_review", lambda reviewer: None)
        _run_signoff_demo(earlier)
    technician = User.objects.get(username="former_technician")
    creator = User.objects.get(username="demo_admin")
    creation = AuditEvent.objects.get(
        object_type="accounts.user", object_id=str(technician.pk), action="account.created"
    )
    assert creation.actor_id == creator.pk and creation.reason == seed.UNUSED_REASON
    assert not AccessReview.objects.exists()

    owner = make_user("other-demo-owner", is_staff=True, is_superuser=True)
    account_change(
        http_for(owner),
        creator,
        username="original-demo-admin",
        email="original-operator@example.invalid",
    )
    _run_signoff_demo(monkeypatch)

    assert User.objects.get(username="demo_admin").pk != creation.actor_id
    review = AccessReview.objects.get()
    technician.refresh_from_db()
    assert not technician.has_usable_password()
    assert (
        access_report.unused_accounts(90, {}, now=review.signed_at)
        .filter(pk=technician.pk)
        .exists()
    )
    role = review.items.get(account=technician)
    assert role.label == "Technical staff role"
    assert role.decision == Decision.REMOVE


@pytest.mark.django_db(transaction=True)
def test_limit_warning_clears_when_the_redrawn_scope_fits(
    client, reviewer, make_user, clinic, monkeypatch
):
    make_user("resident-contact", client=clinic)
    visitors = User.objects.bulk_create(
        [
            User(
                username=f"visitor-{i}",
                email=f"visitor-{i}@example.invalid",
                client=clinic,
                is_active=False,
            )
            for i in range(access_reviews.MOST_ACCOUNTS)
        ]
    )
    visitor_ids = [user.pk for user in visitors]
    client.force_login(reviewer)
    opened = form_page(client, f"?scope=portal&organization={clinic.pk}")
    data = posted(opened, decide=Decision.REMOVE)

    def set_visitors_active(active):
        try:
            User.objects.filter(pk__in=visitor_ids).update(is_active=active)
        finally:
            connections.close_all()

    sign = access_reviews.sign_access_review
    with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as timing:

        def sign_while_over_limit(**kwargs):
            pool.submit(set_visitors_active, True).result(timeout=15)
            try:
                return sign(**kwargs)
            except ValidationError as exc:
                assert exc.messages == [access_reviews.TOO_MANY]
                pool.submit(set_visitors_active, False).result(timeout=15)
                raise

        timing.setattr(access_reviews, "sign_access_review", sign_while_over_limit)
        response = client.post(reverse("access_review:review-create"), data)

    assert response.status_code == 200 and not AccessReview.objects.exists()
    reading = response.context["reading"]
    assert not reading.too_many and reading.accounts == len(reading.grants) == 1
    assert response.context["form"][reading.grants[0].field_name].value() == Decision.REMOVE
    assert b'name="confirm_password"' in response.content
    assert b"this choice has more" not in response.content
