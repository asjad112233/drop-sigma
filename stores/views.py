from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.models import User
from django.conf import settings
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
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

# Signer for the WooCommerce OAuth `user_id` parameter. We sign a compact
# payload (user_pk + store_url + name) so we don't have to push JSON through
# the URL — WC's nonce verification breaks when the user_id has spaces /
# quotes / braces that the browser or WC plugin may re-encode mid-flow.
_WC_OAUTH_SIGNER = TimestampSigner(salt="wc-oauth-state-v1")
_WC_OAUTH_MAX_AGE = 30 * 60  # 30 minutes — plenty for a user to approve


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


def _kickoff_initial_sync(store, days=30):
    """
    Trigger a non-blocking initial order sync right after a store is connected.
    Fetches the last `days` days of orders so the tenant sees data immediately.
    Failures are swallowed (logged) — the connect response must not block on this.
    """
    import threading, logging
    log = logging.getLogger(__name__)

    def _run():
        try:
            from datetime import datetime, timedelta
            after_iso = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            if store.platform == "woocommerce":
                from orders.services import sync_woocommerce_orders
                count = sync_woocommerce_orders(store, after=after_iso)
            elif store.platform == "shopify":
                from orders.services import sync_shopify_orders
                count = sync_shopify_orders(store, after=after_iso)
            else:
                return
            log.info(f"Initial sync for store {store.id} ({store.name}): {count} orders fetched")
        except Exception as e:
            log.error(f"Initial sync failed for store {store.id}: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def stores_page(request):
    return render(request, "dashboard.html")


@api_view(["GET"])
@permission_classes([AllowAny])
def stores_list_api(request):
    # Every account — including superusers — only sees its own stores in the
    # tenant dashboard. Cross-tenant management belongs in /superadmin/, not
    # the normal /dashboard/, so a misclick can't cascade-delete a tenant's data.
    if request.user.is_authenticated:
        stores = Store.objects.filter(user=request.user).order_by("-id")
    else:
        stores = Store.objects.none()
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

    # Require auth — never silently attach to the first superuser, which would
    # leak the store into the wrong tenant's dashboard.
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)
    user = request.user

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

    # If the user previously deleted a store with AI training preserved,
    # restore it now (same URL) OR discard it (different URL = auto-reset).
    ai_action = None
    try:
        from emails.services import consume_pending_ai_training
        ai_action = consume_pending_ai_training(user, store)
    except Exception:
        ai_action = None

    return Response({
        "success": True,
        "store": StoreSerializer(store).data,
        "ai_training_action": ai_action,  # "restored" | "reset" | None
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

    # Require auth — the callback uses request.user identity transitively.
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Login required."}, status=401)

    # Sign a compact state token instead of stuffing JSON into `user_id`.
    # WC's wc-auth plugin computes a nonce against the exact querystring it
    # initially saw; when the value carries spaces / quotes / braces those can
    # get re-encoded between authorize → access_granted (especially when the
    # admin clicks "Approve" and WC builds the redirect URL itself), and the
    # nonce verification fails with "Invalid nonce verification".
    state_token = _WC_OAUTH_SIGNER.sign_object({
        "name":      name,
        "store_url": store_url,
        "user_pk":   request.user.pk,
    })

    params = {
        "app_name":     "VendorFlow AI",
        "scope":        "read_write",
        "user_id":      state_token,
        "return_url":   f"{base_url}/stores/connect/success/",
        "callback_url": f"{base_url}/stores/api/wc-callback/",
    }

    auth_url = f"{store_url}/wc-auth/v1/authorize?{urlencode(params)}"

    return Response({
        "success": True,
        "auth_url": auth_url
    })


# ✅ STEP 2: WooCommerce sends keys here
@csrf_exempt
@api_view(["POST", "GET"])
@permission_classes([AllowAny])
def wc_callback_api(request):
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"WC callback received — method={request.method} data={dict(request.data)}")

    if request.method == "GET":
        return Response({"status": "callback endpoint active"})

    consumer_key    = request.data.get("consumer_key")
    consumer_secret = request.data.get("consumer_secret")
    key_id          = request.data.get("key_id")
    user_id_raw     = request.data.get("user_id", "")

    logger.info(f"WC callback received: ck={bool(consumer_key)} cs={bool(consumer_secret)} user_id_len={len(user_id_raw or '')}")

    # `user_id` is now a TimestampSigner token (current flow). Legacy flows
    # may still carry a JSON blob (in-flight requests started before the
    # nonce-fix deploy), so we try the signed form first and fall through
    # to the old JSON parse for compat.
    user_data = {}
    if user_id_raw:
        try:
            user_data = _WC_OAUTH_SIGNER.unsign_object(user_id_raw, max_age=_WC_OAUTH_MAX_AGE)
        except SignatureExpired:
            logger.warning("WC callback: signed state expired")
            return Response({
                "success": False,
                "message": "OAuth state expired. Please reconnect from the dashboard."
            }, status=400)
        except BadSignature:
            # Either tampering OR a legacy JSON payload from before the fix.
            try:
                user_data = json.loads(user_id_raw)
            except Exception:
                user_data = {}

    name      = user_data.get("name", "WooCommerce Store")
    store_url = user_data.get("store_url", "").rstrip("/")
    user_pk   = user_data.get("user_pk")

    logger.info(f"WC callback parsed: name={name!r} store_url={store_url!r} user_pk={user_pk}")

    if not consumer_key or not consumer_secret or not store_url:
        logger.error(f"WC callback missing required data")
        return Response({
            "success": False,
            "message": "WooCommerce callback missing required data."
        }, status=400)

    # Associate store with the correct user. user_pk comes from our own OAuth
    # state, so trust it — no is_staff filter (regular tenants aren't staff and
    # would otherwise fall through to the superuser fallback, leaking the store
    # into the wrong account).
    user = None
    if user_pk:
        user = User.objects.filter(pk=user_pk).first()
    if not user:
        logger.error(f"WC callback: no user matched user_pk={user_pk!r}, store will not be saved")
        return Response({
            "success": False,
            "message": "Could not match the OAuth flow to a user. Please reconnect from the dashboard."
        }, status=400)

    try:
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
        logger.info(f"WC store saved: {store.id} created={created}")
    except Exception as e:
        logger.error(f"WC callback DB error: {e}")
        return Response({"success": False, "message": str(e)}, status=500)

    _register_webhook_for_store(store, request)
    _kickoff_initial_sync(store)

    # Same restore-or-reset path as the manual create endpoint.
    ai_action = None
    if created:
        try:
            from emails.services import consume_pending_ai_training
            ai_action = consume_pending_ai_training(user, store)
        except Exception:
            ai_action = None

    return Response({
        "success": True,
        "message": "WooCommerce store connected successfully.",
        "store_id": store.id,
        "created": created,
        "ai_training_action": ai_action,
    })


# ✅ SUCCESS REDIRECT PAGE — WooCommerce redirects user here with credentials in query params
def connect_success_page(request):
    consumer_key = request.GET.get("consumer_key", "")
    consumer_secret = request.GET.get("consumer_secret", "")
    key_id = request.GET.get("key_id", "")
    user_id_raw = request.GET.get("user_id", "")

    new_store_id = None
    new_store_name = ""

    if consumer_key and consumer_secret and user_id_raw:
        try:
            user_data = json.loads(user_id_raw)
        except Exception:
            user_data = {}

        name = user_data.get("name", "WooCommerce Store")
        store_url = user_data.get("store_url", "")

        if store_url:
            # Resolve owner: prefer the logged-in browser session, fall back to
            # the user_pk we stamped into the OAuth state. Never silently
            # attach to the first superuser — that's how stores leak into the
            # wrong tenant's dashboard.
            user_pk = user_data.get("user_pk")
            user = request.user if request.user.is_authenticated else None
            if not user and user_pk:
                user = User.objects.filter(pk=user_pk).first()
            if not user:
                return render(request, "dashboard.html", {
                    "connect_error": "Could not match this connection to your account. Please reconnect from the dashboard while logged in."
                })
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
            new_store_id = store.id
            new_store_name = store.name
            _register_webhook_for_store(store, request)
            _kickoff_initial_sync(store)

    # Redirect straight to /dashboard/ so the homepage redirect doesn't strip our query params.
    # Include connected=1 + connected_store_id so the success modal can target the right store.
    qs_parts = ["section=stores", "connected=1"]
    if new_store_id:
        qs_parts.append(f"connected_store_id={new_store_id}")
    if new_store_name:
        from urllib.parse import quote
        qs_parts.append(f"connected_store_name={quote(new_store_name)}")
    return redirect(f"/dashboard/?{'&'.join(qs_parts)}")


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


def _http_get_subprocess(url, auth=None, headers=None, timeout=12):
    """
    Make an HTTP GET in a fresh subprocess to bypass stale DNS state
    in long-running server processes (macOS mDNSResponder bug).
    Returns (status_code, None) on success, raises on error.
    """
    import subprocess, sys, base64, json as _json
    payload = json.dumps({"url": url, "auth": list(auth) if auth else None, "headers": headers or {}, "timeout": timeout})
    script = (
        "import sys,json,requests;"
        "d=json.loads(sys.stdin.read());"
        "r=requests.get(d['url'],auth=tuple(d['auth']) if d['auth'] else None,headers=d['headers'],timeout=d['timeout']);"
        "print(r.status_code)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=payload, capture_output=True, text=True, timeout=timeout + 3
    )
    if proc.returncode != 0:
        raise Exception(proc.stderr.strip() or "subprocess failed")
    return int(proc.stdout.strip()), None


def _diagnose_store(store):
    """
    Attempt to reach the store API and return a detailed diagnosis dict.
    Returns: { online, issue, title, message, fix }
    """
    if store.platform == "woocommerce":
        url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/"
        auth = (store.api_key, store.api_secret)
        req_headers = None
    elif store.platform == "shopify":
        url = f"{store.store_url.rstrip('/')}/admin/api/2024-01/shop.json"
        req_headers = {"Content-Type": "application/json"}
        if store.access_token:
            req_headers["X-Shopify-Access-Token"] = store.access_token
            auth = None
        else:
            auth = (store.api_key, store.api_secret)
    else:
        return {"online": False, "issue": "unsupported", "title": "Platform Not Supported",
                "message": "This platform does not support health checks yet.", "fix": ""}

    # Try direct request first; fall back to subprocess if DNS fails in this process
    def _do_request():
        kwargs = {"timeout": 10}
        if auth:
            kwargs["auth"] = auth
        if req_headers:
            kwargs["headers"] = req_headers
        try:
            r = _req.get(url, **kwargs)
            return r.status_code, None
        except _req.exceptions.ConnectionError as e:
            err = str(e).lower()
            if "nodename nor servname" in err or "getaddrinfo failed" in err or "name or service not known" in err:
                # Stale DNS in this process — retry via subprocess with fresh DNS
                try:
                    return _http_get_subprocess(url, auth=auth, headers=req_headers)
                except subprocess.TimeoutExpired:
                    raise _req.exceptions.Timeout()
                except Exception as sub_e:
                    sub_msg = str(sub_e).lower()
                    if "nodename nor servname" in sub_msg or "getaddrinfo" in sub_msg or "name or service" in sub_msg:
                        raise _req.exceptions.ConnectionError(sub_e)
                    raise _req.exceptions.ConnectionError(sub_e)
            raise

    try:
        import subprocess
        status_code, _ = _do_request()

        if status_code in range(200, 300):
            return {"online": True, "issue": None, "title": "Store Online",
                    "message": "Store API is reachable and responding correctly.", "fix": ""}

        if status_code in (401, 403):
            return {"online": False, "issue": "auth",
                    "title": "Invalid API Credentials",
                    "message": f"The store responded with HTTP {status_code}. Your API key or secret is incorrect or has been revoked.",
                    "fix": "Go to your store's admin panel and regenerate API keys, then update them here."}

        if status_code == 404:
            return {"online": False, "issue": "not_found",
                    "title": "API Endpoint Not Found",
                    "message": f"HTTP 404 — The API endpoint was not found at:\n{url}",
                    "fix": "Make sure WooCommerce REST API is enabled under WooCommerce → Settings → Advanced → REST API, or verify the store URL is correct."}

        if status_code >= 500:
            return {"online": False, "issue": "server_error",
                    "title": "Store Server Error",
                    "message": f"The store's server returned HTTP {status_code}. The server is experiencing internal issues.",
                    "fix": "Contact your hosting provider or check your server error logs. This is a server-side issue, not an API key problem."}

        return {"online": False, "issue": "unexpected",
                "title": "Unexpected Response",
                "message": f"The store returned an unexpected HTTP status: {status_code}.",
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
def store_health_api(request, store_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    # Per-user scope: only diagnose stores the requester actually owns.
    store = get_object_or_404(Store, id=store_id, user=request.user)
    result = _diagnose_store(store)
    return Response({"success": True, **result})




# 🔥 DELETE STORE
@api_view(["DELETE", "POST"])  # POST kept for backwards-compat with old frontend
def delete_store_api(request, store_id):
    if not request.user.is_authenticated:
        return Response({"success": False, "message": "Authentication required"}, status=401)
    # Per-user scope: tenants can only delete their own stores.
    store = get_object_or_404(Store, id=store_id, user=request.user)

    # Read delete_ai_training from EVERY plausible source — some proxies
    # strip the body off DELETE requests, and a stale browser tab may not
    # have the body-sending JS yet, so we accept ?delete_ai_training=1 in
    # the query string as a fallback. Truthy values: "1", "true", "yes",
    # "on", True. Anything else → preserve.
    def _truthy(v):
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    raw = (
        (request.data.get("delete_ai_training") if hasattr(request, "data") and request.data else None)
        or request.GET.get("delete_ai_training")
        or request.POST.get("delete_ai_training")
        or request.META.get("HTTP_X_DELETE_AI_TRAINING")  # custom header fallback
        or False
    )
    delete_ai_training = _truthy(raw)
    user = request.user

    # Log so production diagnostics show exactly what we received and what
    # the flag resolved to — solves the "I clicked the checkbox, why
    # didn't anything happen?" debugging gap.
    import logging
    _log = logging.getLogger(__name__)
    _log.info(
        "delete_store_api: store_id=%s user=%s method=%s raw_flag=%r resolved=%s body=%r qs=%r",
        store_id, user.id, request.method, raw, delete_ai_training,
        dict(request.data) if hasattr(request, "data") and request.data else {},
        dict(request.GET),
    )

    snapshot_taken = False
    reset_counts = {"profiles": 0, "snippets": 0, "feedbacks": 0, "snapshots": 0}

    if delete_ai_training:
        # ☑ Checkbox: WIPE EVERYTHING. Per the rule "complete reset", we
        # interpret this as a tenant-wide wipe — every AiTrainingProfile,
        # KnowledgeSnippet, AiReplyFeedback, and any pending snapshot
        # belonging to this user goes. That way the AI Training Studio
        # genuinely looks empty after the delete, not just the deleted
        # store's slice (which the user might never have been viewing
        # in the first place).
        try:
            from emails.models import AiTrainingProfile, KnowledgeSnippet, AiReplyFeedback, PendingAiTrainingSnapshot
            reset_counts["profiles"]  = AiTrainingProfile.objects.filter(store__user=user).count()
            reset_counts["snippets"]  = KnowledgeSnippet.objects.filter(store__user=user).count()
            reset_counts["feedbacks"] = AiReplyFeedback.objects.filter(store__user=user).count()
            reset_counts["snapshots"] = PendingAiTrainingSnapshot.objects.filter(user=user).count()
            AiTrainingProfile.objects.filter(store__user=user).delete()
            KnowledgeSnippet.objects.filter(store__user=user).delete()
            AiReplyFeedback.objects.filter(store__user=user).delete()
            PendingAiTrainingSnapshot.objects.filter(user=user).delete()
        except Exception:
            pass
    else:
        # ☐ Unchecked — snapshot the deleted store's training so it
        # can be restored if the same store URL is reconnected later.
        try:
            from emails.services import snapshot_ai_training_for_store
            snap = snapshot_ai_training_for_store(store)
            snapshot_taken = snap is not None
        except Exception:
            snapshot_taken = False

    store.delete()

    return Response({
        "success":                  True,
        "message":                  "Store deleted successfully",
        "ai_training_preserved":    snapshot_taken,
        "ai_training_reset":        bool(delete_ai_training),
        "ai_training_reset_counts": reset_counts if delete_ai_training else None,
        # Echo what we received so the UI can verify the round-trip.
        "_debug_received_flag":     raw,
    })