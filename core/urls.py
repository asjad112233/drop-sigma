from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.conf import settings
from django.conf.urls.static import static


def dashboard_page(request):
    return render(request, "dashboard.html")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", dashboard_page, name="dashboard"),

    # Apps
    path("stores/", include("stores.urls")),
    path("orders/", include("orders.urls")),
    path("teamapp/", include("teamapp.urls")),
    path("emails/", include("emails.urls")),
    path("vendors/api/", include("vendors.urls")),
    path("vendor/", include("vendors.portal_urls")),
    path("employee/", include("teamapp.portal_urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)