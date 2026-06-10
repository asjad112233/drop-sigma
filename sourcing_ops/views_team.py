"""Team / role-related ops views.

Endpoints:
  GET  /ops/api/team/                       → list all OpsTeamMember (?role=, ?status=)
  POST /ops/api/team/create/                → create User + add to DropSigmaOps + OpsTeamMember
  POST /ops/api/team/<member_id>/update/    → partial update of an OpsTeamMember
  GET  /ops/api/roles/                      → list all OpsRole
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .models import OpsRole, OpsTeamMember
from .permissions import ops_required, ensure_ops_group
from .views import _parse_body, serialize_team_member


User = get_user_model()


# ─── Allow-list for the partial-update endpoint ────────────────────────
_TEAM_UPDATABLE_FIELDS = {
    "display_name", "title", "avatar_url", "avatar_emoji", "avatar_color",
    "status", "timezone", "is_manager", "can_assign", "can_quote",
}
_VALID_STATUSES = {"active", "away", "offline", "inactive"}


def _serialize_role(r):
    return {
        "id":           r.id,
        "name":         r.name,
        "slug":         r.slug,
        "description":  r.description,
        "color":        r.color,
        "sort_order":   r.sort_order,
        "member_count": r.members.count(),
    }


def _normalize_languages(value):
    """Accept a list, or a comma/newline-separated string. Return a clean list."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parts = [p.strip() for chunk in value.replace("\n", ",").split(",") for p in [chunk]]
        return [p for p in parts if p]
    return []


# ─────────────────────────────────────────────────────────────────────────
# GET /ops/api/team/?role=<slug>&status=<status>
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_team_list(request):
    qs = OpsTeamMember.objects.select_related("user", "role").all()

    role_slug = (request.GET.get("role") or "").strip()
    if role_slug and role_slug.lower() != "all":
        qs = qs.filter(role__slug=role_slug)

    status_filter = (request.GET.get("status") or "").strip().lower()
    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    members = [serialize_team_member(m) for m in qs]
    return JsonResponse({"ok": True, "members": members, "count": len(members)})


# ─────────────────────────────────────────────────────────────────────────
# GET /ops/api/roles/
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_team_roles(request):
    roles = [_serialize_role(r) for r in OpsRole.objects.all()]
    return JsonResponse({"ok": True, "roles": roles, "count": len(roles)})


# ─────────────────────────────────────────────────────────────────────────
# POST /ops/api/team/create/
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_team_create(request):
    data = _parse_body(request)
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip()
    display  = (data.get("display_name") or "").strip()
    role_id  = data.get("role_id")

    if not username:
        return JsonResponse({"ok": False, "error": "Username is required."}, status=400)

    if User.objects.filter(username__iexact=username).exists():
        return JsonResponse(
            {"ok": False, "error": f"Username '{username}' is already taken."},
            status=400,
        )

    role = None
    if role_id:
        try:
            role = OpsRole.objects.get(pk=role_id)
        except OpsRole.DoesNotExist:
            return JsonResponse(
                {"ok": False, "error": "Selected role no longer exists."},
                status=400,
            )

    status_val = (data.get("status") or "active").strip().lower()
    if status_val not in _VALID_STATUSES:
        status_val = "active"

    password  = data.get("password") or "dropsigma123"
    languages = _normalize_languages(data.get("languages")) or []

    with transaction.atomic():
        u = User.objects.create_user(
            username=username,
            email=email,
            password=password,
        )
        if display:
            parts = display.split(" ", 1)
            u.first_name = parts[0][:30]
            if len(parts) > 1:
                u.last_name = parts[1][:150]
            u.save(update_fields=["first_name", "last_name"])

        # Add to DropSigmaOps so the permission gate accepts them
        group = ensure_ops_group()
        u.groups.add(group)

        member = OpsTeamMember.objects.create(
            user=u,
            display_name=display,
            role=role,
            title=(data.get("title") or "").strip()[:120],
            avatar_emoji=(data.get("avatar_emoji") or "👤")[:4] or "👤",
            avatar_color=(data.get("avatar_color") or "#6366f1")[:20],
            status=status_val,
            timezone=(data.get("timezone") or "Asia/Karachi")[:64],
            languages=languages,
            is_manager=bool(data.get("is_manager")),
            can_assign=bool(data.get("can_assign", True)),
            can_quote=bool(data.get("can_quote", True)),
        )

    return JsonResponse(
        {"ok": True, "member": serialize_team_member(member)},
        status=201,
    )


