# hypipe — Development Plan & System Audit

*Last updated: 2026-07-08*

This document has three parts:

1. **Audit** — a complete, honest study of the existing `indices/` system
2. **Hardening plan** — how to make `indices/` robust and production-grade
3. **New module plan** — `fx_commodities/`: a mathematical prediction engine for
   currency pairs (EUR/GBP/JPY vs USD), bullion, and commodities using orderflow,
   liquidity heatmaps, and statistical modeling

---

## Part 1 — Audit of the existing `indices/` system

### What it does today

```
indices/
├── indicators.py        DEMA 10/20/95 math (2·EMA − EMA(EMA))
├── strategy.py          Crossover signals + DEMA-95 trend filter + touch detection
├── option_selector.py   Scans weekly chains (NIFTY/BANKNIFTY/MIDCPNIFTY/FINNIFTY),
│                        filters contracts by LTP × lot_size ≤ budget
├── dhan_client.py       dhanhq SDK + raw v2 REST (candles, option chain, bulk LTP, orders)
├── trader.py            Polling loop: scan → evaluate DEMA per candidate → trade best signal
├── webhook_server.py    FastAPI: TradingView alert → pick nearest-ATM affordable option → order
└── pine/                Pine v5 mirror of the strategy for chart markers + alerts
```

### Honest gap analysis

#### A. Signal-quality gaps

| # | Gap | Why it matters |
|---|-----|----------------|
| A1 | **DEMA computed on the option premium, not the underlying.** | Premium = f(underlying, IV, theta). Theta decay creates a permanent bearish drift in the premium series; IV spikes create false crossovers. The trend being traded is really in the *index*, not the premium. Fix: compute signals on the underlying index candles (`IDX_I`), execute on the selected option. |
| A2 | **Tick not aligned to candle close.** `schedule.every(2).minutes` starts from process launch, so evaluation can happen mid-candle. | The "last closed candle" seen by the strategy is nondeterministic; the same market produces different trades depending on when the bot was started. |
| A3 | **No liquidity/spread filter in the trader path.** The budget filter naturally selects deep-OTM contracts, which have the widest spreads. | A 0.50/1.00 bid-ask on a ₹5 premium is 66% round-trip cost — slippage silently eats any edge. Need: min OI, min volume, max spread% filters. |
| A4 | **`_best_signal` fetches candle history for every affordable candidate every tick.** Hundreds of contracts → hundreds of API calls/2min. | Will hit Dhan rate limits and get the token throttled. Signal should be computed once per *underlying* (4 indices = 4 calls), not per contract. |
| A5 | **`MAX_LOSS_PER_TRADE` conflates two concepts** — the premium budget and the stop-loss distance. `SL = price − MAX_LOSS/lot` can go negative for cheap options. | For a *bought* option, max loss is already capped at premium paid. Budget and risk need separate knobs: `BUDGET_PER_TRADE` (contract cost ceiling) and `RISK_PER_TRADE` (SL distance). |

#### B. Execution & state gaps

| # | Gap | Why it matters |
|---|-----|----------------|
| B1 | **All position state is in-memory.** Crash/restart ⇒ live position with no bot tracking it. | Highest-severity operational risk. Needs a SQLite journal + startup reconciliation against Dhan's positions API. |
| B2 | **No fill confirmation.** Market orders are assumed filled at the signal price; PnL is computed from that assumption. | Actual fills differ (slippage, partial fills, rejections). Need order-status polling or Dhan Postback integration; PnL from actual `filled_qty`/trade price. |
| B3 | **Access token expires every 24 h; nothing renews it or alerts.** | The bot silently turns into a log-spammer overnight. Needs `/v2/RenewToken` + failure alerting. |
| B4 | **Webhook server state lives in module globals.** Breaks with >1 uvicorn worker; no idempotency. | TradingView can re-fire alerts; duplicate POSTs = duplicate orders. Need idempotency keys (`correlationId`) and shared state. |
| B5 | **No retries/circuit breaker on Dhan API errors mid-session.** | One transient 5xx at the wrong moment = missed exit. |

#### C. Engineering gaps

| # | Gap |
|---|-----|
| C1 | **Zero tests.** Indicators, strategy, and selector logic have no unit coverage. |
| C2 | **No backtester.** The DEMA strategy has never been validated on historical data — we don't know its expectancy, hit rate, or drawdown. |
| C3 | Lot-size parsing takes the first CSV row per symbol — may grab a far-month contract's lot after a SEBI revision window. |
| C4 | `trading_symbol` in the selector is a cosmetic guess, not Dhan's real symbol. |
| C5 | No structured trade log / metrics (win rate, profit factor, max drawdown). |

---

## Part 2 — Hardening plan for `indices/` (no code changed yet)

Executed in four phases, each independently shippable:

### Phase R1 — Signal correctness
- Compute DEMA signals on the **underlying index** (`IDX_I` candles), trade the selected option (fixes A1)
- Align evaluation to candle-close boundaries using exchange timestamps (fixes A2)
- One signal computation per index per tick, not per contract (fixes A4)
- Liquidity gate: `OI ≥ min_oi`, `volume ≥ min_vol`, `spread/mid ≤ max_spread_pct` (fixes A3)
- Split `BUDGET_PER_TRADE` from `RISK_PER_TRADE`; SL floors at zero (fixes A5)

