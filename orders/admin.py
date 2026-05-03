from django.contrib import admin
from .models import Order

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'external_order_id',
        'customer_name',
        'customer_email',
        'total_price',
        'currency',
        'payment_status',
        'created_at',
    )

    search_fields = ('external_order_id', 'customer_name', 'customer_email')
    list_filter = ('payment_status', 'currency')