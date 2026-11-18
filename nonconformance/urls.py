"""Pages of the nonconformance module, mounted at quality/ (config/urls.py). While the module is off
every address is Not found, for every method, before the CSRF check or any sign-in and role check
(core.modules.ModuleAddressMiddleware), and each view checks the switch again
(core.modules.module_view)."""

from django.urls import path as django_path

from core.modules import module_view

from . import summary_views, views

app_name = "nonconformance"
RECORD = "nonconformances/<int:pk>/"


def path(route, view, name):
    """A module address: 404 for everyone while the module is off (also when it is switched off
    between resolving the address and running the view), and never stored by the browser or a
    proxy, refusals included (record text and typed input)."""
    return django_path(route, module_view("nonconformance")(view), name=name)


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
