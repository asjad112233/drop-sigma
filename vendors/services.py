"""Vendor Pricing & Approval — automation engine + reminder logic.

Tenant resolution: `ProductVendorAssignment.store.user` is the tenant.
There is no `tenant` field on the assignment — the project resolves tenant
via `store.user` (the User that owns the Store).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from typing import Tuple

from django.db import transaction
from django.utils import timezone


# Hardcoded global v1 fallback if no per-vendor trust setting exists.
# (Per-vendor trust rows can override these via VendorTrustSetting.)
THRESHOLD_PCT = Decimal("10")
THRESHOLD_ABS = Decimal("5")

# SLA windows (days)
VENDOR_REMINDER_DAYS = 7
TENANT_ESCALATION_DAYS = 14


# ─── Helpers ────────────────────────────────────────────────────────────────

def get_tenant_for_assignment(assignment) -> "User | None":
    """Resolve tenant (the User that owns the store) for an assignment.
    Returns None if the store has no owner — caller must handle.
    """
    store = assignment.store
    return getattr(store, "user", None)


def compute_deltas(old_value, new_value) -> Tuple[Decimal, Decimal]:
    """Return (delta_abs, delta_pct). delta_pct=0 if old=0."""
    old = Decimal(old_value or 0)
    new = Decimal(new_value or 0)
    delta_abs = new - old
    if old == 0:
        delta_pct = Decimal("0")
    else:
        delta_pct = (delta_abs / old) * Decimal("100")
    # Quantize to 2dp for storage
    delta_abs = delta_abs.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    delta_pct = delta_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return delta_abs, delta_pct


# ─── Automation engine ──────────────────────────────────────────────────────

def evaluate_change_request(change_request) -> Tuple[str, str]:
    """Return (decision, automation_reason).

    decision ∈ {'auto_approved', 'pending'} — never auto-rejects.
    automation_reason: short audit string.

    Rules (in order):
      1) cost decrease (delta_abs < 0) → ALWAYS auto-approve
      2) no trust row OR trust_mode=False → pending (trust_off)
      3) reason not auto_eligible → pending (reason_not_auto_eligible)
      4) delta_pct > threshold_pct → pending (over_threshold_pct)
      5) delta_abs > threshold_abs → pending (over_threshold_abs)
      6) else → auto_approved (within_threshold)
    """
    # Local imports to avoid circulars at module load
    from .models import VendorTrustSetting, ReasonConfig

    # Rule 1: cost decrease → always auto-approve
    if change_request.delta_abs < 0:
        return ("auto_approved", "cost_decrease")

    # Rule 2: trust mode must be ON for the (tenant, vendor)
    tenant = get_tenant_for_assignment(change_request.quote.assignment)
    vendor = change_request.quote.assignment.vendor
    trust = None
    if tenant is not None:
        trust = VendorTrustSetting.objects.filter(tenant=tenant, vendor=vendor).first()
    if not trust or not trust.trust_mode:
        return ("pending", "trust_off")

    # Rule 3: reason must be auto-eligible
    reason = ReasonConfig.objects.filter(key=change_request.reason_key).first()
    if not reason or not reason.auto_eligible:
        return ("pending", "reason_not_auto_eligible")

    # Rule 4 + 5: both thresholds must be respected
    pct_limit = Decimal(trust.threshold_pct) if trust.threshold_pct is not None else THRESHOLD_PCT
    abs_limit = Decimal(trust.threshold_abs) if trust.threshold_abs is not None else THRESHOLD_ABS

    if Decimal(change_request.delta_pct) > pct_limit:
        return ("pending", "over_threshold_pct")
    if Decimal(change_request.delta_abs) > abs_limit:
        return ("pending", "over_threshold_abs")

    return ("auto_approved", "within_threshold")


# ─── Apply approved change → new locked snapshot ───────────────────────────

@transaction.atomic
def apply_approved_change(change_request, reviewed_by=None) -> "VendorQuote":
    """Apply an approved change to its quote:
      - Create a NEW VendorQuote with updated values
      - Mark old quote status='superseded'
      - Copy over shipping overrides (with the change applied to the relevant one)
      - Clear review_pending on the new active quote
      - Old quote remains intact so existing orders that snapshot it are unaffected

    Returns the new active VendorQuote.
    """
    from .models import VendorQuote, VendorShippingOverride, QuoteChangeRequest

    old_quote = change_request.quote
    # Build new values from old + change
    new_product_cost = Decimal(old_quote.product_cost)
    new_default_shipping = Decimal(old_quote.default_shipping)

    if change_request.change_type == QuoteChangeRequest.CHANGE_PRODUCT_COST:
        new_product_cost = Decimal(change_request.new_value)
    elif change_request.change_type == QuoteChangeRequest.CHANGE_DEFAULT_SHIPPING:
        new_default_shipping = Decimal(change_request.new_value)
    # country_* changes don't touch the base values — they're applied to overrides

    new_quote = VendorQuote.objects.create(
        assignment=old_quote.assignment,
        variant_id=old_quote.variant_id,
        currency=old_quote.currency,
        product_cost=new_product_cost,
        default_shipping=new_default_shipping,
        status=VendorQuote.STATUS_ACTIVE,
        review_pending=False,
        approved_at=timezone.now(),
        approved_by=reviewed_by,
    )

    # Copy over shipping overrides
    for ov in old_quote.overrides.filter(status=VendorShippingOverride.STATUS_ACTIVE):
        if (change_request.change_type == QuoteChangeRequest.CHANGE_COUNTRY_REMOVED
                and ov.country_code == change_request.country_code):
            # Remove this override — don't copy it
            continue
        new_cost = ov.shipping_cost
        if (change_request.change_type == QuoteChangeRequest.CHANGE_COUNTRY_SHIPPING
                and ov.country_code == change_request.country_code):
            new_cost = Decimal(change_request.new_value)
        VendorShippingOverride.objects.create(
            quote=new_quote, country_code=ov.country_code,
            shipping_cost=new_cost, status=VendorShippingOverride.STATUS_ACTIVE,
        )
    # Handle country_added: add the new override
    if change_request.change_type == QuoteChangeRequest.CHANGE_COUNTRY_ADDED:
        VendorShippingOverride.objects.get_or_create(
            quote=new_quote, country_code=change_request.country_code,
            defaults={"shipping_cost": Decimal(change_request.new_value),
                      "status": VendorShippingOverride.STATUS_ACTIVE},
        )

    # Mark the old one superseded
    old_quote.status = VendorQuote.STATUS_SUPERSEDED
    old_quote.review_pending = False
    old_quote.save(update_fields=["status", "review_pending"])

    return new_quote


# ─── SLA reminder cron logic ────────────────────────────────────────────────

def _has_active_quote(assignment) -> bool:
    """An assignment that has an active or pending quote = vendor has acted.
    Only assignments WITHOUT any quote are eligible for reminders.
    """
    from .models import VendorQuote
    return assignment.quotes.filter(
        status__in=[VendorQuote.STATUS_ACTIVE, VendorQuote.STATUS_PENDING]
    ).exists()


def send_quote_reminders():
    """Run daily.

    - Day 7+: email vendor (once) for assignments still awaiting a quote
    - Day 14+: email tenant (once) for the same set

    Idempotency tracked via QuoteReminderLog. Returns a dict of counts.
    """
    from .models import ProductVendorAssignment, QuoteReminderLog

    now = timezone.now()
    seven_days_ago = now - timedelta(days=VENDOR_REMINDER_DAYS)
    fourteen_days_ago = now - timedelta(days=TENANT_ESCALATION_DAYS)

    vendor_sent = 0
    tenant_sent = 0
    skipped = 0
    failed = 0

    # Candidate assignments: active, no quote yet, older than 7 days
    base_qs = (
        ProductVendorAssignment.objects
        .filter(is_active=True, created_at__lte=seven_days_ago)
        .select_related("vendor", "store", "store__user")
    )

    for assignment in base_qs:
        if _has_active_quote(assignment):
            skipped += 1
            continue

        # Vendor 7-day reminder
        already_v = QuoteReminderLog.objects.filter(
            assignment=assignment, kind=QuoteReminderLog.KIND_VENDOR_7D
        ).exists()
        if not already_v:
            try:
                _send_vendor_reminder(assignment)
                QuoteReminderLog.objects.create(
                    assignment=assignment, kind=QuoteReminderLog.KIND_VENDOR_7D
                )
                vendor_sent += 1
            except Exception:
                failed += 1

        # Tenant 14-day escalation
        if assignment.created_at <= fourteen_days_ago:
            already_t = QuoteReminderLog.objects.filter(
                assignment=assignment, kind=QuoteReminderLog.KIND_TENANT_14D
            ).exists()
            if not already_t:
                try:
                    _send_tenant_escalation(assignment)
                    QuoteReminderLog.objects.create(
                        assignment=assignment, kind=QuoteReminderLog.KIND_TENANT_14D
                    )
                    tenant_sent += 1
                except Exception:
                    failed += 1

    return {
        "vendor_reminders_sent": vendor_sent,
        "tenant_escalations_sent": tenant_sent,
        "skipped": skipped,
        "failed": failed,
    }


def _send_vendor_reminder(assignment):
    """Email the vendor that a quote is pending."""
    from emails.views import send_email_with_store_account
    vendor = assignment.vendor
    if not vendor or not vendor.email:
        return
    store = assignment.store
    subject = f"Quote pending: {assignment.product_name or assignment.product_id}"
    body = (
        f"Hi {vendor.name},\n\n"
        f"You were assigned a product for quoting on {assignment.created_at.date().isoformat()} "
        f"but we haven't received a quote yet.\n\n"
        f"Product: {assignment.product_name or assignment.product_id}\n"
        f"Store: {store.name}\n\n"
        f"Please log in to your Drop Sigma vendor portal and submit your quote.\n\n"
        f"— Drop Sigma"
    )
    send_email_with_store_account(store, vendor.email, subject, body)


def _send_tenant_escalation(assignment):
    """Email the tenant (store owner) about a stalled vendor."""
    from emails.views import send_email_with_store_account
    tenant = get_tenant_for_assignment(assignment)
    if not tenant or not tenant.email:
        return
    store = assignment.store
    vendor = assignment.vendor
    subject = (
        f"Vendor {vendor.name} hasn't quoted: "
        f"{assignment.product_name or assignment.product_id}"
    )
    body = (
        f"Hi,\n\n"
        f"It's been 14 days since you assigned this product to {vendor.name} "
        f"and they still haven't submitted a quote.\n\n"
        f"Product: {assignment.product_name or assignment.product_id}\n"
        f"Vendor: {vendor.name} <{vendor.email}>\n"
        f"Store: {store.name}\n\n"
        f"You may want to follow up with them directly or re-assign the product.\n\n"
        f"— Drop Sigma"
    )
    send_email_with_store_account(store, tenant.email, subject, body)
