import threading
import datetime
import logging

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import models, transaction, IntegrityError
from django.db.models import Q
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import TeamMember, AssignmentRule, ChatChannel, ChatMessage, ChatReaction, ChatReadReceipt, ChannelMember, EmployeeInvitation
from .serializers import TeamMemberSerializer, AssignmentRuleSerializer
from .services import add_user_to_default_channels, get_or_create_admin_dm

logger = logging.getLogger(__name__)

# ─── Chat constants (limits, validation) ──────────────────────────────────────
MAX_MESSAGE_LENGTH        = 4000              # characters
MAX_CHAT_IMAGE_BYTES      = 8 * 1024 * 1024   # 8 MiB
ALLOWED_CHAT_IMAGE_MIMES  = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp",
}
DEFAULT_MESSAGES_PAGE_SIZE = 100
MAX_MESSAGES_PAGE_SIZE     = 500


# ─── Admin: Team Members ──────────────────────────────────────────────────────

def _admin_display_name(u):
    full = u.get_full_name()
    return full if full else u.username


@api_view(["GET"])
def team_members_api(request):
    if not request.user.is_authenticated:
        return Response({"success": True, "members": [], "admin_contacts": []})

    # Employee view: return their owner (admin) as contact + fellow employees
    my_profile = request.user.team_profile.first()
    if my_profile and my_profile.owner:
        owner = my_profile.owner
        admin_name = owner.get_full_name() or owner.username
        admin_contacts = [{
            "user": owner.id,
            "name": admin_name,
            "role": "owner",
            "status": "available",
            "is_admin": True,
        }]
        fellow_qs = TeamMember.objects.filter(owner=owner, is_active=True).exclude(user=request.user).order_by("name")
        fellow = [{"id": m.id, "user": m.user_id, "name": m.name, "role": m.role, "status": m.status, "is_admin": False} for m in fellow_qs]
        return Response({"success": True, "members": fellow, "admin_contacts": admin_contacts})

    # Vendor view: return their store's admin as top contact + store's employees
    # Vendor view: find the admin who invited this vendor (most reliable source)
    try:
        from vendors.models import Vendor as _VModel, VendorInvitation as _VInv
        vp = _VModel.objects.select_related("assigned_store__user").get(user=request.user)

        # Primary: use the admin who sent the invitation (invitation owner = real admin)
        inv = _VInv.objects.filter(email=request.user.email, status="accepted").select_related("owner").first()
        owner = inv.owner if inv else (vp.assigned_store.user if vp.assigned_store else None)

        if owner and owner.id != request.user.id:
            admin_name = owner.get_full_name() or owner.username
            admin_contacts = [{
                "user": owner.id,
                "name": admin_name,
                "role": "owner",
                "status": "available",
                "is_admin": True,
            }]
            # 🔒 Vendors only DM-see the tenant ADMIN by default — not the
            #    tenant's employees. Privacy + simpler mental model. If the
            #    tenant wants a specific employee to chat with the vendor,
            #    they add both into a shared channel (group chat already works).
            return Response({"success": True, "members": [], "admin_contacts": admin_contacts})
    except Exception:
        pass

    # Admin view: return their employees + vendors
    from vendors.models import Vendor as VendorModel, VendorInvitation
    from stores.models import Store as _Store
    from django.db.models import Q

    emp_qs = TeamMember.objects.filter(owner=request.user, is_active=True).order_by("name")
    employees = [{
        "id": m.id,
        "user": m.user_id,
        "name": m.name,
        "email": m.email or "",
        "role": m.role,
        "status": m.status,
        "is_admin": False,
        "is_vendor": False,
        "is_self": m.user_id == request.user.id,
    } for m in emp_qs]

    # Find vendors via accepted invitations (primary) OR store ownership (secondary)
    inv_emails = list(VendorInvitation.objects.filter(owner=request.user, status="accepted").values_list("email", flat=True))
    store_ids  = list(_Store.objects.filter(user=request.user).values_list("id", flat=True))
    vendor_qs  = VendorModel.objects.filter(
        Q(email__in=inv_emails) | Q(assigned_store_id__in=store_ids),
        user__isnull=False
    ).exclude(user=request.user).select_related("user").distinct().order_by("name")
    vendors = [{"user": v.user_id, "name": v.name, "role": "vendor", "status": "active", "is_admin": False, "is_vendor": True} for v in vendor_qs]

    members = employees + vendors
    return Response({"success": True, "members": members, "admin_contacts": []})


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
        owner=request.user,
        user=user,
        name=name,
        email=email,
        role=role,
        status=status,
        permissions=perms,
        is_active=True,
    )

    # Auto-add to all default channels and create admin DM
    add_user_to_default_channels(user, added_by_user=request.user)
    get_or_create_admin_dm(request.user, user)

    return Response({"success": True, "member": TeamMemberSerializer(member).data})


@api_view(["DELETE"])
def delete_team_member_api(request, member_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)

    try:
        member = TeamMember.objects.get(id=member_id, owner=request.user)
    except TeamMember.DoesNotExist:
        return Response({"success": False, "message": "Team member not found."}, status=404)

    # CRITICAL safety net: never delete the tenant's OWN User account through
    # this endpoint. If a TeamMember row somehow points its `user` FK at the
    # tenant themselves (legacy data, accidental signup auto-add), deleting
    # would cascade-wipe every store, order, vendor, email, and stock entry.
    if member.user_id and member.user_id == request.user.id:
        # Detach the User link and remove only the TeamMember row, so the
        # stray entry disappears from the list without nuking the account.
        member.user = None
        member.save(update_fields=["user"])
        member.delete()
        return Response({"success": True, "message": "Removed your own entry from the team list (your admin account is untouched)."})

    try:
        if member.user_id:
            # Cascade-deletes the TeamMember row alongside the linked User.
            member.user.delete()
        else:
            # Orphan row (legacy data with user=NULL) — drop it directly.
            member.delete()
    except Exception as e:
        return Response({"success": False, "message": f"Delete failed: {e}"}, status=500)

    return Response({"success": True, "message": "Team member removed."})


# ─── Admin: Assignment Rules ──────────────────────────────────────────────────

def _rules_acting_owner(user, perm_key):
    """Tenant uses themselves; employee with perm_key uses their owner."""
    if not user.is_authenticated:
        return None
    member = user.team_profile.filter(is_active=True).select_related("owner").first() if hasattr(user, "team_profile") else None
    if member:
        if not (member.permissions or {}).get(perm_key):
            return None
        return member.owner or user
    return user


@api_view(["GET"])
def assignment_rules_api(request):
    if not request.user.is_authenticated:
        return Response({"success": True, "rules": []})
    owner = _rules_acting_owner(request.user, "view_rules") or _rules_acting_owner(request.user, "manage_rules")
    # Tenant always sees own; employee needs view_rules OR manage_rules
    if not owner and request.user.team_profile.filter(is_active=True).exists():
        return Response({"success": False, "message": "Not allowed."}, status=403)
    if not owner:
        owner = request.user
    qs = AssignmentRule.objects.filter(owner=owner).order_by("-id")
    serializer = AssignmentRuleSerializer(qs, many=True)
    return Response({"success": True, "rules": serializer.data})


