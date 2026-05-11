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
]
