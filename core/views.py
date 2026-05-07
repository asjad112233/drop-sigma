import uuid
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.conf import settings
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt


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

                # Send verification email via Resend API (Railway blocks SMTP ports)
                import threading, logging, os as _os, resend as _resend
                _mail_logger = logging.getLogger("dropsigma.mail")
                def _send():
                    try:
                        _mail_logger.info(f"Sending verification email to {email} via Resend")
                        _resend.api_key = _os.getenv("RESEND_API_KEY", "")
                        html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:40px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">

        <!-- Header -->
        <tr><td align="center" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:16px 16px 0 0;padding:32px 24px;">
          <table cellpadding="0" cellspacing="0"><tr>
            <td style="background:rgba(255,255,255,0.15);border-radius:12px;width:44px;height:44px;text-align:center;vertical-align:middle;">
              <span style="color:#fff;font-weight:900;font-size:18px;letter-spacing:-.5px;">DS</span>
            </td>
            <td style="padding-left:12px;">
              <div style="color:#fff;font-weight:900;font-size:20px;letter-spacing:-.3px;">Drop Sigma</div>
              <div style="color:rgba(255,255,255,0.7);font-size:11px;margin-top:2px;">Ecommerce Operations OS</div>
            </td>
          </tr></table>
        </td></tr>

        <!-- Body -->
        <tr><td style="background:#ffffff;padding:36px 40px;">
          <h1 style="margin:0 0 8px;font-size:22px;font-weight:800;color:#0f172a;letter-spacing:-.3px;">Verify your email address</h1>
          <p style="margin:0 0 24px;font-size:14px;color:#64748b;line-height:1.6;">Hi <strong style="color:#0f172a;">{name}</strong>, welcome to Drop Sigma! Click the button below to verify your email and activate your account.</p>

          <table cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
            <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:10px;">
              <a href="{link}" style="display:inline-block;padding:14px 32px;color:#fff;font-weight:700;font-size:15px;text-decoration:none;letter-spacing:-.1px;">Verify Email Address →</a>
            </td></tr>
          </table>

          <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;margin-bottom:24px;">
            <p style="margin:0 0 6px;font-size:11px;font-weight:700;color:#94a3b8;letter-spacing:.05em;text-transform:uppercase;">Or copy this link</p>
            <p style="margin:0;font-size:12px;color:#6366f1;word-break:break-all;"><a href="{link}" style="color:#6366f1;text-decoration:none;">{link}</a></p>
          </div>

          <div style="border-top:1px solid #e2e8f0;padding-top:20px;">
            <p style="margin:0;font-size:12px;color:#94a3b8;line-height:1.6;">This link expires in <strong style="color:#64748b;">24 hours</strong>. If you did not create a Drop Sigma account, you can safely ignore this email.</p>
          </div>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f8fafc;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 16px 16px;padding:20px 40px;text-align:center;">
          <p style="margin:0;font-size:11px;color:#94a3b8;">© 2026 Drop Sigma · <a href="https://dropsigma.com" style="color:#6366f1;text-decoration:none;">dropsigma.com</a></p>
          <p style="margin:6px 0 0;font-size:11px;color:#cbd5e1;">This email was sent from <a href="mailto:noreply@dropsigma.com" style="color:#94a3b8;text-decoration:none;">noreply@dropsigma.com</a></p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""
                        _resend.Emails.send({
                            "from": "Drop Sigma <noreply@dropsigma.com>",
                            "to": [email],
                            "subject": "Verify your Drop Sigma account",
                            "html": html_body,
                        })
                        _mail_logger.info(f"Verification email sent OK to {email}")
                    except Exception as exc:
                        _mail_logger.error(f"Verification email FAILED to {email}: {exc}")
                threading.Thread(target=_send, daemon=True).start()

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

    from django.contrib.auth.models import User
    from superadmin.models import EmailVerificationToken
    import threading, logging, os as _os, resend as _resend

    _mail_logger = logging.getLogger("dropsigma.mail")

    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        return redirect(f"/signup/email-sent/?email={email}&resent=1")

    # Delete old tokens and create fresh one
    EmailVerificationToken.objects.filter(user=user).delete()
    token_obj = EmailVerificationToken.objects.create(user=user)

    host   = request.get_host()
    scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
    link   = f"{scheme}://{host}/verify-email/{token_obj.token}/"
    name   = user.first_name or user.username

    def _send():
        try:
            _resend.api_key = _os.getenv("RESEND_API_KEY", "")
            html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
  <tr><td align="center">
  <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">
    <tr><td align="center" style="padding-bottom:28px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;width:40px;height:40px;text-align:center;vertical-align:middle;">
          <span style="color:#fff;font-weight:900;font-size:16px;">DS</span>
        </td>
        <td style="padding-left:10px;">
          <span style="font-size:18px;font-weight:800;color:#0f172a;letter-spacing:-.3px;">Drop Sigma</span>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;padding:44px 48px;">
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;letter-spacing:-.4px;">Verify your email address</h1>
      <p style="margin:0 0 28px;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, here is your new verification link. Click the button below to activate your account.</p>
      <table cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
        <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:10px;">
          <a href="{link}" style="display:inline-block;padding:14px 36px;color:#fff;font-weight:700;font-size:15px;text-decoration:none;">Verify Email Address →</a>
        </td></tr>
      </table>
      <hr style="border:none;border-top:1px solid #e2e8f0;margin:0 0 24px;">
      <p style="margin:0 0 8px;font-size:12px;font-weight:700;color:#94a3b8;letter-spacing:.06em;text-transform:uppercase;">Or copy this link</p>
      <p style="margin:0 0 28px;font-size:13px;color:#6366f1;word-break:break-all;">{link}</p>
      <div style="background:#fafafa;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;">
        <p style="margin:0;font-size:13px;color:#64748b;line-height:1.6;">⏱ This link expires in <strong style="color:#0f172a;">24 hours</strong>. If you didn't create an account, you can safely ignore this email.</p>
      </div>
    </td></tr>
    <tr><td style="padding:28px 8px 0;text-align:center;">
      <p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">Questions? Email us at <a href="mailto:support@dropsigma.com" style="color:#6366f1;text-decoration:none;">support@dropsigma.com</a></p>
      <p style="margin:0;font-size:12px;color:#cbd5e1;">© 2026 Drop Sigma · <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a></p>
    </td></tr>
  </table>
  </td></tr>
  </table>
</body></html>"""
            _resend.Emails.send({
                "from": "Drop Sigma <noreply@dropsigma.com>",
                "to": [email],
                "subject": "Verify your Drop Sigma account",
                "html": html_body,
            })
            _mail_logger.info(f"Resent verification email to {email}")
        except Exception as exc:
            _mail_logger.error(f"Resend verification FAILED to {email}: {exc}")

    threading.Thread(target=_send, daemon=True).start()
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
        return redirect("/login/?tab=team")

    imp_id    = request.session.get("impersonate_id")
    imp_name  = request.session.get("impersonate_name", "")
    imp_email = request.session.get("impersonate_email", "")

    subscribed = _is_subscribed(request.user)

    real_user   = request.user
    display_name = real_user.get_full_name().strip() or real_user.username
    initials     = "".join(w[0].upper() for w in display_name.split()[:2]) or "U"

    return render(request, "dashboard.html", {
        "is_impersonating":  bool(imp_id),
        "impersonate_name":  imp_name,
        "impersonate_email": imp_email,
        "is_subscribed":     subscribed,
        "display_name":      display_name,
        "user_initials":     initials,
    })


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
