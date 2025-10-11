from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, AnyHttpUrl
import uvicorn

# Import from the repo’s code
from scripts.tcgplayer_last_sold_monitor import fetch_last_sold_once  # Adjust if needed

app = FastAPI()

class Req(BaseModel):
    url: AnyHttpUrl

@app.post("/last-sold")
def last_sold(req: Req):
    try:
        record = fetch_last_sold_once(str(req.url))
        if not record:
            return {"url": str(req.url), "most_recent_sale": None, "error": "no data"}
        return record
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
