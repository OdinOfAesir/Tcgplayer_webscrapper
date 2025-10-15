# scripts/one_shot.py
import os
import re
import time
import base64
import pathlib
import uuid
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

STATE_PATH = "/app/state.json"
DEBUG_DIR  = "/app/debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

# Hydrate /app/state.json from STATE_B64 if missing
if not pathlib.Path(STATE_PATH).exists():
    _b64 = os.getenv("STATE_B64")
    if _b64:
        try:
            data = base64.b64decode(_b64.encode("ascii"))
            _ = json.loads(data.decode("utf-8"))
            with open(STATE_PATH, "wb") as f:
                f.write(data)
            print("[boot] wrote storage state from STATE_B64")
        except Exception as e:
            print("[boot] failed to write state from STATE_B64:", e)

def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.getenv(name) or "").strip())
        return v if v > 0 else default
    except Exception:
        return default

NAV_TIMEOUT_MS   = _env_int("TIMEOUT_MS", 60000)
SNAPSHOT_WAIT_MS = _env_int("SNAPSHOT_WAIT_MS", 45000)
RETRY_TIMES      = _env_int("RETRY_TIMES", 3)
FORCE_STATE_ONLY = (os.getenv("FORCE_STATE_ONLY") == "1")
USER_AGENT       = os.getenv("USER_AGENT") or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
NAV_PLATFORM = os.getenv("NAV_PLATFORM", "MacIntel")
NAV_LANGS    = os.getenv("NAV_LANGS", "en-US,en")
MAX_LISTING_PAGES   = _env_int("LISTING_MAX_PAGES", 20)
LISTING_PAGE_WAIT_MS = _env_int("LISTING_PAGE_WAIT_MS", 20000)

# ---------- proxy ----------
def _parse_proxy_env():
    raw = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if not raw:
        return None
    u = urlparse(raw if "://" in raw else f"http://{raw}")
    if not u.hostname or not u.port:
        print("[proxy] invalid proxy URL:", raw)
        return None
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy["username"] = u.username
    if u.password:
        proxy["password"] = u.password
    print("[proxy] using", proxy["server"], "auth=" + ("yes" if "username" in proxy else "no"))
    return proxy

# ---------- helpers ----------
def _save_debug(page: Page, tag: str) -> Dict[str, str]:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex[:8]
    base = f"{DEBUG_DIR}/{tag}-{ts}-{uid}"
    out: Dict[str, str] = {}
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        out["screenshot"] = f"{base}.png"
    except Exception:
        pass
    try:
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        out["html"] = f"{base}.html"
    except Exception:
        pass
    return out

def _to_money_float(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\$[0-9][0-9,]*\.?[0-9]{0,2}", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace("$", "").replace(",", ""))
    except Exception:
        return None

def _extract_recent_sale_from_html(html: str) -> Optional[float]:
    soup = BeautifulSoup(html, "lxml")
    labels = soup.find_all(string=re.compile(r"(Most\s+Recent\s+Sale|Last\s+Sold)", re.I))
    for node in labels:
        el = node.parent
        for _ in range(4):
            if not el:
                break
            val = _to_money_float(el.get_text(" ", strip=True))
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
            loc = page.locator(sel).first
            if loc and loc.is_visible():
                loc.click(timeout=1500)
                break
        except Exception:
            pass

def _new_context(p, use_saved_state: bool):
    storage_state_path = STATE_PATH if (use_saved_state and pathlib.Path(STATE_PATH).exists()) else None
    proxy_cfg = _parse_proxy_env()
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=USER_AGENT,
        storage_state=storage_state_path,
        device_scale_factor=1.0,
        is_mobile=False,
        has_touch=False,
        java_script_enabled=True,
        proxy=proxy_cfg,
    )
    context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    # Fingerprint
    langs_js = "[" + ",".join([f"'{x.strip()}'" for x in NAV_LANGS.split(",") if x.strip()]) + "]"
    context.add_init_script(f"""
        Object.defineProperty(navigator, 'platform', {{ get: () => '{NAV_PLATFORM}' }});
        Object.defineProperty(navigator, 'languages', {{ get: () => {langs_js} }});
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
        window.chrome = window.chrome || {{ runtime: {{}} }};
    """)
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

