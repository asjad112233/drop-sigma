from rest_framework import serializers
from .models import Vendor, VendorTrackingSubmission


class VendorSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source="assigned_store.name", read_only=True)

    class Meta:
        model = Vendor
        fields = [
            "id", "name", "email", "phone", "company_name", "country",
            "status", "assigned_store", "store_name", "notes", "permissions",
            "password_plain", "created_at",
        ]
        extra_kwargs = {"password_plain": {"write_only": False}}


class VendorTrackingSubmissionSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.external_order_id", read_only=True)
    customer_name = serializers.CharField(source="order.customer_name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = VendorTrackingSubmission
        fields = "__all__"
