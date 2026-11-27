"""The demonstration seed, app by app (seed_demo runs it; reset_demo runs seed_demo).

Each app in SEED_APPS has a `seed.py` with two functions taking a SeedContext:
`seed_setup(ctx)` (configuration: teams, categories, calendars, systems) and
`seed_records(ctx)` (organizations, requests, links ...). All setups run first, then all records,
each phase in SEED_APPS order, which follows the apps' dependencies. Seeds write through the
apps' audited services, create their records once, and use synthetic data only. A database reset
by reset_demo has lost rows that data migrations created, so a setup recreates what it needs.
"""

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

SEED_APPS = (
    "clients",
    "teams",
    "taxonomy",
    # After taxonomy (its presets need the categories) and clients (its records offer forms to
    # the demonstration organizations); before desk, whose story raises requests with forms.
    "request_forms",
    "desk",
    "service_levels",
    "links",
    # After desk: its records concern the story's complaints (ctx.created["requests"]).
    "nonconformance",
    # After desk, whose story grants the reviewer the capability.
    "audit_review",
)
PHASES = ("seed_setup", "seed_records")


@dataclass(frozen=True)
class SeedContext:
    password: str  # DEMO_PASSWORD, for the portal accounts a seed creates
    stdout: Any = None
    # What a seed created in this run, for a later seed to use by identity rather than look up
    # again by a code someone may have reused: {"clients": {account code: Client}}.
    created: dict = field(default_factory=dict)
    # The seed's day, read once for the run (accounts.grant_ends.later_today): the ends it sets
    # count from it, so a run that crosses midnight gives them one day. None: read when needed.
    day: Any = None


def run_seed(ctx):
    for phase in PHASES:
        for app in SEED_APPS:
            getattr(import_module(f"{app}.seed"), phase)(ctx)
