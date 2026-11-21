"""The page's questions about its own session (accounts.sessions): how long remains, and Stay
signed in. Both answer JSON to the page's script and never carry anything about the account but
the times. Asking how long remains is not activity; Stay signed in is (the middleware records it
before the view runs)."""

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .sessions import sign_in_url


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
