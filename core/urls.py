from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.csrf import csrf_exempt
from . import views
from teamapp import views as teamapp_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.homepage, name="home"),
    path("dashboard/", views.dashboard_page, name="dashboard"),
    path("login/", views.admin_login_page, name="admin_login"),
    path("setup-admin-x9k2/", views.setup_admin),
    path("logout/", views.admin_logout_view, name="admin_logout"),
    path("signup/", views.signup_view, name="signup"),
    path("signup/email-sent/", views.email_sent_view, name="email_sent"),
    path("signup/resend-verification/", views.resend_verification_email_view, name="resend_verification"),
    path("verify-email/<uuid:token>/", views.verify_email_view, name="verify_email"),
    path("api/profile/", views.api_profile, name="api_profile"),
    path("profile/", views.profile_page, name="profile"),
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

    # Apps
    path("stores/", include("stores.urls")),
    path("orders/", include("orders.urls")),
    path("teamapp/", include("teamapp.urls")),
    path("emails/", include("emails.urls")),
    path("vendors/api/", include("vendors.urls")),
    path("vendor/", include("vendors.portal_urls")),
    path("employee/", include("teamapp.portal_urls")),
    path("employee/invite/accept/<uuid:token>/",       teamapp_views.accept_invitation_page),
    path("employee/invite/set-password/<uuid:token>/", csrf_exempt(teamapp_views.set_invitation_password_api)),
    path("employee/login/activate/<uuid:token>/",      teamapp_views.employee_activate_login),
    path("stock/", include("stock.urls")),
    path("superadmin/", include("superadmin.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
