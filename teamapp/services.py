from django.conf import settings

from .models import TeamMember, AssignmentRule, ChatChannel, ChatMessage, ChannelMember


def get_rule_type_for_order(order):
    payment_status = (order.payment_status or "").lower()
    tracking_number = order.tracking_number

    if "failed" in payment_status or "cancelled" in payment_status:
        return "failed_payment"

    if "refund" in payment_status or "dispute" in payment_status:
        return "refund_dispute"

    if not tracking_number:
        return "tracking_missing"

    return "new_order"


def auto_assign_order(order):
    rule_type = get_rule_type_for_order(order)

    print("Order:", order.id, "Rule:", rule_type)

    rule = AssignmentRule.objects.filter(
        rule_type=rule_type,
        is_active=True
    ).first()

    print("Found rule:", rule)

    if not rule:
        return None

    member = TeamMember.objects.filter(
        role=rule.assign_to_role,
        status="available",
        is_active=True,
        workload__lt=90
    ).order_by("workload").first()

    print("Found member:", member)

    if not member:
        return None

    order.assigned_to = member
    order.save()

    member.workload = min(member.workload + 5, 100)
    member.save()

    return member


def _get_display_name(user):
    member = user.team_profile.first()
    if member:
        return member.name
    try:
        return user.vendor_profile.name
    except Exception:
        pass
    # Admin — use profile full name (updated via profile settings) or username
    return user.get_full_name() or user.username


DEFAULT_CHANNEL_SPECS = [
    ("general",    "Team-wide chat"),
    ("operations", "Orders & vendor ops"),
    ("support",    "Customer support discussions"),
]


def ensure_tenant_default_channels(tenant):
    """Make sure `tenant` (a tenant/admin User) owns the three default group
    channels. Returns a list of ChatChannel rows owned by this tenant.

    Idempotent + race-safe — two concurrent signals can't both create
    duplicate channels because we wrap the .create() in an atomic block
    and let the (owner, name, is_dm=False) unique constraint fail fast
    if a competitor already inserted. Then we fetch the winner.
    """
    from django.db import IntegrityError, transaction

    if tenant is None or not getattr(tenant, "id", None):
        return []
    channels = []
    for name, desc in DEFAULT_CHANNEL_SPECS:
        existing = ChatChannel.objects.filter(
            owner=tenant, name=name, is_dm=False,
        ).first()
        if existing:
            channels.append(existing)
            continue
        base_slug = f"{tenant.username}-{name}".lower().replace(" ", "-")[:50]
        slug = base_slug
        ctr = 1
        while ChatChannel.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{ctr}"[:50]
            ctr += 1
        try:
            with transaction.atomic():
                ch = ChatChannel.objects.create(
                    owner=tenant, name=name, slug=slug, description=desc, is_dm=False,
                )
        except IntegrityError:
            # Lost the race — another signal/worker just created this
            # exact (owner, name) channel. Fetch the winner instead of
            # producing a duplicate.
            ch = ChatChannel.objects.filter(
                owner=tenant, name=name, is_dm=False,
            ).first()
            if ch is None:
                # Truly unexpected — re-raise so we don't swallow a real bug.
                raise
        # Always add the tenant to their own channels.
        ChannelMember.objects.get_or_create(
            channel=ch, user=tenant, defaults={"is_active": True},
        )
        channels.append(ch)
    return channels


def add_user_to_default_channels(user, added_by_user=None):
    """
    Ensure `user` is a member of every default channel owned by `added_by_user`
    (the tenant who is adding them). Posts a welcome system message in
    #general on first join. Returns the list of channels the user was newly
    added to.

    Tenant scoping: members are added ONLY to the tenant's own channels —
    never to another tenant's channels.
    """
    newly_added = []

    # Resolve the tenant. If we weren't given one explicitly, treat the user
    # themselves as their own tenant (e.g. legacy callers).
    tenant = added_by_user or user
    tenant_channels = ensure_tenant_default_channels(tenant)

    for channel in tenant_channels:
        _, created = ChannelMember.objects.get_or_create(
            channel=channel,
            user=user,
            defaults={"is_active": True},
        )
        if created:
            newly_added.append(channel)

    # Post welcome message in tenant's #general on first add.
    general = next((ch for ch in newly_added if ch.name == "general"), None)
    if general:
        user_name = _get_display_name(user)
        added_by_name = _get_display_name(added_by_user) if added_by_user else "Admin"
        sender = added_by_user or user
        ChatMessage.objects.create(
            channel=general,
            sender=sender,
            content=f"📢 {user_name} has been added to #general by {added_by_name}. Welcome!",
        )

    return newly_added


def get_or_create_admin_dm(admin_user, new_user):
    """
    Get or create a private DM channel between admin_user and new_user.
    Returns the channel. Race-safe: two concurrent calls for the same pair
    won't both create a channel — the IntegrityError fallback fetches the
    row that won the unique-slug race.
    """
    from django.db import transaction, IntegrityError

    ids = sorted([admin_user.id, new_user.id])
    slug = f"dm-{ids[0]}-{ids[1]}"

    channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
    if channel:
        # Idempotent guard — make sure both participants are present.
        channel.participants.add(admin_user, new_user)
        return channel

    name = f"{_get_display_name(admin_user)} & {_get_display_name(new_user)}"
    try:
        with transaction.atomic():
            channel = ChatChannel.objects.create(name=name, slug=slug, is_dm=True)
            channel.participants.set([admin_user, new_user])
    except IntegrityError:
        # Lost the race — fetch the winning row.
        channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
        if channel:
            channel.participants.add(admin_user, new_user)
    return channel
