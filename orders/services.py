from teamapp.services import auto_assign_order
import requests

# ── WooCommerce / WP REST helper ────────────────────────────────────────────
# Many merchants put their WooCommerce store behind Cloudflare, which
# aggressively challenges `/wp-json/*` requests from plain Python `requests`
# (HTTP 406 / 403 / 503). `cloudscraper` solves the JS challenge + browser
# fingerprint check transparently. Fall back to plain `requests` if the
# library isn't installed (older deploys), and add browser-like headers
# either way so non-Cloudflare WAFs (Wordfence, SiteGround, etc.) also
# stop flagging the call.
try:
    import cloudscraper as _cs
    _WOO_SCRAPER_AVAILABLE = True
except Exception:
    _WOO_SCRAPER_AVAILABLE = False


_WOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def woo_session():
    """Return a session that survives Cloudflare's bot challenges.

    Use this in place of bare `requests` for every WooCommerce REST call so
    merchant stores hidden behind Cloudflare keep working without asking
    them to whitelist our IP or disable Bot Fight Mode. Behaves identically
    to a `requests.Session()` — supports .get / .post / .put / .delete
    with the same kwargs (auth=, params=, json=, timeout=, headers=)."""
    if _WOO_SCRAPER_AVAILABLE:
        s = _cs.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True},
            delay=2,
        )
    else:
        s = requests.Session()
    s.headers.update(_WOO_HEADERS)
    return s

COURIER_URL_TEMPLATES = {
    "yuntrack":       "https://www.yuntrack.com/parcelTracking?id={num}",
    "dhl":            "https://www.dhl.com/en/express/tracking.html?AWB={num}&brand=DHL",
    "fedex":          "https://www.fedex.com/fedextrack/?trknbr={num}",
    "ups":            "https://www.ups.com/track?tracknum={num}",
    "postnl":         "https://jouw.postnl.nl/track-and-trace/{num}",
    "royal mail":     "https://www.royalmail.com/track-your-item#/tracking-results/{num}",
    "usps":           "https://tools.usps.com/go/TrackConfirmAction?tLabels={num}",
    "australia post": "https://auspost.com.au/mypost/track/#/details/{num}",
    "4px":            "https://track.4px.com/#/result/0/{num}",
    "china post":     "https://ems.com.cn/mailquery/parcelQuery?mailNo={num}",
    "cainiao":        "https://global.cainiao.com/detail.htm?mailNo={num}",
    "tcs pakistan":   "https://www.tcs.com.pk/tracking.php?cn={num}",
    "leopards":       "https://www.leopardscourier.com/api/track_n_trace?cn={num}",
}

from .models import Order, OrderActivity
from vendors.models import ProductVendorAssignment


def log_activity(order, activity_type, description, actor=None):
    OrderActivity.objects.create(
        order=order,
        activity_type=activity_type,
        description=description,
        actor=actor,
    )


def apply_vendor_auto_assignment(order):
    if not order.product_id:
        return

    if order.assigned_vendor_id and order.assignment_type == "permanent_auto":
        return  # already auto-assigned, don't override

    # Product-global lookup — same product always goes to same vendor regardless of store
    assignment = ProductVendorAssignment.objects.filter(
        product_id=order.product_id,
        is_active=True
    ).first()

    if assignment:
        order.assigned_vendor = assignment.vendor
        order.assignment_type = "permanent_auto"
        order.vendor_status = "assigned"
        order.save(update_fields=["assigned_vendor", "assignment_type", "vendor_status"])
        log_activity(order, "vendor_assigned",
                     f"Vendor '{assignment.vendor.name}' auto-assigned by product mapping",
                     actor="System")


