"""Drop Sigma Operations Portal — internal back-office data models.

This app powers `/ops/`, the workspace where Drop Sigma's own specialist
team handles every tenant-assigned order: quoting, supplier linking,
procurement tracking, QC sign-off, shipping desk, and tenant chat.

Key relationship:
  - tenant assigns an `orders.Order` to Drop Sigma (status=pending_source)
  - ops portal picks it up, links a Supplier, sets prices → pending_payment
  - tenant pays via wallet (existing flow) → processing
  - ops portal tracks procurement, QC, ships, marks delivered
"""
from decimal import Decimal
from django.conf import settings
from django.db import models


# ───────────────────────────────────────────────────────────────────────
# SUPPLIER DATABASE
# Every supplier the ops team works with — once a SKU is linked to a
# supplier here, future orders for the same SKU auto-detect the source.
# ───────────────────────────────────────────────────────────────────────
class Supplier(models.Model):
    """Verified factory / wholesaler / sourcing partner."""

    TIER_CHOICES = [
        ("preferred", "Preferred · top supplier"),
        ("standard",  "Standard · trusted"),
        ("watch",     "Watch · monitor performance"),
        ("blocked",   "Blocked · do not use"),
    ]
    PAYMENT_TERMS_CHOICES = [
        ("prepaid",   "100% prepaid"),
        ("30_70",     "30% deposit · 70% before ship"),
        ("50_50",     "50% deposit · 50% on delivery"),
        ("net_30",    "Net-30 (credit)"),
        ("net_60",    "Net-60 (credit)"),
        ("custom",    "Custom"),
    ]

    name           = models.CharField(max_length=200)
    slug           = models.SlugField(unique=True)
    legal_name     = models.CharField(max_length=255, blank=True)
    tier           = models.CharField(max_length=12, choices=TIER_CHOICES,
                                       default="standard", db_index=True)

    # Location
    country        = models.CharField(max_length=100, blank=True)
    city           = models.CharField(max_length=100, blank=True)
    address        = models.CharField(max_length=400, blank=True)
    timezone       = models.CharField(max_length=64, blank=True,
                                       help_text="e.g. Asia/Shanghai")

    # Primary contact
    contact_name   = models.CharField(max_length=120, blank=True)
    contact_role   = models.CharField(max_length=80, blank=True,
                                       help_text="Sales rep, owner, etc.")
    email          = models.EmailField(blank=True)
    phone          = models.CharField(max_length=50, blank=True)
    whatsapp       = models.CharField(max_length=50, blank=True)
    wechat         = models.CharField(max_length=80, blank=True)
    languages      = models.JSONField(default=list, blank=True,
                                      help_text='e.g. ["English", "Mandarin"]')

    # Commercial
    website        = models.URLField(max_length=300, blank=True)
    alibaba_url    = models.URLField(max_length=400, blank=True)
    default_lead_days = models.PositiveIntegerField(default=10)
    moq            = models.PositiveIntegerField(default=1,
                                                  help_text="Minimum order qty (units)")
    payment_terms  = models.CharField(max_length=12, choices=PAYMENT_TERMS_CHOICES,
                                       default="30_70")
    payment_methods = models.JSONField(default=list, blank=True,
                                        help_text='e.g. ["T/T", "PayPal"]')
    currency       = models.CharField(max_length=10, default="USD")

    # Identification
    tax_id              = models.CharField(max_length=80, blank=True)
    business_license_no = models.CharField(max_length=80, blank=True)

    # Categories / specialties (free-form tags)
    specialties    = models.JSONField(default=list, blank=True,
                                       help_text='e.g. ["Apparel", "Cosmetics"]')

    # Internal scoring
    rating              = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("5.00"))
    quality_score       = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("5.00"))
    communication_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("5.00"))
    delivery_score      = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("5.00"))

    # Performance
    total_orders        = models.PositiveIntegerField(default=0)
    on_time_pct         = models.PositiveSmallIntegerField(default=95)
    defect_pct          = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("0.50"))

    # Visuals
    logo_url       = models.URLField(max_length=400, blank=True)
    cover_emoji    = models.CharField(max_length=4, default="🏭")
    cover_gradient_from = models.CharField(max_length=20, default="#6366f1")
    cover_gradient_to   = models.CharField(max_length=20, default="#a855f7")

    notes          = models.TextField(blank=True,
                                       help_text="Internal-only notes for ops team")
    is_active      = models.BooleanField(default=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-tier", "-rating", "name"]

    def __str__(self):
        return self.name

    @property
    def initials(self):
        words = [w for w in self.name.split() if w]
        if len(words) >= 2:
            return (words[0][0] + words[1][0]).upper()
        return (self.name[:2] or "?").upper()

    @property
    def gradient_css(self):
        return f"linear-gradient(135deg, {self.cover_gradient_from} 0%, {self.cover_gradient_to} 100%)"


class SupplierContact(models.Model):
    """Additional contacts at a supplier (multiple sales reps, owner, QC, etc.)"""
    supplier   = models.ForeignKey(Supplier, on_delete=models.CASCADE,
                                    related_name="contacts")
    name       = models.CharField(max_length=120)
    role       = models.CharField(max_length=80, blank=True)
    email      = models.EmailField(blank=True)
    phone      = models.CharField(max_length=50, blank=True)
    whatsapp   = models.CharField(max_length=50, blank=True)
    is_primary = models.BooleanField(default=False)
    notes      = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class SupplierImage(models.Model):
    """Photos of supplier's facility, sample products, certifications, etc."""
    KIND_CHOICES = [
        ("facility",      "Facility"),
        ("sample",        "Sample product"),
        ("certificate",   "Certification"),
        ("license",       "License doc"),
        ("other",         "Other"),
    ]
    supplier   = models.ForeignKey(Supplier, on_delete=models.CASCADE,
                                    related_name="images")
    kind       = models.CharField(max_length=12, choices=KIND_CHOICES, default="other")
    url        = models.URLField(max_length=600)
    caption    = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]


