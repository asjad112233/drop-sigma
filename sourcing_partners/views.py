"""DropSigma Sourcing Partners — list, conversation, chat APIs.

Frontend opens the section → calls /sourcing-partners/api/list/ to get all
verified partners. Click a partner → /sourcing-partners/api/<id>/open/ to
fetch or create the conversation + message history. Send → POST
/sourcing-partners/api/<conv_id>/send/ (text + optional attachments).
"""
import os
import json
import mimetypes
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Count, Avg, F
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import (
    require_GET, require_POST, require_http_methods,
)

from django.db import transaction

from .models import (
    SourcingPartner, PartnerConversation, PartnerMessage, PartnerAttachment,
    VendorOrder, LockedPrice, PriceHistory, VendorStockItem, VendorPayment,
    VendorDocument, AutoRule, WorkspaceActivity, PartnerReview,
    TenantWallet, WalletTransaction,
)
from orders.models import Order
from orders.tracking_link import build_ds_tracking_link, safe_carrier_name


# ─── Serializers ────────────────────────────────────────────────────────────

def _partner_json(p, *, full=False):
    base = {
        "id":            p.id,
        "name":          p.name,
        "slug":          p.slug,
        "initials":      p.initials,
        "logo_url":      p.logo_url,
        "cover_emoji":   p.cover_emoji,
        "gradient_from": p.cover_gradient_from,
        "gradient_to":   p.cover_gradient_to,
        "gradient_css":  p.gradient_css,
        "tagline":       p.tagline,
        "country":       p.country,
        "headquarters":  p.headquarters,
        "rating":        float(p.rating),
        "total_reviews": p.total_reviews,
        "total_orders":  p.total_orders,
        "on_time_pct":   p.on_time_rate_pct,
        "response_hrs":  p.avg_response_hours,
        "is_verified":   p.is_verified,
        "is_featured":   p.is_featured,
        "specialties":   p.specialties or [],
        "languages":     p.languages or [],
        "min_order":     p.min_order_value_usd,
        "lead_days":     p.typical_lead_days,
    }
    if full:
        base.update({
            "description":     p.description,
            "founded_year":    p.founded_year,
            "team_size":       p.team_size,
            "monthly_volume":  p.monthly_volume,
            "payment_methods": p.payment_methods or [],
        })
    return base


def _attachment_json(a):
    if a.kind == "image" and a.file:
        return {
            "kind": "image", "url": a.file.url,
            "filename": a.filename, "size": a.display_size,
        }
    if a.kind == "file" and a.file:
        return {
            "kind": "file", "url": a.file.url,
            "filename": a.filename, "size": a.display_size, "mime": a.mime_type,
        }
    if a.kind == "link":
        return {"kind": "link", "url": a.url}
    return None


def _message_json(m):
    return {
        "id":         m.id,
        "direction":  m.direction,
        "body":       m.body,
        "is_read":    m.is_read,
        "created_at": m.created_at.isoformat(),
        "created_iso": m.created_at.strftime("%Y-%m-%d %H:%M"),
        "attachments": [_attachment_json(a) for a in m.attachments.all()
                        if _attachment_json(a) is not None],
    }


# ═══════════════════════════════════════════════════════════════════════
# CHAT SMART-LINK TOKEN VALIDATION (tenant-isolation guard)
#
# The frontend regex catches ANY order-ref / SKU / email shape in a chat
# body. Without server-side validation, a foreign DS-ref pasted into the
# chat would *look* clickable to a tenant that can't actually see that
# order — which is both a UX lie and a tenant-isolation leak.
#
# `_validate_message_tokens(bodies, user)` runs the same regexes the
# frontend uses, then bulk-resolves which (type, token) pairs actually
# resolve to data inside the given user's scope. Result is a flat list
# of "type:token" strings the frontend uses as a whitelist — only matches
# in that list become clickable chips; anything else stays plain text.
#
# This is called from EVERY endpoint that returns chat messages — both
# the tenant SP endpoints AND the ops chat endpoints (the ops version
# passes `conv.tenant`, not the caller, so ops see exactly what the
# tenant sees).
# ═══════════════════════════════════════════════════════════════════════
import re as _re

# Mirror of templates' SPV_RX_* / CX_RX_* — keep in sync.
_TOKRX_ORDER = _re.compile(r'#?DS-(?:DEMO-)?\d{2,8}|#\d{4,12}')
_TOKRX_EMAIL = _re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_TOKRX_SKU   = _re.compile(r'\b[A-Z][A-Z0-9]{1,5}(?:-[A-Z0-9]{1,8}){1,4}\b')


def _validate_message_tokens(bodies, user):
    """Return a sorted list of `"type:token"` strings — the set of chip
    candidates extracted from `bodies` that actually resolve to data the
    given `user` is allowed to see.

    Tokens NOT in the returned list must be rendered as plain text by
    the frontend, otherwise we leak the existence of other tenants'
    orders by making foreign refs look clickable.
    """
    from collections import defaultdict

    candidates = defaultdict(set)  # type -> set of raw tokens (as matched)
    for body in bodies:
        if not body:
            continue
        for m in _TOKRX_ORDER.findall(body):
            candidates["order"].add(m)
        for m in _TOKRX_EMAIL.findall(body):
            candidates["email"].add(m)
        for m in _TOKRX_SKU.findall(body):
            # SKU regex also matches DS-DEMO-1061 / DS-000288 / etc.
            # Frontend prefers "order" first, so anything that already
            # matches the order regex must be filtered out of SKU here.
            if not _TOKRX_ORDER.fullmatch(m):
                candidates["sku"].add(m)

    if not any(candidates.values()):
        return []

    qs = Order.objects.filter(store__user=user)
    valid = set()

    # ── Orders: DS-XXXXXX → numeric id match, anything else → external id
    if candidates["order"]:
        ids_to_check = set()
        ext_to_check = set()
        for tok in candidates["order"]:
            stripped = tok.lstrip("#")
            if stripped.upper().startswith("DS-") and not stripped.upper().startswith("DS-DEMO"):
                tail = stripped[3:].lstrip("0") or "0"
                if tail.isdigit():
                    try:
                        ids_to_check.add(int(tail))
                    except ValueError:
                        pass
                else:
                    ext_to_check.add(stripped)
            else:
                ext_to_check.add(stripped)
        existing_ids = (set(qs.filter(id__in=ids_to_check)
                            .values_list("id", flat=True))
                        if ids_to_check else set())
        existing_ext = (set(qs.filter(external_order_id__in=ext_to_check)
                            .values_list("external_order_id", flat=True))
                        if ext_to_check else set())
        existing_ext_ci = {e.lower() for e in existing_ext}
        for tok in candidates["order"]:
            stripped = tok.lstrip("#")
            if stripped.upper().startswith("DS-") and not stripped.upper().startswith("DS-DEMO"):
                tail = stripped[3:].lstrip("0") or "0"
                if tail.isdigit() and int(tail) in existing_ids:
                    valid.add(f"order:{tok}")
                elif stripped.lower() in existing_ext_ci:
                    valid.add(f"order:{tok}")
            elif stripped.lower() in existing_ext_ci:
                valid.add(f"order:{tok}")

    # ── Emails: simple iexact bulk lookup
    if candidates["email"]:
        existing = set(qs.filter(customer_email__in=list(candidates["email"]))
                       .values_list("customer_email", flat=True))
        existing_lc = {e.lower() for e in existing if e}
        for tok in candidates["email"]:
            if tok.lower() in existing_lc:
                valid.add(f"email:{tok}")

    # ── SKUs: have to walk raw_data JSON. Use a single icontains filter
    #    keyed on each candidate; this trims the candidate set quickly.
    #    For tenants with very large order tables we could batch via
    #    PostgreSQL JSONB queries, but icontains is portable + safe.
    if candidates["sku"]:
        for sku in candidates["sku"]:
            qs_check = qs.filter(raw_data__icontains=sku)[:200]
            found = False
            for o in qs_check:
                raw = o.raw_data or {}
                if not isinstance(raw, dict):
                    continue
                items = raw.get("line_items") or []
                if not isinstance(items, list):
                    continue
                for li in items:
                    if not isinstance(li, dict):
                        continue
                    li_sku = (li.get("sku") or "").strip()
                    if li_sku and li_sku.upper() == sku.upper():
                        found = True
                        break
                if found:
                    break
            if found:
                valid.add(f"sku:{sku}")

    return sorted(valid)


# ─── List / open ────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_partner_list(request):
    """Return all active partners + the tenant's existing convo previews."""
    q = (request.GET.get("q") or "").strip()
    partners = SourcingPartner.objects.filter(is_active=True)
    if q:
        partners = partners.filter(
            Q(name__icontains=q) | Q(country__icontains=q) |
            Q(tagline__icontains=q)
        )

    convs = {
        c.partner_id: c for c in
        PartnerConversation.objects.filter(tenant=request.user, is_archived=False)
    }

    out = []
    for p in partners:
        item = _partner_json(p)
        conv = convs.get(p.id)
        if conv:
            item["conversation_id"] = conv.id
            item["last_message_preview"] = conv.last_message_preview
            item["unread"] = conv.unread_count_for_tenant
            item["last_message_at"] = conv.last_message_at.isoformat()
        else:
            item["conversation_id"] = None
            item["unread"] = 0
        out.append(item)
    return JsonResponse({"ok": True, "partners": out})


@login_required(login_url="/login/")
@require_GET
def api_partner_detail(request, pk):
    """Full partner profile (for the "About" panel in chat view)."""
    p = get_object_or_404(SourcingPartner, pk=pk, is_active=True)
    return JsonResponse({"ok": True, "partner": _partner_json(p, full=True)})


@login_required(login_url="/login/")
@require_POST
def api_open_conversation(request, pk):
    """Get or create a conversation with a partner, return message history."""
    p = get_object_or_404(SourcingPartner, pk=pk, is_active=True)
    # Make sure the tenant has a dedicated Sourcing Manager assigned. This
    # is the lazy trigger: on first chat open they get auto-assigned and a
    # welcome message is seeded so the thread isn't empty.
    try:
        from sourcing_ops.services import ensure_tenant_manager
        ensure_tenant_manager(request.user)
    except Exception:
        # Don't let assignment failures block chat open — the chat must
        # work even if ops staffing has hiccups.
        pass
    conv, created = PartnerConversation.objects.get_or_create(
        tenant=request.user, partner=p,
    )
    if created:
        # Seed a welcome message from the partner so the thread isn't empty.
        welcome = PartnerMessage.objects.create(
            conversation=conv, direction="in",
            body=(f"Hello! Welcome to {p.name}. I'm your dedicated sourcing "
                  f"liaison. How can we help you today? Share product specs, "
                  f"sample images, or any reference links — we'll get back "
                  f"with a quote within {p.avg_response_hours} hours."),
        )
        conv.last_message_at = welcome.created_at
        conv.last_message_preview = welcome.body[:180]
        conv.unread_count_for_tenant = 1
        conv.save(update_fields=[
            "last_message_at", "last_message_preview", "unread_count_for_tenant",
        ])

    # Mark partner-sent messages as read since the tenant just opened the convo.
    conv.messages.filter(direction="in", is_read=False).update(is_read=True)
    conv.unread_count_for_tenant = 0
    conv.save(update_fields=["unread_count_for_tenant"])

    msg_objs = list(conv.messages.all())
    messages = [_message_json(m) for m in msg_objs]
    return JsonResponse({
        "ok": True,
        "conversation_id": conv.id,
        "partner": _partner_json(p, full=True),
        "messages": messages,
        # Tenant-isolation guard: only tokens whose target order lives in
        # this user's scope become clickable on the frontend.
        "valid_tokens": _validate_message_tokens(
            [m.body for m in msg_objs], request.user),
    })


# ─── Send + attachments ────────────────────────────────────────────────────

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
ALLOWED_FILE_EXT  = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv",
                     ".zip", ".rar", ".7z", ".mp4", ".mov", ".webm"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


@login_required(login_url="/login/")
@require_POST
def api_send_message(request, conv_id):
    """Send a message to a conversation. Supports body + multiple files +
    one or more URLs (one per line in `links` POST field)."""
    # Tenant just sent a message — make sure they have a dedicated
    # Sourcing Manager (idempotent).
    try:
        from sourcing_ops.services import ensure_tenant_manager
        ensure_tenant_manager(request.user)
    except Exception:
        pass
    conv = get_object_or_404(
        PartnerConversation,
        pk=conv_id, tenant=request.user,
    )
    body = (request.POST.get("body") or "").strip()
    links_raw = (request.POST.get("links") or "").strip()
    # Frontend sends images under "images" and other files under "files".
    # Accept either key so pasted/drag-dropped images aren't silently
    # dropped if the field name shifts.
    image_files = request.FILES.getlist("images")
    other_files = request.FILES.getlist("files")
    files = list(image_files) + list(other_files)

    if not body and not files and not links_raw:
        return JsonResponse({"ok": False, "error": "Message is empty."}, status=400)

    # Cap message body to 5,000 chars.
    body = body[:5000]

    msg = PartnerMessage.objects.create(
        conversation=conv, direction="out", body=body,
    )

    # Attach files. Trust the upload field name ("images" → image), then
    # fall back to extension/MIME for unlabeled or legacy uploads.
    for f in files:
        if f.size > MAX_UPLOAD_BYTES:
            continue  # skip oversized silently
        ext = os.path.splitext(f.name)[1].lower()
        mime = mimetypes.guess_type(f.name)[0] or (getattr(f, "content_type", "") or "")
        from_images_field = f in image_files
        if from_images_field or mime.startswith("image/") or ext in ALLOWED_IMAGE_EXT:
            kind = "image"
        elif ext in ALLOWED_FILE_EXT:
            kind = "file"
        else:
            kind = "file"  # accept anyway, conservative
        PartnerAttachment.objects.create(
            message=msg, kind=kind, file=f,
            filename=f.name[:240], filesize_bytes=f.size, mime_type=mime,
        )

    # Attach links (one per line)
    for url in [u.strip() for u in links_raw.splitlines() if u.strip()]:
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        PartnerAttachment.objects.create(message=msg, kind="link", url=url[:500])

    # Update conversation preview + bump OPS-side unread count + clear
    # the tenant's typing stamp (they've now sent — they're no longer
    # mid-keystroke; the OPS user should see the indicator drop).
    conv.last_message_at = msg.created_at
    preview = body or ("📎 Attachment" if files or links_raw else "")
    conv.last_message_preview = preview[:180]
    conv.unread_count_for_partner = (conv.unread_count_for_partner or 0) + 1
    conv.typing_tenant_at = None
    conv.save(update_fields=[
        "last_message_at", "last_message_preview",
        "unread_count_for_partner", "typing_tenant_at",
    ])

    # NOTE: no auto-reply here. Welcome message is sent ONCE when the
    # tenant enters the chat (via ensure_tenant_manager → assignment
    # creation), not on send. After that, real manager replies through
    # the ops portal.

    return JsonResponse({
        "ok": True,
        "message": _message_json(msg),
        "auto_reply": None,
        # Validate any chip candidates in the sent message body so the
        # optimistic UI render uses the same whitelist as a polled message.
        "valid_tokens": _validate_message_tokens([msg.body], request.user),
    })


