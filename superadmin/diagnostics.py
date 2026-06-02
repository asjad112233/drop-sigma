"""
Platform-wide system health diagnostics for the Super Admin portal.

Each `check_*` function is standalone, returns a `_result(...)` dict, and
must complete within a few seconds. The orchestrator (`run_diagnostics`)
runs every check in parallel via a thread pool so external HTTP probes
don't serialise.

Status semantics:
    ok   — green: working as expected
    warn — amber: degraded / attention recommended / partial coverage
    fail — red:   broken / missing / hard error

Add a new check by writing a `check_*` function and registering it in
`CATEGORIES` at the bottom of this module.
"""

import os
import re
import time
import json
import socket
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from django.conf import settings
from django.db import connection, OperationalError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from django.contrib.auth.models import User

from stores.models import Store
from orders.models import Order
from vendors.models import Vendor, VendorInvitation, VendorTrackingSubmission, ProductVendorAssignment
from emails.models import EmailAccount, EmailTemplate
from teamapp.models import TeamMember, EmployeeInvitation, AssignmentRule, ChatChannel
from .models import Tenant, Subscription


# ── Helpers ─────────────────────────────────────────────────────────────────

def _result(status, message, details=None, fix_hint=None):
    return {
        "status": status,
        "message": message,
        "details": details or {},
        "fix_hint": fix_hint,
    }


def _timed(check_fn, title):
    """Wrap a check: time it, catch any exception, attach title + duration."""
    start = time.monotonic()
    try:
        r = check_fn()
        if not isinstance(r, dict) or "status" not in r:
            r = _result("fail", "Check returned malformed result")
    except Exception as e:
        r = _result("fail", f"{type(e).__name__}: {e}"[:200])
    r["title"] = title
    r["duration_ms"] = int((time.monotonic() - start) * 1000)
    return r


def _http_probe(url, headers=None, timeout=4, method="GET"):
    """Lightweight HTTP request that returns (status_code, body_str | None, error).

    Adds a browser-like User-Agent so health probes against merchant stores
    hidden behind Cloudflare (Bot Fight Mode) aren't blocked with HTTP 406.
    """
    merged = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2048).decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return 0, None, f"{type(e.reason).__name__}: {e.reason}"
    except socket.timeout:
        return 0, None, "timeout"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"[:120]


def _env_set(name):
    return bool(os.getenv(name, "").strip())


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 1 — INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

def check_database():
    with connection.cursor() as c:
        c.execute("SELECT 1")
        c.fetchone()
    engine = connection.settings_dict.get("ENGINE", "?").rsplit(".", 1)[-1]
    return _result("ok", f"Connected · {engine}")


def check_migrations():
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        pending = len(plan)
    except Exception as e:
        return _result("fail", f"Could not inspect migrations: {e}"[:160])
    if pending == 0:
        return _result("ok", "All migrations applied")
    return _result(
        "warn",
        f"{pending} unapplied migration(s)",
        {"pending": pending},
        fix_hint="Run `python manage.py migrate` on this environment.",
    )


def check_debug_flag():
    if not settings.DEBUG:
        return _result("ok", "DEBUG is off (production-safe)")
    return _result(
        "warn",
        "DEBUG is on — must be off in production",
        fix_hint="Set DEBUG=False in environment.",
    )


def check_allowed_hosts():
    hosts = settings.ALLOWED_HOSTS or []
    if "*" in hosts:
        return _result(
            "warn",
            "ALLOWED_HOSTS contains wildcard (*) — restrict in production",
            {"hosts": hosts},
        )
    if not hosts:
        return _result("fail", "ALLOWED_HOSTS empty — Django will reject all requests")
    return _result("ok", f"{len(hosts)} host(s) allowed", {"hosts": hosts})