# ─────────────────────────────────────────────────────────────────────────
# POST /ops/api/team/<member_id>/update/
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_team_update(request, member_id):
    try:
        member = (OpsTeamMember.objects
                  .select_related("user", "role")
                  .get(pk=member_id))
    except OpsTeamMember.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Team member not found."}, status=404)

    data = _parse_body(request)
    changed = []

    for field in _TEAM_UPDATABLE_FIELDS:
        if field not in data:
            continue
        val = data[field]
        if field == "status":
            v = str(val or "").strip().lower()
            if v not in _VALID_STATUSES:
                return JsonResponse(
                    {"ok": False, "error": f"Invalid status '{val}'."},
                    status=400,
                )
            setattr(member, field, v)
        elif field in ("is_manager", "can_assign", "can_quote"):
            setattr(member, field, bool(val))
        elif field == "avatar_emoji":
            setattr(member, field, (str(val or "👤"))[:4] or "👤")
        elif field == "avatar_color":
            setattr(member, field, (str(val or "#6366f1"))[:20])
        elif field == "timezone":
            setattr(member, field, (str(val or ""))[:64])
        elif field == "title":
            setattr(member, field, (str(val or ""))[:120])
        elif field == "display_name":
            setattr(member, field, (str(val or ""))[:120])
        elif field == "avatar_url":
            setattr(member, field, (str(val or ""))[:400])
        else:
            setattr(member, field, val)
        changed.append(field)

    if "role_id" in data:
        rid = data.get("role_id")
        if rid in (None, "", 0, "0"):
            member.role = None
        else:
            try:
                member.role = OpsRole.objects.get(pk=rid)
            except OpsRole.DoesNotExist:
                return JsonResponse(
                    {"ok": False, "error": "Selected role no longer exists."},
                    status=400,
                )
        changed.append("role")

    if "languages" in data:
        member.languages = _normalize_languages(data.get("languages")) or []
        changed.append("languages")

    if "email" in data:
        email = str(data.get("email") or "").strip()
        if email and email != member.user.email:
            member.user.email = email[:254]
            member.user.save(update_fields=["email"])

    if changed:
        member.save()

    member = (OpsTeamMember.objects
              .select_related("user", "role")
              .get(pk=member.pk))
    return JsonResponse(
        {"ok": True, "member": serialize_team_member(member), "changed": changed}
    )


# ═══════════════════════════════════════════════════════════════════════
# DEDICATED SOURCING MANAGER — ops-facing inbox
# ═══════════════════════════════════════════════════════════════════════
from django.db.models import Count, Sum, Q
from django.utils import timezone
from decimal import Decimal

from .models import TenantManagerAssignment
from .services import reassign_tenant_manager
from .views import _iso, _country_flag, _dec_to_float


def _manager_self_profile(member):
    """Same shape as the tenant-facing manager card — used by both
    endpoints so the ops dashboard can show the manager their own card."""
    if member is None:
        return None
    user = member.user
    role_name = member.role.name if member.role_id else "Account Manager"
    role_color = member.role.color if member.role_id else "#a855f7"
    display = (member.display_name or user.get_full_name() or user.username
               or "Sourcing Manager")
    specialties = [s.strip() for s in (member.specialties_text or "").split(",")
                   if s.strip()]
    return {
        "id":                member.id,
        "user_id":           user.id,
        "username":          user.username,
        "display_name":      display,
        "title":             member.title or role_name,
        "role":              role_name,
        "role_color":        role_color,
        "photo_url":         member.photo_url or member.avatar_url or "",
        "avatar_emoji":      member.avatar_emoji or "👤",
        "avatar_color":      member.avatar_color or "#a855f7",
        "bio":               member.bio or "",
        "specialties":       specialties,
        "languages":         member.languages or [],
        "signature":         member.signature or display,
        "typical_reply_min": int(member.typical_reply_min or 5),
        "status":            member.status,
        "status_label":      member.get_status_display(),
        "is_manager":        bool(member.is_manager),
    }


