"""The rule a new password must meet, checked whenever one is chosen: on the account pages
(changing, setting or resetting it by an emailed link), in the admin console (an account's
password, one's own, a new account), by setup_lab, by the changepassword command and by the
demonstration's seed. Django runs every validator in AUTH_PASSWORD_VALIDATORS
(config/settings.py) for each of them. Django's createsuperuser checks it only when it asks for
the password, and lets its operator go past it; with --noinput it does not check.

- Length: at least 15 characters where the password is the account's only factor, and at least 8
  where the laboratory's second-factor rule (core.second_factor) covers the account when the
  password is checked, since its password then always comes with a second factor. An account not
  yet saved (a new account in the admin console, the first administrator at setup) is held to
  15. Each Unicode code point counts as one character; letters of any language, digits, symbols
  and spaces are accepted (Django's form fields refuse the null character), and nothing limits
  the length.
- Common and expected passwords: a password on Django's list of common passwords, taken from
  breached passwords (CommonPasswordValidator); one whose letters, where it has at least four,
  are those of a common password with only digits, symbols, spaces or letter case changed; one
  whose letters are a name of this laboratory, this application or the account with only those
  changed; one character or a text of fewer than 15 characters repeated; a run of letters,
  digits or keyboard keys; one too similar to the account's username, names or email address
  (AccountSimilarityValidator). The whole password is compared each time, never a word found
  inside it, so a passphrase that contains a common word is accepted.
- No composition rule (no required kinds of character) and no periodic change.

What the checks read is read when the password is checked, just before it is saved: the
second-factor rule and the account (its roles, grants, names and status) as they stand then, not
as the form was drawn, and the laboratory's name (setup_lab also names the one it is saving).
The form's guidance is drawn again from the length that check used (PasswordGuidance in
accounts.forms). A rule saved between the check and the save applies from the next password, as
it does to every password chosen before it: a password chosen while the rule covered the account
keeps working if the rule later stops covering it, as every password chosen before this release
does. Each meets the rule in force when it is next changed.

Basis: NIST SP 800-63B-4 §3.1.1.2 (a recommendation): 15 characters for a password used alone, 8
for one used only with a second factor, a blocklist of breached, dictionary and context-specific
passwords compared as a whole, no other composition rules, no periodic change, guidance for the
person choosing. 45 CFR §164.308(a)(5)(ii)(D), password management (addressable), names no rule.
"""

import contextvars
import gzip
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from django.contrib.auth import password_validation
from django.contrib.auth.password_validation import (
    UserAttributeSimilarityValidator,
    get_default_password_validators,
)
from django.core.exceptions import ValidationError
from django.utils.html import format_html, format_html_join

SINGLE_FACTOR_MINIMUM = 15
WITH_SECOND_FACTOR_MINIMUM = 8
# The application's own names, as people shorten them.
APPLICATION_NAMES = ("OpenBGA Lab Ops", "OpenBGA", "Lab Ops")
# A password with fewer letters than this is not compared with the common list by its letters
# alone: nearly every one or two letters are what is left of some common password once its
# digits are taken away. Names are compared whatever their length.
SHORTEST_COMMON_LETTERS = 4
# A repeated text this long or longer is a passage, not a pattern: a password that repeats one
# is accepted.
LONGEST_REPEATED_UNIT = SINGLE_FACTOR_MINIMUM - 1
# The alphabet, the letter keys row by row and the digits (wrapping round), each read either way.
RUNS = ("abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm", "1234567890" * 8)
# Django's list of common passwords, which CommonPasswordValidator reads too.
COMMON_LIST = Path(password_validation.__file__).resolve().parent / "common-passwords.txt.gz"
GUIDANCE = (
    "A few unrelated words, with spaces if you like, make a long password that is easy to "
    "remember. Letters of any language, digits, symbols and spaces can be used, and a password "
    "manager can fill it in."
)
# Where LengthByFactorValidator leaves the length it checked an account's password against, on
# the account object it was given, for the form to state that length (accounts.forms).
CHECKED_MINIMUM = "_vr_password_minimum"
# A laboratory name that is about to be saved (setup_lab), compared as the saved one is.
_proposed_names = contextvars.ContextVar("proposed_laboratory_names", default=())


@contextmanager
def laboratory_named(name):
    """While checking a password, also compare it with `name`, a laboratory name about to be
    saved (setup_lab checks the first administrator's password before it saves the name)."""
    token = _proposed_names.set((*_proposed_names.get(), name or ""))
    try:
        yield
    finally:
        _proposed_names.reset(token)


def current_account(user):
    """The account as it stands now, for a saved account (read again, so a name corrected after
    the account was read, or after the form was drawn, is the one compared); `user` itself for
    an account not yet saved, or None."""
    if user is None or getattr(user, "pk", None) is None:
        return user
    from django.contrib.auth import get_user_model

    return get_user_model()._default_manager.filter(pk=user.pk).first() or user


def minimum_length(user):
    """The fewest characters a new password of `user` may have: 8 when the laboratory's rule
    requires a second factor of the saved, active account now (core.second_factor.requires, one
    statement), 15 otherwise, and for an account not yet saved."""
    if user is None or getattr(user, "pk", None) is None:
        return SINGLE_FACTOR_MINIMUM
    from core.second_factor import requires

    return WITH_SECOND_FACTOR_MINIMUM if requires(user) else SINGLE_FACTOR_MINIMUM


