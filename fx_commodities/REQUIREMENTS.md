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
| D5 | **Operating profile: low capital, high win rate, 1–2 trades/day.** See §0.5 — this profile is encoded as the default configuration. |

### 0.5 Operating profile — the high-win-rate parameterization (D5)

The user's target is an ~85% win rate at 1–2 trades per day on a small account.
Honest math first: **no directional edge supports 85% wins at 2:1 reward:risk**
(that would imply E[R] ≈ +1.4 ATR/trade — nobody has that). A high win rate is
*bought* by inverting the barrier geometry and being extremely selective:

- **Asymmetric barriers:** `PT_MULT=0.8, SL_MULT=1.6` (target 0.5× the stop).
  Breakeven from §8.0 with typical cost `c≈0.15 ATR`:
  `p* = (1.6 + 0.15)/(0.8 + 1.6) ≈ 0.73`. At the target operating point
  `p̂ = 0.85`: `E[R] = 0.85·0.8 − 0.15·1.6 − 0.15 ≈ +0.29 ATR/trade` — a real,
  positive expectancy with a win rate that high thresholds can plausibly reach,
  because the market only has to *not fall 1.6 ATR* before rising 0.8 ATR.
- **Selectivity produces the 1–2 trades/day, not a scheduler:** `EDGE_MARGIN=0.07`
  (trade only at `p̂ ≥ p* + 0.07 ≈ 0.80`), `CONFORMAL_EPS=0.15` (tighter
  abstention), plus a hard `MAX_TRADES_PER_DAY=2` cap as a backstop. If the
  thresholds only yield one trade some days, that is correct behaviour — do
  not loosen them to hit a quota.
- **Risk asymmetry warning (encode in the runbook):** with SL = 2× TP, the rare
  loss erases ~2.3 wins. At 85% wins the math works; at 75% it breaks even
  before costs. The walk-forward gate must therefore verify the *realized* win
  rate matches the calibrated probabilities (ECE < 0.05 already enforces this).
- **Low capital:** fractional lots make this viable from ~$500–1000 account
  equity: `RISK_PER_TRADE_PCT=1.0` (small accounts can run 1% — the daily cap
  still limits to 2 trades), 0.01-lot minimum sizing rules of §9.3 apply
  unchanged. Expectation management: at 0.29 ATR/trade × 1–2 trades/day the
  account grows steadily, not explosively; leverage is NOT the lever to pull.

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

### 2.4 Crypto universe (added per user decision, 2026-07-08)

| Key | Class | Venue (data + execution) | Notes |
|-----|-------|--------------------------|-------|
| BTCUSD | Crypto | Binance/Bybit spot or USDT-perp | deepest book on earth |
| ETHUSD | Crypto | Binance/Bybit spot or USDT-perp | |
| SOLUSD (optional) | Crypto | Binance/Bybit | enable after BTC/ETH prove out |

**Why crypto strengthens the whole system:** unlike retail FX feeds, crypto
exchanges publish EVERYTHING free over public websockets — full L2 order book,
every trade with aggressor side, real volume, open interest, funding, and
liquidations. Crypto symbols are therefore **T1 + T2 natively**: true CVD,
footprints, DOM heatmaps, iceberg detection all work from day one, and GEX
comes from Deribit's options API. Crypto is where the §7 feature set runs at
full strength — treat it as the reference asset class for validating the
orderflow features that FX can only partially express.

Implementation requirements:
- **`broker/crypto_adapter.py`** implementing the same `BrokerAdapter`
  Protocol (§4.2) — this is exactly what the abstraction was built for.
  Data: native exchange websockets (trade + depth streams). Execution: the
  exchange REST API (or ccxt for venue portability). Runs on Linux — no MT5
  terminal needed for the crypto slice.
- Sessions: 24/7 — no square-off; funding timestamps (every 8 h on most
  perps) replace the swap-avoidance logic; `funding_rate` joins §7.8 features.
- Risk: crypto ATR is proportionally much larger — the ATR-based barrier and
  sizing math (§8.1, §9.3) needs NO changes (that is the point of
  volatility-normalized design), but `MAX_LOTS`-equivalent is quote-quantity
  based, and the correlation guard treats BTC/ETH as one group (ρ ≈ 0.8) and
  as half-weight members of the short-USD group.
- Weekend risk: crypto trades while FX/MCX are closed. Daily-loss accounting
  runs on UTC days uniformly — weekend crypto losses count against the UTC day
  they occur and halt crypto entries for that day, exactly as on weekdays.

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
├── model/
│   ├── labeling.py          # triple-barrier + cost-aware breakeven (§8.1)
│   ├── har.py               # HAR-RV volatility forecaster (§8.2)
│   ├── regime.py            # Gaussian HMM + variance-ratio (§8.3)
│   ├── frac_diff.py         # fractional differentiation (§8.4)
│   ├── dataset.py           # purged CV + uniqueness weights (§8.5)
│   ├── train.py             # primary LightGBM (§8.6)
│   ├── meta.py              # meta-labeling secondary model (§8.7)
│   ├── conformal.py         # split-conformal abstention (§8.8)
│   ├── kalman.py            # trend filter (§8.9)
│   ├── drift.py             # PSI/KS live drift monitor (§8.10)
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
- **Delta dynamics (T1):** `delta_change` (bar-over-bar Δ of aggressor
  delta), `delta_change_z`, and `delta_flip` — sign reversal of delta with
  magnitude > 2σ, the footprint-style "control changed hands" event that
  often precedes the price turn. `buy_pressure_pct` — rolling share of
  aggressive buying (T1 signed volume; T0 falls back to up/down tick counts).

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

