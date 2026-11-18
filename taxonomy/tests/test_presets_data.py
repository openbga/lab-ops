"""The preset catalogue (T2) is consistent data: pure, no database."""

import re

from taxonomy.presets import ALWAYS, DEFAULT_TEAM, GROUPS, PRESETS, TEAMS

KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIORITIES = {"low", "normal", "high", "urgent"}


def all_categories():
    return [category for preset in PRESETS.values() for category in preset.categories]


def test_catalogue_has_the_six_presets_of_the_product_document():
    assert list(PRESETS) == [
        "all",
        "clinical",
        "toxicology",
        "environmental",
        "food-manufacturing",
        "biorepository",
    ]
    assert ALWAYS in PRESETS
    assert len(PRESETS["all"].categories) == 17


def test_every_category_points_at_a_known_group_and_team_with_valid_values():
    for category in all_categories():
        assert KEY.match(category.key), category.key
        assert category.group in GROUPS
        assert category.team in TEAMS
        assert category.priority in PRIORITIES
        assert len(category.name) <= 120


def test_keys_are_unique_unless_the_same_category_is_shared():
    by_key = {}
    for category in all_categories():
        assert by_key.setdefault(category.key, category) == category, category.key
    shared = {key for key in by_key if sum(c.key == key for c in all_categories()) > 1}
    assert shared == {"requisition-information", "chain-of-custody-discrepancy"}
    names = {}
    for category in by_key.values():
        assert (
            names.setdefault((category.group, category.name.casefold()), category.key)
            == category.key
        )
    assert not set(by_key) & set(GROUPS)  # a group key never doubles as a category key


def test_flags_follow_the_product_document():
    by_key = {c.key: c for c in all_categories()}
    assert [k for k, c in by_key.items() if c.is_complaint] == ["complaint"]
    assert [k for k, c in by_key.items() if c.is_restricted] == ["privacy-request"]
    assert {k for k, c in by_key.items() if not c.client_may_raise} == {
        "critical-result-follow-up",
        "privacy-request",
    }
    assert by_key["critical-result-follow-up"].priority == "urgent"
    assert DEFAULT_TEAM == "client-services" and TEAMS[DEFAULT_TEAM] == "Client services"