### Phase R2 — State, fills, reconciliation
- SQLite trade journal (orders, fills, positions, daily PnL) — single source of truth
- Startup reconciliation: journal vs `GET /positions`; alert on mismatch (fixes B1)
- `correlationId` on every order; poll order status until terminal; PnL from actual fills (fixes B2, B4)
- Idempotency: reject duplicate webhook alerts within a time window (fixes B4)

### Phase R3 — Operations
- Daily token renewal via `/v2/RenewToken` + expiry alerting (fixes B3)
- Retry with exponential backoff + circuit breaker around every Dhan call (fixes B5)
- Healthcheck endpoint + heartbeat log; systemd unit / Dockerfile for supervised runs
- Structured JSON logging with trade IDs

### Phase R4 — Validation
- Backtest engine over Dhan historical data with **fees + slippage model**
  (brokerage, STT, exchange charges, GST, stamp — Indian F&O costs are material)
- Walk-forward evaluation; report hit rate, profit factor, max drawdown, Sharpe
- Unit tests for `indicators.py`, `strategy.py`, selector filtering
- One-week paper-trading soak before any live deployment
- **Kill criterion:** if backtested profit factor after costs < 1.1, the strategy
  does not go live — parameters get revisited instead

---

## Part 3 — New module: `fx_commodities/`

> **AMENDED 2026-07-08:** This part originally targeted Dhan (Indian exchanges).
> Per user decision, `fx_commodities/` now targets **MetaTrader 5 / third-party
> brokers** for true global FX spot (EURUSD/GBPUSD/USDJPY), bullion
> (XAUUSD/XAGUSD), and commodity CFDs. The authoritative, buildable spec is
> [`fx_commodities/REQUIREMENTS.md`](fx_commodities/REQUIREMENTS.md) **v2.0** —
> where it conflicts with the text below, REQUIREMENTS.md wins. The section
> below is retained for the architectural rationale (features, model, risk),
> which carries over.

Prediction and trading engine for **currency crosses, bullion, and commodities**,
originally scoped for Dhan on Indian exchanges (superseded — see amendment above).

### 3.1 Instruments

| Class | Contracts | Dhan segment | Session (IST) |
|-------|-----------|--------------|----------------|
| Currency crosses | EURUSD, GBPUSD, USDJPY futures | `NSE_CURRENCY` | 09:00–19:30 |
| INR pairs (optional, more liquid) | USDINR, EURINR, GBPINR, JPYINR | `NSE_CURRENCY` | 09:00–17:00 |
| Bullion | GOLD / GOLDM, SILVER / SILVERM futures | `MCX_COMM` | 09:00–23:30 |
| Energy & metals | CRUDEOIL, NATURALGAS, COPPER futures | `MCX_COMM` | 09:00–23:30 |

**Honest liquidity note:** NSE cross-currency futures (EURUSD/GBPUSD/USDJPY) trade
thin compared to INR pairs and MCX bullion. The module will measure realized spread
during the data-collection phase and drop any contract whose median spread makes the
model's expected edge negative. INR pairs are the fallback proxies (USDINR inverse-
correlates with the same USD flows).

### 3.2 Data infrastructure (the foundation — built first)

Everything downstream depends on tick and depth data that **Dhan does not provide
historically** — we must record it ourselves.

- **`data/feed.py`** — Dhan Live Market Feed WebSocket client (ticker / quote / full
  packets), auto-reconnect, sequence-gap detection
- **`data/depth.py`** — Dhan 20-level Full Market Depth WebSocket subscriber
- **`data/recorder.py`** — persists ticks + depth snapshots to Parquet (partitioned
  by symbol/date); runs as its own always-on process
- **`data/store.py`** — query layer; also pulls Dhan historical OHLCV for backtesting
  the baseline strategy while tick history accumulates

> A 2–4 week recording period is required before orderflow features have enough
> history to train on. The baseline DEMA layer can trade (paper) from day one using
> OHLCV; the ML layer activates only after the data moat exists.

### 3.3 Feature engineering — orderflow, heatmaps, and friends

**Orderflow (`features/orderflow.py`)**
- Aggressor classification per tick (quote rule: trade at/above ask = buyer-initiated)
- **CVD** (cumulative volume delta) + CVD/price divergence detection
- **Footprint aggregation**: per-price-level buy/sell volume per bar; diagonal
  imbalance ratios (≥ 3:1), stacked imbalances, absorption (high volume, no progress)
- **OFI** (Order Flow Imbalance, Cont–Kukanov–Stoikov) from best bid/ask updates —
  one of the few microstructure features with published short-horizon predictive power

**Liquidity heatmap (`features/heatmap.py`)**
- Rolling matrix of 20-level resting depth over time (the "Bookmap-style" view)
- Detects: persistent liquidity walls (support/resistance), pulling/stacking of
  quotes ahead of moves, depth imbalance ratio (bid depth vs ask depth)