def check_required_env_vars():
    required = {
        "SECRET_KEY":          _env_set("SECRET_KEY"),
        "DATABASE_URL":        _env_set("DATABASE_URL"),
        "GOOGLE_CLIENT_ID":    _env_set("GOOGLE_CLIENT_ID"),
        "GOOGLE_CLIENT_SECRET":_env_set("GOOGLE_CLIENT_SECRET"),
        "EMAIL_HOST_USER":     _env_set("EMAIL_HOST_USER"),
        "EMAIL_HOST_PASSWORD": _env_set("EMAIL_HOST_PASSWORD"),
    }
    missing = [k for k, v in required.items() if not v]
    if not missing:
        return _result("ok", "All required env vars present", required)
    return _result(
        "fail",
        f"{len(missing)} missing: {', '.join(missing)}",
        required,
        fix_hint="Set these in Railway → Variables.",
    )


def check_optional_env_vars():
    optional = {
        "OPENAI_API_KEY":      _env_set("OPENAI_API_KEY"),
        "ANTHROPIC_API_KEY":   _env_set("ANTHROPIC_API_KEY"),
        "STRIPE_SECRET_KEY":   _env_set("STRIPE_SECRET_KEY"),
        "PAYPAL_CLIENT_ID":    _env_set("PAYPAL_CLIENT_ID"),
        "GMAIL_PUBSUB_TOPIC":  _env_set("GMAIL_PUBSUB_TOPIC"),
    }
    missing = [k for k, v in optional.items() if not v]
    if not missing:
        return _result("ok", "All optional integrations configured")
    return _result(
        "warn",
        f"{len(missing)} integration(s) not configured",
        optional,
        fix_hint="Set in Railway → Variables to enable AI, payments, real-time mail.",
    )


def check_channel_layer():
    try:
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is None:
            return _result("warn", "Channel layer not configured (chat WebSockets disabled)")
        backend = layer.__class__.__name__
        return _result("ok", f"Channel layer active · {backend}")
    except Exception as e:
        return _result("fail", f"Channel layer error: {e}"[:160])


