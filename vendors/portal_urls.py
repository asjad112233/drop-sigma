from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.vendor_login_page, name="vendor_login"),
    path("logout/", views.vendor_logout_view, name="vendor_logout"),
    path("dashboard/", views.vendor_portal_page, name="vendor_portal"),
    path("api/orders/", views.vendor_orders_api, name="vendor_orders_api"),
    path("api/submit-tracking/<int:order_id>/", views.vendor_submit_tracking_api, name="vendor_submit_tracking"),
    path("api/tracking-history/", views.vendor_tracking_history_api, name="vendor_tracking_history"),
    # Stock
    path("api/stock/", views.vendor_stock_api, name="vendor_stock_api"),
    path("api/stock/<int:assignment_id>/pricing/", views.vendor_stock_submit_pricing_api, name="vendor_stock_pricing"),
    path("api/stock/<int:assignment_id>/arrived/", views.vendor_stock_mark_arrived_api, name="vendor_stock_arrived"),

    # ─── Vendor Pricing & Approval System (v1) ──────────────────────────────
    path("api/sourcing/assignments/",                      views.portal_my_assignments_api,    name="portal_sourcing_assignments"),
    path("api/sourcing/quote/<int:assignment_id>/submit/", views.portal_submit_quote_api,      name="portal_submit_quote"),
    path("api/sourcing/quote/<int:quote_id>/change/",      views.portal_submit_change_api,     name="portal_submit_change"),
    path("api/sourcing/reasons/",                          views.portal_reasons_api,           name="portal_sourcing_reasons"),
]
