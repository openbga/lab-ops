"""Staff pages for a request's form values (F4), mounted at /request-forms/."""

from django.urls import path

from . import request_views

app_name = "request_details"
urlpatterns = [
    path("<str:number>/edit/", request_views.edit_values, name="edit"),
    path("values/<int:pk>/reveal/", request_views.reveal_value, name="reveal"),
]