class SupplierProduct(models.Model):
    """A SKU we've sourced from a supplier with locked pricing.

    The KEY auto-detect mechanism: when a new tenant order comes in with a
    given `product_sku` (from Shopify/WC), the ops queue looks up this
    table — if a SupplierProduct exists for that SKU, the supplier link,
    cost, and lead time pre-fill automatically. The ops specialist just
    reviews + sends the quote.
    """
    supplier         = models.ForeignKey(Supplier, on_delete=models.CASCADE,
                                          related_name="products")
    sku              = models.CharField(max_length=120, db_index=True,
                                         help_text="The merchant's SKU (Shopify/WC)")
    product_name     = models.CharField(max_length=255)
    product_image_url = models.URLField(max_length=600, blank=True)

    variant_summary  = models.CharField(max_length=200, blank=True,
                                         help_text="e.g. 'Size: M-XL, Color: 5 options'")

    # Pricing (per unit, at MOQ)
    unit_cost_cny    = models.DecimalField(max_digits=10, decimal_places=2,
                                            null=True, blank=True)
    unit_cost_usd    = models.DecimalField(max_digits=10, decimal_places=2,
                                            help_text="Cost per unit in USD")
    shipping_unit_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                             default=Decimal("0.00"),
                                             help_text="Per-unit shipping (small parcel)")
    bulk_shipping_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                             null=True, blank=True,
                                             help_text="Optional: per-unit shipping at MOQ")
    moq              = models.PositiveIntegerField(default=1)
    lead_days        = models.PositiveIntegerField(default=10)

    suggested_tenant_price_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                                      null=True, blank=True)

    is_preferred     = models.BooleanField(default=True,
                                            help_text="Use this supplier first for this SKU")
    is_in_stock      = models.BooleanField(default=True)
    last_ordered_at  = models.DateTimeField(null=True, blank=True)
    times_ordered    = models.PositiveIntegerField(default=0)

    notes            = models.TextField(blank=True)
    created_by       = models.ForeignKey(settings.AUTH_USER_MODEL,
                                          null=True, blank=True,
                                          on_delete=models.SET_NULL,
                                          related_name="created_supplier_products")
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_preferred", "-times_ordered", "sku"]
        indexes = [
            models.Index(fields=["sku", "is_preferred"]),
            models.Index(fields=["supplier", "sku"]),
        ]

    def __str__(self):
        return f"{self.sku} @ {self.supplier.name}"


class SupplierProductVariant(models.Model):
    """Variant-level overrides (e.g. specific size/color of a product)."""
    product       = models.ForeignKey(SupplierProduct, on_delete=models.CASCADE,
                                       related_name="variants")
    variant       = models.CharField(max_length=200,
                                      help_text="e.g. 'M / Black' or 'Size: 50ml'")
    sku           = models.CharField(max_length=120, blank=True,
                                      help_text="Variant-specific SKU if any")
    unit_cost_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                         null=True, blank=True)
    is_in_stock   = models.BooleanField(default=True)


