# app.py — FastAPI service exposing:
#   GET /            -> simple index
#   GET /health      -> health check
#   POST /last-sold  -> { url } -> scrapes 'Most Recent Sale' after login

import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl

from scripts.one_shot import fetch_last_sold_once

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="tcgplayer-scraper", version="1.0.0")

@app.get("/")
def home():
    return {
        "service": "tcgplayer-scraper",
        "status": "ok",
        "endpoints": {
            "health": "GET /health",
            "last_sold": "POST /last-sold { url }"
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
        data = fetch_last_sold_once(str(req.url))
        return data
    except Exception as e:
        logger.exception("last-sold failed")
        raise HTTPException(status_code=500, detail=str(e))
