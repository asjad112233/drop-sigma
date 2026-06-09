from teamapp.services import auto_assign_order
import logging
import os
import requests

_woolog = logging.getLogger("orders.woo_session")

# ── WooCommerce / WP REST helper ────────────────────────────────────────────
# Two-tier strategy with hard isolation between the tiers:
#
# PRIMARY tier — `cloudscraper` (or bare `requests` fallback). Solves
#   Cloudflare JS challenges and works for every store we currently sync
#   successfully. We never change behaviour here, so working stores
#   stay working.
#
# RETRY tier — `curl_cffi` impersonating Chrome 131's TLS fingerprint
#   AT THE BYTE LEVEL (real curl-impersonate). Defeats JA3/JA4
#   fingerprint blocking (Kinsta, WP Engine, Sucuri "managed" plans).
#   This tier ONLY fires when the primary call raises an SSLError /
#   ConnectionError matching the WAF-handshake-block pattern. A
#   normal HTTP 4xx / 5xx response from the primary does NOT trigger
#   it — that's a real merchant-side response that the existing
#   diagnose code already handles correctly.
#
# Critically: the retry tier sends API-shape headers (Accept: json,
# X-Requested-With: XMLHttpRequest, Sec-Fetch-Mode: cors) instead of
# Chrome's default navigation-shape headers (Accept: html, Sec-Fetch-
# Mode: navigate). That distinction is what an earlier attempt got
# wrong — navigation headers + Authorization: Basic was a giveaway
# pattern Wordfence 403'd on.
try:
    import cloudscraper as _cs
    _WOO_SCRAPER_AVAILABLE = True
except Exception:
    _WOO_SCRAPER_AVAILABLE = False

try:
    from curl_cffi import requests as _cffi_req
    from curl_cffi import exceptions as _cffi_ex
    _CURL_CFFI_AVAILABLE = True
except Exception:
    _CURL_CFFI_AVAILABLE = False


_WOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Headers a real WordPress-admin dashboard XHR sends when calling its own
# REST API. Used ONLY in the curl_cffi retry tier to make our request
# shape match what Wordfence / other WAFs consider "legitimate
# authenticated REST traffic" (instead of "browser navigating to
# /wp-json/* with Basic auth" — the giveaway pattern).
_WOO_API_HEADERS_CURLCFFI = {
    "User-Agent": _WOO_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    # NOTE: previously we sent X-Requested-With + Sec-Fetch-* to mimic a
    # logged-in WP-admin XHR. But the live production diagnostic on
    # 2026-06-09 proved Kinsta accepts our request just fine with plain
    # Accept: application/json (and rejects the XHR-shape variant).
    # Likely Kinsta's nginx WAF flags `X-Requested-With` from external
    # IPs as suspicious cross-origin scraping. Removing those headers
    # makes our request look like any other curl-style API client.
}


# Substrings that, when they appear in an exception message, indicate a
# hosting-WAF block at the TLS layer (silent drop or active rejection)
# rather than a genuine cert / DNS / refused problem. Mirrors the list
# in stores.views — kept in sync deliberately.
_WAF_HANDSHAKE_PATTERNS = (
    # Silent-drop family — server eats our handshake
    "eof occurred in violation of protocol",
    "ssl: unexpected_eof",
    "connection reset by peer",
    "connection aborted",
    "read timed out",
    "remote end closed connection",
    # Active TLS-alert family — server actively rejects our ClientHello
    "alert handshake failure",
    "sslv3_alert_handshake_failure",
    "tlsv1 alert handshake failure",
    "alert internal error",
    "alert protocol version",
    "alert access denied",
    "alert insufficient security",
    "alert unrecognized name",
    "alert decrypt error",
)


def _is_waf_handshake_error(exc) -> bool:
    """Heuristic for 'this exception looks like a WAF blocking our TLS
    handshake, NOT a genuine cert / DNS / refused problem.' Drives the
    retry-with-curl_cffi decision. Conservative: returns False on
    anything ambiguous so we only retry when we're confident."""
    if not exc:
        return False
    msg = str(exc).lower()
    return any(p in msg for p in _WAF_HANDSHAKE_PATTERNS)


def _looks_like_json_response(response) -> bool:
    """True iff the HTTP response body is plausibly a JSON document.

    Why this exists: the `?rest_route=` URL variant in the retry ladder
    can silently fall through to the site's homepage when WP's
    permalink config doesn't honour the query parameter — returning
    200 OK with an HTML body. The naive status-code check thinks
    we succeeded; the caller then calls `response.json()` and gets
    "Expecting value: line 1 column 1 (char 0)" with no clue why.
    Sniff Content-Type + first body char so we treat HTML-200 as a
    soft fail and keep walking the strategy ladder."""
    try:
        ctype = (response.headers.get("Content-Type") or "").lower()
    except Exception:
        ctype = ""
    if "application/json" in ctype or "text/json" in ctype:
        return True
    # Some WC installs return JSON with text/html or no Content-Type
    # at all (misconfigured but technically valid). Fall back to
    # inspecting the first non-whitespace byte: JSON arrays/objects
    # always start with [ or {, HTML always starts with <.
    try:
        body = (response.text or "").lstrip()
    except Exception:
        return False
    if not body:
        return False
    return body[0] in ("{", "[")


