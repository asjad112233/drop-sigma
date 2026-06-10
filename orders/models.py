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

    # ===== DROP SIGMA SOURCING LIFECYCLE =====
    # Independent of payment_status/fulfillment_status (which reflect what
    # the END CUSTOMER did on the merchant store). These fields track the
    # tenant ↔ Drop Sigma sourcing flow.
    SOURCING_STATUS_CHOICES = [
        ("pending_source",  "Pending Source"),
        ("pending_payment", "Pending Payment"),
        ("processing",      "Processing"),
        ("shipping",        "Shipping"),
        ("delivered",       "Delivered"),
        ("cancel",          "Cancelled"),
    ]
    sourcing_status = models.CharField(
        max_length=20, choices=SOURCING_STATUS_CHOICES,
        default="pending_source", db_index=True,
    )
    sourcing_total_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Drop Sigma's quoted total for the tenant — wallet charge amount.",
    )
    # Breakdown shown to tenant once the order leaves Pending Source. The
    # sum (product + shipping) should equal sourcing_total_usd; the team UI
    # is responsible for keeping these in sync at quote time.
    sourcing_product_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Product cost portion of the sourcing quote.",
    )
    sourcing_shipping_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Shipping cost portion of the sourcing quote.",
    )
    sourcing_quoted_at = models.DateTimeField(null=True, blank=True)
    sourcing_paid_at = models.DateTimeField(null=True, blank=True)
    sourcing_shipped_at = models.DateTimeField(null=True, blank=True)
    sourcing_delivered_at = models.DateTimeField(null=True, blank=True)
    sourcing_cancelled_at = models.DateTimeField(null=True, blank=True)
    sourcing_lead_days = models.PositiveIntegerField(null=True, blank=True)
    sourcing_partner = models.ForeignKey(
        "sourcing_partners.SourcingPartner",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="synced_orders",
        help_text="Internal routing — never surfaced to the tenant.",
    )
    sourcing_locked_price = models.ForeignKey(
        "sourcing_partners.LockedPrice",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="used_in_orders",
    )
    # Tenant-editable shipping override — once we lock for procurement we
    # persist any edits the tenant made before paying. Until that lock, the
    # UI uses raw_data's address.
    shipping_override = models.JSONField(null=True, blank=True)

    @property
    def is_shipping_editable(self):
        return self.sourcing_status in ("pending_source", "pending_payment")

    @property
    def ds_order_ref(self):
        """Tenant-facing Drop Sigma order number, e.g. DS-001234."""
        return f"DS-{self.id:06d}"

    def __str__(self):
        return self.external_order_id

    # ───────────────────────────────────────────────────────────────────
    # Address helpers — the Order model only stores city/country as
    # top-level columns, but the full shipping/billing address always
    # lives in raw_data (WC + Shopify sync persists the original
    # payload). Surface it as a structured dict the UI can render
    # without each portal having to grok platform-specific JSON shapes.
    # ───────────────────────────────────────────────────────────────────
    def _address_from_raw(self, kind):
        """kind: 'shipping' or 'billing'.
        WooCommerce keys: shipping / billing (nested first_name, address_1, etc.)
        Shopify keys:     shipping_address / billing_address (nested address1, etc.)
        Returns a normalised dict or {} when nothing usable is present.
        """
        raw = self.raw_data or {}
        if not isinstance(raw, dict):
            return {}

        # Platform detection — WC sends 'billing' as a dict; Shopify uses
        # 'billing_address'/'shipping_address'. Try both shapes.
        if kind == "shipping":
            src = raw.get("shipping") or raw.get("shipping_address") or {}
        else:
            src = raw.get("billing") or raw.get("billing_address") or {}
        if not isinstance(src, dict):
            return {}

        # WC uses address_1/2; Shopify uses address1/2. Read whichever exists.
        line1 = (src.get("address_1") or src.get("address1") or "").strip()
        line2 = (src.get("address_2") or src.get("address2") or "").strip()
        first = (src.get("first_name") or "").strip()
        last  = (src.get("last_name")  or "").strip()
        full_name = (src.get("name") or f"{first} {last}").strip()

        return {
            "name":         full_name,
            "company":      (src.get("company") or "").strip(),
            "line1":        line1,
            "line2":        line2,
            "city":         (src.get("city") or "").strip(),
            "state":        (src.get("state") or src.get("province") or "").strip(),
            "postal_code":  (src.get("postcode") or src.get("zip") or "").strip(),
            "country":      (src.get("country_code") or src.get("country") or "").strip(),
            "phone":        (src.get("phone") or "").strip(),
            "email":        (src.get("email") or "").strip(),
        }

    @property
    def shipping_address(self):
        """Where the customer wants the order delivered. Falls back to
        billing if shipping is empty (some merchants only collect one)."""
        sh = self._address_from_raw("shipping")
        # Treat an address as empty if both line1 and city are missing —
        # WC populates a hollow shipping dict on virtual orders.
        if not sh.get("line1") and not sh.get("city"):
            return self._address_from_raw("billing")
        return sh

    @property
    def billing_address(self):
        return self._address_from_raw("billing")

    @property
    def shipping_address_text(self):
        """One-line formatted version for legacy contexts that want a
        ready-to-display string (vendor lists, emails, etc.)."""
        a = self.shipping_address
        parts = [a.get("name"), a.get("line1"), a.get("line2"),
                 a.get("city"), a.get("state"), a.get("postal_code"),
                 a.get("country")]
        return ", ".join(p for p in parts if p)


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

# ════════════════════════════════════════════════════════════════════
# SHOPIFY APP STORE — GDPR audit log
# ────────────────────────────────────────────────────────────────────
# Shopify requires apps to respond to GDPR webhooks (data export,
# customer redact, shop redact). We log every incoming request here
# so support can fulfil within the SLA and prove compliance during
# App Store review.
# ════════════════════════════════════════════════════════════════════
class ShopifyGdprRequest(models.Model):
    REQUEST_TYPES = [
        ("data_request",      "Customer Data Request"),
        ("customers_redact",  "Customer Redact"),
        ("shop_redact",       "Shop Redact"),
    ]
    request_type   = models.CharField(max_length=32, choices=REQUEST_TYPES, db_index=True)
    shop_domain    = models.CharField(max_length=255, db_index=True)
    customer_email = models.EmailField(blank=True, default="")
    payload        = models.JSONField(default=dict, blank=True)
    handled        = models.BooleanField(default=False)
    created_at     = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["request_type", "-created_at"]),
            models.Index(fields=["shop_domain", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.request_type} @ {self.shop_domain} ({self.created_at:%Y-%m-%d})"
