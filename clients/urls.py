from django.urls import path

from . import views

app_name = "clients"
urlpatterns = [
    path("", views.client_list, name="client-list"),
    path("new/", views.client_create, name="client-create"),
    path("<int:pk>/", views.client_detail, name="client-detail"),
    path("<int:pk>/edit/", views.client_edit, name="client-edit"),
    path("<int:pk>/timeline/", views.client_timeline, name="client-timeline"),
    path("<int:pk>/contacts/new/", views.contact_edit, name="contact-create"),
    path("<int:pk>/contacts/<int:contact_pk>/", views.contact_edit, name="contact-edit"),
    path("<int:pk>/sites/new/", views.site_edit, name="site-create"),
    path("<int:pk>/sites/<int:site_pk>/", views.site_edit, name="site-edit"),
    path(
        "<int:pk>/portal-accounts/<int:user_pk>/access/",
        views.portal_access_edit,
        name="portal-access-edit",
    ),
]
