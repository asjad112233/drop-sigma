"""Product synchronisation helpers.

The HTTP views in stock/views.py already know how to fetch a store's catalog
and import it into StockProduct/StockVariant. These helpers extract that
logic so it can be invoked from other places — most importantly the
post_save signal on Store, so newly-connected stores auto-populate the
Products section without the tenant having to click anything.

All functions are best-effort: they swallow per-product exceptions so a
single bad row doesn't abort the entire sync, and they always return a
counts dict instead of raising.
"""
import logging
import threading
import requests as _req
from django.db import transaction
from .models import StockProduct, StockVariant, StockEntry, StockAuditLog

logger = logging.getLogger(__name__)


def fetch_store_products(store):
    """Pull the full active product catalog from the connected store API.

    Returns a list of dicts: {product_id, product_name, image_url, variants}.
    Returns [] on auth / network errors (errors are logged, never raised).
    """
    out = []
    try:
        if store.platform == "woocommerce":
            base = (store.store_url or "").rstrip("/")
            if not base or not store.api_key or not store.api_secret:
                return []
            auth = (store.api_key, store.api_secret)
            page = 1
            while True:
                r = _req.get(
                    f"{base}/wp-json/wc/v3/products",
                    auth=auth,
                    params={"per_page": 100, "page": page, "status": "publish"},
                    timeout=20,
                )
                if r.status_code != 200:
                    break
                batch = r.json()
                if not batch:
                    break
                for p in batch:
                    variants = []
                    if p.get("type") == "variable":
                        try:
                            vr = _req.get(
                                f"{base}/wp-json/wc/v3/products/{p['id']}/variations",
                                auth=auth, params={"per_page": 100}, timeout=20,
                            )
                            if vr.status_code == 200:
                                for v in vr.json():
                                    color, size = "", ""
                                    for attr in v.get("attributes", []):
                                        name = (attr.get("name") or "").lower()
                                        val = attr.get("option", "")
                                        if name in ("color", "colour"):
                                            color = val
                                        elif name == "size":
                                            size = val
                                    variants.append({
                                        "variant_id": str(v["id"]),
                                        "color": color, "size": size,
                                        "sku": v.get("sku") or "",
                                        "stock_quantity": v.get("stock_quantity") or 0,
                                    })
                        except Exception:
                            logger.exception("woo variant fetch failed for %s", p.get("id"))
                    else:
                        variants.append({
                            "variant_id": str(p["id"]),
                            "color": "", "size": "",
                            "sku": p.get("sku") or "",
                            "stock_quantity": p.get("stock_quantity") or 0,
                        })
                    images = p.get("images") or []
                    image_url = images[0].get("src", "") if images else ""
                    out.append({
                        "product_id": str(p["id"]),
                        "product_name": p.get("name", "") or "",
                        "image_url": image_url,
                        "variants": variants,
                    })
                if len(batch) < 100:
                    break
                page += 1

        elif store.platform == "shopify":
            base = (store.store_url or "").rstrip("/")
            if not base or not getattr(store, "access_token", ""):
                return []
            headers = {"X-Shopify-Access-Token": store.access_token}
            # Shopify paginates with page_info links; do up to 8 pages × 250 = 2,000 products
            url = f"{base}/admin/api/2024-01/products.json?limit=250&status=active"
            for _ in range(8):
                r = _req.get(url, headers=headers, timeout=20)
                if r.status_code != 200:
                    break
                for p in r.json().get("products", []):
                    variants = []
                    for v in p.get("variants", []):
                        variants.append({
                            "variant_id": str(v["id"]),
                            "color": v.get("option1", "") or "",
                            "size": v.get("option2", "") or "",
                            "sku": v.get("sku") or "",
                            "stock_quantity": v.get("inventory_quantity") or 0,
                        })
                    images = p.get("images") or []
                    image_url = images[0].get("src", "") if images else ""
                    out.append({
                        "product_id": str(p["id"]),
                        "product_name": p.get("title", "") or "",
                        "image_url": image_url,
                        "variants": variants,
                    })
                # Look for next-page link header
                link = r.headers.get("Link", "") or r.headers.get("link", "")
                next_url = None
                if 'rel="next"' in link:
                    for part in link.split(","):
                        if 'rel="next"' in part:
                            seg = part.split(";")[0].strip()
                            if seg.startswith("<") and seg.endswith(">"):
                                next_url = seg[1:-1]
                                break
                if not next_url:
                    break
                url = next_url

    except Exception:
        logger.exception("fetch_store_products failed for store id=%s", getattr(store, "id", "?"))

    return out


def import_store_products(store, products):
    """Bulk-create StockProduct rows (and variant skeletons) for the given products.

    Idempotent: existing products are reused (image_url is refreshed if it was
    missing). Returns {created, updated, total}.
    """
    created_n = 0
    updated_n = 0
    total = 0
    try:
        with transaction.atomic():
            for item in products:
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue
                total += 1
                pname = (item.get("product_name") or pid).strip()
                image_url = (item.get("image_url") or "").strip()
                product, created = StockProduct.objects.get_or_create(
                    store=store, product_id=pid,
                    defaults={"product_name": pname, "image_url": image_url},
                )
                if created:
                    created_n += 1
                else:
                    fields_to_update = []
                    # Refresh image if it was previously empty
                    if image_url and not product.image_url:
                        product.image_url = image_url
                        fields_to_update.append("image_url")
                    # Refresh name if it changed upstream
                    if pname and product.product_name != pname:
                        product.product_name = pname
                        fields_to_update.append("product_name")
                    if fields_to_update:
                        product.save(update_fields=fields_to_update)
                        updated_n += 1
                # Variants: only create skeletons (no stock movements).
                for v in item.get("variants") or []:
                    color = (v.get("color") or "").strip()
                    size = (v.get("size") or "").strip()
                    sku = (v.get("sku") or "").strip()
                    variant, vcreated = StockVariant.objects.get_or_create(
                        product=product, color=color, size=size,
                        defaults={"sku": sku},
                    )
                    StockEntry.objects.get_or_create(variant=variant)
                    if vcreated:
                        try:
                            StockAuditLog.objects.create(
                                variant=variant, action="sync",
                                qty_before=0, qty_after=0,
                                actor=None, note="Auto-synced on store connect",
                            )
                        except Exception:
                            pass
    except Exception:
        logger.exception("import_store_products failed for store id=%s", getattr(store, "id", "?"))
    return {"created": created_n, "updated": updated_n, "total": total}


def auto_sync_store_products(store, async_=True):
    """Fetch + import products for a newly-connected store.

    By default runs in a background thread so the request that created the
    store returns immediately. Set async_=False for synchronous use (tests).
    """
    def _work():
        try:
            products = fetch_store_products(store)
            if not products:
                logger.info("Auto-sync: 0 products for store id=%s (%s) — no credentials or empty catalog",
                            store.id, store.platform)
                return {"created": 0, "updated": 0, "total": 0}
            stats = import_store_products(store, products)
            logger.info("Auto-sync: store id=%s — created=%d updated=%d total=%d",
                        store.id, stats["created"], stats["updated"], stats["total"])
            return stats
        except Exception:
            logger.exception("auto_sync_store_products thread crashed for store id=%s", store.id)
            return {"created": 0, "updated": 0, "total": 0}

    if not async_:
        return _work()
    t = threading.Thread(target=_work, daemon=True, name=f"store-{store.id}-product-sync")
    t.start()
    return {"queued": True}
