"""URLs for testing tracking views on dropsigma.com/track/...

Production traffic on track.dropsigma.com is routed by
``TrackingHostMiddleware`` — these URL patterns just give us a
fallback path on the main domain for QA and the rare case where
someone bookmarks dropsigma.com/track/<id>/.
"""
from django.urls import path
from . import views

app_name = "tracking_public"

urlpatterns = [
    path("",                          views.tracking_landing,    name="landing"),
    path("api/lookup/",               views.tracking_lookup_api, name="lookup_api"),
    path("<str:tracking_id>/",        views.tracking_detail,     name="detail"),
]
