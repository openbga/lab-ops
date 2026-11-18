"""Staff pages for record links (L2 to L5), mounted at /links/ (config/urls.py).

Links are addressed by UUID and attachments by id, so a person-level key never appears in a URL.
"""

from django.urls import path

from . import views

app_name = "link_records"
urlpatterns = [
    path("find-person/", views.person_search, name="person-search"),
    path("requests/<int:pk>/new/", views.request_link_add, name="request-link-add"),
    path("clients/<int:pk>/new/", views.client_link_add, name="client-link-add"),
    path("attachments/<int:pk>/remove/", views.attachment_remove, name="attachment-remove"),
    path("attachments/<int:pk>/shown/", views.attachment_shown, name="attachment-shown"),
    path("attachments/<int:pk>/reveal/", views.attachment_reveal, name="attachment-reveal"),
    path("<uuid:pk>/", views.link_detail, name="link-detail"),
    path("<uuid:pk>/edit/", views.link_edit, name="link-edit"),
]
