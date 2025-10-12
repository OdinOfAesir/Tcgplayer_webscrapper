# scripts/one_shot.py
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PRICE_RE = re.compile(r"\$\s?([\d{1,3}(?:,\d{3})*]+(?:\.\d{2})?)|\$([0-9][0-9,]*\.?[0-9]{0,2})")

def _to_float(price_text: str) -> float | None:
    if not price_text:
        return None
    # strip currency/commas
    cleaned = price_text.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except:
        return None

def _extract_price_by_dom(page_html: str) -> float | None:
    """
    Try to locate a price near a 'Most Recent Sale' / 'Last Sold' label.
    Fallback: first price-like token on the page (worst-case).
    """
    soup = BeautifulSoup(page_html, "lxml")

    # 1) Look for label text and its nearby price
    candidates = soup.find_all(text=re.compile(r"(Most\s+Recent\s+Sale|Last\s+Sold)", re.I))
    for node in candidates:
        # search close parent/sibling for a $amount
        container = node.parent
        for _ in range(4):  # walk up a few levels just in case
            if not container:
                break
            text_blob = container.get_text(" ", strip=True)
            m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", text_blob)
            if m:
                return _to_float(m.group(0))
            container = container.parent

    # 2) fallback: first price-looking token on entire page (not ideal, but better than nothing)
    m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", soup.get_text(" ", strip=True))
    if m:
        return _to_float(m.group(0))
    return None

def fetch_last_sold_once(url: str) -> dict:
    """
    Open the product page headlessly and return the most recent sale price if found.
    NOTE: This may be blocked by anti-bot protections. Keep request rates low.
    """
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        try:
            # Go to the page and wait for network to settle a bit
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Optional: small waits to allow client-side price widgets to appear
            page.wait_for_timeout(1500)

            # Quick anti-bot check: if page title/body indicates a challenge, bail gracefully
            title = page.title()
            body_text = page.text_content("body") or ""
            if "Access denied" in title or "verify you are a human" in body_text.lower():
                return {
                    "url": url,
                    "most_recent_sale": None,
                    "error": "blocked_or_challenge",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            html = page.content()
            price = _extract_price_by_dom(html)

            return {
                "url": url,
                "most_recent_sale": price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close()
            browser.close()
