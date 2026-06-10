"""Master Product Catalog — cross-tenant product view.

A unified search-and-filter view over every product across every tenant
store. Powers the `/ops/master-catalog` workspace.

Architecture:
  - `TenantProductCache` is refreshed from each tenant's Shopify/WC API
    (live-fetch, same logic as views_suppliers.api_tenant_store_products).
  - `CrossTenantSKU` is computed in batch — it's the source of truth for
    list/detail responses (so we never re-aggregate per request).
  - Refresh runs via `python manage.py sync_master_catalog` (cron-ready)
    or on demand via POST endpoints below.

All endpoints are ops-only.
"""
from collections import defaultdict
from decimal import Decimal
import logging

import requests as _req

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, Value
from django.db.models.functions import Coalesce, Lower
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from stores.models import Store
from orders.models import Order

from .models import (
    SupplierProduct,
    TenantProductCache, CrossTenantSKU,
)
from .permissions import ops_required
from .views import (
    _parse_body,
    _dec_to_float,
    _iso,
)
from .views_suppliers import (
    _PLATFORM_LABELS,
    _store_has_credentials,
    _demo_catalog,
)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────
DEFAULT_LIMIT = 50
MAX_LIMIT     = 200

SORT_CHOICES = {
    "cross_tenant",
    "most_listings",
    "name_asc",
    "name_desc",
    "retail_asc",
    "retail_desc",
    "newest",
}

SOURCED_CHOICES = {"all", "yes", "no", "single", "multi"}
CROSS_TENANT_CHOICES = {"all", "multi"}


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────
def _norm_sku(sku) -> str:
    return (sku or "").strip().lower()