def _create_demo_autoreply(conv, tenant_msg):
    """Drop a canned partner-side reply so the chat feels alive locally.
    In production this would be a real message from the partner's
    operator dashboard."""
    p = conv.partner
    if tenant_msg.attachments.filter(kind="image").exists():
        body = (f"Thanks for the photos! Our sourcing team will review the "
                f"specs and revert with three quotes from our top suppliers "
                f"in {p.country}. Typical turnaround is {p.avg_response_hours} "
                f"hours during business hours.")
    elif tenant_msg.attachments.filter(kind="file").exists():
        body = ("Received the attachment ✓. We'll have an operator review "
                "it shortly. If urgent, mention it in the next message and "
                "we'll prioritise.")
    elif tenant_msg.attachments.filter(kind="link").exists():
        body = ("Got the link — opening it now to check the product. "
                "We'll come back with sourcing options and a target price.")
    else:
        body = (f"Got it. I've flagged this to our sourcing desk; "
                f"expect a detailed reply within {p.avg_response_hours} "
                f"working hours.")
    reply = PartnerMessage.objects.create(
        conversation=conv, direction="in", body=body, is_read=True,
    )
    conv.last_message_at = reply.created_at
    conv.last_message_preview = body[:180]
    conv.save(update_fields=["last_message_at", "last_message_preview"])
    return reply


# ─── Poll for new partner messages (lightweight) ───────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_poll_messages(request, conv_id):
    """Return any messages newer than the supplied ?after_id=N, plus the
    OPS-side typing presence (so the tenant sees "Drop Sigma is typing…"
    when the partner is mid-keystroke)."""
    conv = get_object_or_404(
        PartnerConversation,
        pk=conv_id, tenant=request.user,
    )
    # Accept either ?after_id= (legacy) or ?after= (newer frontend uses this)
    raw_after = request.GET.get("after_id") or request.GET.get("after") or 0
    try:
        after_id = int(raw_after)
    except (TypeError, ValueError):
        after_id = 0
    new = list(conv.messages.filter(id__gt=after_id))
    # Mark partner-sent ones read on poll, and clear the tenant unread
    # badge so the sidebar pill drops the moment they're looking at
    # the thread.
    if new:
        conv.messages.filter(
            id__gt=after_id, direction="in", is_read=False,
        ).update(is_read=True)
    # Tenant is actively looking at the thread → their unread counter
    # should also reset on every poll, not just on send.
    if conv.unread_count_for_tenant:
        PartnerConversation.objects.filter(pk=conv.pk).update(
            unread_count_for_tenant=0)
    return JsonResponse({
        "ok": True,
        "messages": [_message_json(m) for m in new],
        "valid_tokens": _validate_message_tokens(
            [m.body for m in new], request.user),
        # Who's typing right now (from the tenant's perspective):
        #   partner_typing — the OPS side is mid-keystroke
        #   tenant_typing  — echo so the tenant can debug their own state
        "presence": {
            "partner_typing": conv.is_partner_typing,
            "tenant_typing":  conv.is_tenant_typing,
        },
    })


# ─── Typing presence ping (tenant → partner) ───────────────────────────
@login_required(login_url="/login/")
@require_POST
def api_typing(request, conv_id):
    """Tenant ping: mark them as typing in this conversation. The OPS
    poll endpoint reads ``typing_tenant_at`` via ``is_tenant_typing`` to
    render the "tenant is typing…" pill. Ping cadence on the frontend is
    every 3s while the keystroke loop is active; we treat ANY stamp
    within the last 6s as "still typing"."""
    conv = get_object_or_404(
        PartnerConversation,
        pk=conv_id, tenant=request.user,
    )
    PartnerConversation.objects.filter(pk=conv.pk).update(
        typing_tenant_at=timezone.now())
    return JsonResponse({"ok": True})


# ─── Unread + last-message summary (sidebar badge + popup) ──────────────
@login_required(login_url="/login/")
@require_GET
def api_conv_unread_summary(request):
    """Tenant-wide unread totals + the freshest "in" message preview.

    The dashboard sidebar polls this every few seconds so the My Manager
    badge stays live without re-opening the workspace, and so a small
    side popup can fire on a NEW partner message when the user isn't
    inside the chat thread.
    """
    convs = (PartnerConversation.objects
             .filter(tenant=request.user, is_archived=False)
             .order_by("-last_message_at"))
    total_unread = 0
    latest = None
    for c in convs:
        total_unread += int(c.unread_count_for_tenant or 0)
        if latest is None and (c.unread_count_for_tenant or 0) > 0:
            # Pull the freshest UNREAD incoming message so the popup
            # has something to surface. Empty body falls back to the
            # preview string we stamped on conv.
            m = (c.messages
                 .filter(direction="in", is_read=False)
                 .order_by("-created_at").first())
            latest = {
                "conversation_id": c.pk,
                "partner_name":    c.partner.name if c.partner_id else "Drop Sigma",
                "body":            (m.body if m else c.last_message_preview) or "📎 New attachment",
                "created_at_iso":  _iso(m.created_at if m else c.last_message_at),
            }
    return JsonResponse({
        "ok": True,
        "total_unread": total_unread,
        "latest": latest,
    })


# ═══════════════════════════════════════════════════════════════════════
# WORKSPACE TABS — Orders, Catalog, Stock, Payments, Documents,
# Performance, Auto-Rules, Activity, Reviews, Summary
# ═══════════════════════════════════════════════════════════════════════


# ─── helpers ────────────────────────────────────────────────────────────

def _parse_body(request):
    """Accept either JSON body or form-encoded POST. Returns a dict."""
    ctype = (request.META.get("CONTENT_TYPE") or "").lower()
    if "application/json" in ctype:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
    # Fall back to form data (works for multipart and urlencoded).
    return {k: v for k, v in request.POST.items()}


def _iso(dt):
    return dt.isoformat() if dt else None


def _dec_to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _human_size(size):
    if size is None:
        return ""
    size = int(size)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size/1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size/(1024*1024):.2f} MB"
    return f"{size/(1024*1024*1024):.2f} GB"


def _human_ago(when):
    if not when:
        return ""
    now = timezone.now()
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return "Just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 7:
        return f"{secs // 86400}d ago"
    if secs < 86400 * 30:
        return f"{secs // (86400 * 7)}w ago"
    if secs < 86400 * 365:
        return f"{secs // (86400 * 30)}mo ago"
    return f"{secs // (86400 * 365)}y ago"


def _decimal_or_zero(value, default="0"):
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _vendor_order_json(o):
    return {
        "id":                 o.id,
        "order_ref":          o.order_ref,
        "source_order_ref":   o.source_order_ref,
        "product_name":       o.product_name,
        "product_sku":        o.product_sku,
        "product_image_url":  o.product_image_url,
        "variant":            o.variant,
        "quantity":           o.quantity,
        "customer_name":      o.customer_name,
        "ship_city":          o.ship_city,
        "ship_country":       o.ship_country,
        "ship_full_address":  o.ship_full_address,
        "status":             o.status,
        "status_label":       o.get_status_display(),
        "priority":           o.priority,
        "china_cost_cny":     _dec_to_float(o.china_cost_cny),
        "china_cost_usd":     _dec_to_float(o.china_cost_usd),
        "shipping_usd":       _dec_to_float(o.shipping_usd),
        "vendor_margin_usd":  _dec_to_float(o.vendor_margin_usd),
        "platform_fee_usd":   _dec_to_float(o.platform_fee_usd),
        "tenant_total_usd":   _dec_to_float(o.tenant_total_usd),
        "margin_pct":         _dec_to_float(o.margin_pct),
        "lead_days_quoted":   o.lead_days_quoted,
        "china_tracking_no":  o.china_tracking_no,
        "last_mile_carrier":  o.last_mile_carrier,
        "last_mile_tracking": o.last_mile_tracking,
        "last_mile_url":      o.last_mile_url,
        "tenant_notes":       o.tenant_notes,
        "vendor_notes":       o.vendor_notes,
        "created_at_iso":     _iso(o.created_at),
        "locked_price_used":  o.locked_price_used_id,
        "quoted_at":          _iso(o.quoted_at),
        "paid_at":            _iso(o.paid_at),
        "ordered_at":         _iso(o.ordered_at),
        "in_transit_at":      _iso(o.in_transit_at),
        "received_at":        _iso(o.received_at),
        "shipped_at":         _iso(o.shipped_at),
        "delivered_at":       _iso(o.delivered_at),
        "cancelled_at":       _iso(o.cancelled_at),
    }


def _locked_price_json(lp, *, with_history=True):
    out = {
        "id":                  lp.id,
        "sku":                 lp.sku,
        "product_name":        lp.product_name,
        "product_image_url":   lp.product_image_url,
        "china_cost_cny":      _dec_to_float(lp.china_cost_cny),
        "china_cost_usd":      _dec_to_float(lp.china_cost_usd),
        "shipping_usd":        _dec_to_float(lp.shipping_usd),
        "vendor_margin_usd":   _dec_to_float(lp.vendor_margin_usd),
        "locked_price_usd":    _dec_to_float(lp.locked_price_usd),
        "lead_days":           lp.lead_days,
        "auto_assign":         lp.auto_assign,
        "status":              lp.status,
        "status_label":        lp.get_status_display(),
        "pending_price_usd":   _dec_to_float(lp.pending_price_usd),
        "pending_reason":      lp.pending_reason,
        "pending_proposed_at": _iso(lp.pending_proposed_at),
        "locked_at":           _iso(lp.locked_at),
        "last_used_at":        _iso(lp.last_used_at),
        "times_used":          lp.times_used,
    }
    if with_history:
        out["history"] = [
            {
                "event":      h.event,
                "old_price":  _dec_to_float(h.old_price),
                "new_price":  _dec_to_float(h.new_price),
                "reason":     h.reason,
                "acted_by":   h.acted_by,
                "created_at": _iso(h.created_at),
            }
            for h in lp.history.all()[:10]
        ]
    return out


def _stock_json(s):
    return {
        "id":            s.id,
        "sku":           s.sku,
        "product_name":  s.product_name,
        "image_url":     s.image_url,
        "qty_on_hand":   s.qty_on_hand,
        "qty_reserved":  s.qty_reserved,
        "qty_available": s.qty_available,
        "qty_inbound":   s.qty_inbound,
        "low_threshold": s.low_threshold,
        "status":        s.status,
        "last_updated":  _iso(s.last_updated),
    }


def _payment_json(p):
    return {
        "id":                p.id,
        "payment_type":      p.payment_type,
        "payment_type_label": p.get_payment_type_display(),
        "method":            p.method,
        "method_label":      p.get_method_display(),
        "amount_usd":        _dec_to_float(p.amount_usd),
        "fee_usd":           _dec_to_float(p.fee_usd),
        "status":            p.status,
        "status_label":      p.get_status_display(),
        "reference":         p.reference,
        "note":              p.note,
        "related_order_ref": p.related_order.order_ref if p.related_order_id else None,
        "created_at":        _iso(p.created_at),
        "settled_at":        _iso(p.settled_at),
    }


def _document_json(d):
    file_url = ""
    if d.file:
        try:
            file_url = d.file.url
        except Exception:
            file_url = ""
    if not file_url:
        file_url = d.external_url or ""
    return {
        "id":               d.id,
        "category":         d.category,
        "category_label":   d.get_category_display(),
        "title":            d.title,
        "file_url":         file_url,
        "filename":         d.filename,
        "filesize_bytes":   d.filesize_bytes,
        "filesize_display": _human_size(d.filesize_bytes),
        "description":      d.description,
        "uploaded_by":      d.uploaded_by,
        "created_at":       _iso(d.created_at),
    }


def _rule_json(r):
    return {
        "id":              r.id,
        "rule_type":       r.rule_type,
        "rule_type_label": r.get_rule_type_display(),
        "is_active":       r.is_active,
        "config":          r.config or {},
        "created_at":      _iso(r.created_at),
    }


def _activity_json(a):
    return {
        "id":                a.id,
        "event_type":        a.event_type,
        "title":             a.title,
        "detail":            a.detail,
        "icon":              a.icon,
        "actor":             a.actor,
        "related_order_ref": a.related_order.order_ref if a.related_order_id else None,
        "created_at":        _iso(a.created_at),
        "created_ago":       _human_ago(a.created_at),
    }


def _review_json(r):
    return {
        "id":             r.id,
        "rating":         r.rating,
        "comm_score":     r.comm_score,
        "quality_score":  r.quality_score,
        "delivery_score": r.delivery_score,
        "comment":        r.comment,
        "is_public":      r.is_public,
        "created_at":     _iso(r.created_at),
    }


def _log_activity(tenant, partner, *, event_type, title, detail="",
                  icon="•", actor="tenant", related_order=None):
    return WorkspaceActivity.objects.create(
        tenant=tenant, partner=partner,
        event_type=event_type, title=title, detail=detail,
        icon=icon, actor=actor, related_order=related_order,
    )


# ─── ORDERS ─────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_orders_list(request, partner_id):
    """List the tenant's orders with this partner, with optional ?status= filter."""
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = VendorOrder.objects.filter(tenant=request.user, partner=partner)

    status_filter = (request.GET.get("status") or "").strip()
    if status_filter:
        qs = qs.filter(status=status_filter)

    # Counts across ALL statuses, not the filtered subset.
    counts_qs = (VendorOrder.objects
                 .filter(tenant=request.user, partner=partner)
                 .values("status")
                 .annotate(n=Count("id")))
    counts = {row["status"]: row["n"] for row in counts_qs}

    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "counts": counts,
        "orders": [_vendor_order_json(o) for o in qs],
    })


