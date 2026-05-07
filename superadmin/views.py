import json
import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth import login
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.db.models import Count, Sum, Q

from stores.models import Store
from orders.models import Order
from vendors.models import Vendor
from teamapp.models import TeamMember

from .models import Tenant, Subscription, TenantActivity, PLAN_PRICES, Coupon


# ── Guards ──────────────────────────────────────────────────────────────────

def superadmin_required(view_fn):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return redirect("/login/")
        return view_fn(request, *args, **kwargs)
    wrapper.__name__ = view_fn.__name__
    return wrapper


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tenant_dict(t):
    sub = getattr(t, "subscription", None)
    stores_qs  = Store.objects.filter(user=t.user)
    orders_qs  = Order.objects.filter(store__user=t.user)
    vendors_qs = Vendor.objects.filter(assigned_store__user=t.user)
    team_qs    = TeamMember.objects.filter(user__isnull=False)

    return {
        "id":      t.pk,
        "name":    t.name,
        "email":   t.user.email,
        "plan":    t.plan,
        "status":  t.status,
        "stores":  stores_qs.count(),
        "orders":  orders_qs.count(),
        "vendors": vendors_qs.count(),
        "team":    TeamMember.objects.filter(user=t.user).count(),
        "joined":  t.created_at.date().isoformat(),
        "renews":  sub.renews_on.isoformat() if sub and sub.renews_on else None,
        "mrr":     t.mrr,
        "ltv":     t.ltv,
        "notes":   t.notes,
        "flagged": t.flagged,
        "payment_status": sub.payment_status if sub else "paid",
    }


def _time_ago(dt):
    diff = timezone.now() - dt
    s = diff.total_seconds()
    if s < 60:       return f"{int(s)}s ago"
    if s < 3600:     return f"{int(s//60)}m ago"
    if s < 86400:    return f"{int(s//3600)}h ago"
    return f"{int(s//86400)}d ago"


# ── Page ─────────────────────────────────────────────────────────────────────

@superadmin_required
def superadmin_page(request):
    return render(request, "superadmin.html")


# ── API: Stats ────────────────────────────────────────────────────────────────

@superadmin_required
@require_GET
def api_stats(request):
    tenants = Tenant.objects.all()
    active  = tenants.filter(status="active")
    trials  = tenants.filter(status="trial")
    mrr     = sum(t.mrr for t in active)
    tenant_user_ids = Tenant.objects.values_list("user_id", flat=True)
    total_orders = Order.objects.filter(store__user_id__in=tenant_user_ids).count()
    return JsonResponse({
        "active_tenants": active.count(),
        "mrr":            mrr,
        "trials":         trials.count(),
        "total_orders":   total_orders,
        "total_tenants":  tenants.count(),
    })


# ── API: Tenants list / create ────────────────────────────────────────────────

@superadmin_required
def api_tenants(request):
    if request.method == "GET":
        tenants = Tenant.objects.select_related("user", "subscription").all()
        return JsonResponse([_tenant_dict(t) for t in tenants], safe=False)

    if request.method == "POST":
        data = json.loads(request.body)
        name  = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        plan  = data.get("plan", "trial")
        notes = data.get("notes", "")

        if not name or not email:
            return JsonResponse({"error": "Name and email required."}, status=400)

        if User.objects.filter(email=email).exists():
            return JsonResponse({"error": "A user with that email already exists."}, status=400)

        username = email.split("@")[0]
        base = username
        n = 1
        while User.objects.filter(username=username).exists():
            username = f"{base}{n}"
            n += 1

        user = User.objects.create_user(
            username=username,
            email=email,
            password=User.objects.make_random_password(),
            is_staff=True,
        )
        user.save()

        trial_ends = datetime.date.today() + datetime.timedelta(days=14)
        status = "trial" if plan == "trial" else "active"

        tenant = Tenant.objects.create(
            user=user,
            name=name,
            plan=plan,
            status=status,
            notes=notes,
            trial_ends=trial_ends,
        )

        price     = PLAN_PRICES.get(plan, 0)
        renews_on = datetime.date.today() + datetime.timedelta(days=30)
        Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            price=price,
            start_date=datetime.date.today(),
            renews_on=renews_on if plan != "trial" else trial_ends,
        )

        TenantActivity.objects.create(
            tenant=tenant,
            action=f"Tenant onboarded by super admin — plan: {plan}",
            action_type="signup",
        )

        return JsonResponse({"ok": True, "tenant": _tenant_dict(tenant)})

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── API: Tenant detail ────────────────────────────────────────────────────────