class _SmartWooSession:
    """Drop-in `requests.Session()`-compatible wrapper that:

      1. Routes every call through the PRIMARY session (cloudscraper or
         bare requests). If primary succeeds OR fails with a non-WAF
         exception, this is the ONLY path that runs — working stores
         see zero behavioural change.

      2. If primary raises SSLError / ConnectionError matching the WAF
         handshake-block pattern, retries ONCE through curl_cffi with
         a real Chrome TLS fingerprint + WC-admin-XHR-shape headers.
         This is the only way to defeat Kinsta-class WAFs that reject
         Python's TLS fingerprint at the handshake layer.

      3. Translates curl_cffi's exception classes into the
         `requests.exceptions.*` equivalents so the diagnose endpoint
         and other downstream catch blocks keep working unchanged.

    Returns `requests.Response` from the primary path; returns
    `curl_cffi.requests.Response` from the retry path — both expose
    .status_code / .text / .json() / .headers / .content / .raise_for_status
    with identical signatures, so callers don't need to special-case.
    """

    def __init__(self, primary):
        self._primary = primary
        self.headers = primary.headers   # callers mutate this; preserve the contract

    # Verb shortcuts — same signature as `requests.Session.<verb>`.
    def get(self, url, **kw):    return self._call("get",    url, **kw)
    def post(self, url, **kw):   return self._call("post",   url, **kw)
    def put(self, url, **kw):    return self._call("put",    url, **kw)
    def delete(self, url, **kw): return self._call("delete", url, **kw)
    def request(self, method, url, **kw):
        return self._call(method.lower(), url, **kw)

    def _call(self, method, url, **kw):
        try:
            return getattr(self._primary, method)(url, **kw)
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as primary_exc:
            if not _CURL_CFFI_AVAILABLE:
                raise
            if not _is_waf_handshake_error(primary_exc):
                raise
            # WAF handshake block detected → retry through curl_cffi
            # impersonating Chrome at the TLS-fingerprint layer.
            try:
                return self._curl_cffi_retry(method, url, **kw)
            except Exception as retry_exc:
                # Retry didn't save us. Log both for diagnosis and
                # re-raise the ORIGINAL exception so existing
                # firewall-classification logic in stores.views keeps
                # routing this to the right error card.
                _woolog.warning(
                    "WooCommerce WAF retry via curl_cffi also failed for %s: "
                    "primary=%s, retry=%s",
                    url, type(primary_exc).__name__, type(retry_exc).__name__,
                )
                raise primary_exc

    def _curl_cffi_retry(self, method, url, **kw):
        """Try MULTIPLE evasion strategies in order until one succeeds.

        Strategy ladder (fastest-to-slowest, cheapest-to-most-expensive):

          1. Chrome 131 TLS fingerprint  + standard /wp-json URL  (direct)
          2. Firefox 133 TLS fingerprint + standard /wp-json URL  (direct)
          3. Safari 17.2 TLS fingerprint + standard /wp-json URL  (direct)
          4. Chrome 131  + `?rest_route=` URL variant (some WAFs only
             pattern-match on `/wp-json/*`; the ?rest_route= form
             reaches the same WC endpoint but slips past path-based
             rules)
          5. If `WOO_PROXY_URL` env is set: repeat steps 1-3 through
             that proxy. Use a residential-IP proxy service
             (BrightData, NetNut, Oxylabs, Smartproxy) for stores
             behind IP-reputation firewalls like Kinsta's nginx
             firewall. Format: `https://user:pass@host:port` or
             `socks5://user:pass@host:port`.

        First strategy that returns 2xx OR 404 wins (404 means the URL
        was valid + we reached the WC layer — different from a WAF
        403). 4xx WAF responses on every strategy → raise SSLError so
        the firewall-card UX still surfaces.
        """
        from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl
        parsed = urlparse(url)
        headers = dict(_WOO_API_HEADERS_CURLCFFI)
        # NOTE: previously also set Origin + Referer headers to mimic an
        # in-admin XHR. Live diagnostic proved Kinsta's WAF rejects
        # those (probably because legit API clients don't send them).
        # Now matches the minimal header set that succeeds in production.

        # Build the alternate `?rest_route=` URL — WC's officially-
        # supported pretty-permalink-independent form. Only convert
        # paths that start with /wp-json/wc/v3/ to /wc/v3/.
        rest_route_url = url
        if "/wp-json/" in parsed.path:
            new_path = parsed.path.split("/wp-json", 1)[1]  # e.g. /wc/v3/orders
            existing = dict(parse_qsl(parsed.query))
            existing["rest_route"] = new_path
            rest_route_url = urlunparse((
                parsed.scheme, parsed.netloc, "/",
                "", urlencode(existing), parsed.fragment,
            ))

        # Optional residential-IP proxy (read at call-time so a
        # superadmin can toggle it without a redeploy via env var).
        proxy_url = (os.getenv("WOO_PROXY_URL") or "").strip() or None
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        # Optional Cloudflare-Worker relay. The worker URL receives our
        # request with X-Target-URL + X-Proxy-Secret headers, then makes
        # the actual call FROM Cloudflare's network (whose IPs are
        # whitelisted by virtually every managed-WP host because they
        # use Cloudflare as a CDN partner). FREE on CF's 100k-req/day
        # tier — see cf-worker/SETUP.md for the 5-min setup steps.
        cf_worker_url    = (os.getenv("WOO_CF_PROXY_URL") or "").strip() or None
        cf_worker_secret = (os.getenv("WOO_CF_PROXY_SECRET") or "").strip() or None

        # ── Strategy shape ──────────────────────────────────────────
        #   ('direct', impersonation, url, proxies_dict_or_None, label)
        #   ('cfworker', impersonation, real_target_url, label)
        # Each is tried in order; first one that returns a real (non-WAF)
        # response wins.
        strategies = [
            ("direct", "chrome131",      url,            None,    "chrome+wpjson"),
            ("direct", "firefox133",     url,            None,    "firefox+wpjson"),
            ("direct", "safari17_2_ios", url,            None,    "safari+wpjson"),
            ("direct", "chrome131",      rest_route_url, None,    "chrome+restroute"),
        ]

        # Cloudflare Worker relay strategies — cheap & free, prefer them
        # over residential proxy because no per-request data cost.
        if cf_worker_url and cf_worker_secret:
            strategies += [
                ("cfworker", "chrome131", url,            "chrome+cfworker"),
                ("cfworker", "chrome131", rest_route_url, "chrome+cfworker+restroute"),
            ]

        # Residential-IP proxy strategies — paid, last resort.
        if proxies:
            strategies += [
                ("direct", "chrome131",  url,            proxies, "chrome+wpjson+proxy"),
                ("direct", "firefox133", url,            proxies, "firefox+wpjson+proxy"),
                ("direct", "chrome131",  rest_route_url, proxies, "chrome+restroute+proxy"),
            ]

        last_response   = None  # last 4xx body so we can log on final failure
        last_exception  = None
        for strategy in strategies:
            kind = strategy[0]
            logged_target = url  # what we tell the log we're trying to reach
            label = "?"
            try:
                if kind == "cfworker":
                    _, imp, target_url, label = strategy
                    logged_target = target_url
                    sess = _cffi_req.Session(
                        impersonate=imp,
                        default_headers=False,
                    )
                    # Pass merchant-style headers + ADD the worker control
                    # headers. The worker strips its control headers before
                    # forwarding so the upstream sees only merchant ones.
                    sess.headers.update(headers)
                    sess.headers["X-Target-URL"]   = target_url
                    sess.headers["X-Proxy-Secret"] = cf_worker_secret
                    response = getattr(sess, method)(cf_worker_url, **kw)
                else:  # "direct"
                    _, imp, try_url, proxies_arg, label = strategy
                    logged_target = try_url
                    sess = _cffi_req.Session(
                        impersonate=imp,
                        default_headers=False,
                    )
                    sess.headers.update(headers)
                    call_kw = dict(kw)
                    # CRITICAL: curl_cffi's proxy API is `proxy=<url>`
                    # (singular string), NOT `proxies={"http":..,"https":..}`
                    # which is the `requests` library convention. Passing
                    # `proxies=` to curl_cffi is SILENTLY IGNORED — the
                    # request goes direct (not through the proxy) and
                    # we get the same Railway-IP 403 as without a proxy
                    # set at all. Took 4 hours and a residential proxy
                    # subscription to discover this. Verified via local
                    # test:
                    #   curl_cffi + proxies={...}  → HTTP 403 (no proxy used)
                    #   curl_cffi + proxy=<url>     → HTTP 200 OK ✓
                    if proxies_arg:
                        # `proxies_arg` is a dict (built upstream for
                        # familiarity); curl_cffi just wants the URL.
                        # Prefer https, fall back to http.
                        proxy_str = proxies_arg.get("https") or proxies_arg.get("http")
                        if proxy_str:
                            call_kw["proxy"] = proxy_str
                    response = getattr(sess, method)(try_url, **call_kw)
                status = getattr(response, "status_code", 0)
                # Success or a real WC response → we're done.
                # 2xx = OK. 4xx WC API errors (auth, validation) are
                # genuine merchant responses — NOT WAF blocks — and
                # should pass through to existing handlers.
                #   • 401 = real auth failure (creds wrong)
                #   • 404 = endpoint wrong (e.g. WC not installed)
                #   • 422 = WC validation error
                # The 403 / 406 / 444 / 429 codes are the WAF
                # signature pattern — keep trying the next strategy.
                if status < 400 or status in (401, 404, 422):
                    # CRITICAL: also verify the body is actually a WC
                    # JSON response, not an HTML page that happened to
                    # return 200. The `?rest_route=` URL variant can
                    # silently fall through to the WordPress homepage
                    # (200 OK + HTML body) when the site's permalink
                    # config doesn't recognise the parameter. Returning
                    # that to the caller would break `response.json()`
                    # with the cryptic "Expecting value: line 1 col 1
                    # (char 0)" error.
                    if _looks_like_json_response(response):
                        _woolog.info(
                            "WooCommerce WAF retry %s succeeded for %s (HTTP %s)",
                            label, logged_target, status,
                        )
                        return response
                    # 200 with non-JSON body = soft fail. Treat like a
                    # WAF 4xx and keep walking the ladder.
                    last_response = response
                    _woolog.debug(
                        "WooCommerce WAF retry %s returned HTTP %s but body "
                        "is NOT JSON (likely WP homepage fallback) — trying next",
                        label, status,
                    )
                else:
                    # WAF-shape 4xx — remember it for the final log and
                    # try the next strategy.
                    last_response = response
                    # Emit at INFO so we can debug strategy-by-strategy
                    # behaviour in production logs (don't need to enable
                    # DEBUG just to see retry intermediate steps).
                    _woolog.info(
                        "WooCommerce WAF retry %s returned HTTP %s — trying next",
                        label, status,
                    )
            except Exception as e:
                last_exception = e
                _woolog.info(
                    "WooCommerce WAF retry %s raised %s: %s — trying next",
                    label, type(e).__name__, str(e)[:200],
                )
                continue

        # ── All strategies exhausted. Log the LAST 4xx body for
        # diagnosis (which WAF page came back) so we can decide if
        # adding another header / impersonation would help next
        # time, then signal upstream that this remains a firewall
        # block (so the diagnose UI shows the right card).
        if last_response is not None:
            body_preview = ""
            try:
                body_preview = (getattr(last_response, "text", "") or "")[:800]
            except Exception:
                pass
            status = getattr(last_response, "status_code", "?")
            _woolog.warning(
                "WooCommerce WAF retry: exhausted ALL %d strategies for %s "
                "(last HTTP %s). Likely an IP-reputation block at the "
                "server / hosting layer (Kinsta nginx firewall, WP Engine "
                "guard, etc.) that no header or fingerprint trick can "
                "bypass — needs WOO_PROXY_URL or customer-side IP "
                "whitelist. Final body preview: %r",
                len(strategies), url, status, body_preview,
            )
            raise requests.exceptions.SSLError(
                f"All {len(strategies)} WAF-evasion strategies returned "
                f"HTTP {status}. IP-reputation block at hosting layer; "
                f"set WOO_PROXY_URL or customer must whitelist our IP."
            )
        # No 4xx ever returned — every attempt errored out. Surface
        # the last exception (translated to requests-compatible).
        e = last_exception
        try:
            raise e
        except requests.exceptions.SSLError:
            raise
        except Exception as e:
            if _CURL_CFFI_AVAILABLE:
                if isinstance(e, _cffi_ex.Timeout):
                    raise requests.exceptions.Timeout(str(e)) from e
                if isinstance(e, _cffi_ex.ConnectionError):
                    msg = str(e).lower()
                    if any(t in msg for t in ("ssl", "tls", "alert", "handshake", "cert")):
                        raise requests.exceptions.SSLError(str(e)) from e
                    raise requests.exceptions.ConnectionError(str(e)) from e
            raise


