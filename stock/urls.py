from django.urls import path
from . import views

urlpatterns = [
    path("dashboard/", views.stock_dashboard_api),
    path("sync/", views.stock_sync_api),
    path("entry/", views.stock_entry_api),
    path("add/", views.stock_add_product_api),
    path("bulk-upload/", views.stock_bulk_upload_api),
    path("deduct/", views.stock_deduct_api),
    path("audit/", views.stock_audit_api),
    path("fetch-products/", views.stock_fetch_store_products_api),
    path("import-products/", views.stock_import_products_api),
    path("export/", views.stock_export_api),
    path("bulk-update/", views.stock_bulk_update_api),
    path("assign-order/", views.stock_assign_order_api),
    path("order-assignments/", views.stock_order_assignments_api),
    path("orders/", views.stock_orders_api),
]
