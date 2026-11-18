"""Shared row locks for decisions that must hold until the transaction ends.

A decision read from rows that other transactions may change (who may receive a notice, who may
change a setting) holds those rows FOR SHARE: readers and other holders go on, and a change to
one of them (an UPDATE, a DELETE, a FOR UPDATE lock) waits until the decision's transaction
ends. Django's select_for_update offers only the exclusive modes, so this issues the SQL itself.
"""

from django.db import OperationalError, connection

LOCK_NOT_AVAILABLE = "55P03"


MODES = {"share": "FOR SHARE", "key share": "FOR KEY SHARE"}


def share_lock(model, pks, *, nowait=False, mode="share"):
    """Hold the rows of `model` with these primary keys until the transaction ends. Returns the
    keys found. With `nowait`, a row someone is changing raises OperationalError (lock not
    available) at once instead of waiting.

    mode "share" (FOR SHARE) makes every change to the rows wait. mode "key share" (FOR KEY
    SHARE) makes only a deletion, or a change that first takes the row FOR UPDATE, wait: the
    team services lock a team or membership FOR UPDATE to add a member or change a lead, while
    an assignment locks them FOR NO KEY UPDATE and goes ahead."""
    if not connection.in_atomic_block:
        raise RuntimeError("A shared row lock lasts for a transaction; open one first.")
    pks = sorted({pk for pk in pks if pk is not None})
    if not pks:
        return []
    table = connection.ops.quote_name(model._meta.db_table)
    column = connection.ops.quote_name(model._meta.pk.column)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM {table} WHERE {column} = ANY(%s) ORDER BY {column} "
            f"{MODES[mode]}{' NOWAIT' if nowait else ''}",
            [pks],
        )
        return [row[0] for row in cursor.fetchall()]


def is_lock_unavailable(exc):
    """True for PostgreSQL's lock_not_available (NOWAIT, or lock_timeout ran out)."""
    if not isinstance(exc, OperationalError):
        return False
    cause = exc.__cause__
    return getattr(cause, "sqlstate", None) == LOCK_NOT_AVAILABLE
