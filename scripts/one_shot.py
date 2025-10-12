# scripts/one_shot.py
# Robust one-shot scrapers:
#  - fetch_last_sold_once(url)
#  - fetch_sales_snapshot(url)
#
# Improvements:
#  * Configurable timeouts via env: TIMEOUT_MS (default 45000), SNAPSHOT_WAIT_MS (default 12000)
#  * Resilient navigation with retries/backoff
#  * Smarter consent handling, scrolling, and multiple selector strategies
#  * Clear error payloads instead of generic timeouts

# scripts/one_shot.py

import os          # <-- add this
import re
import time
import base64
import pathlib
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

import uuid
DEBUG_DIR = "/app/debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

def _save_debug(page, tag: str):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex[:8]
    base = f"{DEBUG_DIR}/{tag}-{ts}-{uid}"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass
    return {"screenshot": f"{base}.png", "html": f"{base}.html"}

STATE_PATH = "/app/state.json"  # persisted session cookies (inside container FS)

# ---- Configurable timeouts (ms) via env ----
def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
        return v if v > 0 else default
    except Exception:
        return default

NAV_TIMEOUT_MS      = _env_int("TIMEOUT_MS", 45000)        # for page.goto / network settles
SNAPSHOT_WAIT_MS    = _env_int("SNAPSHOT_WAIT_MS", 12000)  # for dialog to appear
RETRY_TIMES         = _env_int("RETRY_TIMES", 2)           # navigation/dialog retries
DEBUG               = os.getenv("DEBUG") == "1"

# -----------------------------
# Utilities
# -----------------------------

def _to_money_float(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace("$", "").replace(",", ""))
    except:
        return None

