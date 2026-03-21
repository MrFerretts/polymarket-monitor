"""
BTC ↔ Polymarket Monitor
FastAPI backend — corre en Railway sin problemas de CORS
"""
import asyncio
import json
import re
import time
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# ─── Estado compartido en memoria ────────────────────────────────
state = {
    "polyProb": None,
    "polyMarket": "Buscando mercado BTC...",
    "lastPolyUpdate": None,
    "error": None,
    "spread": None,        # bid-ask spread del orderbook
    "volume": None,        # volumen del mercado seleccionado
}

news_state = {
    "articles": [],        # últimas noticias BTC
    "sentiment": 0,        # -1 a 1 (bearish a bullish)
    "lastUpdate": None,
}

# ─── Palabras clave para análisis de sentimiento ──────────────────
BULLISH_WORDS = [
    "surge", "soar", "rally", "jump", "gain", "rise", "bull", "high",
    "record", "breakout", "pump", "moon", "adoption", "buy", "bought",
    "profit", "growth", "climb", "up", "above", "ath", "institutional",
    "etf approved", "halving", "bullish", "optimism", "recover",
    "sube", "alza", "récord", "máximo", "alcista", "ganancia",
]

BEARISH_WORDS = [
    "crash", "plunge", "drop", "fall", "dump", "bear", "low", "sell",
    "loss", "fear", "ban", "hack", "scam", "fraud", "regulation",
    "crackdown", "decline", "sink", "tumble", "below", "warning",
    "bubble", "risk", "lawsuit", "sec", "bearish", "panic", "liquidat",
    "baja", "caída", "desplome", "bajista", "pérdida", "riesgo",
]


def analyze_sentiment(text: str) -> float:
    """Sentimiento simple por conteo de palabras. Retorna -1 a 1."""
    text_lower = text.lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)
    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return (bull_count - bear_count) / total


def strip_html(text: str) -> str:
    """Quita tags HTML de un string."""
    return re.sub(r"<[^>]+>", "", text or "")


# ─── Fetch noticias BTC (Google News RSS — sin API key) ──────────
async def fetch_news_loop():
    """Busca noticias BTC cada 5 minutos vía Google News RSS."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    "https://news.google.com/rss/search?q=bitcoin+price&hl=en&gl=US&ceid=US:en",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                res.raise_for_status()

                root = ElementTree.fromstring(res.text)
                items = root.findall(".//item")[:10]

                articles = []
                sentiments = []
                for item in items:
                    title = strip_html(item.findtext("title", ""))
                    source = item.findtext("source", "")
                    pub_date = item.findtext("pubDate", "")
                    link = item.findtext("link", "")

                    sent = analyze_sentiment(title)
                    sentiments.append(sent)

                    articles.append({
                        "title": title[:120],
                        "source": source,
                        "date": pub_date,
                        "link": link,
                        "sentiment": round(sent, 2),
                    })

                news_state["articles"] = articles
                news_state["sentiment"] = round(
                    sum(sentiments) / len(sentiments), 2
                ) if sentiments else 0
                news_state["lastUpdate"] = time.time()

        except Exception as e:
            print(f"[news] Error: {e}")

        await asyncio.sleep(300)  # cada 5 minutos


# ─── Fetch Polymarket (corre en el servidor, sin CORS) ────────────
async def fetch_polymarket_loop():
    """Se ejecuta en background cada 30 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(
                    "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=volume&ascending=false"
                )
                res.raise_for_status()
                markets = res.json()
                if not isinstance(markets, list):
                    markets = markets.get("data", [])

                # Filtrar mercados de precio BTC DIRECCIONALES
                btc = []
                for m in markets:
                    q = (m.get("question","") + " " + m.get("title","")).lower()
                    has_btc   = "bitcoin" in q or "btc" in q
                    has_directional = any(kw in q for kw in [
                        "above", "reach", "exceed", "higher", "over", "hit",
                        "below", "under", "lower", "drop", "fall",
                    ])
                    has_num   = any(c.isdigit() for c in q)
                    is_range  = "between" in q and "and" in q
                    is_noise  = any(kw in q for kw in [
                        "etf","senate","congress","election","president","party",
                        "sec","approve","ban","regulation","legal","trump","biden",
                        "republican","democrat","fed","interest rate","hold","fewer","seat"
                    ])
                    if has_btc and has_directional and has_num and not is_noise and not is_range:
                        btc.append(m)

                pool = btc if btc else markets[:5]
                pool.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)

                found = False
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
                        try:
                            price = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
                        except (TypeError, ValueError):
                            price = 0
                    if price and 0.01 < price < 0.99:
                        state["polyProb"] = price
                        label = (m.get("question") or m.get("title") or "BTC market")[:60]
                        state["polyMarket"] = label
                        state["lastPolyUpdate"] = time.time()
                        state["error"] = None
                        found = True

                        # Extraer spread del mercado (bestBid vs bestAsk)
                        try:
                            bid = float(m.get("bestBid") or 0)
                            ask = float(m.get("bestAsk") or 0)
                            if bid > 0 and ask > 0:
                                state["spread"] = round(ask - bid, 4)
                            else:
                                state["spread"] = None
                        except (TypeError, ValueError):
                            state["spread"] = None

                        # Volumen
                        try:
                            state["volume"] = float(m.get("volume") or 0)
                        except (TypeError, ValueError):
                            state["volume"] = None

                        break

                if not found and state["lastPolyUpdate"]:
                    if time.time() - state["lastPolyUpdate"] > 300:
                        state["polyProb"] = None
                        state["error"] = "No se encontró mercado BTC válido en Polymarket"

        except Exception as e:
            state["error"] = str(e)

        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    poly_task = asyncio.create_task(fetch_polymarket_loop())
    news_task = asyncio.create_task(fetch_news_loop())
    yield
    poly_task.cancel()
    news_task.cancel()


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
        "updatedAt": state["lastPolyUpdate"],
        "spread": state["spread"],
        "volume": state["volume"],
    })


@app.get("/api/news")
async def get_news():
    return JSONResponse({
        "articles": news_state["articles"],
        "sentiment": news_state["sentiment"],
        "updatedAt": news_state["lastUpdate"],
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r") as f:
        return f.read()