# ───────────────────────────────────────────────────────────────────────
# OPS TEAM & ASSIGNMENT
# ───────────────────────────────────────────────────────────────────────
class OpsRole(models.Model):
    """Role of a Drop Sigma team member (Sourcing Specialist, Procurement, QC, etc.)."""
    name       = models.CharField(max_length=80, unique=True)
    slug       = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    color      = models.CharField(max_length=20, default="#6366f1",
                                   help_text="Used for team-member chips")
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class OpsTeamMember(models.Model):
    """A Drop Sigma staff user — separate from tenants."""

    STATUS_CHOICES = [
        ("active",   "Active"),
        ("away",     "Away"),
        ("offline",  "Offline"),
        ("inactive", "Inactive"),
    ]

    user        = models.OneToOneField(settings.AUTH_USER_MODEL,
                                        on_delete=models.CASCADE,
                                        related_name="ops_profile")
    display_name = models.CharField(max_length=120, blank=True)
    role        = models.ForeignKey(OpsRole, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="members")
    title       = models.CharField(max_length=120, blank=True)
    avatar_url  = models.URLField(max_length=400, blank=True)
    avatar_emoji = models.CharField(max_length=4, default="👤")
    avatar_color = models.CharField(max_length=20, default="#6366f1")

    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    timezone    = models.CharField(max_length=64, default="Asia/Karachi")
    languages   = models.JSONField(default=list, blank=True)

    # ─── Dedicated Sourcing Manager profile fields ─────────────────────
    # Surfaced to tenant on the chat header as their assigned manager.
    bio              = models.TextField(blank=True,
                                         help_text="Short bio shown to tenants on chat header")
    specialties_text = models.CharField(max_length=255, blank=True,
                                         help_text="Comma-separated specialties: 'Apparel, Beauty, Home'")
    photo_url        = models.URLField(max_length=600, blank=True,
                                        help_text="Headshot URL — replaces avatar emoji on tenant chat")
    signature        = models.CharField(max_length=160, blank=True,
                                         help_text="Short signature line shown after messages")
    customer_facing  = models.BooleanField(default=True,
                                            help_text="Can be auto-assigned as a dedicated manager")
    typical_reply_min = models.PositiveSmallIntegerField(default=5,
                                                          help_text="Typical reply time in minutes (shown to tenant)")

    orders_handled  = models.PositiveIntegerField(default=0)
    avg_quote_hrs   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("4.0"))
    on_time_pct     = models.PositiveSmallIntegerField(default=95)

    is_manager  = models.BooleanField(default=False)
    can_assign  = models.BooleanField(default=True)
    can_quote   = models.BooleanField(default=True)

    joined_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_name", "user__username"]

    def __str__(self):
        return self.display_name or self.user.get_full_name() or self.user.username


class OpsAssignment(models.Model):
    """Which ops team member owns a given Order at a given stage."""

    STAGE_CHOICES = [
        ("sourcing",    "Sourcing"),
        ("procurement", "Procurement"),
        ("qc",          "QC"),
        ("shipping",    "Shipping"),
        ("support",     "Support"),
    ]

    order      = models.ForeignKey("orders.Order", on_delete=models.CASCADE,
                                    related_name="ops_assignments")
    member     = models.ForeignKey(OpsTeamMember, on_delete=models.SET_NULL,
                                    null=True, related_name="assignments")
    stage      = models.CharField(max_length=12, choices=STAGE_CHOICES,
                                   default="sourcing")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     null=True, blank=True,
                                     on_delete=models.SET_NULL,
                                     related_name="assignments_made")
    note       = models.CharField(max_length=255, blank=True)
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order", "stage", "is_active"]),
        ]


