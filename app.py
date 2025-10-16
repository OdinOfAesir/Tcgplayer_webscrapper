# app.py
import os
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from scripts.one_shot import (
    fetch_last_sold_once,
    fetch_sales_snapshot,
    fetch_active_listings,
    fetch_pages_in_product,
    fetch_active_listings_in_page,
    debug_login_only,
    debug_proxy_ip,
    debug_cookies,
    debug_localstorage,
    debug_visit,
    debug_trace,
    debug_myaccount,
    debug_js,
)

app = FastAPI(title="tcgplayer-scraper", version="1.4.0-public")

# ---- API Key Authentication ----
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise ValueError("API_KEY environment variable is not set. Please set it in your .env file.")

@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """Verify API key for all requests except root endpoint."""
    # Allow root endpoint without authentication for health checks
    if request.url.path == "/":
        return await call_next(request)

    # Check for API key in headers
    api_key = request.headers.get("X-API-Key") or request.headers.get("Authorization")

    # Support Bearer token format
    if api_key and api_key.startswith("Bearer "):
        api_key = api_key[7:]

    if api_key != API_KEY:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key. Include 'X-API-Key' header with your API key."}
        )

    return await call_next(request)

# ---- CORS: allow all (public testing) ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # PUBLIC
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "service": "tcgplayer-scraper", "version": "1.4.0-public"}

# ---- Public API ----

@app.post("/last-sold")
def last_sold(payload: dict):
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    return JSONResponse(fetch_last_sold_once(url))

@app.post("/sales-snapshot")
def sales_snapshot(payload: dict):
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    return JSONResponse(fetch_sales_snapshot(url))

@app.post("/active-listings")
def active_listings(payload: dict):
    product_id = payload.get("productId") or payload.get("product_id")
    if not product_id:
        raise HTTPException(status_code=400, detail="Missing productId")
    return JSONResponse(fetch_active_listings(str(product_id)))

@app.post("/pages-in-product")
def pages_in_product(payload: dict):
    product_id = payload.get("productId") or payload.get("product_id")
    if not product_id:
        raise HTTPException(status_code=400, detail="Missing productId")
    return JSONResponse(fetch_pages_in_product(str(product_id)))

@app.post("/active-listings-in-page")
def active_listings_in_page(payload: dict):
    product_id = payload.get("productId") or payload.get("product_id")
    page = payload.get("page") or payload.get("pageNumber")
    if not product_id:
        raise HTTPException(status_code=400, detail="Missing productId")
    if page is None:
        raise HTTPException(status_code=400, detail="Missing page number")
    try:
        page_num = int(page)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid page number")
    return JSONResponse(fetch_active_listings_in_page(str(product_id), page_num))

# ---- Debug / Diagnostics (PUBLIC in this build) ----

@app.post("/debug/login")
def debug_login():
    return JSONResponse(debug_login_only())

@app.get("/debug/proxy-ip")
def _proxy_ip():
    return JSONResponse(debug_proxy_ip())

@app.get("/debug/cookies")
def _cookies():
    return JSONResponse(debug_cookies())

@app.get("/debug/localstorage")
def _localstorage():
    return JSONResponse(debug_localstorage())

@app.get("/debug/visit")
def _visit(url: str):
    return JSONResponse(debug_visit(url))

@app.get("/debug/trace")
def _trace(url: str):
    return JSONResponse(debug_trace(url))

@app.get("/debug/myaccount")
def _myaccount():
    return JSONResponse(debug_myaccount())

@app.get("/debug/js")
def _js():
    return JSONResponse(debug_js())

# Simple file server for artifacts under /app/debug
@app.get("/debug/artifact")
def artifact(path: str):
    if not path.startswith("/app/debug/"):
        raise HTTPException(status_code=400, detail="invalid path")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
