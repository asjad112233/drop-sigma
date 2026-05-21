from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display  = ("title", "recipient", "audience", "category", "priority", "is_read", "created_at")
    list_filter   = ("audience", "category", "priority", "is_read")
    search_fields = ("title", "body", "recipient__email", "recipient__username")
    readonly_fields = ("created_at", "read_at")
