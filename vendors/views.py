from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Vendor, ProductVendorAssignment, VendorTrackingSubmission, StoreVendorAssignment, TrackingQueueSetting, ProductTrackingAutoApprove, VendorInvitation
from .serializers import VendorSerializer
from orders.models import Order
from orders.services import log_activity, COURIER_URL_TEMPLATES
from stores.models import Store


# ─── Admin: Vendor CRUD ───────────────────────────────────────────────────────

@api_view(["GET"])
def vendor_list(request):
    store_id = request.GET.get("store_id")
    if request.user.is_authenticated and not request.user.is_superuser:
        vendors = Vendor.objects.filter(assigned_store__user=request.user).order_by("-id").select_related("assigned_store")
    else:
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

        from teamapp.services import add_user_to_default_channels, get_or_create_admin_dm
        added_by = request.user if request.user.is_authenticated else None
        add_user_to_default_channels(user, added_by_user=added_by)
        if added_by:
            get_or_create_admin_dm(added_by, user)

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

        try:
            from stock.views import deduct_vendor_stock_for_order
            deduct_vendor_stock_for_order(order, sub.vendor)
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

    if request.method == "GET":
        return redirect("/login/?tab=vendor")

    # POST — try login
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
    return redirect("/login/?tab=vendor&error=Invalid+email+or+password.")


def vendor_logout_view(request):
    logout(request)
    return redirect("/")


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
        try:
            from stock.views import deduct_vendor_stock_for_order
            deduct_vendor_stock_for_order(order, vendor)
        except Exception:
            pass
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


# ─────────────────────────────────────────────────────────────────────────────
# VENDOR PORTAL — STOCK APIs
# ─────────────────────────────────────────────────────────────────────────────

def _require_vendor(request):
    if not request.user.is_authenticated:
        return None, Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        return request.user.vendor_profile, None
    except Exception:
        return None, Response({"success": False, "message": "Not a vendor"}, status=403)


@api_view(["GET"])
def vendor_stock_api(request):
    """Vendor sees their own stock assignments + tracker."""
    vendor, err = _require_vendor(request)
    if err:
        return err

    from stock.models import VendorStockAssignment, VendorStockAssignmentLine

    assignments = (
        VendorStockAssignment.objects
        .filter(vendor=vendor)
        .select_related("store", "product")
        .prefetch_related("lines__variant")
        .order_by("-created_at")
    )

    result = []
    for a in assignments:
        lines   = list(a.lines.select_related("variant").all())
        attempts = list(a.attempts.order_by("attempt_number"))
        total_qty = sum(l.quantity_assigned for l in lines)
        result.append({
            "id":               a.id,
            "product_id":       a.product.product_id,
            "product_name":     a.product.product_name,
            "product_image":    a.product.image_url or "",
            "store_name":       a.store.name,
            "status":           a.status,
            "admin_note":       a.admin_note,
            "vendor_note":      a.vendor_note,
            "reject_reason":    a.reject_reason,
            # Quotation
            "per_unit_price":   str(a.per_unit_price) if a.per_unit_price else "",
            "estimated_days":   a.estimated_days,
            "resubmission_count": a.resubmission_count,
            "days_remaining":   a.days_remaining,
            "total_price":      str(round(float(a.per_unit_price or 0) * total_qty, 2)),
            # Payment
            "payment_amount":   str(a.payment_amount) if a.payment_amount else "",
            "payment_method":   a.payment_method or "",
            "approved_at":      a.approved_at.isoformat() if a.approved_at else None,
            "stock_arrived":    a.stock_arrived,
            "arrived_at":       a.arrived_at.isoformat() if a.arrived_at else None,
            "created_at":       a.created_at.date().isoformat(),
            "total_assigned":   total_qty,
            "total_sold":       a.total_sold(),
            "total_on_hand":    a.total_on_hand(),
            "lines": [
                {
                    "id":                l.id,
                    "variant_id":        l.variant_id,
                    "color":             l.variant.color,
                    "size":              l.variant.size,
                    "sku":               l.variant.sku,
                    "quantity_assigned": l.quantity_assigned,
                    "quantity_sold":     l.quantity_sold,
                    "on_hand":           l.on_hand,
                    "unit_price":        str(l.unit_price) if l.unit_price is not None else "",
                }
                for l in lines
            ],
            "attempts": [
                {
                    "attempt_number": at.attempt_number,
                    "per_unit_price": str(at.per_unit_price),
                    "total_quantity": at.total_quantity,
                    "total_price":    str(at.total_price),
                    "estimated_days": at.estimated_days,
                    "vendor_note":    at.vendor_note,
                    "submitted_at":   at.submitted_at.strftime("%d %b %Y"),
                    "status":         at.status,
                    "admin_note":     at.admin_note,
                }
                for at in attempts
            ],
        })

    # Stats
    approved = [a for a in result if a["status"] == "approved"]
    stats = {
        "total_assigned":        sum(a["total_assigned"] for a in approved),
        "total_sold":            sum(a["total_sold"]     for a in approved),
        "total_on_hand":         sum(a["total_on_hand"]  for a in approved),
        "pending_pricing":       sum(1 for a in result if a["status"] == "pending_pricing"),
        "pending_approval":      sum(1 for a in result if a["status"] == "pending_approval"),
        "approved":              sum(1 for a in result if a["status"] == "approved"),
        "rejected":              sum(1 for a in result if a["status"] == "rejected"),
        "permanently_rejected":  sum(1 for a in result if a["status"] == "permanently_rejected"),
    }

    return Response({"success": True, "assignments": result, "stats": stats})


