"""OPS team-member invitation flow.

Three things live here:

  1. ``send_invitation_email(invite, *, request=None)`` — Builds the
     HTML email a brand-new ops team member receives, signs the
     accept link with the invite's UUID token, and dispatches via
     core.password_reset.send_platform_email so the From address
     stays on noreply@dropsigma.com regardless of which tenant's
     Gmail is connected. Also updates ``last_sent_at`` /
     ``send_count`` so the superadmin UI can show "Resent 5 min ago".

  2. ``accept_invitation_view`` — The public endpoint at
     ``/ops/invite/accept/<uuid:token>/``. Renders a set-password
     form for the invitee.

  3. ``submit_invitation_accept_api`` — JSON endpoint the form
     posts to. Validates the password, creates the auth User if it
     doesn't exist, builds the OpsTeamMember row inside the
     workspace, marks the invitation accepted, and logs the new
     user in. After this the invitee can hit /ops/ and only see
     the workspace's assigned tenants.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth import login as auth_login
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.password_reset import send_platform_email
from .models import OpsInvitation, OpsTeamMember


User = get_user_model()


# ── Email rendering ────────────────────────────────────────────────
def _build_invite_html(invite: OpsInvitation, accept_url: str) -> str:
    """Drop Sigma branded invite — same shell as the vendor / employee
    invitation emails so a tenant who's also an ops member recognises
    the look-and-feel instantly."""
    workspace_name = invite.workspace.name
    invited_name   = invite.name or invite.email.split("@")[0]
    role_label     = invite.role.name if invite.role_id else "Drop Sigma Operations"
    invited_by_disp = (
        (invite.invited_by.get_full_name() or invite.invited_by.username)
        if invite.invited_by_id else "the Drop Sigma team"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f6fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:36px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0"
             style="max-width:560px;width:100%;background:#fff;border-radius:18px;
                    overflow:hidden;box-shadow:0 14px 40px rgba(15,23,42,.10);">
        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#6366f1 0%,#a855f7 60%,#06b6d4 100%);
                        padding:34px 40px 28px;text-align:center;">
          <table cellpadding="0" cellspacing="0" align="center"><tr>
            <td style="vertical-align:middle;padding-right:10px;">
              <img src="https://dropsigma.com/static/branding/icon.png" width="42" height="42"
                   alt="DS" style="display:block;border-radius:11px;background:#fff;padding:3px;">
            </td>
            <td style="vertical-align:middle;font-size:20px;font-weight:900;color:#fff;letter-spacing:-.3px;">
              Drop Sigma
            </td>
          </tr></table>
          <p style="margin:14px 0 0;font-size:11px;letter-spacing:.2em;color:rgba(255,255,255,.85);
                    text-transform:uppercase;font-weight:800;">Operations Portal · Invitation</p>
        </td></tr>
        <!-- Body -->
        <tr><td style="padding:38px 44px 28px;color:#0f172a;">
          <h1 style="margin:0 0 14px;font-size:24px;font-weight:900;letter-spacing:-.4px;line-height:1.25;">
            You're invited to join the {workspace_name} ops desk
          </h1>
          <p style="margin:0 0 22px;font-size:14.5px;color:#475569;line-height:1.6;font-weight:500;">
            Hi {invited_name}, {invited_by_disp} has added you to <b style="color:#0f172a;">{workspace_name}</b>
            as a <b style="color:#0f172a;">{role_label}</b>. The Drop Sigma Operations Portal is where the desk
            handles every tenant-assigned order end-to-end — sourcing, quotes, procurement, QC, and shipping.
          </p>
          <table cellpadding="0" cellspacing="0" align="center" style="margin:8px auto 26px;">
            <tr><td align="center"
                    style="background:linear-gradient(135deg,#6366f1 0%,#a855f7 70%,#ec4899 100%);
                           border-radius:99px;padding:0;">
              <a href="{accept_url}"
                 style="display:inline-block;padding:14px 38px;color:#fff;text-decoration:none;
                        font-size:14px;font-weight:800;letter-spacing:.04em;border-radius:99px;">
                Accept invitation &amp; set password →
              </a>
            </td></tr>
          </table>
          <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;
                      padding:16px 18px;margin:0 0 22px;">
            <p style="margin:0 0 6px;font-size:11px;font-weight:800;color:#64748b;letter-spacing:.1em;
                      text-transform:uppercase;">Direct link</p>
            <p style="margin:0;font-size:12.5px;color:#334155;word-break:break-all;
                      font-family:'SF Mono',Menlo,monospace;">
              {accept_url}
            </p>
          </div>
          <p style="margin:0 0 6px;font-size:12.5px;color:#94a3b8;font-weight:600;line-height:1.65;">
            This invitation expires <b style="color:#475569;">{invite.expires_at.strftime("%d %b %Y, %H:%M UTC")}</b>.
            If you don't recognise this email, you can safely ignore it — the link is single-use and tied to
            <code style="font-family:'SF Mono',Menlo,monospace;background:#f1f5f9;padding:1px 4px;border-radius:3px;">{invite.email}</code>.
          </p>
        </td></tr>
        <!-- Footer -->
        <tr><td style="background:#fafbff;padding:18px 40px;border-top:1px solid #eef2f7;text-align:center;">
          <p style="margin:0;font-size:11px;color:#94a3b8;font-weight:700;letter-spacing:.04em;">
            © Drop Sigma · The logistics intelligence layer
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _absolute_accept_url(invite: OpsInvitation, request=None) -> str:
    path = reverse("ops_invite_accept", args=[invite.token])
    if request is not None:
        return request.build_absolute_uri(path)
    # Off-request callers (background tasks, management commands)
    # fall back to the canonical production host. Override via
    # settings.DS_PUBLIC_HOST if a staging instance ever calls into
    # this code path.
    host = getattr(settings, "DS_PUBLIC_HOST", "dropsigma.com")
    return f"https://{host}{path}"


def send_invitation_email(invite: OpsInvitation, *, request=None) -> bool:
    """Build + dispatch the invite email. Updates the invite's
    send_count / last_sent_at on success."""
    accept_url = _absolute_accept_url(invite, request=request)
    html       = _build_invite_html(invite, accept_url)
    subject    = f"You're invited to join the {invite.workspace.name} ops desk"
    ok = bool(send_platform_email(invite.email, subject, html))
    if ok:
        invite.last_sent_at = timezone.now()
        invite.send_count   = (invite.send_count or 0) + 1
        invite.save(update_fields=["last_sent_at", "send_count"])
    return ok


# ── Accept flow ─────────────────────────────────────────────────────
def _invite_or_410(token):
    """Resolve token → invite or render 410 Gone. Acceptable terminal
    states (accepted / revoked / expired) all map to the same "this
    link is no longer usable" page so we don't tip off attackers
    about which token state they hit."""
    try:
        invite = OpsInvitation.objects.select_related(
            "workspace", "role", "invited_by",
        ).get(token=token)
    except OpsInvitation.DoesNotExist:
        return None, _render_link_dead(state="missing")
    if not invite.can_accept():
        # Mark stale invites so the superadmin UI can show "expired"
        # without needing a periodic sweep.
        if invite.status == "pending" and invite.is_expired():
            invite.status = "expired"
            invite.save(update_fields=["status"])
        return None, _render_link_dead(state=invite.status)
    return invite, None


def _render_link_dead(*, state):
    """Tiny standalone HTML page — works without any tenant context."""
    from django.http import HttpResponse
    title_map = {
        "accepted": "Invitation already accepted",
        "revoked":  "Invitation revoked",
        "expired":  "Invitation expired",
        "missing":  "Invitation not found",
    }
    title = title_map.get(state, "Invitation no longer usable")
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title} · Drop Sigma Ops</title>
<meta name="robots" content="noindex,nofollow">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#f4f6fb;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;padding:24px;}}
.card{{max-width:480px;width:100%;background:#fff;border-radius:18px;padding:36px 38px;
box-shadow:0 14px 40px rgba(15,23,42,.10);text-align:center;}}
h1{{font-size:22px;font-weight:900;color:#0f172a;margin:0 0 8px;letter-spacing:-.3px;}}
p{{font-size:14px;color:#64748b;line-height:1.6;margin:0;font-weight:500;}}
.dot{{width:60px;height:60px;border-radius:50%;margin:0 auto 22px;
background:linear-gradient(135deg,#94a3b8,#64748b);display:flex;align-items:center;
justify-content:center;font-size:30px;color:#fff;}}</style></head>
<body><div class="card"><div class="dot">⏳</div><h1>{title}</h1>
<p>If you still need access, ask whoever invited you to send a fresh link from the Drop Sigma superadmin portal.</p>
</div></body></html>"""
    return HttpResponse(body, status=410)