**Classical structure (`features/volume_profile.py`, `features/indicators.py`)**
- Session volume profile: POC, VAH/VAL, developing value area
- VWAP + σ-bands, ATR, realized volatility (Yang-Zhang)
- Regime features: ADX (trend strength), rolling Hurst exponent (trending vs
  mean-reverting), time-of-day encoding — **London/NY session overlaps drive FX and
  bullion volatility and must be features, not noise**

### 3.4 The mathematical model — layered ensemble

Buy/sell signals are never skipped: the final output is always
`{signal: BUY|SELL|HOLD, confidence, entry, stop_loss, target}`.

```
Layer 0  Baseline:      DEMA 10/20/95 crossover on futures (continuity with indices/,
                        and the benchmark every other layer must beat)
Layer 1  Statistical:   Gradient-boosted trees (LightGBM) on the full feature set.
                        Labels via triple-barrier method (López de Prado):
                        P(price hits +k·ATR before −k·ATR within N bars).
                        Probability calibration (isotonic) so 0.65 means 65%.
Layer 2  Microstructure gate: a Layer-1 signal only fires if OFI + CVD agree with
                        its direction over the last M bars — orderflow as veto.
Trend state:            Kalman filter on price → smoothed trend + velocity, used as
                        a Layer-1 feature and to trail stops adaptively.
```

**Risk & sizing**
- SL/TP as ATR multiples (adaptive to each instrument's volatility, not fixed ₹)
- Position size: fractional Kelly from calibrated probability, hard-capped at a
  fixed % of capital per trade; per-instrument and portfolio daily-loss limits
- Correlation guard: no simultaneous same-direction USD exposure across
  EURUSD/GBPUSD/gold (they share the dollar leg)

**Validation regime (non-negotiable before live)**
- Walk-forward training (train 3 months → test 2 weeks, roll)
- Purged k-fold CV with embargo to kill label leakage
- Full cost model: exchange fees, spread-crossing, slippage vs recorded depth
- Kill criterion: out-of-sample profit factor after costs < 1.1 ⇒ not deployed

### 3.5 Module layout

```
fx_commodities/
├── config.py                  # env-driven settings
├── instruments.py             # contract specs: tick size, lot, sessions, segments
├── data/
│   ├── feed.py                # market feed websocket
│   ├── depth.py               # 20-level depth websocket
│   ├── recorder.py            # tick + depth → Parquet
│   └── store.py               # query layer + historical OHLCV
├── features/
│   ├── orderflow.py           # CVD, footprint, OFI, absorption
│   ├── heatmap.py             # liquidity heatmap + wall detection
│   ├── volume_profile.py      # POC/VAH/VAL, VWAP
│   └── indicators.py          # DEMA, ATR, ADX, Hurst, realized vol
├── model/
│   ├── labeling.py            # triple-barrier labels
│   ├── train.py               # LightGBM + calibration + walk-forward
│   ├── kalman.py              # trend-state filter
│   └── ensemble.py            # layer composition → final signal
├── backtest/
│   ├── engine.py              # event-driven backtester
│   ├── costs.py               # fees + slippage model
│   └── walkforward.py         # rolling evaluation + reports
├── execution/
│   ├── risk.py                # sizing, limits, correlation guard
│   ├── executor.py            # order placement + fill tracking (journaled)
│   └── journal.py             # SQLite trade journal
├── signals.py                 # emits BUY/SELL/HOLD + SL/TP (+ optional webhook out)
├── trader.py                  # live loop
└── paper.py                   # paper-trading harness
```

Shared plumbing (Dhan auth, retry/circuit-breaker, journaling) will live in a new
`common/` package used by `fx_commodities/` — **`indices/` keeps its own copies
untouched** until its hardening phases migrate it onto `common/` deliberately.

### 3.6 Build order

| Phase | Deliverable | Gate to next phase |
|-------|-------------|--------------------|
| 1 | `instruments.py` + websocket feed + recorder running 24/7 | Clean data recording for ≥ 1 week |
| 2 | Feature library on recorded data + baseline DEMA port + backtester + cost model | Backtest report on baseline |
| 3 | Labeling + LightGBM + Kalman + ensemble; walk-forward validation | OOS profit factor ≥ 1.1 after costs |
| 4 | Paper trading (2+ weeks), then live with minimum size | Paper results consistent with backtest |

In parallel, `indices/` hardening phases R1–R4 can proceed independently.

---

## Decision points — RESOLVED (2026-07-08)

1. **Cross pairs vs INR pairs** — record both during Phase 1, measure spreads,
   let data decide the tradeable universe. ✔ approved
2. **MCX contract size** — mini contracts (GOLDM, SILVERM, CRUDEOILM). ✔ approved
3. **Capital allocation** — configurable split, default 50/50 between modules;
   all `fx_commodities/` risk limits derive from its slice. ✔ approved

The full implementation brief lives at
[`fx_commodities/REQUIREMENTS.md`](fx_commodities/REQUIREMENTS.md) — written to be
buildable by an engineer or AI model with no other context.
