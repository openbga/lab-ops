"""Standard-library logging: one console stream, every record tagged with the request ID."""

import hashlib


def source_sha256(path, loader):
    """SHA-256 of a configuration module's source as its own loader reads it while the module
    runs, so it describes exactly the configuration this process loaded; "" when the source
    cannot be read. A module loaded from bytecode is described by the source beside it.
    core.security_settings compares it with this release's."""
    try:
        source = path if path.endswith(".py") else path[:-1] if path.endswith(".pyc") else ""
        return hashlib.sha256(loader.get_data(source)).hexdigest() if source else ""
    except Exception:  # an unreadable source is not this release's
        return ""


LOGGING_SHA256 = source_sha256(__file__, __loader__)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "core.request_logging.RequestIdFilter"}},
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s "
            "render_request_id=%(render_request_id)s %(message)s"
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["request_id"],
            "formatter": "standard",
        },
        "discard": {"class": "logging.NullHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    # Replace Django's default handlers (including mail_admins) so records reach the root only.
    "loggers": {
        "django": {"handlers": [], "level": "INFO"},
        # runserver's own request line logs the raw path and query; the middleware replaces it.
        "django.server": {"handlers": ["discard"], "level": "INFO", "propagate": False},
    },
}