def check_media_storage():
    media_root = str(settings.MEDIA_ROOT)
    try:
        os.makedirs(media_root, exist_ok=True)
        test_path = os.path.join(media_root, ".diag_write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.unlink(test_path)
        return _result("ok", "MEDIA_ROOT writable", {"path": media_root})
    except Exception as e:
        return _result("fail", f"Not writable: {e}"[:160], {"path": media_root})


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 2 — STORES
# ═════════════════════════════════════════════════════════════════════════════

def check_stores_overview():
    total = Store.objects.count()
    active = Store.objects.filter(is_active=True).count()
    wc = Store.objects.filter(platform="woocommerce").count()
    shopify = Store.objects.filter(platform="shopify").count()
    if total == 0:
        return _result("warn", "No stores connected yet")
    return _result(
        "ok",
        f"{active}/{total} active · {wc} WooCommerce · {shopify} Shopify",
        {"total": total, "active": active, "woocommerce": wc, "shopify": shopify},
    )


def check_woocommerce_health():
    stores = list(Store.objects.filter(platform="woocommerce", is_active=True)[:30])
    if not stores:
        return _result("ok", "No WooCommerce stores to probe", {"checked": 0})

    def probe(s):
        url = f"{s.store_url.rstrip('/')}/wp-json/wc/v3/"
        import base64
        token = base64.b64encode(f"{s.api_key or ''}:{s.api_secret or ''}".encode()).decode()
        code, _, err = _http_probe(url, headers={"Authorization": f"Basic {token}"}, timeout=4)
        return s, code, err

    failed = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for s, code, err in pool.map(probe, stores):
            if not (200 <= code < 300):
                failed.append({"store": s.name, "url": s.store_url, "code": code, "error": err})

    if not failed:
        return _result("ok", f"All {len(stores)} WooCommerce stores reachable")
    if len(failed) == len(stores):
        return _result("fail", f"All {len(stores)} stores unreachable", {"failed": failed})
    return _result(
        "warn",
        f"{len(failed)}/{len(stores)} stores unreachable",
        {"failed": failed},
        fix_hint="Check store URL + API key/secret in each tenant's settings.",
    )


def check_shopify_health():
    stores = list(Store.objects.filter(platform="shopify", is_active=True)[:30])
    if not stores:
        return _result("ok", "No Shopify stores to probe", {"checked": 0})

    def probe(s):
        url = f"{s.store_url.rstrip('/')}/admin/api/2024-01/shop.json"
        headers = {}
        if s.access_token:
            headers["X-Shopify-Access-Token"] = s.access_token
        code, _, err = _http_probe(url, headers=headers, timeout=4)
        return s, code, err

    failed = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for s, code, err in pool.map(probe, stores):
            if not (200 <= code < 300):
                failed.append({"store": s.name, "code": code, "error": err})

    if not failed:
        return _result("ok", f"All {len(stores)} Shopify stores reachable")
    return _result(
        "warn" if len(failed) < len(stores) else "fail",
        f"{len(failed)}/{len(stores)} stores unreachable",
        {"failed": failed},
        fix_hint="Verify access token / store URL.",
    )


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 3 — ORDERS
# ═════════════════════════════════════════════════════════════════════════════

def check_recent_orders():
    now = timezone.now()
    last_24h = Order.objects.filter(created_at__gte=now - timedelta(hours=24)).count()
    last_7d  = Order.objects.filter(created_at__gte=now - timedelta(days=7)).count()
    total    = Order.objects.count()
    if total == 0:
        return _result("warn", "No orders ingested yet")
    if last_24h == 0 and last_7d == 0 and total > 0:
        return _result(
            "warn",
            "No orders in last 7 days — webhooks may be down",
            {"last_24h": 0, "last_7d": 0, "total": total},
            fix_hint="Confirm WooCommerce/Shopify webhooks point at production URL.",
        )
    return _result(
        "ok",
        f"{last_24h} in last 24h · {last_7d} in last 7d · {total} all-time",
        {"last_24h": last_24h, "last_7d": last_7d, "total": total},
    )


def check_stuck_orders():
    cutoff = timezone.now() - timedelta(hours=48)
    stuck = Order.objects.filter(
        vendor_status="unassigned",
        assigned_vendor__isnull=True,
        created_at__lt=cutoff,
    ).count()
    if stuck == 0:
        return _result("ok", "No orders stuck without a vendor")
    if stuck < 5:
        return _result(
            "warn",
            f"{stuck} order(s) unassigned > 48h",
            {"count": stuck},
            fix_hint="Tenants should review the Orders page and assign vendors.",
        )
    return _result(
        "fail",
        f"{stuck} orders stuck without a vendor for > 48h",
        {"count": stuck},
        fix_hint="Set up Permanent Product Assignments to auto-route future orders.",
    )


def check_tracking_scraper():
    env_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "")
    found = None
    if env_path and os.path.exists(env_path):
        found = env_path
    else:
        for p in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/local/bin/chromium"):
            if os.path.exists(p):
                found = p
                break
    try:
        import playwright  # noqa: F401
        pw_installed = True
    except Exception:
        pw_installed = False
    if found and pw_installed:
        return _result("ok", f"Chromium ready · {found}", {"chromium": found, "playwright": True})
    if not pw_installed:
        return _result(
            "fail",
            "Playwright not installed",
            fix_hint="`pip install playwright` and `playwright install chromium`.",
        )
    return _result(
        "fail",
        "Chromium binary not found on this host",
        fix_hint="Install chromium or set PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH.",
    )


def check_tracking_queue():
    pending = VendorTrackingSubmission.objects.filter(status="pending").count()
    rejected = VendorTrackingSubmission.objects.filter(status="rejected").count()
    if pending == 0:
        return _result("ok", "No pending tracking submissions", {"rejected_total": rejected})
    if pending > 50:
        return _result(
            "warn",
            f"{pending} tracking submissions pending review",
            {"pending": pending, "rejected_total": rejected},
            fix_hint="Tenants should review the Tracking Approval Queue. "
                     "Consider enabling auto-approve per store.",
        )
    return _result("ok", f"{pending} pending · {rejected} historically rejected",
                   {"pending": pending, "rejected_total": rejected})


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 4 — EMAIL / GMAIL
# ═════════════════════════════════════════════════════════════════════════════

def check_email_accounts():
    total = EmailAccount.objects.count()
    active = EmailAccount.objects.filter(is_active=True).count()
    oauth = EmailAccount.objects.filter(auth_type="oauth").count()
    if total == 0:
        return _result("warn", "No email inboxes connected by any tenant")
    return _result(
        "ok",
        f"{active}/{total} active · {oauth} via OAuth",
        {"total": total, "active": active, "oauth": oauth},
    )


