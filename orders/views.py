import hashlib
import hmac
import base64
import requests

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


# ─── Per-user access helpers ──────────────────────────────────────────────
# These extend the existing "store__user=request.user" tenant scope so an
# employee with the matching permission can also act on the tenant's data.
# Pattern: tenant always wins; if not the tenant, check active TeamMember
# under the relevant owner AND the permission flag.
def _employee_for(user):
    """Return the active TeamMember row for this user, or None."""
    if not user or not user.is_authenticated:
        return None
    return TeamMember.objects.filter(user=user, is_active=True).select_related("owner").first()


def _user_can_act_on_store(user, store, perm_key):
    """Tenant owns the store ▸ allowed.
    Employee under the tenant with perm_key granted ▸ allowed.
    Anyone else ▸ blocked. Used by sync_orders, export_orders, etc."""
    if not user or not user.is_authenticated or not store:
        return False
    if store.user_id == user.id:
        return True
    member = _employee_for(user)
    if not member or not member.owner_id:
        return False
    if member.owner_id != store.user_id:
        return False
    if not (member.permissions or {}).get(perm_key):
        return False
    # Respect allowed_stores allow-list if the admin narrowed it.
    allowed = (member.permissions or {}).get("allowed_stores") or []
    if allowed:
        try:
            allowed_ids = {int(s) for s in allowed}
            if store.id not in allowed_ids:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _user_can_act_on_order(user, order, perm_key):
    """Same logic as _user_can_act_on_store but starting from an Order."""
    if not user or not user.is_authenticated or not order or not order.store_id:
        return False
    return _user_can_act_on_store(user, order.store, perm_key)


def _scoped_orders_qs(user, perm_key):
    """Base queryset: every Order the requester can act on with perm_key.
    Tenant gets all their orders; employee gets orders inside the owner's
    stores (narrowed by allowed_stores if set)."""
    if not user or not user.is_authenticated:
        return Order.objects.none()
    own = Order.objects.filter(store__user=user)
    member = _employee_for(user)
    if member and member.owner_id and (member.permissions or {}).get(perm_key):
        emp_qs = Order.objects.filter(store__user=member.owner)
        allowed = (member.permissions or {}).get("allowed_stores") or []
        if allowed:
            try:
                emp_qs = emp_qs.filter(store_id__in=[int(s) for s in allowed])
            except (TypeError, ValueError):
                pass
        return (own | emp_qs).distinct()
    return own


@api_view(["GET"])
def orders_poll_api(request):
    """Lightweight endpoint — returns latest order id + total count. Used by frontend polling."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    store_id = request.GET.get("store_id")
    # Per-user scope: only count orders inside the requester's own stores.
    # No superuser fallback — cross-tenant access belongs in /superadmin/.
    qs = Order.objects.filter(store__user=request.user)
    if store_id:
        qs = qs.filter(store_id=store_id)
    latest = qs.order_by("-id").values("id").first()
    return Response({
        "latest_id": latest["id"] if latest else None,
        "count": qs.count(),
    })


def sync_orders(request, store_id):
    """
    Sync orders from the connected store with optional date range filtering.

    Query / body params:
      - range:      '7d' | '30d' | '90d' | '1y' | 'all' | 'custom'   (default '30d')
      - start_date: ISO YYYY-MM-DD (only used when range='custom')
      - end_date:   ISO YYYY-MM-DD (optional, defaults to today, only for custom)
    """
    from datetime import datetime, timedelta
    from django.utils import timezone as _tz

    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "message": "Authentication required"}, status=401)
    # Per-user scope: tenant always; employees with `sync_orders` permission too.
    store = get_object_or_404(Store, id=store_id)
    if not _user_can_act_on_store(request.user, store, "sync_orders"):
        return JsonResponse({"success": False, "message": "Not allowed."}, status=403)

    # Read params from either GET or POST body
    range_key  = (request.GET.get("range") or request.POST.get("range") or "30d").lower()
    start_raw  = request.GET.get("start_date") or request.POST.get("start_date") or ""
    end_raw    = request.GET.get("end_date")   or request.POST.get("end_date")   or ""

    # Compute the `after` ISO timestamp (and optional end) for the platform sync call
    after_iso = None
    range_label = "Last 30 days"
    now = _tz.now()

    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    range_map = {
        "7d":   (timedelta(days=7),   "Last 7 days"),
        "30d":  (timedelta(days=30),  "Last 30 days"),
        "90d":  (timedelta(days=90),  "Last 90 days"),
        "1y":   (timedelta(days=365), "Last 1 year"),
    }

    if range_key == "all":
        after_iso = None
        range_label = "All time"
    elif range_key == "custom":
        try:
            sd = datetime.strptime(start_raw, "%Y-%m-%d")
            after_iso = _iso(sd)
            range_label = f"Custom · from {start_raw}"
            if end_raw:
                range_label += f" to {end_raw}"
        except Exception:
            return JsonResponse({
                "success": False,
                "message": "Invalid start_date — use YYYY-MM-DD format."
            }, status=400)
    elif range_key in range_map:
        delta, range_label = range_map[range_key]
        after_iso = _iso(now - delta)
    else:
        # Unknown range key — default to 30d
        after_iso = _iso(now - timedelta(days=30))
        range_label = "Last 30 days"

    try:
        if store.platform == "woocommerce":
            count = sync_woocommerce_orders(store, after=after_iso)
        elif store.platform == "shopify":
            count = sync_shopify_orders(store, after=after_iso)
        else:
            return JsonResponse({"success": False, "message": "Platform not supported yet"})
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Sync failed: {e}"}, status=500)

    return JsonResponse({
        "success": True,
        "orders_synced": count,
        "range": range_key,
        "range_label": range_label,
        "after": after_iso,
        "last_synced": store.last_synced.isoformat() if getattr(store, "last_synced", None) else None,
    })


@api_view(["GET"])
def orders_list_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    store_id = request.GET.get("store_id")
    status = request.GET.get("status")
    search = request.GET.get("search")

    # Per-user scope. No superuser fallback — /superadmin/ has its own panel.
    orders = Order.objects.filter(store__user=request.user).order_by("-created_at")

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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    # Per-user scope: tenants can only inspect their own orders.
    order = get_object_or_404(Order, id=order_id, store__user=request.user)
    serializer = OrderSerializer(order)

    return Response({
        "success": True,
        "order": serializer.data,
        "raw_data": order.raw_data
    })


@api_view(["DELETE"])
def delete_order_api(request, order_id):
    """Delete an order. Allowed for the store owner OR an employee with
    `delete_orders` permission whose owner owns the store."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    order = get_object_or_404(Order, id=order_id)
    if not _user_can_act_on_order(request.user, order, "delete_orders"):
        return Response({"success": False, "message": "Not allowed."}, status=403)

    order_num = order.external_order_id or str(order.id)
    order.delete()
    return Response({"success": True, "message": f"Order #{order_num} deleted."})