@login_required(login_url="/login/")
@require_GET
def api_order_detail(request, order_id):
    o = get_object_or_404(VendorOrder, pk=order_id, tenant=request.user)
    timeline = WorkspaceActivity.objects.filter(
        tenant=request.user, partner=o.partner, related_order=o,
    )
    data = _vendor_order_json(o)
    data["timeline"] = [_activity_json(a) for a in timeline]
    return JsonResponse({"ok": True, "order": data})


@login_required(login_url="/login/")
@require_POST
def api_order_accept_quote(request, order_id):
    o = get_object_or_404(VendorOrder, pk=order_id, tenant=request.user)
    now = timezone.now()
    o.status = "paid"
    o.paid_at = now

    # Create payment row.
    VendorPayment.objects.create(
        tenant=request.user, partner=o.partner, related_order=o,
        payment_type="order", method="wallet",
        amount_usd=o.tenant_total_usd, status="settled",
        reference=o.order_ref,
        settled_at=now,
    )

    # Create LockedPrice if missing for this (tenant, partner, sku).
    locked_created = False
    locked = LockedPrice.objects.filter(
        tenant=request.user, partner=o.partner, sku=o.product_sku,
    ).first()
    if not locked:
        locked = LockedPrice.objects.create(
            tenant=request.user, partner=o.partner, sku=o.product_sku,
            product_name=o.product_name, product_image_url=o.product_image_url,
            china_cost_cny=o.china_cost_cny, china_cost_usd=o.china_cost_usd,
            shipping_usd=o.shipping_usd, vendor_margin_usd=o.vendor_margin_usd,
            platform_fee_usd=o.platform_fee_usd,
            locked_price_usd=o.tenant_total_usd,
            lead_days=o.lead_days_quoted,
            locked_via_order_ref=o.order_ref,
            locked_at=now,
        )
        PriceHistory.objects.create(
            locked=locked, event="locked",
            old_price=None, new_price=o.tenant_total_usd,
            reason="Initial lock from order " + o.order_ref,
            acted_by="tenant",
        )
        locked_created = True
    o.locked_price_used = locked
    o.save()

    _log_activity(
        request.user, o.partner,
        event_type="quote.accepted",
        title=f"Quote accepted for {o.order_ref}",
        detail=f"${_dec_to_float(o.tenant_total_usd)} paid via wallet.",
        icon="✓", actor="tenant", related_order=o,
    )

    return JsonResponse({
        "ok": True,
        "order": _vendor_order_json(o),
        "locked_price_created": locked_created,
    })


@login_required(login_url="/login/")
@require_POST
def api_order_reject_quote(request, order_id):
    o = get_object_or_404(VendorOrder, pk=order_id, tenant=request.user)
    body = _parse_body(request)
    reason = (body.get("reason") or "").strip()
    o.status = "quote_rejected"
    o.save(update_fields=["status", "updated_at"])

    _log_activity(
        request.user, o.partner,
        event_type="quote.rejected",
        title=f"Quote rejected for {o.order_ref}",
        detail=reason or "No reason provided.",
        icon="✗", actor="tenant", related_order=o,
    )
    return JsonResponse({"ok": True, "order": _vendor_order_json(o)})


@login_required(login_url="/login/")
@require_POST
def api_order_cancel(request, order_id):
    o = get_object_or_404(VendorOrder, pk=order_id, tenant=request.user)
    now = timezone.now()
    o.status = "cancelled"
    o.cancelled_at = now
    o.save(update_fields=["status", "cancelled_at", "updated_at"])

    _log_activity(
        request.user, o.partner,
        event_type="order.cancelled",
        title=f"Order {o.order_ref} cancelled",
        icon="✗", actor="tenant", related_order=o,
    )
    return JsonResponse({"ok": True, "order": _vendor_order_json(o)})


@login_required(login_url="/login/")
@require_POST
def api_order_create(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    body = _parse_body(request)

    sku = (body.get("product_sku") or "").strip()
    if not sku:
        return JsonResponse(
            {"ok": False, "error": "product_sku is required."}, status=400
        )

    try:
        quantity = int(body.get("quantity") or 1)
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    # Auto-gen order_ref.
    order_ref = f"DSV-{VendorOrder.objects.count() + 2401:06d}"

    locked = LockedPrice.objects.filter(
        tenant=request.user, partner=partner, sku=sku, auto_assign=True,
    ).first()

    now = timezone.now()
    order_kwargs = dict(
        tenant=request.user, partner=partner,
        order_ref=order_ref,
        source_order_ref=(body.get("source_order_ref") or "").strip(),
        product_sku=sku,
        product_name=(body.get("product_name") or "").strip(),
        product_image_url=(body.get("product_image_url") or "").strip(),
        variant=(body.get("variant") or "").strip(),
        quantity=quantity,
        customer_name=(body.get("customer_name") or "").strip(),
        customer_email=(body.get("customer_email") or "").strip(),
        ship_city=(body.get("ship_city") or "").strip(),
        ship_country=(body.get("ship_country") or "").strip(),
        ship_full_address=(body.get("ship_full_address") or "").strip(),
        tenant_notes=(body.get("tenant_notes") or "").strip(),
    )

    used_locked = False
    if locked:
        order_kwargs.update(
            china_cost_cny=locked.china_cost_cny,
            china_cost_usd=locked.china_cost_usd,
            shipping_usd=locked.shipping_usd,
            vendor_margin_usd=locked.vendor_margin_usd,
            platform_fee_usd=locked.platform_fee_usd,
            tenant_total_usd=locked.locked_price_usd,
            lead_days_quoted=locked.lead_days,
            locked_price_used=locked,
            status="paid",
            paid_at=now,
            quoted_at=now,
        )
        used_locked = True
    else:
        order_kwargs.update(status="awaiting_quote")

    order = VendorOrder.objects.create(**order_kwargs)

    if used_locked:
        VendorPayment.objects.create(
            tenant=request.user, partner=partner, related_order=order,
            payment_type="order", method="wallet",
            amount_usd=order.tenant_total_usd, status="settled",
            reference=order.order_ref, settled_at=now,
        )
        locked.times_used = (locked.times_used or 0) + 1
        locked.last_used_at = now
        locked.save(update_fields=["times_used", "last_used_at"])
        _log_activity(
            request.user, partner,
            event_type="order.auto_paid",
            title=f"Order {order.order_ref} auto-paid via locked price",
            detail=f"SKU {sku} matched locked price (${_dec_to_float(locked.locked_price_usd)}).",
            icon="⚡", actor="tenant", related_order=order,
        )
    else:
        _log_activity(
            request.user, partner,
            event_type="order.created",
            title=f"Order {order.order_ref} routed to {partner.name}",
            detail=f"Awaiting quote on SKU {sku}.",
            icon="•", actor="tenant", related_order=order,
        )

    return JsonResponse({
        "ok": True,
        "order": _vendor_order_json(order),
        "used_locked_price": used_locked,
    })


# ─── CATALOG / LOCKED PRICES ────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_catalog_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = LockedPrice.objects.filter(tenant=request.user, partner=partner)
    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "locked_prices": [_locked_price_json(lp) for lp in qs],
    })


@login_required(login_url="/login/")
@require_POST
def api_catalog_toggle_auto(request, locked_id):
    lp = get_object_or_404(LockedPrice, pk=locked_id, tenant=request.user)
    body = _parse_body(request)
    if "auto_assign" in body:
        v = body.get("auto_assign")
        if isinstance(v, str):
            lp.auto_assign = v.lower() in ("1", "true", "yes", "on")
        else:
            lp.auto_assign = bool(v)
    else:
        lp.auto_assign = not lp.auto_assign
    lp.save(update_fields=["auto_assign"])
    return JsonResponse({"ok": True, "locked_price": _locked_price_json(lp)})


@login_required(login_url="/login/")
@require_POST
def api_catalog_request_renegotiation(request, locked_id):
    lp = get_object_or_404(LockedPrice, pk=locked_id, tenant=request.user)
    body = _parse_body(request)
    new_price = _decimal_or_zero(body.get("new_price"))
    if new_price <= 0:
        return JsonResponse(
            {"ok": False, "error": "new_price must be > 0."}, status=400
        )
    reason = (body.get("reason") or "").strip()

    now = timezone.now()
    old_price = lp.locked_price_usd
    lp.status = "renegotiation_proposed"
    lp.pending_price_usd = new_price
    lp.pending_reason = reason
    lp.pending_proposed_at = now
    lp.save(update_fields=[
        "status", "pending_price_usd", "pending_reason", "pending_proposed_at",
    ])

    PriceHistory.objects.create(
        locked=lp, event="renegotiation_proposed",
        old_price=old_price, new_price=new_price,
        reason=reason, acted_by="vendor",
    )
    _log_activity(
        request.user, lp.partner,
        event_type="price.renegotiation_proposed",
        title=f"New price proposed for {lp.sku}",
        detail=(f"${_dec_to_float(old_price)} → ${_dec_to_float(new_price)}. "
                f"{reason}").strip(),
        icon="↻", actor="vendor",
    )
    return JsonResponse({"ok": True, "locked_price": _locked_price_json(lp)})


@login_required(login_url="/login/")
@require_POST
def api_catalog_approve_renegotiation(request, locked_id):
    lp = get_object_or_404(LockedPrice, pk=locked_id, tenant=request.user)
    if lp.pending_price_usd is None:
        return JsonResponse(
            {"ok": False, "error": "No pending price to approve."}, status=400
        )
    old_price = lp.locked_price_usd
    new_price = lp.pending_price_usd
    lp.locked_price_usd = new_price
    lp.pending_price_usd = None
    lp.pending_reason = ""
    lp.pending_proposed_at = None
    lp.status = "active"
    lp.save(update_fields=[
        "locked_price_usd", "pending_price_usd",
        "pending_reason", "pending_proposed_at", "status",
    ])

    PriceHistory.objects.create(
        locked=lp, event="renegotiation_approved",
        old_price=old_price, new_price=new_price,
        reason="Approved by tenant.", acted_by="tenant",
    )
    _log_activity(
        request.user, lp.partner,
        event_type="price.renegotiation_approved",
        title=f"Price approved for {lp.sku}",
        detail=f"${_dec_to_float(old_price)} → ${_dec_to_float(new_price)}.",
        icon="✓", actor="tenant",
    )
    return JsonResponse({"ok": True, "locked_price": _locked_price_json(lp)})


@login_required(login_url="/login/")
@require_POST
def api_catalog_reject_renegotiation(request, locked_id):
    lp = get_object_or_404(LockedPrice, pk=locked_id, tenant=request.user)
    body = _parse_body(request)
    reason = (body.get("reason") or "").strip()
    pending = lp.pending_price_usd
    lp.pending_price_usd = None
    lp.pending_reason = ""
    lp.pending_proposed_at = None
    lp.status = "active"
    lp.save(update_fields=[
        "pending_price_usd", "pending_reason", "pending_proposed_at", "status",
    ])

    PriceHistory.objects.create(
        locked=lp, event="renegotiation_rejected",
        old_price=lp.locked_price_usd,
        new_price=pending if pending is not None else lp.locked_price_usd,
        reason=reason, acted_by="tenant",
    )
    _log_activity(
        request.user, lp.partner,
        event_type="price.renegotiation_rejected",
        title=f"Price renegotiation rejected for {lp.sku}",
        detail=reason or "No reason provided.",
        icon="✗", actor="tenant",
    )
    return JsonResponse({"ok": True, "locked_price": _locked_price_json(lp)})


# ─── STOCK ──────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_stock_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = VendorStockItem.objects.filter(tenant=request.user, partner=partner)
    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "stock": [_stock_json(s) for s in qs],
    })


@login_required(login_url="/login/")
@require_POST
def api_stock_reorder(request, stock_id):
    s = get_object_or_404(VendorStockItem, pk=stock_id, tenant=request.user)
    body = _parse_body(request)
    try:
        qty = int(body.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return JsonResponse(
            {"ok": False, "error": "quantity must be > 0."}, status=400
        )
    s.qty_inbound = (s.qty_inbound or 0) + qty
    s.save(update_fields=["qty_inbound", "last_updated"])

    _log_activity(
        request.user, s.partner,
        event_type="stock.reorder_requested",
        title=f"Reorder requested: {qty} × {s.sku}",
        detail=f"Inbound now {s.qty_inbound}.",
        icon="⤴", actor="tenant",
    )
    return JsonResponse({"ok": True, "stock": _stock_json(s)})


@login_required(login_url="/login/")
@require_POST
def api_stock_update_threshold(request, stock_id):
    s = get_object_or_404(VendorStockItem, pk=stock_id, tenant=request.user)
    body = _parse_body(request)
    try:
        threshold = int(body.get("low_threshold") or 0)
    except (TypeError, ValueError):
        threshold = 0
    if threshold < 0:
        threshold = 0
    s.low_threshold = threshold
    s.save(update_fields=["low_threshold", "last_updated"])
    return JsonResponse({"ok": True, "stock": _stock_json(s)})


# ─── PAYMENTS ───────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_payments_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = VendorPayment.objects.filter(tenant=request.user, partner=partner)

    now = timezone.now()
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )

    lifetime = qs.filter(status="settled").aggregate(
        s=Sum("amount_usd"))["s"] or Decimal("0")
    this_month = qs.filter(
        status="settled", created_at__gte=month_start,
    ).aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
    pending = qs.filter(status="pending").aggregate(
        s=Sum("amount_usd"))["s"] or Decimal("0")
    refunded = qs.filter(status="refunded").aggregate(
        s=Sum("amount_usd"))["s"] or Decimal("0")

    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "summary": {
            "lifetime_usd":    float(lifetime),
            "this_month_usd":  float(this_month),
            "pending_usd":     float(pending),
            "refunded_usd":    float(refunded),
        },
        "payments": [_payment_json(p) for p in qs],
    })


# ─── DOCUMENTS ──────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_documents_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = VendorDocument.objects.filter(tenant=request.user, partner=partner)
    grouped = defaultdict(list)
    for d in qs:
        grouped[d.category].append(_document_json(d))
    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "categories": dict(grouped),
    })


