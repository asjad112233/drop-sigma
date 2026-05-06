import csv
import io
import json
import uuid
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.db import transaction

from stores.models import Store
from orders.models import Order
from .models import StockProduct, StockVariant, StockEntry, StockAuditLog


def _actor(request):
    if request.user.is_authenticated:
        name = request.user.get_full_name() or request.user.username
        return name
    return "Admin"


def _variant_data(variant):
    entry = getattr(variant, "entry", None)
    return {
        "id": variant.id,
        "color": variant.color,
        "size": variant.size,
        "sku": variant.sku,
        "quantity": entry.quantity if entry else 0,
        "reserved": entry.reserved if entry else 0,
        "available": entry.available() if entry else 0,
    }


def _product_data(product, include_variants=True):
    variants = list(product.variants.all()) if include_variants else []
    total_qty = 0
    total_available = 0
    for v in variants:
        e = getattr(v, "entry", None)
        if e:
            total_qty += e.quantity
            total_available += e.available()
    return {
        "id": product.id,
        "product_id": product.product_id,
        "product_name": product.product_name,
        "store_id": product.store_id,
        "store_name": product.store.name,
        "is_active": product.is_active,
        "synced_at": product.synced_at.isoformat(),
        "variants": [_variant_data(v) for v in variants] if include_variants else [],
        "total_qty": total_qty,
        "total_available": total_available,
        "variant_count": len(variants),
    }


@login_required
@require_http_methods(["GET"])
def stock_dashboard_api(request):
    store_id = request.GET.get("store_id")
    qs = StockProduct.objects.select_related("store").prefetch_related("variants__entry")
    if store_id:
        qs = qs.filter(store_id=store_id)

    products = list(qs)
    all_entries = StockEntry.objects.filter(variant__product__in=products)
    total_qty = sum(e.quantity for e in all_entries)
    low_stock = sum(1 for e in all_entries if 0 < e.quantity <= 5)
    out_of_stock = sum(1 for e in all_entries if e.quantity == 0)
    total_variants = sum(p.variant_count for p in [])
    tv = StockVariant.objects.filter(product__in=products).count()

    return JsonResponse({
        "success": True,
        "stats": {
            "total_products": len(products),
            "total_variants": tv,
            "total_qty": total_qty,
            "low_stock": low_stock,
            "out_of_stock": out_of_stock,
        },
        "products": [_product_data(p) for p in products],
    })


@login_required
@require_http_methods(["POST"])
def stock_sync_api(request):
    data = json.loads(request.body)
    store_id = data.get("store_id")
    if not store_id:
        return JsonResponse({"success": False, "message": "store_id required"}, status=400)

    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return JsonResponse({"success": False, "message": "Store not found"}, status=404)

    orders = Order.objects.filter(store=store).exclude(product_id__isnull=True).exclude(product_id="")
    synced = 0
    for order in orders:
        pid = str(order.product_id).strip()
        pname = (order.product_name or pid).strip()
        if not pid:
            continue
        product, _ = StockProduct.objects.get_or_create(
            store=store, product_id=pid,
            defaults={"product_name": pname},
        )

        # Try to get variants from raw order data
        raw = order.raw_data or {}
        line_items = raw.get("line_items", [])
        found_variant = False
        for item in line_items:
            color, size, sku = "", "", ""
            for prop in item.get("properties", []):
                k = str(prop.get("name", "")).lower()
                v = str(prop.get("value", ""))
                if k in ("color", "colour"):
                    color = v
                elif k == "size":
                    size = v
            sku = item.get("sku", "") or ""
            variant, _ = StockVariant.objects.get_or_create(
                product=product, color=color, size=size, defaults={"sku": sku}
            )
            StockEntry.objects.get_or_create(variant=variant)
            found_variant = True

        if not found_variant:
            variant, _ = StockVariant.objects.get_or_create(
                product=product, color="", size="", defaults={"sku": ""}
            )
            StockEntry.objects.get_or_create(variant=variant)

        synced += 1

    return JsonResponse({"success": True, "synced": synced})


@login_required
@require_http_methods(["GET", "POST"])
def stock_entry_api(request):
    if request.method == "GET":
        variant_id = request.GET.get("variant_id")
        try:
            variant = StockVariant.objects.select_related("product__store").get(id=variant_id)
        except StockVariant.DoesNotExist:
            return JsonResponse({"success": False, "message": "Variant not found"}, status=404)
        StockEntry.objects.get_or_create(variant=variant)
        return JsonResponse({"success": True, "variant": _variant_data(variant)})

    data = json.loads(request.body)
    variant_id = data.get("variant_id")
    new_qty = data.get("quantity")
    note = data.get("note", "")
    action = data.get("action", "adjust")

    try:
        variant = StockVariant.objects.get(id=variant_id)
    except StockVariant.DoesNotExist:
        return JsonResponse({"success": False, "message": "Variant not found"}, status=404)

    entry, _ = StockEntry.objects.get_or_create(variant=variant)
    qty_before = entry.quantity
    if new_qty is not None:
        entry.quantity = max(0, int(new_qty))
    entry.updated_by = request.user
    entry.save()

    StockAuditLog.objects.create(
        variant=variant, action=action,
        qty_before=qty_before, qty_after=entry.quantity,
        actor=_actor(request), note=note,
    )
    return JsonResponse({"success": True, "quantity": entry.quantity, "available": entry.available()})


