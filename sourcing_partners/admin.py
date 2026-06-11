from django.contrib import admin
from .models import (
    SourcingPartner, PartnerConversation, PartnerMessage, PartnerAttachment,
)


@admin.register(SourcingPartner)
class SourcingPartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "rating", "total_orders",
                    "on_time_rate_pct", "is_verified", "is_featured", "is_active")
    list_filter  = ("country", "is_verified", "is_featured", "is_active")
    search_fields = ("name", "tagline", "headquarters")
    list_editable = ("is_featured", "is_active")


@admin.register(PartnerConversation)
class PartnerConversationAdmin(admin.ModelAdmin):
    list_display  = ("tenant", "partner", "last_message_at", "unread_count_for_tenant", "is_archived")
    list_filter   = ("is_archived", "partner")
    search_fields = ("tenant__username", "partner__name")


@admin.register(PartnerMessage)
class PartnerMessageAdmin(admin.ModelAdmin):
    list_display  = ("conversation", "direction", "created_at", "is_read")
    list_filter   = ("direction", "is_read")


@admin.register(PartnerAttachment)
class PartnerAttachmentAdmin(admin.ModelAdmin):
    list_display  = ("message", "kind", "filename", "filesize_bytes")
    list_filter   = ("kind",)
