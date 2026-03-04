# BTC ↔ Polymarket Monitor

Monitor de divergencias entre precio BTC y probabilidades de Polymarket.
Corre en Railway con datos reales en tiempo real.

## Deploy en Railway (5 minutos)

### 1. Subir a GitHub
1. Crea un repo nuevo en github.com (ej: `polymarket-monitor`)
2. Sube estos 4 archivos:
   - `main.py`
   - `index.html`
   - `requirements.txt`
   - `Procfile`

### 2. Conectar Railway
1. Ve a railway.app → New Project
2. "Deploy from GitHub repo"
3. Selecciona tu repo
4. Railway detecta el `Procfile` automáticamente y despliega

### 3. Listo
Railway te da una URL pública tipo:
`https://polymarket-monitor-production.up.railway.app`

## Cómo funciona

- **Precio BTC**: WebSocket directo a Binance (tiempo real en el browser)
- **Polymarket**: El servidor (Railway) consulta la API cada 30s y la sirve en `/api/polymarket`
- **Modelo**: Momentum + mean reversion calibrado para 5 minutos
- **Sin trades reales**: Paper mode únicamente

## Archivos

| Archivo | Función |
|---------|---------|
| `main.py` | Servidor FastAPI — obtiene datos de Polymarket |
| `index.html` | Dashboard — precio BTC en tiempo real |
| `requirements.txt` | Dependencias Python |
| `Procfile` | Instrucción de arranque para Railway |
