"""Service clocks follow every change to a request through these calls.

desk.services calls each one inside its transaction, after its own writes, with the request
locked. They keep the request's RequestClock current (`clocks.py`, rules in `rules.py`) and never
change the request itself.
"""

from . import clocks

RECALCULATING_FIELDS = frozenset({"category_id", "category", "priority"})


def request_created(request):
    """A request was created: its clock starts if a target applies. A request whose channel is in
    desk.models.ANSWERED_AT_INTAKE (call, walk-in) has its first response met at creation."""
    clocks.start(request)


def request_changed(request, *, previous):
    """Status, priority or category of `request` changed. `previous` maps each changed field
    name ("status", "priority", "category_id") to its old value. A category or priority change
    takes the target that applies now (met clocks stay); a status change pauses, resumes, stops
    or reopens the resolution clock."""
    if RECALCULATING_FIELDS.intersection(previous):
        clocks.recalculate(request)
    elif "status" in previous:
        clocks.status_changed(request)


def entry_added(request, entry):
    """An entry was added. A staff reply, or a logged contact whose outcome is in
    desk.models.REACHED_OUTCOMES, meets the first response; the request's opening entry does
    not."""
    clocks.entry_added(request, entry)
