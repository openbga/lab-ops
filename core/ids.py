"""Whole numbers typed into addresses and forms: record ids, page sizes, durations.

`str.isdigit` accepts other scripts' digits, which int() refuses, and int() refuses more than
4300 digits; both used to answer with a server error (reviews 5 and 8). A record id here is a
PostgreSQL bigint, at most 19 digits, so anything longer is not one.
"""

MAX_DIGITS = 19


def ascii_digits(raw, max_digits=MAX_DIGITS):
    """Whether `raw` is a whole number written in 1 to `max_digits` ASCII digits."""
    raw = str(raw or "")
    return 0 < len(raw) <= max_digits and raw.isascii() and raw.isdigit()


def whole_number(raw, max_digits=MAX_DIGITS):
    """int(raw) when it is ascii_digits, else None."""
    return int(raw) if ascii_digits(raw, max_digits) else None
