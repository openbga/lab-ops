"""Fixtures for the scheduled jobs and outbox tests. The desk's fixtures (staff of each role,
a team, categories, organizations, portal accounts) are reused. All data is synthetic."""

import pytest

from core.models import Lab
from desk.tests.conftest import (  # noqa: F401  (fixtures reused by the tests here)
    accessioning,
    agent,
    category,
    clinic,
    clinic_contact,
    clinic_portal,
    group,
    join,
    make_user,
    plain_static_storage,
    privacy_category,
    quality,
)

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
DUMMY = "django.core.mail.backends.dummy.EmailBackend"


@pytest.fixture
def lab(db):
    return Lab.objects.update_or_create(pk=1, defaults={"name": "Example Reference Laboratory"})[0]


@pytest.fixture
def locmem(settings, lab):
    """Email on: Django's in-memory backend, and a public address for links."""
    settings.EMAIL_BACKEND = LOCMEM
    settings.PUBLIC_URL = "https://lab-ops.example.invalid"