@api_view(["GET"])
def overview_api(request):
    from datetime import timedelta
    from vendors.models import VendorTrackingSubmission

    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    store_id = request.GET.get("store_id")
    # Per-user scope. No superuser fallback — overview is a tenant page.
    orders = Order.objects.select_related("store", "assigned_vendor").filter(store__user=request.user)
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

    # Store stats — strictly scoped to current user.
    stores_qs = Store.objects.filter(user=request.user, is_active=True)
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

    # Top vendors by order count — strictly scoped to current user's stores.
    top_vendors = []
    vendor_qs = Vendor.objects.filter(status="active", assigned_store__user=request.user)
    for v in vendor_qs[:6]:
        v_orders = orders.filter(assigned_vendor=v)
        top_vendors.append({
            "id": v.id,
            "name": v.name,
            "order_count": v_orders.count(),
            "shipped": v_orders.filter(fulfillment_status__in=["shipped", "completed"]).count(),
        })
    top_vendors.sort(key=lambda x: x["order_count"], reverse=True)

    # Tracking queue — strictly scoped to the current tenant's orders.
    pending_tracking = VendorTrackingSubmission.objects.filter(
        status="pending",
        order__store__user=request.user,
    ).count()

    # Email stats — strictly scoped to current user's stores.
    try:
        from emails.models import EmailMessage, EmailThreadAssignment
        email_qs = EmailMessage.objects.filter(store__user=request.user)
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
        # Open (unresolved) thread assignments — scoped to the tenant's stores.
        open_threads = EmailThreadAssignment.objects.filter(
            is_resolved=False,
            store__user=request.user,
        ).count()
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
        "active_vendors": vendor_qs.count(),
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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    # Tenant OR employee with `assign_orders` permission may assign.
    order = get_object_or_404(Order, id=order_id)
    if not _user_can_act_on_order(request.user, order, "assign_orders"):
        return Response({"success": False, "message": "Not allowed."}, status=403)
    member_id = request.data.get("member_id")

    # Null / empty member_id → UNASSIGN the order
    if member_id in (None, "", 0, "0", "null"):
        previous_name = order.assigned_to.name if order.assigned_to else None
        order.assigned_to = None
        order.save()
        if previous_name:
            log_activity(order, "unassigned",
                         f"Unassigned from {previous_name}",
                         actor="Admin")
        return Response({
            "success": True,
            "message": "Order unassigned." if previous_name else "Order has no team-member assignment.",
            "assigned_to": None,
        })

    # Scope the target member to the relevant tenant (owner of the order's
    # store), not necessarily the requester — covers the employee case where
    # requester != tenant.
    owner_user_id = order.store.user_id
    member = get_object_or_404(TeamMember, id=member_id, owner_id=owner_user_id)

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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    store_id = request.data.get("store_id")

    # Per-user scope: only the requester's own unassigned orders are eligible.
    orders = Order.objects.filter(assigned_to__isnull=True, store__user=request.user)

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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    # Per-user scope on the order being mutated.
    order = get_object_or_404(Order, id=order_id, store__user=request.user)

    vendor_id = request.data.get("vendor_id")
    permanent = request.data.get("permanent", False)

    if not vendor_id:
        return Response({
            "success": False,
            "message": "vendor_id is required"
        }, status=400)

    # Vendor must also belong to this tenant.
    # `assigned_store__user` is the tenant; `Vendor.user` is the vendor's own login.
    vendor = get_object_or_404(Vendor, id=vendor_id, assigned_store__user=request.user)

    order.assigned_vendor = vendor
    order.assignment_type = "manual"
    order.vendor_status = "assigned"
    order.save()

    log_activity(order, "vendor_assigned",
                 f"Vendor '{vendor.name}' assigned manually",
                 actor="Admin")

    # Notify vendor of new assignment
    try:
        from notifications.services import notify
        if vendor.user_id:
            notify(
                recipient=vendor.user,
                audience="vendor",
                category="order",
                priority="high",
                title=f"New order assigned: #{order.external_order_id}",
                body=f"{order.customer_name or 'A customer'} ordered "
                     f"{order.product_name or 'an item'}. Please ship and submit tracking.",
                action_url=f"/vendor/dashboard/#order/{order.id}",
                action_label="View",
                related_order_id=order.id,
            )
    except Exception:
        pass

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

        # ⚠ Intentionally NO retroactive assignment here. Permanent assignment
        # applies only to *future* orders that arrive after this point — the
        # webhook / sync path will pick the rule up via attach_permanent_vendor
        # in orders/services.py. Already-fetched orders keep whatever state
        # the admin had set on them, so a single manual assignment doesn't
        # silently sweep older orders.

    return Response({
        "success": True,
        "message": "Vendor assigned successfully"
    })


