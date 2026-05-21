from rest_framework import serializers
from .models import Order


class OrderSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source="store.name", read_only=True)
    platform = serializers.CharField(source="store.platform", read_only=True)

    assigned_to = serializers.PrimaryKeyRelatedField(read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.name", read_only=True)
    assigned_to_role = serializers.CharField(source="assigned_to.role", read_only=True)

    assigned_vendor_name = serializers.CharField(source="assigned_vendor.name", read_only=True)
    assigned_vendor_company = serializers.CharField(source="assigned_vendor.company_name", read_only=True)

    # Full structured shipping + billing addresses parsed from raw_data so
    # every UI (admin, vendor, employee) can render street/city/state/zip
    # without re-parsing WC vs Shopify JSON shapes themselves.
    shipping_address      = serializers.ReadOnlyField()
    billing_address       = serializers.ReadOnlyField()
    shipping_address_text = serializers.ReadOnlyField()

    class Meta:
        model = Order
        fields = [
            "id",
            "store",
            "store_name",
            "platform",
            "assigned_to",
            "assigned_to_name",
            "assigned_to_role",
            "assigned_vendor",
            "assigned_vendor_name",
            "assigned_vendor_company",
            "vendor_status",
            "external_order_id",
            "customer_name",
            "customer_email",
            "customer_phone",
            "country",
            "city",
            "shipping_address",
            "billing_address",
            "shipping_address_text",
            "total_price",
            "currency",
            "payment_status",
            "fulfillment_status",
            "tracking_number",
            "tracking_company",
            "tracking_url",
            "tracking_status",
            "live_tracking_status",
            "delivered_at",
            "created_at",
            "product_name",
            "product_id",
            "assignment_type",
            "raw_data",
        ]