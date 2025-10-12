# scripts/one_shot.py
# - Logs in if env creds provided (TCG_EMAIL / TCG_PASSWORD), saves session to /app/state.json
# - fetch_last_sold_once(url): prior behavior
# - fetch_sales_snapshot(url): NEW — opens "Sales History Snapshot" dialog and extracts tables/text as JSON

import os
import re
import time
import base64
import pathlib
from datetime import datetime, timezone
from typing import List, Dict, Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


STATE_PATH = "/app/state.json"  # persisted session cookies (inside container FS)

# -----------------------------
# Utilities
# -----------------------------

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
    """Find price near 'Most Recent Sale' or 'Last Sold'; fallback to first money token."""
    soup = BeautifulSoup(html, "lxml")

    labels = soup.find_all(string=re.compile(r"(Most\s+Recent\s+Sale|Last\s+Sold)", re.I))
    for node in labels:
        el = node.parent
        for _ in range(4):
            if not el:
                break
            txt = el.get_text(" ", strip=True)
            val = _to_money_float(txt)
            if val is not None:
                return val
            el = el.parent

    return _to_money_float(soup.get_text(" ", strip=True))

def _click_consent_if_present(page: Page):
    for sel in [
        'button:has-text("Accept All")',
        'button:has-text("I Accept")',
        '[data-testid="accept-all"]',
        'button[aria-label*="Accept"]',
    ]:
        try:
            page.locator(sel).click(timeout=1500)
            break
        except Exception:
            pass

