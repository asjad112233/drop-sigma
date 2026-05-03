from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.models import User
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from urllib.parse import urlencode
import json
import requests as _req
import ssl

from .models import Store
from .serializers import StoreSerializer
from vendors.models import StoreVendorAssignment


def _register_webhook_for_store(store, request):
    """Silently register webhook for WooCommerce or Shopify. Never raises."""
    try:
        from stores.tunnel import get_base_url
        base = get_base_url(request=request, wait_secs=0)

        if store.platform == "woocommerce":
            from orders.services import setup_woocommerce_webhook
            delivery_url = f"{base}/orders/webhook/woocommerce/{store.id}/"
            setup_woocommerce_webhook(store, delivery_url)
        elif store.platform == "shopify":
            from orders.services import setup_shopify_webhook
            delivery_url = f"{base}/orders/webhook/shopify/{store.id}/"
            setup_shopify_webhook(store, delivery_url)
    except Exception:
        pass


def stores_page(request):
    return render(request, "dashboard.html")


@api_view(["GET"])
@permission_classes([AllowAny])
def stores_list_api(request):
    stores = Store.objects.all().order_by("-id")
    serializer = StoreSerializer(stores, many=True)
    data = serializer.data

    # Attach full-store vendor assignments to each store
    assignments = StoreVendorAssignment.objects.filter(
        store__in=stores, is_active=True
    ).select_related("vendor")
    store_vendors_map = {}
    for a in assignments:
        store_vendors_map.setdefault(a.store_id, []).append({"id": a.vendor.id, "name": a.vendor.name})

    for item in data:
        item["full_vendors"] = store_vendors_map.get(item["id"], [])

    return Response({
        "success": True,
        "count": len(data),
        "stores": data
    })


@api_view(["POST"])
@permission_classes([AllowAny])
def create_store_api(request):
    name = request.data.get("name")
    platform = request.data.get("platform")
    store_url = request.data.get("store_url")
    api_key = request.data.get("api_key")
    api_secret = request.data.get("api_secret")
    access_token = request.data.get("access_token")

    if not name or not platform or not store_url:
        return Response({
            "success": False,
            "message": "Name, platform and store URL are required."
        }, status=400)

    user = request.user if request.user.is_authenticated else User.objects.filter(is_superuser=True).first()

    store = Store.objects.create(
        user=user,
        name=name,
        platform=platform,
        store_url=store_url,
        api_key=api_key,
        api_secret=api_secret,
        access_token=access_token,
    )

    _register_webhook_for_store(store, request)

    return Response({
        "success": True,
        "store": StoreSerializer(store).data
    })


# ✅ STEP 1: Start WooCommerce authorization
@api_view(["POST"])
@permission_classes([AllowAny])
def auto_connect_store(request):
    name = request.data.get("name")
    platform = request.data.get("platform")
    store_url = request.data.get("store_url")

    if not platform or not store_url:
        return Response({
            "success": False,
            "message": "Platform and Store URL are required."
        }, status=400)

    if platform.lower() != "woocommerce":
        return Response({
            "success": False,
            "message": "Auto connect currently supports WooCommerce only."
        }, status=400)

    store_url = store_url.rstrip("/")

    if not name:
        name = store_url.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    from stores.tunnel import get_base_url
    base_url = get_base_url(request=request, wait_secs=0)

    user_id_data = {
        "name": name,
        "store_url": store_url,
    }

    params = {
        "app_name": "VendorFlow AI",
        "scope": "read_write",
        "user_id": json.dumps(user_id_data),
        "return_url": f"{base_url}/stores/connect/success/",
        "callback_url": f"{base_url}/stores/api/wc-callback/",
    }

    auth_url = f"{store_url}/wc-auth/v1/authorize?{urlencode(params)}"

    return Response({
        "success": True,
        "auth_url": auth_url
    })


