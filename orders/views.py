import hashlib
import hmac
import base64

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.db.models import Sum
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from stores.models import Store
from teamapp.models import TeamMember
from teamapp.services import auto_assign_order
from vendors.models import Vendor, ProductVendorAssignment

from .models import Order, OrderActivity


from .serializers import OrderSerializer
from .services import (
    sync_woocommerce_orders, sync_shopify_orders,
    log_activity, COURIER_URL_TEMPLATES,
    process_woocommerce_order, process_shopify_order,
    setup_woocommerce_webhook,
    _fire_auto_email,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def orders_poll_api(request):
    """Lightweight endpoint — returns latest order id + total count. Used by frontend polling."""
    store_id = request.GET.get("store_id")
    qs = Order.objects.all()
    if store_id:
        qs = qs.filter(store_id=store_id)
    latest = qs.order_by("-id").values("id").first()
    return Response({
        "latest_id": latest["id"] if latest else None,
        "count": qs.count(),
    })


def sync_orders(request, store_id):
    store = get_object_or_404(Store, id=store_id)

    if store.platform == "woocommerce":
        count = sync_woocommerce_orders(store)
        return JsonResponse({"success": True, "orders_synced": count})

    if store.platform == "shopify":
        count = sync_shopify_orders(store)
        return JsonResponse({"success": True, "orders_synced": count})

    return JsonResponse({"success": False, "message": "Platform not supported yet"})


@api_view(["GET"])
def orders_list_api(request):
    store_id = request.GET.get("store_id")
    status = request.GET.get("status")
    search = request.GET.get("search")

    orders = Order.objects.all().order_by("-created_at")

    if store_id:
        orders = orders.filter(store_id=store_id)

    if status:
        orders = orders.filter(payment_status__icontains=status)

    if search:
        orders = (
            orders.filter(customer_name__icontains=search)
            | orders.filter(customer_email__icontains=search)
            | orders.filter(external_order_id__icontains=search)
        )

    serializer = OrderSerializer(orders, many=True)

    return Response({
        "success": True,
        "count": orders.count(),
        "orders": serializer.data
    })


@api_view(["GET"])
def order_detail_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    serializer = OrderSerializer(order)

    return Response({
        "success": True,
        "order": serializer.data,
        "raw_data": order.raw_data
    })


@api_view(["GET"])
def overview_api(request):
    from datetime import timedelta
    from vendors.models import VendorTrackingSubmission

    store_id = request.GET.get("store_id")
    orders = Order.objects.select_related("store", "assigned_vendor").all()
    if store_id:
        orders = orders.filter(store_id=store_id)

    today = timezone.now().date()
    today_qs = orders.filter(created_at__date=today)

    total_revenue = float(orders.aggregate(s=Sum("total_price"))["s"] or 0)
    today_revenue = float(today_qs.aggregate(s=Sum("total_price"))["s"] or 0)

    unassigned = orders.filter(assigned_vendor__isnull=True, fulfillment_status__in=["processing", "pending"]).count()
    no_tracking = orders.filter(tracking_number__isnull=True).exclude(tracking_number="").exclude(fulfillment_status__in=["cancelled", "refunded"]).count()
    no_tracking2 = orders.filter(tracking_number="").exclude(fulfillment_status__in=["cancelled", "refunded"]).count()

    # Revenue last 7 days
    revenue_7 = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_q = orders.filter(created_at__date=day)
        revenue_7.append({
            "date": day.strftime("%d %b"),
            "revenue": float(day_q.aggregate(s=Sum("total_price"))["s"] or 0),
            "count": day_q.count(),
        })

    # Recent orders (last 8)
    recent_orders = []
    for o in orders.order_by("-created_at")[:8]:
        recent_orders.append({
            "id": o.id,
            "order_number": o.external_order_id or str(o.id),
            "customer": o.customer_name or "—",
            "product": o.product_name or "—",
            "amount": float(o.total_price or 0),
            "currency": o.currency or "USD",
            "fulfillment_status": o.fulfillment_status or "—",
            "payment_status": o.payment_status or "—",
            "store_name": o.store.name if o.store else "—",
            "vendor_name": o.assigned_vendor.name if o.assigned_vendor else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })

    # Store stats
    stores_qs = Store.objects.filter(is_active=True)
    if store_id:
        stores_qs = stores_qs.filter(id=store_id)
    store_stats = []
    for s in stores_qs:
        s_orders = orders.filter(store=s)
        store_stats.append({
            "id": s.id,
            "name": s.name,
            "platform": s.platform,
            "total": s_orders.count(),
            "today": s_orders.filter(created_at__date=today).count(),
            "unassigned": s_orders.filter(assigned_vendor__isnull=True, fulfillment_status__in=["processing", "pending"]).count(),
            "last_synced": s.last_synced.isoformat() if getattr(s, "last_synced", None) else None,
        })

    # Top vendors by order count
    top_vendors = []
    for v in Vendor.objects.filter(status="active")[:6]:
        v_orders = orders.filter(assigned_vendor=v)
        top_vendors.append({
            "id": v.id,
            "name": v.name,
            "order_count": v_orders.count(),
            "shipped": v_orders.filter(fulfillment_status__in=["shipped", "completed"]).count(),
        })
    top_vendors.sort(key=lambda x: x["order_count"], reverse=True)

    # Tracking queue
    pending_tracking = VendorTrackingSubmission.objects.filter(status="pending").count()

    # Email stats
    try:
        from emails.models import EmailMessage, EmailThreadAssignment
        email_qs = EmailMessage.objects.all()
        if store_id:
            email_qs = email_qs.filter(store_id=store_id)
        email_unread = email_qs.filter(is_read=False).count()
        email_new = email_qs.filter(status="new").count()
        email_open = email_qs.filter(status__in=["new", "assigned", "drafted"]).count()
        email_replied = email_qs.filter(status="replied").count()
        email_today = email_qs.filter(created_at__date=today).count()
        # Recent unread threads (last 6)
        recent_emails_raw = email_qs.filter(is_read=False).order_by("-created_at").select_related("store")[:6]
        recent_emails = [{
            "id": e.id,
            "sender": e.sender_name or e.sender or "Unknown",
            "subject": e.subject or "(No subject)",
            "category": e.category or "general",
            "status": e.status,
            "store_name": e.store.name if e.store else "—",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in recent_emails_raw]
        # Open (unresolved) thread assignments
        open_threads = EmailThreadAssignment.objects.filter(is_resolved=False).count()
    except Exception:
        email_unread = email_new = email_open = email_replied = email_today = open_threads = 0
        recent_emails = []

    return Response({
        "success": True,
        "total_orders": orders.count(),
        "today_orders": today_qs.count(),
        "total_revenue": total_revenue,
        "today_revenue": today_revenue,
        "pending_orders": orders.filter(payment_status__icontains="pending").count(),
        "failed_orders": orders.filter(payment_status__icontains="failed").count(),
        "no_tracking": no_tracking + no_tracking2,
        "unassigned_orders": unassigned,
        "vendor_assigned": orders.filter(assigned_vendor__isnull=False).count(),
        "active_vendors": Vendor.objects.filter(status="active").count(),
        "active_stores": stores_qs.count(),
        "revenue_7_days": revenue_7,
        "recent_orders": recent_orders,
        "store_stats": store_stats,
        "top_vendors": top_vendors,
        "pending_tracking": pending_tracking,
        "email_unread": email_unread,
        "email_new": email_new,
        "email_open": email_open,
        "email_replied": email_replied,
        "email_today": email_today,
        "email_open_threads": open_threads,
        "recent_emails": recent_emails,
    })


@api_view(["POST"])
def assign_order_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    member_id = request.data.get("member_id")

    if not member_id:
        return Response({
            "success": False,
            "message": "member_id is required"
        }, status=400)

    member = get_object_or_404(TeamMember, id=member_id)

    order.assigned_to = member
    order.save()

    log_activity(order, "assigned",
                 f"Assigned to {member.name} ({member.role})",
                 actor="Admin")

    return Response({
        "success": True,
        "message": f"Order assigned to {member.name}",
        "assigned_to": {
            "id": member.id,
            "name": member.name,
            "role": member.role
        }
    })


@api_view(["POST"])
def auto_assign_orders_api(request):
    store_id = request.data.get("store_id")

    orders = Order.objects.filter(assigned_to__isnull=True)

    if store_id:
        orders = orders.filter(store_id=store_id)

    assigned_count = 0

    for order in orders:
        member = auto_assign_order(order)
        if member:
            assigned_count += 1

    return Response({
        "success": True,
        "assigned_count": assigned_count
    })


@api_view(["POST"])
def assign_vendor_to_order_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    vendor_id = request.data.get("vendor_id")
    permanent = request.data.get("permanent", False)

    if not vendor_id:
        return Response({
            "success": False,
            "message": "vendor_id is required"
        }, status=400)

    vendor = get_object_or_404(Vendor, id=vendor_id)

    order.assigned_vendor = vendor
    order.assignment_type = "manual"
    order.vendor_status = "assigned"
    order.save()

    log_activity(order, "vendor_assigned",
                 f"Vendor '{vendor.name}' assigned manually",
                 actor="Admin")

    if permanent:
        if not order.product_id:
            return Response({
                "success": False,
                "message": "Product ID missing. Permanent assign not possible."
            }, status=400)

        ProductVendorAssignment.objects.update_or_create(
            store=order.store,
            product_id=order.product_id,
            defaults={
                "product_name": order.product_name,
                "vendor": vendor,
                "is_active": True
            }
        )

        # Retroactively assign same vendor to all existing orders for this product
        unassigned = Order.objects.filter(
            product_id=order.product_id,
            assigned_vendor__isnull=True
        ).exclude(id=order.id)
        for o in unassigned:
            o.assigned_vendor = vendor
            o.assignment_type = "permanent_auto"
            o.vendor_status = "assigned"
            o.save(update_fields=["assigned_vendor", "assignment_type", "vendor_status"])
            log_activity(o, "vendor_assigned",
                         f"Vendor '{vendor.name}' auto-assigned by product mapping",
                         actor="System")

    return Response({
        "success": True,
        "message": "Vendor assigned successfully"
    })


@api_view(["POST"])
def bulk_assign_vendor_api(request):
    order_ids = request.data.get("order_ids", [])
    vendor_id = request.data.get("vendor_id")
    permanent = request.data.get("permanent", False)

    if not order_ids:
        return Response({
            "success": False,
            "message": "order_ids are required"
        }, status=400)

    if not vendor_id:
        return Response({
            "success": False,
            "message": "vendor_id is required"
        }, status=400)

    vendor = get_object_or_404(Vendor, id=vendor_id)

    orders = Order.objects.filter(id__in=order_ids)

    assigned_count = 0
    permanent_count = 0

    for order in orders:
        order.assigned_vendor = vendor
        order.assignment_type = "manual"
        order.vendor_status = "assigned"
        order.save()
        assigned_count += 1

        log_activity(order, "vendor_assigned",
                     f"Vendor '{vendor.name}' assigned manually",
                     actor="Admin")

        if permanent and order.product_id:
            ProductVendorAssignment.objects.update_or_create(
                store=order.store,
                product_id=order.product_id,
                defaults={
                    "product_name": order.product_name,
                    "vendor": vendor,
                    "is_active": True
                }
            )
            permanent_count += 1
            # Retroactively assign to all existing unassigned orders with same product
            for o in Order.objects.filter(product_id=order.product_id, assigned_vendor__isnull=True).exclude(id=order.id):
                o.assigned_vendor = vendor
                o.assignment_type = "permanent_auto"
                o.vendor_status = "assigned"
                o.save(update_fields=["assigned_vendor", "assignment_type", "vendor_status"])
                log_activity(o, "vendor_assigned",
                             f"Vendor '{vendor.name}' auto-assigned by product mapping",
                             actor="System")

    return Response({
        "success": True,
        "assigned_count": assigned_count,
        "permanent_count": permanent_count
    })


@api_view(["POST"])
def remove_product_vendor_assignment_api(request):
    store_id = request.data.get("store_id")
    product_id = request.data.get("product_id")

    if not store_id or not product_id:
        return Response({
            "success": False,
            "message": "store_id and product_id are required"
        }, status=400)

    ProductVendorAssignment.objects.filter(
        store_id=store_id,
        product_id=product_id
    ).delete()

    return Response({
        "success": True,
        "message": "Permanent assignment removed"
    })


@api_view(["POST"])
def save_order_tracking_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    tracking_number = request.data.get("tracking_number", "").strip()
    if not tracking_number:
        return Response({"success": False, "message": "Tracking number is required."}, status=400)
    order.tracking_number = tracking_number
    company = request.data.get("tracking_company", "").strip()
    tracking_url = request.data.get("tracking_url", "").strip()
    # If user accidentally put the URL in the company field, auto-correct
    if company.lower().startswith("http"):
        tracking_url = tracking_url or company
        company = ""
    order.tracking_company = company or order.tracking_company
    # Auto-build tracking URL from courier template if not manually provided
    if not tracking_url and company:
        template = COURIER_URL_TEMPLATES.get(company.lower())
        if template:
            tracking_url = template.format(num=tracking_number)
    order.tracking_url = tracking_url if tracking_url else order.tracking_url

    # Auto-update fulfillment status to shipped (unless already cancelled/failed/completed)
    skip_statuses = {"cancelled", "failed", "completed"}
    status_changed = False
    if (order.fulfillment_status or "").lower() not in skip_statuses:
        order.fulfillment_status = "shipped"
        status_changed = True

    order.save()

    if status_changed:
        _fire_auto_email(order, "shipped")

    log_activity(order, "tracking_added",
                 f"Tracking number added: {tracking_number}",
                 actor="Admin")

    return Response({"success": True, "message": "Tracking number saved."})


@api_view(["POST"])
def fetch_live_tracking_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not order.tracking_url:
        return Response({"success": False, "message": "No tracking URL saved for this order."}, status=400)

    # Already confirmed delivered — never re-scrape, but ensure status is completed
    if order.delivered_at:
        if order.fulfillment_status not in ("completed", "cancelled", "failed"):
            order.fulfillment_status = "completed"
            order.save(update_fields=["fulfillment_status"])
        return Response({"success": True, "status": order.live_tracking_status, "skipped": True})

    from .tracking_scraper import scrape_tracking_status
    status = scrape_tracking_status(order.tracking_url, tracking_number=order.tracking_number or "")

    if status:
        status_low = status.lower()
        is_delivered = "deliver" in status_low or "complet" in status_low
        update_fields = ["live_tracking_status"]
        order.live_tracking_status = status
        if is_delivered and not order.delivered_at:
            order.delivered_at = timezone.now()
            update_fields.append("delivered_at")
            # Auto-update fulfillment status to completed
            order.fulfillment_status = "completed"
            update_fields.append("fulfillment_status")
        order.save(update_fields=update_fields)
        if is_delivered:
            _fire_auto_email(order, "completed")

    return Response({"success": True, "status": status or "No status found on page"})


@api_view(["GET"])
def order_activity_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    activities = order.activities.all()
    data = [
        {
            "id": a.id,
            "type": a.activity_type,
            "description": a.description,
            "actor": a.actor or "",
            "created_at": a.created_at.isoformat(),
        }
        for a in activities
    ]
    return Response({"success": True, "activities": data, "count": len(data)})


@api_view(["POST"])
def setup_webhook_api(request, store_id):
    store = get_object_or_404(Store, id=store_id)

    try:
        from stores.tunnel import get_base_url
        base = get_base_url(request=request, wait_secs=5)
        if store.platform == "woocommerce":
            from .services import setup_woocommerce_webhook
            delivery_url = f"{base}/orders/webhook/woocommerce/{store_id}/"
            webhook_id, created = setup_woocommerce_webhook(store, delivery_url)
        elif store.platform == "shopify":
            from .services import setup_shopify_webhook
            delivery_url = f"{base}/orders/webhook/shopify/{store_id}/"
            webhook_id, created = setup_shopify_webhook(store, delivery_url)
        else:
            return Response({"success": False, "message": "Platform not supported"}, status=400)
        return Response({"success": True, "webhook_id": webhook_id, "created": created})
    except Exception as e:
        return Response({"success": False, "message": str(e)}, status=500)


def orders_page(request):
    return render(request, "dashboard.html")


@csrf_exempt
def shopify_webhook(request, store_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    store = get_object_or_404(Store, id=store_id)

    body = request.body
    sig_header = request.META.get("HTTP_X_SHOPIFY_HMAC_SHA256", "")
    if sig_header and store.api_secret:
        secret = store.api_secret.encode("utf-8")
        expected = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(sig_header, expected):
            return JsonResponse({"success": False, "message": "Invalid signature"}, status=401)

    import json
    try:
        data = json.loads(body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    if not isinstance(data, dict) or "id" not in data:
        return JsonResponse({"success": False, "message": "Invalid payload"}, status=400)

    _, created = process_shopify_order(store, data)
    return JsonResponse({"success": True, "created": created})


@csrf_exempt
def woocommerce_webhook(request, store_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    store = get_object_or_404(Store, id=store_id)

    body = request.body
    sig_header = request.META.get("HTTP_X_WC_WEBHOOK_SIGNATURE", "")
    if sig_header and store.api_secret:
        secret = store.api_secret.encode("utf-8")
        expected = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(sig_header, expected):
            return JsonResponse({"success": False, "message": "Invalid signature"}, status=401)

    import json
    try:
        data = json.loads(body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    if not isinstance(data, dict) or "id" not in data:
        return JsonResponse({"success": False, "message": "Invalid payload"}, status=400)

    _, created = process_woocommerce_order(store, data)
    return JsonResponse({"success": True, "created": created})


@api_view(["POST"])
@permission_classes([AllowAny])
def update_order_status_api(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    new_status = (request.data.get("fulfillment_status") or "").strip()
    if not new_status:
        return Response({"success": False, "message": "fulfillment_status required."}, status=400)
    old_status = order.fulfillment_status or ""
    order.fulfillment_status = new_status
    order.save(update_fields=["fulfillment_status"])
    log_activity(order, "status_changed", f"Status changed from '{old_status}' to '{new_status}'", actor="Admin")
    if new_status.lower() != old_status.lower():
        _fire_auto_email(order, new_status)
    return Response({"success": True, "fulfillment_status": new_status})

@api_view(["GET"])
def order_lookup_by_number_api(request):
    """Look up an order by external_order_id, enforcing per-store access for team members."""
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)

    q = request.GET.get("q", "").strip().lstrip("#")
    if not q:
        return Response({"success": False, "message": "q required."}, status=400)

    # Find the order (any store)
    try:
        order = Order.objects.select_related("store").get(external_order_id=q)
    except Order.DoesNotExist:
        return Response({"success": False, "error": "not_found",
                         "message": f"Order #{q} not found."}, status=404)
    except Order.MultipleObjectsReturned:
        order = Order.objects.select_related("store").filter(external_order_id=q).first()

    # Admins/superusers always have access
    if request.user.is_superuser or request.user.is_staff:
        return Response({"success": True, "id": order.id,
                         "order_number": order.external_order_id,
                         "store_name": order.store.name})

    # Team member — check allowed_stores
    from teamapp.models import TeamMember
    try:
        member = TeamMember.objects.get(user=request.user, is_active=True)
    except TeamMember.DoesNotExist:
        return Response({"success": False, "error": "no_access",
                         "message": "You don't have access to this order."}, status=403)

    allowed = member.permissions.get("allowed_stores", [])
    # Empty list means no restriction was set (full access)
    if allowed and str(order.store_id) not in [str(s) for s in allowed]:
        return Response({"success": False, "error": "no_access",
                         "message": f"You don't have access to orders from «{order.store.name}».",
                         "store_name": order.store.name}, status=403)

    return Response({"success": True, "id": order.id,
                     "order_number": order.external_order_id,
                     "store_name": order.store.name})
