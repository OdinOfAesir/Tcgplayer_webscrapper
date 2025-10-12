# scripts/one_shot.py
# - Robust login with artifacts BEFORE and AFTER login
# - Auto login + verification used by scrapers; retries when not logged in
# - Endpoints can call debug_login_only() to test login separately
# - Still includes last-sold and sales-snapshot logic with retries/timeouts/artifacts

import os
import re
import time
import base64
import pathlib
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

STATE_PATH = "/app/state.json"   # persisted cookies/session
DEBUG_DIR  = "/app/debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

# ---- Configurable via env ----
def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.getenv(name) or "").strip())
        return v if v > 0 else default
    except Exception:
        return default

NAV_TIMEOUT_MS   = _env_int("TIMEOUT_MS", 60000)        # navigation (goto/load)
SNAPSHOT_WAIT_MS = _env_int("SNAPSHOT_WAIT_MS", 45000)  # dialog wait
RETRY_TIMES      = _env_int("RETRY_TIMES", 3)
DEBUG            = (os.getenv("DEBUG") == "1")

# -----------------------------
# Helpers / Artifacts
# -----------------------------
def _save_debug(page: Page, tag: str) -> Dict[str, str]:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex[:8]
    base = f"{DEBUG_DIR}/{tag}-{ts}-{uid}"
    paths = {}
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        paths["screenshot"] = f"{base}.png"
    except Exception:
        pass
    try:
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        paths["html"] = f"{base}.html"
    except Exception:
        pass
    return paths

def _to_money_float(text: str) -> Optional[float]:
    if not text: return None
    m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", text)
    if not m: return None
    try: return float(m.group(0).replace("$","").replace(",",""))
    except: return None

def _extract_recent_sale_from_html(html: str) -> Optional[float]:
    soup = BeautifulSoup(html, "lxml")
    labels = soup.find_all(string=re.compile(r"(Most\s+Recent\s+Sale|Last\s+Sold)", re.I))
    for node in labels:
        el = node.parent
        for _ in range(4):
            if not el: break
            val = _to_money_float(el.get_text(" ", strip=True))
            if val is not None: return val
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
            loc = page.locator(sel).first
            if loc and loc.is_visible():
                loc.click(timeout=1500)
                break
        except Exception:
            pass

def _new_context(p, use_saved_state: bool):
    storage_state_path = STATE_PATH if (use_saved_state and pathlib.Path(STATE_PATH).exists()) else None
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        storage_state=storage_state_path
    )
    return browser, context

def _anti_bot_check(page: Page) -> Optional[str]:
    title = (page.title() or "")
    body  = (page.text_content("body") or "").lower()
    if "access denied" in title.lower() or "verify you are a human" in body or "are you human" in body:
        return "blocked_or_challenge"
    return None

