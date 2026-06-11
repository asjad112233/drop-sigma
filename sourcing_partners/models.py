"""
DropSigma Sourcing Partners — verified sourcing vendors that tenants
can discover and chat with directly inside the dashboard.

This is distinct from the existing `vendors` app, which represents
the TENANT's OWN personal vendors. Sourcing Partners are DropSigma-
vetted sourcing houses (typically based in China) that handle
end-to-end procurement on behalf of the tenant.
"""
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone


class SourcingPartner(models.Model):
    """A DropSigma-verified sourcing vendor visible to every tenant."""

    name        = models.CharField(max_length=200)
    slug        = models.SlugField(unique=True)
    logo_url    = models.URLField(blank=True, default="")
    cover_emoji = models.CharField(max_length=8, default="🏭",
                                   help_text="Emoji shown on the card when no logo.")
    cover_gradient_from = models.CharField(max_length=20, default="#6366f1")
    cover_gradient_to   = models.CharField(max_length=20, default="#a855f7")

    tagline     = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    # Origin
    country      = models.CharField(max_length=80, default="China")
    headquarters = models.CharField(max_length=120, blank=True)
    founded_year = models.PositiveSmallIntegerField(default=2020)

    # Capability
    specialties      = models.JSONField(default=list, blank=True)   # ["Apparel","Tech"]
    languages        = models.JSONField(default=list, blank=True)
    payment_methods  = models.JSONField(default=list, blank=True)
    min_order_value_usd  = models.PositiveIntegerField(default=100)
    typical_lead_days    = models.PositiveSmallIntegerField(default=7)
    team_size       = models.CharField(max_length=40, default="50–100", blank=True)
    monthly_volume  = models.CharField(max_length=80, default="1,000+ orders", blank=True)

    # Trust signals
    is_verified         = models.BooleanField(default=True)
    is_featured         = models.BooleanField(default=False)
    rating              = models.DecimalField(max_digits=3, decimal_places=2,
                                              default=Decimal("4.80"))
    total_reviews       = models.PositiveIntegerField(default=0)
    total_orders        = models.PositiveIntegerField(default=0)
    on_time_rate_pct    = models.PositiveSmallIntegerField(default=95)
    avg_response_hours  = models.PositiveSmallIntegerField(default=4)

    is_active   = models.BooleanField(default=True)
    sort_order  = models.IntegerField(default=0,
                                      help_text="Lower = appears earlier.")
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "-rating", "name"]

    def __str__(self):
        return self.name

    @property
    def initials(self):
        parts = [w for w in self.name.split() if w]
        return ("".join(p[0] for p in parts[:2]) or "DS").upper()

    @property
    def gradient_css(self):
        return (f"linear-gradient(135deg, {self.cover_gradient_from} 0%, "
                f"{self.cover_gradient_to} 100%)")


class PartnerConversation(models.Model):
    """1-1 thread between a tenant (Drop Sigma User) and a SourcingPartner."""

    tenant  = models.ForeignKey(settings.AUTH_USER_MODEL,
                                on_delete=models.CASCADE,
                                related_name="partner_conversations")
    partner = models.ForeignKey(SourcingPartner,
                                on_delete=models.CASCADE,
                                related_name="conversations")
    last_message_at  = models.DateTimeField(auto_now_add=True, db_index=True)
    last_message_preview = models.CharField(max_length=200, blank=True)
    unread_count_for_tenant = models.PositiveIntegerField(default=0)
    is_archived      = models.BooleanField(default=False)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("tenant", "partner")]
        ordering = ["-last_message_at"]

    def __str__(self):
        return f"{self.tenant.username} ↔ {self.partner.name}"


class PartnerMessage(models.Model):
    DIRECTION_CHOICES = [
        ("out", "Tenant → Partner"),
        ("in",  "Partner → Tenant"),
    ]

    conversation = models.ForeignKey(PartnerConversation,
                                     on_delete=models.CASCADE,
                                     related_name="messages")
    direction    = models.CharField(max_length=4, choices=DIRECTION_CHOICES,
                                    default="out")
    body         = models.TextField(blank=True)
    is_read      = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.direction} · {self.created_at:%Y-%m-%d %H:%M}"


