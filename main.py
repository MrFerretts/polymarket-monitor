"""
BTC ↔ Polymarket Monitor
FastAPI backend — corre en Railway sin problemas de CORS
"""
import asyncio
import json
import os
import re
import time
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ─── Estado compartido en memoria ────────────────────────────────
state = {
    "polyProb": None,
    "polyMarket": "Buscando mercado BTC...",
    "lastPolyUpdate": None,
    "error": None,
    "spread": None,
    "volume": None,
    "liquidity": None,
    "endDate": None,
}

news_state = {
    "articles": [],
    "sentiment": 0,
    "lastUpdate": None,
}

forecast_state = {
    "probability": None,
    "reasoning": None,
    "confidence": None,
    "lastUpdate": None,
    "enabled": bool(os.getenv("GROQ_API_KEY")),
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

SUPERFORECASTER_PROMPT = """\
Eres un superforecaster experto en mercados de predicción y criptomonedas.
Tu trabajo es estimar la probabilidad de que Bitcoin cumpla la condición del mercado.

Analiza usando el framework de superforecasting:
1. Tasa base: ¿Cuál es la probabilidad histórica de movimientos similares de BTC?
2. Factores actuales: ¿Qué dicen las señales técnicas (momentum, volatilidad) y el sentimiento?
3. Calibración: ¿El mercado de predicción está sobre/subvalorando respecto a los datos?
4. Contrarian check: ¿Hay razones para ir contra el consenso?

Sé preciso y calibrado. Evita anclar tu estimación a la probabilidad del mercado.

RESPONDE ÚNICAMENTE con JSON válido (sin markdown, sin texto extra):
{"probability": 0.XX, "confidence": "alta|media|baja", "reasoning": "resumen en 1-2 oraciones"}\
"""


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
    return re.sub(r"<[^>]+>", "", text or "")


def is_btc_directional(m: dict) -> bool:
    """Filtra mercados de precio BTC direccionales, excluyendo ruido."""
    q = (m.get("question", "") + " " + m.get("title", "")).lower()
    has_btc = "bitcoin" in q or "btc" in q
    has_directional = any(kw in q for kw in [
        "above", "reach", "exceed", "higher", "over", "hit",
        "below", "under", "lower", "drop", "fall",
        "up or down", "up/down",
    ])
    has_num = any(c.isdigit() for c in q)
    is_range = "between" in q and "and" in q
    is_noise = any(kw in q for kw in [
        "etf", "senate", "congress", "election", "president", "party",
        "sec", "approve", "ban", "regulation", "legal", "trump", "biden",
        "republican", "democrat", "fed", "interest rate", "hold", "fewer", "seat",
    ])
    return has_btc and has_directional and has_num and not is_noise and not is_range


def extract_price(m: dict) -> float | None:
    """Extrae la probabilidad/precio de un mercado."""
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
    return price if price and 0.01 < price < 0.99 else None


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

        await asyncio.sleep(300)


# ─── Fetch Polymarket con paginación ──────────────────────────────
async def fetch_btc_markets(client: httpx.AsyncClient) -> list[dict]:
    """Pagina la Gamma API para encontrar todos los mercados BTC activos."""
    all_btc = []
    offset = 0
    limit = 50
    max_pages = 4

    for _ in range(max_pages):
        res = await client.get(
            GAMMA_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            },
        )
        res.raise_for_status()
        markets = res.json()
        if not isinstance(markets, list):
            markets = markets.get("data", [])
        if not markets:
            break

        for m in markets:
            if is_btc_directional(m):
                all_btc.append(m)

        # Si ya encontramos mercados BTC, no seguir paginando
        if all_btc:
            break

        offset += limit

    return all_btc


