"""DropSigma Sourcing Partners URL routes. Mounted at /sourcing-partners/."""
from django.urls import path
from . import views

urlpatterns = [
    # ─── Discovery + chat (existing) ────────────────────────────────────
    path("api/list/",                    views.api_partner_list,        name="sp_api_list"),
    path("api/<int:pk>/",                views.api_partner_detail,      name="sp_api_detail"),
    path("api/<int:pk>/open/",           views.api_open_conversation,   name="sp_api_open"),
    path("api/conv/<int:conv_id>/send/", views.api_send_message,        name="sp_api_send"),
    path("api/conv/<int:conv_id>/poll/", views.api_poll_messages,       name="sp_api_poll"),
    # Typing-presence ping. The keystroke loop on the chat composer hits
    # this every ~3s while typing; the OPS poll then surfaces it as the
    # "tenant is typing…" pill.
    path("api/conv/<int:conv_id>/typing/", views.api_typing,            name="sp_api_typing"),
    # Tenant-wide unread summary — drives the sidebar badge + the
    # side-popup that fires on a new partner message while the user
    # isn't inside the chat thread.
    path("api/conv/unread-summary/",     views.api_conv_unread_summary, name="sp_api_conv_unread_summary"),

    # ─── Dedicated Sourcing Manager (lazy-creates assignment) ───────────
    path("api/my-manager/",              views.api_my_manager,          name="sp_api_my_manager"),

    # ─── Chat smart-link resolver (order # / SKU / customer email) ──────
    # Used by both tenant chat and ops chat to turn inline mentions into
    # clickable rows that drill into the matching order(s).
    path("api/chat-lookup/",             views.api_chat_lookup,         name="sp_api_chat_lookup"),

    # ─── Workspace summary (left rail) ──────────────────────────────────
    path("api/workspace/summary/",
         views.api_workspace_summary,        name="sp_api_workspace_summary"),

    # ─── Orders tab ─────────────────────────────────────────────────────
    path("api/<int:partner_id>/orders/",
         views.api_orders_list,              name="sp_api_orders_list"),
    path("api/<int:partner_id>/orders/create/",
         views.api_order_create,             name="sp_api_order_create"),
    path("api/order/<int:order_id>/",
         views.api_order_detail,             name="sp_api_order_detail"),
    path("api/order/<int:order_id>/accept/",
         views.api_order_accept_quote,       name="sp_api_order_accept"),
    path("api/order/<int:order_id>/reject/",
         views.api_order_reject_quote,       name="sp_api_order_reject"),
    path("api/order/<int:order_id>/cancel/",
         views.api_order_cancel,             name="sp_api_order_cancel"),

    # ─── Catalog / locked prices ────────────────────────────────────────
    path("api/<int:partner_id>/catalog/",
         views.api_catalog_list,             name="sp_api_catalog_list"),
    path("api/catalog/<int:locked_id>/toggle-auto/",
         views.api_catalog_toggle_auto,      name="sp_api_catalog_toggle_auto"),
    path("api/catalog/<int:locked_id>/renegotiate/",
         views.api_catalog_request_renegotiation,
         name="sp_api_catalog_renegotiate"),
    path("api/catalog/<int:locked_id>/renegotiate/approve/",
         views.api_catalog_approve_renegotiation,
         name="sp_api_catalog_renegotiate_approve"),
    path("api/catalog/<int:locked_id>/renegotiate/reject/",
         views.api_catalog_reject_renegotiation,
         name="sp_api_catalog_renegotiate_reject"),

    # ─── Stock tab ──────────────────────────────────────────────────────
    path("api/<int:partner_id>/stock/",
         views.api_stock_list,               name="sp_api_stock_list"),
    path("api/stock/<int:stock_id>/reorder/",
         views.api_stock_reorder,            name="sp_api_stock_reorder"),
    path("api/stock/<int:stock_id>/threshold/",
         views.api_stock_update_threshold,   name="sp_api_stock_threshold"),

    # ─── Payments tab ───────────────────────────────────────────────────
    path("api/<int:partner_id>/payments/",
         views.api_payments_list,            name="sp_api_payments_list"),

    # ─── Documents tab ──────────────────────────────────────────────────
    path("api/<int:partner_id>/documents/",
         views.api_documents_list,           name="sp_api_documents_list"),
    path("api/<int:partner_id>/documents/upload/",
         views.api_documents_upload,         name="sp_api_documents_upload"),
    path("api/documents/<int:doc_id>/delete/",
         views.api_documents_delete,         name="sp_api_documents_delete"),

    # ─── Performance tab ────────────────────────────────────────────────
    path("api/<int:partner_id>/performance/",
         views.api_performance,              name="sp_api_performance"),

    # ─── Auto-rules tab ─────────────────────────────────────────────────
    path("api/<int:partner_id>/rules/",
         views.api_rules_list,               name="sp_api_rules_list"),
    path("api/<int:partner_id>/rules/create/",
         views.api_rules_create,             name="sp_api_rules_create"),
    path("api/rules/<int:rule_id>/toggle/",
         views.api_rules_toggle,             name="sp_api_rules_toggle"),
    path("api/rules/<int:rule_id>/update/",
         views.api_rules_update,             name="sp_api_rules_update"),
    path("api/rules/<int:rule_id>/delete/",
         views.api_rules_delete,             name="sp_api_rules_delete"),

    # ─── Activity tab ───────────────────────────────────────────────────
    path("api/<int:partner_id>/activity/",
         views.api_activity_list,            name="sp_api_activity_list"),

    # ─── Reviews ────────────────────────────────────────────────────────
    path("api/<int:partner_id>/review/",
         views.api_review_submit,            name="sp_api_review_submit"),

    # ─── Aggregate ("Drop Sigma Sourcing" unified front) ────────────────
    path("api/all/overview/",     views.api_aggregate_overview,     name="sp_api_agg_overview"),
    path("api/all/orders/",       views.api_aggregate_orders,       name="sp_api_agg_orders"),
    path("api/all/catalog/",      views.api_aggregate_catalog,      name="sp_api_agg_catalog"),
    path("api/all/stock/",        views.api_aggregate_stock,        name="sp_api_agg_stock"),
    path("api/all/payments/",     views.api_aggregate_payments,     name="sp_api_agg_payments"),
    path("api/all/documents/",    views.api_aggregate_documents,    name="sp_api_agg_documents"),
    path("api/all/rules/",        views.api_aggregate_rules,        name="sp_api_agg_rules"),
    path("api/all/activity/",     views.api_aggregate_activity,     name="sp_api_agg_activity"),
    path("api/all/chat-inbox/",   views.api_aggregate_chat_inbox,   name="sp_api_agg_chat_inbox"),
    path("api/all/performance/",  views.api_aggregate_performance,  name="sp_api_agg_performance"),

    # ─── Sourcing order actions (read directly from orders.Order) ───────
    path("api/sourcing-order/<int:order_id>/",
         views.api_order_detail_sourcing,    name="sp_api_sorder_detail"),
    path("api/sourcing-order/<int:order_id>/shipping/",
         views.api_order_shipping_update,    name="sp_api_sorder_shipping"),
    path("api/sourcing-order/<int:order_id>/items/<int:item_index>/delete/",
         views.api_order_item_delete,        name="sp_api_sorder_item_delete"),
    path("api/sourcing-order/<int:order_id>/pay/",
         views.api_order_pay,                name="sp_api_sorder_pay"),
    path("api/sourcing-order/<int:order_id>/invoice/",
         views.api_order_invoice,            name="sp_api_sorder_invoice"),
    path("api/sourcing-orders/bulk-pay/",
         views.api_orders_bulk_pay,          name="sp_api_sorders_bulk_pay"),
    # Tenant-side soft delete + restore for Pending Source / Pending
    # Payment rows. Same {"order_ids": [...]} contract as bulk-pay so
    # the frontend can reuse the request shape.
    path("api/sourcing-orders/bulk-delete/",
         views.api_orders_bulk_delete,       name="sp_api_sorders_bulk_delete"),
    path("api/sourcing-orders/bulk-restore/",
         views.api_orders_bulk_restore,      name="sp_api_sorders_bulk_restore"),
    path("api/wallet/",
         views.api_wallet,                   name="sp_api_wallet"),

    # ─── Wallet top-up (Stripe one-time + PayPal) ───────────────────────
    path("api/wallet/topup/stripe/",
         views.api_wallet_topup_stripe,          name="sp_api_wallet_topup_stripe"),
    path("wallet/topup/stripe/success/",
         views.wallet_topup_stripe_success,      name="sp_wallet_topup_stripe_success"),
    path("api/wallet/topup/paypal/create/",
         views.api_wallet_topup_paypal_create,   name="sp_api_wallet_topup_paypal_create"),
    path("api/wallet/topup/paypal/capture/",
         views.api_wallet_topup_paypal_capture,  name="sp_api_wallet_topup_paypal_capture"),
]
