"""Tenant chat — the ops-side inbox.

Drop Sigma's specialist team replies here to tenant messages that come
in through the tenant's Sourcing Partners chat. Tenants see one unified
"Drop Sigma Sourcing" persona; this side shows who actually replied.

Endpoints:
  GET  /ops/api/chats/                       — list all conversations across all tenants
  GET  /ops/api/chat/<conv_id>/messages/     — fetch full thread (marks tenant msgs read)
  POST /ops/api/chat/<conv_id>/send/         — reply as Drop Sigma (direction='in')
"""
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from sourcing_partners.models import PartnerConversation, PartnerMessage
from .permissions import ops_required
from .views import _parse_body, _iso, _short_dt, _human_ago, _country_flag


def _tenant_location(user):
    """Most recent IP-derived location for a tenant. Returns a dict
    shaped for the chat header (flag + country + city + last_seen) or
    None if we've never logged an IP for this user.

    The data comes from the existing `UserIPLog` rows populated by
    `ImpersonationMiddleware` — no extra geolocation calls here. The
    middleware already caches lookups via `IPGeoCache` (7-day TTL), so
    this view stays a pure DB read.
    """
    try:
        from superadmin.models import UserIPLog
    except Exception:
        return None

    row = (UserIPLog.objects
           .filter(user=user)
           .exclude(country_code="")
           .order_by("-last_seen", "-id")
           .only("country", "country_code", "city", "region",
                 "last_seen", "ip_address")
           .first())
    if row is None:
        # Fall back to the very latest log even if country_code missing —
        # at least surface "Unknown" with timestamp so ops sees it tried.
        row = (UserIPLog.objects.filter(user=user)
               .order_by("-last_seen", "-id").first())
    if row is None:
        return None

    code    = (row.country_code or "").upper()
    country = row.country or ""
    city    = row.city or ""

    # Localhost / private-network tagging from the middleware uses
    # country_code "LO" or "XX" with country "Local / Dev". Those are
    # noise (and "LO" happens to be Laos's ISO code, which would render
    # the Laos flag — wrong signal). Render a generic 🌐 globe + "Local"
    # label so the dev chip is honest rather than misleading.
    is_local = code in ("LO", "XX") or country.lower().startswith("local")
    if is_local:
        flag = "🌐"
        country = "Local network"
        city = ""
    else:
        flag = _country_flag(code) if code else "🌐"

    return {
        "country":       country,
        "country_code":  code,
        "city":          city,
        "region":        row.region or "",
        "flag":          flag,
        "is_local":      is_local,
        "last_seen_iso": _iso(row.last_seen) if row.last_seen else None,
        "last_seen_ago": _human_ago(row.last_seen) if row.last_seen else "",
        # NEVER include the IP itself — ops doesn't need it for the chat
        # header, and exposing IPs in API responses is a privacy footgun.
    }


def _conv_to_card(c):
    last_msg = c.messages.order_by("-created_at").first()
    tenant = c.tenant
    return {
        "id":            c.id,
        "tenant_id":     tenant.id,
        "tenant_username": tenant.username,
        "tenant_name":   tenant.get_full_name() or tenant.username,
        "tenant_email":  tenant.email,
        # Tenant IP-derived location — used by the chat header so ops can
        # see at a glance which country/city the tenant logged in from.
        "tenant_location": _tenant_location(tenant),
        "partner_id":    c.partner_id,
        "partner_name":  c.partner.name,
        "partner_emoji": c.partner.cover_emoji,
        "partner_gradient_from": c.partner.cover_gradient_from,
        "partner_gradient_to":   c.partner.cover_gradient_to,
        "last_preview":  c.last_message_preview or (last_msg.body[:140] if last_msg else ""),
        "last_at_iso":   _iso(c.last_message_at),
        "last_ago":      _human_ago(c.last_message_at),
        "last_direction": last_msg.direction if last_msg else "out",
        "unread":        c.unread_count_for_tenant,
        "tenant_unread": _tenant_unread_for_ops(c),
        "is_archived":   c.is_archived,
    }


def _tenant_unread_for_ops(c):
    """Number of unread tenant→Drop-Sigma messages (so ops knows what to answer)."""
    return c.messages.filter(direction="out", is_read=False).count()


