import uuid
import json
import os
import threading
import logging
import resend as _resend

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.conf import settings
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

_mail_logger = logging.getLogger("dropsigma.mail")


def _build_verification_email(name, link):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

  <!-- Logo -->
  <tr><td align="center" style="padding-bottom:32px;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
        <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
      </td>
      <td style="padding-left:12px;text-align:left;">
        <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Ecommerce Operations OS</div>
      </td>
    </tr></table>
  </td></tr>

  <!-- Main Card -->
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6,#a855f7);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:44px 48px;">

      <div style="display:inline-block;background:#ede9fe;color:#6d28d9;font-size:11px;font-weight:800;padding:4px 12px;border-radius:999px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:20px;">
        Email Verification
      </div>

      <h1 style="margin:0 0 14px;font-size:26px;font-weight:900;color:#0f172a;letter-spacing:-.5px;line-height:1.2;">
        Confirm your email address
      </h1>

      <p style="margin:0 0 32px;font-size:15px;color:#64748b;line-height:1.75;">
        Hi <strong style="color:#0f172a;">{name}</strong> &#x1F44B; &mdash; thanks for joining Drop Sigma!<br>
        Please verify your email to activate your account and get started.
      </p>

      <table cellpadding="0" cellspacing="0" style="margin-bottom:36px;">
        <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="{link}" style="display:inline-block;padding:16px 40px;color:#fff;font-weight:800;font-size:16px;text-decoration:none;letter-spacing:-.2px;">
            &#10003; &nbsp;Verify My Email
          </a>
        </td></tr>
      </table>

      <hr style="border:none;border-top:1px solid #f1f5f9;margin:0 0 28px;">

      <p style="margin:0 0 10px;font-size:11px;font-weight:800;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;">
        Button not working? Copy this link:
      </p>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-left:3px solid #6366f1;border-radius:8px;padding:12px 16px;margin-bottom:32px;">
        <a href="{link}" style="font-size:12px;color:#6366f1;word-break:break-all;text-decoration:none;">{link}</a>
      </div>

      <div style="background:#fefce8;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;">
        <p style="margin:0;font-size:13px;color:#92400e;line-height:1.6;">
          &#9200; <strong>This link expires in 24 hours.</strong>
          If you didn&apos;t create a Drop Sigma account, please ignore this email &mdash; no action is needed.
        </p>
      </div>

    </td></tr>
    </table>
  </td></tr>

  <!-- Support Box -->
  <tr><td style="padding:16px 0 0;">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:22px 28px;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td>
          <p style="margin:0 0 5px;font-size:13px;font-weight:700;color:#0f172a;">Need help?</p>
          <p style="margin:0;font-size:13px;color:#64748b;line-height:1.6;">
            Having trouble with your account? Our support team is here for you.<br>
            Reach us at <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:700;text-decoration:none;">support@dropsigma.com</a>
          </p>
        </td>
        <td width="50" style="text-align:right;vertical-align:middle;padding-left:16px;">
          <div style="width:44px;height:44px;background:linear-gradient(135deg,#ede9fe,#ddd6fe);border-radius:12px;text-align:center;line-height:44px;font-size:20px;">&#x1F4AC;</div>
        </td>
      </tr></table>
    </td></tr>
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding:24px 8px 0;text-align:center;">
    <p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">
      &copy; 2026 Drop Sigma &nbsp;&middot;&nbsp;
      <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a>
      &nbsp;&middot;&nbsp;
      <a href="mailto:support@dropsigma.com" style="color:#94a3b8;text-decoration:none;">support@dropsigma.com</a>
    </p>
    <p style="margin:0;font-size:11px;color:#cbd5e1;">
      You received this email because you signed up at dropsigma.com
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _build_welcome_email(name, upgrade_url):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Welcome to Drop Sigma</title>
</head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

  <!-- Logo -->
  <tr><td align="center" style="padding-bottom:32px;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
        <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
      </td>
      <td style="padding-left:12px;text-align:left;">
        <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Ecommerce Operations OS</div>
      </td>
    </tr></table>
  </td></tr>

  <!-- Card -->
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6,#a855f7);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">

    <!-- Header -->
    <tr><td align="center" style="padding:40px 48px 32px;">
      <div style="font-size:12px;font-weight:700;color:#6366f1;background:#ede9fe;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">
        &#x1F389; WELCOME ABOARD
      </div>
      <h1 style="margin:0 0 12px;font-size:26px;font-weight:800;color:#0f172a;line-height:1.3;">
        You&apos;re in, {name}!
      </h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">
        Your email is verified. Drop Sigma is ready to transform how you run your ecommerce business &mdash; fully automated, all in one place.
      </p>
    </td></tr>

    <!-- Divider -->
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>

    <!-- Steps -->
    <tr><td style="padding:32px 48px;">
      <p style="margin:0 0 20px;font-size:12px;font-weight:700;color:#94a3b8;letter-spacing:0.8px;text-transform:uppercase;">3 Steps to Full Automation</p>

      <!-- Step 1 Done -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr>
        <td width="40" valign="top">
          <div style="width:32px;height:32px;background:#dcfce7;border-radius:50%;text-align:center;line-height:32px;font-size:14px;">&#10003;</div>
        </td>
        <td valign="top" style="padding-left:12px;">
          <p style="margin:0;font-size:14px;font-weight:700;color:#16a34a;">Email Verified</p>
          <p style="margin:4px 0 0;font-size:13px;color:#94a3b8;">Your account is active and secure.</p>
        </td>
      </tr>
      </table>

      <!-- Step 2 Active -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr><td style="background:#faf5ff;border:1px solid #e9d5ff;border-radius:12px;padding:14px 16px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="40" valign="top">
            <div style="width:32px;height:32px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:50%;text-align:center;line-height:32px;font-size:13px;color:#fff;font-weight:800;">2</div>
          </td>
          <td valign="top" style="padding-left:12px;">
            <p style="margin:0;font-size:14px;font-weight:700;color:#4c1d95;">Choose Your Plan</p>
            <p style="margin:4px 0 0;font-size:13px;color:#7c3aed;">Unlock stores, order sync, vendors, team chat &amp; AI tools. Plans start at just $49/mo.</p>
          </td>
        </tr></table>
      </td></tr>
      </table>

      <!-- Step 3 Locked -->
      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="40" valign="top">
          <div style="width:32px;height:32px;background:#f1f5f9;border-radius:50%;text-align:center;line-height:32px;font-size:14px;color:#94a3b8;">3</div>
        </td>
        <td valign="top" style="padding-left:12px;">
          <p style="margin:0;font-size:14px;font-weight:700;color:#94a3b8;">Run on Autopilot</p>
          <p style="margin:4px 0 0;font-size:13px;color:#cbd5e1;">Auto sync orders, manage vendors, track stock &mdash; all automated.</p>
        </td>
      </tr>
      </table>
    </td></tr>

    <!-- Feature Grid -->
    <tr><td style="padding:0 48px 32px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">What&apos;s waiting for you inside</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F3EA; &nbsp;Multi-store management</td>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F4E6; &nbsp;Auto order sync</td>
          </tr>
          <tr>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F91D; &nbsp;Vendor portal</td>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F4CA; &nbsp;Stock tracking</td>
          </tr>
          <tr>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F4AC; &nbsp;Team chat</td>
            <td width="50%" style="padding:5px 0;font-size:13px;color:#475569;">&#x1F916; &nbsp;AI assistant</td>
          </tr>
        </table>
      </div>
    </td></tr>

    <!-- CTA -->
    <tr><td align="center" style="padding:0 48px 40px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="{upgrade_url}" style="display:inline-block;color:#ffffff;font-size:15px;font-weight:700;text-decoration:none;padding:15px 40px;">
            Choose a Plan &rarr;
          </a>
        </td>
      </tr></table>
    </td></tr>

    <!-- Support -->
    <tr><td style="padding:0 48px 40px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">
          &#x1F6DF; &nbsp;Questions before you start? We&apos;re here.<br/>
          <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a>
        </p>
      </div>
    </td></tr>

    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td align="center" style="padding:24px 8px 0;text-align:center;">
    <p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">
      &copy; 2026 Drop Sigma &nbsp;&middot;&nbsp;
      <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a>
      &nbsp;&middot;&nbsp;
      <a href="mailto:support@dropsigma.com" style="color:#94a3b8;text-decoration:none;">support@dropsigma.com</a>
    </p>
    <p style="margin:0;font-size:11px;color:#cbd5e1;">
      You received this because you signed up at dropsigma.com
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _send_welcome_email(email, name, upgrade_url):
    def _send():
        try:
            _mail_logger.info(f"Sending welcome email to {email} via Resend")
            _resend.api_key = os.getenv("RESEND_API_KEY", "")
            _resend.Emails.send({
                "from": "Drop Sigma <noreply@dropsigma.com>",
                "to": [email],
                "subject": "Welcome to Drop Sigma — you're one step away 🚀",
                "html": _build_welcome_email(name, upgrade_url),
            })
            _mail_logger.info(f"Welcome email sent OK to {email}")
        except Exception as exc:
            _mail_logger.error(f"Welcome email FAILED to {email}: {exc}")
    threading.Thread(target=_send, daemon=True).start()