@superadmin_required
def api_tenant_detail(request, pk):
    t = get_object_or_404(Tenant, pk=pk)

    if request.method == "GET":
        d = _tenant_dict(t)
        d["activities"] = [
            {
                "action":      a.action,
                "action_type": a.action_type,
                "when":        _time_ago(a.created_at),
            }
            for a in t.activities.all()[:20]
        ]
        return JsonResponse(d)

    if request.method == "POST":
        data = json.loads(request.body)
        action = data.get("action")

        if action == "suspend":
            reason = data.get("reason", "")
            t.status = "suspended"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action=f"Account suspended — {reason}",
                action_type="general",
            )
            return JsonResponse({"ok": True})

        if action == "activate":
            t.status = "active"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action="Account reactivated by super admin",
                action_type="general",
            )
            return JsonResponse({"ok": True})

        if action == "change_plan":
            new_plan = data.get("plan")
            reason   = data.get("reason", "")
            old_plan = t.plan
            t.plan   = new_plan
            if t.status == "trial" and new_plan != "trial":
                t.status = "active"
            t.save()
            sub = getattr(t, "subscription", None)
            if sub:
                sub.plan  = new_plan
                sub.price = PLAN_PRICES.get(new_plan, 0)
                sub.renews_on = datetime.date.today() + datetime.timedelta(days=30)
                sub.save()
            TenantActivity.objects.create(
                tenant=t,
                action=f"Plan changed: {old_plan} → {new_plan}. {reason}".strip(" .") + ".",
                action_type="plan",
            )
            return JsonResponse({"ok": True})

        if action == "add_note":
            note = data.get("note", "").strip()
            if note:
                sep = "\n\n" if t.notes else ""
                t.notes += sep + note
                t.save()
            return JsonResponse({"ok": True, "notes": t.notes})

        if action == "delete":
            confirm = data.get("confirm", "")
            if confirm != "DELETE":
                return JsonResponse({"error": "Type DELETE to confirm."}, status=400)
            t.user.delete()
            return JsonResponse({"ok": True})

        if action == "flag":
            t.flagged = not t.flagged
            t.save()
            return JsonResponse({"ok": True, "flagged": t.flagged})

        return JsonResponse({"error": "Unknown action."}, status=400)

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── API: Activity log ─────────────────────────────────────────────────────────

@superadmin_required
@require_GET
def api_activity(request):
    acts = TenantActivity.objects.select_related("tenant").all()[:60]
    return JsonResponse([
        {
            "tenant":      a.tenant.name,
            "action":      a.action,
            "action_type": a.action_type,
            "when":        _time_ago(a.created_at),
        }
        for a in acts
    ], safe=False)


# ── Impersonation ─────────────────────────────────────────────────────────────

@superadmin_required
def impersonate(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    TenantActivity.objects.create(
        tenant=tenant,
        action="Super admin started impersonation session",
        action_type="general",
    )
    request.session["impersonate_id"]   = tenant.user.pk
    request.session["impersonate_name"] = tenant.name
    request.session["impersonate_email"] = tenant.user.email
    return redirect("/dashboard/")


@superadmin_required
def exit_impersonation(request):
    request.session.pop("impersonate_id",    None)
    request.session.pop("impersonate_name",  None)
    request.session.pop("impersonate_email", None)
    return redirect("/superadmin/")


# ── API: Coupons ──────────────────────────────────────────────────────────────

def _coupon_dict(c):
    return {
        "id":             c.pk,
        "code":           c.code,
        "discount_type":  c.discount_type,
        "discount_value": float(c.discount_value),
        "max_uses":       c.max_uses,
        "uses":           c.uses,
        "is_active":      c.is_active,
        "expires_at":     c.expires_at.isoformat() if c.expires_at else None,
        "created_at":     c.created_at.date().isoformat(),
    }


@superadmin_required
def api_coupons(request):
    if request.method == "GET":
        coupons = Coupon.objects.all().order_by("-created_at")
        return JsonResponse([_coupon_dict(c) for c in coupons], safe=False)

    if request.method == "POST":
        data = json.loads(request.body)
        code           = data.get("code", "").strip().upper()
        discount_type  = data.get("discount_type", "flat")
        discount_value = data.get("discount_value")
        max_uses       = data.get("max_uses") or None
        expires_at     = data.get("expires_at") or None

        if not code:
            return JsonResponse({"error": "Code is required."}, status=400)
        if not discount_value:
            return JsonResponse({"error": "Discount value is required."}, status=400)
        if Coupon.objects.filter(code=code).exists():
            return JsonResponse({"error": "Coupon code already exists."}, status=400)

        c = Coupon.objects.create(
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            max_uses=max_uses,
            expires_at=expires_at,
        )
        return JsonResponse({"ok": True, "coupon": _coupon_dict(c)})

    return JsonResponse({"error": "Method not allowed."}, status=405)


@superadmin_required
def api_coupon_detail(request, pk):
    c = get_object_or_404(Coupon, pk=pk)

    if request.method == "POST":
        data   = json.loads(request.body)
        action = data.get("action")

        if action == "toggle":
            c.is_active = not c.is_active
            c.save()
            return JsonResponse({"ok": True, "is_active": c.is_active})

        if action == "delete":
            c.delete()
            return JsonResponse({"ok": True})

        return JsonResponse({"error": "Unknown action."}, status=400)

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── Public: Validate coupon (called from upgrade/checkout page) ───────────────

def api_validate_coupon(request):
    code     = request.GET.get("code", "").strip().upper()
    plan_key = request.GET.get("plan", "pro")

    PLAN_PRICES_DISPLAY = {"pro": 99, "starter": 49, "growth": 99, "scale": 149}
    original = PLAN_PRICES_DISPLAY.get(plan_key, 99)

    try:
        c = Coupon.objects.get(code=code)
    except Coupon.DoesNotExist:
        return JsonResponse({"valid": False, "error": "Invalid coupon code."})

    valid, msg = c.is_valid()
    if not valid:
        return JsonResponse({"valid": False, "error": msg})

    discounted = c.apply(original)
    if c.discount_type == "flat":
        label = f"${float(c.discount_value):.0f} off"
    else:
        label = f"{float(c.discount_value):.0f}% off"

    return JsonResponse({
        "valid":      True,
        "label":      label,
        "original":   original,
        "discounted": round(discounted, 2),
        "savings":    round(original - discounted, 2),
    })
