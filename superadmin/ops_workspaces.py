"""Superadmin → Ops Workspaces management API.

A workspace is a logical slice of the OPS portal that's scoped to a
subset of tenants + a roster of team members. Only superusers
hit these endpoints; tenants and ops members never see them.

URL surface (all mounted under /superadmin/api/ops-workspaces/):

    GET    /                            list every workspace + counts
    POST   /                            create a workspace
    GET    /<id>/                       detail: tenants + members + pending invites
    PATCH  /<id>/                       rename / change colour / deactivate
    DELETE /<id>/                       delete the workspace (and detach members)

    POST   /<id>/tenants/               assign a tenant by user_id
    DELETE /<id>/tenants/<tenant_id>/   unassign a tenant

    POST   /<id>/invitations/           create + send a new invitation
    POST   /<id>/invitations/<iid>/resend/  re-send the email
    POST   /<id>/invitations/<iid>/revoke/  revoke a pending invitation

    DELETE /<id>/members/<member_id>/   remove a team member from this workspace

All write endpoints decorate with @superadmin_required (reuses the
existing decorator in superadmin/views.py) so a stray non-super
session gets 403'd at the door.
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_GET, require_POST, require_http_methods,
)

from .views import superadmin_required
from sourcing_ops.models import (
    OpsInvitation,
    OpsRole,
    OpsTeamMember,
    OpsWorkspace,
    OpsWorkspaceTenant,
)
from sourcing_ops.invitations import send_invitation_email


User = get_user_model()


# ── helpers ─────────────────────────────────────────────────────────
def _parse(request):
    try:
        return json.loads(request.body or b"{}")
    except Exception:
        return {}


def _ws_or_404(pk) -> OpsWorkspace:
    return get_object_or_404(OpsWorkspace, pk=pk)


def _slugify_unique(name: str) -> str:
    base = slugify(name) or "workspace"
    candidate, n = base, 1
    while OpsWorkspace.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _workspace_brief(ws: OpsWorkspace) -> dict:
    return {
        "id":           ws.id,
        "name":         ws.name,
        "slug":         ws.slug,
        "description":  ws.description,
        "color":        ws.color,
        "emoji":        ws.emoji,
        "is_active":    ws.is_active,
        "tenant_count": ws.tenant_count,
        "member_count": ws.member_count,
        "pending_invites": ws.pending_invite_count,
        "created_at":   ws.created_at.isoformat(),
        "created_by":   (ws.created_by.get_full_name() or ws.created_by.username)
                          if ws.created_by_id else "",
    }


def _tenant_brief(user) -> dict:
    """Minimal info on a tenant — enough for the superadmin to identify
    which Drop Sigma seller they're moving between workspaces."""
    stores = list(user.store_set.filter(is_active=True).values("id", "name")[:5]) \
        if hasattr(user, "store_set") else []
    return {
        "id":         user.id,
        "username":   user.username,
        "email":      user.email,
        "full_name":  user.get_full_name(),
        "is_active":  user.is_active,
        "store_count": len(stores),
        "stores":     stores,
    }


def _member_brief(member: OpsTeamMember) -> dict:
    return {
        "id":           member.id,
        "user_id":      member.user_id,
        "username":     member.user.username,
        "email":        member.user.email,
        "display_name": member.display_name,
        "title":        member.title,
        "role":         member.role.name if member.role_id else "",
        "role_color":   member.role.color if member.role_id else "#94a3b8",
        "is_manager":   member.is_manager,
        "status":       member.status,
        "joined_at":    member.joined_at.isoformat(),
    }


def _invite_brief(inv: OpsInvitation) -> dict:
    # Auto-expire stale invites on the read so the list reflects truth
    # without a periodic sweeper.
    if inv.status == "pending" and inv.is_expired():
        inv.status = "expired"
        inv.save(update_fields=["status"])
    return {
        "id":          inv.id,
        "email":       inv.email,
        "name":        inv.name,
        "status":      inv.status,
        "role":        inv.role.name if inv.role_id else "",
        "title":       inv.title,
        "is_manager":  inv.is_manager,
        "invited_at":  inv.invited_at.isoformat(),
        "expires_at":  inv.expires_at.isoformat(),
        "last_sent_at": inv.last_sent_at.isoformat() if inv.last_sent_at else "",
        "send_count":  inv.send_count,
        "invited_by":  (inv.invited_by.get_full_name() or inv.invited_by.username)
                         if inv.invited_by_id else "",
    }


