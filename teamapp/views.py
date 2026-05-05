from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import TeamMember, AssignmentRule, ChatChannel, ChatMessage, ChatReaction, ChatReadReceipt
from .serializers import TeamMemberSerializer, AssignmentRuleSerializer


# ─── Admin: Team Members ──────────────────────────────────────────────────────

def _admin_display_name(u):
    full = u.get_full_name()
    return full if full else u.username


@api_view(["GET"])
def team_members_api(request):
    qs = TeamMember.objects.filter(is_active=True).order_by("name")
    if request.user.is_authenticated:
        qs = qs.exclude(user=request.user)
    serializer = TeamMemberSerializer(qs, many=True)

    # Include admin/superuser contacts (excluding self)
    admin_contacts = []
    for u in User.objects.filter(is_superuser=True).order_by("id"):
        if request.user.is_authenticated and u.id == request.user.id:
            continue
        admin_contacts.append({
            "id": None, "user": u.id,
            "name": _admin_display_name(u),
            "role": "owner", "status": "available",
            "is_admin": True,
        })

    return Response({"success": True, "members": serializer.data, "admin_contacts": admin_contacts})


@api_view(["POST"])
def create_team_member_api(request):
    name     = request.data.get("name", "").strip()
    email    = request.data.get("email", "").strip()
    password = request.data.get("password", "").strip()
    role     = request.data.get("role", "support")
    status   = request.data.get("status", "available")
    perms    = request.data.get("permissions", {})

    if not name or not email or not password:
        return Response({"success": False, "message": "Name, email and password are required."}, status=400)

    if TeamMember.objects.filter(email=email).exists():
        return Response({"success": False, "message": "An employee with this email already exists."}, status=400)

    if User.objects.filter(email=email).exists():
        return Response({"success": False, "message": "A user with this email already exists."}, status=400)

    base = email.split("@")[0] + "_emp"
    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{counter}"
        counter += 1

    user = User.objects.create_user(username=username, email=email, password=password)

    member = TeamMember.objects.create(
        user=user,
        name=name,
        email=email,
        role=role,
        status=status,
        permissions=perms,
        is_active=True,
    )

    # Auto-add to the General channel
    general, _ = ChatChannel.objects.get_or_create(slug="general", is_dm=False, defaults={"name": "general", "description": "General team discussion"})
    if general:
        general.members.add(user)
        added_by = _sender_info(request.user)["name"]
        ChatMessage.objects.create(
            channel=general,
            sender=request.user,
            content=f"📢 {name} has been added to #general by {added_by}. Welcome!",
        )

    return Response({"success": True, "member": TeamMemberSerializer(member).data})


@api_view(["DELETE"])
def delete_team_member_api(request, member_id):
    try:
        member = TeamMember.objects.get(id=member_id)
        member.user.delete()
        return Response({"success": True, "message": "Employee deleted."})
    except TeamMember.DoesNotExist:
        return Response({"success": False, "message": "Employee not found."}, status=404)


# ─── Admin: Assignment Rules ──────────────────────────────────────────────────

@api_view(["GET"])
def assignment_rules_api(request):
    rules = AssignmentRule.objects.all().order_by("-id")
    serializer = AssignmentRuleSerializer(rules, many=True)
    return Response({"success": True, "rules": serializer.data})


@api_view(["POST"])
def create_assignment_rule_api(request):
    rule_type      = request.data.get("rule_type")
    assign_to_role = request.data.get("assign_to_role")
    is_active      = request.data.get("is_active", True)

    if not rule_type or not assign_to_role:
        return Response({"success": False, "message": "rule_type and assign_to_role are required."}, status=400)

    rule, created = AssignmentRule.objects.update_or_create(
        rule_type=rule_type,
        defaults={"assign_to_role": assign_to_role, "is_active": is_active}
    )
    serializer = AssignmentRuleSerializer(rule)
    return Response({
        "success": True,
        "created": created,
        "message": "Rule created successfully." if created else "Rule updated successfully.",
        "rule": serializer.data
    })


# ─── Employee Auth ────────────────────────────────────────────────────────────

def employee_login_page(request):
    if request.user.is_authenticated and request.user.team_profile.exists():
        return redirect("/employee/dashboard/")

    if request.method == "GET":
        return redirect("/login/?tab=team")

    # POST — try login
    email    = request.POST.get("email", "").strip()
    password = request.POST.get("password", "").strip()
    try:
        member = TeamMember.objects.get(email=email, is_active=True)
        user = authenticate(request, username=member.user.username, password=password)
        if user:
            login(request, user)
            return redirect("/employee/dashboard/")
    except TeamMember.DoesNotExist:
        pass
    return redirect("/login/?tab=team&error=Invalid+email+or+password.")


