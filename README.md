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

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env — fill in DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, and instrument details
```

### Getting Dhan credentials
1. Log in to [Dhan](https://dhan.co) → API section
2. Generate an access token
3. Copy your Client ID and Access Token into `.env`

### Finding your Security ID
Use the Dhan scrip search or their securities master CSV to find the `security_id`
for the specific options contract you want to trade (e.g. NIFTY 26 MAY 25000 CE).

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
├── config.py        # Loads all settings from .env
├── indicators.py    # DEMA calculation (pure pandas/numpy)
├── strategy.py      # Signal logic — DEMACrossoverStrategy
├── dhan_client.py   # dhanhq SDK wrapper (candles + orders)
├── trader.py        # Main loop, risk manager, scheduler
├── requirements.txt
└── .env.example
```

## Risk controls

- **Per-trade stop-loss** — configurable via `MAX_LOSS_PER_TRADE`
- **Daily loss limit** — trading halts when `MAX_DAILY_LOSS` is breached
- **EOD square-off** — all positions closed at `TRADE_CUTOFF` (default 15:15 IST)
- **Dry-run mode** — test signals without placing orders

## Disclaimer

This software is for educational purposes. Algorithmic trading involves
significant financial risk. Always test in paper mode before going live.
