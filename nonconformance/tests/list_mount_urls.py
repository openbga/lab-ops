"""A host address list that mounts the module's pattern list directly, beneath a prefix, for
test_review_round1_closing_fixes (round 1 sign-off of efea433)."""

from django.http import HttpResponseNotFound
from django.urls import include, path

from nonconformance import urls as quality

urlpatterns = [
    path("site/quality/", include((quality.urlpatterns, quality.app_name))),
    path("site/", include("config.urls")),
]


def handler404(request, exception):
    return HttpResponseNotFound("List-mount site page not found")


def use_this_urlconf(get_response):
    def handle(request):
        request.urlconf = __name__
        return get_response(request)

    return handle


def guide(request):
    from django.http import HttpResponse

    return HttpResponse("Synthetic guide")


# A host list that mixes its own route with the module's patterns in one include.
mixed_urlpatterns = [
    path(
        "site/quality/",
        include(([path("guide/", guide, name="guide"), *quality.urlpatterns], quality.app_name)),
    ),
    path("site/", include("config.urls")),
]
