"""Demonstration seed for requests (core.seeding): the story in desk/story.py, told once through
the audited services. Categories come from the taxonomy presets, teams from teams.seed, systems
from links.seed (all setups run before any records)."""

from .story import tell


def seed_setup(ctx):
    """Nothing to configure: categories and teams come from their own apps."""


def seed_records(ctx):
    tell()
