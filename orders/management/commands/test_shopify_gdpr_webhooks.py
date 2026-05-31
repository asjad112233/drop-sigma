"""
Offline validator for the 4 mandatory Shopify GDPR webhooks.

Shopify reviewers probe these URLs with valid + invalid HMAC signatures
during App Store submission. This command exercises the same code path
locally using Django's RequestFactory (no network calls) so we know the
endpoints are spec-compliant *before* submitting.

For each endpoint we verify:
  - valid HMAC  -> 200 OK    (Shopify retries on non-2xx; we must ack)
  - invalid HMAC -> 401      (proves we are actually verifying signatures)

Usage:
    python manage.py test_shopify_gdpr_webhooks
Exit code 0 on full pass, 1 if any case fails.
"""
import base64
import hashlib
import hmac
import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import RequestFactory

from orders import views


# Test secret used to sign mock requests. We monkey-patch this onto
# settings.SHOPIFY_API_SECRET for the duration of the run so the
# handlers' _verify_shopify_app_hmac sees the same value we sign with.
TEST_SECRET = "test_secret_abc123"
WRONG_SECRET = "definitely_not_the_real_secret"


# Realistic Shopify GDPR webhook payloads -- per Shopify docs
# https://shopify.dev/docs/apps/build/privacy-law-compliance
PAYLOADS = {
    "customers_data_request": {
        "shop_id": 954889,
        "shop_domain": "test-shop.myshopify.com",
        "orders_requested": [299938, 280263],
        "customer": {
            "id": 191167,
            "email": "test@example.com",
            "phone": "+1234567890",
        },
        "data_request": {"id": 9999},
    },
    "customers_redact": {
        "shop_id": 954889,
        "shop_domain": "test-shop.myshopify.com",
        "customer": {
            "id": 191167,
            "email": "test@example.com",
            "phone": "+1234567890",
        },
        "orders_to_redact": [299938, 280263],
    },
    "shop_redact": {
        "shop_id": 954889,
        "shop_domain": "test-shop.myshopify.com",
    },
    "app_uninstalled": {
        "id": 954889,
        "name": "Test Shop",
        "domain": "test-shop.myshopify.com",
        "myshopify_domain": "test-shop.myshopify.com",
    },
}


def _sign(secret, body):
    """Return base64(HMAC-SHA256(secret, body)) -- exactly what Shopify sends."""
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode()


def _build_request(factory, url, payload, secret, extra_headers=None):
    """Construct a POST request signed with `secret` so the handler can verify."""
    body = json.dumps(payload).encode("utf-8")
    sig = _sign(secret, body)
    headers = {
        "HTTP_X_SHOPIFY_HMAC_SHA256": sig,
        "content_type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    return factory.post(url, data=body, **headers)


class Command(BaseCommand):
    help = "Validate the 4 mandatory Shopify GDPR webhooks (valid + invalid HMAC cases)."

    def handle(self, *args, **options):
        # Force-set the test secret so the handlers verify against the
        # same key we sign with. We restore the original at the end.
        original_secret = getattr(settings, "SHOPIFY_API_SECRET", "")
        settings.SHOPIFY_API_SECRET = TEST_SECRET

        try:
            results = self._run_all_cases()
        finally:
            settings.SHOPIFY_API_SECRET = original_secret

        self._print_report(results)

        all_pass = all(r["ok"] for r in results)
        sys.exit(0 if all_pass else 1)

    # ------------------------------------------------------------------
    def _run_all_cases(self):
        rf = RequestFactory()

        cases = [
            # (label, view fn, payload key, extra headers)
            ("app/uninstalled",
             views.shopify_app_uninstalled_webhook,
             "app_uninstalled",
             {"HTTP_X_SHOPIFY_SHOP_DOMAIN": "test-shop.myshopify.com"}),

            ("customers/data_request",
             views.shopify_customers_data_request_webhook,
             "customers_data_request",
             None),

            ("customers/redact",
             views.shopify_customers_redact_webhook,
             "customers_redact",
             None),

            ("shop/redact",
             views.shopify_shop_redact_webhook,
             "shop_redact",
             None),
        ]

        results = []
        for label, view_fn, payload_key, extras in cases:
            payload = PAYLOADS[payload_key]

            # --- a) Valid HMAC -- should return 200 ---
            req = _build_request(rf, "/orders/webhook/shopify/" + label + "/",
                                 payload, TEST_SECRET, extras)
            resp = view_fn(req)
            results.append({
                "label": label + "  (valid HMAC)",
                "expected": 200,
                "actual": resp.status_code,
                "ok": resp.status_code == 200,
                "body": resp.content[:200].decode("utf-8", "replace"),
            })

            # --- b) Invalid HMAC -- should return 401 ---
            req_bad = _build_request(rf, "/orders/webhook/shopify/" + label + "/",
                                     payload, WRONG_SECRET, extras)
            resp_bad = view_fn(req_bad)
            results.append({
                "label": label + "  (invalid HMAC)",
                "expected": 401,
                "actual": resp_bad.status_code,
                "ok": resp_bad.status_code == 401,
                "body": resp_bad.content[:200].decode("utf-8", "replace"),
            })

        return results

    # ------------------------------------------------------------------
    def _print_report(self, results):
        self.stdout.write("")
        self.stdout.write("Shopify GDPR Webhook Validation Report")
        self.stdout.write("=" * 70)
        self.stdout.write("{:<40} {:<10} {:<10} Result".format(
            "Test Case", "Expected", "Actual"))
        self.stdout.write("-" * 70)
        for r in results:
            marker = "PASS" if r["ok"] else "FAIL"
            self.stdout.write("{:<40} {:<10} {:<10} {}".format(
                r["label"], r["expected"], r["actual"], marker))
        self.stdout.write("-" * 70)

        passed = sum(1 for r in results if r["ok"])
        total = len(results)
        self.stdout.write("Summary: {}/{} passed".format(passed, total))

        # Always show response bodies for any failures so the bug is
        # immediately visible.
        failed = [r for r in results if not r["ok"]]
        if failed:
            self.stdout.write("")
            self.stdout.write("Failure details:")
            for r in failed:
                self.stdout.write("  - " + r["label"])
                self.stdout.write("      response body: " + r["body"])
        self.stdout.write("")
