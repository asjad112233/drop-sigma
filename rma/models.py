"""
Returns & Refunds (RMA) data model.

Tenant isolation: every RMA is owned by a Store (which is owned by a Django
User — the tenant). All queries MUST be filtered by store__user=request.user.
The model also carries an opaque UUID token used for unauthenticated
customer access to the public return-form & tracking pages.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

from stores.models import Store
from orders.models import Order


# ── Choices ─────────────────────────────────────────────────────────────────

RMA_STATUS_CHOICES = [
    ("pending",            "Pending Review"),
    ("approved",           "Approved"),
    ("rejected",           "Rejected"),
    ("tracking_submitted", "Tracking Submitted"),
    ("in_transit",         "In Transit"),
    ("received",           "Received"),
    ("refunded",           "Refunded"),
    ("resolved",           "Resolved"),
    ("canceled",           "Canceled"),
]

# Statuses where the customer can no longer take actions on the link.
RMA_TERMINAL_STATUSES = ("refunded", "resolved", "rejected", "canceled")

# Reason → (label, emoji, requires_photo, who_pays_shipping, auto_approve_eligible)
DEFAULT_REASONS = [
    {"key": "defective",     "label": "Defective / Not working",   "emoji": "⚠️", "photo": True,  "pays": "tenant",   "auto_eligible": True},
    {"key": "wrong_size",    "label": "Wrong size",                "emoji": "📏", "photo": False, "pays": "tenant",   "auto_eligible": False},
    {"key": "damaged",       "label": "Damaged in transit",        "emoji": "📦", "photo": True,  "pays": "tenant",   "auto_eligible": True},
    {"key": "not_described", "label": "Not as described",          "emoji": "🤔", "photo": True,  "pays": "customer", "auto_eligible": False},
    {"key": "changed_mind",  "label": "Changed my mind",           "emoji": "💭", "photo": False, "pays": "customer", "auto_eligible": False},
    {"key": "wrong_item",    "label": "Received wrong item",       "emoji": "🚫", "photo": True,  "pays": "tenant",   "auto_eligible": True},
]

REASON_KEYS = [r["key"] for r in DEFAULT_REASONS]
REASON_CHOICES = [(r["key"], r["label"]) for r in DEFAULT_REASONS]

SHIPPING_POLICY_CHOICES = [
    ("free",          "Free for customer (you pay)"),
    ("customer_pays", "Customer pays return shipping"),
    ("free_defects",  "Free for defects only"),
]


# ── Settings ────────────────────────────────────────────────────────────────

class RMASettings(models.Model):
    """Per-tenant configuration. One row per store."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="rma_settings")

    # Policy
    return_window_days     = models.PositiveIntegerField(default=30)
    restocking_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    shipping_policy        = models.CharField(max_length=20, choices=SHIPPING_POLICY_CHOICES, default="free")
    policy_text            = models.TextField(blank=True, default=(
        "We accept returns within 30 days of delivery for any unused items in "
        "original packaging. Defective items receive a full refund including "
        "original shipping. Custom or final-sale items are not eligible."
    ))

    # Return address (label generation uses this)
    return_address_line1 = models.CharField(max_length=200, blank=True, default="")
    return_address_line2 = models.CharField(max_length=200, blank=True, default="")
    return_city          = models.CharField(max_length=100, blank=True, default="")
    return_state         = models.CharField(max_length=100, blank=True, default="")
    return_postcode      = models.CharField(max_length=20,  blank=True, default="")
    return_country       = models.CharField(max_length=100, blank=True, default="US")

    # Auto-rules
    auto_approve_defects_under = models.DecimalField(max_digits=10, decimal_places=2, default=50)
    auto_approve_vip           = models.BooleanField(default=True)
    auto_flag_suspicious       = models.BooleanField(default=True)
    auto_generate_label        = models.BooleanField(default=True)

    # Enabled reason keys (subset of DEFAULT_REASONS)
    enabled_reasons = models.JSONField(default=list, blank=True)

    # Email automation
    auto_email_enabled = models.BooleanField(default=False)
    auto_email_subject = models.CharField(max_length=255, blank=True, default="Re: {{subject}} — Start your return")
    auto_email_body    = models.TextField(blank=True, default=(
        "Hi {{customer_name}},\n\n"
        "Thanks for reaching out — we're sorry to hear about the issue. "
        "To start your return, please open this short form and submit the details:\n\n"
        "{{return_link}}\n\n"
        "You'll get live status updates on the same link and can chat with us directly there.\n\n"
        "— {{store_name}} Support"
    ))

    # Triage: which refund-category emails the tenant has dismissed (not a real return)
    dismissed_email_ids = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"RMA Settings — {self.store.name}"

    @classmethod
    def for_store(cls, store):
        obj, created = cls.objects.get_or_create(store=store)
        if created and not obj.enabled_reasons:
            obj.enabled_reasons = REASON_KEYS
            obj.save(update_fields=["enabled_reasons"])
        return obj

    def reason_meta(self):
        """Return DEFAULT_REASONS filtered to only enabled keys."""
        enabled = self.enabled_reasons or REASON_KEYS
        return [r for r in DEFAULT_REASONS if r["key"] in enabled]


