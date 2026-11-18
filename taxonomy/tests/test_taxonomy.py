"""Request categories and presets (T1, T2): services, constraints and pages."""

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts.models import User
from core.models import AuditEvent
from service_levels.models import ServiceTarget
from taxonomy import presets as catalogue
from taxonomy.models import RequestCategory
from taxonomy.services import (
    create_category,
    load_presets,
    move_category,
    offered_categories,
    update_category,
)
from teams.models import Team

PASSWORD = "test-password-729!"


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def user(name, *roles):
    account = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD)
    for role in roles:
        account.groups.add(Group.objects.get_or_create(name=role)[0])
    return account


@pytest.fixture
def admin(db):
    return user("setup_admin", "admin")


@pytest.fixture
def supervisor(db):
    return user("supervisor", "supervisor")


@pytest.fixture
def group(admin):
    return create_category(actor=admin, key="status-results", name="Status and results")


@pytest.fixture
def category(admin, group):
    return create_category(
        actor=admin, key="missing-result", name="Missing result or report", parent=group
    )


# T1 ----------------------------------------------------------------------------------------


def test_setup_is_admin_only(supervisor, db):
    with pytest.raises(PermissionDenied):
        create_category(actor=supervisor, key="x", name="X")
    with pytest.raises(PermissionDenied):
        load_presets(actor=supervisor, keys=[])


def test_two_levels_only(admin, group, category):
    with pytest.raises(ValidationError, match="two levels"):
        create_category(actor=admin, key="deep", name="Too deep", parent=category)
    other = create_category(actor=admin, key="billing", name="Billing")
    with pytest.raises(ValidationError, match="holds categories"):
        update_category(
            actor=admin,
            category_id=group.pk,
            expected_revision=group.revision,
            reason="Nest",
            parent=other,
        )


def test_key_never_changes_and_is_unique(admin, group, category):
    with pytest.raises(ValidationError, match="never changes"):
        update_category(
            actor=admin, category_id=category.pk, expected_revision=1, reason="Rename", key="new"
        )
    with pytest.raises(ValidationError, match="already uses this key"):
        create_category(actor=admin, key="missing-result", name="Another", parent=group)
    renamed = update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=1,
        reason="Our wording",
        name="Result not received",
    )
    assert renamed.key == "missing-result" and renamed.revision == 2


def test_names_unique_within_a_group_case_insensitively(admin, group, category):
    with pytest.raises(ValidationError, match="already has a category with this name"):
        create_category(actor=admin, key="dup", name="MISSING RESULT OR REPORT", parent=group)
    other = create_category(actor=admin, key="other-group", name="Other group")
    create_category(actor=admin, key="dup", name="Missing result or report", parent=other)


@pytest.mark.parametrize("flag", ["is_complaint", "is_restricted"])
def test_flag_changes_need_admin_and_a_reason(admin, supervisor, category, flag):
    with pytest.raises(PermissionDenied):
        update_category(
            actor=supervisor,
            category_id=category.pk,
            expected_revision=1,
            reason="x",
            **{flag: True},
        )
    with pytest.raises(ValidationError, match="reason"):
        update_category(
            actor=admin, category_id=category.pk, expected_revision=1, reason="  ", **{flag: True}
        )
    update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=1,
        reason="Quality decision QM-TEST-1",
        **{flag: True},
    )
    event = AuditEvent.objects.get(action="category.flags_changed")
    assert event.reason == "Quality decision QM-TEST-1"
    assert event.before[flag] is False and event.after[flag] is True


def test_stale_and_empty_updates_are_refused(admin, category):
    update_category(
        actor=admin,
        category_id=category.pk,
        expected_revision=1,
        reason="r",
        guidance="Ask for the order number.",
    )
    with pytest.raises(ValidationError, match="changed after you opened it"):
        update_category(
            actor=admin, category_id=category.pk, expected_revision=1, reason="r", name="X"
        )
    with pytest.raises(ValidationError, match="Change at least one"):
        update_category(
            actor=admin,
            category_id=category.pk,
            expected_revision=2,
            reason="r",
            name=category.name,
        )


