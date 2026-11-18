import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from clients.models import Client

PASSWORD = "test-password-729!"


@pytest.fixture
def staff(db):
    def make(name, role):
        user = User.objects.create_user(name, f"{name}@example.invalid", PASSWORD)
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    return make


@pytest.fixture
def client_org(db):
    """A synthetic client organization."""
    return Client.objects.create(
        name="Example Clinic", account_number="EXCLIN", city="Exampleville"
    )


@pytest.fixture
def portal_user(db):
    """A client portal account of the given organization."""

    def make(client, name="portal"):
        return User.objects.create_user(name, f"{name}@example.invalid", PASSWORD, client=client)

    return make
