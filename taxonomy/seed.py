"""Demonstration seed for request categories (core.seeding): every preset, loaded as an admin
would load them. Loading is idempotent, so a second run adds nothing."""

from accounts.models import User

from .presets import PRESETS
from .services import load_presets


def seed_setup(ctx):
    load_presets(actor=User.objects.get(username="demo_admin"), keys=list(PRESETS))


def seed_records(ctx):
    """Nothing: requests are seeded by the desk."""
