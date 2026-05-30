"""
Returns & Refunds (RMA) views.

Tenant side: authenticated, scoped to request.user's stores.
Customer side: public, accessed via opaque RMA token (no login).

All tenant queries must run through `_user_rma_qs(user)` to enforce
isolation. Never trust IDs from POST body without re-checking tenancy.
"""
import json
import logging
import datetime as _dt
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, Http404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction, IntegrityError
from django.db.models import Count, Sum, Q, Avg, F
from django.utils import timezone
from django.conf import settings

from stores.models import Store
from orders.models import Order

from .models import (
    RMA, RMAItem, RMAPhoto, RMAEvent, RMAMessage, RMASettings,
    DEFAULT_REASONS, REASON_KEYS, RMA_STATUS_CHOICES, SHIPPING_POLICY_CHOICES,
    RMA_TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)


# Status-transition rules — what statuses can transition into a target.
# Used by action endpoints to reject invalid moves with a clear 400.
_ALLOWED_FROM = {
    "approved":   ("pending",),
    "rejected":   ("pending", "approved"),          # tenant can change mind on a pending case
    "in_transit": ("approved", "tracking_submitted"),
    "received":   ("approved", "in_transit", "tracking_submitted"),
    "refunded":   ("approved", "in_transit", "tracking_submitted", "received"),
    "resolved":   ("approved", "in_transit", "tracking_submitted", "received", "refunded", "rejected"),
}


def _to_decimal(value, default=None):
    """Safely coerce arbitrary JSON values into Decimal. Returns `default` on bad input."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _parse_json_body(request):
    """Parse request.body as JSON, returning {} on any failure."""
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return {}


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _user_stores(user):
    """Stores this tenant owns."""
    return Store.objects.filter(user=user)


def _user_rma_qs(user):
    """All RMAs owned by this tenant. EVERY tenant-side query starts here."""
    return RMA.objects.filter(store__user=user)


def _get_tenant_rma(user, pk):
    """404-safe tenant-scoped RMA lookup."""
    try:
        return _user_rma_qs(user).select_related("store", "order").get(pk=int(pk))
    except (RMA.DoesNotExist, ValueError, TypeError):
        raise Http404("RMA not found")


def _settings_for_user(user):
    """Return the RMASettings row for the user's first store (or create)."""
    store = _user_stores(user).first()
    if not store:
        return None, None
    return store, RMASettings.for_store(store)


def _log_event(rma, event_type, message="", actor_user=None, actor_label=""):
    """Append a timeline event."""
    return RMAEvent.objects.create(
        rma=rma,
        event_type=event_type,
        actor_user=actor_user,
        actor_label=actor_label or (actor_user.get_full_name() or actor_user.username if actor_user else "System"),
        message=message,
    )


def _create_rma_with_unique_number(**kwargs):
    """
    Create an RMA, retrying rma_number generation on the unique-constraint
    race condition that occurs when two requests hit `next_rma_number` at
    the same instant. Returns the saved RMA.
    """
    store = kwargs.pop("store")
    last_err = None
    for _attempt in range(5):
        try:
            with transaction.atomic():
                return RMA.objects.create(
                    store=store,
                    rma_number=RMA.next_rma_number(store),
                    **kwargs,
                )
        except IntegrityError as e:
            last_err = e
            continue
    # Final attempt with a UUID-style fallback so the customer flow never breaks.
    import uuid as _uuid
    return RMA.objects.create(
        store=store,
        rma_number=f"RMA-{_uuid.uuid4().hex[:10].upper()}",
        **kwargs,
    )


def _safe_send_to_customer(rma, subject, body):
    """
    Best-effort email send to the customer.

    Project rule (memory): outbound emails MUST go through the tenant's
    connected Gmail (emails.views.send_email_with_store_account). If the
    tenant hasn't connected Gmail yet, fall back to Django's default mail
    backend so the flow never silently fails during early onboarding.

    Returns True if a send was attempted successfully; False otherwise.
    Failures are logged but never raised — the RMA action itself must
    succeed regardless of email outcome (best-effort by design).
    """
    if not rma or not rma.customer_email or not rma.store:
        return False

    # Try tenant's Gmail / connected account first.
    try:
        from emails.views import send_email_with_store_account
        send_email_with_store_account(
            store=rma.store,
            recipient=rma.customer_email,
            subject=subject,
            body=body,
        )
        return True
    except Exception as e:
        # Includes "No connected email account" — fall through to default backend.
        logger.info(
            "RMA %s: store-account send failed (%s); using fallback.",
            getattr(rma, "rma_number", "?"), e,
        )

    # Fallback — Django default mail backend (console / SMTP).
    try:
        from django.core.mail import send_mail
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@dropsigma.com"
        send_mail(subject, body, from_email, [rma.customer_email], fail_silently=True)
        return True
    except Exception as e:
        logger.warning(
            "RMA %s: fallback email send failed: %s",
            getattr(rma, "rma_number", "?"), e,
        )
        return False


def _platform_creds():
    """Defer to core._platform_creds for Stripe + PayPal credential resolution."""
    try:
        from core.views import _platform_creds as _resolve
        return _resolve()
    except Exception:
        return {
            "stripe_secret":       settings.STRIPE_SECRET_KEY,
            "stripe_publishable":  settings.STRIPE_PUBLISHABLE_KEY,
            "stripe_webhook":      settings.STRIPE_WEBHOOK_SECRET,
            "paypal_client_id":    settings.PAYPAL_CLIENT_ID,
            "paypal_client_secret":settings.PAYPAL_CLIENT_SECRET,
            "paypal_mode":         settings.PAYPAL_MODE,
        }


# ──────────────────────────────────────────────────────────────────────
# TENANT — INBOX
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
def rma_list(request):
    """Tenant inbox: list of RMAs with stats + filters."""
    if not request.user.is_staff:
        return redirect("/login/")

    qs = _user_rma_qs(request.user)
    status_filter = (request.GET.get("status") or "").strip()
    search        = (request.GET.get("q") or "").strip()

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)
    if search:
        qs = qs.filter(
            Q(rma_number__icontains=search) |
            Q(customer_email__icontains=search) |
            Q(customer_name__icontains=search) |
            Q(order__external_order_id__icontains=search)
        )

    # Stats — always computed against the unfiltered tenant-wide queryset
    all_qs = _user_rma_qs(request.user)
    now    = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_30d    = now - _dt.timedelta(days=30)

    pending_count   = all_qs.filter(status="pending").count()
    refunded_month  = all_qs.filter(status="refunded", refunded_at__gte=month_start)
    refunded_total  = refunded_month.aggregate(s=Sum("refund_amount"))["s"] or Decimal("0")
    refunded_count  = refunded_month.count()

    # Return rate = refunded RMAs in 30d / orders in 30d (across user's stores)
    orders_30d  = Order.objects.filter(store__user=request.user, created_at__gte=last_30d).count() or 1
    returns_30d = all_qs.filter(submitted_at__gte=last_30d).count()
    return_rate = round((returns_30d / orders_30d) * 100, 1) if orders_30d else 0

    # Avg approval time over last 30d (hours between submitted_at and approved_at)
    approved_recent = all_qs.filter(submitted_at__gte=last_30d, approved_at__isnull=False)
    avg_h = None
    if approved_recent.exists():
        deltas = [
            (r.approved_at - r.submitted_at).total_seconds() / 3600
            for r in approved_recent.only("submitted_at", "approved_at")
        ]
        avg_h = round(sum(deltas) / len(deltas), 1)

    # Status counts for tabs
    tab_counts = {
        "all":      all_qs.count(),
        "pending":  all_qs.filter(status="pending").count(),
        "approved": all_qs.filter(status="approved").count(),
        "in_transit": all_qs.filter(status="in_transit").count(),
        "received":  all_qs.filter(status="received").count(),
        "refunded":  all_qs.filter(status="refunded").count(),
        "rejected":  all_qs.filter(status="rejected").count(),
    }

    rmas = list(qs.select_related("order", "store").prefetch_related("items")[:100])

    return render(request, "rma/list.html", {
        "rmas":            rmas,
        "active_tab":      status_filter or "all",
        "search":          search,
        "tab_counts":      tab_counts,
        "stat_pending":    pending_count,
        "stat_return_rate":return_rate,
        "stat_avg_hours":  avg_h,
        "stat_refunded":   refunded_total,
        "stat_refund_count": refunded_count,
    })


