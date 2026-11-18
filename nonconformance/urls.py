"""Pages of the nonconformance module, mounted at quality/ (config/urls.py). Every view answers
404 while the module is off (nonconformance.selectors)."""

from django.urls import path

from . import summary_views, views

app_name = "nonconformance"
RECORD = "nonconformances/<int:pk>/"
urlpatterns = [
    path("nonconformances/", views.nonconformance_list, name="list"),
    path("nonconformances/new/", views.nonconformance_create, name="create"),
    path("nonconformances/from-ticket/<int:pk>/", views.from_ticket, name="from-ticket"),
    path(RECORD, views.nonconformance_detail, name="detail"),
    path(RECORD + "edit/", views.nonconformance_edit, name="edit"),
    path(RECORD + "owner/", views.nonconformance_owner, name="owner"),
    path(RECORD + "status/", views.nonconformance_status, name="status"),
    path(RECORD + "investigation/", views.nonconformance_investigation, name="investigation"),
    path(RECORD + "impact/", views.nonconformance_impact, name="impact"),
    path(RECORD + "effectiveness/", views.nonconformance_effectiveness, name="effectiveness"),
    path(RECORD + "close/", views.nonconformance_close, name="close"),
    path(RECORD + "reopen/", views.nonconformance_reopen, name="reopen"),
    path(RECORD + "actions/new/", views.action_add, name="action-add"),
    path(RECORD + "actions/<int:action_pk>/edit/", views.action_edit, name="action-edit"),
    path(RECORD + "actions/<int:action_pk>/done/", views.action_done, name="action-done"),
    path(RECORD + "actions/<int:action_pk>/cancel/", views.action_cancel, name="action-cancel"),
    path(RECORD + "tickets/new/", views.ticket_link, name="ticket-link"),
    path(RECORD + "tickets/<int:link_pk>/remove/", views.ticket_unlink, name="ticket-unlink"),
    path("actions/", views.action_list, name="action-list"),
    path("summary/", summary_views.summary_page, name="summary"),
]
