import threading
import urllib.request
import json
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

    t = threading.Thread(target=_run, daemon=True)
    t.start()
