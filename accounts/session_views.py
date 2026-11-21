"""The account's own sessions.

The page's questions about its own session (accounts.sessions): how long remains, and Stay
signed in. Both answer JSON to the page's script and never carry anything about the account but
the times. Asking how long remains is not activity; Stay signed in is (the middleware records it
before the view runs).

Where you are signed in (accounts.session_list): every signed-in account, staff or client portal,
sees its own open sessions and signs out the others. A shared demonstration account
(credentials_locked) sees only the session it is using and how many others are open: the others
belong to other visitors.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from . import session_list
from .sessions import sign_in_url

PAGE = "account-sessions"


def _answer(request):
    state = getattr(request, "account_session", None)
    if state is None or not request.user.is_authenticated:
        return JsonResponse({"signed_in": False, "sign_in": sign_in_url(None)})
    return JsonResponse(state.as_json())


@require_GET
def session_status(request):
    return _answer(request)


@require_POST
def session_continue(request):
    return _answer(request)


def _current_id(request):
    state = getattr(request, "account_session", None)
    return state.id if state is not None else None


def _count(number, singular, plural=None):
    return f"{number} {singular if number == 1 else plural or singular + 's'}"


@login_required
@require_GET
def sessions_page(request):
    current = _current_id(request)
    rows, now = session_list.reading(request.user)
    this = next((row for row in rows if row.id == current), None)
    others = [row for row in rows if row.id != current]
    locked = request.user.credentials_locked
    return render(
        request,
        "accounts/sessions.html",
        {
            "page_title": "Where you are signed in",
            "this_session": this,
            # A shared demonstration account's other sessions are other visitors': only counted.
            "other_sessions": [] if locked else others,
            "other_count": len(others),
            "listed_count": (0 if this is None else 1) + (0 if locked else len(others)),
            "locked": locked,
            # The database's clock when the sessions were read, so that the confirmation of Sign
            # out of all other sessions can say how many signed in after the page was opened.
            "drawn_at": now.isoformat(),
        },
    )


def _refused(request, error):
    messages.warning(request, str(error.args[0]) if error.args else str(error))
    return redirect(PAGE)


@login_required
@require_POST
def sign_out_session(request, session_id):
    try:
        ended = session_list.sign_out_other(
            actor=request.user, current=_current_id(request), session_id=session_id
        )
    except PermissionDenied as error:
        return _refused(request, error)
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect(PAGE)
    if ended:
        messages.success(
            request,
            "That session was signed out. Its browser goes to the sign-in page at its next "
            "request.",
        )
    else:
        messages.info(request, session_list.NOT_OPEN)
    return redirect(PAGE)


@login_required
@require_POST
def sign_out_other_sessions(request):
    drawn_at = parse_datetime(request.POST.get("drawn_at", "") or "")
    if drawn_at is not None and timezone.is_naive(drawn_at):
        drawn_at = None
    try:
        ended = session_list.sign_out_others(
            actor=request.user, current=_current_id(request), drawn_at=drawn_at
        )
    except PermissionDenied as error:
        return _refused(request, error)
    if not ended.ids:
        messages.info(request, "No other session was open, so nothing changed.")
        return redirect(PAGE)
    newer = ended.later or 0
    text = f"Signed out of {_count(len(ended.ids), 'other session')}."
    if newer:
        text += (
            f" {_count(newer, 'of them was', 'of them were')} signed in after you opened this page."
        )
    messages.success(request, text)
    return redirect(PAGE)