# ── List + create workspaces ───────────────────────────────────────
@superadmin_required
@require_http_methods(["GET", "POST"])
@csrf_exempt
def api_workspaces(request):
    if request.method == "GET":
        rows = [_workspace_brief(w) for w in OpsWorkspace.objects.all()]
        return JsonResponse({"ok": True, "workspaces": rows})

    # POST → create
    body = _parse(request)
    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse(
            {"ok": False, "error": "name_required"},
            status=400,
        )
    ws = OpsWorkspace.objects.create(
        name=name,
        slug=_slugify_unique(name),
        description=(body.get("description") or "").strip(),
        color=(body.get("color") or "#6366f1").strip(),
        emoji=(body.get("emoji") or "🛠️").strip(),
        created_by=request.user,
    )
    return JsonResponse({"ok": True, "workspace": _workspace_brief(ws)}, status=201)


# ── Workspace detail + update + delete ─────────────────────────────
@superadmin_required
@require_http_methods(["GET", "PATCH", "DELETE"])
@csrf_exempt
def api_workspace_detail(request, pk):
    ws = _ws_or_404(pk)

    if request.method == "GET":
        tenants = [
            _tenant_brief(a.tenant)
            for a in (ws.tenant_assignments
                          .select_related("tenant")
                          .order_by("tenant__username"))
        ]
        members = [
            _member_brief(m)
            for m in (ws.members
                         .select_related("user", "role")
                         .order_by("display_name", "user__username"))
        ]
        invitations = [
            _invite_brief(i)
            for i in (ws.invitations
                          .select_related("invited_by", "role")
                          .order_by("-invited_at"))
        ]
        roles = list(OpsRole.objects.values("id", "name", "color"))
        return JsonResponse({
            "ok": True,
            "workspace":   _workspace_brief(ws),
            "tenants":     tenants,
            "members":     members,
            "invitations": invitations,
            "roles":       roles,
        })

    if request.method == "PATCH":
        body = _parse(request)
        dirty = []
        if "name" in body:
            n = (body.get("name") or "").strip()
            if n:
                ws.name = n
                dirty.append("name")
        for f in ("description", "color", "emoji"):
            if f in body:
                setattr(ws, f, (body.get(f) or "").strip())
                dirty.append(f)
        if "is_active" in body:
            ws.is_active = bool(body.get("is_active"))
            dirty.append("is_active")
        if dirty:
            ws.save(update_fields=dirty + ["updated_at"])
        return JsonResponse({"ok": True, "workspace": _workspace_brief(ws)})

    # DELETE — detach members + drop the workspace. Tenant assignments
    # CASCADE; invites CASCADE; members SET_NULL to preserve auth users.
    ws.members.update(workspace=None)
    ws.delete()
    return JsonResponse({"ok": True})


# ── Tenant assignment ──────────────────────────────────────────────
@superadmin_required
@require_POST
@csrf_exempt
def api_workspace_assign_tenant(request, pk):
    """Assign a tenant (auth_user) to this workspace. If the tenant is
    already in a different workspace, the previous assignment is
    moved (OneToOne on the tenant side guarantees consistency)."""
    ws = _ws_or_404(pk)
    body = _parse(request)
    user_id = body.get("user_id")
    if not user_id:
        return JsonResponse({"ok": False, "error": "user_id_required"}, status=400)
    try:
        tenant = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)

    # OneToOne: replace any existing assignment in a single statement.
    OpsWorkspaceTenant.objects.update_or_create(
        tenant=tenant,
        defaults={
            "workspace":  ws,
            "assigned_by": request.user,
            "note":        (body.get("note") or "").strip(),
        },
    )
    return JsonResponse({"ok": True, "tenant": _tenant_brief(tenant)})


@superadmin_required
@require_http_methods(["DELETE"])
@csrf_exempt
def api_workspace_unassign_tenant(request, pk, tenant_id):
    ws = _ws_or_404(pk)
    OpsWorkspaceTenant.objects.filter(workspace=ws, tenant_id=tenant_id).delete()
    return JsonResponse({"ok": True})


