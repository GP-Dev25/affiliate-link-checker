import os
import time
import random
import logging
from threading import Lock
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional headless flag
USE_HEADLESS = os.getenv("USE_HEADLESS", "false").lower() == "true"

# --- App setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# --- Config / ENV ---
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
FETCH_THROTTLE_SECONDS = float(os.getenv("FETCH_THROTTLE_SECONDS", "1.0"))

# --- Browser-like fetch session ---
SESSION = requests.Session()
retry_strategy = Retry(
    total=3,
    status_forcelist=(403, 404, 408, 409, 429, 500, 502, 503, 504),
    allowed_methods=("HEAD", "GET", "OPTIONS"),
    backoff_factor=0.6,
    raise_on_status=False,
    respect_retry_after_header=True,
)
adapter = HTTPAdapter(max_retries=retry_strategy)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)
_DEFAULT_USER_AGENTS = [
    os.getenv("CUSTOM_USER_AGENT", "").strip() or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.6261.128 Safari/537.36"
    ),
]

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Not.A/Brand";v="24", "Chromium";v="123", "Google Chrome";v="123"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

_HOST_LAST_FETCH: dict[str, float] = {}
_HOST_FETCH_LOCK = Lock()

def _origin(u: str) -> str:
    p = urlparse(u);  return urlunparse((p.scheme, p.netloc, "", "", "", ""))

def _choose_user_agent(attempt: int) -> str:
    # Cycle through a desktop Chrome, Safari, and Linux Chrome UA.
    # Later attempts re-use the options to avoid unbounded growth.
    return _DEFAULT_USER_AGENTS[attempt % len(_DEFAULT_USER_AGENTS)]


def _respect_host_throttle(url: str) -> None:
    """Sleep briefly to avoid hammering the same host repeatedly."""
    if FETCH_THROTTLE_SECONDS <= 0:
        return

    host = urlparse(url).netloc.lower()
    if not host:
        return

    now = time.monotonic()
    jitter_upper = max(0.05, min(0.35, FETCH_THROTTLE_SECONDS))
    jitter = random.uniform(0.01, jitter_upper)

    with _HOST_FETCH_LOCK:
        last = _HOST_LAST_FETCH.get(host)
        wait = 0.0 if last is None else max(0.0, (last + FETCH_THROTTLE_SECONDS) - now)
        _HOST_LAST_FETCH[host] = now + wait + jitter

    if wait:
        time.sleep(wait)
    time.sleep(jitter)


def fetch_html_with_fallback(url: str, timeout: float) -> str:
    """
    Try:
      1) Direct GET with realistic headers + small retries for 403/404/429/503
      2) If 200, try <link rel='amphtml'>
      3) Try common AMP variants: /amp, ?amp=1, ?outputType=amp
    """
    def _get(u, *, referer=None, attempt=0):
        _respect_host_throttle(u)
        h = dict(BROWSER_HEADERS)
        h["Referer"] = referer or _origin(u)
        h["User-Agent"] = _choose_user_agent(attempt)
        return SESSION.get(u, headers=h, timeout=timeout, allow_redirects=True)

    last_err = None

    # Tier 1: direct with brief retries
    for attempt in range(len(_DEFAULT_USER_AGENTS) * 2):
        try:
            resp = _get(url, attempt=attempt)
            if resp.status_code == 200 and "<html" in resp.text.lower():
                return resp.text
            if resp.status_code in (403, 404, 429, 503):
                last_err = f"{resp.status_code} {resp.reason}"
                if resp.status_code == 403:
                    SESSION.cookies.clear()
                time.sleep(0.6 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(0.6 * (attempt + 1))

    # Tier 2: discover explicit amphtml link
    try:
        resp = _get(url, attempt=0)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            amp = soup.find("link", rel=lambda v: v and "amphtml" in v.lower())
            if amp and amp.get("href"):
                amp_url = amp["href"]
                amp_resp = _get(amp_url, referer=url, attempt=1)
                if amp_resp.status_code == 200:
                    return amp_resp.text
    except Exception:
        pass

    # Tier 3: common AMP patterns
    try:
        p = urlparse(url)
        candidates = [
            url.rstrip("/") + "/amp",
            urlunparse((p.scheme, p.netloc, p.path, p.params,
                        ("amp=1" if not p.query else p.query + "&amp=1"), p.fragment)),
            urlunparse((p.scheme, p.netloc, p.path, p.params,
                        ("outputType=amp" if not p.query else p.query + "&outputType=amp"), p.fragment)),
        ]
        for index, cand in enumerate(candidates, start=1):
            amp_resp = _get(cand, referer=url, attempt=index)
            if amp_resp.status_code == 200:
                return amp_resp.text
    except Exception:
        pass

    raise requests.RequestException(f"Blocked or unavailable: {last_err or 'unknown error'}")


def _unique_preserve_order(values):
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out

# --- Optional headless fallback (Playwright) ---
# Only imported/used if USE_HEADLESS=true
if USE_HEADLESS:
    import asyncio
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as e:
        async_playwright = None
        logger.warning("Playwright not available: %s", e)

    async def _render_playwright(url: str) -> str:
        if not async_playwright:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            html = await page.content()
            await browser.close()
            return html

    def fetch_html_strict(url: str, timeout: float) -> str:
        try:
            return fetch_html_with_fallback(url, timeout)
        except Exception as e:
            logger.info("Fallback to headless for %s due to %s", url, e)
            return asyncio.run(_render_playwright(url))
else:
    def fetch_html_strict(url: str, timeout: float) -> str:
        # When headless disabled, just use the Lite fetcher
        return fetch_html_with_fallback(url, timeout)

# --- Routes ---
@app.route("/", methods=["GET"])
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/api/check-links", methods=["POST"])
def check_links():
    """
    Body:
    {
      "urls": ["https://example.com/a", "https://example.com/b"]
    }
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []

    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "Provide a non-empty 'urls' array."}), 400

    results = []
    for url in urls:
        try:
            html = fetch_html_strict(url, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(html, "html.parser")
            anchors = soup.find_all("a", href=True)

            hrefs = []
            for anchor in anchors:
                raw_href = anchor.get("href")
                if not raw_href:
                    continue
                normalized = urljoin(url, raw_href.strip())
                if not normalized:
                    continue
                hrefs.append(normalized)

            links = _unique_preserve_order(hrefs)

            status = f"Found {len(links)} link(s)" if links else "No links found"
            results.append({
                "url": url,
                "links": links,
                "status": status,
            })

        except requests.RequestException as e:
            logger.warning("Fetch error for %s: %s", url, e, exc_info=False)
            results.append({
                "url": url,
                "links": [],
                "status": "Error fetching page (likely blocked)",
                "error": str(e),
            })
        except Exception as e:
            logger.exception("Unexpected error for %s", url)
            results.append({
                "url": url,
                "links": [],
                "status": "Unexpected error",
                "error": str(e),
            })

    return jsonify(results), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
