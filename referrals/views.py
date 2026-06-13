"""Referral endpoints.

Two surfaces:

* Authenticated tenant API (JSON) — what the dashboard banner calls
  to fetch the user's link + progress.

* Public landing — ``/invite/<code>/`` stamps a session cookie with
  the referrer's code and redirects the visitor to ``/signup/``. The
  signup view (in core) calls ``attribute_signup`` once the new user
  is created.
"""

from __future__ import annotations

from django.http import JsonResponse, HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

from .models import ReferralCode
from .services import get_or_create_code, progress_for


# Session key used by signup to look up the referrer after signup.
REFERRAL_SESSION_KEY = "ds_referral_code"


def _build_share_url(request, code: str) -> str:
    """Public share URL. Uses the request host so the link works on
    every environment (localhost during dev, dropsigma.com on prod).
    HTTPS only on non-local hosts."""
    host = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    return f"{scheme}://{host}/invite/{code}/"


@login_required(login_url="/login/")
@require_GET
def api_my_link(request):
    """Return the calling user's referral code + share URL. Lazily
    creates the code on first call."""
    rc = get_or_create_code(request.user)
    return JsonResponse({
        "ok":   True,
        "code": rc.code,
        "share_url": _build_share_url(request, rc.code),
    })


@login_required(login_url="/login/")
@require_GET
def api_progress(request):
    """Live progress for the dashboard banner. Cheap — single index
    lookup on ReferralCode."""
    p = progress_for(request.user)
    p["share_url"] = _build_share_url(request, p["code"]) if p.get("code") else ""
    return JsonResponse({"ok": True, **p})


def invite_landing(request, code: str):
    """Public link the referrer shares. We do NOT auto-attribute here
    — attribution happens in signup_view AFTER the new user is
    actually created. This view only stamps the session so signup
    knows which referrer to credit.

    Silently ignores unknown codes (we still redirect to /signup/ so
    the user isn't dead-ended, just without a referral credit)."""
    code = (code or "").strip().upper()
    if code:
        rc = ReferralCode.objects.filter(code=code).first()
        if rc:
            # Don't let a logged-in user accidentally attribute themselves.
            if request.user.is_authenticated and request.user.id == rc.user_id:
                return HttpResponseRedirect("/dashboard/?ref=self")
            try:
                request.session[REFERRAL_SESSION_KEY] = code
                request.session.modified = True
            except Exception:
                pass
    return HttpResponseRedirect(f"/signup/?ref={code}" if code else "/signup/")
