from rest_framework import serializers
from .models import Store


class StoreSerializer(serializers.ModelSerializer):
    order_count = serializers.SerializerMethodField()
    vendor_count = serializers.SerializerMethodField()
    last_synced = serializers.SerializerMethodField()

    class Meta:
        model = Store
        fields = [
            "id",
            "name",
            "platform",
            "store_url",
            "is_active",
            "order_count",
            "vendor_count",
            "last_synced",
        ]

    def get_order_count(self, obj):
        return obj.order_set.count()

    def get_vendor_count(self, obj):
        from vendors.models import Vendor
        return Vendor.objects.filter(assigned_store=obj).count()

    def get_last_synced(self, obj):
        last = obj.order_set.order_by("-created_at").first()
        if last and last.created_at:
            return last.created_at.isoformat()
        return None
