"""The account's own sessions.

The page's questions about its own session (accounts.sessions): how long remains, and Stay
signed in. Both answer JSON to the page's script and never carry anything about the account but
the times. Asking how long remains is not activity; Stay signed in is (the middleware records it
before the view runs). Once its session has ended, the page sends what was typed in its forms to
be kept (accounts.kept); that is not activity either, and needs no signed-in session.

Where you are signed in (accounts.session_list): every signed-in account, staff or client portal,
sees its own open sessions and signs out the others. A shared demonstration account
(credentials_locked) sees only the session it is using and how many others are open: the others
belong to other visitors.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from . import kept, session_list
from .sessions import sign_in_url

# The largest body the keep address reads: four copies of kept.MAX_TOTAL characters, each up to
# six bytes as JSON escapes, with room for the tokens.
KEEP_BODY_BYTES = kept.MAX_FORMS * (kept.MAX_TOTAL * 6 + 16_000)

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


# No CSRF check: the browser's session, and the CSRF secret of a new sign-in in another tab, may
# be gone by now. Each form's signed token proves the server drew that form for that account and
# session, which another site cannot read; nothing else is done here.
@csrf_exempt
@sensitive_post_parameters()
@never_cache
@require_POST
def keep_typing(request):
    """Keep what was typed in the page's forms, sent as JSON {"forms": [{"token": ..., "values":
    {name: text}}]} once the page's session has ended. Answers {"kept": n}, the forms a copy is
    held for; then the sign-in page says so."""
    length = request.META.get("CONTENT_LENGTH", "")
    if not (length.isascii() and length.isdigit()) or int(length) > KEEP_BODY_BYTES:
        return JsonResponse({"kept": 0}, status=413)
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"kept": 0}, status=400)
    items = data.get("forms") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return JsonResponse({"kept": 0}, status=400)
    count = sum(
        1
        for item in items[: kept.MAX_FORMS]
        if isinstance(item, dict) and kept.keep_for(request, item.get("token"), item.get("values"))
    )
    if count:
        messages.info(request, kept.KEPT_MESSAGE, fail_silently=True)
    response = JsonResponse({"kept": count})
    kept.remember_browser(request, response)
    return response


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
    try:
        drawn_at = parse_datetime(request.POST.get("drawn_at", "") or "")
    except ValueError:  # well formed but not a date (2026-02-29): the wording only needs it
        drawn_at = None
    if drawn_at is not None and timezone.is_naive(drawn_at):
        drawn_at = None
    try:
        ended = session_list.sign_out_others(
            actor=request.user, current=_current_id(request), drawn_at=drawn_at
        )
    except PermissionDenied as error:
        return _refused(request, error)
    if not ended.ids:
        messages.info(request, "No other session was open, so none was signed out.")
        return redirect(PAGE)
    newer = ended.later or 0
    text = f"Signed out of {_count(len(ended.ids), 'other session')}."
    if newer:
        text += (
            f" {_count(newer, 'of them was', 'of them were')} signed in after you opened this page."
        )
    messages.success(request, text)
    return redirect(PAGE)