async def fetch_polymarket_loop():
    """Se ejecuta en background cada 30 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                btc_markets = await fetch_btc_markets(client)

                pool = btc_markets if btc_markets else []
                pool.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)

                found = False
                for m in pool:
                    price = extract_price(m)
                    if price is None:
                        continue

                    state["polyProb"] = price
                    label = (m.get("question") or m.get("title") or "BTC market")[:60]
                    state["polyMarket"] = label
                    state["lastPolyUpdate"] = time.time()
                    state["error"] = None
                    found = True

                    # Spread
                    try:
                        bid = float(m.get("bestBid") or 0)
                        ask = float(m.get("bestAsk") or 0)
                        state["spread"] = round(ask - bid, 4) if bid > 0 and ask > 0 else None
                    except (TypeError, ValueError):
                        state["spread"] = None

                    # Volumen y liquidez
                    try:
                        state["volume"] = float(m.get("volume") or 0)
                    except (TypeError, ValueError):
                        state["volume"] = None

                    try:
                        state["liquidity"] = float(m.get("liquidity") or 0)
                    except (TypeError, ValueError):
                        state["liquidity"] = None

                    state["endDate"] = m.get("endDate")
                    break

                if not found and state["lastPolyUpdate"]:
                    if time.time() - state["lastPolyUpdate"] > 300:
                        state["polyProb"] = None
                        state["error"] = "No se encontró mercado BTC válido en Polymarket"

        except Exception as e:
            state["error"] = str(e)

        await asyncio.sleep(30)


# ─── Superforecasting con Groq ────────────────────────────────────
async def call_groq(system_prompt: str, user_prompt: str) -> dict | None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
        )
        res.raise_for_status()
        data = res.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


def build_forecast_prompt() -> str | None:
    """Construye el prompt del usuario con contexto actual."""
    if state["polyProb"] is None:
        return None

    headlines = "\n".join(
        f"- {a['title']}" for a in news_state["articles"][:5]
    ) or "Sin noticias recientes"

    sentiment_label = "neutral"
    s = news_state["sentiment"]
    if s > 0.15:
        sentiment_label = f"bullish ({s:+.0%})"
    elif s < -0.15:
        sentiment_label = f"bearish ({s:+.0%})"

    spread_str = f"{state['spread'] * 100:.1f}%" if state["spread"] else "no disponible"

    return f"""\
Mercado: "{state['polyMarket']}"
Probabilidad Polymarket: {state['polyProb']:.1%}
Spread orderbook: {spread_str}
Volumen: ${state['volume']:,.0f}
Sentimiento noticias: {sentiment_label} ({len(news_state['articles'])} artículos)

Titulares recientes:
{headlines}

Estima la probabilidad real de que se cumpla la condición del mercado."""


async def fetch_forecast_loop():
    """Llama a Groq cada 5 minutos para superforecasting."""
    api_key = os.getenv("GROQ_API_KEY")
    forecast_state["enabled"] = bool(api_key)

    if not api_key:
        print("[forecast] GROQ_API_KEY no configurada — forecast deshabilitado")
        return

    print("[forecast] Groq habilitado — forecast cada 5 minutos")

    # Esperar 60s para que haya datos
    await asyncio.sleep(60)

    while True:
        try:
            user_prompt = build_forecast_prompt()
            if user_prompt:
                result = await call_groq(SUPERFORECASTER_PROMPT, user_prompt)
                if result:
                    prob = result.get("probability")
                    if isinstance(prob, (int, float)) and 0 <= prob <= 1:
                        forecast_state["probability"] = round(prob, 3)
                        forecast_state["reasoning"] = result.get("reasoning", "")[:200]
                        forecast_state["confidence"] = result.get("confidence", "media")
                        forecast_state["lastUpdate"] = time.time()
                        print(f"[forecast] Prob: {prob:.1%} | {forecast_state['confidence']} | {forecast_state['reasoning'][:80]}")
        except Exception as e:
            print(f"[forecast] Error: {e}")

        await asyncio.sleep(300)


# ─── App lifecycle ────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    poly_task = asyncio.create_task(fetch_polymarket_loop())
    news_task = asyncio.create_task(fetch_news_loop())
    forecast_task = asyncio.create_task(fetch_forecast_loop())
    yield
    poly_task.cancel()
    news_task.cancel()
    forecast_task.cancel()


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
        "liquidity": state["liquidity"],
        "endDate": state["endDate"],
    })


@app.get("/api/news")
async def get_news():
    return JSONResponse({
        "articles": news_state["articles"],
        "sentiment": news_state["sentiment"],
        "updatedAt": news_state["lastUpdate"],
    })


@app.get("/api/forecast")
async def get_forecast():
    return JSONResponse({
        "probability": forecast_state["probability"],
        "reasoning": forecast_state["reasoning"],
        "confidence": forecast_state["confidence"],
        "updatedAt": forecast_state["lastUpdate"],
        "enabled": forecast_state["enabled"],
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r") as f:
        return f.read()
