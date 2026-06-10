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
from django.utils import timezone

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
    user.email = "admin@dropsigma.com"
    user.is_staff = True
    user.is_superuser = True
    user.set_password("Admin@1234!")
    user.save()
    return JsonResponse({"success": True, "created": created, "msg": "Admin ready. Username: admin, Password: Admin@1234!"})


# ── Legal / public pages (App Store compliance) ─────────────────────────────
def privacy_policy_page(request):
    """Public privacy policy — required by Shopify App Store review."""
    response = render(request, "legal/privacy.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def terms_of_service_page(request):
    """Public terms of service."""
    response = render(request, "legal/terms.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def support_page(request):
    """Public support / contact page — required by Shopify App Store review."""
    response = render(request, "legal/support.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def about_page(request):
    """Public About page."""
    response = render(request, "legal/about.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def pricing_page(request):
    """Public Pricing page."""
    response = render(request, "legal/pricing.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def contact_page(request):
    """Public Contact page with form."""
    response = render(request, "legal/contact.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


@csrf_exempt
@require_POST
def contact_submit(request):
    """Handle contact-form POST. Logs the inquiry; never emails unauthenticated form to third parties."""
    import json
    from django.core.mail import mail_admins
    try:
        name = (request.POST.get("name") or "").strip()[:120]
        email = (request.POST.get("email") or "").strip()[:200]
        topic = (request.POST.get("topic") or "").strip()[:60]
        message = (request.POST.get("message") or "").strip()[:5000]
        if not (name and email and message):
            return JsonResponse({"ok": False, "error": "Please fill in name, email, and message."}, status=400)

        # Best-effort log to admins; never blocks the response if it fails.
        try:
            subject = f"[Drop Sigma contact] {topic or 'general'} — {name}"
            body = f"From: {name} <{email}>\nTopic: {topic}\n\n{message}\n"
            mail_admins(subject, body, fail_silently=True)
        except Exception:
            pass

        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": "Could not send. Please email support@dropsigma.com directly."}, status=500)


def cookies_page(request):
    """Public Cookie Policy page (GDPR compliance)."""
    response = render(request, "legal/cookies.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def refund_page(request):
    """Public Refund Policy page."""
    response = render(request, "legal/refund.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def docs_page(request):
    """Public Documentation hub."""
    response = render(request, "legal/docs.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


def features_page(request):
    """Public Features page."""
    response = render(request, "legal/features.html")
    response["Cache-Control"] = "public, max-age=60"
    return response


# ── Homepage ─────────────────────────────────────────────────────────────────

def homepage(request):
    # Preserve any query string the caller attached (e.g. ?store_id=…&section=…
    # from the project switcher). Without this, the switcher's selection
    # silently gets stripped on the / → /dashboard/ hop and the page reloads
    # with the previously-selected store.
    qs = request.META.get("QUERY_STRING", "")
    suffix = ("?" + qs) if qs else ""

    if request.user.is_authenticated and request.user.is_staff:
        return redirect(f"/dashboard/{suffix}")
    if request.user.is_authenticated and request.user.team_profile.exists():
        return redirect(f"/employee/dashboard/{suffix}")
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
            # Force fresh logins to land on Overview (?section=overview overrides
            # any localStorage-saved section from a previous session). Respect
            # ?next= only if it's an explicit deep link.
            next_url = request.GET.get("next")
            if next_url and next_url != "/dashboard/":
                return redirect(next_url)
            return redirect("/dashboard/?section=overview&login=1")
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
    """
    Authoritative access gate. A tenant is "subscribed" only when:
      - Tenant status is active
      - Subscription is paid AND status in {active, trialing}
      - For recurring subscriptions, current_period_end has not passed

    Hard cancels (customer.subscription.deleted) flip tenant.status to
    "suspended" via webhook, but this layer is a defense-in-depth check
    that survives missed webhooks.
    """
    if user.is_superuser:
        return True
    try:
        tenant = user.tenant_profile
        sub    = tenant.subscription
    except Exception:
        return False

    if tenant.status != "active":
        return False
    if sub.payment_status != "paid":
        return False
    # status field added in 0010 migration — older rows default to "active"
    if sub.status and sub.status not in ("active", "trialing"):
        return False
    # Recurring subscriptions must not have expired
    if sub.current_period_end:
        # Allow a 1-day grace so Stripe webhook + clock skew don't lock out
        # tenants whose invoice.paid hasn't arrived yet.
        from datetime import timedelta
        if sub.current_period_end + timedelta(days=1) < timezone.now():
            return False
    return True


@login_required(login_url="/login/")
def dashboard_page(request):
    # ── Shopify install entry-point ──────────────────────────────────────
    # The Shopify Partner Dashboard's "App URL" is set to /dashboard/, so
    # when a merchant clicks "Install" (from the App Store, Dev Dashboard,
    # or the "Welcome back · Drop Sigma Test" store-picker) Shopify
    # redirects the browser HERE with a `?shop=xxx.myshopify.com` query
    # param. Without this branch the shop param gets silently dropped
    # and the install never starts — exactly the bug the user hit when
    # the Shopify store didn't appear after clicking Install.
    #
    # If a logged-in Drop Sigma user lands here with a `shop=` param, kick
    # off the standard OAuth flow (same code path that powers the
    # "+ Add Store > Shopify > One-Click Connect" button).
    shop_param = (request.GET.get("shop") or "").strip().lower()
    if shop_param and shop_param.endswith(".myshopify.com"):
        if request.user.is_authenticated:
            try:
                from stores.views import _shopify_oauth_build_auth_url
                # Use the shop's subdomain as the display name by default;
                # the user can rename later in the Stores UI.
                display_name = shop_param.split(".")[0].replace("-", " ").title()
                resp = _shopify_oauth_build_auth_url(request, name=display_name, store_url=shop_param)
                # _shopify_oauth_build_auth_url returns a DRF Response. Extract
                # the auth_url and redirect the browser to Shopify so the user
                # can approve scopes.
                data = getattr(resp, "data", None) or {}
                auth_url = data.get("auth_url")
                if auth_url:
                    return redirect(auth_url)
            except Exception:
                import logging as _l
                _l.getLogger(__name__).exception(
                    "Shopify install entry-point failed for shop=%s", shop_param,
                )
                # Fall through to the normal dashboard render so the user
                # at least sees the app instead of a blank error page.
        else:
            # Not logged in but Shopify just sent the merchant here to install.
            # Bounce to login while preserving the full ?shop= query so the
            # post-login redirect resumes the install — instead of dropping
            # the param and leaving the merchant stranded on the dashboard.
            from urllib.parse import urlencode as _uenc
            next_qs = _uenc({"shop": shop_param})
            login_qs = _uenc({"next": f"/dashboard/?{next_qs}", "tab": "admin"})
            return redirect(f"/login/?{login_qs}")

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

    # Wallet top-up uses the PayPal JS SDK, which needs the platform client id
    # at render time. Resolve it the same way the checkout page does.
    try:
        _wallet_paypal_cid = _platform_creds()["paypal_client_id"] or ""
    except Exception:
        _wallet_paypal_cid = ""

    response = render(request, "dashboard.html", {
        "is_impersonating":  bool(imp_id),
        "impersonate_name":  imp_name,
        "impersonate_email": imp_email,
        "is_subscribed":     subscribed,
        "is_suspended":      is_suspended,
        "is_flagged":        is_flagged,
        "display_name":      display_name,
        "user_initials":     initials,
        "wallet_paypal_client_id": _wallet_paypal_cid,
    })
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@login_required(login_url="/login/")
def dashboard_embed_emails(request):
    """Render dashboard.html in 'embedded emails-only' mode for the
    employee portal iframe. Allowed for tenant owners AND team members
    — the email APIs themselves do all scope filtering, so this view
    just has to render the chrome without redirecting employees away.

    For team members, we resolve the OWNER's first accessible store
    and pass it down via context. The template falls back to that
    store_id when the URL query param is missing (which it is when
    the employee's iframe loads without an explicit selection)."""
    real_user = request.user
    display_name = real_user.get_full_name().strip() or real_user.username
    initials = "".join(w[0].upper() for w in display_name.split()[:2]) or "U"

    # Resolve a sensible default store_id for the requester so the
    # embedded dashboard's API calls don't all default to "2".
    default_store_id = ""
    has_any_assignment = True   # default: assume access until proven otherwise
    try:
        from stores.models import Store as _Store
        if hasattr(real_user, "team_profile"):
            member = real_user.team_profile.filter(is_active=True).select_related("owner").first()
            if member and member.owner_id:
                perms = member.permissions or {}
                allowed = perms.get("allowed_stores") or []
                qs = _Store.objects.filter(user_id=member.owner_id)
                if allowed:
                    qs = qs.filter(id__in=[int(s) for s in allowed if str(s).isdigit()])
                first = qs.order_by("id").first()
                if first:
                    default_store_id = str(first.id)
                # Pre-resolve whether this employee has ANY assignment so
                # we can avoid the empty-state flash on initial render.
                from emails.models import FolderAssignment, EmailThreadAssignment
                from django.db.models import Q as _Q
                has_view_all   = bool(perms.get("view_all_threads"))
                has_fa         = FolderAssignment.objects.filter(
                                    assigned_to=member, is_active=True, store__user=member.owner
                                 ).exists()
                has_individual = EmailThreadAssignment.objects.filter(
                                    store__user=member.owner
                                 ).filter(_Q(assigned_to=member) | _Q(co_assignees=member)).exists()
                has_any_assignment = bool(has_view_all or has_fa or has_individual)
        if not default_store_id:
            first_own = _Store.objects.filter(user=real_user).order_by("id").first()
            if first_own:
                default_store_id = str(first_own.id)
    except Exception:
        pass

    response = render(request, "dashboard.html", {
        "embed_mode":        "emails",
        "embed_default_store_id": default_store_id,
        "embed_has_assignments":  has_any_assignment,
        "is_impersonating":  False,
        "impersonate_name":  "",
        "impersonate_email": "",
        "is_subscribed":     True,
        "is_suspended":      False,
        "is_flagged":        False,
        "display_name":      display_name,
        "user_initials":     initials,
    })
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["X-Frame-Options"] = "SAMEORIGIN"
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
    creds = _platform_creds()
    return render(request, "upgrade.html", {
        "plans":            PLANS,
        "paypal_client_id": creds["paypal_client_id"],
    })


@login_required(login_url="/login/")
def checkout_view(request):
    if not request.user.is_staff:
        return redirect("/login/")
    if _is_subscribed(request.user):
        return redirect("/dashboard/")

    plan_key = request.GET.get("plan", "starter")
    plan     = next((p for p in PLANS if p["key"] == plan_key), PLANS[0])

    # Resolve from DB-saved superadmin keys first, env-var fallback.
    # A button is rendered ONLY if real credentials exist (not the placeholder
    # "sk_test_your..." / "your_paypal..." defaults from settings.py).
    creds = _platform_creds()
    stripe_secret = creds["stripe_secret"]
    paypal_cid    = creds["paypal_client_id"]

    stripe_ok = bool(stripe_secret and not stripe_secret.startswith("sk_test_your"))
    paypal_ok = bool(paypal_cid    and not paypal_cid.startswith("your_paypal"))

    return render(request, "checkout.html", {
        "plan":             plan,
        "stripe_ok":        stripe_ok,
        "paypal_ok":        paypal_ok,
        "paypal_client_id": paypal_cid if paypal_ok else "",
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

def _activate_subscription(user, plan_key, price, note="", *,
                            provider="none",
                            stripe_customer_id="",
                            stripe_subscription_id="",
                            stripe_price_id="",
                            current_period_end=None,
                            paypal_subscription_id="",
                            paypal_plan_id=""):
    """
    Activate or refresh a tenant subscription record.

    For Stripe recurring (mode=subscription), pass the IDs received from the
    Checkout Session / webhook so we can later open the Customer Portal,
    enforce period_end, and reconcile renewals.
    """
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
    sub.status         = "active"
    sub.start_date     = datetime.date.today()
    sub.renews_on      = datetime.date.today() + datetime.timedelta(days=30)

    if provider:
        sub.provider = provider
    # Stripe IDs — only overwrite when provided (don't clobber existing values)
    if stripe_customer_id:
        sub.stripe_customer_id = stripe_customer_id
    if stripe_subscription_id:
        sub.stripe_subscription_id = stripe_subscription_id
    if stripe_price_id:
        sub.stripe_price_id = stripe_price_id
    if current_period_end:
        sub.current_period_end = current_period_end
        # also keep renews_on in sync as a date
        try:
            sub.renews_on = current_period_end.date()
        except Exception:
            pass
    # PayPal IDs (used by Phase 2 — PayPal subscriptions)
    if paypal_subscription_id:
        sub.paypal_subscription_id = paypal_subscription_id
    if paypal_plan_id:
        sub.paypal_plan_id = paypal_plan_id

    # Returning customer? Clear any prior cancellation flags.
    sub.cancel_at_period_end = False
    sub.canceled_at          = None

    sub.save()
    TenantActivity.objects.create(
        tenant=tenant,
        action=f"Subscribed to {plan_key} plan (${price}/mo) via {note}",
        action_type="plan",
    )
    return sub


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

def _platform_creds():
    """
    Live credential resolver. Reads superadmin-saved keys from the
    PlatformPaymentSettings singleton when set, otherwise falls back to
    env-var values from settings.py — keeps old behaviour intact while
    letting the superadmin override per provider from the UI.
    """
    try:
        from superadmin.models import PlatformPaymentSettings
        row = PlatformPaymentSettings.load()
    except Exception:
        row = None

    def pick(db_val, env_val):
        return db_val if (row and db_val) else env_val

    return {
        # Stripe — only consider DB values if Stripe is enabled in the panel
        "stripe_secret":       pick(row.stripe_secret_key if row and row.stripe_enabled else "", settings.STRIPE_SECRET_KEY),
        "stripe_publishable":  pick(row.stripe_publishable_key if row and row.stripe_enabled else "", settings.STRIPE_PUBLISHABLE_KEY),
        "stripe_webhook":      pick(row.stripe_webhook_secret if row and row.stripe_enabled else "", settings.STRIPE_WEBHOOK_SECRET),
        # PayPal — same gating on paypal_enabled
        "paypal_client_id":    pick(row.paypal_client_id if row and row.paypal_enabled else "", settings.PAYPAL_CLIENT_ID),
        "paypal_client_secret":pick(row.paypal_client_secret if row and row.paypal_enabled else "", settings.PAYPAL_CLIENT_SECRET),
        "paypal_mode":         (row.paypal_mode if row and row.paypal_enabled and row.paypal_client_id else settings.PAYPAL_MODE),
    }


PLAN_NAMES = {"starter": "Starter", "growth": "Growth", "scale": "Scale"}


def _stripe_get_or_create_price(plan_key, amount_usd):
    """
    Idempotently locate (or create) a recurring monthly Stripe Price for the
    given plan key + amount. Stripe doesn't allow editing a Price's amount, so
    each (plan, amount) pair becomes its own Price object — that handles both
    the base prices and any coupon-discounted variants.

    Returns the price id (e.g. "price_xxx") or raises.
    """
    import stripe
    plan_label = PLAN_NAMES.get(plan_key, plan_key.title())
    product_lookup = f"dropsigma_plan_{plan_key}"
    unit_amount = int(round(float(amount_usd) * 100))

    # 1) Find existing Product for this plan (by metadata.lookup_key)
    products = stripe.Product.list(limit=100, active=True).data
    product = next((p for p in products if (p.metadata or {}).get("lookup_key") == product_lookup), None)
    if not product:
        product = stripe.Product.create(
            name=f"Drop Sigma {plan_label} Plan",
            metadata={"lookup_key": product_lookup, "plan_key": plan_key},
        )

    # 2) Find a recurring monthly Price on this Product for this amount
    prices = stripe.Price.list(product=product.id, active=True, limit=100).data
    for pr in prices:
        rec = pr.get("recurring") or {}
        if (
            pr.unit_amount == unit_amount
            and pr.currency == "usd"
            and rec.get("interval") == "month"
            and rec.get("interval_count") == 1
        ):
            return pr.id

    # 3) Create it
    new_price = stripe.Price.create(
        product=product.id,
        currency="usd",
        unit_amount=unit_amount,
        recurring={"interval": "month", "interval_count": 1},
        metadata={"plan_key": plan_key},
    )
    return new_price.id


def _stripe_customer_for_user(user):
    """Find or create a Stripe Customer for this Django user."""
    import stripe
    from superadmin.models import Tenant, Subscription

    try:
        sub = user.tenant_profile.subscription
        if sub.stripe_customer_id:
            try:
                cust = stripe.Customer.retrieve(sub.stripe_customer_id)
                if not getattr(cust, "deleted", False):
                    return cust.id
            except Exception:
                pass
    except Exception:
        pass

    # Search by email
    if user.email:
        try:
            existing = stripe.Customer.list(email=user.email, limit=1).data
            if existing:
                return existing[0].id
        except Exception:
            pass

    cust = stripe.Customer.create(
        email=user.email or None,
        name=user.get_full_name() or user.username,
        metadata={"django_user_id": user.id},
    )
    return cust.id


@login_required(login_url="/login/")
@require_POST
def stripe_create_session(request):
    import stripe
    creds = _platform_creds()
    secret = creds["stripe_secret"]

    if not secret or secret.startswith("sk_test_your"):
        return redirect("/upgrade/?error=stripe_not_configured")

    stripe.api_key = secret

    plan_key    = request.POST.get("plan", "starter")
    coupon_code = request.POST.get("coupon_code", "")
    price, _    = _apply_coupon(plan_key, coupon_code)

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    base   = f"{scheme}://{host}"

    try:
        # Upsert recurring Price object + Customer for tenant
        price_id    = _stripe_get_or_create_price(plan_key, price)
        customer_id = _stripe_customer_for_user(request.user)

        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{base}/payment/stripe/success/?session_id={{CHECKOUT_SESSION_ID}}&plan={plan_key}&coupon={coupon_code}",
            cancel_url=f"{base}/upgrade/",
            allow_promotion_codes=True,
            client_reference_id=str(request.user.id),
            metadata={
                "django_user_id": str(request.user.id),
                "plan_key": plan_key,
                "coupon_code": coupon_code or "",
            },
            subscription_data={
                "metadata": {
                    "django_user_id": str(request.user.id),
                    "plan_key": plan_key,
                },
            },
        )
        return redirect(session.url)
    except stripe.error.AuthenticationError:
        return redirect("/upgrade/?error=stripe_auth")
    except Exception as e:
        # Log to console in dev so we can see what failed; production safe.
        try:
            print(f"[stripe_create_session] {type(e).__name__}: {e}")
        except Exception:
            pass
        return redirect("/upgrade/?error=stripe_error")


@login_required(login_url="/login/")
def stripe_success(request):
    import stripe, datetime as _dt
    stripe.api_key = _platform_creds()["stripe_secret"]

    session_id  = request.GET.get("session_id", "")
    plan_key    = request.GET.get("plan", "starter")
    coupon_code = request.GET.get("coupon", "")

    try:
        session = stripe.checkout.Session.retrieve(
            session_id, expand=["subscription", "subscription.items.data.price"]
        )
        # For subscription mode, payment_status may stay "no_payment_required"
        # immediately after checkout — the source of truth is session.status.
        if session.status != "complete":
            return redirect("/upgrade/?error=payment_incomplete")
    except Exception:
        return redirect("/upgrade/?error=session_invalid")

    # Pull recurring subscription identifiers from the session
    sub_obj          = session.get("subscription")
    customer_id      = session.get("customer") or ""
    subscription_id  = sub_obj.id if hasattr(sub_obj, "id") else (sub_obj or "")
    price_id         = ""
    period_end_dt    = None

    if hasattr(sub_obj, "items"):
        try:
            price_id = sub_obj["items"]["data"][0]["price"]["id"]
        except Exception:
            price_id = ""
        try:
            period_end_dt = _dt.datetime.fromtimestamp(
                sub_obj["current_period_end"], tz=_dt.timezone.utc
            )
        except Exception:
            period_end_dt = None

    price, label = _apply_coupon(plan_key, coupon_code)
    _activate_subscription(
        request.user, plan_key, price, f"Stripe{label}",
        provider="stripe",
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        stripe_price_id=price_id,
        current_period_end=period_end_dt,
    )
    return redirect("/dashboard/")


@csrf_exempt
def stripe_webhook(request):
    """
    Stripe subscription lifecycle handler.

    Events handled (the rest are acknowledged with 200 so Stripe doesn't retry):
      - checkout.session.completed        → first activation (redundant w/ /success/)
      - customer.subscription.created     → store sub_id + period_end
      - customer.subscription.updated     → period renewal, plan change, cancel-at-period-end
      - customer.subscription.deleted     → mark canceled, downgrade tenant
      - invoice.paid                      → extend period_end, save invoice
      - invoice.payment_failed            → mark past_due
    """
    import stripe, datetime as _dt
    from superadmin.models import Subscription, Tenant, Invoice, TenantActivity

    creds          = _platform_creds()
    stripe.api_key = creds["stripe_secret"]
    payload        = request.body
    sig_header     = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    webhook_secret = creds["stripe_webhook"]

    # Verify (skip strict check only if webhook secret not configured — useful
    # for local dev when not yet wired through `stripe listen`).
    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = json.loads(payload.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid signature"}, status=400)

    etype  = event.get("type", "")
    data   = (event.get("data") or {}).get("object") or {}

    def _utc(ts):
        try:
            return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
        except Exception:
            return None

    def _find_sub_by_stripe_id(sub_id):
        if not sub_id:
            return None
        return Subscription.objects.filter(stripe_subscription_id=sub_id).first()

    def _find_sub_by_customer(cust_id):
        if not cust_id:
            return None
        return Subscription.objects.filter(stripe_customer_id=cust_id).first()

    try:
        if etype == "checkout.session.completed":
            # Redundant safety net — /success/ usually handles activation, but
            # if the user closes the tab before redirect, this still runs.
            user_id  = (data.get("metadata") or {}).get("django_user_id") or data.get("client_reference_id")
            plan_key = (data.get("metadata") or {}).get("plan_key", "starter")
            sub_id   = data.get("subscription") or ""
            cust_id  = data.get("customer") or ""
            if user_id:
                from django.contrib.auth.models import User
                user = User.objects.filter(id=int(user_id)).first()
                if user:
                    price, _ = _apply_coupon(plan_key, "")
                    period_end_dt = None
                    if sub_id:
                        try:
                            s = stripe.Subscription.retrieve(sub_id)
                            period_end_dt = _utc(s.get("current_period_end"))
                        except Exception:
                            pass
                    _activate_subscription(
                        user, plan_key, price, "Stripe webhook",
                        provider="stripe",
                        stripe_customer_id=cust_id,
                        stripe_subscription_id=sub_id,
                        current_period_end=period_end_dt,
                    )

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            sub_id  = data.get("id") or ""
            sub_row = _find_sub_by_stripe_id(sub_id) or _find_sub_by_customer(data.get("customer"))
            if sub_row:
                sub_row.provider = "stripe"
                sub_row.stripe_subscription_id = sub_id
                if data.get("customer"):
                    sub_row.stripe_customer_id = data["customer"]
                sub_row.current_period_end   = _utc(data.get("current_period_end")) or sub_row.current_period_end
                sub_row.cancel_at_period_end = bool(data.get("cancel_at_period_end"))
                stripe_status = data.get("status") or ""
                if stripe_status:
                    sub_row.status = stripe_status if stripe_status in dict(
                        Subscription._meta.get_field("status").choices
                    ) else sub_row.status
                # Reflect cancellation in tenant if Stripe says canceled
                if stripe_status == "canceled":
                    sub_row.canceled_at = timezone.now()
                if data.get("items") and data["items"].get("data"):
                    try:
                        sub_row.stripe_price_id = data["items"]["data"][0]["price"]["id"]
                    except Exception:
                        pass
                if sub_row.current_period_end:
                    sub_row.renews_on = sub_row.current_period_end.date()
                sub_row.save()

        elif etype == "customer.subscription.deleted":
            sub_id  = data.get("id") or ""
            sub_row = _find_sub_by_stripe_id(sub_id)
            if sub_row:
                sub_row.status               = "canceled"
                sub_row.canceled_at          = timezone.now()
                sub_row.cancel_at_period_end = False
                sub_row.payment_status       = "failed"
                sub_row.save()
                # Downgrade tenant access
                t = sub_row.tenant
                t.status = "suspended"
                t.save(update_fields=["status"])
                TenantActivity.objects.create(
                    tenant=t,
                    action="Stripe subscription canceled — access suspended",
                    action_type="plan",
                )

        elif etype == "invoice.paid":
            cust_id = data.get("customer") or ""
            sub_id  = data.get("subscription") or ""
            sub_row = _find_sub_by_stripe_id(sub_id) or _find_sub_by_customer(cust_id)
            if sub_row:
                sub_row.payment_status = "paid"
                sub_row.status         = "active"
                # invoice line item gives the period_end of next billing cycle
                try:
                    line_pe = data["lines"]["data"][0]["period"]["end"]
                    sub_row.current_period_end = _utc(line_pe) or sub_row.current_period_end
                except Exception:
                    pass
                if sub_row.current_period_end:
                    sub_row.renews_on = sub_row.current_period_end.date()
                sub_row.last_invoice_id  = data.get("id") or ""
                sub_row.last_invoice_url = data.get("hosted_invoice_url") or ""
                sub_row.save()

                # tenant access ON
                t = sub_row.tenant
                if t.status != "active":
                    t.status = "active"
                    t.save(update_fields=["status"])

                # Invoice record
                Invoice.objects.update_or_create(
                    provider="stripe",
                    external_id=data.get("id") or "",
                    defaults={
                        "tenant":       sub_row.tenant,
                        "amount":       (data.get("amount_paid") or 0) / 100.0,
                        "currency":     (data.get("currency") or "usd"),
                        "status":       "paid",
                        "period_start": _utc((data.get("lines") or {}).get("data", [{}])[0].get("period", {}).get("start")) if data.get("lines") else None,
                        "period_end":   _utc((data.get("lines") or {}).get("data", [{}])[0].get("period", {}).get("end"))   if data.get("lines") else None,
                        "invoice_url":  data.get("hosted_invoice_url") or "",
                        "pdf_url":      data.get("invoice_pdf") or "",
                        "description":  (data.get("lines") or {}).get("data", [{}])[0].get("description") or "Drop Sigma subscription",
                    },
                )

        elif etype == "invoice.payment_failed":
            cust_id = data.get("customer") or ""
            sub_id  = data.get("subscription") or ""
            sub_row = _find_sub_by_stripe_id(sub_id) or _find_sub_by_customer(cust_id)
            if sub_row:
                sub_row.payment_status = "failed"
                sub_row.status         = "past_due"
                sub_row.last_invoice_url = data.get("hosted_invoice_url") or sub_row.last_invoice_url
                sub_row.save()
                TenantActivity.objects.create(
                    tenant=sub_row.tenant,
                    action="Stripe payment failed — subscription past due",
                    action_type="payment",
                )

    except Exception as e:
        # Never 500 to Stripe — that triggers retries forever. Log and ack.
        try:
            print(f"[stripe_webhook] {etype} error: {type(e).__name__}: {e}")
        except Exception:
            pass

    return JsonResponse({"received": True})


# ── Tenant Billing Page + Stripe Customer Portal ─────────────────────────────

@login_required(login_url="/login/")
def billing_view(request):
    """
    Tenant-facing billing dashboard. Shows current plan, renewal date, last
    payment status, and invoice history. Self-service actions (upgrade /
    downgrade / cancel / update card / download invoice) all delegate to
    Stripe's hosted Customer Portal via stripe_portal_session.
    """
    from superadmin.models import Tenant, Subscription, Invoice

    user = request.user
    if not user.is_staff:
        return redirect("/login/")

    tenant = getattr(user, "tenant_profile", None)
    sub    = None
    if tenant:
        try:
            sub = tenant.subscription
        except Exception:
            sub = None

    plan_label = PLAN_NAMES.get(
        {"basic": "starter", "pro": "growth", "enterprise": "scale"}.get(
            (sub.plan if sub else "trial"), "trial"
        ),
        (sub.plan if sub else "Trial").title(),
    )

    invoices = []
    if tenant:
        invoices = list(Invoice.objects.filter(tenant=tenant).order_by("-created_at")[:24])

    # Is this tenant on a real recurring subscription?
    has_stripe_sub = bool(sub and sub.stripe_subscription_id)

    creds = _platform_creds()
    portal_enabled = bool(creds["stripe_secret"] and not creds["stripe_secret"].startswith("sk_test_your"))

    ctx = {
        "tenant":             tenant,
        "sub":                sub,
        "plan_label":         plan_label,
        "invoices":           invoices,
        "has_stripe_sub":     has_stripe_sub,
        "portal_enabled":     portal_enabled,
        "is_subscribed":      _is_subscribed(user),
    }
    return render(request, "billing.html", ctx)


@login_required(login_url="/login/")
@require_POST
def stripe_portal_session(request):
    """
    Create a Stripe Customer Portal session and redirect there. The portal
    lets the tenant upgrade, downgrade, cancel, update card, and download
    invoices — all on Stripe's hosted, PCI-compliant UI.
    """
    import stripe
    creds = _platform_creds()
    stripe.api_key = creds["stripe_secret"]

    if not stripe.api_key or stripe.api_key.startswith("sk_test_your"):
        return redirect("/billing/?error=stripe_not_configured")

    try:
        sub = request.user.tenant_profile.subscription
        customer_id = sub.stripe_customer_id
    except Exception:
        return redirect("/billing/?error=no_subscription")

    if not customer_id:
        return redirect("/billing/?error=no_customer")

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    base   = f"{scheme}://{host}"

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/billing/",
        )
        return redirect(portal.url)
    except Exception as e:
        try:
            print(f"[stripe_portal_session] {type(e).__name__}: {e}")
        except Exception:
            pass
        return redirect("/billing/?error=portal_failed")


@login_required(login_url="/login/")
@require_POST
def stripe_cancel_subscription(request):
    """
    One-click cancel — sets cancel_at_period_end=true so the tenant keeps
    access until the current period ends, then auto-downgrades via webhook.
    """
    import stripe
    creds = _platform_creds()
    stripe.api_key = creds["stripe_secret"]

    try:
        sub_row = request.user.tenant_profile.subscription
        if not sub_row.stripe_subscription_id:
            return JsonResponse({"ok": False, "error": "No active subscription."}, status=400)
        stripe.Subscription.modify(
            sub_row.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        sub_row.cancel_at_period_end = True
        sub_row.save(update_fields=["cancel_at_period_end"])
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required(login_url="/login/")
@require_POST
def stripe_resume_subscription(request):
    """Undo a pending cancel — keeps the subscription rolling."""
    import stripe
    creds = _platform_creds()
    stripe.api_key = creds["stripe_secret"]

    try:
        sub_row = request.user.tenant_profile.subscription
        if not sub_row.stripe_subscription_id:
            return JsonResponse({"ok": False, "error": "No active subscription."}, status=400)
        stripe.Subscription.modify(
            sub_row.stripe_subscription_id,
            cancel_at_period_end=False,
        )
        sub_row.cancel_at_period_end = False
        sub_row.save(update_fields=["cancel_at_period_end"])
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


# ── PayPal ────────────────────────────────────────────────────────────────────

@login_required(login_url="/login/")
@require_POST
def paypal_create_order(request):
    import requests as _req
    data        = json.loads(request.body)
    plan_key    = data.get("plan", "starter")
    coupon_code = data.get("coupon_code", "")
    price, _    = _apply_coupon(plan_key, coupon_code)

    # Get PayPal access token (DB-saved credentials override env vars)
    creds    = _platform_creds()
    mode     = creds["paypal_mode"]
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(creds["paypal_client_id"], creds["paypal_client_secret"]),
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

    creds    = _platform_creds()
    mode     = creds["paypal_mode"]
    base_url = "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"

    token_res = _req.post(
        f"{base_url}/v1/oauth2/token",
        auth=(creds["paypal_client_id"], creds["paypal_client_secret"]),
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


# ─── Support AI — in-app help assistant ─────────────────────────────────────
def support_ai_ask(request):
    """Tenant-facing AI help. Pass a question; returns structured guidance JSON
    (reply + optional steps + breadcrumb + deep_link). Frontend renders this
    as either a paragraph or a step-by-step card.

    POST body: { question: str, history: [{role, content}, ...] (optional) }
    Returns:   { success, reply, steps?, breadcrumb?, deep_link? }
    """
    import json as _json
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST required."}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "message": "Sign in to use the AI assistant."}, status=401)

    try:
        body = _json.loads(request.body or b"{}")
    except Exception:
        body = {}
    question = (body.get("question") or "").strip()
    history  = body.get("history") or []
    if not question:
        return JsonResponse({"success": False, "message": "Question is required."}, status=400)
    if len(question) > 2000:
        return JsonResponse({"success": False, "message": "Question too long (max 2000 chars)."}, status=400)

    # Groq (free tier) — rotates across 3 models, then falls back to the
    # offline keyword matcher if all are rate-limited or down. Same JSON
    # response shape as the previous Claude path; no frontend change.
    try:
        from core.groq_support import answer as _groq_answer
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Help service unavailable: {e}"}, status=500)

    payload = _groq_answer(question, history)

    return JsonResponse({
        "success":       True,
        "reply":         payload.get("reply") or "",
        "steps":         payload.get("steps") or [],
        "breadcrumb":    payload.get("breadcrumb") or [],
        "deep_link":     payload.get("deep_link") or "",
        "deep_link_url": payload.get("deep_link_url") or "",
        "_source":       payload.get("_source") or "",
    })


# ════════════════════════════════════════════════════════════════════════
# Product-image download proxy
# ────────────────────────────────────────────────────────────────────────
# Many e-commerce CDNs (Shopify, WooCommerce-hosted, AliExpress, Cloudinary,
# etc.) block cross-origin fetch() in the browser, so client-side "fetch +
# blob + a.download" silently falls back to opening the URL in a new tab —
# which means the user has to right-click → save. This proxy fetches the
# image server-side and streams it back with Content-Disposition: attachment,
# so the browser ALWAYS triggers a real file download.
#
# Security:
#   - Authenticated users only
#   - HTTPS / HTTP only (no file://, no ftp://, etc.)
#   - SSRF guard: block private/loopback/link-local IPs
#   - Size cap: 25 MB
#   - Whitelist of common image content-types — anything else is rejected
# ════════════════════════════════════════════════════════════════════════
@login_required(login_url="/login/")
def download_image_proxy(request):
    """Force-download a remote image via the server (bypasses CORS).

    GET /api/download-image/?url=<encoded_url>&name=<optional_filename>
    """
    import ipaddress, os.path, re
    from urllib.parse import urlparse, unquote
    from django.http import HttpResponse, HttpResponseBadRequest, StreamingHttpResponse

    raw_url = (request.GET.get("url") or "").strip()
    if not raw_url:
        return HttpResponseBadRequest("Missing url parameter")

    # ── Validate URL ─────────────────────────────────────────────────
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return HttpResponseBadRequest("Invalid URL")
    if parsed.scheme not in ("http", "https"):
        return HttpResponseBadRequest("Only http/https URLs are allowed")
    if not parsed.netloc:
        return HttpResponseBadRequest("URL missing host")

    # ── SSRF guard: refuse private / loopback / link-local hosts ────
    host = parsed.hostname or ""
    try:
        # Reject obvious local hostnames
        if host.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "metadata.google.internal"}:
            return HttpResponseBadRequest("Disallowed host")
        # If it parses as an IP, ensure it's public
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return HttpResponseBadRequest("Disallowed host")
        except ValueError:
            pass  # Hostname (not raw IP) — fine
    except Exception:
        return HttpResponseBadRequest("Invalid host")

    # ── Fetch ────────────────────────────────────────────────────────
    try:
        import requests
        upstream = requests.get(
            raw_url,
            stream=True,
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": "DropSigma-ImageProxy/1.0"},
        )
    except Exception as e:
        return HttpResponse(f"Could not fetch image: {e}", status=502)

    if upstream.status_code >= 400:
        return HttpResponse(
            f"Upstream returned HTTP {upstream.status_code}",
            status=502,
        )

    # ── Validate content-type (must be an image) ─────────────────────
    ctype = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    allowed_types = {
        "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",
        "image/avif", "image/bmp", "image/svg+xml", "image/tiff",
        # Some CDNs serve images as octet-stream — accept if URL ends with image ext
        "application/octet-stream",
    }
    if ctype not in allowed_types:
        return HttpResponse(f"Not an image (content-type: {ctype})", status=415)

    # ── Size guard ───────────────────────────────────────────────────
    MAX_BYTES = 25 * 1024 * 1024  # 25 MB
    declared = upstream.headers.get("Content-Length")
    try:
        if declared and int(declared) > MAX_BYTES:
            return HttpResponse("Image too large", status=413)
    except Exception:
        pass

    # ── Pick a clean filename ────────────────────────────────────────
    requested_name = (request.GET.get("name") or "").strip()
    # Try to infer extension from content-type, fall back to URL path
    ext_map = {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
        "image/bmp": "bmp", "image/svg+xml": "svg", "image/tiff": "tiff",
    }
    ext = ext_map.get(ctype, "")
    if not ext:
        # Pull extension from URL path
        path_ext = os.path.splitext(unquote(parsed.path))[1].lstrip(".").lower()
        if path_ext in {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "tiff"}:
            ext = "jpeg" if path_ext == "jpg" else path_ext
            ext = "jpg" if ext == "jpeg" else ext
        else:
            ext = "jpg"  # safe default
    # Sanitize requested name
    if requested_name:
        # Strip filesystem-unsafe chars, allow unicode word chars + dash/underscore/dot/space
        safe = re.sub(r"[^\w؀-ۿ一-鿿\-_. ]", "", requested_name)
        safe = re.sub(r"\s+", "_", safe).strip("._-") or "product"
        # If user didn't include an extension, append the inferred one
        if "." not in safe or not safe.rsplit(".", 1)[1].lower() in {"jpg","jpeg","png","gif","webp","avif","bmp","svg","tiff"}:
            safe = f"{safe}.{ext}"
        filename = safe[:120]  # cap length
    else:
        # Derive from URL filename
        url_base = os.path.basename(unquote(parsed.path)) or "product"
        url_base = re.sub(r"[^\w؀-ۿ一-鿿\-_. ]", "", url_base) or "product"
        if "." not in url_base:
            url_base = f"{url_base}.{ext}"
        filename = url_base[:120]

    # ── Stream back with download header ─────────────────────────────
    def _stream():
        bytes_seen = 0
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            bytes_seen += len(chunk)
            if bytes_seen > MAX_BYTES:
                break
            yield chunk

    # Use image/* even for octet-stream upstream so the browser handles it as an image
    out_ctype = ctype if ctype.startswith("image/") else f"image/{ext}"
    resp = StreamingHttpResponse(_stream(), content_type=out_ctype)
    # ?inline=1 → display in the page (used by the lightbox so the source CDN
    # URL never appears in the rendered <img src=…>). Default = force download.
    inline_mode = request.GET.get("inline") in ("1", "true", "yes")
    disposition = "inline" if inline_mode else "attachment"
    # RFC 5987 encoded filename for non-ASCII safety
    from urllib.parse import quote
    resp["Content-Disposition"] = (
        f'{disposition}; filename="{filename}"; filename*=UTF-8\'\'{quote(filename)}'
    )
    resp["X-Content-Type-Options"] = "nosniff"
    # Allow modest caching for inline display so re-renders are fast, but never
    # cache attachments (privacy of the download action).
    resp["Cache-Control"] = "private, max-age=600" if inline_mode else "private, no-store"
    # Prevent referrer leakage so the upstream CDN can never tell who clicked
    resp["Referrer-Policy"] = "no-referrer"
    return resp
