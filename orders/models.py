from django.db import models
from stores.models import Store


class Order(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)

    # 👇 Team Assignment
    assigned_to = models.ForeignKey(
        "teamapp.TeamMember",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="assigned_orders"
    )

    # ===== VENDOR SYSTEM =====
    assigned_vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="vendor_orders"
    )

    assignment_type = models.CharField(
        max_length=20,
        default="manual"
    )  # manual / permanent_auto

    product_id = models.CharField(max_length=255, blank=True, null=True)
    product_name = models.CharField(max_length=255, blank=True, null=True)

    tracking_status = models.CharField(
        max_length=50,
        default="pending"
    )

    vendor_status = models.CharField(
        max_length=50,
        default="unassigned",
        choices=[
            ("unassigned", "Unassigned"),
            ("assigned", "Assigned"),
            ("in_progress", "In Progress"),
            ("tracking_submitted", "Tracking Submitted"),
            ("rejected", "Rejected"),
            ("approved", "Approved"),
        ],
    )

    # ===== ORDER DATA =====
    external_order_id = models.CharField(max_length=255)
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    customer_email = models.EmailField(blank=True, null=True)
    customer_phone = models.CharField(max_length=50, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=20, default='USD')
    payment_status = models.CharField(max_length=100, blank=True, null=True)
    fulfillment_status = models.CharField(max_length=100, blank=True, null=True)
    tracking_number = models.CharField(max_length=255, blank=True, null=True)
    tracking_company = models.CharField(max_length=255, blank=True, null=True)
    tracking_url = models.URLField(max_length=1000, blank=True, null=True)
    live_tracking_status = models.CharField(max_length=255, blank=True, null=True)
    delivered_at = models.DateTimeField(blank=True, null=True)
    raw_data = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.external_order_id


class OrderActivity(models.Model):
    TYPES = [
        ("received",            "Order Received"),
        ("assigned",            "Order Assigned"),
        ("vendor_assigned",     "Vendor Assigned"),
        ("tracking_submitted",  "Tracking Submitted"),
        ("tracking_approved",   "Tracking Approved"),
        ("tracking_rejected",   "Tracking Rejected"),
        ("tracking_added",      "Tracking Added"),
        ("note",                "Note"),
    ]

    order       = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="activities")
    activity_type = models.CharField(max_length=50, choices=TYPES)
    description = models.TextField()
    actor       = models.CharField(max_length=255, blank=True, null=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.order.external_order_id} — {self.activity_type}"