def _tenant_row_for_manager_inbox(assignment):
    """Build one tenant card for the "My Tenants" inbox."""
    tenant = assignment.tenant

    # Stores (per-tenant)
    stores_qs = tenant.store_set.all() if hasattr(tenant, "store_set") else []
    stores = list(stores_qs)
    store_payload = [
        {"id": s.id, "name": s.name, "platform": s.platform}
        for s in stores
    ]

    # Order counts (orders are joined via stores.Store)
    order_qs = None
    try:
        from orders.models import Order
        order_qs = Order.objects.filter(store__user=tenant)
    except Exception:
        order_qs = None

    order_count_total = order_qs.count() if order_qs is not None else 0
    if order_qs is not None:
        active_statuses = [
            "pending_source", "quote_sent", "processing",
            "in_production", "in_transit", "awaiting_payment",
        ]
        active_q = Q()
        for s in active_statuses:
            active_q |= Q(sourcing_status=s)
        order_count_active = order_qs.filter(active_q).count()
        last_order = order_qs.order_by("-created_at").first()
    else:
        order_count_active = 0
        last_order = None

    # Wallet
    wallet_balance_usd = None
    wallet = getattr(tenant, "sourcing_wallet", None)
    if wallet is not None:
        wallet_balance_usd = _dec_to_float(wallet.balance_usd)

    # Chat activity — pull the latest message across the tenant's
    # PartnerConversation rows. Drop Sigma is a single brand to the
    # tenant; we look at every conversation they own.
    last_msg = None
    unread_messages = 0
    try:
        from sourcing_partners.models import PartnerConversation, PartnerMessage
        conv_ids = list(PartnerConversation.objects
                        .filter(tenant=tenant, is_archived=False)
                        .values_list("id", flat=True))
        if conv_ids:
            last_msg = (PartnerMessage.objects
                        .filter(conversation_id__in=conv_ids)
                        .order_by("-created_at").first())
            # Tenant→ops unread: direction='out' (tenant sent, ops hasn't read)
            unread_messages = (PartnerMessage.objects
                               .filter(conversation_id__in=conv_ids,
                                       direction="out", is_read=False)
                               .count())
    except Exception:
        pass

    addr_country = ""
    if last_order is not None:
        addr = last_order.shipping_address or {}
        if isinstance(addr, dict):
            addr_country = addr.get("country") or last_order.country or ""
        else:
            addr_country = last_order.country or ""

    display_name = (tenant.get_full_name() or tenant.username or "").strip()

    return {
        "tenant_id":            tenant.id,
        "username":             tenant.username,
        "display_name":         display_name,
        "email":                tenant.email,
        "flag":                 _country_flag(addr_country) if addr_country else "",
        "store_count":          len(stores),
        "stores":               store_payload,
        "order_count_total":    order_count_total,
        "order_count_active":   order_count_active,
        "last_order_iso":       _iso(last_order.created_at) if last_order else None,
        "wallet_balance_usd":   wallet_balance_usd,
        "assigned_at_iso":      _iso(assignment.assigned_at),
        "assigned_via":         assignment.assigned_via,
        "unread_messages":      unread_messages,
        "last_message_preview": (last_msg.body[:140] if last_msg else ""),
        "last_message_iso":     _iso(last_msg.created_at) if last_msg else None,
        "last_message_direction": last_msg.direction if last_msg else None,
    }


