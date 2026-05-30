from django.contrib import admin
from .models import (
    Vendor, ProductVendorAssignment,
    VendorQuote, VendorShippingOverride, QuoteChangeRequest,
    VendorTrustSetting, ReasonConfig, QuoteReminderLog,
)


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


@admin.register(VendorQuote)
class VendorQuoteAdmin(admin.ModelAdmin):
    list_display = ("id", "assignment", "product_cost", "default_shipping",
                    "status", "review_pending", "submitted_at")
    list_filter = ("status", "review_pending", "currency")
    search_fields = ("assignment__product_name", "assignment__vendor__name")
    readonly_fields = ("submitted_at",)


@admin.register(VendorShippingOverride)
class VendorShippingOverrideAdmin(admin.ModelAdmin):
    list_display = ("id", "quote", "country_code", "shipping_cost", "status", "created_at")
    list_filter = ("status", "country_code")
    search_fields = ("country_code",)


@admin.register(QuoteChangeRequest)
class QuoteChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "quote", "change_type", "old_value", "new_value",
                    "delta_pct", "reason_key", "status", "auto_decision", "submitted_at")
    list_filter = ("status", "change_type", "auto_decision", "reason_key")
    search_fields = ("quote__assignment__product_name", "notes", "automation_reason")
    readonly_fields = ("submitted_at",)


@admin.register(VendorTrustSetting)
class VendorTrustSettingAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "vendor", "trust_mode",
                    "threshold_pct", "threshold_abs", "updated_at")
    list_filter = ("trust_mode",)
    search_fields = ("tenant__username", "vendor__name")


@admin.register(ReasonConfig)
class ReasonConfigAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "auto_eligible", "requires_notes", "sort_order")
    list_filter = ("auto_eligible", "requires_notes")
    search_fields = ("key", "label")


@admin.register(QuoteReminderLog)
class QuoteReminderLogAdmin(admin.ModelAdmin):
    list_display = ("id", "assignment", "kind", "sent_at")
    list_filter = ("kind",)
    readonly_fields = ("sent_at",)