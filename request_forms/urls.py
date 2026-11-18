"""Request form setup (F1, F3, F7), mounted at /setup/request-forms/. Admin only."""

from django.urls import path

from . import views

app_name = "request_forms"
urlpatterns = [
    path("", views.template_list, name="template-list"),
    path("new/", views.template_create, name="template-create"),
    path("presets/", views.preset_load, name="preset-load"),
    path("<int:pk>/", views.template_detail, name="template-detail"),
    path("<int:pk>/edit/", views.template_edit, name="template-edit"),
    path("<int:pk>/preview/", views.template_preview, name="template-preview"),
    path("<int:pk>/copy/", views.template_copy, name="template-copy"),
    path("<int:pk>/move/<str:direction>/", views.template_move, name="template-move"),
    path("<int:pk>/fields/new/", views.field_create, name="field-create"),
    path("fields/<int:pk>/edit/", views.field_edit, name="field-edit"),
    path("fields/<int:pk>/move/<str:direction>/", views.field_move, name="field-move"),
    path("catalogs/", views.catalog_list, name="catalog-list"),
    path("catalogs/new/", views.catalog_create, name="catalog-create"),
    path("catalogs/<int:pk>/", views.catalog_detail, name="catalog-detail"),
    path("catalogs/<int:pk>/edit/", views.catalog_edit, name="catalog-edit"),
    path("catalogs/<int:pk>/items/new/", views.item_create, name="item-create"),
    path("catalogs/items/<int:pk>/edit/", views.item_edit, name="item-edit"),
    path("catalogs/items/<int:pk>/move/<str:direction>/", views.item_move, name="item-move"),
]
