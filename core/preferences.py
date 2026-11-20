"""Per-user presentation preferences, stored on the server (lab workstations are shared).

Keys and the values they accept:
  grid:<grid>:v<version>  {"order": [column id, …], "hidden": [column id, …]}, static/js/grid.js
  page:<view name>        {"per_page": 25 | 50 | 100, "sort": "<-column>"}, the middleware below
  nav.collapsed           true | false, static/js/sidebar.js
  whats_new.seen          {"date": "YYYY-MM-DD", "ids": [entry id, …]}, set by the What's new
                          page (core.views.whats_new) for its unread indicator (core.changelog)
  notifications.email     true | false, set by the Email notices page (desk.notification_views);
                          false turns a staff member's ticket notices off (desk.notifications)

Every value is checked when written and again when read: a value that no longer fits (an old
format, a column that was removed) is ignored and the page falls back to its defaults.
Presentation only: a preference never widens what a user may see.

Each request reads the stored values once (for_request); nothing is kept in the session, so a
change made in another tab, browser or workstation shows from the next page. A person has at
most MAX_PREFERENCES; adding one counts and inserts holding a lock of that person's additions.
"""

import json
import re

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.changelog import valid_marker
from core.grids import PAGE_SIZES
from core.ids import whole_number
from core.models import UserPreference

MAX_PREFERENCES = 300  # per user: grids and pages are a bounded set
MAX_BODY = 8_000  # bytes of one request
GRID_KEY = re.compile(r"^grid:[a-z0-9_.-]{1,80}:v\d{1,3}$")
PAGE_KEY = re.compile(r"^page:[a-z0-9_:-]{1,100}$")
COLUMN_ID = re.compile(r"^[a-z0-9_-]{1,40}$")
SORT = re.compile(r"^-?[a-z0-9_]{1,40}$")
WHATS_NEW_KEY = "whats_new.seen"  # written only by the What's new page, never by /preferences/
CLIENT_KEYS = re.compile(r"^(grid:|nav\.)")  # what the page hands to its scripts


def _columns(value):
    return (
        isinstance(value, list)
        and len(value) <= 60
        and all(isinstance(item, str) and COLUMN_ID.match(item) for item in value)
    )


def clean(key, value):
    """The value if it is valid for the key, else None."""
    if GRID_KEY.match(key):
        if isinstance(value, dict) and set(value) == {"order", "hidden"}:
            if _columns(value["order"]) and _columns(value["hidden"]):
                return value
        return None
    if PAGE_KEY.match(key):
        if not isinstance(value, dict) or not set(value) <= {"per_page", "sort"}:
            return None
        cleaned = {}
        if value.get("per_page") in PAGE_SIZES and not isinstance(value["per_page"], bool):
            cleaned["per_page"] = value["per_page"]
        if isinstance(value.get("sort"), str) and SORT.match(value["sort"]):
            cleaned["sort"] = value["sort"]
        return cleaned or None
    if key in ("nav.collapsed", "notifications.email"):
        return value if isinstance(value, bool) else None
    if key == WHATS_NEW_KEY:
        return value if valid_marker(value) else None
    return None


# The advisory lock of one person's additions to their preferences (PostgreSQL's two-key form; the
# first key is this lock's own, as accounts.acting.ACCESS_GATE is the access gates'). An addition
# counts the person's preferences and inserts its row holding it, so two additions at the same
# moment cannot both take the last place under MAX_PREFERENCES. Changing or removing a stored
# value adds no row and does not take it. The email notice opt-out (desk.notification_views) is
# written outside the limit and does not take it either.
PREFERENCE_ADDITIONS = 19557


def for_request(request):
    """All valid preferences of the signed-in user, read from the database once per request.

    Not kept in the session: two tabs of one sign-in can answer at the same moment, and each
    saves its whole session as it ends, so a copy kept there could put back what the other had
    just changed (an older What's new mark, a column order). Because they are read for each
    request, a change made in another tab, browser or workstation shows from the next page.
    """
    user = getattr(request, "user", None)
    pk = user.pk if user is not None and user.is_authenticated else None
    cached = getattr(request, "_vr_preferences", None)
    if cached is not None and cached[0] == pk:
        return cached[1]
    prefs = {}
    if pk is not None:
        for key, value in UserPreference.objects.filter(user_id=pk).values_list("key", "value"):
            if (valid := clean(key, value)) is not None:
                prefs[key] = valid
    request._vr_preferences = (pk, prefs)
    return prefs


