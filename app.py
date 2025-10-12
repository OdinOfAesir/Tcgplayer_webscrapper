# app.py
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

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
)

app = FastAPI(title="tcgplayer-scraper", version="1.1.0")

@app.get("/")
def root():
    return {"ok": True, "service": "tcgplayer-scraper", "version": "1.1.0"}

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

# ---- Debug / Diagnostics ----

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

# Simple file server for artifacts under /app/debug
@app.get("/debug/artifact")
def artifact(path: str):
    # only allow files under /app/debug
    if not path.startswith("/app/debug/"):
        raise HTTPException(status_code=400, detail="invalid path")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
