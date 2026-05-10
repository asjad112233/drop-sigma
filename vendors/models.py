import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from stores.models import Store


class Vendor(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("inactive", "Inactive"),
    )

    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vendor_profile",
    )

    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, null=True)
    password_plain = models.CharField(max_length=255, blank=True, null=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")

    assigned_store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="vendors",
    )

    notes = models.TextField(blank=True, null=True)
    permissions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ProductVendorAssignment(models.Model):
    store = models.ForeignKey(
        "stores.Store",
        on_delete=models.CASCADE,
        related_name="product_vendor_assignments",
    )
    product_id = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255, blank=True, null=True)
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="product_assignments",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("store", "product_id")

    def __str__(self):
        return f"{self.product_name} → {self.vendor.name}"


class StoreVendorAssignment(models.Model):
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="store_assignments",
    )
    store = models.ForeignKey(
        "stores.Store",
        on_delete=models.CASCADE,
        related_name="vendor_assignments",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("vendor", "store")

    def __str__(self):
        return f"{self.vendor.name} → {self.store.name} (Full Store)"


class VendorTrackingSubmission(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending Approval"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="tracking_submissions",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="tracking_submissions",
    )

    tracking_number = models.CharField(max_length=255)
    tracking_url = models.URLField(blank=True, null=True)
    courier_name = models.CharField(max_length=100, blank=True, null=True)
    vendor_note = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    reject_reason = models.TextField(blank=True, null=True)
    is_auto_approved = models.BooleanField(default=False)

    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"#{self.order.external_order_id} — {self.vendor.name} — {self.status}"


class TrackingQueueSetting(models.Model):
    """Per-store auto-approve toggle for tracking submissions."""
    store = models.OneToOneField(
        "stores.Store",
        on_delete=models.CASCADE,
        related_name="tracking_queue_setting",
    )
    auto_approve = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.store.name} — auto_approve={self.auto_approve}"


class ProductTrackingAutoApprove(models.Model):
    """Products whose tracking submissions are always auto-approved."""
    product_id = models.CharField(max_length=255)
    store = models.ForeignKey(
        "stores.Store",
        on_delete=models.CASCADE,
        related_name="tracking_auto_approvals",
    )
    product_name = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("product_id", "store")

    def __str__(self):
        return f"{self.product_name or self.product_id} — auto-approve"


class VendorPermissionLog(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="permission_logs")
    changed_by = models.CharField(max_length=255)
    changes = models.JSONField(default=dict)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.vendor.name} — changed by {self.changed_by}"


class VendorInvitation(models.Model):
    STATUS_CHOICES = (
        ("pending",  "Pending"),
        ("accepted", "Accepted"),
        ("expired",  "Expired"),
    )

    token      = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner      = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_vendor_invitations")
    name       = models.CharField(max_length=255)
    email      = models.EmailField()
    store      = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="vendor_invitations")
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return self.status == "pending" and timezone.now() < self.expires_at

    def __str__(self):
        return f"Vendor Invitation → {self.email} ({self.status})"
