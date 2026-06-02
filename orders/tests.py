"""
Drop Sigma — Orders app tests.

The webhook-sentinel regression tests below exist specifically so the
"new orders silently don't sync" bug can never come back. They lock
down the wiring between:

    setup_woocommerce_webhook / setup_shopify_webhook  (return shape)
    _register_webhook_for_store                        (calls them)
    ensure_webhook / record_delivery / sweeper         (sentinel module)
    woocommerce_webhook + shopify_webhook receivers    (call record_delivery)
    orders_poll_api                                    (calls ensure_webhook)
    sync_orders                                        (calls ensure_webhook)
    OrdersConfig.ready()                               (starts sentinel)

Any commit that breaks one of those links will fail at least one test.
Run with:  python manage.py test orders -v 2
"""
from __future__ import annotations
import inspect
import re
import time
from unittest.mock import patch, MagicMock

from django.test import TestCase


# ════════════════════════════════════════════════════════════════════════
# 1. WEBHOOK SETUP CONTRACT — both platform helpers must return the same
#    rich-status dict shape. _register_webhook_for_store + everything
#    downstream depends on these keys.
# ════════════════════════════════════════════════════════════════════════

class WebhookSetupContractTests(TestCase):
    """Lock the public return shape of setup_*_webhook so callers don't break."""

    def test_woocommerce_setup_returns_status_dict(self):
        from orders import services
        sig = inspect.signature(services.setup_woocommerce_webhook)
        # (store, delivery_url) — two positional args, no surprise kwargs.
        params = list(sig.parameters)
        self.assertEqual(params[:2], ["store", "delivery_url"])

    def test_shopify_setup_returns_status_dict(self):
        from orders import services
        sig = inspect.signature(services.setup_shopify_webhook)
        params = list(sig.parameters)
        self.assertEqual(params[:2], ["store", "delivery_url"])

    def test_woocommerce_setup_returns_required_dict_keys(self):
        """Even when the store API errors out, must return the dict
        shape, not raise. Mocks the HTTP session so the test finishes
        in milliseconds instead of waiting on real network timeouts."""
        from orders import services

        fake = MagicMock()
        fake.store_url = "https://x.test"
        fake.api_key = "x"
        fake.api_secret = "y"

        with patch.object(services, "woo_session") as mock_sess:
            session = MagicMock()
            session.get.side_effect = Exception("simulated network error")
            session.post.side_effect = Exception("simulated network error")
            session.put.side_effect = Exception("simulated network error")
            mock_sess.return_value = session

            result = services.setup_woocommerce_webhook(
                fake, "https://dropsigma.com/orders/webhook/woocommerce/1/"
            )

        self.assertIsInstance(result, dict)
        for key in ("ok", "registered", "errors", "webhook_ids"):
            self.assertIn(key, result, f"missing required key {key!r}")
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["errors"], list)
        self.assertGreater(len(result["errors"]), 0)

    def test_shopify_setup_returns_required_dict_keys(self):
        from orders import services

        fake = MagicMock()
        fake.store_url = "https://x.test"
        fake.api_key = "x"
        fake.api_secret = "y"
        fake.access_token = ""

        with patch.object(services, "requests") as mock_req:
            mock_req.get.side_effect = Exception("simulated network error")
            mock_req.post.side_effect = Exception("simulated network error")
            mock_req.put.side_effect = Exception("simulated network error")

            result = services.setup_shopify_webhook(
                fake, "https://dropsigma.com/orders/webhook/shopify/1/"
            )

        self.assertIsInstance(result, dict)
        for key in ("ok", "registered", "errors", "webhook_ids"):
            self.assertIn(key, result)
        self.assertFalse(result["ok"])


# ════════════════════════════════════════════════════════════════════════
# 2. SENTINEL MODULE PUBLIC API — everything callers depend on must exist
#    with the right signatures. If someone removes ensure_webhook or
#    renames record_delivery, this fails immediately.
# ════════════════════════════════════════════════════════════════════════