def employee_logout_view(request):
    logout(request)
    return redirect("/")


def employee_portal_page(request):
    if not request.user.is_authenticated:
        return redirect("/employee/login/")
    member = request.user.team_profile.first()
    if not member:
        return redirect("/employee/login/")
    return render(request, "employee_dashboard.html", {"member": member})


# ─── Employee APIs ────────────────────────────────────────────────────────────

@api_view(["GET"])
def employee_me_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)
    return Response({"success": True, "member": TeamMemberSerializer(member).data})


@api_view(["GET"])
def employee_orders_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from orders.models import Order

    status_filter = request.GET.get("status", "all")
    orders = Order.objects.filter(assigned_to=member).order_by("-created_at")
    if status_filter != "all":
        orders = orders.filter(payment_status=status_filter)

    data = []
    for order in orders:
        line_items = []
        if order.raw_data and isinstance(order.raw_data, dict):
            line_items = order.raw_data.get("line_items", [])

        billing = (order.raw_data or {}).get("billing", {})
        full_address = ", ".join(filter(None, [
            billing.get("address_1", ""),
            order.city or billing.get("city", ""),
            billing.get("postcode", ""),
            order.country or billing.get("country", ""),
        ]))

        data.append({
            "id":                 order.id,
            "order_number":       order.external_order_id,
            "customer_name":      order.customer_name or "-",
            "customer_phone":     order.customer_phone or "-",
            "customer_city":      order.city or "-",
            "customer_country":   order.country or "-",
            "customer_address":   full_address or "-",
            "customer_email":     order.customer_email or "-",
            "product_name":       order.product_name or "-",
            "payment_status":     order.payment_status or "-",
            "fulfillment_status": order.fulfillment_status or "-",
            "vendor_status":      order.vendor_status or "-",
            "tracking_number":    order.tracking_number or "",
            "total_price":        str(order.total_price) if order.total_price else "-",
            "currency":           order.currency or "",
            "created_at":         order.created_at.isoformat() if order.created_at else None,
            "line_items": [
                {
                    "name":     i.get("name", ""),
                    "quantity": i.get("quantity", 1),
                    "sku":      i.get("sku", ""),
                    "image":    i.get("image", {}).get("src", "") if isinstance(i.get("image"), dict) else "",
                }
                for i in line_items
            ],
        })

    all_mine = Order.objects.filter(assigned_to=member)
    stats = {
        "total":       all_mine.count(),
        "pending":     all_mine.filter(payment_status="pending").count(),
        "processing":  all_mine.filter(payment_status="processing").count(),
        "fulfilled":   all_mine.filter(fulfillment_status="fulfilled").count(),
        "no_tracking": all_mine.filter(tracking_number="").count(),
    }

    return Response({"success": True, "orders": data, "stats": stats})


@api_view(["GET"])
def employee_emails_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from emails.models import EmailThreadAssignment, EmailMessage
    from emails.views import get_thread_contact, extract_clean_email

    assignments = EmailThreadAssignment.objects.filter(assigned_to=member).select_related("store")
    threads = []

    for ta in assignments:
        emails_qs = EmailMessage.objects.filter(store=ta.store).order_by("-created_at")
        thread_emails = [e for e in emails_qs if get_thread_contact(e) == ta.contact]
        if not thread_emails:
            continue
        latest = thread_emails[0]
        threads.append({
            "contact":        ta.contact,
            "store_id":       ta.store.id,
            "store_name":     ta.store.name,
            "assigned_at":    ta.assigned_at.isoformat(),
            "latest_subject": latest.subject or "No subject",
            "latest_body":    (latest.body or "")[:200],
            "latest_status":  latest.status,
            "latest_time":    latest.created_at.isoformat(),
            "total_messages": len(thread_emails),
            "is_resolved":    ta.is_resolved,
            "resolved_at":    ta.resolved_at.isoformat() if ta.resolved_at else None,
        })

    total = len(threads)
    new_count = sum(1 for t in threads if t["latest_status"] == "new")
    replied_count = sum(1 for t in threads if t["latest_status"] == "replied")
    resolved_count = sum(1 for t in threads if t["is_resolved"])

    return Response({
        "success": True,
        "threads": threads,
        "stats": {
            "total":    total,
            "new":      new_count,
            "replied":  replied_count,
            "resolved": resolved_count,
            "other":    total - new_count - replied_count,
        }
    })