# ───────────────────────────────────────────────────────────────────────
# OPS-SIDE ORDER STATE
# ───────────────────────────────────────────────────────────────────────
class OrderOpsState(models.Model):
    """Internal ops-side state attached to a tenant Order."""

    QUOTE_STATUS_CHOICES = [
        ("none",         "Not yet quoted"),
        ("supplier_picked", "Supplier picked"),
        ("quote_sent",   "Quote sent to tenant"),
        ("approved",     "Approved · paid"),
        ("rejected",     "Rejected by tenant"),
    ]
    PROCURE_STATUS_CHOICES = [
        ("not_started", "Not started"),
        ("po_sent",     "PO sent to supplier"),
        ("producing",   "In production"),
        ("ready",       "Ready to ship"),
        ("delayed",     "Delayed"),
    ]
    QC_STATUS_CHOICES = [
        ("not_scheduled", "Not scheduled"),
        ("scheduled",     "Inspection scheduled"),
        ("passed",        "Passed"),
        ("failed",        "Failed"),
        ("waived",        "Waived"),
    ]

    order               = models.OneToOneField("orders.Order",
                                                on_delete=models.CASCADE,
                                                related_name="ops_state")
    supplier            = models.ForeignKey(Supplier, on_delete=models.SET_NULL,
                                             null=True, blank=True,
                                             related_name="orders_linked")
    supplier_product    = models.ForeignKey(SupplierProduct, on_delete=models.SET_NULL,
                                             null=True, blank=True,
                                             related_name="orders_used")
    supplier_auto_matched = models.BooleanField(default=False,
                                                 help_text="True if SKU was auto-linked")

    quote_status        = models.CharField(max_length=20,
                                            choices=QUOTE_STATUS_CHOICES,
                                            default="none")
    procurement_status  = models.CharField(max_length=20,
                                            choices=PROCURE_STATUS_CHOICES,
                                            default="not_started")
    qc_status           = models.CharField(max_length=20,
                                            choices=QC_STATUS_CHOICES,
                                            default="not_scheduled")

    supplier_cost_usd   = models.DecimalField(max_digits=10, decimal_places=2,
                                                null=True, blank=True)
    supplier_shipping_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                                  null=True, blank=True)
    drop_sigma_margin_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                                  default=Decimal("0.00"))

    tenant_product_usd  = models.DecimalField(max_digits=10, decimal_places=2,
                                                null=True, blank=True)
    tenant_shipping_usd = models.DecimalField(max_digits=10, decimal_places=2,
                                                null=True, blank=True)
    tenant_total_usd    = models.DecimalField(max_digits=10, decimal_places=2,
                                                null=True, blank=True)
    quoted_lead_days    = models.PositiveIntegerField(null=True, blank=True)

    # Per-line-item cost basis when the order has multiple SKUs.
    # Format: [{"idx": 0, "name": "...", "sku": "...", "variant": "...",
    #          "qty": 1, "unit_cost_usd": "20.00"}, ...]
    # Aggregated supplier_cost_usd = sum(unit_cost_usd * qty) across lines.
    line_item_costs     = models.JSONField(default=list, blank=True)
    margin_pct          = models.DecimalField(max_digits=6, decimal_places=2,
                                                default=Decimal("20.00"))

    quote_sent_at       = models.DateTimeField(null=True, blank=True)
    po_sent_at          = models.DateTimeField(null=True, blank=True)
    production_started_at = models.DateTimeField(null=True, blank=True)
    qc_scheduled_at     = models.DateTimeField(null=True, blank=True)
    qc_completed_at     = models.DateTimeField(null=True, blank=True)
    shipped_at          = models.DateTimeField(null=True, blank=True)
    delivered_at        = models.DateTimeField(null=True, blank=True)

    internal_notes      = models.TextField(blank=True)
    delay_reason        = models.CharField(max_length=255, blank=True)

    quoted_by           = models.ForeignKey(settings.AUTH_USER_MODEL,
                                             null=True, blank=True,
                                             on_delete=models.SET_NULL,
                                             related_name="quotes_sent")

    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["quote_status"]),
            models.Index(fields=["procurement_status"]),
        ]

    def __str__(self):
        return f"OpsState · {self.order_id}"


# ───────────────────────────────────────────────────────────────────────
# ACTIVITY TIMELINE
# ───────────────────────────────────────────────────────────────────────
class OpsActivity(models.Model):
    """Append-only audit log of every ops action on an order."""

    KIND_CHOICES = [
        ("note",         "Note"),
        ("assigned",     "Order assigned"),
        ("supplier",     "Supplier linked"),
        ("quote_sent",   "Quote sent"),
        ("paid",         "Order paid"),
        ("po_sent",      "PO sent to supplier"),
        ("production",   "Production update"),
        ("qc",           "QC inspection"),
        ("shipped",      "Shipment dispatched"),
        ("delivered",    "Order delivered"),
        ("delay",        "Delay reported"),
        ("issue",        "Issue raised"),
        ("cancelled",    "Order cancelled"),
        ("system",       "System event"),
    ]

    order      = models.ForeignKey("orders.Order", on_delete=models.CASCADE,
                                    related_name="ops_activity")
    kind       = models.CharField(max_length=20, choices=KIND_CHOICES)
    title      = models.CharField(max_length=200)
    detail     = models.TextField(blank=True)
    icon       = models.CharField(max_length=4, blank=True)

    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True,
                                    related_name="ops_activity_created")
    actor_label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}: {self.title}"


class OpsMessage(models.Model):
    """Internal team chat / order-level discussion (not shown to tenant)."""
    order       = models.ForeignKey("orders.Order", on_delete=models.CASCADE,
                                     related_name="ops_messages")
    author      = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.CASCADE,
                                     related_name="ops_messages_sent")
    body        = models.TextField()
    mentions    = models.JSONField(default=list, blank=True)
    is_internal = models.BooleanField(default=True,
                                       help_text="False = visible in tenant chat")
    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]


