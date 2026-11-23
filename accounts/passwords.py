"""The rule a new password must meet, checked whenever one is chosen: on the account pages
(changing, setting or resetting it by an emailed link), in the admin console (an account's
password, one's own, a new account) and by setup_lab. Django runs every validator in
AUTH_PASSWORD_VALIDATORS (config/settings.py) for each of them.

- Length: at least 15 characters where the password is the account's only factor, and at least 8
  where the laboratory's second-factor rule (core.second_factor) covers the account when the
  password is chosen, since its password then always comes with a second factor. An account not
  yet saved (a new account in the admin console, the first administrator at setup) is held to
  15. Each Unicode code point counts as one character; any character, spaces included, is
  accepted, and nothing limits the length.
- Common and expected passwords: a password on Django's list of 20,000 common passwords, taken
  from breached passwords (CommonPasswordValidator); one whose letters are those of a common
  password, or of a name of this laboratory, this application or the account, with only digits,
  symbols, spaces or letter case changed; one made of one character or a short text repeated; a
  run of letters, digits or keyboard keys; one too similar to the account's username, names or
  email address (UserAttributeSimilarityValidator). The whole password is compared each time,
  never a word found inside it, so a passphrase that contains a common word is accepted.
- No composition rule (no required kinds of character) and no periodic change.

The length depends on the rule as it stands when the password is checked: a password chosen
while the rule covered the account keeps working if the rule later stops covering it, as every
password chosen before this release does. Each meets the rule in force when it is next changed.

Basis: NIST SP 800-63B-4 §3.1.1.2 (a recommendation): 15 characters for a password used alone, 8
for one used only with a second factor, a blocklist of breached, dictionary and context-specific
passwords compared as a whole, no other composition rules, no periodic change, guidance for the
person choosing. 45 CFR §164.308(a)(5)(ii)(D), password management (addressable), names no rule.
"""

import gzip
from functools import cache
from pathlib import Path

from django.contrib.auth import password_validation
from django.contrib.auth.password_validation import get_default_password_validators
from django.core.exceptions import ValidationError
from django.utils.html import format_html, format_html_join

SINGLE_FACTOR_MINIMUM = 15
WITH_SECOND_FACTOR_MINIMUM = 8
# The application's own names, as people shorten them.
APPLICATION_NAMES = ("OpenBGA Lab Ops", "OpenBGA", "Lab Ops")
# Shorter letter runs (a password's letters, a name's) are not compared: "ab" plus thirteen
# digits is not a derivative of anything.
SHORTEST_COMPARED = 4
# The alphabet, the letter keys row by row and the digits (wrapping round), each read either way.
RUNS = ("abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm", "1234567890" * 8)
# Django's list of common passwords, which CommonPasswordValidator reads too.
COMMON_LIST = Path(password_validation.__file__).resolve().parent / "common-passwords.txt.gz"
GUIDANCE = (
    "A few unrelated words, with spaces if you like, make a long password that is easy to "
    "remember. Any character can be used, and a password manager can fill it in."
)


def minimum_length(user):
    """The fewest characters a new password of `user` may have: 8 when the laboratory's rule
    requires a second factor of the saved, active account now (core.second_factor.requires, one
    statement), 15 otherwise, and for an account not yet saved."""
    if user is None or getattr(user, "pk", None) is None:
        return SINGLE_FACTOR_MINIMUM
    from core.second_factor import requires

    return WITH_SECOND_FACTOR_MINIMUM if requires(user) else SINGLE_FACTOR_MINIMUM


class LengthByFactorValidator:
    """At least 15 characters, or 8 for an account the second-factor rule covers
    (minimum_length)."""

    def validate(self, password, user=None):
        minimum = minimum_length(user)
        if len(password) < minimum:
            raise ValidationError(
                f"This password is too short. It must contain at least {minimum} characters.",
                code="password_too_short",
                params={"min_length": minimum},
            )

    def get_help_text(self):
        return (
            f"Your password must contain at least {SINGLE_FACTOR_MINIMUM} characters, or "
            f"{WITH_SECOND_FACTOR_MINIMUM} where the laboratory requires a second sign-in factor "
            "of the account."
        )

    def help_text_for(self, user, *, own):
        minimum = minimum_length(user)
        whose = "Your" if own else "The"
        text = f"{whose} password must contain at least {minimum} characters"
        if minimum == WITH_SECOND_FACTOR_MINIMUM:
            account = "your account" if own else "this account"
            return f"{text}, because the laboratory requires a second sign-in factor of {account}."
        return f"{text}."