def woo_session():
    """Return a session that survives merchant-side WAFs.

    Tier 1 (primary, always runs): cloudscraper for Cloudflare-style JS
        challenges. Falls back to bare `requests` if cloudscraper isn't
        installed. Working stores ONLY ever hit this tier.

    Tier 2 (retry, only on WAF handshake errors): curl_cffi impersonating
        Chrome 131's exact TLS ClientHello — defeats JA3/JA4 fingerprint
        blocks (Kinsta, WP Engine, Sucuri managed plans).

    Returns a `requests.Session()`-compatible object exposing .get / .post
    / .put / .delete / .request — drop-in replacement for any caller
    that previously used `requests.Session()`.
    """
    if _WOO_SCRAPER_AVAILABLE:
        primary = _cs.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True},
            delay=2,
        )
    else:
        primary = requests.Session()
    primary.headers.update(_WOO_HEADERS)
    return _SmartWooSession(primary)

COURIER_URL_TEMPLATES = {
    "yuntrack":       "https://www.yuntrack.com/parcelTracking?id={num}",
    "dhl":            "https://www.dhl.com/en/express/tracking.html?AWB={num}&brand=DHL",
    "fedex":          "https://www.fedex.com/fedextrack/?trknbr={num}",
    "ups":            "https://www.ups.com/track?tracknum={num}",
    "postnl":         "https://jouw.postnl.nl/track-and-trace/{num}",
    "royal mail":     "https://www.royalmail.com/track-your-item#/tracking-results/{num}",
    "usps":           "https://tools.usps.com/go/TrackConfirmAction?tLabels={num}",
    "australia post": "https://auspost.com.au/mypost/track/#/details/{num}",
    "4px":            "https://track.4px.com/#/result/0/{num}",
    "china post":     "https://ems.com.cn/mailquery/parcelQuery?mailNo={num}",
    "cainiao":        "https://global.cainiao.com/detail.htm?mailNo={num}",
    "tcs pakistan":   "https://www.tcs.com.pk/tracking.php?cn={num}",
    "leopards":       "https://www.leopardscourier.com/api/track_n_trace?cn={num}",
}

