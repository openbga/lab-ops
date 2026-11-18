from django.urls import path

from desk import portal_views

from . import portal

app_name = "portal"
urlpatterns = [
    path("", portal.home, name="home"),
    path("requests/", portal_views.request_list, name="request-list"),
    path("requests/new/", portal_views.request_create, name="request-create"),
    path("requests/<str:number>/", portal_views.request_detail, name="request-detail"),
    path("requests/<str:number>/reply/", portal_views.request_reply, name="request-reply"),
]