def _goto_with_retries(page: Page, url: str) -> None:
    last_err = None
    for attempt in range(RETRY_TIMES + 1):
        try:
            page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=min(15000, NAV_TIMEOUT_MS // 2))
            except Exception:
                pass
            return
        except Exception as e:
            last_err = e
            page.wait_for_timeout(500 * (attempt + 1))
    raise last_err if last_err else RuntimeError("navigation failed")

# -----------------------------
# Login (robust + artifacts + verification)
# -----------------------------
def _is_logged_in(page: Page) -> bool:
    """
    Heuristics: check for elements typically present AFTER login and ABSENT before.
    We avoid site-specific secrets but try common patterns.
    """
    try:
        # If the URL contains /login, definitely not logged in
        if "/login" in (page.url or "").lower():
            return False
    except Exception:
        pass

    # Look for 'Sign In' / 'Log In' patterns that indicate logged-out state
    try:
        if page.locator('text=/Sign\\s*In|Log\\s*In/i').first.is_visible():
            return False
    except Exception:
        pass

    # Look for profile/account/notifications/cart indicators (common post-login)
    for sel in [
        '[aria-label*="Account"]',
        '[data-testid*="account"]',
        '[aria-label*="Profile"]',
        'a[href*="/myaccount"]',
        'button[aria-label*="Account"]',
        '.header__account', '.AccountMenu', '.user-menu',
    ]:
        try:
            loc = page.locator(sel).first
            if loc and loc.is_visible():
                return True
        except Exception:
            pass

    # Fallback: absence of obvious login link + presence of generic nav items
    return False

def _do_login_flow(context, capture=True) -> Dict[str, Any]:
    """
    Open login page, capture BEFORE, fill creds, submit, wait, capture AFTER.
    Save state on success. Return dict with status and artifact paths.
    """
    email = os.getenv("TCG_EMAIL")
    password = os.getenv("TCG_PASSWORD")
    if not email or not password:
        return {"ok": False, "error": "missing_credentials"}

    page = context.new_page()
    before_paths = {}
    after_paths  = {}
    try:
        page.goto("https://www.tcgplayer.com/login?returnUrl=https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _click_consent_if_present(page)
        if capture:
            before_paths = _save_debug(page, "login-before")

        # Fill email
        filled_email = False
        for sel in ['input[name="email"]', 'input[type="email"]', '#email', 'input[autocomplete="username"]']:
            try:
                page.fill(sel, email, timeout=4000); filled_email = True; break
            except PWTimeout:
                pass

        # Fill password
        filled_pass = False
        for sel in ['input[name="password"]', 'input[type="password"]', '#password', 'input[autocomplete="current-password"]']:
            try:
                page.fill(sel, password, timeout=4000); filled_pass = True; break
            except PWTimeout:
                pass

        if not (filled_email and filled_pass):
            if capture: after_paths = _save_debug(page, "login-after")
            return {"ok": False, "error": "selectors_not_found", "before": before_paths, "after": after_paths}

        # Submit (try several button variants)
        clicked = False
        for sel in [
            'button[type="submit"]',
            'button:has-text("Sign In")',
            'button:has-text("Log In")',
            'button:has-text("Sign in")',
        ]:
            try:
                page.click(sel, timeout=4000); clicked = True; break
            except PWTimeout:
                pass
        if not clicked:
            # Press Enter as fallback
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        # Post-submit: wait for either redirect to homepage or for a known logged-in element to appear
        page.wait_for_timeout(1500)
        # Try a few times: load, then check logged-in indicator
        success = False
        for _ in range(12):
            if _is_logged_in(page):
                success = True
                break
            page.wait_for_timeout(500)

        if capture:
            after_paths = _save_debug(page, "login-after")

        if success:
            context.storage_state(path=STATE_PATH)
            return {"ok": True, "before": before_paths, "after": after_paths}
        else:
            return {"ok": False, "error": "login_verification_failed", "before": before_paths, "after": after_paths}

    finally:
        page.close()

def _ensure_logged_in(context) -> Dict[str, Any]:
    """
    Try to use existing state. If state missing or invalid, perform login.
    Returns {"ok": bool, ...}
    """
    # If we already have a stored state, sanity-check it quickly by loading homepage
    page = context.new_page()
    try:
        page.goto("https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _click_consent_if_present(page)
        if _is_logged_in(page):
            return {"ok": True, "used_existing_state": True}
    except Exception:
        pass
    finally:
        page.close()

    # No valid session → do the login flow
    return _do_login_flow(context, capture=True)

# Public endpoint helper
def debug_login_only() -> Dict[str, Any]:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        try:
            result = _do_login_flow(context, capture=True)
            result.update({
                "elapsed_ms": int((time.time() - t0) * 1000),
                "state_saved": True if result.get("ok") else False
            })
            return result
        finally:
            context.close(); browser.close()

# -----------------------------
# Public: last sold (auto-login if needed)
# -----------------------------
def fetch_last_sold_once(url: str) -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # 1) Ensure logged in (or proceed anonymous if creds missing)
        login_info = _ensure_logged_in(context)

        page = context.new_page()
        try:
            # 2) Navigate
            try:
                _goto_with_retries(page, url)
                _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "nav-failed")
                return {
                    "url": url, "most_recent_sale": None, "error": "timeout_nav", "reason": str(e),
                    "login": login_info,
                    "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # 3) If looks logged out on product page, try login once and reload
            if not _is_logged_in(page):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page()  # new page with fresh storage state
                _goto_with_retries(page, url)
                _click_consent_if_present(page)

            # 4) Price extract
            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "challenge")
                return {
                    "url": url, "most_recent_sale": None, "error": err,
                    "login": login_info,
                    "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            html = page.content()
            price = _extract_recent_sale_from_html(html)

            if DEBUG and price is None:
                img_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
                print("[DEBUG] last_sold title:", page.title())
                print("[DEBUG] last_sold url:", page.url)
                print("[DEBUG] last_sold html_head:", html[:1500].replace("\n", " "))
                print("[DEBUG] last_sold screenshot_prefix:", img_b64[:120])

            return {
                "url": url,
                "most_recent_sale": price,
                "login": login_info,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close(); browser.close()

# -----------------------------
# Dialog helpers & snapshot (auto-login)
# -----------------------------
def _extract_tables_from_dialog_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, Any]] = []
    for t in soup.find_all("table"):
        headers: List[str] = []
        thead = t.find("thead")
        if thead:
            headers = [th.get_text(" ", strip=True) for th in thead.find_all(["th", "td"]) if th.get_text(strip=True)]
        else:
            first = t.find("tr")
            if first:
                headers = [c.get_text(" ", strip=True) for c in first.find_all(["th", "td"])]

        rows: List[Dict[str, Any]] = []
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

        out.append({"headers": headers, "rows": rows})
    return out

def _extract_key_values_from_dialog_html(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, str]] = []

    for dl in soup.find_all("dl"):
        dts = [dt.get_text(" ", strip=True) for dt in dl.find_all("dt")]
        dds = [dd.get_text(" ", strip=True) for dd in dl.find_all("dd")]
        for i in range(min(len(dts), len(dds))):
            if dts[i] or dds[i]:
                out.append({"label": dts[i], "value": dds[i]})

    text = soup.get_text("\n", strip=True)
    for line in text.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            if label.strip() and value.strip():
                out.append({"label": label.strip(), "value": value.strip()})
    return out

def _slow_scroll(page: Page, steps: int = 14):
    try:
        total = page.evaluate("() => document.body.scrollHeight")
        y = 0
        for i in range(steps):
            y = int(total * (i + 1) / steps)
            page.evaluate("(yy) => window.scrollTo(0, yy)", y)
            page.wait_for_timeout(350)
        page.evaluate("() => window.scrollBy(0, -300)")
        page.wait_for_timeout(300)
    except Exception:
        pass

def _open_snapshot_dialog(page: Page, wait_ms: int) -> None:
    _slow_scroll(page, steps=14)
    try:
        page.locator(".latest-sales__header__history").first.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass

    selectors = [
        ".latest-sales__header__history button",
        ".latest-sales__header__history",
        'button:has-text("History")',
        'button[aria-label*="History"]',
        'button:has-text("Sales History")',
        '[data-testid*="history"]',
        'xpath=//button[.//text()[contains(., "History")]]'
    ]
    clicked = False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc and loc.is_visible():
                try: loc.hover(timeout=800)
                except Exception: pass
                loc.click(timeout=2000)
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        try:
            ok = page.evaluate("(sel)=>{const el=document.querySelector(sel); if(el){ el.click(); return true } return false }",
                               ".latest-sales__header__history")
            if ok: clicked = True
        except Exception:
            pass

    if not clicked:
        try:
            h = page.locator(".latest-sales__header__history").first
            if h:
                h.focus()
                page.keyboard.press("Enter")
                clicked = True
        except Exception:
            pass

    # Save state after clicking to aid debugging
    _save_debug(page, "after-click-history")

    # Poll for a modal-ish element or title text
    deadline = time.time() + (wait_ms / 1000.0)
    while time.time() < deadline:
        try:
            dlg = page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
            if dlg and dlg.is_visible():
                return
        except Exception:
            pass
        for sel in ['[role="dialog"]', '[aria-modal="true"]', '.modal', '.MuiDialog-paper', '.chakra-modal__content', '[class*="dialog"]', '[class*="modal"]']:
            try:
                loc = page.locator(sel).first
                if loc and loc.is_visible():
                    return
            except Exception:
                pass
        try:
            if page.locator('text=/Sales\\s+History\\s+Snapshot/i').first.is_visible():
                return
        except Exception:
            pass
        page.wait_for_timeout(500)

    raise TimeoutError("Sales History Snapshot dialog not found")

def fetch_sales_snapshot(url: str) -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)

        # 1) Ensure logged in (or attempt login)
        login_info = _ensure_logged_in(context)

        page = context.new_page()
        try:
            # 2) NAV + consent
            try:
                _goto_with_retries(page, url)
                _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "nav-failed")
                return {
                    "url": url, "title": None, "tables": [], "stats": [], "text": None,
                    "error": "timeout_nav", "reason": str(e), "login": login_info, "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # 3) If looks logged out on product page, try login once and reload
            if not _is_logged_in(page):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page()
                _goto_with_retries(page, url)
                _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "challenge")
                return {
                    "url": url, "title": None, "tables": [], "stats": [], "text": None,
                    "error": err, "login": login_info, "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # 4) OPEN DIALOG
            try:
                _open_snapshot_dialog(page, wait_ms=SNAPSHOT_WAIT_MS)
            except Exception as e:
                art = _save_debug(page, "dialog-failed")
                return {
                    "url": url, "title": None, "tables": [], "stats": [], "text": None,
                    "error": "timeout_dialog", "reason": str(e), "login": login_info, "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            # 5) Identify dialog root
            dialog = None
            for sel in [
                'role=dialog[name=/Sales\\s+History\\s+Snapshot/i]',
                '[role="dialog"]',
                '[aria-modal="true"]',
                '.modal', '.MuiDialog-paper', '.chakra-modal__content',
                '[class*="dialog"]', '[class*="modal"]'
            ]:
                try:
                    loc = page.locator(sel).first if not sel.startswith('role=') else page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
                    if loc and loc.is_visible():
                        dialog = loc
                        break
                except Exception:
                    pass

            if not dialog:
                art = _save_debug(page, "dialog-missing-after-open")
                return {
                    "url": url, "title": None, "tables": [], "stats": [], "text": None,
                    "error": "dialog_not_found_after_open", "login": login_info, "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            dialog_html = dialog.inner_html()
            dialog_text = dialog.inner_text()
            title = "Sales History Snapshot"

            tables = _extract_tables_from_dialog_html(dialog_html)
            stats  = _extract_key_values_from_dialog_html(dialog_html)

            if not tables and not stats and (not dialog_text or not dialog_text.strip()):
                art = _save_debug(page, "dialog-empty")
                return {
                    "url": url, "title": title, "tables": [], "stats": [], "text": None,
                    "error": "dialog_empty", "login": login_info, "artifacts": art,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                }

            return {
                "url": url,
                "title": title,
                "tables": tables,
                "stats": stats,
                "text": dialog_text.strip() if dialog_text else None,
                "login": login_info,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
        finally:
            context.close(); browser.close()