### 7.6 Price zones (`features/zones.py`) — T0

Support/resistance zones for the confirmation gate (§9.1b). A zone is a band,
never a line: `Z = [center − w/2, center + w/2]` with `w = ZONE_WIDTH_ATR × ATR`
(default 0.25) frozen at creation. Level sources, all computable at T0:

- **Swing pivots (fractals):** bar *t* is a pivot high if `high_t` is the max of
  `high_{t−n..t+n}` (n = 3). **A pivot at t is only knowable at t+n** — its
  `detect_ts = t + n` and it must not exist in the feature matrix before then
  (no-lookahead test covers this explicitly).
- **Prior-session structure:** previous UTC day's POC, VAH, VAL, high, low.
- **Round numbers:** price grid every `ZONE_ROUND_GRID_POINTS × point`
  (0 disables).
- **DOM walls** join the zone set on T2 symbols.

Zone strength = number of independent sources within w/2 of the center + count
of prior confirmed holds. A violation close (see §9.1b) flips the zone's role
and **re-arms it**: broken support becomes live resistance, so a later
touch-and-reject on the flipped zone is the classic **break-and-retest
continuation entry**, handled by the same confirmation state machine in the
new direction. A second violation (whipsaw both ways) retires the zone.
`ZoneSet.report(price)` renders the supply/demand level table (side, level,
sources, holds, flipped, distance %) for the daily report and dashboard.

### 7.7 Large-participant footprints (`features/footprints.py`) — T1/T2

**Honest framing first:** orderflow does not *predict* when a large firm will
decide to move a market — institutions split orders through TWAP/VWAP/iceberg
algos precisely to stay invisible, and actual manipulation is illegal and rare
relative to legitimate large-order execution. What orderflow CAN do is *detect
the footprints of large executions already in progress*, which lag intent by
seconds-to-minutes but still lead price on our bar horizon. Detectable
signatures, each a feature:

- **Absorption (T1, already §7.1):** heavy aggressive selling into a level
  that refuses to drop — a large passive buyer. The single most reliable
  footprint.
- **Iceberg detection (T2):** a DOM level that repeatedly refills after being
  consumed. Feature: `iceberg_score` = refill count × refill size at the
  most-refilled level within k ticks of price, over the last N bars.
- **Sweep detection (T1):** a single aggressive burst consuming ≥ 3 price
  levels within one bar (`max_levels_swept`, `sweep_volume_z`). Sweeps mark
  urgency — someone paying up for immediacy.
- **Spoof/pull patterns (T2, extends §7.3 pull/stack):** large resting size
  that appears and is pulled without ever trading, repeatedly, on one side —
  `phantom_liquidity_score`. Treat as *distrust that side's depth*, never as a
  directional signal alone.
- **CVD/price divergence at zones (T1):** rising CVD while price is capped at
  resistance = absorption by a seller; the reverse at support. Exported as
  `zone_absorption_divergence` and consumed by the confirmation gate (§9.1b
  amendment below).

### 7.8 Positioning & sentiment layer (`features/positioning.py`)

Slow-moving "who is positioned where" context — weekly/daily cadence, joined
onto bars as forward-filled daily features:

- **COT (CFTC Commitments of Traders):** free weekly CSV from cftc.gov
  (published Friday, data as-of Tuesday — join with a 3-day lag, NEVER as-of
  the data date: lookahead). Map: EURUSD→EC, GBPUSD→BP, USDJPY→JY,
  XAUUSD→GC, XAGUSD→SI, WTI→CL, NATGAS→NG, COPPER→HG, BTC→CME BTC futures.
  Features per symbol: `cot_noncomm_net_pctile` (3-year percentile of
  non-commercial net position), `cot_net_delta_4w`. Extremes (>90th / <10th
  percentile) are contrarian context, not timing signals.
- **Gamma exposure (GEX):** dealer gamma from options OI:
  `GEX = Σ_strikes OI × gamma × contract_multiplier × spot²` (calls +, puts −
  under standard dealer-positioning assumptions). Availability is the
  constraint: ✔ crypto via Deribit's public options API (BTC/ETH — full chain
  with greeks); ✔ Indian indices via the Dhan option-chain API (the indices/
  module already fetches this chain — reuse); ✘ NOT computable for spot FX /
  MCX via MT5 (no options chain). Features where available: `gex_total`,
  `gex_flip_level` (spot where net gamma crosses zero), `dist_to_gex_flip_atr`,
  `dist_to_max_pain_atr`. Positive dealer gamma ⇒ mean-reversion regime
  (dealers fade moves); negative ⇒ amplification regime — this composes
  directly with the §8.3 HMM as a regime prior.
- **Crypto sentiment (crypto only):** perp `funding_rate` and its z-score
  (persistent positive funding = crowded longs), `oi_change_pct`,
  `liquidation_volume_z` from exchange liquidation feeds — cascade detection.

Feature availability matrix (extends §7.0): `positioning` features are daily
tier-D columns, present per symbol per the table above; absent columns are
never imputed.

### 7.9 Micro-window traits (`features/microstructure.py`) — T0/T1

