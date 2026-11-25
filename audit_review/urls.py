"""The audit review workspace, mounted at audit-review/ (config/urls.py)."""

from django.urls import path

from . import review_views, views
from .models import Source

app_name = "audit_review"

urlpatterns = [
    path("changes/", views.workspace, {"source": Source.CHANGES}, name="changes"),
    path("access/", views.workspace, {"source": Source.ACCESS}, name="access"),
    path("sign-ins/", views.workspace, {"source": Source.SIGN_INS}, name="sign-ins"),
    path("people/", views.people_page, name="people"),
    path("uses/", views.workspace, {"source": Source.USES}, name="uses"),
    path("changes/export/", views.export, {"source": Source.CHANGES}, name="changes-export"),
    path("access/export/", views.export, {"source": Source.ACCESS}, name="access-export"),
    path("sign-ins/export/", views.export, {"source": Source.SIGN_INS}, name="sign-ins-export"),
    path("people/export/", views.people_export, name="people-export"),
    path("uses/export/", views.export, {"source": Source.USES}, name="uses-export"),
    # Review records (audit_review.reviews)
    path("reviews/", review_views.reviews_page, name="reviews"),
    path("reviews/export/", review_views.reviews_export, name="reviews-export"),
    path("reviews/new/", review_views.review_create, name="review-create"),
    path("reviews/interval/", review_views.review_interval, name="review-interval"),
    path("reviews/<int:pk>/", review_views.review_page, name="review"),
    path("reviews/<int:pk>/note/", review_views.review_note, name="review-note"),
]
