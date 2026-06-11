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
# Edit .env — fill in DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, INDICES, and budget
```

### Getting Dhan credentials
1. Log in to [Dhan](https://dhan.co) → API section
2. Generate an access token
3. Copy your Client ID and Access Token into `.env`

## Usage

```bash
# Paper trade first — signals printed, no real orders
python trader.py --dry-run

# Live trading
python trader.py
```

Logs are written to both stdout and `trader.log`.

## Project structure

```
hypipe/
├── config.py             # Loads all settings from .env
├── indicators.py         # DEMA calculation (pure pandas/numpy)
├── strategy.py           # Signal logic — DEMACrossoverStrategy
├── option_selector.py    # Scans option chains, filters by budget
├── dhan_client.py         # dhanhq SDK + raw v2 REST wrapper (candles, chain, orders)
├── trader.py              # Main loop, risk manager, scheduler
├── pine/
│   └── dema_crossover_strategy.pine  # TradingView Pine Script — same logic
├── requirements.txt
└── .env.example
```

## TradingView chart markers (Pine Script)

`pine/dema_crossover_strategy.pine` reimplements the exact same DEMA-10/20/95
crossover logic in Pine v5, so you can see — directly on the TradingView chart
inside Dhan — the same BUY/SELL/exit points the Python bot will act on, before
or alongside running `trader.py`.

**To add it:**
1. Open the chart for the contract in Dhan (TradingView widget)
2. Click the **`fx`** (Indicators / Pine Editor) button in the toolbar
3. Open **Pine Editor**, paste the contents of `pine/dema_crossover_strategy.pine`
4. Click **Add to chart** — strategy markers (▲ BUY / ★ BUY / ▼ SELL) and the
   SL/Target lines from `strategy.exit()` will plot automatically as new
   candles form
5. Set the chart timeframe to match `CANDLE_INTERVAL` in `.env` (default 2m)

> **Note:** Whether Dhan's embedded TradingView widget exposes the full Pine
> Editor depends on your Dhan plan/integration. If the `fx` button only offers
> built-in studies (no custom Pine Script), this file can instead be used on
> tradingview.com with the same chart/symbol for visual confirmation, and
> `alertcondition()` blocks are included so TradingView alerts can fire
> webhooks to external automation if your plan supports it.
>
> The Python bot (`trader.py`) is fully self-contained and does **not** depend
> on the Pine Script — it computes DEMAs and signals independently via the
> Dhan REST API. The Pine Script is purely for visual confirmation on the chart.

## Risk controls

- **Per-trade stop-loss** — configurable via `MAX_LOSS_PER_TRADE`
- **Daily loss limit** — trading halts when `MAX_DAILY_LOSS` is breached
- **EOD square-off** — all positions closed at `TRADE_CUTOFF` (default 15:15 IST)
- **Dry-run mode** — test signals without placing orders

## Disclaimer

This software is for educational purposes. Algorithmic trading involves
significant financial risk. Always test in paper mode before going live.
