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
# store for this long. Tight (60s) because order confirmation emails are
# time-critical — a tenant placing a test order can't wait 5 min for the
# sentinel to notice a broken webhook. Cheap thanks to cache: the cost is
# one extra HTTP call per active tab per minute per store.
_VERIFY_TTL_SECONDS = 60

# Background sweeper: how often the daemon thread walks every store.
# 60s so even idle stores (no one's logged in) get their webhook checked
# every minute — worst-case lag from "webhook just rotted" to "sentinel
# healed it" is 60 seconds.
_SWEEPER_INTERVAL_SECONDS = 60

# Initial delay before first sweep. Short enough that a fresh deploy heals
# everyone within the first minute, long enough to let gunicorn warm up
# and avoid stampeding the very first cold worker.
_SWEEPER_STARTUP_DELAY_SECONDS = 10

# When the sentinel detects a broken webhook and successfully heals it,
# pull this many hours of recent orders to catch anything that landed in
# the store while the webhook was dead. Keeps order-confirmation emails
# from being silently dropped.
_HEAL_CATCHUP_HOURS = 1

# ── Freshness pull (the "I went to sleep and the order didn't sync" fix) ──
# Even when the webhook says it's healthy, the sentinel periodically pulls
# recent orders from each store. Reason: a healthy webhook can still drop
# deliveries silently — Railway sleep, brief 5xx, network blip, WC retry
# back-off. Without a defensive pull, those orders would only arrive when
# the tenant clicks Refresh.
#
# Strategy:
#   - If a store has had no webhook delivery in _STALE_DELIVERY_SECONDS,
#     pull the last _FRESHNESS_PULL_HOURS of orders.
#   - Throttle per-store to one pull every _FRESHNESS_PULL_INTERVAL so
#     we never hammer a slow merchant store.
# Reduced from 300s (5 min) so the founder notification email lands
# within ~1 min of an order even when the real-time webhook drops
# the delivery (or hasn't been registered yet on a freshly-connected
# store). 60 s × 6 active stores × WC-API call ≈ 6 calls/min — well
# below any merchant rate-limit and a noticeable UX improvement.
_STALE_DELIVERY_SECONDS = 60          # 1 min without a webhook = pull defensively
_FRESHNESS_PULL_HOURS = 2             # how far back to look on each pull
_FRESHNESS_PULL_INTERVAL = 60         # min seconds between pulls per store

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
_last_verified:    dict[int, float] = {}   # store_id -> unix ts of last successful verify
_last_status:      dict[int, dict] = {}    # store_id -> last ensure_webhook result
_last_delivery:    dict[int, float] = {}   # store_id -> unix ts of last webhook receive
_last_freshness:   dict[int, float] = {}   # store_id -> unix ts of last defensive pull

# Sweeper lifecycle
_sweeper_thread: Optional[threading.Thread] = None
_sweeper_started = False
_heal_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_HEALS)


# ─── Public API ────────────────────────────────────────────────────────────

def ensure_webhook(store, request=None, *, force: bool = False) -> dict:
    """Verify + auto-heal real-time webhooks for `store`. Cheap on the hot path.

    If we successfully verified this store within the last
    _VERIFY_TTL_SECONDS and the cached status says ok, return the cached
    result without hitting the network. Pass `force=True` to bypass the
    cache (e.g. user clicked "Re-register Webhook" explicitly).

    Side effect — catch-up sync: if this call transitions the store from
    "broken or unknown" → "ok", we immediately background-pull the last
    `_HEAL_CATCHUP_HOURS` of orders so any order that landed in the
    store while the webhook was dead lands in our DB too. Without this,
    the test order the tenant just placed (while waiting for the
    sentinel to heal) would silently never arrive — and the auto-email
    template attached to that order would never fire.

    Returns the same dict shape that _register_webhook_for_store returns:
        {ok, delivery_url, registered, errors, message}
    """
    store_id = store.id

    # Snapshot the pre-heal status so we know whether this call actually
    # transitions us from broken to ok (which is what triggers catch-up).
    with _STATE_LOCK:
        prev_status = _last_status.get(store_id)
    was_broken = (prev_status is None) or (not prev_status.get("ok"))

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

    # ── Catch-up sync on transition broken → ok ───────────────────────────
    # The store may have received orders while its webhook was dead.
    # Background-fetch them right now so the tenant doesn't lose data
    # (and their auto-email-on-status-change templates fire on the missed
    # orders too, as part of normal order processing).
    if was_broken and result.get("ok"):
        _kickoff_catchup_sync(store)

    return result


