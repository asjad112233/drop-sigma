"""Real-carrier tracking event puller — feeds track.dropsigma.com.

The job of this module is narrow and important:

    Given a Drop Sigma Order with a tracking number, fetch the actual
    carrier's event list, classify each event onto one of the six
    Drop Sigma stages, and persist it. The public tracking page then
    renders the customer-facing timeline from this data — so the
    customer sees real progress, not a synthesised guess.

The classifier (``classify_event_to_stage``) is the single source of
truth for "this carrier sentence means stage X". It's keyword-based,
intentionally readable, and lives at the top of the file so new
carrier verbiage is easy to add.

The puller is **carrier-pluggable**. Right now Yuntrack is implemented
(it covers the majority of Drop Sigma's cross-border shipments).
Others can be added by registering in ``_CARRIER_REGISTRY``. If no
puller matches, ``refresh_order_tracking_events`` returns False and
the order keeps falling back to the synthesised progression — never a
broken page.

Importantly:
  - No carrier name, no origin city, no hub code touches the storage
    in a CUSTOMER-VISIBLE shape. The raw text we save IS the raw
    feed, but the public renderer pipes it through the sanitiser
    in ``tracking_public.stage_mapper`` before display.
  - We never block the request thread on this. All polling happens
    from the webhook sentinel loop (server-side, off-request).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable

import requests

logger = logging.getLogger(__name__)


# ── Classifier ──────────────────────────────────────────────────────
# Order matters: we test from most-specific (destination) backwards
# so that "shipment received at destination customs" buckets as
# in_transit before "received" can bucket it as shipment_created.
_STAGE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("destination", (
        # Hard delivered claims
        "delivered to recipient", "package delivered", "successfully delivered",
        "signed by", "signed for", "left with",
        "out for delivery", "loaded for delivery", "with delivery driver",
        "delivery completed",
        # Note: bare "delivered" is checked separately below — too easy
        # to false-match on phrases like "to be delivered" or
        # "shipment will be delivered".
    )),
    ("in_transit", (
        "arrived at destination country", "destination customs",
        "destination facility", "destination sortation",
        "customs clearance", "import customs", "released by customs",
        "in transit", "shipment is in transit", "in-transit",
        "international flight", "flight has departed", "flight departed",
        "long haul", "long-haul",
        "arrived in country", "arrived in the destination",
        "transferred to local carrier", "handed over to local carrier",
        "last mile carrier", "last-mile",
    )),
    ("origin_hub", (
        "departed from origin", "departed origin",
        "departed from sort facility", "departed sort facility",
        "loaded onto flight", "loaded on flight",
        "arrived at origin", "exported", "export customs",
        "outbound", "left origin facility",
        "departed from facility",
        "international flight has departed",  # echoes Yuntrack's exact phrasing
    )),
    ("shipment_created", (
        "shipment created", "label created", "waybill",
        "accepted by carrier", "carrier accepted",
        "received at facility", "picked up", "collected",
        "arrived at facility", "arrived at sort facility",
        "arrived at origin facility", "sort facility",
        "handed over to carrier", "manifest",
        "scanned in", "first scan",
    )),
    ("in_production", (
        "order accepted", "ready to ship",
        "ready for collection", "production", "warehouse",
        "fulfilment", "fulfillment", "packing",
        "preparing for shipment", "being prepared",
    )),
    ("order_placed", (
        "order placed", "order received", "order created",
        "payment confirmed", "payment received",
    )),
]

# Hard-evidence delivered phrase. Distinct list because bare "delivered"
# is so common that we want a focused check (with whole-word + leading
# preposition guards).
_DELIVERED_PATTERNS = (
    re.compile(r"\bdelivered\s*[\.\!]?\s*$", re.I),
    re.compile(r"\bdelivered\s+(to|at|on)\b", re.I),
    re.compile(r"\bpackage\s+(was|has\s+been)\s+delivered\b", re.I),
)


def classify_event_to_stage(raw_text: str) -> str:
    """Map a carrier event sentence onto one of the six Drop Sigma
    stage keys. Returns "" if no confident match — the renderer will
    still show the event but won't advance the stepper.
    """
    if not raw_text:
        return ""
    low = raw_text.lower().strip()

    # Hard-evidence delivered (most specific)
    for pat in _DELIVERED_PATTERNS:
        if pat.search(raw_text):
            return "destination"

    for stage, keywords in _STAGE_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return stage
    return ""


# ── Carrier pullers ─────────────────────────────────────────────────
# Each puller takes a tracking number and returns a list of:
#   {"ts": "<ISO8601 UTC>", "raw": "<carrier sentence>"}
# … sorted oldest-first. Failures return [] so the sentinel keeps
# trying without raising.


_YUNTRACK_API = "https://services.yuntrack.com/Track/Query"
_YUNTRACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.yuntrack.com",
    "Referer": "https://www.yuntrack.com/",
}


def _to_iso_utc(ts_str: str) -> str:
    """Best-effort parse of a carrier timestamp into ISO 8601 UTC."""
    if not ts_str:
        return ""
    try:
        # ISO with timezone
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    # Common Yuntrack-ish format: "2026-06-10 14:05:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            continue
    return ""


def fetch_yuntrack_events(tracking_number: str, timeout: int = 12) -> list[dict]:
    """Pull events from Yuntrack's public tracking JSON endpoint.

    The endpoint is the one the yuntrack.com tracking page itself uses,
    and it accepts CORS from the browser, so it's effectively public.
    We're polite: single tracking number per call, sensible UA + Origin
    so Cloudflare doesn't flag us.

    Returns oldest-first list of {ts, raw}. Empty on any failure —
    callers must NOT raise on empty.
    """
    if not tracking_number:
        return []
    try:
        resp = requests.post(
            _YUNTRACK_API,
            json={"NumberList": [tracking_number]},
            headers=_YUNTRACK_HEADERS,
            timeout=timeout,
        )
    except Exception as e:
        logger.info("yuntrack fetch network error for %s: %s", tracking_number, e)
        return []

    if resp.status_code != 200:
        logger.info("yuntrack fetch HTTP %s for %s", resp.status_code, tracking_number)
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    # Yuntrack returns slightly different shapes across endpoints; we
    # probe a few well-known keys defensively rather than depending on
    # any single one. Worst case: zero events.
    results = (
        data.get("ResultList")
        or data.get("ResultListList")
        or data.get("Data")
        or []
    )
    if not isinstance(results, list):
        return []

    events: list[dict] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        details = (
            result.get("TrackingDetailList")
            or result.get("TrackEventDetails")
            or result.get("EventList")
            or []
        )
        if not isinstance(details, list):
            continue
        for d in details:
            if not isinstance(d, dict):
                continue
            ts_raw = (
                d.get("CreatedDate")
                or d.get("ProcessDate")
                or d.get("EventDate")
                or d.get("Date")
                or ""
            )
            text_raw = (
                d.get("TrackingDescription")
                or d.get("EventDesc")
                or d.get("Description")
                or d.get("Status")
                or ""
            )
            text = (text_raw or "").strip()
            if not text:
                continue
            iso = _to_iso_utc(str(ts_raw).strip())
            events.append({"ts": iso, "raw": text})

    # Oldest-first
    def _sort_key(e):
        return e.get("ts") or ""
    events.sort(key=_sort_key)
    return events


def _detect_carrier(tracking_number: str, tracking_url: str = "") -> str:
    """Identify which carrier puller to use. Conservative — when
    unknown, we return '' and the order keeps the synthesised
    fallback rather than guessing wrong."""
    tn = (tracking_number or "").strip().upper()
    url = (tracking_url or "").lower()
    if not tn:
        return ""
    # Yuntrack: starts with YT + digits, or the URL hosts yuntrack.com.
    if tn.startswith("YT") and tn[2:].isdigit():
        return "yuntrack"
    if "yuntrack.com" in url or "yunexpress" in url:
        return "yuntrack"
    return ""


_CARRIER_REGISTRY: dict[str, Callable[[str], list[dict]]] = {
    "yuntrack": fetch_yuntrack_events,
}


# ── Persistence wrapper ─────────────────────────────────────────────
def refresh_order_tracking_events(order) -> bool:
    """Pull the latest carrier events for an Order and persist.

    Returns True if at least one event was stored. The sentinel uses
    that to decide whether to back off (no carrier match available)
    or keep polling on the regular cadence.
    """
    tn = (getattr(order, "tracking_number", "") or "").strip()
    if not tn:
        return False

    carrier = _detect_carrier(tn, getattr(order, "tracking_url", "") or "")
    if not carrier:
        return False

    puller = _CARRIER_REGISTRY.get(carrier)
    if not puller:
        return False

    raw_events = puller(tn)
    if not raw_events:
        return False

    classified = []
    for ev in raw_events:
        raw = (ev.get("raw") or "").strip()
        if not raw:
            continue
        classified.append({
            "ts":    ev.get("ts") or "",
            "raw":   raw,
            "stage": classify_event_to_stage(raw),
        })

    if not classified:
        return False

    # Detect delivery from the LATEST event (not just any event in
    # the feed — a journey can briefly mention "out for delivery" and
    # then show a delivery exception that requires re-attempt; the
    # truth is whatever the carrier currently reports).
    latest = classified[-1] if classified else None
    is_actually_delivered = bool(latest and latest.get("stage") == "destination")

    # The carrier-confirmed delivery timestamp comes from the LATEST
    # destination-class event (oldest-first list → walk back).
    delivered_at = None
    if is_actually_delivered:
        for ev in reversed(classified):
            if ev["stage"] != "destination":
                continue
            iso = ev.get("ts")
            if iso:
                try:
                    delivered_at = datetime.fromisoformat(iso)
                    break
                except Exception:
                    pass

    order.tracking_events = classified
    order.tracking_events_updated_at = datetime.now(tz=timezone.utc)
    # Reset attempt counter on a successful pull so we go back to the
    # tight cadence.
    order.tracking_events_attempts = 0
    update_fields = [
        "tracking_events", "tracking_events_updated_at",
        "tracking_events_attempts",
    ]

    if delivered_at and not getattr(order, "delivered_at", None):
        order.delivered_at = delivered_at
        update_fields.append("delivered_at")
    elif (not is_actually_delivered) and getattr(order, "delivered_at", None):
        # Auto-heal: the order has a stale delivered_at (probably from
        # the old over-eager "deliver" substring check in
        # fetch_live_tracking_api), but the live carrier feed says the
        # parcel is NOT delivered. Trust the carrier.
        order.delivered_at = None
        update_fields.append("delivered_at")
        logger.info(
            "carrier-tracking: cleared stale delivered_at on order %s — "
            "live carrier feed reports stage=%s, not destination",
            order.id, latest.get("stage") if latest else "unknown",
        )

    order.save(update_fields=update_fields)
    return True