"Microstructure eyes, macro hands": traits that live in 1–30 second windows
inside each bar, aggregated to bar close and consumed by minute-scale
decisions. We never *act* at these timescales (§8.11 not-pursued list); we
*sense* at them — micro information survives aggregation far better than the
ability to trade on it.

Per bar: `burst_ratio` (max 1-second arrival count / mean — a cheap Hawkes
self-excitation proxy until R3 lands), `arrival_accel` (2nd-half/1st-half
trade rate), `tick_run_max` (longest same-direction tick run), `flip_rate`
(micro chop), `sweep_1s_max` (max price traversal inside any 1 s window —
feeds §7.7 sweep detection), `micro_mom_eob` (signed move in the final 5 s —
who won the close), `eob_delta_share` (T1: aggressor delta share of the final
5 s), plus rolling z-scores for burst/sweep (the model consumes surprise, not
level). Ticks stamped exactly at ts_close belong to the next bar — boundary
inclusion is lookahead and is tested.

**No-lookahead rule and test apply to every feature exactly as v1:** value at
bar *t* uses only `ts < t.close`; `tests/test_no_lookahead.py` truncates input
and asserts unchanged earlier values. For weekly/daily joined data (COT, GEX)
the rule extends to *publication* time, not measurement time.

---

## 8. Model layer — the mathematical prediction framework

### 8.0 First principles: what is actually predictable, and the trade inequality

Everything in this section follows from one honest premise: **short-horizon
price *direction* in liquid markets is only weakly predictable** (correctly
calibrated edges of 2–8 percentage points over base rate, decaying over time),
while **volatility is strongly predictable** and **regime is moderately
predictable**. The architecture therefore does not try to be a crystal ball; it
(a) forecasts volatility well, (b) conditions a weak directional edge on regime
and orderflow, (c) bets only when the *net-of-costs* inequality below holds, and
(d) sizes bets by the magnitude of the edge.

**The trade inequality.** A trade with profit target `π·ATR`, stop `σ·ATR`,
round-trip cost `c` (in ATR units: `(spread + commission + slippage) / ATR`),
and true success probability `p` has expectancy

```
E[R] = p·π − (1−p)·σ − c        (in ATR units)
```

which is positive iff

```
p  >  p*  =  (σ + c) / (π + σ)
```

With the defaults `π=2, σ=1` and a typical `c≈0.15`: `p* ≈ 0.383`. **`p*` is
computed live per symbol from current ATR and recorded costs** — it replaces the
fixed `P_ENTRY` constant. The entry condition everywhere in §9 is
`p̂ ≥ p* + EDGE_MARGIN` (default margin 0.05). This single change makes every
threshold cost-aware: when spreads widen, `p*` rises and the system automatically
demands more evidence.

All models below output *calibrated probabilities* so this inequality is
meaningful; an uncalibrated 0.65 is worthless.

### 8.1 Triple-barrier labeling (`model/labeling.py`)

Upper barrier `close + π·ATR_t`, lower `close − σ·ATR_t`, vertical
`MAX_HOLD_BARS` (60). `y ∈ {+1, −1, 0}`, record `t_touch` (needed for purging
and uniqueness weights). ATR here is the **HAR-adjusted ATR** of §8.2 —
barriers scale with *forecast* volatility, not just trailing volatility, so
labels mean the same thing in quiet and violent sessions.

Also store per event: entry cost `c_t` in ATR units (from live spread at t) —
used to compute realized `p*_t` per sample so training-time and trade-time
thresholds agree.

### 8.2 Volatility layer — HAR-RV (`model/har.py`)

The single most predictable market quantity. Realized variance from 1-minute
mid-price log returns within bar aggregation windows:

```
RV_t(day)  = Σ_i r_i²          (r_i = 1-min log returns of day t)
HAR:  RV_{t+1} = β₀ + β_d·RV_t + β_w·(1/5)Σ_{j=0..4} RV_{t−j}
                + β_m·(1/22)Σ_{j=0..21} RV_{t−j} + ε
```

- Fit by OLS on log(RV) (log stabilizes heteroskedasticity), per symbol,
  refit weekly on a 250-day rolling window. Intraday version: same structure on
  30-minute RV buckets with daily/weekly components for hour-ahead forecasts.
- Outputs, exported as features **and** consumed by other layers:
  `rv_forecast_1d`, `rv_forecast_1h`, `vol_surprise = RV_realized/RV_forecast`
  (values ≫ 1 mean the market is doing something the vol model didn't expect —
  a regime-change early warning), `har_atr = √(rv_forecast_1h) × scaling` used
  for barrier placement (§8.1) and sizing denominator (§9.3).
- Acceptance test: HAR out-of-sample R² on log RV must exceed a random-walk
  RV forecast (R² > 0.3 is typical for FX; if not, check the RV computation).

### 8.3 Regime layer — Gaussian HMM + variance-ratio (`model/regime.py`)

Markets alternate between persistent regimes (trend / chop / crisis) and the
optimal behaviour differs per regime. Two complementary detectors:

**(a) 3-state Gaussian HMM** on observation vector `x_t = [r_t, log RV_t]`
(bar log-return, log realized variance):

```
P(x_t | s_t = k) = N(μ_k, Σ_k),   P(s_t = k | s_{t−1} = j) = A_{jk}
```

- Fit by EM (Baum-Welch) on a rolling 2000-bar window, refit weekly.
- **Use filtered probabilities only** `γ_t(k) = P(s_t = k | x_{1..t})` — the
  forward pass. Smoothed (forward-backward) probabilities use future data and
  are lookahead; the no-lookahead test must cover this.