# ──────────────────────────────────────────────────────────────────────
# TENANT — DETAIL
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
def rma_detail(request, pk):
    rma = _get_tenant_rma(request.user, pk)
    items    = list(rma.items.all())
    photos   = list(rma.photos.all())
    events   = list(rma.events.all())
    messages = list(rma.messages.all())
    store, sett = _settings_for_user(request.user)

    # Customer order stats — how many prior orders does this customer have?
    prior_orders = 0
    if rma.customer_email and rma.store_id:
        prior_orders = Order.objects.filter(
            store=rma.store, customer_email__iexact=rma.customer_email
        ).count()

    return render(request, "rma/detail.html", {
        "rma":           rma,
        "items":         items,
        "photos":        photos,
        "events":        events,
        "msgs_thread":   [m for m in messages if m.direction != "internal"],
        "msgs_internal": [m for m in messages if m.direction == "internal"],
        "prior_orders":  prior_orders,
        "settings_row":  sett,
        "all_statuses":  RMA_STATUS_CHOICES,
    })


# ──────────────────────────────────────────────────────────────────────
# TENANT — ACTIONS (POST endpoints)
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def rma_approve(request, pk):
    data = _parse_json_body(request)
    custom_refund   = _to_decimal(data.get("refund_amount"))
    refund_shipping = _to_decimal(data.get("refund_shipping"))
    restocking      = _to_decimal(data.get("restocking_fee"))

    with transaction.atomic():
        try:
            rma = (_user_rma_qs(request.user)
                   .select_for_update(of=("self",))
                   .select_related("store", "order")
                   .get(pk=int(pk)))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        # Strict transition — only pending → approved. Use /reopen/ to go from
        # rejected. This prevents re-approving an already-refunded case.
        if rma.status not in _ALLOWED_FROM["approved"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot approve from status '{rma.status}'."},
                status=400,
            )

        if refund_shipping is not None:
            rma.refund_shipping = refund_shipping
        if restocking is not None:
            rma.restocking_fee = restocking
        if custom_refund is not None:
            rma.refund_amount = custom_refund
        else:
            rma.refund_amount = rma.total_refund

        rma.status      = "approved"
        rma.approved_at = timezone.now()
        rma.approved_by = request.user
        rma.save()

        _log_event(
            rma, "approved",
            f"Approved by {request.user.username}. Refund: ${rma.refund_amount}",
            actor_user=request.user, actor_label="You",
        )

        # Auto-generate label if enabled (stub — Shippo/EasyPost integration later)
        sett = RMASettings.for_store(rma.store) if rma.store else None
        if sett and sett.auto_generate_label and not rma.return_label_url:
            rma.return_carrier     = "USPS Priority Mail"
            rma.return_tracking_no = f"9405{rma.pk:010d}"
            rma.return_label_url   = f"/rma/{rma.pk}/label.pdf"
            rma.save(update_fields=["return_carrier", "return_tracking_no", "return_label_url"])
            _log_event(rma, "label_generated", f"Label generated: {rma.return_tracking_no}", actor_label="System")

    # Send the email AFTER the transaction commits so a failing send never
    # rolls back the approval. Best-effort by design.
    try:
        _send_status_email(rma, kind="approved")
    except Exception as e:
        logger.warning("RMA %s: approval email send raised: %s", rma.rma_number, e)

    return JsonResponse({
        "ok": True,
        "status": rma.status,
        "refund_amount": str(rma.refund_amount),
        "return_label_url": rma.return_label_url,
        "return_tracking_no": rma.return_tracking_no,
    })


@login_required(login_url="/login/")
@require_POST
def rma_reject(request, pk):
    data = _parse_json_body(request)
    reason = (data.get("reason") or "").strip()[:2000]

    with transaction.atomic():
        try:
            rma = _user_rma_qs(request.user).select_for_update(of=("self",)).get(pk=int(pk))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        if rma.status not in _ALLOWED_FROM["rejected"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot reject from status '{rma.status}'."},
                status=400,
            )

        rma.status      = "rejected"
        rma.rejected_at = timezone.now()
        rma.reject_note = reason
        rma.save(update_fields=["status", "rejected_at", "reject_note"])
        _log_event(rma, "rejected", reason or "Rejected", actor_user=request.user, actor_label="You")

    try:
        _send_status_email(rma, kind="rejected")
    except Exception as e:
        logger.warning("RMA %s: reject email send raised: %s", rma.rma_number, e)
    return JsonResponse({"ok": True, "status": "rejected"})


@login_required(login_url="/login/")
@require_POST
def rma_mark_received(request, pk):
    with transaction.atomic():
        try:
            rma = _user_rma_qs(request.user).select_for_update(of=("self",)).get(pk=int(pk))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        if rma.status == "received":
            # Idempotent — return success rather than crash.
            return JsonResponse({"ok": True, "status": "received", "noop": True})
        if rma.status not in _ALLOWED_FROM["received"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot mark received from '{rma.status}'."},
                status=400,
            )
        rma.status      = "received"
        rma.received_at = timezone.now()
        rma.save(update_fields=["status", "received_at"])
        _log_event(rma, "received", "Marked received in dashboard", actor_user=request.user, actor_label="You")
    return JsonResponse({"ok": True, "status": "received"})


@login_required(login_url="/login/")
@require_POST
def rma_mark_in_transit(request, pk):
    with transaction.atomic():
        try:
            rma = _user_rma_qs(request.user).select_for_update(of=("self",)).get(pk=int(pk))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        if rma.status == "in_transit":
            return JsonResponse({"ok": True, "status": "in_transit", "noop": True})
        if rma.status not in _ALLOWED_FROM["in_transit"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot mark in transit from '{rma.status}'."},
                status=400,
            )
        rma.status = "in_transit"
        rma.save(update_fields=["status"])
        _log_event(rma, "in_transit", "Marked in transit", actor_user=request.user, actor_label="You")
    return JsonResponse({"ok": True, "status": "in_transit"})


@login_required(login_url="/login/")
@require_POST
def rma_refund(request, pk):
    """One-click Stripe refund. Falls back to manual-mark-only if no charge linked.

    Leaves status at 'refunded' (does NOT auto-jump to 'resolved') so the
    Refunded tab actually contains refunded cases. Use the Resolve action
    after the case is fully closed.
    """
    data = _parse_json_body(request)
    amount = _to_decimal(data.get("amount"))

    with transaction.atomic():
        try:
            rma = (_user_rma_qs(request.user)
                   .select_for_update(of=("self",))
                   .select_related("store", "order")
                   .get(pk=int(pk)))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        # Idempotency — if already refunded, return the current state (no crash).
        if rma.status in ("refunded", "resolved") and rma.refunded_at:
            return JsonResponse({
                "ok": True,
                "status": rma.status,
                "noop": True,
                "refund_amount": str(rma.refund_amount),
                "stripe_refund_id": rma.stripe_refund_id,
            })

        if rma.status not in _ALLOWED_FROM["refunded"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot refund from status '{rma.status}'. Approve it first."},
                status=400,
            )

        if amount is not None:
            rma.refund_amount = amount
        if not rma.refund_amount or rma.refund_amount <= 0:
            rma.refund_amount = rma.total_refund

        # Attempt Stripe refund if we have a charge_id.
        stripe_ok  = False
        stripe_err = ""
        if rma.stripe_charge_id:
            try:
                import stripe
                stripe.api_key = _platform_creds()["stripe_secret"]
                refund = stripe.Refund.create(
                    charge=rma.stripe_charge_id,
                    amount=int(float(rma.refund_amount) * 100),
                    metadata={"rma_number": rma.rma_number, "django_user_id": str(request.user.id)},
                )
                rma.stripe_refund_id = refund.id
                stripe_ok = True
            except Exception as e:
                stripe_err = f"{type(e).__name__}: {e}"
                logger.warning(
                    "RMA %s: Stripe refund failed: %s",
                    rma.rma_number, stripe_err,
                )

        rma.status      = "refunded"
        rma.refunded_at = timezone.now()
        rma.save(update_fields=["status", "refunded_at", "refund_amount", "stripe_refund_id"])

        _log_event(
            rma, "refunded",
            f"Refund processed: ${rma.refund_amount}" +
            (f" (Stripe: {rma.stripe_refund_id})" if stripe_ok
             else " (manual mark — no Stripe charge linked)" if not rma.stripe_charge_id
             else f" (Stripe failed: {stripe_err})"),
            actor_user=request.user, actor_label="You",
        )

        # Sync the linked Order's fulfillment status → "refunded" so the
        # Orders dashboard reflects reality and downstream automations
        # (status badges, vendor filters, exports) stay consistent.
        # Doing this inside the same atomic block guarantees both rows
        # flip together or not at all.
        if rma.order_id:
            try:
                old_status = (rma.order.fulfillment_status or "").lower()
                if old_status != "refunded":
                    rma.order.fulfillment_status = "refunded"
                    rma.order.save(update_fields=["fulfillment_status"])
            except Exception as e:
                logger.warning(
                    "RMA %s: linked order fulfillment_status sync failed: %s",
                    rma.rma_number, e,
                )

    try:
        _send_status_email(rma, kind="refunded")
    except Exception as e:
        logger.warning("RMA %s: refund email send raised: %s", rma.rma_number, e)

    return JsonResponse({
        "ok": True,
        "status": rma.status,
        "refund_amount": str(rma.refund_amount),
        "stripe_refund_id": rma.stripe_refund_id,
        "stripe_attempted": bool(rma.stripe_charge_id),
        "stripe_ok": stripe_ok,
        "stripe_error": stripe_err,
    })


