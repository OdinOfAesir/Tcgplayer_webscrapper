# app.py
import os
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from scripts.one_shot import (
    fetch_last_sold_once,
    fetch_sales_snapshot,
    debug_login_only,
    debug_proxy_ip,
    debug_cookies,
    debug_localstorage,
    debug_visit,
    debug_trace,
    debug_myaccount,
    debug_js,
)

app = FastAPI(title="tcgplayer-scraper", version="1.3.0")

# ---- CORS (allow your Base44 domains to call this API from the browser) ----
# Set ALLOWED_ORIGINS env to a comma-separated list, e.g.:
# https://app.base44.com,https://yourdomain.com
ALLOWED_ORIGINS = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "").split(",") if o.strip()]
if not ALLOWED_ORIGINS:
    # fallback for testing; you should set ALLOWED_ORIGINS in Render
    ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---- Simple API-key gate (protects your endpoints) ----
API_KEY = os.getenv("API_KEY")  # set in Render → Environment
def _auth(x_api_key: str | None) -> None:
    if not API_KEY:
        return  # no key configured -> no auth (not recommended)
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

@app.get("/")
def root():
    return {"ok": True, "service": "tcgplayer-scraper", "version": "1.3.0"}

# ---- Public API ----

@app.post("/last-sold")
def last_sold(payload: dict, x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key)
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    return JSONResponse(fetch_last_sold_once(url))

@app.post("/sales-snapshot")
def sales_snapshot(payload: dict, x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key)
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    return JSONResponse(fetch_sales_snapshot(url))

# ---- Debug / Diagnostics (consider gating these with API_KEY or disabling in prod) ----

@app.post("/debug/login")
def debug_login(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_login_only())

@app.get("/debug/proxy-ip")
def _proxy_ip(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_proxy_ip())

@app.get("/debug/cookies")
def _cookies(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_cookies())

@app.get("/debug/localstorage")
def _localstorage(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_localstorage())

@app.get("/debug/visit")
def _visit(url: str, x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_visit(url))

@app.get("/debug/trace")
def _trace(url: str, x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_trace(url))

@app.get("/debug/myaccount")
def _myaccount(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_myaccount())

@app.get("/debug/js")
def _js(x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key);  return JSONResponse(debug_js())

# Simple file server for artifacts under /app/debug
@app.get("/debug/artifact")
def artifact(path: str, x_api_key: str | None = Header(default=None, convert_underscores=False)):
    _auth(x_api_key)
    if not path.startswith("/app/debug/"):
        raise HTTPException(status_code=400, detail="invalid path")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
