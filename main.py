"""
BTC ↔ Polymarket Monitor
FastAPI backend — corre en Railway sin problemas de CORS
"""
import asyncio
import json
import math
import os
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ─── Binance API URLs ────────────────────────────────────────────
BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
BINANCE_FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_FUTURES_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"

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

# ─── Estado de señales de mercado (datos reales de Binance) ──────
signals_state = {
    # Order book imbalance: ratio bid_vol / (bid_vol + ask_vol) - 0.5
    # >0 = más presión compradora, <0 = más presión vendedora
    "obImbalance": 0.0,
    "obImbalanceEma": 0.0,    # EMA suavizado (alpha=0.3) para reducir ruido
    "obBidVol": 0.0,
    "obAskVol": 0.0,
    "obSpread": 0.0,          # spread bid-ask real de Binance

    # Funding rate de futuros perpetuos
    "fundingRate": 0.0,        # positivo = longs pagan a shorts
    "fundingTime": None,

    # Volumen relativo (vol último minuto vs media 30 min)
    "volumeRatio": 1.0,
    "volume1m": 0.0,
    "volumeAvg30m": 0.0,

    # Liquidaciones acumuladas (últimos 5 min)
    "liqLongTotal": 0.0,       # USD liquidados en longs
    "liqShortTotal": 0.0,      # USD liquidados en shorts
    "liqEvents": [],           # últimos 20 eventos

    # Open Interest
    "openInterest": 0.0,

    "lastUpdate": None,
}

# ─── Estado del mercado "Bitcoin Up or Down - 5 Minutes" ────────
updown_state = {
    "found": False,
    "marketTitle": None,
    "strikePrice": None,       # precio de referencia al inicio del intervalo
    "upPrice": None,           # precio del token "Up" (e.g. 0.65)
    "downPrice": None,         # precio del token "Down" (e.g. 0.36)
    "intervalEndTs": None,     # epoch ms cuando termina el intervalo
    "conditionId": None,
    "lastUpdate": None,
}

# Ring buffer para volumen por minuto (últimos 30 minutos)
_volume_per_minute = deque(maxlen=30)
_current_minute_vol = 0.0
_current_minute_ts = 0

# Ring buffer para liquidaciones (últimos 5 min)
_liquidations = deque(maxlen=500)

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


def is_btc_5min_updown(m: dict) -> bool:
    """Detecta el mercado específico 'Bitcoin Up or Down - 5 Minutes'."""
    q = (m.get("question", "") + " " + m.get("title", "")).lower()
    return ("bitcoin" in q or "btc" in q) and (
        "up or down" in q or "5 minute" in q or "5-minute" in q
    )


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

        now = datetime.now(timezone.utc)
        for m in markets:
            if not is_btc_directional(m):
                continue
            # Filtrar mercados cuya ventana ya expiró
            end = m.get("endDate")
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    if end_dt <= now:
                        continue
                except ValueError:
                    pass
            all_btc.append(m)

        offset += limit

    return all_btc


async def fetch_polymarket_loop():
    """Se ejecuta en background cada 30 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                btc_markets = await fetch_btc_markets(client)

                pool = btc_markets if btc_markets else []
                pool.sort(key=lambda m: m.get("endDate") or "9999")  # soonest first

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


# ─── Fetch Order Book Depth (cada 2 segundos) ───────────────────
async def fetch_orderbook_loop():
    """Obtiene order book depth de Binance spot cada 2 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.get(
                    BINANCE_DEPTH_URL,
                    params={"symbol": "BTCUSDT", "limit": 20},
                )
                res.raise_for_status()
                data = res.json()

                bids = data.get("bids", [])
                asks = data.get("asks", [])

                bid_vol = sum(float(b[1]) for b in bids[:10])
                ask_vol = sum(float(a[1]) for a in asks[:10])
                total = bid_vol + ask_vol

                signals_state["obBidVol"] = round(bid_vol, 4)
                signals_state["obAskVol"] = round(ask_vol, 4)
                raw_imbalance = (bid_vol / total - 0.5) * 2 if total > 0 else 0
                signals_state["obImbalance"] = round(raw_imbalance, 4)

                # EMA smoothing (alpha=0.3 → ~6 readings half-life at 2s = 12s)
                alpha = 0.3
                prev_ema = signals_state["obImbalanceEma"]
                signals_state["obImbalanceEma"] = round(
                    alpha * raw_imbalance + (1 - alpha) * prev_ema, 4
                )

                # Spread real bid-ask
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    signals_state["obSpread"] = round(best_ask - best_bid, 2)

                signals_state["lastUpdate"] = time.time()

        except Exception as e:
            print(f"[orderbook] Error: {e}")

        await asyncio.sleep(2)