- States are labeled post-hoc each refit by their moments: the state with
  highest |μ_return|/σ = "trend", highest Σ variance = "crisis", remainder =
  "chop". Exported features: `p_trend, p_chop, p_crisis`, `state_persistence`
  (self-transition `A_kk` of the currently most likely state).
- Hard gate: **no new entries while `p_crisis > 0.5`** — crisis bars are where
  backtested edges evaporate and spreads explode.

**(b) Lo–MacKinlay variance ratio** as a lightweight cross-check feature:

```
VR(q) = Var(r_t^{(q)}) / (q · Var(r_t))     (q-bar vs 1-bar returns, q=10)
```

`VR > 1` ⇒ momentum/trending, `VR < 1` ⇒ mean-reverting. Export `vr_10` and its
heteroskedasticity-robust z-statistic `vr_z`. (This formalizes and replaces the
role Hurst played alone in v1; keep Hurst too — they disagree informatively.)

### 8.4 Stationarity with memory — fractional differentiation (`model/frac_diff.py`)

Raw prices are non-stationary (models overfit trends); returns are stationary
but memoryless (the model can't see levels). Fractional differencing keeps both:

```
X̃_t = Σ_{k=0}^{K} w_k · X_{t−k},   w_0 = 1,  w_k = −w_{k−1} · (d − k + 1) / k
```

applied to log price, weights truncated at `|w_k| < 1e−4`. Choose the **smallest
d ∈ {0.1, 0.2, …, 1.0}** whose output passes the ADF test at 95% confidence on
the training window (typically d ≈ 0.3–0.5). Export `ffd_price` and
`ffd_price_z`. Re-select d only at walk-forward retrain boundaries.

### 8.5 Dataset assembly (`model/dataset.py`)

- Per-symbol matrices with tier-gated columns (§7.0) + symbol one-hot for
  pooled training; every §8.2–8.4 output is a column.
- **Purged K-fold with embargo** (`EMBARGO_PCT=0.01`): drop training samples
  whose label window `[t, t_touch]` overlaps any test sample's window, then
  embargo 1% of samples after each test block. Mandatory — overlapping-label
  leakage is the #1 cause of fake backtests.
- **Sample weights (new):** overlapping labels are not i.i.d. Weight each
  sample by its *average uniqueness* — over the bars of its label window,
  `u_i = mean_t ( 1 / (# labels concurrently open at t) )` — multiplied by a
  linear time-decay from 0.5 (oldest) to 1.0 (newest). Passed to LightGBM as
  `sample_weight`. Without this, dense signal clusters dominate training.
- Persist with `dataset_version` = hash(features, tiers, label params, d, range).

### 8.6 Primary model — direction (`model/train.py`)

LightGBM classifier estimating `P(y = +1)` (up-barrier first):
`num_leaves=31, max_depth=6, lr=0.05, n_estimators=400,
min_child_samples=100, subsample=0.8, colsample_bytree=0.8`, sample weights
from §8.5, class weights for the vertical-barrier zeros. Tuned **only** via
purged CV; hyperparameters frozen after the first walk-forward window.
Persist model + `metrics.json` + feature-gain report (alert if any single
feature > 40% gain — leakage smell). The primary's job is *direction candidate
generation*; its raw probability is deliberately not traded directly.

### 8.7 Meta-labeling — bet/no-bet and size (`model/meta.py`)

The López de Prado meta-labeling construction, which consistently improves
precision on weak primaries by separating "which way" from "whether to bet":

1. **Primary events:** every bar where the primary says `|p_up − 0.5| ≥ δ`
   (δ = `PRIMARY_DELTA`, default 0.03) or the DEMA baseline (L0) crosses —
   direction `d_t = sign(p_up − 0.5)` (or the cross direction for L0 events).
2. **Meta-label:** run the triple barrier *in direction `d_t`*; label
   `m_t = 1` if the profit-target barrier is hit first, else 0.
3. **Meta-model:** second LightGBM on the same features **plus**
   `p_up, |p_up−0.5|, d_t, source (L0/L1)` predicting `P(m_t = 1)` — i.e., the
   probability *this specific trade idea* works. Same purged CV, same weights.
4. The traded probability everywhere downstream is the **meta probability**
   `p̂ = P(m=1)`, not the primary's `p_up`.

Why this is mathematically the right decomposition: the primary optimizes
recall over both directions on all bars; the meta optimizes precision on the
exact conditional distribution of *proposed trades* — which is the distribution
the trade inequality of §8.0 actually applies to.

### 8.8 Calibration + conformal abstention (`model/conformal.py`)

- **Isotonic calibration** of the meta probability on a held-out calibration
  fold (as before) — post-calibration reliability diagram saved with the model;
  max calibration error (10-bin ECE) must be < 0.05.
- **Split-conformal gate (new):** on the calibration fold compute
  nonconformity scores `α_i = 1 − p̂_i(true class)`. Let `q̂` be the
  `⌈(n+1)(1−ε)⌉/n` empirical quantile of `α` with `ε = CONFORMAL_EPS`
  (default 0.2). At trade time, the bet is allowed only if
  `1 − p̂ ≤ q̂` — i.e. the (1−ε) conformal prediction set contains *only*
  "success". This is a distribution-free guarantee that, on exchangeable data,
  at most ε of allowed bets are mispredicted at this confidence level — a
  principled abstention rule rather than an arbitrary second threshold.
  (Markets aren't perfectly exchangeable — the guarantee degrades under drift,
  which is exactly what §8.10 monitors.)

### 8.9 Kalman trend filter (`model/kalman.py`)

Local-linear-trend state space on bar closes — state `[level, velocity]`:

```
F = [[1,1],[0,1]],  H = [1,0],  Q = diag(q, q),  R = [r]
predict:  x̂ = F x,  P = F P Fᵀ + Q
update:   K = P Hᵀ (H P Hᵀ + R)⁻¹,  x̂ += K (z − H x̂),  P = (I − K H) P
```

`KALMAN_Q=1e-5, KALMAN_R=1e-2`, per-symbol overridable. Outputs `kf_level,
kf_velocity, kf_velocity_z` — features and the trailing-stop anchor (§9.1).

### 8.10 Live drift monitoring (`model/drift.py`)

Edges decay; the system must know when its training distribution no longer
matches reality:

- **PSI** per feature, live window (last 5 sessions) vs training distribution,
  10 quantile bins: `PSI = Σ (p_i − q_i)·ln(p_i / q_i)`. `PSI > 0.25` on ≥ 3
  features ⇒ WARN + flag in health endpoint; on ≥ 6 ⇒ block new entries, alert
  "retrain required".
- **KS test** on the live distribution of meta probabilities vs the
  calibration fold: p-value < 0.01 ⇒ calibration is stale ⇒ same escalation.
- Rolling live hit-rate vs conformal expectation: if realized error over the
  last 50 allowed bets exceeds `ε + 0.10`, block entries (the conformal
  guarantee is broken — distribution has shifted).

---

### 8.11 Advanced detection & GPU research track (Phase 5+, prioritized)

What is worth adding beyond §7–§8, in priority order, with the promotion rule
that governs all of it: **a candidate model/feature ships only if it beats the
incumbent out-of-sample under the same purged walk-forward + PSR protocol.**
Trial counts feed the §10.3 DSR accounting — GPU compute makes it easy to run
thousands of experiments, which makes overfitting-by-search the #1 risk.

**R1. Economic-calendar blackout (trivial, immediate).** No entries within
±N minutes of scheduled high-impact events (NFP, CPI, FOMC, ECB/BoJ; crypto:
FOMC + large token unlocks). Quant desks gate releases as a matter of course;
for a high-win-rate profile this is pure win-rate defence. Data: any free
economic-calendar API, cached daily. Config: `NEWS_BLACKOUT_MIN=30`.

**R2. Flow toxicity — VPIN (Easley, López de Prado, O'Hara).** Bucket volume
into equal-volume bins, estimate per-bin signed imbalance, then
`VPIN = Σ|V_buy − V_sell| / (n·V_bucket)` over a rolling window. High VPIN =
informed flow is picking off liquidity providers → spreads about to widen,
regime fragile. Needs real volume: crypto/T1 only. Join the §9.2 regime guard.

**R3. Hawkes-process endogeneity.** Order/trade arrivals as a self-exciting
point process `λ(t) = μ + Σ_{t_i<t} α·e^{−β(t−t_i)}`; the branching ratio
`n = α/β` measures reflexivity — how much activity is triggered by other
activity rather than news (Filimonov & Sornette). `n → 1` marks fragile,
cascade-prone markets (flash-crash precursor); also the cleanest available
*mathematical* answer to "can we see large-player cascades coming": we can
see when the market is primed for one. Fit by MLE per session, T1+.

**R4. Cross-asset lead-lag.** DXY→gold/EURUSD, ES futures→BTC (US hours),
BTC→ETH→alts. Estimate with the Hayashi–Yoshida covariance estimator (handles
asynchronous ticks) at lags 0–60 s; trade the laggard on the leader's
confirmed move only where lag correlation is stable out-of-sample. This is a
genuine, documented micro-inefficiency in crypto.

**R5. Mean-reversion pair module.** ETH/BTC spread first: fit an
Ornstein–Uhlenbeck process `dX = θ(μ−X)dt + σdW`; trade z-score extremes only
when the estimated half-life `ln2/θ` is short and stable. Johansen test for
cointegration re-checked at every walk-forward boundary — pairs die.

**R6. Deep learning on the order book (the GPU track).** The one DL
architecture family with replicated results in this domain: DeepLOB-style
CNNs/CNN-LSTMs on stacked L2 snapshots (Zhang, Zohren, Roberts 2019) for
short-horizon direction. Recipe: inputs = last 100 book states × 20 levels
(price, size both sides), labels = §8.1 triple-barrier, training = purged
walk-forward identical to LightGBM. Crypto only (needs full L2 history — our
recorder provides it). Runs comfortably on one 24 GB consumer GPU (RTX
4090-class): days of training data ≈ minutes/epoch. Serving: at 1-min bars,
live inference latency is a non-issue — run the torch model directly.
Promotion gate: must beat the calibrated LightGBM meta-stack OOS by ≥ 2 pts of
precision at equal recall, else it stays a research artifact.

**R7. Alpha mining with strict accounting (optional).** Genetic
programming / formulaic alpha search (à la WorldQuant's alpha101) over the
feature set. Permitted ONLY with automatic trial logging into
`reports/trials.json` — mined alphas are DSR-discounted by construction.

**R8. Reinforcement learning — execution only.** The honest read of the
literature and industry practice: RL is productive for *order execution*
(when to cross the spread, order sizing/segmentation — cf. optimal execution
frameworks) and unstable for signal generation. If we ever do RL, it
optimizes fill quality against recorded books, never entry/exit decisions.

**R10. Cross-venue mispricing in immature venues (research, unscheduled).**
The one "fast trading" family genuinely open to solo operators — as
demonstrated publicly by small stat-arb outfits — is NOT racing HFT firms on
exchanges; it is arbitraging *young, structurally inefficient venues* against
mature ones: prediction-market contracts vs their underlying reference prices
(e.g. Polymarket outcome odds vs live equity/index prices), and crypto
cross-exchange basis/latency spreads. The edge source is venue immaturity and
slow participants, so millisecond engineering helps but nanoseconds are not
required (NTP-synced seconds-scale is competitive). Honest constraints before
this ever becomes a module: venue/counterparty risk, KYC/geo eligibility,
fee-adjusted edges are thin, inventory management across venues is the real
problem, and the edges decay as venues mature. Fits our stack naturally
(BrokerAdapter per venue, R4 lead-lag machinery, same gates); prioritize only
after core modules trade.

**Explicitly not pursued (out of a single-operator's reach, stated so the
roadmap stays honest):** colocation/FPGA latency arbitrage, queue-position
games, maker-rebate harvesting, index-rebalance front-running at size. These
are the profitable things HFT firms do that no retail setup can replicate —
our edge must come from horizon (minutes, not microseconds), discipline
(gates), and data breadth (crypto L2 + positioning), not speed.

**Local GPU training stack (reference):** PyTorch + CUDA on Linux; polars +
DuckDB over the Parquet lake (columnar scans of tick data); Optuna for HPO
inside purged CV; MLflow (local) for experiment tracking; every experiment id
increments the DSR trial counter. A single consumer GPU is sufficient for
everything in R6; multi-GPU adds nothing until the dataset spans years of
multi-venue L2.

## 9. Ensemble, risk, and signal output

### 9.1 Decision logic (`model/ensemble.py`)

The full pipeline of §8, composed. Per bar:

```
# 1. Candidate generation (primary + baseline)
L1_event = |p_up − 0.5| ≥ PRIMARY_DELTA          → direction d = sign(p_up − 0.5)
L0_event = DEMA10×DEMA20 cross with DEMA95 trend  → direction d = cross direction
if no event: HOLD

# 2. Regime & environment gates (any failure → HOLD)
p_crisis ≤ 0.5                                    (§8.3 HMM)
regime guard of §9.2 passes
orderflow gate agrees with d:
    T1: sign(ofi_zscore) == d and sign(cvd_slope_n) == d
    T0: sign(pdelta_slope) == d and (updown_ratio − 1) has sign d

# 3. Meta-decision (the bet/no-bet mathematics)
p̂       = calibrated meta probability for this event (§8.7–8.8)
p*      = (σ + c_live) / (π + σ)                  cost-aware breakeven (§8.0),
                                                   c_live from current spread+ATR
trade  iff  p̂ ≥ p* + EDGE_MARGIN  AND  conformal gate allows (1 − p̂ ≤ q̂)

signal = BUY if d > 0 else SELL;  confidence = p̂
# L0-only events (no trained model yet) use p̂ = historical L0 hit rate from the
# backtest report, and trade at reduced fixed size (§9.3) — the system still
# emits buy/sell signals before/without ML, as required.
```

Exits: opposite signal, native SL/TP touch (server-side), trailing stop —
`modify_sltp(ticket, sl=kf_level − TRAIL_MULT×ATR)` for longs once ≥ 1R in
profit — and the swap-avoidance square-off (§2.3) when `ALLOW_OVERNIGHT=false`.

### 9.1b Zone-confirmation entry gate (`confirmation.py`)

**Rule (user requirement):** never enter on a live touch of a level. Require
proof the zone holds — closed candles only — and enter on the bar *after*
confirmation completes, accepting a worse price for a higher win probability.

**State machine, per (zone, direction), driven only by CLOSED bars:**

```
IDLE ──(bar range intersects Z)──▶ TOUCHED
TOUCHED / CONFIRMING:
  a confirming close (support case): close > z_hi          (closed back above)
                                     AND low ≥ z_lo − λ·w  (violation tolerance,
                                                            λ = CONFIRM_VIOLATE_FRAC)
  N_CONFIRM consecutive confirming closes (default 2), of which ≥ 1 must show a
  defence wick: (close − low)/(high − low) ≥ MIN_WICK_RATIO (default 0.5)
      ──▶ CONFIRMED: entry window opens for ENTRY_WINDOW_BARS bars (default 3);
          fill at the NEXT bar's open. SL anchored beyond the zone:
          sl = z_lo − λ·w − buffer;  never intrabar entries.
  any close beyond z_lo − λ·w  ──▶ FAILED: zone marked broken, flips role.
Resistance case is symmetric.
```

**The mathematics of trading confirmation.** Entering after confirmation is
worse by `δ` (ATR units) — the drift from touch price to confirmed-entry open.
With target `π` and stop `σ` both anchored to the zone, the delayed entry
shrinks the reward to `π − δ` and widens the risk to `σ + δ`:

```
EV_touch     = p₀·π − (1−p₀)·σ − c
EV_confirmed = p₁·(π − δ) − (1−p₁)·(σ + δ) − c
EV_confirmed > EV_touch   ⟺   Δp = p₁ − p₀  >  δ / (π + σ)
```

(the p₁·δ terms cancel exactly). With the D5 barriers (π=0.8, σ=1.6) and a
typical 2-bar confirmation drift δ ≈ 0.3 ATR, confirmation must add
**≥ 12.5 percentage points of win probability** to pay for itself.

**This is measured, not assumed:** the backtest engine records, for every zone
touch, BOTH the hypothetical at-touch outcome and the confirmed-entry outcome
(when confirmation completed), and the report prints empirical `p₀, p₁, Δp, δ`
and `confirmation_value = Δp·(π+σ) − δ` per symbol. If confirmation_value ≤ 0
on a symbol, the gate is disabled there — the math decides, not preference.

**Orderflow-coupled confirmation (T1 symbols, config `CONFIRM_REQUIRE_CVD`):**
where true aggressor data exists, a confirming close additionally requires CVD
agreement over the confirmation bars — for a support hold, bar-delta sum ≥ 0
(sellers hit the zone and were absorbed, §7.7). Price closing back above a
zone on *falling* CVD is a weaker hold; requiring the CVD term measurably
raises p₁ on T1 symbols or the config stays off — same evidence standard as
the gate itself (the confirmation_study reports both variants).

**Global closed-bar rule (applies to every entry path, §10.1 amended):**
signals are evaluated ONLY on completed bars; fills occur at the next bar's
open plus slippage. The engine and the live trader share this rule so backtest
and live behaviour cannot diverge on timing.

### 9.2 Regime guard
Skip entries when `ADX < 15 and |hurst − 0.5| < 0.05`, when live spread over the
last 30 min > `MAX_LIVE_SPREAD_PCT`, when `spread_z > 3` (news blowout), and
within `SKIP_MIN_AFTER_OPEN`/Friday-close windows (§2.3).

### 9.3 Risk & sizing (`execution/risk.py`)

- `capital = CAPITAL_TOTAL × ALLOC_FX_COMMODITIES`; account currency conversion
  via `account_info().currency` (assume USD account by default; if INR-funded
  via broker, conversion handled by broker — use `account_info().balance`
  directly and treat `CAPITAL_TOTAL` as account-currency).
- **Kelly sizing from the calibrated edge (replaces ad-hoc scaling).** For a
  bet with odds `b = π/σ` (reward:risk ratio) and calibrated success
  probability `p̂`, the growth-optimal fraction is

  ```
  f* = (p̂·(b + 1) − 1) / b          (≤ 0 ⇒ no trade — consistent with §8.0)
  f  = KELLY_FRACTION × f*           (fractional Kelly, default 0.5)
  risk_frac = min(f, RISK_PER_TRADE_PCT)   # hard cap, default 0.5%
  risk_amt  = capital × risk_frac
  ```

  Fractional Kelly because p̂ is estimated with error: half-Kelly gives ~75% of
  optimal growth at half the drawdown variance, and is robust to calibration
  error of a few points.
- Stop distance in account currency per lot:
  `stop_value = (SL_MULT × har_atr / tick_size) × tick_value` — note **HAR-ATR**
  (§8.2), so size shrinks automatically when forecast volatility rises.
- `lots = round_step(risk_amt / stop_value, volume_step)`, clipped to
  `[volume_min, min(volume_max, MAX_LOTS=1.0)]`; if below `volume_min` → no trade.
- Baseline-path (L0-only) trades use fixed `risk_frac = 0.25 × RISK_PER_TRADE_PCT`.
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
broker_symbol`; add `lots: float`, `tier: str` ("T0"/"T1"/"T2"). The
`features_digest` must include the decision mathematics for auditability:
`{p_hat, p_star, edge (= p_hat − p_star), kelly_f, conformal_q, p_crisis,
vol_surprise, ofi_z_or_pdelta, adx, vr_10, spread_pct}` — every live trade is
explainable after the fact from its journal row alone. Every non-HOLD signal
optionally POSTs to `SIGNAL_WEBHOOK_URL`.

---

## 10. Backtesting

### 10.1 Engine
Event-driven over bars; **live and backtest call the identical
`ensemble.decide()` / `risk.size()`**. Signals evaluate on CLOSED bars only;
entry fills at the **next bar's open** + slippage (§9.1b global rule). SL/TP
intra-bar conservative rule (both touched in one bar ⇒ SL first). Metrics: net
PnL, profit factor, hit rate, avg win/loss, max DD, Sharpe, turnover, cost
total — plus the confirmation study of §9.1b (`p₀, p₁, Δp, δ,
confirmation_value` per symbol).

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

### 10.3 Walk-forward + anti-overfitting statistics
Train 60 trading days → test 10 → step 10; ≥ 6 windows; hyperparameters frozen
after first-window purged-CV tune; recalibrate (isotonic + conformal q̂) each
step. Report JSON + MD + equity PNG in `reports/`.

**Deployment gate in code (all three must hold):**
1. Aggregate `profit_factor_after_costs ≥ 1.1`
2. `max_dd ≤ 10%` of module capital
3. **Probabilistic Sharpe Ratio ≥ 0.95** on the concatenated OOS trade returns:

   ```
   PSR(SR*) = Φ( (SR − SR*)·√(n − 1) / √(1 − γ₃·SR + ((γ₄ − 1)/4)·SR²) )
   ```

   with benchmark `SR* = 0`, `n` = number of OOS trades, `γ₃/γ₄` = skew/kurtosis
   of trade returns. This asks: *given how many trades we have and how non-normal
   they are, what is the probability the true Sharpe is above zero?* A PF of 1.3
   on 40 fat-tailed trades can easily fail this gate — that is the point.

**Trial accounting (mandatory):** every configuration evaluated against OOS data
(feature sets, label params, hyperparameter tunes) increments a counter persisted
in `reports/trials.json`. The report computes the **Deflated Sharpe Ratio** —
PSR with `SR*` set to the expected maximum Sharpe among `N` independent trials:

```
SR* = √Var(SR_trials) · ( (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ),  γ ≈ 0.5772
```

DSR is reported (not gated in v1) so the operator sees how much of the observed
performance is explainable by selection over N tries. If N grows past ~50, treat
a DSR < 0.5 as a de-facto failure regardless of the formal gates.

Per-symbol breakdown required; any symbol with PF < 1.0 is dropped from
`reports/live_universe.json`.

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
BAR_INTERVAL_S=60  PT_MULT=2.0  SL_MULT=1.0  MAX_HOLD_BARS=60
EDGE_MARGIN=0.05  PRIMARY_DELTA=0.03  CONFORMAL_EPS=0.2
IMBALANCE_RATIO=3.0  TRAIL_MULT=1.5  KALMAN_Q=1e-5  KALMAN_R=1e-2  EMBARGO_PCT=0.01
HMM_STATES=3  HMM_WINDOW_BARS=2000  HAR_REFIT_DAYS=7  FFD_ADF_ALPHA=0.05
DRIFT_PSI_WARN=0.25  DRIFT_PSI_FEATURES_BLOCK=6
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
Deliverables: all of `model/` (§8.0–8.10: HAR, HMM, FFD, primary, meta,
conformal, drift monitor), walk-forward runner with PSR/DSR statistics.
**Gate:** OOS PF after costs ≥ 1.1, max DD ≤ 10%, **PSR ≥ 0.95** (§10.3);
HAR beats random-walk RV forecast OOS; meta-model precision on allowed bets
exceeds the primary's precision (that is meta-labeling's whole job — if it
doesn't, investigate before proceeding); calibration ECE < 0.05. Per-symbol
live universe written. Failed symbols dropped; if all fail, baseline-only mode
at 0.25 size — never lower the gates.

### Phase 4 — Paper, then live
`paper.py` uses the real adapter on a **demo account** (MT5 demo = identical
API) — this is true paper trading with real broker fills. ≥ 10 sessions, PF
within 30% of backtest, zero reconciliation mismatches. Then live with
`MAX_LOTS=0.01` hard-clamped for the first two weeks regardless of sizing.

### Testing requirements
pytest; ≥ 90% line coverage on `features/`, `model/labeling.py`, `model/har.py`,
`model/regime.py`, `model/conformal.py`, `execution/risk.py`; no-lookahead test
(must cover HMM filtered-vs-smoothed probabilities explicitly); math unit tests
against hand-computed fixtures for: breakeven `p*`, Kelly `f*`, FFD weights,
variance ratio, PSI, PSR; conformal coverage test on synthetic exchangeable
data (realized error ≤ ε within tolerance); risk tests (halt persistence across
restart, correlation guard blocks 3rd USD position, volume_step rounding never
exceeds caps, Kelly f* ≤ 0 produces no trade); adapter retcode-handling tests
with mocked `order_send`.

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

## 15b. Relationship to NautilusTrader (and similar platforms)

Recurring question, answered here for the record. **NautilusTrader is not a
competitor to our model — it is a competitor to our *engine*.** It is an
open-source, Rust-core, event-driven trading platform: nanosecond-precision
backtesting, live/backtest code parity, and production adapters for many
venues (Binance, Bybit, Interactive Brokers, …). It ships **zero alpha**: no
features, no prediction, no edge — you bring the strategy, it runs it.

| Dimension | This repo's engine | NautilusTrader |
|---|---|---|
| Prediction/alpha | §7–§9 (the actual edge) | none — bring your own |
| Backtest granularity | bar-level + recorded-spread costs | tick/order-book level |
| Live/backtest parity | shared decide()/size(), closed-bar rule | first-class, battle-tested |
| Venue adapters | MT5 (ours), crypto (planned) | many, maintained by community |
| Complexity | ~600 lines, fully understood | large framework, steep curve |

Position: our *edge* lives in features + model + gates, which port anywhere.
The pragmatic path is to keep our lean engine through Phase 3 validation, and
**adopt NautilusTrader as the execution/backtest backbone for the crypto slice
in Phase 4+ if tick-level fidelity starts to matter** — its Binance/Bybit
adapters would replace `crypto_adapter.py` execution while our `BrokerAdapter`
seam keeps strategy code unchanged. Migrating before the model is validated
would be infrastructure procrastination.

## 16. Explicitly out of scope (v1)

- Options on FX/metals; only spot/CFD market orders + native SL/TP.
- Additional broker adapters (OANDA/cTrader/IBKR) — the Protocol in §4.2 is
  designed for them, but only `mt5_adapter.py` ships in v1.
- Tick-level ML backtesting (bar-level with recorded-spread slippage is v1).
- Any UI beyond the health endpoint and MD reports.
- Modifying `indices/` (it stays on Dhan; separate roadmap in PLAN.md Part 2).
