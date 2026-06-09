import threading
import urllib.request
import json
import re
from django.contrib.auth.models import User
from django.utils import timezone
import datetime


class ImpersonationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        impersonate_id = request.session.get("impersonate_id")
        if (
            impersonate_id
            and request.user.is_authenticated
            and request.user.is_superuser
            and not request.path.startswith("/superadmin/")
            and not request.path.startswith("/admin/")
        ):
            try:
                impersonated = User.objects.get(pk=impersonate_id)
                request.user = impersonated
                request._impersonating = True
            except User.DoesNotExist:
                request.session.pop("impersonate_id",    None)
                request.session.pop("impersonate_name",  None)
                request.session.pop("impersonate_email", None)

        response = self.get_response(request)

        # Log IP for all authenticated users on non-admin paths
        if (
            request.user.is_authenticated
            and not request.path.startswith("/static/")
            and not request.path.startswith("/media/")
            and not request.path.startswith("/superadmin/")
            and not request.path.startswith("/admin/")
        ):
            _log_ip_async(request)

        return response


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _parse_ua(ua):
    browser, os_name, device_type = "Unknown", "Unknown", "desktop"
    ua = ua or ""
    if "Edg/" in ua:           browser = "Edge"
    elif "Chrome/" in ua:      browser = "Chrome"
    elif "Firefox/" in ua:     browser = "Firefox"
    elif "Safari/" in ua:      browser = "Safari"

    if "Android" in ua:        os_name = "Android";  device_type = "mobile"
    elif "iPhone" in ua:       os_name = "iOS";       device_type = "mobile"
    elif "iPad" in ua:         os_name = "iPadOS";    device_type = "tablet"
    elif "Windows" in ua:      os_name = "Windows"
    elif "Mac OS X" in ua:     os_name = "macOS"
    elif "Linux" in ua:        os_name = "Linux"
    return browser, os_name, device_type


_LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

def _is_local(ip):
    return ip in _LOCAL_IPS or ip.startswith("192.168.") or ip.startswith("10.")

def _geo_lookup(ip):
    if _is_local(ip):
        return {
            "country": "Local / Dev", "country_code": "LO",
            "region": "Localhost", "city": "Localhost",
            "isp": "Local Network", "lat": None, "lng": None,
        }
    try:
        with urllib.request.urlopen(
            f"http://ip-api.com/json/{ip}?fields=country,countryCode,regionName,city,isp,lat,lon,status",
            timeout=4
        ) as r:
            data = json.loads(r.read())
        if data.get("status") == "success":
            return {
                "country":      data.get("country", ""),
                "country_code": data.get("countryCode", ""),
                "region":       data.get("regionName", ""),
                "city":         data.get("city", ""),
                "isp":          data.get("isp", ""),
                "lat":          data.get("lat"),
                "lng":          data.get("lon"),
            }
    except Exception:
        pass
    return {}


def _log_ip_async(request):
    user_id  = request.user.pk
    ip       = _get_client_ip(request)
    ua       = request.META.get("HTTP_USER_AGENT", "")
    browser, os_name, device_type = _parse_ua(ua)

    def _run():
        # CRITICAL: this thread MUST release its DB connection back to the
        # pool when it's done. Django auto-closes connections only on the
        # main request thread — background threads leak otherwise. We saw
        # this in production as `FATAL: sorry, too many clients already`
        # on 2026-06-09 once tenant traffic ramped (Railway logs).
        from django.db import connections
        try:
            from .models import UserIPLog
            threshold = timezone.now() - datetime.timedelta(minutes=15)
            latest = UserIPLog.objects.filter(user_id=user_id).order_by("-last_seen").first()

            if latest and latest.last_seen > threshold and latest.ip_address == ip:
                # Just bump last_seen
                UserIPLog.objects.filter(pk=latest.pk).update(
                    last_seen=timezone.now(),
                    browser=browser, os_name=os_name, device_type=device_type
                )
                return

            geo = _geo_lookup(ip)
            if latest and latest.ip_address == ip:
                UserIPLog.objects.filter(pk=latest.pk).update(
                    last_seen=timezone.now(),
                    browser=browser, os_name=os_name, device_type=device_type,
                    **geo
                )
            else:
                UserIPLog.objects.create(
                    user_id=user_id, ip_address=ip,
                    browser=browser, os_name=os_name, device_type=device_type,
                    **geo
                )
        except Exception:
            # Never let analytics break the request flow.
            pass
        finally:
            # Always close — even on early return or exception.
            try:
                connections.close_all()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# VisitTrackingMiddleware — site-wide visitor analytics (every page visit).