class PartnerAttachment(models.Model):
    """File, image, or link attached to a PartnerMessage."""
    KIND_CHOICES = [
        ("image", "Image"),
        ("file",  "File"),
        ("link",  "Link"),
    ]

    message       = models.ForeignKey(PartnerMessage,
                                      on_delete=models.CASCADE,
                                      related_name="attachments")
    kind          = models.CharField(max_length=10, choices=KIND_CHOICES)
    file          = models.FileField(upload_to="partner_chat/%Y/%m/",
                                     null=True, blank=True)
    url           = models.URLField(blank=True)
    filename      = models.CharField(max_length=255, blank=True)
    filesize_bytes = models.PositiveBigIntegerField(default=0)
    mime_type     = models.CharField(max_length=80, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    @property
    def display_size(self):
        size = self.filesize_bytes
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size/1024:.1f} KB"
        return f"{size/(1024*1024):.2f} MB"

    @property
    def is_image(self):
        return self.kind == "image"


# ═══════════════════════════════════════════════════════════════════════
# WORKSPACE MODELS — tabs for Orders / Catalog / Stock / Payments /
# Documents / Performance / Auto-Rules / Activity
# ═══════════════════════════════════════════════════════════════════════


class VendorOrder(models.Model):
    """An order the tenant has routed to a DropSigma sourcing partner.

    Separate from the existing orders.Order model — that one represents
    the tenant's Shopify/Woo orders. This one represents the *procurement
    instruction* the tenant sent to a partner. Links back to the source
    order via `source_order_ref`."""

    STATUS_CHOICES = [
        ("awaiting_quote",  "Awaiting Quote"),
        ("quote_received",  "Quote Received"),
        ("quote_rejected",  "Quote Rejected"),
        ("paid",            "Paid · Procuring"),
        ("ordered_china",   "Ordered from China"),
        ("in_transit",      "In Transit"),
        ("at_warehouse",    "At Vendor Warehouse"),
        ("shipped",         "Shipped to Customer"),
        ("delivered",       "Delivered"),
        ("cancelled",       "Cancelled"),
        ("disputed",        "Disputed"),
    ]
    STATUS_ACTIVE = {
        "awaiting_quote", "quote_received", "paid", "ordered_china",
        "in_transit", "at_warehouse", "shipped",
    }
    STATUS_NEEDS_TENANT = {"quote_received"}  # action required from tenant

    tenant            = models.ForeignKey(settings.AUTH_USER_MODEL,
                                          on_delete=models.CASCADE,
                                          related_name="vendor_orders")
    partner           = models.ForeignKey(SourcingPartner,
                                          on_delete=models.PROTECT,
                                          related_name="vendor_orders")
    order_ref         = models.CharField(max_length=40, unique=True)
    source_order_ref  = models.CharField(max_length=120, blank=True,
                                         help_text="Shopify/Woo order ID this came from.")

    # Product
    product_sku       = models.CharField(max_length=120)
    product_name      = models.CharField(max_length=300)
    product_image_url = models.URLField(blank=True)
    variant           = models.CharField(max_length=200, blank=True)
    quantity          = models.PositiveIntegerField(default=1)

    # Customer (the end customer, masked for vendor employees later)
    customer_name     = models.CharField(max_length=200, blank=True)
    customer_email    = models.EmailField(blank=True)
    ship_city         = models.CharField(max_length=120, blank=True)
    ship_country      = models.CharField(max_length=80, blank=True)
    ship_full_address = models.TextField(blank=True)

    # Money (vendor's quote breakdown)
    china_cost_cny    = models.DecimalField(max_digits=10, decimal_places=2,
                                            default=Decimal("0.00"))
    china_cost_usd    = models.DecimalField(max_digits=10, decimal_places=2,
                                            default=Decimal("0.00"))
    shipping_usd      = models.DecimalField(max_digits=8, decimal_places=2,
                                            default=Decimal("0.00"))
    vendor_margin_usd = models.DecimalField(max_digits=8, decimal_places=2,
                                            default=Decimal("0.00"))
    platform_fee_usd  = models.DecimalField(max_digits=8, decimal_places=2,
                                            default=Decimal("0.00"))
    tenant_total_usd  = models.DecimalField(max_digits=10, decimal_places=2,
                                            default=Decimal("0.00"),
                                            help_text="What the tenant pays.")

    # Status + workflow
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                         default="awaiting_quote", db_index=True)
    priority          = models.CharField(max_length=10, default="normal")
    lead_days_quoted  = models.PositiveSmallIntegerField(default=7)

    # Tracking
    china_tracking_no = models.CharField(max_length=120, blank=True)
    last_mile_carrier = models.CharField(max_length=80, blank=True)
    last_mile_tracking = models.CharField(max_length=120, blank=True)
    last_mile_url     = models.URLField(blank=True)

    # Notes
    tenant_notes      = models.TextField(blank=True)
    vendor_notes      = models.TextField(blank=True)

    # Locked-price link
    locked_price_used = models.ForeignKey(
        "LockedPrice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders_used",
        help_text="Set when this order used an existing locked price (skipped quote step).",
    )

    # Lifecycle timestamps
    quoted_at         = models.DateTimeField(null=True, blank=True)
    paid_at           = models.DateTimeField(null=True, blank=True)
    ordered_at        = models.DateTimeField(null=True, blank=True)
    in_transit_at     = models.DateTimeField(null=True, blank=True)
    received_at       = models.DateTimeField(null=True, blank=True)
    shipped_at        = models.DateTimeField(null=True, blank=True)
    delivered_at      = models.DateTimeField(null=True, blank=True)
    cancelled_at      = models.DateTimeField(null=True, blank=True)
    created_at        = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "partner"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"{self.order_ref} ({self.partner.name})"

    @property
    def cost_subtotal_usd(self):
        return (self.china_cost_usd + self.shipping_usd).quantize(Decimal("0.01"))

    @property
    def margin_pct(self):
        if self.tenant_total_usd <= 0:
            return Decimal("0.00")
        return ((self.vendor_margin_usd / self.tenant_total_usd) * 100).quantize(Decimal("0.1"))


