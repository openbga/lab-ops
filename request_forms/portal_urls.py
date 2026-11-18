"""Client portal request forms (F5), mounted at /portal/forms/."""

from django.urls import path

from . import portal_views

app_name = "portal_forms"
urlpatterns = [
    path("", portal_views.choose, name="choose"),
    path("<slug:key>/", portal_views.raise_request, name="raise"),
]
