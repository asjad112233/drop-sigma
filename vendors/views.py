from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Vendor, ProductVendorAssignment, VendorTrackingSubmission, StoreVendorAssignment, TrackingQueueSetting, ProductTrackingAutoApprove
from .serializers import VendorSerializer
from orders.models import Order
from orders.services import log_activity, COURIER_URL_TEMPLATES
from stores.models import Store


# ─── Admin: Vendor CRUD ───────────────────────────────────────────────────────

@api_view(["GET"])
def vendor_list(request):
    store_id = request.GET.get("store_id")
    vendors = Vendor.objects.all().order_by("-id").select_related("assigned_store")
    if store_id:
        vendors = vendors.filter(assigned_store_id=store_id)
    serializer = VendorSerializer(vendors, many=True)
    data = serializer.data

    for item, vendor in zip(data, vendors):
        # Permanent product assignments
        perm_qs = ProductVendorAssignment.objects.filter(vendor=vendor, is_active=True).select_related("store")
        perm_pids = set(perm_qs.values_list("product_id", flat=True))
        item["perm_count"] = len(perm_pids)

        # Non-permanent orders
        non_perm_q = Order.objects.filter(assigned_vendor=vendor)
        if perm_pids:
            non_perm_q = non_perm_q.exclude(product_id__in=perm_pids)
        item["non_perm_count"] = non_perm_q.count()

        # Full-store assignments
        full_store_qs = StoreVendorAssignment.objects.filter(vendor=vendor, is_active=True).select_related("store")
        full_store_ids = set(full_store_qs.values_list("store_id", flat=True))

        # Partial stores: have product assignments but NOT full store
        partial_store_ids = set(perm_qs.values_list("store_id", flat=True)) - full_store_ids

        # Order-only stores: vendor has assigned orders but no product/full assignment
        order_store_ids = set(
            Order.objects.filter(assigned_vendor=vendor)
            .values_list("store_id", flat=True)
            .distinct()
        ) - full_store_ids - partial_store_ids

        # Build per-store breakdown (all stores the vendor touches)
        all_store_ids = full_store_ids | partial_store_ids | order_store_ids
        store_breakdown = []
        # Prefetch store names for order-only stores
        order_stores = {s.id: s for s in Store.objects.filter(id__in=order_store_ids)} if order_store_ids else {}

        for sid in all_store_ids:
            store_obj = (
                full_store_qs.filter(store_id=sid).first() or
                perm_qs.filter(store_id=sid).first()
            )
            if store_obj:
                store_name = store_obj.store.name
            else:
                store_name = order_stores[sid].name if sid in order_stores else str(sid)

            is_full = sid in full_store_ids
            s_perm_pids = set(perm_qs.filter(store_id=sid).values_list("product_id", flat=True))
            s_perm_count = len(s_perm_pids)
            s_non_perm = Order.objects.filter(assigned_vendor=vendor, store_id=sid)
            if perm_pids:
                s_non_perm = s_non_perm.exclude(product_id__in=perm_pids)
            s_non_perm_count = s_non_perm.count()
            # Count only distinct products this vendor is involved with in this store
            s_non_perm_pids = set(
                s_non_perm.exclude(product_id="").exclude(product_id__isnull=True)
                .values_list("product_id", flat=True).distinct()
            )
            total_products = len(s_perm_pids | s_non_perm_pids)
            store_breakdown.append({
                "store_id": sid,
                "store_name": store_name,
                "is_full": is_full,
                "perm_count": s_perm_count,
                "non_perm_count": s_non_perm_count,
                "total_products": total_products,
            })

        item["full_stores_count"] = len(full_store_ids)
        item["partial_stores_count"] = len(partial_store_ids) + len(order_store_ids)
        item["total_stores_count"] = len(all_store_ids)
        item["store_breakdown"] = store_breakdown

    return Response(data)