def check_gmail_watch_expirations():
    now = timezone.now()
    soon_cutoff = now + timedelta(hours=24)
    expiring = EmailAccount.objects.filter(
        auth_type="oauth",
        gmail_watch_expiration__isnull=False,
        gmail_watch_expiration__lt=soon_cutoff,
    ).count()
    expired = EmailAccount.objects.filter(
        auth_type="oauth",
        gmail_watch_expiration__isnull=False,
        gmail_watch_expiration__lt=now,
    ).count()
    if expired:
        return _result(
            "fail",
            f"{expired} Gmail watch(es) expired · {expiring} expiring in 24h",
            {"expired": expired, "expiring_24h": expiring},
            fix_hint="Run `python manage.py renew_gmail_watches` or wait for "
                     "the cron to run.",
        )
    if expiring:
        return _result(
            "warn",
            f"{expiring} Gmail watch(es) expiring in next 24h",
            {"expiring_24h": expiring},
            fix_hint="The scheduled renewal job should handle this automatically.",
        )
    return _result("ok", "All Gmail watches healthy")


def check_gmail_pubsub_config():
    topic = settings.GMAIL_PUBSUB_TOPIC
    sa = settings.GMAIL_PUBSUB_SA
    if topic and sa:
        return _result(
            "ok",
            "Real-time Gmail push configured",
            {"topic": topic, "service_account": sa},
        )
    if not topic and not sa:
        return _result(
            "warn",
            "Pub/Sub not configured — falls back to 60s polling",
            fix_hint="Set GMAIL_PUBSUB_TOPIC + GMAIL_PUBSUB_SA env vars.",
        )
    return _result(
        "warn",
        "Pub/Sub partially configured",
        {"topic": topic, "service_account": sa},
        fix_hint="Both GMAIL_PUBSUB_TOPIC and GMAIL_PUBSUB_SA must be set.",
    )


def check_smtp_fallback():
    host = settings.EMAIL_HOST
    user = settings.EMAIL_HOST_USER
    pwd  = settings.EMAIL_HOST_PASSWORD
    if user and pwd and host:
        return _result(
            "ok",
            f"SMTP configured · {host} as {user}",
            {"host": host, "user": user, "port": settings.EMAIL_PORT},
        )
    return _result(
        "warn",
        "SMTP fallback not fully configured",
        fix_hint="Set EMAIL_HOST_USER + EMAIL_HOST_PASSWORD for system emails.",
    )


def check_inbox_sync_freshness():
    cutoff = timezone.now() - timedelta(hours=1)
    stale = EmailAccount.objects.filter(
        is_active=True,
        last_synced__isnull=False,
        last_synced__lt=cutoff,
    ).count()
    never = EmailAccount.objects.filter(is_active=True, last_synced__isnull=True).count()
    if stale == 0 and never == 0:
        return _result("ok", "All active inboxes synced recently")
    if stale + never > 5:
        return _result(
            "warn",
            f"{stale} inbox(es) not synced in last hour · {never} never synced",
            {"stale_1h": stale, "never_synced": never},
            fix_hint="Check Gmail OAuth tokens and Pub/Sub subscription.",
        )
    return _result(
        "ok",
        f"{stale} slightly stale · {never} never synced",
        {"stale_1h": stale, "never_synced": never},
    )


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 5 — EMAIL TEMPLATES
# ═════════════════════════════════════════════════════════════════════════════

# Pattern that catches the specific corruption left by the old broken
# Visual Editor save (style attribute leaking into anchor inner text).
_CORRUPT_TAG_RE = re.compile(
    r'<(a|p|td|div|span)\b[^>]*>[^<]{1,200}"\s+style="[^"<]+"[^<]*</\1>',
    re.IGNORECASE,
)


def check_templates_overview():
    total = EmailTemplate.objects.count()
    active = EmailTemplate.objects.filter(status="active").count()
    if total == 0:
        return _result("warn", "No templates created yet")
    return _result(
        "ok",
        f"{active}/{total} active",
        {"total": total, "active": active},
    )


