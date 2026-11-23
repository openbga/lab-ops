"""The shared cache (CACHES in config/settings.py): Django's database cache in the table
lab_ops_cache (core migration 0007), where allauth keeps its sign-in limits for every server
process of the deployment.

Three changes to Django's database cache, each for the counts it keeps:

- Keys are stored as keyed digests (keyed_digest). allauth builds its keys from the name typed at
  sign-in or the network address; the table holds an HMAC of each, keyed with the deployment's
  SECRET_KEY, so it holds no name or address and nobody without the key can tell whose attempts
  it counts. Changing SECRET_KEY starts every count afresh.
- Expired entries are removed whenever an entry is written, and a live entry is never removed to
  make room (SharedCache._cull): Django's database cache otherwise keeps expired entries until
  their key is read again and, past MAX_ENTRIES, drops a third of the live ones, counts included.
- An entry lasts at least as long as asked: the table stores whole seconds, and Django's database
  cache drops the fraction, which would end a minute's count up to a second early
  (SharedCache.get_backend_timeout rounds up instead).
"""

import math

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.db import DatabaseCache
from django.db import connections
from django.utils.crypto import salted_hmac

_SALT = "openbga.lab_ops.cache_key"


def keyed_digest(key, key_prefix, version):
    """Django's KEY_FUNCTION: the cache key as an HMAC-SHA256 of the prefix, version and key."""
    return salted_hmac(_SALT, f"{key_prefix}:{version}:{key}", algorithm="sha256").hexdigest()


class SharedCache(DatabaseCache):
    def __init__(self, table, params):
        super().__init__(table, params)
        # Every write first removes the expired entries (_cull runs when the table holds more
        # than this many, so: whenever it holds any).
        self._max_entries = 0

    def get_backend_timeout(self, timeout=DEFAULT_TIMEOUT):
        expiry = super().get_backend_timeout(timeout)
        return None if expiry is None else math.ceil(expiry)

    def _cull(self, db, cursor, now, num):
        """Remove the entries that have expired, never a live one."""
        connection = connections[db]
        table = connection.ops.quote_name(self._table)
        cursor.execute(
            f"DELETE FROM {table} WHERE {connection.ops.quote_name('expires')} < %s",
            [connection.ops.adapt_datetimefield_value(now)],
        )
