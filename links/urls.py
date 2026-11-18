"""External systems setup (L1), mounted at /setup/systems/. Admin only. Link pages are in
record_urls.py, mounted at /links/."""

from django.urls import path

from . import views

app_name = "links"
urlpatterns = [
    path("", views.system_list, name="system-list"),
    path("new/", views.system_create, name="system-create"),
    path("<int:pk>/edit/", views.system_edit, name="system-edit"),
]
