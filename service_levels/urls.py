"""Service targets and business calendars (S1, S2), at /setup/service-levels/. Admin only."""

from django.urls import path

from . import views

app_name = "service_levels"
urlpatterns = [
    path("", views.target_list, name="target-list"),
    path("targets/new/", views.target_create, name="target-create"),
    path("targets/<int:pk>/edit/", views.target_edit, name="target-edit"),
    path("calendars/", views.calendar_list, name="calendar-list"),
    path("calendars/new/", views.calendar_create, name="calendar-create"),
    path("calendars/<int:pk>/", views.calendar_detail, name="calendar-detail"),
    path("calendars/<int:pk>/edit/", views.calendar_edit, name="calendar-edit"),
    path("calendars/<int:pk>/holidays/new/", views.holiday_create, name="holiday-create"),
    path("holidays/<int:pk>/remove/", views.holiday_remove, name="holiday-remove"),
]