@api_view(["GET"])
def employee_thread_detail_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from emails.models import EmailMessage, EmailThreadAssignment
    from emails.views import get_thread_contact, extract_clean_email
    from emails.serializers import EmailMessageSerializer

    store_id = request.GET.get("store_id")
    contact  = extract_clean_email(request.GET.get("contact", ""))

    if not contact:
        return Response({"success": False, "message": "Contact is required."}, status=400)

    assignment = EmailThreadAssignment.objects.filter(
        store_id=store_id, contact=contact, assigned_to=member
    ).first()
    if not assignment:
        return Response({"success": False, "message": "Thread not assigned to you."}, status=403)

    emails_qs = EmailMessage.objects.filter(store_id=store_id).order_by("created_at")
    thread_emails = [e for e in emails_qs if get_thread_contact(e) == contact]

    from emails.models import EmailAccount
    account = EmailAccount.objects.filter(store_id=store_id, is_active=True).first()
    store_email = extract_clean_email(account.email) if account else ""

    serializer = EmailMessageSerializer(thread_emails, many=True)
    return Response({
        "success":     True,
        "contact":     contact,
        "store_email": store_email,
        "is_resolved": assignment.is_resolved,
        "resolved_at": assignment.resolved_at.isoformat() if assignment.resolved_at else None,
        "count":       len(thread_emails),
        "emails":      serializer.data,
    })


@api_view(["POST"])
def employee_thread_resolve_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from emails.models import EmailThreadAssignment
    from emails.views import extract_clean_email
    from django.utils import timezone

    store_id = request.data.get("store_id")
    contact  = extract_clean_email(request.data.get("contact", ""))

    assignment = EmailThreadAssignment.objects.filter(
        store_id=store_id, contact=contact, assigned_to=member
    ).first()
    if not assignment:
        return Response({"success": False, "message": "Thread not assigned to you."}, status=403)

    assignment.is_resolved = True
    assignment.resolved_at = timezone.now()
    assignment.save()
    return Response({"success": True, "message": "Thread marked as resolved."})


