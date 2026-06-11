"""Carrier-aware 6-stage shipment timeline builder.

The single source of truth for what a customer sees on
``track.dropsigma.com``. Two modes:

1. **Real carrier events** (preferred). When ``order.tracking_events``
   is populated by the carrier puller (see ``orders/carrier_tracking.py``),
   we derive the active stage from the LATEST classified event, the
   per-stage timestamps from the EARLIEST event mapped to each stage,
   and the shipment-events list from the full sanitised event feed.

2. **Synthesised fallback** (used until first carrier pull lands).
   When no events are available, we infer a conservative stage from
   the order's lifecycle status. We deliberately do NOT advance to
   ``destination`` here without a hard ``delivered_at`` — the worst-
   case mode is "the page is a little behind reality", not "the page
   claims delivered when the parcel is still in flight".

In every case:
  - Stage LABELS are fixed (Order placed / In production / Shipment
    created / Origin hub / In transit / Destination).
  - Carrier names, origin cities, hand-off partners are stripped
    by ``_sanitize_text`` before any string reaches the customer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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

STAGE_ORDER = [s["key"] for s in STAGE_DEFS]


# ── Anti-leak sanitiser ──────────────────────────────────────────────
# Any free-text field that has touched a third-party carrier feed gets
# run through this. Two layers:
#   - whole-string wipe (replace match with empty) for words we never
#     want anywhere on the page (carrier brand names);
#   - token-level redaction (replace match with a neutral word) for
#     things the sentence might still read sensibly without (city
#     names → "the origin facility").
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
    """Defence-in-depth scrubber. Removes carrier brand names entirely
    and rewrites origin city/country mentions into neutral phrasing.
    Returns "" only if the string is empty after scrubbing AND
    became empty (so the caller can fall back to fixed copy)."""
    if not s:
        return ""
    text = s

    # Carrier names → wipe (case-insensitive). We walk the longest
    # tokens first so "yun express" is wiped as a phrase before "yun"
    # could partial-match anything.
    for name in sorted(_CARRIER_NAMES_WIPE, key=len, reverse=True):
        # Use a generous match: word-boundary-ish but case-insensitive
        idx = 0
        low_name = name.lower()
        while True:
            low = text.lower()
            pos = low.find(low_name, idx)
            if pos < 0:
                break
            text = text[:pos] + text[pos + len(name):]
            idx = pos
    # Origin locations → redact to neutral
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

    # Collapse double-spaces, stray ", , ", trailing "," etc that
    # appear after wipes.
    text = " ".join(text.split())
    text = text.replace(" ,", ",").replace(", ,", ",").replace(" .", ".")

    # Collapse repeated neutral-redactions: "the origin facility, the
    # origin facility" → "the origin facility". After two distinct
    # location words both redact to the same neutral phrase, the
    # sentence reads cleanly without an artefact.
    import re as _re
    text = _re.sub(
        r"\bthe origin facility(?:[\s,/|·\-:;]+the origin facility)+",
        "the origin facility",
        text,
        flags=_re.IGNORECASE,
    )
    # Strip orphan separators left after wipes (e.g. "Last Mile: |").
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


# ── Synthesised fallback (no carrier events yet) ────────────────────
# Offsets, in hours, from order.created_at to each stage's start.
# These are conservative — when there's NO real data, we'd rather
# under-show than over-claim.
_STAGE_OFFSETS_H = {
    "order_placed":     0,
    "in_production":    8,
    "shipment_created": 24,
    "origin_hub":       40,
    "in_transit":       56,
    # destination is NEVER synthesised — only set from delivered_at.
}


def _synthesised_active_stage(order) -> str:
    """Best-effort stage classification when we have no carrier events.

    Critical: we only return ``destination`` when there's HARD evidence
    (an actual delivered_at or fulfillment_status=='delivered'). For
    every "completed" / "fulfilled" / "shipped" we treat the shipment
    as still in transit, because seller-side completion does NOT mean
    the parcel reached the customer.
    """
    status = (
        (getattr(order, "fulfillment_status", "") or "")
        or (getattr(order, "payment_status", "") or "")
    ).strip().lower()

    # Failed / cancelled
    if status in ("failed", "cancelled", "canceled", "voided"):
        return "failed"

    # HARD-evidence delivered
    if getattr(order, "delivered_at", None):
        return "destination"
    if status in ("delivered",):
        return "destination"

    has_tracking = bool((getattr(order, "tracking_number", "") or "").strip())

    # Anything else with a tracking number is at-best in_transit. We
    # do NOT trust "completed"/"fulfilled" to mean delivered.
    if has_tracking:
        if status in ("shipped", "in_transit", "in transit"):
            return "in_transit"
        if status in ("on-hold", "on hold"):
            return "origin_hub"
        # Tracking number exists but status is processing/pending/
        # completed/fulfilled → treat as in_transit (cautious choice:
        # the parcel is somewhere between origin pickup and final mile).
        return "in_transit"

    if status == "processing":
        return "in_production"
    return "order_placed"


def _synthesised_stage_timestamp(order, stage_key: str, active_key: str) -> datetime | None:
    """Synthesise a timestamp for a completed stage from order
    lifecycle. Active stage / future stages → None."""
    if STAGE_ORDER.index(stage_key) > STAGE_ORDER.index(active_key):
        return None
    if stage_key == active_key:
        return None  # renders as "Now"
    if stage_key == "destination":
        return getattr(order, "delivered_at", None)
    created = getattr(order, "created_at", None)
    if not created:
        return None
    offset_h = _STAGE_OFFSETS_H.get(stage_key, 0)
    ts = created + timedelta(hours=offset_h)
    now = _utcnow()
    return ts if ts <= now else now


# ── Real-events path ────────────────────────────────────────────────
def _events_to_stage_state(order) -> dict | None:
    """Derive (active_key, per-stage timestamps, sanitised events
    list) from order.tracking_events. Returns None when the field is
    empty so the caller falls back to the synthesised path.

    Active stage rule: the LATEST event's classified stage. Stages
    earlier in STAGE_ORDER are implicitly done — even if no event
    explicitly hit them, conceptually they must have happened (a
    parcel that's "in transit" obviously had a "shipment created"
    moment we just didn't get told about).
    """
    events = getattr(order, "tracking_events", None) or []
    if not events:
        return None

    earliest_at_stage: dict[str, datetime] = {}
    latest_ts: datetime | None = None
    latest_stage: str | None = None

    # Build sanitised, sorted (oldest-first) event list for the UI.
    ui_events = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        raw = (ev.get("raw") or "").strip()
        stage = ev.get("stage") or ""
        ts = _parse_iso(ev.get("ts") or "")
        if not raw or not ts:
            continue

        clean = _sanitize_text(raw)
        if not clean:
            # If the event reduced to nothing after sanitising, fall
            # back to the stage's fixed description so the customer
            # still sees a sensible line on the timeline.
            clean = _stage_desc_for(stage) or ""

        ui_events.append({
            "ts":     ts,
            "raw":    raw,           # debug only — never rendered
            "clean":  clean,
            "stage":  stage,
        })

        if stage in STAGE_ORDER:
            if stage not in earliest_at_stage or ts < earliest_at_stage[stage]:
                earliest_at_stage[stage] = ts
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_stage = stage

    if not latest_stage:
        # We had events but couldn't classify any of them. Don't
        # fall back to synthesis (events exist, just unfamiliar
        # verbiage) — pin to in_transit with the last raw line.
        latest_stage = "in_transit"

    # Sort UI events oldest-first.
    ui_events.sort(key=lambda e: e["ts"])

    return {
        "active_key":         latest_stage,
        "earliest_at_stage":  earliest_at_stage,
        "ui_events":          ui_events,
        "last_event_ts":      latest_ts,
    }


def _stage_desc_for(key: str) -> str:
    for s in STAGE_DEFS:
        if s["key"] == key:
            return s["desc"]
    return ""


# ── Public entry point ──────────────────────────────────────────────
def build_stages(order) -> dict:
    """Build the full 6-stage timeline for an Order.

    Returns a dict ready for the template:

        {
          "stages":       [{key, label, code, desc, status, ts_iso, ts_short, ts_full}, …],
          "active_key":   "in_transit",
          "active_index": 4,
          "is_failed":    False,
          "is_delivered": False,
          "headline":     "International flight has departed",
          "headline_sub": "Scheduled long-haul route. Next update on arrival …",
          "updated_label": "Updated 2 h ago",
          "events":       [{ts_iso, ts_full, text, stage}, …]   # carrier events list
          "events_source": "carrier"  |  "synthesised",
        }
    """
    # Try real events first.
    realtime = _events_to_stage_state(order)
    is_failed = False

    if realtime:
        active_key = realtime["active_key"]
        stage_ts   = realtime["earliest_at_stage"]
        ui_events  = realtime["ui_events"]
        last_event_ts = realtime["last_event_ts"]
        events_source = "carrier"
    else:
        # Synthesised path.
        active_key = _synthesised_active_stage(order)
        if active_key == "failed":
            is_failed = True
            active_key = "shipment_created" if (order.tracking_number or "") else "in_production"
        stage_ts = {}
        ui_events = []
        last_event_ts = None
        events_source = "synthesised"

    if active_key not in STAGE_ORDER:
        active_key = "order_placed"
    active_idx = STAGE_ORDER.index(active_key)

    # ── Build per-stage rows for the stepper ──────────────────────
    out_stages = []
    for idx, sdef in enumerate(STAGE_DEFS):
        if idx < active_idx:
            status = "done"
        elif idx == active_idx:
            status = "active"
        else:
            status = "pending"

        # Pick a timestamp: real-events first, synthesised fallback ONLY
        # when no carrier feed is available. With a live carrier feed, a
        # missing earlier-stage timestamp is honest — we mark the stage
        # done (it must have happened) but show no date, rather than
        # making one up from order.created_at + offset.
        ts = stage_ts.get(sdef["key"])
        if not ts and idx < active_idx and events_source == "synthesised":
            ts = _synthesised_stage_timestamp(order, sdef["key"], active_key)

        out_stages.append({
            "key":   sdef["key"],
            "code":  sdef["code"],
            "label": sdef["label"],
            "desc":  sdef["desc"],
            "status": status,
            "ts_iso":   ts.isoformat() if ts else "",
            "ts_short": ts.strftime("%d %b") if ts else "",
            "ts_full":  ts.strftime("%d %b · %H:%M UTC") if ts else "",
        })

    # ── Headline ──────────────────────────────────────────────────
    if events_source == "carrier" and ui_events:
        latest = ui_events[-1]
        headline = latest["clean"] or _stage_desc_for(active_key)
        headline_sub = _stage_desc_for(active_key)
    else:
        fallback_map = {
            "order_placed":     ("Order received", "Your order is queued for fulfilment."),
            "in_production":    ("Order in production", "Our fulfilment team is preparing and quality-checking your items."),
            "shipment_created": ("Shipment created", "Air waybill issued. Your parcel is ready to leave the facility."),
            "origin_hub":       ("At origin hub", "Your parcel has been processed and loaded for outbound flight."),
            "in_transit":       ("In international transit", "Scheduled long-haul route. Next update on arrival at destination country."),
            "destination":      ("Delivered", "Your shipment has reached its destination."),
        }
        headline, headline_sub = fallback_map.get(active_key, ("In transit", ""))

    # ── Updated label ─────────────────────────────────────────────
    if last_event_ts:
        updated_label = _ago_label(last_event_ts)
    else:
        last_done_ts = None
        for s in out_stages:
            if s["status"] == "done" and s["ts_iso"]:
                last_done_ts = _parse_iso(s["ts_iso"]) or last_done_ts
        if last_done_ts:
            updated_label = _ago_label(last_done_ts)
        else:
            updated_label = "Live tracking"

    # ── Sanitised, render-ready carrier events list ───────────────
    events_payload = [
        {
            "ts_iso":  ev["ts"].isoformat(),
            "ts_full": ev["ts"].strftime("%d %b · %H:%M UTC"),
            "ts_short": ev["ts"].strftime("%d %b"),
            "text":    ev["clean"],
            "stage":   ev["stage"],
            "stage_label": next((s["label"] for s in STAGE_DEFS if s["key"] == ev["stage"]), ""),
        }
        for ev in ui_events
    ]

    return {
        "stages":         out_stages,
        "active_key":     active_key,
        "active_index":   active_idx,
        "is_failed":      is_failed,
        "is_delivered":   active_key == "destination",
        "headline":       headline,
        "headline_sub":   headline_sub,
        "updated_label":  updated_label,
        "events":         events_payload,
        "events_source":  events_source,
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
    for ch in ("­", "​", "‌", "‍", "﻿"):
        s = s.replace(ch, "")
    return s.upper()