@api_view(["POST"])
def vendor_create(request):
    data = request.data.copy()
    password = data.get("password", "").strip()

    email = data.get("email", "").strip()
    if Vendor.objects.filter(email=email).exists():
        return Response({"success": False, "errors": {"email": ["A vendor with this email already exists."]}}, status=400)

    serializer = VendorSerializer(data=data)
    if not serializer.is_valid():
        return Response({"success": False, "errors": serializer.errors}, status=400)

    vendor = serializer.save()

    perms = data.get("permissions", {})
    if perms:
        vendor.permissions = perms
        vendor.save()

    if password and email:
        base = email.split("@")[0] + "_vendor"
        username = base
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base}_{counter}"
            counter += 1
        user = User.objects.create_user(username=username, email=email, password=password)
        vendor.user = user
        vendor.password_plain = password
        vendor.save()

    return Response({"success": True, "vendor": VendorSerializer(vendor).data})


@api_view(["DELETE"])
def vendor_delete(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    if vendor.user:
        vendor.user.delete()
    vendor.delete()
    return Response({"success": True, "message": "Vendor deleted successfully"})


@api_view(["POST"])
def vendor_update_permissions(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    new_perms = request.data.get("permissions", {})
    changed_by = request.data.get("changed_by", "Admin")

    old_perms = vendor.permissions or {}
    changes = {}
    for key, new_val in new_perms.items():
        old_val = old_perms.get(key, False)
        if old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}

    vendor.permissions = new_perms
    vendor.save()

    if changes:
        from .models import VendorPermissionLog
        VendorPermissionLog.objects.create(
            vendor=vendor,
            changed_by=changed_by,
            changes=changes,
        )

    return Response({"success": True, "permissions": vendor.permissions})


@api_view(["GET"])
def vendor_permission_logs_api(request, vendor_id):
    from .models import VendorPermissionLog
    vendor = get_object_or_404(Vendor, id=vendor_id)
    logs = VendorPermissionLog.objects.filter(vendor=vendor)
    data = [{
        "id": l.id,
        "changed_by": l.changed_by,
        "changes": l.changes,
        "changed_at": l.changed_at.isoformat(),
    } for l in logs]
    return Response({"success": True, "logs": data})


# ─── Admin: Tracking Approval Queue ──────────────────────────────────────────

@api_view(["GET"])
def tracking_queue_api(request):
    store_id = request.GET.get("store_id")
    status_filter = request.GET.get("status", "pending")

    qs = VendorTrackingSubmission.objects.select_related("order", "vendor").order_by("-submitted_at")
    if store_id:
        qs = qs.filter(order__store_id=store_id)
    if status_filter != "all":
        qs = qs.filter(status=status_filter)

    data = []
    for sub in qs:
        data.append({
            "id": sub.id,
            "order_id": sub.order.id,
            "order_number": sub.order.external_order_id,
            "customer_name": sub.order.customer_name,
            "vendor_id": sub.vendor.id,
            "vendor_name": sub.vendor.name,
            "tracking_number": sub.tracking_number,
            "tracking_url": sub.tracking_url or "",
            "courier_name": sub.courier_name or "",
            "vendor_note": sub.vendor_note or "",
            "status": sub.status,
            "is_auto_approved": sub.is_auto_approved,
            "reject_reason": sub.reject_reason or "",
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
            "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        })

    return Response({"success": True, "submissions": data, "count": len(data)})


@api_view(["POST"])
def approve_tracking_api(request, submission_id):
    try:
        sub = get_object_or_404(VendorTrackingSubmission, id=submission_id)
        sub.status = "approved"
        sub.reviewed_at = timezone.now()
        sub.save()

        order = sub.order
        order.tracking_number = sub.tracking_number
        order.tracking_company = sub.courier_name or order.tracking_company
        order.tracking_url = sub.tracking_url or order.tracking_url
        order.vendor_status = "approved"
        order.fulfillment_status = "shipped"
        order.save()

        try:
            from orders.services import _fire_auto_email
            _fire_auto_email(order, "shipped")
        except Exception:
            pass

        try:
            log_activity(order, "tracking_approved",
                         f"Tracking approved: {sub.tracking_number}. Customer notified.",
                         actor="Admin")
        except Exception:
            pass

        return Response({"success": True, "message": "Tracking approved and customer notified."})
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"approve_tracking_api error: {e}", exc_info=True)
        return Response({"success": False, "message": str(e)}, status=500)


@api_view(["POST"])
def reject_tracking_api(request, submission_id):
    sub = get_object_or_404(VendorTrackingSubmission, id=submission_id)
    reason = request.data.get("reason", "").strip()

    sub.status = "rejected"
    sub.reject_reason = reason
    sub.reviewed_at = timezone.now()
    sub.save()

    order = sub.order
    order.vendor_status = "rejected"
    order.save()

    reject_desc = f"Tracking rejected: {sub.tracking_number}."
    if reason:
        reject_desc += f" Reason: {reason}"
    log_activity(order, "tracking_rejected", reject_desc, actor="Admin")

    return Response({"success": True, "message": "Tracking submission rejected."})


@api_view(["GET", "POST"])
def tracking_queue_settings_api(request):
    """Get or set auto-approve toggle for a store's tracking queue."""
    store_id = request.data.get("store_id") or request.GET.get("store_id")
    store = get_object_or_404(Store, id=store_id) if store_id else None

    if request.method == "GET":
        if not store:
            return Response({"success": False, "message": "store_id required"}, status=400)
        setting, _ = TrackingQueueSetting.objects.get_or_create(store=store)
        auto_products = list(
            ProductTrackingAutoApprove.objects.filter(store=store).values("product_id", "product_name")
        )
        return Response({
            "success": True,
            "auto_approve": setting.auto_approve,
            "auto_approve_products": auto_products,
        })

    # POST — toggle auto_approve
    if not store:
        return Response({"success": False, "message": "store_id required"}, status=400)
    auto_approve = request.data.get("auto_approve")
    setting, _ = TrackingQueueSetting.objects.get_or_create(store=store)
    if auto_approve is not None:
        setting.auto_approve = bool(auto_approve)
    else:
        setting.auto_approve = not setting.auto_approve
    setting.save()
    return Response({"success": True, "auto_approve": setting.auto_approve})


@api_view(["POST"])
def approve_tracking_permanent_api(request, submission_id):
    """Approve this submission AND permanently auto-approve all future submissions for this product."""
    try:
        sub = get_object_or_404(VendorTrackingSubmission, id=submission_id)

        sub.status = "approved"
        sub.reviewed_at = timezone.now()
        sub.save()

        order = sub.order
        from orders.services import COURIER_URL_TEMPLATES, _fire_auto_email
        if sub.courier_name:
            tmpl = COURIER_URL_TEMPLATES.get(sub.courier_name.lower())
            tracking_url = tmpl.format(num=sub.tracking_number) if tmpl else (sub.tracking_url or "")
        else:
            tracking_url = sub.tracking_url or ""

        order.tracking_number = sub.tracking_number
        order.tracking_company = sub.courier_name or order.tracking_company
        order.tracking_url = tracking_url or order.tracking_url
        order.vendor_status = "approved"
        order.fulfillment_status = "shipped"
        order.save()

        try:
            _fire_auto_email(order, "shipped")
        except Exception:
            pass

        try:
            log_activity(order, "tracking_approved",
                         f"Tracking approved (permanent): {sub.tracking_number}. Customer notified.",
                         actor="Admin")
        except Exception:
            pass

        if order.product_id:
            ProductTrackingAutoApprove.objects.update_or_create(
                product_id=order.product_id,
                store=order.store,
                defaults={"product_name": order.product_name or ""},
            )

        return Response({
            "success": True,
            "message": "Approved and set to auto-approve for all future orders of this product."
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"approve_tracking_permanent_api error: {e}", exc_info=True)
        return Response({"success": False, "message": str(e)}, status=500)


@api_view(["DELETE"])
def remove_product_auto_approve_api(request, product_id):
    """Remove a product from the permanent auto-approve list."""
    store_id = request.data.get("store_id") or request.GET.get("store_id")
    ProductTrackingAutoApprove.objects.filter(product_id=product_id, store_id=store_id).delete()
    return Response({"success": True})


# ─── Vendor Auth ──────────────────────────────────────────────────────────────

def vendor_login_page(request):
    if request.user.is_authenticated and hasattr(request.user, "vendor_profile"):
        return redirect("/vendor/dashboard/")

    error = None
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "").strip()
        try:
            vendor = Vendor.objects.get(email=email)
            if vendor.user:
                user = authenticate(request, username=vendor.user.username, password=password)
                if user:
                    login(request, user)
                    return redirect("/vendor/dashboard/")
        except Vendor.DoesNotExist:
            pass
        error = "Invalid email or password."

    return render(request, "vendor_login.html", {"error": error})


def vendor_logout_view(request):
    logout(request)
    return redirect("/vendor/login/")


# ─── Vendor Portal ────────────────────────────────────────────────────────────

def vendor_portal_page(request):
    if not request.user.is_authenticated:
        return redirect("/vendor/login/")
    try:
        vendor = request.user.vendor_profile
    except Exception:
        return redirect("/vendor/login/")
    return render(request, "vendor_dashboard.html", {"vendor": vendor})


@api_view(["GET"])
def vendor_orders_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        vendor = request.user.vendor_profile
    except Exception:
        return Response({"success": False, "message": "Not a vendor"}, status=403)

    status_filter = request.GET.get("status", "all")
    orders = Order.objects.filter(assigned_vendor=vendor).order_by("-created_at")
    if status_filter != "all":
        orders = orders.filter(vendor_status=status_filter)

    perms = vendor.permissions or {}

    def perm(key):
        return bool(perms.get(key, False))

    data = []
    for order in orders:
        latest_sub = order.tracking_submissions.order_by("-submitted_at").first()
        line_items = []
        if order.raw_data and isinstance(order.raw_data, dict):
            line_items = order.raw_data.get("line_items", [])

        billing = (order.raw_data or {}).get("billing", {})
        full_address = ", ".join(filter(None, [
            billing.get("address_1", ""),
            billing.get("address_2", ""),
            order.city or billing.get("city", ""),
            billing.get("postcode", ""),
            order.country or billing.get("country", ""),
        ]))

        row = {
            "id": order.id,
            "order_number": order.external_order_id,
            "customer_name": order.customer_name or "-",
            "customer_phone": order.customer_phone or "-",
            "customer_city": order.city or "-",
            "customer_country": order.country or "-",
            "customer_address": full_address or "-",
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "product_name": order.product_name or "",
            "payment_status": order.payment_status or "-",
            "fulfillment_status": order.fulfillment_status or "-",
            "vendor_status": order.vendor_status,
            "tracking_number": order.tracking_number or "",
            "line_items": [
                {
                    "name": i.get("name", ""),
                    "quantity": i.get("quantity", 1),
                    "product_id": str(i.get("product_id", "")),
                    "sku": i.get("sku", ""),
                    "image": i.get("image", {}).get("src", "") if isinstance(i.get("image"), dict) else "",
                    "meta_data": [
                        {"display_key": m.get("display_key",""), "display_value": str(m.get("display_value",""))}
                        for m in i.get("meta_data", [])
                        if m.get("display_key") and not str(m.get("key","")).startswith("_")
                    ],
                    "total": i.get("total", ""),
                }
                for i in line_items
            ],
            "latest_submission": {
                "id": latest_sub.id,
                "tracking_number": latest_sub.tracking_number,
                "courier_name": latest_sub.courier_name or "",
                "tracking_url": latest_sub.tracking_url or "",
                "vendor_note": latest_sub.vendor_note or "",
                "status": latest_sub.status,
                "reject_reason": latest_sub.reject_reason or "",
                "submitted_at": latest_sub.submitted_at.isoformat(),
            } if latest_sub else None,
        }

        # Optional fields gated by permissions
        if perm("show_order_amount"):
            row["total_price"] = str(order.total_price)
            row["currency"] = order.currency
        if perm("show_order_email"):
            row["customer_email"] = order.customer_email or "-"
        if perm("show_assigned_member"):
            row["assigned_to_name"] = order.assigned_to.name if order.assigned_to else None
        if perm("show_store_url"):
            row["store_url"] = order.store.store_url if order.store else None

        data.append(row)

    all_orders = Order.objects.filter(assigned_vendor=vendor)
    stats = {
        "total": all_orders.count(),
        "assigned": all_orders.filter(vendor_status="assigned").count(),
        "in_progress": all_orders.filter(vendor_status="in_progress").count(),
        "tracking_submitted": all_orders.filter(vendor_status="tracking_submitted").count(),
        "rejected": all_orders.filter(vendor_status="rejected").count(),
        "approved": all_orders.filter(vendor_status="approved").count(),
    }

    return Response({"success": True, "orders": data, "stats": stats, "permissions": perms})


@api_view(["POST"])
def vendor_submit_tracking_api(request, order_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        vendor = request.user.vendor_profile
    except Exception:
        return Response({"success": False, "message": "Not a vendor"}, status=403)

    order = get_object_or_404(Order, id=order_id, assigned_vendor=vendor)

    tracking_number = request.data.get("tracking_number", "").strip()
    if not tracking_number:
        return Response({"success": False, "message": "Tracking number is required."}, status=400)

    courier_name = request.data.get("courier_name", "") or None
    tracking_url = request.data.get("tracking_url", "") or None
    # Known couriers always get a proper tracking URL with the tracking number — ignore any base URL sent from frontend
    if courier_name:
        template = COURIER_URL_TEMPLATES.get(courier_name.lower())
        if template:
            tracking_url = template.format(num=tracking_number)
        elif not tracking_url:
            tracking_url = None  # unknown courier, no URL provided — keep empty

    # Check if this submission should be auto-approved
    should_auto_approve = False
    try:
        setting = TrackingQueueSetting.objects.filter(store=order.store).first()
        if setting and setting.auto_approve:
            should_auto_approve = True
        elif order.product_id:
            if ProductTrackingAutoApprove.objects.filter(product_id=order.product_id, store=order.store).exists():
                should_auto_approve = True
    except Exception:
        pass

    sub = VendorTrackingSubmission.objects.create(
        order=order,
        vendor=vendor,
        tracking_number=tracking_number,
        tracking_url=tracking_url,
        courier_name=courier_name,
        vendor_note=request.data.get("vendor_note", "") or None,
        status="approved" if should_auto_approve else "pending",
        is_auto_approved=should_auto_approve,
    )

    if should_auto_approve:
        from django.utils import timezone as tz
        sub.reviewed_at = tz.now()
        sub.save(update_fields=["reviewed_at"])
        order.tracking_number = tracking_number
        order.tracking_company = courier_name or order.tracking_company
        order.tracking_url = tracking_url or order.tracking_url
        order.vendor_status = "approved"
        order.fulfillment_status = "shipped"
        order.save()
        from orders.services import _fire_auto_email
        _fire_auto_email(order, "shipped")
        log_activity(order, "tracking_approved",
                     f"Tracking auto-approved: {tracking_number}. Customer notified.",
                     actor="System")
        return Response({"success": True, "message": "Tracking auto-approved.", "submission_id": sub.id, "auto_approved": True})

    order.vendor_status = "tracking_submitted"
    order.save()

    log_activity(order, "tracking_submitted",
                 f"Vendor '{vendor.name}' submitted tracking: {tracking_number}",
                 actor=vendor.name)

    return Response({"success": True, "message": "Tracking submitted for approval.", "submission_id": sub.id})


@api_view(["DELETE"])
def remove_perm_assignment_api(request, assignment_id):
    assignment = get_object_or_404(ProductVendorAssignment, id=assignment_id)
    assignment.delete()
    return Response({"success": True, "message": "Permanent assignment removed."})


@api_view(["POST", "DELETE"])
def store_full_vendor_api(request, store_id):
    store = get_object_or_404(Store, id=store_id)
    vendor_id = request.data.get("vendor_id")
    if not vendor_id:
        return Response({"success": False, "message": "vendor_id required"}, status=400)
    vendor = get_object_or_404(Vendor, id=vendor_id)
    if request.method == "POST":
        StoreVendorAssignment.objects.get_or_create(vendor=vendor, store=store, defaults={"is_active": True})
        return Response({"success": True, "message": f"{vendor.name} assigned to {store.name}"})
    else:
        StoreVendorAssignment.objects.filter(vendor=vendor, store=store).delete()
        return Response({"success": True, "message": "Assignment removed."})


@api_view(["POST"])
def vendor_toggle_store_scope_api(request, vendor_id, store_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    store = get_object_or_404(Store, id=store_id)
    action = request.data.get("action", "set_full")
    if action == "set_full":
        StoreVendorAssignment.objects.get_or_create(vendor=vendor, store=store, defaults={"is_active": True})
    else:
        StoreVendorAssignment.objects.filter(vendor=vendor, store=store).delete()
    return Response({"success": True})


def _get_product_image(vendor, product_id):
    order = Order.objects.filter(assigned_vendor=vendor, product_id=product_id, raw_data__isnull=False).first()
    if not order or not order.raw_data:
        return ""
    for item in order.raw_data.get("line_items", []):
        if str(item.get("product_id", "")) == str(product_id):
            img = item.get("image")
            if isinstance(img, dict):
                return img.get("src", "")
            return img or ""
    return ""


@api_view(["GET"])
def vendor_products_api(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)

    permanent = ProductVendorAssignment.objects.filter(vendor=vendor, is_active=True).select_related("store")

    permanent_data = []
    permanent_product_ids = set()
    for a in permanent:
        permanent_product_ids.add(a.product_id)
        product_orders = Order.objects.filter(
            assigned_vendor=vendor,
            product_id=a.product_id,
        ).select_related("store").order_by("-created_at")
        shipped_count = sum(1 for o in product_orders if o.fulfillment_status in ("shipped", "completed"))
        active_orders = [
            {
                "id": o.id,
                "order_number": o.external_order_id,
                "customer_name": o.customer_name or "-",
                "fulfillment_status": o.fulfillment_status or "-",
                "store_name": o.store.name if o.store else "",
                "store_id": o.store_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in product_orders
        ]
        permanent_data.append({
            "id": a.id,
            "product_id": a.product_id,
            "product_name": a.product_name or a.product_id,
            "store_name": a.store.name if a.store else "",
            "store_id": a.store_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "image": _get_product_image(vendor, a.product_id),
            "shipped_count": shipped_count,
            "active_orders": active_orders,
        })

    assigned_orders = Order.objects.filter(assigned_vendor=vendor).select_related("store").order_by("-created_at")
    non_permanent_orders = []
    for order in assigned_orders:
        pid = order.product_id or ""
        if pid and pid in permanent_product_ids:
            continue
        non_permanent_orders.append({
            "id": order.id,
            "order_number": order.external_order_id,
            "product_id": pid,
            "product_name": order.product_name or "-",
            "customer_name": order.customer_name or "-",
            "fulfillment_status": order.fulfillment_status or "-",
            "vendor_status": order.vendor_status or "-",
            "store_name": order.store.name if order.store else "",
            "store_id": order.store_id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        })

    return Response({
        "success": True,
        "vendor_name": vendor.name,
        "permanent_products": permanent_data,
        "assigned_orders": non_permanent_orders,
    })


@api_view(["GET"])
def vendor_tracking_history_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        vendor = request.user.vendor_profile
    except Exception:
        return Response({"success": False, "message": "Not a vendor"}, status=403)

    submissions = VendorTrackingSubmission.objects.filter(vendor=vendor).order_by("-submitted_at")
    data = [{
        "id": s.id,
        "order_id": s.order.id,
        "order_number": s.order.external_order_id,
        "tracking_number": s.tracking_number,
        "courier_name": s.courier_name or "",
        "tracking_url": s.tracking_url or "",
        "vendor_note": s.vendor_note or "",
        "status": s.status,
        "reject_reason": s.reject_reason or "",
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
    } for s in submissions]

    return Response({"success": True, "submissions": data})


@api_view(["POST"])
def vendor_update_status(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    new_status = request.data.get("status")
    if new_status not in ("active", "inactive"):
        return Response({"success": False, "message": "Invalid status."}, status=400)
    vendor.status = new_status
    vendor.save(update_fields=["status"])
    return Response({"success": True, "status": vendor.status})


@api_view(["GET", "POST"])
def vendor_store_manage_products_api(request, vendor_id, store_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    store = get_object_or_404(Store, id=store_id)

    if request.method == "POST":
        action = request.data.get("action")
        product_ids = request.data.get("product_ids", [])
        product_names = request.data.get("product_names", [])
        if not product_ids or action not in ("assign_permanent", "unassign"):
            return Response({"success": False, "message": "Invalid request."}, status=400)
        if action == "assign_permanent":
            for i, pid in enumerate(product_ids):
                pname = product_names[i] if i < len(product_names) else None
                if not pname:
                    row = Order.objects.filter(store=store, product_id=pid).first()
                    pname = row.product_name if row else pid
                ProductVendorAssignment.objects.update_or_create(
                    store=store, product_id=str(pid),
                    defaults={"vendor": vendor, "product_name": pname, "is_active": True}
                )
        else:
            ProductVendorAssignment.objects.filter(
                vendor=vendor, store=store, product_id__in=[str(p) for p in product_ids]
            ).delete()
        return Response({"success": True})

    # GET — return all products in store with assignment status
    perm_qs = ProductVendorAssignment.objects.filter(vendor=vendor, store=store, is_active=True)
    perm_map = {a.product_id: a.id for a in perm_qs}

    # All unique products from orders in this store
    rows = (Order.objects.filter(store=store)
            .exclude(product_id="").exclude(product_id__isnull=True)
            .values("product_id", "product_name").distinct())
    product_map = {}
    for row in rows:
        pid = str(row["product_id"])
        if pid not in product_map:
            product_map[pid] = row["product_name"] or pid

    # Non-perm: vendor has orders here but product not permanently assigned
    non_perm_pids = set(
        Order.objects.filter(assigned_vendor=vendor, store=store)
        .exclude(product_id__in=list(perm_map.keys()))
        .values_list("product_id", flat=True)
    )

    # Active orders count per product (pending/processing)
    active_orders_qs = (
        Order.objects.filter(assigned_vendor=vendor, store=store)
        .exclude(fulfillment_status__in=["shipped", "completed", "cancelled"])
        .values_list("product_id", flat=True)
    )
    active_counts = {}
    for pid in active_orders_qs:
        k = str(pid)
        active_counts[k] = active_counts.get(k, 0) + 1

    products = []
    for pid, pname in product_map.items():
        if pid in perm_map:
            status = "permanent"
        elif pid in non_perm_pids:
            status = "non_permanent"
        else:
            status = "unassigned"
        products.append({
            "product_id": pid,
            "product_name": pname,
            "status": status,
            "assignment_id": perm_map.get(pid),
            "active_orders": active_counts.get(pid, 0),
        })

    order_key = {"permanent": 0, "non_permanent": 1, "unassigned": 2}
    products.sort(key=lambda x: order_key[x["status"]])

    return Response({
        "success": True,
        "vendor_name": vendor.name,
        "store_name": store.name,
        "products": products,
        "total_products": len(product_map),
        "perm_count": len(perm_map),
        "non_perm_count": len(non_perm_pids),
    })


@api_view(["GET"])
def vendor_credentials_api(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    login_url = request.build_absolute_uri("/vendor/login/")
    return Response({
        "success": True,
        "name": vendor.name,
        "email": vendor.email,
        "password": vendor.password_plain or "",
        "login_url": login_url,
        "has_account": vendor.user_id is not None,
    })


import random
import string

@api_view(["POST"])
def vendor_reset_password_api(request, vendor_id):
    vendor = get_object_or_404(Vendor, id=vendor_id)
    new_password = request.data.get("password", "").strip()
    if not new_password:
        chars = string.ascii_letters + string.digits + "!@#$"
        new_password = "".join(random.choices(chars, k=12))

    if vendor.user:
        vendor.user.set_password(new_password)
        vendor.user.save()
    vendor.password_plain = new_password
    vendor.save(update_fields=["password_plain"])
    return Response({"success": True, "password": new_password})