def _parse_iso_dt(value):
    """Parse an ISO-8601 timestamp from WooCommerce/Shopify into an aware
    datetime, or return None on any failure. WC `date_created_gmt` has no
    Z suffix but is always UTC; WC `date_created` is local; Shopify
    `created_at` is ISO with offset. We normalise everything to UTC."""
    if not value:
        return None
    try:
        from datetime import datetime, timezone as _tz
        s = str(value).strip()
        # WooCommerce returns date_created_gmt without timezone but it's UTC.
        # date_created has no tz info either but is local — caller must
        # prefer _gmt fields when available.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # If no timezone info, assume UTC (caller passed a _gmt field).
        if "+" not in s and "T" in s and s.count("-") <= 2:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _set_order_created_at(order_obj, dt):
    """Patch Order.created_at directly via UPDATE so we bypass
    auto_now_add=True (which otherwise locks the field at INSERT time).

    Idempotent — skips if the existing value already matches to the second."""
    if not dt or not order_obj:
        return
    current = getattr(order_obj, "created_at", None)
    if current and abs((current - dt).total_seconds()) < 2:
        return  # already in sync
    Order.objects.filter(pk=order_obj.pk).update(created_at=dt)
    order_obj.created_at = dt


def process_woocommerce_order(store, item):
    """Parse one WooCommerce order dict and upsert into DB. Returns (order, created)."""
    billing = item.get("billing", {})
    line_items = item.get("line_items", [])
    product_id = str(line_items[0].get("product_id")) if line_items else None
    product_name = line_items[0].get("name") if line_items else None

    new_status = item.get("status", "")

    # Capture old status before update to detect changes
    try:
        existing = Order.objects.get(store=store, external_order_id=str(item.get("id")))
        old_status = existing.fulfillment_status or ""
    except Order.DoesNotExist:
        old_status = None  # Will be created

    order_obj, created = Order.objects.update_or_create(
        store=store,
        external_order_id=str(item.get("id")),
        defaults={
            "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
            "customer_email": billing.get("email"),
            "customer_phone": billing.get("phone"),
            "country": billing.get("country"),
            "city": billing.get("city"),
            "total_price": item.get("total") or 0,
            "currency": item.get("currency", "USD"),
            "payment_status": new_status,
            "fulfillment_status": new_status,
            "tracking_number": "",
            "product_id": product_id,
            "product_name": product_name,
            "raw_data": item,
        }
    )

    # Pin created_at to WC's actual order time, not the moment we synced.
    # Without this, batched syncs land all orders with near-identical
    # created_at (DB insert time) → sort-by-newest shows reverse-WC order.
    wc_dt = _parse_iso_dt(item.get("date_created_gmt") or item.get("date_created"))
    if wc_dt:
        _set_order_created_at(order_obj, wc_dt)

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from {store.platform.title()}",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
            _notify_employee_assigned(order_obj, member)
        apply_vendor_auto_assignment(order_obj)
        _notify_admin_new_order(order_obj)
        if order_obj.assigned_vendor_id:
            _notify_vendor_assigned(order_obj)
        # Fire auto email for the new order's status
        _fire_auto_email(order_obj, new_status)
    elif old_status is not None and new_status.lower() != old_status.lower():
        # Status changed on WooCommerce side — fire auto email
        _fire_auto_email(order_obj, new_status)

    return order_obj, created


def _fire_auto_email(order, new_status):
    try:
        from emails.views import send_auto_status_email
        send_auto_status_email(order, new_status)
    except Exception:
        pass


def _notify_admin_new_order(order):
    """Tell the tenant owner a new order just landed."""
    try:
        from notifications.services import notify
        owner = getattr(order.store, "user", None)
        if not owner:
            return
        customer = order.customer_name or "a customer"
        notify(
            recipient=owner,
            audience="admin",
            category="order",
            priority="high",
            title=f"New order #{order.external_order_id} from {order.store.name}",
            body=f"{customer} ordered for {order.currency} {order.total_price}.",
            action_url=f"/dashboard/?section=orders&order_id={order.id}",
            action_label="View order",
            related_order_id=order.id,
        )
    except Exception:
        pass


