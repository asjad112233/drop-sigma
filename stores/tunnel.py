"""
Auto-manages the Cloudflare quick tunnel for local development.
Starts cloudflared when Django starts, stores the URL in memory + temp file + .env.
On Django autoreload, reuses the existing tunnel instead of restarting it.
"""
import os
import re
import subprocess
import threading
import pathlib

CLOUDFLARED = pathlib.Path.home() / ".local/bin/cloudflared"
_URL_FILE = pathlib.Path("/tmp/dropsigma_tunnel_url")
_LOG_FILE = "/tmp/cf_tunnel.log"
_ENV_FILE = pathlib.Path(__file__).parent.parent / ".env"

_url = None
_lock = threading.Lock()


def get_url(wait_secs=0):
    """
    Return the live tunnel URL, or None if not available.
    Pass wait_secs > 0 to poll during server startup (e.g. first request after boot).
    """
    import time
    global _url
    deadline = time.time() + wait_secs
    while True:
        # 1. In-memory (fastest)
        if _url:
            return _url
        # 2. Temp file written by _discover_url (survives autoreload)
        if _URL_FILE.exists():
            try:
                candidate = _URL_FILE.read_text().strip()
                if candidate.startswith("https://") and _is_cloudflared_running():
                    with _lock:
                        _url = candidate
                    return _url
            except Exception:
                pass
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    return None


def get_base_url(request=None, wait_secs=3):
    """
    Single source of truth for the webhook/callback base URL.
    Priority:
      1. Live Cloudflare tunnel (dev only — when running locally with public tunnel)
      2. Real request host IF it's a public domain (production canonical — handles
         multi-domain setups: dropsigma.com, custom domains all work)
      3. WOOCOMMERCE_BASE_URL env override (explicit override for special cases)
      4. RAILWAY_PUBLIC_DOMAIN (auto-detected by Railway)
      5. Whatever request host is available (last resort, even localhost)
    Always returns an HTTPS URL (or None if nothing is available).
    Call this everywhere — never build callback URLs manually.
    """
    from django.conf import settings

    def _is_local_host(host):
        if not host:
            return True
        host = host.split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local")

    # 1. Local Cloudflare tunnel (dev). Only matters when there's actually a tunnel.
    url = get_url(wait_secs=wait_secs)

    # 2. Real request host (production canonical). Prefer this over stale env vars
    #    so users connecting from dropsigma.com get dropsigma.com callbacks, etc.
    if not url and request is not None:
        try:
            host = request.get_host()
            if host and not _is_local_host(host):
                url = request.build_absolute_uri("/")
        except Exception:
            pass

    # 3. Explicit override env var (e.g. local dev pointing at a tunnel).
    if not url:
        override = getattr(settings, "WOOCOMMERCE_BASE_URL", "") or ""
        if override and not override.endswith("trycloudflare.com"):
            url = override

    # 4. Railway auto-detected public domain
    if not url:
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway_domain:
            url = f"https://{railway_domain}"

    # 5. Last resort — request host even if localhost (better than nothing).
    if not url and request is not None:
        try:
            url = request.build_absolute_uri("/")
        except Exception:
            pass

    if not url:
        return None

    url = url.rstrip("/")
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def start(port=8000):
    """Start cloudflared if not already running, then discover and store the URL."""
    if not CLOUDFLARED.exists():
        return

    # If cloudflared is running and we already have a URL, just load it
    if _is_cloudflared_running() and _URL_FILE.exists():
        try:
            candidate = _URL_FILE.read_text().strip()
            if candidate.startswith("https://"):
                global _url
                with _lock:
                    _url = candidate
                return
        except Exception:
            pass

    # Kill any stale cloudflared and start fresh
    os.system("pkill -f cloudflared 2>/dev/null")
    _URL_FILE.unlink(missing_ok=True)

    import time
    time.sleep(0.5)

    with open(_LOG_FILE, "w") as log:
        subprocess.Popen(
            [str(CLOUDFLARED), "tunnel", "--url", f"http://localhost:{port}"],
            stdout=log,
            stderr=log,
        )

    threading.Thread(target=_discover_url, daemon=True).start()


def _discover_url():
    """Poll the log until the tunnel URL appears, then persist it and update all webhooks."""
    import time
    # Extended to 60 attempts (60s) for slow machines
    for _ in range(60):
        time.sleep(1)
        try:
            with open(_LOG_FILE) as f:
                content = f.read()
            # Match any Cloudflare quick-tunnel hostname
            m = re.search(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", content)
            if not m:
                m = re.search(r"https://[a-z0-9][a-z0-9-]*\.cfargotunnel\.com", content)
            if m:
                url = m.group(0)
                global _url
                with _lock:
                    _url = url
                _URL_FILE.write_text(url)
                _update_env(url)
                _update_woocommerce_webhooks(url)
                return
        except Exception:
            pass


def _update_woocommerce_webhooks(tunnel_url):
    """Update every WooCommerce store's webhooks to point at the new tunnel URL."""
    try:
        import requests, warnings
        warnings.filterwarnings("ignore")
        from stores.models import Store
        from orders.services import setup_woocommerce_webhook
        for store in Store.objects.filter(platform="woocommerce", api_key__isnull=False).exclude(api_key=""):
            try:
                new_url = f"{tunnel_url}/orders/webhook/woocommerce/{store.id}/"
                setup_woocommerce_webhook(store, new_url)
                # Also patch any existing webhooks still pointing to old URL
                resp = requests.get(
                    f"{store.store_url}/wp-json/wc/v3/webhooks",
                    auth=(store.api_key, store.api_secret),
                    timeout=10, verify=False,
                )
                if resp.ok:
                    for wh in resp.json():
                        if isinstance(wh, dict) and wh.get("delivery_url", "").rstrip("/") != new_url.rstrip("/"):
                            requests.put(
                                f"{store.store_url}/wp-json/wc/v3/webhooks/{wh['id']}",
                                auth=(store.api_key, store.api_secret),
                                json={"delivery_url": new_url},
                                timeout=10, verify=False,
                            )
            except Exception:
                pass
    except Exception:
        pass


def _is_cloudflared_running():
    return os.system("pgrep -f cloudflared > /dev/null 2>&1") == 0


def _update_env(url):
    try:
        content = _ENV_FILE.read_text() if _ENV_FILE.exists() else ""
        if "WOOCOMMERCE_BASE_URL" in content:
            content = re.sub(r"WOOCOMMERCE_BASE_URL=.*", f"WOOCOMMERCE_BASE_URL={url}", content)
        else:
            content += f"\nWOOCOMMERCE_BASE_URL={url}"
        _ENV_FILE.write_text(content)
    except Exception:
        pass