@login_required(login_url="/login/")
@require_POST
def api_documents_upload(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)

    category = (request.POST.get("category") or "spec").strip()
    valid_cats = {c[0] for c in VendorDocument.CATEGORY_CHOICES}
    if category not in valid_cats:
        category = "other"

    title = (request.POST.get("title") or "").strip()[:200]
    description = (request.POST.get("description") or "").strip()
    external_url = (request.POST.get("external_url") or "").strip()
    f = request.FILES.get("file")

    if not title:
        return JsonResponse(
            {"ok": False, "error": "title is required."}, status=400
        )
    if not f and not external_url:
        return JsonResponse(
            {"ok": False, "error": "Provide a file or external_url."}, status=400
        )

    doc_kwargs = dict(
        tenant=request.user, partner=partner,
        category=category, title=title,
        description=description, external_url=external_url,
        uploaded_by="tenant",
    )
    if f:
        doc_kwargs["file"] = f
        doc_kwargs["filename"] = f.name[:240]
        doc_kwargs["filesize_bytes"] = f.size
    elif external_url:
        # Try to derive a filename from the URL.
        try:
            tail = external_url.rstrip("/").rsplit("/", 1)[-1][:240]
            doc_kwargs["filename"] = tail
        except Exception:
            doc_kwargs["filename"] = ""

    doc = VendorDocument.objects.create(**doc_kwargs)

    _log_activity(
        request.user, partner,
        event_type="document.uploaded",
        title=f"Document uploaded: {title}",
        detail=f"Category: {doc.get_category_display()}.",
        icon="📄", actor="tenant",
    )
    return JsonResponse({"ok": True, "document": _document_json(doc)})


@login_required(login_url="/login/")
@require_http_methods(["POST", "DELETE"])
def api_documents_delete(request, doc_id):
    doc = get_object_or_404(VendorDocument, pk=doc_id, tenant=request.user)
    title = doc.title
    partner = doc.partner
    doc.delete()
    _log_activity(
        request.user, partner,
        event_type="document.deleted",
        title=f"Document deleted: {title}",
        icon="🗑", actor="tenant",
    )
    return JsonResponse({"ok": True})


# ─── PERFORMANCE ────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_performance(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    orders = VendorOrder.objects.filter(tenant=request.user, partner=partner)

    delivered = orders.filter(status="delivered")
    shipped_or_delivered = orders.filter(status__in=["shipped", "delivered"])
    total_orders = shipped_or_delivered.count()

    active_orders = orders.filter(
        status__in=list(VendorOrder.STATUS_ACTIVE)
    ).count()

    lifetime_statuses = ["paid", "ordered_china", "in_transit",
                         "at_warehouse", "shipped", "delivered"]
    lifetime = orders.filter(status__in=lifetime_statuses).aggregate(
        s=Sum("tenant_total_usd")
    )["s"] or Decimal("0")

    # On-time pct over delivered.
    on_time_pct = None
    avg_lead_actual = None
    delivered_list = list(delivered)
    if delivered_list:
        on_time = 0
        lead_days_sum = 0
        lead_days_n = 0
        for o in delivered_list:
            if o.delivered_at and o.created_at and o.lead_days_quoted is not None:
                deadline = o.created_at + timedelta(days=o.lead_days_quoted)
                if o.delivered_at <= deadline:
                    on_time += 1
            if o.delivered_at and o.paid_at:
                lead_days_sum += (o.delivered_at - o.paid_at).days
                lead_days_n += 1
        on_time_pct = round(on_time / len(delivered_list) * 100, 1)
        avg_lead_actual = (round(lead_days_sum / lead_days_n, 1)
                           if lead_days_n else None)

    # Reorder rate: distinct SKUs with >1 order / total distinct SKUs.
    sku_counts = (orders.values("product_sku")
                  .annotate(n=Count("id"))
                  .order_by())
    distinct_skus = sku_counts.count()
    repeat_skus = sum(1 for r in sku_counts if r["n"] > 1)
    reorder_rate = (round(repeat_skus / distinct_skus * 100, 1)
                    if distinct_skus else None)

    # Quote acceptance: paid+ / (quote_received + paid+ + quote_rejected)
    decided_statuses = ["quote_received", "quote_rejected", "paid",
                        "ordered_china", "in_transit", "at_warehouse",
                        "shipped", "delivered"]
    accepted_statuses = ["paid", "ordered_china", "in_transit",
                         "at_warehouse", "shipped", "delivered"]
    decided = orders.filter(status__in=decided_statuses).count()
    accepted = orders.filter(status__in=accepted_statuses).count()
    quote_acceptance_pct = (round(accepted / decided * 100, 1)
                            if decided else None)

    # Avg margin pct over paid+ orders.
    paid_orders = list(orders.filter(status__in=accepted_statuses))
    if paid_orders:
        avg_margin = sum(float(o.margin_pct or 0) for o in paid_orders) / len(paid_orders)
        avg_margin_pct = round(avg_margin, 1)
    else:
        avg_margin_pct = None

    # Top 5 SKUs (by count) with revenue.
    top_skus_qs = (orders.filter(status__in=accepted_statuses)
                   .values("product_sku", "product_name")
                   .annotate(count=Count("id"),
                             revenue=Sum("tenant_total_usd"))
                   .order_by("-count")[:5])
    top_5_skus = [
        {
            "sku":           row["product_sku"],
            "product_name":  row["product_name"],
            "count":         row["count"],
            "total_revenue": float(row["revenue"] or 0),
        }
        for row in top_skus_qs
    ]

    # Last 30 days timeline (created_at-based, count+revenue).
    today = timezone.now().date()
    start = today - timedelta(days=29)
    buckets = {(start + timedelta(days=i)).isoformat(): {"count": 0, "revenue": 0.0}
               for i in range(30)}
    recent = orders.filter(created_at__date__gte=start)
    for o in recent:
        key = o.created_at.date().isoformat()
        if key in buckets:
            buckets[key]["count"] += 1
            buckets[key]["revenue"] += float(o.tenant_total_usd or 0)
    last_30 = [{"date": d, "count": v["count"], "revenue": v["revenue"]}
               for d, v in buckets.items()]

    # Reviews summary.
    reviews_qs = PartnerReview.objects.filter(
        tenant=request.user, partner=partner,
    )
    review_count = reviews_qs.count()
    avg_rating = reviews_qs.aggregate(a=Avg("rating"))["a"]
    recent_reviews = list(reviews_qs[:3])

    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "total_orders":          total_orders,
        "active_orders":         active_orders,
        "lifetime_spent_usd":    float(lifetime),
        "on_time_pct":           on_time_pct,
        "avg_lead_days_actual":  avg_lead_actual,
        "reorder_rate":          reorder_rate,
        "quote_acceptance_pct":  quote_acceptance_pct,
        "avg_margin_pct":        avg_margin_pct,
        "top_5_skus":            top_5_skus,
        "last_30_days_timeline": last_30,
        "reviews": {
            "avg_rating": float(avg_rating) if avg_rating is not None else None,
            "count":      review_count,
            "recent":     [_review_json(r) for r in recent_reviews],
        },
    })


# ─── AUTO-RULES ─────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_rules_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = AutoRule.objects.filter(tenant=request.user, partner=partner)
    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "rules": [_rule_json(r) for r in qs],
    })


@login_required(login_url="/login/")
@require_POST
def api_rules_create(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    body = _parse_body(request)
    rule_type = (body.get("rule_type") or "").strip()
    valid_types = {c[0] for c in AutoRule.RULE_TYPE_CHOICES}
    if rule_type not in valid_types:
        return JsonResponse(
            {"ok": False, "error": f"Invalid rule_type. Must be one of {sorted(valid_types)}."},
            status=400,
        )

    config = body.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = {}
    if not isinstance(config, dict):
        config = {}

    is_active_v = body.get("is_active", True)
    if isinstance(is_active_v, str):
        is_active = is_active_v.lower() in ("1", "true", "yes", "on")
    else:
        is_active = bool(is_active_v)

    rule = AutoRule.objects.create(
        tenant=request.user, partner=partner,
        rule_type=rule_type, is_active=is_active, config=config,
    )
    _log_activity(
        request.user, partner,
        event_type="rule.created",
        title=f"Auto-rule created: {rule.get_rule_type_display()}",
        icon="⚙", actor="tenant",
    )
    return JsonResponse({"ok": True, "rule": _rule_json(rule)})


@login_required(login_url="/login/")
@require_POST
def api_rules_toggle(request, rule_id):
    rule = get_object_or_404(AutoRule, pk=rule_id, tenant=request.user)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active"])
    return JsonResponse({"ok": True, "rule": _rule_json(rule)})


@login_required(login_url="/login/")
@require_POST
def api_rules_update(request, rule_id):
    rule = get_object_or_404(AutoRule, pk=rule_id, tenant=request.user)
    body = _parse_body(request)
    updated_fields = []
    if "config" in body:
        config = body.get("config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        if isinstance(config, dict):
            rule.config = config
            updated_fields.append("config")
    if "is_active" in body:
        v = body.get("is_active")
        if isinstance(v, str):
            rule.is_active = v.lower() in ("1", "true", "yes", "on")
        else:
            rule.is_active = bool(v)
        updated_fields.append("is_active")
    if updated_fields:
        rule.save(update_fields=updated_fields)
    return JsonResponse({"ok": True, "rule": _rule_json(rule)})


@login_required(login_url="/login/")
@require_http_methods(["POST", "DELETE"])
def api_rules_delete(request, rule_id):
    rule = get_object_or_404(AutoRule, pk=rule_id, tenant=request.user)
    rule.delete()
    return JsonResponse({"ok": True})


# ─── ACTIVITY ───────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_activity_list(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    qs = (WorkspaceActivity.objects
          .filter(tenant=request.user, partner=partner)
          .order_by("-created_at")[:50])
    return JsonResponse({
        "ok": True,
        "partner_id": partner.id,
        "activity": [_activity_json(a) for a in qs],
    })


# ─── REVIEWS ────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def api_review_submit(request, partner_id):
    partner = get_object_or_404(SourcingPartner, pk=partner_id, is_active=True)
    body = _parse_body(request)

    def _clamp(value, default=5):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(5, n))

    related_order = None
    order_ref = (body.get("order_ref") or "").strip()
    if order_ref:
        related_order = VendorOrder.objects.filter(
            tenant=request.user, order_ref=order_ref,
        ).first()

    is_public_v = body.get("is_public", False)
    if isinstance(is_public_v, str):
        is_public = is_public_v.lower() in ("1", "true", "yes", "on")
    else:
        is_public = bool(is_public_v)

    review = PartnerReview.objects.create(
        tenant=request.user, partner=partner,
        related_order=related_order,
        rating=_clamp(body.get("rating"), 5),
        comm_score=_clamp(body.get("comm_score"), 5),
        quality_score=_clamp(body.get("quality_score"), 5),
        delivery_score=_clamp(body.get("delivery_score"), 5),
        comment=(body.get("comment") or "").strip(),
        is_public=is_public,
    )

    # Recompute partner stats.
    agg = PartnerReview.objects.filter(partner=partner).aggregate(
        avg=Avg("rating"), n=Count("id"),
    )
    if agg["avg"] is not None:
        partner.rating = Decimal(str(round(agg["avg"], 2)))
    partner.total_reviews = agg["n"] or 0
    partner.save(update_fields=["rating", "total_reviews"])

    _log_activity(
        request.user, partner,
        event_type="review.submitted",
        title=f"Review submitted ({review.rating}/5)",
        detail=review.comment[:300],
        icon="★", actor="tenant",
        related_order=related_order,
    )
    return JsonResponse({"ok": True, "review": _review_json(review)})


# ─── WORKSPACE SUMMARY ──────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_GET
def api_workspace_summary(request):
    """Tenant-wide aggregate for the left rail."""
    # All partners the tenant has interacted with (orders, conversations,
    # locked prices). Returning all active partners is simpler and matches
    # how the list view works; we'll only show counts > 0 in the UI.
    partners = SourcingPartner.objects.filter(is_active=True)

    convs = {
        c.partner_id: c for c in
        PartnerConversation.objects.filter(
            tenant=request.user, is_archived=False,
        )
    }

    open_status = list(VendorOrder.STATUS_ACTIVE)
    partner_rows = []
    total_open = 0
    total_quote_pending = 0
    total_in_transit = 0
    total_disputes = 0

    for p in partners:
        orders_qs = VendorOrder.objects.filter(
            tenant=request.user, partner=p,
        )
        open_orders = orders_qs.filter(status__in=open_status).count()
        quote_pending = orders_qs.filter(status="quote_received").count()
        in_transit = orders_qs.filter(status="in_transit").count()
        disputes = orders_qs.filter(status="disputed").count()

        conv = convs.get(p.id)
        unread = conv.unread_count_for_tenant if conv else 0

        # Skip partners with zero engagement to keep payload small.
        if (open_orders == 0 and quote_pending == 0
                and unread == 0 and in_transit == 0 and disputes == 0):
            continue

        partner_rows.append({
            "id":            p.id,
            "name":          p.name,
            "initials":      p.initials,
            "gradient_from": p.cover_gradient_from,
            "gradient_to":   p.cover_gradient_to,
            "cover_emoji":   p.cover_emoji,
            "open_orders":   open_orders,
            "quote_pending": quote_pending,
            "unread_chat":   unread,
        })
        total_open += open_orders
        total_quote_pending += quote_pending
        total_in_transit += in_transit
        total_disputes += disputes

    return JsonResponse({
        "ok": True,
        "partners": partner_rows,
        "totals": {
            "open_orders":   total_open,
            "quote_pending": total_quote_pending,
            "in_transit":    total_in_transit,
            "disputes":      total_disputes,
        },
    })


# ═══════════════════════════════════════════════════════════════════════════
# AGGREGATE ("Drop Sigma Sourcing" unified front)
#
# These endpoints fold the 8 underlying SourcingPartner rows into one
# brand — "Drop Sigma Sourcing" — and surface each partner as a
# "Specialist Desk" (Electronics Desk, Apparel Desk, …). The tenant sees
# one trusted operation; specialization is shown via desk tags.
#
# All aggregate endpoints accept ?desk=<partner_id> to filter to a single
# desk. Counts always reflect totals before filtering so the UI can show
# both "total" and "filtered" views.
# ═══════════════════════════════════════════════════════════════════════════

def _desk_label(partner):
    """Convert a SourcingPartner into a Drop Sigma desk label."""
    specialties = partner.specialties or []
    primary = specialties[0] if specialties else "Sourcing"
    return f"{primary} Desk"


