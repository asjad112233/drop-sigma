"""Founder / platform-owner order notifications.

Sends an admin-style HTML email to the address in the
``FOUNDER_NOTIFY_EMAIL`` env var every time a tenant gets a real
order via real-time webhook delivery. Two variants are sent:

  • "New order"   — for normal incoming orders (processing/pending/paid)
  • "Failed"      — for payment-failed orders (WC failed,
                     Shopify voided/cancelled)

Rules baked in:
  - Reuses the existing ``send_platform_email()`` (Resend → SMTP
    fallback, ``Drop Sigma <noreply@dropsigma.com>`` sender). No new
    credentials, no DNS work — same pipe that already delivers
    password-reset emails.
  - Per-(order, kind) dedup via ``FounderNotificationLog`` so a sync
    rerun, a webhook redelivery, or a bulk import never doubles a
    notification.
  - Triggered ONLY from real-time paths: the
    ``orders/webhook/{platform}/<store_id>/`` views pass
    ``is_realtime=True``. Bulk syncs (``sync_woocommerce_orders``,
    ``sync_shopify_orders``, freshness sweeps) leave it ``False``
    so onboarding-style 100-order imports don't carpet-bomb the
    inbox.
  - Test / demo stores skipped: any store whose name matches /test/i,
    /demo/i, /sandbox/i, or whose id is in ``_DEMO_STORE_IDS``.

Fail-silent: every send happens inside a broad try/except that
``logger.warning``-s on error. Notification failures NEVER bubble up
to break the order-sync code path.
"""
from __future__ import annotations

import logging
import os
from html import escape as _esc

from django.utils.timezone import now as _now

logger = logging.getLogger(__name__)

# Hard-coded test/demo store IDs so even if the merchant later renames
# them to look real, they still won't trigger founder notifications.
_DEMO_STORE_IDS: set[int] = {39, 52}


# ───────────────────────────────────────────────────────────────────────
# Public entry points
# ───────────────────────────────────────────────────────────────────────

def maybe_notify_founder(order, store, is_realtime: bool) -> None:
    """Top-level decision point called from the order processors.

    Doesn't care about platform or status nuances — figures everything
    out from ``order``. Safe to call on every order (bulk + real-time):
    the ``is_realtime`` gate, the store-isolation skips, the per-order
    dedup all live inside, so the caller doesn't have to reason about
    any of it.
    """
    try:
        if not is_realtime:
            return  # bulk sync — silent
        if not _should_notify_for_store(store):
            return
        to_addr = (os.getenv("FOUNDER_NOTIFY_EMAIL") or "").strip()
        if not to_addr:
            return
        kind = _classify_order_kind(order, store)
        if kind is None:
            return  # status we don't notify on (refunded / completed / etc)
        if _already_notified(order, kind):
            return
        _send(order, store, kind, to_addr)
        _mark_notified(order, kind)
    except Exception as exc:
        logger.warning("founder-notify: skipped order %s (%s): %s",
                       getattr(order, 'id', '?'), getattr(store, 'name', '?'), exc)


# ───────────────────────────────────────────────────────────────────────
# Guards
# ───────────────────────────────────────────────────────────────────────

def _should_notify_for_store(store) -> bool:
    if not store:
        return False
    if getattr(store, "id", None) in _DEMO_STORE_IDS:
        return False
    name = (getattr(store, "name", "") or "").lower()
    if any(tag in name for tag in ("test", "demo", "sandbox")):
        return False
    url = (getattr(store, "store_url", "") or "").lower()
    if any(tag in url for tag in ("demo.", "sandbox.", "test.", "staging.")):
        return False
    return True