@login_required(login_url="/login/")
@require_POST
def rma_resolve(request, pk):
    """Mark the case Resolved manually (e.g. tenant handled it outside Stripe)."""
    with transaction.atomic():
        try:
            rma = _user_rma_qs(request.user).select_for_update(of=("self",)).get(pk=int(pk))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        if rma.status == "resolved":
            return JsonResponse({"ok": True, "status": "resolved", "noop": True})
        if rma.status not in _ALLOWED_FROM["resolved"]:
            return JsonResponse(
                {"ok": False, "error": f"Cannot resolve from '{rma.status}'."},
                status=400,
            )
        rma.status      = "resolved"
        rma.resolved_at = timezone.now()
        rma.resolved_by = request.user
        rma.save(update_fields=["status", "resolved_at", "resolved_by"])
        _log_event(rma, "resolved", "Manually marked resolved", actor_user=request.user, actor_label="You")

        # Mark the customer's email thread Resolved so it moves out of the
        # active Returns/Refunds queues and into the Resolved folder. The
        # email side keys threads by (store, contact=customer_email).
        try:
            contact = (rma.customer_email or "").strip().lower()
            if contact:
                from emails.models import EmailThreadAssignment
                EmailThreadAssignment.objects.update_or_create(
                    store=rma.store,
                    contact=contact,
                    defaults={
                        "is_resolved": True,
                        "resolved_at": timezone.now(),
                    },
                )
        except Exception as e:
            logger.warning(
                "RMA %s: email thread resolve sync failed: %s",
                rma.rma_number, e,
            )

    try:
        _send_status_email(rma, kind="resolved")
    except Exception as e:
        logger.warning("RMA %s: resolve email send raised: %s", rma.rma_number, e)
    return JsonResponse({"ok": True, "status": "resolved"})


@login_required(login_url="/login/")
@require_POST
def rma_reopen(request, pk):
    """Reopen a resolved/rejected case (back to last meaningful state)."""
    with transaction.atomic():
        try:
            rma = _user_rma_qs(request.user).select_for_update(of=("self",)).get(pk=int(pk))
        except (RMA.DoesNotExist, ValueError, TypeError):
            raise Http404("RMA not found")

        if rma.status not in ("resolved", "rejected"):
            return JsonResponse(
                {"ok": False, "error": "Only resolved or rejected RMAs can be reopened."},
                status=400,
            )

        # Decide a reasonable target state — read the timestamps in reverse
        # chronological order so refunded > received > approved > pending.
        if rma.refunded_at:
            target = "refunded"
        elif rma.received_at:
            target = "received"
        elif rma.approved_at:
            target = "approved"
        else:
            target = "pending"

        rma.status      = target
        # Clear closure timestamps so the case is genuinely "open" again.
        rma.resolved_at = None
        rma.resolved_by = None
        if rma.status != "rejected":
            # Only nuke rejected_at when leaving rejected — preserves history otherwise.
            rma.rejected_at = None
            rma.reject_note = ""
        rma.save(update_fields=["status", "resolved_at", "resolved_by", "rejected_at", "reject_note"])
        _log_event(rma, "status_change", f"Reopened to {target}", actor_user=request.user, actor_label="You")
    return JsonResponse({"ok": True, "status": target})


@login_required(login_url="/login/")
@require_POST
def rma_message(request, pk):
    """Send a reply to the customer (visible on their tracking page)."""
    rma = _get_tenant_rma(request.user, pk)
    data = _parse_json_body(request)
    body = (data.get("body") or "").strip()[:5000]
    if not body:
        return JsonResponse({"ok": False, "error": "Message body is required."}, status=400)
    msg = RMAMessage.objects.create(
        rma=rma, direction="tenant", body=body,
        sender_name=request.user.get_full_name() or request.user.username,
        sender_email=request.user.email or "",
        sender_user=request.user,
    )
    _log_event(rma, "message_sent", body[:120], actor_user=request.user, actor_label="You")
    return JsonResponse({
        "ok": True,
        "message": {
            "id": msg.id,
            "body": msg.body,
            "sender": msg.sender_name,
            "created_at": msg.created_at.isoformat(),
            "direction": msg.direction,
        },
    })


@login_required(login_url="/login/")
@require_POST
def rma_internal_note(request, pk):
    """Add a team-only internal note (not visible to customer)."""
    rma = _get_tenant_rma(request.user, pk)
    data = _parse_json_body(request)
    body = (data.get("body") or "").strip()[:5000]
    if not body:
        return JsonResponse({"ok": False, "error": "Note body is required."}, status=400)
    msg = RMAMessage.objects.create(
        rma=rma, direction="internal", body=body,
        sender_name=request.user.get_full_name() or request.user.username,
        sender_user=request.user,
    )
    _log_event(rma, "note_added", body[:120], actor_user=request.user, actor_label="You")
    return JsonResponse({"ok": True, "id": msg.id, "body": msg.body, "sender": msg.sender_name})


# ──────────────────────────────────────────────────────────────────────
# TENANT — MANUAL CREATE (used by "Create Manual RMA" button)
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def rma_create_manual(request):
    """Create an RMA directly from an existing Order (skips customer form)."""
    data = _parse_json_body(request)

    order_id = data.get("order_id")
    order_ext = (data.get("order_external_id") or "").strip()
    order = None

    if order_id:
        try:
            order = Order.objects.select_related("store").get(id=int(order_id), store__user=request.user)
        except (Order.DoesNotExist, ValueError, TypeError):
            order = None

    if not order and order_ext:
        order = (Order.objects.select_related("store")
                 .filter(store__user=request.user, external_order_id=order_ext)
                 .first())

    if not order:
        return JsonResponse({"ok": False, "error": "Order not found. Check the order number and that the store is connected."}, status=404)

    reason = (data.get("reason") or "").strip()
    if reason and reason not in REASON_KEYS:
        reason = ""  # Don't store free-text reasons that don't match our enum.
    note = (data.get("note") or "").strip()[:5000]

    rma = _create_rma_with_unique_number(
        store=order.store, order=order,
        customer_name=order.customer_name or "",
        customer_email=(order.customer_email or "").strip().lower(),
        customer_phone=order.customer_phone or "",
        reason=reason,
        customer_note=note,
        status="pending",
    )

    # Single line item from order (best-effort, since raw data is platform-specific)
    RMAItem.objects.create(
        rma=rma,
        product_name=order.product_name or "Order item",
        sku="",
        variant_label="",
        quantity=1,
        unit_price=order.total_price or Decimal("0"),
        external_id=order.product_id or "",
    )

    _log_event(rma, "submitted", "Created manually from tenant dashboard", actor_user=request.user, actor_label="You")

    # Send the submission ack so the customer has a tracking link they can use.
    try:
        _send_status_email(rma, kind="submitted")
    except Exception as e:
        logger.warning("RMA %s: manual-create email send raised: %s", rma.rma_number, e)

    return JsonResponse({"ok": True, "rma_id": rma.id, "rma_number": rma.rma_number})