def _ensure_login(context) -> None:
    """
    If creds exist and we don't have a stored session yet, attempt email/password login once.
    MFA/challenges will cause an early return.
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

        page.wait_for_timeout(2000)

        # crude MFA/challenge detection
        body = (page.text_content("body") or "").lower()
        if any(k in body for k in ["verify", "verification", "code", "2fa", "two-factor", "one-time password"]):
            return  # cannot solve automatically

        context.storage_state(path=STATE_PATH)
    finally:
        page.close()

def _new_context(p, use_saved_state: bool):
    storage_state_path = STATE_PATH if (use_saved_state and pathlib.Path(STATE_PATH).exists()) else None
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        storage_state=storage_state_path
    )
    return browser, context

def _anti_bot_check(page: Page) -> str | None:
    title = (page.title() or "")
    body = (page.text_content("body") or "").lower()
    if "access denied" in title.lower() or "verify you are a human" in body or "are you human" in body:
        return "blocked_or_challenge"
    return None

# -----------------------------
# Public: previous function
# -----------------------------

def fetch_last_sold_once(url: str) -> dict:
    t0 = time.time()
    debug = os.getenv("DEBUG") == "1"

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # If no saved session and creds exist, try to login once
        if not pathlib.Path(STATE_PATH).exists():
            _ensure_login(context)

        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_load_state("networkidle", timeout=15000)
            _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                return {
                    "url": url, "most_recent_sale": None, "error": err,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            html = page.content()
            price = _extract_recent_sale_from_html(html)

            if not pathlib.Path(STATE_PATH).exists():
                context.storage_state(path=STATE_PATH)

            # Light debug (optional)
            if debug and price is None:
                img_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
                print("[DEBUG] title:", page.title())
                print("[DEBUG] url:", page.url)
                print("[DEBUG] html_head:", html[:1500].replace("\n", " "))
                print("[DEBUG] screenshot_b64_prefix:", img_b64[:120])

            return {
                "url": url,
                "most_recent_sale": price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close(); browser.close()

# -----------------------------
# NEW: Sales History Snapshot
# -----------------------------

def _extract_tables_from_dialog_html(html: str) -> List[Dict[str, Any]]:
    """
    Parse any <table> elements inside the dialog.
    Returns a list of tables with headers + rows.
    """
    soup = BeautifulSoup(html, "lxml")
    tables_out: List[Dict[str, Any]] = []

    # optional: capture any heading near the top
    # (we'll also return top-level text separately)
    for t in soup.find_all("table"):
        # headers
        headers = []
        thead = t.find("thead")
        if thead:
            ths = thead.find_all(["th", "td"])
            headers = [th.get_text(" ", strip=True) for th in ths if th.get_text(strip=True)]
        else:
            # sometimes header row is first tr of tbody
            first_tr = t.find("tr")
            if first_tr:
                headers = [cell.get_text(" ", strip=True) for cell in first_tr.find_all(["th", "td"])]

        # rows
        rows = []
        tbodies = t.find_all("tbody") or [t]
        for body in tbodies:
            for tr in body.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                # skip header-like row duplicated
                if headers and cells == headers:
                    continue
                if headers and len(headers) == len(cells):
                    rows.append({headers[i] or f"col_{i}": cells[i] for i in range(len(cells))})
                else:
                    rows.append({"cols": cells})

        tables_out.append({
            "headers": headers,
            "rows": rows,
        })

    return tables_out

def _extract_key_values_from_dialog_html(html: str) -> List[Dict[str, str]]:
    """
    Extract simple key-value stats if the dialog uses <dl>, or scattered 'Label: Value' text.
    """
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, str]] = []

    # Definition lists
    for dl in soup.find_all("dl"):
        dts = [dt.get_text(" ", strip=True) for dt in dl.find_all("dt")]
        dds = [dd.get_text(" ", strip=True) for dd in dl.find_all("dd")]
        for i in range(min(len(dts), len(dds))):
            if dts[i] or dds[i]:
                out.append({"label": dts[i], "value": dds[i]})

    # Loose label: value patterns
    text = soup.get_text("\n", strip=True)
    for line in text.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            if label.strip() and value.strip():
                out.append({"label": label.strip(), "value": value.strip()})
    return out

def _open_snapshot_dialog(page: Page) -> None:
    """
    Click the 'history' button to open 'Sales History Snapshot' dialog.
    Tries a few robust selectors and waits for the dialog to appear.
    """
    # Try clicking the known container or an internal button
    for sel in [
        ".latest-sales__header__history button",
        ".latest-sales__header__history",
        'button:has-text("History")',
        'button[aria-label*="History"]',
        'button:has-text("Sales History")',
    ]:
        try:
            loc = page.locator(sel).first
            if loc and loc.is_visible():
                loc.click(timeout=3000)
                break
        except Exception:
            pass

    # Wait for a dialog with proper title, or any dialog with the title inside
    try:
        # Preferred: ARIA dialog by name
        dialog = page.get_by_role("dialog", name=re.compile(r"Sales\s+History\s+Snapshot", re.I)).first
        dialog.wait_for(timeout=6000)
        return
    except Exception:
        pass

    # Fallback: wait for text then consider the closest dialog/container
    page.locator('text=/Sales\\s+History\\s+Snapshot/i').first.wait_for(timeout=6000)

def fetch_sales_snapshot(url: str) -> dict:
    """
    Navigate to product page, open Sales History Snapshot dialog,
    parse all tables + key-values + top-level text into JSON.
    """
    t0 = time.time()
    debug = os.getenv("DEBUG") == "1"

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # If no saved session and creds exist, try to login once
        if not pathlib.Path(STATE_PATH).exists():
            _ensure_login(context)

        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_load_state("networkidle", timeout=15000)
            _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                return {
                    "url": url, "title": None, "tables": [], "stats": [], "text": None,
                    "error": err,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # Open the dialog
            _open_snapshot_dialog(page)

            # Try to grab the dialog node (role-based)
            dialog = None
            try:
                dialog = page.get_by_role("dialog", name=re.compile(r"Sales\s+History\s+Snapshot", re.I)).first
                dialog.wait_for(timeout=4000)
            except Exception:
                # Fallback: find the title text and take a nearby container
                pass

            # Get dialog HTML/text (role or fallback)
            if dialog:
                dialog_html = dialog.inner_html()
                dialog_text = dialog.inner_text()
                title = "Sales History Snapshot"
            else:
                # fallback: scope the whole body but still extract around the title text
                title = "Sales History Snapshot"
                # Narrow to a likely modal container (common modal classes)
                possible = page.locator(
                    'css=[role="dialog"], .modal, .MuiDialog-paper, .chakra-modal__content, [class*="dialog"], [class*="modal"]'
                ).first
                if possible and possible.is_visible():
                    dialog_html = possible.inner_html()
                    dialog_text = possible.inner_text()
                else:
                    # last resort: entire page
                    dialog_html = page.content()
                    dialog_text = page.inner_text("body")

            # Parse tables & key-values
            tables = _extract_tables_from_dialog_html(dialog_html)
            stats = _extract_key_values_from_dialog_html(dialog_html)

            if not pathlib.Path(STATE_PATH).exists():
                context.storage_state(path=STATE_PATH)

            # Optional debug
            if debug:
                try:
                    img_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
                    print("[DEBUG][snapshot] url:", page.url)
                    print("[DEBUG][snapshot] dialog_head:", dialog_html[:1500].replace("\n", " "))
                    print("[DEBUG][snapshot] screenshot_prefix:", img_b64[:120])
                except Exception:
                    pass

            return {
                "url": url,
                "title": title,
                "tables": tables,           # list of { headers: [...], rows: [ {col:value} | {cols:[...]} ] }
                "stats": stats,             # list of {label, value} extracted from dl or "Label: Value" lines
                "text": dialog_text.strip() if dialog_text else None,  # full dialog text (for backup)
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close(); browser.close()