def _classify_order_kind(order, store) -> str | None:
    """Return 'new_order', 'failed', or None.

    WC statuses (lowercased): processing, pending, on-hold, completed,
                              cancelled, refunded, failed.
    Shopify financial_status: pending, authorized, partially_paid, paid,
                              refunded, voided, partially_refunded.
    Shopify fulfillment_status (separate axis): null, partial, fulfilled.
    """
    status = (
        (getattr(order, "fulfillment_status", None) or "")
        or (getattr(order, "payment_status", None) or "")
    ).strip().lower()

    if not status:
        return "new_order"  # default to "new order"

    # Failed bucket
    failed_keywords = ("failed", "voided", "cancelled", "canceled")
    if status in failed_keywords:
        return "failed"

    # New-order bucket (anything that's a fresh incoming order)
    ok_keywords = ("processing", "pending", "on-hold", "on hold",
                   "paid", "authorized", "partially_paid", "partially paid")
    if status in ok_keywords:
        return "new_order"

    # Skip terminal states we don't care about (completed, refunded, etc).
    return None


# ───────────────────────────────────────────────────────────────────────
# Dedup
# ───────────────────────────────────────────────────────────────────────

def _already_notified(order, kind: str) -> bool:
    try:
        from .models import FounderNotificationLog
        return FounderNotificationLog.objects.filter(order_id=order.id, kind=kind).exists()
    except Exception:
        return False  # If the log model isn't migrated yet, fail-open so we still send


def _mark_notified(order, kind: str) -> None:
    try:
        from .models import FounderNotificationLog
        FounderNotificationLog.objects.create(order_id=order.id, kind=kind, sent_at=_now())
    except Exception:
        # Race against concurrent webhooks: unique constraint may fire;
        # that's the desired behaviour (one row, one notification).
        pass


# ───────────────────────────────────────────────────────────────────────
# Send
# ───────────────────────────────────────────────────────────────────────

def _send(order, store, kind: str, to_addr: str) -> None:
    from core.password_reset import send_platform_email
    ctx = _build_context(order, store, kind)
    subject = _subject_for(kind, ctx)
    html = _render_html(kind, ctx)
    ok, err = send_platform_email(to_addr, subject, html)
    if not ok:
        logger.warning("founder-notify: send_platform_email failed for order %s: %s",
                       order.id, err)
    else:
        logger.info("founder-notify: sent %s for order %s to %s",
                    kind, order.id, to_addr)


def _subject_for(kind: str, ctx: dict) -> str:
    brand = ctx.get("brand_name") or "store"
    ext = ctx.get("ext_ref") or ""
    money = ctx.get("total_display") or ""
    if kind == "failed":
        return f"⚠️ Payment failed — Order {ext} — {brand}"
    return f"🛒 New order {ext} — {money} — {brand}"


