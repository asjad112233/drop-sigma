"""Order-related ops views — the bulk of the /ops/ portal.

These endpoints power the Order Queue, Quote workspace, Procurement,
Shipping desk, Activity log and internal team chat. URL routes are
wired in `sourcing_ops/urls.py`.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .permissions import ops_required
from .models import (
    Supplier,
    SupplierProduct,
    OpsTeamMember,
    OpsAssignment,
    OrderOpsState,
    OpsActivity,
    OpsMessage,
)
from .views import (
    _parse_body,
    _dec,
    _dec_to_float,
    _iso,
    _short_dt,
    _human_ago,
    _country_flag,
    serialize_supplier,
    serialize_supplier_product,
    serialize_team_member,
    serialize_order_brief,
)


# ───────────────────────────────────────────────────────────────────────
# Internal helpers
# ───────────────────────────────────────────────────────────────────────
def _get_order_or_404(order_id):
    """Fetch a tenant Order with the related objects we need most often."""
    from orders.models import Order
    return (Order.objects
            .select_related("store", "store__user")
            .filter(pk=order_id)
            .first())


def _ensure_state(order):
    state, _ = OrderOpsState.objects.get_or_create(order=order)
    return state


def _actor_label(user):
    profile = getattr(user, "ops_profile", None)
    if profile and profile.display_name:
        return profile.display_name
    return user.get_full_name() or user.username


def _log_activity(order, *, kind, title, detail="", icon="", actor=None):
    label = ""
    if actor and actor.is_authenticated:
        label = _actor_label(actor)
    OpsActivity.objects.create(
        order=order,
        kind=kind,
        title=title,
        detail=detail,
        icon=icon,
        actor_user=actor if actor and actor.is_authenticated else None,
        actor_label=label,
    )


def _auto_match_supplier_products(sku):
    if not sku:
        return SupplierProduct.objects.none()
    return (SupplierProduct.objects
            .select_related("supplier")
            .filter(sku__iexact=sku, supplier__is_active=True)
            .order_by("-is_preferred", "-times_ordered"))


def _line_items(order):
    raw = order.raw_data or {}
    if not isinstance(raw, dict):
        return []
    items = raw.get("line_items")
    return items if isinstance(items, list) else []


def _first_line_item_sku(order):
    items = _line_items(order)
    if not items:
        return ""
    first = items[0] or {}
    return first.get("sku") or ""


def _to_float(v, default=0.0):
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _compute_merchant_breakdown(order):
    """Reconstruct the merchant-side money breakdown for the Quote workspace.

    Pulls explicit discount/shipping/tax from raw_data when available, and
    derives an implicit discount (line items sum − merchant total) when the
    line items don't add up to the order total. Returns floats throughout.
    """
    raw = order.raw_data or {}
    if not isinstance(raw, dict):
        raw = {}
    items = raw.get("line_items") if isinstance(raw.get("line_items"), list) else []

    # Per-line price × qty (gross, before any discount)
    lines_subtotal = 0.0
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = _to_float(it.get("quantity") or it.get("qty") or 1, 1)
        unit = _to_float(it.get("price"), 0)
        lines_subtotal += unit * qty

    # Explicit fields from common platforms (Woo / Shopify variants)
    explicit_discount = _to_float(
        raw.get("discount_total") or raw.get("total_discounts") or
        raw.get("total_discount") or 0
    )
    explicit_shipping = _to_float(
        raw.get("shipping_total") or raw.get("total_shipping") or
        raw.get("shipping_price") or 0
    )
    explicit_tax = _to_float(
        raw.get("total_tax") or raw.get("total_taxes") or raw.get("tax_total") or 0
    )

    merchant_total = _to_float(order.total_price or 0)

    # Implicit discount: if line items sum is higher than what was actually
    # charged (after accounting for shipping/tax), the difference is treated
    # as a discount even when discount_total is reported as 0.
    derived_discount = lines_subtotal + explicit_shipping + explicit_tax - merchant_total
    if derived_discount < 0.01:
        derived_discount = 0.0
    discount = max(explicit_discount, derived_discount)
    discount = round(discount, 2)

    return {
        "merchant_lines_subtotal_usd": round(lines_subtotal, 2),
        "merchant_discount_usd":       discount,
        "merchant_shipping_usd":       round(explicit_shipping, 2),
        "merchant_tax_usd":            round(explicit_tax, 2),
    }


def _serialize_line_items(items):
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        img = ""
        raw_img = it.get("image")
        if isinstance(raw_img, dict):
            img = raw_img.get("src") or ""
        elif isinstance(raw_img, str):
            img = raw_img
        out.append({
            "name":      it.get("name") or it.get("title") or "",
            "sku":       it.get("sku") or "",
            "variant":   it.get("variant_title") or it.get("variant") or "",
            "quantity":  it.get("quantity") or it.get("qty") or 1,
            "price":     it.get("price") or it.get("total") or "",
            "image_url": img,
        })
    return out


def _serialize_assignment(a):
    if not a:
        return None
    member = a.member
    return {
        "id":           a.id,
        "stage":        a.stage,
        "stage_label":  a.get_stage_display(),
        "is_active":    a.is_active,
        "note":         a.note,
        "created_iso":  _iso(a.created_at),
        "created_ago":  _human_ago(a.created_at),
        "member_id":    member.id if member else None,
        "member_name":  (member.display_name or member.user.username) if member else "—",
        "member_role":  (member.role.name if member and member.role_id else ""),
        "member_color": (member.role.color if member and member.role_id else "#94a3b8"),
        "assigned_by":  (a.assigned_by.get_full_name() or a.assigned_by.username) if a.assigned_by_id else "",
    }


def _serialize_activity(a):
    return {
        "id":          a.id,
        "kind":        a.kind,
        "kind_label":  a.get_kind_display(),
        "title":       a.title,
        "detail":      a.detail,
        "icon":        a.icon or "•",
        "actor":       a.actor_label or "system",
        "created_iso": _iso(a.created_at),
        "created_ago": _human_ago(a.created_at),
        "created_at":  _short_dt(a.created_at),
    }


def _serialize_message(m):
    author = m.author
    profile = getattr(author, "ops_profile", None)
    return {
        "id":            m.id,
        "body":          m.body,
        "is_internal":   m.is_internal,
        "author_id":     m.author_id,
        "author_name":   (profile.display_name if profile and profile.display_name
                          else (author.get_full_name() or author.username)),
        "author_color":  (profile.role.color if profile and profile.role_id else "#6366f1"),
        "created_iso":   _iso(m.created_at),
        "created_ago":   _human_ago(m.created_at),
        "created_at":    _short_dt(m.created_at),
    }


def _serialize_state(state):
    if not state:
        return {
            "quote_status":       "none",
            "quote_status_label": "Not yet quoted",
            "procurement_status": "not_started",
            "procurement_status_label": "Not started",
            "qc_status":          "not_scheduled",
            "qc_status_label":    "Not scheduled",
            "supplier":           None,
            "supplier_product":   None,
            "supplier_auto_matched": False,
            "quoted_lead_days":   None,
            "internal_notes":     "",
            "delay_reason":       "",
            "quote_sent_at_iso":  None,
            "po_sent_at_iso":     None,
            "shipped_at_iso":     None,
            "delivered_at_iso":   None,
        }
    return {
        "quote_status":       state.quote_status,
        "quote_status_label": state.get_quote_status_display(),
        "procurement_status": state.procurement_status,
        "procurement_status_label": state.get_procurement_status_display(),
        "qc_status":          state.qc_status,
        "qc_status_label":    state.get_qc_status_display(),
        "supplier":           serialize_supplier(state.supplier) if state.supplier_id else None,
        "supplier_product":   serialize_supplier_product(state.supplier_product) if state.supplier_product_id else None,
        "supplier_auto_matched": state.supplier_auto_matched,
        "supplier_cost_usd":  _dec_to_float(state.supplier_cost_usd),
        "supplier_shipping_usd": _dec_to_float(state.supplier_shipping_usd),
        "tenant_product_usd": _dec_to_float(state.tenant_product_usd),
        "tenant_shipping_usd": _dec_to_float(state.tenant_shipping_usd),
        "tenant_total_usd":   _dec_to_float(state.tenant_total_usd),
        "line_item_costs":    state.line_item_costs or [],
        "margin_pct":         _dec_to_float(state.margin_pct),
        "quoted_lead_days":   state.quoted_lead_days,
        "internal_notes":     state.internal_notes,
        "delay_reason":       state.delay_reason,
        "quote_sent_at_iso":  _iso(state.quote_sent_at),
        "quote_sent_at":      _short_dt(state.quote_sent_at),
        "po_sent_at_iso":     _iso(state.po_sent_at),
        "po_sent_at":         _short_dt(state.po_sent_at),
        "production_started_at_iso": _iso(state.production_started_at),
        "qc_scheduled_at_iso": _iso(state.qc_scheduled_at),
        "qc_completed_at_iso": _iso(state.qc_completed_at),
        "shipped_at_iso":     _iso(state.shipped_at),
        "delivered_at_iso":   _iso(state.delivered_at),
        "quoted_by":          (state.quoted_by.get_full_name() or state.quoted_by.username) if state.quoted_by_id else "",
    }


# ───────────────────────────────────────────────────────────────────────
# Orders list
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_orders_list(request):
    from orders.models import Order

    qs = (Order.objects
            .select_related("store", "store__user", "ops_state", "ops_state__supplier")
            .prefetch_related("ops_assignments__member__user", "ops_assignments__member__role"))

    status = (request.GET.get("status") or "").strip()
    if status and status != "all":
        if status == "active":
            qs = qs.exclude(sourcing_status__in=["delivered", "cancel"])
        else:
            qs = qs.filter(sourcing_status=status)

    search = (request.GET.get("search") or "").strip()
    if search:
        sq = (
            Q(external_order_id__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(customer_email__icontains=search)
            | Q(customer_phone__icontains=search)
            | Q(product_name__icontains=search)
            | Q(store__name__icontains=search)
            | Q(store__user__username__icontains=search)
        )
        if search.isdigit():
            sq |= Q(pk=int(search))
        elif search.upper().startswith("DS-"):
            tail = search[3:].lstrip("0")
            if tail.isdigit():
                sq |= Q(pk=int(tail))
        qs = qs.filter(sq)

    assigned = (request.GET.get("assigned") or "").strip()
    if assigned == "unassigned":
        qs = qs.exclude(ops_assignments__is_active=True)
    elif assigned == "me":
        profile = getattr(request.user, "ops_profile", None)
        if profile:
            qs = qs.filter(ops_assignments__is_active=True,
                           ops_assignments__member=profile)
        else:
            qs = qs.none()
    elif assigned.isdigit():
        qs = qs.filter(ops_assignments__is_active=True,
                       ops_assignments__member_id=int(assigned))

    supplier = (request.GET.get("supplier") or "").strip()
    if supplier.isdigit():
        qs = qs.filter(ops_state__supplier_id=int(supplier))

    sort = (request.GET.get("sort") or "newest").strip()
    if sort == "oldest":
        qs = qs.order_by("created_at")
    elif sort == "amount_desc":
        qs = qs.order_by("-sourcing_total_usd", "-created_at")
    elif sort == "amount_asc":
        qs = qs.order_by("sourcing_total_usd", "-created_at")
    elif sort == "customer":
        qs = qs.order_by("customer_name", "-created_at")
    else:
        qs = qs.order_by("-created_at")

    try:
        limit = max(1, min(int(request.GET.get("limit") or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    qs = qs.distinct()[:limit]

    rows = [serialize_order_brief(o) for o in qs]
    return JsonResponse({"ok": True, "orders": rows, "count": len(rows)})


# ───────────────────────────────────────────────────────────────────────
# Order detail
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_order_detail(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    state = getattr(order, "ops_state", None)
    assignments = list(
        order.ops_assignments
        .select_related("member__user", "member__role", "assigned_by")
        .order_by("-is_active", "-created_at")[:20]
    )
    items = _line_items(order)
    first_sku = _first_line_item_sku(order)
    match_qs = list(_auto_match_supplier_products(first_sku)[:8]) if first_sku else []

    tenant = order.store.user if order.store_id and order.store.user_id else None
    addr = order.shipping_address or {}
    billing = order.billing_address or {}

    brief = serialize_order_brief(order)
    brief.update(_compute_merchant_breakdown(order))
    brief.update({
        "sourcing_quoted_at_iso":  _iso(order.sourcing_quoted_at),
        "sourcing_paid_at_iso":    _iso(order.sourcing_paid_at),
        "sourcing_shipped_at_iso": _iso(order.sourcing_shipped_at),
        "sourcing_delivered_at_iso": _iso(order.sourcing_delivered_at),
        "sourcing_cancelled_at_iso": _iso(order.sourcing_cancelled_at),
        "sourcing_lead_days":      order.sourcing_lead_days,
        "created_at":              _short_dt(order.created_at),
        "customer_email":          order.customer_email or "",
        "shipping_address":        {
            "name":         addr.get("name") or "",
            "company":      addr.get("company") or "",
            "line1":        addr.get("line1") or "",
            "line2":        addr.get("line2") or "",
            "city":         addr.get("city") or "",
            "state":        addr.get("state") or "",
            "postal_code":  addr.get("postal_code") or "",
            "country":      addr.get("country") or "",
            "phone":        addr.get("phone") or "",
            "email":        addr.get("email") or "",
        },
        "billing_address":         {
            "name":         billing.get("name") or "",
            "company":      billing.get("company") or "",
            "line1":        billing.get("line1") or "",
            "line2":        billing.get("line2") or "",
            "city":         billing.get("city") or "",
            "state":        billing.get("state") or "",
            "postal_code":  billing.get("postal_code") or "",
            "country":      billing.get("country") or "",
            "phone":        billing.get("phone") or "",
            "email":        billing.get("email") or "",
        },
        "shipping_address_text":   order.shipping_address_text,
        "is_shipping_editable":    order.is_shipping_editable,
        "store_id":                order.store_id,
        "tenant_username":         tenant.username if tenant else "",
        "tenant_email":            tenant.email if tenant else "",
        "tenant_full_name":        tenant.get_full_name() if tenant else "",
    })

    return JsonResponse({
        "ok": True,
        "order":  brief,
        "state":  _serialize_state(state),
        "items":  _serialize_line_items(items),
        "assignments": [_serialize_assignment(a) for a in assignments],
        "supplier_matches": [serialize_supplier_product(p) for p in match_qs],
        "auto_match_sku": first_sku,
    })


# ───────────────────────────────────────────────────────────────────────
# Quote workspace
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_quote(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    supplier_id = body.get("supplier_id")
    supplier_product_id = body.get("supplier_product_id")

    if not supplier_id:
        return JsonResponse({"ok": False, "error": "supplier_id is required."}, status=400)
    try:
        supplier = Supplier.objects.get(pk=int(supplier_id))
    except (Supplier.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Supplier not found."}, status=404)

    supplier_product = None
    if supplier_product_id:
        try:
            supplier_product = SupplierProduct.objects.get(pk=int(supplier_product_id))
        except (SupplierProduct.DoesNotExist, ValueError, TypeError):
            supplier_product = None

    # Inputs from the Quote workspace:
    #   line_costs         = [{"idx", "name", "sku", "variant", "qty", "unit_cost_usd"}]
    #                        Per-line cost basis. Required for multi-item orders;
    #                        for single-item we still accept the legacy
    #                        `product_cost_usd` aggregate as a convenience.
    #   product_cost_usd   = (legacy single-item fallback) DS's product cost
    #   shipping_cost_usd  = DS's combined shipping cost
    #   margin_pct         = DS's markup % applied on top of cost
    #
    # The tenant-facing quote (sourcing_*_usd) carries the marked-up values
    # so Product + Shipping always add up to the Total the tenant sees.
    # The raw cost basis is preserved in state.supplier_cost_usd/supplier_shipping_usd
    # and the per-line breakdown lives in state.line_item_costs.
    line_costs_in = body.get("line_costs")
    line_item_costs = []
    if isinstance(line_costs_in, list) and line_costs_in:
        items_sum = Decimal("0")
        for row in line_costs_in:
            if not isinstance(row, dict):
                continue
            try:
                unit_cost = _dec(row.get("unit_cost_usd"))
            except Exception:
                unit_cost = Decimal("0")
            try:
                qty = int(row.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty < 1:
                qty = 1
            items_sum += unit_cost * Decimal(qty)
            line_item_costs.append({
                "idx":     int(row.get("idx") or 0),
                "name":    str(row.get("name") or "")[:255],
                "sku":     str(row.get("sku") or "")[:120],
                "variant": str(row.get("variant") or "")[:200],
                "qty":     qty,
                "unit_cost_usd": str(unit_cost.quantize(Decimal("0.01"))),
            })
        product_cost = items_sum
    else:
        product_cost = _dec(body.get("product_cost_usd"))

    shipping_cost = _dec(body.get("shipping_cost_usd"))
    if product_cost <= 0:
        return JsonResponse({"ok": False,
                             "error": "Product cost must be > 0 — set a unit cost for at least one line."},
                            status=400)

    margin_pct = _dec(body.get("margin_pct"))
    if margin_pct < 0:
        return JsonResponse({"ok": False, "error": "margin_pct cannot be negative."}, status=400)

    cost_base = product_cost + shipping_cost
    markup = (Decimal("1") + (margin_pct / Decimal("100")))
    quant = Decimal("0.01")
    tenant_product = (product_cost * markup).quantize(quant)
    tenant_shipping = (shipping_cost * markup).quantize(quant)
    total = tenant_product + tenant_shipping
    drop_sigma_margin = (total - cost_base).quantize(quant)

    lead_days = body.get("lead_days")
    try:
        lead_days = int(lead_days) if lead_days not in (None, "") else supplier.default_lead_days
    except (TypeError, ValueError):
        lead_days = supplier.default_lead_days

    # Planned shipping carrier — defaults to "Yuntrack" if not provided.
    # Saved to Order.tracking_company so tenant sees who'll ship it.
    # Real tracking number is added later via the Shipping desk endpoint.
    shipping_carrier = (body.get("shipping_carrier") or "").strip() or "Yuntrack"

    internal_notes = (body.get("internal_notes") or "").strip()
    now = timezone.now()

    with transaction.atomic():
        order.sourcing_product_usd = tenant_product
        order.sourcing_shipping_usd = tenant_shipping
        order.sourcing_total_usd = total
        order.sourcing_quoted_at = now
        order.sourcing_lead_days = lead_days
        order.tracking_company = shipping_carrier
        if order.sourcing_status == "pending_source":
            order.sourcing_status = "pending_payment"
        order.save(update_fields=[
            "sourcing_product_usd", "sourcing_shipping_usd",
            "sourcing_total_usd", "sourcing_quoted_at",
            "sourcing_lead_days", "tracking_company", "sourcing_status",
        ])

        state = _ensure_state(order)
        state.supplier = supplier
        state.supplier_product = supplier_product
        state.supplier_cost_usd = product_cost
        state.supplier_shipping_usd = shipping_cost
        state.drop_sigma_margin_usd = drop_sigma_margin
        state.tenant_product_usd = tenant_product
        state.tenant_shipping_usd = tenant_shipping
        state.tenant_total_usd = total
        state.line_item_costs = line_item_costs
        state.margin_pct = margin_pct
        state.quoted_lead_days = lead_days
        state.quote_status = "quote_sent"
        state.quote_sent_at = now
        state.quoted_by = request.user
        if internal_notes:
            state.internal_notes = (
                (state.internal_notes + "\n" if state.internal_notes else "")
                + f"[{_short_dt(now)}] {internal_notes}"
            )
        sku = _first_line_item_sku(order)
        if supplier_product and sku and supplier_product.sku.lower() == sku.lower():
            state.supplier_auto_matched = True
        state.save()

        if supplier_product:
            SupplierProduct.objects.filter(pk=supplier_product.pk).update(
                times_ordered=supplier_product.times_ordered + 1,
                last_ordered_at=now,
            )

        _log_activity(
            order, kind="quote_sent",
            title=f"Quote sent · {supplier.name}",
            detail=(f"Total ${total:.2f} = cost ${cost_base:.2f} + margin "
                    f"${drop_sigma_margin:.2f} ({margin_pct:.1f}%), lead {lead_days}d"),
            icon="💰", actor=request.user,
        )

    return JsonResponse({
        "ok": True,
        "order":  serialize_order_brief(order),
        "state":  _serialize_state(state),
    })


# ───────────────────────────────────────────────────────────────────────
# Assign
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_assign(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    member_id = body.get("member_id")
    stage = (body.get("stage") or "sourcing").strip()
    note = (body.get("note") or "").strip()

    valid_stages = {"sourcing", "procurement", "qc", "shipping", "support"}
    if stage not in valid_stages:
        return JsonResponse({"ok": False, "error": "Invalid stage."}, status=400)
    if not member_id:
        return JsonResponse({"ok": False, "error": "member_id is required."}, status=400)
    try:
        member = OpsTeamMember.objects.select_related("user", "role").get(pk=int(member_id))
    except (OpsTeamMember.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Team member not found."}, status=404)

    with transaction.atomic():
        OpsAssignment.objects.filter(
            order=order, stage=stage, is_active=True,
        ).update(is_active=False)
        a = OpsAssignment.objects.create(
            order=order, member=member, stage=stage,
            assigned_by=request.user, note=note, is_active=True,
        )
        _log_activity(
            order, kind="assigned",
            title=f"Assigned to {member.display_name or member.user.username}",
            detail=f"Stage: {a.get_stage_display()}" + (f" · {note}" if note else ""),
            icon="👤", actor=request.user,
        )

    return JsonResponse({"ok": True, "assignment": _serialize_assignment(a)})


# ───────────────────────────────────────────────────────────────────────
# Procurement
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_procure(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    procurement_status = (body.get("procurement_status") or "").strip()
    valid = dict(OrderOpsState.PROCURE_STATUS_CHOICES)
    if procurement_status not in valid:
        return JsonResponse({"ok": False, "error": "Invalid procurement_status."}, status=400)
    po_sent_at = body.get("po_sent_at")
    internal_notes = (body.get("internal_notes") or "").strip()

    with transaction.atomic():
        state = _ensure_state(order)
        state.procurement_status = procurement_status
        if procurement_status == "po_sent" and not state.po_sent_at:
            state.po_sent_at = timezone.now()
        elif po_sent_at == "now":
            state.po_sent_at = timezone.now()
        if procurement_status == "producing" and not state.production_started_at:
            state.production_started_at = timezone.now()
        if internal_notes:
            now = timezone.now()
            state.internal_notes = (
                (state.internal_notes + "\n" if state.internal_notes else "")
                + f"[{_short_dt(now)}] {internal_notes}"
            )
        state.save()

        _log_activity(
            order, kind="po_sent" if procurement_status == "po_sent" else "production",
            title=f"Procurement: {valid[procurement_status]}",
            detail=internal_notes,
            icon="🏭", actor=request.user,
        )

    return JsonResponse({"ok": True, "state": _serialize_state(state)})


# ───────────────────────────────────────────────────────────────────────
# QC
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_qc(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    qc_status = (body.get("qc_status") or "").strip()
    valid = dict(OrderOpsState.QC_STATUS_CHOICES)
    if qc_status not in valid:
        return JsonResponse({"ok": False, "error": "Invalid qc_status."}, status=400)
    notes = (body.get("notes") or "").strip()

    with transaction.atomic():
        state = _ensure_state(order)
        state.qc_status = qc_status
        if qc_status == "scheduled" and not state.qc_scheduled_at:
            state.qc_scheduled_at = timezone.now()
        if qc_status in ("passed", "failed", "waived"):
            state.qc_completed_at = timezone.now()
        if notes:
            now = timezone.now()
            state.internal_notes = (
                (state.internal_notes + "\n" if state.internal_notes else "")
                + f"[QC {_short_dt(now)}] {notes}"
            )
        state.save()

        _log_activity(
            order, kind="qc",
            title=f"QC: {valid[qc_status]}",
            detail=notes, icon="✅" if qc_status == "passed" else ("⚠️" if qc_status == "failed" else "🔎"),
            actor=request.user,
        )

    return JsonResponse({"ok": True, "state": _serialize_state(state)})


# ───────────────────────────────────────────────────────────────────────
# Ship
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_ship(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    tracking_number = (body.get("tracking_number") or "").strip()
    tracking_company = (body.get("tracking_company") or "").strip()
    tracking_url = (body.get("tracking_url") or "").strip()

    if not tracking_number:
        return JsonResponse({"ok": False, "error": "tracking_number is required."}, status=400)
    if not tracking_company:
        return JsonResponse({"ok": False, "error": "tracking_company is required."}, status=400)

    now = timezone.now()
    with transaction.atomic():
        order.tracking_number = tracking_number
        order.tracking_company = tracking_company
        order.tracking_url = tracking_url
        order.sourcing_status = "shipping"
        order.sourcing_shipped_at = now
        order.save(update_fields=[
            "tracking_number", "tracking_company", "tracking_url",
            "sourcing_status", "sourcing_shipped_at",
        ])
        state = _ensure_state(order)
        state.shipped_at = now
        state.save(update_fields=["shipped_at", "updated_at"])

        _log_activity(
            order, kind="shipped",
            title=f"Shipped · {tracking_company}",
            detail=f"Tracking: {tracking_number}",
            icon="✈️", actor=request.user,
        )

    return JsonResponse({
        "ok": True,
        "order": serialize_order_brief(order),
        "state": _serialize_state(state),
    })


# ───────────────────────────────────────────────────────────────────────
# Deliver
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_deliver(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    now = timezone.now()
    with transaction.atomic():
        order.sourcing_status = "delivered"
        order.sourcing_delivered_at = now
        order.save(update_fields=["sourcing_status", "sourcing_delivered_at"])
        state = _ensure_state(order)
        state.delivered_at = now
        state.save(update_fields=["delivered_at", "updated_at"])
        _log_activity(
            order, kind="delivered",
            title="Marked delivered",
            detail="", icon="📬", actor=request.user,
        )

    return JsonResponse({
        "ok": True,
        "order": serialize_order_brief(order),
        "state": _serialize_state(state),
    })


# ───────────────────────────────────────────────────────────────────────
# Cancel
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_order_cancel(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)

    body = _parse_body(request)
    reason = (body.get("reason") or "").strip() or "No reason provided"
    now = timezone.now()
    with transaction.atomic():
        order.sourcing_status = "cancel"
        order.sourcing_cancelled_at = now
        order.save(update_fields=["sourcing_status", "sourcing_cancelled_at"])
        state = _ensure_state(order)
        state.delay_reason = reason
        state.save(update_fields=["delay_reason", "updated_at"])
        _log_activity(
            order, kind="cancelled",
            title="Order cancelled", detail=reason,
            icon="🚫", actor=request.user,
        )

    return JsonResponse({
        "ok": True,
        "order": serialize_order_brief(order),
        "state": _serialize_state(state),
    })


# ───────────────────────────────────────────────────────────────────────
# Activity log
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_order_activity(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)
    items = list(order.ops_activity.all()[:50])
    return JsonResponse({
        "ok": True,
        "activity": [_serialize_activity(a) for a in items],
    })


# ───────────────────────────────────────────────────────────────────────
# Messages (internal team chat)
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_order_message_list(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)
    msgs = list(order.ops_messages
                .select_related("author", "author__ops_profile",
                                "author__ops_profile__role")[:200])
    return JsonResponse({
        "ok": True,
        "messages": [_serialize_message(m) for m in msgs],
    })


@ops_required
@require_POST
def api_order_message_send(request, order_id):
    order = _get_order_or_404(order_id)
    if not order:
        return JsonResponse({"ok": False, "error": "Order not found."}, status=404)
    body = _parse_body(request)
    text = (body.get("body") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "Message body is required."}, status=400)
    is_internal = body.get("is_internal")
    if is_internal is None:
        is_internal = True
    msg = OpsMessage.objects.create(
        order=order, author=request.user,
        body=text, is_internal=bool(is_internal),
    )
    return JsonResponse({"ok": True, "message": _serialize_message(msg)})


# ───────────────────────────────────────────────────────────────────────
# Supplier auto-match
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_supplier_match(request):
    sku = (request.GET.get("sku") or "").strip()
    if not sku:
        return JsonResponse({"ok": True, "matches": [], "sku": ""})
    matches = list(_auto_match_supplier_products(sku)[:8])
    return JsonResponse({
        "ok": True,
        "sku": sku,
        "matches": [serialize_supplier_product(p) for p in matches],
    })