# ── RMA core ────────────────────────────────────────────────────────────────

def _new_token():
    return uuid.uuid4().hex


class RMA(models.Model):
    """A single return / refund case."""
    # tenant + order link
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="rmas")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="rmas",
                              null=True, blank=True)  # nullable to allow manual RMAs

    # identity
    rma_number = models.CharField(max_length=40, unique=True, db_index=True)
    token      = models.CharField(max_length=64, unique=True, default=_new_token, db_index=True)

    # customer (denormalized for quick lookup, even after order delete)
    customer_name  = models.CharField(max_length=200, blank=True, default="")
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=50, blank=True, default="")

    # status / lifecycle
    status        = models.CharField(max_length=20, choices=RMA_STATUS_CHOICES, default="pending", db_index=True)
    reason        = models.CharField(max_length=30, choices=REASON_CHOICES, blank=True, default="")
    customer_note = models.TextField(blank=True, default="")

    # decisions
    submitted_at = models.DateTimeField(auto_now_add=True)
    approved_at  = models.DateTimeField(null=True, blank=True)
    approved_by  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="rmas_approved")
    rejected_at  = models.DateTimeField(null=True, blank=True)
    reject_note  = models.TextField(blank=True, default="")
    received_at  = models.DateTimeField(null=True, blank=True)
    refunded_at  = models.DateTimeField(null=True, blank=True)

    # refund details
    refund_amount   = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refund_currency = models.CharField(max_length=10, default="usd")
    refund_shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # original shipping refunded
    restocking_fee  = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    stripe_refund_id     = models.CharField(max_length=120, blank=True, default="")
    stripe_charge_id     = models.CharField(max_length=120, blank=True, default="")
    refund_notes         = models.TextField(blank=True, default="")

    # shipping label (return) — tenant-side label sent TO customer
    return_label_url     = models.URLField(blank=True, default="")
    return_tracking_no   = models.CharField(max_length=120, blank=True, default="")
    return_carrier       = models.CharField(max_length=50,  blank=True, default="")

    # Customer-side tracking (after they ship the item back)
    customer_tracking_id     = models.CharField(max_length=120, blank=True, default="")
    customer_tracking_company= models.CharField(max_length=80,  blank=True, default="")
    customer_tracking_url    = models.URLField(max_length=500, blank=True, default="")
    customer_tracking_at     = models.DateTimeField(null=True, blank=True)

    # Resolved (final closed state)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="rmas_resolved")

    # Optional link back to the email that triggered this RMA (Phase 5)
    source_email_id = models.CharField(max_length=120, blank=True, default="", db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["store", "status"]),
            models.Index(fields=["store", "submitted_at"]),
        ]

    def __str__(self):
        return f"{self.rma_number} — {self.customer_name or self.customer_email}"

    # ── helpers ────────────────────────────────────────────────────────
    @classmethod
    def next_rma_number(cls, store):
        """Sequential RMA-2XXXX number scoped per store."""
        last = cls.objects.filter(store=store).order_by("-id").first()
        if last and last.rma_number.startswith("RMA-"):
            try:
                n = int(last.rma_number.split("-")[1])
                return f"RMA-{n+1}"
            except (ValueError, IndexError):
                pass
        return f"RMA-{2400 + cls.objects.filter(store=store).count() + 1}"

    @property
    def age_hours(self):
        return int((timezone.now() - self.submitted_at).total_seconds() // 3600)

    @property
    def age_label(self):
        h = self.age_hours
        if h < 1:
            return "just now"
        if h < 24:
            return f"{h}h"
        return f"{h // 24}d"

    @property
    def items_count(self):
        return sum((i.quantity for i in self.items.all()), 0)

    @property
    def items_total(self):
        return sum((i.unit_price * i.quantity for i in self.items.all()), Decimal("0"))

    @property
    def total_refund(self):
        """Item total + shipping refund − restocking fee."""
        return (self.items_total + (self.refund_shipping or 0) - (self.restocking_fee or 0))

    @property
    def reason_label(self):
        for r in DEFAULT_REASONS:
            if r["key"] == self.reason:
                return r["label"]
        return self.reason or "—"

    @property
    def reason_emoji(self):
        for r in DEFAULT_REASONS:
            if r["key"] == self.reason:
                return r["emoji"]
        return "📝"

    @property
    def is_terminal(self):
        return self.status in RMA_TERMINAL_STATUSES

    @property
    def can_customer_act(self):
        """Customer can submit tracking / chat / etc. only while link is live."""
        return self.status not in RMA_TERMINAL_STATUSES

    @property
    def can_show_tracking_button(self):
        """The customer-side 'Add Tracking Info' CTA is gated to status=approved."""
        return self.status == "approved"


class RMAItem(models.Model):
    """A specific item within the return."""
    rma             = models.ForeignKey(RMA, on_delete=models.CASCADE, related_name="items")
    product_name    = models.CharField(max_length=255)
    sku             = models.CharField(max_length=120, blank=True, default="")
    variant_label   = models.CharField(max_length=255, blank=True, default="")  # "Brown · 42mm"
    quantity        = models.PositiveIntegerField(default=1)
    unit_price      = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    image_url       = models.URLField(blank=True, default="")
    external_id     = models.CharField(max_length=120, blank=True, default="")

    def __str__(self):
        return f"{self.product_name} ×{self.quantity}"

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class RMAPhoto(models.Model):
    """Photo evidence uploaded by customer.

    Uses FileField (not ImageField) to avoid the Pillow runtime dependency.
    The customer-facing upload view validates content-type at request time.
    """
    rma         = models.ForeignKey(RMA, on_delete=models.CASCADE, related_name="photos")
    image       = models.FileField(upload_to="rma_photos/")
    caption     = models.CharField(max_length=255, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Photo for {self.rma.rma_number}"


class RMAEvent(models.Model):
    """Audit timeline entry."""
    EVENT_TYPES = [
        ("submitted",          "Submitted"),
        ("approved",           "Approved"),
        ("rejected",           "Rejected"),
        ("label_generated",    "Label Generated"),
        ("tracking_submitted", "Customer Submitted Tracking"),
        ("in_transit",         "In Transit"),
        ("received",           "Received"),
        ("refunded",           "Refunded"),
        ("resolved",           "Resolved"),
        ("message_sent",       "Message Sent"),
        ("note_added",         "Internal Note"),
        ("status_change",      "Status Change"),
    ]
    rma         = models.ForeignKey(RMA, on_delete=models.CASCADE, related_name="events")
    event_type  = models.CharField(max_length=30, choices=EVENT_TYPES)
    actor_label = models.CharField(max_length=120, blank=True, default="")  # "Sarah" or "You" or "System"
    actor_user  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    message     = models.TextField(blank=True, default="")
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.rma.rma_number} — {self.event_type}"


class RMAMessage(models.Model):
    """Customer ↔ tenant conversation (and internal team notes)."""
    DIRECTION_CHOICES = [
        ("customer", "From Customer"),
        ("tenant",   "From Tenant"),
        ("internal", "Internal Note"),
    ]
    rma          = models.ForeignKey(RMA, on_delete=models.CASCADE, related_name="messages")
    direction    = models.CharField(max_length=20, choices=DIRECTION_CHOICES, default="tenant")
    body         = models.TextField()
    sender_name  = models.CharField(max_length=120, blank=True, default="")
    sender_email = models.EmailField(blank=True, default="")
    sender_user  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.direction} — {self.body[:40]}"
