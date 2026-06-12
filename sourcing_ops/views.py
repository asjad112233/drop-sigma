"""Drop Sigma Operations Portal — central views + helpers.

Most endpoints are JSON APIs consumed by the single-page-app shell at
templates/sourcing_ops/dashboard.html. Section views live in
views_orders.py, views_suppliers.py, views_team.py — imported at the
bottom so the URL config can register them.
"""
from decimal import Decimal, InvalidOperation
import json

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from orders.tracking_link import build_ds_tracking_link, safe_carrier_name
from orders.fx_rates import convert_to_usd, get_rate_for, get_rate_date
from .permissions import ops_required, is_ops_user

from django.contrib.auth import authenticate, login as auth_login, get_user_model
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods


@csrf_protect
@require_http_methods(["GET", "POST"])
def ops_login_page(request):
    """Dedicated login surface for the Drop Sigma Operations Portal.

    This is intentionally separate from /login/ (which serves tenants
    + vendors + employees). OPS team members get their own URL,
    branded specifically for the back-office. The decorator
    @ops_required redirects here when an unauthenticated user hits
    any /ops/* surface.

    Accepts:
      - OPS team members (have an ``ops_profile``)
      - Superusers / staff (so founders can drop in without an
        explicit OpsTeamMember row)

    Anyone else gets a clear "not an Ops account" error rather than
    being silently routed away. We don't want a tenant accidentally
    discovering this URL to think the platform is broken.
    """
    User = get_user_model()

    # Already-authenticated ops users skip straight to the portal.
    if request.user.is_authenticated and is_ops_user(request.user):
        return redirect("/ops/")

    next_url = request.GET.get("next") or "/ops/"
    error    = None

    if request.method == "POST":
        identifier = (request.POST.get("username") or "").strip()
        password   = request.POST.get("password") or ""
        user = authenticate(request, username=identifier, password=password)
        if user is None and "@" in identifier:
            # Email lookup fallback.
            try:
                u = User.objects.get(email__iexact=identifier)
                user = authenticate(request, username=u.username, password=password)
            except User.DoesNotExist:
                user = None

        if user and is_ops_user(user):
            auth_login(request, user)
            # Honour ?next= only when it points back into /ops/ — a
            # tenant /dashboard/ URL on this login page should fall
            # back to the canonical Ops home instead of silently
            # taking an ops user to a portal they shouldn't render.
            if next_url and next_url.startswith("/ops/"):
                return redirect(next_url)
            return redirect("/ops/")
        elif user:
            error = "This account isn't on the Ops team. Talk to a Drop Sigma superadmin."
        else:
            error = "Invalid email or password."

    return render(request, "sourcing_ops/login.html", {
        "error":    error,
        "next_url": next_url,
    })
from .models import (
    Supplier, OpsTeamMember, OpsActivity, OrderOpsState,
)


# ───────────────────────────────────────────────────────────────────────
# Shared helpers (used by section views)
# ───────────────────────────────────────────────────────────────────────
def _parse_body(request):
    if request.body:
        try:
            return json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
    return {}