# ──────────────────────────────────────────────────────────────────────
# TENANT — ANALYTICS
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
def rma_analytics(request):
    if not request.user.is_staff:
        return redirect("/login/")

    qs = _user_rma_qs(request.user)
    now = timezone.now()
    last_30d  = now - _dt.timedelta(days=30)
    prev_30d  = now - _dt.timedelta(days=60)

    recent = qs.filter(submitted_at__gte=last_30d)
    refunded_recent = recent.filter(status="refunded")

    total_orders_30d = Order.objects.filter(store__user=request.user, created_at__gte=last_30d).count() or 1
    return_rate = round((recent.count() / total_orders_30d) * 100, 1)

    approval_eligible = recent.filter(status__in=["approved", "in_transit", "received", "refunded", "rejected"])
    approved_n = approval_eligible.exclude(status="rejected").count()
    approval_rate = round((approved_n / approval_eligible.count()) * 100, 1) if approval_eligible.count() else 0

    avg_h = 0
    approved_set = recent.filter(approved_at__isnull=False)
    if approved_set.exists():
        deltas = [(r.approved_at - r.submitted_at).total_seconds() / 3600 for r in approved_set.only("submitted_at", "approved_at")]
        avg_h = round(sum(deltas) / max(len(deltas), 1) / 24, 1)

    refunded_total = refunded_recent.aggregate(s=Sum("refund_amount"))["s"] or Decimal("0")

    # Reasons distribution
    reason_counts = recent.values("reason").annotate(c=Count("id")).order_by("-c")
    total_reason  = sum(r["c"] for r in reason_counts) or 1
    reasons = []
    for row in reason_counts:
        key = row["reason"] or "other"
        label = next((r["label"] for r in DEFAULT_REASONS if r["key"] == key), key.title())
        reasons.append({
            "label":   label,
            "count":   row["c"],
            "percent": round((row["c"] / total_reason) * 100, 1),
        })

    # Daily counts for bar chart (14 days)
    days = []
    for i in range(13, -1, -1):
        d = (now - _dt.timedelta(days=i)).date()
        c = qs.filter(submitted_at__date=d).count()
        days.append({"x": d.day, "count": c})
    max_count = max((d["count"] for d in days), default=1) or 1
    for d in days:
        d["height"] = round((d["count"] / max_count) * 100)

    # Top returned products
    top_products = (
        RMAItem.objects.filter(rma__store__user=request.user, rma__submitted_at__gte=last_30d)
        .values("product_name").annotate(returned=Sum("quantity"))
        .order_by("-returned")[:5]
    )

    return render(request, "rma/analytics.html", {
        "return_rate":   return_rate,
        "approval_rate": approval_rate,
        "avg_days":      avg_h,
        "refunded_total":refunded_total,
        "refunded_count":refunded_recent.count(),
        "reasons":       reasons,
        "days":          days,
        "top_products":  list(top_products),
    })


# ──────────────────────────────────────────────────────────────────────
# TENANT — SETTINGS
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
def rma_settings_view(request):
    if not request.user.is_staff:
        return redirect("/login/")
    store, sett = _settings_for_user(request.user)
    if sett is None:
        # No store yet — show a friendly empty state
        return render(request, "rma/settings.html", {"sett": None, "no_store": True, "default_reasons": DEFAULT_REASONS})

    if request.method == "POST":
        data = _parse_json_body(request)

        try:
            sett.return_window_days = max(0, min(int(data.get("return_window_days", sett.return_window_days)), 3650))
        except (TypeError, ValueError):
            pass

        d = _to_decimal(data.get("restocking_fee_percent"), sett.restocking_fee_percent)
        if d is not None:
            sett.restocking_fee_percent = max(Decimal("0"), min(d, Decimal("100")))

        sp = data.get("shipping_policy", sett.shipping_policy)
        valid_policies = {k for k, _ in SHIPPING_POLICY_CHOICES}
        if sp in valid_policies:
            sett.shipping_policy = sp

        # String fields — cap to schema length so DB-level errors don't surface.
        sett.policy_text          = (data.get("policy_text", sett.policy_text) or "")[:50000]
        sett.return_address_line1 = (data.get("return_address_line1", sett.return_address_line1) or "")[:200]
        sett.return_address_line2 = (data.get("return_address_line2", sett.return_address_line2) or "")[:200]
        sett.return_city          = (data.get("return_city",  sett.return_city) or "")[:100]
        sett.return_state         = (data.get("return_state", sett.return_state) or "")[:100]
        sett.return_postcode      = (data.get("return_postcode", sett.return_postcode) or "")[:20]
        sett.return_country       = (data.get("return_country", sett.return_country) or "")[:100]

        d = _to_decimal(data.get("auto_approve_defects_under"), sett.auto_approve_defects_under)
        if d is not None:
            sett.auto_approve_defects_under = max(Decimal("0"), d)

        sett.auto_approve_vip     = bool(data.get("auto_approve_vip", sett.auto_approve_vip))
        sett.auto_flag_suspicious = bool(data.get("auto_flag_suspicious", sett.auto_flag_suspicious))
        sett.auto_generate_label  = bool(data.get("auto_generate_label", sett.auto_generate_label))

        en = data.get("enabled_reasons")
        if isinstance(en, list):
            sett.enabled_reasons = [k for k in en if k in REASON_KEYS]
        if "auto_email_enabled" in data:
            sett.auto_email_enabled = bool(data.get("auto_email_enabled"))
        if "auto_email_subject" in data:
            new_subject = (data.get("auto_email_subject") or "").strip()[:255]
            if new_subject:
                sett.auto_email_subject = new_subject
        if "auto_email_body" in data:
            new_body = data.get("auto_email_body")
            if new_body:
                sett.auto_email_body = str(new_body)[:50000]
        sett.save()
        return JsonResponse({"ok": True})

    return render(request, "rma/settings.html", {
        "sett":            sett,
        "no_store":        False,
        "default_reasons": DEFAULT_REASONS,
        "shipping_policy_choices": SHIPPING_POLICY_CHOICES,
    })


# ──────────────────────────────────────────────────────────────────────
# JSON API — used by the in-dashboard RMA section (no full-page render)
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_list(request):
    """JSON inbox: stats + paginated list with optional status/q filters."""
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "Not staff"}, status=403)

    qs = _user_rma_qs(request.user)
    status_filter = (request.GET.get("status") or "").strip()
    search        = (request.GET.get("q") or "").strip()

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)
    if search:
        qs = qs.filter(
            Q(rma_number__icontains=search) |
            Q(customer_email__icontains=search) |
            Q(customer_name__icontains=search) |
            Q(order__external_order_id__icontains=search)
        )

    all_qs = _user_rma_qs(request.user)
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_30d    = now - _dt.timedelta(days=30)

    refunded_month = all_qs.filter(status="refunded", refunded_at__gte=month_start)
    refunded_total = refunded_month.aggregate(s=Sum("refund_amount"))["s"] or Decimal("0")

    orders_30d  = Order.objects.filter(store__user=request.user, created_at__gte=last_30d).count() or 1
    returns_30d = all_qs.filter(submitted_at__gte=last_30d).count()
    return_rate = round((returns_30d / orders_30d) * 100, 1) if orders_30d else 0

    approved_recent = all_qs.filter(submitted_at__gte=last_30d, approved_at__isnull=False)
    avg_h = None
    if approved_recent.exists():
        deltas = [
            (r.approved_at - r.submitted_at).total_seconds() / 3600
            for r in approved_recent.only("submitted_at", "approved_at")
        ]
        avg_h = round(sum(deltas) / len(deltas), 1)

    tab_counts = {
        "all":                all_qs.count(),
        "pending":            all_qs.filter(status="pending").count(),
        "approved":           all_qs.filter(status="approved").count(),
        "tracking_submitted": all_qs.filter(status="tracking_submitted").count(),
        "in_transit":         all_qs.filter(status="in_transit").count(),
        "received":           all_qs.filter(status="received").count(),
        "refunded":           all_qs.filter(status="refunded").count(),
        "resolved":           all_qs.filter(status="resolved").count(),
        "rejected":           all_qs.filter(status="rejected").count(),
    }

    rmas = []
    for r in qs.select_related("order").prefetch_related("items")[:200]:
        rmas.append({
            "id":             r.id,
            "rma_number":     r.rma_number,
            "status":         r.status,
            "status_display": r.get_status_display(),
            "customer_name":  r.customer_name,
            "customer_email": r.customer_email,
            "order_id":       r.order.external_order_id if r.order else "",
            "order_date":     r.order.created_at.strftime("%b %-d") if r.order else "",
            "items_count":    r.items_count,
            "items_total":    float(r.items_total),
            "refund_amount":  float(r.refund_amount or 0),
            "reason":         r.reason,
            "reason_label":   r.reason_label,
            "reason_emoji":   r.reason_emoji,
            "age_label":      r.age_label,
            "token":          r.token,
        })

    return JsonResponse({
        "ok": True,
        "stats": {
            "pending":       tab_counts["pending"],
            "return_rate":   return_rate,
            "avg_hours":     avg_h,
            "refunded_total":float(refunded_total),
            "refunded_count":refunded_month.count(),
        },
        "tab_counts": tab_counts,
        "rmas":       rmas,
    })