class LockedPrice(models.Model):
    """A permanent price contract for one tenant + partner + SKU.

    Created the first time a tenant pays a quote for a given SKU. From
    then on, identical SKUs from the same tenant routed to the same
    partner skip the quote step and use this price directly."""

    STATUS_CHOICES = [
        ("active",                    "Active"),
        ("renegotiation_requested",   "Renegotiation requested"),
        ("renegotiation_proposed",    "New price proposed"),
        ("retired",                   "Retired"),
    ]

    tenant            = models.ForeignKey(settings.AUTH_USER_MODEL,
                                          on_delete=models.CASCADE,
                                          related_name="locked_prices")
    partner           = models.ForeignKey(SourcingPartner,
                                          on_delete=models.CASCADE,
                                          related_name="locked_prices")
    sku               = models.CharField(max_length=120)
    product_name      = models.CharField(max_length=300, blank=True)
    product_image_url = models.URLField(blank=True)

    # Locked numbers
    china_cost_cny    = models.DecimalField(max_digits=10, decimal_places=2)
    china_cost_usd    = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_usd      = models.DecimalField(max_digits=8, decimal_places=2)
    vendor_margin_usd = models.DecimalField(max_digits=8, decimal_places=2)
    platform_fee_usd  = models.DecimalField(max_digits=8, decimal_places=2,
                                            default=Decimal("0.00"))
    locked_price_usd  = models.DecimalField(max_digits=10, decimal_places=2,
                                            help_text="Final price tenant pays per unit.")
    lead_days         = models.PositiveSmallIntegerField(default=7)
    auto_assign       = models.BooleanField(default=True,
                                            help_text="Future orders auto-route here without re-quoting.")

    # Renegotiation
    status            = models.CharField(max_length=30, choices=STATUS_CHOICES, default="active")
    pending_price_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                            null=True, blank=True,
                                            help_text="Vendor's proposed new price awaiting tenant decision.")
    pending_reason    = models.CharField(max_length=300, blank=True)
    pending_proposed_at = models.DateTimeField(null=True, blank=True)

    # Audit
    locked_via_order_ref = models.CharField(max_length=40, blank=True)
    locked_at          = models.DateTimeField(default=timezone.now)
    last_used_at       = models.DateTimeField(null=True, blank=True)
    times_used         = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-locked_at"]
        unique_together = [("tenant", "partner", "sku")]

    def __str__(self):
        return f"{self.sku} @ ${self.locked_price_usd} (locked w/ {self.partner.name})"