# ✅ STEP 2: WooCommerce sends keys here
@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def wc_callback_api(request):
    consumer_key = request.data.get("consumer_key")
    consumer_secret = request.data.get("consumer_secret")
    key_id = request.data.get("key_id")
    user_id_raw = request.data.get("user_id")

    try:
        user_data = json.loads(user_id_raw) if user_id_raw else {}
    except Exception:
        user_data = {}

    name = user_data.get("name", "WooCommerce Store")
    store_url = user_data.get("store_url")

    if not consumer_key or not consumer_secret or not store_url:
        return Response({
            "success": False,
            "message": "WooCommerce callback missing required data."
        }, status=400)

    user = User.objects.filter(is_superuser=True).first()

    store, created = Store.objects.update_or_create(
        store_url=store_url,
        defaults={
            "user": user,
            "name": name,
            "platform": "woocommerce",
            "api_key": consumer_key,
            "api_secret": consumer_secret,
            "access_token": str(key_id) if key_id else "",
            "is_active": True,
        }
    )

    _register_webhook_for_store(store, request)

    return Response({
        "success": True,
        "message": "WooCommerce store connected successfully.",
        "store_id": store.id,
        "created": created
    })


# ✅ SUCCESS REDIRECT PAGE — WooCommerce redirects user here with credentials in query params
def connect_success_page(request):
    consumer_key = request.GET.get("consumer_key", "")
    consumer_secret = request.GET.get("consumer_secret", "")
    key_id = request.GET.get("key_id", "")
    user_id_raw = request.GET.get("user_id", "")

    if consumer_key and consumer_secret and user_id_raw:
        try:
            user_data = json.loads(user_id_raw)
        except Exception:
            user_data = {}

        name = user_data.get("name", "WooCommerce Store")
        store_url = user_data.get("store_url", "")

        if store_url:
            user = request.user if request.user.is_authenticated else User.objects.filter(is_superuser=True).first()
            store, _ = Store.objects.update_or_create(
                store_url=store_url,
                defaults={
                    "user": user,
                    "name": name,
                    "platform": "woocommerce",
                    "api_key": consumer_key,
                    "api_secret": consumer_secret,
                    "access_token": str(key_id) if key_id else "",
                    "is_active": True,
                }
            )
            _register_webhook_for_store(store, request)

    return redirect("/?section=stores&connected=1")


# ✅ STEP 3: Frontend polls this to know when store connected
@api_view(["GET"])
@permission_classes([AllowAny])
def check_connected_api(request):
    store_url = request.GET.get("store_url", "").rstrip("/")
    if not store_url:
        return Response({"connected": False})
    store = Store.objects.filter(store_url=store_url, api_key__isnull=False, is_active=True).exclude(api_key="").first()
    if store:
        return Response({"connected": True, "store_id": store.id, "store_name": store.name})
    return Response({"connected": False})