@api_view(["POST"])
def vendor_stock_submit_pricing_api(request, assignment_id):
    """Vendor submits a quotation (per_unit_price + estimated_days) for an assignment."""
    vendor, err = _require_vendor(request)
    if err:
        return err

    from stock.models import VendorStockAssignment, VendorQuotationAttempt

    try:
        assignment = VendorStockAssignment.objects.get(id=assignment_id, vendor=vendor)
    except VendorStockAssignment.DoesNotExist:
        return Response({"success": False, "message": "Assignment not found"}, status=404)

    if assignment.status == "permanently_rejected":
        return Response({"success": False, "message": "This quotation has been permanently rejected. You cannot resubmit."}, status=403)

    if assignment.status not in ("pending_pricing", "rejected"):
        return Response({"success": False, "message": "Quotation already under review."}, status=400)

    per_unit_price = request.data.get("per_unit_price")
    estimated_days = request.data.get("estimated_days")
    vendor_note    = request.data.get("vendor_note", "")

    # Validate
    try:
        per_unit_price = float(per_unit_price)
        if per_unit_price <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return Response({"success": False, "message": "Per unit price must be greater than 0."}, status=400)

    try:
        estimated_days = int(estimated_days)
        if estimated_days < 1:
            raise ValueError()
    except (TypeError, ValueError):
        return Response({"success": False, "message": "Estimated days must be at least 1."}, status=400)

    total_qty   = assignment.total_assigned()
    total_price = round(per_unit_price * total_qty, 2)

    # Save all lines with same unit price
    for line in assignment.lines.all():
        line.unit_price = per_unit_price
        line.save(update_fields=["unit_price"])

    attempt_number = assignment.attempts.count() + 1
    VendorQuotationAttempt.objects.create(
        assignment     = assignment,
        attempt_number = attempt_number,
        per_unit_price = per_unit_price,
        total_quantity = total_qty,
        total_price    = total_price,
        estimated_days = estimated_days,
        vendor_note    = vendor_note,
    )

    assignment.status        = "pending_approval"
    assignment.per_unit_price = per_unit_price
    assignment.estimated_days = estimated_days
    assignment.vendor_note   = vendor_note
    assignment.payment_amount = total_price
    assignment.save(update_fields=["status", "per_unit_price", "estimated_days", "vendor_note", "payment_amount"])

    return Response({"success": True, "message": "Quotation submitted. Awaiting admin review.", "attempt_number": attempt_number})