# ─── Fetch Funding Rate (cada 60 segundos) ──────────────────────
async def fetch_funding_loop():
    """Obtiene funding rate de futuros perpetuos BTC cada 60 segundos."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(
                    BINANCE_FUTURES_FUNDING_URL,
                    params={"symbol": "BTCUSDT", "limit": 1},
                )
                res.raise_for_status()
                data = res.json()
                if data:
                    signals_state["fundingRate"] = float(data[-1].get("fundingRate", 0))
                    signals_state["fundingTime"] = data[-1].get("fundingTime")

                # Open Interest
                res2 = await client.get(
                    BINANCE_FUTURES_OI_URL,
                    params={"symbol": "BTCUSDT"},
                )
                res2.raise_for_status()
                oi_data = res2.json()
                signals_state["openInterest"] = float(oi_data.get("openInterest", 0))

        except Exception as e:
            print(f"[funding] Error: {e}")

        await asyncio.sleep(60)


# ─── Fetch Volume Ratio (cada 10 segundos) ──────────────────────
async def fetch_volume_loop():
    """Calcula volumen relativo: último minuto vs media 30 min."""
    global _current_minute_vol, _current_minute_ts

    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Klines de 1 minuto, últimos 31
                res = await client.get(
                    BINANCE_KLINES_URL,
                    params={"symbol": "BTCUSDT", "interval": "1m", "limit": 31},
                )
                res.raise_for_status()
                klines = res.json()

                if len(klines) >= 2:
                    # Volumen del último minuto completo
                    vol_1m = float(klines[-2][5])  # index 5 = volume
                    signals_state["volume1m"] = round(vol_1m, 4)

                    # Media de los 30 minutos anteriores
                    vols = [float(k[5]) for k in klines[:-1]]
                    avg_vol = sum(vols) / len(vols) if vols else 1
                    signals_state["volumeAvg30m"] = round(avg_vol, 4)

                    signals_state["volumeRatio"] = round(
                        vol_1m / avg_vol if avg_vol > 0 else 1.0, 3
                    )

        except Exception as e:
            print(f"[volume] Error: {e}")

        await asyncio.sleep(10)


# ─── Fetch Liquidaciones vía REST (cada 5 segundos) ─────────────
async def fetch_liquidations_loop():
    """Monitorea liquidaciones recientes vía /fapi/v1/allForceOrders."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.get(
                    "https://fapi.binance.com/fapi/v1/allForceOrders",
                    params={"symbol": "BTCUSDT", "limit": 50},
                )
                res.raise_for_status()
                orders = res.json()

                # Acumular liquidaciones de los últimos 5 minutos
                now_ms = int(time.time() * 1000)
                cutoff = now_ms - 300000  # 5 min

                long_total = 0.0
                short_total = 0.0
                recent_events = []

                for o in orders:
                    trade_time = o.get("time", 0)
                    if trade_time < cutoff:
                        continue

                    side = o.get("side", "")  # BUY = short liquidated, SELL = long liquidated
                    qty = float(o.get("executedQty", 0))
                    price_val = float(o.get("averagePrice") or o.get("price", 0))
                    usd_val = qty * price_val

                    if side == "SELL":
                        long_total += usd_val
                    elif side == "BUY":
                        short_total += usd_val

                    recent_events.append({
                        "side": "LONG" if side == "SELL" else "SHORT",
                        "usd": round(usd_val, 0),
                        "time": trade_time,
                    })

                signals_state["liqLongTotal"] = round(long_total, 0)
                signals_state["liqShortTotal"] = round(short_total, 0)
                signals_state["liqEvents"] = recent_events[-20:]

        except Exception as e:
            # allForceOrders puede no estar disponible en todas las regiones
            # Fallback silencioso
            if "403" not in str(e) and "451" not in str(e):
                print(f"[liquidations] Error: {e}")

        await asyncio.sleep(5)


