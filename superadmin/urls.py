from django.urls import path
from . import views
from . import ops_workspaces as ws

urlpatterns = [
    path("",                         views.superadmin_page,     name="superadmin"),

    # ─── OPS Workspaces (multi-instance Ops Portal) ──────────────────────
    path("api/ops-workspaces/default/",                  ws.api_workspaces_default,        name="sa_ops_ws_default"),
    path("api/ops-workspaces/",                          ws.api_workspaces,                name="sa_ops_ws_list"),
    path("api/ops-workspaces/<int:pk>/",                 ws.api_workspace_detail,          name="sa_ops_ws_detail"),
    path("api/ops-workspaces/<int:pk>/assignable-tenants/", ws.api_assignable_tenants,    name="sa_ops_ws_assignable_tenants"),
    path("api/ops-workspaces/<int:pk>/tenants/",         ws.api_workspace_assign_tenant,   name="sa_ops_ws_assign_tenant"),
    path("api/ops-workspaces/<int:pk>/tenants/<int:tenant_id>/", ws.api_workspace_unassign_tenant, name="sa_ops_ws_unassign_tenant"),
    path("api/ops-workspaces/<int:pk>/tenants/<int:tenant_id>/manager/", ws.api_workspace_tenant_manager, name="sa_ops_ws_tenant_manager"),
    path("api/ops-workspaces/<int:pk>/invitations/",     ws.api_workspace_invite,          name="sa_ops_ws_invite"),
    path("api/ops-workspaces/<int:pk>/invitations/<int:iid>/resend/", ws.api_workspace_invite_resend, name="sa_ops_ws_invite_resend"),
    path("api/ops-workspaces/<int:pk>/invitations/<int:iid>/revoke/", ws.api_workspace_invite_revoke, name="sa_ops_ws_invite_revoke"),
    path("api/ops-workspaces/<int:pk>/members/<int:member_id>/", ws.api_workspace_member_remove, name="sa_ops_ws_member_remove"),
    path("api/stats/",               views.api_stats,           name="sa_stats"),
    path("api/tenants/",             views.api_tenants,         name="sa_tenants"),
    path("api/tenants/<int:pk>/",    views.api_tenant_detail,   name="sa_tenant_detail"),
    path("api/activity/",            views.api_activity,        name="sa_activity"),
    path("impersonate/<int:pk>/",    views.impersonate,         name="sa_impersonate"),
    path("exit/",                    views.exit_impersonation,  name="sa_exit"),
    path("api/coupons/",             views.api_coupons,         name="sa_coupons"),
    path("api/coupons/<int:pk>/",    views.api_coupon_detail,   name="sa_coupon_detail"),
    path("api/validate-coupon/",     views.api_validate_coupon, name="sa_validate_coupon"),
    path("api/locations/",           views.api_locations,       name="sa_locations"),
    path("api/diagnostics/",         views.api_diagnostics,     name="sa_diagnostics"),

    # ─── Site-wide Visitor Analytics ────────────────────────────────────
    path("api/visitors/overview/",   views.api_visitors_overview,   name="sa_visitors_overview"),
    path("api/visitors/list/",       views.api_visitors_list,       name="sa_visitors_list"),
    path("api/visitors/countries/",  views.api_visitors_countries,  name="sa_visitors_countries"),
    path("api/visitors/realtime/",   views.api_visitors_realtime,   name="sa_visitors_realtime"),
    path("api/visitors/map/",        views.api_visitors_map,        name="sa_visitors_map"),
    path("api/visitors/top-pages/",  views.api_visitors_top_pages,  name="sa_visitors_top_pages"),
    path("api/visitors/devices/",    views.api_visitors_devices,    name="sa_visitors_devices"),

    # ─── Platform Payment Gateways (superadmin's own Stripe + PayPal) ───
    path("api/payment-gateways/",         views.api_payment_gateways,        name="sa_payment_gateways"),
    path("api/payment-gateways/save/",    views.api_payment_gateways_save,   name="sa_payment_gateways_save"),
    path("api/payment-gateways/toggle/",  views.api_payment_gateways_toggle, name="sa_payment_gateways_toggle"),
    path("api/payment-gateways/test/",    views.api_payment_gateways_test,   name="sa_payment_gateways_test"),
]