def length_text(minimum, *, own):
    whose = "Your" if own else "The"
    text = f"{whose} password must contain at least {minimum} characters"
    if minimum == WITH_SECOND_FACTOR_MINIMUM:
        account = "your account" if own else "this account"
        return f"{text}, because the laboratory requires a second sign-in factor of {account}."
    return f"{text}."


class LengthByFactorValidator:
    """At least 15 characters, or 8 for an account the second-factor rule covers
    (minimum_length). The length it checked is left on the account object (CHECKED_MINIMUM)."""

    def validate(self, password, user=None):
        minimum = minimum_length(user)
        if user is not None:
            setattr(user, CHECKED_MINIMUM, minimum)
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

    def help_text_for(self, user, *, own, minimum=None):
        return length_text(minimum_length(user) if minimum is None else minimum, own=own)


class AccountSimilarityValidator(UserAttributeSimilarityValidator):
    """Django's check that a password is not too similar to the account's username, names or
    email address, against the account as it stands when the password is checked."""

    def validate(self, password, user=None):
        super().validate(password, current_account(user))


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


def _short_repeat(text):
    unit = _repeated_unit(text)
    return unit is not None and len(unit) <= LONGEST_REPEATED_UNIT


@cache
def _common_list():
    with gzip.open(COMMON_LIST, "rt", encoding="utf-8") as listed:
        return frozenset(line.strip() for line in listed)


def common_password_count():
    """How many passwords Django's list holds, as CommonPasswordValidator reads it."""
    return len(_common_list())


@cache
def _common_letters():
    """The letters of each password on Django's common list, where there are enough to compare."""
    found = {letters(item) for item in _common_list()}
    return frozenset(item for item in found if len(item) >= SHORTEST_COMMON_LETTERS)


def _lab_name():
    from core.models import Lab

    return Lab.objects.filter(pk=1).values_list("name", flat=True).first() or ""


def context_names(user):
    """The names a password must not be built on: the laboratory's (and one about to be saved,
    laboratory_named), this application's, and the account's username, the part of its email
    address before the @, its first and last name, and both names together either way round, as
    the account stands now (current_account)."""
    names = [_lab_name(), *_proposed_names.get(), *APPLICATION_NAMES]
    account = current_account(user)
    if account is not None:
        first = getattr(account, "first_name", "") or ""
        last = getattr(account, "last_name", "") or ""
        email = getattr(account, "email", "") or ""
        names += [
            getattr(account, "username", "") or "",
            email.split("@", 1)[0],
            first,
            last,
            first + last,
            last + first,
        ]
    return [name for name in names if isinstance(name, str)]


class ExpectedPasswordValidator:
    """Common and expected passwords, the whole password compared each time: a common password
    (by its letters, where it has at least four) or one of the context names with only digits,
    symbols, spaces or letter case changed; one character or a text of fewer than 15 characters
    repeated; a run of letters, digits or keyboard keys."""

    def validate(self, password, user=None):
        folded = password.casefold()
        found = letters(password)
        if found:
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
                if name_letters and name_letters in (found, unit):
                    raise ValidationError(
                        "This password is a name of this laboratory, this application or the "
                        "account with only digits, symbols, spaces or letter case changed.",
                        code="password_context_name",
                    )
        joined = "".join(folded.split())
        if _short_repeat(folded) or _short_repeat(joined):
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


def check_published_password(password, user, setting):
    """For the demonstration seed: a password it publishes (`setting` names where it comes from)
    must meet the rule for each account it is given to. The error names the setting and the
    account, never the password."""
    from django.contrib.auth.password_validation import validate_password

    try:
        validate_password(password, user)
    except ValidationError as error:
        raise ValidationError(
            f"{setting} does not meet the password rule for {user.get_username()}: "
            + " ".join(error.messages)
        ) from None


# Django's own help texts speak to the person choosing their own password; for another account's
# password (the admin console's account page, a new account) they name the account instead.
OTHER_ACCOUNT = (
    ("Your password", "The password"),
    ("your other personal information", "the account’s other personal information"),
    ("your account", "the account"),
)


def guidance(user, *, own, minimum=None):
    """What a new password of `user` must be, one line per validator, with the length for this
    account (help_text_for; `minimum`, when given, is the length a check of this password used),
    then advice on choosing one. `own`: the person is choosing their own password; otherwise the
    lines name the account."""
    lines = []
    for validator in get_default_password_validators():
        text_for = getattr(validator, "help_text_for", None)
        if text_for:
            text = text_for(user, own=own, minimum=minimum)
        else:
            text = str(validator.get_help_text())
        if not own:
            for mine, theirs in OTHER_ACCOUNT:
                text = text.replace(mine, theirs)
        lines.append(text)
    return [*lines, GUIDANCE]


def guidance_html(user, *, own, minimum=None):
    """guidance() as the list Django draws for a password field's help."""
    lines = guidance(user, own=own, minimum=minimum)
    items = format_html_join("", "<li>{}</li>", ((line,) for line in lines))
    return format_html("<ul>{}</ul>", items)
