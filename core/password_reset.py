"""Unified platform password reset.

Works for tenants (admin login), team members, and vendors — anyone with a
row in `auth_user`. The vendor flow that used to only log a request is
replaced by this end-to-end flow (link arrives in inbox, click → set new
password).

All emails go out from noreply@dropsigma.com via the platform sender
(Resend → Django SMTP fallback). NEVER uses a tenant's connected Gmail.

Token: Django's stateless PasswordResetTokenGenerator (no DB needed).
Valid for ~3 days. Self-invalidates when the password changes or
last_login is bumped.
"""
import logging
import os

from django.contrib.auth import login as auth_login
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_http_methods

logger = logging.getLogger(__name__)


# ─── Platform email sender ───────────────────────────────────────────────────
def send_platform_email(to_email, subject, html):
    """Send a system email from noreply@dropsigma.com.

    Tries Resend (RESEND_API_KEY) first, then falls back to Django SMTP via
    EMAIL_HOST_USER. Returns (sent: bool, error_message: str).
    """
    from django.conf import settings

    errors = []

    # Path 1: Resend
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
            logger.info("Platform email sent via Resend to %s (id=%s)",
                        to_email, getattr(result, "id", result))
            return True, ""
        except Exception as exc:
            errors.append(f"Resend: {exc}")
            logger.warning("Resend send failed for %s: %s", to_email, exc)

    # Path 2: Django SMTP fallback
    if settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD:
        try:
            from django.core.mail import EmailMultiAlternatives
            from_addr = settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER
            msg = EmailMultiAlternatives(
                subject=subject,
                body="Open in an HTML-capable email client to view this message.",
                from_email=f"Drop Sigma <{from_addr}>",
                to=[to_email],
            )
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=False)
            logger.info("Platform email sent via SMTP to %s from %s",
                        to_email, from_addr)
            return True, ""
        except Exception as exc:
            errors.append(f"SMTP: {exc}")
            logger.warning("SMTP send failed for %s: %s", to_email, exc)

    if not resend_key and not (settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD):
        return False, ("Server email is not configured. Set RESEND_API_KEY or "
                       "EMAIL_HOST_USER + EMAIL_HOST_PASSWORD on the server.")
    return False, "Email send failed: " + " | ".join(errors)


# ─── Reset-link email body ───────────────────────────────────────────────────
def _build_reset_email(name, reset_url):
    safe_name = (name or "there").strip() or "there"
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
        <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
      </td>
      <td style="padding-left:12px;text-align:left;">
        <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Password Reset</div>
      </td>
    </tr></table>
  </td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#3730a3;background:#eef2ff;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">🔐 RESET PASSWORD</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Reset your Drop Sigma password</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{safe_name}</strong>, we got a request to reset your password. Click the button below to choose a new one. If you didn't ask for this, you can ignore this email — your password stays unchanged.</p>
    </td></tr>
    <tr><td align="center" style="padding:8px 48px 32px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="{reset_url}" style="display:block;padding:16px 36px;font-size:15px;font-weight:800;color:#ffffff;text-decoration:none;letter-spacing:.2px;">Choose a new password</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 8px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Or copy this link</p>
        <p style="margin:0;font-size:12px;color:#6366f1;word-break:break-all;">{reset_url}</p>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 36px;">
      <p style="margin:0;font-size:12px;color:#94a3b8;">This link expires in <strong>3 days</strong>.</p>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding-top:28px;">
    <p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">© 2026 Drop Sigma &middot; <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a></p>
    <p style="margin:0;font-size:11px;color:#cbd5e1;">Help: <a href="mailto:support@dropsigma.com" style="color:#cbd5e1;">support@dropsigma.com</a></p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


# ─── Endpoints ───────────────────────────────────────────────────────────────
@require_POST
def forgot_password_api(request):
    """POST {email}. Sends a reset link from noreply@dropsigma.com.
    Always returns success — never leaks whether the email exists."""
    try:
        import json
        try:
            data = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            data = {}
        email = (data.get("email") or request.POST.get("email") or "").strip().lower()
    except Exception:
        email = ""

    if not email or "@" not in email:
        return JsonResponse({"success": False, "message": "Please enter a valid email."}, status=400)

    # Look up by email — case-insensitive. Excludes inactive (sanitized
    # orphans) so they can't be hijacked.
    user = (User.objects
            .filter(email__iexact=email, is_active=True)
            .order_by("-last_login", "-id")
            .first())

    if user:
        try:
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            scheme = "https" if request.is_secure() else request.scheme
            host = request.get_host()
            reset_url = f"{scheme}://{host}/reset-password/{uidb64}/{token}/"
            display_name = (user.first_name or user.username or "").strip() or "there"
            html = _build_reset_email(display_name, reset_url)
            sent, err = send_platform_email(email, "Reset your Drop Sigma password", html)
            if not sent:
                logger.warning("forgot_password: send failed for %s: %s", email, err)
        except Exception as exc:
            logger.exception("forgot_password unexpected error for %s: %s", email, exc)

    # Always pretend success — anti-enumeration
    return JsonResponse({
        "success": True,
        "message": "If an account exists for that email, a reset link has been sent. Check your inbox (and spam).",
    })


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def reset_password_page(request, uidb64, token):
    """GET → form. POST → validate + set new password + redirect to login.
    The token is single-use: once the password changes, the same token is
    invalidated (PasswordResetTokenGenerator hashes the password into the
    token, so a new password produces a new hash → old token rejected)."""
    bad_ctx = {
        "error": "This reset link is invalid or has expired. Please request a new one.",
        "invalid": True,
    }
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid, is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return render(request, "reset_password.html", bad_ctx, status=400)

    if not default_token_generator.check_token(user, token):
        return render(request, "reset_password.html", bad_ctx, status=400)

    if request.method == "GET":
        return render(request, "reset_password.html", {
            "email": user.email, "invalid": False, "error": None,
        })

    # POST
    pw1 = (request.POST.get("password1") or "").strip()
    pw2 = (request.POST.get("password2") or "").strip()
    if not pw1 or not pw2:
        return render(request, "reset_password.html", {
            "email": user.email, "invalid": False,
            "error": "Please fill in both password fields.",
        })
    if pw1 != pw2:
        return render(request, "reset_password.html", {
            "email": user.email, "invalid": False,
            "error": "The two passwords don't match.",
        })
    if len(pw1) < 8:
        return render(request, "reset_password.html", {
            "email": user.email, "invalid": False,
            "error": "Password must be at least 8 characters.",
        })

    user.set_password(pw1)
    user.save(update_fields=["password"])
    # Auto-login the user for convenience
    try:
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    except Exception:
        pass

    return HttpResponseRedirect("/dashboard/")
