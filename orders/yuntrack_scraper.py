"""Playwright-based Yuntrack tracking page scraper.

Aliyun's WAF blocks direct POSTs to services.yuntrack.com/Track/Query
(returns HTTP 405 "potential threat") so the JSON-API path in
``carrier_tracking.fetch_yuntrack_events`` always comes back empty.
This module is the workaround: render the public tracking page in
headless Chromium and harvest the data three ways, in order of
reliability:

  1. **XHR interception** — listen for response bodies that look like
     Yuntrack's own tracking JSON (the browser request goes through
     because Aliyun trusts the real Origin/Referer/Cookies). When we
     get one, we parse it the same way the legacy JSON path did.
  2. **In-browser fetch** — if no XHR fires (e.g. the page caches),
     run an in-page ``fetch()`` from the same context. Cookies +
     Origin are real so Aliyun lets it through.
  3. **DOM scrape** — last-resort: pull visible event sentences
     directly from the rendered timeline.

The return contract matches what ``refresh_order_tracking_events``
expects, plus an ``active_stage`` for the YunExpress 5-stage stepper:

    {
      "ok": bool,
      "active_stage": "pickup" | "departed_origin" |
                      "arrived_destination" | "local_carrier" |
                      "delivered" | "",
      "events": [{"ts": ISO 8601 UTC, "raw": "…"}, …],  # oldest-first
    }

Empty result on any failure — never raises. Callers degrade
gracefully to the synthesised stage progression.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from .tracking_scraper import _find_chromium

logger = logging.getLogger(__name__)


# YunExpress's visible stepper labels → our internal stage keys.
# Compared case-insensitively.
YUNEXPRESS_STAGES = [
    ("pickup",                   "pickup",              "Pickup"),
    ("departed from origin",     "departed_origin",     "Departed from origin"),
    ("arrived at destination",   "arrived_destination", "Arrived at destination"),
    ("local carrier on the way", "local_carrier",       "Local carrier on the way"),
    ("delivered successfully",   "delivered",           "Delivered"),
    # synonyms that may show up in DOM
    ("delivered",                "delivered",           "Delivered"),
]
_YUNEXPRESS_LABEL_TO_KEY = {label: key for label, key, _ in YUNEXPRESS_STAGES}


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _to_iso_utc(value) -> str:
    """Robust timestamp parser. Handles:
      - ISO ("2026-06-10T14:05:00Z", "2026-06-10 14:05:00")
      - Yuntrack/YunExpress display: "Wednesday June,10,2026 14:05:00"
      - Date-only: "June 10, 2026"
    Returns ISO 8601 UTC string or '' if unparseable.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    # 1. ISO with optional Z
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass

    # 2. Space-separated ISO
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(
                tzinfo=timezone.utc
            ).isoformat()
        except Exception:
            continue

    # 3. YunExpress display format: "[Day] Month DD, YYYY HH:MM[:SS]"
    m = re.search(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|"
        r"Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
        r"[\s,]*([0-3]?\d)[\s,]*(\d{4})"
        r"(?:[\s,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        text, re.IGNORECASE,
    )
    if m:
        try:
            month_name, dd, yyyy, hh, mm, ss = m.groups()
            month = _MONTHS.get(month_name.lower())
            if not month:
                return ""
            dt = datetime(
                int(yyyy), month, int(dd),
                int(hh or 0), int(mm or 0), int(ss or 0),
                tzinfo=timezone.utc,
            )
            return dt.isoformat()
        except Exception:
            pass

    return ""


