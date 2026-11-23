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
- A password marked as compromised for the account (accounts.compromised), while the mark is open
  or after it was lifted: compared with the copy of its hash each mark keeps
  (MarkedPasswordValidator).
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
import inspect
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
# A repeated text this long or longer, in characters as typed, is a passage, not a pattern: a
# password that repeats one is accepted.
LONGEST_REPEATED_UNIT = SINGLE_FACTOR_MINIMUM - 1
# What people put between the copies of a repeated text, besides any kind of space: a password
# followed by one of these is compared too, so "word word" repeats "word ".
SEPARATORS = ("-", "_", ".", ",", "/", "|", ":", ";", "+", "*", "#", "!")
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
# Whether the saved laboratory name is read too (the demonstration's seed knows the name and
# checks before it may read any table).
_read_saved_name = contextvars.ContextVar("read_saved_laboratory_name", default=True)


@contextmanager
def laboratory_named(name, *, read_saved=True):
    """While checking a password, also compare it with `name`, a laboratory name about to be
    saved (setup_lab checks the first administrator's password before it saves the name). With
    `read_saved` false, compare with `name` instead of reading the saved name: the caller knows
    it and must not read the laboratory's table yet."""
    names = _proposed_names.set((*_proposed_names.get(), name or ""))
    reading = _read_saved_name.set(_read_saved_name.get() and read_saved)
    try:
        yield
    finally:
        _read_saved_name.reset(reading)
        _proposed_names.reset(names)


def _saved(user):
    """Whether `user` is a saved account: it has a primary key and is not an instance about to be
    added (which may carry one already)."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    state = getattr(user, "_state", None)
    return not getattr(state, "adding", False)


def current_account(user):
    """The account as it stands now, for a saved account (read again, so a name corrected after
    the account was read, or after the form was drawn, is the one compared; None when it no
    longer exists); `user` itself for an account not yet saved, or None."""
    if user is not None and getattr(user, "pk", None) is None:
        state = getattr(user, "_state", None)
        if state is not None and not state.adding:
            return None  # an instance that was saved and then deleted (Django clears its key)
    if not _saved(user):
        return user
    from django.contrib.auth import get_user_model

    return get_user_model()._default_manager.filter(pk=user.pk).first()


def minimum_length(user):
    """The fewest characters a new password of `user` may have: 8 when the laboratory's rule
    requires a second factor of the saved, active account now (core.second_factor.requires, one
    statement), 15 otherwise, and for an account not yet saved."""
    if not _saved(user):
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
    """The length of the shortest unit `text` (a string or a list of characters) repeats
    (len(text) when it repeats none), by the prefix function, in time proportional to its
    length."""
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


def _short_repeat(characters):
    """Whether the characters (each case folded on its own, so a unit's length is counted in the
    characters typed) repeat a text of fewer than 15 characters at least twice, a last copy
    possibly cut short, with or without one separator after the last copy. At most 14 passes,
    each ending at the first character that breaks the pattern."""
    present = set(characters)
    marks = sorted(mark for mark in present if mark in SEPARATORS or mark.isspace())
    for text in (characters, *([*characters, mark] for mark in marks)):
        for unit in range(1, LONGEST_REPEATED_UNIT + 1):
            if 2 * unit > len(text):
                break
            if all(text[index] == text[index - unit] for index in range(unit, len(text))):
                return True
    return False


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
    saved = [_lab_name()] if _read_saved_name.get() else []
    names = [*saved, *_proposed_names.get(), *APPLICATION_NAMES]
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
        if _short_repeat([character.casefold() for character in password]):
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


class MarkedPasswordValidator:
    """A password marked as compromised for this account (accounts.compromised), open or lifted,
    is refused as its new password: compared with the copy of its hash each mark keeps, one
    comparison per mark. Nothing to compare for an account not yet saved."""

    def validate(self, password, user=None):
        if not _saved(user):
            return
        from django.contrib.auth.hashers import check_password

        from .models import CompromisedPassword

        marked = CompromisedPassword.objects.filter(user_id=user.pk).order_by("-pk")
        for encoded in marked.values_list("password", flat=True):
            if check_password(password, encoded):
                raise ValidationError(
                    "This password was marked as compromised for this account. Choose a "
                    "different one.",
                    code="password_marked_compromised",
                )

    def get_help_text(self):
        return "Your password can’t be one that was marked as compromised for your account."

    def help_text_for(self, user, *, own):
        """Stated only for an account with a marked password: for others it says nothing."""
        if not _saved(user):
            return ""
        from .models import CompromisedPassword

        if not CompromisedPassword.objects.filter(user_id=user.pk).exists():
            return ""
        return self.get_help_text()


# Why the rule refused a password, by the refusal's code, in fixed words: a validator's own message
# may quote the password.
REFUSAL_REASONS = {
    "password_too_short": "it is too short",
    "password_too_similar": "it is too similar to the account's username, names or email address",
    "password_too_common": "it is a common password, or one with only digits, symbols, spaces or "
    "letter case changed",
    "password_context_name": "it is a name of the laboratory, this application or the account "
    "with only digits, symbols, spaces or letter case changed",
    "password_repeated": "it repeats one character or a short text",
    "password_run": "it is a run of letters, digits or keyboard keys",
    "password_marked_compromised": "it was marked as compromised for the account",
}


def check_published_password(password, user, setting):
    """For the demonstration seed: a password it publishes (`setting` names where it comes from)
    must meet the rule for the account `user` (unsaved, as the seed will create it). The error
    names the setting, the account unless one of the two contains the other, and the reasons in
    fixed words: never the password, nor a validator's message, which may quote it."""
    from django.contrib.auth.password_validation import validate_password

    try:
        validate_password(password, user)
    except ValidationError as error:
        username = user.get_username()
        overlap = username.casefold() in password.casefold() or (
            password.casefold() in username.casefold()
        )
        whose = "one of the accounts it creates" if overlap else username
        reasons = sorted(
            {
                REFUSAL_REASONS.get(item.code, "a password check of this deployment refused it")
                for item in error.error_list
            }
        )
        raise ValidationError(
            f"{setting} does not meet the password rule for {whose}: " + "; ".join(reasons) + "."
        ) from None


# Django's own help texts speak to the person choosing their own password; for another account's
# password (the admin console's account page, a new account) they name the account instead.
OTHER_ACCOUNT = (
    ("Your password", "The password"),
    ("your other personal information", "the account’s other personal information"),
    ("your account", "the account"),
)


def _takes_minimum(hook):
    """Whether a validator's help_text_for accepts the length a check used as the keyword
    `minimum`."""
    try:
        parameters = inspect.signature(hook).parameters.values()
    except TypeError, ValueError:
        return False
    by_name = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return any(
        (parameter.name == "minimum" and parameter.kind in by_name)
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def guidance(user, *, own, minimum=None):
    """What a new password of `user` must be, one line per validator, with the length for this
    account (help_text_for; `minimum`, when given, is the length a check of this password used),
    then advice on choosing one. `own`: the person is choosing their own password; otherwise the
    lines name the account. A validator whose help is empty for this account adds no line."""
    lines = []
    for validator in get_default_password_validators():
        text_for = getattr(validator, "help_text_for", None)
        if not callable(text_for):
            text = str(validator.get_help_text())
        elif _takes_minimum(text_for):
            text = text_for(user, own=own, minimum=minimum)
        else:
            text = text_for(user, own=own)
        if not text:
            continue
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
