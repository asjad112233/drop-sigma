from django.urls import path
from . import views

urlpatterns = [
    path("", views.vendor_list, name="vendor_list"),
    path("create/", views.vendor_create, name="vendor_create"),
    path("delete/<int:vendor_id>/", views.vendor_delete, name="vendor_delete"),
    path("<int:vendor_id>/permissions/", views.vendor_update_permissions, name="vendor_update_permissions"),
    path("<int:vendor_id>/permission-logs/", views.vendor_permission_logs_api, name="vendor_permission_logs_api"),
    path("<int:vendor_id>/products/", views.vendor_products_api, name="vendor_products_api"),
    path("assignments/<int:assignment_id>/remove/", views.remove_perm_assignment_api, name="remove_perm_assignment"),
    path("<int:vendor_id>/store-scope/<int:store_id>/", views.vendor_toggle_store_scope_api, name="vendor_toggle_store_scope"),
    path("store/<int:store_id>/full-vendor/", views.store_full_vendor_api, name="store_full_vendor_api"),
    path("tracking-queue/", views.tracking_queue_api, name="tracking_queue_api"),
    path("tracking-queue/<int:submission_id>/approve/", views.approve_tracking_api, name="approve_tracking"),
    path("tracking-queue/<int:submission_id>/approve-permanent/", views.approve_tracking_permanent_api, name="approve_tracking_permanent"),
    path("tracking-queue/<int:submission_id>/reject/", views.reject_tracking_api, name="reject_tracking"),
    path("tracking-settings/", views.tracking_queue_settings_api, name="tracking_queue_settings"),
    path("tracking-auto-approve/<str:product_id>/remove/", views.remove_product_auto_approve_api, name="remove_product_auto_approve"),
    path("<int:vendor_id>/status/", views.vendor_update_status, name="vendor_update_status"),
    path("<int:vendor_id>/store/<int:store_id>/manage-products/", views.vendor_store_manage_products_api, name="vendor_manage_products"),
    path("<int:vendor_id>/credentials/", views.vendor_credentials_api, name="vendor_credentials"),
    path("<int:vendor_id>/reset-password/", views.vendor_reset_password_api, name="vendor_reset_password"),
    path("invite/send/", views.send_vendor_invitation_api, name="send_vendor_invitation"),

    # ─── Vendor Pricing & Approval System (v1) ──────────────────────────────
    path("sourcing/products/",                         views.sourcing_products_list_api,     name="sourcing_products_list"),
    path("sourcing/assign/",                           views.sourcing_bulk_assign_api,       name="sourcing_bulk_assign"),
    path("sourcing/queue/",                            views.sourcing_approvals_queue_api,   name="sourcing_approvals_queue"),
    path("sourcing/quote/<int:quote_id>/approve/",     views.sourcing_approve_quote_api,     name="sourcing_approve_quote"),
    path("sourcing/quote/<int:quote_id>/reject/",      views.sourcing_reject_quote_api,      name="sourcing_reject_quote"),
    path("sourcing/change/<int:change_id>/approve/",   views.sourcing_approve_change_api,    name="sourcing_approve_change"),
    path("sourcing/change/<int:change_id>/reject/",    views.sourcing_reject_change_api,     name="sourcing_reject_change"),
    path("sourcing/bulk-reject/",                      views.sourcing_bulk_reject_api,       name="sourcing_bulk_reject"),
    path("sourcing/trust/",                            views.sourcing_trust_settings_api,    name="sourcing_trust_settings"),
    path("sourcing/trust/<int:vendor_id>/",            views.sourcing_set_trust_api,         name="sourcing_set_trust"),
    path("sourcing/reasons/",                          views.sourcing_reasons_api,           name="sourcing_reasons"),
    path("sourcing/assignment/<int:assignment_id>/",   views.sourcing_assignment_detail_api, name="sourcing_assignment_detail"),
]