def _slow_scroll(page: Page, steps: int = 14):
    try:
        total = page.evaluate("() => document.body.scrollHeight")
        for i in range(steps):
            y = int(total * (i + 1) / steps)
            page.evaluate("(yy) => window.scrollTo(0, yy)", y)
            page.wait_for_timeout(350)
        page.evaluate("() => window.scrollBy(0, -300)")
        page.wait_for_timeout(300)
    except Exception:
        pass

# ---------- login ----------
def _is_logged_in(page: Page) -> bool:
    try:
        if "/login" in (page.url or "").lower():
            return False
    except Exception:
        pass
    try:
        if page.locator('text=/Sign\\s*In|Log\\s*In/i').first.is_visible():
            return False
    except Exception:
        pass
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
    return False

def _do_login_flow(context, capture=True) -> Dict[str, Any]:
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

        filled_email = False
        for sel in ['input[name="email"]', 'input[type="email"]', '#email', 'input[autocomplete="username"]']:
            try:
                page.fill(sel, email, timeout=4000); filled_email = True; break
            except PWTimeout:
                pass

        filled_pass = False
        for sel in ['input[name="password"]', 'input[type="password"]', '#password', 'input[autocomplete="current-password"]']:
            try:
                page.fill(sel, password, timeout=4000); filled_pass = True; break
            except PWTimeout:
                pass

        if not (filled_email and filled_pass):
            if capture: after_paths = _save_debug(page, "login-after")
            return {"ok": False, "error": "selectors_not_found", "before": before_paths, "after": after_paths}

        clicked = False
        for sel in ['button[type="submit"]','button:has-text("Sign In")','button:has-text("Log In")','button:has-text("Sign in")']:
            try:
                page.click(sel, timeout=4000); clicked = True; break
            except PWTimeout:
                pass
        if not clicked:
            try: page.keyboard.press("Enter")
            except Exception: pass

        page.wait_for_timeout(1500)
        success = False
        for _ in range(12):
            if _is_logged_in(page):
                success = True; break
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

    if FORCE_STATE_ONLY:
        return {"ok": True, "state_only": True, "note": "FORCE_STATE_ONLY; not attempting password login"}

    if os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
        return _do_login_flow(context, capture=True)

    return {"ok": False, "error": "no_valid_state_and_no_creds"}

