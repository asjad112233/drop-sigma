from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.vendor_login_page, name="vendor_login"),
    path("logout/", views.vendor_logout_view, name="vendor_logout"),
    path("dashboard/", views.vendor_portal_page, name="vendor_portal"),
    path("api/orders/", views.vendor_orders_api, name="vendor_orders_api"),
    path("api/submit-tracking/<int:order_id>/", views.vendor_submit_tracking_api, name="vendor_submit_tracking"),
    path("api/tracking-history/", views.vendor_tracking_history_api, name="vendor_tracking_history"),
]