# ─── Fetch "Bitcoin Up or Down - 5 Minutes" market ───────────────
async def fetch_updown_loop():
    """Busca el mercado activo de 'Bitcoin Up or Down - 5 Minutes' cada 15s.
    Extrae strike price, precios Up/Down, y tiempo restante del intervalo."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Buscar en Gamma API con query específico
                for query in ["Bitcoin Up or Down 5 Minutes", "BTC Up or Down"]:
                    res = await client.get(
                        GAMMA_URL,
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": 20,
                            "order": "volume",
                            "ascending": "false",
                        },
                    )
                    res.raise_for_status()
                    markets = res.json()
                    if not isinstance(markets, list):
                        markets = markets.get("data", [])

                    for m in markets:
                        if not is_btc_5min_updown(m):
                            continue

                        # Verificar que no haya expirado
                        end = m.get("endDate")
                        if end:
                            try:
                                end_dt = datetime.fromisoformat(
                                    end.replace("Z", "+00:00")
                                )
                                if end_dt <= datetime.now(timezone.utc):
                                    continue
                            except ValueError:
                                pass

                        # Extraer precios Up/Down de outcomePrices
                        up_price = None
                        down_price = None
                        outcome_prices = m.get("outcomePrices")
                        if outcome_prices:
                            try:
                                arr = outcome_prices
                                if isinstance(arr, str):
                                    arr = json.loads(arr)
                                up_price = float(arr[0])
                                if len(arr) > 1:
                                    down_price = float(arr[1])
                            except Exception:
                                pass

                        # Extraer strike price del título/description
                        # Formato típico: "Bitcoin Up or Down - 5 Minutes"
                        # El strike viene en description o en campos custom
                        strike = None
                        desc = m.get("description", "") or ""
                        title = m.get("question", "") or m.get("title", "") or ""

                        # Buscar precio en description/title: $71,094.51 o 71094.51
                        for text in [desc, title]:
                            price_match = re.search(
                                r'\$?([\d,]+\.?\d*)', text
                            )
                            if price_match:
                                try:
                                    val = float(
                                        price_match.group(1).replace(",", "")
                                    )
                                    # Debe ser un precio razonable de BTC
                                    if 10000 < val < 500000:
                                        strike = val
                                        break
                                except ValueError:
                                    pass

                        # Calcular fin del intervalo desde endDate
                        interval_end_ts = None
                        if end:
                            try:
                                end_dt = datetime.fromisoformat(
                                    end.replace("Z", "+00:00")
                                )
                                interval_end_ts = int(end_dt.timestamp() * 1000)
                            except ValueError:
                                pass

                        updown_state["found"] = True
                        updown_state["marketTitle"] = (
                            title[:80] if title else "BTC Up or Down 5min"
                        )
                        updown_state["strikePrice"] = strike
                        updown_state["upPrice"] = up_price
                        updown_state["downPrice"] = down_price
                        updown_state["intervalEndTs"] = interval_end_ts
                        updown_state["conditionId"] = m.get("conditionId")
                        updown_state["lastUpdate"] = time.time()
                        break

                    if updown_state["found"]:
                        break

                if not updown_state["found"]:
                    updown_state["lastUpdate"] = time.time()

        except Exception as e:
            print(f"[updown] Error: {e}")

        await asyncio.sleep(15)


# ─── App lifecycle ────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    poly_task = asyncio.create_task(fetch_polymarket_loop())
    news_task = asyncio.create_task(fetch_news_loop())
    forecast_task = asyncio.create_task(fetch_forecast_loop())
    orderbook_task = asyncio.create_task(fetch_orderbook_loop())
    funding_task = asyncio.create_task(fetch_funding_loop())
    volume_task = asyncio.create_task(fetch_volume_loop())
    liquidations_task = asyncio.create_task(fetch_liquidations_loop())
    updown_task = asyncio.create_task(fetch_updown_loop())
    yield
    for task in [poly_task, news_task, forecast_task,
                 orderbook_task, funding_task, volume_task,
                 liquidations_task, updown_task]:
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


@app.get("/api/signals")
async def get_signals():
    return JSONResponse({
        "obImbalance": signals_state["obImbalance"],
        "obImbalanceEma": signals_state["obImbalanceEma"],
        "obBidVol": signals_state["obBidVol"],
        "obAskVol": signals_state["obAskVol"],
        "obSpread": signals_state["obSpread"],
        "fundingRate": signals_state["fundingRate"],
        "fundingTime": signals_state["fundingTime"],
        "volumeRatio": signals_state["volumeRatio"],
        "volume1m": signals_state["volume1m"],
        "volumeAvg30m": signals_state["volumeAvg30m"],
        "liqLongTotal": signals_state["liqLongTotal"],
        "liqShortTotal": signals_state["liqShortTotal"],
        "openInterest": signals_state["openInterest"],
        "updatedAt": signals_state["lastUpdate"],
    })


@app.get("/api/updown")
async def get_updown():
    """Estado del mercado 'Bitcoin Up or Down - 5 Minutes'."""
    return JSONResponse({
        "found": updown_state["found"],
        "marketTitle": updown_state["marketTitle"],
        "strikePrice": updown_state["strikePrice"],
        "upPrice": updown_state["upPrice"],
        "downPrice": updown_state["downPrice"],
        "intervalEndTs": updown_state["intervalEndTs"],
        "conditionId": updown_state["conditionId"],
        "updatedAt": updown_state["lastUpdate"],
    })


@app.get("/api/klines")
async def get_klines(interval: str = "1m", limit: int = 500):
    """Proxy para klines de Binance (para backtesting con datos reales)."""
    limit = min(limit, 1000)
    if interval not in ("1m", "3m", "5m", "15m", "1h"):
        return JSONResponse({"error": "interval inválido"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                BINANCE_KLINES_URL,
                params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            )
            res.raise_for_status()
            return JSONResponse(res.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ─── Signal logging para calibración ──────────────────────────────
SIGNALS_DB = os.getenv("SIGNALS_DB", "signals.db")


def _init_db():
    conn = sqlite3.connect(SIGNALS_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        flow REAL, cvd REAL, ob REAL, liq REAL, mom REAL,
        funding REAL, regime REAL, spread_disc REAL,
        model_prob REAL,
        btc_price REAL,
        outcome INTEGER DEFAULT -1,
        outcome_price REAL DEFAULT NULL
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_ts ON signals(ts)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_outcome ON signals(outcome)""")
    conn.commit()
    conn.close()