def _build_context(order, store, kind: str) -> dict:
    raw = getattr(order, "raw_data", None) or {}

    # Tenant + store
    owner = getattr(store, "user", None)
    owner_name = (
        (getattr(owner, "get_full_name", lambda: "")() if owner else "")
        or (getattr(owner, "username", "") if owner else "")
        or "Tenant"
    )
    owner_email = getattr(owner, "email", "") if owner else ""
    platform = (getattr(store, "platform", "") or "").lower()
    platform_label = "WooCommerce" if platform == "woocommerce" else (
                     "Shopify" if platform == "shopify" else (platform or "—"))

    # Order numbers
    ext_raw = str(getattr(order, "external_order_id", "") or "")
    ds_ref = f"DS-{ext_raw}" if ext_raw else f"DS-{order.id}"
    ext_ref = f"#{ext_raw}" if ext_raw else ""

    # Money — Symbol detection
    currency = (getattr(order, "currency", None) or "USD").upper()
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$",
               "NZD": "NZ$", "PKR": "₨", "INR": "₹"}
    sym = symbols.get(currency, "")
    try:
        total = float(getattr(order, "total_price", 0) or 0)
    except Exception:
        total = 0.0
    total_display = f"{sym}{total:.2f}".strip() + (f" {currency}" if not sym else "")

    # Subtotal / shipping / tax / discount — from raw_data, best-effort
    def _f(key_chain):
        cur = raw
        for k in key_chain:
            if isinstance(cur, dict):
                cur = cur.get(k)
            else:
                cur = None
                break
        try: return float(cur or 0)
        except Exception: return 0.0
    subtotal = _f(["subtotal_price"]) or _f(["subtotal"]) or total
    shipping = _f(["shipping_total"]) or _f(["total_shipping_price_set", "shop_money", "amount"])
    tax      = _f(["total_tax"])
    discount = _f(["discount_total"]) or _f(["total_discounts"])

    def _fmt(v):
        if not v:
            return f"{sym}0.00" + (f" {currency}" if not sym else "")
        return f"{sym}{v:.2f}" + (f" {currency}" if not sym else "")

    # Status pills
    status_label = (getattr(order, "fulfillment_status", "") or "—").title()
    payment_status_label = (getattr(order, "payment_status", "") or "—").title()

    # Payment method (best-effort across both platforms)
    pay_method = (
        raw.get("payment_method_title")
        or raw.get("payment_method")
        or raw.get("gateway")
        or (raw.get("payment_gateway_names") or [None])[0]
        or "—"
    )
    tx_id = (
        raw.get("transaction_id")
        or raw.get("checkout_id")
        or raw.get("order_number")
        or ""
    )

    # Shipping address
    ship = raw.get("shipping") or raw.get("shipping_address") or {}
    bill = raw.get("billing") or raw.get("billing_address") or {}
    addr = ship if (ship.get("address_1") or ship.get("address1")) else bill
    ship_line1 = addr.get("address_1") or addr.get("address1") or ""
    ship_line2 = addr.get("address_2") or addr.get("address2") or ""
    ship_city  = addr.get("city") or getattr(order, "city", "") or ""
    ship_state = addr.get("state") or addr.get("province") or ""
    ship_post  = addr.get("postcode") or addr.get("zip") or ""
    ship_country = addr.get("country") or getattr(order, "country", "") or ""
    ship_name = (
        f"{addr.get('first_name','') } {addr.get('last_name','')}".strip()
        or getattr(order, "customer_name", "") or "—"
    )

    # Items
    items = raw.get("line_items") or []
    items_rows = []
    for li in items:
        name = li.get("name") or li.get("title") or "Item"
        qty = li.get("quantity") or 1
        try:
            price = float(li.get("subtotal") or li.get("price") or 0)
        except Exception:
            price = 0.0
        attrs = []
        v = li.get("variant_title") or ""
        if v and v.lower() not in ("default title", ""):
            attrs.append(v)
        for m in (li.get("meta_data") or [])[:3]:
            k = m.get("display_key") or m.get("key") or ""
            val = m.get("display_value") or m.get("value") or ""
            if k and val and not k.startswith("_"):
                attrs.append(f"{k}: {val}")
        attrs_str = " · ".join(attrs)
        img = ""
        if isinstance(li.get("image"), dict):
            img = li["image"].get("src") or ""
        if not img:
            img = li.get("image_url") or ""
        items_rows.append({
            "name": name, "qty": qty, "price": _fmt(price),
            "attrs": attrs_str, "image": img,
            "sku": li.get("sku") or "",
        })

    return {
        "kind": kind,
        "brand_name": getattr(store, "name", "") or "—",
        "store_url": getattr(store, "store_url", "") or "",
        "platform_label": platform_label,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "tenant_id": getattr(owner, "id", "") if owner else "",
        "store_id": getattr(store, "id", ""),
        "order_id": order.id,
        "ds_ref": ds_ref,
        "ext_ref": ext_ref,
        "ext_raw": ext_raw,
        "placed_at": (getattr(order, "created_at", None) or _now()),
        "status_label": status_label,
        "payment_status_label": payment_status_label,
        "pay_method": pay_method,
        "tx_id": str(tx_id) if tx_id else "",
        "currency": currency,
        "total_display": total_display,
        "subtotal_display": _fmt(subtotal),
        "shipping_display": _fmt(shipping),
        "tax_display": _fmt(tax),
        "discount_display": _fmt(discount) if discount else "",
        "customer_name": getattr(order, "customer_name", "") or "—",
        "customer_email": getattr(order, "customer_email", "") or "—",
        "customer_phone": getattr(order, "customer_phone", "") or "—",
        "ship_name": ship_name,
        "ship_line1": ship_line1,
        "ship_line2": ship_line2,
        "ship_city": ship_city,
        "ship_state": ship_state,
        "ship_post": ship_post,
        "ship_country": ship_country,
        "items": items_rows,
        "items_count": len(items_rows),
        "wp_admin_url": _wp_admin_url(store, ext_raw),
        "dashboard_url": _dashboard_url(store, order),
    }