def check_template_category_defaults():
    """For each store with templates, verify each major category has a
    template marked as default. Categories with zero default mean
    Auto-Email-on-Status-Change cannot fire for that category."""
    critical = ["order", "shipping", "cancelled", "failed"]
    stores_with_templates = Store.objects.filter(email_templates__isnull=False).distinct()
    gaps = []
    for store in stores_with_templates:
        for cat in critical:
            has = EmailTemplate.objects.filter(
                store=store, category=cat, is_category_default=True
            ).exists()
            if not has:
                gaps.append({"store_id": store.id, "store": store.name, "category": cat})
    if not gaps:
        return _result("ok", "Every store has defaults for critical categories")
    return _result(
        "warn",
        f"{len(gaps)} category default(s) missing across {len(set(g['store_id'] for g in gaps))} store(s)",
        {"missing": gaps[:25]},
        fix_hint="In Email Templates → mark a default per category so "
                 "Auto-Email-on-Status-Change can fire.",
    )


def check_corrupted_templates():
    """Scan EmailTemplate.body_html for the broken anchor/attribute leak
    left by the old Visual Editor index-pairing save."""
    suspect = []
    for t in EmailTemplate.objects.only("id", "name", "store_id", "body_html").iterator():
        body = t.body_html or ""
        if not body:
            continue
        if _CORRUPT_TAG_RE.search(body):
            suspect.append({"id": t.id, "name": t.name, "store_id": t.store_id})
    if not suspect:
        return _result("ok", "No corrupted templates detected")
    return _result(
        "fail" if len(suspect) > 5 else "warn",
        f"{len(suspect)} template(s) have corrupted markup (anchor/style leak)",
        {"affected": suspect[:50]},
        fix_hint='Open each template and click "Reset to Default" to '
                 "restore the seed body_html. Recent Visual Editor fix "
                 "prevents new corruption.",
    )


def check_auto_email_coverage():
    enabled = EmailAccount.objects.filter(auto_email_enabled=True).count()
    total = EmailAccount.objects.count()
    if total == 0:
        return _result("ok", "No email accounts (skipped)")
    if enabled == 0:
        return _result(
            "warn",
            f"Auto-email disabled on all {total} inbox(es)",
            fix_hint="Tenants can enable Auto-Email-on-Status-Change in Email Settings.",
        )
    return _result("ok", f"{enabled}/{total} inbox(es) auto-emailing on status change")


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 6 — VENDORS
# ═════════════════════════════════════════════════════════════════════════════

def check_vendors_overview():
    total = Vendor.objects.count()
    active = Vendor.objects.filter(status="active").count()
    if total == 0:
        return _result("warn", "No vendors created yet")
    return _result("ok", f"{active}/{total} active", {"total": total, "active": active})


def check_vendor_user_linkage():
    """A vendor without a User FK can't log into the vendor portal."""
    orphaned = Vendor.objects.filter(user__isnull=True).count()
    if orphaned == 0:
        return _result("ok", "All vendors have login accounts")
    return _result(
        "warn",
        f"{orphaned} vendor(s) without login accounts",
        {"orphaned": orphaned},
        fix_hint="Send an invitation from the vendor row's actions menu.",
    )


def check_pending_vendor_invitations():
    pending = VendorInvitation.objects.filter(status="pending")
    count = pending.count()
    if count == 0:
        return _result("ok", "No pending vendor invitations")
    cutoff = timezone.now() - timedelta(days=7)
    stale = pending.filter(created_at__lt=cutoff).count()
    if stale:
        return _result(
            "warn",
            f"{count} pending · {stale} sent > 7 days ago",
            {"pending": count, "stale_7d": stale},
            fix_hint="Resend or expire stale invitations.",
        )
    return _result("ok", f"{count} pending invitation(s)", {"pending": count})


def check_product_vendor_assignments():
    count = ProductVendorAssignment.objects.filter(is_active=True).count()
    if count == 0:
        return _result(
            "warn",
            "No permanent product→vendor assignments",
            fix_hint="Permanent assignments auto-route new orders to the right vendor.",
        )
    return _result("ok", f"{count} permanent product→vendor mapping(s) active")


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 7 — TEAM
# ═════════════════════════════════════════════════════════════════════════════