def _default_partner_for_tenant(user):
    """Pick ONE underlying SourcingPartner to act as Drop Sigma Sourcing.

    Tenant never sees per-partner identity — to them this is a single brand
    handled by a 200+ specialist team. Internally we still need a real
    SourcingPartner FK for orders/messages/uploads, so we pick the most-used
    partner for this tenant (stable conversation), falling back to the first
    active one.
    """
    used = (VendorOrder.objects.filter(tenant=user)
            .values("partner_id")
            .annotate(c=Count("id"))
            .order_by("-c")
            .values_list("partner_id", flat=True)
            .first())
    if used:
        p = SourcingPartner.objects.filter(id=used, is_active=True).first()
        if p:
            return p
    return SourcingPartner.objects.filter(is_active=True).order_by("id").first()


def _desk_brief(partner):
    """Compact JSON for a desk — used in tags + sidebar."""
    return {
        "id":            partner.id,
        "name":          _desk_label(partner),
        "subtitle":      partner.headquarters or partner.country or "",
        "emoji":         partner.cover_emoji,
        "gradient_from": partner.cover_gradient_from,
        "gradient_to":   partner.cover_gradient_to,
        "specialties":   partner.specialties or [],
    }


def _attach_desk(rows, partner_field="partner"):
    """Decorate a list of dict rows with desk_id/desk_name/desk_emoji."""
    # When called we know each row already has the FK id; we look up partners.
    partner_ids = {r.get(partner_field + "_id") for r in rows
                   if r.get(partner_field + "_id")}
    if not partner_ids:
        return rows
    partners = {p.id: p for p in SourcingPartner.objects.filter(id__in=partner_ids)}
    for r in rows:
        pid = r.get(partner_field + "_id")
        p = partners.get(pid)
        if p:
            r["desk_id"] = p.id
            r["desk_name"] = _desk_label(p)
            r["desk_emoji"] = p.cover_emoji
    return rows


def _filter_desk(qs, request):
    """Apply ?desk=<partner_id> filter to a queryset that has partner FK."""
    desk = (request.GET.get("desk") or "").strip()
    if desk and desk.isdigit():
        return qs.filter(partner_id=int(desk))
    return qs


# ─── Overview (hero dashboard) ─────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_overview(request):
    """One-shot snapshot: hero stats + desk list + recent activity peek."""
    user = request.user
    partners = list(SourcingPartner.objects.filter(is_active=True))

    orders_qs = VendorOrder.objects.filter(tenant=user)
    payments_qs = VendorPayment.objects.filter(tenant=user)
    stock_qs = VendorStockItem.objects.filter(tenant=user)

    open_status = list(VendorOrder.STATUS_ACTIVE)

    active_orders = orders_qs.filter(status__in=open_status).count()
    awaiting_quote = orders_qs.filter(status="awaiting_quote").count()
    quote_received = orders_qs.filter(status="quote_received").count()
    in_transit = orders_qs.filter(status="in_transit").count()
    delivered = orders_qs.filter(status="delivered").count()
    disputes = orders_qs.filter(status="disputed").count()

    lifetime_spent = _dec_to_float(
        payments_qs.filter(status="settled").aggregate(s=Sum("amount_usd"))["s"]
        or Decimal("0")
    )

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month_spent = _dec_to_float(
        payments_qs.filter(status="settled", settled_at__gte=month_start)
        .aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
    )

    low_stock = stock_qs.filter(qty_on_hand__lte=F("low_threshold")).count()

    unread_chat = sum(
        c.unread_count_for_tenant for c in
        PartnerConversation.objects.filter(tenant=user, is_archived=False)
    )

    # Desk list with per-desk badge counts
    desks = []
    for p in partners:
        p_orders = orders_qs.filter(partner=p)
        desks.append({
            **_desk_brief(p),
            "open_orders":   p_orders.filter(status__in=open_status).count(),
            "quote_pending": p_orders.filter(status="quote_received").count(),
            "rating":        float(p.rating),
        })

    # On-time % across all delivered orders
    on_time = 0.0
    delivered_qs = orders_qs.filter(status="delivered", delivered_at__isnull=False,
                                     lead_days_quoted__isnull=False)
    delivered_total = delivered_qs.count()
    if delivered_total:
        on_time_count = 0
        for o in delivered_qs:
            if not o.created_at or not o.lead_days_quoted:
                continue
            expected = o.created_at + timedelta(days=o.lead_days_quoted)
            if o.delivered_at <= expected:
                on_time_count += 1
        on_time = round((on_time_count / delivered_total) * 100, 1)

    # Pick ONE underlying partner to act as the Drop Sigma identity for new
    # actions (order creation, chat, uploads). Tenant never sees the per-
    # partner identity.
    default_partner = _default_partner_for_tenant(user)
    default_conv = None
    if default_partner:
        default_conv = (PartnerConversation.objects
                        .filter(tenant=user, partner=default_partner,
                                is_archived=False)
                        .first())
        # Lazy-create the conversation on first overview hit so the
        # chat panel renders immediately for tenants whose workspace
        # wasn't pre-seeded (e.g. brand-new sign-ups). The conversation
        # is owned by this tenant and routes to the picked partner —
        # exactly what seed_workspace would have created — so messages
        # sent through it work the same way.
        if not default_conv:
            try:
                default_conv = PartnerConversation.objects.create(
                    tenant=user, partner=default_partner, is_archived=False,
                )
            except Exception:
                default_conv = None

    return JsonResponse({
        "ok": True,
        "brand": {
            "name":     "Drop Sigma Sourcing",
            "tagline":  "200+ specialist team handling your fulfillment, end-to-end.",
            "team":     "200+",
        },
        "stats": {
            "active_orders":     active_orders,
            "awaiting_quote":    awaiting_quote,
            "quote_received":    quote_received,
            "in_transit":        in_transit,
            "delivered":         delivered,
            "disputes":          disputes,
            "lifetime_spent":    lifetime_spent,
            "this_month_spent":  this_month_spent,
            "low_stock":         low_stock,
            "unread_chat":       unread_chat,
            "on_time_pct":       on_time,
        },
        # Internal routing fields — frontend uses these to send new orders,
        # uploads, and chat messages to ONE partner without surfacing it.
        "default_partner_id":     default_partner.id if default_partner else None,
        "default_conversation_id": default_conv.id if default_conv else None,
    })


# ─── Aggregate orders ─────────────────────────────────────────────────────
# The 6 tenant-facing sourcing groups (matches Order.SOURCING_STATUS_CHOICES).
ORDER_STATUS_GROUPS = [
    ("pending_source",  "Pending Source"),
    ("pending_payment", "Pending Payment"),
    ("processing",      "Processing"),
    ("shipping",        "Shipping"),
    ("delivered",       "Delivered"),
    ("cancel",          "Cancel"),
]
_GROUP_KEYS = {key for key, _ in ORDER_STATUS_GROUPS}


def _extract_line_items(order):
    """Pull line items out of raw_data, normalising Shopify + WC shapes.

    Returns a list of dicts: {sku, product_name, variant, image, quantity, price}
    Variants are surfaced as human strings ("Size: M · Color: Black").
    """
    raw = order.raw_data or {}
    if not isinstance(raw, dict):
        return []
    items_raw = raw.get("line_items") or raw.get("items") or []
    if not isinstance(items_raw, list):
        return []

    out = []
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        # Shopify embeds variant_title directly; WC stuffs it in meta_data.
        variant_bits = []
        vt = (it.get("variant_title") or "").strip()
        if vt and vt.lower() not in ("default", "default title", ""):
            variant_bits.append(vt)
        meta = it.get("meta_data") or it.get("meta") or []
        if isinstance(meta, list):
            for m in meta:
                if not isinstance(m, dict):
                    continue
                k = (m.get("display_key") or m.get("key") or "").strip()
                v = (m.get("display_value") or m.get("value") or "")
                if not k or not v or k.startswith("_"):
                    continue
                v = str(v).strip()
                if not v:
                    continue
                variant_bits.append(f"{k}: {v}")
        # Shopify also publishes properties: list of {name, value}
        props = it.get("properties") or []
        if isinstance(props, list):
            for p in props:
                if isinstance(p, dict) and p.get("name") and p.get("value"):
                    variant_bits.append(f"{p['name']}: {p['value']}")

        img = ""
        if isinstance(it.get("image"), dict):
            img = it["image"].get("src") or it["image"].get("url") or ""
        elif isinstance(it.get("image"), str):
            img = it["image"]

        out.append({
            "sku":          (it.get("sku") or "").strip(),
            "product_name": (it.get("name") or it.get("title")
                             or it.get("product_name") or "").strip(),
            "variant":      " · ".join(variant_bits),
            "image":        img,
            "quantity":     int(it.get("quantity") or 1),
            "price":        _dec_to_float(it.get("price") or it.get("total") or 0),
        })
    return out


def _order_row_json(o, *, include_address=False):
    """Tenant-facing serializer for one Order in the Sourcing > Orders view.

    NOTE: Drop Sigma's internal partner/desk identity is NEVER included.
    """
    items = _extract_line_items(o)
    # Primary item display (rows show first item summary + "+N more" count)
    first = items[0] if items else {}
    image = first.get("image") or ""
    product = first.get("product_name") or o.product_name or "(no name)"
    sku = first.get("sku") or ""
    variant = first.get("variant") or ""
    qty = first.get("quantity") or 1
    addr = o.shipping_address or {}

    # Resolved address: if the tenant has saved an override (pre-payment),
    # that takes precedence over raw_data's snapshot.
    override = o.shipping_override if isinstance(o.shipping_override, dict) else None
    if override:
        addr = {**addr, **override}

    eta = None
    if (o.sourcing_status in ("processing", "shipping") and o.sourcing_paid_at
            and o.sourcing_lead_days):
        eta_date = o.sourcing_paid_at + timedelta(days=int(o.sourcing_lead_days))
        eta = eta_date.strftime("%b %d")

    row = {
        "id":                 o.id,
        "ds_order_ref":       o.ds_order_ref,
        "source_order_ref":   o.external_order_id,
        "store_platform":     getattr(o.store, "platform", "") if o.store_id else "",
        "store_name":         getattr(o.store, "store_name", "") or getattr(o.store, "name", "") if o.store_id else "",
        "customer_name":      addr.get("name") or o.customer_name or "",
        "customer_phone":     addr.get("phone") or o.customer_phone or "",
        "customer_email":     addr.get("email") or o.customer_email or "",
        "ship_line1":         addr.get("line1") or "",
        "ship_line2":         addr.get("line2") or "",
        "ship_state":         addr.get("state") or "",
        "ship_postal":        addr.get("postal_code") or "",
        "ship_city":          addr.get("city") or o.city or "",
        "ship_country":       addr.get("country") or o.country or "",
        "product_name":       product,
        "product_sku":        sku,
        "product_image_url":  image,
        "variant":            variant,
        "quantity":           qty,
        "item_count":         len(items),
        "items":              items,  # full list (used by modal)
        "sourcing_status":    o.sourcing_status,
        "sourcing_total_usd":    _dec_to_float(o.sourcing_total_usd),
        "sourcing_product_usd":  _dec_to_float(o.sourcing_product_usd),
        "sourcing_shipping_usd": _dec_to_float(o.sourcing_shipping_usd),
        "has_quote":             bool(o.sourcing_total_usd and o.sourcing_total_usd > 0),
        "lead_days":          o.sourcing_lead_days,
        "eta":                eta,
        "tracking_number":    o.tracking_number or "",
        # The link the tenant (and via shipping-notification email,
        # their customer) opens is ALWAYS Drop Sigma's branded tracking
        # page. We never leak the raw carrier URL outside of the
        # vendor-submission / admin-debug paths. See
        # orders/tracking_link.py for the contract.
        "tracking_url":       build_ds_tracking_link(o),
        # safe_carrier_name collapses cross-border supplier brands
        # to "Drop Sigma" before the tenant sees the carrier.
        "tracking_company":   safe_carrier_name(o.tracking_company or ""),
        "is_shipping_editable": o.is_shipping_editable,
        "is_tenant_deletable": o.is_tenant_deletable,
        "is_deleted":         o.is_deleted,
        "deleted_at_iso":     _iso(o.deleted_at),
        "created_at_iso":     _iso(o.created_at),
        "paid_at_iso":        _iso(o.sourcing_paid_at),
        "delivered_at_iso":   _iso(o.sourcing_delivered_at),
    }
    if include_address:
        row["shipping_address"] = addr
    return row


@login_required(login_url="/login/")
@require_GET
def api_aggregate_orders(request):
    """Tenant-facing orders list — backed by Order (Shopify + WC synced).

    Soft-delete behaviour:

    * Default (every status chip from "all" through "cancel"): only LIVE
      rows (``is_deleted=False``). The tenant should never see orders
      they previously removed mixed into their working queue.
    * ``?group=deleted``: ONLY soft-deleted rows. This is the dedicated
      Deleted bucket the tenant uses to restore orders back into the
      live queue. Counts in ``group_counts['deleted']`` reflect the
      same set.
    """
    user = request.user
    grp = (request.GET.get("group") or "").strip()
    deleted_view = grp == "deleted"

    base_qs = Order.objects.filter(store__user=user).select_related("store")
    base_qs = base_qs.filter(is_deleted=deleted_view)

    # Counts over both buckets so the chip row can show "Deleted (N)"
    # without paying for a second round-trip. Live counts exclude
    # deleted, deleted bucket count is its own number.
    live_counts_qs = (Order.objects.filter(store__user=user, is_deleted=False)
                      .values("sourcing_status").annotate(n=Count("id")))
    group_counts = {row["sourcing_status"]: row["n"] for row in live_counts_qs}
    group_counts["all"] = sum(group_counts.get(g, 0) for g, _ in ORDER_STATUS_GROUPS)
    group_counts["deleted"] = (Order.objects
                               .filter(store__user=user, is_deleted=True).count())

    qs = base_qs
    if not deleted_view and grp and grp in _GROUP_KEYS:
        qs = qs.filter(sourcing_status=grp)

    # Search (DS#, customer #, customer name, product name)
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        # DS-000123 → strip prefix to int id match
        ds_match = None
        if q.upper().startswith("DS-"):
            try:
                ds_match = int(q[3:])
            except ValueError:
                ds_match = None
        filters = (Q(external_order_id__icontains=q) |
                   Q(customer_name__icontains=q) |
                   Q(customer_email__icontains=q) |
                   Q(product_name__icontains=q))
        if ds_match is not None:
            filters = filters | Q(id=ds_match)
        qs = qs.filter(filters)

    # Sort
    sort = (request.GET.get("sort") or "newest").strip()
    if sort == "amount":
        qs = qs.order_by(F("sourcing_total_usd").desc(nulls_last=True), "-created_at")
    elif sort == "customer":
        qs = qs.order_by("customer_name", "-created_at")
    else:
        qs = qs.order_by("-created_at")

    # ── Pagination ────────────────────────────────────────────────
    # The Sourcing Partners portal renders the same Drop-Sigma pager
    # the OPS Queue uses (templates/dashboard.html ↔ ds-pager /
    # ds-loader components). Default 25 rows, capped at 100. Total +
    # total_pages ride alongside the per-status group_counts so the
    # UI never confuses "visible right now" with "exists at all".
    try:
        page_size = int(request.GET.get("page_size") or 25)
    except (TypeError, ValueError):
        page_size = 25
    page_size = max(1, min(page_size, 100))
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    total = qs.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end   = start + page_size
    rows  = [_order_row_json(o) for o in qs[start:end]]

    return JsonResponse({
        "ok": True,
        "group_counts": group_counts,
        "groups": [{"key": k, "label": l} for k, l in ORDER_STATUS_GROUPS],
        "orders": rows,
        "pagination": {
            "page":        page,
            "page_size":   page_size,
            "total":       total,
            "total_pages": total_pages,
        },
    })


