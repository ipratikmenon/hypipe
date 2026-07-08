# hypipe — DEMA Crossover Algo Trader (Dhan API)

Automated intraday options trader that replicates the TradingView DEMA setup:
**DEMA 10 / DEMA 20 / DEMA 95** on 2-minute candles, trading via the [Dhan](https://dhan.co) broker API.

## Strategy

| Signal | Condition |
|--------|-----------|
| **BUY**  | DEMA-10 crosses *above* DEMA-20 AND close > DEMA-95 |
| **SELL** | DEMA-10 crosses *below* DEMA-20 AND close < DEMA-95 |
| **EXIT** | Opposite crossover, stop-loss hit, or target hit |
| **HIGH CONFIDENCE** | Previous candle low touched DEMA-95 (price bounced off yellow line) |

Stop-loss and target are auto-calculated per trade based on `MAX_LOSS_PER_TRADE` and `PROFIT_TARGET_MULTIPLIER`.

## Instrument selection

The bot does **not** trade a fixed contract. Each trading day `option_selector.py`:

1. Scans the **nearest weekly expiry** option chain for `NIFTY`, `BANKNIFTY`, `MIDCPNIFTY`, `FINNIFTY` (configurable via `INDICES`)
2. Keeps every CE/PE where `LTP × lot_size ≤ MAX_LOSS_PER_TRADE`
3. Refreshes LTPs each tick and runs the DEMA strategy on every affordable contract
4. Trades the first high-confidence signal, falling back to any BUY/SELL signal

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env — fill in DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, and budget
```

### Getting Dhan credentials
1. Log in to [Dhan](https://dhan.co) → API section → Generate access token
2. Copy Client ID and Access Token into `.env`
3. Whitelist your server IP: **web.dhan.co → Profile → IP Whitelist** (mandatory per SEBI; 7-day lock after change)

## Usage

### Option A — Standalone bot (no TradingView needed)
```bash
python trader.py --dry-run   # paper trade first
python trader.py             # live
```
The bot polls Dhan candle data every 2 minutes, computes DEMA signals internally, and places orders.

### Option B — TradingView webhook automation
```bash
python webhook_server.py
```
Receives TradingView alert POSTs, picks the best affordable option dynamically, and places the order.
See [TradingView webhook setup](#tradingview-webhook-setup) below.

Logs are written to both stdout and `trader.log`.

## Project structure

```
hypipe/
├── config.py              # All settings loaded from .env
├── indicators.py          # DEMA calculation (pure pandas/numpy)
├── strategy.py            # Signal logic — DEMACrossoverStrategy
├── option_selector.py     # Scans option chains, filters by budget
├── dhan_client.py         # dhanhq SDK + raw v2 REST wrapper
├── trader.py              # Standalone polling bot
├── webhook_server.py      # FastAPI server for TradingView alerts
├── pine/
│   └── dema_crossover_strategy.pine  # Pine Script v5 — same logic + alert conditions
├── requirements.txt
└── .env.example
```

## TradingView webhook setup

> **Requirement**: TradingView Pro or above for webhook alerts.
> tv.dhan.co (Dhan's hosted chart) does not support custom Pine Scripts —
> use a separate tradingview.com account.

### Architecture
```
TradingView alert (Pine Script alertcondition fires)
  └─→ POST https://<your-server>/alert      ← webhook_server.py
        └─→ OptionSelector picks best affordable CE/PE
              └─→ POST https://api.dhan.co/v2/orders
```

### Step-by-step

**1. Expose webhook_server.py publicly**

On a VPS or cloud instance:
```bash
python webhook_server.py          # listens on 0.0.0.0:8000
```
Or locally with [ngrok](https://ngrok.com) for testing:
```bash
ngrok http 8000                   # gives https://<id>.ngrok.io
```

**2. Set WEBHOOK_SECRET in `.env`**
```
WEBHOOK_SECRET=some_random_secret_string
```
This prevents unauthorized POSTs to your server.

**3. Add the Pine Script to tradingview.com**
1. Open tradingview.com → open any NIFTY/BANKNIFTY/etc. chart
2. Set timeframe to **2m** (matches `CANDLE_INTERVAL`)
3. Pine Editor → paste `pine/dema_crossover_strategy.pine` → **Add to chart**
4. Visual BUY ▲ / SELL ▼ / ★HIGH-CONF markers appear immediately

**4. Create TradingView alerts**

Create one alert per signal condition. In the alert dialog:

| Condition | Message field (JSON) |
|-----------|---------------------|
| `Hypipe BUY → webhook_server` | `{"signal":"BUY","option_type":"CE","index":"NIFTY","price":{{close}},"secret":"YOUR_SECRET"}` |
| `Hypipe SELL → webhook_server` | `{"signal":"SELL","option_type":"PE","index":"NIFTY","price":{{close}},"secret":"YOUR_SECRET"}` |
| `Hypipe EXIT LONG` | `{"signal":"EXIT","price":{{close}},"secret":"YOUR_SECRET"}` |

- **Webhook URL**: `https://<your-server>/alert`
- Replace `YOUR_SECRET` with `WEBHOOK_SECRET` from `.env`
- Replace `"NIFTY"` with `"BANKNIFTY"`, `"MIDCPNIFTY"`, or `"FINNIFTY"` if charting a different index
- `{{close}}` is a TradingView placeholder — it inserts the bar's close price automatically

**5. Verify**
```bash
curl -X POST https://<your-server>/alert \
  -H "Content-Type: application/json" \
  -d '{"signal":"BUY","option_type":"CE","index":"NIFTY","price":23500,"secret":"YOUR_SECRET"}'
```
Response: `{"status":"ok","signal":"BUY","instrument":"NIFTY ... CE","order_id":"...",...}`

Health check: `GET https://<your-server>/health`

## Risk controls

- **Budget filter** — only options where `LTP × lot_size ≤ MAX_LOSS_PER_TRADE` are considered
- **Per-trade stop-loss** — auto-calculated from `MAX_LOSS_PER_TRADE` and `PROFIT_TARGET_MULTIPLIER`
- **Daily loss limit** — bot stops trading when `MAX_DAILY_LOSS` is breached
- **EOD square-off** — all positions closed at `TRADE_CUTOFF` (default 15:15 IST)
- **Dry-run mode** — `python trader.py --dry-run` logs signals without placing orders

## Disclaimer

This software is for educational purposes. Algorithmic trading involves
significant financial risk. Always test in paper / dry-run mode before going live.