@require_GET
def accept_invitation_view(request, token):
    """Render the set-password form for an invite token."""
    invite, dead = _invite_or_410(token)
    if dead is not None:
        return dead
    return render(request, "sourcing_ops/invite_accept.html", {
        "invite":     invite,
        "workspace":  invite.workspace,
        "role_label": invite.role.name if invite.role_id else "",
        "token":      str(invite.token),
    })


@csrf_exempt
@require_POST
def submit_invitation_accept_api(request, token):
    """JSON endpoint the set-password form posts to.

    Body: {"name": str, "password": str}
    On success returns {"ok": true, "redirect_url": "/ops/"} and the
    invitee is already logged in via session cookie.
    """
    invite, dead = _invite_or_410(token)
    if invite is None:
        return JsonResponse({"ok": False, "error": "link_invalid"}, status=410)

    try:
        body = json.loads(request.body or b"{}")
    except Exception:
        body = {}
    name     = (body.get("name") or invite.name or "").strip()
    password = (body.get("password") or "").strip()

    if not password or len(password) < 8:
        return JsonResponse({
            "ok": False, "error": "password_too_short",
            "message": "Password must be at least 8 characters.",
        }, status=400)
    try:
        validate_password(password)
    except ValidationError as e:
        return JsonResponse({
            "ok": False, "error": "password_weak",
            "message": " ".join(e.messages),
        }, status=400)

    # Resolve / create the User. If an account already exists for the
    # invited email we attach to it rather than creating a duplicate.
    email = invite.email.strip().lower()
    user  = User.objects.filter(email__iexact=email).first()
    if user is None:
        # Use the email-local as the base username, dedupe with a
        # numeric suffix if it collides.
        base = email.split("@")[0].replace(".", "_").replace("+", "_")
        candidate, n = base, 0
        while User.objects.filter(username=candidate).exists():
            n += 1
            candidate = f"{base}{n}"
        user = User.objects.create_user(
            username=candidate,
            email=email,
            password=password,
            first_name=name.split(" ", 1)[0][:30] if name else "",
            last_name=(name.split(" ", 1)[1][:30] if " " in name else "") if name else "",
        )
    else:
        user.set_password(password)
        if name and not user.get_full_name():
            user.first_name = name.split(" ", 1)[0][:30]
            user.last_name  = name.split(" ", 1)[1][:30] if " " in name else ""
        user.save()

    # Create / update the OpsTeamMember row inside this workspace. If
    # the user was already a global ops member, we attach them to the
    # invited workspace; the previously-unscoped visibility narrows.
    profile, _ = OpsTeamMember.objects.get_or_create(
        user=user,
        defaults={
            "display_name": name or user.get_full_name() or user.username,
            "role":         invite.role,
            "title":        invite.title,
            "workspace":    invite.workspace,
            "is_manager":   invite.is_manager,
        },
    )
    # Always re-pin the workspace on accept (covers the
    # already-existed branch above).
    profile.workspace = invite.workspace
    if invite.role_id and not profile.role_id:
        profile.role = invite.role
    if invite.title and not profile.title:
        profile.title = invite.title
    if name and not profile.display_name:
        profile.display_name = name
    if invite.is_manager:
        profile.is_manager = True
    profile.save()

    # Close out the invite + remember which user accepted it.
    invite.status        = "accepted"
    invite.accepted_at   = timezone.now()
    invite.accepted_user = user
    invite.save(update_fields=["status", "accepted_at", "accepted_user"])

    # Log them in so the next page lands them straight inside the
    # workspace they were invited to.
    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    return JsonResponse({"ok": True, "redirect_url": "/ops/"})