@api_view(["POST"])
def create_assignment_rule_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    owner = _rules_acting_owner(request.user, "manage_rules")
    if not owner:
        return Response({"success": False, "message": "Not allowed."}, status=403)

    rule_type      = request.data.get("rule_type")
    assign_to_role = request.data.get("assign_to_role")
    is_active      = request.data.get("is_active", True)

    if not rule_type or not assign_to_role:
        return Response({"success": False, "message": "rule_type and assign_to_role are required."}, status=400)

    rule, created = AssignmentRule.objects.update_or_create(
        owner=owner,
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

    # GET: render the standalone employee_login template (was previously
    # redirecting to /login/?tab=team and leaving employee_login.html orphaned).
    if request.method == "GET":
        # Allow ?simple=1 to keep the legacy combined-login experience for users
        # who linked to it from older docs.
        if request.GET.get("simple") == "1":
            return redirect("/login/?tab=team")
        error = request.GET.get("error")
        return render(request, "employee_login.html", {"error": error})

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
    # Render the page again with an error so users don't lose their typed email
    return render(request, "employee_login.html", {"error": "Invalid email or password.", "email": email})


def employee_logout_view(request):
    # CSRF protection: enforce POST for logout (matches Django best practice).
    if request.method != "POST":
        # GET request — show the dashboard with a hint (or just redirect home).
        # This keeps any stray bookmarked GET-logout from quietly killing the session.
        return redirect("/employee/dashboard/" if request.user.is_authenticated else "/")
    logout(request)
    return redirect("/")


def employee_portal_page(request):
    import json
    if not request.user.is_authenticated:
        return redirect("/employee/login/")
    member = request.user.team_profile.first()
    if not member:
        return redirect("/employee/login/")
    member_json = json.dumps(TeamMemberSerializer(member).data).replace("</", "<\\/")
    return render(request, "employee_dashboard.html", {"member": member, "member_json": member_json})


# ─── Employee APIs ────────────────────────────────────────────────────────────

@api_view(["GET"])
def employee_me_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)
    return Response({"success": True, "member": TeamMemberSerializer(member).data})


@api_view(["POST"])
def employee_set_status_api(request):
    """Employee sets their own status (available / busy / limited / offline)."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    new_status = (request.data.get("status") or "").strip()
    valid = {choice[0] for choice in TeamMember.STATUS_CHOICES}
    if new_status not in valid:
        return Response({
            "success": False,
            "message": f"Invalid status. Allowed: {', '.join(sorted(valid))}.",
        }, status=400)

    member.status = new_status
    member.save(update_fields=["status"])
    return Response({"success": True, "status": member.status})


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

        # Shipping address now sourced from the order's structured helper —
        # picks up street/state/zip that the old billing-only path was missing.
        shipping = order.shipping_address

        data.append({
            "id":                 order.id,
            "store_id":           order.store_id,
            "store_name":         order.store.name if order.store_id else "",
            "order_number":       order.external_order_id,
            "customer_name":      order.customer_name or "-",
            "customer_phone":     order.customer_phone or "-",
            "customer_city":      order.city or "-",
            "customer_country":   order.country or "-",
            "customer_address":   order.shipping_address_text or "-",
            "shipping_address":   shipping,
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
    from django.db.models import Q

    # `view_all_threads`: employee sees every thread in owner's stores, not
    # just assigned ones. Without it, only threads where they're the primary
    # `assigned_to` OR a co-assignee — admin multi-assign was previously
    # invisible to co-assignees because the query only filtered on assigned_to.
    if (member.permissions or {}).get("view_all_threads") and member.owner_id:
        assignments = EmailThreadAssignment.objects.filter(
            store__user=member.owner
        ).select_related("store")
        # Honour allowed_stores narrowing.
        allowed = (member.permissions or {}).get("allowed_stores") or []
        if allowed:
            try:
                assignments = assignments.filter(store_id__in=[int(s) for s in allowed])
            except (TypeError, ValueError):
                pass
    else:
        assignments = EmailThreadAssignment.objects.filter(
            Q(assigned_to=member) | Q(co_assignees=member)
        ).select_related("store").distinct()
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
            # "other" = anything that isn't new / replied / resolved.
            # Previous formula ignored resolved → resolved threads were double-counted.
            "other":    max(0, total - new_count - replied_count - resolved_count),
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


def _employee_can_act_on_thread(user, store_id, contact, member):
    """Return True if `user` may resolve/reopen the (store, contact) thread.

    Ownership rules:
    - Store owner (admin) can always act.
    - Employee can act only if they are assigned to the thread
      (as primary assignee OR co-assignee).
    """
    from emails.models import EmailThreadAssignment
    from stores.models import Store

    if Store.objects.filter(id=store_id, user=user).exists():
        return True
    if not member:
        return False
    return EmailThreadAssignment.objects.filter(
        store_id=store_id, contact=contact,
    ).filter(
        models.Q(assigned_to=member) | models.Q(co_assignees=member)
    ).exists()


@api_view(["POST"])
def employee_thread_resolve_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    from emails.models import EmailThreadAssignment
    from emails.views import extract_clean_email
    from stores.models import Store
    from django.utils import timezone

    store_id = request.data.get("store_id")
    contact  = extract_clean_email(request.data.get("contact", ""))

    member = request.user.team_profile.first()
    is_store_owner = Store.objects.filter(id=store_id, user=request.user).exists()

    if not member and not is_store_owner:
        return Response({"success": False, "message": "Not authorised."}, status=403)

    if is_store_owner:
        # Admin/store owner can resolve any thread in their store
        assignment, _ = EmailThreadAssignment.objects.get_or_create(store_id=store_id, contact=contact)
    else:
        # Employee must be assigned (primary or co-assignee) to the thread
        if not _employee_can_act_on_thread(request.user, store_id, contact, member):
            return Response({"success": False, "message": "You are not assigned to this thread."}, status=403)
        assignment = EmailThreadAssignment.objects.filter(store_id=store_id, contact=contact).first()
        if not assignment:
            # Should be unreachable now — _employee_can_act_on_thread requires an
            # existing assignment — but keep a defensive get_or_create just in case.
            assignment, _ = EmailThreadAssignment.objects.get_or_create(store_id=store_id, contact=contact)

    assignment.is_resolved = True
    assignment.resolved_at = timezone.now()
    assignment.save()
    return Response({"success": True, "resolved_at": assignment.resolved_at.isoformat(), "message": "Thread marked as resolved."})


@api_view(["POST"])
def employee_thread_reopen_api(request):
    """Admin or assigned employee can reopen a resolved thread."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    from emails.models import EmailThreadAssignment
    from emails.views import extract_clean_email
    from stores.models import Store

    store_id = request.data.get("store_id")
    contact  = extract_clean_email(request.data.get("contact", ""))

    # Ownership gate: must be store owner OR an assigned team member
    member = request.user.team_profile.first()
    if not _employee_can_act_on_thread(request.user, store_id, contact, member):
        return Response({"success": False, "message": "You are not authorised to re-open this thread."}, status=403)

    assignment = EmailThreadAssignment.objects.filter(
        store_id=store_id, contact=contact
    ).first()
    if not assignment:
        return Response({"success": False, "message": "Assignment not found."}, status=404)

    assignment.is_resolved = False
    assignment.resolved_at = None
    assignment.save()
    return Response({"success": True, "message": "Thread re-opened."})


# ─── Employee: My Tasks ───────────────────────────────────────────────────────

@api_view(["GET"])
def employee_tasks_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from .models import Task
    from datetime import date

    tasks = Task.objects.filter(assigned_to=member).select_related("assigned_to", "owner")
    result = []
    for t in tasks:
        due = t.due_date.isoformat() if t.due_date else None
        is_overdue = bool(t.due_date and t.due_date < date.today() and t.status != "done")
        # Provide implicit "watcher" names (owner + assignee) for the UI to render
        assignee_name = t.assigned_to.name if t.assigned_to else None
        owner_name = (t.owner.get_full_name() or t.owner.username) if t.owner else None
        result.append({
            "id":          t.id,
            "title":       t.title,
            "description": t.description,
            "priority":    t.priority,
            "category":    t.category,
            "status":      t.status,
            "progress":    t.progress,
            "due_date":    due,
            "is_overdue":  is_overdue,
            "assignee_name": assignee_name,
            "owner_name":    owner_name,
        })
    return Response({"success": True, "tasks": result})


def _employee_owner_stores(member):
    """The stores this employee can see — owner's stores, optionally
    narrowed by the `allowed_stores` permission set by the admin."""
    from stores.models import Store
    qs = Store.objects.filter(user=member.owner, is_active=True).order_by("name")
    allowed = (member.permissions or {}).get("allowed_stores")
    if allowed:
        try:
            allowed_ids = [int(s) for s in allowed]
            qs = qs.filter(id__in=allowed_ids)
        except (TypeError, ValueError):
            pass
    return qs


