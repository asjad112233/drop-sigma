import json

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from .models import Notification


def _serialize(n):
    return {
        "id":             n.id,
        "audience":       n.audience,
        "category":       n.category,
        "priority":       n.priority,
        "title":          n.title,
        "body":           n.body,
        "icon":           n.icon,
        "action_url":     n.action_url,
        "action_label":   n.action_label,
        "is_read":        n.is_read,
        "created_at":     n.created_at.isoformat(),
        "time_ago":       _time_ago(n.created_at),
        "related_order_id": n.related_order_id,
        "related_email_id": n.related_email_id,
        "related_task_id":  n.related_task_id,
    }


def _time_ago(dt):
    diff = timezone.now() - dt
    s = int(diff.total_seconds())
    if s < 5:        return "Just now"
    if s < 60:       return f"{s}s ago"
    if s < 3600:     return f"{s // 60}m ago"
    if s < 86400:    return f"{s // 3600}h ago"
    if s < 604800:   return f"{s // 86400}d ago"
    return dt.strftime("%b %d")


def _group_day(dt):
    now = timezone.localtime()
    local = timezone.localtime(dt)
    if local.date() == now.date():
        return "Today"
    if (now.date() - local.date()).days == 1:
        return "Yesterday"
    if (now.date() - local.date()).days < 7:
        return local.strftime("%A")
    return "Earlier"


@login_required(login_url="/login/")
@require_GET
def api_list(request):
    """Paginated notifications for the current user.
    Optional filters:
      ?audience=admin|vendor|employee
      ?tab=unread|all
      ?limit=20 (default 30, max 100)
    """
    qs = Notification.objects.filter(recipient=request.user)

    audience = request.GET.get("audience")
    if audience:
        qs = qs.filter(audience=audience)

    if request.GET.get("tab") == "unread":
        qs = qs.filter(is_read=False)

    try:
        limit = max(1, min(100, int(request.GET.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30

    notifications = list(qs[:limit])

    # Group by day for the UI
    grouped = {}
    order = []
    for n in notifications:
        day = _group_day(n.created_at)
        if day not in grouped:
            grouped[day] = []
            order.append(day)
        grouped[day].append(_serialize(n))

    groups = [{"day": d, "items": grouped[d]} for d in order]

    unread_total = Notification.objects.filter(recipient=request.user, is_read=False).count()
    total = Notification.objects.filter(recipient=request.user).count()

    return JsonResponse({
        "success":      True,
        "groups":       groups,
        "unread_count": unread_total,
        "total":        total,
    })


@login_required(login_url="/login/")
@require_GET
def api_unread_count(request):
    n = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return JsonResponse({"count": n})


@csrf_exempt
@login_required(login_url="/login/")
@require_POST
def api_mark_read(request, pk):
    try:
        n = Notification.objects.get(pk=pk, recipient=request.user)
    except Notification.DoesNotExist:
        return JsonResponse({"success": False, "error": "Not found"}, status=404)
    if not n.is_read:
        n.is_read = True
        n.read_at = timezone.now()
        n.save(update_fields=["is_read", "read_at"])
    return JsonResponse({"success": True})


@csrf_exempt
@login_required(login_url="/login/")
@require_POST
def api_mark_all_read(request):
    body = {}
    try:
        body = json.loads(request.body or b"{}")
    except Exception:
        pass
    qs = Notification.objects.filter(recipient=request.user, is_read=False)
    if body.get("audience"):
        qs = qs.filter(audience=body["audience"])
    updated = qs.update(is_read=True, read_at=timezone.now())
    return JsonResponse({"success": True, "marked_read": updated})


@csrf_exempt
@login_required(login_url="/login/")
@require_POST
def api_delete(request, pk):
    try:
        n = Notification.objects.get(pk=pk, recipient=request.user)
    except Notification.DoesNotExist:
        return JsonResponse({"success": False, "error": "Not found"}, status=404)
    n.delete()
    return JsonResponse({"success": True})
