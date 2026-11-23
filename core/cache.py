"""The shared cache (CACHES in config/settings.py): Django's database cache in the table
lab_ops_cache (core migration 0007), where allauth keeps its sign-in limits for every server
process of the deployment.

Three changes to Django's database cache, each for the counts it keeps:

- Keys are stored as keyed digests (keyed_digest). allauth builds its keys from the name typed at
  sign-in or the network address; the table holds an HMAC of each, keyed with the deployment's
  SECRET_KEY, so it holds no name or address and nobody without the key can tell whose attempts
  it counts. Changing SECRET_KEY starts every count afresh.
- Expired entries are removed whenever an entry is set, added, touched or deleted (a read that
  finds none expired writes nothing), by the time now, and a live entry is never removed to make
  room (SharedCache._cull): Django's database cache otherwise keeps expired entries until their
  key is read again and, past MAX_ENTRIES, drops a third of the live ones, counts included.
  Removing them first also means an entry that has expired is never taken for a live one by an
  add or a touch.
- An entry lasts at least as long as asked: the table stores whole seconds, and Django's database
  cache drops the fraction, which would end a minute's count up to a second early
  (SharedCache.get_backend_timeout rounds up instead). A timeout of zero or less, given or the
  default, is expired at once.
"""

import math
import time

from django.core.cache.backends import db as database_cache
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.db import DatabaseCache
from django.db import connections, router
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
        if timeout == DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        if timeout is None:
            return None
        if timeout <= 0:
            return time.time() - 1
        return math.ceil(time.time() + timeout)

    def _cull(self, db, cursor, now, num):
        """Remove the entries that have expired by the time now (Django passes `now` without its
        fraction of a second), never a live one."""
        connection = connections[db]
        table = connection.ops.quote_name(self._table)
        cursor.execute(
            f"DELETE FROM {table} WHERE {connection.ops.quote_name('expires')} < %s",
            [connection.ops.adapt_datetimefield_value(database_cache.tz_now())],
        )

    def _base_delete_many(self, keys):
        # Django calls this from every read too, with the expired keys it found (often none): a
        # read that removes nothing writes nothing.
        if keys:
            db = router.db_for_write(self.cache_model_class)
            with connections[db].cursor() as cursor:
                self._cull(db, cursor, None, None)
        return super()._base_delete_many(keys)
