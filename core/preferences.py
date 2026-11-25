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
import logging
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import access
from core.changelog import valid_marker
from core.grids import PAGE_SIZES
from core.ids import whole_number
from core.models import UserPreference

MAX_PREFERENCES = 300  # per user: grids and pages are a bounded set
MAX_BODY = 8_000  # bytes of one request
GRID_KEY = re.compile(r"^grid:[a-z0-9_.-]{1,80}:v\d{1,3}$")
logger = logging.getLogger(__name__)
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
        size = value.get("per_page")
        if type(size) is int and size in PAGE_SIZES:  # not 50.0: the address takes whole numbers
            cleaned["per_page"] = size
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
# written outside the limit and does not take it either. The second key is the account's id
# modulo 2**31 - 1, as for the access gate: accounts whose ids differ by that share it, which only
# makes their additions take turns. Counting holding it needs READ COMMITTED (Django's default),
# where the count sees what the previous holder committed; an addition in a transaction with an
# older snapshot (REPEATABLE READ or stricter) is refused instead. Writers of an earlier release
# take no such lock: while a deploy replaces processes one by one, an earlier process's addition
# can still take the last place beside this one's, and add a row this one then finds.
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


def _same_json(stored, value):
    """Whether `stored` and `value` are written as the same JSON (Python takes 50.0 == 50)."""
    return json.dumps(stored, sort_keys=True) == json.dumps(value, sort_keys=True)


def _writable_json(value):
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        return False
    return True


def account_removed(error, user_id):
    """True when `error` (an IntegrityError) is a preference row of `user_id` losing its account:
    a foreign-key failure of the preferences table while that account no longer exists. Any other
    failure, also of another table's reference, is not."""
    cause = error.__cause__
    return (
        getattr(cause, "sqlstate", None) == "23503"
        and getattr(getattr(cause, "diag", None), "table_name", None)
        == UserPreference._meta.db_table
        and not get_user_model().objects.filter(pk=user_id).exists()
    )


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


# Transaction isolation levels at which a count or a locked read sees what the previous holder of
# the additions lock committed. PostgreSQL runs READ UNCOMMITTED as READ COMMITTED.
WRITABLE_ISOLATION = ("read committed", "read uncommitted")


def _writable():
    """Inside the write's own transaction: refuse it at an isolation stricter than READ
    COMMITTED (Django's default), where a count or a locked read comes from a snapshot older than
    the lock, and a row changed since fails the statement. Raises ValueError, which every caller
    treats as "not saved"."""
    if not connection.in_atomic_block:
        raise RuntimeError("Check the isolation inside the write's transaction.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('transaction_isolation')")
        if cursor.fetchone()[0] not in WRITABLE_ISOLATION:
            raise ValueError("Preferences can be saved only at READ COMMITTED.")


def _add(user_id, key, value):
    """Add `key` with `value` for `user_id`, unless they have MAX_PREFERENCES already (ValueError).
    The caller holds the additions lock and found no row for `key` holding it. False when a row
    for `key` exists all the same, added meanwhile by a writer that takes no lock (a process of
    an earlier release, during a deploy): the caller reads it again."""
    counted = UserPreference.objects.filter(user_id=user_id).aggregate(
        total=Count("pk"), this=Count("pk", filter=Q(key=key))
    )  # one statement, so both describe the same moment
    if counted["total"] >= MAX_PREFERENCES:
        if counted["this"]:
            return False
        raise ValueError("Too many saved preferences.")
    try:
        with transaction.atomic():
            UserPreference.objects.create(user_id=user_id, key=key, value=value)
    except IntegrityError as error:
        diag = getattr(error.__cause__, "diag", None)
        if getattr(diag, "constraint_name", None) != "unique_user_preference":
            raise
        return False
    return True


# How often a save starts again when the row it found, or failed to add, changes under it.
ATTEMPTS = 3


def save(request, key, value):
    """Store (or, for None, remove) one preference of the signed-in user. Raises ValueError.

    Compared with what is stored, not with what this request read: another tab may have changed
    or removed it since."""
    prefs = for_request(request)
    user_id = request.user.pk
    if value is not None and clean(key, value) != value:
        raise ValueError(f"Not a valid value for {key!r}.")
    with transaction.atomic():
        _writable()
        if value is None:
            UserPreference.objects.filter(user_id=user_id, key=key).delete()
        elif not _replace(user_id, key, value):
            _hold_additions(user_id)
            # Again holding the lock: a row for this key added meanwhile (another tab) is
            # replaced, not added a second time; so is one an earlier release added unlocked,
            # and if that one is removed before it can be replaced, this adds it again.
            for _attempt in range(ATTEMPTS):
                if _replace(user_id, key, value) or _add(user_id, key, value):
                    break
            else:
                raise ValueError("The preference changed while saving; try again.")
    if value is None:
        prefs.pop(key, None)
    else:
        prefs[key] = value


def save_combined(request, key, combine):
    """Store `combine(stored)` as the signed-in user's `key`, where `stored` is its valid value
    as stored when this saves (None when there is none), not as this request read it: another tab
    may have saved one meanwhile. The row is held from reading to writing (the additions lock
    while there is none). Raises ValueError."""
    prefs = for_request(request)
    user_id = request.user.pk
    rows = UserPreference.objects.select_for_update(no_key=True).filter(user_id=user_id, key=key)

    def combined(row):
        value = combine(None if row is None else clean(key, row.value))
        if clean(key, value) != value:
            raise ValueError(f"Not a valid value for {key!r}.")
        return value

    with transaction.atomic():
        _writable()
        row = rows.first()
        if row is None:
            _hold_additions(user_id)
        for _attempt in range(ATTEMPTS):
            row = row or rows.first()  # again holding the lock: one added meanwhile is combined
            value = combined(row)
            if row is not None:  # held: nobody can change or remove it before this commits
                if not _same_json(row.value, value):  # 50.0 is replaced by 50, not kept
                    rows.update(value=value, updated_at=timezone.now())
                break
            if _add(user_id, key, value):
                break
        else:
            raise ValueError("The preference changed while saving; try again.")
    prefs[key] = value


