# app.py — FastAPI service
# Endpoints:
#   GET  /                          -> index
#   GET  /health                    -> health check
#   POST /last-sold                 -> one-shot price (auto login if needed)
#   POST /sales-snapshot            -> Sales History Snapshot dialog (auto login if needed)
#   POST /debug/login               -> RUN LOGIN ONLY (returns status + artifact paths)
#   GET  /debug/login-before-...    -> fetch latest login-before artifact
#   GET  /debug/login-after-...     -> fetch latest login-after artifact
#   GET  /debug/last-screenshot     -> latest generic screenshot
#   GET  /debug/last-html           -> latest generic html

import glob
import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, AnyHttpUrl

from scripts.one_shot import (
    fetch_last_sold_once,
    fetch_sales_snapshot,
    debug_login_only,
)

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="tcgplayer-scraper", version="3.0.0")

@app.get("/")
def home():
    return {
        "service": "tcgplayer-scraper",
        "status": "ok",
        "endpoints": {
            "health": "GET /health",
            "last_sold": "POST /last-sold { url }",
            "sales_snapshot": "POST /sales-snapshot { url }",
            "debug_login": "POST /debug/login",
            "debug_login_before_screenshot": "GET /debug/login-before-screenshot",
            "debug_login_before_html": "GET /debug/login-before-html",
            "debug_login_after_screenshot": "GET /debug/login-after-screenshot",
            "debug_login_after_html": "GET /debug/login-after-html",
            "debug_last_screenshot": "GET /debug/last-screenshot",
            "debug_last_html": "GET /debug/last-html"
        }
    }

@app.get("/health")
def health():
    return {"ok": True}

class Req(BaseModel):
    url: AnyHttpUrl

@app.post("/last-sold")
def last_sold(req: Req):
    try:
        return fetch_last_sold_once(str(req.url))
    except Exception as e:
        logger.exception("last-sold failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sales-snapshot")
def sales_snapshot(req: Req):
    try:
        return fetch_sales_snapshot(str(req.url))
    except Exception as e:
        logger.exception("sales-snapshot failed")
        raise HTTPException(status_code=500, detail=str(e))

# ---------- Login debug ----------
@app.post("/debug/login")
def debug_login():
    try:
        return debug_login_only()
    except Exception as e:
        logger.exception("debug login failed")
        raise HTTPException(status_code=500, detail=str(e))

def _latest(pattern: str) -> str | None:
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None

@app.get("/debug/login-before-screenshot")
def login_before_screenshot():
    f = _latest("/app/debug/login-before-*.png")
    if not f: raise HTTPException(status_code=404, detail="no login-before screenshots")
    return FileResponse(f, media_type="image/png")

@app.get("/debug/login-before-html")
def login_before_html():
    f = _latest("/app/debug/login-before-*.html")
    if not f: raise HTTPException(status_code=404, detail="no login-before html")
    return FileResponse(f, media_type="text/html")

@app.get("/debug/login-after-screenshot")
def login_after_screenshot():
    f = _latest("/app/debug/login-after-*.png")
    if not f: raise HTTPException(status_code=404, detail="no login-after screenshots")
    return FileResponse(f, media_type="image/png")

@app.get("/debug/login-after-html")
def login_after_html():
    f = _latest("/app/debug/login-after-*.html")
    if not f: raise HTTPException(status_code=404, detail="no login-after html")
    return FileResponse(f, media_type="text/html")

# ---------- generic latest (still useful) ----------
@app.get("/debug/last-screenshot")
def last_screenshot():
    f = _latest("/app/debug/*.png")
    if not f: raise HTTPException(status_code=404, detail="no screenshots")
    return FileResponse(f, media_type="image/png")

@app.get("/debug/last-html")
def last_html():
    f = _latest("/app/debug/*.html")
    if not f: raise HTTPException(status_code=404, detail="no html")
    return FileResponse(f, media_type="text/html")
