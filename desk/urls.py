from django.urls import path

from . import views

app_name = "desk"
urlpatterns = [
    path("", views.request_list, name="request-list"),
    path("new/", views.request_create, name="request-create"),
    path("new/guidance/", views.category_guidance, name="category-guidance"),
    path("find/", views.request_find, name="request-find"),
    path("bulk/", views.request_bulk, name="request-bulk"),
    path("views/new/", views.view_create, name="view-create"),
    path("views/<int:pk>/", views.view_edit, name="view-edit"),
    path("views/<int:pk>/delete/", views.view_delete, name="view-delete"),
    path("views/<int:pk>/move/", views.view_move, name="view-move"),
    path("<int:pk>/", views.request_detail, name="request-detail"),
    path("<int:pk>/entries/", views.request_entry, name="request-entry"),
    path("<int:pk>/take/", views.request_take, name="request-take"),
    path("<int:pk>/assign/", views.request_assign, name="request-assign"),
    path("<int:pk>/status/", views.request_status, name="request-status"),
    path("<int:pk>/details/", views.request_properties, name="request-properties"),
    path("<int:pk>/complaint/", views.request_complaint_mark, name="complaint-mark"),
    path("<int:pk>/complaint/decision/", views.complaint_decision, name="complaint-decision"),
    path("<int:pk>/complaint/record/", views.complaint_record, name="complaint-record"),
    path("<int:pk>/complaint/review/", views.complaint_review, name="complaint-review"),
    path(
        "<int:pk>/entries/<int:entry_pk>/withdraw/",
        views.entry_withdraw,
        name="entry-withdraw",
    ),
    path("<int:pk>/history/", views.request_history, name="request-history"),
]