def save_if_absent(request, key, value):
    """Store `value` as the signed-in user's `key` only while they have none stored, and keep a
    value stored meanwhile (by another tab, or an explicit choice). A stored value that can no
    longer be read counts as none and is replaced, whenever it is found. Raises ValueError, also
    when the row keeps changing under it."""
    if clean(key, value) != value:
        raise ValueError(f"Not a valid value for {key!r}.")
    prefs = for_request(request)
    user_id = request.user.pk
    stored = UserPreference.objects.filter(user_id=user_id, key=key)
    with transaction.atomic():
        _writable()
        locked = False
        for _attempt in range(2 * ATTEMPTS):  # reading, locking, adding and repairing
            row = stored.first()
            if row is not None:
                if (kept := clean(key, row.value)) is not None:
                    break  # a readable one, from another tab or an explicit choice, stays
                # Unreadable: replaced, unless it was replaced meanwhile (then read it again).
                # A number JSON cannot write back (out of range) cannot be compared: left as is,
                # it counts as none.
                if not _writable_json(row.value):
                    raise ValueError(f"The stored {key!r} cannot be compared.")
                if stored.filter(pk=row.pk, value=row.value).update(
                    value=value, updated_at=timezone.now()
                ):
                    kept = value  # held by this update until the transaction ends
                    break
            elif not locked:
                _hold_additions(user_id)  # then read again holding it
                locked = True
            elif _add(user_id, key, value):
                kept = value
                break
        else:
            raise ValueError("The preference changed while saving; try again.")
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


def drop_unchosen(request):
    """Leave out of request.GET an empty page size or sort, which a list's filter form sends for a
    setting the person did not choose: it is no choice."""
    blank = [
        name
        for name in GridPreferenceMiddleware.PARAMS
        if name in request.GET and not request.GET[name]
    ]
    if blank:
        params = request.GET.copy()
        for name in blank:
            del params[name]
        request.GET = params


def _add_saved(request, stored):
    """Add to request.GET the page size and sort in `stored` that its address leaves out (after
    drop_unchosen), and note their names: links may leave them out (core.grids.chosen_query)."""
    drop_unchosen(request)
    missing = {
        name: stored[name]
        for name in GridPreferenceMiddleware.PARAMS
        if name in stored and name not in request.GET
    }
    if missing:
        params = request.GET.copy()
        for name, value in missing.items():
            params[name] = str(value)
        request.GET = params
        request.vr_saved_params = set(missing)


def apply_saved_grid(request):
    """For a list that also takes a form (the staff queue's changes to several tickets): add the
    page size and sort saved for it to the form's request, as GridPreferenceMiddleware does for
    each GET of the list, so the list drawn again when the change is refused matches a GET of the
    address the form posted to. The preferences are read anew, after the change, which can have
    waited while another tab saved them. Nothing is saved."""
    request._vr_preferences = None  # the reading for_request keeps for the request
    _add_saved(request, for_request(request).get(f"page:{request.resolver_match.view_name}", {}))


class GridPreferenceMiddleware:
    """Remember each user's page size and sort per list, and apply them when the URL has none.

    Lists follow the shared grid contract (core.grids): `per_page` and `sort` in the query string.
    Without one in the URL, the saved value is added to the request, so the view, its pagination
    and its links all see it; the view still validates both. Once the response is ready, a value
    the URL gave is saved for this list if the list applied it with valid filters
    (core.grids.shown) and the response succeeded: a page that is not a list (a lookup), a sort
    the list does not offer (an old link), filters the list refused, or an error saves nothing.
    At the limit of saved preferences nothing is saved and the page still follows its address.
    """

    PARAMS = ("per_page", "sort")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code < 400:
            if access.carries(response):
                # A page recorded as it is sent (the audit review workspace): saved only once
                # its record is written, so a page withheld for want of it saves nothing.
                access.after_written(response, lambda: self.remember_sent(request))
            else:
                self.remember(request)
        return response

    def remember_sent(self, request):
        """remember, for a page whose record is already written: the page is sent whatever
        happens here, so a setting that cannot be saved is left unsaved, never a failed answer."""
        try:
            self.remember(request)
        except DatabaseError:
            logger.warning("preference.not_saved reason=database_error", exc_info=True)

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
        if given:
            request._vr_grid_given = (user.pk, key, given)
        _add_saved(request, stored)
        return None

    def remember(self, request):
        given = getattr(request, "_vr_grid_given", None)
        grid = getattr(request, "vr_grid", None)
        user = getattr(request, "user", None)
        if given is None or grid is None or user is None or user.pk != given[0]:
            return
        _user_id, key, values = given
        showed = {"per_page": grid.per_page, "sort": grid.sort}
        accepted = {name: value for name, value in values.items() if showed[name] == value}
        if not accepted:
            return
        try:
            save_combined(request, key, lambda stored: {**(stored or {}), **accepted})
        except ValueError:
            pass  # too many saved preferences: the page followed its address, nothing is saved
        except IntegrityError as error:
            if getattr(error.__cause__, "sqlstate", None) != "23503":
                raise
            # The account was removed while the page was drawn: there is nobody to save it for.