# ═════════════════════════════════════════════════════════════════════════════
# WALLET, EDIT SHIPPING, SINGLE + BULK PAY (single Drop Sigma front)
# ═════════════════════════════════════════════════════════════════════════════

TRIAL_WALLET_USD = Decimal("1000.00")


def _ensure_wallet(user):
    """Get-or-seed the tenant's wallet. New tenants get $1000 trial credit."""
    wallet, created = TenantWallet.objects.get_or_create(
        tenant=user, defaults={"balance_usd": TRIAL_WALLET_USD},
    )
    if created:
        WalletTransaction.objects.create(
            wallet=wallet, kind="topup",
            amount_usd=TRIAL_WALLET_USD,
            balance_after=wallet.balance_usd,
            reference="trial_credit",
            note="Drop Sigma trial wallet credit",
        )
    return wallet


def _serialize_wallet(w):
    return {
        "balance_usd": _dec_to_float(w.balance_usd),
        "currency":    w.currency,
    }


@login_required(login_url="/login/")
@require_GET
def api_wallet(request):
    w = _ensure_wallet(request.user)
    txns = w.transactions.all()[:50]
    return JsonResponse({
        "ok": True,
        "wallet": _serialize_wallet(w),
        "transactions": [{
            "kind":          t.kind,
            "kind_label":    t.get_kind_display(),
            "amount_usd":    _dec_to_float(t.amount_usd),
            "balance_after": _dec_to_float(t.balance_after),
            "reference":     t.reference,
            "note":          t.note,
            "order_id":      t.related_order_id,
            "order_ref":     t.related_order.ds_order_ref if t.related_order_id else "",
            "created_at_iso": _iso(t.created_at),
        } for t in txns],
    })


# ─── Wallet top-up (Stripe one-time + PayPal) ───────────────────────────────
#
# Tenants fund their wallet from the dashboard Wallet tab. Two rails:
#   • Stripe Checkout (mode="payment", one-time card charge)
#   • PayPal Orders v2 (CAPTURE intent)
# Both credit TenantWallet and write a WalletTransaction(kind="topup"). Credit
# is idempotent on `reference` (stripe:<session_id> / paypal:<order_id>) so a
# refreshed success page or a double-captured order never double-credits.

MIN_TOPUP_USD = Decimal("5.00")
MAX_TOPUP_USD = Decimal("50000.00")


def _parse_topup_amount(raw):
    """Validate a requested top-up amount. Returns (amount|None, error|None)."""
    try:
        amount = Decimal(str(raw or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None, "Enter a valid amount."
    if amount < MIN_TOPUP_USD:
        return None, f"Minimum top-up is ${MIN_TOPUP_USD:.0f}."
    if amount > MAX_TOPUP_USD:
        return None, f"Maximum top-up is ${MAX_TOPUP_USD:,.0f}."
    return amount, None


def _credit_topup(user, amount_usd, reference, note):
    """Idempotently credit the wallet for a completed top-up.

    Returns (wallet, created). `created` is False when a top-up with this
    reference was already booked (so the caller knows not to double-count).
    """
    with transaction.atomic():
        wallet = _ensure_wallet(user)
        if reference and WalletTransaction.objects.filter(
            wallet=wallet, reference=reference, kind="topup",
        ).exists():
            return wallet, False
        wallet = TenantWallet.objects.select_for_update().get(pk=wallet.pk)
        wallet.balance_usd = (wallet.balance_usd + amount_usd)
        wallet.save(update_fields=["balance_usd", "updated_at"])
        WalletTransaction.objects.create(
            wallet=wallet, kind="topup",
            amount_usd=amount_usd, balance_after=wallet.balance_usd,
            reference=reference, note=note,
        )
    return wallet, True


def _dashboard_wallet_redirect(state):
    return redirect(f"/dashboard/?section=sourcingPartners&view=wallet&topup={state}")


@login_required(login_url="/login/")
@require_POST
def api_wallet_topup_stripe(request):
    """Create a Stripe Checkout Session (one-time) for a wallet top-up.
    Returns the hosted-checkout URL for the frontend to redirect to."""
    import stripe
    from core.views import _platform_creds

    body = _parse_body(request)
    amount, err = _parse_topup_amount(body.get("amount"))
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)

    creds  = _platform_creds()
    secret = creds["stripe_secret"]
    if not secret or secret.startswith("sk_test_your"):
        return JsonResponse(
            {"ok": False, "error": "Card payments aren't configured yet."},
            status=400,
        )
    stripe.api_key = secret

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    base   = f"{scheme}://{host}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            client_reference_id=str(request.user.id),
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(round(float(amount) * 100)),
                    "product_data": {"name": "Drop Sigma Wallet Top-up"},
                },
            }],
            success_url=f"{base}/sourcing-partners/wallet/topup/stripe/success/?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/dashboard/?section=sourcingPartners&view=wallet&topup=cancel",
            metadata={
                "django_user_id": str(request.user.id),
                "purpose":        "wallet_topup",
                "amount_usd":     f"{amount:.2f}",
            },
        )
        return JsonResponse({"ok": True, "url": session.url})
    except stripe.error.AuthenticationError:
        return JsonResponse({"ok": False, "error": "Card gateway auth failed."}, status=400)
    except Exception as e:
        try:
            print(f"[api_wallet_topup_stripe] {type(e).__name__}: {e}")
        except Exception:
            pass
        return JsonResponse({"ok": False, "error": "Could not start card checkout."}, status=500)


@login_required(login_url="/login/")
@require_GET
def wallet_topup_stripe_success(request):
    """Stripe redirects here after a successful one-time top-up. Verifies the
    session was paid + belongs to this tenant, then credits the wallet."""
    import stripe
    from core.views import _platform_creds

    stripe.api_key = _platform_creds()["stripe_secret"]
    session_id = request.GET.get("session_id", "")
    if not session_id:
        return _dashboard_wallet_redirect("error")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return _dashboard_wallet_redirect("error")

    if session.get("payment_status") != "paid":
        return _dashboard_wallet_redirect("incomplete")

    meta = session.get("metadata") or {}
    if str(meta.get("django_user_id")) != str(request.user.id):
        # Session belongs to a different account — never credit cross-tenant.
        return _dashboard_wallet_redirect("error")

    try:
        amount = Decimal(str(meta.get("amount_usd") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    if amount <= 0:
        amount = (Decimal(str(session.get("amount_total") or 0)) / 100).quantize(Decimal("0.01"))
    if amount <= 0:
        return _dashboard_wallet_redirect("error")

    _credit_topup(
        request.user, amount,
        reference=f"stripe:{session_id}",
        note="Card top-up via Stripe",
    )
    return _dashboard_wallet_redirect("success")


@login_required(login_url="/login/")
@require_POST
def api_wallet_topup_paypal_create(request):
    """Create a PayPal CAPTURE order for a wallet top-up. Returns the order id
    for the PayPal JS SDK to approve."""
    import requests as _req
    from core.views import _platform_creds

    body = _parse_body(request)
    amount, err = _parse_topup_amount(body.get("amount"))
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)

    creds = _platform_creds()
    if not creds["paypal_client_id"] or not creds["paypal_client_secret"]:
        return JsonResponse(
            {"ok": False, "error": "PayPal isn't configured yet."}, status=400,
        )
    mode     = creds["paypal_mode"]
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(creds["paypal_client_id"], creds["paypal_client_secret"]),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    if token_res.status_code != 200:
        return JsonResponse({"ok": False, "error": "PayPal auth failed."}, status=500)
    access_token = token_res.json()["access_token"]

    order_res = _req.post(
        f"{base_url}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
                "description": "Drop Sigma Wallet Top-up",
                "custom_id": f"wallet_topup:{request.user.id}",
            }],
        },
        timeout=15,
    )
    order = order_res.json()
    if not order.get("id"):
        return JsonResponse({"ok": False, "error": "PayPal order failed."}, status=500)
    return JsonResponse({"ok": True, "id": order["id"], "amount": float(amount)})


@login_required(login_url="/login/")
@require_POST
def api_wallet_topup_paypal_capture(request):
    """Capture an approved PayPal top-up order and credit the wallet."""
    import requests as _req
    from core.views import _platform_creds

    body     = _parse_body(request)
    order_id = (body.get("order_id") or "").strip()
    if not order_id:
        return JsonResponse({"ok": False, "error": "Missing order id."}, status=400)

    creds    = _platform_creds()
    mode     = creds["paypal_mode"]
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(creds["paypal_client_id"], creds["paypal_client_secret"]),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    if token_res.status_code != 200:
        return JsonResponse({"ok": False, "error": "PayPal auth failed."}, status=500)
    access_token = token_res.json()["access_token"]

    cap_res = _req.post(
        f"{base_url}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        timeout=15,
    )
    cap = cap_res.json()
    if cap.get("status") != "COMPLETED":
        return JsonResponse({"ok": False, "error": "Payment not completed."}, status=400)

    # Trust the captured amount from PayPal, not the client.
    try:
        captured = cap["purchase_units"][0]["payments"]["captures"][0]["amount"]["value"]
        amount   = Decimal(str(captured)).quantize(Decimal("0.01"))
    except (KeyError, IndexError, InvalidOperation, ValueError):
        return JsonResponse({"ok": False, "error": "Could not read captured amount."}, status=500)

    wallet, _ = _credit_topup(
        request.user, amount,
        reference=f"paypal:{order_id}",
        note="Top-up via PayPal",
    )
    return JsonResponse({"ok": True, "balance_usd": _dec_to_float(wallet.balance_usd)})


@login_required(login_url="/login/")
@require_GET
def api_order_detail_sourcing(request, order_id):
    """Detail for one Order row (sourcing-facing). Tenant-scoped."""
    o = get_object_or_404(Order, pk=order_id, store__user=request.user)
    return JsonResponse({"ok": True, "order": _order_row_json(o, include_address=True)})


@login_required(login_url="/login/")
@require_POST
def api_order_shipping_update(request, order_id):
    """Tenant edits shipping address + customer name + phone. Locked once
    we've moved past pending_payment."""
    o = get_object_or_404(Order, pk=order_id, store__user=request.user)
    if not o.is_shipping_editable:
        return JsonResponse({
            "ok": False,
            "error": "Shipping is locked once the order moves past Pending Payment.",
        }, status=400)
    body = _parse_body(request)
    fields = ["name", "phone", "line1", "line2", "city", "state",
              "postal_code", "country"]
    override = o.shipping_override if isinstance(o.shipping_override, dict) else {}
    for f in fields:
        if f in body:
            v = (body.get(f) or "").strip()
            if v:
                override[f] = v
            elif f in override:
                # Allow blanking a field by sending empty string explicitly.
                del override[f]
    o.shipping_override = override
    # Also bring top-level customer_name / customer_phone in sync when the
    # tenant edits them, so the rest of the dashboard reflects the change.
    if "name" in override:
        o.customer_name = override["name"]
    if "phone" in override:
        o.customer_phone = override["phone"]
    o.save(update_fields=["shipping_override", "customer_name", "customer_phone"])
    return JsonResponse({
        "ok": True,
        "order": _order_row_json(o, include_address=True),
    })


@login_required(login_url="/login/")
@require_POST
def api_order_item_delete(request, order_id, item_index):
    """Remove a single line item from the order's raw_data.

    Allowed only while the order is in Pending Source or Pending Payment
    (anything before the wallet is charged). The deleted item's allocated
    share of `sourcing_product_usd` is subtracted; shipping cost stays
    unchanged, as the tenant requested.
    """
    o = get_object_or_404(Order, pk=order_id, store__user=request.user)
    if not o.is_shipping_editable:
        return JsonResponse({
            "ok": False,
            "error": "Items can't be removed once the order moves past Pending Payment.",
        }, status=400)

    raw = o.raw_data if isinstance(o.raw_data, dict) else {}
    items = raw.get("line_items") or []
    if not isinstance(items, list) or not (0 <= item_index < len(items)):
        return JsonResponse({"ok": False, "error": "Item not found."}, status=400)

    if len(items) <= 1:
        return JsonResponse({
            "ok": False,
            "error": "This is the only item — cancel the order instead.",
        }, status=400)

    # Subtract the deleted item's share of the product cost. Shipping is
    # left untouched per the tenant flow ("agr customer koi product delete
    # krna chahy to wo kr skai but only product cost delete ho shipping
    # same rahy").
    deleted = items[item_index]
    deleted_line_total = (float(deleted.get("price") or 0)
                          * (deleted.get("quantity") or 1))
    merchant_sum = sum(
        (float(it.get("price") or 0) * (it.get("quantity") or 1))
        for it in items
    )

    if (o.sourcing_product_usd is not None and merchant_sum > 0
            and deleted_line_total > 0):
        share_ratio = Decimal(str(deleted_line_total / merchant_sum))
        deleted_share = (o.sourcing_product_usd * share_ratio).quantize(Decimal("0.01"))
        o.sourcing_product_usd = max(Decimal("0.00"),
                                      o.sourcing_product_usd - deleted_share)
        # Total = (possibly-reduced) product cost + unchanged shipping
        o.sourcing_total_usd = (o.sourcing_product_usd
                                 + (o.sourcing_shipping_usd or Decimal("0.00")))

    # Persist the trimmed line_items array.
    new_items = items[:item_index] + items[item_index + 1:]
    raw["line_items"] = new_items
    o.raw_data = raw

    # If the deleted row was the cached "first item" the Order row caches
    # at top level, refresh those mirror fields to the new first item so
    # the orders list / vendor sync stay consistent.
    if item_index == 0 and new_items:
        first = new_items[0]
        o.product_name = (first.get("name") or first.get("title")
                          or first.get("product_name") or o.product_name)
        if first.get("id") is not None:
            o.product_id = str(first.get("id"))

    # Update the cached merchant total too so subsequent share math stays
    # consistent across multiple deletions.
    new_merchant_total = sum(
        Decimal(str(float(it.get("price") or 0) * (it.get("quantity") or 1)))
        for it in new_items
    )
    o.total_price = new_merchant_total.quantize(Decimal("0.01"))

    o.save()
    return JsonResponse({
        "ok": True,
        "order": _order_row_json(o, include_address=True),
    })


