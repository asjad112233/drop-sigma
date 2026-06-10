"""Permission helpers for the Drop Sigma Operations Portal.

A user is "ops" if they have a row in `OpsTeamMember` OR they belong to
the `DropSigmaOps` Django group OR they're a superuser (for bootstrap).
"""
from functools import wraps
from django.contrib.auth.models import Group
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse


OPS_GROUP_NAME = "DropSigmaOps"


def is_ops_user(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if hasattr(user, "ops_profile"):
        return True
    return user.groups.filter(name=OPS_GROUP_NAME).exists()


def ops_required(view_func=None, *, api=False):
    """Gate a view to ops-team users only.

    For API endpoints (api=True), returns JSON 403 on failure. For HTML
    views, redirects to /login/ with a next= param.
    """
    def decorator(func):
        @wraps(func)
        def wrapped(request, *args, **kwargs):
            if not is_ops_user(request.user):
                if api or request.path.startswith("/ops/api/"):
                    return JsonResponse(
                        {"ok": False,
                         "error": "Not authorized — Drop Sigma ops team only."},
                        status=403,
                    )
                return redirect(f"{reverse('login')}?next={request.path}")
            return func(request, *args, **kwargs)
        return wrapped

    if view_func is None:
        return decorator
    return decorator(view_func)


def ensure_ops_group():
    """Make sure the DropSigmaOps group exists. Called on first use."""
    group, _ = Group.objects.get_or_create(name=OPS_GROUP_NAME)
    return group
