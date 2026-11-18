"""Request category setup (T1, T2), mounted at /setup/categories/. Admin only."""

from django.urls import path

from . import views

app_name = "taxonomy"
urlpatterns = [
    path("", views.category_list, name="category-list"),
    path("new/", views.category_create, name="category-create"),
    path("groups/new/", views.group_create, name="group-create"),
    path("presets/", views.preset_load, name="preset-load"),
    path("<int:pk>/edit/", views.category_edit, name="category-edit"),
    path("<int:pk>/move/<str:direction>/", views.category_move, name="category-move"),
]