# ─────────────────────────────────────────────────────────────────────
# Tenant-side soft-delete + restore for Sourcing orders.
#
# Eligibility: a row can be soft-deleted ONLY while it's in
# pending_source / pending_payment AND the tenant owns it (Order.store.user
# == request.user). Once procurement starts (processing onward) the row is
# locked — too much downstream work (vendor orders, label generation,
# wallet charges) hinges on it. The is_tenant_deletable property is the
# single source of truth.
#
# Restore: only the rows owned by the tenant come back, and only into the
# same status they were in when deleted (we never touched sourcing_status
# during the delete — only is_deleted). So a soft-deleted Pending Source
# row restores into Pending Source.
#
# Both endpoints log to OpsActivity so the OPS team sees the change in
# real-time on their workspace activity feed.
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_POST
def api_orders_bulk_delete(request):
    """Soft-delete one or more orders owned by the calling tenant.

    Request body: ``{"order_ids": [1, 2, 3]}``

    Returns: ``{ok: True, deleted: [...], skipped: [{id, reason}, ...]}``
    Skipped rows include their reason (locked status, not owned, already
    deleted) so the UI can surface a precise toast.
    """
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Bad JSON."}, status=400)

    raw_ids = payload.get("order_ids") or []
    try:
        order_ids = [int(x) for x in raw_ids if x is not None]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "order_ids must be ints."}, status=400)

    if not order_ids:
        return JsonResponse({"ok": False, "error": "No orders selected."}, status=400)

    user = request.user
    qs = Order.objects.filter(id__in=order_ids).select_related("store")
    found_ids = {o.id: o for o in qs}

    deleted, skipped = [], []
    now = timezone.now()
    with transaction.atomic():
        for oid in order_ids:
            o = found_ids.get(oid)
            if not o:
                skipped.append({"id": oid, "reason": "not_found"})
                continue
            if o.store_id is None or o.store.user_id != user.id:
                skipped.append({"id": oid, "reason": "not_owned"})
                continue
            if o.is_deleted:
                skipped.append({"id": oid, "reason": "already_deleted"})
                continue
            if not o.is_tenant_deletable:
                skipped.append({"id": oid, "reason": "locked_status"})
                continue
            o.is_deleted = True
            o.deleted_at = now
            o.deleted_by = user
            o.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])
            deleted.append(o.id)

    # OPS audit trail — write one activity per deleted order so the OPS
    # workspace feed shows the tenant pulled it.
    if deleted:
        try:
            from sourcing_ops.models import OpsActivity
            for oid in deleted:
                o = found_ids[oid]
                OpsActivity.objects.create(
                    order=o,
                    kind="tenant_delete",
                    title="Tenant removed order from queue",
                    detail=(
                        f"{o.ds_order_ref} was soft-deleted by the tenant "
                        f"while in {o.sourcing_status}."
                    ),
                    icon="🗑️",
                )
        except Exception:
            # Don't fail the request just because activity logging blew up.
            pass

    return JsonResponse({
        "ok": True,
        "deleted": deleted,
        "skipped": skipped,
    })


@login_required(login_url="/login/")
@require_POST
def api_orders_bulk_restore(request):
    """Restore one or more soft-deleted orders back into the live queue.

    Request body: ``{"order_ids": [1, 2, 3]}``

    Restored rows go back to the status they were in when deleted (we
    never mutated sourcing_status during delete, so this is a no-op on
    status — just flipping is_deleted back to False).
    """
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Bad JSON."}, status=400)

    raw_ids = payload.get("order_ids") or []
    try:
        order_ids = [int(x) for x in raw_ids if x is not None]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "order_ids must be ints."}, status=400)

    if not order_ids:
        return JsonResponse({"ok": False, "error": "No orders selected."}, status=400)

    user = request.user
    qs = Order.objects.filter(id__in=order_ids).select_related("store")
    found_ids = {o.id: o for o in qs}

    restored, skipped = [], []
    with transaction.atomic():
        for oid in order_ids:
            o = found_ids.get(oid)
            if not o:
                skipped.append({"id": oid, "reason": "not_found"})
                continue
            if o.store_id is None or o.store.user_id != user.id:
                skipped.append({"id": oid, "reason": "not_owned"})
                continue
            if not o.is_deleted:
                skipped.append({"id": oid, "reason": "not_deleted"})
                continue
            o.is_deleted = False
            o.deleted_at = None
            o.deleted_by = None
            o.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])
            restored.append(o.id)

    if restored:
        try:
            from sourcing_ops.models import OpsActivity
            for oid in restored:
                o = found_ids[oid]
                OpsActivity.objects.create(
                    order=o,
                    kind="tenant_restore",
                    title="Tenant restored order to queue",
                    detail=(
                        f"{o.ds_order_ref} was restored by the tenant — "
                        f"back in {o.sourcing_status}."
                    ),
                    icon="↩️",
                )
        except Exception:
            pass

    return JsonResponse({
        "ok": True,
        "restored": restored,
        "skipped": skipped,
    })


def _charge_orders(user, order_ids):
    """All-or-nothing wallet charge for one or many orders.

    Returns (ok, payload). On success the payload contains a `charged` list
    and the new wallet balance. On failure: an `error` string plus optional
    `unpayable_ids` / `missing_quote_ids` so the UI can surface specifics.
    """
    if not order_ids:
        return False, {"error": "No orders selected."}

    orders = list(Order.objects.filter(
        id__in=order_ids, store__user=user,
    ).select_for_update())
    found_ids = {o.id for o in orders}
    missing = [i for i in order_ids if i not in found_ids]
    if missing:
        return False, {"error": f"{len(missing)} order(s) not found.",
                        "missing_ids": missing}

    not_payable = [o.id for o in orders if o.sourcing_status != "pending_payment"]
    if not_payable:
        return False, {
            "error": "Some orders are no longer in Pending Payment.",
            "unpayable_ids": not_payable,
        }

    no_quote = [o.id for o in orders if not o.sourcing_total_usd or o.sourcing_total_usd <= 0]
    if no_quote:
        return False, {
            "error": "Some orders are missing a quote — try again shortly.",
            "missing_quote_ids": no_quote,
        }

    total = sum((o.sourcing_total_usd for o in orders), Decimal("0"))
    wallet = _ensure_wallet(user)
    if wallet.balance_usd < total:
        return False, {
            "error": "Insufficient wallet balance.",
            "needed_usd":  _dec_to_float(total),
            "balance_usd": _dec_to_float(wallet.balance_usd),
            "shortfall_usd": _dec_to_float(total - wallet.balance_usd),
        }

    now = timezone.now()
    charged = []
    for o in orders:
        wallet.balance_usd = wallet.balance_usd - o.sourcing_total_usd
        WalletTransaction.objects.create(
            wallet=wallet, kind="charge",
            amount_usd=(-o.sourcing_total_usd),
            balance_after=wallet.balance_usd,
            related_order=o,
            reference=o.ds_order_ref,
            note=f"Payment for {o.ds_order_ref}",
        )
        o.sourcing_status = "processing"
        o.sourcing_paid_at = now
        o.save(update_fields=["sourcing_status", "sourcing_paid_at"])
        charged.append({
            "id": o.id, "ds_order_ref": o.ds_order_ref,
            "amount_usd": _dec_to_float(o.sourcing_total_usd),
        })
    wallet.save(update_fields=["balance_usd", "updated_at"])

    return True, {
        "charged": charged,
        "total_usd": _dec_to_float(total),
        "wallet": _serialize_wallet(wallet),
    }


def _country_flag(code):
    """ISO 3166 alpha-2 → regional-indicator flag emoji (Python side)."""
    if not code or not isinstance(code, str):
        return ""
    c = code.strip().upper()
    if len(c) != 2 or not c.isalpha():
        return ""
    A = 0x1F1E6
    return chr(A + ord(c[0]) - 65) + chr(A + ord(c[1]) - 65)


@login_required(login_url="/login/")
@require_GET
def api_order_invoice(request, order_id):
    """Render the tenant-facing service invoice for a paid sourcing order.

    Returns full HTML — caller opens in a new tab so it can be printed or
    saved as PDF via the browser. Allowed only once the wallet has been
    charged (sourcing_paid_at exists).
    """
    from django.template.loader import render_to_string
    from django.http import HttpResponse

    o = get_object_or_404(Order, pk=order_id, store__user=request.user)
    if not o.sourcing_paid_at:
        return HttpResponse(
            "Invoice is available only after the order has been paid.",
            status=400, content_type="text/plain",
        )

    # Per-item allocated product cost (same allocation logic the modal
    # uses, computed server-side here so the printed invoice is reliable).
    items_full = _extract_line_items(o)
    line_totals = [
        Decimal(str(float(it.get("price") or 0) * (it.get("quantity") or 1)))
        for it in items_full
    ]
    merchant_sum = sum(line_totals, Decimal("0"))
    product_quote = o.sourcing_product_usd or Decimal("0")

    allocated = []
    if merchant_sum > 0 and product_quote > 0:
        for lt in line_totals:
            share = (lt / merchant_sum) * product_quote
            allocated.append(share.quantize(Decimal("0.01")))
        # Rounding correction: bump last item by the leftover cent so the
        # sum matches sourcing_product_usd exactly.
        if allocated:
            diff = product_quote - sum(allocated)
            allocated[-1] = (allocated[-1] + diff).quantize(Decimal("0.01"))
    else:
        allocated = [None] * len(items_full)

    items_ctx = []
    for it, alloc in zip(items_full, allocated):
        qty = it.get("quantity") or 1
        unit = (alloc / qty) if (alloc is not None and qty > 0) else None
        items_ctx.append({
            "name":     it.get("product_name") or "(no name)",
            "variant":  it.get("variant") or "",
            "sku":      it.get("sku") or "",
            "quantity": qty,
            "unit":     _dec_to_float(unit) if unit is not None else None,
            "line":     _dec_to_float(alloc) if alloc is not None else None,
        })

    # Resolved shipping address (override layered on top of raw_data)
    addr = o.shipping_address or {}
    if isinstance(o.shipping_override, dict):
        addr = {**addr, **o.shipping_override}
    addr_parts = [addr.get("line1"), addr.get("line2"),
                  addr.get("city"), addr.get("state"),
                  addr.get("postal_code"), addr.get("country")]
    customer_address_line = " · ".join(p for p in addr_parts if p)

    # Wallet transaction reference (the charge that paid for this order)
    tx = (WalletTransaction.objects
          .filter(wallet__tenant=request.user, related_order=o, kind="charge")
          .first())
    tx_ref = ""
    if tx:
        tx_ref = f"WLT-{tx.created_at.strftime('%Y%m')}-{tx.id:06d}"

    # Tenant identity — we don't have a dedicated BillingProfile model
    # yet, so derive from the User row + the Store name.
    user = request.user
    store = o.store
    tenant_business = (getattr(store, "name", "") or "Your store").strip()
    tenant_name = user.get_full_name() or user.username
    tenant_email = user.email or ""

    inv_year = (o.created_at or timezone.now()).year
    ctx = {
        "invoice_no":      f"INV-{inv_year}-{o.id:06d}",
        "ds_order_ref":    o.ds_order_ref,
        "source_order_ref": o.external_order_id,
        "platform":        (getattr(store, "platform", "") or ""),
        "store_name":      tenant_business,

        "tenant_business": tenant_business,
        "tenant_name":     tenant_name,
        "tenant_email":    tenant_email,

        "invoice_date":    o.sourcing_paid_at,
        "service_from":    o.sourcing_paid_at,
        "service_to":      o.sourcing_delivered_at or o.delivered_at or o.sourcing_paid_at,

        "customer_name":         addr.get("name") or o.customer_name or "",
        "customer_phone":        addr.get("phone") or o.customer_phone or "",
        "customer_address_line": customer_address_line,
        "flag":                  _country_flag(addr.get("country") or o.country or ""),

        "items":      items_ctx,
        "item_count": len(items_ctx),
        "subtotal":   _dec_to_float(o.sourcing_product_usd),
        "shipping":   _dec_to_float(o.sourcing_shipping_usd),
        "total":      _dec_to_float(o.sourcing_total_usd),

        "paid_at":         o.sourcing_paid_at,
        "delivered_at":    o.sourcing_delivered_at or o.delivered_at,
        "tracking_company": o.tracking_company or "",
        "tracking_number":  o.tracking_number or "",
        "status_label":    o.get_sourcing_status_display(),
        "tx_ref":          tx_ref,
    }
    html = render_to_string("sourcing_partners/invoice.html", ctx)
    return HttpResponse(html, content_type="text/html; charset=utf-8")


@login_required(login_url="/login/")
@require_POST
@transaction.atomic
def api_order_pay(request, order_id):
    """Pay a single order from wallet."""
    ok, payload = _charge_orders(request.user, [int(order_id)])
    if not ok:
        return JsonResponse({"ok": False, **payload}, status=400)
    return JsonResponse({"ok": True, **payload})


@login_required(login_url="/login/")
@require_POST
@transaction.atomic
def api_orders_bulk_pay(request):
    """Pay many orders from wallet (all-or-nothing)."""
    body = _parse_body(request)
    ids = body.get("order_ids") or []
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid order_ids"}, status=400)
    ok, payload = _charge_orders(request.user, ids)
    if not ok:
        return JsonResponse({"ok": False, **payload}, status=400)
    return JsonResponse({"ok": True, **payload})


