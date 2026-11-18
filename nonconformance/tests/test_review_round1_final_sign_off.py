"""An off-module 404 preserves the host site's other named routes."""

import pytest
from django.http import HttpResponse, HttpResponseNotFound
from django.urls import include, path, reverse


def guide(request):
    return HttpResponse("Public operator guide")


# The qualified guide name is distinct from every route in the included project.
guide_page = ([path("", guide, name="index")], "guide_topic")
guide_topics = ([path("quality/", include(guide_page, namespace="nonconformance"))], "guide")
urlpatterns = [
    path("lab/", include("config.urls")),
    path("guide/", include(guide_topics)),
]


def handler404(request, exception):
    guide_url = reverse("guide:nonconformance:index")
    return HttpResponseNotFound(f'See the <a href="{guide_url}">operator guide</a>.')


@pytest.mark.django_db
def test_module_off_keeps_the_independent_guide_link_on_the_ordinary_404(client, settings):
    settings.DEBUG = False
    settings.ROOT_URLCONF = __name__
    client.raise_request_exception = False
    guide_url = reverse("guide:nonconformance:index")
    assert client.get(guide_url, secure=True).status_code == 200
    missing = client.get("/lab/unknown-sign-off/", secure=True)
    assert missing.status_code == 404 and guide_url.encode() in missing.content

    off = client.get(reverse("nonconformance:list"), secure=True)
    assert off.status_code == 404
    assert off.content == missing.content