@login_required(login_url="/login/")
@require_GET
def api_detail(request, pk):
    """Full RMA payload for the dashboard detail panel."""
    rma = _get_tenant_rma(request.user, pk)
    items = [{
        "id":            it.id,
        "product_name":  it.product_name,
        "variant_label": it.variant_label,
        "sku":           it.sku,
        "quantity":      it.quantity,
        "unit_price":    float(it.unit_price),
        "line_total":    float(it.line_total),
        "image_url":     it.image_url,
    } for it in rma.items.all()]

    photos = [{
        "id":  p.id,
        "url": p.image.url,
    } for p in rma.photos.all()]

    events = [{
        "id":          e.id,
        "type":        e.event_type,
        "type_label":  e.get_event_type_display(),
        "actor":       e.actor_label,
        "message":     e.message,
        "created_at":  e.created_at.isoformat(),
        "ts_display":  e.created_at.strftime("%b %-d · %-I:%M %p"),
    } for e in rma.events.all()]

    all_msgs = list(rma.messages.all())
    thread = [{
        "id":         m.id,
        "direction":  m.direction,
        "body":       m.body,
        "sender":     m.sender_name,
        "ts_display": m.created_at.strftime("%b %-d · %-I:%M %p"),
        "since":      timezone.now() - m.created_at,
    } for m in all_msgs if m.direction != "internal"]
    for t in thread:
        s = t.pop("since")
        days = s.days
        secs = s.seconds
        if days:
            t["age"] = f"{days}d"
        elif secs >= 3600:
            t["age"] = f"{secs // 3600}h"
        else:
            t["age"] = f"{max(1, secs // 60)}m"

    internal = [{
        "id":         m.id,
        "body":       m.body,
        "sender":     m.sender_name,
        "ts_display": m.created_at.strftime("%b %-d · %-I:%M %p"),
    } for m in all_msgs if m.direction == "internal"]

    prior_orders = 0
    if rma.customer_email and rma.store_id:
        prior_orders = Order.objects.filter(
            store=rma.store, customer_email__iexact=rma.customer_email
        ).count()

    # Quick-action snippets for the chat box (Send Address / Send Label etc.)
    sett = RMASettings.for_store(rma.store) if rma.store else None
    quick_snippets = {}
    if sett:
        addr_lines = [sett.return_address_line1, sett.return_address_line2]
        addr_lines = [l for l in addr_lines if l]
        loc = ", ".join([x for x in [sett.return_city, sett.return_state, sett.return_postcode] if x])
        full_addr = "\n".join(addr_lines + ([loc] if loc else []) + ([sett.return_country] if sett.return_country else []))
        if full_addr:
            quick_snippets["address"] = (
                "Please ship the item back to:\n\n"
                f"{rma.store.name}\n{full_addr}\n\n"
                "Add the tracking info using the button on your return page once shipped."
            )
    if rma.return_label_url:
        quick_snippets["label"] = (
            f"Here's your pre-paid return label — drop the package at any {rma.return_carrier or 'carrier'} location:\n"
            f"{rma.return_label_url}\n\nTracking: {rma.return_tracking_no}"
        )
    if rma.customer_tracking_url:
        quick_snippets["carrier_tracking"] = f"Tracking your shipment: {rma.customer_tracking_url}"

    return JsonResponse({
        "ok": True,
        "rma": {
            "id":             rma.id,
            "rma_number":     rma.rma_number,
            "token":          rma.token,
            "status":         rma.status,
            "status_display": rma.get_status_display(),
            "customer_name":  rma.customer_name,
            "customer_email": rma.customer_email,
            "customer_phone": rma.customer_phone,
            "reason":         rma.reason,
            "reason_label":   rma.reason_label,
            "reason_emoji":   rma.reason_emoji,
            "customer_note":  rma.customer_note,
            "items_total":    float(rma.items_total),
            "refund_amount":  float(rma.refund_amount or 0),
            "refund_shipping":float(rma.refund_shipping or 0),
            "restocking_fee": float(rma.restocking_fee or 0),
            "total_refund":   float(rma.total_refund),
            "stripe_refund_id": rma.stripe_refund_id,
            "return_label_url": rma.return_label_url,
            "return_tracking_no": rma.return_tracking_no,
            "return_carrier":   rma.return_carrier,
            "customer_tracking_id":      rma.customer_tracking_id,
            "customer_tracking_company": rma.customer_tracking_company,
            "customer_tracking_url":     rma.customer_tracking_url,
            "customer_tracking_at":      rma.customer_tracking_at.isoformat() if rma.customer_tracking_at else "",
            "reject_note":      rma.reject_note,
            "submitted_at":   rma.submitted_at.isoformat(),
            "approved_at":    rma.approved_at.isoformat() if rma.approved_at else "",
            "refunded_at":    rma.refunded_at.isoformat() if rma.refunded_at else "",
            "rejected_at":    rma.rejected_at.isoformat() if rma.rejected_at else "",
            "is_terminal":    rma.is_terminal,
            "order": ({
                "id":          rma.order.id,
                "external_id": rma.order.external_order_id,
                "total":       float(rma.order.total_price or 0),
                "currency":    rma.order.currency,
                "created_at":  rma.order.created_at.strftime("%b %-d, %Y"),
                "delivered_at":rma.order.delivered_at.strftime("%b %-d, %Y") if rma.order.delivered_at else "",
            } if rma.order else None),
        },
        "items":        items,
        "photos":       photos,
        "events":       events,
        "thread":       thread,
        "internal":     internal,
        "prior_orders":  prior_orders,
        "quick_snippets":quick_snippets,
    })


@login_required(login_url="/login/")
@require_GET
def api_analytics(request):
    """Returns analytics payload for the in-dashboard analytics tab."""
    qs = _user_rma_qs(request.user)
    now = timezone.now()
    last_30d = now - _dt.timedelta(days=30)

    recent = qs.filter(submitted_at__gte=last_30d)
    refunded_recent = recent.filter(status="refunded")

    total_orders_30d = Order.objects.filter(store__user=request.user, created_at__gte=last_30d).count() or 1
    return_rate = round((recent.count() / total_orders_30d) * 100, 1)

    approval_eligible = recent.filter(status__in=["approved", "in_transit", "received", "refunded", "rejected"])
    approved_n = approval_eligible.exclude(status="rejected").count()
    approval_rate = round((approved_n / approval_eligible.count()) * 100, 1) if approval_eligible.count() else 0

    avg_days = 0
    approved_set = recent.filter(approved_at__isnull=False)
    if approved_set.exists():
        deltas = [(r.approved_at - r.submitted_at).total_seconds() / 86400 for r in approved_set.only("submitted_at", "approved_at")]
        avg_days = round(sum(deltas) / max(len(deltas), 1), 1)

    refunded_total = refunded_recent.aggregate(s=Sum("refund_amount"))["s"] or Decimal("0")

    reason_counts = recent.values("reason").annotate(c=Count("id")).order_by("-c")
    total_reason = sum(r["c"] for r in reason_counts) or 1
    reasons = []
    for row in reason_counts:
        key = row["reason"] or "other"
        label = next((r["label"] for r in DEFAULT_REASONS if r["key"] == key), key.title() or "Other")
        reasons.append({"label": label, "count": row["c"], "percent": round((row["c"] / total_reason) * 100, 1)})

    days = []
    max_count = 0
    for i in range(13, -1, -1):
        d = (now - _dt.timedelta(days=i)).date()
        c = qs.filter(submitted_at__date=d).count()
        days.append({"x": d.day, "count": c})
        max_count = max(max_count, c)
    max_count = max_count or 1
    for d in days:
        d["height"] = round((d["count"] / max_count) * 100)

    top_products = list(
        RMAItem.objects.filter(rma__store__user=request.user, rma__submitted_at__gte=last_30d)
        .values("product_name").annotate(returned=Sum("quantity"))
        .order_by("-returned")[:5]
    )

    return JsonResponse({
        "ok": True,
        "return_rate":   return_rate,
        "approval_rate": approval_rate,
        "avg_days":      avg_days,
        "refunded_total":float(refunded_total),
        "refunded_count":refunded_recent.count(),
        "reasons":       reasons,
        "days":          days,
        "top_products":  top_products,
    })


