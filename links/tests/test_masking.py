"""Person-level label rules and identifier/URL checks (L1, L2, L3): pure, no database."""

import pytest
from django.core.exceptions import ValidationError

from links.identifiers import check_https_url, check_link_url, clean_identifier
from links.masking import (
    PersonPlaceholder,
    check_label,
    key_digest,
    label_problems,
    propose_label,
)

KEY = "PT-TEST-00004821"


@pytest.mark.parametrize(
    ("record_type", "key", "label"),
    [
        ("patient", KEY, "Patient ····4821"),
        ("participant", "PARTICIPANT-TEST-0007", "Participant ····0007"),
        ("donor", "DONOR-TEST-1234", "Donor ····1234"),
        ("patient", "12345678", "Patient ····5678"),  # exactly eight characters
        ("patient", "1234567", ""),  # shorter: the user types an alias
        ("patient", "  ", ""),
    ],
)
def test_proposed_label(record_type, key, label):
    assert propose_label(record_type, key) == label


def test_proposed_label_passes_its_own_rules():
    assert check_label(propose_label("patient", KEY), KEY) == "Patient ····4821"


@pytest.mark.parametrize(
    ("label", "problem"),
    [
        ("", "Give a label"),
        (f"Patient {KEY}", "whole key"),
        (f"patient {KEY.lower()}", "whole key"),  # case-insensitive
        ("Patient PT TEST 00004821", "whole key"),  # punctuation does not hide it
        ("Patient pt.test.00004821 follow-up", "whole key"),
        ("Patient jane.doe@example.invalid", "email address"),
        ("Patient 123456789", "more than eight digits"),
        ("Patient ref 2026-11-17 0012345678", "more than eight digits"),
        ("x" * 121, "120 characters"),
    ],
)
def test_refused_labels(label, problem):
    problems = label_problems(label, KEY)
    assert any(problem in message for message in problems), problems
    with pytest.raises(ValidationError):
        check_label(label, KEY)


@pytest.mark.parametrize(
    "label",
    ["Patient ····4821", "Patient A (Westside draw)", "Donor 12345678", "Participant 2026-11-17"],
)
def test_accepted_labels(label):
    assert label_problems(label, "OTHER-KEY-998877") == []


def test_messages_never_quote_the_key_or_label():
    label = f"Patient {KEY} jane.doe@example.invalid 1234567890"
    for message in label_problems(label, KEY):
        assert KEY not in message and "jane" not in message and "1234567890" not in message


def test_short_key_alias_rules():
    assert label_problems("Donor alias Kestrel", "D7") == []
    assert label_problems("Donor D7", "D7")  # contains the whole key


def test_key_digest_is_keyed_and_stable_and_hides_the_key():
    digest = key_digest(KEY, "secret-one")
    assert digest == key_digest(f"  {KEY} ", "secret-one")
    assert digest != key_digest(KEY, "secret-two")
    assert KEY not in digest and "4821" not in digest
    assert len(digest) == 64


def test_placeholder_carries_nothing_identifying():
    placeholder = PersonPlaceholder("participant")
    assert str(placeholder) == "Participant linked"
    assert set(vars(placeholder)) == {"record_type"}


# Identifiers and URLs ------------------------------------------------------------------------


def test_identifier_is_trimmed_and_otherwise_untouched():
    assert clean_identifier("  ORD-TEST-000042 ") == "ORD-TEST-000042"
    assert clean_identifier("0042") == "0042"  # never parsed as a number


@pytest.mark.parametrize(
    "value", ["", "   ", "ORD\x00TEST", "ORD\tTEST", "ORD\u200bTEST", "a\u2028b", "x" * 201]
)
def test_bad_identifiers_are_refused(value):
    with pytest.raises(ValidationError):
        clean_identifier(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://lis.example.invalid/orders/1",
        "javascript:alert(1)",
        "https://",
        "https://user:pass@lis.example.invalid/",
        "https://lis.example.invalid/a b",
        "https://lis.example.invalid:99999/",
        "ftp://lis.example.invalid/",
    ],
)
def test_only_plain_https_urls(url):
    with pytest.raises(ValidationError):
        check_https_url(url)


def test_link_url_must_be_on_the_system_host():
    base = "https://LIS.example.invalid/app/"
    assert check_link_url("https://lis.example.invalid/orders/ORD-TEST-000042", base)
    assert check_link_url("https://lis.example.invalid:443/orders/1", base)
    assert check_link_url("", base) == ""
    # A system without a base address takes no addresses at all.
    with pytest.raises(ValidationError, match="no base address"):
        check_link_url("https://anywhere.example.invalid/x", "")
    for other in ("https://lis.example.invalid.evil.invalid/", "https://lis.example.invalid:8443/"):
        with pytest.raises(ValidationError):
            check_link_url(other, base)
