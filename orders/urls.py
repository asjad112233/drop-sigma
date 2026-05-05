from django.urls import path
from . import views

urlpatterns = [
    # Dashboard Page
    path("page/", views.orders_page, name="orders_page"),

    # Sync Orders
    path("sync/<int:store_id>/", views.sync_orders, name="sync_orders"),

    # APIs
    path("api/", views.orders_list_api, name="orders_list_api"),
    path("api/poll/", views.orders_poll_api, name="orders_poll_api"),
    path("api/<int:order_id>/", views.order_detail_api, name="order_detail_api"),
    path("api/<int:order_id>/activity/", views.order_activity_api, name="order_activity_api"),
    path("api/<int:order_id>/assign/", views.assign_order_api, name="assign_order_api"),
    path("api/<int:order_id>/tracking/", views.save_order_tracking_api, name="save_order_tracking"),
    path("api/<int:order_id>/fetch-tracking-status/", views.fetch_live_tracking_api, name="fetch_live_tracking"),
    path("api/<int:order_id>/update-status/", views.update_order_status_api, name="update_order_status_api"),

    # 🔥 AUTO ASSIGN (TEAM)
    path("api/auto-assign/", views.auto_assign_orders_api, name="auto_assign_orders_api"),

    # 🔥 VENDOR APIs (NEW)
    path("assign-vendor/<int:order_id>/", views.assign_vendor_to_order_api),
    path("bulk-assign-vendor/", views.bulk_assign_vendor_api),
    path("remove-product-vendor-assignment/", views.remove_product_vendor_assignment_api),

    # Order lookup by number (for chat links)
    path("api/lookup/", views.order_lookup_by_number_api, name="order_lookup_by_number"),

    # Overview
    path("overview/", views.overview_api, name="overview_api"),

    # WooCommerce Webhook (auto-sync new orders)
    path("webhook/woocommerce/<int:store_id>/", views.woocommerce_webhook, name="woocommerce_webhook"),
    path("webhook/woocommerce/<int:store_id>/setup/", views.setup_webhook_api, name="setup_webhook_api"),

    # Shopify Webhook (auto-sync new orders)
    path("webhook/shopify/<int:store_id>/", views.shopify_webhook, name="shopify_webhook"),
]