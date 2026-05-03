from django.contrib import admin
from .models import Vendor, ProductVendorAssignment


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