# ───────────────────────────────────────────────────────────────────────
# MASTER PRODUCT CATALOG
# Cached snapshot of every product across every tenant store, plus an
# aggregate view per-SKU that powers the cross-tenant intelligence UI.
# Refreshed by `python manage.py sync_master_catalog`.
# ───────────────────────────────────────────────────────────────────────
class TenantProductCache(models.Model):
    """Cached snapshot of a tenant's store product, refreshed periodically."""
    store           = models.ForeignKey(
        "stores.Store", on_delete=models.CASCADE,
        related_name="catalog_cache",
    )
    external_id     = models.CharField(max_length=120)  # Shopify/WC product ID
    sku             = models.CharField(max_length=120, db_index=True, blank=True)
    product_name    = models.CharField(max_length=400)
    image_url       = models.URLField(max_length=600, blank=True)
    retail_price    = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    currency        = models.CharField(max_length=10, default="USD")
    variant_count   = models.PositiveIntegerField(default=0)
    variants        = models.JSONField(default=list, blank=True)
    category        = models.CharField(max_length=120, blank=True)
    raw_data        = models.JSONField(default=dict, blank=True)
    cached_at       = models.DateTimeField(auto_now=True)
    is_active       = models.BooleanField(default=True)

    class Meta:
        ordering = ["-cached_at"]
        unique_together = [("store", "external_id")]
        indexes = [
            models.Index(fields=["sku"]),
            models.Index(fields=["store", "sku"]),
            models.Index(fields=["is_active", "-cached_at"]),
        ]

    def __str__(self):
        return f"{self.sku or '(no SKU)'} @ store#{self.store_id}"


class CrossTenantSKU(models.Model):
    """Auto-computed: aggregates one SKU across all tenant stores."""
    sku                = models.CharField(max_length=120, unique=True, db_index=True)
    sample_name        = models.CharField(max_length=400, blank=True)
    sample_image       = models.URLField(max_length=600, blank=True)
    tenant_count       = models.PositiveIntegerField(default=0)
    listing_count      = models.PositiveIntegerField(default=0)
    avg_retail_usd     = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    min_retail_usd     = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    max_retail_usd     = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    has_supplier       = models.BooleanField(default=False)
    supplier_count     = models.PositiveIntegerField(default=0)
    avg_supplier_cost  = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    last_computed_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-tenant_count", "-listing_count"]
        indexes = [
            models.Index(fields=["-tenant_count"]),
            models.Index(fields=["has_supplier"]),
        ]

    def __str__(self):
        return f"{self.sku} ({self.tenant_count} tenants)"


# ───────────────────────────────────────────────────────────────────────
# DEDICATED SOURCING MANAGER (per-tenant, lifelong)
# Each tenant is assigned ONE OpsTeamMember on first chat interaction.
# The assignment is permanent unless ops manually reassigns (manager
# left, on leave, tenant requested change).
# ───────────────────────────────────────────────────────────────────────
class TenantManagerAssignment(models.Model):
    """Permanent 1:1 mapping of tenant ↔ dedicated Sourcing Manager."""

    ASSIGNED_VIA_CHOICES = [
        ("auto_round_robin", "Auto-assigned (load balanced)"),
        ("manual_ops",       "Manually assigned by ops"),
        ("tenant_request",   "Tenant requested specific manager"),
        ("migrated",         "Migrated from previous manager"),
    ]

    tenant       = models.OneToOneField(settings.AUTH_USER_MODEL,
                                         on_delete=models.CASCADE,
                                         related_name="sourcing_manager_assignment")
    manager      = models.ForeignKey(OpsTeamMember,
                                      on_delete=models.PROTECT,
                                      related_name="dedicated_tenants")
    assigned_at  = models.DateTimeField(auto_now_add=True)
    assigned_via = models.CharField(max_length=20, choices=ASSIGNED_VIA_CHOICES,
                                     default="auto_round_robin")

    # Re-assignment history (when manager leaves, on leave, tenant request, etc.)
    previous_manager     = models.ForeignKey(OpsTeamMember, null=True, blank=True,
                                              on_delete=models.SET_NULL,
                                              related_name="past_tenants")
    reassigned_at        = models.DateTimeField(null=True, blank=True)
    reassignment_reason  = models.CharField(max_length=255, blank=True)

    welcome_message_sent = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["manager", "-assigned_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.username} → {self.manager}"