class SentinelPublicAPITests(TestCase):

    def test_sentinel_exports(self):
        from orders import webhook_sentinel
        # The names callers in the codebase import. Don't loosen this list
        # without updating every caller.
        required = (
            "ensure_webhook",
            "record_delivery",
            "get_last_delivery_age",
            "get_status_snapshot",
            "clear_cache",
            "start_sentinel",
        )
        for name in required:
            self.assertTrue(
                hasattr(webhook_sentinel, name),
                f"webhook_sentinel.{name} missing — orders sync wiring is broken",
            )

    def test_tunables_are_reasonable_for_real_time(self):
        """The whole point of the sentinel is real-time sync. If somebody
        bumps these values too high, order confirmation emails start
        arriving minutes late again — exactly the bug we fixed."""
        from orders import webhook_sentinel as s
        self.assertLessEqual(
            s._VERIFY_TTL_SECONDS, 120,
            "_VERIFY_TTL_SECONDS too high — real-time sync degrades. "
            "Should be ≤ 120s.",
        )
        self.assertLessEqual(
            s._SWEEPER_INTERVAL_SECONDS, 120,
            "_SWEEPER_INTERVAL_SECONDS too high — idle stores rot. "
            "Should be ≤ 120s.",
        )
        self.assertLessEqual(
            s._SWEEPER_STARTUP_DELAY_SECONDS, 30,
            "_SWEEPER_STARTUP_DELAY too long — pre-existing broken "
            "webhooks stay broken after a deploy.",
        )
        self.assertGreaterEqual(
            s._HEAL_CATCHUP_HOURS, 1,
            "_HEAL_CATCHUP_HOURS too low — orders placed during the "
            "broken window won't be caught up.",
        )

    def test_ensure_webhook_caches_successful_results(self):
        from orders import webhook_sentinel

        store = MagicMock()
        store.id = 90001
        store.platform = "woocommerce"
        webhook_sentinel.clear_cache(store.id)

        with patch("stores.views._register_webhook_for_store") as mock_reg:
            mock_reg.return_value = {
                "ok": True, "delivery_url": "https://x", "registered": ["order.created"],
                "errors": [], "message": "ok",
            }

            first = webhook_sentinel.ensure_webhook(store)
            second = webhook_sentinel.ensure_webhook(store)  # cache hit
            third = webhook_sentinel.ensure_webhook(store, force=True)  # bypass

        # 1st + 3rd call → 2 real verifications. 2nd is the cache hit.
        self.assertEqual(mock_reg.call_count, 2,
                         "ensure_webhook cache is broken — poll endpoint will hammer WC every 10s.")
        self.assertTrue(first["ok"])
        self.assertEqual(first, second)
        self.assertTrue(third["ok"])
        webhook_sentinel.clear_cache(store.id)

    def test_ensure_webhook_does_not_cache_failures(self):
        """A failed heal must be retried on the next call, not cached
        for the TTL — otherwise a transient WC outage looks permanent."""
        from orders import webhook_sentinel

        store = MagicMock()
        store.id = 90002
        store.platform = "woocommerce"
        webhook_sentinel.clear_cache(store.id)

        with patch("stores.views._register_webhook_for_store") as mock_reg:
            mock_reg.return_value = {
                "ok": False, "delivery_url": "", "registered": [], "errors": [],
                "message": "fail",
            }
            webhook_sentinel.ensure_webhook(store)
            webhook_sentinel.ensure_webhook(store)

        self.assertEqual(mock_reg.call_count, 2,
                         "Failed ensure_webhook must NOT be cached, or transient errors stick.")
        webhook_sentinel.clear_cache(store.id)

    def test_ensure_webhook_triggers_catchup_on_broken_to_ok_transition(self):
        """When a previously-broken webhook heals, the sentinel must
        background-pull recent orders so the test order the tenant just
        placed (during the broken window) actually arrives."""
        from orders import webhook_sentinel

        store = MagicMock()
        store.id = 90003
        store.platform = "woocommerce"
        webhook_sentinel.clear_cache(store.id)

        # 1st call: returns broken.
        with patch("stores.views._register_webhook_for_store") as mock_reg, \
             patch("orders.webhook_sentinel._kickoff_catchup_sync") as mock_catchup:
            mock_reg.return_value = {"ok": False, "registered": [], "errors": [],
                                     "delivery_url": "", "message": "broken"}
            webhook_sentinel.ensure_webhook(store, force=True)
            self.assertEqual(mock_catchup.call_count, 0,
                             "Catch-up shouldn't fire while still broken.")

        # 2nd call: heals → catchup must fire ONCE.
        with patch("stores.views._register_webhook_for_store") as mock_reg, \
             patch("orders.webhook_sentinel._kickoff_catchup_sync") as mock_catchup:
            mock_reg.return_value = {"ok": True, "registered": ["order.created"],
                                     "errors": [], "delivery_url": "https://x", "message": "ok"}
            webhook_sentinel.ensure_webhook(store, force=True)
            self.assertEqual(mock_catchup.call_count, 1,
                             "Catch-up MUST fire when webhook transitions broken→ok, "
                             "or orders missed during the gap silently disappear.")

        webhook_sentinel.clear_cache(store.id)

    def test_record_delivery_roundtrip(self):
        from orders.webhook_sentinel import record_delivery, get_last_delivery_age, clear_cache
        clear_cache(90004)
        self.assertIsNone(get_last_delivery_age(90004))
        record_delivery(90004)
        age = get_last_delivery_age(90004)
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)
        clear_cache(90004)


