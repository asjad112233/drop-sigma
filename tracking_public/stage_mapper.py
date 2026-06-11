"""Per-carrier 7-stage / fallback-6-stage shipment timeline builder.

Single source of truth for what a customer sees on
``track.dropsigma.com``. Two paths:

  **Carrier-aware (preferred)** — When the order has a recognised
  carrier (``order.tracking_carrier``) we render its OWN stepper
  flow, pre-pended with the two Drop Sigma platform stages we
  uniquely know about. For Yuntrack/YunExpress that's:

      Order placed  →  Shipment created           (platform stages)
                    →  Pickup
                    →  Departed from origin       (the active flow
                    →  Arrived at destination      shown to the user
                    →  Local carrier on the way    mirrors what the
                    →  Delivered                   carrier reports)

  The active stage and per-stage timestamps come from real carrier
  events sanitised before render.

  **Generic 6-stage fallback** — When the carrier is unknown OR
  ``tracking_events`` is empty (very new order, no scrape yet).
  Synthesises a conservative progression from order lifecycle so the
  page still has structure. NEVER advances to ``destination`` without
  hard fulfillment_status=='delivered'.

In every case:
  - Carrier brand names ("yuntrack", "dhl", "intelcom|dragonfly", …)
    are wiped from any rendered text.
  - Origin cities/countries are redacted to neutral phrasing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── Stage definitions ───────────────────────────────────────────────
# Generic 6-stage flow (fallback when we don't know the carrier).
GENERIC_STAGE_DEFS = [
    {"key": "order_placed",     "code": "ORDER",     "label": "Order placed",
     "desc": "Payment authorised. Order received and queued for fulfilment."},
    {"key": "in_production",    "code": "PROD",      "label": "In production",
     "desc": "Your items were prepared and quality-checked by our fulfilment team."},
    {"key": "shipment_created", "code": "CREATED",   "label": "Shipment created",
     "desc": "Air waybill issued. Parcel manifested for express freight routing."},
    {"key": "origin_hub",       "code": "ORG",       "label": "Origin hub",
     "desc": "Collected and scanned into the origin sortation centre, then loaded onto the outbound flight."},
    {"key": "in_transit",       "code": "TRANSIT",   "label": "In transit",
     "desc": "Your shipment is in flight on a scheduled long-haul route to the destination country."},
    {"key": "destination",      "code": "DELIVERED", "label": "Destination",
     "desc": "Your shipment has reached the delivery destination."},
]

# Yuntrack-mirror 7-stage flow: 2 platform stages + YunExpress's 5.
YUNTRACK_STAGE_DEFS = [
    {"key": "order_placed",        "code": "ORDER",   "label": "Order placed",
     "desc": "Payment authorised. Order received and queued for fulfilment.",
     "source": "platform"},
    {"key": "shipment_created",    "code": "CREATED", "label": "Shipment created",
     "desc": "Air waybill issued. Parcel manifested for express freight routing.",
     "source": "platform"},
    {"key": "pickup",              "code": "PICKUP",  "label": "Pickup",
     "desc": "Shipment information received and collected at the origin facility.",
     "source": "carrier"},
    {"key": "departed_origin",     "code": "ORG",     "label": "Departed from origin",
     "desc": "Parcel has left the origin facility on its outbound flight.",
     "source": "carrier"},
    {"key": "arrived_destination", "code": "DEST",    "label": "Arrived at destination",
     "desc": "Parcel has arrived in the destination country and is being processed for delivery.",
     "source": "carrier"},
    {"key": "local_carrier",       "code": "LOCAL",   "label": "Local carrier on the way",
     "desc": "Out for delivery with the local courier on the final leg.",
     "source": "carrier"},
    {"key": "delivered",           "code": "DELIVERED","label": "Delivered",
     "desc": "Your shipment has reached the delivery destination.",
     "source": "carrier"},
]

# Back-compat alias: existing callers still import STAGE_DEFS.
STAGE_DEFS = GENERIC_STAGE_DEFS


def _template_for_carrier(carrier: str) -> tuple[list[dict], str]:
    """Returns (stage_defs, template_key). Template key is used by the
    template to choose icons / styling."""
    if carrier == "yuntrack":
        return YUNTRACK_STAGE_DEFS, "yuntrack"
    return GENERIC_STAGE_DEFS, "generic"


# ── Anti-leak sanitiser ──────────────────────────────────────────────
_CARRIER_NAMES_WIPE = (
    "yuntrack", "yun track", "yun express", "yunexpress", "yunexp",
    "4px", "4 px", "4-px",
    "china post", "chinapost",
    "ems china",
    "sf express", "sfexpress", "shunfeng",
    "yt express", "yt-express", "yto express",
    "cainiao", "alibaba",
    "winit", "ws express",
    "dhl ecommerce", "dhl ecom", "dhl",
    "ups",
    "fedex", "federal express",
    "usps", "us postal",
    "tnt express",
    "aramex",
    "royal mail",
    "evri", "hermes",
    "intelcom", "dragonfly",
)

_ORIGIN_LOCATIONS_REDACT = (
    "shenzhen", "guangzhou", "beijing", "shanghai", "hangzhou",
    "yiwu", "ningbo", "qingdao", "xiamen", "dongguan", "chengdu",
    "hong kong", "hongkong",
    "mainland china", "china", "prc",
)


def _sanitize_text(s: str) -> str:
    if not s:
        return ""
    text = s

    for name in sorted(_CARRIER_NAMES_WIPE, key=len, reverse=True):
        idx = 0
        low_name = name.lower()
        while True:
            low = text.lower()
            pos = low.find(low_name, idx)
            if pos < 0:
                break
            text = text[:pos] + text[pos + len(name):]
            idx = pos
    for loc in sorted(_ORIGIN_LOCATIONS_REDACT, key=len, reverse=True):
        idx = 0
        low_loc = loc.lower()
        while True:
            low = text.lower()
            pos = low.find(low_loc, idx)
            if pos < 0:
                break
            text = text[:pos] + "the origin facility" + text[pos + len(loc):]
            idx = pos + len("the origin facility")

    text = " ".join(text.split())
    text = text.replace(" ,", ",").replace(", ,", ",").replace(" .", ".")

    import re as _re
    text = _re.sub(
        r"\bthe origin facility(?:[\s,/|·\-:;]+the origin facility)+",
        "the origin facility",
        text,
        flags=_re.IGNORECASE,
    )
    text = _re.sub(r"[|/·]{1,}\s*$", "", text)
    text = _re.sub(r"\s*[|/·]\s*[|/·]\s*", " ", text)
    text = _re.sub(r":\s*$", "", text)
    text = " ".join(text.split())
    text = text.strip(" ,.|/·:-")
    return text


# ── Time helpers ────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _ago_label(dt: datetime) -> str:
    secs = max(0, int((_utcnow() - dt).total_seconds()))
    if secs < 60:
        return "Updated just now"
    if secs < 3600:
        return f"Updated {secs // 60} min ago"
    if secs < 86400:
        return f"Updated {secs // 3600} h ago"
    return f"Updated {secs // 86400} d ago"


# ── Yuntrack-mirror builder ─────────────────────────────────────────
def _earliest_event_ts(events: list, *predicates) -> datetime | None:
    """Earliest event whose raw text matches any of the keyword
    predicates (case-insensitive)."""
    for ev in events:
        raw = (ev.get("raw") or "").lower()
        for needles in predicates:
            for needle in needles:
                if needle in raw:
                    ts = _parse_iso(ev.get("ts") or "")
                    if ts:
                        return ts
    return None


def _build_yuntrack_stages(order) -> dict:
    """Render the 7-stage Yuntrack-mirror timeline for an order with a
    populated tracking_events list and tracking_carrier='yuntrack'."""
    events_raw = getattr(order, "tracking_events", None) or []
    carrier_stage = (getattr(order, "tracking_carrier_stage", "") or "").strip()
    stage_defs, template_key = YUNTRACK_STAGE_DEFS, "yuntrack"
    stage_keys = [s["key"] for s in stage_defs]

    # ── Sanitised, sorted UI events ──────────────────────────────
    ui_events = []
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        raw = (ev.get("raw") or "").strip()
        ts = _parse_iso(ev.get("ts") or "")
        if not raw or not ts:
            continue
        clean = _sanitize_text(raw)
        ui_events.append({"ts": ts, "raw": raw, "clean": clean or raw})
    ui_events.sort(key=lambda e: e["ts"])

    # ── Determine active stage ───────────────────────────────────
    active_key = carrier_stage if carrier_stage in stage_keys else ""
    if not active_key:
        # Fall back: derive from latest event using the YunExpress
        # classifier (kept off the hot path import to avoid pulling
        # Playwright machinery just to render).
        try:
            from orders.yuntrack_scraper import classify_yunexpress_stage
            latest_classified = ""
            for ev in reversed(events_raw):
                c = classify_yunexpress_stage((ev.get("raw") or ""))
                if c:
                    latest_classified = c
                    break
            if latest_classified in stage_keys:
                active_key = latest_classified
        except Exception:
            pass
    if not active_key:
        # Last resort: shipment_created (we at least know we have a
        # tracking number).
        active_key = "shipment_created"

    active_idx = stage_keys.index(active_key)
    last_event_ts = ui_events[-1]["ts"] if ui_events else None

    # ── Per-stage timestamps ──────────────────────────────────────
    # Platform stages: we know created_at + ~24h offset for the
    # shipment_created step (a reasonable bound), plus we never advance
    # past it from synthesis here — the carrier stages take over.
    created_at = getattr(order, "created_at", None)

    # Per-carrier-stage timestamps from real events.
    pickup_ts            = _earliest_event_ts(events_raw, (
        "shipment information received", "information received",
        "picked up", "collected", "shipment created",
        "label created", "accepted by carrier",
    ))
    departed_origin_ts   = _earliest_event_ts(events_raw, (
        "departed from origin", "departed origin",
        "departed from sort facility", "international flight has departed",
        "flight has departed", "arrived at origin facility",
        "country of origin commences customs",
        "arrived at the origin international airport",
        "loaded onto flight",
    ))
    arrived_dest_ts      = _earliest_event_ts(events_raw, (
        "arrived at destination", "destination customs",
        "destination facility", "arrived in country",
        "released by customs", "cleared destination",
    ))
    local_carrier_ts     = _earliest_event_ts(events_raw, (
        "out for delivery", "loaded for delivery",
        "transferred to local carrier", "last mile",
        "last-mile", "with delivery driver",
    ))
    delivered_ts         = _earliest_event_ts(events_raw, (
        "delivered to recipient", "successfully delivered",
        "package delivered", "delivery completed",
        "signed by", "signed for",
    ))
    stage_ts = {
        "order_placed":        created_at,
        "shipment_created":    created_at + timedelta(hours=12) if created_at else None,
        "pickup":              pickup_ts,
        "departed_origin":     departed_origin_ts,
        "arrived_destination": arrived_dest_ts,
        "local_carrier":       local_carrier_ts,
        "delivered":           delivered_ts,
    }
    # Carrier-stage timestamp fallback: if a stage in the past has no
    # direct evidence, use the earliest event whose classified stage
    # is THAT one, else leave blank (honest).

    # ── Build stepper rows ───────────────────────────────────────
    out_stages = []
    for idx, sdef in enumerate(stage_defs):
        if idx < active_idx:
            status = "done"
        elif idx == active_idx:
            status = "active"
        else:
            status = "pending"
        ts = stage_ts.get(sdef["key"])
        out_stages.append({
            "key":     sdef["key"],
            "code":    sdef["code"],
            "label":   sdef["label"],
            "desc":    sdef["desc"],
            "status":  status,
            "ts_iso":   ts.isoformat() if ts else "",
            "ts_short": ts.strftime("%d %b") if ts else "",
            "ts_full":  ts.strftime("%d %b · %H:%M UTC") if ts else "",
        })

    # ── Headline + events payload ────────────────────────────────
    headline = ""
    headline_sub = ""
    if ui_events:
        latest = ui_events[-1]
        headline = latest["clean"]
        # Match against the active stage's description as subline.
        for sdef in stage_defs:
            if sdef["key"] == active_key:
                headline_sub = sdef["desc"]
                break
    else:
        for sdef in stage_defs:
            if sdef["key"] == active_key:
                headline = sdef["label"]
                headline_sub = sdef["desc"]
                break

    updated_label = (
        _ago_label(last_event_ts) if last_event_ts else "Live tracking"
    )

    events_payload = [
        {
            "ts_iso":      ev["ts"].isoformat(),
            "ts_full":     ev["ts"].strftime("%d %b · %H:%M UTC"),
            "ts_short":    ev["ts"].strftime("%d %b"),
            "text":        ev["clean"],
            "stage":       "",   # not needed for Yuntrack-mirror render
            "stage_label": "",
        }
        for ev in ui_events
    ]

    return {
        "stages":         out_stages,
        "active_key":     active_key,
        "active_index":   active_idx,
        "is_failed":      False,
        "is_delivered":   active_key == "delivered",
        "headline":       headline,
        "headline_sub":   headline_sub,
        "updated_label":  updated_label,
        "events":         events_payload,
        "events_source":  "carrier",
        "template":       template_key,
    }


# ── Generic 6-stage synthesised fallback ────────────────────────────
_GENERIC_STAGE_OFFSETS_H = {
    "order_placed":     0,
    "in_production":    8,
    "shipment_created": 24,
    "origin_hub":       40,
    "in_transit":       56,
}


def _synthesised_active_stage(order) -> str:
    """Conservative classification for orders with no carrier events.

    Critical: requires fulfillment_status to explicitly say 'delivered'
    to advance to destination. A stale delivered_at without matching
    status is treated as in_transit — defends against the old over-
    eager 'deliver' substring check that wrongly stamped delivered_at
    on many in-flight parcels.
    """
    status = (
        (getattr(order, "fulfillment_status", "") or "")
        or (getattr(order, "payment_status", "") or "")
    ).strip().lower()

    if status in ("failed", "cancelled", "canceled", "voided"):
        return "failed"
    if status in ("delivered",):
        return "destination"

    has_tracking = bool((getattr(order, "tracking_number", "") or "").strip())
    if has_tracking:
        if status in ("shipped", "in_transit", "in transit"):
            return "in_transit"
        if status in ("on-hold", "on hold"):
            return "origin_hub"
        return "in_transit"

    if status == "processing":
        return "in_production"
    return "order_placed"


def _synthesised_stage_timestamp(order, stage_key: str, active_key: str) -> datetime | None:
    stage_order = [s["key"] for s in GENERIC_STAGE_DEFS]
    if stage_order.index(stage_key) > stage_order.index(active_key):
        return None
    if stage_key == active_key:
        return None
    if stage_key == "destination":
        return getattr(order, "delivered_at", None)
    created = getattr(order, "created_at", None)
    if not created:
        return None
    offset_h = _GENERIC_STAGE_OFFSETS_H.get(stage_key, 0)
    ts = created + timedelta(hours=offset_h)
    now = _utcnow()
    return ts if ts <= now else now


def _build_generic_stages(order) -> dict:
    """Generic 6-stage synthesised renderer — fallback when no carrier
    feed available."""
    active_key = _synthesised_active_stage(order)
    is_failed = False
    if active_key == "failed":
        is_failed = True
        active_key = "shipment_created" if (order.tracking_number or "") else "in_production"

    stage_keys = [s["key"] for s in GENERIC_STAGE_DEFS]
    if active_key not in stage_keys:
        active_key = "order_placed"
    active_idx = stage_keys.index(active_key)

    out_stages = []
    for idx, sdef in enumerate(GENERIC_STAGE_DEFS):
        if idx < active_idx:
            status = "done"
        elif idx == active_idx:
            status = "active"
        else:
            status = "pending"
        ts = _synthesised_stage_timestamp(order, sdef["key"], active_key) if idx < active_idx else None
        out_stages.append({
            "key":     sdef["key"],
            "code":    sdef["code"],
            "label":   sdef["label"],
            "desc":    sdef["desc"],
            "status":  status,
            "ts_iso":   ts.isoformat() if ts else "",
            "ts_short": ts.strftime("%d %b") if ts else "",
            "ts_full":  ts.strftime("%d %b · %H:%M UTC") if ts else "",
        })

    fallback_map = {
        "order_placed":     ("Order received", "Your order is queued for fulfilment."),
        "in_production":    ("Order in production", "Our fulfilment team is preparing and quality-checking your items."),
        "shipment_created": ("Shipment created", "Air waybill issued. Your parcel is ready to leave the facility."),
        "origin_hub":       ("At origin hub", "Your parcel has been processed and loaded for outbound flight."),
        "in_transit":       ("In international transit", "Scheduled long-haul route. Next update on arrival at destination country."),
        "destination":      ("Delivered", "Your shipment has reached its destination."),
    }
    headline, headline_sub = fallback_map.get(active_key, ("In transit", ""))

    last_done_ts = None
    for s in out_stages:
        if s["status"] == "done" and s["ts_iso"]:
            last_done_ts = _parse_iso(s["ts_iso"]) or last_done_ts
    updated_label = _ago_label(last_done_ts) if last_done_ts else "Live tracking"

    return {
        "stages":         out_stages,
        "active_key":     active_key,
        "active_index":   active_idx,
        "is_failed":      is_failed,
        "is_delivered":   active_key == "destination",
        "headline":       headline,
        "headline_sub":   headline_sub,
        "updated_label":  updated_label,
        "events":         [],
        "events_source":  "synthesised",
        "template":       "generic",
    }


# ── Public entry point ──────────────────────────────────────────────
def build_stages(order) -> dict:
    """Build the timeline for an Order. Picks per-carrier template
    when applicable, generic fallback otherwise."""
    carrier = (getattr(order, "tracking_carrier", "") or "").strip().lower()
    events  = getattr(order, "tracking_events", None) or []

    if carrier == "yuntrack" and events:
        return _build_yuntrack_stages(order)

    return _build_generic_stages(order)


# ── Tenant branding helpers ─────────────────────────────────────────
def tenant_brand_mark(store_name: str) -> str:
    if not store_name:
        return "DS"
    parts = [p for p in store_name.strip().split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return store_name.strip()[:2].upper()


def build_brand_payload(store) -> dict:
    if not store:
        return {"name": "Drop Sigma", "sub": "Shipment tracking", "mark": "DS"}
    return {
        "name": store.name or "Shipment tracking",
        "sub":  "Shipment tracking · Express",
        "mark": tenant_brand_mark(store.name or ""),
    }


# ── Public-friendly tracking number normalisation ──────────────────
def normalize_tracking_input(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    for ch in ("­", "​", "‌", "‍", "﻿"):
        s = s.replace(ch, "")
    return s.upper()
