"""File downloads and withdrawal, mounted at /attachments/ (config/urls.py)."""

from django.urls import path

from . import views

app_name = "attachments"
urlpatterns = [
    path("<int:pk>/", views.download, name="download"),
    path("<int:pk>/withdraw/", views.withdraw, name="withdraw"),
]
