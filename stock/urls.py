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
]
