# app.py — FastAPI service
#   GET  /                -> index
#   GET  /health          -> health check
#   POST /last-sold       -> previous single-price scrape
#   POST /sales-snapshot  -> NEW: scrape "Sales History Snapshot" dialog as JSON

import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl

from scripts.one_shot import fetch_last_sold_once, fetch_sales_snapshot

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="tcgplayer-scraper", version="2.0.0")

@app.get("/")
def home():
    return {
        "service": "tcgplayer-scraper",
        "status": "ok",
        "endpoints": {
            "health": "GET /health",
            "last_sold": "POST /last-sold { url }",
            "sales_snapshot": "POST /sales-snapshot { url }"
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