def _send_verification_email(email, name, link):
    def _send():
        try:
            _mail_logger.info(f"Sending verification email to {email} via Resend")
            _resend.api_key = os.getenv("RESEND_API_KEY", "")
            _resend.Emails.send({
                "from": "Drop Sigma <noreply@dropsigma.com>",
                "to": [email],
                "subject": "Confirm your Drop Sigma email address",
                "html": _build_verification_email(name, link),
            })
            _mail_logger.info(f"Verification email sent OK to {email}")
        except Exception as exc:
            _mail_logger.error(f"Verification email FAILED to {email}: {exc}")
    threading.Thread(target=_send, daemon=True).start()


# ── One-time setup ──────────────────────────────────────────────────────────

def setup_admin(request):
    user, created = User.objects.get_or_create(username="admin")
    user.email = "admin@baghawat.com"
    user.is_staff = True
    user.is_superuser = True
    user.set_password("Admin@1234!")
    user.save()
    return JsonResponse({"success": True, "created": created, "msg": "Admin ready. Username: admin, Password: Admin@1234!"})


# ── Homepage ─────────────────────────────────────────────────────────────────

def homepage(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/dashboard/")
    if request.user.is_authenticated and request.user.team_profile.exists():
        return redirect("/employee/dashboard/")
    return render(request, "home.html")


# ── Login / Logout ────────────────────────────────────────────────────────────

def admin_login_page(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/dashboard/")

    tab   = request.GET.get("tab", "admin")
    error = request.GET.get("error", None)

    if request.method == "POST":
        identifier = request.POST.get("username", "").strip()
        password   = request.POST.get("password", "")
        user = authenticate(request, username=identifier, password=password)
        if user is None and "@" in identifier:
            # Try email lookup
            try:
                u = User.objects.get(email__iexact=identifier)
                user = authenticate(request, username=u.username, password=password)
            except User.DoesNotExist:
                pass
        if user and user.is_staff:
            login(request, user)
            return redirect(request.GET.get("next", "/dashboard/"))
        elif user and not user.is_staff:
            error = "You don't have admin access."
        else:
            error = "Invalid username or password."
        tab = "admin"

    return render(request, "admin_login.html", {"error": error, "tab": tab})


def admin_logout_view(request):
    logout(request)
    return redirect("/")


# ── User Profile API ──────────────────────────────────────────────────────────

def api_profile(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)

    from superadmin.models import UserProfile
    user    = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)

    if request.method == "GET":
        name = user.get_full_name().strip() or user.username
        return JsonResponse({
            "name":    name,
            "email":   user.email,
            "address": profile.address,
        })

    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        name    = data.get("name", "").strip()
        address = data.get("address", "").strip()

        if name:
            parts = name.split(" ", 1)
            user.first_name = parts[0]
            user.last_name  = parts[1] if len(parts) > 1 else ""
            user.save(update_fields=["first_name", "last_name"])

        profile.address = address
        profile.save(update_fields=["address"])

        return JsonResponse({"ok": True, "name": user.get_full_name().strip() or user.username})

    return JsonResponse({"error": "Method not allowed"}, status=405)


# ── Profile Page ─────────────────────────────────────────────────────────────

def profile_page(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return redirect("/login/")

    from superadmin.models import UserProfile, Tenant, Subscription
    user    = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)

    # Subscription / plan info
    tenant = sub = None
    try:
        tenant = Tenant.objects.get(user=user)
        sub    = getattr(tenant, "subscription", None)
    except Tenant.DoesNotExist:
        pass

    ctx = {
        "user":         user,
        "profile":      profile,
        "tenant":       tenant,
        "sub":          sub,
        "display_name": user.get_full_name().strip() or user.username,
        "user_initials": "".join(w[0].upper() for w in (user.get_full_name().strip() or user.username).split()[:2]),
    }
    return render(request, "profile.html", ctx)


# ── Signup ────────────────────────────────────────────────────────────────────

def signup_view(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/dashboard/")

    error = None
    if request.method == "POST":
        name     = request.POST.get("name", "").strip()
        email    = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        confirm  = request.POST.get("confirm", "")

        if not name or not email or not password:
            error = "All fields are required."
        elif password != confirm:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            from superadmin.models import EmailVerificationToken

            existing = User.objects.filter(email=email).first()
            if existing and existing.is_active:
                error = "An account with this email already exists."
            else:
                # Delete unverified user so we can re-create cleanly
                if existing and not existing.is_active:
                    existing.delete()

                # Build username from email prefix
                base = email.split("@")[0]
                username = base
                n = 1
                while User.objects.filter(username=username).exists():
                    username = f"{base}{n}"; n += 1

                # Create inactive user (until email is verified)
                user = User.objects.create_user(
                    username=username, email=email,
                    password=password, is_active=False, is_staff=True,
                )
                user.first_name = name
                user.save()

                # Verification token
                token_obj = EmailVerificationToken.objects.create(user=user)

                # Build verification URL
                host   = request.get_host()
                scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
                link   = f"{scheme}://{host}/verify-email/{token_obj.token}/"

                # Send verification email via Resend API
                _send_verification_email(email, name, link)

                return redirect(f"/signup/email-sent/?email={email}")

    return render(request, "signup.html", {"error": error})


def email_sent_view(request):
    email = request.GET.get("email", "")
    resent = request.GET.get("resent", "")
    return render(request, "email_sent.html", {"email": email, "resent": resent})


def resend_verification_email_view(request):
    email = request.GET.get("email", "").strip()
    if not email:
        return redirect("/signup/")

    from superadmin.models import EmailVerificationToken

    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        return redirect(f"/signup/email-sent/?email={email}&resent=1")

    EmailVerificationToken.objects.filter(user=user).delete()
    token_obj = EmailVerificationToken.objects.create(user=user)

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    link   = f"{scheme}://{host}/verify-email/{token_obj.token}/"
    name   = user.first_name or user.username

    _send_verification_email(email, name, link)
    return redirect(f"/signup/email-sent/?email={email}&resent=1")


# ── Email Verification ────────────────────────────────────────────────────────

def verify_email_view(request, token):
    from superadmin.models import EmailVerificationToken, Tenant, Subscription, TenantActivity, PLAN_PRICES
    import datetime

    try:
        token_obj = EmailVerificationToken.objects.select_related("user").get(token=token)
    except EmailVerificationToken.DoesNotExist:
        return render(request, "verify_email.html", {"status": "invalid"})

    if token_obj.is_used:
        return render(request, "verify_email.html", {"status": "already_used"})

    if token_obj.is_expired():
        return render(request, "verify_email.html", {"status": "expired"})

    user = token_obj.user
    user.is_active = True
    user.save()
    token_obj.is_used = True
    token_obj.save()

    # Create Tenant + trial Subscription if not already
    if not hasattr(user, "tenant_profile"):
        tenant = Tenant.objects.create(
            user=user,
            name=user.first_name or user.username,
            plan="trial",
            status="trial",
            trial_ends=datetime.date.today() + datetime.timedelta(days=14),
        )
        Subscription.objects.create(
            tenant=tenant,
            plan="trial",
            price=0,
            start_date=datetime.date.today(),
            renews_on=datetime.date.today() + datetime.timedelta(days=14),
            payment_status="paid",
        )
        TenantActivity.objects.create(
            tenant=tenant,
            action="Account verified and created via signup",
            action_type="signup",
        )

        # Send welcome email after first-time verification
        host = request.get_host()
        scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
        upgrade_url = f"{scheme}://{host}/upgrade/"
        name = user.first_name or user.username
        _send_welcome_email(user.email, name, upgrade_url)

    return render(request, "verify_email.html", {"status": "success", "email": user.email})


# ── Dashboard ─────────────────────────────────────────────────────────────────

def _is_subscribed(user):
    if user.is_superuser:
        return True
    try:
        tenant = user.tenant_profile
        sub    = tenant.subscription
        return sub.payment_status == "paid" and tenant.status == "active"
    except Exception:
        return False


@login_required(login_url="/login/")
def dashboard_page(request):
    if not request.user.is_staff:
        if request.user.team_profile.exists():
            return redirect("/employee/dashboard/")
        return redirect("/login/?tab=team")

    imp_id    = request.session.get("impersonate_id")
    imp_name  = request.session.get("impersonate_name", "")
    imp_email = request.session.get("impersonate_email", "")

    subscribed = _is_subscribed(request.user)

    real_user   = request.user
    display_name = real_user.get_full_name().strip() or real_user.username
    initials     = "".join(w[0].upper() for w in display_name.split()[:2]) or "U"

    is_suspended = False
    is_flagged   = False
    try:
        from superadmin.models import Tenant
        tenant = real_user.tenant_profile
        is_suspended = tenant.status == "suspended"
        is_flagged   = tenant.flagged and not is_suspended
    except Exception:
        pass

    response = render(request, "dashboard.html", {
        "is_impersonating":  bool(imp_id),
        "impersonate_name":  imp_name,
        "impersonate_email": imp_email,
        "is_subscribed":     subscribed,
        "is_suspended":      is_suspended,
        "is_flagged":        is_flagged,
        "display_name":      display_name,
        "user_initials":     initials,
    })
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


# ── Upgrade / Subscribe ────────────────────────────────────────────────────────

PLANS = [
    {
        "key": "pro", "name": "Drop Sigma Pro", "price": 99,
        "desc": "Everything you need to run a full dropshipping operation",
        "popular": True,
        "features": [
            "3 Stores (WooCommerce or Shopify)",
            "2 Email Accounts (Gmail Integration)",
            "Unlimited Vendors + Vendor Portal",
            "Unlimited Orders & Order Management",
            "Permanent Product-Vendor Assignment",
            "Unlimited Team Members + Role-Based Permissions",
            "Live Tracking Sync (Auto Status Update)",
            "Tracking Approval Queue",
            "Unlimited Email Templates",
            "Auto Email on Status Change",
            "AI Email Drafts (GPT + Claude powered)",
            "Revenue & KPI Dashboard",
            "Store Health Monitor",
            "Bulk Order Actions",
            "Stock Management",
            "Team Chat",
            "Priority Support",
        ],
        "not_included": [],
    },
]


@login_required(login_url="/login/")
def upgrade_view(request):
    if not request.user.is_staff:
        return redirect("/login/")
    if _is_subscribed(request.user):
        return redirect("/dashboard/")
    return render(request, "upgrade.html", {
        "plans":            PLANS,
        "paypal_client_id": settings.PAYPAL_CLIENT_ID,
    })


@login_required(login_url="/login/")
def checkout_view(request):
    if not request.user.is_staff:
        return redirect("/login/")
    if _is_subscribed(request.user):
        return redirect("/dashboard/")

    plan_key = request.GET.get("plan", "starter")
    plan     = next((p for p in PLANS if p["key"] == plan_key), PLANS[0])

    stripe_ok  = bool(settings.STRIPE_SECRET_KEY and not settings.STRIPE_SECRET_KEY.startswith("sk_test_your"))
    paypal_ok  = bool(settings.PAYPAL_CLIENT_ID  and not settings.PAYPAL_CLIENT_ID.startswith("your_paypal"))

    return render(request, "checkout.html", {
        "plan":             plan,
        "stripe_ok":        stripe_ok,
        "paypal_ok":        paypal_ok,
        "paypal_client_id": settings.PAYPAL_CLIENT_ID if paypal_ok else "",
        "user":             request.user,
    })


@login_required(login_url="/login/")
@require_POST
def checkout_free(request):
    """Activate subscription when coupon brings price to $0."""
    plan_key    = request.POST.get("plan", "starter")
    coupon_code = request.POST.get("coupon_code", "")
    price, label = _apply_coupon(plan_key, coupon_code)
    if float(price) > 0:
        return redirect(f"/checkout/?plan={plan_key}&error=coupon_not_zero")
    _activate_subscription(request.user, plan_key, 0, f"Coupon{label}")
    return redirect("/dashboard/")


@login_required(login_url="/login/")
@require_POST
def subscribe_view(request):
    """Disabled — use Stripe or PayPal checkout instead."""
    return redirect("/upgrade/")

    plan_key = request.POST.get("plan", "starter")
    valid_keys = {p["key"] for p in PLANS}
    if plan_key not in valid_keys:
        plan_key = "starter"

    plan_data = next(p for p in PLANS if p["key"] == plan_key)
    price     = plan_data["price"]

    # Apply coupon if provided
    from superadmin.models import Tenant, Subscription, TenantActivity, PLAN_PRICES, Coupon
    import datetime

    coupon_code = request.POST.get("coupon_code", "").strip().upper()
    coupon_label = ""
    if coupon_code:
        try:
            coupon = Coupon.objects.get(code=coupon_code)
            valid, _ = coupon.is_valid()
            if valid:
                price = coupon.apply(price)
                coupon.uses += 1
                coupon.save(update_fields=["uses"])
                coupon_label = f" (coupon: {coupon_code})"
        except Coupon.DoesNotExist:
            pass

    # Map our plan keys to superadmin model plan choices
    plan_map = {"starter": "basic", "growth": "pro", "scale": "enterprise"}
    tenant_plan = plan_map.get(plan_key, "basic")

    user = request.user

    # Get or create Tenant
    try:
        tenant = user.tenant_profile
    except Exception:
        tenant = Tenant.objects.create(
            user=user,
            name=user.first_name or user.username,
            plan=tenant_plan,
            status="active",
        )

    tenant.plan   = tenant_plan
    tenant.status = "active"
    tenant.save()

    # Get or create Subscription
    try:
        sub = tenant.subscription
    except Exception:
        sub = Subscription(tenant=tenant)

    sub.plan           = tenant_plan
    sub.price          = price
    sub.payment_status = "paid"
    sub.start_date     = datetime.date.today()
    sub.renews_on      = datetime.date.today() + datetime.timedelta(days=30)
    sub.save()

    TenantActivity.objects.create(
        tenant=tenant,
        action=f"Subscribed to {plan_data['name']} plan (${price}/mo){coupon_label}",
        action_type="plan",
    )

    return redirect("/dashboard/")


# ── Payment helpers ───────────────────────────────────────────────────────────

def _activate_subscription(user, plan_key, price, note=""):
    from superadmin.models import Tenant, Subscription, TenantActivity
    import datetime
    plan_map   = {"starter": "basic", "growth": "pro", "scale": "enterprise"}
    tenant_plan = plan_map.get(plan_key, "basic")
    try:
        tenant = user.tenant_profile
    except Exception:
        tenant = Tenant.objects.create(
            user=user, name=user.first_name or user.username,
            plan=tenant_plan, status="active",
        )
    tenant.plan   = tenant_plan
    tenant.status = "active"
    tenant.save()
    try:
        sub = tenant.subscription
    except Exception:
        sub = Subscription(tenant=tenant)
    sub.plan           = tenant_plan
    sub.price          = price
    sub.payment_status = "paid"
    sub.start_date     = datetime.date.today()
    sub.renews_on      = datetime.date.today() + datetime.timedelta(days=30)
    sub.save()
    TenantActivity.objects.create(
        tenant=tenant,
        action=f"Subscribed to {plan_key} plan (${price}/mo) via {note}",
        action_type="plan",
    )


def _apply_coupon(plan_key, coupon_code):
    from superadmin.models import Coupon
    PRICES = {"starter": 49, "growth": 99, "scale": 149}
    price  = PRICES.get(plan_key, 49)
    coupon_label = ""
    if coupon_code:
        try:
            c = Coupon.objects.get(code=coupon_code.strip().upper())
            valid, _ = c.is_valid()
            if valid:
                price = c.apply(price)
                c.uses += 1
                c.save(update_fields=["uses"])
                coupon_label = f" coupon:{c.code}"
        except Coupon.DoesNotExist:
            pass
    return price, coupon_label


# ── Stripe ────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def stripe_create_session(request):
    import stripe

    if not settings.STRIPE_SECRET_KEY or settings.STRIPE_SECRET_KEY.startswith("sk_test_your"):
        return redirect("/upgrade/?error=stripe_not_configured")

    stripe.api_key = settings.STRIPE_SECRET_KEY

    plan_key    = request.POST.get("plan", "starter")
    coupon_code = request.POST.get("coupon_code", "")
    price, _    = _apply_coupon(plan_key, coupon_code)

    PLAN_NAMES = {"starter": "Starter", "growth": "Growth", "scale": "Scale"}

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    base   = f"{scheme}://{host}"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency":     "usd",
                    "unit_amount":  int(float(price) * 100),
                    "product_data": {"name": f"Drop Sigma {PLAN_NAMES.get(plan_key, plan_key)} Plan"},
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{base}/payment/stripe/success/?session_id={{CHECKOUT_SESSION_ID}}&plan={plan_key}&coupon={coupon_code}",
            cancel_url=f"{base}/upgrade/",
            customer_email=request.user.email,
        )
        return redirect(session.url)
    except stripe.error.AuthenticationError:
        return redirect("/upgrade/?error=stripe_auth")
    except Exception:
        return redirect("/upgrade/?error=stripe_error")


@login_required(login_url="/login/")
def stripe_success(request):
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY

    session_id  = request.GET.get("session_id", "")
    plan_key    = request.GET.get("plan", "starter")
    coupon_code = request.GET.get("coupon", "")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return redirect("/upgrade/?error=payment_incomplete")
    except Exception:
        return redirect("/upgrade/?error=session_invalid")

    price, label = _apply_coupon(plan_key, coupon_code)
    _activate_subscription(request.user, plan_key, price, f"Stripe{label}")
    return redirect("/dashboard/")


@csrf_exempt
def stripe_webhook(request):
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    payload    = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except Exception:
        return JsonResponse({"error": "Invalid signature"}, status=400)
    # Handle completed sessions if needed (for reliability)
    return JsonResponse({"received": True})


# ── PayPal ────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def paypal_create_order(request):
    import requests as _req
    data        = json.loads(request.body)
    plan_key    = data.get("plan", "starter")
    coupon_code = data.get("coupon_code", "")
    price, _    = _apply_coupon(plan_key, coupon_code)

    # Get PayPal access token
    mode     = settings.PAYPAL_MODE
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    if token_res.status_code != 200:
        return JsonResponse({"error": "PayPal auth failed"}, status=500)

    access_token = token_res.json()["access_token"]

    order_res = _req.post(
        f"{base_url}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": f"{float(price):.2f}"},
                "description": f"Drop Sigma {plan_key.title()} Plan",
            }],
        },
        timeout=15,
    )
    order = order_res.json()
    return JsonResponse({"id": order.get("id"), "plan": plan_key, "coupon": coupon_code})


@login_required(login_url="/login/")
@require_POST
def paypal_capture_order(request):
    import requests as _req
    data        = json.loads(request.body)
    order_id    = data.get("order_id")
    plan_key    = data.get("plan", "starter")
    coupon_code = data.get("coupon_code", "")

    mode     = settings.PAYPAL_MODE
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    access_token = token_res.json()["access_token"]

    cap_res = _req.post(
        f"{base_url}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        timeout=15,
    )
    cap = cap_res.json()
    if cap.get("status") != "COMPLETED":
        return JsonResponse({"error": "Payment not completed"}, status=400)

    price, label = _apply_coupon(plan_key, coupon_code)
    _activate_subscription(request.user, plan_key, price, f"PayPal{label}")
    return JsonResponse({"ok": True})
