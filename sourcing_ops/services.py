"""Drop Sigma Operations — service-layer helpers.

Houses cross-view business logic that doesn't belong in any one views
module. Specifically: the auto-assignment of a *dedicated Sourcing
Manager* to a tenant on their first chat interaction, plus the welcome-
message bootstrap so the tenant sees a populated thread immediately.
"""
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import OpsTeamMember, TenantManagerAssignment


# ─── Welcome message: system intro + manager's personal hello ────────────
# Two-message sequence so the tenant sees a clear hand-off:
#   1) System notice — "Manager assigned" (formal, sets expectations)
#   2) Manager hello — first personal touch
_SYSTEM_NOTICE_TEMPLATE = (
    "✨ Welcome to Drop Sigma Sourcing\n\n"
    "{manager_full} has been assigned as your permanent Sourcing Manager. "
    "From here on, {manager_first} personally leads every part of your account:\n\n"
    "  • Sourcing & supplier negotiation\n"
    "  • Quote prep and pricing reviews\n"
    "  • Order tracking & shipment updates\n"
    "  • Quality control sign-off\n"
    "  • Returns, replacements and disputes\n\n"
    "One contact, no hand-offs. {manager_first} replies within "
    "{reply_min} minutes during business hours."
)
_MANAGER_HELLO_TEMPLATE = (
    "Hi {tenant_name}, {manager_first} here 👋\n\n"
    "Glad to be your dedicated Sourcing Manager. I've taken full ownership "
    "of your account — anytime you have a product to source, a question "
    "about an order, or just need a quick pricing check, drop it in this "
    "chat and I'll handle it directly.\n\n"
    "Looking forward to working together."
)


def _manager_full_name(m):
    return (m.display_name or m.user.get_full_name() or m.user.username
            or "Your Sourcing Manager")


def _format_welcome(assignment):
    """Return a list of (body, role) tuples for the welcome sequence.

    role = 'system' for the formal notice (rendered differently in UI),
    role = 'manager' for the personal hello.
    """
    m = assignment.manager
    tenant = assignment.tenant
    tenant_name = (tenant.first_name or tenant.username or "there").strip()
    manager_full = _manager_full_name(m)
    manager_first = manager_full.split(" ")[0]
    reply_min = m.typical_reply_min or 5
    notice = _SYSTEM_NOTICE_TEMPLATE.format(
        manager_full=manager_full,
        manager_first=manager_first,
        reply_min=reply_min,
    )
    hello = _MANAGER_HELLO_TEMPLATE.format(
        tenant_name=tenant_name,
        manager_first=manager_first,
    )
    return [(notice, "system"), (hello, "manager")]


def send_manager_welcome_message(assignment) -> bool:
    """Create a welcome message in the tenant's chat thread from the
    newly-assigned manager. Reuses PartnerConversation / PartnerMessage
    (the existing tenant chat model). Picks the same underlying
    SourcingPartner that the tenant aggregate chat uses, so the message
    lands in the visible thread.

    Idempotent: if a welcome was already sent, this is a no-op.
    Returns True if a message was created, False otherwise.
    """
    if assignment is None or assignment.welcome_message_sent:
        return False

    # Local imports to avoid circular imports at app-load time.
    try:
        from sourcing_partners.models import (
            SourcingPartner, PartnerConversation, PartnerMessage,
        )
    except ImportError:
        return False

    # Pick the same SourcingPartner used as the "default desk" for this
    # tenant by the aggregate chat front. Mirror the logic from
    # `_default_partner_for_tenant`: prefer the partner this tenant has
    # ordered from most often (so the welcome lands in the same
    # conversation the tenant actually opens), with fallback to the
    # lowest-id active partner.
    partner = None
    try:
        # VendorOrder lives in sourcing_partners.models (NOT orders.models)
        # and is the per-tenant order shadow used by the chat front. Picking
        # the partner this tenant has ordered from most ensures the welcome
        # lands in the same conversation `_default_partner_for_tenant`
        # returns, which is what the chat panel opens.
        from sourcing_partners.models import VendorOrder
        from django.db.models import Count
        used_id = (VendorOrder.objects.filter(tenant=assignment.tenant)
                   .values("partner_id")
                   .annotate(c=Count("id")).order_by("-c")
                   .values_list("partner_id", flat=True).first())
        if used_id:
            partner = SourcingPartner.objects.filter(id=used_id, is_active=True).first()
    except Exception:
        partner = None
    if not partner:
        partner = (SourcingPartner.objects.filter(is_active=True)
                   .order_by("id").first())
    if not partner:
        # No partners yet — nothing to attach the message to. We still
        # mark the assignment as having sent the welcome so we don't
        # spin on every chat open. Frontend can render a header-only state.
        assignment.welcome_message_sent = True
        assignment.save(update_fields=["welcome_message_sent"])
        return False

    welcome_sequence = _format_welcome(assignment)  # [(body, role), ...]

    with transaction.atomic():
        conv, _ = PartnerConversation.objects.get_or_create(
            tenant=assignment.tenant, partner=partner,
        )
        last_msg = None
        last_body = ""
        for body, _role in welcome_sequence:
            last_msg = PartnerMessage.objects.create(
                conversation=conv,
                direction="in",  # Drop Sigma → tenant
                body=body,
                is_read=False,
            )
            last_body = body
        if last_msg:
            conv.last_message_at = last_msg.created_at
            conv.last_message_preview = last_body[:200]
            conv.unread_count_for_tenant = (
                (conv.unread_count_for_tenant or 0) + len(welcome_sequence)
            )
            conv.save(update_fields=[
                "last_message_at", "last_message_preview",
                "unread_count_for_tenant",
            ])

        assignment.welcome_message_sent = True
        assignment.save(update_fields=["welcome_message_sent"])

    return True