def _kickoff_catchup_sync(store) -> None:
    """Fire-and-forget pull of last _HEAL_CATCHUP_HOURS of orders.

    Used right after a webhook heals — any order placed during the broken
    window would have been missed by the webhook, so we go fetch them
    explicitly. Cheap (single store-API call returning at most a few
    orders) and safe to call from any thread."""
    def _run():
        try:
            after = (datetime.now(_tz.utc) - timedelta(hours=_HEAL_CATCHUP_HOURS))
            after_iso = after.strftime("%Y-%m-%dT%H:%M:%S")
            if store.platform == "woocommerce":
                from .services import sync_woocommerce_orders
                count = sync_woocommerce_orders(store, after=after_iso)
            elif store.platform == "shopify":
                from .services import sync_shopify_orders
                count = sync_shopify_orders(store, after=after_iso)
            else:
                return
            if count:
                log.info(
                    "webhook sentinel catch-up: pulled %d order(s) for store %s (%s) "
                    "from the last %dh after webhook heal",
                    count, store.id, store.name, _HEAL_CATCHUP_HOURS,
                )
        except Exception as e:
            log.warning(
                "webhook sentinel catch-up failed for store %s (%s): %s",
                store.id, store.name, e,
            )

    threading.Thread(target=_run, name=f"webhook-catchup-{store.id}",
                     daemon=True).start()


def _maybe_freshness_pull(store) -> bool:
    """Defensive pull of recent orders when the webhook hasn't delivered
    anything lately. Returns True if a pull was triggered.

    This is the "I went to sleep, an order came, the webhook silently
    missed it, I woke up to an empty dashboard" guard. The sweeper calls
    this for every store on every pass. Throttled per-store so we never
    hammer a slow merchant store.

    Behaviour:
      - If we received a webhook from this store within
        _STALE_DELIVERY_SECONDS → skip (no pull needed, real-time is fine).
      - If we pulled from this store within _FRESHNESS_PULL_INTERVAL →
        skip (we already checked recently).
      - Otherwise → kick off background sync_*_orders for the last
        _FRESHNESS_PULL_HOURS.

    Cost per store with no deliveries: ~1 store-API call every
    _FRESHNESS_PULL_INTERVAL seconds (5 min by default). For 100 active
    stores that's ~20 calls/min total → negligible."""
    now_ts = time.time()
    with _STATE_LOCK:
        last_recv = _last_delivery.get(store.id)
        last_pull = _last_freshness.get(store.id, 0)

    # Real-time is firing fine → no defensive pull needed.
    if last_recv is not None and (now_ts - last_recv) < _STALE_DELIVERY_SECONDS:
        return False
    # Already pulled recently → don't hammer the store API.
    if (now_ts - last_pull) < _FRESHNESS_PULL_INTERVAL:
        return False

    # Record the pull attempt up front so concurrent sweepers don't double-fire.
    with _STATE_LOCK:
        _last_freshness[store.id] = now_ts

    def _run():
        try:
            after = datetime.now(_tz.utc) - timedelta(hours=_FRESHNESS_PULL_HOURS)
            after_iso = after.strftime("%Y-%m-%dT%H:%M:%S")
            if store.platform == "woocommerce":
                from .services import sync_woocommerce_orders
                count = sync_woocommerce_orders(store, after=after_iso)
            elif store.platform == "shopify":
                from .services import sync_shopify_orders
                count = sync_shopify_orders(store, after=after_iso)
            else:
                return
            if count:
                log.info(
                    "webhook sentinel freshness-pull: store %s (%s) — pulled "
                    "%d order(s) from last %dh (webhook idle %s)",
                    store.id, store.name, count, _FRESHNESS_PULL_HOURS,
                    f"{int(now_ts - last_recv)}s" if last_recv else "forever",
                )
        except Exception as e:
            log.warning(
                "webhook sentinel freshness-pull failed for store %s (%s): %s",
                store.id, store.name, e,
            )

    threading.Thread(target=_run, name=f"webhook-freshness-{store.id}",
                     daemon=True).start()
    return True


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
            _last_freshness.clear()
        else:
            _last_verified.pop(store_id, None)
            _last_status.pop(store_id, None)
            _last_delivery.pop(store_id, None)
            _last_freshness.pop(store_id, None)