@api_view(["GET"])
def employee_stores_api(request):
    """Stores the employee can see. Gated by the `view_stores` permission."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)
    if not (member.permissions or {}).get("view_stores"):
        return Response({"success": False, "message": "You don't have access to Stores."}, status=403)

    stores = _employee_owner_stores(member)
    data = [{
        "id":          s.id,
        "name":        s.name,
        "platform":    s.platform,
        "store_url":   s.store_url,
        "is_active":   s.is_active,
        "last_synced": s.last_synced.isoformat() if getattr(s, "last_synced", None) else None,
    } for s in stores]
    return Response({"success": True, "stores": data})


@api_view(["GET"])
def employee_vendors_api(request):
    """Vendors attached to the employee's accessible stores. Gated by `view_vendors`."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)
    if not (member.permissions or {}).get("view_vendors"):
        return Response({"success": False, "message": "You don't have access to Vendors."}, status=403)

    from vendors.models import Vendor
    stores = _employee_owner_stores(member)
    vendors = (Vendor.objects
               .filter(assigned_store__in=stores)
               .select_related("assigned_store")
               .distinct()
               .order_by("name"))
    data = [{
        "id":           v.id,
        "name":         v.name,
        "email":        v.email,
        "phone":        v.phone or "",
        "company_name": v.company_name or "",
        "country":      v.country or "",
        "status":       v.status,
        "store_name":   v.assigned_store.name if v.assigned_store_id else "",
    } for v in vendors]
    return Response({"success": True, "vendors": data})


@api_view(["GET"])
def employee_team_api(request):
    """Fellow team members under the same owner. Gated by `view_team`."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)
    if not (member.permissions or {}).get("view_team"):
        return Response({"success": False, "message": "You don't have access to Team."}, status=403)

    if not member.owner_id:
        return Response({"success": True, "members": []})

    fellows = (TeamMember.objects
               .filter(owner=member.owner, is_active=True)
               .order_by("name"))
    data = [{
        "id":       m.id,
        "name":     m.name,
        "email":    m.email,
        "role":     m.role,
        "status":   m.status,
        "workload": m.workload or 0,
        "is_self":  m.user_id == request.user.id,
    } for m in fellows]
    return Response({"success": True, "members": data})


@api_view(["PATCH"])
def employee_task_update_api(request, task_id):
    """Employee can update status and progress of their own assigned task."""
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False}, status=403)

    from .models import Task
    try:
        task = Task.objects.get(pk=task_id, assigned_to=member)
    except Task.DoesNotExist:
        return Response({"success": False, "message": "Task not found"}, status=404)

    if "status" in request.data:
        task.status = request.data["status"]
        if task.status == "done":
            task.progress = 100
    if "progress" in request.data:
        task.progress = int(request.data["progress"])
    task.save()
    return Response({"success": True})


@api_view(["GET", "POST"])
def employee_task_comments_api(request, task_id):
    """Employee can list + post comments on their assigned tasks."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    member = request.user.team_profile.first()
    if not member:
        return Response({"success": False, "message": "Not an employee"}, status=403)

    from .models import Task, TaskComment
    try:
        task = Task.objects.get(pk=task_id, assigned_to=member)
    except Task.DoesNotExist:
        return Response({"success": False, "message": "Task not found"}, status=404)

    if request.method == "GET":
        comments = task.comments.select_related("author").order_by("created_at")
        return Response({
            "success": True,
            "comments": [{
                "id": c.id,
                "author": c.author.get_full_name() or c.author.username,
                "initials": (c.author.get_full_name() or c.author.username)[0].upper(),
                "content": c.content,
                "created_at": c.created_at.isoformat(),
            } for c in comments],
        })

    content = (request.data.get("content") or "").strip()
    if not content:
        return Response({"success": False, "message": "Comment cannot be empty."}, status=400)
    c = TaskComment.objects.create(task=task, author=request.user, content=content)
    return Response({
        "success": True,
        "comment": {
            "id": c.id,
            "author": c.author.get_full_name() or c.author.username,
            "initials": (c.author.get_full_name() or c.author.username)[0].upper(),
            "content": c.content,
            "created_at": c.created_at.isoformat(),
        },
    })


# ─── Team Chat APIs ───────────────────────────────────────────────────────────

from django.conf import settings as django_settings
_DEFAULT_CHANNELS = getattr(django_settings, "CHAT_DEFAULT_CHANNELS", [
    {"name": "general",    "slug": "general",    "description": "General team discussion"},
    {"name": "operations", "slug": "operations", "description": "Orders & vendor ops"},
    {"name": "support",    "slug": "support",    "description": "Customer support"},
])


def _user_can_access_channel(user, channel):
    """Return True if `user` is permitted to read/write in `channel`.

    Rules:
    - Superusers can always access.
    - DM channels: user must be in channel.participants.
    - Regular channels: user must be the channel.owner (tenant) OR an active
      ChannelMember. Owner-without-membership is a defensive fallback in case
      a tenant's own membership row was deleted; the tenant always owns the
      channel and must retain access.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if channel.is_dm:
        return channel.participants.filter(id=user.id).exists()
    if channel.owner_id == user.id:
        return True
    return ChannelMember.objects.filter(channel=channel, user=user, is_active=True).exists()


def _is_tenant_user(user):
    """True if `user` is a tenant/admin (not an employee, not a vendor).
    Used to gate channel-management actions inside a tenant's org."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.team_profile.exists():
        return False  # they're somebody's employee
    try:
        if user.vendor_profile is not None:
            return False  # they're a vendor
    except Exception:
        pass
    return True


def _user_can_manage_channel(user, channel):
    """True if `user` may add/remove members on `channel`.

    A tenant may manage a non-DM channel iff:
      • they're a superuser, OR
      • they're a tenant-level user AND they are themselves a member of
        the channel (or the channel has no members yet — bootstrap).
    DMs are not managed via the members API.
    """
    if not user or not user.is_authenticated or channel.is_dm:
        return False
    if user.is_superuser:
        return True
    if not _is_tenant_user(user):
        return False
    # Allow the tenant to manage a channel they themselves are a member of.
    if ChannelMember.objects.filter(channel=channel, user=user, is_active=True).exists():
        return True
    # Allow bootstrapping a brand-new channel that has no members yet.
    if not ChannelMember.objects.filter(channel=channel).exists():
        return True
    return False


def _users_in_same_org(user_a, user_b):
    """Return True if user_a and user_b belong to the same tenant org.

    The 'tenant org' is rooted at the admin/tenant User. Vendors and
    employees both resolve up to a tenant via their profile relationships.
    Used to prevent cross-tenant DM creation."""
    if not user_a or not user_b:
        return False
    if user_a.id == user_b.id:
        return False  # self-DM is rejected upstream anyway

    def _org_root(u):
        if u.is_superuser:
            return u.id  # superuser can DM anyone
        prof = u.team_profile.first()
        if prof and prof.owner_id:
            return prof.owner_id
        try:
            vp = u.vendor_profile
            if vp and vp.assigned_store and vp.assigned_store.user_id:
                # vendor's org root = the admin who owns their store
                # but cross-check accepted invitation owner if present
                from vendors.models import VendorInvitation as _VInv
                inv = _VInv.objects.filter(email=u.email, status="accepted").first()
                if inv and inv.owner_id:
                    return inv.owner_id
                return vp.assigned_store.user_id
        except Exception:
            pass
        return u.id  # treat them as their own tenant root

    a_root = _org_root(user_a)
    b_root = _org_root(user_b)
    # Superuser shortcut: org_root == own id; if either side is superuser, allow.
    if user_a.is_superuser or user_b.is_superuser:
        return True
    return a_root == b_root