def test_inactive_categories_and_groups_are_not_offered(admin, group, category):
    other = create_category(
        actor=admin,
        key="result-question",
        name="Result question",
        parent=group,
        client_may_raise=True,
    )
    assert set(offered_categories()) == {category, other}
    assert list(offered_categories(portal=True)) == [other]
    update_category(
        actor=admin, category_id=other.pk, expected_revision=1, reason="Retired", is_active=False
    )
    assert list(offered_categories()) == [category]
    update_category(
        actor=admin, category_id=group.pk, expected_revision=1, reason="Retired", is_active=False
    )
    assert list(offered_categories()) == []
    assert group not in offered_categories()  # a group is never offered


def test_inactive_category_is_refused_for_a_new_request(admin, group, category, db):
    from desk.services import create_request

    agent = user("agent", "client_services")
    update_category(
        actor=admin, category_id=category.pk, expected_revision=1, reason="Retired", is_active=False
    )
    category.refresh_from_db()
    with pytest.raises(ValidationError):
        create_request(
            actor=agent,
            category=category,
            subject="Example",
            first_entry_kind="note",
            first_entry_body="Synthetic.",
            channel="phone",
        )


def test_reorder_within_siblings(admin, group, category):
    second = create_category(actor=admin, key="b", name="B", parent=group)
    third = create_category(actor=admin, key="c", name="C", parent=group)

    def order():
        return list(
            RequestCategory.objects.filter(parent=group)
            .order_by("position")
            .values_list("key", flat=True)
        )

    assert order() == ["missing-result", "b", "c"]
    move_category(actor=admin, category_id=third.pk, direction="up")
    assert order() == ["missing-result", "c", "b"]
    move_category(actor=admin, category_id=category.pk, direction="down")
    assert order() == ["c", "missing-result", "b"]
    with pytest.raises(ValidationError, match="already the first"):
        move_category(actor=admin, category_id=third.pk, direction="up")
    assert AuditEvent.objects.filter(action="category.reordered").count() == 2
    del second


def test_default_team_must_be_active(admin, group):
    team = Team.objects.create(slug="retired", name="Retired team", is_active=False)
    with pytest.raises(ValidationError, match="active team"):
        create_category(actor=admin, key="x", name="X", parent=group, default_team=team)


# T2 ----------------------------------------------------------------------------------------


def all_keys(keys):
    found = set()
    for key in ["all", *keys]:
        for item in catalogue.PRESETS[key].categories:
            found |= {item.key, item.group}
    return found


@pytest.mark.parametrize("preset", [k for k in catalogue.PRESETS if k != "all"])
def test_each_preset_loads(admin, preset):
    counts = load_presets(actor=admin, keys=[preset])
    keys = set(RequestCategory.objects.values_list("key", flat=True))
    assert keys == all_keys([preset])
    assert counts["categories"] == RequestCategory.objects.filter(parent__isnull=False).count()
    for item in catalogue.PRESETS[preset].categories:
        category = RequestCategory.objects.get(key=item.key)
        assert category.parent.key == item.group
        assert category.default_team.slug == item.team
        assert category.default_priority == item.priority
        assert category.client_may_raise is item.client_may_raise
        assert category.is_complaint is item.is_complaint
        assert category.is_restricted is item.is_restricted
        assert category.preset in ("all", preset)
    event = AuditEvent.objects.get(action="preset.loaded")
    assert event.after["presets"] == ["all", preset]


def test_loading_twice_changes_nothing(admin):
    load_presets(actor=admin, keys=["clinical"])
    before = list(RequestCategory.objects.order_by("pk").values())
    teams = list(Team.objects.order_by("pk").values())
    events = AuditEvent.objects.count()
    assert load_presets(actor=admin, keys=["clinical"]) == {
        "groups": 0,
        "categories": 0,
        "teams": 0,
    }
    assert list(RequestCategory.objects.order_by("pk").values()) == before
    assert list(Team.objects.order_by("pk").values()) == teams
    assert AuditEvent.objects.count() == events


