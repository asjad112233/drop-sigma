from django.contrib import admin
from .models import Vendor, ProductVendorAssignment, VendorPasswordResetRequest


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "status", "assigned_store", "created_at")
    list_filter = ("status", "assigned_store")
    search_fields = ("name", "email")


@admin.register(ProductVendorAssignment)
class ProductVendorAssignmentAdmin(admin.ModelAdmin):
    list_display = ("product_name", "product_id", "vendor", "store", "is_active", "created_at")
    list_filter = ("store", "vendor", "is_active")
    search_fields = ("product_name", "product_id", "vendor__name")


@admin.register(VendorPasswordResetRequest)
class VendorPasswordResetRequestAdmin(admin.ModelAdmin):
    list_display = ("vendor", "requested_email", "status", "created_at", "resolved_by", "resolved_at")
    list_filter = ("status", "created_at")
    search_fields = ("vendor__name", "requested_email")
    readonly_fields = ("vendor", "requested_email", "requested_ip", "user_agent", "created_at")
