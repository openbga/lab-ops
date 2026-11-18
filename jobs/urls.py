"""The Scheduled jobs page and its settings form, at /lab/scheduled-jobs/. Admin only."""

from django.urls import path

from . import views

app_name = "jobs"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("settings/", views.settings_edit, name="settings-edit"),
]