def letters(text):
    """The letters of `text`, case folded: what is left when digits, symbols, spaces and letter
    case are taken away."""
    return "".join(character for character in text.casefold() if character.isalpha())


def _period(text):
    """The length of the shortest unit `text` repeats (len(text) when it repeats none), by the
    prefix function, in time proportional to its length."""
    border = [0] * len(text)
    for index in range(1, len(text)):
        length = border[index - 1]
        while length and text[index] != text[length]:
            length = border[length - 1]
        if text[index] == text[length]:
            length += 1
        border[index] = length
    return len(text) - (border[-1] if text else 0)


def _repeated_unit(text):
    """The unit `text` is made of when it repeats a shorter text at least twice (a last copy may
    be cut short), else None."""
    if len(text) < 2:
        return None
    period = _period(text)
    return text[:period] if period * 2 <= len(text) else None


@cache
def _common_letters():
    """The letters of each password on Django's common list, where there are enough to compare."""
    with gzip.open(COMMON_LIST, "rt", encoding="utf-8") as listed:
        found = {letters(line.strip()) for line in listed}
    return frozenset(item for item in found if len(item) >= SHORTEST_COMPARED)


def _lab_name():
    from core.models import Lab

    return Lab.objects.filter(pk=1).values_list("name", flat=True).first() or ""


def context_names(user):
    """The names a password must not be built on: the laboratory's, this application's, and the
    account's username, the part of its email address before the @, its first and last name,
    and both names together either way round."""
    names = [_lab_name(), *APPLICATION_NAMES]
    if user is not None:
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        email = getattr(user, "email", "") or ""
        names += [
            getattr(user, "username", "") or "",
            email.split("@", 1)[0],
            first,
            last,
            first + last,
            last + first,
        ]
    return [name for name in names if isinstance(name, str)]


class ExpectedPasswordValidator:
    """Common and expected passwords, the whole password compared each time: a common password
    or one of the context names with only digits, symbols, spaces or letter case changed; one
    character or a short text repeated; a run of letters, digits or keyboard keys."""

    def validate(self, password, user=None):
        folded = password.casefold()
        found = letters(password)
        if len(found) >= SHORTEST_COMPARED:
            unit = _repeated_unit(found) or found
            common = _common_letters()
            if found in common or unit in common:
                raise ValidationError(
                    "This password is a common password with only digits, symbols, spaces or "
                    "letter case changed.",
                    code="password_too_common",
                )
            for name in context_names(user):
                name_letters = letters(name)
                if len(name_letters) >= SHORTEST_COMPARED and name_letters in (found, unit):
                    raise ValidationError(
                        "This password is a name of this laboratory, this application or the "
                        "account with only digits, symbols, spaces or letter case changed.",
                        code="password_context_name",
                    )
        joined = "".join(folded.split())
        if _repeated_unit(folded) or _repeated_unit(joined):
            raise ValidationError(
                "This password repeats one character or a short text.",
                code="password_repeated",
            )
        if len(joined) > 1 and any(joined in run or joined in run[::-1] for run in RUNS):
            raise ValidationError(
                "This password is a run of letters, digits or keyboard keys.",
                code="password_run",
            )

    def get_help_text(self):
        return (
            "Your password can’t be a common password, or a name of this laboratory, this "
            "application or your account, with only digits, symbols, spaces or letter case "
            "changed, nor one character or a short text repeated, nor a run such as 123456789 "
            "or qwertyuiop."
        )


def guidance(user, *, own):
    """What a new password of `user` must be, one line per validator, with the length for this
    account (help_text_for), then advice on choosing one."""
    lines = []
    for validator in get_default_password_validators():
        text_for = getattr(validator, "help_text_for", None)
        lines.append(text_for(user, own=own) if text_for else validator.get_help_text())
    return [*lines, GUIDANCE]


def guidance_html(user, *, own):
    """guidance() as the list Django draws for a password field's help."""
    items = format_html_join("", "<li>{}</li>", ((line,) for line in guidance(user, own=own)))
    return format_html("<ul>{}</ul>", items)