def _wp_admin_url(store, ext):
    if (getattr(store, "platform", "") or "").lower() == "woocommerce" and ext:
        return f"{(store.store_url or '').rstrip('/')}/wp-admin/post.php?post={ext}&action=edit"
    return ""


def _dashboard_url(store, order):
    sid = getattr(store, "id", "")
    return f"https://dropsigma.com/dashboard/?store_id={sid}&section=orders&order_id={order.id}"


# ───────────────────────────────────────────────────────────────────────
# HTML render
# ───────────────────────────────────────────────────────────────────────

def _render_html(kind: str, c: dict) -> str:
    is_failed = kind == "failed"
    if is_failed:
        header_grad = "linear-gradient(135deg,#dc2626 0%,#f59e0b 50%,#d4af37 100%)"
        section_label = "⚠️ ORDER FAILED"
        hero_title = "Payment failed on a tenant order"
        hero_money_color = "#dc2626"
        primary_pill_bg = "#fee2e2"
        primary_pill_color = "#991b1b"
        primary_pill_text = "● FAILED"
    else:
        header_grad = "linear-gradient(135deg,#6366f1 0%,#a855f7 50%,#d4af37 100%)"
        section_label = "🛒 NEW ORDER"
        hero_title = "New order received"
        hero_money_color = "#15803d"
        primary_pill_bg = "#dcfce7"
        primary_pill_color = "#15803d"
        primary_pill_text = "● " + (c["payment_status_label"].upper() or "PAID")

    placed_str = ""
    try:
        placed_str = c["placed_at"].strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        placed_str = str(c["placed_at"])

    full_addr = ", ".join([p for p in [c["ship_line1"], c["ship_line2"], c["ship_city"],
                                         c["ship_state"], c["ship_post"], c["ship_country"]] if p])

    # Items rows
    items_html = ""
    for it in c["items"]:
        img = (f'<img src="{_esc(it["image"])}" width="64" height="64" '
               f'style="display:block;width:64px;height:64px;object-fit:cover;border-radius:10px;'
               f'background:#f1f5f9;border:1px solid #e2e8f0;">' if it.get("image") else
               '<div style="width:64px;height:64px;border-radius:10px;background:#f1f5f9;'
               'border:1px solid #e2e8f0;display:table;text-align:center;line-height:64px;font-size:26px;">📦</div>')
        sku = f'<div style="margin-top:6px;font-size:11.5px;color:#94a3b8;font-weight:600;font-family:\'SF Mono\',Menlo,monospace;">SKU: {_esc(it["sku"])}</div>' if it.get("sku") else ""
        attrs = f'<div style="margin-top:4px;font-size:12px;color:#64748b;font-weight:600;">{_esc(it["attrs"])}</div>' if it.get("attrs") else ""
        items_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-bottom:1px solid #f1f5f9;">
          <tr><td style="padding:12px 0;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="68" valign="top">{img}</td>
                <td style="padding-left:14px;vertical-align:top;">
                  <div style="font-size:14px;font-weight:800;color:#0f172a;line-height:1.3;">{_esc(it["name"])}</div>
                  {attrs}
                  {sku}
                  <div style="margin-top:4px;display:inline-block;background:#f1f5f9;color:#475569;font-size:11px;font-weight:700;padding:2px 8px;border-radius:5px;">Qty {it["qty"]}</div>
                </td>
                <td style="vertical-align:top;text-align:right;white-space:nowrap;padding-left:12px;font-size:14px;color:#0f172a;font-weight:800;">{_esc(it["price"])}</td>
              </tr>
            </table>
          </td></tr>
        </table>'''

    discount_row = ""
    if c["discount_display"]:
        discount_row = f'''
        <tr>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">Discount</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#dc2626;font-weight:600;">−{_esc(c["discount_display"])}</td>
        </tr>'''

    wp_admin_link = (f'<div style="margin-top:10px;font-size:11.5px;color:#94a3b8;font-weight:500;">'
                     f'or open <a href="{_esc(c["wp_admin_url"])}" style="color:#64748b;text-decoration:none;font-weight:600;">'
                     f'in tenant\'s admin ↗</a></div>') if c["wp_admin_url"] else ""

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_esc(_subject_for(kind, c))}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#0f172a;">
<center style="width:100%;background:#f1f5f9;padding:32px 16px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" style="width:100%;max-width:640px;background:#ffffff;border-radius:18px;box-shadow:0 20px 50px rgba(15,23,42,0.08);overflow:hidden;">

  <tr><td style="background:{header_grad};padding:24px 32px;color:#fff;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td width="56" style="vertical-align:middle;padding-right:14px;">
        <!-- Drop Sigma icon — served from /static/branding/icon.png on
             dropsigma.com so every email client (including Gmail mobile)
             can fetch it without needing inline base64. White rounded
             frame so the icon reads clearly against any header gradient
             variant (purple for new orders, red/orange for failed). -->
        <div style="width:48px;height:48px;border-radius:12px;background:rgba(255,255,255,.92);box-shadow:0 4px 14px rgba(15,23,42,.28);padding:6px;box-sizing:border-box;text-align:center;">
          <img src="https://dropsigma.com/static/branding/icon.png" alt="Drop Sigma" width="36" height="36" style="display:block;width:36px;height:36px;border-radius:8px;margin:0 auto;">
        </div>
      </td>
      <td style="vertical-align:middle;">
        <div style="font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;opacity:.85;">DROP SIGMA · ADMIN</div>
        <div style="font-size:22px;font-weight:900;margin-top:4px;letter-spacing:-.3px;">{section_label}</div>
      </td>
      <td style="vertical-align:middle;text-align:right;white-space:nowrap;">
        <div style="display:inline-block;background:rgba(255,255,255,.18);border-radius:99px;padding:6px 14px;font-size:11.5px;font-weight:700;border:1px solid rgba(255,255,255,.28);">⚡ Live sync</div>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:28px 32px 16px;background:linear-gradient(180deg,#fbfaff 0%,#ffffff 100%);">
    <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td>
        <div style="font-size:11.5px;font-weight:700;color:#64748b;letter-spacing:.12em;text-transform:uppercase;">{("Failed order — total" if is_failed else "Total amount")}</div>
        <div style="font-size:38px;font-weight:900;color:{hero_money_color};letter-spacing:-.8px;margin-top:4px;">{_esc(c["total_display"])}</div>
        <div style="margin-top:6px;font-size:13px;color:#64748b;font-weight:600;">{_esc(c["brand_name"])} · Order <b style="color:#0f172a;">{_esc(c["ds_ref"])}</b></div>
      </td>
      <td style="text-align:right;vertical-align:top;">
        <span style="display:inline-block;background:{primary_pill_bg};color:{primary_pill_color};font-size:11px;font-weight:800;padding:5px 11px;border-radius:6px;letter-spacing:.04em;text-transform:uppercase;">{primary_pill_text}</span>
        <div style="margin-top:8px;">
          <span style="display:inline-block;background:#eff6ff;color:#1d4ed8;font-size:11px;font-weight:800;padding:5px 11px;border-radius:6px;letter-spacing:.04em;text-transform:uppercase;">⚙ {_esc(c["status_label"])}</span>
        </div>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:18px 32px 6px;">
    <div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Tenant</div>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fbfaff;border:1px solid #e9d5ff;border-radius:14px;">
      <tr><td style="padding:14px 16px;">
        <div style="font-size:14.5px;font-weight:800;color:#0f172a;">{_esc(c["owner_name"])}</div>
        <div style="font-size:12px;color:#64748b;margin-top:2px;">{_esc(c["owner_email"])} · Tenant #{c["tenant_id"]}</div>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-top:1px solid #e9d5ff;">
          <tr>
            <td style="padding:10px 0 0;font-size:12px;color:#64748b;width:38%;">Brand</td>
            <td style="padding:10px 0 0;font-size:12.5px;color:#0f172a;font-weight:700;">{_esc(c["brand_name"])}</td>
          </tr>
          <tr>
            <td style="padding:6px 0 0;font-size:12px;color:#64748b;">Store URL</td>
            <td style="padding:6px 0 0;font-size:12.5px;color:#0f172a;font-weight:700;"><a href="{_esc(c["store_url"])}" style="color:#6366f1;text-decoration:none;">{_esc(c["store_url"])} ↗</a></td>
          </tr>
          <tr>
            <td style="padding:6px 0 0;font-size:12px;color:#64748b;">Platform</td>
            <td style="padding:6px 0 0;font-size:12.5px;color:#0f172a;font-weight:700;">{_esc(c["platform_label"])}</td>
          </tr>
        </table>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:18px 32px 6px;">
    <div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Order</div>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e2e8f0;border-radius:14px;">
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;width:40%;">Drop Sigma reference</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:800;font-family:'SF Mono',Menlo,monospace;">{_esc(c["ds_ref"])}</td></tr>
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">External order ID</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600;font-family:'SF Mono',Menlo,monospace;">{_esc(c["ext_ref"])}</td></tr>
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">Placed at</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600;">{_esc(placed_str)}</td></tr>
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">Subtotal</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600;">{_esc(c["subtotal_display"])}</td></tr>
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">Shipping</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600;">{_esc(c["shipping_display"])}</td></tr>
      <tr><td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:12px;color:#64748b;">Tax</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#0f172a;font-weight:600;">{_esc(c["tax_display"])}</td></tr>
      {discount_row}
      <tr><td style="padding:14px 16px;font-size:13px;color:#0f172a;font-weight:800;">Total charged</td>
          <td style="padding:14px 16px;font-size:16px;color:{hero_money_color};font-weight:900;letter-spacing:-.3px;">{_esc(c["total_display"])}</td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:18px 32px 6px;">
    <div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Payment</div>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{('#fef2f2' if is_failed else '#f0fdf4')};border:1px solid {('#fecaca' if is_failed else '#bbf7d0')};border-radius:14px;">
      <tr><td style="padding:14px 16px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-size:12px;color:#64748b;font-weight:700;width:38%;">Method</td>
              <td style="font-size:13px;color:#0f172a;font-weight:700;">💳 {_esc(c["pay_method"])}</td></tr>
          <tr><td style="padding-top:8px;font-size:12px;color:#64748b;font-weight:700;">Status</td>
              <td style="padding-top:8px;"><span style="display:inline-block;background:{('#dc2626' if is_failed else '#16a34a')};color:#fff;font-size:11px;font-weight:800;padding:3px 10px;border-radius:5px;letter-spacing:.04em;">{("✗ FAILED" if is_failed else "✓ " + (c["payment_status_label"].upper() or "PAID"))}</span></td></tr>
          {f'<tr><td style="padding-top:8px;font-size:12px;color:#64748b;font-weight:700;">Transaction ID</td><td style="padding-top:8px;font-size:12px;color:#0f172a;font-weight:600;font-family:Menlo,monospace;">{_esc(c["tx_id"])}</td></tr>' if c["tx_id"] else ""}
        </table>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:18px 32px 6px;">
    <div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Customer</div>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e2e8f0;border-radius:14px;">
      <tr><td style="padding:14px 16px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="font-size:12px;color:#64748b;width:38%;">Name</td>
              <td style="font-size:13px;color:#0f172a;font-weight:700;">{_esc(c["customer_name"])}</td></tr>
          <tr><td style="padding-top:8px;font-size:12px;color:#64748b;">Email</td>
              <td style="padding-top:8px;font-size:13px;color:#0f172a;font-weight:600;"><a href="mailto:{_esc(c["customer_email"])}" style="color:#6366f1;text-decoration:none;">{_esc(c["customer_email"])}</a></td></tr>
          <tr><td style="padding-top:8px;font-size:12px;color:#64748b;">Phone</td>
              <td style="padding-top:8px;font-size:13px;color:#0f172a;font-weight:600;">{_esc(c["customer_phone"])}</td></tr>
        </table>
      </td></tr>
    </table>
  </td></tr>

  {('<tr><td style="padding:18px 32px 6px;"><div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Shipping address</div><table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fffbeb;border:1px solid #fde68a;border-radius:14px;"><tr><td style="padding:14px 16px;"><div style="font-size:13.5px;color:#0f172a;font-weight:800;">📍 ' + _esc(c["ship_name"]) + '</div><div style="margin-top:6px;font-size:13px;color:#0f172a;font-weight:600;line-height:1.55;">' + _esc(full_addr) + '</div></td></tr></table></td></tr>') if full_addr else ""}

  {('<tr><td style="padding:18px 32px 6px;"><div style="font-size:10.5px;font-weight:800;color:#64748b;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px;">Items (' + str(c["items_count"]) + ')</div><table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e2e8f0;border-radius:14px;"><tr><td style="padding:6px 16px;">' + items_html + '</td></tr></table></td></tr>') if items_html else ""}

  <tr><td style="padding:22px 32px 8px;text-align:center;">
    <a href="{_esc(c["dashboard_url"])}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;text-decoration:none;font-size:14px;font-weight:800;padding:13px 28px;border-radius:12px;box-shadow:0 8px 22px rgba(99,102,241,0.32);letter-spacing:.01em;">View order in dashboard →</a>
    {wp_admin_link}
  </td></tr>

  <tr><td style="background:#0f172a;padding:20px 32px;color:#94a3b8;font-size:11.5px;line-height:1.7;border-radius:0 0 18px 18px;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="vertical-align:top;">
        <div style="color:#fff;font-weight:800;font-size:13px;letter-spacing:.04em;">Drop Sigma</div>
        <div style="margin-top:4px;">Sent automatically by the platform · <a href="mailto:noreply@dropsigma.com" style="color:#94a3b8;text-decoration:none;">noreply@dropsigma.com</a></div>
      </td>
      <td style="vertical-align:top;text-align:right;font-family:Menlo,monospace;font-size:10.5px;color:#64748b;">
        tenant:{c["tenant_id"]} · store:{c["store_id"]}<br>order:{c["ext_raw"]} · evt:{kind}
      </td>
    </tr></table>
    <div style="margin-top:14px;padding-top:14px;border-top:1px solid rgba(255,255,255,.08);font-size:10.5px;color:#64748b;">
      You're receiving this because you are the platform owner.<br>
      To stop receiving these notifications, unset <code style="color:#cbd5e1;">FOUNDER_NOTIFY_EMAIL</code> in Railway env vars.
    </div>
  </td></tr>

</table>
</center>
</body></html>'''