from .models import Order, OrderActivity
from vendors.models import ProductVendorAssignment


def log_activity(order, activity_type, description, actor=None):
    OrderActivity.objects.create(
        order=order,
        activity_type=activity_type,
        description=description,
        actor=actor,
    )


def apply_vendor_auto_assignment(order):
    if not order.product_id:
        return

    if order.assigned_vendor_id and order.assignment_type == "permanent_auto":
        return  # already auto-assigned, don't override

    # Product-global lookup — same product always goes to same vendor regardless of store
    assignment = ProductVendorAssignment.objects.filter(
        product_id=order.product_id,
        is_active=True
    ).first()

    if assignment:
        order.assigned_vendor = assignment.vendor
        order.assignment_type = "permanent_auto"
        order.vendor_status = "assigned"
        order.save(update_fields=["assigned_vendor", "assignment_type", "vendor_status"])
        log_activity(order, "vendor_assigned",
                     f"Vendor '{assignment.vendor.name}' auto-assigned by product mapping",
                     actor="System")


def _parse_iso_dt(value):
    """Parse an ISO-8601 timestamp from WooCommerce/Shopify into an aware
    datetime, or return None on any failure. WC `date_created_gmt` has no
    Z suffix but is always UTC; WC `date_created` is local; Shopify
    `created_at` is ISO with offset. We normalise everything to UTC."""
    if not value:
        return None
    try:
        from datetime import datetime, timezone as _tz
        s = str(value).strip()
        # WooCommerce returns date_created_gmt without timezone but it's UTC.
        # date_created has no tz info either but is local — caller must
        # prefer _gmt fields when available.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # If no timezone info, assume UTC (caller passed a _gmt field).
        if "+" not in s and "T" in s and s.count("-") <= 2:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _set_order_created_at(order_obj, dt):
    """Patch Order.created_at directly via UPDATE so we bypass
    auto_now_add=True (which otherwise locks the field at INSERT time).

    Idempotent — skips if the existing value already matches to the second."""
    if not dt or not order_obj:
        return
    current = getattr(order_obj, "created_at", None)
    if current and abs((current - dt).total_seconds()) < 2:
        return  # already in sync
    Order.objects.filter(pk=order_obj.pk).update(created_at=dt)
    order_obj.created_at = dt


