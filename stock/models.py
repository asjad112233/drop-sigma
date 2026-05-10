from django.db import models
from django.contrib.auth.models import User
from stores.models import Store
from orders.models import Order
from django.utils import timezone


class StockProduct(models.Model):
    store        = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="stock_products")
    product_id   = models.CharField(max_length=200)
    product_name = models.CharField(max_length=500)
    image_url    = models.URLField(max_length=1000, blank=True, default="")
    is_active    = models.BooleanField(default=True)
    synced_at    = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("store", "product_id")
        ordering = ["product_name"]

    def __str__(self):
        return f"{self.product_name} ({self.store.name})"


class StockVariant(models.Model):
    product = models.ForeignKey(StockProduct, on_delete=models.CASCADE, related_name="variants")
    color   = models.CharField(max_length=100, blank=True, default="")
    size    = models.CharField(max_length=100, blank=True, default="")
    sku     = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        unique_together = ("product", "color", "size")
        ordering = ["color", "size"]

    def __str__(self):
        parts = [self.product.product_name]
        if self.color:
            parts.append(self.color)
        if self.size:
            parts.append(self.size)
        return " / ".join(parts)


class StockEntry(models.Model):
    variant      = models.OneToOneField(StockVariant, on_delete=models.CASCADE, related_name="entry")
    quantity     = models.IntegerField(default=0)
    reserved     = models.IntegerField(default=0)
    updated_by   = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    last_updated = models.DateTimeField(auto_now=True)

    def available(self):
        return max(0, self.quantity - self.reserved)

    def __str__(self):
        return f"{self.variant} — qty:{self.quantity} res:{self.reserved}"


class StockOrderAssignment(models.Model):
    """Links an order line item to a specific stock variant."""
    order      = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="stock_assignments")
    variant    = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="order_assignments")
    product_id = models.CharField(max_length=200, blank=True, default="")
    quantity   = models.IntegerField(default=1)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("order", "product_id")
        ordering = ["-assigned_at"]

    def __str__(self):
        return f"Order#{self.order_id} → {self.variant}"


class StockAutoRule(models.Model):
    """Permanent rule: auto-assign a stock variant whenever a product is ordered."""
    store      = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="stock_auto_rules")
    product_id = models.CharField(max_length=200)
    variant    = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="auto_rules")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("store", "product_id")
        ordering = ["-created_at"]

    def __str__(self):
        return f"AutoRule {self.product_id} → {self.variant}"


class StockAuditLog(models.Model):
    ACTION_CHOICES = [
        ("add",     "Stock Added"),
        ("deduct",  "Deducted (Order)"),
        ("reserve", "Reserved"),
        ("restore", "Restored"),
        ("adjust",  "Manual Adjustment"),
        ("sync",    "Product Synced"),
    ]

    variant    = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="audit_logs")
    order      = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL)
    action     = models.CharField(max_length=20, choices=ACTION_CHOICES)
    qty_before = models.IntegerField(default=0)
    qty_after  = models.IntegerField(default=0)
    actor      = models.CharField(max_length=200, default="Admin")
    note       = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} — {self.variant} [{self.qty_before}→{self.qty_after}]"


class VendorStockAssignment(models.Model):
    """Admin assigns a batch of stock (per variant) to a vendor for a product."""
    STATUS_CHOICES = [
        ("pending_pricing",      "Pending Vendor Quotation"),
        ("pending_approval",     "Pending Admin Approval"),
        ("approved",             "Approved"),
        ("rejected",             "Rejected — Resubmit"),
        ("permanently_rejected", "Permanently Rejected"),
    ]

    store               = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="vendor_stock_assignments")
    vendor              = models.ForeignKey("vendors.Vendor", on_delete=models.CASCADE, related_name="stock_assignments")
    product             = models.ForeignKey(StockProduct, on_delete=models.CASCADE, related_name="vendor_assignments")
    status              = models.CharField(max_length=25, choices=STATUS_CHOICES, default="pending_pricing")
    admin_note          = models.TextField(blank=True, default="")
    vendor_note         = models.TextField(blank=True, default="")
    reject_reason       = models.TextField(blank=True, default="")
    # Quotation fields (set by vendor)
    per_unit_price      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimated_days      = models.IntegerField(null=True, blank=True)
    resubmission_count  = models.IntegerField(default=0)
    # Payment (set by admin on approve)
    payment_amount      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payment_method      = models.CharField(max_length=100, blank=True, default="")
    payment_reference   = models.CharField(max_length=255, blank=True, default="")
    approved_by         = models.CharField(max_length=200, blank=True, default="")
    approved_at         = models.DateTimeField(null=True, blank=True)
    # Arrival confirmation (vendor marks after physical stock arrives)
    stock_arrived       = models.BooleanField(default=False)
    arrived_at          = models.DateTimeField(null=True, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def total_assigned(self):
        return sum(l.quantity_assigned for l in self.lines.all())

    def total_sold(self):
        return sum(l.quantity_sold for l in self.lines.all())

    def total_on_hand(self):
        return self.total_assigned() - self.total_sold()

    @property
    def days_remaining(self):
        if self.status != "approved" or not self.approved_at or not self.estimated_days:
            return None
        elapsed = (timezone.now() - self.approved_at).days
        return max(0, self.estimated_days - elapsed)

    def __str__(self):
        return f"{self.vendor.name} ← {self.product.product_name} [{self.status}]"


class VendorQuotationAttempt(models.Model):
    """Tracks every quotation submission attempt by the vendor."""
    ATTEMPT_STATUS = [
        ("pending",              "Pending Review"),
        ("approved",             "Approved"),
        ("rejected",             "Rejected"),
        ("permanently_rejected", "Permanently Rejected"),
    ]
    assignment     = models.ForeignKey(VendorStockAssignment, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.IntegerField()
    per_unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_quantity = models.IntegerField()
    total_price    = models.DecimalField(max_digits=12, decimal_places=2)
    estimated_days = models.IntegerField()
    vendor_note    = models.TextField(blank=True, default="")
    submitted_at   = models.DateTimeField(auto_now_add=True)
    status         = models.CharField(max_length=25, choices=ATTEMPT_STATUS, default="pending")
    admin_note     = models.TextField(blank=True, default="")
    responded_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["attempt_number"]

    def __str__(self):
        return f"Attempt #{self.attempt_number} — {self.assignment}"


class VendorStockAssignmentLine(models.Model):
    """One variant row in a VendorStockAssignment."""
    assignment        = models.ForeignKey(VendorStockAssignment, on_delete=models.CASCADE, related_name="lines")
    variant           = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="vendor_lines")
    quantity_assigned = models.IntegerField(default=0)
    quantity_sold     = models.IntegerField(default=0)
    unit_price        = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("assignment", "variant")
        ordering = ["variant__size", "variant__color"]

    @property
    def on_hand(self):
        return max(0, self.quantity_assigned - self.quantity_sold)

    def __str__(self):
        return f"{self.assignment} / {self.variant} qty={self.quantity_assigned}"