_init_db()


@app.post("/api/log_signal")
async def log_signal_handler(request):
    """Guarda señal + outcome para calibración futura."""
    try:
        data = await request.json()
        conn = sqlite3.connect(SIGNALS_DB)
        conn.execute(
            """INSERT INTO signals (ts, flow, cvd, ob, liq, mom, funding,
               regime, spread_disc, model_prob, btc_price, outcome, outcome_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(data.get("ts", time.time() * 1000)),
                data.get("flow"),
                data.get("cvd"),
                data.get("ob"),
                data.get("liq"),
                data.get("mom"),
                data.get("funding"),
                data.get("regime"),
                data.get("spreadDisc"),
                data.get("modelProb"),
                data.get("btcPrice"),
                data.get("outcome", -1),
                data.get("outcomePrice"),
            ),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        conn.close()
        return JSONResponse({"ok": True, "totalRecords": count})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/log_outcome")
async def log_outcome(request):
    """Actualiza el outcome de una señal registrada hace ~5 min."""
    try:
        data = await request.json()
        ts = int(data["ts"])
        outcome = int(data["outcome"])  # 1 = acertó, 0 = falló
        outcome_price = float(data.get("outcomePrice", 0))

        conn = sqlite3.connect(SIGNALS_DB)
        conn.execute(
            """UPDATE signals SET outcome = ?, outcome_price = ?
               WHERE ts = ? AND outcome = -1""",
            (outcome, outcome_price, ts),
        )
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/calibration")
async def get_calibration():
    """Estadísticas de calibración del modelo basadas en datos reales."""
    try:
        conn = sqlite3.connect(SIGNALS_DB)
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        with_outcome = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE outcome >= 0"
        ).fetchone()[0]

        if with_outcome < 10:
            conn.close()
            return JSONResponse({
                "status": "insufficient_data",
                "totalSignals": total,
                "withOutcome": with_outcome,
                "message": f"Necesitas al menos 10 señales con outcome. Tienes {with_outcome}.",
            })

        # Win rate por rango de confianza
        rows = conn.execute(
            """SELECT model_prob, outcome, flow, cvd, ob, liq, mom
               FROM signals WHERE outcome >= 0"""
        ).fetchall()
        conn.close()

        # Calcular stats
        wins = sum(1 for r in rows if r[1] == 1)
        total_outcomes = len(rows)
        overall_wr = wins / total_outcomes if total_outcomes > 0 else 0

        # Win rate por bucket de confianza
        buckets = {"low": [], "med": [], "high": []}
        for r in rows:
            prob = r[0] or 0.5
            conf = abs(prob - 0.5) * 2
            if conf < 0.10:
                buckets["low"].append(r[1])
            elif conf < 0.20:
                buckets["med"].append(r[1])
            else:
                buckets["high"].append(r[1])

        bucket_stats = {}
        for k, v in buckets.items():
            if v:
                bucket_stats[k] = {
                    "count": len(v),
                    "winRate": round(sum(v) / len(v), 3),
                }
            else:
                bucket_stats[k] = {"count": 0, "winRate": None}

        # Correlación simple de cada señal con outcome
        signal_corr = {}
        signal_names = ["flow", "cvd", "ob", "liq", "mom"]
        for i, name in enumerate(signal_names):
            vals = [(r[i + 2], r[1]) for r in rows if r[i + 2] is not None]
            if len(vals) >= 10:
                mean_sig = sum(v[0] for v in vals) / len(vals)
                mean_out = sum(v[1] for v in vals) / len(vals)
                cov = sum((v[0] - mean_sig) * (v[1] - mean_out) for v in vals) / len(vals)
                std_sig = (sum((v[0] - mean_sig)**2 for v in vals) / len(vals)) ** 0.5
                std_out = (sum((v[1] - mean_out)**2 for v in vals) / len(vals)) ** 0.5
                if std_sig > 0 and std_out > 0:
                    signal_corr[name] = round(cov / (std_sig * std_out), 3)
                else:
                    signal_corr[name] = 0
            else:
                signal_corr[name] = None

        return JSONResponse({
            "status": "ok",
            "totalSignals": total,
            "withOutcome": with_outcome,
            "overallWinRate": round(overall_wr, 3),
            "byConfidence": bucket_stats,
            "signalCorrelation": signal_corr,
            "message": f"Win rate: {overall_wr:.1%} sobre {with_outcome} señales.",
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("index.html", "r") as f:
        return f.read()
