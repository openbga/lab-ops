"""Clock state for request lists and dashboard counts.

States: "met", "missed", "paused", "overdue", "due_soon", "on_track", "none". Computed in SQL by
`states.py`, which mirrors `rules.request_state` (tested against it over a table of cases).
"""

from .rules import STATE_LABELS
from .states import annotate_clock_state, filter_clock_state

CLOCK_STATES = ("met", "missed", "paused", "overdue", "due_soon", "on_track", "none")
CLOCK_STATE_CHOICES = [(state, STATE_LABELS[state]) for state in CLOCK_STATES]

__all__ = ["CLOCK_STATES", "CLOCK_STATE_CHOICES", "annotate_clock_state", "filter_clock_state"]