# ─────────────────────────────────────────────────────────────────────────
# GET /ops/api/my-tenants/
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_GET
def api_my_tenants(request):
    """Tenants assigned to the calling ops user. Managers see all
    assignments; specialists see only their own.
    """
    member = getattr(request.user, "ops_profile", None)
    if member is None and not request.user.is_superuser:
        return JsonResponse(
            {"ok": False, "error": "No ops profile attached to user."},
            status=403,
        )

    # Manager scope: managers can see every assignment; others see own only.
    qs = (TenantManagerAssignment.objects
          .select_related("tenant", "manager", "manager__user", "manager__role")
          .order_by("-assigned_at"))

    is_manager = bool(member and member.is_manager) or request.user.is_superuser
    if not is_manager and member is not None:
        qs = qs.filter(manager=member)

    rows = []
    active_conversations = 0
    for a in qs:
        row = _tenant_row_for_manager_inbox(a)
        if row["unread_messages"] or (row["last_message_iso"] and
                                       row["last_message_direction"] == "out"):
            active_conversations += 1
        rows.append(row)

    # Sort: unread DESC, then last_message_iso DESC, then assigned_at DESC.
    # Python's sort is stable, so applying sorts in reverse precedence
    # (least-significant first) yields the correct multi-key ordering.
    # ISO-8601 strings sort lexicographically in date order.
    rows.sort(key=lambda r: r["assigned_at_iso"] or "", reverse=True)
    rows.sort(key=lambda r: r["last_message_iso"] or "", reverse=True)
    rows.sort(key=lambda r: r["unread_messages"] or 0, reverse=True)

    return JsonResponse({
        "ok": True,
        "manager":              _manager_self_profile(member),
        "is_manager":           is_manager,
        "tenant_count":         len(rows),
        "active_conversations": active_conversations,
        "tenants":              rows,
    })


# ─────────────────────────────────────────────────────────────────────────
# POST /ops/api/tenant/<tenant_id>/reassign-manager/
# Body: { "new_manager_id": 7, "reason": "Sara on extended leave" }
# Manager-only.
# ─────────────────────────────────────────────────────────────────────────
@ops_required
@require_POST
def api_reassign_manager(request, tenant_id):
    actor = getattr(request.user, "ops_profile", None)
    is_actor_manager = bool(actor and actor.is_manager) or request.user.is_superuser
    if not is_actor_manager:
        return JsonResponse(
            {"ok": False, "error": "Only ops managers can reassign tenants."},
            status=403,
        )

    body = _parse_body(request)
    new_manager_id = body.get("new_manager_id")
    reason = (body.get("reason") or "").strip()

    try:
        new_manager_id = int(new_manager_id)
    except (TypeError, ValueError):
        return JsonResponse(
            {"ok": False, "error": "new_manager_id is required."},
            status=400,
        )

    try:
        new_manager = OpsTeamMember.objects.get(pk=new_manager_id)
    except OpsTeamMember.DoesNotExist:
        return JsonResponse(
            {"ok": False, "error": "New manager not found."},
            status=404,
        )
    if new_manager.status != "active":
        return JsonResponse(
            {"ok": False,
             "error": "Selected manager is not active — pick an active member."},
            status=400,
        )

    User = get_user_model()
    try:
        tenant_user = User.objects.get(pk=tenant_id)
    except User.DoesNotExist:
        return JsonResponse(
            {"ok": False, "error": "Tenant not found."},
            status=404,
        )

    try:
        assignment = (TenantManagerAssignment.objects
                      .select_related("manager", "tenant")
                      .get(tenant=tenant_user))
    except TenantManagerAssignment.DoesNotExist:
        return JsonResponse(
            {"ok": False,
             "error": "Tenant has no current assignment — call my-manager first."},
            status=404,
        )

    if assignment.manager_id == new_manager.id:
        return JsonResponse({
            "ok": True,
            "no_change": True,
            "assignment": {
                "tenant_id": tenant_user.id,
                "manager":   _manager_self_profile(new_manager),
            },
        })

    assignment = reassign_tenant_manager(
        assignment=assignment,
        new_manager=new_manager,
        reason=reason,
        via="manual_ops",
    )

    return JsonResponse({
        "ok": True,
        "assignment": {
            "tenant_id":          tenant_user.id,
            "manager":            _manager_self_profile(assignment.manager),
            "previous_manager_id": assignment.previous_manager_id,
            "reassigned_at_iso":  _iso(assignment.reassigned_at),
            "reason":             assignment.reassignment_reason,
        },
    })