@login_required
@require_http_methods(["POST"])
def stock_add_product_api(request):
    data = json.loads(request.body)
    store_id = data.get("store_id")
    product_name = data.get("product_name", "").strip()
    color = data.get("color", "").strip()
    size = data.get("size", "").strip()
    sku = data.get("sku", "").strip()
    quantity = max(0, int(data.get("quantity", 0) or 0))

    if not store_id or not product_name:
        return JsonResponse({"success": False, "message": "store_id and product_name required"}, status=400)

    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return JsonResponse({"success": False, "message": "Store not found"}, status=404)

    product_id = data.get("product_id") or f"manual-{uuid.uuid4().hex[:8]}"

    with transaction.atomic():
        product, _ = StockProduct.objects.get_or_create(
            store=store, product_id=product_id,
            defaults={"product_name": product_name},
        )
        variant, _ = StockVariant.objects.get_or_create(
            product=product, color=color, size=size, defaults={"sku": sku}
        )
        entry, _ = StockEntry.objects.get_or_create(variant=variant)
        qty_before = entry.quantity
        entry.quantity = max(0, entry.quantity + quantity)
        entry.updated_by = request.user
        entry.save()

        StockAuditLog.objects.create(
            variant=variant, action="add",
            qty_before=qty_before, qty_after=entry.quantity,
            actor=_actor(request), note=data.get("note", "Manual add"),
        )

    return JsonResponse({"success": True, "product_id": product.id, "variant_id": variant.id})


@login_required
@require_http_methods(["POST"])
def stock_bulk_upload_api(request):
    store_id = request.POST.get("store_id")
    csv_file = request.FILES.get("file")
    if not store_id or not csv_file:
        return JsonResponse({"success": False, "message": "store_id and file required"}, status=400)

    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return JsonResponse({"success": False, "message": "Store not found"}, status=404)

    text = csv_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows_ok, rows_err = 0, 0
    errors = []

    with transaction.atomic():
        for i, row in enumerate(reader, start=2):
            try:
                pname = (row.get("product_name") or "").strip()
                color = (row.get("color") or "").strip()
                size = (row.get("size") or "").strip()
                sku = (row.get("sku") or "").strip()
                qty = max(0, int(row.get("quantity") or 0))
                if not pname:
                    raise ValueError("product_name is empty")

                pid = (row.get("product_id") or "").strip() or f"csv-{pname[:30].lower().replace(' ', '-')}"
                product, _ = StockProduct.objects.get_or_create(
                    store=store, product_id=pid, defaults={"product_name": pname}
                )
                variant, _ = StockVariant.objects.get_or_create(
                    product=product, color=color, size=size, defaults={"sku": sku}
                )
                entry, _ = StockEntry.objects.get_or_create(variant=variant)
                qty_before = entry.quantity
                entry.quantity = qty
                entry.updated_by = request.user
                entry.save()

                StockAuditLog.objects.create(
                    variant=variant, action="add",
                    qty_before=qty_before, qty_after=qty,
                    actor=_actor(request), note="CSV bulk upload",
                )
                rows_ok += 1
            except Exception as e:
                rows_err += 1
                errors.append(f"Row {i}: {e}")

    return JsonResponse({"success": True, "imported": rows_ok, "errors": rows_err, "error_details": errors[:20]})


@login_required
@require_http_methods(["POST"])
def stock_deduct_api(request):
    data = json.loads(request.body)
    variant_id = data.get("variant_id")
    qty = int(data.get("quantity", 1))
    order_id = data.get("order_id")

    try:
        variant = StockVariant.objects.get(id=variant_id)
    except StockVariant.DoesNotExist:
        return JsonResponse({"success": False, "message": "Variant not found"}, status=404)

    entry, _ = StockEntry.objects.get_or_create(variant=variant)
    qty_before = entry.quantity
    entry.quantity = max(0, entry.quantity - qty)
    entry.updated_by = request.user
    entry.save()

    order = None
    if order_id:
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            pass

    StockAuditLog.objects.create(
        variant=variant, order=order, action="deduct",
        qty_before=qty_before, qty_after=entry.quantity,
        actor=_actor(request), note=data.get("note", ""),
    )
    return JsonResponse({"success": True, "quantity": entry.quantity, "available": entry.available()})


@login_required
@require_http_methods(["GET"])
def stock_audit_api(request):
    variant_id = request.GET.get("variant_id")
    product_id = request.GET.get("product_id")
    store_id = request.GET.get("store_id")
    limit = min(int(request.GET.get("limit", 100)), 500)

    qs = StockAuditLog.objects.select_related("variant__product__store", "order")

    if variant_id:
        qs = qs.filter(variant_id=variant_id)
    elif product_id:
        qs = qs.filter(variant__product_id=product_id)
    elif store_id:
        qs = qs.filter(variant__product__store_id=store_id)

    logs = []
    for log in qs[:limit]:
        logs.append({
            "id": log.id,
            "action": log.action,
            "action_label": log.get_action_display(),
            "variant": str(log.variant),
            "product_name": log.variant.product.product_name,
            "color": log.variant.color,
            "size": log.variant.size,
            "qty_before": log.qty_before,
            "qty_after": log.qty_after,
            "actor": log.actor,
            "note": log.note,
            "order_id": log.order_id,
            "created_at": log.created_at.isoformat(),
        })

    return JsonResponse({"success": True, "logs": logs})
