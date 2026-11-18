"""External identifiers and URLs (L1, L2): pure checks shared by forms and services.

An identifier is opaque: trimmed, free of control and formatting characters, never parsed or
compared as a number. URLs are https only; a link's URL must be on its system's host when the
system has a base URL. Lab Ops never fetches either.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

IDENTIFIER_MAX_LENGTH = 200
URL_MAX_LENGTH = 500


def clean_identifier(value: str) -> str:
    """The trimmed identifier, or ValidationError."""
    value = (value or "").strip()
    if not value:
        raise ValidationError(
            "Type the identifier as the source system writes it.", code="required"
        )
    if len(value) > IDENTIFIER_MAX_LENGTH:
        raise ValidationError(
            f"Keep the identifier to {IDENTIFIER_MAX_LENGTH} characters.", code="max_length"
        )
    # Cc control, Cf formatting (zero-width), Cs/Co/Cn; Zl/Zp line and paragraph separators.
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp")
        for ch in value
    ):
        raise ValidationError(
            "The identifier contains control or invisible characters.", code="control_characters"
        )
    return value


def _parts(url: str):
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as error:
        raise ValidationError("Enter a valid https address.", code="invalid") from error
    return parts


def check_https_url(url: str) -> str:
    """A stripped https URL with a host and no credentials ('' allowed), or ValidationError."""
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > URL_MAX_LENGTH:
        raise ValidationError(
            f"Keep the address to {URL_MAX_LENGTH} characters.", code="max_length"
        )
    parts = _parts(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValidationError("Use an https:// address.", code="https_only")
    if parts.username or parts.password:
        raise ValidationError(
            "The address must not contain a user name or password.", code="credentials"
        )
    if any(ch.isspace() or unicodedata.category(ch)[0] == "C" for ch in url):
        raise ValidationError("The address contains spaces or control characters.", code="invalid")
    return url


def host_of(url: str) -> tuple[str, int] | None:
    if not url:
        return None
    parts = _parts(url)
    return (parts.hostname or "").lower(), parts.port or 443


def check_link_url(url: str, base_url: str) -> str:
    """check_https_url plus: the system must have a base address, and the link's address must be
    on its host (and port). A system without a base address takes links without an address."""
    url = check_https_url(url)
    if not url:
        return ""
    if not base_url:
        raise ValidationError(
            "This system has no base address, so its links cannot have one. An administrator "
            "can set the system's base address in External systems.",
            code="no_base_url",
        )
    if host_of(url) != host_of(base_url):
        raise ValidationError(
            "The address must be on the system's own host (%(host)s).",
            code="wrong_host",
            params={"host": host_of(base_url)[0]},
        )
    return url


def url_on_system_host(url: str, base_url: str) -> bool:
    """Whether a stored address may be rendered as a link: on the system's current host. A system
    whose base address changed or was cleared leaves its old addresses as plain text."""
    if not url or not base_url:
        return False
    try:
        return check_https_url(url) == url and host_of(url) == host_of(base_url)
    except ValidationError:
        return False
