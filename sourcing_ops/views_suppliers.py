"""Supplier-related ops views.

Implements the Supplier Database tab in the Drop Sigma operations
portal: list / create / detail / update suppliers, plus per-supplier
SKU catalog (SupplierProduct) management.

The catalog endpoints are how Drop Sigma builds its SKU -> supplier
auto-detect map: once ops links a SKU to a supplier here with locked
pricing, the Quote workspace pre-fills the supplier choice the next
time the same SKU comes through.
"""
from decimal import Decimal

import requests as _req

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from stores.models import Store

from .models import (
    Supplier, SupplierContact, SupplierImage,
    SupplierProduct, SupplierProductVariant,
)
from .permissions import ops_required
from .views import (
    _parse_body,
    _dec,
    _dec_to_float,
    _iso,
    _human_ago,
    _country_flag,
    serialize_supplier,
    serialize_supplier_product,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
_VALID_TIERS = {choice[0] for choice in Supplier.TIER_CHOICES}
_VALID_PAYMENT_TERMS = {choice[0] for choice in Supplier.PAYMENT_TERMS_CHOICES}


def _unique_slug(name: str) -> str:
    """Generate a unique slug from `name`, appending -2, -3... on collision."""
    base = slugify(name or "supplier") or "supplier"
    candidate = base[:50] or "supplier"
    n = 2
    while Supplier.objects.filter(slug=candidate).exists():
        suffix = f"-{n}"
        candidate = (base[: 50 - len(suffix)]) + suffix
        n += 1
        if n > 999:  # paranoia
            break
    return candidate


def _coerce_list(value):
    """Coerce a payload value into a clean list of trimmed strings."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    return []


def _serialize_contact(c):
    return {
        "id":         c.id,
        "name":       c.name,
        "role":       c.role,
        "email":      c.email,
        "phone":      c.phone,
        "whatsapp":   c.whatsapp,
        "is_primary": c.is_primary,
        "notes":      c.notes,
    }


def _serialize_image(im):
    return {
        "id":      im.id,
        "kind":    im.kind,
        "url":     im.url,
        "caption": im.caption,
        "sort":    im.sort_order,
    }


# ---------------------------------------------------------------------
# LIST  (GET /ops/api/suppliers/)
# ---------------------------------------------------------------------
@ops_required
@require_GET
def api_suppliers_list(request):
    """Return all suppliers (optionally filtered by q / tier / country)."""
    q       = (request.GET.get("q") or "").strip()
    tier    = (request.GET.get("tier") or "").strip().lower()
    country = (request.GET.get("country") or "").strip()

    qs = Supplier.objects.all()

    if tier and tier != "all" and tier in _VALID_TIERS:
        qs = qs.filter(tier=tier)
    if country:
        qs = qs.filter(country__iexact=country)

    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(legal_name__icontains=q)
            | Q(contact_name__icontains=q)
            | Q(country__icontains=q)
            | Q(city__icontains=q)
            | Q(email__icontains=q)
            | Q(specialties__icontains=q)
        )

    qs = qs.distinct()
    suppliers = [serialize_supplier(s) for s in qs]

    # Tier facets for the filter chips
    facets = {t: 0 for t in _VALID_TIERS}
    facets["all"] = 0
    for s in Supplier.objects.all():
        facets["all"] += 1
        if s.tier in facets:
            facets[s.tier] += 1

    return JsonResponse({
        "ok": True,
        "suppliers": suppliers,
        "total":     len(suppliers),
        "facets":    facets,
    })


# ---------------------------------------------------------------------
# CREATE  (POST /ops/api/suppliers/create/)
# ---------------------------------------------------------------------
@ops_required
@require_POST
def api_supplier_create(request):
    body = _parse_body(request)

    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Supplier name is required."}, status=400)

    tier = (body.get("tier") or "standard").strip().lower()
    if tier not in _VALID_TIERS:
        tier = "standard"

    payment_terms = (body.get("payment_terms") or "30_70").strip()
    if payment_terms not in _VALID_PAYMENT_TERMS:
        payment_terms = "30_70"

    def _str(key, default=""):
        v = body.get(key)
        return (v if isinstance(v, str) else (str(v) if v is not None else default)).strip()

    def _int(key, default=0):
        v = body.get(key)
        try:
            return max(0, int(v)) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    with transaction.atomic():
        s = Supplier.objects.create(
            name           = name,
            slug           = _unique_slug(name),
            legal_name     = _str("legal_name"),
            tier           = tier,
            country        = _str("country"),
            city           = _str("city"),
            address        = _str("address"),
            timezone       = _str("timezone"),
            contact_name   = _str("contact_name"),
            contact_role   = _str("contact_role"),
            email          = _str("email"),
            phone          = _str("phone"),
            whatsapp       = _str("whatsapp"),
            wechat         = _str("wechat"),
            languages      = _coerce_list(body.get("languages")),
            website        = _str("website"),
            alibaba_url    = _str("alibaba_url"),
            default_lead_days = _int("default_lead_days", 10) or 10,
            moq            = _int("moq", 1) or 1,
            payment_terms  = payment_terms,
            payment_methods = _coerce_list(body.get("payment_methods")),
            currency       = (_str("currency") or "USD")[:10],
            tax_id         = _str("tax_id"),
            business_license_no = _str("business_license_no"),
            specialties    = _coerce_list(body.get("specialties")),
            cover_emoji    = (_str("cover_emoji") or "\U0001F3ED")[:4],
            cover_gradient_from = (_str("cover_gradient_from") or "#6366f1")[:20],
            cover_gradient_to   = (_str("cover_gradient_to")   or "#a855f7")[:20],
            logo_url       = _str("logo_url"),
            notes          = _str("notes"),
            is_active      = bool(body.get("is_active", True)),
        )

    return JsonResponse({
        "ok": True,
        "supplier": serialize_supplier(s, full=True),
    })


# ---------------------------------------------------------------------
# DETAIL  (GET /ops/api/supplier/<id>/)
# ---------------------------------------------------------------------
@ops_required
@require_GET
def api_supplier_detail(request, supplier_id):
    try:
        s = Supplier.objects.get(pk=supplier_id)
    except Supplier.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    contacts = [_serialize_contact(c) for c in s.contacts.all()]
    images   = [_serialize_image(im)  for im in s.images.all()]
    products = [serialize_supplier_product(p) for p in s.products.all()]

    return JsonResponse({
        "ok": True,
        "supplier": serialize_supplier(s, full=True),
        "contacts": contacts,
        "images":   images,
        "products": products,
    })


# ---------------------------------------------------------------------
# UPDATE  (POST /ops/api/supplier/<id>/update/)
# ---------------------------------------------------------------------
@ops_required
@require_POST
def api_supplier_update(request, supplier_id):
    try:
        s = Supplier.objects.get(pk=supplier_id)
    except Supplier.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    body = _parse_body(request)

    string_fields = [
        "name", "legal_name", "country", "city", "address", "timezone",
        "contact_name", "contact_role", "email", "phone", "whatsapp", "wechat",
        "website", "alibaba_url", "currency", "tax_id", "business_license_no",
        "cover_emoji", "cover_gradient_from", "cover_gradient_to", "logo_url",
        "notes",
    ]
    for f in string_fields:
        if f in body and body[f] is not None:
            setattr(s, f, str(body[f]).strip())

    if "tier" in body:
        tv = (body.get("tier") or "").strip().lower()
        if tv in _VALID_TIERS:
            s.tier = tv

    if "payment_terms" in body:
        pt = (body.get("payment_terms") or "").strip()
        if pt in _VALID_PAYMENT_TERMS:
            s.payment_terms = pt

    if "default_lead_days" in body:
        try: s.default_lead_days = max(0, int(body["default_lead_days"]))
        except (TypeError, ValueError): pass
    if "moq" in body:
        try: s.moq = max(1, int(body["moq"] or 1))
        except (TypeError, ValueError): pass

    if "languages" in body:        s.languages = _coerce_list(body["languages"])
    if "payment_methods" in body:  s.payment_methods = _coerce_list(body["payment_methods"])
    if "specialties" in body:      s.specialties = _coerce_list(body["specialties"])
    if "is_active" in body:        s.is_active = bool(body["is_active"])

    for f in ("rating", "quality_score", "communication_score", "delivery_score", "defect_pct"):
        if f in body and body[f] is not None and body[f] != "":
            setattr(s, f, _dec(body[f], default=str(getattr(s, f))))
    if "on_time_pct" in body:
        try: s.on_time_pct = max(0, min(100, int(body["on_time_pct"])))
        except (TypeError, ValueError): pass
    if "total_orders" in body:
        try: s.total_orders = max(0, int(body["total_orders"]))
        except (TypeError, ValueError): pass

    s.save()
    return JsonResponse({"ok": True, "supplier": serialize_supplier(s, full=True)})


# ---------------------------------------------------------------------
# PRODUCT LIST  (GET /ops/api/supplier/<id>/products/)
# ---------------------------------------------------------------------
@ops_required
@require_GET
def api_supplier_products(request, supplier_id):
    try:
        s = Supplier.objects.get(pk=supplier_id)
    except Supplier.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    products = [serialize_supplier_product(p) for p in s.products.all()]
    return JsonResponse({
        "ok": True,
        "supplier_id": s.id,
        "supplier_name": s.name,
        "products": products,
        "total": len(products),
    })


# ---------------------------------------------------------------------
# PRODUCT CREATE  (POST /ops/api/supplier/<id>/products/create/)
# ---------------------------------------------------------------------
@ops_required
@require_POST
def api_supplier_product_create(request, supplier_id):
    try:
        s = Supplier.objects.get(pk=supplier_id)
    except Supplier.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    body = _parse_body(request)

    sku          = (body.get("sku") or "").strip()
    product_name = (body.get("product_name") or "").strip()
    if not sku:
        return JsonResponse({"ok": False, "error": "SKU is required."}, status=400)
    if not product_name:
        return JsonResponse({"ok": False, "error": "Product name is required."}, status=400)

    unit_cost_usd = _dec(body.get("unit_cost_usd"), default="0")
    if unit_cost_usd <= 0:
        return JsonResponse(
            {"ok": False, "error": "Unit cost (USD) must be greater than 0."},
            status=400,
        )

    shipping_unit = _dec(body.get("shipping_unit_usd"), default="0")

    # Auto-compute suggested tenant price = unit_cost * 1.4 if not provided
    raw_suggested = body.get("suggested_tenant_price_usd")
    if raw_suggested in (None, "", 0, "0"):
        suggested = (unit_cost_usd * Decimal("1.4")).quantize(Decimal("0.01"))
    else:
        suggested = _dec(raw_suggested, default=str(unit_cost_usd * Decimal("1.4")))

    def _int(key, default):
        v = body.get(key)
        try:
            return max(1, int(v)) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    p = SupplierProduct.objects.create(
        supplier            = s,
        sku                 = sku,
        product_name        = product_name,
        product_image_url   = (body.get("product_image_url") or "").strip(),
        variant_summary     = (body.get("variant_summary") or "").strip(),
        unit_cost_cny       = _dec(body.get("unit_cost_cny"), default="0") or None,
        unit_cost_usd       = unit_cost_usd,
        shipping_unit_usd   = shipping_unit,
        moq                 = _int("moq", s.moq or 1),
        lead_days           = _int("lead_days", s.default_lead_days or 10),
        suggested_tenant_price_usd = suggested,
        is_preferred        = bool(body.get("is_preferred", True)),
        is_in_stock         = bool(body.get("is_in_stock", True)),
        notes               = (body.get("notes") or "").strip(),
        created_by          = request.user if request.user.is_authenticated else None,
    )

    return JsonResponse({"ok": True, "product": serialize_supplier_product(p)})


# ---------------------------------------------------------------------
# PRODUCT UPDATE  (POST /ops/api/supplier-product/<id>/update/)
# ---------------------------------------------------------------------
@ops_required
@require_POST
def api_supplier_product_update(request, product_id):
    """Patch pricing / lead time / MOQ / preferred-flag / notes on an
    existing SupplierProduct. Only fields present in the body are
    updated — any missing field is left untouched.
    """
    try:
        p = SupplierProduct.objects.select_related("supplier").get(pk=product_id)
    except SupplierProduct.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Product not found."}, status=404)

    body = _parse_body(request)
    fields_changed = []

    if "unit_cost_usd" in body:
        cost = _dec(body.get("unit_cost_usd"), default="0")
        if cost < 0:
            return JsonResponse({"ok": False, "error": "Unit cost cannot be negative."}, status=400)
        p.unit_cost_usd = cost
        fields_changed.append("unit_cost_usd")

    if "shipping_unit_usd" in body:
        ship = _dec(body.get("shipping_unit_usd"), default="0")
        if ship < 0:
            return JsonResponse({"ok": False, "error": "Shipping cost cannot be negative."}, status=400)
        p.shipping_unit_usd = ship
        fields_changed.append("shipping_unit_usd")

    if "unit_cost_cny" in body:
        v = body.get("unit_cost_cny")
        if v in (None, "", 0, "0"):
            p.unit_cost_cny = None
        else:
            p.unit_cost_cny = _dec(v, default="0")
        fields_changed.append("unit_cost_cny")

    if "lead_days" in body:
        try:
            ld = int(body.get("lead_days") or 0)
            p.lead_days = max(1, ld) if ld else (p.supplier.default_lead_days or 10)
            fields_changed.append("lead_days")
        except (TypeError, ValueError):
            pass

    if "moq" in body:
        try:
            mq = int(body.get("moq") or 0)
            p.moq = max(1, mq) if mq else 1
            fields_changed.append("moq")
        except (TypeError, ValueError):
            pass

    if "suggested_tenant_price_usd" in body:
        raw = body.get("suggested_tenant_price_usd")
        if raw in (None, "", 0, "0"):
            # Recompute from current unit_cost
            p.suggested_tenant_price_usd = (
                (p.unit_cost_usd or Decimal("0")) * Decimal("1.4")
            ).quantize(Decimal("0.01"))
        else:
            p.suggested_tenant_price_usd = _dec(raw, default="0")
        fields_changed.append("suggested_tenant_price_usd")

    if "is_preferred" in body:
        p.is_preferred = bool(body.get("is_preferred"))
        fields_changed.append("is_preferred")

    if "is_in_stock" in body:
        p.is_in_stock = bool(body.get("is_in_stock"))
        fields_changed.append("is_in_stock")

    if "product_name" in body:
        nm = (body.get("product_name") or "").strip()
        if nm:
            p.product_name = nm
            fields_changed.append("product_name")

    if "notes" in body:
        p.notes = (body.get("notes") or "").strip()
        fields_changed.append("notes")

    if not fields_changed:
        return JsonResponse({
            "ok": False,
            "error": "No editable fields supplied.",
        }, status=400)

    p.save()
    return JsonResponse({
        "ok": True,
        "product": serialize_supplier_product(p),
        "fields_changed": fields_changed,
    })


# ---------------------------------------------------------------------
# PRODUCT DELETE  (POST /ops/api/supplier-product/<id>/delete/)
# ---------------------------------------------------------------------
@ops_required
@require_POST
def api_supplier_product_delete(request, product_id):
    try:
        p = SupplierProduct.objects.select_related("supplier").get(pk=product_id)
    except SupplierProduct.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Product not found."}, status=404)

    supplier_id = p.supplier_id
    sku = p.sku
    p.delete()
    return JsonResponse({
        "ok": True,
        "deleted_id": product_id,
        "supplier_id": supplier_id,
        "sku": sku,
    })


# ═════════════════════════════════════════════════════════════════════
# IMPORT FROM TENANT STORE
# ─────────────────────────────────────────────────────────────────────
# These endpoints power the "📥 Import from tenant store" flow in the
# supplier detail modal: ops picks any tenant's connected Shopify/WC
# store, live-fetches its product list, and bulk-creates SupplierProduct
# rows in the supplier's catalog. Ops sees ALL tenant stores (no per-user
# filter) since ops is the back-office team.
# ═════════════════════════════════════════════════════════════════════


_PLATFORM_LABELS = {
    "shopify":     "Shopify",
    "woocommerce": "WooCommerce",
}


def _store_has_credentials(store) -> bool:
    if (store.access_token or "").strip():
        return True
    return bool((store.api_key or "").strip() and (store.api_secret or "").strip())


# ─── Demo product catalog ────────────────────────────────────────────────
# Curated sample products served when a tenant store has no real API
# credentials. Lets ops fully exercise the import modal without touching
# Shopify/WC. Uses picsum.photos for stable, deterministic thumbnails.
_DEMO_CATALOG = [
    # ── Apparel ──────────────────────────────────────────────────────
    {
        "name":  "Premium Linen Shirt",
        "sku":   "APP-LIN-001",
        "img":   "linen-shirt",
        "price": "59.00",
        "colors": ["White", "Sand", "Sage", "Navy"],
        "sizes":  ["S", "M", "L", "XL"],
    },
    {
        "name":  "Organic Cotton T-Shirt",
        "sku":   "APP-TEE-002",
        "img":   "cotton-tee",
        "price": "24.50",
        "colors": ["Black", "White", "Olive", "Heather Grey", "Burgundy"],
        "sizes":  ["XS", "S", "M", "L", "XL"],
    },
    {
        "name":  "Slim-Fit Denim Jeans",
        "sku":   "APP-JNS-003",
        "img":   "denim-jeans",
        "price": "78.00",
        "colors": ["Dark Indigo", "Mid Wash", "Black"],
        "sizes":  ["28", "30", "32", "34", "36", "38"],
    },
    {
        "name":  "Merino Wool Sweater",
        "sku":   "APP-SWT-004",
        "img":   "wool-sweater",
        "price": "129.00",
        "colors": ["Charcoal", "Cream", "Forest"],
        "sizes":  ["S", "M", "L"],
    },
    # ── Footwear ─────────────────────────────────────────────────────
    {
        "name":  "Canvas Low-Top Sneakers",
        "sku":   "SHO-CNV-101",
        "img":   "canvas-sneakers",
        "price": "65.00",
        "colors": ["White", "Black", "Khaki"],
        "sizes":  ["6", "7", "8", "9", "10", "11", "12"],
    },
    {
        "name":  "Italian Leather Loafers",
        "sku":   "SHO-LFR-102",
        "img":   "leather-loafers",
        "price": "189.00",
        "colors": ["Cognac", "Black"],
        "sizes":  ["8", "9", "10", "11", "12"],
    },
    # ── Bags ─────────────────────────────────────────────────────────
    {
        "name":  "Minimalist Canvas Tote Bag",
        "sku":   "BAG-TOT-201",
        "img":   "canvas-tote",
        "price": "32.00",
        "colors": ["Natural", "Black", "Olive", "Rust"],
        "sizes":  [],
    },
    {
        "name":  "Leather Crossbody Bag",
        "sku":   "BAG-CRB-202",
        "img":   "leather-bag",
        "price": "145.00",
        "colors": ["Tan", "Black", "Cognac"],
        "sizes":  ["Small", "Medium"],
    },
    # ── Home goods ───────────────────────────────────────────────────
    {
        "name":  "Ceramic Pour-Over Coffee Set",
        "sku":   "HOM-CFE-301",
        "img":   "ceramic-coffee",
        "price": "48.00",
        "colors": [],
        "sizes":  ["4-cup", "8-cup"],
    },
    {
        "name":  "Linen Throw Pillow Cover",
        "sku":   "HOM-PLW-302",
        "img":   "throw-pillow",
        "price": "22.00",
        "colors": ["Sand", "Charcoal", "Mustard", "Sage", "Terracotta"],
        "sizes":  ["18x18", "22x22"],
    },
    {
        "name":  "Bamboo Cutting Board",
        "sku":   "HOM-CUT-303",
        "img":   "cutting-board",
        "price": "34.00",
        "colors": [],
        "sizes":  ["Small", "Medium", "Large"],
    },
    {
        "name":  "Soy Wax Candle · Sandalwood",
        "sku":   "HOM-CND-304",
        "img":   "soy-candle",
        "price": "28.00",
        "colors": [],
        "sizes":  ["8oz", "12oz"],
    },
    # ── Electronics ──────────────────────────────────────────────────
    {
        "name":  "Wireless Noise-Cancelling Earbuds",
        "sku":   "ELC-EBD-401",
        "img":   "earbuds",
        "price": "129.00",
        "colors": ["Midnight Black", "Pearl White", "Sage Green"],
        "sizes":  [],
    },
    {
        "name":  "Adjustable Phone Stand",
        "sku":   "ELC-STD-402",
        "img":   "phone-stand",
        "price": "25.00",
        "colors": ["Silver", "Space Gray"],
        "sizes":  [],
    },
    {
        "name":  "Braided USB-C Cable",
        "sku":   "ELC-CBL-403",
        "img":   "usb-cable",
        "price": "12.00",
        "colors": ["Black", "White"],
        "sizes":  ["1m", "2m", "3m"],
    },
    {
        "name":  "Mechanical Keyboard · Tactile",
        "sku":   "ELC-KBD-404",
        "img":   "keyboard",
        "price": "169.00",
        "colors": ["Black", "White"],
        "sizes":  ["60%", "TKL", "Full-size"],
    },
    # ── Beauty / personal care ───────────────────────────────────────
    {
        "name":  "Lavender Hand Soap",
        "sku":   "BEA-SOP-501",
        "img":   "hand-soap",
        "price": "14.00",
        "colors": [],
        "sizes":  ["8oz", "16oz"],
    },
    {
        "name":  "Vitamin C Brightening Serum",
        "sku":   "BEA-SRM-502",
        "img":   "vitamin-c",
        "price": "42.00",
        "colors": [],
        "sizes":  ["15ml", "30ml"],
    },
    {
        "name":  "Bamboo Boar-Bristle Hairbrush",
        "sku":   "BEA-BRH-503",
        "img":   "hairbrush",
        "price": "26.00",
        "colors": [],
        "sizes":  [],
    },
    # ── Accessories ──────────────────────────────────────────────────
    {
        "name":  "Aviator Sunglasses",
        "sku":   "ACC-SUN-601",
        "img":   "sunglasses",
        "price": "85.00",
        "colors": ["Gold/Brown", "Silver/Black", "Rose Gold/Pink"],
        "sizes":  [],
    },
    {
        "name":  "Slim Leather Wallet",
        "sku":   "ACC-WAL-602",
        "img":   "wallet",
        "price": "52.00",
        "colors": ["Black", "Brown", "Tan", "Navy"],
        "sizes":  [],
    },
    {
        "name":  "Stainless Steel Watch",
        "sku":   "ACC-WCH-603",
        "img":   "watch",
        "price": "215.00",
        "colors": ["Silver", "Rose Gold"],
        "sizes":  [],
    },
    # ── Stationery ───────────────────────────────────────────────────
    {
        "name":  "Hardcover Lined Notebook",
        "sku":   "STA-NTB-701",
        "img":   "notebook",
        "price": "18.00",
        "colors": ["Black", "Navy", "Burgundy"],
        "sizes":  ["A5", "A4"],
    },
    {
        "name":  "Premium Fountain Pen Set",
        "sku":   "STA-PEN-702",
        "img":   "fountain-pen",
        "price": "94.00",
        "colors": [],
        "sizes":  [],
    },
    # ── Edge cases ───────────────────────────────────────────────────
    {
        # Product without SKU — tests the "no SKU" warning path
        "name":  "Unlabeled Sample Item",
        "sku":   "",
        "img":   "sample-box",
        "price": "9.99",
        "colors": [],
        "sizes":  [],
    },
]


def _demo_catalog(catalog_skus: set) -> list:
    """Materialize the demo catalog as live-API-shaped product dicts.

    Each entry from _DEMO_CATALOG expands into a product with all
    color × size combinations as variants (variant SKU appended).
    """
    out = []
    for i, p in enumerate(_DEMO_CATALOG):
        primary_sku = p["sku"]
        # Build variant matrix (color × size, or whichever is present)
        colors = p["colors"] or [""]
        sizes  = p["sizes"]  or [""]
        variants = []
        for ci, c in enumerate(colors):
            for si, s in enumerate(sizes):
                if c or s:
                    suffix_parts = []
                    if s: suffix_parts.append(s.replace(" ", "").replace("/", "").upper()[:6])
                    if c: suffix_parts.append("".join(w[0] for w in c.split())[:4].upper())
                    suffix = "-".join(suffix_parts) or f"V{ci}{si}"
                    vsku = f"{primary_sku}-{suffix}" if primary_sku else ""
                else:
                    vsku = primary_sku
                variants.append({
                    "variant_id":     f"demo-{i}-{ci}-{si}",
                    "color":          c,
                    "size":           s,
                    "sku":            vsku,
                    "stock_quantity": 50 - (ci * 7) - (si * 3),
                    "price":          p["price"],
                })
        if not variants:
            variants = [{
                "variant_id":     f"demo-{i}-0-0",
                "color":          "",
                "size":           "",
                "sku":            primary_sku,
                "stock_quantity": 25,
                "price":          p["price"],
            }]
        out.append({
            "product_id":         f"demo-{i}",
            "product_name":       p["name"],
            "image_url":          f"https://picsum.photos/seed/{p['img']}/600/600",
            "sku":                primary_sku,
            "price":              p["price"],
            "variants":           variants,
            "already_in_catalog": bool(primary_sku) and primary_sku in catalog_skus,
        })
    return out


def _serialize_tenant_store(store) -> dict:
    """Brief shape for the picker grid in step 1 of the import modal."""
    owner = store.user
    owner_username  = owner.username       if owner else ""
    owner_email     = owner.email          if owner else ""
    owner_full_name = (owner.get_full_name() if owner else "") or owner_username

    return {
        "id":              store.id,
        "name":            store.name,
        "platform":        store.platform,
        "platform_label":  _PLATFORM_LABELS.get(store.platform, store.platform.title()),
        "store_url":       store.store_url,
        "is_active":       store.is_active,
        "owner_username":  owner_username,
        "owner_email":     owner_email,
        "owner_full_name": owner_full_name,
        "has_credentials": _store_has_credentials(store),
        "created_at_iso":  None,
    }


# ---------------------------------------------------------------------
# TENANT STORES LIST  (GET /ops/api/tenant-stores/)
# ---------------------------------------------------------------------
@ops_required
@require_GET
def api_tenant_stores_list(request):
    """Return ALL tenant stores connected to Drop Sigma.

    Optional ?search= filters by name / url / owner email / owner username.
    Ordered by name. Used by step 1 of the import-from-store modal.
    """
    search = (request.GET.get("search") or "").strip()

    qs = Store.objects.select_related("user").all()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(store_url__icontains=search)
            | Q(user__email__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(platform__icontains=search)
        )

    qs = qs.order_by("name", "id")
    stores = [_serialize_tenant_store(s) for s in qs]

    return JsonResponse({
        "ok":     True,
        "stores": stores,
        "total":  len(stores),
    })


# ---------------------------------------------------------------------
# TENANT STORE PRODUCTS  (GET /ops/api/tenant-store/<id>/products/)
# ---------------------------------------------------------------------
@ops_required
@require_GET
def api_tenant_store_products(request, store_id):
    """Live-fetch products from a tenant store's Shopify/WC API.

    Mirrors stock.views.stock_fetch_store_products_api but ops-scoped
    (no per-user filter — ops sees all stores). Adds:
      - price: retail price hint from the platform
      - already_in_catalog: True if a SupplierProduct with this SKU
        already exists for the supplier passed via ?supplier_id=

    Falls back to a rich demo product catalog when the store has no API
    credentials OR ?demo=1 is passed — so the import flow is fully testable
    without real Shopify/WC connections. Demo responses include
    `demo: true` so the UI can show a banner.
    """
    try:
        store = Store.objects.select_related("user").get(pk=store_id)
    except Store.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Store not found."}, status=404)

    # Optional: scope "already_in_catalog" flag to a specific supplier
    supplier_id = request.GET.get("supplier_id")
    catalog_skus = set()
    if supplier_id:
        try:
            sid = int(supplier_id)
            catalog_skus = set(
                SupplierProduct.objects.filter(supplier_id=sid)
                .values_list("sku", flat=True)
            )
        except (TypeError, ValueError):
            catalog_skus = set()

    # Demo mode — when store has no credentials, return curated sample
    # products so ops can test the full import flow end-to-end.
    force_demo = request.GET.get("demo") == "1"
    if force_demo or not _store_has_credentials(store):
        demo_products = _demo_catalog(catalog_skus)
        return JsonResponse({
            "ok":         True,
            "store_id":   store.id,
            "store_name": store.name,
            "platform":   store.platform,
            "products":   demo_products,
            "total":      len(demo_products),
            "demo":       True,
            "demo_reason": ("Demo mode requested" if force_demo
                            else "Store has no API credentials — showing sample products for testing."),
        })

    # Real live fetch (credentials present and demo not forced) — uses
    # the catalog_skus already resolved above.
    products = []

    try:
        if store.platform == "woocommerce":
            from orders.services import woo_session
            base = store.store_url.rstrip("/")
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
                        return JsonResponse({
                            "ok": False,
                            "error": (
                                f"WooCommerce API returned {r.status_code}. "
                                "Check the store's API key/secret."
                            ),
                        }, status=502)
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
                        "product_id":         str(p["id"]),
                        "product_name":       p.get("name", "") or "",
                        "image_url":          image_url,
                        "sku":                primary_sku,
                        "price":              price,
                        "variants":           variants,
                        "already_in_catalog": bool(primary_sku) and primary_sku in catalog_skus,
                    })
                if len(batch) < 100:
                    break
                page += 1

        elif store.platform == "shopify":
            base = store.store_url.rstrip("/")
            headers = {"X-Shopify-Access-Token": store.access_token} if store.access_token else {}
            r = _req.get(
                f"{base}/admin/api/2024-01/products.json",
                headers=headers, params={"limit": 250, "status": "active"}, timeout=15,
            )
            if r.status_code != 200:
                return JsonResponse({
                    "ok": False,
                    "error": (
                        f"Shopify API returned {r.status_code}. "
                        "The token may have expired — owner must reconnect."
                    ),
                }, status=502)
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
                    "product_id":         str(p["id"]),
                    "product_name":       p.get("title", "") or "",
                    "image_url":          image_url,
                    "sku":                primary_sku,
                    "price":              price,
                    "variants":           variants,
                    "already_in_catalog": bool(primary_sku) and primary_sku in catalog_skus,
                })
        else:
            return JsonResponse(
                {"ok": False, "error": f"Platform '{store.platform}' not supported."},
                status=400,
            )

    except _req.exceptions.Timeout:
        return JsonResponse(
            {"ok": False, "error": "Store API timed out after 15s — try again."},
            status=502,
        )
    except _req.exceptions.ConnectionError:
        return JsonResponse(
            {"ok": False, "error": "Could not connect to the store API."},
            status=502,
        )
    except _req.exceptions.RequestException as exc:
        return JsonResponse(
            {"ok": False, "error": f"Store API error: {str(exc)[:200]}"},
            status=502,
        )

    return JsonResponse({
        "ok":         True,
        "store_id":   store.id,
        "store_name": store.name,
        "platform":   store.platform,
        "products":   products,
        "total":      len(products),
    })


# ---------------------------------------------------------------------
# BULK IMPORT TO SUPPLIER  (POST /ops/api/supplier/<id>/import-products/)
# ---------------------------------------------------------------------
def _build_variant_summary(variants: list) -> str:
    """Build a short human label for the variant pool (capped at 100+)."""
    if not variants:
        return ""
    count = len(variants)
    sizes  = sorted({(v.get("size")  or "").strip() for v in variants if (v.get("size")  or "").strip()})
    colors = sorted({(v.get("color") or "").strip() for v in variants if (v.get("color") or "").strip()})

    parts = []
    if count >= 100:
        parts.append("100+ variants")
    else:
        parts.append(f"{count} variant{'s' if count != 1 else ''}")
    if sizes:
        parts.append("Sizes: " + ", ".join(sizes[:8]) + ("…" if len(sizes) > 8 else ""))
    if colors:
        parts.append(f"Colors: {len(colors)}")
    return " · ".join(parts)


@ops_required
@require_POST
def api_supplier_import_products(request, supplier_id):
    """Bulk-import products from a tenant store into a supplier's catalog.

    Body: {store_id, products: [{product_id, sku, product_name, image_url,
           unit_cost_usd, shipping_unit_usd, lead_days, moq, is_preferred,
           variants:[{sku, variant, unit_cost_usd}]}]}

    Dedup: (supplier, sku) — second import with same SKU is skipped, not
    duplicated. Unit cost defaults to 0 so ops can fill it after import.
    """
    try:
        supplier = Supplier.objects.get(pk=supplier_id)
    except Supplier.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    body = _parse_body(request)
    raw_products = body.get("products") or []
    if not isinstance(raw_products, list) or not raw_products:
        return JsonResponse(
            {"ok": False, "error": "No products to import."}, status=400,
        )

    default_lead_days = supplier.default_lead_days or 10
    default_moq       = supplier.moq or 1

    imported_objs = []
    skipped = 0

    def _int_or_default(value, default):
        try:
            return max(1, int(value)) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    with transaction.atomic():
        for raw in raw_products:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            sku = (raw.get("sku") or "").strip()
            product_name = (raw.get("product_name") or "").strip()
            if not sku or not product_name:
                skipped += 1
                continue

            # Dedup: skip SKUs already linked to this supplier
            if SupplierProduct.objects.filter(supplier=supplier, sku=sku).exists():
                skipped += 1
                continue

            unit_cost = _dec(raw.get("unit_cost_usd"), default="0")
            shipping  = _dec(raw.get("shipping_unit_usd"), default="0")
            lead_days = _int_or_default(raw.get("lead_days"), default_lead_days)
            moq       = _int_or_default(raw.get("moq"),       default_moq)

            # Auto-suggest tenant price at 1.4x cost when cost is set
            suggested = None
            if unit_cost > 0:
                suggested = (unit_cost * Decimal("1.4")).quantize(Decimal("0.01"))

            variants_raw = raw.get("variants") or []
            if not isinstance(variants_raw, list):
                variants_raw = []
            variant_summary = _build_variant_summary(variants_raw)

            product = SupplierProduct.objects.create(
                supplier            = supplier,
                sku                 = sku,
                product_name        = product_name,
                product_image_url   = (raw.get("image_url") or "").strip()[:600],
                variant_summary     = variant_summary[:200],
                unit_cost_usd       = unit_cost,
                shipping_unit_usd   = shipping,
                moq                 = moq,
                lead_days           = lead_days,
                suggested_tenant_price_usd = suggested,
                is_preferred        = bool(raw.get("is_preferred", True)),
                is_in_stock         = True,
                notes               = (
                    f"Imported from tenant store #{body.get('store_id') or '?'} "
                    f"(product_id={raw.get('product_id') or '?'})"
                ),
                created_by          = request.user if request.user.is_authenticated else None,
            )
            imported_objs.append(product)

            # Variant rows (dedup per product by variant string)
            seen_variant_keys = set()
            for v in variants_raw:
                if not isinstance(v, dict):
                    continue
                variant_label = (v.get("variant") or "").strip()
                if not variant_label:
                    # Build from color/size when caller didn't pre-compose it
                    parts = [
                        (v.get("size")  or "").strip(),
                        (v.get("color") or "").strip(),
                    ]
                    variant_label = " / ".join([p for p in parts if p])
                if not variant_label:
                    continue
                if variant_label in seen_variant_keys:
                    continue
                seen_variant_keys.add(variant_label)

                v_cost_raw = v.get("unit_cost_usd")
                v_cost = _dec(v_cost_raw, default="0") if v_cost_raw not in (None, "") else None

                SupplierProductVariant.objects.get_or_create(
                    product=product,
                    variant=variant_label[:200],
                    defaults={
                        "sku":           (v.get("sku") or "").strip()[:120],
                        "unit_cost_usd": v_cost,
                        "is_in_stock":   True,
                    },
                )

    return JsonResponse({
        "ok":           True,
        "imported":     len(imported_objs),
        "skipped":      skipped,
        "supplier_id":  supplier.id,
        "products":     [serialize_supplier_product(p) for p in imported_objs],
    })