def check_team_overview():
    total = TeamMember.objects.count()
    active = TeamMember.objects.filter(is_active=True).count()
    if total == 0:
        return _result("warn", "No team members created yet")
    return _result("ok", f"{active}/{total} active", {"total": total, "active": active})


def check_pending_employee_invitations():
    pending = EmployeeInvitation.objects.filter(status="pending")
    count = pending.count()
    if count == 0:
        return _result("ok", "No pending employee invitations")
    cutoff = timezone.now() - timedelta(days=7)
    stale = pending.filter(created_at__lt=cutoff).count()
    if stale:
        return _result(
            "warn",
            f"{count} pending · {stale} sent > 7 days ago",
            {"pending": count, "stale_7d": stale},
        )
    return _result("ok", f"{count} pending invitation(s)", {"pending": count})


def check_assignment_rules():
    total = AssignmentRule.objects.filter(is_active=True).count()
    if total == 0:
        return _result(
            "warn",
            "No active assignment rules — orders won't auto-route to team",
            fix_hint="Tenants can set rules at /dashboard/ → Team → Rules.",
        )
    return _result("ok", f"{total} active rule(s)")


def check_chat_channels():
    count = ChatChannel.objects.filter(is_dm=False).count()
    expected = len(getattr(settings, "CHAT_DEFAULT_CHANNELS", []))
    if count == 0:
        return _result(
            "warn",
            "No team chat channels yet",
            fix_hint="Default channels are seeded on first chat load.",
        )
    return _result("ok", f"{count} channel(s) · expected ≥ {expected}")


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 8 — EXTERNAL APIs
# ═════════════════════════════════════════════════════════════════════════════

def check_openai():
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return _result("warn", "OPENAI_API_KEY not set — AI drafts disabled")
    code, _, err = _http_probe(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=4,
    )
    if 200 <= code < 300:
        return _result("ok", "OpenAI API key valid")
    if code == 401:
        return _result("fail", "OpenAI key rejected (401)", fix_hint="Regenerate key.")
    return _result("warn", f"Could not reach OpenAI · {err or code}")


def check_anthropic():
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return _result("warn", "ANTHROPIC_API_KEY not set")
    code, _, err = _http_probe(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        timeout=4,
    )
    if 200 <= code < 300:
        return _result("ok", "Anthropic API key valid")
    if code == 401 or code == 403:
        return _result("fail", f"Anthropic key rejected ({code})", fix_hint="Regenerate key.")
    return _result("warn", f"Could not reach Anthropic · {err or code}")


def check_stripe():
    key = settings.STRIPE_SECRET_KEY
    if not key:
        return _result("warn", "STRIPE_SECRET_KEY not set — payments disabled")
    code, _, err = _http_probe(
        "https://api.stripe.com/v1/balance",
        headers={"Authorization": f"Bearer {key}"},
        timeout=4,
    )
    if 200 <= code < 300:
        mode = "test" if key.startswith("sk_test") else "live"
        return _result("ok", f"Stripe ready · {mode} mode")
    if code == 401:
        return _result("fail", "Stripe key rejected (401)")
    return _result("warn", f"Could not reach Stripe · {err or code}")