# Captures anonymous AND authenticated traffic for the Super Admin dashboard.
# ─────────────────────────────────────────────────────────────────────────────

# Paths we never log: static, media, internal APIs/webhooks, the super admin
# itself (so admin browsing doesn't pollute traffic numbers), Django admin,
# health/diagnostic probes.
_SKIP_PREFIXES = (
    "/static/", "/media/",
    "/admin/", "/superadmin/",
    "/orders/webhook/", "/emails/webhook/",
    "/orders/api/poll/", "/teamapp/chat/",
    "/emails/api/latest-event/", "/emails/api/sync-inbox/",
    "/_health", "/healthz", "/favicon",
)

# Quick bot detector (covers the most common crawlers without external lib).
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|baidu|yandex|duckduck|bing|googlebot|"
    r"facebookexternalhit|twitterbot|linkedinbot|whatsapp|telegrambot|"
    r"applebot|petalbot|semrush|ahrefs|mj12|dotbot|seokicks",
    re.IGNORECASE,
)


def _device_from_ua(ua):
    """Return (browser, os_name, device_type, is_bot)."""
    if not ua:
        return "Unknown", "Unknown", "unknown", False
    is_bot = bool(_BOT_RE.search(ua))
    if is_bot:
        # Identify the bot by name when possible
        m = _BOT_RE.search(ua)
        return (m.group(0).capitalize() if m else "Bot"), "Bot", "bot", True

    browser, os_name, device_type = "Unknown", "Unknown", "desktop"
    if "Edg/" in ua:           browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua: browser = "Opera"
    elif "Chrome/" in ua:      browser = "Chrome"
    elif "Firefox/" in ua:     browser = "Firefox"
    elif "Safari/" in ua:      browser = "Safari"

    if "Android" in ua:        os_name = "Android";  device_type = "mobile"
    elif "iPhone" in ua:       os_name = "iOS";       device_type = "mobile"
    elif "iPad" in ua:         os_name = "iPadOS";    device_type = "tablet"
    elif "Windows" in ua:      os_name = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua: os_name = "macOS"
    elif "Linux" in ua:        os_name = "Linux"
    elif "CrOS" in ua:         os_name = "ChromeOS"
    return browser, os_name, device_type, False


def _looks_local(ip):
    if not ip:
        return True
    if ip in {"127.0.0.1", "::1", "localhost"}:
        return True
    if ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                      "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                      "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                      "172.29.", "172.30.", "172.31.")):
        return True
    return False