@api_view(["POST"])
def vendor_stock_mark_arrived_api(request, assignment_id):
    """Vendor marks stock as physically arrived after admin approval."""
    vendor, err = _require_vendor(request)
    if err:
        return err

    from stock.models import VendorStockAssignment
    from django.utils import timezone

    try:
        assignment = VendorStockAssignment.objects.get(id=assignment_id, vendor=vendor)
    except VendorStockAssignment.DoesNotExist:
        return Response({"success": False, "message": "Assignment not found"}, status=404)

    if assignment.status != "approved":
        return Response({"success": False, "message": "Stock can only be marked arrived once approved."}, status=400)

    if assignment.stock_arrived:
        return Response({"success": False, "message": "Stock already marked as arrived."}, status=400)

    assignment.stock_arrived = True
    assignment.arrived_at    = timezone.now()
    assignment.save(update_fields=["stock_arrived", "arrived_at"])

    return Response({"success": True, "message": "Stock marked as arrived. Admin has been notified."})


# ─── Vendor Invitations ───────────────────────────────────────────────────────

import os
import threading
import datetime

_VINV_LOGO = """<table cellpadding="0" cellspacing="0"><tr>
  <td style="background:linear-gradient(135deg,#059669,#10b981);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
    <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
  </td>
  <td style="padding-left:12px;text-align:left;">
    <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Vendor Partner Portal</div>
  </td>
</tr></table>"""

_VINV_FOOTER = """<p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">
  &copy; 2026 Drop Sigma &nbsp;&middot;&nbsp;
  <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a>
  &nbsp;&middot;&nbsp;
  <a href="mailto:support@dropsigma.com" style="color:#94a3b8;text-decoration:none;">support@dropsigma.com</a>
</p>
<p style="margin:0;font-size:11px;color:#cbd5e1;">This invitation expires in 48 hours. If you did not expect this, ignore this email.</p>"""


def _build_vendor_invitation_email(name, invite_url, invited_by, store_name):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_VINV_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#059669,#10b981);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#065f46;background:#d1fae5;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x1F91D; VENDOR INVITATION</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">You're invited as a Vendor Partner</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, <strong style="color:#0f172a;">{invited_by}</strong> has invited you to join <strong style="color:#0f172a;">{store_name}</strong> as a vendor partner on Drop Sigma. Click below to accept and set your password.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td align="center" style="padding:32px 48px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#059669,#10b981);border-radius:12px;box-shadow:0 4px 14px rgba(5,150,105,.35);">
          <a href="{invite_url}" style="display:block;padding:16px 36px;font-size:15px;font-weight:800;color:#ffffff;text-decoration:none;letter-spacing:.2px;">Accept Invitation &amp; Set Password</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 8px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Or copy this link</p>
        <p style="margin:0;font-size:12px;color:#059669;word-break:break-all;">{invite_url}</p>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 36px;">
      <p style="margin:0;font-size:12px;color:#94a3b8;">This link expires in <strong>48 hours</strong>.</p>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding-top:28px;">{_VINV_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _send_vendor_invitation_email(to_email, subject, html):
    import logging
    logger = logging.getLogger(__name__)

    def _do():
        try:
            import resend as _resend
            api_key = os.getenv("RESEND_API_KEY", "")
            if not api_key:
                logger.warning("RESEND_API_KEY not set — vendor invitation email not sent to %s", to_email)
                return
            _resend.api_key = api_key
            result = _resend.Emails.send({
                "from":    "Drop Sigma <noreply@dropsigma.com>",
                "to":      [to_email],
                "subject": subject,
                "html":    html,
            })
            logger.info("Vendor invitation email sent to %s — id: %s", to_email, getattr(result, "id", result))
        except Exception as exc:
            logger.error("Failed to send vendor invitation email to %s: %s", to_email, exc)
    threading.Thread(target=_do, daemon=True).start()