# ─── Background sweeper ────────────────────────────────────────────────────

def _sweep_once() -> None:
    """One pass: verify every active webhook-capable store + freshness-pull
    any store that hasn't had a recent webhook delivery.

    Two responsibilities per pass:
      1. Webhook health verify+heal (existing behaviour).
      2. Defensive freshness pull (NEW) — even when the webhook is "ok",
         if no real-time delivery has been seen in the last few minutes
         we pull recent orders directly from the store API. This is the
         "tenant was asleep, webhook silently dropped during Railway sleep,
         tenant woke to an empty dashboard" guard.
    """
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
    pulls_kicked = 0

    for store in stores:
        # Freshness pull ALWAYS runs — independent of webhook cache state.
        # Internally throttled so a healthy webhook means no pull.
        try:
            if _maybe_freshness_pull(store):
                pulls_kicked += 1
        except Exception as e:
            log.exception(
                "webhook sentinel: freshness check crashed for store %s (%s): %s",
                store.id, store.name, e,
            )

        # Honour the verify cache for the (more expensive) webhook
        # registration probe.
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
        "webhook sentinel sweep: %d healthy/healed · %d failed · %d cached-skip · "
        "%d freshness-pulls · %d total",
        healed, failed, skipped, pulls_kicked, len(stores),
    )


_BACKFILL_DONE = False
_BACKFILL_LOCK = threading.Lock()


def _backfill_created_at_once() -> None:
    """One-shot rescue: fix Order.created_at for already-synced orders.

    Background: pre-existing orders have created_at = DB insert time (because
    the model used auto_now_add=True). When a batch sync ingested e.g. 50
    orders in one go, all their created_at values were near-identical and
    sorted in reverse-WC order, so the dashboard showed oldest-at-top. The
    sync path is now fixed (process_*_order pin created_at to the real
    platform date), but historical rows still need correcting.

    This walks every Order where raw_data has a date field, parses it, and
    issues an UPDATE if the stored created_at differs by more than 2 seconds.
    Runs in the background — never blocks request serving.

    Idempotent + safe to call repeatedly. Self-disables once successful so
    every subsequent deploy doesn't re-scan unchanged rows."""
    global _BACKFILL_DONE
    with _BACKFILL_LOCK:
        if _BACKFILL_DONE:
            return
        _BACKFILL_DONE = True  # set up-front so concurrent gunicorn workers
                               # don't all kick off the same scan.

    def _run():
        from orders.models import Order
        from orders.services import _parse_iso_dt
        log.info("orders backfill: starting created_at backfill from raw_data")
        scanned = 0
        fixed = 0
        try:
            # Iterate cheaply via .only() — raw_data is heavy, but we need it.
            qs = Order.objects.exclude(raw_data__isnull=True).only(
                "id", "created_at", "raw_data", "store_id"
            )
            for o in qs.iterator(chunk_size=200):
                scanned += 1
                raw = o.raw_data or {}
                # WC: prefer date_created_gmt → date_created
                # Shopify: created_at
                candidate = (
                    raw.get("date_created_gmt")
                    or raw.get("date_created")
                    or raw.get("created_at")
                )
                if not candidate:
                    continue
                dt = _parse_iso_dt(candidate)
                if not dt:
                    continue
                # Skip if already in sync (within 2 sec).
                if o.created_at and abs((o.created_at - dt).total_seconds()) < 2:
                    continue
                Order.objects.filter(pk=o.pk).update(created_at=dt)
                fixed += 1
            log.info(
                "orders backfill: complete — scanned %d, fixed %d created_at values",
                scanned, fixed,
            )
        except Exception as e:
            log.exception("orders backfill: crashed: %s", e)
            # On failure, allow a retry on the next boot.
            global _BACKFILL_DONE
            with _BACKFILL_LOCK:
                _BACKFILL_DONE = False

    threading.Thread(target=_run, name="orders-created-at-backfill",
                     daemon=True).start()