def _resolve_geo(ip):
    """Look up geolocation for an IP, using IPGeoCache to avoid repeat API hits."""
    from .models import IPGeoCache

    if _looks_local(ip):
        return {
            "country": "Local / Dev", "country_code": "XX",
            "region": "Localhost", "city": "Localhost",
            "isp": "Local Network", "lat": None, "lng": None,
            "timezone_str": "", "is_bot": False,
        }

    # Check cache first
    try:
        cache_row = IPGeoCache.objects.filter(pk=ip).first()
        if cache_row and not cache_row.is_stale(max_age_days=7):
            return {
                "country":      cache_row.country,
                "country_code": cache_row.country_code,
                "region":       cache_row.region,
                "city":         cache_row.city,
                "isp":          cache_row.isp,
                "lat":          cache_row.lat,
                "lng":          cache_row.lng,
                "timezone_str": cache_row.timezone_str,
                "is_bot":       cache_row.is_bot,
            }
    except Exception:
        pass

    # Hit ip-api.com (free, no key, generous limits)
    try:
        with urllib.request.urlopen(
            f"http://ip-api.com/json/{ip}?fields=country,countryCode,regionName,city,isp,lat,lon,timezone,proxy,hosting,status",
            timeout=4,
        ) as r:
            data = json.loads(r.read())
        if data.get("status") == "success":
            is_bot = bool(data.get("hosting") or data.get("proxy"))
            geo = {
                "country":      data.get("country", ""),
                "country_code": data.get("countryCode", ""),
                "region":       data.get("regionName", ""),
                "city":         data.get("city", ""),
                "isp":          data.get("isp", ""),
                "lat":          data.get("lat"),
                "lng":          data.get("lon"),
                "timezone_str": data.get("timezone", ""),
                "is_bot":       is_bot,
            }
            # Persist to cache
            try:
                IPGeoCache.objects.update_or_create(
                    ip_address=ip,
                    defaults={**geo, "is_bot": is_bot},
                )
            except Exception:
                pass
            return geo
    except Exception:
        pass

    return {
        "country": "", "country_code": "", "region": "", "city": "",
        "isp": "", "lat": None, "lng": None, "timezone_str": "", "is_bot": False,
    }


def _should_track(request):
    path = request.path or ""
    if request.method != "GET":
        return False
    for prefix in _SKIP_PREFIXES:
        if path.startswith(prefix):
            return False
    # Skip JSON / XHR — only count real page views
    accept = request.META.get("HTTP_ACCEPT", "")
    if "text/html" not in accept and "*/*" not in accept:
        return False
    return True


class VisitTrackingMiddleware:
    """Log every page visit (anonymous + authenticated) to VisitLog.

    Runs after the response, on a background thread, so it never blocks the
    user's request. Geolocation is cached per-IP for 7 days.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if not _should_track(request):
            return response

        # Snapshot the request data we need (request object is per-request)
        snapshot = {
            "ip":         _get_client_ip(request),
            "ua":         request.META.get("HTTP_USER_AGENT", "")[:500],
            "path":       (request.path or "")[:500],
            "referrer":   (request.META.get("HTTP_REFERER", "") or "")[:500],
            "method":     request.method,
            "status":     response.status_code,
            "user_id":    request.user.pk if request.user.is_authenticated else None,
            "session_key": request.session.session_key or "",
        }

        def _persist():
            # Background threads MUST release their DB connection back to
            # the pool when done — see ImpersonationMiddleware._run for
            # the same fix and the 2026-06-09 "too many clients" incident.
            from django.db import connections
            try:
                from .models import VisitLog
                browser, os_name, device_type, is_bot_ua = _device_from_ua(snapshot["ua"])
                geo = _resolve_geo(snapshot["ip"])
                # Combined bot signal: UA pattern OR hosting/proxy
                is_bot = is_bot_ua or geo.get("is_bot", False)
                VisitLog.objects.create(
                    ip_address   = snapshot["ip"] or "0.0.0.0",
                    user_id      = snapshot["user_id"],
                    session_key  = snapshot["session_key"][:64],
                    country      = geo.get("country", "")[:100],
                    country_code = (geo.get("country_code") or "")[:10],
                    region       = (geo.get("region") or "")[:100],
                    city         = (geo.get("city") or "")[:100],
                    isp          = (geo.get("isp") or "")[:200],
                    lat          = geo.get("lat"),
                    lng          = geo.get("lng"),
                    timezone_str = (geo.get("timezone_str") or "")[:64],
                    path         = snapshot["path"],
                    referrer     = snapshot["referrer"],
                    method       = snapshot["method"][:10],
                    status_code  = snapshot["status"],
                    user_agent   = snapshot["ua"],
                    browser      = browser[:100],
                    os_name      = os_name[:100],
                    device_type  = device_type[:20],
                    is_bot       = is_bot,
                )
            except Exception:
                # Never let analytics break the request
                pass
            finally:
                try:
                    connections.close_all()
                except Exception:
                    pass

        threading.Thread(target=_persist, daemon=True).start()
        return response