def _extract_recent_sale_from_html(html: str) -> Optional[float]:
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
        '[aria-label*="Accept all"]',
    ]:
        try:
            if page.locator(sel).first.is_visible():
                page.locator(sel).first.click(timeout=1500)
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
        page.goto("https://www.tcgplayer.com/login", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

        # If redirected away from login page, assume already logged in
        if "login" not in page.url.lower():
            context.storage_state(path=STATE_PATH)
            return

        # Fill email
        for sel in ['input[name="email"]', 'input[type="email"]', '#email']:
            try:
                page.fill(sel, email, timeout=4000); break
            except PWTimeout:
                pass

        # Fill password
        for sel in ['input[name="password"]', 'input[type="password"]', '#password']:
            try:
                page.fill(sel, password, timeout=4000); break
            except PWTimeout:
                pass

        # Submit
        for sel in ['button[type="submit"]', 'button:has-text("Sign In")', 'button:has-text("Log In")']:
            try:
                page.click(sel, timeout=4000); break
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

def _anti_bot_check(page: Page) -> Optional[str]:
    title = (page.title() or "")
    body = (page.text_content("body") or "").lower()
    if "access denied" in title.lower() or "verify you are a human" in body or "are you human" in body:
        return "blocked_or_challenge"
    return None

def _goto_with_retries(page: Page, url: str) -> None:
    """Navigate with retries & exponential backoff; favor 'load' then settle to 'networkidle'."""
    last_err = None
    for attempt in range(RETRY_TIMES + 1):
        try:
            page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            # give client-side scripts time to attach
            try:
                page.wait_for_load_state("networkidle", timeout=min(15000, NAV_TIMEOUT_MS // 2))
            except Exception:
                # not fatal; proceed
                pass
            return
        except Exception as e:
            last_err = e
            page.wait_for_timeout(500 * (attempt + 1))  # backoff
    raise last_err if last_err else RuntimeError("navigation failed")

# -----------------------------
# Public: previous function
# -----------------------------

def fetch_last_sold_once(url: str) -> dict:
    t0 = time.time()

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # If no saved session and creds exist, try to login once
        if not pathlib.Path(STATE_PATH).exists():
            _ensure_login(context)

        page = context.new_page()
        try:
            _goto_with_retries(page, url)
            _click_consent_if_present(page)
        except Exception as e:
            art = _save_debug(page, "nav-failed")
            return {
                "url": url, "error": "timeout_nav", "reason": str(e),
                "artifacts": art,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }


            html = page.content()
            price = _extract_recent_sale_from_html(html)

            if not pathlib.Path(STATE_PATH).exists():
                context.storage_state(path=STATE_PATH)

            if DEBUG and price is None:
                img_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
                print("[DEBUG] last_sold title:", page.title())
                print("[DEBUG] last_sold url:", page.url)
                print("[DEBUG] last_sold html_head:", html[:1500].replace("\n", " "))
                print("[DEBUG] last_sold screenshot_prefix:", img_b64[:120])

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
    """Parse any <table> elements inside the dialog."""
    soup = BeautifulSoup(html, "lxml")
    tables_out: List[Dict[str, Any]] = []

    for t in soup.find_all("table"):
        # headers
        headers = []
        thead = t.find("thead")
        if thead:
            ths = thead.find_all(["th", "td"])
            headers = [th.get_text(" ", strip=True) for th in ths if th.get_text(strip=True)]
        else:
            first_tr = t.find("tr")
            if first_tr:
                headers = [cell.get_text(" ", strip=True) for cell in first_tr.find_all(["th", "td"])]

        # rows
        rows = []
        tbodies = t.find_all("tbody") or [t]
        for body in tbodies:
            for tr in body.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if headers and cells == headers:
                    continue
                if headers and len(headers) == len(cells):
                    rows.append({headers[i] or f"col_{i}": cells[i] for i in range(len(cells))})
                else:
                    rows.append({"cols": cells})

        tables_out.append({"headers": headers, "rows": rows})

    return tables_out

def _extract_key_values_from_dialog_html(html: str) -> List[Dict[str, str]]:
    """Extract simple key-value stats if the dialog uses <dl>, or scattered 'Label: Value' text."""
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

def _scroll_into_view(page: Page, selector: str) -> None:
    try:
        loc = page.locator(selector).first
        if loc.count() > 0:
            loc.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass

def _open_snapshot_dialog(page: Page) -> None:
    """
    Click the 'history' button to open 'Sales History Snapshot' dialog.
    Tries multiple strategies and waits for the dialog to appear.
    """
    # ensure the header block is in view
    _scroll_into_view(page, ".latest-sales__header__history")

    # Try clicking known button first
    clicked = False
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
                loc.click(timeout=2500)
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        # Try JS click on the container (sometimes overlays intercept normal clicks)
        try:
            page.evaluate("""
              (sel)=>{ const el=document.querySelector(sel); if(el){ el.click(); return true;} return false; }
            """, ".latest-sales__header__history")
            clicked = True
        except Exception:
            pass

    # Wait for dialog: role-based first, then text-based
    last_err = None
    for attempt in range(RETRY_TIMES + 1):
        try:
            dlg = page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
            dlg.wait_for(timeout=SNAPSHOT_WAIT_MS)
            return
        except Exception as e:
            last_err = e
            try:
                page.locator('text=/Sales\\s+History\\s+Snapshot/i').first.wait_for(timeout=SNAPSHOT_WAIT_MS)
                return
            except Exception as e2:
                last_err = e2
                page.wait_for_timeout(500 * (attempt + 1))  # small backoff
    raise last_err if last_err else RuntimeError("snapshot dialog not found")

def fetch_sales_snapshot(url: str) -> dict:
    """
    Navigate to product page, open Sales History Snapshot dialog,
    parse all tables + key-values + top-level text into JSON.
    """
    t0 = time.time()

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # If no saved session and creds exist, try to login once
        if not pathlib.Path(STATE_PATH).exists():
            _ensure_login(context)

        page = context.new_page()
        try:
            _goto_with_retries(page, url)
            _click_consent_if_present(page)
        except Exception as e:
            art = _save_debug(page, "nav-failed")
            return {
                "url": url, "error": "timeout_nav", "reason": str(e),
                "artifacts": art,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

            # Open the dialog (with retries)
            try:
                _open_snapshot_dialog(page)
            except Exception as e:
                art = _save_debug(page, "dialog-failed")
                return {
                    "url": url, "error": "timeout_dialog", "reason": str(e),
                    "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # Prefer role-based dialog; else fallback to typical modal containers
            dialog = None
            try:
                dialog = page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
                dialog.wait_for(timeout=3000)
            except Exception:
                pass

            if dialog:
                dialog_html = dialog.inner_html()
                dialog_text = dialog.inner_text()
                title = "Sales History Snapshot"
            else:
                # fallback: most visible modal-ish container
                possible = page.locator(
                    'css=[role="dialog"], .modal, .MuiDialog-paper, .chakra-modal__content, [class*="dialog"], [class*="modal"]'
                ).first
                if possible and possible.is_visible():
                    dialog_html = possible.inner_html()
                    dialog_text = possible.inner_text()
                    title = "Sales History Snapshot"
                else:
                    # last resort: entire page
                    dialog_html = page.content()
                    dialog_text = page.inner_text("body")
                    title = "Sales History Snapshot (fallback)"

            tables = _extract_tables_from_dialog_html(dialog_html)
            stats  = _extract_key_values_from_dialog_html(dialog_html)

            if not pathlib.Path(STATE_PATH).exists():
                context.storage_state(path=STATE_PATH)

            if DEBUG:
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
                "tables": tables,
                "stats": stats,
                "text": dialog_text.strip() if dialog_text else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close(); browser.close()