def check_paypal():
    cid = settings.PAYPAL_CLIENT_ID
    secret = settings.PAYPAL_CLIENT_SECRET
    if not (cid and secret):
        return _result("warn", "PayPal credentials not set")
    mode = settings.PAYPAL_MODE
    base = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"
    import base64
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"{base}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            if r.status == 200:
                return _result("ok", f"PayPal ready · {mode}")
            return _result("warn", f"PayPal HTTP {r.status}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return _result("fail", "PayPal credentials rejected")
        return _result("warn", f"PayPal HTTP {e.code}")
    except Exception as e:
        return _result("warn", f"PayPal unreachable · {type(e).__name__}")


def check_resend():
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        return _result("warn", "RESEND_API_KEY not set (transactional email off)")
    code, _, err = _http_probe(
        "https://api.resend.com/api-keys",
        headers={"Authorization": f"Bearer {key}"},
        timeout=4,
    )
    if 200 <= code < 300:
        return _result("ok", "Resend API key valid")
    if code == 401:
        return _result("fail", "Resend key rejected (401)")
    return _result("warn", f"Could not reach Resend · {err or code}")


def check_google_oauth_config():
    cid = settings.GOOGLE_CLIENT_ID
    secret = settings.GOOGLE_CLIENT_SECRET
    redirect = settings.GOOGLE_OAUTH_REDIRECT_URI
    if not (cid and secret):
        return _result(
            "fail",
            "Google OAuth credentials missing — Gmail connect disabled",
            fix_hint="Set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET.",
        )
    if "localhost" in (redirect or "") or "127.0.0.1" in (redirect or ""):
        if not settings.DEBUG:
            return _result(
                "warn",
                "OAuth redirect URI points at localhost in production",
                {"redirect_uri": redirect},
                fix_hint="Set GOOGLE_OAUTH_REDIRECT_URI to production URL.",
            )
    return _result("ok", "Google OAuth configured", {"redirect_uri": redirect})


# ═════════════════════════════════════════════════════════════════════════════
# CATEGORY 9 — TENANT LIFECYCLE
# ═════════════════════════════════════════════════════════════════════════════

def check_tenants_overview():
    by_status = {
        "active":    Tenant.objects.filter(status="active").count(),
        "trial":     Tenant.objects.filter(status="trial").count(),
        "suspended": Tenant.objects.filter(status="suspended").count(),
        "deleted":   Tenant.objects.filter(is_deleted=True).count(),
    }
    total = Tenant.objects.count()
    if total == 0:
        return _result("warn", "No tenants onboarded yet")
    return _result(
        "ok",
        f"{by_status['active']} active · {by_status['trial']} trial · {by_status['suspended']} suspended",
        by_status,
    )


def check_trials_expiring():
    now = timezone.now().date()
    cutoff = now + timedelta(days=7)
    soon = Tenant.objects.filter(
        status="trial",
        trial_ends__gte=now,
        trial_ends__lte=cutoff,
    )
    count = soon.count()
    if count == 0:
        return _result("ok", "No trials expiring in next 7 days")
    return _result(
        "warn",
        f"{count} trial(s) expiring in next 7 days",
        {"tenants": [{"id": t.id, "name": t.name, "expires": t.trial_ends.isoformat()} for t in soon[:20]]},
        fix_hint="Reach out for conversion before expiry.",
    )


def check_failed_payments():
    failed = Subscription.objects.filter(payment_status="failed").select_related("tenant")
    count = failed.count()
    if count == 0:
        return _result("ok", "No failed payments")
    return _result(
        "fail" if count > 2 else "warn",
        f"{count} subscription(s) with failed payment",
        {"tenants": [{"id": s.tenant_id, "name": s.tenant.name, "plan": s.plan} for s in failed[:20]]},
        fix_hint="Contact tenants to update payment method.",
    )


def check_unverified_users():
    """Users created > 24h ago but email-verification token still unused."""
    from .models import EmailVerificationToken
    cutoff = timezone.now() - timedelta(hours=24)
    stale = EmailVerificationToken.objects.filter(
        is_used=False, created_at__lt=cutoff,
    ).count()
    if stale == 0:
        return _result("ok", "No stale unverified accounts")
    return _result(
        "warn",
        f"{stale} account(s) unverified > 24h",
        {"count": stale},
        fix_hint="Tenants can request a resend from /signup/resend-verification/.",
    )


# ═════════════════════════════════════════════════════════════════════════════
# REGISTRY — Order here drives display order in the UI
# ═════════════════════════════════════════════════════════════════════════════

CATEGORIES = [
    ("infrastructure", "Infrastructure", "🧱", [
        ("Database",            check_database),
        ("Migrations",          check_migrations),
        ("DEBUG flag",          check_debug_flag),
        ("Allowed hosts",       check_allowed_hosts),
        ("Required env vars",   check_required_env_vars),
        ("Optional integrations", check_optional_env_vars),
        ("Channel layer",       check_channel_layer),
        ("Media storage",       check_media_storage),
    ]),
    ("stores", "Stores", "🏬", [
        ("Stores overview",      check_stores_overview),
        ("WooCommerce health",   check_woocommerce_health),
        ("Shopify health",       check_shopify_health),
    ]),
    ("orders", "Orders & Tracking", "📦", [
        ("Recent ingestion",     check_recent_orders),
        ("Stuck orders",         check_stuck_orders),
        ("Tracking scraper",     check_tracking_scraper),
        ("Tracking queue",       check_tracking_queue),
    ]),
    ("email", "Email & Gmail", "📧", [
        ("Email accounts",       check_email_accounts),
        ("Gmail watch renewal",  check_gmail_watch_expirations),
        ("Pub/Sub real-time",    check_gmail_pubsub_config),
        ("SMTP fallback",        check_smtp_fallback),
        ("Inbox sync freshness", check_inbox_sync_freshness),
    ]),
    ("templates", "Email Templates", "📝", [
        ("Templates overview",   check_templates_overview),
        ("Category defaults",    check_template_category_defaults),
        ("Corruption scan",      check_corrupted_templates),
        ("Auto-email coverage",  check_auto_email_coverage),
    ]),
    ("vendors", "Vendor Portal", "🤝", [
        ("Vendors overview",          check_vendors_overview),
        ("Vendor login linkage",      check_vendor_user_linkage),
        ("Pending invitations",       check_pending_vendor_invitations),
        ("Product→vendor mappings",   check_product_vendor_assignments),
    ]),
    ("team", "Team & Chat", "👥", [
        ("Team overview",        check_team_overview),
        ("Pending invitations",  check_pending_employee_invitations),
        ("Assignment rules",     check_assignment_rules),
        ("Chat channels",        check_chat_channels),
    ]),
    ("external", "External APIs", "🔌", [
        ("OpenAI",          check_openai),
        ("Anthropic",       check_anthropic),
        ("Stripe",          check_stripe),
        ("PayPal",          check_paypal),
        ("Resend",          check_resend),
        ("Google OAuth",    check_google_oauth_config),
    ]),
    ("tenants", "Tenant Lifecycle", "🧑‍💼", [
        ("Tenants overview",     check_tenants_overview),
        ("Trials expiring",      check_trials_expiring),
        ("Failed payments",      check_failed_payments),
        ("Unverified accounts",  check_unverified_users),
    ]),
]


# ═════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

def run_diagnostics(category_key=None):
    """Run all (or one) category of checks in parallel and aggregate."""
    started = time.monotonic()

    cats = [c for c in CATEGORIES if not category_key or c[0] == category_key]
    if not cats:
        return {"error": f"Unknown category: {category_key}"}

    # Build task list
    tasks = []  # (cat_key, title, fn)
    for cat_key, _title, _icon, checks in cats:
        for title, fn in checks:
            tasks.append((cat_key, title, fn))

    # Run in parallel
    results_by_cat = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_timed, fn, title): (cat_key, title) for (cat_key, title, fn) in tasks}
        for fut in as_completed(futures):
            cat_key, _title = futures[fut]
            results_by_cat.setdefault(cat_key, []).append(fut.result())

    # Preserve registered check order within each category
    out_categories = []
    for cat_key, cat_title, icon, checks in cats:
        order_map = {title: idx for idx, (title, _fn) in enumerate(checks)}
        items = results_by_cat.get(cat_key, [])
        items.sort(key=lambda r: order_map.get(r["title"], 999))
        ok = sum(1 for r in items if r["status"] == "ok")
        warn = sum(1 for r in items if r["status"] == "warn")
        fail = sum(1 for r in items if r["status"] == "fail")
        out_categories.append({
            "key":   cat_key,
            "title": cat_title,
            "icon":  icon,
            "ok":    ok,
            "warn":  warn,
            "fail":  fail,
            "checks": items,
        })

    total_ok   = sum(c["ok"]   for c in out_categories)
    total_warn = sum(c["warn"] for c in out_categories)
    total_fail = sum(c["fail"] for c in out_categories)
    total      = total_ok + total_warn + total_fail
    overall = "fail" if total_fail else ("warn" if total_warn else "ok")

    return {
        "overall":          overall,
        "total":            total,
        "ok":               total_ok,
        "warn":             total_warn,
        "fail":             total_fail,
        "duration_ms":      int((time.monotonic() - started) * 1000),
        "ran_at":           timezone.now().isoformat(),
        "categories":       out_categories,
    }
