# scripts/one_shot.py
# Logs in to TCGplayer (if credentials provided), saves session to /app/state.json,
# then opens the product page and extracts the "Most Recent Sale" / "Last Sold" price.

import os
import re
import time
import base64
import pathlib
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


STATE_PATH = "/app/state.json"  # persisted session cookies (inside container FS)

def _to_money_float(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace("$", "").replace(",", ""))
    except:
        return None

def _extract_recent_sale_from_html(html: str) -> float | None:
    """
    Try to find a price near label 'Most Recent Sale' or 'Last Sold'.
    Fallback: first money-looking token in the page (least preferred).
    """
    soup = BeautifulSoup(html, "lxml")

    # Primary: look near explicit label text
    labels = soup.find_all(string=re.compile(r"(Most\s+Recent\s+Sale|Last\s+Sold)", re.I))
    for node in labels:
        el = node.parent
        # walk up to 4 levels to catch structured containers
        for _ in range(4):
            if not el:
                break
            txt = el.get_text(" ", strip=True)
            val = _to_money_float(txt)
            if val is not None:
                return val
            el = el.parent

    # Fallback: the first money token in body text (not ideal)
    return _to_money_float(soup.get_text(" ", strip=True))

def _click_consent_if_present(page):
    for sel in [
        'button:has-text("Accept All")',
        'button:has-text("I Accept")',
        '[data-testid="accept-all"]',
        'button[aria-label="Accept"]',
    ]:
        try:
            page.locator(sel).click(timeout=1500)
            break
        except Exception:
            pass

def _ensure_login(context) -> None:
    """
    If creds exist and we don't have a stored session yet, attempt email/password login once.
    This will NO-OP if already logged in or if no credentials provided.
    MFA prompts will cause an early return (cannot solve automatically).
    """
    email = os.getenv("TCG_EMAIL")
    password = os.getenv("TCG_PASSWORD")
    if not email or not password:
        return  # anonymous mode

    page = context.new_page()
    try:
        page.goto("https://www.tcgplayer.com/login", wait_until="domcontentloaded", timeout=30000)

        # If redirected away from login page, assume already logged in
        if "login" not in page.url.lower():
            context.storage_state(path=STATE_PATH)
            return

        # Fill email
        for sel in ['input[name="email"]', 'input[type="email"]', '#email']:
            try:
                page.fill(sel, email, timeout=4000)
                break
            except PWTimeout:
                pass

        # Fill password
        for sel in ['input[name="password"]', 'input[type="password"]', '#password']:
            try:
                page.fill(sel, password, timeout=4000)
                break
            except PWTimeout:
                pass

        # Submit
        for sel in ['button[type="submit"]', 'button:has-text("Sign In")', 'button:has-text("Log In")']:
            try:
                page.click(sel, timeout=4000)
                break
            except PWTimeout:
                pass

        # wait a moment for redirect
        page.wait_for_timeout(2000)

        # crude MFA/challenge detection
        body = (page.text_content("body") or "").lower()
        if any(k in body for k in ["verify", "verification", "code", "2fa", "two-factor", "one-time password"]):
            # can't solve automatically; don't save invalid state
            return

        # store session for reuse
        context.storage_state(path=STATE_PATH)
    finally:
        page.close()

def fetch_last_sold_once(url: str) -> dict:
    t0 = time.time()
    debug = os.getenv("DEBUG") == "1"

    with sync_playwright() as p:
        # reuse cookies if we have them
        storage_state_path = STATE_PATH if pathlib.Path(STATE_PATH).exists() else None

        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            storage_state=storage_state_path
        )

        # If no saved session and creds exist, try to login once
        if storage_state_path is None:
            _ensure_login(context)

        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)

            # optional: settle network + accept consent
            page.wait_for_load_state("networkidle", timeout=15000)
            _click_consent_if_present(page)

            # quick anti-bot check
            title = page.title()
            body = (page.text_content("body") or "").lower()
            if "access denied" in title.lower() or "verify you are a human" in body:
                return {
                    "url": url,
                    "most_recent_sale": None,
                    "error": "blocked_or_challenge",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # try DOM-first parse for nearby price, then BS4 fallback
            html = page.content()
            price = _extract_recent_sale_from_html(html)

            # Light debug (optional)
            if debug and price is None:
                img_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
                # DO NOT print entire html; only head to keep logs small
                print("[DEBUG] title:", title)
                print("[DEBUG] url:", page.url)
                print("[DEBUG] html_head:", html[:1500].replace("\n", " "))
                print("[DEBUG] screenshot_b64_prefix:", img_b64[:120])

            # if we just logged in successfully, persist state for next time
            if not pathlib.Path(STATE_PATH).exists():
                context.storage_state(path=STATE_PATH)

            return {
                "url": url,
                "most_recent_sale": price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close()
            browser.close()
