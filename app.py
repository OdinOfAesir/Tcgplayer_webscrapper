from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl
from scripts.one_shot import fetch_last_sold_once  # <-- use the new function

app = FastAPI()

@app.get("/")
def home():
    return {"service": "tcgplayer-scraper", "endpoints": ["GET /health", "POST /last-sold"]}

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
        raise HTTPException(status_code=500, detail=str(e))