def ensure_tenant_manager(tenant_user):
    """Ensure the tenant has a dedicated manager assigned. Idempotent.

    Picks the customer-facing OpsTeamMember with the fewest active
    dedicated_tenants. Falls back to any active member if no
    customer-facing ones exist. Returns the assignment, or None when
    no ops staff exist at all.
    """
    if not tenant_user or not getattr(tenant_user, "is_authenticated", False):
        return None

    # Fast path — already assigned.
    existing = (TenantManagerAssignment.objects
                .select_related("manager", "manager__user", "manager__role")
                .filter(tenant=tenant_user).first())
    if existing:
        # If the welcome message hasn't been sent yet (e.g. assignment was
        # created before the welcome feature, or the flag was reset for a
        # re-send), fire it now. Idempotent — checks the flag internally.
        if not existing.welcome_message_sent:
            try:
                send_manager_welcome_message(existing)
            except Exception:
                pass
        return existing

    # Race-safe creation: lock the row(s) we touch.
    with transaction.atomic():
        # Re-check inside the transaction in case a sibling request
        # created the assignment between the SELECT above and now.
        existing = (TenantManagerAssignment.objects
                    .select_for_update()
                    .filter(tenant=tenant_user).first())
        if existing:
            return existing

        # Find least-loaded customer-facing manager.
        candidates = (OpsTeamMember.objects
                      .filter(customer_facing=True, status="active")
                      .annotate(load=Count("dedicated_tenants"))
                      .order_by("load", "orders_handled", "id"))
        chosen = candidates.first()
        if not chosen:
            # Fallback: any active ops member.
            chosen = (OpsTeamMember.objects
                      .filter(status="active")
                      .order_by("id").first())
        if not chosen:
            # Last fallback: any ops member at all.
            chosen = OpsTeamMember.objects.order_by("id").first()
        if not chosen:
            return None  # No ops staff yet — caller handles gracefully.

        assignment = TenantManagerAssignment.objects.create(
            tenant=tenant_user,
            manager=chosen,
            assigned_via="auto_round_robin",
        )

    # Welcome message fires here — the moment the assignment is created.
    # That happens on the tenant's first chat interaction (opening the
    # chat OR fetching their manager), so they see the welcome sequence
    # the instant they enter the chat.
    # Idempotent: send_manager_welcome_message checks welcome_message_sent
    # and bails if already sent, so re-entries are safe.
    try:
        send_manager_welcome_message(assignment)
    except Exception:
        # Welcome is best-effort; never break assignment on chat hiccup.
        pass

    return assignment


def reassign_tenant_manager(*, assignment, new_manager,
                            reason="", via="manual_ops"):
    """Atomically move a tenant from one manager to another.

    Updates `previous_manager`, `reassigned_at`, `reassignment_reason`
    on the existing assignment row. Posts a system message in the
    tenant's chat thread announcing the change.
    Returns the refreshed assignment.
    """
    if assignment is None or new_manager is None:
        return assignment
    if assignment.manager_id == new_manager.id:
        return assignment

    old_manager = assignment.manager
    now = timezone.now()

    with transaction.atomic():
        assignment.previous_manager = old_manager
        assignment.manager = new_manager
        assignment.reassigned_at = now
        assignment.reassignment_reason = (reason or "")[:255]
        assignment.assigned_via = via if via in dict(
            TenantManagerAssignment.ASSIGNED_VIA_CHOICES
        ) else "manual_ops"
        assignment.save(update_fields=[
            "previous_manager", "manager", "reassigned_at",
            "reassignment_reason", "assigned_via",
        ])

    # System-style announcement message in tenant's chat thread.
    try:
        from sourcing_partners.models import (
            SourcingPartner, PartnerConversation, PartnerMessage,
        )
        partner = (SourcingPartner.objects.filter(is_active=True)
                   .order_by("id").first())
        if partner:
            old_name = (old_manager.display_name
                        or old_manager.user.get_full_name()
                        or old_manager.user.username)
            new_name = (new_manager.display_name
                        or new_manager.user.get_full_name()
                        or new_manager.user.username)
            body = (f"{old_name} has been reassigned. From today, "
                    f"{new_name} will be handling your account.")
            conv, _ = PartnerConversation.objects.get_or_create(
                tenant=assignment.tenant, partner=partner,
            )
            msg = PartnerMessage.objects.create(
                conversation=conv, direction="in",
                body=body, is_read=False,
            )
            conv.last_message_at = msg.created_at
            conv.last_message_preview = body[:200]
            conv.unread_count_for_tenant = (conv.unread_count_for_tenant or 0) + 1
            conv.save(update_fields=[
                "last_message_at", "last_message_preview",
                "unread_count_for_tenant",
            ])
    except Exception:
        pass

    return assignment
