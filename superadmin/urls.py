from django.urls import path
from . import views

urlpatterns = [
    path("",                         views.superadmin_page,     name="superadmin"),
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
]