def _sweeper_loop() -> None:
    """Daemon thread body. Runs until process death."""
    log.info(
        "webhook sentinel: sweeper thread started (interval=%ds, cache_ttl=%ds, "
        "startup_delay=%ds, catchup_hours=%d, stale_delivery=%ds, "
        "freshness_pull=%dh @ every %ds)",
        _SWEEPER_INTERVAL_SECONDS, _VERIFY_TTL_SECONDS,
        _SWEEPER_STARTUP_DELAY_SECONDS, _HEAL_CATCHUP_HOURS,
        _STALE_DELIVERY_SECONDS, _FRESHNESS_PULL_HOURS, _FRESHNESS_PULL_INTERVAL,
    )
    # Short initial delay: let Django finish booting + gunicorn warm up,
    # but heal any pre-existing broken webhooks within seconds of deploy.
    time.sleep(_SWEEPER_STARTUP_DELAY_SECONDS)

    # One-shot rescue for historical orders that landed with INSERT-time
    # created_at (the old auto_now_add=True behaviour). Runs once per
    # process lifetime in the background.
    try:
        _backfill_created_at_once()
    except Exception as e:
        log.exception("orders backfill: kickoff failed (non-fatal): %s", e)

    while True:
        try:
            _sweep_once()
        except Exception as e:
            log.exception("webhook sentinel: sweeper iteration crashed: %s", e)
        time.sleep(_SWEEPER_INTERVAL_SECONDS)


def _read_func_source(module_parts: tuple, func_name: str) -> str:
    """Read source for a top-level function from disk (bypasses DRF's
    @api_view wrapper, which inspect.getsource would otherwise show)."""
    import re as _re
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    path = base / Path(*module_parts).with_suffix(".py")
    text = path.read_text(encoding="utf-8")
    m = _re.search(
        rf"^def {_re.escape(func_name)}\(.*?(?=^(?:def |class |@api_view|@csrf_exempt|@permission_classes)\b)",
        text, _re.MULTILINE | _re.DOTALL,
    )
    if not m:
        m = _re.search(rf"^def {_re.escape(func_name)}\(.*\Z", text,
                       _re.MULTILINE | _re.DOTALL)
    return m.group(0) if m else ""