@api_view(["POST"])
def employee_thread_reopen_api(request):
    """Admin can reopen a resolved thread."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    from emails.models import EmailThreadAssignment
    from emails.views import extract_clean_email

    store_id = request.data.get("store_id")
    contact  = extract_clean_email(request.data.get("contact", ""))

    assignment = EmailThreadAssignment.objects.filter(
        store_id=store_id, contact=contact
    ).first()
    if not assignment:
        return Response({"success": False, "message": "Assignment not found."}, status=404)

    assignment.is_resolved = False
    assignment.resolved_at = None
    assignment.save()
    return Response({"success": True, "message": "Thread re-opened."})


# ─── Team Chat APIs ───────────────────────────────────────────────────────────

_DEFAULT_CHANNELS = [
    {"name": "general",    "slug": "general",    "description": "General team discussion"},
    {"name": "operations", "slug": "operations", "description": "Orders & vendor ops"},
    {"name": "support",    "slug": "support",    "description": "Customer support"},
]


def _sender_info(user):
    # Team member
    member = user.team_profile.first()
    if member:
        initials = "".join(w[0].upper() for w in member.name.split()[:2])
        return {"name": member.name, "role": member.role, "initials": initials}
    # Vendor
    try:
        vendor = user.vendor_profile
        initials = "".join(w[0].upper() for w in vendor.name.split()[:2])
        return {"name": vendor.name, "role": "vendor", "initials": initials}
    except Exception:
        pass
    # Admin / superuser fallback
    full = user.get_full_name() or user.username
    initials = "".join(w[0].upper() for w in full.split()[:2]) or "AD"
    return {"name": full, "role": "owner", "initials": initials}


def _serialize_message(msg, current_user_id, include_replies=True):
    info = _sender_info(msg.sender)
    reactions = {}
    for r in msg.reactions.select_related("sender"):
        if r.emoji not in reactions:
            reactions[r.emoji] = {"count": 0, "mine": False}
        reactions[r.emoji]["count"] += 1
        if r.sender_id == current_user_id:
            reactions[r.emoji]["mine"] = True

    replies = []
    if include_replies:
        for rep in msg.replies.order_by("created_at").select_related("sender"):
            replies.append(_serialize_message(rep, current_user_id, include_replies=False))

    return {
        "id":             msg.id,
        "content":        msg.content,
        "image_url":      msg.image.url if msg.image else None,
        "sender_id":      msg.sender_id,
        "sender_name":    info["name"],
        "sender_role":    info["role"],
        "sender_initials": info["initials"],
        "created_at":     msg.created_at.isoformat(),
        "is_mine":        msg.sender_id == current_user_id,
        "reactions":      reactions,
        "reply_count":    msg.replies.count(),
        "replies":        replies,
    }


@api_view(["POST"])
def chat_dm_api(request):
    """Get or create a private DM channel between current user and target_user_id."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    target_id = request.data.get("target_user_id")
    if not target_id:
        return Response({"success": False, "message": "target_user_id required."}, status=400)
    try:
        target = User.objects.get(pk=target_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    me = request.user
    if me.id == target.id:
        return Response({"success": False, "message": "Cannot DM yourself."}, status=400)

    # Deterministic slug: dm-{lower_id}-{higher_id}
    a, b = sorted([me.id, target.id])
    slug = f"dm-{a}-{b}"

    channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
    if not channel:
        # Build display name from both sides
        def _display(u):
            try:
                p = u.team_profile.first()
                if p: return p.name
            except Exception: pass
            try:
                v = u.vendor_profile
                if v: return v.name
            except Exception: pass
            return u.get_full_name() or u.username

        name = f"{_display(me)} & {_display(target)}"
        channel = ChatChannel.objects.create(name=name, slug=slug, is_dm=True)
        channel.participants.set([me, target])

    # Build per-caller display name (show the OTHER person's name)
    def _display(u):
        try:
            p = u.team_profile.first()
            if p: return p.name
        except Exception: pass
        try:
            v = u.vendor_profile
            if v: return v.name
        except Exception: pass
        return u.get_full_name() or u.username

    other_name = _display(target)
    return Response({"success": True, "channel_id": channel.id, "channel_name": other_name})


@api_view(["GET", "POST"])
def chat_channels_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    if request.method == "POST":
        name = request.data.get("name", "").strip()
        description = request.data.get("description", "").strip()
        if not name:
            return Response({"success": False, "message": "Name required."}, status=400)
        slug = name.lower().replace(" ", "-")
        base, ctr = slug, 1
        while ChatChannel.objects.filter(slug=slug).exists():
            slug = f"{base}-{ctr}"; ctr += 1
        ch = ChatChannel.objects.create(name=name, slug=slug, description=description)
        return Response({"success": True, "channel": {"id": ch.id, "name": ch.name, "slug": ch.slug, "description": ch.description}})

    for d in _DEFAULT_CHANNELS:
        ChatChannel.objects.get_or_create(slug=d["slug"], defaults={"name": d["name"], "description": d["description"]})

    if request.user.is_superuser:
        channels = ChatChannel.objects.filter(is_dm=False).order_by("id")
    else:
        channels = ChatChannel.objects.filter(is_dm=False, members=request.user).order_by("id")
    data = []
    for ch in channels:
        last = ch.messages.filter(parent=None).order_by("-created_at").first()
        receipt = ChatReadReceipt.objects.filter(user=request.user, channel=ch).first()
        if receipt:
            unread = ch.messages.filter(parent=None, created_at__gt=receipt.last_read_at).count()
        else:
            unread = ch.messages.filter(parent=None).count()
        last_sender = _sender_info(last.sender)["name"] if last else None
        data.append({
            "id":             ch.id,
            "name":           ch.name,
            "slug":           ch.slug,
            "description":    ch.description,
            "message_count":  ch.messages.filter(parent=None).count(),
            "last_message":   last.content[:60] if last else None,
            "last_sender":    last_sender,
            "last_time":      last.created_at.isoformat() if last else None,
            "unread_count":   unread,
        })
    return Response({"success": True, "channels": data})


@api_view(["POST"])
def chat_mark_read_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    channel_id = request.data.get("channel_id")
    if not channel_id:
        return Response({"success": False}, status=400)
    from django.utils import timezone
    ch = ChatChannel.objects.filter(id=channel_id).first()
    if not ch:
        return Response({"success": False}, status=404)
    ChatReadReceipt.objects.update_or_create(
        user=request.user, channel=ch,
        defaults={"last_read_at": timezone.now()}
    )
    return Response({"success": True})


@api_view(["GET"])
def chat_messages_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    channel_id = request.GET.get("channel_id")
    if not channel_id:
        return Response({"success": False, "message": "channel_id required."}, status=400)

    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    if not ch.is_dm and not request.user.is_superuser and not ch.members.filter(pk=request.user.pk).exists():
        return Response({"success": False, "message": "Not a member of this channel."}, status=403)

    msgs = ch.messages.filter(parent=None).select_related("sender").prefetch_related("reactions", "replies__sender", "replies__reactions")
    uid = request.user.id
    return Response({
        "success":         True,
        "current_user_id": uid,
        "channel":         {"id": ch.id, "name": ch.name, "description": ch.description},
        "messages":        [_serialize_message(m, uid) for m in msgs],
    })


@api_view(["POST"])
def chat_send_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    channel_id = request.data.get("channel_id")
    content    = request.data.get("content", "").strip()
    parent_id  = request.data.get("parent_id")

    if not channel_id:
        return Response({"success": False, "message": "channel_id required."}, status=400)

    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    parent = None
    if parent_id:
        parent = ChatMessage.objects.filter(id=parent_id, channel=ch).first()

    msg = ChatMessage.objects.create(channel=ch, sender=request.user, content=content, parent=parent)
    return Response({"success": True, "message": _serialize_message(msg, request.user.id)})


@api_view(["POST"])
def chat_upload_image_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    channel_id = request.data.get("channel_id")
    image_file = request.FILES.get("image")
    if not channel_id or not image_file:
        return Response({"success": False, "message": "channel_id and image required."}, status=400)
    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)
    msg = ChatMessage.objects.create(channel=ch, sender=request.user, content="", image=image_file)
    return Response({"success": True, "message": _serialize_message(msg, request.user.id)})


