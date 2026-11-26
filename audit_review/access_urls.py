"""The Access review section (audit_review.access_report), mounted at access-review/
(config/urls.py)."""

from django.urls import path

from . import access_views

app_name = "access_review"

urlpatterns = [
    path("accounts/", access_views.accounts_page, name="accounts"),
    path("accounts/export/", access_views.accounts_export, name="accounts-export"),
    path("unused/", access_views.unused_page, name="unused"),
    path("unused/export/", access_views.unused_export, name="unused-export"),
    path("unused/period/", access_views.unused_period, name="unused-period"),
]