# ─── Aggregate catalog ────────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_catalog(request):
    user = request.user
    qs = LockedPrice.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)
    rows = []
    for lp in qs[:300]:
        row = _locked_price_json(lp, with_history=False)
        row["partner_id"] = lp.partner_id
        row["desk_id"] = lp.partner_id
        row["desk_name"] = _desk_label(lp.partner)
        row["desk_emoji"] = lp.partner.cover_emoji
        rows.append(row)
    return JsonResponse({"ok": True, "locked_prices": rows})


# ─── Aggregate stock ──────────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_stock(request):
    user = request.user
    qs = VendorStockItem.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)
    rows = []
    for s in qs[:300]:
        row = _stock_json(s)
        row["partner_id"] = s.partner_id
        row["desk_id"] = s.partner_id
        row["desk_name"] = _desk_label(s.partner)
        row["desk_emoji"] = s.partner.cover_emoji
        rows.append(row)
    return JsonResponse({"ok": True, "stock": rows})


# ─── Aggregate payments ───────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_payments(request):
    user = request.user
    qs = VendorPayment.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)

    paid = qs.filter(status="settled")
    lifetime = _dec_to_float(paid.aggregate(s=Sum("amount_usd"))["s"] or Decimal("0"))
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = _dec_to_float(
        paid.filter(settled_at__gte=month_start).aggregate(s=Sum("amount_usd"))["s"]
        or Decimal("0")
    )
    pending = _dec_to_float(
        qs.filter(status="pending").aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
    )
    refunded = _dec_to_float(
        qs.filter(status="refunded").aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
    )

    rows = []
    for p in qs[:300]:
        row = _payment_json(p)
        row["partner_id"] = p.partner_id
        row["desk_id"] = p.partner_id
        row["desk_name"] = _desk_label(p.partner)
        row["desk_emoji"] = p.partner.cover_emoji
        rows.append(row)

    return JsonResponse({
        "ok": True,
        "summary": {
            "lifetime_usd":   lifetime,
            "this_month_usd": this_month,
            "pending_usd":    pending,
            "refunded_usd":   refunded,
        },
        "payments": rows,
    })


# ─── Aggregate documents ──────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_documents(request):
    user = request.user
    qs = VendorDocument.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)
    grouped = defaultdict(list)
    for d in qs:
        row = _document_json(d)
        row["partner_id"] = d.partner_id
        row["desk_id"] = d.partner_id
        row["desk_name"] = _desk_label(d.partner)
        row["desk_emoji"] = d.partner.cover_emoji
        grouped[d.category].append(row)
    return JsonResponse({"ok": True, "categories": dict(grouped)})


# ─── Aggregate auto-rules ─────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_rules(request):
    user = request.user
    qs = AutoRule.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)
    rows = []
    for r in qs[:200]:
        row = _rule_json(r)
        row["partner_id"] = r.partner_id
        row["desk_id"] = r.partner_id
        row["desk_name"] = _desk_label(r.partner)
        row["desk_emoji"] = r.partner.cover_emoji
        rows.append(row)
    return JsonResponse({"ok": True, "rules": rows})


# ─── Aggregate activity ───────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_activity(request):
    user = request.user
    qs = WorkspaceActivity.objects.filter(tenant=user).select_related("partner")
    qs = _filter_desk(qs, request)
    rows = []
    for a in qs[:80]:
        row = _activity_json(a)
        row["partner_id"] = a.partner_id
        row["desk_id"] = a.partner_id
        row["desk_name"] = _desk_label(a.partner)
        row["desk_emoji"] = a.partner.cover_emoji
        rows.append(row)
    return JsonResponse({"ok": True, "activity": rows})


# ─── Aggregate chat inbox ─────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_chat_inbox(request):
    """Unified inbox: one row per desk conversation, latest msg preview + unread."""
    user = request.user
    convs = list(
        PartnerConversation.objects.filter(tenant=user, is_archived=False)
        .select_related("partner")
    )
    rows = []
    for c in convs:
        p = c.partner
        rows.append({
            "conversation_id":      c.id,
            "partner_id":           p.id,
            "desk_id":              p.id,
            "desk_name":            _desk_label(p),
            "desk_emoji":           p.cover_emoji,
            "gradient_from":        p.cover_gradient_from,
            "gradient_to":          p.cover_gradient_to,
            "last_message_preview": getattr(c, "last_message_preview", "") or "",
            "last_message_at":      _iso(getattr(c, "last_message_at", None)),
            "unread":               c.unread_count_for_tenant,
        })
    rows.sort(key=lambda r: r["last_message_at"] or "", reverse=True)
    return JsonResponse({"ok": True, "conversations": rows})


# ─── Aggregate performance ────────────────────────────────────────────────
@login_required(login_url="/login/")
@require_GET
def api_aggregate_performance(request):
    """Drop-Sigma-wide scorecard, with per-desk leaderboard."""
    user = request.user
    orders_qs = VendorOrder.objects.filter(tenant=user)
    payments_qs = VendorPayment.objects.filter(tenant=user, status="settled")

    total_orders = orders_qs.count()
    delivered = orders_qs.filter(status="delivered")
    delivered_total = delivered.count()
    lifetime_spent = _dec_to_float(
        payments_qs.aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
    )

    # On-time across all delivered
    on_time_count = 0
    for o in delivered.filter(delivered_at__isnull=False,
                              lead_days_quoted__isnull=False):
        expected = o.created_at + timedelta(days=o.lead_days_quoted)
        if o.delivered_at <= expected:
            on_time_count += 1
    on_time_pct = round((on_time_count / delivered_total) * 100, 1) if delivered_total else 0.0

    # Per-desk leaderboard
    desks = []
    for p in SourcingPartner.objects.filter(is_active=True):
        p_orders = orders_qs.filter(partner=p)
        p_paid = payments_qs.filter(partner=p)
        p_delivered_qs = p_orders.filter(status="delivered")
        p_delivered = p_delivered_qs.count()
        p_spent = _dec_to_float(
            p_paid.aggregate(s=Sum("amount_usd"))["s"] or Decimal("0")
        )
        p_on_time = 0
        for o in p_delivered_qs.filter(delivered_at__isnull=False,
                                        lead_days_quoted__isnull=False):
            expected = o.created_at + timedelta(days=o.lead_days_quoted)
            if o.delivered_at <= expected:
                p_on_time += 1
        p_on_time_pct = round((p_on_time / p_delivered) * 100, 1) if p_delivered else 0.0

        desks.append({
            **_desk_brief(p),
            "total_orders":   p_orders.count(),
            "delivered":      p_delivered,
            "lifetime_spent": p_spent,
            "on_time_pct":    p_on_time_pct,
            "rating":         float(p.rating),
        })

    desks.sort(key=lambda d: d["lifetime_spent"], reverse=True)

    return JsonResponse({
        "ok": True,
        "company": {
            "total_orders":   total_orders,
            "delivered":      delivered_total,
            "lifetime_spent": lifetime_spent,
            "on_time_pct":    on_time_pct,
            "team_size":      "200+",
            "desks":          len(desks),
        },
        "desks": desks,
    })




# ═══════════════════════════════════════════════════════════════════════
# DEDICATED SOURCING MANAGER (tenant-facing)
# GET /sourcing-partners/api/my-manager/
# Returns the tenant's assigned Account Manager. Lazy-creates an
# assignment on first hit so this endpoint doubles as the trigger.
# ═══════════════════════════════════════════════════════════════════════
@login_required(login_url="/login/")
@require_GET
def api_my_manager(request):
    """Return the calling tenant's dedicated Sourcing Manager profile.

    If no assignment exists yet, auto-create one via the round-robin
    service and seed a welcome message in the tenant's chat thread.
    """
    # Local imports — keep startup-time light.
    from sourcing_ops.services import ensure_tenant_manager
    from sourcing_ops.models import OpsTeamMember  # noqa: F401

    assignment = ensure_tenant_manager(request.user)
    if assignment is None:
        # No ops staff at all — gracefully tell the frontend so it can
        # render a fallback ("manager onboarding") state instead of an error.
        return JsonResponse({
            "ok": True,
            "manager": None,
            "reason": "no_ops_staff",
        })

    m = assignment.manager
    user = m.user
    role_name = m.role.name if m.role_id else "Account Manager"
    role_color = m.role.color if m.role_id else "#a855f7"
    display = (m.display_name or user.get_full_name() or user.username
               or "Sourcing Manager")
    specialties = [s.strip() for s in (m.specialties_text or "").split(",")
                   if s.strip()]

    # Friendly "Assigned Jun 8, 2026"-style label.
    assigned_at_label = ""
    try:
        assigned_at_label = (timezone.localtime(assignment.assigned_at)
                             .strftime("Assigned %b %d, %Y").replace(" 0", " "))
    except (AttributeError, ValueError):
        assigned_at_label = ""

    return JsonResponse({
        "ok": True,
        "manager": {
            "id":                m.id,
            "display_name":      display,
            "title":             m.title or role_name,
            "role":              role_name,
            "role_color":        role_color,
            "photo_url":         m.photo_url or m.avatar_url or "",
            "avatar_emoji":      m.avatar_emoji or "👤",
            "avatar_color":      m.avatar_color or "#a855f7",
            "bio":               m.bio or "",
            "specialties":       specialties,
            "languages":         m.languages or [],
            "signature":         m.signature or display,
            "typical_reply_min": int(m.typical_reply_min or 5),
            "status":            m.status,
            "status_label":      m.get_status_display(),
            "assigned_at_iso":   assignment.assigned_at.isoformat() if assignment.assigned_at else None,
            "assigned_at_label": assigned_at_label,
            "assigned_via":      assignment.assigned_via,
        },
    })


# ═══════════════════════════════════════════════════════════════════════
# CHAT SMART-LINK LOOKUP
# GET /sourcing-partners/api/chat-lookup/?type=order|sku|email&token=...
#
# Resolves an inline mention in a chat message (order number, SKU, or
# customer email) to one or more orders, scoped to the calling tenant.
#
# Both portals call the same shape:
#   • Tenant   → this endpoint (scope: store__user=request.user)
#   • Ops Mgr  → /ops/api/chat/<conv_id>/lookup/ (scope: conversation tenant)
#
# Response:
#   { ok: true, kind: "single"|"multiple"|"none",
#     query: { type, token },
#     orders: [{id, ds_ref, external_id, customer_name, customer_email,
#               total, currency, sourcing_status, status_label,
#               created_at_iso, matched_sku?, store_name}] }
# ═══════════════════════════════════════════════════════════════════════
def _serialize_chat_order(o, matched_sku=None):
    """Compact order row for the chat-lookup picker."""
    return {
        "id":               o.id,
        "ds_ref":           o.ds_order_ref,
        "external_id":      o.external_order_id,
        "customer_name":    o.customer_name or "",
        "customer_email":   o.customer_email or "",
        "total":            float(o.total_price or 0),
        "currency":         o.currency or "USD",
        "sourcing_status":  o.sourcing_status,
        "status_label":     o.get_sourcing_status_display(),
        "created_at_iso":   o.created_at.isoformat() if o.created_at else None,
        "store_name":       o.store.name if o.store_id else "",
        "matched_sku":      matched_sku or "",
    }


def _orders_for_user(user):
    """Tenant scope: only orders inside the requester's own stores."""
    return (Order.objects
            .select_related("store")
            .filter(store__user=user))


def _resolve_chat_token(qs, type_, token):
    """Run the type-specific lookup. Returns a list of Order objects
    (possibly empty). Caller decides single vs multiple framing."""
    token = (token or "").strip()
    if not token:
        return []

    if type_ == "order":
        # Accept DS-XXXXXX (internal ref), #1234 (external), or bare digits.
        t = token.lstrip("#").strip()
        if t.upper().startswith("DS-"):
            # ds_order_ref is f"DS-{id:06d}" — pull the numeric tail and
            # match against id directly.
            tail = t[3:].lstrip("0") or "0"
            try:
                oid = int(tail) if tail.isdigit() else None
            except (TypeError, ValueError):
                oid = None
            if oid is not None:
                return list(qs.filter(id=oid)[:10])
            # Non-numeric tail (e.g. "DEMO-1061") — try external_order_id
            return list(qs.filter(external_order_id__iexact=t)[:10])
        # Plain external ID
        return list(qs.filter(external_order_id__iexact=t)[:10])

    if type_ == "email":
        return list(qs.filter(customer_email__iexact=token)[:20])

    if type_ == "sku":
        # SKUs live inside raw_data.line_items[*].sku. Filtering JSON
        # arrays portably across SQLite + Postgres is messy, so we do a
        # text-substring filter first to cut the candidate set, then
        # confirm with a Python pass.
        candidates = qs.filter(raw_data__icontains=token)[:200]
        hits = []
        for o in candidates:
            raw = o.raw_data or {}
            if not isinstance(raw, dict):
                continue
            items = raw.get("line_items") or []
            if not isinstance(items, list):
                continue
            for li in items:
                if not isinstance(li, dict):
                    continue
                sku = (li.get("sku") or "").strip()
                if sku and sku.upper() == token.upper():
                    hits.append(o)
                    break
            if len(hits) >= 20:
                break
        return hits

    return []


def _chat_lookup_response(orders, type_, token, matched_sku_for=""):
    """Shape the JSON response shared by both portals."""
    if not orders:
        return JsonResponse({
            "ok": True,
            "kind": "none",
            "query": {"type": type_, "token": token},
            "orders": [],
        })
    kind = "single" if len(orders) == 1 else "multiple"
    rows = [_serialize_chat_order(o, matched_sku=matched_sku_for)
            for o in orders]
    return JsonResponse({
        "ok":     True,
        "kind":   kind,
        "query":  {"type": type_, "token": token},
        "orders": rows,
    })


@login_required(login_url="/login/")
@require_GET
def api_chat_lookup(request):
    """Tenant-facing chat smart-link resolver. See block header above."""
    type_ = (request.GET.get("type") or "").strip().lower()
    token = (request.GET.get("token") or "").strip()
    if type_ not in ("order", "sku", "email"):
        return JsonResponse({"ok": False, "error": "bad_type"}, status=400)
    if not token:
        return JsonResponse({"ok": False, "error": "no_token"}, status=400)

    qs = _orders_for_user(request.user)
    orders = _resolve_chat_token(qs, type_, token)
    matched_sku = token if type_ == "sku" else ""
    return _chat_lookup_response(orders, type_, token,
                                 matched_sku_for=matched_sku)
