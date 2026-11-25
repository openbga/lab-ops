"""The audit review workspace, mounted at audit-review/ (config/urls.py)."""

from django.urls import path

from . import views
from .models import Source

app_name = "audit_review"

urlpatterns = [
    path("changes/", views.workspace, {"source": Source.CHANGES}, name="changes"),
    path("access/", views.workspace, {"source": Source.ACCESS}, name="access"),
    path("sign-ins/", views.workspace, {"source": Source.SIGN_INS}, name="sign-ins"),
    path("uses/", views.workspace, {"source": Source.USES}, name="uses"),
    path("changes/export/", views.export, {"source": Source.CHANGES}, name="changes-export"),
    path("access/export/", views.export, {"source": Source.ACCESS}, name="access-export"),
    path("sign-ins/export/", views.export, {"source": Source.SIGN_INS}, name="sign-ins-export"),
    path("uses/export/", views.export, {"source": Source.USES}, name="uses-export"),
]