class PriceHistory(models.Model):
    """Append-only log of price changes on a LockedPrice."""
    locked     = models.ForeignKey(LockedPrice, on_delete=models.CASCADE,
                                   related_name="history")
    event      = models.CharField(max_length=40)  # "locked", "renegotiated", "rejected"
    old_price  = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    new_price  = models.DecimalField(max_digits=10, decimal_places=2)
    reason     = models.CharField(max_length=400, blank=True)
    acted_by   = models.CharField(max_length=80, blank=True)  # "tenant" / "vendor"
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class VendorStockItem(models.Model):
    """Inventory the partner maintains at their China warehouse FOR a tenant."""

    tenant      = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.CASCADE,
                                    related_name="vendor_stock_items")
    partner     = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                    related_name="stock_items")
    sku         = models.CharField(max_length=120)
    product_name = models.CharField(max_length=300, blank=True)
    image_url   = models.URLField(blank=True)
    qty_on_hand = models.IntegerField(default=0)
    qty_reserved = models.IntegerField(default=0)
    qty_inbound = models.IntegerField(default=0, help_text="Ordered from supplier but not yet received.")
    low_threshold = models.PositiveIntegerField(default=10)
    last_updated  = models.DateTimeField(auto_now=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["product_name"]
        unique_together = [("tenant", "partner", "sku")]

    def __str__(self):
        return f"{self.sku}: {self.qty_on_hand} ({self.partner.name})"

    @property
    def qty_available(self):
        return max(0, self.qty_on_hand - self.qty_reserved)

    @property
    def status(self):
        if self.qty_available <= 0:
            return "out"
        if self.qty_available < (self.low_threshold * 0.4):
            return "critical"
        if self.qty_available <= self.low_threshold:
            return "low"
        return "good"


class VendorPayment(models.Model):
    """Money tenant has paid a partner (settled, pending, or refunded)."""

    TYPE_CHOICES = [
        ("order",    "Order payment"),
        ("sample",   "Sample request"),
        ("refund",   "Refund"),
        ("dispute",  "Dispute resolution"),
        ("adjust",   "Manual adjustment"),
    ]
    STATUS_CHOICES = [
        ("pending",   "Pending"),
        ("settled",   "Settled"),
        ("refunded",  "Refunded"),
        ("failed",    "Failed"),
    ]
    METHOD_CHOICES = [
        ("wallet",    "Wallet"),
        ("stripe",    "Stripe"),
        ("paypal",    "PayPal"),
        ("wise",      "Wise"),
        ("bank",      "Bank Transfer"),
    ]

    tenant       = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.CASCADE,
                                     related_name="vendor_payments")
    partner      = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                     related_name="payments")
    related_order = models.ForeignKey(VendorOrder, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="payments")

    payment_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default="order")
    method       = models.CharField(max_length=12, choices=METHOD_CHOICES, default="wallet")
    amount_usd   = models.DecimalField(max_digits=10, decimal_places=2)
    fee_usd      = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    status       = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    reference    = models.CharField(max_length=120, blank=True)
    note         = models.CharField(max_length=300, blank=True)

    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)
    settled_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"${self.amount_usd} {self.payment_type} ({self.status})"


