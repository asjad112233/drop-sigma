from teamapp.services import auto_assign_order
import requests

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

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from {store.platform.title()}",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
        apply_vendor_auto_assignment(order_obj)
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


def setup_woocommerce_webhook(store, delivery_url):
    """Register order.created + order.updated webhooks. Returns (webhook_id, created)."""
    base = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/webhooks"
    auth = (store.api_key, store.api_secret)

    try:
        existing = requests.get(base, auth=auth, params={"per_page": 100}, timeout=15, verify=False)
        existing_topics = {}
        if existing.ok:
            for wh in existing.json():
                if isinstance(wh, dict):
                    existing_topics[wh.get("topic")] = wh.get("delivery_url", "")
    except Exception:
        existing_topics = {}

    last_id, last_created = None, False
    for topic in ("order.created", "order.updated"):
        if existing_topics.get(topic, "").rstrip("/") == delivery_url.rstrip("/"):
            continue  # already registered with correct URL
        payload = {
            "name": f"Drop Sigma {topic.replace('.', ' ').title()}",
            "topic": topic,
            "delivery_url": delivery_url,
            "secret": store.api_secret or "",
            "status": "active",
        }
        try:
            r = requests.post(base, auth=auth, json=payload, timeout=15, verify=False)
            if r.ok:
                last_id = r.json().get("id")
                last_created = True
        except Exception:
            pass

    return last_id, last_created


def _shopify_session(store):
    """Return (headers, auth) tuple for Shopify API requests."""
    headers = {"Content-Type": "application/json"}
    if store.access_token:
        headers["X-Shopify-Access-Token"] = store.access_token
        return headers, None
    return headers, (store.api_key, store.api_secret)


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

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from Shopify",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
        apply_vendor_auto_assignment(order_obj)

    return order_obj, created


def setup_shopify_webhook(store, delivery_url):
    """Register webhook in Shopify if not already present. Returns (webhook_id, created)."""
    headers, auth = _shopify_session(store)
    base = f"{store.store_url.rstrip('/')}/admin/api/2024-01/webhooks.json"

    # Check for existing webhook with same address
    try:
        r = requests.get(base, headers=headers, auth=auth, timeout=15)
        if r.ok:
            for wh in r.json().get("webhooks", []):
                if wh.get("address", "").rstrip("/") == delivery_url.rstrip("/"):
                    return wh["id"], False
    except Exception:
        pass

    payload = {
        "webhook": {
            "topic": "orders/create",
            "address": delivery_url,
            "format": "json",
        }
    }
    try:
        response = requests.post(base, headers=headers, auth=auth, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()["webhook"]["id"], True
    except Exception:
        return None, False


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

    response = requests.get(url, auth=(store.api_key, store.api_secret), params=params, timeout=30)
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