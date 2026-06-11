"""Carrier-agnostic 6-stage shipment timeline builder.

The single point of truth for what a customer sees on
``track.dropsigma.com``. Takes a Drop Sigma ``Order`` and outputs a
clean 6-stage timeline with our own copy — the underlying carrier
(Yuntrack, DHL, USPS, FedEx, China Post, 4PX, …) is NEVER mentioned,
no origin city, no transit airport, no hand-off partner. The customer
sees a logistics journey that looks like it's run end-to-end by the
seller's own brand and powered by Drop Sigma's "logistics network".

If you ever extend this module to ingest live carrier-API events
(Yuntrack tracking webhook, AfterShip pull, etc), keep the contract:

  - Stage labels are FIXED:
       Order placed · In production · Shipment created ·
       Origin hub · In transit · Destination
  - Stage descriptions come from ``_STAGE_COPY`` below. They are
    written by us, not echoed from carrier feeds.
  - Stage timestamps may be drawn from carrier events, but anything
    text-shaped (location names, hub codes, carrier hand-off notes)
    MUST be discarded before reaching the response.
  - Use ``_sanitize_text(s)`` as a defence-in-depth scrubber for any
    string that DOES come from a third party.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

# Fixed customer-facing copy. NEVER include carrier names, cities, or
# country names of the origin here.
STAGE_DEFS = [
    {
        "key":   "order_placed",
        "code":  "ORDER",
        "label": "Order placed",
        "desc":  "Payment authorised. Order received and queued for fulfilment.",
    },
    {
        "key":   "in_production",
        "code":  "PROD",
        "label": "In production",
        "desc":  "Your items were prepared and quality-checked by our fulfilment team.",
    },
    {
        "key":   "shipment_created",
        "code":  "CREATED",
        "label": "Shipment created",
        "desc":  "Air waybill issued. Parcel manifested for express freight routing.",
    },
    {
        "key":   "origin_hub",
        "code":  "ORG",
        "label": "Origin hub",
        "desc":  "Collected and scanned into the origin sortation centre, then loaded onto the outbound flight.",
    },
    {
        "key":   "in_transit",
        "code":  "TRANSIT",
        "label": "In transit",
        "desc":  "Your shipment is in flight on a scheduled long-haul route to the destination country.",
    },
    {
        "key":   "destination",
        "code":  "DELIVERED",
        "label": "Destination",
        "desc":  "Your shipment has reached the delivery destination.",
    },
]

# ── Anti-leak sanitiser ──────────────────────────────────────────────
# Any free-text field that has touched a third-party carrier feed gets
# run through this. If a known carrier name or origin keyword is
# detected, the field is wiped to empty rather than leaking.
_CARRIER_BLACKLIST = {
    # Common Chinese cross-border carriers
    "yuntrack", "yun track", "yun express", "yunexpress", "yunexp",
    "4px", "4 px", "4-px",
    "china post", "chinapost",
    "ems", "ems china",
    "sf express", "sfexpress", "shunfeng",
    "yt express", "yt-express", "yto express",
    "cainiao", "alibaba",
    "winit", "ws express",
    # Mainstream carriers
    "dhl", "dhl ecom", "dhl ecommerce",
    "ups", "united parcel",
    "fedex", "federal express",
    "usps", "us postal",
    "tnt express",
    "aramex",
    # Origin/transit cities/regions we should never leak
    "shenzhen", "guangzhou", "beijing", "shanghai", "hangzhou",
    "yiwu", "ningbo", "qingdao",
    "hong kong", "hongkong", "hk",
    "china", "prc",
}

def _sanitize_text(s: str) -> str:
    """Wipe a string if it contains any blacklisted carrier / origin
    keyword. Conservative: if there's any whiff of a leak, return empty
    so the caller falls back to our fixed copy."""
    if not s:
        return ""
    low = s.lower()
    for term in _CARRIER_BLACKLIST:
        if term in low:
            return ""
    return s


# ── Time-based stage progression heuristic ──────────────────────────
# When no live carrier events are available (most of the time today),
# we synthesise timestamps from the order's lifecycle so the customer
# still sees a believable journey. Each stage advances after a fixed
# offset from the previous one, capped so we never "predict" a stage
# that hasn't actually happened.

# Offsets, in hours, from order.created_at to each stage's start.
_STAGE_OFFSETS_H = {
    "order_placed":     0,
    "in_production":    8,
    "shipment_created": 24,
    "origin_hub":       40,
    "in_transit":       56,
    "destination":      168,  # delivered ~7 days after placement
}


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _classify_active_stage(order) -> str:
    """Map order status → which stage is currently 'active'.

    Returns one of the STAGE_DEFS keys, or 'destination' if delivered,
    or 'failed' for failed/cancelled (which we render as a special
    state in the timeline).
    """
    status = (
        (getattr(order, "fulfillment_status", "") or "")
        or (getattr(order, "payment_status", "") or "")
    ).strip().lower()

    # Failed / cancelled — special state
    if status in ("failed", "cancelled", "canceled", "voided"):
        return "failed"

    # Delivered
    if status in ("delivered", "completed"):
        return "destination"

    # Has tracking → at least at shipment_created
    has_tracking = bool((getattr(order, "tracking_number", "") or "").strip())

    if status in ("shipped", "in_transit", "in transit"):
        return "in_transit"

    if status in ("on-hold", "on hold"):
        return "origin_hub"

    if has_tracking and status in ("processing", "pending"):
        # Has tracking + processing → in transit
        return "in_transit"

    if status == "processing":
        return "in_production"

    # Default for very new orders
    return "order_placed"


def _stage_timestamp(order, stage_key: str, active_key: str) -> datetime | None:
    """Compute a stage's timestamp.

    - For stages BEFORE the active one: synthesise from order.created_at
      + the configured offset (so the customer sees a believable
      progression even when we don't have real carrier events).
    - For the ACTIVE stage: return None (we render "Now" instead).
    - For stages AFTER the active one: return None (those haven't
      happened yet — and the page only renders up to the active one).
    """
    created = getattr(order, "created_at", None)
    if not created:
        return None

    # Stages after active haven't happened.
    stage_order = [s["key"] for s in STAGE_DEFS]
    if stage_order.index(stage_key) > stage_order.index(active_key):
        return None

    # Active stage shows "Now".
    if stage_key == active_key:
        return None

    # For destination (delivered), use the actual delivered_at if set.
    if stage_key == "destination":
        delivered = getattr(order, "delivered_at", None)
        return delivered or (created + timedelta(hours=_STAGE_OFFSETS_H["destination"]))

    # All other completed stages: synthesised from offset, capped to "now"
    # so we never show a future timestamp.
    offset_h = _STAGE_OFFSETS_H.get(stage_key, 0)
    ts = created + timedelta(hours=offset_h)
    now = _utcnow()
    if ts > now:
        ts = now
    return ts


def build_stages(order) -> dict:
    """Build the full 6-stage timeline for a given order.

    Returns a dict ready to be JSON-serialised or passed to the
    template:

        {
            "stages":       [{key, label, code, desc, status, ts_iso, ts_display}, …],
            "active_key":   "in_transit",
            "active_index": 4,
            "is_failed":    False,
            "is_delivered": False,
            "headline":     "In international transit · Departed origin hub",
            "headline_sub": "Scheduled long-haul route. Next update on arrival …",
            "updated_label": "Updated 2 h ago",
        }
    """
    active_key = _classify_active_stage(order)
    is_failed = active_key == "failed"
    if is_failed:
        # Render whatever HAS happened up to whatever last stage we know
        # about. For most failed orders, we know at most up to shipment
        # created.
        active_key = "shipment_created" if (order.tracking_number or "") else "in_production"

    stage_keys = [s["key"] for s in STAGE_DEFS]
    active_idx = stage_keys.index(active_key) if active_key in stage_keys else 0

    out_stages = []
    for idx, sdef in enumerate(STAGE_DEFS):
        if idx < active_idx:
            status = "done"
        elif idx == active_idx:
            status = "active"
        else:
            status = "pending"

        ts = _stage_timestamp(order, sdef["key"], active_key)
        out_stages.append({
            "key":   sdef["key"],
            "code":  sdef["code"],
            "label": sdef["label"],
            "desc":  sdef["desc"],
            "status": status,
            "ts_iso": ts.isoformat() if ts else "",
            "ts_short": ts.strftime("%d %b") if ts else "",
            "ts_full":  ts.strftime("%d %b · %H:%M UTC") if ts else "",
        })

    # Headline based on active stage
    headline_map = {
        "order_placed":     ("Order received", "Your order is queued for fulfilment."),
        "in_production":    ("Order in production",  "Our fulfilment team is preparing and quality-checking your items."),
        "shipment_created": ("Shipment created",     "Air waybill issued. Your parcel is ready to leave the facility."),
        "origin_hub":       ("At origin hub",        "Your parcel has been processed and loaded for outbound flight."),
        "in_transit":       ("In international transit · Departed origin hub",
                             "Scheduled long-haul route. Next update on arrival at destination country."),
        "destination":      ("Delivered",            "Your shipment has reached its destination."),
    }
    headline, headline_sub = headline_map.get(active_key, ("In transit", ""))

    # Updated label — based on last completed stage timestamp
    updated_label = ""
    last_done_ts = None
    for s in out_stages:
        if s["status"] == "done" and s["ts_iso"]:
            try:
                last_done_ts = datetime.fromisoformat(s["ts_iso"])
            except Exception:
                last_done_ts = None
    if last_done_ts:
        delta = _utcnow() - last_done_ts
        secs = max(0, int(delta.total_seconds()))
        if secs < 3600:
            updated_label = f"Updated {max(1, secs // 60)} min ago"
        elif secs < 86400:
            updated_label = f"Updated {secs // 3600} h ago"
        else:
            updated_label = f"Updated {secs // 86400} d ago"

    return {
        "stages":        out_stages,
        "active_key":    active_key,
        "active_index":  active_idx,
        "is_failed":     is_failed,
        "is_delivered":  active_key == "destination",
        "headline":      headline,
        "headline_sub":  headline_sub,
        "updated_label": updated_label or "Live tracking",
    }


# ── Tenant branding helpers ─────────────────────────────────────────
def tenant_brand_mark(store_name: str) -> str:
    """Two-letter mark for the brand circle. Falls back to first 2
    chars of the store name."""
    if not store_name:
        return "DS"
    parts = [p for p in store_name.strip().split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return store_name.strip()[:2].upper()


def build_brand_payload(store) -> dict:
    """Tenant-branded chrome for the detail page. The customer ONLY
    sees the seller's brand at the top (mark + name) — no contact
    links, no support email, no store URL. That's deliberate so the
    page can never be used to bounce the customer back to the seller's
    direct site (which sometimes reveals the supplier behind the scenes)
    or to leak any tenant PII."""
    if not store:
        return {
            "name": "Drop Sigma",
            "sub":  "Shipment tracking",
            "mark": "DS",
        }
    return {
        "name": store.name or "Shipment tracking",
        "sub":  "Shipment tracking · Express",
        "mark": tenant_brand_mark(store.name or ""),
    }


# ── Public-friendly tracking number normalisation ──────────────────
def normalize_tracking_input(raw: str) -> str:
    """Tracking numbers are mostly case-insensitive and may be pasted
    with surrounding whitespace, soft hyphens, or zero-width spaces."""
    if not raw:
        return ""
    s = raw.strip()
    # Strip Unicode invisibles + the soft hyphen.
    for ch in ("­", "​", "‌", "‍", "﻿"):
        s = s.replace(ch, "")
    return s.upper()
