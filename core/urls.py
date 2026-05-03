from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.dashboard_page, name="dashboard"),
    path("login/", views.admin_login_page, name="admin_login"),
    path("logout/", views.admin_logout_view, name="admin_logout"),

    # Apps
    path("stores/", include("stores.urls")),
    path("orders/", include("orders.urls")),
    path("teamapp/", include("teamapp.urls")),
    path("emails/", include("emails.urls")),
    path("vendors/api/", include("vendors.urls")),
    path("vendor/", include("vendors.portal_urls")),
    path("employee/", include("teamapp.portal_urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
