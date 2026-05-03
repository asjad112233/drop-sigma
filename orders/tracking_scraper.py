import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# On Railway, Chromium is installed via apt or playwright install
_CHROMIUM_PATHS = [
    "/app/.playwright/chromium-*/chrome-linux/chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/local/bin/chromium",
]

def _find_chromium():
    import glob
    # Check env var first (set in Dockerfile)
    env_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for pattern in _CHROMIUM_PATHS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None

STATUS_KEYWORDS = [
    "delivered", "out for delivery", "in transit", "picked up",
    "departed", "arrived", "customs", "clearance", "exception",
    "failed", "returned", "on the way", "shipment received",
    "handed over", "accepted", "dispatched", "sorted", "transit",
    "processing", "shipped", "collected", "loaded",
]

# Common tracking URL patterns — {base} and {num} are replaced at runtime
TRACKING_URL_PATTERNS = [
    "{base}/parcelTracking?id={num}",
    "{base}/track/{num}",
    "{base}/tracking/{num}",
    "{base}/track?id={num}",
    "{base}/track?no={num}",
    "{base}/track?number={num}",
    "{base}/track?trackingNumber={num}",
    "{base}/tracking?id={num}",
    "{base}/tracking?no={num}",
    "{base}/tracking?number={num}",
    "{base}/en/track?number={num}",
    "{base}/en/tracking/{num}",
    "{base}/shipment/{num}",
    "{base}/shipment-tracking/{num}",
    "{base}/parcel/{num}",
    "{base}/trace/{num}",
    "{base}/trace?no={num}",
    "{base}/query?no={num}",
    "{base}/results?tracking_number={num}",
]

STATUS_SELECTORS = [
    "[class*='status' i]",
    "[class*='track' i]",
    "[class*='delivery' i]",
    "[class*='shipment' i]",
    "[class*='result' i]",
    "[class*='milestone' i]",
    "[class*='timeline' i]",
    "[class*='event' i]",
    "h1", "h2", "h3",
]


def _extract_status(page, tracking_number: str = "") -> str:
    # For "delivered/completed" claims, the tracking number MUST appear on the page
    # to avoid false positives from courier homepage generic text
    STRONG_KEYWORDS = {"delivered", "completed", "complete"}

    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    def _tracking_on_page():
        if not tracking_number:
            return True
        if tracking_number.lower() in body_text.lower():
            return True
        # URL mein tracking number ho toh hum sahi page pe hain
        try:
            if tracking_number.lower() in page.url.lower():
                return True
        except Exception:
            pass
        return False

    for sel in STATUS_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                txt = (el.inner_text() or "").strip()
                if not txt or len(txt) > 200:
                    continue
                low = txt.lower()
                for kw in STATUS_KEYWORDS:
                    if kw in low:
                        # Strong keywords require tracking number on page
                        if any(sk in low for sk in STRONG_KEYWORDS) and not _tracking_on_page():
                            continue
                        return txt.splitlines()[0].strip()
        except Exception:
            continue

    for line in body_text.splitlines():
        line = line.strip()
        if not line or len(line) > 150:
            continue
        low = line.lower()
        for kw in STATUS_KEYWORDS:
            if kw in low:
                if any(sk in low for sk in STRONG_KEYWORDS) and not _tracking_on_page():
                    continue
                return line

    return ""


def _base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _tracking_number_in_url(url: str, tracking_number: str) -> bool:
    return tracking_number.lower() in url.lower()


def scrape_tracking_status(url: str, tracking_number: str = "", timeout_ms: int = 30000) -> str:
    """
    Try to find tracking status.
    1. If URL already contains the tracking number → scrape it directly.
    2. Otherwise → try common URL patterns with base site + tracking number.
    """
    try:
        with sync_playwright() as p:
            launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]}
            chromium_path = _find_chromium()
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            try:
                direct = _tracking_number_in_url(url, tracking_number) if tracking_number else True

                if direct:
                    # Direct URL — open and scrape
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_timeout(6000)
                    return _extract_status(page, tracking_number)

                # Smart mode: try URL patterns
                base = _base_url(url)
                num = tracking_number

                for pattern in TRACKING_URL_PATTERNS:
                    candidate = pattern.format(base=base, num=num)
                    try:
                        resp = page.goto(candidate, wait_until="domcontentloaded", timeout=12000)
                        if resp and resp.status >= 400:
                            continue
                        page.wait_for_timeout(3500)
                        status = _extract_status(page, tracking_number)
                        if status:
                            return status
                    except PlaywrightTimeout:
                        continue
                    except Exception:
                        continue

                return "Could not find tracking status — try entering the full tracking URL"

            except PlaywrightTimeout:
                return "Timeout - site took too long"
            finally:
                browser.close()

    except Exception as e:
        return f"Error: {str(e)[:80]}"
