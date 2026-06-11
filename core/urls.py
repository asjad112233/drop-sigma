from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import RedirectView
from . import views
from . import password_reset as _pr
from teamapp import views as teamapp_views
from vendors import views as vendor_views
from sourcing_partners import views as sp_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # Chrome / Safari request /favicon.ico unconditionally, even when a
    # <link rel="icon"> is present. Send them to our static SVG so the
    # tab actually has a brand mark instead of the generic globe.
    path("favicon.ico", RedirectView.as_view(url="/static/favicon.svg", permanent=True)),
    path("", views.homepage, name="home"),
    # Public legal pages (mandatory for Shopify App Store submission)
    path("privacy/", views.privacy_policy_page, name="privacy_policy"),
    path("terms/",   views.terms_of_service_page, name="terms_of_service"),
    path("support/", views.support_page,         name="support_page"),
    # Public marketing / info pages
    path("about/",    views.about_page,    name="about_page"),
    path("pricing/",  views.pricing_page,  name="pricing_page"),
    path("contact/",  views.contact_page,  name="contact_page"),
    path("contact/submit/", views.contact_submit, name="contact_submit"),
    path("cookies/",  views.cookies_page,  name="cookies_page"),
    path("refund/",   views.refund_page,   name="refund_page"),
    path("docs/",     views.docs_page,     name="docs_page"),
    path("features/", views.features_page, name="features_page"),
    path("dashboard/", views.dashboard_page, name="dashboard"),
    # Embeddable emails-only view for the employee portal iframe
    path("dashboard/embed/emails/", views.dashboard_embed_emails, name="dashboard_embed_emails"),
    path("login/", views.admin_login_page, name="admin_login"),
    path("setup-admin-x9k2/", views.setup_admin),
    path("logout/", views.admin_logout_view, name="admin_logout"),

    # Unified platform password reset (admin / team / vendor — anyone with auth_user)
    path("api/forgot-password/", _pr.forgot_password_api, name="forgot_password_api"),
    path("reset-password/<str:uidb64>/<str:token>/", _pr.reset_password_page, name="reset_password_page"),
    path("signup/", views.signup_view, name="signup"),
    path("signup/email-sent/", views.email_sent_view, name="email_sent"),
    path("signup/resend-verification/", views.resend_verification_email_view, name="resend_verification"),
    path("verify-email/<uuid:token>/", views.verify_email_view, name="verify_email"),
    path("api/profile/", views.api_profile, name="api_profile"),
    path("profile/", views.profile_page, name="profile"),

    # Support AI — in-app help assistant
    path("support-ai/ask/", csrf_exempt(views.support_ai_ask), name="support_ai_ask"),

    # Product-image download proxy (force-download bypassing CDN CORS blocks)
    path("api/download-image/", views.download_image_proxy, name="download_image_proxy"),
    path("upgrade/", views.upgrade_view, name="upgrade"),
    path("checkout/", views.checkout_view, name="checkout"),
    path("checkout/free/", views.checkout_free, name="checkout_free"),
    path("subscribe/", views.subscribe_view, name="subscribe"),
    # Stripe
    path("payment/stripe/create/",   views.stripe_create_session, name="stripe_create"),
    path("payment/stripe/success/",  views.stripe_success,        name="stripe_success"),
    path("payment/stripe/webhook/",  views.stripe_webhook,        name="stripe_webhook"),
    # PayPal
    path("payment/paypal/create-order/",  views.paypal_create_order,  name="paypal_create"),
    path("payment/paypal/capture-order/", views.paypal_capture_order, name="paypal_capture"),

    # Tenant billing & self-service (Stripe Customer Portal)
    path("billing/",         views.billing_view,                name="billing"),
    path("billing/portal/",  views.stripe_portal_session,       name="stripe_portal"),
    path("billing/cancel/",  views.stripe_cancel_subscription,  name="stripe_cancel"),
    path("billing/resume/",  views.stripe_resume_subscription,  name="stripe_resume"),

    # Returns & Refunds (RMA)
    path("rma/", include("rma.urls")),
    path("r/",   include("rma.urls_customer")),

    # DropSigma Sourcing Partners — verified vendors tenants can chat with
    path("sourcing-partners/", include("sourcing_partners.urls")),
    # Short alias for the dedicated-manager endpoint (consumed by the
    # dashboard chat header).
    path("sourcing/api/my-manager/", sp_views.api_my_manager,
         name="sp_api_my_manager_alias"),

    # DropSigma Operations Portal — internal back-office for the ops team
    path("ops/", include("sourcing_ops.urls")),

    # Apps
    path("stores/", include("stores.urls")),
    path("orders/", include("orders.urls")),
    # Public tracking. Production traffic on track.dropsigma.com is
    # host-routed by TrackingHostMiddleware; this /track/ mount is the
    # fallback for direct testing on dropsigma.com/track/<id>/.
    path("track/", include("tracking_public.urls")),
    path("teamapp/", include("teamapp.urls")),
    path("emails/", include("emails.urls")),
    path("vendors/api/", include("vendors.urls")),
    path("vendor/", include("vendors.portal_urls")),
    path("employee/", include("teamapp.portal_urls")),
    path("employee/invite/accept/<uuid:token>/",       teamapp_views.accept_invitation_page),
    path("employee/invite/set-password/<uuid:token>/", csrf_exempt(teamapp_views.set_invitation_password_api)),
    path("employee/login/activate/<uuid:token>/",      teamapp_views.employee_activate_login),
    path("vendor/invite/accept/<uuid:token>/",         vendor_views.accept_vendor_invitation_page),
    path("vendor/invite/set-password/<uuid:token>/",   csrf_exempt(vendor_views.set_vendor_invitation_password_api)),
    path("vendor/login/activate/<uuid:token>/",        vendor_views.vendor_activate_login_by_token),
    path("stock/", include("stock.urls")),
    path("superadmin/", include("superadmin.urls")),
    path("notifications/", include("notifications.urls")),
]

# ── Media file serving ────────────────────────────────────────────────
# `static()` from django.conf.urls.static ONLY registers when DEBUG=True,
# which means uploaded chat images / attachments return 404 in production
# AND in dev when DEBUG isn't explicitly enabled. We need media to work
# everywhere, so we register an explicit `serve` route. On production
# Railway this still works since uploads land on the same filesystem;
# when we migrate to S3/CDN later the URL pattern will be replaced.
from django.urls import re_path
from django.views.static import serve as _static_serve
urlpatterns += [
    re_path(
        r'^' + settings.MEDIA_URL.lstrip('/') + r'(?P<path>.*)$',
        _static_serve,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
# Keep the legacy DEBUG-gated helper too so collectstatic/etc still works
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# ── Local-only promo/ad routes (never deployed) ──────────────────────
# The file `core/local_promo.py` is intentionally gitignored. It holds
# every animated promo, cinematic intro, walkthrough, and 9:16 paid-ad
# template used for screen-capture → MP4 export on the dev server only.
# On Railway production the file is absent → ImportError is swallowed
# → those routes simply don't exist. dropsigma.com stays clean.
try:
    from .local_promo import local_promo_patterns
    urlpatterns += local_promo_patterns
except ImportError:
    pass
