"""The keys of the shared cache (CACHES in config/settings.py), stored as keyed digests.

allauth keeps its sign-in limits in the cache under keys built from the name typed at sign-in or
the network address. The cache is a database table, so each key is replaced by an HMAC of it,
keyed with the deployment's SECRET_KEY: the table holds no name or address, and nobody without
the key can tell whose attempts it counts. Changing SECRET_KEY starts every count afresh.
"""

from django.utils.crypto import salted_hmac

_SALT = "openbga.lab_ops.cache_key"


def keyed_digest(key, key_prefix, version):
    """Django's KEY_FUNCTION: the cache key as an HMAC-SHA256 of the prefix, version and key."""
    return salted_hmac(_SALT, f"{key_prefix}:{version}:{key}", algorithm="sha256").hexdigest()
