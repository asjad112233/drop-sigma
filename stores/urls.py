from django.urls import path
from . import views

urlpatterns = [
    path("", views.stores_page, name="stores_page"),

    path("api/", views.stores_list_api, name="stores_list_api"),
    path("api/create/", views.create_store_api, name="create_store_api"),
    path("api/delete/<int:store_id>/", views.delete_store_api, name="delete_store_api"),

    # ✅ AUTO CONNECT FLOW
    path("api/auto-connect/", views.auto_connect_store, name="auto_connect_store"),
    path("api/wc-callback/", views.wc_callback_api, name="wc_callback_api"),
    # Shopify OAuth callback — Shopify redirects merchants here with
    # code, hmac, shop and state. Handler verifies the HMAC, exchanges
    # the code for a long-lived access token, and creates the Store.
    path("api/shopify-callback/", views.shopify_callback_api, name="shopify_callback_api"),
    path("api/check-connected/", views.check_connected_api, name="check_connected_api"),
    path("api/<int:store_id>/health/", views.store_health_api, name="store_health_api"),
    path("connect/success/", views.connect_success_page, name="connect_success_page"),
]