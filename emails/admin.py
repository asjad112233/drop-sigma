from django.contrib import admin
from .models import EmailMessage


@admin.register(EmailMessage)
class EmailMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "sender", "recipient", "store", "status", "created_at")
    list_filter = ("status", "store", "created_at")
    search_fields = ("subject", "sender", "recipient", "body")