from django.urls import path

from . import views

app_name = "teams"
urlpatterns = [
    path("", views.team_list, name="team-list"),
    path("new/", views.team_create, name="team-create"),
    path("<int:pk>/", views.team_detail, name="team-detail"),
    path("<int:pk>/edit/", views.team_edit, name="team-edit"),
    path(
        "<int:pk>/members/<int:membership_pk>/remove/",
        views.member_remove,
        name="member-remove",
    ),
    path("<int:pk>/members/<int:membership_pk>/lead/", views.member_lead, name="member-lead"),
]
