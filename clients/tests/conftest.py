import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from clients.models import Client, ClientContact

PASSWORD = "test-password-729!"
READERS = ("admin", "supervisor", "client_services", "account_manager", "technical")
MANAGERS = ("admin", "account_manager", "client_services")
READ_ONLY = ("supervisor", "technical")


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def make_user(db):
    def make(name, *roles, **extra):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, **extra)
        for role in roles:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    return make


@pytest.fixture
def clinic(db):
    return Client.objects.create(
        name="Example Clinic",
        account_number="EXCLIN",
        email="frontdesk@clinic.example.invalid",
        phone="555-0100",
        city="Exampleville",
        state="NY",
        notes="Internal: pays late.",
    )


@pytest.fixture
def water(db):
    """A second organization, whose data must never reach the clinic's portal account."""
    return Client.objects.create(
        name="Example Water Authority", account_number="EXWATER", city="Sampletown", state="PA"
    )


@pytest.fixture
def pat(clinic):
    return ClientContact.objects.create(
        client=clinic, name="Pat Example", role="Billing", email="pat@clinic.example.invalid"
    )
