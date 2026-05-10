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


def add_user_to_default_channels(user, added_by_user=None):
    """
    Ensure `user` is a member of every default channel defined in CHAT_DEFAULT_CHANNELS.
    Posts a welcome system message in #general on first join.
    Returns list of channels the user was newly added to.
    """
    default_channels = getattr(settings, "CHAT_DEFAULT_CHANNELS", [])
    newly_added = []

    for ch_def in default_channels:
        channel, _ = ChatChannel.objects.get_or_create(
            slug=ch_def["slug"],
            is_dm=False,
            defaults={"name": ch_def["name"], "description": ch_def["description"]},
        )
        _, created = ChannelMember.objects.get_or_create(
            channel=channel,
            user=user,
            defaults={"is_active": True},
        )
        if created:
            newly_added.append(channel)

    # Post welcome message in #general on first add
    general = next((ch for ch in newly_added if ch.slug == "general"), None)
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
    Returns the channel.
    """
    ids = sorted([admin_user.id, new_user.id])
    slug = f"dm-{ids[0]}-{ids[1]}"

    channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
    if channel:
        return channel

    def _display(u):
        return _get_display_name(u)

    name = f"{_display(admin_user)} & {_display(new_user)}"
    channel = ChatChannel.objects.create(name=name, slug=slug, is_dm=True)
    channel.participants.set([admin_user, new_user])
    return channel
