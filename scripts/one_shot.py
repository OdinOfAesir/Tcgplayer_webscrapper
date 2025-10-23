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
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode
import hashlib

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

def _extract_seller_id_from_href(href: Optional[str]) -> Optional[str]:
    """Extract seller ID from href like https://shop.tcgplayer.com/sellerfeedback/{sellerID}"""
    if not href:
        return None
    try:
        # Match the text after the last "/"
        match = re.search(r"/([^/]+)$", href.strip())
        if match:
            return match.group(1)
        return None
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
                const findShippingSpan = (priceEl) => {
                    if (!priceEl) {
                        return null;
                    }
                    let sibling = priceEl.nextElementSibling;
                    while (sibling) {
                        if (sibling.tagName && sibling.tagName.toLowerCase() === 'span') {
                            return sibling;
                        }
                        sibling = sibling.nextElementSibling;
                    }
                    return null;
                };
                const readShippingInfo = (priceEl) => {
                    const span = findShippingSpan(priceEl);
                    if (!span) {
                        return { text: null, hasAnchor: false };
                    }
                    const hasAnchor = !!span.querySelector('a');
                    const text = (span.textContent || '').trim();
                    return { text, hasAnchor };
                };
                const extractAdditionalInfo = (el) => {
                    if (!el) {
                        return null;
                    }
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('a').forEach((link) => link.remove());
                    const text = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
                    return text || null;
                };

                const priceNodes = Array.from(container.querySelectorAll('.listing-item__listing-data__info__price'));
                for (const priceEl of priceNodes) {
                    const root = resolveRoot(priceEl);
                    if (!root) {
                        continue;
                    }
                    const baseKey = root.getAttribute('data-listingid') ||
                                    root.getAttribute('data-sku') ||
                                    root.getAttribute('data-id') ||
                                    priceEl.getAttribute('data-sku-id') ||
                                    priceEl.getAttribute('data-store-sku') ||
                                    (root.id ? `id:${root.id}` : null) ||
                                    priceEl.outerHTML.slice(0, 180);
                    let key = baseKey || `listing-${records.length}`;
                    if (seenKeys.has(key)) {
                        let suffix = 2;
                        while (seenKeys.has(`${key}#${suffix}`)) {
                            suffix += 1;
                        }
                        key = `${key}#${suffix}`;
                    }
                    seenKeys.add(key);
                    const conditionEl = root.querySelector('.listing-item__listing-data__info__condition');
                    const shippingInfo = readShippingInfo(priceEl);
                    const quantityEl = root.querySelector('.add-to-cart__available');
                    const additionalInfoEl = root.querySelector('.listing-item__listing-data__listo');
                    const sellerEl = root.querySelector('.seller-info a');
                    records.push({
                        key,
                        condition: conditionEl ? conditionEl.textContent.trim() : null,
                        priceText: priceEl.textContent.trim(),
                        priceContext: priceEl.parentElement ? priceEl.parentElement.textContent.trim() : priceEl.textContent.trim(),
                        shippingText: shippingInfo.text,
                        shippingHasAnchor: shippingInfo.hasAnchor,
                        sellerName: sellerEl ? sellerEl.textContent.trim() : null,
                        sellerHref: sellerEl ? sellerEl.getAttribute('href') : null,
                        quantityText: quantityEl ? quantityEl.textContent.trim() : null,
                        additionalInfo: extractAdditionalInfo(additionalInfoEl)
                    });
                }

                if (!priceNodes.length) {
                    const candidates = Array.from(container.querySelectorAll('[data-testid="listing-item"], [data-testid="listing-card"], .listing-item, li, article'));
                    for (const node of candidates) {
                        const priceEl = node.querySelector('.listing-item__listing-data__info__price');
                        if (!priceEl) {
                            continue;
                        }
                        const baseKey = node.getAttribute('data-listingid') ||
                                        node.getAttribute('data-sku') ||
                                        node.getAttribute('data-id') ||
                                        priceEl.getAttribute('data-sku-id') ||
                                        priceEl.getAttribute('data-store-sku') ||
                                        (node.id ? `id:${node.id}` : null) ||
                                        node.outerHTML.slice(0, 180);
                        let key = baseKey || `listing-${records.length}`;
                        if (seenKeys.has(key)) {
                            let suffix = 2;
                            while (seenKeys.has(`${key}#${suffix}`)) {
                                suffix += 1;
                            }
                            key = `${key}#${suffix}`;
                        }
                        seenKeys.add(key);
                        const conditionEl = node.querySelector('.listing-item__listing-data__info__condition');
                        const shippingInfo = readShippingInfo(priceEl);
                        const quantityEl = node.querySelector('.add-to-cart__available');
                        const additionalInfoEl = node.querySelector('.listing-item__listing-data__listo');
                        const sellerEl = node.querySelector('.seller-info a');
                        records.push({
                            key,
                            condition: conditionEl ? conditionEl.textContent.trim() : null,
                            priceText: priceEl.textContent.trim(),
                            priceContext: priceEl.parentElement ? priceEl.parentElement.textContent.trim() : priceEl.textContent.trim(),
                            shippingText: shippingInfo.text,
                            shippingHasAnchor: shippingInfo.hasAnchor,
                            sellerName: sellerEl ? sellerEl.textContent.trim() : null,
                            sellerHref: sellerEl ? sellerEl.getAttribute('href') : null,
                            quantityText: quantityEl ? quantityEl.textContent.trim() : null,
                            additionalInfo: extractAdditionalInfo(additionalInfoEl)
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
    for idx, entry in enumerate(raw_listings or []):
        if not isinstance(entry, dict):
            continue

        price_val = _to_money_float(entry.get("priceText") or "")
        if price_val is None:
            price_val = _to_money_float(entry.get("priceContext") or "")
        if price_val is None:
            continue

        shipping_has_anchor = bool(entry.get("shippingHasAnchor"))
        shipping_text = entry.get("shippingText") or ""
        if shipping_has_anchor:
            shipping_val = 0.0
        else:
            shipping_val = _parse_shipping_text(shipping_text)
        if shipping_val is None:
            shipping_val = 0.0

        quantity_val = _parse_quantity_text(entry.get("quantityText"))

        condition_text = (entry.get("condition") or "").strip()
        if not condition_text:
            condition_text = "Unknown Condition"

        seller_href = entry.get("sellerHref")
        seller_id = _extract_seller_id_from_href(seller_href)
        seller_name = (entry.get("sellerName") or "").strip()

        additional_info = entry.get("additionalInfo")
        if additional_info is not None:
            additional_info = additional_info.strip()
            if not additional_info:
                additional_info = None

        raw_key = str(entry.get("key") or "").strip()
        fallback_key = "|".join([
            seller_id or seller_name or "",
            condition_text,
            f"{price_val:.2f}",
            f"{round(shipping_val or 0.0, 2):.2f}",
            str(quantity_val if quantity_val is not None else 0),
            additional_info or "",
        ])
        base_key = raw_key or fallback_key or f"listing-{idx}"
        candidate_key = base_key
        suffix = 1
        while candidate_key in seen_keys:
            suffix += 1
            candidate_key = f"{base_key}#{suffix}"
        seen_keys.add(candidate_key)

        processed.append({
            "_key": candidate_key,
            "condition": condition_text,
            "price": round(price_val, 2),
            "shippingPrice": round(shipping_val, 2) if shipping_val is not None else 0.0,
            "sellerName": seller_name,
            "sellerId": seller_id,
            "quantityAvailable": quantity_val if quantity_val is not None else 0,
            "additionalInfo": additional_info
        })

    return processed

def _normalize_pagination_target(current_url: str, target: Optional[str]) -> Optional[str]:
    if not target:
        return None
    target = target.strip()
    if not target or target.lower().startswith("javascript"):
        return None
    try:
        current_parts = urlparse(current_url)
        joined = urljoin(current_url, target)
        dest_parts = urlparse(joined)

        if not dest_parts.netloc:
            dest_parts = dest_parts._replace(netloc=current_parts.netloc, scheme=current_parts.scheme)
        if not dest_parts.path:
            dest_parts = dest_parts._replace(path=current_parts.path)

        current_q = {k: v for k, v in parse_qsl(current_parts.query, keep_blank_values=True)}
        dest_q = {k: v for k, v in parse_qsl(dest_parts.query, keep_blank_values=True)}
        merged_q = current_q
        merged_q.update(dest_q)

        new_query = urlencode(merged_q, doseq=False)
        dest_parts = dest_parts._replace(query=new_query)

        return dest_parts.geturl()
    except Exception:
        return None

def _wait_for_listings_refresh(page: Page,
                               prev_html: Optional[str],
                               prev_label: Optional[str],
                               prev_url: str,
                               timeout_ms: int) -> bool:
    try:
        page.wait_for_function(
            """
            (arg) => {
                const listings = document.querySelector('.product-details__listings');
                if (!listings) { return false; }
                const html = listings.innerHTML || '';
                const pager = document.querySelector('.tcg-pagination.search-pagination [aria-current="page"], .tcg-pagination.search-pagination [aria-current="true"], .tcg-pagination.search-pagination .is-current, .tcg-pagination.search-pagination .active');
                const label = pager ? (pager.textContent || '').trim() : null;

                if (!arg.prev_html) {
                    return html.length > 0;
                }
                if (html && html !== arg.prev_html) {
                    return true;
                }
                if (label && arg.prev_label && label !== arg.prev_label) {
                    return true;
                }
                if (window.location.href !== arg.prev_url) {
                    return true;
                }
                return false;
            }
            """,
            {"prev_html": prev_html or "", "prev_label": prev_label or "", "prev_url": prev_url or ""},
            timeout=timeout_ms
        )
        return True
    except Exception:
        return False

def _detect_current_page(page: Page) -> Optional[int]:
    try:
        label = page.evaluate(
            """
            () => {
                const root = document.querySelector('.tcg-pagination.search-pagination');
                if (!root) { return null; }
                const current = root.querySelector('[aria-current="page"], [aria-current="true"], .is-current, .active');
                return current ? (current.textContent || '').trim() : null;
            }
            """
        )
        if label:
            match = re.search(r"\d+", label)
            if match:
                return int(match.group(0))
    except Exception:
        pass
    try:
        parsed = urlparse(page.url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if "page" in params:
            value = params.get("page")
            if value is not None and value.strip():
                return int(value)
    except Exception:
        pass
    return None

def _go_to_page_via_direct_url(page: Page, base_url: str, desired_page: int) -> bool:
    desired_page = max(1, desired_page)
    try:
        prev_url = page.url
    except Exception:
        prev_url = base_url
    try:
        prev_label = page.evaluate(
            """
            () => {
                const root = document.querySelector('.tcg-pagination.search-pagination');
                if (!root) { return null; }
                const current = root.querySelector('[aria-current="page"], [aria-current="true"], .is-current, .active');
                return current ? (current.textContent || '').trim() : null;
            }
            """
        )
    except Exception:
        prev_label = None
    try:
        prev_html = page.evaluate(
            "() => { const el = document.querySelector('.product-details__listings'); return el ? el.innerHTML : null; }"
        )
    except Exception:
        prev_html = None

    normalized = _normalize_pagination_target(prev_url, f"?page={desired_page}")
    if not normalized:
        normalized = _normalize_pagination_target(base_url, f"?page={desired_page}")
    if not normalized:
        base_parts = urlparse(base_url)
        rebuilt = base_parts._replace(query="")
        fallback = rebuilt.geturl().rstrip("/")
        normalized = f"{fallback}?page={desired_page}"
    try:
        page.goto(normalized, wait_until="load", timeout=max(NAV_TIMEOUT_MS, LISTING_PAGE_WAIT_MS))
    except Exception:
        return False

    refreshed = _wait_for_listings_refresh(page, prev_html, prev_label, prev_url, LISTING_PAGE_WAIT_MS)
    if not refreshed and page.url and page.url != prev_url:
        refreshed = True
    if not refreshed:
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        refreshed = _wait_for_listings_refresh(page, prev_html, prev_label, prev_url, 2000)

    try:
        page.evaluate(
            "() => { const root = document.querySelector('.tcg-pagination.search-pagination'); if (root) { root.querySelectorAll('[data-codex-next-marker]').forEach(el => el.removeAttribute('data-codex-next-marker')); } }"
        )
    except Exception:
        pass

    return refreshed

def _navigate_to_page_number(page: Page, base_url: str, target_page: int, last_page: int) -> bool:
    if last_page <= 0:
        last_page = 1
    target_page = max(1, min(last_page, target_page))
    current = _detect_current_page(page) or 1
    if current == target_page:
        return True

    if target_page <= 5 or last_page <= 5:
        step = 1 if target_page > current else -1
        guard = 0
        while current != target_page and guard < 20:
            guard += 1
            next_page = current + step
            next_page = max(1, min(last_page, next_page))
            if next_page == current:
                break
            if not _go_to_page_via_direct_url(page, base_url, next_page):
                return False
            current = _detect_current_page(page) or next_page
        return current == target_page

    via_page = 5 if abs(target_page - 5) <= abs(last_page - target_page) else last_page

    if current != via_page:
        if via_page == 5 and current < 5:
            guard_seq = 0
            while current < 5 and guard_seq < 10:
                guard_seq += 1
                next_page = current + 1
                if not _go_to_page_via_direct_url(page, base_url, next_page):
                    return False
                current = _detect_current_page(page) or next_page
        if current != via_page:
            if not _go_to_page_via_direct_url(page, base_url, via_page):
                return False
            current = _detect_current_page(page) or via_page

    guard = 0
    while current != target_page and guard < 20:
        guard += 1
        delta = target_page - current
        if abs(delta) <= 5:
            next_page = target_page
        else:
            next_page = current + (5 if delta > 0 else -5)
        next_page = max(1, min(last_page, next_page))
        if next_page == current:
            break
        if not _go_to_page_via_direct_url(page, base_url, next_page):
            return False
        current = _detect_current_page(page) or next_page

    return current == target_page

def _extract_last_page_number(page: Page) -> int:
    try:
        label = page.evaluate(
            """
            () => {
                const root = document.querySelector('.tcg-pagination__pages');
                if (!root) { return null; }
                const anchors = Array.from(root.querySelectorAll('a')).filter(el => el.textContent && el.textContent.trim());
                if (!anchors.length) { return null; }
                const last = anchors[anchors.length - 1];
                return last ? (last.textContent || '').trim() : null;
            }
            """
        )
        if label:
            match = re.search(r"\d+", label)
            if match:
                value = int(match.group(0))
                return value if value > 0 else 1
    except Exception:
        pass
    return 1

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

def fetch_pages_in_product(product_id: str) -> dict:
    """Fetch the total number of pages in a product's active listings."""
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
                art = _save_debug(page, "pages-nav-failed")
                return {"product_id": str(product_id), "url": url, "total_pages": None,
                        "error": "timeout_nav", "reason": str(e), "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            if not _is_logged_in(page) and not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page(); _goto_with_retries(page, url); _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "pages-challenge")
                return {"product_id": str(product_id), "url": url, "total_pages": None,
                        "error": err, "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            try:
                page.wait_for_selector(".product-details__listings", timeout=LISTING_PAGE_WAIT_MS)
            except Exception as e:
                art = _save_debug(page, "pages-container-missing")
                return {"product_id": str(product_id), "url": page.url,
                        "total_pages": None, "error": "listings_container_not_found", "reason": str(e),
                        "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            last_page = _extract_last_page_number(page)

            return {
                "product_id": str(product_id),
                "url": page.url,
                "total_pages": last_page,
                "login": login_info,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000)
            }
        finally:
            context.close(); browser.close()

def fetch_active_listings_in_page(product_id: str, target_page: int) -> dict:
    """Fetch active listings from a specific page number by directly navigating to the page URL."""
    t0 = time.time()

    # Validate target page
    if target_page < 1:
        return {"product_id": str(product_id), "target_page": target_page,
                "listings": [], "error": "invalid_page_number", "reason": "Page number must be >= 1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000)}

    # Build URL with page parameter
    url = f"https://www.tcgplayer.com/product/{product_id}?page={target_page}"

    with sync_playwright() as p:
        browser, context = _new_context(p, use_saved_state=True)
        login_info = _ensure_logged_in(context)
        page = context.new_page()
        try:
            try:
                _goto_with_retries(page, url); _click_consent_if_present(page)
            except Exception as e:
                art = _save_debug(page, "listings-page-nav-failed")
                return {"product_id": str(product_id), "url": url, "target_page": target_page,
                        "listings": [], "error": "timeout_nav", "reason": str(e),
                        "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            if not _is_logged_in(page) and not FORCE_STATE_ONLY and os.getenv("TCG_EMAIL") and os.getenv("TCG_PASSWORD"):
                li2 = _do_login_flow(context, capture=True)
                login_info = {"first": login_info, "retry": li2}
                page = context.new_page(); _goto_with_retries(page, url); _click_consent_if_present(page)

            err = _anti_bot_check(page)
            if err:
                art = _save_debug(page, "listings-page-challenge")
                return {"product_id": str(product_id), "url": url, "target_page": target_page,
                        "listings": [], "error": err, "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            try:
                page.wait_for_selector(".product-details__listings", timeout=LISTING_PAGE_WAIT_MS)
            except Exception as e:
                art = _save_debug(page, "listings-page-container-missing")
                return {"product_id": str(product_id), "url": page.url, "target_page": target_page,
                        "listings": [], "error": "listings_container_not_found", "reason": str(e),
                        "login": login_info, "artifacts": art,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            # Get the last page number to validate
            last_page = _extract_last_page_number(page)

            # Check if target page exceeds available pages
            if target_page > last_page:
                return {"product_id": str(product_id), "url": page.url, "target_page": target_page,
                        "listings": [], "error": "page_out_of_range",
                        "reason": f"Target page {target_page} exceeds last page {last_page}",
                        "total_pages": last_page, "login": login_info,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": int((time.time() - t0) * 1000)}

            # Verify we're on the correct page
            current_page = _detect_current_page(page)

            # Scrape listings from current page
            page_listings = _scrape_active_listings_from_dom(page)

            # Remove internal _key field from listings
            listings = []
            for listing in page_listings:
                listing.pop("_key", None)
                listings.append(listing)

            return {
                "product_id": str(product_id),
                "url": page.url,
                "target_page": target_page,
                "current_page": current_page,
                "total_pages": last_page,
                "listings": listings,
                "listings_count": len(listings),
                "login": login_info,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.time() - t0) * 1000)
            }
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
