"""
BTC ↔ Polymarket Monitor
FastAPI backend — corre en Railway sin problemas de CORS
"""
import asyncio
import json
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

# ─── Estado compartido en memoria ────────────────────────────────
state = {
    "polyProb": None,
    "polyMarket": "Buscando mercado BTC...",
    "lastPolyUpdate": None,
    "error": None,
}

# ─── Fetch Polymarket (corre en el servidor, sin CORS) ────────────
async def fetch_polymarket_loop():
    """Se ejecuta en background cada 30 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(
                    "https://gamma-api.polymarket.com/markets"
                    "?active=true&closed=false&tag_slug=bitcoin"
                    "&limit=20&order=volume&ascending=false"
                )
                res.raise_for_status()
                markets = res.json()
                if not isinstance(markets, list):
                    markets = markets.get("data", [])

                # Filtrar mercados BTC de subida
                btc = [m for m in markets if any(
                    kw in (m.get("question","") + m.get("title","")).lower()
                    for kw in ["above","higher","reach","over","exceed","hit","price","up"]
                ) and any(
                    kw in (m.get("question","") + m.get("title","")).lower()
                    for kw in ["bitcoin","btc"]
                )]

                pool = btc if btc else markets[:5]
                pool.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)

                for m in pool:
                    price = None
                    if m.get("outcomePrices"):
                        try:
                            arr = m["outcomePrices"]
                            if isinstance(arr, str):
                                arr = json.loads(arr)
                            price = float(arr[0])
                        except Exception:
                            pass
                    if not price or not (0.01 < price < 0.99):
                        price = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
                    if price and 0.01 < price < 0.99:
                        state["polyProb"] = price
                        label = (m.get("question") or m.get("title") or "BTC market")[:60]
                        state["polyMarket"] = label
                        state["lastPolyUpdate"] = asyncio.get_event_loop().time()
                        state["error"] = None
                        break

        except Exception as e:
            state["error"] = str(e)

        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Arrancar loop de Polymarket en background
    task = asyncio.create_task(fetch_polymarket_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Endpoints ───────────────────────────────────────────────────
@app.get("/api/polymarket")
async def get_polymarket():
    return JSONResponse({
        "prob": state["polyProb"],
        "market": state["polyMarket"],
        "error": state["error"],
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r") as f:
        return f.read()