def _audit_wiring() -> list[str]:
    """Static check that every part of the real-time sync chain is still
    wired correctly. Run once at sentinel startup. Returns a list of
    human-readable problems (empty = healthy).

    This is a runtime guard against refactors silently removing one of
    the integration points — the kind of regression that wouldn't break
    any single request but would silently turn off real-time sync."""
    import inspect
    problems: list[str] = []

    # 1. orders_poll_api must call ensure_webhook (so every 10s poll
    #    opportunistically self-heals the focused store) AND must do a
    #    safety pull when webhook delivery is stale.
    try:
        src = _read_func_source(("orders", "views"), "orders_poll_api")
        if "ensure_webhook" not in src:
            problems.append(
                "orders.views.orders_poll_api no longer calls ensure_webhook — "
                "the every-10s poll has stopped self-healing webhooks."
            )
        if "sync_woocommerce_orders" not in src and "sync_shopify_orders" not in src:
            problems.append(
                "orders.views.orders_poll_api safety-pull is missing — "
                "broken webhooks will silently drop orders again."
            )
    except Exception as e:
        problems.append(f"could not audit orders_poll_api: {e}")

    # 2. Both webhook receivers must call record_delivery.
    for view_name in ("woocommerce_webhook", "shopify_webhook"):
        try:
            src = _read_func_source(("orders", "views"), view_name)
            if "record_delivery" not in src:
                problems.append(
                    f"orders.views.{view_name} no longer calls record_delivery — "
                    f"sentinel can't tell whether real-time sync is alive."
                )
        except Exception as e:
            problems.append(f"could not audit {view_name}: {e}")

    # 3. sync_orders (manual Refresh) must call ensure_webhook so
    #    Refresh also self-heals.
    try:
        src = _read_func_source(("orders", "views"), "sync_orders")
        if "ensure_webhook" not in src:
            problems.append(
                "orders.views.sync_orders no longer calls ensure_webhook — "
                "manual Refresh has stopped self-healing webhooks."
            )
    except Exception as e:
        problems.append(f"could not audit sync_orders: {e}")

    # 4. _register_webhook_for_store must return the rich status dict.
    try:
        from stores.views import _register_webhook_for_store
        src = inspect.getsource(_register_webhook_for_store)
        for token in ('"ok"', '"message"', '"registered"', '"errors"'):
            if token not in src:
                problems.append(
                    f"stores.views._register_webhook_for_store no longer "
                    f"returns {token} — connect endpoints + UI warnings break."
                )
                break
    except Exception as e:
        problems.append(f"could not audit _register_webhook_for_store: {e}")

    # 5. Tunables must stay tight enough to keep order-confirmation
    #    emails real-time.
    if _VERIFY_TTL_SECONDS > 120:
        problems.append(
            f"_VERIFY_TTL_SECONDS={_VERIFY_TTL_SECONDS} is too high — "
            f"real-time sync degrades. Should be ≤ 120s."
        )
    if _SWEEPER_INTERVAL_SECONDS > 120:
        problems.append(
            f"_SWEEPER_INTERVAL_SECONDS={_SWEEPER_INTERVAL_SECONDS} is too high — "
            f"idle stores' webhooks rot. Should be ≤ 120s."
        )
    if _SWEEPER_STARTUP_DELAY_SECONDS > 30:
        problems.append(
            f"_SWEEPER_STARTUP_DELAY={_SWEEPER_STARTUP_DELAY_SECONDS}s too long — "
            f"pre-existing broken webhooks stay broken after deploy."
        )

    return problems


def start_sentinel() -> bool:
    """Idempotently start the background sweeper. Returns True if started.

    Safe to call multiple times — subsequent calls are no-ops. Skipped in
    test/migration contexts to avoid leaking threads.

    Also runs `_audit_wiring()` and logs CRITICAL for any integration
    point that's been silently disconnected. This is the lifetime
    guarantee: even if a future refactor breaks the sentinel's wiring,
    the very next deploy will surface the regression in Railway logs
    instead of silently dropping new orders."""
    global _sweeper_thread, _sweeper_started

    if _SENTINEL_DISABLED:
        log.info("webhook sentinel: disabled via env / pytest")
        return False

    # Don't run inside `manage.py migrate` / `collectstatic` / `shell` etc.
    # Only the actual server processes should spin up the sweeper.
    if not _should_run_sentinel():
        return False

    # Wiring audit — fail loudly on regressions instead of silently
    # serving traffic with no real-time sync.
    problems = []
    try:
        problems = _audit_wiring()
    except Exception:
        log.exception("webhook sentinel: wiring audit crashed")
    if problems:
        log.critical(
            "🚨 webhook sentinel: %d wiring problem(s) detected — real-time "
            "order sync is degraded! Fix before continuing:\n  - %s",
            len(problems), "\n  - ".join(problems),
        )
    else:
        log.info("webhook sentinel: wiring audit passed (all integration points intact)")

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