def _msg_to_dict(m):
    atts = []
    for a in m.attachments.all():
        atts.append({
            "id":       a.id,
            "kind":     a.kind,
            "url":      a.url or (a.file.url if a.file else ""),
            "filename": getattr(a, "filename", "") or "",
            "size":     getattr(a, "display_size", "") if hasattr(a, "display_size") else "",
            "mime":     getattr(a, "mime_type", "") or "",
        })
    return {
        "id":         m.id,
        "direction":  m.direction,
        "side":       "tenant" if m.direction == "out" else "ops",
        "body":       m.body,
        "is_read":    m.is_read,
        "created_iso": _iso(m.created_at),
        "created_ago": _human_ago(m.created_at),
        "created_at": _short_dt(m.created_at),
        "attachments": atts,
    }


# ─── List all chats across all tenants ──────────────────────────────────
@ops_required
@require_GET
def api_chats_list(request):
    q = (request.GET.get("q") or "").strip()
    only_unread = request.GET.get("unread") == "1"

    qs = (PartnerConversation.objects
          .select_related("tenant", "partner")
          .filter(is_archived=False))

    if q:
        qs = qs.filter(
            Q(tenant__username__icontains=q)
            | Q(tenant__first_name__icontains=q)
            | Q(tenant__last_name__icontains=q)
            | Q(tenant__email__icontains=q)
            | Q(partner__name__icontains=q)
            | Q(last_message_preview__icontains=q)
        ).distinct()

    cards = []
    for c in qs:
        card = _conv_to_card(c)
        if only_unread and not card["tenant_unread"]:
            continue
        cards.append(card)

    total_unread = sum(c["tenant_unread"] for c in cards)
    return JsonResponse({
        "ok": True,
        "conversations": cards,
        "total": len(cards),
        "total_unread": total_unread,
    })


# ─── Fetch a thread ─────────────────────────────────────────────────────
@ops_required
@require_GET
def api_chat_messages(request, conv_id):
    try:
        c = (PartnerConversation.objects
             .select_related("tenant", "partner")
             .get(pk=conv_id))
    except PartnerConversation.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Conversation not found."}, status=404)

    # Mark tenant→ops messages as read (so the unread badge clears)
    c.messages.filter(direction="out", is_read=False).update(is_read=True)

    msg_objs = list(c.messages.all().prefetch_related("attachments"))
    msgs = [_msg_to_dict(m) for m in msg_objs]
    # Smart-link whitelist — scope is the CONVERSATION'S TENANT, never
    # the calling ops user. Ops must see exactly the same clickable
    # tokens the tenant would see in their own chat. Lazy-imported so
    # this module stays loadable even if sourcing_partners hasn't booted.
    from sourcing_partners.views import _validate_message_tokens
    return JsonResponse({
        "ok": True,
        "conversation": _conv_to_card(c),
        "messages": msgs,
        "valid_tokens": _validate_message_tokens(
            [m.body for m in msg_objs], c.tenant),
    })


