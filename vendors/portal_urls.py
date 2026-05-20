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
    # Self-service
    path("api/my-permissions/", views.vendor_my_permissions_api, name="vendor_my_permissions"),
    path("api/my-permanent-products/", views.vendor_my_permanent_products_api, name="vendor_my_permanent_products"),
    path("api/change-password/", views.vendor_change_password_api, name="vendor_change_password"),
    path("forgot-password/", views.vendor_forgot_password_api, name="vendor_forgot_password"),
]
