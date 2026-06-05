"""Tenant-side URLs for the RMA app. Mounted at /rma/ in core/urls.py."""
from django.urls import path
from . import views

urlpatterns = [
    # JSON API endpoints (used by the in-dashboard RMA section)
    path("api/list/",                       views.api_list,           name="rma_api_list"),
    path("api/analytics/",                  views.api_analytics,      name="rma_api_analytics"),
    path("api/settings/",                   views.api_settings_get,   name="rma_api_settings"),
    path("api/triage/",                     views.api_triage_list,    name="rma_api_triage"),
    path("api/triage/<int:email_id>/start/",   views.api_triage_start,    name="rma_api_triage_start"),
    path("api/triage/<int:email_id>/dismiss/", views.api_triage_dismiss,  name="rma_api_triage_dismiss"),
    path("api/<int:pk>/",                   views.api_detail,         name="rma_api_detail"),

    # POST action endpoints (already JSON)
    path("create/",               views.rma_create_manual,   name="rma_create_manual"),
    path("<int:pk>/approve/",     views.rma_approve,         name="rma_approve"),
    path("<int:pk>/reject/",      views.rma_reject,          name="rma_reject"),
    path("<int:pk>/received/",    views.rma_mark_received,   name="rma_mark_received"),
    path("<int:pk>/in-transit/",  views.rma_mark_in_transit, name="rma_mark_in_transit"),
    path("<int:pk>/refund/",      views.rma_refund,          name="rma_refund"),
    path("<int:pk>/resolve/",     views.rma_resolve,         name="rma_resolve"),
    path("<int:pk>/reopen/",      views.rma_reopen,          name="rma_reopen"),
    path("<int:pk>/delete/",      views.rma_delete,          name="rma_delete"),
    path("<int:pk>/message/",     views.rma_message,         name="rma_message"),
    path("<int:pk>/note/",        views.rma_internal_note,   name="rma_internal_note"),
    path("settings/save/",        views.rma_settings_view,   name="rma_settings_save"),

    # Legacy full-page routes (kept for direct bookmarks, sidebar uses section)
    path("",                      views.rma_list,            name="rma_list"),
    path("analytics/",            views.rma_analytics,       name="rma_analytics"),
    path("settings/",             views.rma_settings_view,   name="rma_settings"),
    path("<int:pk>/",             views.rma_detail,          name="rma_detail"),
]
