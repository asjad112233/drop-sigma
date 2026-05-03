from rest_framework import serializers
from .models import EmailMessage, EmailAttachment


class EmailAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailAttachment
        fields = ["id", "filename", "content_type", "file", "size", "created_at"]


class EmailMessageSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source="store.name", read_only=True)
    order_number = serializers.CharField(source="order.external_order_id", read_only=True)
    attachments = EmailAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = EmailMessage
        fields = "__all__"