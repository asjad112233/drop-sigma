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


# ─────────────────────────────────────────────────────────────────────────────
# Vendor Pricing & Approval System (v1)
# Locked quote → vendor proposes change → automation engine decides
# Tenant resolution: ProductVendorAssignment.store.user is the tenant.
# ─────────────────────────────────────────────────────────────────────────────


class VendorQuote(models.Model):
    """Locked or pending quote for a (product, vendor) assignment.

    Multiple quotes per assignment over time (history); exactly one ACTIVE at
    a time. Orders snapshot the active quote at place-time — pending change
    requests do NOT affect placed orders.
    """
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"            # the locked one
    STATUS_SUPERSEDED = "superseded"    # replaced by a newer active quote
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Approval"),
        (STATUS_ACTIVE, "Active (Locked)"),
        (STATUS_SUPERSEDED, "Superseded"),
        (STATUS_REJECTED, "Rejected"),
    ]
    assignment = models.ForeignKey(
        ProductVendorAssignment, on_delete=models.CASCADE, related_name="quotes"
    )
    variant_id = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="Future-proofing for variant-level quoting (v1: always null)"
    )
    currency = models.CharField(
        max_length=8, default="USD",
        help_text="Tenant's currency; v1 doesn't convert"
    )
    product_cost = models.DecimalField(max_digits=10, decimal_places=2)
    default_shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    review_pending = models.BooleanField(
        default=False,
        help_text="True if there's an open change request on this quote — UI badge"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["assignment", "status"]),
            models.Index(fields=["status", "-submitted_at"]),
        ]

    def __str__(self):
        return f"Quote #{self.id} ({self.status}) — assignment {self.assignment_id}"


class VendorShippingOverride(models.Model):
    """Per-country shipping override on a quote.

    Each (quote, country_code) is unique. Status: active or pending_review.
    A NEW country added later is silently applied (no review). An existing
    override being removed is treated as a shipping change (reason + review).
    """
    STATUS_ACTIVE = "active"
    STATUS_PENDING = "pending"
    STATUS_CHOICES = [(STATUS_ACTIVE, "Active"), (STATUS_PENDING, "Pending Review")]

    quote = models.ForeignKey(
        VendorQuote, on_delete=models.CASCADE, related_name="overrides"
    )
    country_code = models.CharField(max_length=3, help_text="ISO 3166-1 alpha-2")
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("quote", "country_code")
        indexes = [models.Index(fields=["quote", "country_code"])]

    def __str__(self):
        return f"{self.country_code} ${self.shipping_cost} (quote {self.quote_id})"


class QuoteChangeRequest(models.Model):
    """A change the vendor proposes on an active quote. Goes through the
    automation engine (vendors.services.evaluate_change_request).

    NOTE: 'quote' points to the ACTIVE quote being changed. If approved, a
    NEW VendorQuote is created with the new values and the old one is marked
    superseded. Original orders still reference the OLD quote — they are
    unaffected (immutable snapshot).
    """
    CHANGE_PRODUCT_COST = "product_cost"
    CHANGE_DEFAULT_SHIPPING = "default_shipping"
    CHANGE_COUNTRY_SHIPPING = "country_shipping"
    CHANGE_COUNTRY_ADDED = "country_added"
    CHANGE_COUNTRY_REMOVED = "country_removed"
    CHANGE_TYPE_CHOICES = [
        (CHANGE_PRODUCT_COST, "Product Cost"),
        (CHANGE_DEFAULT_SHIPPING, "Default Shipping"),
        (CHANGE_COUNTRY_SHIPPING, "Country Shipping"),
        (CHANGE_COUNTRY_ADDED, "Country Added"),
        (CHANGE_COUNTRY_REMOVED, "Country Removed"),
    ]
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"              # tenant approved manually
    STATUS_AUTO_APPROVED = "auto_approved"    # automation approved
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_AUTO_APPROVED, "Auto-Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    quote = models.ForeignKey(
        VendorQuote, on_delete=models.CASCADE, related_name="change_requests"
    )
    change_type = models.CharField(max_length=30, choices=CHANGE_TYPE_CHOICES)
    country_code = models.CharField(
        max_length=3, blank=True, default="",
        help_text="Only for country_* change types"
    )
    old_value = models.DecimalField(max_digits=10, decimal_places=2)
    new_value = models.DecimalField(max_digits=10, decimal_places=2)
    delta_abs = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="new_value - old_value"
    )
    delta_pct = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="((new - old) / old) * 100; 0 if old=0"
    )
    reason_key = models.CharField(max_length=50, help_text="FK-like to ReasonConfig.key")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    auto_decision = models.BooleanField(
        default=False,
        help_text="True if status was decided by automation, not human"
    )
    automation_reason = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Audit: why automation approved/escalated, e.g. 'cost_decrease', 'over_threshold_pct', 'reason_not_auto_eligible'"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rejection_reason = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["quote", "status", "-submitted_at"]),
            models.Index(fields=["status", "-submitted_at"]),
        ]

    def __str__(self):
        return f"Change #{self.id} ({self.change_type}, {self.status})"


class VendorTrustSetting(models.Model):
    """Per-tenant per-vendor trust mode settings.

    Defaults: trust_mode=False (new vendor), threshold_pct=10, threshold_abs=5.
    Trust mode + auto-eligible reason + change within thresholds = auto-approve.
    """
    tenant = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="vendor_trust_settings"
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="trust_settings"
    )
    trust_mode = models.BooleanField(default=False)
    threshold_pct = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    threshold_abs = models.DecimalField(max_digits=10, decimal_places=2, default=5)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("tenant", "vendor")

    def __str__(self):
        return f"{self.tenant_id}↔{self.vendor.name} trust={self.trust_mode}"


class ReasonConfig(models.Model):
    """Seed table — change reasons + automation eligibility.

    v1: hardcoded via data migration. V3 plan: make tenant-configurable
    (just flip the auto_eligible bool per row).
    """
    key = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=100)
    auto_eligible = models.BooleanField(default=False)
    requires_notes = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.label} ({'auto' if self.auto_eligible else 'review'})"


class QuoteReminderLog(models.Model):
    """Idempotency log for SLA reminders so we don't double-send."""
    KIND_VENDOR_7D = "vendor_7d"
    KIND_TENANT_14D = "tenant_14d"
    KIND_CHOICES = [
        (KIND_VENDOR_7D, "Vendor 7-day reminder"),
        (KIND_TENANT_14D, "Tenant 14-day escalation"),
    ]
    assignment = models.ForeignKey(
        ProductVendorAssignment, on_delete=models.CASCADE, related_name="reminder_logs"
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("assignment", "kind")
        indexes = [models.Index(fields=["assignment", "kind"])]

    def __str__(self):
        return f"{self.kind} for assignment {self.assignment_id} @ {self.sent_at}"