def _diagnose_store(store):
    """
    Attempt to reach the store API and return a detailed diagnosis dict.
    Returns: { online, issue, title, message, fix }
    """
    if store.platform == "woocommerce":
        url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/"
        kwargs = {"auth": (store.api_key, store.api_secret), "timeout": 10}
    elif store.platform == "shopify":
        url = f"{store.store_url.rstrip('/')}/admin/api/2024-01/shop.json"
        headers = {"Content-Type": "application/json"}
        if store.access_token:
            headers["X-Shopify-Access-Token"] = store.access_token
            kwargs = {"headers": headers, "timeout": 10}
        else:
            kwargs = {"headers": headers, "auth": (store.api_key, store.api_secret), "timeout": 10}
    else:
        return {"online": False, "issue": "unsupported", "title": "Platform Not Supported",
                "message": "This platform does not support health checks yet.", "fix": ""}

    try:
        r = _req.get(url, **kwargs)

        if r.status_code == 200:
            return {"online": True, "issue": None, "title": "Store Online",
                    "message": "Store API is reachable and responding correctly.", "fix": ""}

        if r.status_code in (401, 403):
            return {"online": False, "issue": "auth",
                    "title": "Invalid API Credentials",
                    "message": f"The store responded with HTTP {r.status_code}. Your API key or secret is incorrect or has been revoked.",
                    "fix": "Go to your store's admin panel and regenerate API keys, then update them here."}

        if r.status_code == 404:
            return {"online": False, "issue": "not_found",
                    "title": "API Endpoint Not Found",
                    "message": f"HTTP 404 — The API endpoint was not found at:\n{url}",
                    "fix": "Make sure WooCommerce REST API is enabled under WooCommerce → Settings → Advanced → REST API, or verify the store URL is correct."}

        if r.status_code >= 500:
            return {"online": False, "issue": "server_error",
                    "title": "Store Server Error",
                    "message": f"The store's server returned HTTP {r.status_code}. The server is experiencing internal issues.",
                    "fix": "Contact your hosting provider or check your server error logs. This is a server-side issue, not an API key problem."}

        return {"online": False, "issue": "unexpected",
                "title": "Unexpected Response",
                "message": f"The store returned an unexpected HTTP status: {r.status_code}.",
                "fix": "Check the store URL and ensure the API is properly configured."}

    except _req.exceptions.SSLError as e:
        err = str(e).lower()
        if "certificate has expired" in err or "certificate verify failed" in err:
            detail = "Your SSL certificate has expired."
        elif "self signed" in err or "self-signed" in err:
            detail = "Your store is using a self-signed SSL certificate."
        elif "hostname mismatch" in err or "hostname" in err:
            detail = "The SSL certificate does not match the domain name."
        else:
            detail = "An SSL/TLS handshake error occurred."
        return {"online": False, "issue": "ssl",
                "title": "SSL Certificate Error",
                "message": f"{detail}\n\nThe secure connection to your store could not be established.",
                "fix": "Renew or install a valid SSL certificate from your hosting provider (e.g. Let's Encrypt). Until fixed, the store API will remain unreachable."}

    except _req.exceptions.ConnectionError as e:
        err = str(e).lower()
        if "name or service not known" in err or "getaddrinfo failed" in err or "nodename nor servname" in err:
            return {"online": False, "issue": "dns",
                    "title": "Domain Not Found (DNS Error)",
                    "message": f"The domain could not be resolved. Either the domain does not exist, DNS records are misconfigured, or the domain has expired.",
                    "fix": "Check that the domain name is spelled correctly and that your DNS records are pointing to the correct server. Contact your domain registrar if needed."}
        if "connection refused" in err:
            return {"online": False, "issue": "refused",
                    "title": "Connection Refused",
                    "message": "The server actively refused the connection. The web server may be stopped or a firewall is blocking access.",
                    "fix": "Restart your web server (Apache/Nginx) or check your firewall rules. Contact your hosting provider if the issue persists."}
        return {"online": False, "issue": "offline",
                "title": "Store Unreachable",
                "message": "Cannot connect to the store server. The server may be down, restarting, or experiencing a network outage.",
                "fix": "Wait a few minutes and try again. If the problem continues, contact your hosting provider to check server status."}

    except _req.exceptions.Timeout:
        return {"online": False, "issue": "timeout",
                "title": "Connection Timed Out",
                "message": "The store server did not respond within 10 seconds. It may be overloaded or experiencing high traffic.",
                "fix": "Try again in a few minutes. If timeouts persist, check your server's performance or upgrade your hosting plan."}

    except Exception as e:
        return {"online": False, "issue": "unknown",
                "title": "Unknown Error",
                "message": f"An unexpected error occurred while connecting to the store:\n{str(e)[:200]}",
                "fix": "Check the store URL is correct and the server is running."}


# ✅ STORE HEALTH CHECK — real API ping with diagnosis
@api_view(["GET"])
@permission_classes([AllowAny])
def store_health_api(request, store_id):
    store = get_object_or_404(Store, id=store_id)
    result = _diagnose_store(store)
    return Response({"success": True, **result})


# 🔥 DELETE STORE
@api_view(["POST"])
@permission_classes([AllowAny])
def delete_store_api(request, store_id):
    store = get_object_or_404(Store, id=store_id)
    store.delete()

    return Response({
        "success": True,
        "message": "Store deleted successfully"
    })