# fx_commodities — Build Requirements Specification

**Version 2.0 — 2026-07-08 — MT5 / third-party-broker edition**
**Status: APPROVED FOR BUILD**
**Supersedes v1.0 (Dhan edition). Dhan remains the broker for `indices/` only.**

This document is a complete, self-contained implementation brief. An engineer (or
AI model) with no prior context on this repository must be able to build the entire
module from this document alone. Read it fully before writing any code.

---

## 0. Context and hard constraints

### 0.1 What this module is

A prediction and trading engine for **spot FX (EURUSD, GBPUSD, USDJPY), bullion
(XAUUSD, XAGUSD), and commodity CFDs (WTI crude, natural gas, copper)** executed
through **MetaTrader 5**, behind a broker-abstraction layer so other brokers
(OANDA, cTrader, IBKR) can be added without touching strategy code. It produces
`BUY / SELL / HOLD` signals with confidence, entry, stop-loss, and target, using
orderflow analytics (where the broker's data supports them), liquidity/quote
dynamics, volume profile, and a machine-learning ensemble — then executes with
strict risk controls.

### 0.2 Why MT5 (and what it changes vs the Dhan design)

| Aspect | Dhan (v1 design) | MT5 (this design) |
|---|---|---|
| Instruments | NSE/MCX Indian futures | True global FX spot, spot metals, commodity CFDs |
| Sessions | Exchange hours (IST) | 24/5 continuous; daily swap at server midnight |
| Data push | Websocket push | **Pull only** — the Python API polls; no push feed |
| Volume | Real exchange volume | `tick_volume` (quote updates); `volume_real` usually 0 on FX/CFD — **orderflow features must adapt** (§7.0) |
| Depth | 5/20-level exchange book | `market_book_*` DOM only if the broker streams it (many retail brokers don't) |
| Stops | Separate stop order legs | **Native `sl`/`tp` on the position** — server-side, survives our process dying |
| Platform | Any OS (REST) | **Windows-only Python package**; terminal must run on same machine (§0.4) |

### 0.3 Locked-in decisions (do not revisit)

| # | Decision | Value |
|---|----------|-------|
| D1 | Universe selection is **data-driven**: record all candidate symbols during Phase 1; drop any whose median relative spread > `MAX_MEDIAN_SPREAD_PCT` (default 0.03% for FX majors, 0.06% for metals/commodities). |
| D2 | Small-account sizing: volumes in **fractional lots**, min 0.01, always respecting `volume_min/step/max` from `symbol_info`. |
| D3 | Capital split with `indices/` stays: `ALLOC_FX_COMMODITIES` (default 0.50) of `CAPITAL_TOTAL`. All module risk limits derive from that slice. |
| D4 | Broker abstraction is mandatory: strategy/risk code may import **only** `broker/base.py` types, never `MetaTrader5` directly. |

### 0.4 Hard constraints for the implementer

1. **Do NOT modify anything under `indices/`.** It stays Dhan-based and frozen.
2. New code lives in `fx_commodities/` and `common/`. Python ≥ 3.11, full type
   hints. The `MetaTrader5` package pins the runtime to **Windows** (or a Windows
   VPS / Wine). All non-broker code (features, model, backtest) must remain
   OS-independent and testable on Linux with recorded data — CI runs everything
   except `broker/mt5_adapter.py` integration tests.
3. Every broker call goes through the adapter + retry policy (§12).
4. Nothing trades live until the Phase gates (§14) pass. `trader.py --live`
   refuses to start if the latest walk-forward report is missing or shows
   `profit_factor_after_costs < 1.1`.
5. No secrets in code. Credentials via `.env` only.
6. Every order carries this module's **magic number** (`MAGIC=520025`, config-
   overridable). The executor must never touch positions whose magic differs —
   that is how we coexist with manual trades and other EAs on the same account.

---

## 1. MT5 Python API reference (verified)

Package: `pip install MetaTrader5` (imported as `mt5`). The MT5 **terminal must be
installed, logged in, and running** on the same Windows machine; the package is an
IPC bridge to it, not a network client.

### 1.1 Session lifecycle

```python
mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
               login=12345678, password="...", server="Broker-Server",
               timeout=10_000)          # → bool; mt5.last_error() on failure
mt5.account_info()   # balance, equity, margin_free, currency, leverage,
                     # margin_so_call, trade_mode (demo/real), margin_mode
                     #   (NETTING vs HEDGING — record it; executor logic §11.2)
mt5.terminal_info()  # connected, trade_allowed, ping_last
mt5.shutdown()
```

`initialize` must be wrapped with a watchdog: if `terminal_info().connected` goes
false, alert + block new entries until reconnected (positions keep their
server-side SL/TP — that is the safety net).

### 1.2 Symbols

```python
mt5.symbol_select(sym, True)          # add to Market Watch — required before data/orders
si = mt5.symbol_info(sym)
# fields used: digits, point, spread, trade_tick_size, trade_tick_value,
# trade_contract_size, volume_min, volume_max, volume_step,
# swap_long, swap_short, swap_rollover3days (day of triple swap),
# filling_mode (bitmask of allowed ORDER_FILLING_*), trade_mode,
# session_deals / session_buy_orders (optional), currency_profit
```

**Pip/point value:** rupee/dollar value of one point per lot =
`trade_tick_value × (point / trade_tick_size)`. Use this — never hardcode pip
values; contract sizes differ per broker.

### 1.3 Candles & ticks (pull model)

```python
mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 5000)
# numpy structured array: time (epoch s), open, high, low, close,
#                         tick_volume, spread, real_volume
mt5.copy_ticks_from(sym, from_dt, 100_000, mt5.COPY_TICKS_ALL)
mt5.copy_ticks_range(sym, from_dt, to_dt, mt5.COPY_TICKS_ALL)
# tick fields: time, bid, ask, last, volume, time_msc (epoch ms),
#              flags, volume_real
# flags bitmask: TICK_FLAG_BID(2) ask(4) LAST(8) VOLUME(16) BUY(32) SELL(64)
```

- Live "streaming" = poll `copy_ticks_from(sym, last_seen_ms, ...)` on a tight
  loop (`POLL_INTERVAL_MS`, default 200 ms across the universe, §4.1) and
  de-duplicate on `time_msc`.
- History depth: brokers keep weeks–months of ticks and years of M1 — Phase 1
  must probe actual availability per symbol and record the answer.

### 1.4 Depth of market (optional per broker)

```python
mt5.market_book_add(sym)              # subscribe
book = mt5.market_book_get(sym)      # tuple of BookInfo(type, price, volume, volume_dbl)
# type: BOOK_TYPE_SELL=1 (ask), BOOK_TYPE_BUY=2 (bid),
#       BOOK_TYPE_SELL_MARKET=3, BOOK_TYPE_BUY_MARKET=4
mt5.market_book_release(sym)
```

Returns an empty tuple when the broker provides no DOM for that symbol — the
capability probe (§4.0) records this per symbol.

### 1.5 Orders and positions

```python
req = {
  "action":   mt5.TRADE_ACTION_DEAL,          # market execution
  "symbol":   sym,
  "volume":   0.10,                            # lots, float
  "type":     mt5.ORDER_TYPE_BUY,              # or ORDER_TYPE_SELL
  "price":    mt5.symbol_info_tick(sym).ask,
  "sl":       1.06920,                         # native server-side stop
  "tp":       1.07480,                         # native server-side target
  "deviation": 20,                             # max slippage, points
  "magic":    520025,
  "comment":  "fxc-20260708-0001",             # correlation id, ≤ 31 chars
  "type_time": mt5.ORDER_TIME_GTC,
  "type_filling": <from symbol_info.filling_mode>,  # IOC preferred, else FOK
}
chk = mt5.order_check(req)     # margin/validity pre-check — ALWAYS call first
res = mt5.order_send(req)      # OrderSendResult
# res.retcode == mt5.TRADE_RETCODE_DONE (10009) → success
# res.deal, res.order, res.price (actual fill), res.volume
```

- Modify SL/TP on an open position: `TRADE_ACTION_SLTP` with `position=ticket`.
- Close: send opposite `TRADE_ACTION_DEAL` with `position=ticket` (hedging mode)
  — on netting accounts an opposite deal nets automatically.
- `mt5.positions_get(symbol=...)` → open positions (ticket, volume, price_open,
  sl, tp, profit, magic, comment).
- `mt5.history_deals_get(from_dt, to_dt)` → actual fills (price, volume,
  commission, swap, profit, magic) — **the only source of truth for realized
  PnL and costs**.
- Retcodes to handle explicitly: 10009 done, 10004 requote, 10006 rejected,
  10013 invalid request, 10014 invalid volume, 10016 invalid stops (SL too close
  — respect `symbol_info.trade_stops_level`), 10018 market closed, 10019 no
  money, 10027 autotrading disabled in terminal (alert operator!), 10030
  unsupported filling mode (retry with the other mode once).

---

## 2. Instrument universe

### 2.1 Candidate symbols (canonical keys → typical broker names)

| Key | Class | Typical symbol | Notes |
|-----|-------|----------------|-------|
| EURUSD | FX major | `EURUSD` | |
| GBPUSD | FX major | `GBPUSD` | |
| USDJPY | FX major | `USDJPY` | JPY quote — 3-digit pricing |
| XAUUSD | Bullion | `XAUUSD` / `GOLD` | spot gold vs USD |
| XAGUSD | Bullion | `XAGUSD` / `SILVER` | spot silver |
| WTI | Energy | `USOIL` / `XTIUSD` / `WTI` | CFD; check expiry-less vs futures-based |
| NATGAS | Energy | `NATGAS` / `XNGUSD` | CFD |
| COPPER | Metal | `COPPER` / `XCUUSD` | CFD |

Broker symbol names vary (suffixes like `EURUSD.r`, `XAUUSDm`). Therefore:
`SYMBOL_MAP` in `.env` (`EURUSD=EURUSD.r,...`), resolved once at startup;
`instruments.py` validates every mapped symbol exists via `symbol_info` and
fails fast listing near-miss candidates (`mt5.symbols_get("*EURUSD*")`).

### 2.2 `instruments.py`

- `@dataclass(frozen=True) class Contract`: `key, broker_symbol, digits, point,
  tick_size, tick_value, contract_size, volume_min, volume_max, volume_step,
  stops_level_points, swap_long, swap_short, swap_triple_day, has_dom (bool),
  has_last_ticks (bool), has_real_volume (bool), quote_ccy`.
- Capability fields (`has_dom`, `has_last_ticks`, `has_real_volume`) come from
  the Phase 1 probe (§4.0) persisted to `reports/capabilities.json`; at runtime
  they gate which features compute for that symbol (§7.0).
- No expiry/rollover logic needed for spot/CFD — delete that concern from v1.
  Exception: if the chosen broker's energy CFDs are futures-based with expiry,
  the probe must detect `symbol_info.expiration_time ≠ 0` and the resolver then
  applies the v1 rollover rule (flatten 2 days before expiry).

### 2.3 Sessions & timing

- FX/metals trade ~24/5: Monday 00:05 → Friday 23:50 **server time**. All
  internal timestamps are UTC; server-time offset discovered by comparing
  `symbol_info_tick().time` to UTC now, persisted per session.
- **Swap (rollover) cost** applies to positions held across server midnight;
  triple swap on `swap_rollover3days` (usually Wednesday). Default policy:
  `ALLOW_OVERNIGHT=false` → flatten `SQUARE_OFF_MIN_BEFORE_SWAP` (default 15 min)
  before server midnight. If `ALLOW_OVERNIGHT=true`, swap must be included in
  the cost model and in live PnL (it arrives in `history_deals_get` as `swap`).
- Do not trade the first `SKIP_MIN_AFTER_OPEN` (default 30) minutes after Monday
  open and the last 30 before Friday close (spread blowout windows).
- Volatility session flags (all as UTC, converted from the London/NY calendar):
  `in_london (07:00–16:00 UTC)`, `in_ny (12:00–21:00 UTC)`,
  `in_overlap (12:00–16:00 UTC)`. These replace v1's IST session features.

---

## 3. Repository layout to create

```
common/                      # shared with future modules; NO Dhan imports here
├── __init__.py
├── retry.py                 # retry/backoff + circuit breaker decorators
├── journal.py               # SQLite journal (§11.1)
└── alerts.py                # operator alerting (CRITICAL log + webhook stub)

fx_commodities/
├── __init__.py
├── config.py                # pydantic settings from .env (§13)
├── instruments.py           # §2
├── broker/
│   ├── __init__.py
│   ├── base.py              # BrokerAdapter Protocol + dataclasses (§4.6)
│   └── mt5_adapter.py       # the only file importing MetaTrader5 (§4)
├── data/
│   ├── __init__.py
│   ├── recorder.py          # tick/quote/DOM poller → Parquet (§4.3)
│   ├── store.py             # readers + M1 backfill (§4.4)
│   └── schemas.py           # PyArrow schemas (§4.5)
├── features/
│   ├── __init__.py
│   ├── bars.py              # tick → time/volume bars (§6)
│   ├── orderflow.py         # adaptive orderflow (§7.1)
│   ├── quote_dynamics.py    # spread/quote-intensity features (§7.2)
│   ├── heatmap.py           # DOM heatmap, only where has_dom (§7.3)
│   ├── volume_profile.py    # POC/VAH/VAL, VWAP on tick_volume (§7.4)
│   └── indicators.py        # DEMA, ATR, ADX, Hurst, Yang-Zhang (§7.5)
├── model/                   # unchanged in structure from v1
│   ├── labeling.py          # triple-barrier (§8.1)
│   ├── dataset.py           # purged CV assembly (§8.2)
│   ├── train.py             # LightGBM + isotonic (§8.3)
│   ├── kalman.py            # trend filter (§8.4)
│   └── ensemble.py          # decision logic (§9.1)
├── backtest/
│   ├── engine.py            # event-driven engine (§10.1)
│   ├── costs.py             # spread+commission+swap model (§10.2)
│   └── walkforward.py       # rolling evaluation + gate (§10.3)
├── execution/
│   ├── risk.py              # sizing, limits, correlation guard (§9.3)
│   └── executor.py          # order lifecycle + reconciliation (§11)
├── signals.py               # Signal dataclass + emitters (§9.4)
├── trader.py                # live loop
├── paper.py                 # paper harness (§14 Phase 4)
├── requirements.txt
├── .env.example
├── RUNBOOK.md               # §15
└── tests/
```

Dependencies: `MetaTrader5; platform_system=="Windows"`, `pandas, numpy, pyarrow,
lightgbm, scikit-learn, pydantic, pydantic-settings, python-dotenv, requests,
fastapi, uvicorn, pytest` (fastapi/uvicorn only for the health endpoint).

---

## 4. Broker & data layer

### 4.0 Capability probe (Phase 1, runs once per broker)

`python -m fx_commodities.broker.probe` writes `reports/capabilities.json`:

```json
{"EURUSD": {"broker_symbol": "EURUSD", "has_dom": false,
             "has_last_ticks": false, "has_real_volume": false,
             "tick_history_days": 90, "m1_history_days": 2200,
             "median_spread_points": 6, "filling_modes": ["IOC"]}, ...}
```

Method: `symbol_info` + `market_book_add/get` (empty → no DOM) + sample
`copy_ticks_range` over last week (any `TICK_FLAG_LAST` → has_last_ticks; any
`volume_real > 0` → has_real_volume) + binary-search earliest available tick/M1.

### 4.1 `broker/mt5_adapter.py`

Implements `BrokerAdapter` (§4.6). Key requirements:

- Single-threaded MT5 access (the package is not thread-safe): all calls funnel
  through one worker thread with an internal queue; public methods are
  thread-safe wrappers.
- `stream_ticks(symbols)` generator: round-robin poll `copy_ticks_from` per
  symbol using last `time_msc` watermark + 1 ms, `POLL_INTERVAL_MS` sleep per
  full cycle; yields normalized `Tick` objects; dedupe on (symbol, time_msc,
  bid, ask).
- `stream_dom(symbols)`: for `has_dom` symbols only; snapshot via
  `market_book_get` each poll cycle, throttled to `DOM_SNAPSHOT_INTERVAL_MS`
  (default 500).
- Health: `is_connected()` checks `terminal_info().connected and
  account_info() is not None`; auto-`initialize()` retry with backoff on drop.

### 4.2 Normalized broker types (`broker/base.py`)

```python
class Tick(NamedTuple):
    ts_ms: int; symbol: str; bid: float; ask: float
    last: float | None; volume: float | None      # None when broker gives no trades
    side: int                                      # +1/-1 from flags BUY/SELL, else 0

class DomLevel(NamedTuple):  side: int; price: float; volume: float
class Candle(NamedTuple):    ts: int; o: float; h: float; l: float; c: float
                             tick_volume: int; spread_points: int; real_volume: int

class OrderResult(NamedTuple): ok: bool; retcode: int; deal_id: int | None
                               fill_price: float | None; message: str

class BrokerAdapter(Protocol):
    def candles(self, symbol, timeframe_min, count, from_ts=None) -> list[Candle]: ...
    def stream_ticks(self, symbols) -> Iterator[Tick]: ...
    def stream_dom(self, symbols) -> Iterator[tuple[str, list[DomLevel]]]: ...
    def market_order(self, symbol, side, lots, sl, tp, comment) -> OrderResult: ...
    def modify_sltp(self, ticket, sl, tp) -> OrderResult: ...
    def close_position(self, ticket, lots=None) -> OrderResult: ...
    def positions(self, magic=None) -> list[Position]: ...
    def deals(self, from_ts, to_ts, magic=None) -> list[Deal]: ...
    def account(self) -> AccountInfo: ...
```

Strategy/risk/backtest code imports only these types (constraint D4).

### 4.3 `data/recorder.py`

Standalone process (`python -m fx_commodities.data.recorder`), runs 24/5:

- Consumes `stream_ticks` (all symbols) + `stream_dom` (DOM symbols).
- Buffers → Parquet flush every `FLUSH_INTERVAL_S=60`:
  `data_root/{table}/symbol={key}/date={YYYY-MM-DD}/part-{HHMMSS}.parquet`
  (dates in UTC).
- Tables: `ticks`, `dom` (§4.5). No separate quote table — FX ticks are quotes.
- Heartbeat log with per-symbol row counts every 5 min; `feed_gap` journal event
  when a symbol is silent > 120 s during market hours (FX is never silent that
  long — it means the poll loop or terminal died).
- Clean SIGTERM flush; corrupt-file quarantine on boot (validate footers).

### 4.4 `data/store.py`

- `read_ticks/read_dom(key, date_from, date_to) -> pd.DataFrame` (UTC tz-aware).
- `backfill_m1(contract, years=3)`: paginate `copy_rates_from_pos` /
  `copy_rates_range` into the `ohlcv` table; incremental and idempotent.

### 4.5 Parquet schemas

```
ticks: ts_ms int64, symbol_key string, bid float64, ask float64,
       last float64 (nullable), volume float64 (nullable), side int8
dom:   ts_ms int64, symbol_key string, side int8, level int8,
       price float64, volume float64
ohlcv: ts int64, symbol_key string, tf_min int16, o/h/l/c float64,
       tick_volume int64, spread_points int32, real_volume int64
```

### 4.6 (merged into 4.2)

---

## 5. (reserved)

---

## 6. Bars (`features/bars.py`)

- `time_bars(ticks, interval_s)` on **mid price** (`(bid+ask)/2`), columns:
  `open, high, low, close, tick_count, tick_volume, spread_mean_points,
  spread_max_points, delta*, buy_ticks*, sell_ticks*, vwap*` — starred columns
  computed per the orderflow availability rules in §7.0/§7.1.
- Strictly exchange/broker timestamps (`time_msc`), aligned to UTC wall-clock
  boundaries. `volume_bars(ticks, bucket)` on tick_volume for the model layer.

---

## 7. Feature engineering

### 7.0 Feature availability policy (critical, new in v2)

Retail FX is decentralized: most broker feeds carry **quotes only** (no trades,
no real volume, no DOM). Features therefore come in tiers, gated per symbol by
the capability probe:

| Tier | Requires | Features |
|------|----------|----------|
| T0 (always) | quotes | everything in §7.2, §7.4 (tick_volume-based), §7.5 |
| T1 | `has_last_ticks` | true aggressor CVD, footprint, absorption (§7.1) |
| T2 | `has_dom` | DOM heatmap, walls, depth imbalance (§7.3) |

`dataset.py` builds per-symbol feature matrices using only available tiers, and
records the tier set in `dataset_version`. The model for a T0-only symbol simply
has fewer columns — **never impute fake orderflow**.

### 7.1 Orderflow (`features/orderflow.py`) — T1, plus T0 fallback

- **True aggressor (T1):** `side` from `TICK_FLAG_BUY/SELL`; CVD, footprint
  diagonal imbalances (ratio ≥ 3.0, stacked ≥ 3), absorption — formulas
  identical to v1 (kept: CVD slope over n bars, CVD/price divergence flag).
- **Quote-based proxy delta (T0 fallback, separate column names —
  `pdelta_*`):** classify each quote tick by mid-price tick rule (+1 up-move,
  −1 down-move, inherit on unchanged), weight = 1 (tick count). This measures
  directional quote pressure, not traded volume — name it honestly, and let
  feature importance decide if it earns its place.

### 7.2 Quote dynamics (`features/quote_dynamics.py`) — T0, new in v2

Per bar:
- `spread_mean_pct, spread_std, spread_z` (vs trailing 200 bars) — spread
  widening precedes news moves and fades.
- `quote_intensity` = ticks/second; `intensity_z`.
- `microprice_pressure` = mean of `(bid_size×ask + ask_size×bid)/(bid_size+ask_size) − mid`
  when tick sizes present, else omitted.
- `updown_ratio` = up-ticks / down-ticks over the bar.

### 7.3 DOM heatmap (`features/heatmap.py`) — T2 only

Same design as v1 (rolling price×time resting-liquidity matrix, wall detection at
95th percentile persisting ≥ 10 bars, pull/stack score, depth imbalance), built
from `dom` snapshots. Exports `dist_to_support_atr, dist_to_resistance_atr,
support_strength, resistance_strength, depth_imbalance, pull_stack_score`.

### 7.4 Volume profile & VWAP (`features/volume_profile.py`) — T0

As v1, but volume = `tick_volume` (or real volume where `has_real_volume`).
Session = UTC day. Developing POC/VAH/VAL (70% value area), session VWAP ±1σ/±2σ
computed on tick_volume weights. Exports `dist_to_poc_atr, above_vah, below_val,
dist_to_vwap_sigma`.

### 7.5 Classical indicators (`features/indicators.py`) — T0

Unchanged from v1: DEMA(10/20/95) = `2·EMA − EMA(EMA)`, ATR(14) & ADX(14)
Wilder, Yang-Zhang realized vol (20 bars), rolling Hurst (200 bars),
minute-of-day sin/cos, plus the UTC session flags from §2.3.

**No-lookahead rule and test apply to every feature exactly as v1:** value at
bar *t* uses only `ts < t.close`; `tests/test_no_lookahead.py` truncates input
and asserts unchanged earlier values.

---

## 8. Model layer (largely unchanged from v1)

### 8.1 Triple-barrier labeling
Upper `close + PT_MULT×ATR` (2.0), lower `close − SL_MULT×ATR` (1.0), vertical
`MAX_HOLD_BARS` (60). `y ∈ {+1, −1, 0}`, record `t_touch`.

### 8.2 Dataset
Per-symbol matrices with tier-gated columns (§7.0) + symbol one-hot for pooled
training. **Purged K-fold with embargo** (`EMBARGO_PCT=0.01`) exactly as v1 —
overlapping-label leakage control is mandatory. Persist with `dataset_version`
hash of (features, tiers, label params, date range).

### 8.3 Training
LightGBM (`num_leaves=31, max_depth=6, lr=0.05, n_estimators=400,
min_child_samples=100, subsample=0.8, colsample_bytree=0.8`), tuned only via
purged CV; isotonic calibration on a held-out fold; persist
`models/{scope}/{dataset_version}/model.joblib` + `metrics.json` + feature-gain
report (alert if one feature > 40% gain — leakage smell).

### 8.4 Kalman trend filter
Local linear trend `[level, velocity]`, `F=[[1,1],[0,1]]`, `H=[1,0]`,
`KALMAN_Q=1e-5, KALMAN_R=1e-2` per-symbol overridable. Outputs `kf_level,
kf_velocity, kf_velocity_z`; used as features and for trailing stops.

---

## 9. Ensemble, risk, and signal output

### 9.1 Decision logic (`model/ensemble.py`)

```
L0_long  = DEMA10 crossed above DEMA20 this bar AND close > DEMA95
L0_short = DEMA10 crossed below DEMA20 this bar AND close < DEMA95
L1_long  = p_up ≥ P_ENTRY (0.60)      L1_short = p_up ≤ 1 − P_ENTRY
# Orderflow gate uses the best available tier:
gate_long  = (T1: ofi/cvd_slope > 0) else (T0: pdelta_slope > 0 AND updown_ratio > 1)
gate_short = symmetric
signal = BUY  if (L1_long and gate_long)  or (L0_long and gate_long)
signal = SELL if (L1_short and gate_short) or (L0_short and gate_short)
else HOLD
confidence = p_up (BUY) / 1−p_up (SELL) / 0.5
```

Exits: opposite signal, native SL/TP touch (server-side), trailing stop —
`modify_sltp(ticket, sl=kf_level − TRAIL_MULT×ATR)` for longs once ≥ 1R in
profit — and the swap-avoidance square-off (§2.3) when `ALLOW_OVERNIGHT=false`.

### 9.2 Regime guard
Skip entries when `ADX < 15 and |hurst − 0.5| < 0.05`, when live spread over the
last 30 min > `MAX_LIVE_SPREAD_PCT`, when `spread_z > 3` (news blowout), and
within `SKIP_MIN_AFTER_OPEN`/Friday-close windows (§2.3).

### 9.3 Risk & sizing (`execution/risk.py`)

- `capital = CAPITAL_TOTAL × ALLOC_FX_COMMODITIES`; account currency conversion
  via `account_info().currency` (assume USD account by default; if INR-funded
  via broker, conversion handled by broker — use `account_info().balance`
  directly and treat `CAPITAL_TOTAL` as account-currency).
- `risk_amt = capital × RISK_PER_TRADE_PCT (0.5%)`.
- Stop distance in account currency per lot:
  `stop_value = (SL_MULT × ATR / tick_size) × tick_value`.
- `lots = round_step(risk_amt / stop_value, volume_step)`, clipped to
  `[volume_min, min(volume_max, MAX_LOTS=1.0)]`; if below `volume_min` → no trade.
- Kelly modulation `size_mult = clip(2×confidence − 1, 0, 1) × KELLY_FRACTION
  (0.5)`; baseline-path (L0) trades fixed `size_mult = 0.25`.
- Margin pre-check via `order_check` — mandatory before every entry.
- **Correlation guard (USD leg):** long EURUSD, long GBPUSD, short USDJPY, long
  XAUUSD, long XAGUSD are all short-USD. Net same-direction USD-group entries
  capped at `MAX_CORRELATED_POSITIONS=2`; global `MAX_OPEN_POSITIONS=3`.
  WTI/NATGAS/COPPER count half-weight in the USD group.
- Loss limits: per-symbol daily `1%`, module daily `2%` of capital (realized +
  floating, computed from `positions()` + `deals()` each cycle). Breach ⇒
  flatten module positions (by magic only!) + halt entries until next UTC day;
  halt state persists in journal across restarts.

### 9.4 Signal schema (`signals.py`)

As v1 with field renames: `contract_key → symbol_key`, `security_id →
broker_symbol`; add `lots: float`, `tier: str` ("T0"/"T1"/"T2"). Every non-HOLD
signal optionally POSTs to `SIGNAL_WEBHOOK_URL`.

---

## 10. Backtesting

### 10.1 Engine
Event-driven over bars; **live and backtest call the identical
`ensemble.decide()` / `risk.size()`**. Entry fills at `close + slippage`; SL/TP
intra-bar conservative rule (both touched in one bar ⇒ SL first). Metrics: net
PnL, profit factor, hit rate, avg win/loss, max DD, Sharpe, turnover, cost total.

### 10.2 Cost model (`backtest/costs.py`) — MT5 edition

Per round trip, config-driven:
- **Spread**: from recorded per-symbol, per-hour median spread (Phase 1 data);
  applied as half-spread per side on mid-price fills. Fallback
  `DEFAULT_SPREAD_POINTS` per symbol.
- **Commission**: `COMMISSION_PER_LOT_SIDE` (default $3.5) × lots × 2.
- **Swap**: if `ALLOW_OVERNIGHT`, apply `swap_long/short` points per night held,
  ×3 on the triple-swap day.
- **Slippage**: `DEFAULT_SLIPPAGE_POINTS=2` per side on top of spread (stress
  test at 2× in the report).

### 10.3 Walk-forward
Train 60 trading days → test 10 → step 10; ≥ 6 windows; hyperparameters frozen
after first-window purged-CV tune; recalibrate each step. Report JSON + MD +
equity PNG in `reports/`. **Deployment gate in code**: aggregate
`profit_factor_after_costs ≥ 1.1` AND `max_dd ≤ 10%` of module capital. The
report must also show per-symbol breakdown — a symbol with PF < 1.0 gets dropped
from the live universe (`reports/live_universe.json`).

---

## 11. Execution & journaling

### 11.1 SQLite journal (`common/journal.py`)
Same DDL as v1 with columns renamed for MT5: `orders(correlation_id TEXT PK,
ticket INTEGER, deal_id INTEGER, ...)`, `positions(ticket INTEGER, magic
INTEGER, swap REAL, commission REAL, ...)`; `risk_state` and `events` tables
unchanged. `session_date` keys on **UTC date**.

### 11.2 Order lifecycle (`execution/executor.py`)

1. Journal `LOCAL_NEW` row with fresh `correlation_id` (`fxc-{yyyymmdd}-{seq}`,
   goes in MT5 `comment`) **before** sending. Duplicate id ⇒ refuse.
2. `order_check` → `order_send` with native `sl`/`tp` **in the same request** —
   the position is never unprotected, even for a millisecond, and stops survive
   our process dying (major improvement over v1's separate stop legs).
3. On `TRADE_RETCODE_DONE`: fetch the deal via `history_deals_get`, journal
   actual `fill_price, commission`; PnL only ever from deals history.
4. Retcode handling per §1.5; requote (10004) → refresh price, retry once;
   invalid stops (10016) → widen to `trade_stops_level` + 1 point, retry once;
   autotrading disabled (10027) → CRITICAL alert, halt.
5. **Account-mode awareness:** on NETTING accounts an opposite entry on the same
   symbol nets — executor must check `positions()` first and use explicit
   `close_position` for exits. On HEDGING accounts always pass `position=ticket`.
   Mode read once from `account_info().margin_mode`.
6. **Startup reconciliation:** `positions(magic=MAGIC)` vs journal open
   positions. Broker position missing in journal ⇒ CRITICAL alert + halt (never
   auto-close: manual trades have different magic, but a magic collision is
   still possible). Journal position missing at broker ⇒ close in journal as
   `reconcile_lost` (probably SL/TP hit while we were down — confirm via
   `history_deals_get` and record the real exit).
7. Poll cycle (every bar): refresh positions; if a position's SL/TP vanished
   (broker maintenance edge case), re-apply via `modify_sltp`.

---

## 12. Common infrastructure

- `common/retry.py`: `@with_retry(attempts=3, backoff=(1,2,4), jitter=True)` for
  adapter calls; circuit breaker opens after 5 consecutive adapter failures →
  new entries blocked (`CircuitOpenError`), exits still single-attempted.
- Terminal watchdog thread: `is_connected()` every 10 s; disconnect > 60 s ⇒
  alert. (Positions remain protected by server-side SL/TP — say this in the
  alert text to keep the operator calm.)
- `common/alerts.py`: `alert(level, msg, **ctx)` → structured log + optional
  `ALERT_WEBHOOK_URL` POST (Telegram-compatible stub).
- Health endpoint (FastAPI on `HEALTH_PORT=8081`): terminal connectivity, last
  tick age per symbol, open positions, daily PnL, halt state.

---

## 13. Configuration (`.env` spec)

```
# broker
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_LOGIN=            MT5_PASSWORD=            MT5_SERVER=
MAGIC=520025
SYMBOL_MAP=EURUSD=EURUSD,GBPUSD=GBPUSD,USDJPY=USDJPY,XAUUSD=XAUUSD,XAGUSD=XAGUSD,WTI=USOIL,NATGAS=NATGAS,COPPER=COPPER
# capital
CAPITAL_TOTAL=500000  ALLOC_FX_COMMODITIES=0.50
RISK_PER_TRADE_PCT=0.5  MAX_DAILY_LOSS_PCT=2.0  MAX_DAILY_LOSS_PER_SYMBOL_PCT=1.0
MAX_LOTS=1.0  MAX_OPEN_POSITIONS=3  MAX_CORRELATED_POSITIONS=2  KELLY_FRACTION=0.5
# universe / sessions
UNIVERSE=EURUSD,GBPUSD,USDJPY,XAUUSD,XAGUSD,WTI,NATGAS,COPPER
MAX_MEDIAN_SPREAD_PCT_FX=0.03  MAX_MEDIAN_SPREAD_PCT_CMD=0.06  MAX_LIVE_SPREAD_PCT=0.08
ALLOW_OVERNIGHT=false  SQUARE_OFF_MIN_BEFORE_SWAP=15  SKIP_MIN_AFTER_OPEN=30
# strategy / model
BAR_INTERVAL_S=60  PT_MULT=2.0  SL_MULT=1.0  MAX_HOLD_BARS=60  P_ENTRY=0.60
IMBALANCE_RATIO=3.0  TRAIL_MULT=1.5  KALMAN_Q=1e-5  KALMAN_R=1e-2  EMBARGO_PCT=0.01
# data
DATA_ROOT=./data_root  FLUSH_INTERVAL_S=60  POLL_INTERVAL_MS=200  DOM_SNAPSHOT_INTERVAL_MS=500
# costs
COMMISSION_PER_LOT_SIDE=3.5  DEFAULT_SLIPPAGE_POINTS=2
# ops
ALERT_WEBHOOK_URL=  SIGNAL_WEBHOOK_URL=  HEALTH_PORT=8081  LOG_LEVEL=INFO
```

---

## 14. Build phases and acceptance gates

### Phase 1 — Broker adapter + data foundation
Deliverables: `common/`, `broker/base.py`, `broker/mt5_adapter.py`, capability
probe, `instruments.py`, recorder, store + M1 backfill, health endpoint. Unit
tests: adapter logic mocked (no terminal in CI), tick normalization, resolver.
**Gate:** capability report exists; recorder ran 5 consecutive trading days with
< 0.1% gap time; spread report per symbol/hour produced; universe finalized
per D1; M1 backfill ≥ 3 years per symbol.

### Phase 2 — Features + baseline + backtester
Deliverables: `features/*` (tier-gated), bars, backtest engine + cost model,
baseline DEMA through the backtester on 3 years of M1 (T0 features only for the
historical span; tick-derived features over the recorded window).
**Gate:** baseline backtest report; all feature unit tests + no-lookahead test
green on Linux CI.

### Phase 3 — Model + walk-forward
**Gate:** OOS PF after costs ≥ 1.1, max DD ≤ 10%; per-symbol live universe
written. Failed symbols dropped; if all fail, baseline-only mode at 0.25 size —
never lower the gate.

### Phase 4 — Paper, then live
`paper.py` uses the real adapter on a **demo account** (MT5 demo = identical
API) — this is true paper trading with real broker fills. ≥ 10 sessions, PF
within 30% of backtest, zero reconciliation mismatches. Then live with
`MAX_LOTS=0.01` hard-clamped for the first two weeks regardless of sizing.

### Testing requirements
pytest; ≥ 90% line coverage on `features/`, `model/labeling.py`,
`execution/risk.py`; no-lookahead test; risk tests (halt persistence across
restart, correlation guard blocks 3rd USD position, volume_step rounding never
exceeds caps); adapter retcode-handling tests with mocked `order_send`.

---

## 15. Operator runbook (`RUNBOOK.md`)

1. **Environment:** Windows VPS (recommended: same region as broker server for
   latency), MT5 terminal installed + logged in, "Algo Trading" button enabled,
   auto-start on reboot (Task Scheduler entries for terminal, recorder, trader).
2. Broker selection guidance: prefer a regulated ECN/raw-spread broker; DOM and
   last-trade data availability materially improve the feature set (§7.0) —
   check with the capability probe on a demo account **before** funding.
3. Demo → live promotion checklist (Phase 4 gate evidence).
4. What each CRITICAL alert means and the required manual action
   (reconcile_mismatch, autotrading-disabled 10027, terminal disconnected).
5. Weekly: review `reports/` walk-forward drift; monthly: retrain schedule.

---

## 16. Explicitly out of scope (v1)

- Options on FX/metals; only spot/CFD market orders + native SL/TP.
- Additional broker adapters (OANDA/cTrader/IBKR) — the Protocol in §4.2 is
  designed for them, but only `mt5_adapter.py` ships in v1.
- Tick-level ML backtesting (bar-level with recorded-spread slippage is v1).
- Any UI beyond the health endpoint and MD reports.
- Modifying `indices/` (it stays on Dhan; separate roadmap in PLAN.md Part 2).
