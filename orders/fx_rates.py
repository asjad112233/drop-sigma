"""USD-base FX rate fetcher + cache for the Sourcing Ops pricing display.

When a tenant's merchant store sells in a non-USD currency (AUD, EUR,
GBP, …) we can't compute the ops-side margin directly against the USD
Drop-Sigma quote — they're in different units. This module fetches
today's mid-market exchange rates from Frankfurter
(https://frankfurter.dev — ECB sourced, no API key, free) and caches
them for 12 hours so every queue / page load shares one upstream
call per process per half-day.

The margin the frontend computes from this is always presented to
ops staff as an ESTIMATE — the actual margin the tenant realises
depends on the PSP / Stripe / bank conversion at settlement, which
can shift by ±2-3% from mid-market. The dashboard surfaces that
caveat through an ⓘ tooltip next to the figure.

Network failure / unknown currency → ``convert_to_usd`` returns
``None`` and the renderer falls back to the previous "—" display.
This module never raises into the request path.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────
_FX_CACHE_TTL_SECONDS = 12 * 60 * 60          # 12 h — ECB updates once a business day
_FX_FAIL_BACKOFF_SECONDS = 5 * 60             # On fetch failure, wait 5 min before retrying
_FX_TIMEOUT_SECONDS = 8
_FX_ENDPOINT = "https://api.frankfurter.dev/v1/latest"
_FX_USER_AGENT = "DropSigma-OPS/1.0 (+https://dropsigma.com)"


# ── In-process cache ────────────────────────────────────────────────
# Survives across requests within the same gunicorn worker; new
# workers refetch on first need. A slightly-stale FX rate is fine for
# margin estimation (the figure is always labelled estimated anyway),
# so going via Django's cache framework or Redis would be overkill.
_lock = threading.RLock()
_cache = {
    "rates":          {},   # currency_code (upper) → float (units per 1 USD)
    "date":           "",   # ECB rate date as ISO YYYY-MM-DD
    "fetched_at":     0.0,  # unix ts of last successful fetch
    "last_attempt":   0.0,  # unix ts of last fetch attempt (success or fail)
}


def _refresh_locked() -> bool:
    """Fetch + populate the in-process cache. Returns True on success.
    Caller must hold ``_lock``."""
    _cache["last_attempt"] = time.time()
    try:
        r = requests.get(
            _FX_ENDPOINT,
            params={"base": "USD"},
            timeout=_FX_TIMEOUT_SECONDS,
            headers={"User-Agent": _FX_USER_AGENT, "Accept": "application/json"},
        )
    except Exception as e:
        logger.info("fx_rates: fetch network error: %s", e)
        return False

    if r.status_code != 200:
        logger.info("fx_rates: HTTP %s from frankfurter", r.status_code)
        return False

    try:
        data = r.json()
    except Exception as e:
        logger.info("fx_rates: JSON parse error: %s", e)
        return False

    rates = data.get("rates") or {}
    if not isinstance(rates, dict) or not rates:
        logger.info("fx_rates: empty/invalid rates payload")
        return False

    # Frankfurter with base=USD returns `rates[X]` = how many X = 1 USD.
    # Normalise keys to upper case + always pin USD = 1.0 ourselves so
    # convert_to_usd doesn't have to special-case it later.
    clean = {}
    for k, v in rates.items():
        try:
            clean[str(k).upper()] = float(v)
        except Exception:
            continue
    if not clean:
        return False

    clean["USD"] = 1.0
    _cache["rates"] = clean
    _cache["date"] = str(data.get("date") or "")
    _cache["fetched_at"] = time.time()
    logger.info(
        "fx_rates: refreshed (date=%s, %d currencies)",
        _cache["date"], len(clean),
    )
    return True


def _maybe_refresh() -> None:
    """Refresh the cache if it's stale and we're not currently on the
    failure-backoff cooldown."""
    now = time.time()
    with _lock:
        age = now - _cache["fetched_at"]
        if _cache["rates"] and age < _FX_CACHE_TTL_SECONDS:
            return  # fresh-enough; nothing to do
        # Cache is stale (or empty). Honour the failure backoff so a
        # broken upstream doesn't make us hammer Frankfurter every page
        # load — wait at least _FX_FAIL_BACKOFF_SECONDS between
        # attempts when the last attempt didn't succeed.
        last_attempt_age = now - _cache["last_attempt"]
        if (not _cache["rates"]) or last_attempt_age >= _FX_FAIL_BACKOFF_SECONDS:
            _refresh_locked()


# ── Public API ──────────────────────────────────────────────────────
def get_rates_usd_base() -> dict:
    """Return ``{currency_code: float}`` where each value is how many of
    that currency equal 1 USD. USD itself is always present as 1.0.
    Returns an empty dict only if every fetch attempt so far has
    failed AND we're still in the failure-backoff window — callers
    should fall back gracefully (the public-tracking + OPS pricing
    code treats empty rates as "estimate unavailable").
    """
    _maybe_refresh()
    with _lock:
        return dict(_cache["rates"])


def convert_to_usd(amount, from_currency: str):
    """Convert ``amount`` (numeric, in ``from_currency``) to USD using
    the most recent cached mid-market rate. Returns a float or
    ``None`` when no rate is available.

    USD → USD passes the amount through unchanged (returns a float
    even if the input was a ``Decimal`` / int / str-numeric).
    """
    if amount is None:
        return None
    try:
        amt = float(amount)
    except Exception:
        return None
    ccy = (from_currency or "").upper().strip()
    if ccy in ("", "USD"):
        return amt
    rates = get_rates_usd_base()
    rate = rates.get(ccy)
    if not rate or rate <= 0:
        return None
    return amt / rate


def get_rate_for(currency: str):
    """Return ``rates[currency]`` (units per 1 USD) or None when the
    currency is unsupported or the cache is empty."""
    ccy = (currency or "").upper().strip()
    if not ccy:
        return None
    rates = get_rates_usd_base()
    rate = rates.get(ccy)
    return float(rate) if rate else None


def get_rate_date() -> str:
    """ISO date string Frankfurter most recently reported for the cached
    rates (e.g. "2026-06-12") or "" when the cache is empty.
    Surfaced in the UI tooltip so the ops reader knows how recent the
    estimate is."""
    _maybe_refresh()
    with _lock:
        return _cache.get("date") or ""
