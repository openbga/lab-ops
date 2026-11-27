"""The Access review section (audit_review.access_report), mounted at access-review/
(config/urls.py)."""

from django.urls import path

from . import access_review_views, access_views, emergency_views

app_name = "access_review"

urlpatterns = [
    path("accounts/", access_views.accounts_page, name="accounts"),
    path("accounts/export/", access_views.accounts_export, name="accounts-export"),
    path("unused/", access_views.unused_page, name="unused"),
    path("unused/export/", access_views.unused_export, name="unused-export"),
    path("unused/period/", access_views.unused_period, name="unused-period"),
    path("reviews/", access_review_views.reviews_page, name="reviews"),
    path("reviews/export/", access_review_views.reviews_export, name="reviews-export"),
    path("reviews/new/", access_review_views.review_create, name="review-create"),
    path("reviews/<int:pk>/", access_review_views.review_page, name="review"),
    path("reviews/<int:pk>/export/", access_review_views.review_export, name="review-export"),
    path("emergency/", emergency_views.uses_page, name="emergency"),
    path("emergency/<int:pk>/", emergency_views.use_page, name="emergency-use"),
]