def _to_decimal_price(v):
    """Best-effort coerce of platform price (string/number) to Decimal."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _platform_label(p: str) -> str:
    return _PLATFORM_LABELS.get(p, (p or "").title())


def _store_listing(cache_row) -> dict:
    """Serialize a single TenantProductCache row as a "listing" entry."""
    store = cache_row.store
    owner = store.user if store else None
    owner_username  = owner.username if owner else ""
    owner_email     = owner.email if owner else ""
    return {
        "store_id":       store.id if store else None,
        "store_name":     store.name if store else "",
        "platform":       store.platform if store else "",
        "platform_label": _platform_label(store.platform if store else ""),
        "owner_username": owner_username,
        "owner_email":    owner_email,
        "retail_price":   _dec_to_float(cache_row.retail_price),
        "currency":       cache_row.currency or "USD",
        "external_id":    cache_row.external_id,
        "image_url":      cache_row.image_url,
        "product_name":   cache_row.product_name,
        "variant_count":  cache_row.variant_count,
    }


def _supplier_link(sp) -> dict:
    return {
        "supplier_id":   sp.supplier_id,
        "supplier_name": sp.supplier.name if sp.supplier_id else "",
        "unit_cost_usd": _dec_to_float(sp.unit_cost_usd),
        "lead_days":     sp.lead_days,
        "moq":           sp.moq,
        "is_preferred":  sp.is_preferred,
    }


def _decimal_avg(values):
    cleaned = [Decimal(str(v)) for v in values if v is not None]
    if not cleaned:
        return None
    return (sum(cleaned) / Decimal(len(cleaned))).quantize(Decimal("0.01"))


def _decimal_min(values):
    cleaned = [Decimal(str(v)) for v in values if v is not None]
    return min(cleaned) if cleaned else None


def _decimal_max(values):
    cleaned = [Decimal(str(v)) for v in values if v is not None]
    return max(cleaned) if cleaned else None


# ─────────────────────────────────────────────────────────────────────
# SYNC ENGINE — shared by management command + POST endpoints
# ─────────────────────────────────────────────────────────────────────
def _fetch_store_products(store, *, allow_demo: bool = False) -> list:
    """Live-fetch a tenant store's products.

    Mirrors `api_tenant_store_products` in views_suppliers.py but returns
    the structured product list directly (no JsonResponse wrap). Each
    product dict shape:
        {product_id, product_name, image_url, sku, price, variants}

    Raises requests.RequestException on transport errors so the caller
    can log + skip the store.
    """
    has_creds = _store_has_credentials(store)
    if not has_creds:
        if allow_demo:
            return _demo_catalog(catalog_skus=set())
        return []

    products = []

    if store.platform == "woocommerce":
        from orders.services import woo_session
        base = (store.store_url or "").rstrip("/")
        auth = (store.api_key, store.api_secret)
        sess = woo_session()
        page = 1
        while True:
            r = sess.get(
                f"{base}/wp-json/wc/v3/products",
                auth=auth,
                params={"per_page": 100, "page": page, "status": "publish"},
                timeout=15,
            )
            if r.status_code != 200:
                if page == 1:
                    raise _req.exceptions.RequestException(
                        f"WooCommerce API returned {r.status_code}"
                    )
                break
            batch = r.json()
            if not batch:
                break
            for p in batch:
                variants = []
                if p.get("type") == "variable":
                    vr = sess.get(
                        f"{base}/wp-json/wc/v3/products/{p['id']}/variations",
                        auth=auth, params={"per_page": 100}, timeout=15,
                    )
                    if vr.status_code == 200:
                        for v in vr.json():
                            color, size = "", ""
                            for attr in v.get("attributes", []):
                                aname = (attr.get("name") or "").lower()
                                aval  = attr.get("option", "") or ""
                                if aname in ("color", "colour"):
                                    color = aval
                                elif aname == "size":
                                    size = aval
                            variants.append({
                                "variant_id":     str(v["id"]),
                                "color":          color,
                                "size":           size,
                                "sku":            v.get("sku", "") or "",
                                "stock_quantity": v.get("stock_quantity") or 0,
                                "price":          v.get("regular_price") or v.get("price") or "",
                            })
                else:
                    variants.append({
                        "variant_id":     str(p["id"]),
                        "color":          "",
                        "size":           "",
                        "sku":            p.get("sku", "") or "",
                        "stock_quantity": p.get("stock_quantity") or 0,
                        "price":          p.get("regular_price") or p.get("price") or "",
                    })
                images = p.get("images", []) or []
                image_url = images[0].get("src", "") if images else ""
                primary_sku = (p.get("sku") or "") or (variants[0]["sku"] if variants else "")
                price = p.get("regular_price") or p.get("price") or ""
                products.append({
                    "product_id":   str(p["id"]),
                    "product_name": p.get("name", "") or "",
                    "image_url":    image_url,
                    "sku":          primary_sku,
                    "price":        price,
                    "variants":     variants,
                })
            if len(batch) < 100:
                break
            page += 1

    elif store.platform == "shopify":
        base = (store.store_url or "").rstrip("/")
        headers = {"X-Shopify-Access-Token": store.access_token} if store.access_token else {}
        r = _req.get(
            f"{base}/admin/api/2024-01/products.json",
            headers=headers, params={"limit": 250, "status": "active"}, timeout=15,
        )
        if r.status_code != 200:
            raise _req.exceptions.RequestException(
                f"Shopify API returned {r.status_code}"
            )
        for p in r.json().get("products", []):
            variants = []
            for v in p.get("variants", []) or []:
                variants.append({
                    "variant_id":     str(v["id"]),
                    "color":          v.get("option1", "") or "",
                    "size":           v.get("option2", "") or "",
                    "sku":            v.get("sku", "") or "",
                    "stock_quantity": v.get("inventory_quantity") or 0,
                    "price":          v.get("price", "") or "",
                })
            images = p.get("images", []) or []
            image_url = images[0].get("src", "") if images else ""
            primary_sku = (variants[0]["sku"] if variants else "") or ""
            price = (variants[0]["price"] if variants else "") or ""
            products.append({
                "product_id":   str(p["id"]),
                "product_name": p.get("title", "") or "",
                "image_url":    image_url,
                "sku":          primary_sku,
                "price":        price,
                "variants":     variants,
            })

    else:
        # Unknown platform — return nothing (caller skips with warning).
        return []

    return products


def _upsert_cache_for_store(store, products: list) -> int:
    """Upsert TenantProductCache rows for one store. Returns count saved."""
    if not products:
        return 0

    seen_external_ids = set()
    saved = 0

    with transaction.atomic():
        for raw in products:
            if not isinstance(raw, dict):
                continue
            external_id = (raw.get("product_id") or "").strip()[:120]
            if not external_id:
                continue
            seen_external_ids.add(external_id)

            sku           = (raw.get("sku") or "").strip()[:120]
            product_name  = (raw.get("product_name") or "").strip()[:400]
            image_url     = (raw.get("image_url") or "").strip()[:600]
            retail        = _to_decimal_price(raw.get("price"))
            variants      = raw.get("variants") or []
            if not isinstance(variants, list):
                variants = []
            variant_count = len(variants)

            clean_variants = []
            for v in variants:
                if not isinstance(v, dict):
                    continue
                clean_variants.append({
                    "sku":            (v.get("sku") or "").strip()[:120],
                    "color":          (v.get("color") or "").strip()[:80],
                    "size":           (v.get("size") or "").strip()[:80],
                    "price":          str(v.get("price") or "").strip()[:32],
                    "stock_quantity": v.get("stock_quantity") or 0,
                })

            TenantProductCache.objects.update_or_create(
                store=store,
                external_id=external_id,
                defaults={
                    "sku":           sku,
                    "product_name":  product_name or "(Untitled)",
                    "image_url":     image_url,
                    "retail_price":  retail,
                    "currency":      "USD",
                    "variant_count": variant_count,
                    "variants":      clean_variants,
                    "is_active":     True,
                },
            )
            saved += 1

        # Mark previously-cached rows that didn't reappear as inactive
        # so deletes propagate without losing history.
        if seen_external_ids:
            TenantProductCache.objects.filter(store=store).exclude(
                external_id__in=seen_external_ids,
            ).update(is_active=False)

    return saved


def _recompute_cross_tenant_skus() -> int:
    """Recompute CrossTenantSKU aggregates from the active cache."""
    qs = (TenantProductCache.objects
          .filter(is_active=True)
          .exclude(sku="")
          .values("sku", "store_id", "product_name", "image_url", "retail_price"))

    by_sku = defaultdict(lambda: {
        "stores":   set(),
        "listings": 0,
        "name":     "",
        "image":    "",
        "retails":  [],
    })
    for row in qs:
        key = _norm_sku(row["sku"])
        if not key:
            continue
        bucket = by_sku[key]
        bucket["stores"].add(row["store_id"])
        bucket["listings"] += 1
        if not bucket["name"]:
            bucket["name"] = row["product_name"] or ""
        if not bucket["image"]:
            bucket["image"] = row["image_url"] or ""
        if row["retail_price"] is not None:
            bucket["retails"].append(row["retail_price"])

    sku_keys = list(by_sku.keys())
    supplier_info = defaultdict(lambda: {"count": 0, "costs": []})
    if sku_keys:
        sp_rows = (SupplierProduct.objects
                   .annotate(sku_norm=Lower("sku"))
                   .filter(sku_norm__in=sku_keys)
                   .values("sku_norm", "unit_cost_usd"))
        for sp in sp_rows:
            si = supplier_info[sp["sku_norm"]]
            si["count"] += 1
            if sp["unit_cost_usd"] is not None:
                si["costs"].append(sp["unit_cost_usd"])

    with transaction.atomic():
        CrossTenantSKU.objects.all().delete()
        objs = []
        for key, bucket in by_sku.items():
            sup = supplier_info.get(key, {"count": 0, "costs": []})
            objs.append(CrossTenantSKU(
                sku=key,
                sample_name=bucket["name"][:400],
                sample_image=bucket["image"][:600],
                tenant_count=len(bucket["stores"]),
                listing_count=bucket["listings"],
                avg_retail_usd=_decimal_avg(bucket["retails"]),
                min_retail_usd=_decimal_min(bucket["retails"]),
                max_retail_usd=_decimal_max(bucket["retails"]),
                has_supplier=sup["count"] > 0,
                supplier_count=sup["count"],
                avg_supplier_cost=_decimal_avg(sup["costs"]),
            ))
        if objs:
            CrossTenantSKU.objects.bulk_create(objs, batch_size=500)
    return len(by_sku)


def run_sync(*, store_id: int = None, allow_demo: bool = False) -> dict:
    """Top-level sync entrypoint shared by management command + POST APIs.

    Returns a dict with sync counters + per-store errors. Always recomputes
    the CrossTenantSKU aggregates at the end so callers always see a
    consistent view.
    """
    qs = Store.objects.select_related("user").filter(is_active=True)
    if store_id:
        qs = qs.filter(pk=store_id)

    stores_total   = qs.count()
    stores_synced  = 0
    stores_failed  = 0
    stores_skipped = 0
    products_saved = 0
    errors = []

    for store in qs:
        has_creds = _store_has_credentials(store)
        if not has_creds and not allow_demo:
            stores_skipped += 1
            continue
        try:
            products = _fetch_store_products(store, allow_demo=allow_demo)
        except _req.exceptions.Timeout:
            stores_failed += 1
            errors.append({
                "store_id": store.id, "store_name": store.name,
                "error": "Store API timed out after 15s.",
            })
            continue
        except _req.exceptions.ConnectionError:
            stores_failed += 1
            errors.append({
                "store_id": store.id, "store_name": store.name,
                "error": "Could not connect to the store API.",
            })
            continue
        except _req.exceptions.RequestException as exc:
            stores_failed += 1
            errors.append({
                "store_id": store.id, "store_name": store.name,
                "error": f"Store API error: {str(exc)[:200]}",
            })
            continue

        saved = _upsert_cache_for_store(store, products)
        products_saved += saved
        stores_synced += 1

    unique_skus = _recompute_cross_tenant_skus()

    return {
        "stores_total":   stores_total,
        "stores_synced":  stores_synced,
        "stores_failed":  stores_failed,
        "stores_skipped": stores_skipped,
        "products_saved": products_saved,
        "unique_skus":    unique_skus,
        "errors":         errors,
    }


# ─────────────────────────────────────────────────────────────────────
# Catalog row assembly — used by list + detail endpoints.
# ─────────────────────────────────────────────────────────────────────
def _filter_cache_for_listings(*, store_id=None, tenant_id=None, platform=None):
    qs = (TenantProductCache.objects
          .filter(is_active=True)
          .select_related("store", "store__user"))
    if store_id:
        qs = qs.filter(store_id=store_id)
    if tenant_id:
        qs = qs.filter(store__user_id=tenant_id)
    if platform:
        qs = qs.filter(store__platform=platform)
    return qs


def _build_catalog_row(cross, *, listings_by_sku, suppliers_by_sku):
    sku_key = cross.sku
    listings = [_store_listing(c) for c in listings_by_sku.get(sku_key, [])]
    suppliers = [_supplier_link(sp) for sp in suppliers_by_sku.get(sku_key, [])]
    sample_image = cross.sample_image or next(
        (l["image_url"] for l in listings if l["image_url"]), ""
    )
    sample_name = cross.sample_name or next(
        (l["product_name"] for l in listings if l["product_name"]), ""
    )
    variant_count = max((l["variant_count"] for l in listings), default=0)
    currency = next((l["currency"] for l in listings if l.get("currency")), "USD")
    return {
        "sku":               cross.sku,
        "product_name":      sample_name,
        "image_url":         sample_image,
        "variant_count":     variant_count,
        "tenant_count":      cross.tenant_count,
        "listing_count":     cross.listing_count,
        "avg_retail_usd":    _dec_to_float(cross.avg_retail_usd),
        "min_retail_usd":    _dec_to_float(cross.min_retail_usd),
        "max_retail_usd":    _dec_to_float(cross.max_retail_usd),
        "currency":          currency,
        "category":          "",
        "has_supplier":      cross.has_supplier,
        "supplier_count":    cross.supplier_count,
        "avg_supplier_cost": _dec_to_float(cross.avg_supplier_cost),
        "listings":          listings,
        "suppliers":         suppliers,
    }


def _build_orphan_row(cache_row):
    """A cache row whose SKU is empty — render it as its own entry."""
    listing = _store_listing(cache_row)
    return {
        "sku":               "",
        "product_name":      cache_row.product_name,
        "image_url":         cache_row.image_url,
        "variant_count":     cache_row.variant_count,
        "tenant_count":      1,
        "listing_count":     1,
        "avg_retail_usd":    _dec_to_float(cache_row.retail_price),
        "min_retail_usd":    _dec_to_float(cache_row.retail_price),
        "max_retail_usd":    _dec_to_float(cache_row.retail_price),
        "currency":          cache_row.currency or "USD",
        "category":          cache_row.category or "",
        "has_supplier":      False,
        "supplier_count":    0,
        "avg_supplier_cost": None,
        "listings":          [listing],
        "suppliers":         [],
        "_orphan_key":       f"store{cache_row.store_id}-ext{cache_row.external_id}",
    }


# ─────────────────────────────────────────────────────────────────────
# 1) LIST  (GET /ops/api/master-catalog/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_master_catalog_list(request):
    g = request.GET
    search       = (g.get("search") or "").strip()
    store_id     = _int_or(g.get("store"), 0) or None
    tenant_id    = _int_or(g.get("tenant"), 0) or None
    platform     = (g.get("platform") or "").strip().lower() or None
    sourced      = (g.get("sourced") or "all").strip().lower()
    cross_tenant = (g.get("cross_tenant") or "all").strip().lower()
    sort         = (g.get("sort") or "cross_tenant").strip().lower()
    limit        = _clamp(_int_or(g.get("limit"), DEFAULT_LIMIT), 1, MAX_LIMIT)
    offset       = max(0, _int_or(g.get("offset"), 0))

    if sourced not in SOURCED_CHOICES:
        sourced = "all"
    if cross_tenant not in CROSS_TENANT_CHOICES:
        cross_tenant = "all"
    if sort not in SORT_CHOICES:
        sort = "cross_tenant"
    if platform and platform not in ("shopify", "woocommerce"):
        platform = None

    cache_qs = _filter_cache_for_listings(
        store_id=store_id, tenant_id=tenant_id, platform=platform,
    )

    has_store_filter = bool(store_id or tenant_id or platform)
    filtered_sku_set = None
    if has_store_filter:
        filtered_sku_set = set(
            cache_qs.exclude(sku="")
                    .annotate(sku_norm=Lower("sku"))
                    .values_list("sku_norm", flat=True)
                    .distinct()
        )

    cross_qs = CrossTenantSKU.objects.all()
    if filtered_sku_set is not None:
        cross_qs = cross_qs.filter(sku__in=filtered_sku_set)
    if search:
        cross_qs = cross_qs.filter(
            Q(sku__icontains=search) | Q(sample_name__icontains=search)
        )
    if sourced == "yes":
        cross_qs = cross_qs.filter(has_supplier=True)
    elif sourced == "no":
        cross_qs = cross_qs.filter(has_supplier=False)
    elif sourced == "single":
        cross_qs = cross_qs.filter(supplier_count=1)
    elif sourced == "multi":
        cross_qs = cross_qs.filter(supplier_count__gte=2)
    if cross_tenant == "multi":
        cross_qs = cross_qs.filter(tenant_count__gte=2)

    if sort == "most_listings":
        cross_qs = cross_qs.order_by("-listing_count", "-tenant_count", "sku")
    elif sort == "name_asc":
        cross_qs = cross_qs.order_by("sample_name", "sku")
    elif sort == "name_desc":
        cross_qs = cross_qs.order_by("-sample_name", "sku")
    elif sort == "retail_asc":
        cross_qs = cross_qs.order_by(
            Coalesce("avg_retail_usd", Value(Decimal("999999999"))), "sku",
        )
    elif sort == "retail_desc":
        cross_qs = cross_qs.order_by(
            Coalesce("avg_retail_usd", Value(Decimal("-1"))).desc(), "sku",
        )
    elif sort == "newest":
        cross_qs = cross_qs.order_by("-last_computed_at", "sku")
    else:
        cross_qs = cross_qs.order_by("-tenant_count", "-listing_count", "sku")

    # Orphan rows surface only when sourced filter doesn't exclude
    # unsourced and cross_tenant filter doesn't require multi-tenant.
    show_orphans = sourced in ("all", "no") and cross_tenant != "multi"
    orphan_qs = cache_qs.filter(sku="") if show_orphans else TenantProductCache.objects.none()
    if search and show_orphans:
        orphan_qs = orphan_qs.filter(product_name__icontains=search)

    cross_count  = cross_qs.count()
    orphan_count = orphan_qs.count()
    total = cross_count + orphan_count

    rows = []
    if offset < cross_count:
        cross_slice = list(cross_qs[offset:offset + limit])
        sku_keys = [c.sku for c in cross_slice]
        listings_by_sku = defaultdict(list)
        if sku_keys:
            listings_iter = (cache_qs
                             .annotate(sku_norm=Lower("sku"))
                             .filter(sku_norm__in=sku_keys)
                             .order_by("store__name"))
            for c in listings_iter:
                listings_by_sku[_norm_sku(c.sku)].append(c)
        suppliers_by_sku = defaultdict(list)
        if sku_keys:
            sp_iter = (SupplierProduct.objects
                       .select_related("supplier")
                       .annotate(sku_norm=Lower("sku"))
                       .filter(sku_norm__in=sku_keys)
                       .order_by("-is_preferred", "supplier__name"))
            for sp in sp_iter:
                suppliers_by_sku[sp.sku_norm].append(sp)
        for cross in cross_slice:
            rows.append(_build_catalog_row(
                cross,
                listings_by_sku=listings_by_sku,
                suppliers_by_sku=suppliers_by_sku,
            ))

    rows_needed = limit - len(rows)
    if rows_needed > 0 and show_orphans and orphan_count > 0:
        orphan_offset = 0 if rows else max(0, offset - cross_count)
        if orphan_offset < orphan_count:
            orphan_slice = (orphan_qs
                            .select_related("store", "store__user")
                            .order_by("-cached_at", "id"))[orphan_offset:orphan_offset + rows_needed]
            for orow in orphan_slice:
                rows.append(_build_orphan_row(orow))

    return JsonResponse({
        "ok":       True,
        "total":    total,
        "shown":    len(rows),
        "offset":   offset,
        "limit":    limit,
        "products": rows,
    })


# ─────────────────────────────────────────────────────────────────────
# 2) SKU DETAIL  (GET /ops/api/master-catalog/sku/<sku>/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_master_catalog_sku(request, sku):
    key = _norm_sku(sku)
    if not key:
        return JsonResponse(
            {"ok": False, "error": "SKU is required."}, status=400,
        )

    try:
        cross = CrossTenantSKU.objects.get(sku=key)
    except CrossTenantSKU.DoesNotExist:
        return JsonResponse(
            {"ok": False, "error": "SKU not in master catalog."}, status=404,
        )

    listings_cache = list(
        TenantProductCache.objects
        .filter(is_active=True)
        .annotate(sku_norm=Lower("sku"))
        .filter(sku_norm=key)
        .select_related("store", "store__user")
        .order_by("store__name")
    )
    suppliers = list(
        SupplierProduct.objects
        .select_related("supplier")
        .annotate(sku_norm=Lower("sku"))
        .filter(sku_norm=key)
        .order_by("-is_preferred", "supplier__name")
    )

    listings_by_sku  = defaultdict(list, {key: listings_cache})
    suppliers_by_sku = defaultdict(list, {key: suppliers})

    row = _build_catalog_row(
        cross,
        listings_by_sku=listings_by_sku,
        suppliers_by_sku=suppliers_by_sku,
    )

    variants_detail = []
    seen_variant_keys = set()
    for c in listings_cache:
        for v in (c.variants or []):
            vkey = (v.get("sku") or "") + "|" + (v.get("color") or "") + "|" + (v.get("size") or "")
            if vkey in seen_variant_keys:
                continue
            seen_variant_keys.add(vkey)
            variants_detail.append({
                "store_id":       c.store_id,
                "store_name":     c.store.name if c.store_id else "",
                "sku":            v.get("sku") or "",
                "color":          v.get("color") or "",
                "size":           v.get("size") or "",
                "price":          v.get("price") or "",
                "stock_quantity": v.get("stock_quantity") or 0,
            })

    # Past orders with this SKU — the Order model has no `product_sku`
    # column, so we look up the SKU inside raw_data (where line_items
    # live) and fall back to a name match. Best-effort only.
    total_orders = 0
    recent_orders = []
    try:
        order_qs = (Order.objects
                    .select_related("store")
                    .filter(
                        Q(raw_data__icontains=cross.sku)
                        | (Q(product_name__icontains=cross.sample_name)
                           if cross.sample_name else Q(pk=-1))
                    )
                    .order_by("-created_at"))
        total_orders = order_qs.count()
        for o in order_qs[:10]:
            recent_orders.append({
                "id":                o.id,
                "external_order_id": o.external_order_id,
                "ds_order_ref":      o.ds_order_ref,
                "store_id":          o.store_id,
                "store_name":        o.store.name if o.store_id else "",
                "customer_name":     o.customer_name or "",
                "country":           o.country or "",
                "total_price":       _dec_to_float(o.total_price),
                "currency":          o.currency or "USD",
                "sourcing_status":   o.sourcing_status,
                "created_at_iso":    _iso(o.created_at),
            })
    except Exception as exc:  # noqa: BLE001 — orders lookup is non-critical
        logger.warning("master-catalog SKU detail: orders query failed: %s", exc)

    row["variants_detail"]   = variants_detail
    row["total_orders"]      = total_orders
    row["recent_orders"]     = recent_orders
    row["last_computed_at"]  = _iso(cross.last_computed_at)

    return JsonResponse({"ok": True, "product": row})


# ─────────────────────────────────────────────────────────────────────
# 3) STATS  (GET /ops/api/master-catalog/stats/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_master_catalog_stats(request):
    total_products    = TenantProductCache.objects.filter(is_active=True).count()
    unique_skus       = CrossTenantSKU.objects.count()
    sourced_skus      = CrossTenantSKU.objects.filter(has_supplier=True).count()
    multi_source_skus = CrossTenantSKU.objects.filter(supplier_count__gte=2).count()
    cross_tenant_skus = CrossTenantSKU.objects.filter(tenant_count__gte=2).count()
    high_volume_unsourced = (CrossTenantSKU.objects
                             .filter(has_supplier=False, tenant_count__gte=2)
                             .count())
    coverage_pct = round((sourced_skus / unique_skus * 100), 1) if unique_skus else 0.0

    stores_synced = (TenantProductCache.objects
                     .values("store_id").distinct().count())
    last_synced = (TenantProductCache.objects
                   .order_by("-cached_at")
                   .values_list("cached_at", flat=True).first())

    return JsonResponse({
        "ok":                    True,
        "total_products":        total_products,
        "unique_skus":           unique_skus,
        "sourced_skus":          sourced_skus,
        "coverage_pct":          coverage_pct,
        "multi_source_skus":     multi_source_skus,
        "cross_tenant_skus":     cross_tenant_skus,
        "high_volume_unsourced": high_volume_unsourced,
        "last_synced_at":        _iso(last_synced),
        "stores_synced":         stores_synced,
        "stores_failed":         0,
    })


# ─────────────────────────────────────────────────────────────────────
# 4) FILTERS  (GET /ops/api/master-catalog/filters/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_master_catalog_filters(request):
    stores = []
    for s in Store.objects.select_related("user").order_by("name"):
        stores.append({
            "id":       s.id,
            "name":     s.name,
            "platform": s.platform,
        })

    User = get_user_model()
    tenant_rows = (User.objects
                   .filter(store__isnull=False)
                   .annotate(store_count=Count("store", distinct=True))
                   .order_by("username")
                   .distinct())
    tenants = []
    for u in tenant_rows:
        full_name = (u.get_full_name() or u.username).strip()
        tenants.append({
            "id":          u.id,
            "username":    u.username,
            "full_name":   full_name,
            "store_count": u.store_count,
        })

    platform_counts = (Store.objects
                       .values("platform")
                       .annotate(count=Count("id"))
                       .order_by("platform"))
    platforms = [
        {
            "key":   row["platform"] or "",
            "label": _platform_label(row["platform"] or ""),
            "count": row["count"],
        }
        for row in platform_counts if row["platform"]
    ]

    return JsonResponse({
        "ok":        True,
        "stores":    stores,
        "tenants":   tenants,
        "platforms": platforms,
    })


# ─────────────────────────────────────────────────────────────────────
# 5) SYNC ALL  (POST /ops/api/master-catalog/sync/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_master_catalog_sync(request):
    body = _parse_body(request)
    allow_demo = bool(body.get("demo"))
    result = run_sync(allow_demo=allow_demo)
    return JsonResponse({
        "ok":             True,
        "synced":         result["stores_synced"],
        "failed":         result["stores_failed"],
        "skipped":        result["stores_skipped"],
        "products_saved": result["products_saved"],
        "unique_skus":    result["unique_skus"],
        "errors":         result["errors"],
    })


# ─────────────────────────────────────────────────────────────────────
# 6) SYNC ONE STORE  (POST /ops/api/master-catalog/sync-store/<id>/)
# ─────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_master_catalog_sync_store(request, store_id):
    try:
        Store.objects.get(pk=store_id)
    except Store.DoesNotExist:
        return JsonResponse(
            {"ok": False, "error": "Store not found."}, status=404,
        )
    body = _parse_body(request)
    allow_demo = bool(body.get("demo"))
    result = run_sync(store_id=store_id, allow_demo=allow_demo)
    return JsonResponse({
        "ok":             True,
        "store_id":       store_id,
        "synced":         result["stores_synced"],
        "failed":         result["stores_failed"],
        "skipped":        result["stores_skipped"],
        "products_saved": result["products_saved"],
        "unique_skus":    result["unique_skus"],
        "errors":         result["errors"],
    })