def process_woocommerce_order(store, item):
    """Parse one WooCommerce order dict and upsert into DB. Returns (order, created)."""
    billing = item.get("billing", {})
    line_items = item.get("line_items", [])
    product_id = str(line_items[0].get("product_id")) if line_items else None
    product_name = line_items[0].get("name") if line_items else None

    new_status = item.get("status", "")

    # Capture old status before update to detect changes
    try:
        existing = Order.objects.get(store=store, external_order_id=str(item.get("id")))
        old_status = existing.fulfillment_status or ""
    except Order.DoesNotExist:
        old_status = None  # Will be created

    order_obj, created = Order.objects.update_or_create(
        store=store,
        external_order_id=str(item.get("id")),
        defaults={
            "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
            "customer_email": billing.get("email"),
            "customer_phone": billing.get("phone"),
            "country": billing.get("country"),
            "city": billing.get("city"),
            "total_price": item.get("total") or 0,
            "currency": item.get("currency", "USD"),
            "payment_status": new_status,
            "fulfillment_status": new_status,
            "tracking_number": "",
            "product_id": product_id,
            "product_name": product_name,
            "raw_data": item,
        }
    )

    # Pin created_at to WC's actual order time, not the moment we synced.
    # Without this, batched syncs land all orders with near-identical
    # created_at (DB insert time) → sort-by-newest shows reverse-WC order.
    wc_dt = _parse_iso_dt(item.get("date_created_gmt") or item.get("date_created"))
    if wc_dt:
        _set_order_created_at(order_obj, wc_dt)

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from {store.platform.title()}",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
            _notify_employee_assigned(order_obj, member)
        apply_vendor_auto_assignment(order_obj)
        _notify_admin_new_order(order_obj)
        if order_obj.assigned_vendor_id:
            _notify_vendor_assigned(order_obj)
        # Fire auto email for the new order's status
        _fire_auto_email(order_obj, new_status)
    elif old_status is not None and new_status.lower() != old_status.lower():
        # Status changed on WooCommerce side — fire auto email
        _fire_auto_email(order_obj, new_status)

    return order_obj, created


def _fire_auto_email(order, new_status):
    try:
        from emails.views import send_auto_status_email
        send_auto_status_email(order, new_status)
    except Exception:
        pass


def _notify_admin_new_order(order):
    """Tell the tenant owner a new order just landed."""
    try:
        from notifications.services import notify
        owner = getattr(order.store, "user", None)
        if not owner:
            return
        customer = order.customer_name or "a customer"
        notify(
            recipient=owner,
            audience="admin",
            category="order",
            priority="high",
            title=f"New order #{order.external_order_id} from {order.store.name}",
            body=f"{customer} ordered for {order.currency} {order.total_price}.",
            action_url=f"/dashboard/?section=orders&order_id={order.id}",
            action_label="View order",
            related_order_id=order.id,
        )
    except Exception:
        pass


def _notify_vendor_assigned(order):
    """Tell the assigned vendor about the new order."""
    try:
        from notifications.services import notify
        vendor_user = getattr(order.assigned_vendor, "user", None)
        if not vendor_user:
            return
        notify(
            recipient=vendor_user,
            audience="vendor",
            category="order",
            priority="high",
            title=f"New order assigned: #{order.external_order_id}",
            body=f"{order.customer_name or 'A customer'} ordered {order.product_name or 'an item'}. "
                 f"Please ship and submit tracking.",
            action_url=f"/vendor/?tab=orders&order_id={order.id}",
            action_label="View",
            related_order_id=order.id,
        )
    except Exception:
        pass


