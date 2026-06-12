"""Drop Sigma Ops Portal URLs — mounted at /ops/."""
from django.urls import path
from . import views
from . import views_orders
from . import views_suppliers
from . import views_team
from . import views_chat
from . import views_catalog
from . import invitations as views_invite


urlpatterns = [
    # ── SPA shell ────────────────────────────────────────────────────
    path("",                 views.ops_dashboard,  name="ops_dashboard"),

    # ── Invitation acceptance (public — token-gated) ─────────────────
    path("invite/accept/<uuid:token>/",        views_invite.accept_invitation_view,
         name="ops_invite_accept"),
    path("invite/accept/<uuid:token>/submit/", views_invite.submit_invitation_accept_api,
         name="ops_invite_accept_submit"),

    # ── Design previews (mock data, for design review only) ──────────
    path("wallet-preview/",               views.ops_wallet_preview,               name="ops_wallet_preview"),
    path("supplier-form-preview/",        views.ops_supplier_form_preview,        name="ops_supplier_form_preview"),
    path("product-detail-preview/",       views.ops_tenant_product_detail_preview, name="ops_tenant_product_detail_preview"),

    # ── Core / shared APIs ───────────────────────────────────────────
    path("api/overview/",    views.api_overview,   name="ops_api_overview"),
    path("api/me/",          views.api_me,         name="ops_api_me"),

    # ── Orders ───────────────────────────────────────────────────────
    path("api/orders/",                  views_orders.api_orders_list,        name="ops_api_orders_list"),
    path("api/order/<int:order_id>/",    views_orders.api_order_detail,       name="ops_api_order_detail"),
    path("api/order/<int:order_id>/quote/",    views_orders.api_order_quote,    name="ops_api_order_quote"),
    path("api/order/<int:order_id>/assign/",   views_orders.api_order_assign,   name="ops_api_order_assign"),
    path("api/order/<int:order_id>/procure/",  views_orders.api_order_procure,  name="ops_api_order_procure"),
    path("api/order/<int:order_id>/qc/",       views_orders.api_order_qc,       name="ops_api_order_qc"),
    path("api/order/<int:order_id>/ship/",     views_orders.api_order_ship,     name="ops_api_order_ship"),
    path("api/order/<int:order_id>/deliver/",  views_orders.api_order_deliver,  name="ops_api_order_deliver"),
    path("api/order/<int:order_id>/cancel/",   views_orders.api_order_cancel,   name="ops_api_order_cancel"),
    path("api/order/<int:order_id>/activity/", views_orders.api_order_activity, name="ops_api_order_activity"),
    path("api/order/<int:order_id>/messages/", views_orders.api_order_message_list, name="ops_api_order_messages"),
    path("api/order/<int:order_id>/message/",  views_orders.api_order_message_send, name="ops_api_order_message"),
    path("api/supplier-match/",                views_orders.api_supplier_match,     name="ops_api_supplier_match"),

    # ── Suppliers ────────────────────────────────────────────────────
    path("api/suppliers/",                       views_suppliers.api_suppliers_list,     name="ops_api_suppliers_list"),
    path("api/suppliers/create/",                views_suppliers.api_supplier_create,    name="ops_api_supplier_create"),
    path("api/supplier/<int:supplier_id>/",      views_suppliers.api_supplier_detail,    name="ops_api_supplier_detail"),
    path("api/supplier/<int:supplier_id>/update/", views_suppliers.api_supplier_update,  name="ops_api_supplier_update"),
    path("api/supplier/<int:supplier_id>/products/",       views_suppliers.api_supplier_products, name="ops_api_supplier_products"),
    path("api/supplier/<int:supplier_id>/products/create/", views_suppliers.api_supplier_product_create, name="ops_api_supplier_product_create"),
    path("api/supplier-product/<int:product_id>/update/",   views_suppliers.api_supplier_product_update, name="ops_api_supplier_product_update"),
    path("api/supplier-product/<int:product_id>/delete/",   views_suppliers.api_supplier_product_delete, name="ops_api_supplier_product_delete"),

    # ── Import from tenant store (bulk catalog seed) ─────────────────
    path("api/tenant-stores/",                              views_suppliers.api_tenant_stores_list,     name="ops_api_tenant_stores_list"),
    path("api/tenant-store/<int:store_id>/products/",       views_suppliers.api_tenant_store_products,  name="ops_api_tenant_store_products"),
    path("api/supplier/<int:supplier_id>/import-products/", views_suppliers.api_supplier_import_products, name="ops_api_supplier_import_products"),

    # ── Tenant chat inbox ────────────────────────────────────────────
    path("api/chats/",                       views_chat.api_chats_list,    name="ops_api_chats_list"),
    path("api/chat/<int:conv_id>/messages/", views_chat.api_chat_messages, name="ops_api_chat_messages"),
    path("api/chat/<int:conv_id>/send/",     views_chat.api_chat_send,     name="ops_api_chat_send"),
    # Smart-link resolver — scopes orders to the conversation's tenant so
    # ops staff see the same matches the tenant would see in their chat.
    path("api/chat/<int:conv_id>/lookup/",   views_chat.api_chat_lookup,   name="ops_api_chat_lookup"),
    # Read-only mirror of the tenant SP order modal (Drop Sigma quote /
    # shipping / tracking) — used by chat smart-link chips so ops staff
    # see the same view the tenant sees instead of the back-office page.
    path("api/chat/<int:conv_id>/order/<int:order_id>/",
         views_chat.api_chat_order_detail, name="ops_api_chat_order_detail"),

    # ── Team & roles ─────────────────────────────────────────────────
    path("api/team/",         views_team.api_team_list,    name="ops_api_team_list"),
    path("api/team/create/",  views_team.api_team_create,  name="ops_api_team_create"),
    path("api/team/<int:member_id>/update/", views_team.api_team_update, name="ops_api_team_update"),
    path("api/roles/",        views_team.api_team_roles,   name="ops_api_team_roles"),

    # ── Dedicated Sourcing Manager (per-tenant) ──────────────────────
    path("api/my-tenants/",   views_team.api_my_tenants,   name="ops_api_my_tenants"),
    path("api/tenant/<int:tenant_id>/reassign-manager/",
         views_team.api_reassign_manager, name="ops_api_reassign_manager"),

    # ── Master product catalog (cross-tenant view) ───────────────────
    path("api/master-catalog/",                  views_catalog.api_master_catalog_list,    name="ops_api_master_catalog_list"),
    path("api/master-catalog/sku/<str:sku>/",    views_catalog.api_master_catalog_sku,     name="ops_api_master_catalog_sku"),
    path("api/master-catalog/stats/",            views_catalog.api_master_catalog_stats,   name="ops_api_master_catalog_stats"),
    path("api/master-catalog/filters/",          views_catalog.api_master_catalog_filters, name="ops_api_master_catalog_filters"),
    path("api/master-catalog/sync/",             views_catalog.api_master_catalog_sync,    name="ops_api_master_catalog_sync"),
    path("api/master-catalog/sync-store/<int:store_id>/", views_catalog.api_master_catalog_sync_store, name="ops_api_master_catalog_sync_store"),
]