# ── Tenant picker for the assign dialog ────────────────────────────
@superadmin_required
@require_GET
def api_assignable_tenants(request, pk):
    """All real tenants the superadmin can pick from — every active
    auth_user that owns at least one Store. Optionally filtered by
    ``?q=`` substring on username/email."""
    ws = _ws_or_404(pk)
    q = (request.GET.get("q") or "").strip().lower()
    qs = (User.objects
              .filter(is_active=True, store__isnull=False)
              .distinct()
              .order_by("username"))
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(email__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )
    # Tag whether each tenant is currently assigned somewhere — useful
    # for the picker UI to show "Currently in: <Workspace Y>".
    assigned_map = dict(
        OpsWorkspaceTenant.objects
        .values_list("tenant_id", "workspace__name")
    )
    out = []
    for u in qs[:60]:
        brief = _tenant_brief(u)
        brief["current_workspace"] = assigned_map.get(u.id, "")
        brief["is_in_this_workspace"] = assigned_map.get(u.id) == ws.name
        out.append(brief)
    return JsonResponse({"ok": True, "tenants": out})


# ── Invitations ────────────────────────────────────────────────────
@superadmin_required
@require_POST
@csrf_exempt
def api_workspace_invite(request, pk):
    """Create + email a new invitation. Body: {email, name?, role_id?,
    title?, is_manager?}"""
    ws = _ws_or_404(pk)
    body = _parse(request)
    email = (body.get("email") or "").strip().lower()
    if "@" not in email or "." not in email:
        return JsonResponse({"ok": False, "error": "invalid_email"}, status=400)

    role = None
    role_id = body.get("role_id")
    if role_id:
        role = OpsRole.objects.filter(pk=role_id).first()

    invite = OpsInvitation.objects.create(
        workspace=ws,
        email=email,
        name=(body.get("name") or "").strip(),
        role=role,
        title=(body.get("title") or "").strip(),
        is_manager=bool(body.get("is_manager")),
        invited_by=request.user,
        expires_at=timezone.now() + timedelta(days=7),
    )
    sent = send_invitation_email(invite, request=request)
    return JsonResponse({
        "ok": True,
        "invite":    _invite_brief(invite),
        "email_sent": sent,
    }, status=201)


@superadmin_required
@require_POST
@csrf_exempt
def api_workspace_invite_resend(request, pk, iid):
    ws = _ws_or_404(pk)
    invite = get_object_or_404(OpsInvitation, pk=iid, workspace=ws)
    if invite.status != "pending":
        return JsonResponse(
            {"ok": False, "error": "not_pending", "status": invite.status},
            status=400,
        )
    # Extend the window so a resent invite is usable again.
    invite.expires_at = timezone.now() + timedelta(days=7)
    invite.save(update_fields=["expires_at"])
    sent = send_invitation_email(invite, request=request)
    return JsonResponse({
        "ok": True,
        "email_sent": sent,
        "invite":     _invite_brief(invite),
    })


@superadmin_required
@require_POST
@csrf_exempt
def api_workspace_invite_revoke(request, pk, iid):
    ws = _ws_or_404(pk)
    invite = get_object_or_404(OpsInvitation, pk=iid, workspace=ws)
    if invite.status == "accepted":
        return JsonResponse(
            {"ok": False, "error": "already_accepted"},
            status=400,
        )
    invite.status     = "revoked"
    invite.revoked_at = timezone.now()
    invite.revoked_by = request.user
    invite.save(update_fields=["status", "revoked_at", "revoked_by"])
    return JsonResponse({"ok": True, "invite": _invite_brief(invite)})


# ── Member removal ─────────────────────────────────────────────────
@superadmin_required
@require_http_methods(["DELETE"])
@csrf_exempt
def api_workspace_member_remove(request, pk, member_id):
    """Detach an OpsTeamMember from this workspace. Their auth.User
    stays so they can keep logging in (e.g. as a superuser or with no
    workspace), but they lose visibility into this workspace's
    tenants on their next request."""
    ws = _ws_or_404(pk)
    member = get_object_or_404(OpsTeamMember, pk=member_id, workspace=ws)
    member.workspace = None
    member.save(update_fields=["workspace"])
    return JsonResponse({"ok": True})