def _notify_vendor_assigned(order):
    """Tell the assigned vendor about the new order."""
    try:
        from notifications.services import notify
        vendor_user = getattr(order.assigned_vendor, "user", None)
        if not vendor_user:
            return
        notify(
            recipient=vendor_user,
            audience="vendor",
            category="order",
            priority="high",
            title=f"New order assigned: #{order.external_order_id}",
            body=f"{order.customer_name or 'A customer'} ordered {order.product_name or 'an item'}. "
                 f"Please ship and submit tracking.",
            action_url=f"/vendor/?tab=orders&order_id={order.id}",
            action_label="View",
            related_order_id=order.id,
        )
    except Exception:
        pass


def _notify_employee_assigned(order, member):
    """Tell the auto-assigned team member about the new order."""
    try:
        from notifications.services import notify
        notify(
            recipient=getattr(member, "user", None),
            audience="employee",
            category="order",
            priority="medium",
            title=f"Order #{order.external_order_id} assigned to you",
            body=f"Auto-routed by {member.role.replace('_',' ').title()} rule. "
                 f"Customer: {order.customer_name or '—'}.",
            action_url=f"/employee/?order_id={order.id}",
            action_label="Open",
            related_order_id=order.id,
        )
    except Exception:
        pass


def setup_woocommerce_webhook(store, delivery_url):
    """Register order.created + order.updated webhooks idempotently.

    Self-healing: if a webhook for the topic exists with a stale URL we
    PUT-update it to point at `delivery_url` instead of leaving a dead one.

    Returns a dict so callers can surface real status (not just swallow
    failures silently like the old version did). Shape:
        {
            "ok": bool,                       # at least one topic is now correctly registered
            "registered": ["order.created", ...],  # topics confirmed pointing at us
            "errors":     [{"topic": "order.created", "stage": "create", "code": 401, "body": "..."}],
            "webhook_ids": {"order.created": 12, "order.updated": 13},
        }
    """
    base = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/webhooks"
    auth = (store.api_key, store.api_secret)
    sess = woo_session()
    result = {"ok": False, "registered": [], "errors": [], "webhook_ids": {}}

    # 1. Snapshot existing webhooks so we don't duplicate and so we can
    #    repair stale delivery URLs.
    existing_by_topic = {}  # topic -> {"id", "delivery_url"}
    try:
        existing = sess.get(base, auth=auth, params={"per_page": 100}, timeout=15, verify=False)
        if existing.ok:
            for wh in existing.json():
                if isinstance(wh, dict) and wh.get("topic"):
                    # Keep the most recent (last) entry per topic; WC lets you
                    # have multiple with the same topic, but we only care about ours.
                    existing_by_topic[wh["topic"]] = {
                        "id": wh.get("id"),
                        "delivery_url": (wh.get("delivery_url") or "").rstrip("/"),
                    }
        else:
            result["errors"].append({
                "topic": "*",
                "stage": "list",
                "code": existing.status_code,
                "body": (existing.text or "")[:200],
            })
    except Exception as e:
        result["errors"].append({
            "topic": "*",
            "stage": "list",
            "code": 0,
            "body": f"{type(e).__name__}: {e}"[:200],
        })

    want_url = delivery_url.rstrip("/")

    for topic in ("order.created", "order.updated"):
        existing = existing_by_topic.get(topic)

        # Case A: already registered correctly → nothing to do.
        if existing and existing["delivery_url"] == want_url:
            result["registered"].append(topic)
            result["webhook_ids"][topic] = existing["id"]
            continue

        # Case B: stale URL → PUT-update so we don't pile up duplicate webhooks.
        if existing and existing["id"]:
            try:
                r = sess.put(
                    f"{base}/{existing['id']}",
                    auth=auth,
                    json={"delivery_url": delivery_url, "status": "active"},
                    timeout=15,
                    verify=False,
                )
                if r.ok:
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = existing["id"]
                    continue
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
            except Exception as e:
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
                })
            # Fall through to create as a last resort.

        # Case C: not registered → create.
        payload = {
            "name": f"Drop Sigma {topic.replace('.', ' ').title()}",
            "topic": topic,
            "delivery_url": delivery_url,
            "secret": store.api_secret or "",
            "status": "active",
        }
        try:
            r = sess.post(base, auth=auth, json=payload, timeout=15, verify=False)
            if r.ok:
                wh_id = r.json().get("id")
                result["registered"].append(topic)
                if wh_id:
                    result["webhook_ids"][topic] = wh_id
            else:
                result["errors"].append({
                    "topic": topic, "stage": "create",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
        except Exception as e:
            result["errors"].append({
                "topic": topic, "stage": "create",
                "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
            })

    result["ok"] = bool(result["registered"])
    return result


def _refresh_shopify_token(store) -> bool:
    """Exchange refresh_token for a fresh access_token. Returns True on
    success. Idempotent — safe to call when token is still valid.

    Shopify-2025 issues expiring offline tokens (~24h TTL on access,
    ~60 days on refresh). Without refresh logic, every Shopify store
    silently breaks 24h after install. This is the heart of the
    auto-refresh-on-401 mechanism.

    https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/online-access-tokens
    """
    import logging as _l
    log = _l.getLogger(__name__)

    if not store or store.platform != "shopify":
        return False
    if not store.refresh_token:
        log.warning(
            "Shopify refresh: store %s (%s) has no refresh_token — must reconnect",
            store.id, store.name,
        )
        return False

    from django.conf import settings as _s
    from django.utils import timezone as _tz
    from datetime import timedelta as _td

    shop_host = store.store_url.replace("https://", "").replace("http://", "").rstrip("/")
    try:
        r = requests.post(
            f"https://{shop_host}/admin/oauth/access_token",
            json={
                "client_id":     _s.SHOPIFY_API_KEY,
                "client_secret": _s.SHOPIFY_API_SECRET,
                "refresh_token": store.refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=20,
        )
    except Exception as e:
        log.warning("Shopify refresh request failed for store %s: %s", store.id, e)
        return False

    if not r.ok:
        log.warning(
            "Shopify refresh failed for store %s (HTTP %s): %s",
            store.id, r.status_code, (r.text or "")[:200],
        )
        return False

    data = r.json() or {}
    new_access = data.get("access_token") or ""
    new_refresh = data.get("refresh_token") or store.refresh_token  # may rotate or stay same
    expires_in = data.get("expires_in") or 0
    refresh_expires_in = data.get("refresh_token_expires_in") or 0

    if not new_access:
        log.warning("Shopify refresh: store %s returned no access_token", store.id)
        return False

    store.access_token = new_access
    store.refresh_token = new_refresh
    store.token_expires_at = (_tz.now() + _td(seconds=int(expires_in))) if expires_in else None
    if refresh_expires_in:
        store.refresh_token_expires_at = _tz.now() + _td(seconds=int(refresh_expires_in))
    store.save(update_fields=[
        "access_token", "refresh_token",
        "token_expires_at", "refresh_token_expires_at",
    ])
    log.info("Shopify refresh OK for store %s (%s), new TTL=%ss", store.id, store.name, expires_in)
    return True


def _shopify_session(store):
    """Return (headers, auth) tuple for Shopify API requests.

    Side effect: if the stored access_token is about to expire (within
    60 seconds) AND we have a refresh_token, transparently refresh
    BEFORE returning the headers. This pre-empts the typical
    "request lands 1 sec after token expiry → 401" race.
    """
    # Pre-emptive refresh window — refresh slightly before expiry so
    # in-flight calls always use a valid token.
    try:
        from django.utils import timezone as _tz
        if (store.platform == "shopify"
                and store.token_expires_at
                and store.refresh_token
                and (store.token_expires_at - _tz.now()).total_seconds() < 60):
            _refresh_shopify_token(store)
    except Exception:
        pass

    headers = {"Content-Type": "application/json"}
    if store.access_token:
        headers["X-Shopify-Access-Token"] = store.access_token
        return headers, None
    return headers, (store.api_key, store.api_secret)


def shopify_request(store, method, url, **kwargs):
    """Wrapper for requests.<method>() to a Shopify Admin API URL with
    auto-refresh-on-401. Use this instead of `requests.get/post(...)`
    when calling any Shopify endpoint that uses store.access_token.

    Why: even with pre-emptive refresh in _shopify_session, a token
    could be invalidated server-side (manual revoke, scope change). On
    a 401 we refresh once and retry the request — transparent for
    callers."""
    headers, auth = _shopify_session(store)
    headers.update(kwargs.pop("headers", {}) or {})

    fn = getattr(requests, method.lower())
    resp = fn(url, headers=headers, auth=auth, **kwargs)

    if resp.status_code == 401 and store.platform == "shopify" and store.refresh_token:
        # One-shot refresh + retry. If still 401 after refresh, the
        # refresh_token itself is dead — merchant has to reconnect.
        if _refresh_shopify_token(store):
            headers["X-Shopify-Access-Token"] = store.access_token
            resp = fn(url, headers=headers, auth=auth, **kwargs)

    return resp


def process_shopify_order(store, item):
    """Parse one Shopify order dict and upsert into DB. Returns (order, created)."""
    billing = item.get("billing_address") or {}
    line_items = item.get("line_items", [])
    product_id = str(line_items[0].get("product_id")) if line_items else None
    product_name = line_items[0].get("title") if line_items else None

    fulfillment_status = item.get("fulfillment_status") or item.get("financial_status") or "pending"

    order_obj, created = Order.objects.update_or_create(
        store=store,
        external_order_id=str(item.get("id")),
        defaults={
            "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
            "customer_email": item.get("email") or billing.get("email"),
            "customer_phone": item.get("phone") or billing.get("phone"),
            "country": billing.get("country_code") or billing.get("country"),
            "city": billing.get("city"),
            "total_price": item.get("total_price") or 0,
            "currency": item.get("currency", "USD"),
            "payment_status": item.get("financial_status"),
            "fulfillment_status": fulfillment_status,
            "tracking_number": "",
            "product_id": product_id,
            "product_name": product_name,
            "raw_data": item,
        }
    )

    # Pin created_at to Shopify's actual order time, not the moment we synced
    # (Shopify returns ISO-8601 with offset in `created_at`).
    sh_dt = _parse_iso_dt(item.get("created_at"))
    if sh_dt:
        _set_order_created_at(order_obj, sh_dt)

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from Shopify",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
            _notify_employee_assigned(order_obj, member)
        apply_vendor_auto_assignment(order_obj)
        _notify_admin_new_order(order_obj)
        if order_obj.assigned_vendor_id:
            _notify_vendor_assigned(order_obj)

    return order_obj, created


def setup_shopify_webhook(store, delivery_url):
    """Register the full set of order webhooks in Shopify idempotently.

    Subscribes to orders/create + orders/updated + orders/fulfilled +
    orders/cancelled + orders/paid so the dashboard stays in sync
    regardless of which lifecycle event fires.

    Returns the same dict shape as setup_woocommerce_webhook:
        {ok, registered: [topic,...], errors: [...], webhook_ids: {topic: id}}
    """
    headers, auth = _shopify_session(store)
    base = f"{store.store_url.rstrip('/')}/admin/api/2024-01/webhooks.json"
    topics = [
        "orders/create",
        "orders/updated",
        "orders/fulfilled",
        "orders/cancelled",
        "orders/paid",
    ]
    result = {"ok": False, "registered": [], "errors": [], "webhook_ids": {}}

    # Snapshot existing webhooks once so we don't re-create.
    existing_by_topic = {}  # topic -> (id, address)
    try:
        r = requests.get(base, headers=headers, auth=auth, timeout=15)
        if r.ok:
            for wh in r.json().get("webhooks", []):
                topic = wh.get("topic")
                if topic:
                    existing_by_topic[topic] = (
                        wh.get("id"),
                        (wh.get("address") or "").rstrip("/"),
                    )
        else:
            result["errors"].append({
                "topic": "*", "stage": "list",
                "code": r.status_code, "body": (r.text or "")[:200],
            })
    except Exception as e:
        result["errors"].append({
            "topic": "*", "stage": "list",
            "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
        })

    want_url = delivery_url.rstrip("/")

    for topic in topics:
        existing = existing_by_topic.get(topic)

        # Case A: already registered correctly.
        if existing and existing[1] == want_url:
            result["registered"].append(topic)
            result["webhook_ids"][topic] = existing[0]
            continue

        # Case B: stale URL → PUT-update to point at us.
        if existing and existing[0]:
            try:
                r = requests.put(
                    f"{store.store_url.rstrip('/')}/admin/api/2024-01/webhooks/{existing[0]}.json",
                    headers=headers, auth=auth,
                    json={"webhook": {"id": existing[0], "address": delivery_url, "format": "json"}},
                    timeout=15,
                )
                if r.ok:
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = existing[0]
                    continue
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
            except Exception as e:
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
                })
            # Fall through to create as last resort.

        # Case C: not registered → create.
        payload = {
            "webhook": {
                "topic":   topic,
                "address": delivery_url,
                "format":  "json",
            }
        }
        try:
            response = requests.post(base, headers=headers, auth=auth, json=payload, timeout=15)
            if response.ok:
                wh = response.json().get("webhook") or {}
                if wh.get("id"):
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = wh["id"]
                else:
                    result["errors"].append({
                        "topic": topic, "stage": "create",
                        "code": response.status_code, "body": "missing webhook.id in response",
                    })
            else:
                result["errors"].append({
                    "topic": topic, "stage": "create",
                    "code": response.status_code, "body": (response.text or "")[:200],
                })
        except Exception as e:
            result["errors"].append({
                "topic": topic, "stage": "create",
                "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
            })

    result["ok"] = bool(result["registered"])
    return result


def sync_shopify_orders(store, after=None):
    """Fetch orders from Shopify API and sync. Returns count of new orders created."""
    headers, auth = _shopify_session(store)
    url = f"{store.store_url.rstrip('/')}/admin/api/2024-01/orders.json"
    params = {"limit": 250, "status": "any", "order": "created_at desc"}
    if after:
        params["created_at_min"] = after

    response = requests.get(url, headers=headers, auth=auth, params=params, timeout=30)
    response.raise_for_status()

    count = 0
    for item in response.json().get("orders", []):
        _, created = process_shopify_order(store, item)
        if created:
            count += 1

    from django.utils import timezone
    if hasattr(store, "last_synced"):
        store.last_synced = timezone.now()
        store.save(update_fields=["last_synced"])

    return count


def sync_woocommerce_orders(store, after=None):
    """Fetch orders from WooCommerce API and sync. after=ISO datetime string for incremental sync."""
    url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/orders"
    params = {"per_page": 50, "orderby": "date", "order": "desc"}
    if after:
        params["after"] = after

    response = woo_session().get(url, auth=(store.api_key, store.api_secret), params=params, timeout=30)
    response.raise_for_status()

    count = 0
    for item in response.json():
        _, created = process_woocommerce_order(store, item)
        if created:
            count += 1

    # Update last_synced
    from django.utils import timezone
    if hasattr(store, "last_synced"):
        store.last_synced = timezone.now()
        store.save(update_fields=["last_synced"])

    return count