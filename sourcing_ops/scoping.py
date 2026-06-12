"""Workspace-aware visibility helpers for the OPS portal.

Every OPS view that lists tenants, orders, chat threads, supplier
mappings, etc. must run its querysets through one of these helpers
so a workspace-scoped ops user can ONLY see data belonging to the
tenants assigned to their workspace.

Three principles drive the design:

  1. **Superusers always see everything.** Drop Sigma's founders
     bootstrap workspaces from above; their visibility must never be
     accidentally clipped by a workspace boundary.
  2. **Legacy ops users (no workspace) keep seeing everything.** The
     ``workspace`` field on ``OpsTeamMember`` is nullable on
     purpose — existing ops staff who predate this feature still see
     the full fleet until a superadmin re-homes them. This makes the
     rollout zero-disturbance.
  3. **Workspace members are strict.** If ``ops_profile.workspace``
     is set, every queryset is filtered down to tenants assigned to
     that workspace via ``OpsWorkspaceTenant``. There is no
     "see-just-this-once" override; the superadmin has to move the
     tenant before they show up.

Every helper accepts a ``base_qs`` so callers can pre-filter by other
criteria (active stores, search box, etc.) without losing the
workspace scoping.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from .models import OpsWorkspace, OpsWorkspaceTenant


User = get_user_model()


def workspace_for(user) -> OpsWorkspace | None:
    """Return the workspace this ops user is bound to.

    Returns ``None`` for superusers, anonymous users, or legacy ops
    members with no workspace set — those are unscoped and see every
    tenant. Callers check ``is None`` to take the unscoped fast path.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if user.is_superuser:
        return None
    profile = getattr(user, "ops_profile", None)
    if not profile:
        return None
    return profile.workspace


def visible_tenant_ids(user) -> set[int] | None:
    """Return the set of ``auth_user.id`` values this ops user is
    allowed to see as tenants.

    Returns ``None`` when the caller is unscoped (superuser / legacy
    ops member) so they can branch with ``if ids is None`` and skip
    the filter entirely. Returns an empty set when the user IS scoped
    but their workspace has no tenants assigned yet — the view should
    render the "no tenants assigned" empty state in that case.
    """
    ws = workspace_for(user)
    if ws is None:
        return None
    # Defensive: avoid pulling the whole queryset into memory if a
    # workspace ever grows beyond the few hundred tenants we expect.
    return set(
        OpsWorkspaceTenant.objects
        .filter(workspace=ws)
        .values_list("tenant_id", flat=True)
    )


def tenant_qs_visible_to(user, base_qs=None):
    """Tenant ``auth_user`` queryset clipped to this ops user's
    workspace. Pass ``base_qs`` to keep an existing filter (e.g. active
    users only). Returns ``base_qs`` unchanged for unscoped users."""
    if base_qs is None:
        base_qs = User.objects.all()
    ids = visible_tenant_ids(user)
    if ids is None:
        return base_qs
    if not ids:
        return base_qs.none()
    return base_qs.filter(id__in=ids)


def order_qs_visible_to(user, base_qs=None):
    """``orders.Order`` queryset clipped to tenants visible in this
    workspace. The Order → Store → User chain is used (the Store's
    ``user`` IS the tenant)."""
    from orders.models import Order
    if base_qs is None:
        base_qs = Order.objects.all()
    ids = visible_tenant_ids(user)
    if ids is None:
        return base_qs
    if not ids:
        return base_qs.none()
    return base_qs.filter(store__user_id__in=ids)


def store_qs_visible_to(user, base_qs=None):
    """``stores.Store`` queryset clipped to this workspace's tenants."""
    from stores.models import Store
    if base_qs is None:
        base_qs = Store.objects.all()
    ids = visible_tenant_ids(user)
    if ids is None:
        return base_qs
    if not ids:
        return base_qs.none()
    return base_qs.filter(user_id__in=ids)


def conversation_qs_visible_to(user, base_qs=None):
    """``sourcing_partners.PartnerConversation`` queryset clipped to
    this workspace's tenants — used by the ops-side chat inbox."""
    from sourcing_partners.models import PartnerConversation
    if base_qs is None:
        base_qs = PartnerConversation.objects.all()
    ids = visible_tenant_ids(user)
    if ids is None:
        return base_qs
    if not ids:
        return base_qs.none()
    return base_qs.filter(tenant_id__in=ids)
