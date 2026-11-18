"""A host address list mixing its own route with the module's patterns in one include, whose 404
page links to that route (round 1 sign-off of 6d17419)."""

from django.http import HttpResponseNotFound
from django.urls import reverse

from .list_mount_urls import mixed_urlpatterns as urlpatterns  # noqa: F401


def handler404(request, exception):
    return HttpResponseNotFound(f"Mixed site page not found; see {reverse('nonconformance:guide')}")


def use_this_urlconf(get_response):
    def handle(request):
        request.urlconf = __name__
        return get_response(request)

    return handle