@api_view(["POST"])
def bulk_assign_vendor_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
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

    # Vendor must belong to this tenant.
    vendor = get_object_or_404(Vendor, id=vendor_id, assigned_store__user=request.user)

    # Per-user scope on every bulk-target — silently drop ids that aren't ours.
    orders = Order.objects.filter(id__in=order_ids, store__user=request.user)

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
            # ⚠ No retroactive sweep — the rule only catches *future* orders
            # of this product. Already-fetched orders in the bulk selection
            # were assigned above explicitly; everything else stays as-is.

    return Response({
        "success": True,
        "assigned_count": assigned_count,
        "permanent_count": permanent_count
    })


@api_view(["POST"])
def remove_product_vendor_assignment_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    store_id = request.data.get("store_id")
    product_id = request.data.get("product_id")

    if not store_id or not product_id:
        return Response({
            "success": False,
            "message": "store_id and product_id are required"
        }, status=400)

    # Per-user scope: only the requester's own store mappings can be removed.
    ProductVendorAssignment.objects.filter(
        store_id=store_id,
        product_id=product_id,
        store__user=request.user,
    ).delete()

    return Response({
        "success": True,
        "message": "Permanent assignment removed"
    })


@api_view(["POST"])
def save_order_tracking_api(request, order_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    order = get_object_or_404(Order, id=order_id, store__user=request.user)
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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    order = get_object_or_404(Order, id=order_id, store__user=request.user)
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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    order = get_object_or_404(Order, id=order_id, store__user=request.user)
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


# ─── Order Notes & Activity (merged WC + OrderActivity timeline) ────────────

def _classify_activity_kind(activity_type, description):
    """Map OrderActivity.activity_type → UI kind for color coding."""
    t = (activity_type or "").lower()
    if t == "note_private":           return "private"
    if t == "note_customer":          return "customer"
    if t in ("tracking_approved",):   return "success"
    if t in ("tracking_rejected",):   return "failed"
    if t in ("tracking_submitted",
             "tracking_added"):       return "success"
    if t in ("received", "assigned",
             "vendor_assigned"):      return "system"
    # Generic notes default to private
    if t == "note":                   return "private"
    return "system"


def _classify_wc_note_kind(wc_note):
    """Map WooCommerce note → UI kind. WC has: customer_note (bool), note (text)."""
    text = (wc_note.get("note") or "").lower()
    # WC's "system" notes start with phrases like "Order status changed"
    if "declined" in text or "failed" in text or "could not" in text:
        return "failed"
    if "out for delivery" in text or "shipped" in text or "completed" in text:
        return "success"
    if "warning" in text:
        return "warning"
    if wc_note.get("customer_note"):
        return "customer"
    # Plain WC private notes
    if "order status changed" in text or "added by" in text or "stock reduced" in text:
        return "system"
    return "private"


def _fetch_wc_notes(order):
    """Pull notes from WooCommerce for this order. Returns [] if not WC or on error."""
    store = order.store
    if not store or (store.platform or "").lower() != "woocommerce":
        return []
    if not store.api_key or not store.api_secret or not order.external_order_id:
        return []
    try:
        url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/orders/{order.external_order_id}/notes"
        r = requests.get(url, auth=(store.api_key, store.api_secret),
                         params={"type": "any"}, timeout=8)
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:
        return []


def _push_wc_note(order, text, is_customer_note=False):
    """POST a note to WooCommerce. Returns (success, wc_note_id_or_error_msg)."""
    store = order.store
    if not store or (store.platform or "").lower() != "woocommerce":
        return False, "Not a WooCommerce order"
    if not store.api_key or not store.api_secret or not order.external_order_id:
        return False, "Store not connected"
    try:
        url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/orders/{order.external_order_id}/notes"
        r = requests.post(
            url, auth=(store.api_key, store.api_secret),
            json={"note": text, "customer_note": bool(is_customer_note)},
            timeout=10,
        )
        if r.status_code in (200, 201):
            return True, r.json().get("id")
        return False, f"WC API {r.status_code}"
    except Exception as e:
        return False, str(e)


@api_view(["GET", "POST"])
def order_notes_api(request, order_id):
    """GET: merged WC + OrderActivity timeline (newest first)
       POST: add a private/customer note (saves locally + pushes to WC)."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    # Per-user scope. No superuser fallback (memory rule).
    order = get_object_or_404(Order, id=order_id, store__user=request.user)

    if request.method == "POST":
        text = (request.data.get("text") or "").strip()
        kind = (request.data.get("kind") or "private").lower()
        if kind not in ("private", "customer"):
            kind = "private"
        if not text:
            return Response({"success": False, "message": "Note text is required."}, status=400)

        # Save locally with kind encoded into activity_type
        actor_name = request.user.get_full_name() or request.user.username
        activity_type = "note_customer" if kind == "customer" else "note_private"
        from .models import OrderActivity
        act = OrderActivity.objects.create(
            order=order, activity_type=activity_type,
            description=text, actor=actor_name,
        )

        # Push to WooCommerce too (so the WC admin stays in sync)
        wc_ok, wc_info = _push_wc_note(order, text, is_customer_note=(kind == "customer"))

        # If customer-facing, also email via the store's connected Gmail
        # (per project rule: never Brevo — only send via tenant's own inbox).
        if kind == "customer" and order.customer_email:
            try:
                from emails.views import send_email_with_store_account
                subject = f"Update on your order #{order.external_order_id}"
                send_email_with_store_account(
                    store=order.store,
                    recipient=order.customer_email,
                    subject=subject,
                    body=f"<p>Hi {order.customer_name or 'there'},</p><p>{text}</p>",
                )
            except Exception:
                # Don't block the note save if email fails — note is still saved.
                pass

        return Response({
            "success": True,
            "id": act.id,
            "wc_pushed": wc_ok,
            "wc_info": wc_info,
        })

    # ── GET: merged timeline ──────────────────────────────────────────────
    # Scope per tenant request: ONLY show payment-related events + tenant
    # notes here. Vendor assignments, auto-routes, tracking submissions,
    # and generic system events are tracked elsewhere and don't belong on
    # this panel.
    PAYMENT_KEYWORDS = (
        "payment", "paid", "declined", "failed", "refund", "captured",
        "authorized", "charge", "instrument", "voided", "void",
        "transaction", "card was", "settled", "stripe", "paypal",
    )
    NOTE_TYPES = {"note_private", "note_customer", "note"}
    PAYMENT_ACTIVITY_TYPES = {
        "payment", "payment_received", "payment_failed",
        "payment_refunded", "refund", "refunded",
    }
    def _is_payment_text(text):
        t = (text or "").lower()
        return any(k in t for k in PAYMENT_KEYWORDS)

    merged = []

    # 1) Drop Sigma OrderActivity rows — keep tenant notes + payment events.
    current_username = request.user.username
    for a in order.activities.all():
        atype = (a.activity_type or "").lower()
        is_note = atype in NOTE_TYPES
        is_payment = atype in PAYMENT_ACTIVITY_TYPES or _is_payment_text(a.description)
        if not (is_note or is_payment):
            continue
        kind = _classify_activity_kind(a.activity_type, a.description)
        actor_owns = is_note and (
            request.user.is_superuser
            or (a.actor and (
                a.actor == current_username
                or a.actor == request.user.get_full_name()
            ))
        )
        merged.append({
            "id":         a.id,
            "kind":       kind,
            "label":      None,
            "body":       a.description,
            "actor":      a.actor or "—",
            "source":     "Drop Sigma",
            "at":         a.created_at.isoformat(),
            "can_delete": bool(actor_owns),
        })

    # 2) WooCommerce order notes — keep ONLY payment-related notes
    #    (declined, refunded, paid, captured…) and customer-authored notes.
    wc_synced = False
    wc_notes = _fetch_wc_notes(order)
    if wc_notes:
        wc_synced = True
    for wn in wc_notes:
        body = wn.get("note") or ""
        if not (_is_payment_text(body) or wn.get("customer_note")):
            continue
        kind = _classify_wc_note_kind(wn)
        merged.append({
            "id":         f"wc-{wn.get('id')}",
            "kind":       kind,
            "label":      None,
            "body":       body,
            "actor":      wn.get("author") or "WooCommerce",
            "source":     "WooCommerce",
            "at":         wn.get("date_created_gmt") or wn.get("date_created") or "",
            "can_delete": False,
        })

    # Sort newest first
    def _sort_key(n):
        return n.get("at") or ""
    merged.sort(key=_sort_key, reverse=True)

    return Response({
        "success":   True,
        "notes":     merged,
        "count":     len(merged),
        "wc_synced": wc_synced,
    })


@api_view(["DELETE"])
def order_note_delete_api(request, note_id):
    """Delete a tenant-created note. Only the author (or a superuser) can delete."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    from .models import OrderActivity
    act = get_object_or_404(OrderActivity, id=note_id)
    # Scope: order must belong to a store the user owns
    if not request.user.is_superuser and act.order.store and act.order.store.user_id != request.user.id:
        return Response({"success": False, "message": "Not authorised."}, status=403)
    # Only tenant-added notes can be deleted (immutable system events stay)
    if act.activity_type not in ("note_private", "note_customer", "note"):
        return Response({"success": False, "message": "System events cannot be deleted."}, status=400)
    # Author check
    if not request.user.is_superuser:
        author_username = act.actor or ""
        if author_username not in (request.user.username, request.user.get_full_name()):
            return Response({"success": False, "message": "Only the note author can delete it."}, status=403)
    act.delete()
    return Response({"success": True})


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


# ════════════════════════════════════════════════════════════════════
# SHOPIFY APP STORE COMPLIANCE — required webhooks
# ────────────────────────────────────────────────────────────────────
# Every Shopify app on the App Store MUST handle these 4 webhooks:
#   1. app/uninstalled                — cleanup when merchant removes app
#   2. customers/data_request         — GDPR data export request
#   3. customers/redact               — GDPR data deletion request
#   4. shop/redact                    — GDPR shop deletion (48h post-uninstall)
#
# All four endpoints verify the X-Shopify-Hmac-Sha256 signature using
# the app's CLIENT SECRET (not the per-store api_secret like orders).
# Shopify's review system will probe these URLs during submission.
# ════════════════════════════════════════════════════════════════════

def _verify_shopify_app_hmac(request):
    """Validate the HMAC header sent by Shopify on app-level webhooks.
    These are signed with the global SHOPIFY_API_SECRET (app secret),
    NOT a per-store secret. Returns (ok, body_bytes)."""
    from django.conf import settings as _settings
    body = request.body
    sig_header = request.META.get("HTTP_X_SHOPIFY_HMAC_SHA256", "")
    app_secret = (getattr(_settings, "SHOPIFY_API_SECRET", "") or "").encode("utf-8")
    if not (sig_header and app_secret):
        return False, body
    expected = base64.b64encode(
        hmac.new(app_secret, body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(sig_header, expected), body


@csrf_exempt
def shopify_app_uninstalled_webhook(request):
    """Fires when a merchant uninstalls Drop Sigma from their Shopify
    store. We deactivate the store record + clear the access_token so
    we never accidentally hit Shopify with a stale token.

    Endpoint: POST /orders/webhook/shopify/app-uninstalled/
    Configured per-app in Partner Dashboard → Webhooks subscription."""
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    ok, body = _verify_shopify_app_hmac(request)
    if not ok:
        return JsonResponse({"success": False, "message": "Invalid HMAC"}, status=401)

    shop_domain = (request.META.get("HTTP_X_SHOPIFY_SHOP_DOMAIN") or "").strip().lower()
    if not shop_domain:
        return JsonResponse({"success": False, "message": "Missing shop domain"}, status=400)

    # Find matching stores. Match both with and without https:// prefix
    # since older rows may have inconsistent formatting.
    candidates = [
        shop_domain,
        f"https://{shop_domain}",
        f"https://{shop_domain}/",
        f"http://{shop_domain}",
    ]
    affected = Store.objects.filter(
        platform="shopify",
        store_url__in=candidates,
    )
    affected.update(
        is_active=False,
        access_token="",  # invalidate token — merchant has revoked access
    )

    return JsonResponse({"success": True, "deactivated": affected.count()})


@csrf_exempt
def shopify_customers_data_request_webhook(request):
    """GDPR: a customer has requested a copy of their stored personal
    data. Per Shopify policy we must respond within 30 days. We log
    the request — actual data export is fulfilled out-of-band by
    Drop Sigma support (since the request includes minimal info).

    Endpoint: POST /orders/webhook/shopify/customers-data-request/"""
    import logging, json as _json
    log = logging.getLogger(__name__)

    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    ok, body = _verify_shopify_app_hmac(request)
    if not ok:
        return JsonResponse({"success": False, "message": "Invalid HMAC"}, status=401)

    try:
        payload = _json.loads(body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    # Shopify guarantees these fields. We persist them as a structured
    # entry so the support team can fulfil within the SLA.
    log.warning(
        "[Shopify GDPR] customers/data_request — shop_id=%s shop_domain=%s customer=%s orders=%s",
        payload.get("shop_id"),
        payload.get("shop_domain"),
        (payload.get("customer") or {}).get("email"),
        payload.get("orders_requested"),
    )
    # Persist to DB so support can action it
    try:
        from .models import ShopifyGdprRequest
        ShopifyGdprRequest.objects.create(
            request_type="data_request",
            shop_domain=payload.get("shop_domain", ""),
            customer_email=(payload.get("customer") or {}).get("email", "") or "",
            payload=payload,
        )
    except Exception:
        # Table may not exist yet on first deploy — soft fail
        pass

    return JsonResponse({"success": True})


@csrf_exempt
def shopify_customers_redact_webhook(request):
    """GDPR: a customer has requested deletion of their personal data.
    We anonymize any Order rows that referenced that email + log the
    redact request for audit.

    Endpoint: POST /orders/webhook/shopify/customers-redact/"""
    import logging, json as _json
    log = logging.getLogger(__name__)

    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    ok, body = _verify_shopify_app_hmac(request)
    if not ok:
        return JsonResponse({"success": False, "message": "Invalid HMAC"}, status=401)

    try:
        payload = _json.loads(body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    shop_domain = payload.get("shop_domain", "")
    customer_email = (payload.get("customer") or {}).get("email", "") or ""

    # Anonymize matching Order rows for this customer in this shop.
    anonymized = 0
    if shop_domain and customer_email:
        candidate_urls = [
            shop_domain, f"https://{shop_domain}", f"https://{shop_domain}/",
        ]
        anonymized = Order.objects.filter(
            store__store_url__in=candidate_urls,
            customer_email__iexact=customer_email,
        ).update(
            customer_name="[redacted]",
            customer_email="",
            customer_phone="",
        )

    log.warning(
        "[Shopify GDPR] customers/redact — shop=%s customer=%s anonymized=%d",
        shop_domain, customer_email, anonymized,
    )
    try:
        from .models import ShopifyGdprRequest
        ShopifyGdprRequest.objects.create(
            request_type="customers_redact",
            shop_domain=shop_domain,
            customer_email=customer_email,
            payload=payload,
        )
    except Exception:
        pass

    return JsonResponse({"success": True, "anonymized": anonymized})


@csrf_exempt
def shopify_shop_redact_webhook(request):
    """GDPR: the shop has been uninstalled for >48 hours and Shopify
    is now requesting full deletion of all data we hold about it.
    We delete the Store row + cascade through Orders, Customers, etc.

    Endpoint: POST /orders/webhook/shopify/shop-redact/"""
    import logging, json as _json
    log = logging.getLogger(__name__)

    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    ok, body = _verify_shopify_app_hmac(request)
    if not ok:
        return JsonResponse({"success": False, "message": "Invalid HMAC"}, status=401)

    try:
        payload = _json.loads(body)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    shop_domain = (payload.get("shop_domain") or "").strip().lower()
    if not shop_domain:
        return JsonResponse({"success": False, "message": "Missing shop domain"}, status=400)

    candidate_urls = [
        shop_domain, f"https://{shop_domain}", f"https://{shop_domain}/",
    ]
    deleted_stores = 0
    for store in Store.objects.filter(
        platform="shopify",
        store_url__in=candidate_urls,
    ):
        deleted_stores += 1
        try:
            store.delete()  # cascades through Orders + related rows
        except Exception:
            log.exception("Failed to delete store on shop/redact")

    log.warning("[Shopify GDPR] shop/redact — shop=%s deleted=%d", shop_domain, deleted_stores)
    try:
        from .models import ShopifyGdprRequest
        ShopifyGdprRequest.objects.create(
            request_type="shop_redact",
            shop_domain=shop_domain,
            customer_email="",
            payload=payload,
        )
    except Exception:
        pass

    return JsonResponse({"success": True, "deleted_stores": deleted_stores})


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
    """Look up an order by external_order_id, scoped to the requester:
      • Tenant (store owner)   → orders inside their own stores
      • Vendor                 → orders where the vendor is assigned
      • Team member (employee) → orders in their owner's stores, optionally
                                 restricted by `allowed_stores` permission
      • Superuser              → any order (admin support)
    """
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)

    q = request.GET.get("q", "").strip().lstrip("#")
    if not q:
        return Response({"success": False, "message": "q required."}, status=400)

    # Build base queryset on the external order id — there may be the same
    # number across different tenant stores, so we pull matches and then
    # filter by who's asking.
    base_qs = Order.objects.select_related("store").filter(external_order_id=q)
    if not base_qs.exists():
        return Response({"success": False, "error": "not_found",
                         "message": f"Order #{q} not found."}, status=404)

    # ── Tenant scope (store owner) — most common path
    tenant_qs = base_qs.filter(store__user=request.user)
    if tenant_qs.exists():
        order = tenant_qs.first()
        return Response({"success": True, "id": order.id,
                         "order_number": order.external_order_id,
                         "store_name": order.store.name})

    # ── Vendor scope — assigned orders only
    try:
        from vendors.models import Vendor as _Vendor
        vp = _Vendor.objects.filter(user=request.user).first()
        if vp:
            vendor_qs = base_qs.filter(assigned_vendor=vp)
            if vendor_qs.exists():
                order = vendor_qs.first()
                return Response({"success": True, "id": order.id,
                                 "order_number": order.external_order_id,
                                 "store_name": order.store.name})
            # Match exists but not assigned to this vendor → access denied
            return Response({"success": False, "error": "no_access",
                             "message": f"Order #{q} is not assigned to you.",
                             "store_name": base_qs.first().store.name}, status=403)
    except Exception:
        pass

    # ── Team member (employee) scope — limited by owner + allowed_stores
    from teamapp.models import TeamMember
    member = TeamMember.objects.filter(user=request.user, is_active=True).first()
    if member:
        team_qs = base_qs.filter(store__user=member.owner) if member.owner_id else base_qs.none()
        if team_qs.exists():
            order = team_qs.first()
            allowed = (member.permissions or {}).get("allowed_stores", [])
            # Empty list = no restriction; otherwise must include this store.
            if allowed and str(order.store_id) not in [str(s) for s in allowed]:
                return Response({"success": False, "error": "no_access",
                                 "message": f"You don't have access to orders from «{order.store.name}».",
                                 "store_name": order.store.name}, status=403)
            return Response({"success": True, "id": order.id,
                             "order_number": order.external_order_id,
                             "store_name": order.store.name})

    # ── Superuser fallback (support / impersonation) — any order
    if request.user.is_superuser:
        order = base_qs.first()
        return Response({"success": True, "id": order.id,
                         "order_number": order.external_order_id,
                         "store_name": order.store.name})

    return Response({"success": False, "error": "no_access",
                     "message": "You don't have access to this order.",
                     "store_name": base_qs.first().store.name}, status=403)


# ════════════════════════════════════════════════════════════════════════════
# 📥📤 BULK CSV / XLSX IMPORT + EXPORT
# ════════════════════════════════════════════════════════════════════════════

ORDERS_EXPORT_COLUMNS = [
    "Order #",
    "Customer Name",
    "Customer Email",
    "Customer Phone",
    "Store",
    "Product",
    "Total",
    "Currency",
    "Payment Status",
    "Fulfillment Status",
    "Tracking Number",
    "Tracking Company",
    "Tracking Status",
    "Live Tracking Status",
    "City",
    "Country",
    "Created At",
    "Delivered At",
]


def _orders_row(o):
    return [
        o.external_order_id or "",
        o.customer_name or "",
        o.customer_email or "",
        o.customer_phone or "",
        o.store.name if o.store_id else "",
        o.product_name or "",
        float(o.total_price) if o.total_price is not None else 0,
        o.currency or "",
        o.payment_status or "",
        o.fulfillment_status or "",
        o.tracking_number or "",
        o.tracking_company or "",
        o.tracking_status or "",
        o.live_tracking_status or "",
        o.city or "",
        o.country or "",
        o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
        o.delivered_at.strftime("%Y-%m-%d %H:%M:%S") if o.delivered_at else "",
    ]


@csrf_exempt
def orders_export_api(request):
    """Export orders as CSV or XLSX with optional filters.
    Allowed for the store owner OR an employee with `export_orders`."""
    import csv as _csv
    import io as _io
    from django.http import HttpResponse

    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "message": "Authentication required"}, status=401)

    fmt      = (request.GET.get("format") or "csv").lower()
    store_id = request.GET.get("store_id")
    status   = request.GET.get("status")
    search   = request.GET.get("search")

    # Per-user scope via the helper — tenant gets own orders; employee
    # with export_orders gets owner's orders narrowed by allowed_stores.
    qs = _scoped_orders_qs(request.user, "export_orders").select_related("store").order_by("-created_at")
    if store_id:
        qs = qs.filter(store_id=store_id)
    if status:
        qs = qs.filter(payment_status__icontains=status)
    if search:
        qs = (
            qs.filter(customer_name__icontains=search)
            | qs.filter(customer_email__icontains=search)
            | qs.filter(external_order_id__icontains=search)
        )

    rows = [_orders_row(o) for o in qs]

    if fmt == "xlsx":
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Orders"

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(fill_type="solid", fgColor="7C3AED")
        for col, h in enumerate(ORDERS_EXPORT_COLUMNS, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for row_idx, row in enumerate(rows, 2):
            for col_idx, val in enumerate(row, 1):
                ws.cell(row=row_idx, column=col_idx, value=val)

        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) + 4
                      for i, h in enumerate(ORDERS_EXPORT_COLUMNS)]
        for i, width in enumerate(col_widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(width, 50)

        ws.freeze_panes = "A2"

        output = _io.BytesIO()
        wb.save(output)
        output.seek(0)
        response = HttpResponse(
            output.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="orders_export.xlsx"'
        return response

    # Default: CSV
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orders_export.csv"'
    writer = _csv.writer(response)
    writer.writerow(ORDERS_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(row)
    return response


# Mapping of import header (lowercased, accepts both " "/" _") → Order field
_IMPORT_FIELD_MAP = {
    "order_number":         "external_order_id",
    "order_#":              "external_order_id",
    "order#":               "external_order_id",
    "external_order_id":    "external_order_id",
    "customer_name":        "customer_name",
    "customer_email":       "customer_email",
    "customer_phone":       "customer_phone",
    "product":              "product_name",
    "product_name":         "product_name",
    "total":                "total_price",
    "total_price":          "total_price",
    "currency":             "currency",
    "payment_status":       "payment_status",
    "fulfillment_status":   "fulfillment_status",
    "tracking_number":      "tracking_number",
    "tracking_company":     "tracking_company",
    "tracking_status":      "tracking_status",
    "live_tracking_status": "live_tracking_status",
    "city":                 "city",
    "country":              "country",
}


def _normalize_key(k):
    return (k or "").strip().lower().replace(" ", "_")


def _parse_orders_upload(file_obj, filename):
    """Parse CSV / XLSX → list of dicts (lowercased keys). Returns (rows, error)."""
    import csv as _csv
    import io as _io
    name = (filename or "").lower()
    try:
        if name.endswith(".xlsx") or name.endswith(".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers_raw = next(rows_iter, [])
            headers = [_normalize_key(str(h) if h else "") for h in headers_raw]
            result = []
            for row in rows_iter:
                if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
                    continue  # skip blank lines
                result.append({
                    headers[i]: (str(row[i]).strip() if i < len(row) and row[i] is not None else "")
                    for i in range(len(headers))
                })
            return result, None
        else:
            text = file_obj.read().decode("utf-8-sig")
            reader = _csv.DictReader(_io.StringIO(text))
            return [
                {_normalize_key(k): (v or "").strip() for k, v in row.items()}
                for row in reader
            ], None
    except Exception as e:
        return [], f"Unable to parse file: {e}"


@csrf_exempt
def orders_import_api(request):
    """
    Bulk import orders from CSV or XLSX. Each row creates or updates an Order.
    Rows are matched/updated by (store, external_order_id).
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST required"}, status=405)
    store_id = request.POST.get("store_id")
    upload_file = request.FILES.get("file")
    if not store_id or not upload_file:
        return JsonResponse({"success": False, "message": "store_id and file are required."}, status=400)

    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return JsonResponse({"success": False, "message": "Store not found."}, status=404)

    rows, err = _parse_orders_upload(upload_file, upload_file.name)
    if err:
        return JsonResponse({"success": False, "message": err}, status=400)
    if not rows:
        return JsonResponse({"success": False, "message": "File contains no rows."}, status=400)

    created_count = 0
    updated_count = 0
    error_count   = 0
    errors        = []

    for i, row in enumerate(rows, start=2):  # row 2 = first data row in the file
        try:
            order_no = (row.get("order_number")
                        or row.get("order_#")
                        or row.get("order#")
                        or row.get("external_order_id")
                        or "").strip()
            if not order_no:
                raise ValueError("Missing order number column")

            defaults = {}
            for k, v in row.items():
                field = _IMPORT_FIELD_MAP.get(k)
                if not field or field == "external_order_id":
                    continue
                if field == "total_price":
                    try:
                        defaults[field] = float(str(v).replace(",", "")) if v else 0
                    except Exception:
                        defaults[field] = 0
                else:
                    defaults[field] = v or ""

            _, created = Order.objects.update_or_create(
                store=store,
                external_order_id=order_no,
                defaults=defaults,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        except Exception as e:
            error_count += 1
            errors.append(f"Row {i}: {e}")

    return JsonResponse({
        "success": True,
        "created": created_count,
        "updated": updated_count,
        "errors":  error_count,
        "total":   len(rows),
        "error_details": errors[:20],
    })
