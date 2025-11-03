import os
import io
import csv
import time
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

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
API_URL = "https://classic.avantlink.com/api.php"
AV_KEY = os.environ.get("AV_AUTH_KEY")           # REQUIRED in prod
MERCHANT_ID = os.environ.get("AV_MERCHANT_ID")   # REQUIRED in prod
REPORT_ID = os.environ.get("AV_REPORT_ID", "20")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))

# --- Browser-like fetch session ---
SESSION = requests.Session()
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/119.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

def _origin(u: str) -> str:
    p = urlparse(u);  return urlunparse((p.scheme, p.netloc, "", "", "", ""))

def fetch_html_with_fallback(url: str, timeout: float) -> str:
    """
    Try:
      1) Direct GET with realistic headers + small retries for 403/404/429/503
      2) If 200, try <link rel='amphtml'>
      3) Try common AMP variants: /amp, ?amp=1, ?outputType=amp
    """
    def _get(u, referer=None):
        h = dict(BROWSER_HEADERS);  h["Referer"] = referer or _origin(u)
        return SESSION.get(u, headers=h, timeout=timeout, allow_redirects=True)

    last_err = None

    # Tier 1: direct with brief retries
    for attempt in range(3):
        try:
            resp = _get(url)
            if resp.status_code == 200 and "<html" in resp.text.lower():
                return resp.text
            if resp.status_code in (403, 404, 429, 503):
                last_err = f"{resp.status_code} {resp.reason}"
                time.sleep(0.6 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(0.6 * (attempt + 1))

    # Tier 2: discover explicit amphtml link
    try:
        resp = _get(url)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            amp = soup.find("link", rel=lambda v: v and "amphtml" in v.lower())
            if amp and amp.get("href"):
                amp_url = amp["href"]
                amp_resp = _get(amp_url, referer=url)
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
        for cand in candidates:
            amp_resp = _get(cand, referer=url)
            if amp_resp.status_code == 200:
                return amp_resp.text
    except Exception:
        pass

    raise requests.RequestException(f"Blocked or unavailable: {last_err or 'unknown error'}")

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
    return app.send_static_file("index.html")

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/api/check-links", methods=["POST"])
def check_links():
    """
    Body:
    {
      "urls": ["https://example.com/a", "https://example.com/b"],
      "brand": "sitkagear.com",
      "affiliateKeywords": ["avantlink", "avad=", "utm_source=avantlink"]
    }
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    brand = (data.get("brand") or "").lower().strip()
    affiliate_keywords = [str(kw).lower() for kw in (data.get("affiliateKeywords") or [])]

    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "Provide a non-empty 'urls' array."}), 400
    if not brand:
        return jsonify({"error": "Provide 'brand' (e.g., 'sitkagear.com')."}), 400

    results = []
    for url in urls:
        try:
            html = fetch_html_strict(url, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(html, "html.parser")
            anchors = soup.find_all("a", href=True)
            hrefs = [a["href"].strip() for a in anchors if a.get("href")]

            affiliate_href = next(
                (h for h in hrefs if any(kw in h.lower() for kw in affiliate_keywords)),
                None
            )
            if affiliate_href:
                results.append({"url": url, "link": affiliate_href, "status": "✅ Affiliate link to brand found"})
                continue

            direct_href = next(
                (h for h in hrefs if brand in h.lower() and not any(kw in h.lower() for kw in affiliate_keywords)),
                None
            )
            if direct_href:
                results.append({"url": url, "link": direct_href, "status": "✅ Direct link to brand found"})
            else:
                results.append({"url": url, "link": None, "status": "❌ No relevant link found"})

        except requests.RequestException as e:
            logger.warning("Fetch error for %s: %s", url, e, exc_info=False)
            results.append({"url": url, "link": None, "status": f"Error fetching page (likely blocked): {e}"})
        except Exception as e:
            logger.exception("Unexpected error for %s", url)
            results.append({"url": url, "link": None, "status": f"Unexpected error: {e}"})

    return jsonify(results), 200

@app.route("/api/avantlink-report", methods=["POST"])
def avantlink_report():
    """
    Body:
    {
      "urls": ["https://www.sitkagear.com/products/mesa-pant/buckskin?..."],
      "start_date": "2025-07-01",
      "end_date": "2025-07-31"
    }
    """
    if not AV_KEY or not MERCHANT_ID:
        return jsonify({"error": "Missing AV_AUTH_KEY or AV_MERCHANT_ID in environment."}), 500

    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "Provide a non-empty 'urls' array."}), 400

    today = datetime.now(timezone.utc).date()
    end_date = data.get("end_date") or today.strftime("%Y-%m-%d")
    start_date = data.get("start_date") or (today - timedelta(days=30)).strftime("%Y-%m-%d")

    params = {
        "module":      "MerchantReport",
        "auth_key":    AV_KEY,
        "merchant_id": MERCHANT_ID,
        "report_id":   REPORT_ID,
        "date_begin":  start_date,
        "date_end":    end_date,
        "format":      "csv",
    }

    headers = {"User-Agent": "AvantLinkReporter/1.0"}
    try:
        r = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("AvantLink request failed: %s", e)
        return jsonify({"error": f"AvantLink request failed: {e}"}), 502

    reader = csv.DictReader(io.StringIO(r.text))
    report = {u: {"clicks": 0, "sales": 0, "revenue": 0.0} for u in urls}

    def _get_num(row, *keys, cast=int, default=0):
        for k in keys:
            if k in row and row[k] not in (None, "", "NA"):
                try:
                    return cast(row[k].replace(",", ""))
                except Exception:
                    continue
        return default

    for row in reader:
        link_url = row.get("LinkURL") or row.get("link") or row.get("Link Url") or row.get("URL")
        if not link_url:
            continue
        if link_url in report:
            clicks  = _get_num(row, "Clicks", cast=int, default=0)
            sales   = _get_num(row, "Sales", "Orders", cast=int, default=0)
            revenue = _get_num(row, "Commission", "Revenue", "Total Commission", cast=float, default=0.0)
            report[link_url] = {"clicks": clicks, "sales": sales, "revenue": revenue}

    return jsonify(report), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