@api_view(["POST"])
def chat_reaction_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    message_id = request.data.get("message_id")
    emoji      = request.data.get("emoji", "").strip()
    if not message_id or not emoji:
        return Response({"success": False, "message": "message_id and emoji required."}, status=400)

    try:
        msg = ChatMessage.objects.get(id=message_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Message not found."}, status=404)

    obj, created = ChatReaction.objects.get_or_create(message=msg, sender=request.user, emoji=emoji)
    if not created:
        obj.delete()

    reactions = {}
    for r in msg.reactions.select_related("sender"):
        if r.emoji not in reactions:
            reactions[r.emoji] = {"count": 0, "mine": False}
        reactions[r.emoji]["count"] += 1
        if r.sender_id == request.user.id:
            reactions[r.emoji]["mine"] = True

    return Response({"success": True, "added": created, "reactions": reactions})


@api_view(["DELETE"])
def chat_delete_message_api(request, msg_id):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    try:
        msg = ChatMessage.objects.get(id=msg_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Not found."}, status=404)
    if not request.user.is_superuser:
        return Response({"success": False, "message": "Only admins can delete messages."}, status=403)
    msg.delete()
    return Response({"success": True})


@api_view(["PATCH"])
def chat_edit_message_api(request, msg_id):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    try:
        msg = ChatMessage.objects.get(id=msg_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Not found."}, status=404)
    if msg.sender != request.user:
        return Response({"success": False, "message": "Not authorized."}, status=403)
    content = request.data.get("content", "").strip()
    if not content:
        return Response({"success": False, "message": "Content required."})
    msg.content = content
    msg.save()
    return Response({"success": True, "content": msg.content})


@api_view(["GET"])
def chat_channel_members_api(request, channel_id):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    members_data = []
    for u in ch.members.select_related().all():
        info = _sender_info(u)
        members_data.append({"user_id": u.id, "name": info["name"], "role": info["role"], "initials": info["initials"]})

    return Response({"success": True, "channel_id": ch.id, "channel_name": ch.name, "members": members_data})


@api_view(["POST"])
def chat_channel_members_add_api(request, channel_id):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return Response({"success": False, "message": "Admin only."}, status=403)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"success": False, "message": "user_id required."}, status=400)
    try:
        u = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    ch.members.add(u)
    info = _sender_info(u)
    added_by = _sender_info(request.user)["name"]
    # Post a system notification message so the added user sees it as unread
    ChatMessage.objects.create(
        channel=ch,
        sender=request.user,
        content=f"📢 {info['name']} has been added to #{ch.name} by {added_by}.",
    )
    return Response({"success": True, "member": {"user_id": u.id, "name": info["name"], "role": info["role"], "initials": info["initials"]}})


@api_view(["DELETE"])
def chat_channel_members_remove_api(request, channel_id, user_id):
    if not request.user.is_authenticated or not request.user.is_superuser:
        return Response({"success": False, "message": "Admin only."}, status=403)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)
    try:
        u = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    ch.members.remove(u)
    return Response({"success": True})
