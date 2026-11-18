"""Person-level link rules (L3): masked labels, the key digest and the placeholder.

Pure functions; no database and no request. `docs/product.md` §Links to external records says
what is allowed. A person-level link (patient, research participant, donor) stores the source
system's key and a masked label, nothing else. The key is shown only on an explicit, audited
reveal; it never enters a URL, page title, list column, log line, audit payload or notification.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError

PERSON_WORDS = {"patient": "Patient", "participant": "Participant", "donor": "Donor"}
PERSON_RECORD_TYPES = frozenset(PERSON_WORDS)
MASK = "·" * 4  # ····
PROPOSAL_MIN_KEY_LENGTH = 8
LABEL_MAX_LENGTH = 120
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_LONG_DIGIT_RUN = re.compile(r"\d{9,}")
# Below this many letters and digits, comparing labels and keys without punctuation would
# refuse too many harmless aliases; the plain whole-key rule still applies.
_NORMALIZED_MIN = 4


def is_person_type(record_type: str) -> bool:
    return record_type in PERSON_RECORD_TYPES


def person_word(record_type: str) -> str:
    return PERSON_WORDS.get(record_type, "Person")


def propose_label(record_type: str, key: str) -> str:
    """`Patient ····4821` from the key's last four characters, when the key has at least eight.

    Returns '' for a shorter key: the user must type an alias."""
    key = (key or "").strip()
    if len(key) < PROPOSAL_MIN_KEY_LENGTH:
        return ""
    return f"{person_word(record_type)} {MASK}{key[-4:]}"


def _alnum(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def label_problems(label: str, key: str) -> list[str]:
    """Why `label` cannot be a person-level link's label for `key` (empty when it can).

    The messages never quote the key or the label."""
    label = (label or "").strip()
    key = (key or "").strip()
    problems = []
    if not label:
        problems.append(
            "Give a label. Type an alias when the key is shorter than eight characters."
        )
        return problems
    if len(label) > LABEL_MAX_LENGTH:
        problems.append(f"Keep the label to {LABEL_MAX_LENGTH} characters.")
    if key and (
        key.casefold() in label.casefold()
        or (len(_alnum(key)) >= _NORMALIZED_MIN and _alnum(key) in _alnum(label))
    ):
        problems.append(
            "The label must not contain the whole key. Use the last four characters or an alias."
        )
    if _EMAIL.search(label):
        problems.append("The label must not contain an email address.")
    if _LONG_DIGIT_RUN.search(label):
        problems.append("The label must not contain more than eight digits in a row.")
    return problems


def check_label(label: str, key: str) -> str:
    """The stripped label, or ValidationError listing every problem."""
    problems = label_problems(label, key)
    if problems:
        raise ValidationError(problems, code="person_label")
    return label.strip()


def key_digest(key: str, secret: str) -> str:
    """HMAC-SHA256 of a person-level key, for audit events that must not hold the key.

    The same key gives the same digest within a deployment (keyed by its SECRET_KEY), so an
    auditor can tell repeated searches apart without learning the key."""
    return hmac.new(secret.encode(), key.strip().encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class PersonPlaceholder:
    """What a template gets for a person-level link when the reader lacks the capability.

    Carries no identifier, label, URL or link id."""

    record_type: str

    @property
    def text(self) -> str:
        return f"{person_word(self.record_type)} linked"

    is_person = True
    is_placeholder = True

    def __str__(self) -> str:
        return self.text