def test_presets_share_common_categories_by_key(admin):
    load_presets(actor=admin, keys=["clinical"])
    counts = load_presets(actor=admin, keys=["toxicology", "environmental", "food-manufacturing"])
    assert RequestCategory.objects.filter(key="requisition-information").count() == 1
    assert RequestCategory.objects.filter(key="chain-of-custody-discrepancy").count() == 1
    assert counts["categories"] == 2 + 4 + 2  # tox shares one with clinical, food one with env


def test_loading_keeps_local_edits(admin):
    load_presets(actor=admin, keys=[])
    complaint = RequestCategory.objects.get(key="complaint")
    update_category(
        actor=admin,
        category_id=complaint.pk,
        expected_revision=1,
        reason="Our wording",
        name="Formal complaint",
        is_active=False,
    )
    load_presets(actor=admin, keys=["clinical"])
    complaint.refresh_from_db()
    assert complaint.name == "Formal complaint" and not complaint.is_active
    assert not RequestCategory.objects.filter(name="Complaint").exists()


def test_missing_teams_are_created_by_slug_and_existing_ones_reused(admin):
    existing = Team.objects.create(slug="billing", name="Accounts receivable")
    renamed_default = Team.objects.get(is_default=True)
    load_presets(actor=admin, keys=[])
    assert Team.objects.get(slug="billing") == existing
    assert RequestCategory.objects.get(key="billing-question").default_team == existing
    assert RequestCategory.objects.get(key="other").default_team == renamed_default
    assert set(Team.objects.values_list("slug", flat=True)) == set(catalogue.TEAMS)
    assert AuditEvent.objects.filter(action="team.created").count() == len(catalogue.TEAMS) - 2


def test_presets_create_no_targets(admin):
    load_presets(actor=admin, keys=list(catalogue.PRESETS))
    assert not ServiceTarget.objects.exists()


def test_unknown_preset_is_refused(admin):
    with pytest.raises(ValidationError):
        load_presets(actor=admin, keys=["veterinary"])


# Pages -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "taxonomy:category-list",
        "taxonomy:category-create",
        "taxonomy:group-create",
        "taxonomy:preset-load",
    ],
)
def test_pages_are_admin_only(client, admin, supervisor, name):
    client.login(username=supervisor.username, password=PASSWORD)
    assert client.get(reverse(name)).status_code == 403
    client.login(username=admin.username, password=PASSWORD)
    assert client.get(reverse(name)).status_code == 200


def test_preset_page_loads_and_list_shows_groups(client, admin):
    client.login(username=admin.username, password=PASSWORD)
    response = client.post(reverse("taxonomy:preset-load"), {"presets": ["environmental"]})
    assert response.status_code == 302
    page = client.get(reverse("taxonomy:category-list")).content.decode()
    for text in (
        "Status and results",
        "Bottle kit order",
        "Complaint",
        "Restricted" if False else "Clients may raise",
    ):
        assert text in page
    assert "Privacy request" not in page


def test_category_form_round_trip_and_move(client, admin, group, category):
    client.login(username=admin.username, password=PASSWORD)
    response = client.post(
        reverse("taxonomy:category-create"),
        {"parent": group.pk, "name": "Courier question", "default_priority": "normal"},
    )
    assert response.status_code == 302
    created = RequestCategory.objects.get(name="Courier question")
    assert created.key == "courier-question"
    edit = reverse("taxonomy:category-edit", args=[created.pk])
    response = client.post(
        edit,
        {
            "parent": group.pk,
            "name": "Courier question",
            "default_priority": "high",
            "is_active": "on",
            "is_restricted": "on",
            "reason": "Quality decision",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 302
    created.refresh_from_db()
    assert created.is_restricted and created.default_priority == "high"
    response = client.post(reverse("taxonomy:category-move", args=[created.pk, "up"]))
    assert response.status_code == 302
    assert client.get(reverse("taxonomy:category-move", args=[created.pk, "up"])).status_code == 405