# ── Yuntrack JSON shape parsers ──────────────────────────────────────
def _walk_for_event_lists(node, hits, depth=0):
    """Recursively walk a JSON document looking for arrays of event-shaped
    dicts (those containing keys like TrackingDescription / EventDesc /
    Description plus a date-ish key)."""
    if depth > 12:
        return
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            keys = set(node[0].keys())
            desc_keys = {"TrackingDescription", "EventDesc", "Description",
                         "Status", "Detail", "ProcessContent"}
            date_keys = {"CreatedDate", "ProcessDate", "EventDate", "Date",
                         "Time", "OccurDate", "OccurTime"}
            if (keys & desc_keys) and (keys & date_keys):
                hits.append(node)
                return
        for x in node:
            _walk_for_event_lists(x, hits, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_for_event_lists(v, hits, depth + 1)


def _extract_events_from_json(data) -> list[dict]:
    """Parse Yuntrack-shaped JSON (whatever the exact path) into our
    {ts, raw} contract."""
    hits = []
    _walk_for_event_lists(data, hits)
    events: list[dict] = []
    for evlist in hits:
        for d in evlist:
            if not isinstance(d, dict):
                continue
            text = (
                d.get("TrackingDescription")
                or d.get("EventDesc")
                or d.get("Description")
                or d.get("Status")
                or d.get("Detail")
                or d.get("ProcessContent")
                or ""
            ).strip()
            if not text:
                continue
            ts = (
                d.get("CreatedDate") or d.get("ProcessDate")
                or d.get("EventDate") or d.get("Date")
                or d.get("OccurDate") or d.get("OccurTime")
                or d.get("Time") or ""
            )
            events.append({"ts": _to_iso_utc(ts), "raw": text})
    # Dedupe (some endpoints repeat events across nested arrays)
    seen = set()
    deduped = []
    for e in events:
        k = (e["ts"], e["raw"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(e)
    return deduped


# ── YunExpress 5-stage classifier ────────────────────────────────────
# Maps a single carrier event sentence onto one of the YunExpress
# stepper keys (pickup → departed_origin → arrived_destination →
# local_carrier → delivered). The classifier is *intentionally* coarser
# than the 6-stage one in carrier_tracking — it matches YunExpress's
# own customer-facing flow.
_YUNEXPRESS_KEYWORDS = [
    # Most specific first.
    ("delivered", (
        "delivered to recipient", "successfully delivered",
        "package delivered", "signed by", "signed for",
        "delivery completed",
    )),
    ("local_carrier", (
        "out for delivery", "loaded for delivery",
        "with delivery driver", "on vehicle for delivery",
        "transferred to local carrier", "handed over to local carrier",
        "last mile carrier", "last-mile",
        "transferred to last-mile", "transferred to last mile",
        "arrived at delivery facility", "loaded for last-mile",
    )),
    ("arrived_destination", (
        "arrived at destination country", "arrived at the destination",
        "destination customs", "destination facility",
        "destination sortation", "customs clearance",
        "import customs", "released by customs",
        "cleared destination", "arrived in country",
    )),
    ("departed_origin", (
        "departed from origin", "departed origin",
        "international flight has departed", "international flight departed",
        "flight has departed", "loaded onto flight",
        "departed from sort facility", "departed sort facility",
        "departed from facility",
        "shipment is in transit",
        "country of origin commences customs",
        "arrived at the origin international airport",
        "arrived at origin facility", "arrived at origin",
        "exported", "outbound",
    )),
    ("pickup", (
        "shipment information received", "information received",
        "shipment created", "label created", "waybill",
        "accepted by carrier", "carrier accepted",
        "received at facility", "picked up", "collected",
        "order accepted", "order received",
    )),
]


def classify_yunexpress_stage(raw_text: str) -> str:
    """Map a single carrier event sentence onto a YunExpress stage key.
    Returns '' on no match."""
    if not raw_text:
        return ""
    low = raw_text.lower().strip()
    for stage_key, keywords in _YUNEXPRESS_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return stage_key
    return ""


# Stage ordering used to decide "rightmost / latest reached" stage.
_YUNEXPRESS_STAGE_ORDER = [
    "pickup", "departed_origin", "arrived_destination",
    "local_carrier", "delivered",
]


def _active_stage_from_events(events: list[dict]) -> str:
    """Derive YunExpress active stage from the LATEST event's
    classification. Falls back to the highest-reached stage if the
    latest event is unrecognised (e.g. a stage transitioned past the
    classifier's vocabulary)."""
    if not events:
        return ""
    # Latest-first walk to find a classified event.
    for ev in reversed(events):
        stage = classify_yunexpress_stage(ev.get("raw") or "")
        if stage:
            # Also scan all events; the highest-reached stage wins
            # (a "delivered" event one rung down doesn't undo it).
            highest_idx = _YUNEXPRESS_STAGE_ORDER.index(stage)
            for ev2 in events:
                s2 = classify_yunexpress_stage(ev2.get("raw") or "")
                if s2 and _YUNEXPRESS_STAGE_ORDER.index(s2) > highest_idx:
                    highest_idx = _YUNEXPRESS_STAGE_ORDER.index(s2)
            return _YUNEXPRESS_STAGE_ORDER[highest_idx]
    return ""


def _clean_event_text(raw: str) -> str:
    """Strip YunExpress's "----LOCATION" suffix from event lines.
    The location goes through the public-facing sanitiser before
    rendering anyway, but cleaning at source makes logs + storage
    much easier to grep."""
    if not raw:
        return ""
    # YunExpress separates description from location with 4+ dashes.
    parts = re.split(r"-{3,}", raw, maxsplit=1)
    return (parts[0] if parts else raw).strip(" ,;:-")


# ── DOM-scrape JS ────────────────────────────────────────────────────
# Defensive: dumps the visible text + element snapshot so server-side
# parsing can be debugged without re-launching the browser.
_DOM_SCRAPE_JS = r"""
() => {
    const out = { active: '', body: '', steps: [], events: [] };
    out.body = (document.body && document.body.innerText) || '';

    // Active stage detection — walk the stepper looking for an
    // element whose class hints "active/current/finished" AND whose
    // text matches one of the YunExpress labels.
    const stageLabels = [
        'pickup','departed from origin','arrived at destination',
        'local carrier on the way','delivered successfully','delivered'
    ];
    const all = document.querySelectorAll('div,span,li,td,p');
    for (const el of all) {
        const t = (el.innerText || '').trim().toLowerCase();
        if (!t || t.length > 60) continue;
        if (!stageLabels.includes(t)) continue;
        out.steps.push(t);
        // Walk ancestors looking for "active"/"current"/"finished" class
        let node = el;
        for (let i = 0; i < 6 && node; i++) {
            const cls = (typeof node.className === 'string' ? node.className : '') + '';
            if (/(?:^|[\s_-])(active|current|on|finished|done)(?:$|[\s_-])/i.test(cls)) {
                out.active = t;
                break;
            }
            node = node.parentElement;
        }
    }

    return out;
}
"""


def scrape_yuntrack(tracking_number: str, timeout_ms: int = 35000) -> dict:
    """Scrape the YunExpress public tracking page. See module docstring
    for the return contract."""
    out = {"ok": False, "active_stage": "", "events": []}
    if not tracking_number:
        return out

    url = f"https://www.yuntrack.com/parcelTracking?id={tracking_number}"
    captured_jsons: list[dict] = []

    try:
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            chromium_path = _find_chromium()
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                    "Safari/537.36"
                ),
                viewport={"width": 1366, "height": 1000},
                locale="en-US",
            )
            page = ctx.new_page()

            # ── XHR interception ──────────────────────────────────
            def _on_response(response):
                try:
                    rurl = response.url
                    if not any(k in rurl for k in (
                        "Track/Query", "trackInfo", "trackInfoBs",
                        "parcelTracking", "shipment", "GetTrack",
                    )):
                        return
                    ct = (response.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    body = response.text()
                    if not body or len(body) > 200_000:
                        return
                    try:
                        data = json.loads(body)
                    except Exception:
                        return
                    captured_jsons.append({"url": rurl, "data": data})
                except Exception:
                    pass

            page.on("response", _on_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # Let Vue mount + XHR fire + render
                page.wait_for_timeout(6000)
                # Wait for the tracking number to be present in DOM —
                # confirms the page actually loaded data.
                try:
                    page.wait_for_selector(
                        f"text={tracking_number}", timeout=10000,
                    )
                except PlaywrightTimeout:
                    pass
                # Give XHRs a final beat to finish.
                page.wait_for_timeout(2000)

                # ── In-browser fetch as second source ──────────────
                fetch_attempts = [
                    {
                        "url": "https://services.yuntrack.com/Track/Query",
                        "body": {"NumberList": [tracking_number]},
                    },
                    {
                        "url": "https://services.yuntrack.com/Track/Query",
                        "body": {
                            "NumberList": [tracking_number],
                            "Year": 0,
                            "CaptchaVerification": "",
                        },
                    },
                ]
                for attempt in fetch_attempts:
                    try:
                        result = page.evaluate(
                            """
                            async ({url, body}) => {
                                try {
                                    const r = await fetch(url, {
                                        method: 'POST',
                                        headers: {
                                            'Content-Type': 'application/json',
                                            'Accept': 'application/json',
                                        },
                                        body: JSON.stringify(body),
                                        credentials: 'include',
                                    });
                                    const text = await r.text();
                                    return { status: r.status, body: text };
                                } catch (e) {
                                    return { status: 0, body: String(e) };
                                }
                            }
                            """,
                            attempt,
                        )
                        if (
                            result and result.get("status") == 200
                            and result.get("body")
                        ):
                            try:
                                captured_jsons.append({
                                    "url": attempt["url"],
                                    "data": json.loads(result["body"]),
                                })
                            except Exception:
                                pass
                    except Exception:
                        pass

                # ── DOM scrape as third source ─────────────────────
                try:
                    dom_payload = page.evaluate(_DOM_SCRAPE_JS)
                except Exception:
                    dom_payload = {}
            finally:
                browser.close()
    except Exception as e:
        logger.info("yuntrack scrape exception for %s: %s", tracking_number, e)
        return out

    # ── Merge JSON sources ────────────────────────────────────────
    json_events: list[dict] = []
    for cj in captured_jsons:
        json_events.extend(_extract_events_from_json(cj.get("data")))
    # Clean text + dedupe across sources
    seen = set()
    merged = []
    for e in json_events:
        cleaned = _clean_event_text(e["raw"])
        if not cleaned:
            continue
        k = (e["ts"], cleaned)
        if k in seen:
            continue
        seen.add(k)
        merged.append({"ts": e["ts"], "raw": cleaned})

    # ── Fall back to DOM scrape only if JSON gave us nothing ──────
    if not merged and isinstance(dom_payload, dict):
        body_text = dom_payload.get("body") or ""
        # Pull lines that look like real carrier events; pair each with
        # the closest preceding date/time line.
        lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]
        date_re = re.compile(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|"
            r"Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\W*\d{1,2}\W*\d{4}",
            re.IGNORECASE,
        )
        time_re = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s+Local time)?$",
                             re.IGNORECASE)
        event_re = re.compile(
            r"^(Departed|Arrived|Shipment is|International flight|"
            r"The country of|Customs|Order|Picked up|Out for delivery|"
            r"Delivered|Loaded onto|Sorted|Received|Collected|"
            r"Released|Transferred)",
            re.IGNORECASE,
        )
        current_date = ""
        current_time = ""
        for ln in lines:
            if date_re.search(ln):
                current_date = ln
                continue
            if time_re.match(ln):
                current_time = ln
                continue
            if event_re.match(ln):
                ts = _to_iso_utc((current_date + " " + current_time).strip())
                cleaned = _clean_event_text(ln)
                if cleaned:
                    merged.append({"ts": ts, "raw": cleaned})
        # Dedupe
        seen.clear()
        deduped = []
        for e in merged:
            k = (e["ts"], e["raw"])
            if k in seen:
                continue
            seen.add(k)
            deduped.append(e)
        merged = deduped

    # ── Sort oldest-first BEFORE active-stage derivation ──────────
    def _key(e):
        return e["ts"] or "9999-12-31"
    merged.sort(key=_key)

    # ── Active stage: derive from LATEST event's classification ───
    # The DOM stepper isn't reliable (YunExpress renders all 5 stage
    # names regardless of progress, with subtle highlight classes that
    # vary across page revisions). Latest-event-based classification
    # is the carrier-feed truth.
    active_stage = _active_stage_from_events(merged)
    if not active_stage and isinstance(dom_payload, dict):
        # Last resort: DOM hint.
        active_label = (dom_payload.get("active") or "").strip().lower()
        active_stage = _YUNEXPRESS_LABEL_TO_KEY.get(active_label, "")

    out["ok"] = bool(merged) or bool(active_stage)
    out["events"] = merged
    out["active_stage"] = active_stage
    return out