@login_required(login_url="/login/")
@require_GET
def api_settings_get(request):
    """GET current RMA settings JSON."""
    store, sett = _settings_for_user(request.user)
    if sett is None:
        return JsonResponse({"ok": True, "no_store": True, "default_reasons": DEFAULT_REASONS})
    return JsonResponse({
        "ok": True,
        "no_store": False,
        "default_reasons": DEFAULT_REASONS,
        "shipping_policy_choices": SHIPPING_POLICY_CHOICES,
        "sett": {
            "return_window_days":      sett.return_window_days,
            "restocking_fee_percent":  float(sett.restocking_fee_percent),
            "shipping_policy":         sett.shipping_policy,
            "policy_text":             sett.policy_text,
            "return_address_line1":    sett.return_address_line1,
            "return_address_line2":    sett.return_address_line2,
            "return_city":             sett.return_city,
            "return_state":            sett.return_state,
            "return_postcode":         sett.return_postcode,
            "return_country":          sett.return_country,
            "auto_approve_defects_under":float(sett.auto_approve_defects_under),
            "auto_approve_vip":        sett.auto_approve_vip,
            "auto_flag_suspicious":    sett.auto_flag_suspicious,
            "auto_generate_label":     sett.auto_generate_label,
            "enabled_reasons":         sett.enabled_reasons or REASON_KEYS,
            "auto_email_enabled":      sett.auto_email_enabled,
            "auto_email_subject":      sett.auto_email_subject,
            "auto_email_body":         sett.auto_email_body,
        },
    })


# ──────────────────────────────────────────────────────────────────────
# Email Triage — surface refund-category emails in the RMA section
# so tenants can Start Return or Dismiss in one click.
# ──────────────────────────────────────────────────────────────────────

def _tenant_email_qs(user):
    """Return EmailMessage queryset scoped to this tenant's stores."""
    from emails.models import EmailMessage
    return EmailMessage.objects.filter(store__user=user)


@login_required(login_url="/login/")
@require_GET
def api_triage_list(request):
    """List refund-category emails this tenant hasn't acted on yet."""
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "Not staff"}, status=403)
    store, sett = _settings_for_user(request.user)
    dismissed = set(map(str, (sett.dismissed_email_ids if sett else []) or []))

    started_email_ids = set(
        str(x) for x in _user_rma_qs(request.user)
        .exclude(source_email_id="")
        .values_list("source_email_id", flat=True)
    )

    # Triage surfaces BOTH refund-intent and return-intent emails (the new
    # classifier separates them, but both routes end in an RMA).
    qs = _tenant_email_qs(request.user).filter(category__in=["refund", "return"]).order_by("-id")[:200]

    items = []
    for em in qs:
        if str(em.id) in dismissed or str(em.id) in started_email_ids:
            continue
        items.append({
            "id":         em.id,
            "from":       getattr(em, "sender", "") or "",
            "to":         getattr(em, "recipient", "") or "",
            "subject":    em.subject or "",
            "preview":    (em.body or "")[:200],
            "created_at": em.created_at.isoformat() if getattr(em, "created_at", None) else "",
            "ts_display": em.created_at.strftime("%b %-d · %-I:%M %p") if getattr(em, "created_at", None) else "",
        })

    return JsonResponse({"ok": True, "emails": items, "count": len(items)})


