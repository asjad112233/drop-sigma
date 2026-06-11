"""Host-based dispatcher for track.dropsigma.com.

The Django project already serves ``dropsigma.com`` (the dashboard) and
``www.dropsigma.com``. ``track.dropsigma.com`` is the third hostname,
pointed at the same Railway service via DNS. Rather than spinning up a
second Django project, this middleware intercepts requests whose Host
header is ``track.dropsigma.com`` (or its local-dev / Railway-internal
equivalents) and re-routes them onto the tracking views.

URL layout served on track.dropsigma.com:

  GET  /                  → landing form
  GET  /<tracking_id>/    → detail page (or "not associated")
  POST /api/lookup/       → JSON lookup (used by landing form)

Anything that starts with ``/static/`` is passed through untouched so
that the icon image, fonts, etc continue to load.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Hostnames we treat as "track.dropsigma.com" — production, and a few
# local-dev / preview equivalents so the same code path works without
# extra config in non-prod environments.
_TRACK_HOSTS = {
    "track.dropsigma.com",
    "track.dropsigma.com:443",
}


# Tracking IDs are case-sensitive on render but case-INSENSITIVE on
# lookup. Allowed character set is conservative: alphanumerics, dash,
# underscore, dot, slash. Length 4–80 to absorb both 8-char Yuntrack
# IDs and 40-char DHL ones, but reject anything obviously not a
# tracking number (which prevents bots from sweeping arbitrary URL
# patterns).
_TRACKING_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{4,80}$")


class TrackingHostMiddleware:
    """Routes track.dropsigma.com traffic to the tracking views.

    Must be installed AFTER Django's CommonMiddleware (so the host
    header is parsed) and BEFORE any auth/CSRF middleware that would
    treat the tracking views as protected resources. The recommended
    insertion point in ``settings.MIDDLEWARE`` is right after
    ``django.middleware.common.CommonMiddleware``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().lower()
        if not self._is_tracking_host(host):
            return self.get_response(request)

        # Static, media, healthcheck paths bypass tracking routing so
        # the icon / CSS / WhiteNoise continue to work.
        if request.path.startswith(("/static/", "/media/", "/healthz")):
            return self.get_response(request)

        path = request.path.strip("/")

        # Landing
        if path == "" or path == "index" or path == "index.html":
            from .views import tracking_landing
            return tracking_landing(request)

        # JSON lookup API
        if path == "api/lookup" or path == "api/lookup/":
            from .views import tracking_lookup_api
            return tracking_lookup_api(request)

        # Tracking ID path
        first_seg = path.split("/", 1)[0]
        if _TRACKING_PATTERN.match(first_seg):
            from .views import tracking_detail
            return tracking_detail(request, tracking_id=first_seg)

        # Anything else on the tracking host falls through to the
        # not-found page so we don't accidentally expose the dashboard.
        from django.shortcuts import render
        return render(request, "tracking_public/not_found.html", {
            "tracking_id": first_seg or "",
        }, status=404)

    @staticmethod
    def _is_tracking_host(host: str) -> bool:
        # exact match (production)
        if host in _TRACK_HOSTS:
            return True
        # local-dev: `track.localhost`, `track.dropsigma.local`, etc.
        if host.startswith("track."):
            return True
        return False