def _hold_additions(user_id):
    """Take `user_id`'s additions lock until the transaction ends (see PREFERENCE_ADDITIONS)."""
    from accounts.acting import INT4

    if not connection.in_atomic_block:
        raise RuntimeError("The preference additions lock is held for a transaction; open one.")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", [PREFERENCE_ADDITIONS, user_id % INT4]
        )


def _replace(user_id, key, value):
    """Whether `user_id` has `key` stored; if so its value is now `value`."""
    return bool(
        UserPreference.objects.filter(user_id=user_id, key=key).update(
            value=value, updated_at=timezone.now()
        )
    )


def _add(user_id, key, value):
    """A new row for `key`, unless `user_id` has MAX_PREFERENCES already. The caller holds the
    additions lock and has found no row for `key` holding it. Raises ValueError."""
    if UserPreference.objects.filter(user_id=user_id).count() >= MAX_PREFERENCES:
        raise ValueError("Too many saved preferences.")
    return UserPreference.objects.create(user_id=user_id, key=key, value=value)


def save(request, key, value):
    """Store (or, for None, remove) one preference of the signed-in user. Raises ValueError."""
    prefs = for_request(request)
    user_id = request.user.pk
    if value is None:
        UserPreference.objects.filter(user_id=user_id, key=key).delete()
        prefs.pop(key, None)
        return
    if clean(key, value) != value:
        raise ValueError(f"Not a valid value for {key!r}.")
    if prefs.get(key) == value:
        return
    with transaction.atomic():
        if not _replace(user_id, key, value):
            _hold_additions(user_id)
            # Again holding the lock: a row for this key added meanwhile (another tab) is
            # replaced, not added a second time.
            if not _replace(user_id, key, value):
                _add(user_id, key, value)
    prefs[key] = value


def save_if_absent(request, key, value):
    """Store `value` as the signed-in user's `key` only while they have none, and keep a value
    stored meanwhile (by another tab, or an explicit choice). Raises ValueError."""
    if clean(key, value) != value:
        raise ValueError(f"Not a valid value for {key!r}.")
    prefs = for_request(request)
    if key in prefs:
        return
    user_id = request.user.pk
    stored = UserPreference.objects.filter(user_id=user_id, key=key)
    with transaction.atomic():
        row = stored.first()
        if row is None:
            _hold_additions(user_id)
            # Again holding the lock: one added meanwhile is kept.
            row = stored.first() or _add(user_id, key, value)
        if clean(key, row.value) is None:
            # Unreadable, so as good as none: replaced, unless it was replaced meanwhile.
            stored.filter(pk=row.pk, value=row.value).update(value=value, updated_at=timezone.now())
            row = stored.first()
    if row is not None and (kept := clean(key, row.value)) is not None:
        prefs[key] = kept


@login_required
@require_POST
def preference(request):
    """POST {"key": …, "value": …} (value null removes it): the signed-in user's own setting."""
    if len(request.body) > MAX_BODY:
        return HttpResponseBadRequest("Too large.")
    try:
        data = json.loads(request.body)
        key, value = data["key"], data["value"]
        if not isinstance(key, str) or not (GRID_KEY.match(key) or key == "nav.collapsed"):
            raise ValueError("Unknown preference.")
        save(request, key, value)
    except (ValueError, KeyError, TypeError) as error:
        return HttpResponseBadRequest(str(error))
    return HttpResponse(status=204)


class GridPreferenceMiddleware:
    """Remember each user's page size and sort per list, and apply them when the URL has none.

    Lists follow the shared grid contract (core.grids): `per_page` and `sort` in the query string.
    A valid value in the URL is saved for this page; without one, the saved value is added to the
    request, so the view, its pagination and its links all see it. The view still validates both.
    """

    PARAMS = ("per_page", "sort")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        match = request.resolver_match
        if request.method != "GET" or user is None or not user.is_authenticated or match is None:
            return None
        key = f"page:{match.view_name}"
        if not PAGE_KEY.match(key):
            return None
        stored = for_request(request).get(key, {})
        given = {}
        per_page = request.GET.get("per_page", "")
        if whole_number(per_page) in PAGE_SIZES:
            given["per_page"] = int(request.GET["per_page"])
        if SORT.match(request.GET.get("sort", "")):
            given["sort"] = request.GET["sort"]
        if given and {**stored, **given} != stored:
            try:
                save(request, key, {**stored, **given})
            except ValueError:
                pass  # too many saved preferences: this page still follows its address
        missing = {
            name: stored[name] for name in self.PARAMS if name in stored and name not in request.GET
        }
        if missing:
            params = request.GET.copy()
            for name, value in missing.items():
                params[name] = str(value)
            request.GET = params
            request.vr_saved_params = set(missing)  # links may leave these out (core.grids)
        return None