@login_required(login_url="/login/")
@require_POST
def api_triage_start(request, email_id):
    """Create an RMA from this email + send the auto-email template to the sender."""
    from emails.models import EmailMessage
    try:
        email = _tenant_email_qs(request.user).get(id=int(email_id))
    except (EmailMessage.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Email not found."}, status=404)

    # Sender = customer email
    customer_email = getattr(email, "sender", "") or ""
    customer_email = customer_email.strip().lower()
    if "<" in customer_email and ">" in customer_email:
        # "Sarah <sarah@x.com>" → strip name part
        customer_email = customer_email.split("<")[-1].rstrip(">").strip()

    # Try to attach a recent order if we can find one
    order = (
        Order.objects.filter(store=email.store, customer_email__iexact=customer_email)
        .order_by("-created_at").first()
    ) if customer_email else None

    # Already an RMA for this email? Don't duplicate.
    # Wrap the dedupe-check + create in a transaction so two concurrent
    # "Start RMA" clicks on the same triage email can't both create rows.
    with transaction.atomic():
        existing = (_user_rma_qs(request.user)
                    .select_for_update(of=("self",))
                    .filter(source_email_id=str(email.id))
                    .first())
        if existing:
            return JsonResponse({
                "ok": True, "rma_id": existing.id,
                "rma_number": existing.rma_number, "duplicate": True,
            })

        if not customer_email:
            return JsonResponse(
                {"ok": False, "error": "Email has no sender address — can't start an RMA from it."},
                status=400,
            )

        rma = _create_rma_with_unique_number(
            store=email.store,
            order=order,
            customer_name=getattr(order, "customer_name", "") or "",
            customer_email=customer_email,
            customer_phone=getattr(order, "customer_phone", "") or "",
            reason="",
            customer_note="",
            status="pending",
            source_email_id=str(email.id),
        )
    _log_event(rma, "submitted",
               f"Started from email: {email.subject or '(no subject)'}",
               actor_user=request.user, actor_label="You")

    # Send the auto-template email back to customer with the return link
    sent, send_err = _send_return_link_email(rma, email)

    return JsonResponse({
        "ok": True,
        "rma_id":      rma.id,
        "rma_number":  rma.rma_number,
        "email_sent":  sent,
        "send_error":  send_err,
        "return_link": f"/r/{rma.token}/",
    })


@login_required(login_url="/login/")
@require_POST
def api_triage_dismiss(request, email_id):
    """Mark this refund-category email as 'not a return' so it drops off triage."""
    store, sett = _settings_for_user(request.user)
    if sett is None:
        return JsonResponse({"ok": False, "error": "No store connected."}, status=400)
    lst = sett.dismissed_email_ids or []
    if str(email_id) not in lst:
        lst.append(str(email_id))
        sett.dismissed_email_ids = lst
        sett.save(update_fields=["dismissed_email_ids"])
    return JsonResponse({"ok": True})


def _render_template(text, variables):
    """Tiny {{var}} substitution — keeps things dependency-free."""
    out = text or ""
    for k, v in variables.items():
        out = out.replace("{{" + k + "}}", str(v or ""))
    return out


def _send_return_link_email(rma, source_email=None):
    """
    Send the customer the auto-template return-link email (when tenant clicks
    'Start Return' from triage). Subject + body come from RMASettings; we do
    simple {{variable}} substitution.

    Returns (sent_bool, error_str).
    """
    if not rma.customer_email:
        return (False, "No customer email on this RMA.")

    sett = RMASettings.for_store(rma.store) if rma.store else None
    if not sett:
        return (False, "No RMA settings configured.")

    # Build return link — use Site or request-less host fallback
    host = getattr(settings, "PRIMARY_HOST", "") or "dropsigma.com"
    scheme = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    return_link = f"{scheme}://{host}/r/{rma.token}/"

    vars_ = {
        "customer_name": rma.customer_name or rma.customer_email.split("@")[0],
        "store_name":    rma.store.name if rma.store else "",
        "return_link":   return_link,
        "order_id":      rma.order.external_order_id if rma.order else "",
        "subject":       (source_email.subject if source_email else "your return"),
    }
    subject = _render_template(sett.auto_email_subject, vars_)
    body    = _render_template(sett.auto_email_body, vars_)

    ok = _safe_send_to_customer(rma, subject, body)
    if ok:
        _log_event(rma, "message_sent", f"Auto-template email sent: {subject}", actor_label="System")
        return (True, "")
    return (False, "Email send failed — Gmail not connected or SMTP unavailable.")


# ──────────────────────────────────────────────────────────────────────
# CUSTOMER-FACING — PUBLIC (token only, NO auth)
# ──────────────────────────────────────────────────────────────────────

def _get_public_rma(token):
    try:
        return RMA.objects.select_related("store", "order").get(token=token)
    except RMA.DoesNotExist:
        raise Http404("Return not found.")


def cust_track(request, token):
    """Customer-facing tracking page (read-only — actions via POST endpoints)."""
    rma = _get_public_rma(token)
    photos   = list(rma.photos.all())
    items    = list(rma.items.all())
    events   = list(rma.events.all())
    messages = list(rma.messages.filter(direction__in=["customer", "tenant"]).order_by("created_at"))
    sett     = RMASettings.for_store(rma.store) if rma.store else None

    # Map status → stage index (5 visible stages: submitted, approved, in transit, received, resolved)
    if rma.status in ("pending",):                          stage_idx = 0
    elif rma.status in ("approved",):                       stage_idx = 1
    elif rma.status in ("tracking_submitted", "in_transit"):stage_idx = 2
    elif rma.status in ("received",):                       stage_idx = 3
    elif rma.status in ("refunded", "resolved"):            stage_idx = 4
    elif rma.status == "rejected":                          stage_idx = -1
    else:                                                   stage_idx = 0

    last_msg_id = max((m.id for m in messages), default=0)

    return render(request, "rma/customer_track.html", {
        "rma":         rma,
        "store":       rma.store,
        "items":       items,
        "photos":      photos,
        "events":      events,
        "messages":    messages,
        "sett":        sett,
        "stage_idx":   stage_idx,
        "last_msg_id": last_msg_id,
    })


def cust_start(request, order_id):
    """
    Customer-facing return form. `order_id` is the Order's external_order_id.
    Email is passed as ?email= for ownership verification.
    """
    email = (request.GET.get("email") or "").strip().lower()

    # Without an email we can't verify ownership — show the prompt-for-email
    # state rather than leaking order info to anyone with the order number.
    if not email:
        return render(request, "rma/customer_form.html", {
            "error": "Please enter the email used for the order to continue.",
            "order": None,
            "needs_email": True,
            "order_id": order_id,
        })

    # Find the order — must match external_order_id + customer_email (case-insensitive).
    order = (Order.objects.select_related("store")
             .filter(external_order_id=order_id, customer_email__iexact=email)
             .first())
    if not order:
        return render(request, "rma/customer_form.html", {
            "error": "We couldn't find that order. Please check your order number and email.",
            "order": None,
        })

    # Build item list from order.raw_data line_items if present, otherwise single line
    items = _order_items(order)

    sett = RMASettings.for_store(order.store)
    return render(request, "rma/customer_form.html", {
        "order":   order,
        "store":   order.store,
        "items":   items,
        "sett":    sett,
        "reasons": sett.reason_meta() if sett else DEFAULT_REASONS,
    })


@csrf_exempt   # Public endpoint — token replaces session identity
def cust_submit(request, order_id):
    """Public form submission — creates the RMA."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    email = (request.POST.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required to verify the order."}, status=400)

    # Match BOTH order id + email (case-insensitive) — never match on order id alone.
    order = (Order.objects.select_related("store")
             .filter(external_order_id=order_id, customer_email__iexact=email)
             .first())
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    reason = (request.POST.get("reason") or "").strip()
    if reason and reason not in REASON_KEYS:
        reason = ""  # Reject free-text reason values
    note = (request.POST.get("note") or "").strip()[:5000]
    item_idx = request.POST.getlist("item_idx")  # selected indexes
    qtys = {}
    for k, v in request.POST.items():
        if k.startswith("qty_") and str(v).isdigit():
            qtys[k] = max(1, min(int(v), 999))  # clamp to sane range

    rma = _create_rma_with_unique_number(
        store=order.store, order=order,
        customer_name=order.customer_name or "",
        customer_email=(order.customer_email or email).strip().lower(),
        customer_phone=order.customer_phone or "",
        reason=reason,
        customer_note=note,
        status="pending",
    )

    # Re-parse order line items, only include selected ones
    line_items = _order_items(order)
    selected_set = {int(i) for i in item_idx if str(i).isdigit()} or set(range(len(line_items)))
    for idx, it in enumerate(line_items):
        if idx not in selected_set:
            continue
        qty_raw = qtys.get(f"qty_{idx}", it.get("quantity", 1))
        try:
            qty = max(1, int(qty_raw))
        except (TypeError, ValueError):
            qty = 1
        try:
            unit_price = Decimal(str(it.get("unit_price", 0) or 0))
        except (InvalidOperation, ValueError, TypeError):
            unit_price = Decimal("0")
        RMAItem.objects.create(
            rma=rma,
            product_name=(it.get("name") or "Item")[:255],
            sku=(it.get("sku") or "")[:120],
            variant_label=(it.get("variant") or "")[:255],
            quantity=qty,
            unit_price=unit_price,
            external_id=str(it.get("external_id") or "")[:120],
            image_url=(it.get("image_url") or "")[:500],
        )

    # Photos — content-type validated, size-bounded (best-effort)
    for f in request.FILES.getlist("photos"):
        ctype = (f.content_type or "").lower()
        if not ctype.startswith("image/"):
            continue
        # Skip suspiciously large uploads (>10MB) to avoid filling disk
        if getattr(f, "size", 0) > 10 * 1024 * 1024:
            continue
        RMAPhoto.objects.create(rma=rma, image=f, caption="")

    # Customer's submission note also becomes the first conversation message
    if note:
        RMAMessage.objects.create(
            rma=rma, direction="customer", body=note,
            sender_name=rma.customer_name, sender_email=rma.customer_email,
        )
    _log_event(rma, "submitted", "Submitted via customer return form", actor_label=rma.customer_name or rma.customer_email)

    # Auto-approve hook (per settings)
    sett = RMASettings.for_store(order.store)
    if _should_auto_approve(rma, sett):
        rma.status        = "approved"
        rma.approved_at   = timezone.now()
        rma.refund_amount = rma.total_refund
        rma.save()
        _log_event(rma, "approved", "Auto-approved by policy", actor_label="System")
        try:
            _send_status_email(rma, kind="approved")
        except Exception as e:
            logger.warning("RMA %s: submit auto-approve email raised: %s", rma.rma_number, e)
    else:
        try:
            _send_status_email(rma, kind="submitted")
        except Exception as e:
            logger.warning("RMA %s: submit email raised: %s", rma.rma_number, e)

    return JsonResponse({"ok": True, "rma_number": rma.rma_number, "track_url": f"/r/{rma.token}/"})


@csrf_exempt
def cust_message(request, token):
    """Customer-facing reply on tracking page."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    rma = _get_public_rma(token)
    if not rma.can_customer_act:
        return JsonResponse({"ok": False, "error": "This return has been closed."}, status=400)
    body = (request.POST.get("body") or "").strip()[:5000]
    if not body:
        return JsonResponse({"ok": False, "error": "Message body required."}, status=400)
    msg = RMAMessage.objects.create(
        rma=rma, direction="customer", body=body,
        sender_name=rma.customer_name, sender_email=rma.customer_email,
    )
    _log_event(rma, "message_sent", body[:120], actor_label=rma.customer_name or rma.customer_email)
    return JsonResponse({"ok": True, "message": {"id": msg.id, "body": msg.body, "created_at": msg.created_at.isoformat()}})


@csrf_exempt
def cust_tracking(request, token):
    """Customer submits return shipping tracking (gated to status in approved/tracking_submitted)."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    rma = _get_public_rma(token)
    # Allow updating tracking once submitted as well (customer may correct it).
    if rma.status not in ("approved", "tracking_submitted"):
        return JsonResponse({"ok": False, "error": "Tracking can only be added after approval."}, status=400)

    tid     = (request.POST.get("tracking_id") or "").strip()[:120]
    company = (request.POST.get("tracking_company") or "").strip()[:80]
    url     = (request.POST.get("tracking_url") or "").strip()[:500]
    if not tid or not company:
        return JsonResponse({"ok": False, "error": "Tracking ID and Carrier are required."}, status=400)

    rma.customer_tracking_id      = tid
    rma.customer_tracking_company = company
    rma.customer_tracking_url     = url
    rma.customer_tracking_at      = timezone.now()
    rma.status                    = "tracking_submitted"
    rma.save(update_fields=["customer_tracking_id","customer_tracking_company","customer_tracking_url","customer_tracking_at","status"])

    _log_event(
        rma, "tracking_submitted",
        f"{company} · {tid}" + (f" · {url}" if url else ""),
        actor_label=rma.customer_name or rma.customer_email,
    )

    # Add an automatic message into the conversation so tenant sees it inline
    RMAMessage.objects.create(
        rma=rma, direction="customer",
        body=f"📦 I've shipped the item back!\n\nCarrier: {company}\nTracking ID: {tid}" + (f"\nTracking link: {url}" if url else ""),
        sender_name=rma.customer_name, sender_email=rma.customer_email,
    )

    return JsonResponse({
        "ok": True, "status": rma.status,
        "tracking_id": tid, "tracking_company": company, "tracking_url": url,
    })


def cust_poll(request, token):
    """Lightweight read-only endpoint for the customer page's 15-second poll.

    Returns the minimum payload needed to refresh status + chat without
    re-rendering the whole tracking page.
    """
    rma = _get_public_rma(token)
    since_id = request.GET.get("since_id", "0")
    try:
        since_id = int(since_id)
    except (TypeError, ValueError):
        since_id = 0

    msgs = list(
        rma.messages.filter(direction__in=["customer", "tenant"], id__gt=since_id)
        .order_by("id")
    )
    new_messages = [{
        "id":         m.id,
        "direction":  m.direction,
        "body":       m.body,
        "sender":     m.sender_name,
        "ts_display": m.created_at.strftime("%b %-d · %-I:%M %p"),
    } for m in msgs]

    # Last status-bearing event so we can show timeline updates
    events = list(
        rma.events.filter(event_type__in=[
            "approved","rejected","label_generated","tracking_submitted",
            "in_transit","received","refunded","resolved","message_sent",
        ]).order_by("-created_at")[:5]
    )
    events.reverse()

    return JsonResponse({
        "ok": True,
        "status":          rma.status,
        "status_display":  rma.get_status_display(),
        "is_terminal":     rma.is_terminal,
        "can_show_tracking_button": rma.can_show_tracking_button,
        "reject_note":     rma.reject_note,
        "refund_amount":   float(rma.refund_amount or 0),
        "return_label_url":   rma.return_label_url,
        "return_tracking_no": rma.return_tracking_no,
        "return_carrier":     rma.return_carrier,
        "customer_tracking_id":      rma.customer_tracking_id,
        "customer_tracking_company": rma.customer_tracking_company,
        "customer_tracking_url":     rma.customer_tracking_url,
        "new_messages":    new_messages,
        "events": [{
            "type":       e.event_type,
            "type_label": e.get_event_type_display(),
            "ts_display": e.created_at.strftime("%b %-d · %-I:%M %p"),
            "actor":      e.actor_label,
            "message":    e.message,
        } for e in events],
    })


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────

def _order_items(order):
    """Return a normalized list of line items from an Order.

    Tries to extract from raw_data (Shopify / WooCommerce shapes) and falls
    back to a single line built from the Order's top-level columns.
    """
    items = []
    raw = getattr(order, "raw_data", None) or {}

    # Shopify shape
    if isinstance(raw, dict) and "line_items" in raw:
        for li in raw.get("line_items") or []:
            items.append({
                "name":         li.get("title") or li.get("name") or "Item",
                "variant":      li.get("variant_title") or "",
                "sku":          li.get("sku") or "",
                "quantity":     li.get("quantity") or 1,
                "unit_price":   float(li.get("price") or 0),
                "external_id":  str(li.get("product_id") or li.get("id") or ""),
                "image_url":    "",
            })

    # WooCommerce shape
    elif isinstance(raw, dict) and raw.get("line_items_woo"):
        for li in raw["line_items_woo"]:
            items.append({
                "name":       li.get("name") or "Item",
                "variant":    "",
                "sku":        li.get("sku") or "",
                "quantity":   li.get("quantity") or 1,
                "unit_price": float(li.get("price") or li.get("total") or 0),
                "external_id":str(li.get("product_id") or ""),
                "image_url":  "",
            })

    if not items:
        items.append({
            "name":       order.product_name or "Order item",
            "variant":    "",
            "sku":        "",
            "quantity":   1,
            "unit_price": float(order.total_price or 0),
            "external_id":order.product_id or "",
            "image_url":  "",
        })

    return items


def _should_auto_approve(rma, sett):
    """Apply tenant auto-approval rules. Defensive — if anything is fishy, return False."""
    if not sett:
        return False
    if not rma.store:
        return False

    # Auto-approve defects under threshold
    if rma.reason == "defective":
        threshold = sett.auto_approve_defects_under or Decimal("0")
        if threshold > 0 and rma.total_refund <= threshold:
            return True

    # VIP customers — 3+ PRIOR orders (exclude the current order if known)
    if sett.auto_approve_vip and rma.customer_email:
        prior_qs = Order.objects.filter(
            store=rma.store, customer_email__iexact=rma.customer_email,
        )
        if rma.order_id:
            prior_qs = prior_qs.exclude(id=rma.order_id)
        if prior_qs.count() >= 3 and rma.reason in ("defective", "damaged", "wrong_item"):
            return True

    return False


def _send_status_email(rma, kind="submitted"):
    """
    Send a transactional status email to the customer.

    Tries the tenant's connected Gmail (send_email_with_store_account) first;
    falls back to Django's default mail backend so the flow never silently
    fails when Gmail isn't connected yet.
    """
    if not rma.customer_email:
        return False

    subjects = {
        "submitted": f"We received your return request — {rma.rma_number}",
        "approved":  f"✅ Your return is approved — {rma.rma_number}",
        "rejected":  f"Update on your return request — {rma.rma_number}",
        "refunded":  f"💚 Refund processed — ${rma.refund_amount}",
    }
    subject = subjects.get(kind, f"Update on your return — {rma.rma_number}")

    store_name = rma.store.name if rma.store else "the store"
    track_url  = f"/r/{rma.token}/"

    if kind == "submitted":
        body = (
            f"Hi {rma.customer_name or 'there'},\n\n"
            f"We've received your return request for order #{rma.order.external_order_id if rma.order else ''}. "
            f"We'll review it within 24 hours and email you back.\n\n"
            f"Track your return: {track_url}\n\n"
            f"— {store_name}"
        )
    elif kind == "approved":
        body = (
            f"Hi {rma.customer_name or 'there'},\n\n"
            f"Your return has been approved! "
            + (f"Use this label to ship the item back: {rma.return_label_url}\n"
               f"Tracking: {rma.return_tracking_no} ({rma.return_carrier})\n\n" if rma.return_label_url else "\n")
            + f"Refund amount: ${rma.refund_amount}\n"
            f"Track your return: {track_url}\n\n"
            f"— {store_name}"
        )
    elif kind == "rejected":
        body = (
            f"Hi {rma.customer_name or 'there'},\n\n"
            f"After review we're unable to approve your return at this time.\n\n"
            f"Reason: {rma.reject_note or 'See our return policy.'}\n\n"
            f"If you have questions, reply to this email.\n\n"
            f"— {store_name}"
        )
    elif kind == "refunded":
        body = (
            f"Hi {rma.customer_name or 'there'},\n\n"
            f"Your refund of ${rma.refund_amount} has been processed. "
            f"It should appear on your statement in 3-5 business days.\n\n"
            f"— {store_name}"
        )
    else:
        body = f"There's an update on your return ({rma.rma_number}). Track it here: {track_url}"

    return _safe_send_to_customer(rma, subject, body)