def _dec(value, default="0"):
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _dec_to_float(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(dt):
    return dt.isoformat() if dt else None


def _short_dt(dt):
    if not dt:
        return ""
    return timezone.localtime(dt).strftime("%b %d, %Y · %I:%M %p").lstrip("0")


def _human_ago(dt):
    if not dt:
        return ""
    now = timezone.now()
    diff = now - dt
    secs = int(diff.total_seconds())
    if secs < 60:    return "Just now"
    if secs < 3600:  return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    if secs < 604800: return f"{secs // 86400}d ago"
    return dt.strftime("%b %d")


_COUNTRY_NAME_TO_CODE = {
    "china": "CN", "people's republic of china": "CN", "prc": "CN",
    "hong kong": "HK", "taiwan": "TW",
    "india": "IN", "pakistan": "PK", "bangladesh": "BD",
    "vietnam": "VN", "thailand": "TH", "indonesia": "ID",
    "philippines": "PH", "malaysia": "MY", "singapore": "SG",
    "south korea": "KR", "korea": "KR", "japan": "JP",
    "turkey": "TR", "uae": "AE", "united arab emirates": "AE",
    "saudi arabia": "SA", "egypt": "EG", "morocco": "MA",
    "united states": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "poland": "PL", "russia": "RU",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "australia": "AU", "new zealand": "NZ",
}


def _country_flag(code):
    """Build a flag emoji from a 2-letter ISO code OR a country name."""
    if not code or not isinstance(code, str):
        return ""
    code = code.strip()
    if not code:
        return ""
    # Country name lookup → ISO
    if len(code) > 2 or not code.isalpha():
        iso = _COUNTRY_NAME_TO_CODE.get(code.lower())
        if not iso:
            return ""
        code = iso
    if len(code) != 2 or not code.isalpha():
        return ""
    A = 0x1F1E6
    code = code.upper()
    return chr(A + ord(code[0]) - 65) + chr(A + ord(code[1]) - 65)


# ───────────────────────────────────────────────────────────────────────
# Shell view — single-page-app at /ops/
# ───────────────────────────────────────────────────────────────────────
@login_required(login_url="/ops/login/")
def ops_dashboard(request):
    """Render the ops portal SPA shell. Non-ops users get redirected."""
    if not is_ops_user(request.user):
        from django.shortcuts import redirect
        return redirect("/dashboard/")
    return render(request, "sourcing_ops/dashboard.html", {
        "user": request.user,
    })


@login_required(login_url="/ops/login/")
def ops_wallet_preview(request):
    """Static design preview of the upcoming Wallets & Finance section.

    Renders mock data with the final styling so we can review the look-and-feel
    before wiring up real models / APIs. Remove once the live section ships.
    """
    if not is_ops_user(request.user):
        from django.shortcuts import redirect
        return redirect("/dashboard/")
    return render(request, "sourcing_ops/_wallet_preview.html")


@login_required(login_url="/ops/login/")
def ops_supplier_form_preview(request):
    """Static design preview of the supplier-facing pricing form.

    This is the page suppliers would see when ops shares them a unique
    token-gated link — they fill prices/MOQ/lead/variants and submit,
    and the catalog auto-updates in the ops portal. This preview lets us
    review the design before wiring up the real models + endpoints.
    """
    if not is_ops_user(request.user):
        from django.shortcuts import redirect
        return redirect("/dashboard/")
    return render(request, "sourcing_ops/_supplier_form_preview.html")


@login_required(login_url="/ops/login/")
def ops_tenant_product_detail_preview(request):
    """Static design preview of the tenant-facing product detail page.

    HyperSKU-style PDP where a tenant browses a product Drop Sigma has
    sourced (image gallery, variant tiles, size pills, qty stepper,
    pricing with margin hint, one-click "Add to my store" action).
    Lets us review the design before wiring real models / APIs.
    """
    if not is_ops_user(request.user):
        from django.shortcuts import redirect
        return redirect("/dashboard/")
    return render(request, "sourcing_ops/_tenant_product_detail_preview.html")


# ───────────────────────────────────────────────────────────────────────
# Shared serializers (used across section views)
# ───────────────────────────────────────────────────────────────────────
def serialize_supplier(s, *, full=False):
    out = {
        "id":            s.id,
        "name":          s.name,
        "slug":          s.slug,
        "legal_name":    s.legal_name,
        "tier":          s.tier,
        "tier_label":    s.get_tier_display(),
        "country":       s.country,
        "city":          s.city,
        "contact_name":  s.contact_name,
        "contact_role":  s.contact_role,
        "email":         s.email,
        "phone":         s.phone,
        "whatsapp":      s.whatsapp,
        "wechat":        s.wechat,
        "languages":     s.languages or [],
        "specialties":   s.specialties or [],
        "default_lead_days": s.default_lead_days,
        "moq":           s.moq,
        "payment_terms": s.payment_terms,
        "payment_terms_label": s.get_payment_terms_display(),
        "payment_methods": s.payment_methods or [],
        "currency":      s.currency,
        "rating":        float(s.rating),
        "quality_score": float(s.quality_score),
        "communication_score": float(s.communication_score),
        "delivery_score": float(s.delivery_score),
        "total_orders":  s.total_orders,
        "on_time_pct":   s.on_time_pct,
        "defect_pct":    float(s.defect_pct),
        "cover_emoji":   s.cover_emoji,
        "gradient_from": s.cover_gradient_from,
        "gradient_to":   s.cover_gradient_to,
        "gradient_css":  s.gradient_css,
        "initials":      s.initials,
        "is_active":     s.is_active,
        "flag":          _country_flag(s.country[:2] if s.country and len(s.country) >= 2 else ""),
    }
    if full:
        out.update({
            "address":       s.address,
            "timezone":      s.timezone,
            "website":       s.website,
            "alibaba_url":   s.alibaba_url,
            "tax_id":        s.tax_id,
            "business_license_no": s.business_license_no,
            "notes":         s.notes,
            "logo_url":      s.logo_url,
            "product_count": s.products.count(),
        })
    return out


def serialize_supplier_product(p):
    return {
        "id":            p.id,
        "supplier_id":   p.supplier_id,
        "supplier_name": p.supplier.name,
        "sku":           p.sku,
        "product_name":  p.product_name,
        "product_image_url": p.product_image_url,
        "variant_summary": p.variant_summary,
        "unit_cost_cny": _dec_to_float(p.unit_cost_cny),
        "unit_cost_usd": _dec_to_float(p.unit_cost_usd),
        "shipping_unit_usd": _dec_to_float(p.shipping_unit_usd),
        "moq":           p.moq,
        "lead_days":     p.lead_days,
        "suggested_tenant_price_usd": _dec_to_float(p.suggested_tenant_price_usd),
        "is_preferred":  p.is_preferred,
        "is_in_stock":   p.is_in_stock,
        "times_ordered": p.times_ordered,
        "last_ordered_ago": _human_ago(p.last_ordered_at),
        "notes":         p.notes,
        "created_at_iso": _iso(p.created_at),
    }


def serialize_team_member(m):
    return {
        "id":           m.id,
        "user_id":      m.user_id,
        "username":     m.user.username,
        "display_name": m.display_name or m.user.get_full_name() or m.user.username,
        "email":        m.user.email,
        "role":         m.role.name if m.role_id else "",
        "role_id":      m.role_id,
        "role_color":   m.role.color if m.role_id else "#6366f1",
        "title":        m.title,
        "avatar_url":   m.avatar_url,
        "avatar_emoji": m.avatar_emoji,
        "avatar_color": m.avatar_color,
        "status":       m.status,
        "status_label": m.get_status_display(),
        "timezone":     m.timezone,
        "languages":    m.languages or [],
        "orders_handled": m.orders_handled,
        "avg_quote_hrs": float(m.avg_quote_hrs),
        "on_time_pct":  m.on_time_pct,
        "is_manager":   m.is_manager,
        "can_assign":   m.can_assign,
        "can_quote":    m.can_quote,
        "joined_at_iso": _iso(m.joined_at),
    }


def serialize_order_brief(o):
    """Tenant-order summary for ops queue rows."""
    state = getattr(o, "ops_state", None)
    assignee = (o.ops_assignments
                 .filter(is_active=True)
                 .select_related("member__user", "member__role")
                 .first())
    raw = o.raw_data or {}
    items = raw.get("line_items") if isinstance(raw, dict) else []
    items = items if isinstance(items, list) else []
    first = items[0] if items else {}
    img = ""
    if isinstance(first.get("image"), dict):
        img = first["image"].get("src") or ""
    elif isinstance(first.get("image"), str):
        img = first["image"]

    addr = o.shipping_address or {}
    flag = _country_flag(addr.get("country") or o.country or "")
    tenant = o.store.user if o.store_id and o.store.user_id else None

    return {
        "id":              o.id,
        "ds_order_ref":    o.ds_order_ref,
        "source_order_ref": o.external_order_id,
        "store_platform":  getattr(o.store, "platform", "") if o.store_id else "",
        "store_name":      (getattr(o.store, "name", "") or "") if o.store_id else "",
        "tenant_username": tenant.username if tenant else "",
        "tenant_email":    tenant.email if tenant else "",
        "customer_name":   addr.get("name") or o.customer_name or "",
        "customer_phone":  addr.get("phone") or o.customer_phone or "",
        "ship_city":       addr.get("city") or o.city or "",
        "ship_country":    addr.get("country") or o.country or "",
        "flag":            flag,
        "product_name":    first.get("name") or o.product_name or "",
        "product_sku":     first.get("sku") or "",
        "product_image_url": img,
        "item_count":      len(items) if items else 1,
        "sourcing_status": o.sourcing_status,
        "sourcing_status_label": o.get_sourcing_status_display(),
        "sourcing_total_usd": _dec_to_float(o.sourcing_total_usd),
        "sourcing_product_usd": _dec_to_float(o.sourcing_product_usd),
        "sourcing_shipping_usd": _dec_to_float(o.sourcing_shipping_usd),
        # Tenant's selling price = what the END CUSTOMER paid on the merchant
        # store (Shopify / WooCommerce). The field name says "_usd" for
        # historical reasons but the VALUE is in the merchant's native
        # currency (o.currency) — that's why merchant_currency rides
        # alongside it. Drop Sigma's charge to the tenant is
        # sourcing_total_usd (in USD); subtracting directly would mix
        # units when the store isn't on USD, so we ALSO expose a live-FX
        # converted USD figure + the rate metadata for display.
        "merchant_total_usd":   _dec_to_float(o.total_price),
        "merchant_currency":    (o.currency or "USD").upper(),
        # ── Live-FX margin estimate fields ──────────────────────────
        # merchant_total_converted_usd:
        #   o.total_price translated into USD via today's mid-market
        #   rate. Equals merchant_total_usd when the store is on USD.
        #   None when the store currency isn't supported by Frankfurter
        #   OR every fetch attempt has failed this process — frontend
        #   falls back to the "—" display.
        # fx_rate_used:
        #   The rate applied (units of merchant_currency per 1 USD).
        #   Surfaced in the tooltip so ops can see what conversion
        #   went into the estimate.
        # fx_rate_date:
        #   ECB rate date the cached rates were quoted on. Surfaced
        #   so the ops reader knows whether the estimate is "today"
        #   or "yesterday on a weekend".
        "merchant_total_converted_usd": convert_to_usd(
            o.total_price, o.currency or "USD"
        ),
        "fx_rate_used":  get_rate_for(o.currency or "USD"),
        "fx_rate_date":  get_rate_date(),
        "supplier_id":     state.supplier_id if state and state.supplier_id else None,
        "supplier_name":   state.supplier.name if state and state.supplier_id else "",
        "supplier_country": (state.supplier.country if state and state.supplier_id else ""),
        "supplier_tier":   (state.supplier.tier if state and state.supplier_id else ""),
        "supplier_flag":   _country_flag(state.supplier.country)
                              if state and state.supplier_id else "",
        "supplier_auto_matched": state.supplier_auto_matched if state else False,
        "sourcing_lead_days": o.sourcing_lead_days,
        "quote_status":    state.quote_status if state else "none",
        "procurement_status": state.procurement_status if state else "not_started",
        "qc_status":       state.qc_status if state else "not_scheduled",
        "assignee":        (assignee.member.display_name or assignee.member.user.username)
                            if assignee and assignee.member_id else "",
        "assignee_color":  (assignee.member.role.color if assignee and assignee.member_id and assignee.member.role_id else "#94a3b8"),
        "created_at_iso":  _iso(o.created_at),
        "created_ago":     _human_ago(o.created_at),
        "paid_at_iso":     _iso(o.sourcing_paid_at),
        "tracking_number": o.tracking_number or "",
        # safe_carrier_name strips cross-border supplier brands
        # (Yuntrack, YunExpress, 4PX, Intelcom, Dragonfly) so even ops
        # staff see "Drop Sigma" rather than the underlying carrier.
        "tracking_company": safe_carrier_name(o.tracking_company or ""),
        # tracking_url shown to ops staff is the Drop Sigma branded
        # tracking page — never the raw carrier URL. See
        # orders/tracking_link.py for the contract: a single
        # tracking_number flips the link instantly, and the ops staff
        # see exactly the same surface the customer sees.
        "tracking_url":    build_ds_tracking_link(o),
    }


# ───────────────────────────────────────────────────────────────────────
# DASHBOARD / KPI overview
# ───────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_overview(request):
    """Top-level KPIs for the ops dashboard."""
    from orders.models import Order
    from .scoping import order_qs_visible_to

    # Scope to the requesting ops user's workspace. Superusers and
    # legacy ops users (no workspace bound) see every tenant — the
    # helper is a no-op for them. Workspace-bound users only count
    # tenants assigned to that workspace via OpsWorkspaceTenant.
    # Exclude tenant-soft-deleted rows: KPIs should reflect actual
    # outstanding work, not orders the tenant has already removed.
    qs = order_qs_visible_to(request.user, Order.objects.all()).filter(is_deleted=False)
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    counts = {
        "pending_source":  qs.filter(sourcing_status="pending_source").count(),
        "pending_payment": qs.filter(sourcing_status="pending_payment").count(),
        "processing":      qs.filter(sourcing_status="processing").count(),
        "shipping":        qs.filter(sourcing_status="shipping").count(),
        "delivered":       qs.filter(sourcing_status="delivered").count(),
        "cancel":          qs.filter(sourcing_status="cancel").count(),
        "all":             qs.count(),
    }

    awaiting_approval = OrderOpsState.objects.filter(quote_status="quote_sent").count()
    unassigned = qs.filter(
        sourcing_status="pending_source",
    ).exclude(ops_assignments__is_active=True).count()

    revenue_month = qs.filter(
        sourcing_paid_at__gte=month_start,
        sourcing_status__in=["processing", "shipping", "delivered"],
    ).aggregate(s=Sum("sourcing_total_usd"))["s"] or Decimal("0")

    team_active = OpsTeamMember.objects.filter(status="active").count()
    team_total  = OpsTeamMember.objects.count()
    suppliers_active = Supplier.objects.filter(is_active=True).count()

    recent = OpsActivity.objects.select_related("order")[:12]
    activity = [{
        "id":       a.id,
        "kind":     a.kind,
        "title":    a.title,
        "detail":   a.detail,
        "icon":     a.icon,
        "actor":    a.actor_label,
        "order_ref": a.order.ds_order_ref if a.order_id else "",
        "order_id": a.order_id,
        "created_ago": _human_ago(a.created_at),
    } for a in recent]

    return JsonResponse({
        "ok": True,
        "counts": counts,
        "awaiting_approval": awaiting_approval,
        "unassigned": unassigned,
        "revenue_month_usd": _dec_to_float(revenue_month) or 0,
        "team_active": team_active,
        "team_total":  team_total,
        "suppliers_active": suppliers_active,
        "activity": activity,
    })


@ops_required
@require_GET
def api_me(request):
    """Return the logged-in ops user's profile."""
    u = request.user
    profile = getattr(u, "ops_profile", None)
    return JsonResponse({
        "ok": True,
        "user": {
            "username": u.username,
            "email":    u.email,
            "display_name": (profile.display_name if profile else "")
                             or u.get_full_name() or u.username,
            "is_superuser": u.is_superuser,
            "is_manager":   bool(profile and profile.is_manager) or u.is_superuser,
        },
        "profile": serialize_team_member(profile) if profile else None,
    })