# ─── Send a reply as Drop Sigma ─────────────────────────────────────────
@ops_required
@require_POST
def api_chat_send(request, conv_id):
    try:
        c = (PartnerConversation.objects
             .select_related("tenant", "partner")
             .get(pk=conv_id))
    except PartnerConversation.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Conversation not found."}, status=404)

    # Accept either JSON body (legacy) or multipart form-data (with files).
    ctype = (request.META.get("CONTENT_TYPE") or "").lower()
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        text = (request.POST.get("body") or "").strip()
    else:
        body = _parse_body(request)
        text = (body.get("body") or "").strip()

    # Read attachments from BOTH possible field names
    image_files = request.FILES.getlist("images") if hasattr(request, "FILES") else []
    other_files = request.FILES.getlist("files")  if hasattr(request, "FILES") else []
    all_files = list(image_files) + list(other_files)

    if not text and not all_files:
        return JsonResponse({"ok": False, "error": "Empty message."}, status=400)
    if len(text) > 4000:
        return JsonResponse({"ok": False, "error": "Message too long (4,000 char max)."}, status=400)

    # Lazy import — PartnerAttachment lives in sourcing_partners
    from sourcing_partners.models import PartnerAttachment
    import os, mimetypes

    ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
    MAX_UPLOAD_BYTES = 20 * 1024 * 1024

    m = PartnerMessage.objects.create(
        conversation=c,
        direction="in",  # partner → tenant (Drop Sigma reply)
        body=text,
        is_read=False,
    )

    # Attach files — kind detection: form field → MIME → extension
    for f in all_files:
        if f.size > MAX_UPLOAD_BYTES:
            continue
        ext = os.path.splitext(f.name)[1].lower()
        mime = mimetypes.guess_type(f.name)[0] or (getattr(f, "content_type", "") or "")
        if f in image_files or mime.startswith("image/") or ext in ALLOWED_IMAGE_EXT:
            kind = "image"
        else:
            kind = "file"
        PartnerAttachment.objects.create(
            message=m, kind=kind, file=f,
            filename=f.name[:240], filesize_bytes=f.size, mime_type=mime,
        )

    # Bump conversation metadata
    preview = text or ("📷 Image" if image_files else ("📎 Attachment" if other_files else ""))
    c.last_message_at = m.created_at
    c.last_message_preview = preview[:200]
    c.unread_count_for_tenant += 1
    c.save(update_fields=["last_message_at", "last_message_preview",
                          "unread_count_for_tenant"])

    # Validate any chip candidates in the body against the tenant's data
    # so the optimistic render uses the same whitelist as a polled message.
    from sourcing_partners.views import _validate_message_tokens
    return JsonResponse({
        "ok": True,
        "message": _msg_to_dict(m),
        "valid_tokens": _validate_message_tokens([m.body], c.tenant),
    })


# ─── Chat smart-link resolver (ops side, conversation-scoped) ──────────
# GET /ops/api/chat/<conv_id>/lookup/?type=order|sku|email&token=...
# Mirrors /sourcing-partners/api/chat-lookup/ but scopes orders to the
# conversation's TENANT instead of the calling user — ops staff need to
# see what the tenant sees in their own chat thread.
@ops_required
@require_GET
def api_chat_lookup(request, conv_id):
    type_ = (request.GET.get("type") or "").strip().lower()
    token = (request.GET.get("token") or "").strip()
    if type_ not in ("order", "sku", "email"):
        return JsonResponse({"ok": False, "error": "bad_type"}, status=400)
    if not token:
        return JsonResponse({"ok": False, "error": "no_token"}, status=400)

    try:
        conv = (PartnerConversation.objects
                .select_related("tenant").get(pk=conv_id))
    except PartnerConversation.DoesNotExist:
        return JsonResponse({"ok": False, "error": "no_conversation"}, status=404)

    # Reuse the resolver from sourcing_partners.views so the matching rules
    # (DS-ref normalisation, SKU JSON traversal, email iexact) stay in one
    # place. The only difference is the scope queryset.
    from sourcing_partners.views import (
        _orders_for_user, _resolve_chat_token, _chat_lookup_response,
    )
    qs = _orders_for_user(conv.tenant)
    orders = _resolve_chat_token(qs, type_, token)
    matched_sku = token if type_ == "sku" else ""
    return _chat_lookup_response(orders, type_, token,
                                 matched_sku_for=matched_sku)


# ─── Order detail (conv-scoped, mirrors the tenant SP modal payload) ───
# GET /ops/api/chat/<conv_id>/order/<order_id>/
# Returns exactly the same shape as /sourcing-partners/api/sourcing-order/
# <id>/ — but scoped to the conversation's tenant so ops staff see the
# same Drop Sigma quote / shipping / tracking view the tenant sees.
@ops_required
@require_GET
def api_chat_order_detail(request, conv_id, order_id):
    try:
        conv = (PartnerConversation.objects
                .select_related("tenant").get(pk=conv_id))
    except PartnerConversation.DoesNotExist:
        return JsonResponse({"ok": False, "error": "no_conversation"}, status=404)

    from orders.models import Order
    from sourcing_partners.views import _order_row_json
    try:
        o = (Order.objects.select_related("store")
             .get(pk=order_id, store__user=conv.tenant))
    except Order.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)

    return JsonResponse({
        "ok":    True,
        "order": _order_row_json(o, include_address=True),
    })
