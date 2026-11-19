"""The identity of the configuration as it loaded (core.security_settings).

config/settings.py records the SHA-256 of its own source and of the logging configuration's
source while it loads, through each module's own loader, so the Security settings page can tell
whether the configuration this process runs is this release's without reading anything later.
"""

import hashlib


def source_sha256(path, loader):
    """SHA-256 of a module's source at `path` as `loader` reads it, using the .py beside a .pyc;
    "" when there is no path, no loader or no readable source."""
    try:
        source = path if path.endswith(".py") else path[:-1] if path.endswith(".pyc") else ""
        return hashlib.sha256(loader.get_data(source)).hexdigest() if source else ""
    except Exception:  # an unreadable source is not this release's
        return ""


def module_sha256(module):
    """source_sha256 for a loaded module, tolerating a module without a file or a loader."""
    return source_sha256(
        getattr(module, "__file__", None) or "", getattr(module, "__loader__", None)
    )
