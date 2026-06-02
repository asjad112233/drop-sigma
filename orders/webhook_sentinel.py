"""
Drop Sigma — Webhook Sentinel.

Lifetime guarantee that real-time order webhooks are always wired up
correctly for every connected store. The whole point: a tenant should
NEVER have to click "Refresh" on the Orders page to see a new order.
Refresh is a backup option, not the primary path.

────────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS
────────────────────────────────────────────────────────────────────────────
Webhook registration silently rots over time because:

  - Drop Sigma's public URL changes (Railway redeploy with new domain,
    tunnel rotation, custom domain swap).
  - WooCommerce auto-disables a webhook after 5 consecutive delivery
    failures (e.g. during a deploy outage).
  - Cloudflare Bot Fight Mode 406s the registration POST.
  - Store admin accidentally regenerates the API key without the
    `read_write` scope and the existing webhook stops authenticating.
  - Store admin manually deletes the webhook in their WC admin.
  - First-time registration silently failed and the tenant never noticed.

Any of those leaves the tenant with a "connected" store that quietly
drops new orders on the floor until they hit Refresh.

────────────────────────────────────────────────────────────────────────────
HOW IT WORKS (defense in depth — 4 layers)
────────────────────────────────────────────────────────────────────────────

Layer 1 — On-demand `ensure_webhook(store, request=None)`:
  Cheap, cached idempotent verify+heal. Called from:
    - /orders/api/poll/    (frontend every 10s while tab is open)
    - /orders/sync/...     (manual Refresh)
    - Store connect flows  (manual + OAuth + bulk add)
    - Diagnose             (with ?heal=1)
  Cache hits inside a 5-minute window are a no-op (~zero cost). Only the
  first call per store per 5 min actually probes WC/Shopify.

Layer 2 — Delivery tracker `record_delivery(store_id)`:
  Called from every successful webhook receive. Caches a "last delivered
  at" timestamp per store. Lets us prove real-time sync is actually
  working (not just "registered but never firing").

Layer 3 — Background sweeper:
  Daemon thread auto-started on Django boot. Every 5 minutes, walks
  every active Store and runs ensure_webhook with a synthetic request
  hint so even tenants who never open the dashboard keep their
  webhooks fresh.

Layer 4 — Production logging:
  Every heal action logs at INFO; every failed heal logs at WARNING
  with the exact per-topic error. Railway logs become the source of
  truth for "is real-time sync healthy across the fleet?"

────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone as _tz
from typing import Optional

log = logging.getLogger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────
# Verification cache: after a successful verify+heal, skip re-probing this
# store for this long (keeps the per-10s poll endpoint cheap). 5 min matches
# the prompt-cache TTL and Cloudflare's __cf_bm cookie lifetime.
_VERIFY_TTL_SECONDS = 300

# Background sweeper: how often the daemon thread walks every store.
_SWEEPER_INTERVAL_SECONDS = 300  # 5 min

# Safety: don't fire more than this many concurrent verifications. WC stores
# behind shared hosting throttle aggressively if we hit /wp-json with bursts.
_MAX_CONCURRENT_HEALS = 4

# Set in test envs to skip the daemon (avoid leaking threads across tests).
_SENTINEL_DISABLED = (
    os.getenv("DROP_SIGMA_WEBHOOK_SENTINEL_DISABLED", "").lower() in ("1", "true", "yes")
    or os.getenv("PYTEST_CURRENT_TEST")
)


# ─── In-process state ──────────────────────────────────────────────────────
# All access guarded by _STATE_LOCK so concurrent gunicorn workers' threads
# don't trample. (Note: in multi-worker setups each worker has its own
# cache — that's fine, worst case is N workers each verify once per 5 min
# instead of 1.)
_STATE_LOCK = threading.RLock()
_last_verified: dict[int, float] = {}   # store_id -> unix ts of last successful verify
_last_status:   dict[int, dict] = {}    # store_id -> last ensure_webhook result
_last_delivery: dict[int, float] = {}   # store_id -> unix ts of last webhook receive

# Sweeper lifecycle
_sweeper_thread: Optional[threading.Thread] = None
_sweeper_started = False
_heal_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_HEALS)


# ─── Public API ────────────────────────────────────────────────────────────

def ensure_webhook(store, request=None, *, force: bool = False) -> dict:
    """Verify + auto-heal real-time webhooks for `store`. Cheap on the hot path.

    If we successfully verified this store within the last 5 min and the
    cached status says ok, return the cached result without hitting the
    network. Pass `force=True` to bypass the cache (e.g. user clicked
    "Re-register Webhook" explicitly).

    Returns the same dict shape that _register_webhook_for_store returns:
        {ok, delivery_url, registered, errors, message}
    """
    store_id = store.id

    if not force:
        with _STATE_LOCK:
            cached_at = _last_verified.get(store_id, 0)
            cached_status = _last_status.get(store_id)
            if cached_status and cached_status.get("ok") and \
               (time.time() - cached_at) < _VERIFY_TTL_SECONDS:
                return cached_status

    # Throttle concurrent heals so a burst of polls from one tenant doesn't
    # DoS their own store. blocking=False so the caller never blocks — if
    # a heal is already running for *any* store we just return the cached
    # (possibly None) status.
    acquired = _heal_semaphore.acquire(blocking=False)
    if not acquired:
        with _STATE_LOCK:
            cached = _last_status.get(store_id)
        return cached or {
            "ok": False, "delivery_url": "", "registered": [], "errors": [],
            "message": "Verification deferred — concurrent heal in progress.",
        }

    try:
        # Lazy import to avoid circular: stores.views imports orders.services
        # which would import us at module top.
        from stores.views import _register_webhook_for_store
        result = _register_webhook_for_store(store, request)
    finally:
        _heal_semaphore.release()

    with _STATE_LOCK:
        _last_status[store_id] = result
        if result.get("ok"):
            _last_verified[store_id] = time.time()
        else:
            # Don't cache a failure — we want the very next call to retry.
            _last_verified.pop(store_id, None)

    return result


def record_delivery(store_id: int) -> None:
    """Mark that we just received a real webhook from this store.

    Call from the webhook receiver views. Lets `get_last_delivery_age()`
    answer "is real-time sync actually firing for store X?" without
    needing a DB migration. In-memory only — that's enough for the
    sweeper to know whether to be aggressive about a particular store."""
    with _STATE_LOCK:
        _last_delivery[store_id] = time.time()


def get_last_delivery_age(store_id: int) -> Optional[float]:
    """Seconds since the last received webhook for this store, or None."""
    with _STATE_LOCK:
        ts = _last_delivery.get(store_id)
    return (time.time() - ts) if ts else None


def get_status_snapshot(store_id: int) -> Optional[dict]:
    """Last cached ensure_webhook result. Used by Diagnose UI."""
    with _STATE_LOCK:
        return _last_status.get(store_id)


def clear_cache(store_id: Optional[int] = None) -> None:
    """Drop cached state (for store_id, or all). Called after store delete."""
    with _STATE_LOCK:
        if store_id is None:
            _last_verified.clear()
            _last_status.clear()
            _last_delivery.clear()
        else:
            _last_verified.pop(store_id, None)
            _last_status.pop(store_id, None)
            _last_delivery.pop(store_id, None)


# ─── Background sweeper ────────────────────────────────────────────────────

def _sweep_once() -> None:
    """One pass: verify every active webhook-capable store."""
    # Lazy import — Django apps must be ready before we touch models.
    from stores.models import Store

    try:
        stores = list(
            Store.objects.filter(
                platform__in=("woocommerce", "shopify"),
                is_active=True,
            ).only("id", "name", "platform", "store_url", "api_key", "api_secret",
                   "access_token", "user_id")
        )
    except Exception as e:
        log.exception("webhook sentinel: sweep query failed: %s", e)
        return

    if not stores:
        return

    healed = 0
    failed = 0
    skipped = 0

    for store in stores:
        # Honour the same cache the on-demand path uses — if a recent
        # user action already verified this store, no point re-probing.
        with _STATE_LOCK:
            cached_at = _last_verified.get(store.id, 0)
            cached_status = _last_status.get(store.id)
        if cached_status and cached_status.get("ok") and \
           (time.time() - cached_at) < _VERIFY_TTL_SECONDS:
            skipped += 1
            continue

        try:
            res = ensure_webhook(store, request=None, force=True)
            if res.get("ok"):
                healed += 1
            else:
                failed += 1
                log.warning(
                    "webhook sentinel: store %s (%s) heal failed: %s",
                    store.id, store.name,
                    res.get("message") or res.get("errors"),
                )
        except Exception as e:
            failed += 1
            log.exception(
                "webhook sentinel: store %s (%s) heal crashed: %s",
                store.id, store.name, e,
            )

    log.info(
        "webhook sentinel sweep: %d healthy/healed · %d failed · %d cached-skip · %d total",
        healed, failed, skipped, len(stores),
    )


def _sweeper_loop() -> None:
    """Daemon thread body. Runs until process death."""
    log.info(
        "webhook sentinel: sweeper thread started (interval=%ds, cache_ttl=%ds)",
        _SWEEPER_INTERVAL_SECONDS, _VERIFY_TTL_SECONDS,
    )
    # Initial delay: let Django finish booting, gunicorn warm up, first
    # requests settle. Avoids stampeding the first cold worker.
    time.sleep(60)
    while True:
        try:
            _sweep_once()
        except Exception as e:
            log.exception("webhook sentinel: sweeper iteration crashed: %s", e)
        time.sleep(_SWEEPER_INTERVAL_SECONDS)


def start_sentinel() -> bool:
    """Idempotently start the background sweeper. Returns True if started.

    Safe to call multiple times — subsequent calls are no-ops. Skipped in
    test/migration contexts to avoid leaking threads."""
    global _sweeper_thread, _sweeper_started

    if _SENTINEL_DISABLED:
        log.info("webhook sentinel: disabled via env / pytest")
        return False

    # Don't run inside `manage.py migrate` / `collectstatic` / `shell` etc.
    # Only the actual server processes should spin up the sweeper.
    if not _should_run_sentinel():
        return False

    with _STATE_LOCK:
        if _sweeper_started:
            return False
        _sweeper_thread = threading.Thread(
            target=_sweeper_loop,
            name="webhook-sentinel-sweeper",
            daemon=True,
        )
        _sweeper_thread.start()
        _sweeper_started = True
    return True


def _should_run_sentinel() -> bool:
    """Heuristic: only run inside actual server processes (gunicorn/runserver),
    not management commands like `migrate`, `collectstatic`, `shell`, `test`.

    We detect by sniffing sys.argv[0] / sys.argv[1] — same pattern Django's
    own autoreload uses."""
    import sys

    argv = sys.argv
    if not argv:
        return True  # WSGI entry — no manage.py argv → server mode

    cmd = (argv[1] if len(argv) > 1 else "").lower()
    if cmd in {"migrate", "makemigrations", "collectstatic", "shell",
               "test", "createsuperuser", "dumpdata", "loaddata",
               "check", "showmigrations", "sqlmigrate", "diffsettings"}:
        return False

    # `manage.py runserver` and gunicorn both pass — but Django's autoreload
    # forks a child process and we only want the child to run the sentinel,
    # not the parent watching for file changes. RUN_MAIN=true is set in the
    # child only.
    if cmd == "runserver" and os.environ.get("RUN_MAIN") != "true":
        return False

    return True