# ---------- debug: login state-only ----------
def debug_login_only() -> Dict[str, Any]:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        try:
            page = context.new_page()
            try:
                page.goto("https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                _click_consent_if_present(page)
                before = _save_debug(page, "login-state-check-before")

                if _is_logged_in(page):
                    after = _save_debug(page, "login-state-check-after")
                    return {"ok": True, "mode": "state_only_check", "before": before, "after": after,
                            "elapsed_ms": int((time.time() - t0) * 1000), "state_path": STATE_PATH}

                if FORCE_STATE_ONLY:
                    after = _save_debug(page, "login-state-check-after")
                    return {"ok": False, "mode": "state_only_check", "error": "not_logged_in_with_state",
                            "before": before, "after": after, "elapsed_ms": int((time.time() - t0) * 1000)}
            finally:
                page.close()

            if not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                result = _do_login_flow(context, capture=True)
                result.update({"elapsed_ms": int((time.time() - t0) * 1000)})
                return result

            return {"ok": False, "mode": "no_state_no_creds", "error": "no_valid_state_and_no_creds",
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

# ---------- scrapers ----------
def fetch_last_sold_once(url: str) -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        login_info = _ensure_logged_in(context)
        page = context.new_page()
        try:
            try:
                _goto_with_retries(page, url); _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "nav-failed")
                return {"url": url, "most_recent_sale": None, "error": "timeout_nav", "reason": str(e),
                        "login": login_info, "artifacts": art, "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}
            if not _is_logged_in(page) and not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page(); _goto_with_retries(page, url); _click_consent_if_present(page)
            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "challenge")
                return {"url": url, "most_recent_sale": None, "error": err, "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(), "elapsed_ms": int((time.time() - t0) * 1000)}
            html = page.content()
            price = _extract_recent_sale_from_html(html)
            return {"url": url, "most_recent_sale": price, "login": login_info,
                    "timestamp": datetime.now(timezone.utc).isoformat(), "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

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
        bodies = t.find_all("tbody") or [t]
        for body in bodies:
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
            label = (dts[i] or "").strip()
            value = (dds[i] or "").strip()
            if label or value:
                out.append({"label": label, "value": value})
    text = soup.get_text("\n", strip=True)
    for line in text.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            if label.strip() and value.strip():
                out.append({"label": label.strip(), "value": value.strip()})
    return out

def _parse_shipping_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if "free" in lowered:
        return 0.0
    return _to_money_float(cleaned)

def _parse_quantity_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        match = re.search(r"\d+", text.replace(",", ""))
        if not match:
            return None
        return int(match.group(0))
    except Exception:
        return None

def _scrape_active_listings_from_dom(page: Page) -> List[Dict[str, Any]]:
    try:
        raw_listings = page.evaluate(
            """
            () => {
                const container = document.querySelector('.product-details__listings');
                if (!container) {
                    return [];
                }
                const records = [];
                const seenKeys = new Set();

                const resolveRoot = (el) => {
                    if (!el) {
                        return null;
                    }
                    const root = el.closest('[data-testid="listing-item"], [data-testid="listing-card"], li, article, .listing-item, .product-listing, .product-details__listing');
                    return root || el.parentElement;
                };

                const findShipping = (priceEl) => {
                    if (!priceEl) {
                        return null;
                    }
                    let sibling = priceEl.nextSibling;
                    while (sibling) {
                        if (sibling.nodeType === Node.TEXT_NODE) {
                            sibling = sibling.nextSibling;
                            continue;
                        }
                        if (sibling.nodeType === Node.ELEMENT_NODE) {
                            const element = sibling;
                            if (element.tagName && element.tagName.toLowerCase() === 'span') {
                                if (!element.className || element.className.toLowerCase().includes('shipping')) {
                                    return element;
                                }
                            }
                        }
                        break;
                    }
                    const parent = priceEl.parentElement;
                    if (parent) {
                        const candidates = parent.querySelectorAll('span:not([class]), span[class*="shipping"], span[class*="Shipping"]');
                        for (const cand of candidates) {
                            if (cand !== priceEl) {
                                return cand;
                            }
                        }
                    }
                    return null;
                };

                const priceNodes = Array.from(container.querySelectorAll('.listing-item__listing-data__info__price'));
                for (const priceEl of priceNodes) {
                    const root = resolveRoot(priceEl);
                    if (!root) {
                        continue;
                    }
                    const key = root.getAttribute('data-listingid') ||
                                root.getAttribute('data-sku') ||
                                root.getAttribute('data-id') ||
                                priceEl.getAttribute('data-sku-id') ||
                                priceEl.getAttribute('data-store-sku') ||
                                (root.id ? `id:${root.id}` : null) ||
                                priceEl.outerHTML.slice(0, 180);
                    if (seenKeys.has(key)) {
                        continue;
                    }
                    seenKeys.add(key);
                    const conditionEl = root.querySelector('.listing-item__listing-data__info__condition');
                    const shippingEl = findShipping(priceEl);
                    const quantityEl = root.querySelector('.add-to-cart__available');
                    const additionalInfoEl = root.querySelector('.listing-item__listing-data__listo');
                    const sellerEl = root.querySelector('.seller-info a');
                    records.push({
                        key,
                        condition: conditionEl ? conditionEl.textContent.trim() : null,
                        priceText: priceEl.textContent.trim(),
                        priceContext: priceEl.parentElement ? priceEl.parentElement.textContent.trim() : priceEl.textContent.trim(),
                        shippingText: shippingEl ? shippingEl.textContent.trim() : null,
                        sellerName: sellerEl ? sellerEl.textContent.trim() : null,
                        quantityText: quantityEl ? quantityEl.textContent.trim() : null,
                        additionalInfo: additionalInfoEl ? additionalInfoEl.textContent.trim() : null
                    });
                }

                if (!priceNodes.length) {
                    const candidates = Array.from(container.querySelectorAll('[data-testid="listing-item"], [data-testid="listing-card"], .listing-item, li, article'));
                    for (const node of candidates) {
                        const priceEl = node.querySelector('.listing-item__listing-data__info__price');
                        if (!priceEl) {
                            continue;
                        }
                        const key = node.getAttribute('data-listingid') ||
                                    node.getAttribute('data-sku') ||
                                    node.getAttribute('data-id') ||
                                    priceEl.getAttribute('data-sku-id') ||
                                    priceEl.getAttribute('data-store-sku') ||
                                    (node.id ? `id:${node.id}` : null) ||
                                    node.outerHTML.slice(0, 180);
                        if (seenKeys.has(key)) {
                            continue;
                        }
                        seenKeys.add(key);
                        const conditionEl = node.querySelector('.listing-item__listing-data__info__condition');
                        const shippingEl = priceEl.nextElementSibling && priceEl.nextElementSibling.tagName && priceEl.nextElementSibling.tagName.toLowerCase() === 'span'
                            ? priceEl.nextElementSibling
                            : null;
                        const quantityEl = node.querySelector('.add-to-cart__available');
                        const additionalInfoEl = node.querySelector('.listing-item__listing-data__listo');
                        const sellerEl = node.querySelector('.seller-info a');
                        records.push({
                            key,
                            condition: conditionEl ? conditionEl.textContent.trim() : null,
                            priceText: priceEl.textContent.trim(),
                            priceContext: priceEl.parentElement ? priceEl.parentElement.textContent.trim() : priceEl.textContent.trim(),
                            shippingText: shippingEl ? shippingEl.textContent.trim() : null,
                            sellerName: sellerEl ? sellerEl.textContent.trim() : null,
                            quantityText: quantityEl ? quantityEl.textContent.trim() : null,
                            additionalInfo: additionalInfoEl ? additionalInfoEl.textContent.trim() : null
                        });
                    }
                }

                return records;
            }
            """
        )
    except Exception:
        return []

    processed: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for entry in raw_listings or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key:
            key = f"listing-{len(processed)}"
        key = str(key)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        price_val = _to_money_float(entry.get("priceText") or "")
        if price_val is None:
            price_val = _to_money_float(entry.get("priceContext") or "")
        if price_val is None:
            continue

        shipping_text = entry.get("shippingText") or ""
        shipping_val = _parse_shipping_text(shipping_text)
        if shipping_val is None:
            shipping_val = _parse_shipping_text(entry.get("priceContext"))
        if shipping_val is None:
            shipping_val = 0.0

        quantity_val = _parse_quantity_text(entry.get("quantityText"))

        condition_text = (entry.get("condition") or "").strip()
        if not condition_text:
            condition_text = "Unknown Condition"

        seller_name = (entry.get("sellerName") or "").strip()
        additional_info = entry.get("additionalInfo")
        if additional_info is not None:
            additional_info = additional_info.strip()
            if not additional_info:
                additional_info = None

        processed.append({
            "_key": key,
            "condition": condition_text,
            "price": round(price_val, 2),
            "shippingPrice": round(shipping_val, 2) if shipping_val is not None else 0.0,
            "sellerName": seller_name,
            "quantityAvailable": quantity_val if quantity_val is not None else 0,
            "additionalInfo": additional_info
        })

    return processed

def _go_to_next_listings_page(page: Page) -> bool:
    selectors = [
        '.tcg-pagination.search-pagination a[aria-label*="Next"]:not([aria-disabled="true"])',
        '.tcg-pagination.search-pagination button[aria-label*="Next"]:not([disabled])',
        '.tcg-pagination.search-pagination a:has-text("Next")',
        '.tcg-pagination.search-pagination button:has-text("Next")',
        '.tcg-pagination.search-pagination li.next a:not(.disabled)',
        'button:has-text("Load More")',
        '[data-testid*="load-more"]',
        'button:has-text("Show More")'
    ]
    try:
        prev_html = page.evaluate(
            "() => { const el = document.querySelector('.product-details__listings'); return el ? el.innerHTML : null; }"
        )
    except Exception:
        prev_html = None

    for sel in selectors:
        try:
            loc = page.locator(sel).first
        except Exception:
            continue
        try:
            if not loc or not loc.is_visible():
                continue
        except Exception:
            continue

        disabled_attr = ""
        class_attr = ""
        try:
            disabled_attr = (loc.get_attribute("aria-disabled") or "").lower()
            class_attr = (loc.get_attribute("class") or "").lower()
        except Exception:
            pass
        if disabled_attr in ("true", "1"):
            continue
        if "disabled" in class_attr:
            continue
        try:
            if hasattr(loc, "is_enabled") and not loc.is_enabled():
                continue
        except Exception:
            pass

        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            loc.click(timeout=3000)
        except Exception:
            continue

        waited = False
        if prev_html is not None:
            try:
                page.wait_for_function(
                    "(arg) => { const el = document.querySelector(arg.selector); if (!el) { return false; } return el.innerHTML !== arg.prev; }",
                    {"selector": ".product-details__listings", "prev": prev_html},
                    timeout=LISTING_PAGE_WAIT_MS
                )
                waited = True
            except Exception:
                pass

        if not waited:
            try:
                page.wait_for_load_state("networkidle", timeout=min(15000, NAV_TIMEOUT_MS))
            except Exception:
                pass
            page.wait_for_timeout(800)
        return True

    return False

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

    _save_debug(page, "after-click-history")

    deadline = time.time() + (wait_ms / 1000.0)
    while time.time() < deadline:
        try:
            dlg = page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
            if dlg and dlg.is_visible():
                return
        except Exception:
            pass
        for sel in [
            '[role="dialog"]', '[aria-modal="true"]',
            '.modal', '.MuiDialog-paper', '.chakra-modal__content',
            '[class*="dialog"]', '[class*="modal"]'
        ]:
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
        login_info = _ensure_logged_in(context)
        page = context.new_page()
        try:
            try:
                _goto_with_retries(page, url); _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "nav-failed")
                return {"url": url, "title": None, "tables": [], "stats": [], "text": None,
                        "error": "timeout_nav", "reason": str(e), "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            if not _is_logged_in(page) and not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page(); _goto_with_retries(page, url); _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "challenge")
                return {"url": url, "title": None, "tables": [], "stats": [], "text": None,
                        "error": err, "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            try:
                _open_snapshot_dialog(page, wait_ms=SNAPSHOT_WAIT_MS)
            except Exception as e:
                art = _save_debug(page, "dialog-failed")
                return {"url": url, "title": None, "tables": [], "stats": [], "text": None,
                        "error": "timeout_dialog", "reason": str(e), "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            dialog = None
            for sel in [
                'role=dialog[name=/Sales\\s+History\\s+Snapshot/i]',
                '[role="dialog"]', '[aria-modal="true"]',
                '.modal', '.MuiDialog-paper', '.chakra-modal__content',
                '[class*="dialog"]', '[class*="modal"]'
            ]:
                try:
                    loc = page.locator(sel).first if not sel.startswith('role=') else page.get_by_role("dialog", name=re.compile(r"Sales\\s+History\\s+Snapshot", re.I)).first
                    if loc and loc.is_visible():
                        dialog = loc; break
                except Exception:
                    pass

            if not dialog:
                art = _save_debug(page, "dialog-missing-after-open")
                return {"url": url, "title": None, "tables": [], "stats": [], "text": None,
                        "error": "dialog_not_found_after_open", "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            dialog_html = dialog.inner_html()
            dialog_text = dialog.inner_text()
            title = "Sales History Snapshot"

            tables = _extract_tables_from_dialog_html(dialog_html)
            stats  = _extract_key_values_from_dialog_html(dialog_html)

            if not tables and not stats and (not dialog_text or not dialog_text.strip()):
                art = _save_debug(page, "dialog-empty")
                return {"url": url, "title": title, "tables": [], "stats": [], "text": None,
                        "error": "dialog_empty", "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            return {"url": url, "title": title, "tables": tables, "stats": stats,
                    "text": dialog_text.strip() if dialog_text else None,
                    "login": login_info, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def fetch_active_listings(product_id: str) -> dict:
    t0 = time.time()
    url = f"https://www.tcgplayer.com/product/{product_id}"
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        login_info = _ensure_logged_in(context)
        page = context.new_page()
        try:
            try:
                _goto_with_retries(page, url); _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "listings-nav-failed")
                return {"product_id": str(product_id), "url": url, "listings": [],
                        "error": "timeout_nav", "reason": str(e), "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            if not _is_logged_in(page) and not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page(); _goto_with_retries(page, url); _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "listings-challenge")
                return {"product_id": str(product_id), "url": url, "listings": [],
                        "error": err, "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            try:
                page.wait_for_selector(".product-details__listings", timeout=LISTING_PAGE_WAIT_MS)
            except Exception as e:
                art = _save_debug(page, "listings-container-missing")
                return {"product_id": str(product_id), "url": page.url,
                        "listings": [], "error": "listings_container_not_found", "reason": str(e),
                        "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            aggregated: List[Dict[str, Any]] = []
            seen_listing_keys: Set[str] = set()
            seen_signatures: Set[str] = set()
            pages_inspected = 0

            while pages_inspected < MAX_LISTING_PAGES:
                pages_inspected += 1
                try:
                    page.wait_for_selector(".product-details__listings", timeout=LISTING_PAGE_WAIT_MS)
                except Exception:
                    break

                try:
                    signature = page.evaluate(
                        "() => { const el = document.querySelector('.product-details__listings'); return el ? el.innerHTML.slice(0, 4096) : null; }"
                    )
                except Exception:
                    signature = None

                if signature and signature in seen_signatures:
                    break
                if signature:
                    seen_signatures.add(signature)

                page_listings = _scrape_active_listings_from_dom(page)
                for listing in page_listings:
                    key = listing.pop("_key", None)
                    dedup_key = key or f"{listing.get('sellerName','')}|{listing.get('condition','')}|{listing.get('price')}|{listing.get('quantityAvailable')}|{listing.get('additionalInfo')}"
                    if dedup_key in seen_listing_keys:
                        continue
                    seen_listing_keys.add(dedup_key)
                    aggregated.append(listing)

                if not _go_to_next_listings_page(page):
                    break
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    pass

            return {
                "product_id": str(product_id),
                "url": page.url,
                "listings": aggregated,
                "pages_scanned": pages_inspected,
                "login": login_info,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000)
            }
        finally:
            context.close(); browser.close()

# ---------- debug helpers ----------
def debug_proxy_ip() -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=False)
        page = context.new_page()
        try:
            page.goto("https://api.ipify.org?format=json", timeout=30000, wait_until="load")
            return {"ok": True, "ipify": (page.text_content("body") or "").strip(),
                    "proxy_in_use": bool(_parse_proxy_env()), "user_agent": USER_AGENT,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_cookies() -> dict:
    t0 = time.time()
    import json as _json
    state_cookies = []
    try:
        if pathlib.Path(STATE_PATH).exists():
            state = _json.loads(open(STATE_PATH, "r", encoding="utf-8")).read()  # type: ignore
    except Exception:
        state = None
    if state:
        try:
            state = json.loads(open(STATE_PATH, "r", encoding="utf-8").read())
            state_cookies = [{"name": c.get("name"), "domain": c.get("domain")} for c in state.get("cookies", [])]
        except Exception:
            state_cookies = []

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        page = context.new_page()
        try:
            page.goto("https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=30000)
            _click_consent_if_present(page)
            ctx_cookies = context.cookies()
            tcg_ctx = [{"name": c.get("name"), "domain": c.get("domain")} for c in ctx_cookies if "tcgplayer" in (c.get("domain") or "")]
            return {"ok": True, "state_cookie_count": len(state_cookies),
                    "state_cookie_domains": sorted({c.get("domain") for c in state_cookies if isinstance(c, dict) and c.get("domain")}),
                    "ctx_cookie_count": len(ctx_cookies), "ctx_tcg_cookies": tcg_ctx,
                    "logged_in_flag": _is_logged_in(page), "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_localstorage() -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        page = context.new_page()
        try:
            page.goto("https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=30000)
            keys = page.evaluate("""() => Object.keys(window.localStorage || {}).slice(0, 50)""")
            return {"ok": True, "keys_sample": keys, "logged_in_flag": _is_logged_in(page),
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_visit(url: str) -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        page = context.new_page()
        try:
            _goto_with_retries(page, url)
            _click_consent_if_present(page)
            anti = _anti_bot_check(page)
            arts = _save_debug(page, "debug-visit")
            return {"ok": True, "url": page.url, "title": page.title(), "logged_in_flag": _is_logged_in(page),
                    "anti_bot": anti, "artifacts": arts, "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_trace(url: str) -> dict:
    t0 = time.time()
    trace_path = f"{DEBUG_DIR}/trace-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            _goto_with_retries(page, url)
            _click_consent_if_present(page)
            context.tracing.stop(path=trace_path)
            return {"ok": True, "trace": trace_path, "final_url": page.url, "title": page.title(),
                    "logged_in_flag": _is_logged_in(page), "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_myaccount() -> dict:
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        page = context.new_page()
        try:
            start = "https://www.tcgplayer.com/myaccount/"
            _goto_with_retries(page, start)
            _click_consent_if_present(page)
            final = page.url
            anti = _anti_bot_check(page)
            arts = _save_debug(page, "debug-myaccount")
            return {"ok": True, "start_url": start, "final_url": final,
                    "redirected_to_login": ("login" in final.lower()),
                    "logged_in_flag": _is_logged_in(page), "anti_bot": anti,
                    "artifacts": arts, "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()

def debug_js() -> dict:
    """Confirm JS/runtime signals and whether <noscript> is present on homepage."""
    t0 = time.time()
    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        page = context.new_page()
        try:
            page.goto("https://www.tcgplayer.com/", wait_until="domcontentloaded", timeout=30000)
            _click_consent_if_present(page)
            info = page.evaluate("""
                () => ({
                  ua: navigator.userAgent,
                  platform: navigator.platform,
                  languages: navigator.languages,
                  webdriver: navigator.webdriver,
                  hasWindowChrome: !!window.chrome,
                  jsTypeofWindow: typeof window,
                })
            """)
            noscript_present = page.locator("noscript").count() > 0
            return {"ok": True, "info": info, "noscript_present_in_dom": bool(noscript_present),
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        finally:
            context.close(); browser.close()