@api_view(["POST"])
def send_vendor_invitation_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)

    name     = (request.data.get("name") or "").strip()
    email    = (request.data.get("email") or "").strip()
    store_id = request.data.get("store_id")

    if not name or not email:
        return Response({"success": False, "message": "Name and email are required."}, status=400)
    if not store_id:
        return Response({"success": False, "message": "Store is required."}, status=400)

    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return Response({"success": False, "message": "Store not found."}, status=404)

    if Vendor.objects.filter(email=email).exists():
        return Response({"success": False, "message": "A vendor with this email already exists."}, status=400)

    if User.objects.filter(email=email).exists():
        return Response({"success": False, "message": "A user with this email already exists."}, status=400)

    # Expire any existing pending invites for this email
    VendorInvitation.objects.filter(owner=request.user, email=email, status="pending").update(status="expired")

    expires_at = timezone.now() + datetime.timedelta(hours=48)
    inv = VendorInvitation.objects.create(
        owner=request.user,
        name=name,
        email=email,
        store=store,
        expires_at=expires_at,
    )

    scheme = request.scheme
    host   = request.get_host()
    invite_url = f"{scheme}://{host}/vendor/invite/accept/{inv.token}/"

    try:
        invited_by = request.user.tenant_profile.name
    except Exception:
        invited_by = request.user.get_full_name() or request.user.username
    html = _build_vendor_invitation_email(name, invite_url, invited_by, store.name)
    _send_vendor_invitation_email(email, f"You're invited as a vendor partner on Drop Sigma", html)

    return Response({"success": True, "message": f"Invitation sent to {email}."})


def accept_vendor_invitation_page(request, token):
    try:
        inv = VendorInvitation.objects.select_related("store").get(token=token)
    except VendorInvitation.DoesNotExist:
        return render(request, "vendor_invitation.html", {"error": "This invitation link is invalid."})

    if not inv.is_valid():
        msg = "This invitation has already been accepted." if inv.status == "accepted" else "This invitation link has expired."
        return render(request, "vendor_invitation.html", {"error": msg})

    return render(request, "vendor_invitation.html", {"invitation": inv})


def set_vendor_invitation_password_api(request, token):
    from django.http import JsonResponse
    import json

    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Method not allowed."}, status=405)

    try:
        inv = VendorInvitation.objects.select_related("store").get(token=token)
    except VendorInvitation.DoesNotExist:
        return JsonResponse({"success": False, "message": "Invalid invitation."}, status=404)

    if not inv.is_valid():
        return JsonResponse({"success": False, "message": "This invitation has expired or was already used."}, status=400)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid request body."}, status=400)

    password = (body.get("password") or "").strip()
    if len(password) < 8:
        return JsonResponse({"success": False, "message": "Password must be at least 8 characters."}, status=400)

    # Build unique username
    base     = inv.email.split("@")[0] + "_vendor"
    username = base
    counter  = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{counter}"
        counter += 1

    user = User.objects.create_user(username=username, email=inv.email, password=password)
    user.first_name = inv.name.split()[0]
    user.last_name  = " ".join(inv.name.split()[1:])
    user.save()

    vendor = Vendor.objects.create(
        user=inv.owner,  # owner FK for admin scoping (same pattern as employee)
        name=inv.name,
        email=inv.email,
        assigned_store=inv.store,
        status="active",
    )
    vendor.user = user
    vendor.save(update_fields=["user"])

    # Auto-add to default channels + admin DM
    from teamapp.services import add_user_to_default_channels, get_or_create_admin_dm
    add_user_to_default_channels(user, added_by_user=inv.owner)
    get_or_create_admin_dm(inv.owner, user)

    inv.status = "accepted"
    inv.save(update_fields=["status"])

    return JsonResponse({"success": True, "message": "Account activated!", "redirect": f"/vendor/login/activate/{inv.token}/"})


def vendor_activate_login_by_token(request, token):
    """Server-side GET: validates accepted vendor invitation, logs in, redirects to portal."""
    from django.contrib.auth import login as auth_login
    try:
        inv = VendorInvitation.objects.get(token=token, status="accepted")
        vendor = Vendor.objects.get(email=inv.email)
        user = vendor.user
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("/vendor/dashboard/")
    except Exception:
        return redirect("/vendor/login/")