def _notify_employee_assigned(order, member):
    """Tell the auto-assigned team member about the new order."""
    try:
        from notifications.services import notify
        notify(
            recipient=getattr(member, "user", None),
            audience="employee",
            category="order",
            priority="medium",
            title=f"Order #{order.external_order_id} assigned to you",
            body=f"Auto-routed by {member.role.replace('_',' ').title()} rule. "
                 f"Customer: {order.customer_name or '—'}.",
            action_url=f"/employee/?order_id={order.id}",
            action_label="Open",
            related_order_id=order.id,
        )
    except Exception:
        pass


def setup_woocommerce_webhook(store, delivery_url):
    """Register order.created + order.updated webhooks idempotently.

    Self-healing: if a webhook for the topic exists with a stale URL we
    PUT-update it to point at `delivery_url` instead of leaving a dead one.

    Returns a dict so callers can surface real status (not just swallow
    failures silently like the old version did). Shape:
        {
            "ok": bool,                       # at least one topic is now correctly registered
            "registered": ["order.created", ...],  # topics confirmed pointing at us
            "errors":     [{"topic": "order.created", "stage": "create", "code": 401, "body": "..."}],
            "webhook_ids": {"order.created": 12, "order.updated": 13},
        }
    """
    base = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/webhooks"
    auth = (store.api_key, store.api_secret)
    sess = woo_session()
    result = {"ok": False, "registered": [], "errors": [], "webhook_ids": {}}

    # 1. Snapshot existing webhooks so we don't duplicate and so we can
    #    repair stale delivery URLs.
    existing_by_topic = {}  # topic -> {"id", "delivery_url"}
    try:
        existing = sess.get(base, auth=auth, params={"per_page": 100}, timeout=15, verify=False)
        if existing.ok:
            for wh in existing.json():
                if isinstance(wh, dict) and wh.get("topic"):
                    # Keep the most recent (last) entry per topic; WC lets you
                    # have multiple with the same topic, but we only care about ours.
                    existing_by_topic[wh["topic"]] = {
                        "id": wh.get("id"),
                        "delivery_url": (wh.get("delivery_url") or "").rstrip("/"),
                    }
        else:
            result["errors"].append({
                "topic": "*",
                "stage": "list",
                "code": existing.status_code,
                "body": (existing.text or "")[:200],
            })
    except Exception as e:
        result["errors"].append({
            "topic": "*",
            "stage": "list",
            "code": 0,
            "body": f"{type(e).__name__}: {e}"[:200],
        })

    want_url = delivery_url.rstrip("/")

    for topic in ("order.created", "order.updated"):
        existing = existing_by_topic.get(topic)

        # Case A: already registered correctly → nothing to do.
        if existing and existing["delivery_url"] == want_url:
            result["registered"].append(topic)
            result["webhook_ids"][topic] = existing["id"]
            continue

        # Case B: stale URL → PUT-update so we don't pile up duplicate webhooks.
        if existing and existing["id"]:
            try:
                r = sess.put(
                    f"{base}/{existing['id']}",
                    auth=auth,
                    json={"delivery_url": delivery_url, "status": "active"},
                    timeout=15,
                    verify=False,
                )
                if r.ok:
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = existing["id"]
                    continue
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
            except Exception as e:
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
                })
            # Fall through to create as a last resort.

        # Case C: not registered → create.
        payload = {
            "name": f"Drop Sigma {topic.replace('.', ' ').title()}",
            "topic": topic,
            "delivery_url": delivery_url,
            "secret": store.api_secret or "",
            "status": "active",
        }
        try:
            r = sess.post(base, auth=auth, json=payload, timeout=15, verify=False)
            if r.ok:
                wh_id = r.json().get("id")
                result["registered"].append(topic)
                if wh_id:
                    result["webhook_ids"][topic] = wh_id
            else:
                result["errors"].append({
                    "topic": topic, "stage": "create",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
        except Exception as e:
            result["errors"].append({
                "topic": topic, "stage": "create",
                "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
            })

    result["ok"] = bool(result["registered"])
    return result


def _refresh_shopify_token(store) -> bool:
    """Exchange refresh_token for a fresh access_token. Returns True on
    success. Idempotent — safe to call when token is still valid.

    Shopify-2025 issues expiring offline tokens (~24h TTL on access,
    ~60 days on refresh). Without refresh logic, every Shopify store
    silently breaks 24h after install. This is the heart of the
    auto-refresh-on-401 mechanism.

    https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/online-access-tokens
    """
    import logging as _l
    log = _l.getLogger(__name__)

    if not store or store.platform != "shopify":
        return False
    if not store.refresh_token:
        log.warning(
            "Shopify refresh: store %s (%s) has no refresh_token — must reconnect",
            store.id, store.name,
        )
        return False

    from django.conf import settings as _s
    from django.utils import timezone as _tz
    from datetime import timedelta as _td

    shop_host = store.store_url.replace("https://", "").replace("http://", "").rstrip("/")
    try:
        r = requests.post(
            f"https://{shop_host}/admin/oauth/access_token",
            json={
                "client_id":     _s.SHOPIFY_API_KEY,
                "client_secret": _s.SHOPIFY_API_SECRET,
                "refresh_token": store.refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=20,
        )
    except Exception as e:
        log.warning("Shopify refresh request failed for store %s: %s", store.id, e)
        return False

    if not r.ok:
        log.warning(
            "Shopify refresh failed for store %s (HTTP %s): %s",
            store.id, r.status_code, (r.text or "")[:200],
        )
        return False

    data = r.json() or {}
    new_access = data.get("access_token") or ""
    new_refresh = data.get("refresh_token") or store.refresh_token  # may rotate or stay same
    expires_in = data.get("expires_in") or 0
    refresh_expires_in = data.get("refresh_token_expires_in") or 0

    if not new_access:
        log.warning("Shopify refresh: store %s returned no access_token", store.id)
        return False

    store.access_token = new_access
    store.refresh_token = new_refresh
    store.token_expires_at = (_tz.now() + _td(seconds=int(expires_in))) if expires_in else None
    if refresh_expires_in:
        store.refresh_token_expires_at = _tz.now() + _td(seconds=int(refresh_expires_in))
    store.save(update_fields=[
        "access_token", "refresh_token",
        "token_expires_at", "refresh_token_expires_at",
    ])
    log.info("Shopify refresh OK for store %s (%s), new TTL=%ss", store.id, store.name, expires_in)
    return True


def _shopify_session(store):
    """Return (headers, auth) tuple for Shopify API requests.

    Side effect: if the stored access_token is about to expire (within
    60 seconds) AND we have a refresh_token, transparently refresh
    BEFORE returning the headers. This pre-empts the typical
    "request lands 1 sec after token expiry → 401" race.
    """
    # Pre-emptive refresh window — refresh slightly before expiry so
    # in-flight calls always use a valid token.
    try:
        from django.utils import timezone as _tz
        if (store.platform == "shopify"
                and store.token_expires_at
                and store.refresh_token
                and (store.token_expires_at - _tz.now()).total_seconds() < 60):
            _refresh_shopify_token(store)
    except Exception:
        pass

    headers = {"Content-Type": "application/json"}
    if store.access_token:
        headers["X-Shopify-Access-Token"] = store.access_token
        return headers, None
    return headers, (store.api_key, store.api_secret)


def shopify_request(store, method, url, **kwargs):
    """Wrapper for requests.<method>() to a Shopify Admin API URL with
    auto-refresh-on-401. Use this instead of `requests.get/post(...)`
    when calling any Shopify endpoint that uses store.access_token.

    Why: even with pre-emptive refresh in _shopify_session, a token
    could be invalidated server-side (manual revoke, scope change). On
    a 401 we refresh once and retry the request — transparent for
    callers."""
    headers, auth = _shopify_session(store)
    headers.update(kwargs.pop("headers", {}) or {})

    fn = getattr(requests, method.lower())
    resp = fn(url, headers=headers, auth=auth, **kwargs)

    if resp.status_code == 401 and store.platform == "shopify" and store.refresh_token:
        # One-shot refresh + retry. If still 401 after refresh, the
        # refresh_token itself is dead — merchant has to reconnect.
        if _refresh_shopify_token(store):
            headers["X-Shopify-Access-Token"] = store.access_token
            resp = fn(url, headers=headers, auth=auth, **kwargs)

    return resp


def process_shopify_order(store, item):
    """Parse one Shopify order dict and upsert into DB. Returns (order, created)."""
    billing = item.get("billing_address") or {}
    line_items = item.get("line_items", [])
    product_id = str(line_items[0].get("product_id")) if line_items else None
    product_name = line_items[0].get("title") if line_items else None

    fulfillment_status = item.get("fulfillment_status") or item.get("financial_status") or "pending"

    order_obj, created = Order.objects.update_or_create(
        store=store,
        external_order_id=str(item.get("id")),
        defaults={
            "customer_name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip(),
            "customer_email": item.get("email") or billing.get("email"),
            "customer_phone": item.get("phone") or billing.get("phone"),
            "country": billing.get("country_code") or billing.get("country"),
            "city": billing.get("city"),
            "total_price": item.get("total_price") or 0,
            "currency": item.get("currency", "USD"),
            "payment_status": item.get("financial_status"),
            "fulfillment_status": fulfillment_status,
            "tracking_number": "",
            "product_id": product_id,
            "product_name": product_name,
            "raw_data": item,
        }
    )

    # Pin created_at to Shopify's actual order time, not the moment we synced
    # (Shopify returns ISO-8601 with offset in `created_at`).
    sh_dt = _parse_iso_dt(item.get("created_at"))
    if sh_dt:
        _set_order_created_at(order_obj, sh_dt)

    if created:
        log_activity(order_obj, "received",
                     f"Order #{order_obj.external_order_id} received from Shopify",
                     actor="System")
        member = auto_assign_order(order_obj)
        if member:
            log_activity(order_obj, "assigned",
                         f"Auto-assigned to {member.name} ({member.role})",
                         actor="System")
            _notify_employee_assigned(order_obj, member)
        apply_vendor_auto_assignment(order_obj)
        _notify_admin_new_order(order_obj)
        if order_obj.assigned_vendor_id:
            _notify_vendor_assigned(order_obj)

    return order_obj, created


def setup_shopify_webhook(store, delivery_url):
    """Register the full set of order webhooks in Shopify idempotently.

    Subscribes to orders/create + orders/updated + orders/fulfilled +
    orders/cancelled + orders/paid so the dashboard stays in sync
    regardless of which lifecycle event fires.

    Returns the same dict shape as setup_woocommerce_webhook:
        {ok, registered: [topic,...], errors: [...], webhook_ids: {topic: id}}
    """
    headers, auth = _shopify_session(store)
    base = f"{store.store_url.rstrip('/')}/admin/api/2024-01/webhooks.json"
    topics = [
        "orders/create",
        "orders/updated",
        "orders/fulfilled",
        "orders/cancelled",
        "orders/paid",
    ]
    result = {"ok": False, "registered": [], "errors": [], "webhook_ids": {}}

    # Snapshot existing webhooks once so we don't re-create.
    existing_by_topic = {}  # topic -> (id, address)
    try:
        r = requests.get(base, headers=headers, auth=auth, timeout=15)
        if r.ok:
            for wh in r.json().get("webhooks", []):
                topic = wh.get("topic")
                if topic:
                    existing_by_topic[topic] = (
                        wh.get("id"),
                        (wh.get("address") or "").rstrip("/"),
                    )
        else:
            result["errors"].append({
                "topic": "*", "stage": "list",
                "code": r.status_code, "body": (r.text or "")[:200],
            })
    except Exception as e:
        result["errors"].append({
            "topic": "*", "stage": "list",
            "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
        })

    want_url = delivery_url.rstrip("/")

    for topic in topics:
        existing = existing_by_topic.get(topic)

        # Case A: already registered correctly.
        if existing and existing[1] == want_url:
            result["registered"].append(topic)
            result["webhook_ids"][topic] = existing[0]
            continue

        # Case B: stale URL → PUT-update to point at us.
        if existing and existing[0]:
            try:
                r = requests.put(
                    f"{store.store_url.rstrip('/')}/admin/api/2024-01/webhooks/{existing[0]}.json",
                    headers=headers, auth=auth,
                    json={"webhook": {"id": existing[0], "address": delivery_url, "format": "json"}},
                    timeout=15,
                )
                if r.ok:
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = existing[0]
                    continue
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": r.status_code, "body": (r.text or "")[:200],
                })
            except Exception as e:
                result["errors"].append({
                    "topic": topic, "stage": "update",
                    "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
                })
            # Fall through to create as last resort.

        # Case C: not registered → create.
        payload = {
            "webhook": {
                "topic":   topic,
                "address": delivery_url,
                "format":  "json",
            }
        }
        try:
            response = requests.post(base, headers=headers, auth=auth, json=payload, timeout=15)
            if response.ok:
                wh = response.json().get("webhook") or {}
                if wh.get("id"):
                    result["registered"].append(topic)
                    result["webhook_ids"][topic] = wh["id"]
                else:
                    result["errors"].append({
                        "topic": topic, "stage": "create",
                        "code": response.status_code, "body": "missing webhook.id in response",
                    })
            else:
                result["errors"].append({
                    "topic": topic, "stage": "create",
                    "code": response.status_code, "body": (response.text or "")[:200],
                })
        except Exception as e:
            result["errors"].append({
                "topic": topic, "stage": "create",
                "code": 0, "body": f"{type(e).__name__}: {e}"[:200],
            })

    result["ok"] = bool(result["registered"])
    return result


def sync_shopify_orders(store, after=None):
    """Fetch orders from Shopify API and sync. Returns count of new orders created."""
    headers, auth = _shopify_session(store)
    url = f"{store.store_url.rstrip('/')}/admin/api/2024-01/orders.json"
    params = {"limit": 250, "status": "any", "order": "created_at desc"}
    if after:
        params["created_at_min"] = after

    response = requests.get(url, headers=headers, auth=auth, params=params, timeout=30)
    response.raise_for_status()

    count = 0
    for item in response.json().get("orders", []):
        _, created = process_shopify_order(store, item)
        if created:
            count += 1

    from django.utils import timezone
    if hasattr(store, "last_synced"):
        store.last_synced = timezone.now()
        store.save(update_fields=["last_synced"])

    return count


def sync_woocommerce_orders(store, after=None):
    """Fetch orders from WooCommerce API and sync. after=ISO datetime string for incremental sync."""
    url = f"{store.store_url.rstrip('/')}/wp-json/wc/v3/orders"
    params = {"per_page": 50, "orderby": "date", "order": "desc"}
    if after:
        params["after"] = after

    response = woo_session().get(url, auth=(store.api_key, store.api_secret), params=params, timeout=30)
    response.raise_for_status()

    count = 0
    for item in response.json():
        _, created = process_woocommerce_order(store, item)
        if created:
            count += 1

    # Update last_synced
    from django.utils import timezone
    if hasattr(store, "last_synced"):
        store.last_synced = timezone.now()
        store.save(update_fields=["last_synced"])

    return count