class VendorDocument(models.Model):
    """Shared document vault per tenant + partner."""

    CATEGORY_CHOICES = [
        ("spec",        "Product Spec"),
        ("sample",      "Sample Photos"),
        ("contract",    "Agreement / NDA"),
        ("invoice",     "Invoice"),
        ("shipping",    "Shipping / Customs"),
        ("certificate", "Certificate / Compliance"),
        ("other",       "Other"),
    ]

    tenant     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.CASCADE,
                                   related_name="vendor_documents")
    partner    = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                   related_name="documents")
    category   = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="spec")
    title      = models.CharField(max_length=200)
    file       = models.FileField(upload_to="vendor_docs/%Y/%m/", null=True, blank=True)
    external_url = models.URLField(blank=True)
    filename   = models.CharField(max_length=255, blank=True)
    filesize_bytes = models.PositiveBigIntegerField(default=0)
    description = models.TextField(blank=True)

    uploaded_by = models.CharField(max_length=40, default="tenant")  # tenant / vendor
    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class AutoRule(models.Model):
    """Tenant's automation rules per partner."""

    RULE_TYPE_CHOICES = [
        ("auto_route",     "Auto-route new orders to this vendor"),
        ("auto_pay",       "Auto-pay quotes under threshold"),
        ("auto_reorder",   "Auto-reorder when stock low"),
        ("quote_timeout",  "Reassign if no quote in N hours"),
        ("auto_renew_lock","Auto-approve small price renegotiations"),
    ]

    tenant     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.CASCADE,
                                   related_name="vendor_rules")
    partner    = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                   related_name="rules")
    rule_type  = models.CharField(max_length=30, choices=RULE_TYPE_CHOICES)
    is_active  = models.BooleanField(default=True)
    config     = models.JSONField(default=dict, blank=True,
                                  help_text="Rule-specific params (threshold, category, etc.)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class WorkspaceActivity(models.Model):
    """Append-only timeline events per tenant + partner."""

    tenant     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.CASCADE,
                                   related_name="vendor_activities")
    partner    = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                   related_name="activities")
    event_type = models.CharField(max_length=40)  # quote.received, payment.made, status.updated, etc.
    title      = models.CharField(max_length=200)
    detail     = models.TextField(blank=True)
    icon       = models.CharField(max_length=10, blank=True, default="•")
    actor      = models.CharField(max_length=40, default="system")
    related_order = models.ForeignKey(VendorOrder, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class PartnerReview(models.Model):
    """Tenant's review of a partner after order delivery."""

    tenant     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                   on_delete=models.CASCADE,
                                   related_name="partner_reviews")
    partner    = models.ForeignKey(SourcingPartner, on_delete=models.CASCADE,
                                   related_name="reviews")
    related_order = models.ForeignKey(VendorOrder, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    rating     = models.PositiveSmallIntegerField(default=5)  # 1-5 stars
    comm_score = models.PositiveSmallIntegerField(default=5)
    quality_score = models.PositiveSmallIntegerField(default=5)
    delivery_score = models.PositiveSmallIntegerField(default=5)
    comment    = models.TextField(blank=True)
    is_public  = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TenantWallet(models.Model):
    """One wallet per tenant — funds the Drop Sigma sourcing flow.

    Wallet is charged when the tenant approves a sourcing quote (single or
    bulk). Top-ups will eventually come from Stripe; for now a small trial
    balance is seeded on first overview load so tenants can exercise the
    Pay Now / Bulk-Pay flows end-to-end.
    """
    tenant       = models.OneToOneField(settings.AUTH_USER_MODEL,
                                        on_delete=models.CASCADE,
                                        related_name="sourcing_wallet")
    balance_usd  = models.DecimalField(max_digits=12, decimal_places=2,
                                       default=Decimal("0.00"))
    currency     = models.CharField(max_length=10, default="USD")
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant.username} · ${self.balance_usd}"


class WalletTransaction(models.Model):
    """Append-only ledger of every wallet movement."""

    KIND_CHOICES = [
        ("topup",  "Top-up"),
        ("charge", "Charge"),
        ("refund", "Refund"),
        ("adjust", "Manual adjustment"),
    ]

    wallet         = models.ForeignKey(TenantWallet, on_delete=models.CASCADE,
                                       related_name="transactions")
    kind           = models.CharField(max_length=10, choices=KIND_CHOICES)
    amount_usd     = models.DecimalField(max_digits=12, decimal_places=2)  # signed: +credit / -debit
    balance_after  = models.DecimalField(max_digits=12, decimal_places=2)
    related_order  = models.ForeignKey("orders.Order",
                                       on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name="wallet_transactions")
    reference      = models.CharField(max_length=120, blank=True)
    note           = models.CharField(max_length=255, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} ${self.amount_usd} · {self.wallet.tenant.username}"