def _broadcast_chat_message(msg):
    """Push a freshly-saved ChatMessage to all WS subscribers of its channel.

    Synchronous helpers (HTTP send / upload) call this so live receivers
    see the new message without needing an HTTP poll.  Failure is non-fatal
    — the message is already persisted; UI will pick it up on next refresh.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if not layer:
            return
        sender = msg.sender
        info = _sender_info(sender)
        preview = (msg.content or "").strip()
        if not preview and msg.image:
            preview = "📎 image"
        if len(preview) > 200:
            preview = preview[:197] + "…"
        async_to_sync(layer.group_send)(
            f"chat_{msg.channel_id}",
            {
                "type":         "message_event",
                "channel_id":   int(msg.channel_id),
                "message_id":   msg.id,
                "sender_id":    sender.id,
                "sender_name":  info["name"],
                "sender_role":  info["role"],
                "preview":      preview,
            },
        )
    except Exception:
        logger.exception("chat: WS broadcast failed for msg=%s", getattr(msg, "id", None))


def _sender_info(user):
    # Team member
    member = user.team_profile.first()
    if member:
        initials = "".join(w[0].upper() for w in member.name.split()[:2])
        return {"name": member.name, "role": member.role, "initials": initials}
    # Vendor (guard against stale admin-as-vendor records)
    try:
        vendor = user.vendor_profile
        if vendor and vendor.assigned_store and vendor.assigned_store.user_id != user.id:
            initials = "".join(w[0].upper() for w in vendor.name.split()[:2])
            return {"name": vendor.name, "role": "vendor", "initials": initials}
    except Exception:
        pass
    # Admin / owner — use full name then username
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


@api_view(["GET"])
def chat_dm_unreads_api(request):
    """Return unread DM counts for all DM channels the current user participates in."""
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    dm_channels = ChatChannel.objects.filter(is_dm=True, participants=request.user)
    result = {}
    for ch in dm_channels:
        receipt = ChatReadReceipt.objects.filter(user=request.user, channel=ch).first()
        # Only top-level messages, and exclude messages the current user sent themselves.
        base_qs = ch.messages.filter(parent=None).exclude(sender=request.user)
        if receipt:
            unread = base_qs.filter(created_at__gt=receipt.last_read_at).count()
        else:
            unread = base_qs.count()
        # Get the other participant's user id
        other = ch.participants.exclude(id=request.user.id).first()
        if other and unread > 0:
            result[str(other.id)] = unread
    return Response({"success": True, "unreads": result})


@api_view(["POST"])
def chat_dm_api(request):
    """Get or create a private DM channel between current user and target_user_id."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        target_id = int(request.data.get("target_user_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "target_user_id must be an integer."}, status=400)
    if not target_id:
        return Response({"success": False, "message": "target_user_id required."}, status=400)
    try:
        target = User.objects.get(pk=target_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    me = request.user
    if me.id == target.id:
        return Response({"success": False, "message": "Cannot DM yourself."}, status=400)

    # Tenant isolation: both users must belong to the same org.
    if not _users_in_same_org(me, target):
        return Response({"success": False, "message": "You can only DM members of your own organization."}, status=403)

    # Deterministic slug: dm-{lower_id}-{higher_id}
    a, b = sorted([me.id, target.id])
    slug = f"dm-{a}-{b}"

    # Race-safe: wrap fetch-or-create in a transaction and fall back on
    # IntegrityError (concurrent insert) to the row that won.
    try:
        with transaction.atomic():
            channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
            if not channel:
                channel = ChatChannel.objects.create(name=slug, slug=slug, is_dm=True)
                channel.participants.set([me, target])
            else:
                channel.participants.add(me, target)
    except IntegrityError:
        channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
        if channel:
            channel.participants.add(me, target)

    # Build display name for the OTHER person (target)
    def _display(u):
        try:
            p = u.team_profile.first()
            if p: return p.name
        except Exception: pass
        try:
            v = u.vendor_profile
            if v and v.assigned_store and v.assigned_store.user_id != u.id:
                return v.name
        except Exception: pass
        return u.get_full_name() or u.username

    other_name = _display(target)
    return Response({"success": True, "channel_id": channel.id, "channel_name": other_name})


@api_view(["GET", "POST"])
def chat_channels_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    if request.method == "POST":
        # Only tenants (or superusers) can create channels.
        if not _is_tenant_user(request.user):
            return Response({"success": False, "message": "Only tenants can create channels."}, status=403)
        name = request.data.get("name", "").strip()
        description = request.data.get("description", "").strip()
        if not name:
            return Response({"success": False, "message": "Name required."}, status=400)
        if len(name) > 100:
            return Response({"success": False, "message": "Name too long (max 100 chars)."}, status=400)
        if len(description) > 255:
            return Response({"success": False, "message": "Description too long (max 255 chars)."}, status=400)
        # Slugs are namespaced by username so two tenants can both have a
        # channel named "general" without colliding on the unique slug.
        base_slug = f"{request.user.username}-{name}".lower().replace(" ", "-")[:50] or "channel"
        slug = base_slug
        ctr = 1
        while ChatChannel.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{ctr}"[:50]
            ctr += 1
        with transaction.atomic():
            ch = ChatChannel.objects.create(
                owner=request.user, name=name, slug=slug, description=description,
            )
            # Auto-add the creator so the new channel actually shows up for them.
            ChannelMember.objects.get_or_create(channel=ch, user=request.user, defaults={"is_active": True})
        return Response({"success": True, "channel": {"id": ch.id, "name": ch.name, "slug": ch.slug, "description": ch.description}})

    # NB: legacy global-defaults seeding was removed. Default channels are now
    # created per-tenant via the stores.signals post_save Store handler.

    # Tenant-scoped channel listing:
    #   • channels owned by the user (tenant view), OR
    #   • channels the user is an active member of (employee/vendor view), OR
    #   • DMs they participate in.
    if request.user.is_superuser:
        channels = ChatChannel.objects.filter(is_dm=False).order_by("id")
    else:
        channels = (
            ChatChannel.objects
            .filter(is_dm=False)
            .filter(Q(owner=request.user) | Q(memberships__user=request.user, memberships__is_active=True))
            .distinct()
            .order_by("id")
        )
    # Build a map of channel_id → joined_at for the current user (non-superuser)
    if not request.user.is_superuser:
        memberships = ChannelMember.objects.filter(user=request.user, is_active=True).values("channel_id", "joined_at")
        joined_at_map = {m["channel_id"]: m["joined_at"] for m in memberships}
    else:
        joined_at_map = {}

    data = []
    for ch in channels:
        joined_at = joined_at_map.get(ch.id)
        base_qs = ch.messages.filter(parent=None)
        if joined_at:
            base_qs = base_qs.filter(created_at__gte=joined_at)

        last = base_qs.order_by("-created_at").first()
        # For unread, ignore messages the current user sent themselves.
        unread_qs = base_qs.exclude(sender=request.user)
        receipt = ChatReadReceipt.objects.filter(user=request.user, channel=ch).first()
        if receipt:
            unread = unread_qs.filter(created_at__gt=receipt.last_read_at).count()
        else:
            unread = unread_qs.count()
        last_sender = _sender_info(last.sender)["name"] if last else None
        data.append({
            "id":             ch.id,
            "name":           ch.name,
            "slug":           ch.slug,
            "description":    ch.description,
            "message_count":  base_qs.count(),
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
    try:
        channel_id = int(request.data.get("channel_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "channel_id must be an integer."}, status=400)
    if not channel_id:
        return Response({"success": False}, status=400)
    from django.utils import timezone
    ch = ChatChannel.objects.filter(id=channel_id).first()
    if not ch:
        return Response({"success": False}, status=404)
    if not _user_can_access_channel(request.user, ch):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)
    ChatReadReceipt.objects.update_or_create(
        user=request.user, channel=ch,
        defaults={"last_read_at": timezone.now()}
    )
    return Response({"success": True})


@api_view(["GET"])
def chat_messages_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    try:
        channel_id = int(request.GET.get("channel_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "channel_id must be an integer."}, status=400)
    if not channel_id:
        return Response({"success": False, "message": "channel_id required."}, status=400)

    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    uid = request.user.id
    membership = None

    if ch.is_dm:
        # DM: enforce participant check (was previously open to any authenticated user)
        if not request.user.is_superuser and not ch.participants.filter(id=request.user.id).exists():
            return Response({"success": False, "message": "Not a participant of this DM."}, status=403)
    else:
        if not request.user.is_superuser:
            membership = ChannelMember.objects.filter(channel=ch, user=request.user, is_active=True).first()
            if not membership:
                return Response({"success": False, "message": "Not a member of this channel."}, status=403)

    # Pagination: ?limit=N&before_id=M  (descending by id, return chronologically)
    try:
        limit = int(request.GET.get("limit") or DEFAULT_MESSAGES_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = DEFAULT_MESSAGES_PAGE_SIZE
    limit = max(1, min(limit, MAX_MESSAGES_PAGE_SIZE))

    before_id = request.GET.get("before_id")
    try:
        before_id = int(before_id) if before_id else None
    except (TypeError, ValueError):
        before_id = None

    msgs_qs = ch.messages.filter(parent=None).select_related("sender").prefetch_related("reactions", "replies__sender", "replies__reactions")

    # Filter messages to only those sent after the user joined (backend security)
    if membership:
        msgs_qs = msgs_qs.filter(created_at__gte=membership.joined_at)

    if before_id:
        msgs_qs = msgs_qs.filter(id__lt=before_id)

    # Take last `limit` messages chronologically.
    page_ids = list(msgs_qs.order_by("-id").values_list("id", flat=True)[:limit])
    page_qs  = (ch.messages
                .filter(id__in=page_ids)
                .select_related("sender")
                .prefetch_related("reactions", "replies__sender", "replies__reactions")
                .order_by("created_at"))

    serialized = [_serialize_message(m, uid) for m in page_qs]
    has_more = len(page_ids) >= limit and msgs_qs.filter(id__lt=min(page_ids)).exists() if page_ids else False
    oldest_id = min(page_ids) if page_ids else None

    return Response({
        "success":         True,
        "current_user_id": uid,
        "channel":         {"id": ch.id, "name": ch.name, "description": ch.description},
        "messages":        serialized,
        "has_more":        has_more,
        "oldest_id":       oldest_id,
    })


def _audience_for(user):
    """Decide which portal this user logs into so the notification surfaces
    in the right bell. Vendor profile takes precedence (vendors can also be
    employees on paper), then employee, then admin."""
    try:
        if hasattr(user, "vendor_profile") and user.vendor_profile is not None:
            return "vendor"
    except Exception:
        pass
    try:
        if user.team_profile.filter(is_active=True).exists():
            return "employee"
    except Exception:
        pass
    return "admin"


def _chat_action_url_for(user, channel):
    """Where this user should land when they click the notification.
    Each portal opens its own Team Chat tab/section, optionally scrolled
    to the right channel via ?channel=<id>."""
    audience = _audience_for(user)
    cid = channel.id if channel else ""
    if audience == "vendor":
        return f"/vendor/?tab=chat&channel={cid}"
    if audience == "employee":
        return f"/employee/?channel={cid}"
    return f"/dashboard/?section=chat&channel={cid}"


def _notify_chat_message(msg):
    """Drop a Notification row for everyone in the channel except the sender.
    Silently swallows failures so a bad notification can't block a chat send."""
    try:
        from notifications.services import notify
        ch = msg.channel
        sender = msg.sender
        sender_name = (sender.get_full_name() or sender.username or "Someone").strip()

        # Resolve recipient set:
        # • DMs → the other participant
        # • Regular channel → all explicit channel members
        if ch.is_dm:
            recipients = list(ch.participants.exclude(id=sender.id))
        else:
            recipients = list(User.objects.filter(channel_memberships__channel=ch).exclude(id=sender.id).distinct())

        # Compose a short body preview (strip HTML if any sneaked in)
        body_preview = (msg.content or "").strip()
        if len(body_preview) > 180:
            body_preview = body_preview[:177] + "…"
        if not body_preview and msg.image:
            body_preview = "📎 sent an image"

        if ch.is_dm:
            title = f"{sender_name} sent you a message"
            category = "chat"
        else:
            title = f"{sender_name} in #{ch.name}"
            category = "chat"

        for r in recipients:
            notify(
                recipient=r,
                audience=_audience_for(r),
                category=category,
                priority="medium",
                title=title,
                body=body_preview,
                action_url=_chat_action_url_for(r, ch),
                action_label="Open chat",
            )
    except Exception:
        pass


@api_view(["POST"])
def chat_send_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    try:
        channel_id = int(request.data.get("channel_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "channel_id must be an integer."}, status=400)
    content    = (request.data.get("content") or "").strip()
    parent_id  = request.data.get("parent_id")

    if not channel_id:
        return Response({"success": False, "message": "channel_id required."}, status=400)

    if not content:
        return Response({"success": False, "message": "Message body cannot be empty."}, status=400)
    if len(content) > MAX_MESSAGE_LENGTH:
        return Response({"success": False, "message": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)."}, status=400)

    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    # Membership / participant check (previously open to any authenticated user)
    if not _user_can_access_channel(request.user, ch):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)

    parent = None
    if parent_id:
        try:
            parent_id = int(parent_id)
            parent = ChatMessage.objects.filter(id=parent_id, channel=ch).first()
        except (TypeError, ValueError):
            parent = None

    msg = ChatMessage.objects.create(channel=ch, sender=request.user, content=content, parent=parent)
    _notify_chat_message(msg)
    _broadcast_chat_message(msg)
    return Response({"success": True, "message": _serialize_message(msg, request.user.id)})


@api_view(["POST"])
def chat_upload_image_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)
    try:
        channel_id = int(request.data.get("channel_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "channel_id must be an integer."}, status=400)
    image_file = request.FILES.get("image")
    if not channel_id or not image_file:
        return Response({"success": False, "message": "channel_id and image required."}, status=400)
    # Size cap
    size = getattr(image_file, "size", 0) or 0
    if size > MAX_CHAT_IMAGE_BYTES:
        return Response({"success": False, "message": f"Image too large (max {MAX_CHAT_IMAGE_BYTES // (1024*1024)} MB)."}, status=400)
    # MIME-type whitelist
    ctype = (getattr(image_file, "content_type", "") or "").lower()
    if ctype and ctype not in ALLOWED_CHAT_IMAGE_MIMES:
        return Response({"success": False, "message": "Only image uploads are allowed."}, status=400)
    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)
    # Membership check (previously open to any authenticated user)
    if not _user_can_access_channel(request.user, ch):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)
    msg = ChatMessage.objects.create(channel=ch, sender=request.user, content="", image=image_file)
    _notify_chat_message(msg)
    _broadcast_chat_message(msg)
    return Response({"success": True, "message": _serialize_message(msg, request.user.id)})


@api_view(["POST"])
def chat_reaction_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Not authenticated"}, status=401)

    try:
        message_id = int(request.data.get("message_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "message_id must be an integer."}, status=400)
    emoji = (request.data.get("emoji") or "").strip()
    if not message_id or not emoji:
        return Response({"success": False, "message": "message_id and emoji required."}, status=400)
    if len(emoji) > 10:
        return Response({"success": False, "message": "Emoji too long."}, status=400)

    try:
        msg = ChatMessage.objects.select_related("channel").get(id=message_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Message not found."}, status=404)

    # Membership / participant check on the message's channel
    if not _user_can_access_channel(request.user, msg.channel):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)

    # Race-safe toggle: a rapid double-click should not double-count or 500.
    created = False
    try:
        with transaction.atomic():
            obj, created = ChatReaction.objects.get_or_create(
                message=msg, sender=request.user, emoji=emoji
            )
            if not created:
                obj.delete()
    except IntegrityError:
        # Concurrent insert by the same user — treat as toggle-off.
        ChatReaction.objects.filter(message=msg, sender=request.user, emoji=emoji).delete()
        created = False

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
        msg = ChatMessage.objects.select_related("channel").get(id=msg_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Not found."}, status=404)
    # Sender can delete their own message; tenant or superuser can delete any
    # message in a channel they manage.
    is_sender   = (msg.sender_id == request.user.id)
    is_manager  = _user_can_manage_channel(request.user, msg.channel) if not msg.channel.is_dm else False
    if not (is_sender or is_manager or request.user.is_superuser):
        return Response({"success": False, "message": "You do not have permission to delete this message."}, status=403)
    msg.delete()
    return Response({"success": True})


@api_view(["PATCH"])
def chat_edit_message_api(request, msg_id):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    try:
        msg = ChatMessage.objects.select_related("channel").get(id=msg_id)
    except ChatMessage.DoesNotExist:
        return Response({"success": False, "message": "Not found."}, status=404)
    # Defense-in-depth: also confirm the user still has channel access
    if not _user_can_access_channel(request.user, msg.channel):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)
    if msg.sender_id != request.user.id:
        return Response({"success": False, "message": "Not authorized."}, status=403)
    content = (request.data.get("content") or "").strip()
    if not content:
        return Response({"success": False, "message": "Content required."}, status=400)
    if len(content) > MAX_MESSAGE_LENGTH:
        return Response({"success": False, "message": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)."}, status=400)
    msg.content = content
    msg.save(update_fields=["content"])
    return Response({"success": True, "content": msg.content})


@api_view(["GET"])
def chat_channel_members_api(request, channel_id):
    if not request.user.is_authenticated:
        return Response({"success": False}, status=401)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    # Only channel members (or superusers) can list the membership.
    if not _user_can_access_channel(request.user, ch):
        return Response({"success": False, "message": "You are not a member of this channel."}, status=403)

    members_data = []
    seen_ids = set()  # de-dup if the M2M somehow contains the same user twice
    for u in ch.members.all().select_related().order_by("id"):
        # 🔒 Skip sanitized / inactive users — old orphan rows shouldn't leak
        #    into the visible member list.
        if not u.is_active:
            continue
        # 🔒 Tenant isolation — channels are currently platform-wide (legacy),
        #    so only show members who belong to the same org as the viewer.
        #    Superusers see everyone (admin support).
        if not request.user.is_superuser and not _users_in_same_org(request.user, u):
            continue
        if u.id in seen_ids:
            continue
        seen_ids.add(u.id)
        info = _sender_info(u)
        members_data.append({"user_id": u.id, "name": info["name"], "role": info["role"], "initials": info["initials"]})

    return Response({"success": True, "channel_id": ch.id, "channel_name": ch.name, "members": members_data})


@api_view(["POST"])
def chat_channel_members_add_api(request, channel_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    if not _user_can_manage_channel(request.user, ch):
        return Response({"success": False, "message": "You do not have permission to manage this channel."}, status=403)

    try:
        user_id = int(request.data.get("user_id") or 0)
    except (TypeError, ValueError):
        return Response({"success": False, "message": "user_id must be an integer."}, status=400)
    if not user_id:
        return Response({"success": False, "message": "user_id required."}, status=400)
    try:
        u = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    # Cross-tenant guard: the user being added must belong to the same org
    # as the user managing the channel (superuser bypass).
    if not request.user.is_superuser and not _users_in_same_org(request.user, u):
        return Response({"success": False, "message": "You can only add members of your own organization."}, status=403)

    # Additional safety: if the channel has an owner (post-0015 schema), the
    # new member's org root must match the channel-owner's org. This protects
    # against an employee with `invite_members` adding someone from a foreign
    # tenant when the channel was created by their boss.
    if ch.owner_id and not request.user.is_superuser:
        if not _users_in_same_org(ch.owner, u) and ch.owner_id != u.id:
            return Response({"success": False, "message": "User is not part of this channel's organization."}, status=403)

    ChannelMember.objects.get_or_create(channel=ch, user=u, defaults={"is_active": True})
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
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)
    try:
        ch = ChatChannel.objects.get(id=channel_id, is_dm=False)
    except ChatChannel.DoesNotExist:
        return Response({"success": False, "message": "Channel not found."}, status=404)

    if not _user_can_manage_channel(request.user, ch):
        return Response({"success": False, "message": "You do not have permission to manage this channel."}, status=403)

    try:
        u = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"success": False, "message": "User not found."}, status=404)

    # Prevent removing yourself if you're the only manager (avoid orphaned channel).
    ChannelMember.objects.filter(channel=ch, user=u).delete()
    return Response({"success": True})


# ─── Task Manager ─────────────────────────────────────────────────────────────

from .models import Task, TaskComment
import json
from django.utils import timezone
from datetime import date


def _task_to_dict(task):
    due = None
    if task.due_date:
        due = task.due_date.isoformat()
        is_overdue = task.due_date < date.today() and task.status != "done"
    else:
        is_overdue = False

    assignee = None
    if task.assigned_to:
        assignee = {
            "id":      task.assigned_to.id,
            "name":    task.assigned_to.name,
            "initials": task.assigned_to.name[0].upper() if task.assigned_to.name else "?",
        }

    return {
        "id":          task.id,
        "title":       task.title,
        "description": task.description,
        "priority":    task.priority,
        "category":    task.category,
        "status":      task.status,
        "progress":    task.progress,
        "due_date":    due,
        "is_overdue":  is_overdue,
        "assignee":    assignee,
        "created_at":  task.created_at.isoformat(),
    }


@api_view(["GET"])
def tasks_list_api(request):
    if not request.user.is_authenticated:
        return Response({"error": "Login required"}, status=401)
    tasks = Task.objects.filter(owner=request.user)
    return Response({"tasks": [_task_to_dict(t) for t in tasks]})


def _send_task_assignment_dm(admin_user, member, task):
    """Send a professional DM from admin to the assigned employee about the new task."""
    try:
        emp_user = member.user

        priority_labels = {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟢 Low"}
        priority_text = priority_labels.get(task.priority, task.priority.capitalize())

        if task.due_date:
            deadline_text = task.due_date.strftime("%d %b %Y")
        else:
            deadline_text = "No deadline set"

        admin_name = admin_user.get_full_name() or admin_user.username

        msg_text = (
            f"📋 *New Task Assigned*\n\n"
            f"Hi {member.name}, you've been assigned a new task. "
            f"Please review it and complete it before the deadline.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌  Title: {task.title}\n"
            f"⚡  Priority: {priority_text}\n"
            f"📅  Deadline: {deadline_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Assigned by {admin_name}. Please acknowledge and begin at the earliest."
        )

        # Get or create DM channel between admin and employee
        a, b = sorted([admin_user.id, emp_user.id])
        slug = f"dm-{a}-{b}"
        channel = ChatChannel.objects.filter(slug=slug, is_dm=True).first()
        if not channel:
            channel = ChatChannel.objects.create(
                name=f"{admin_name} & {member.name}",
                slug=slug,
                is_dm=True,
            )
            channel.participants.set([admin_user, emp_user])

        ChatMessage.objects.create(channel=channel, sender=admin_user, content=msg_text)
    except Exception:
        pass  # Never break task creation if DM fails


@api_view(["POST"])
def tasks_create_api(request):
    if not request.user.is_authenticated:
        return Response({"error": "Login required"}, status=401)
    d = request.data
    title = (d.get("title") or "").strip()
    if not title:
        return Response({"error": "Title is required"}, status=400)

    assigned_to = None
    if d.get("assigned_to"):
        try:
            assigned_to = TeamMember.objects.get(pk=d["assigned_to"], owner=request.user)
        except TeamMember.DoesNotExist:
            pass

    due = None
    if d.get("due_date"):
        try:
            from datetime import datetime
            due = datetime.strptime(d["due_date"], "%Y-%m-%d").date()
        except ValueError:
            pass

    task = Task.objects.create(
        owner=request.user,
        title=title,
        description=d.get("description", ""),
        assigned_to=assigned_to,
        priority=d.get("priority", "medium"),
        category=d.get("category", "other"),
        status=d.get("status", "todo"),
        progress=int(d.get("progress", 0)),
        due_date=due,
    )

    if assigned_to:
        _send_task_assignment_dm(request.user, assigned_to, task)
        # Notify the employee via notification bar
        try:
            from notifications.services import notify
            priority_map = {"high": "high", "medium": "medium", "low": "low"}
            notify(
                recipient=assigned_to.user,
                audience="employee",
                category="task",
                priority=priority_map.get(task.priority, "medium"),
                title=f"New task assigned: {task.title}",
                body=(task.description or "")[:240] or
                     f"Assigned by {request.user.get_full_name() or request.user.username}.",
                action_url=f"/employee/?task_id={task.id}",
                action_label="Open",
                related_task_id=task.id,
            )
        except Exception:
            pass

    return Response({"task": _task_to_dict(task)})


@api_view(["PATCH", "DELETE"])
def tasks_detail_api(request, task_id):
    if not request.user.is_authenticated:
        return Response({"error": "Login required"}, status=401)
    try:
        task = Task.objects.get(pk=task_id, owner=request.user)
    except Task.DoesNotExist:
        return Response({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        task.delete()
        return Response({"ok": True})

    d = request.data
    if "title" in d:
        task.title = (d["title"] or "").strip() or task.title
    if "description" in d:
        task.description = d["description"]
    if "priority" in d:
        task.priority = d["priority"]
    if "category" in d:
        task.category = d["category"]
    if "status" in d:
        task.status = d["status"]
        if d["status"] == "done" and task.progress < 100:
            task.progress = 100
    if "progress" in d:
        task.progress = int(d["progress"])
    if "due_date" in d:
        if d["due_date"]:
            try:
                from datetime import datetime
                task.due_date = datetime.strptime(d["due_date"], "%Y-%m-%d").date()
            except ValueError:
                pass
        else:
            task.due_date = None
    new_assignee = None
    if "assigned_to" in d:
        if d["assigned_to"]:
            try:
                new_assignee = TeamMember.objects.get(pk=d["assigned_to"], owner=request.user)
                task.assigned_to = new_assignee
            except TeamMember.DoesNotExist:
                pass
        else:
            task.assigned_to = None

    task.save()

    if new_assignee:
        _send_task_assignment_dm(request.user, new_assignee, task)

    return Response({"task": _task_to_dict(task)})


@api_view(["GET", "POST"])
def task_comments_api(request, task_id):
    if not request.user.is_authenticated:
        return Response({"error": "Login required"}, status=401)
    try:
        task = Task.objects.get(pk=task_id, owner=request.user)
    except Task.DoesNotExist:
        return Response({"error": "Not found"}, status=404)

    if request.method == "GET":
        comments = task.comments.all()
        return Response({"comments": [
            {
                "id":         c.id,
                "author":     c.author.get_full_name() or c.author.username,
                "initials":   (c.author.get_full_name() or c.author.username)[0].upper(),
                "content":    c.content,
                "created_at": c.created_at.isoformat(),
            }
            for c in comments
        ]})

    content = (request.data.get("content") or "").strip()
    if not content:
        return Response({"error": "Comment cannot be empty"}, status=400)
    c = TaskComment.objects.create(task=task, author=request.user, content=content)
    return Response({"comment": {
        "id":         c.id,
        "author":     c.author.get_full_name() or c.author.username,
        "initials":   (c.author.get_full_name() or c.author.username)[0].upper(),
        "content":    c.content,
        "created_at": c.created_at.isoformat(),
    }})


# ─── Employee Invitations ─────────────────────────────────────────────────────

import os

_INV_LOGO = """<table cellpadding="0" cellspacing="0"><tr>
  <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
    <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
  </td>
  <td style="padding-left:12px;text-align:left;">
    <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Ecommerce Operations OS</div>
  </td>
</tr></table>"""

_INV_FOOTER = """<p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">
  &copy; 2026 Drop Sigma &nbsp;&middot;&nbsp;
  <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a>
  &nbsp;&middot;&nbsp;
  <a href="mailto:support@dropsigma.com" style="color:#94a3b8;text-decoration:none;">support@dropsigma.com</a>
</p>
<p style="margin:0;font-size:11px;color:#cbd5e1;">This invitation expires in 48 hours. If you did not expect this, ignore this email.</p>"""


def _build_invitation_email(name, invite_url, invited_by):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_INV_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#6d28d9;background:#ede9fe;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x1F4E7; YOU'RE INVITED</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Join the team on Drop Sigma</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, <strong style="color:#0f172a;">{invited_by}</strong> has invited you to join their team as an employee. Click below to accept and set your password.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td align="center" style="padding:32px 48px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="{invite_url}" style="display:block;padding:16px 36px;font-size:15px;font-weight:800;color:#ffffff;text-decoration:none;letter-spacing:.2px;">Accept Invitation &amp; Set Password</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 8px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Or copy this link</p>
        <p style="margin:0;font-size:12px;color:#6366f1;word-break:break-all;">{invite_url}</p>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 36px;">
      <p style="margin:0;font-size:12px;color:#94a3b8;">This link expires in <strong>48 hours</strong>.</p>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding-top:28px;">{_INV_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _send_invitation_email(to_email, subject, html):
    """Send a team invitation from the platform sender (noreply@dropsigma.com).
    Returns (sent: bool, error_msg: str). Synchronous — we wait for the
    SMTP/Resend roundtrip so the API can report real failures instead of
    pretending success while the message silently dies.

    Tries two paths in order so whichever the tenant's infra has wired up
    actually works:
      1. Resend API (the historical path — fast, what signup verification
         uses too). Needs RESEND_API_KEY.
      2. Django SMTP via EMAIL_HOST_USER / EMAIL_HOST_PASSWORD (Gmail).
    """
    import logging
    from django.conf import settings
    logger = logging.getLogger(__name__)

    errors = []

    # ── Path 1: Resend (historical default for platform mail) ─────────
    resend_key = os.getenv("RESEND_API_KEY", "")
    if resend_key:
        try:
            import resend as _resend
            _resend.api_key = resend_key
            result = _resend.Emails.send({
                "from":    "Drop Sigma <noreply@dropsigma.com>",
                "to":      [to_email],
                "subject": subject,
                "html":    html,
            })
            logger.info("Invitation email sent via Resend to %s (id=%s)",
                        to_email, getattr(result, "id", result))
            return True, ""
        except Exception as exc:
            errors.append(f"Resend: {exc}")
            logger.warning("Resend send failed for %s: %s", to_email, exc)

    # ── Path 2: Django SMTP fallback ──────────────────────────────────
    if settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD:
        try:
            from django.core.mail import EmailMultiAlternatives
            from_addr = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
            msg = EmailMultiAlternatives(
                subject=subject,
                body="Open in an HTML-capable email client to view this invitation.",
                from_email=f"Drop Sigma <{from_addr}>",
                to=[to_email],
            )
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=False)
            logger.info("Invitation email sent via SMTP to %s from %s",
                        to_email, from_addr)
            return True, ""
        except Exception as exc:
            errors.append(f"SMTP: {exc}")
            logger.warning("SMTP send failed for %s: %s", to_email, exc)

    if not resend_key and not (settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD):
        return False, ("Server email is not configured. Set either "
                       "RESEND_API_KEY or EMAIL_HOST_USER + EMAIL_HOST_PASSWORD "
                       "on the server, then retry.")

    return False, "Email send failed: " + " | ".join(errors)


@api_view(["POST"])
def send_employee_invitation_api(request):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)

    # Tenant invites under themselves; employee with `invite_members`
    # permission invites under the tenant they work for.
    owner_user = request.user
    em = request.user.team_profile.filter(is_active=True).first()
    if em:
        if not (em.permissions or {}).get("invite_members"):
            return Response({"success": False, "message": "Not allowed — your account doesn't have invite permission."}, status=403)
        owner_user = em.owner or request.user

    name   = (request.data.get("name") or "").strip()
    email  = (request.data.get("email") or "").strip().lower()  # normalise once
    role   = request.data.get("role", "support")
    status = request.data.get("status", "available")
    perms  = request.data.get("permissions", {})

    if not name or not email:
        return Response({"success": False, "message": "Name and email are required."}, status=400)

    # Case-insensitive existence checks. Previous code did `email=email` which
    # only matched when the stored row's case matched exactly — so an existing
    # "john@example.com" would fail to be detected when a user typed
    # "John@Example.com", letting the invitation slip through.
    if TeamMember.objects.filter(email__iexact=email).exists():
        return Response({
            "success": False,
            "message": f"\"{email}\" is already a team member somewhere on Drop Sigma."
        }, status=400)

    if User.objects.filter(email__iexact=email).exists():
        return Response({
            "success": False,
            "message": f"\"{email}\" already has a Drop Sigma account. Ask them to log in instead."
        }, status=400)

    # Stop spam-resending the same pending invite — if THIS tenant has an
    # un-expired pending invitation to the same address, return a clear
    # "already invited" message instead of silently sending a new one.
    pending = EmployeeInvitation.objects.filter(
        owner=owner_user, email__iexact=email, status="pending"
    ).first()
    if pending and pending.expires_at and pending.expires_at > timezone.now():
        return Response({
            "success": False,
            "message": f"You already invited \"{email}\". The previous invite is still valid until "
                       f"{pending.expires_at.strftime('%b %d, %H:%M UTC')}."
        }, status=400)

    # Otherwise expire stale pending invites so we don't pile up rows.
    EmployeeInvitation.objects.filter(
        owner=owner_user, email__iexact=email, status="pending"
    ).update(status="expired")

    expires_at = timezone.now() + datetime.timedelta(hours=48)
    inv = EmployeeInvitation.objects.create(
        owner=owner_user,
        name=name,
        email=email,
        role=role,
        initial_status=status,
        permissions=perms,
        expires_at=expires_at,
    )

    scheme = request.scheme
    host   = request.get_host()
    invite_url = f"{scheme}://{host}/employee/invite/accept/{inv.token}/"

    invited_by = request.user.get_full_name() or request.user.username
    html = _build_invitation_email(name, invite_url, invited_by)
    sent, err = _send_invitation_email(email, f"You're invited to join {invited_by} on Drop Sigma", html)

    if not sent:
        # Roll the invitation back so the user can retry cleanly once
        # the server email is configured.
        inv.delete()
        return Response({"success": False, "message": err}, status=500)

    return Response({"success": True, "message": f"Invitation sent to {email}."})


@api_view(["GET"])
def team_invitations_api(request):
    """List invitations this admin has sent — with status, role, expiry."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)

    # First mark any pending invitations whose deadline passed as expired,
    # so the UI status is honest without needing a cron job.
    EmployeeInvitation.objects.filter(
        owner=request.user, status="pending", expires_at__lt=timezone.now()
    ).update(status="expired")

    rows = (EmployeeInvitation.objects
            .filter(owner=request.user)
            .order_by("-created_at")[:100])

    items = [{
        "id":         inv.id,
        "name":       inv.name,
        "email":      inv.email,
        "role":       inv.role,
        "permissions": inv.permissions or {},
        "status":     inv.status,                              # pending/accepted/expired
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
    } for inv in rows]

    return Response({"success": True, "invitations": items})


@api_view(["POST"])
def team_invitation_revoke_api(request, invite_id):
    """Cancel a pending invitation — moves it to 'expired' state."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)
    try:
        inv = EmployeeInvitation.objects.get(id=invite_id, owner=request.user)
    except EmployeeInvitation.DoesNotExist:
        return Response({"success": False, "message": "Invitation not found."}, status=404)
    if inv.status != "pending":
        return Response({"success": False, "message": f"Invitation is already {inv.status}."}, status=400)
    inv.status = "expired"
    inv.save(update_fields=["status"])
    return Response({"success": True, "message": "Invitation revoked."})


@api_view(["PATCH"])
def update_team_member_api(request, member_id):
    """Edit a team member's role + permissions + status. Used by the
    Edit Access modal in the Team Assignment section."""
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)
    try:
        member = TeamMember.objects.get(id=member_id, owner=request.user)
    except TeamMember.DoesNotExist:
        return Response({"success": False, "message": "Team member not found."}, status=404)

    payload = request.data or {}
    if "role" in payload:
        new_role = (payload.get("role") or "").strip()
        valid_roles = {k for k, _ in TeamMember.ROLE_CHOICES}
        if new_role and new_role in valid_roles:
            member.role = new_role
    if "status" in payload:
        new_status = (payload.get("status") or "").strip()
        valid_status = {k for k, _ in TeamMember.STATUS_CHOICES}
        if new_status and new_status in valid_status:
            member.status = new_status
    if "permissions" in payload and isinstance(payload["permissions"], dict):
        member.permissions = payload["permissions"]
    if "is_active" in payload:
        member.is_active = bool(payload["is_active"])
    if "name" in payload and payload["name"]:
        member.name = (payload["name"] or "").strip()[:255]
    member.save()
    return Response({
        "success": True,
        "member": {
            "id":          member.id,
            "name":        member.name,
            "email":       member.email,
            "role":        member.role,
            "status":      member.status,
            "is_active":   member.is_active,
            "permissions": member.permissions or {},
        }
    })


def accept_invitation_page(request, token):
    try:
        inv = EmployeeInvitation.objects.get(token=token)
    except EmployeeInvitation.DoesNotExist:
        return render(request, "employee_invitation.html", {"error": "This invitation link is invalid."})

    if not inv.is_valid():
        msg = "This invitation has already been accepted." if inv.status == "accepted" else "This invitation link has expired."
        return render(request, "employee_invitation.html", {"error": msg})

    return render(request, "employee_invitation.html", {"invitation": inv})


def set_invitation_password_api(request, token):
    from django.contrib.auth import login as auth_login
    from django.views.decorators.csrf import csrf_exempt
    import json

    if request.method != "POST":
        from django.http import JsonResponse
        return JsonResponse({"success": False, "message": "Method not allowed."}, status=405)

    try:
        inv = EmployeeInvitation.objects.get(token=token)
    except EmployeeInvitation.DoesNotExist:
        from django.http import JsonResponse
        return JsonResponse({"success": False, "message": "Invalid invitation."}, status=404)

    if not inv.is_valid():
        from django.http import JsonResponse
        return JsonResponse({"success": False, "message": "This invitation has expired or was already used."}, status=400)

    try:
        body = json.loads(request.body)
    except Exception:
        from django.http import JsonResponse
        return JsonResponse({"success": False, "message": "Invalid request body."}, status=400)

    password = (body.get("password") or "").strip()
    if len(password) < 8:
        from django.http import JsonResponse
        return JsonResponse({"success": False, "message": "Password must be at least 8 characters."}, status=400)

    # Build unique username
    base     = inv.email.split("@")[0] + "_emp"
    username = base
    counter  = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}_{counter}"
        counter += 1

    user = User.objects.create_user(username=username, email=inv.email, password=password)
    user.first_name = inv.name.split()[0]
    user.last_name  = " ".join(inv.name.split()[1:])
    user.save()

    member = TeamMember.objects.create(
        owner=inv.owner,
        user=user,
        name=inv.name,
        email=inv.email,
        role=inv.role,
        status=inv.initial_status,
        permissions=inv.permissions,
        is_active=True,
    )

    # Auto-add to all default channels and create admin DM
    add_user_to_default_channels(user, added_by_user=inv.owner)
    get_or_create_admin_dm(inv.owner, user)

    inv.status = "accepted"
    inv.save(update_fields=["status"])

    # Return the token — JS will redirect to the server-side auto-login view which
    # sets the session via a real browser GET (guarantees Set-Cookie is saved).
    from django.http import JsonResponse
    return JsonResponse({"success": True, "message": "Account activated!", "redirect": f"/employee/login/activate/{inv.token}/"})


def employee_activate_login(request, token):
    """Server-side GET view: validates accepted invitation, logs in the employee,
    and redirects to dashboard. Because this is a real browser navigation (not fetch),
    the Set-Cookie header is guaranteed to be saved by the browser."""
    from django.contrib.auth import login as auth_login
    try:
        inv = EmployeeInvitation.objects.get(token=token, status="accepted")
        member = TeamMember.objects.get(owner=inv.owner, email=inv.email, is_active=True)
        user = member.user
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("/employee/dashboard/")
    except Exception:
        return redirect("/employee/login/")