# ════════════════════════════════════════════════════════════════════════
# 3. WIRING TESTS — by static inspection of source files. These catch
#    regressions where a future refactor accidentally drops the sentinel
#    call from a critical path (poll endpoint, webhook receiver, etc).
# ════════════════════════════════════════════════════════════════════════

class SentinelWiringTests(TestCase):
    """Greps the source of the hot-path views/receivers to prove the
    sentinel is wired in. Source-level grep beats execution-mocking here
    because a missing call wouldn't fail a request — it would just
    silently stop healing webhooks.

    DRF @api_view decorates views so inspect.getsource() returns the
    wrapper instead of our code. We read the raw file from disk and
    slice out the named function body to dodge that."""

    @staticmethod
    def _func_source(module_path_parts: tuple[str, ...], func_name: str) -> str:
        """Read the source file from disk and return the body of `def func_name(...)`."""
        from pathlib import Path
        # module_path_parts e.g. ("orders", "views") → orders/views.py
        path = Path(__file__).resolve().parent.parent / Path(*module_path_parts).with_suffix(".py")
        text = path.read_text(encoding="utf-8")
        # Match `def func_name(` and grab until the next top-level `def `/`class `.
        m = re.search(
            rf"^def {re.escape(func_name)}\(.*?(?=^(?:def |class |@api_view|@csrf_exempt|@permission_classes)\b)",
            text, re.MULTILINE | re.DOTALL,
        )
        if not m:
            # Fallback: capture from def to EOF.
            m = re.search(
                rf"^def {re.escape(func_name)}\(.*\Z", text, re.MULTILINE | re.DOTALL,
            )
        if not m:
            raise AssertionError(
                f"could not locate def {func_name}() in {path} — "
                f"test helper needs updating."
            )
        return m.group(0)

    def test_orders_poll_calls_ensure_webhook(self):
        src = self._func_source(("orders", "views"), "orders_poll_api")
        self.assertIn("ensure_webhook", src,
                      "orders_poll_api must call ensure_webhook so the "
                      "every-10s poll opportunistically self-heals.")

    def test_sync_orders_calls_ensure_webhook(self):
        src = self._func_source(("orders", "views"), "sync_orders")
        self.assertIn("ensure_webhook", src,
                      "sync_orders (manual 🔄 Refresh) must call "
                      "ensure_webhook so Refresh also fixes the webhook.")

    def test_woocommerce_webhook_calls_record_delivery(self):
        src = self._func_source(("orders", "views"), "woocommerce_webhook")
        self.assertIn("record_delivery", src,
                      "woocommerce_webhook receiver MUST call "
                      "record_delivery — without it the sentinel can't "
                      "tell that real-time sync is alive for the store.")

    def test_shopify_webhook_calls_record_delivery(self):
        src = self._func_source(("orders", "views"), "shopify_webhook")
        self.assertIn("record_delivery", src,
                      "shopify_webhook receiver MUST call record_delivery.")

    def test_apps_ready_starts_sentinel(self):
        """OrdersConfig.ready() must boot the sentinel — otherwise the
        background sweeper never runs and idle stores rot."""
        from orders.apps import OrdersConfig
        src = inspect.getsource(OrdersConfig)
        self.assertIn("webhook_sentinel", src)
        self.assertIn("start_sentinel", src)

    def test_register_webhook_for_store_returns_dict(self):
        """The connect endpoints depend on the rich status return. If
        somebody refactors _register_webhook_for_store back to silent-
        failure, connect responses lose webhook_status and the UI
        warning toast stops working."""
        from stores.views import _register_webhook_for_store
        src = inspect.getsource(_register_webhook_for_store)
        for token in ('"ok"', '"message"', '"registered"', '"errors"'):
            self.assertIn(token, src,
                          f"_register_webhook_for_store must return a "
                          f"dict containing {token}.")

    def test_orders_poll_has_safety_pull(self):
        """The poll endpoint's safety-pull (fetch orders directly from
        the store when webhook delivery is stale) is the LAST line of
        defense against silent loss. Removing it brings back the
        'new order doesn't sync' bug."""
        src = self._func_source(("orders", "views"), "orders_poll_api")
        self.assertTrue(
            re.search(r"sync_(woocommerce|shopify)_orders", src),
            "orders_poll_api must invoke sync_*_orders as a safety "
            "pull when webhook delivery is stale.",
        )
        self.assertIn("_ACTIVE_PULL_CACHE", src,
                      "Safety-pull throttle cache missing — poll will "
                      "hammer the store API on every tick.")
