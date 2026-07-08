# fx_commodities — Build Requirements Specification

**Version 1.0 — 2026-07-08**
**Status: APPROVED FOR BUILD**

This document is a complete, self-contained implementation brief. An engineer (or
AI model) with no prior context on this repository must be able to build the entire
module from this document alone. Read it fully before writing any code.

---

## 0. Context and hard constraints

### 0.1 What this module is

A prediction and trading engine for **currency futures, bullion, and commodities**
on Indian exchanges via the [Dhan](https://dhan.co) broker API v2. It produces
`BUY / SELL / HOLD` signals with confidence, entry, stop-loss, and target, using
orderflow analytics, liquidity heatmaps, volume profile, and a machine-learning
ensemble — then executes them with strict risk controls.

### 0.2 Locked-in decisions (do not revisit)

| # | Decision | Value |
|---|----------|-------|
| D1 | Currency pair selection | **Data-driven.** Record both NSE cross pairs (EURUSD, GBPUSD, USDJPY) and INR pairs (USDINR, EURINR, GBPINR, JPYINR) during Phase 1. After ≥ 5 trading days, compute median relative spread per contract; drop any contract whose median spread > `MAX_MEDIAN_SPREAD_PCT` (default 0.05%). Remaining contracts form the tradeable universe. |
| D2 | MCX contract size | **Mini contracts only**: GOLDM (100 g), SILVERM (5 kg), CRUDEOILM (10 bbl), NATURALGAS mini variant if listed (else standard NATURALGAS), COPPER standard (no mini exists). |
| D3 | Capital allocation | Configurable split between this module and `indices/`. Default `ALLOC_FX_COMMODITIES=0.50` of `CAPITAL_TOTAL`. All risk limits in this module derive from its allocated slice only. |

### 0.3 Hard constraints for the implementer

1. **Do NOT modify anything under `indices/`.** It is a frozen, working system.
2. All new code lives under `fx_commodities/` and `common/` (new shared package).
3. Python ≥ 3.11. Type hints everywhere. No TypeScript-style comments; docstrings
   state *what a constraint is*, not what the next line does.
4. Every external call (HTTP, websocket) goes through the retry/circuit-breaker
   wrapper in `common/` (spec §12).
5. Nothing trades live until the Phase gates in §14 pass. The live executor must
   refuse to start if the walk-forward report file is absent or shows
   `profit_factor_after_costs < 1.1`.
6. No secrets in code or committed files. All credentials via `.env`.
7. **Critical platform limitation** discovered during research: Dhan's 20-level
   depth websocket supports **NSE segments only** (equity + derivatives). MCX is
   NOT supported. Therefore: NSE currency contracts get 20-level heatmaps; MCX
   bullion/commodities get 5-level heatmaps built from the regular feed's Full
   packet. The heatmap module must handle both depths transparently (§7.2).

---

## 1. Dhan API reference (verified against docs v2)

Everything the module needs from Dhan, with exact formats. Base REST URL:
`https://api.dhan.co/v2`. Auth headers on every REST call:

```
access-token: <ACCESS_TOKEN>       (JWT, expires every 24 h)
client-id: <CLIENT_ID>
Content-Type: application/json
```

### 1.1 Live Market Feed websocket (ticks + 5-level depth)

```
wss://api-feed.dhan.co?version=2&token=<ACCESS_TOKEN>&clientId=<CLIENT_ID>&authType=2
```

- Max **5 websocket connections** per user; max 5000 instruments/connection;
  max **100 instruments per subscription message**.
- Requests are JSON; responses are **binary, little-endian**.
- Server pings every 10 s; connection dies after 40 s without pong (standard
  websocket libraries auto-pong — verify yours does).
- Disconnection code 805 = more than 5 sockets; oldest killed.

**Subscribe** (RequestCode 15 = ticker, 17 = quote, 21 = full — use **21 Full**
for tradeable universe, 15 Ticker for anything watch-only):

```json
{
  "RequestCode": 21,
  "InstrumentCount": 2,
  "InstrumentList": [
    {"ExchangeSegment": "MCX_COMM", "SecurityId": "428301"},
    {"ExchangeSegment": "NSE_CURRENCY", "SecurityId": "12345"}
  ]
}
```

**Binary response header (8 bytes, all packet types):**

| Offset | Type | Field |
|--------|------|-------|
| 0 | uint8 | Feed response code |
| 1–2 | int16 | Payload length |
| 3 | uint8 | Exchange segment (numeric) |
| 4–7 | int32 | Security ID |

**Packet types to parse:**

*Ticker (code 2):* header + float32 LTP (bytes 8–11) + int32 last-trade-time
epoch (bytes 12–15).

*Quote (code 4):* header + LTP f32, last-traded-qty i16, LTT i32, ATP f32,
volume i32, total sell qty i32, total buy qty i32, day open/close/high/low f32
(offsets per Dhan docs; total 50 bytes).

*OI (code 5):* header + int32 open interest.

*Prev close (code 6):* header + f32 prev close + i32 prev OI.

*Full (code 8, 162 bytes):* everything in Quote + OI + highest/lowest OI day +
**5 × 20-byte depth levels** at bytes 63–162. Each 20-byte level:

| Offset in level | Type | Field |
|------|------|-------|
| 0–3 | int32 | Bid quantity |
| 4–7 | int32 | Ask quantity |
| 8–9 | int16 | Bid order count |
| 10–11 | int16 | Ask order count |
| 12–15 | float32 | Bid price |
| 16–19 | float32 | Ask price |

*Disconnect (code 50):* header + int16 disconnect reason.

**Unsubscribe / disconnect:** send `{"RequestCode": 12}`.

### 1.2 20-level depth websocket (NSE only — currency contracts)

```
wss://depth-api-feed.dhan.co/twentydepth?token=<ACCESS_TOKEN>&clientId=<CLIENT_ID>&authType=2
```

- Max **50 instruments per connection**. Subscribe with RequestCode **23**,
  same InstrumentList shape as §1.1.
- **Response header is 12 bytes** (differs from §1.1!):

| Offset | Type | Field |
|--------|------|-------|
| 0–1 | int16 | Message length |
| 2 | uint8 | Feed code: **41 = bid side, 51 = ask side** |
| 3 | uint8 | Exchange segment |
| 4–7 | int32 | Security ID |
| 8–11 | uint32 | Message sequence number |

- Payload: **20 levels × 16 bytes** = 320 bytes. Per level: float64 price
  (0–7), uint32 quantity (8–11), uint32 order count (12–15).
- Bid and ask arrive as **separate messages** (codes 41/51) — the book builder
  must pair them by (securityId, sequence proximity).
- Track sequence gaps: if `seq` jumps by > 1, mark the book snapshot as
  `degraded=True` until the next clean pair.

### 1.3 Historical candles (REST)

- `POST /charts/intraday` — 1/5/15/25/60-minute OHLCV+OI, up to 5 years back,
  **max 90 days per request** (must paginate).
- `POST /charts/historical` — daily OHLCV+OI back to inception.

Request body (both):

```json
{
  "securityId": "428301",
  "exchangeSegment": "MCX_COMM",
  "instrument": "FUTCOM",
  "interval": "1",
  "oi": true,
  "fromDate": "2026-06-01",
  "toDate": "2026-07-08"
}
```

Response arrays: `open[], high[], low[], close[], volume[], timestamp[] (epoch),
open_interest[]`.

### 1.4 Orders (REST)

`POST /orders` with body fields: `dhanClientId, correlationId (≤30 chars),
transactionType (BUY|SELL), exchangeSegment, productType, orderType
(LIMIT|MARKET|STOP_LOSS|STOP_LOSS_MARKET), validity (DAY|IOC), securityId,
quantity, price, triggerPrice`. Response: `{orderId, orderStatus}`.
Order status polling: `GET /orders/{order-id}`. Statuses: TRANSIT, PENDING,
REJECTED, CANCELLED, PART_TRADED, TRADED, EXPIRED.

- **`correlationId` is mandatory on every order** this module places. Format:
  `fxc-{yyyymmdd}-{seq:04d}` — it is the idempotency key.
- Static IP whitelisting is mandatory (web.dhan.co → Profile). 7-day lock after
  change. The runbook (§15) must tell the operator this.

### 1.5 Token renewal

Access tokens expire every 24 h. `POST /RenewToken` works only for *active*
tokens. `common/auth.py` must renew on a schedule (§12.1).

### 1.6 Instrument master

CSV: `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`.
Columns of interest: `SEM_EXCH_EXCH_ID, SEM_SEGMENT, SEM_SMST_SECURITY_ID,
SEM_TRADING_SYMBOL, SM_SYMBOL_NAME, SEM_EXPIRY_DATE, SEM_LOT_UNITS,
SEM_TICK_SIZE, SEM_INSTRUMENT_NAME`. Download at startup, cache for the day.

### 1.7 Exchange segment identifiers

| String (REST/JSON) | Numeric (binary feed) |
|---|---|
| NSE_EQ | 1 |
| NSE_FNO | 2 |
| NSE_CURRENCY | 3 |
| BSE_EQ | 4 |
| MCX_COMM | 5 |
| BSE_CURRENCY | 7 |
| BSE_FNO | 8 |

---

## 2. Instrument universe

### 2.1 Contracts

| Key | Exchange segment | Instrument | Contract | Session (IST) |
|-----|------------------|-----------|----------|----------------|
| EURUSD | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–19:30 |
| GBPUSD | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–19:30 |
| USDJPY | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–19:30 |
| USDINR | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–17:00 |
| EURINR | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–17:00 |
| GBPINR | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–17:00 |
| JPYINR | NSE_CURRENCY | FUTCUR | Near-month future | 09:00–17:00 |
| GOLDM | MCX_COMM | FUTCOM | Mini, 100 g | 09:00–23:30 |
| SILVERM | MCX_COMM | FUTCOM | Mini, 5 kg | 09:00–23:30 |
| CRUDEOILM | MCX_COMM | FUTCOM | Mini, 10 bbl | 09:00–23:30 |
| NATURALGAS | MCX_COMM | FUTCOM | Mini variant if listed in scrip master, else standard | 09:00–23:30 |
| COPPER | MCX_COMM | FUTCOM | Standard (no mini exists) | 09:00–23:30 |

Notes:
- MCX evening sessions extend to 23:55 during US daylight-saving winter. Read
  session end from config, not hardcoded.
- Verify session times for cross pairs from the exchange; if scrip master or a
  probe tick shows different hours, config wins.

### 2.2 `instruments.py` requirements

- `@dataclass(frozen=True) class Contract`: `key, exchange_segment (str),
  segment_code (int), instrument_type, security_id, trading_symbol, expiry (date),
  lot_size (int), tick_size (float), session_open (time), session_close (time),
  is_mini (bool), currency_of_quote (str)`.
- `resolve_universe(scrip_master: pd.DataFrame, today: date) -> list[Contract]`:
  for each key in §2.1, select the **near-month** future: minimum
  `SEM_EXPIRY_DATE ≥ today + ROLLOVER_BUFFER_DAYS` (default 2). This implements
  rollover: 2 days before expiry the resolver naturally advances to next month.
- Rollover event: when the resolved `security_id` for a key changes vs the journal's
  open position, the trader must **flatten the old contract** and only then trade
  the new one. Never hold through expiry.
- Unit tests: resolver picks correct contract at month boundaries; mini variants
  chosen when both mini and standard match a key.

---

## 3. Repository layout to create

```
common/
├── __init__.py
├── auth.py            # token store + RenewToken scheduler
├── http.py            # REST wrapper: retry, backoff, circuit breaker, rate budget
├── journal.py         # SQLite journal (shared schema, §11)
└── alerts.py          # operator alerting (log CRITICAL + optional telegram/webhook stub)

fx_commodities/
├── __init__.py
├── config.py          # env-driven config object (§13)
├── instruments.py     # §2
├── data/
│   ├── __init__.py
│   ├── feed.py        # market-feed websocket client (§4.1)
│   ├── depth20.py     # 20-level depth client, NSE only (§4.2)
│   ├── recorder.py    # tick + depth → Parquet (§4.3)
│   ├── store.py       # query layer + historical backfill (§4.4)
│   └── schemas.py     # PyArrow schemas (§4.5)
├── features/
│   ├── __init__.py
│   ├── bars.py        # tick → time/volume bars (§6)
│   ├── orderflow.py   # aggressor, CVD, footprint, OFI (§7.1)
│   ├── heatmap.py     # liquidity heatmap 5-and-20-level (§7.2)
│   ├── volume_profile.py  # POC/VAH/VAL, VWAP (§7.3)
│   └── indicators.py  # DEMA, ATR, ADX, Hurst, Yang-Zhang (§7.4)
├── model/
│   ├── __init__.py
│   ├── labeling.py    # triple-barrier labels (§8.1)
│   ├── dataset.py     # feature matrix assembly, purged splits (§8.2)
│   ├── train.py       # LightGBM + isotonic calibration (§8.3)
│   ├── kalman.py      # trend-state filter (§8.4)
│   └── ensemble.py    # layer composition → signal (§9)
├── backtest/
│   ├── __init__.py
│   ├── engine.py      # event-driven backtester (§10)
│   ├── costs.py       # Indian F&O cost model (§10.2)
│   └── walkforward.py # rolling train/test + report (§10.3)
├── execution/
│   ├── __init__.py
│   ├── risk.py        # sizing, limits, correlation guard (§9.3)
│   └── executor.py    # order lifecycle + reconciliation (§11)
├── signals.py         # Signal dataclass + emitters (§9.4)
├── trader.py          # live loop entrypoint
├── paper.py           # paper-trading harness (§14 Phase 4)
├── requirements.txt
├── .env.example
└── tests/             # pytest; every module gets unit tests
```

Dependencies (`fx_commodities/requirements.txt`): `websockets, pandas, numpy,
pyarrow, lightgbm, scikit-learn, pydantic, python-dotenv, pytz, requests,
fastapi, uvicorn` (fastapi/uvicorn only for the health endpoint).

---

## 4. Data layer specification

### 4.1 `data/feed.py`

- `class MarketFeed`: async client on `websockets`. Constructor takes
  `list[Contract]`, mode (`FULL` default), and an async callback
  `on_packet(pkt: ParsedPacket)`.
- Parses all packet types in §1.1 via `struct.unpack_from('<...')`. Include a
  pure function `parse_packet(buf: bytes) -> ParsedPacket` so parsing is unit-
  testable against fixture bytes (write fixtures by hand from the tables in §1.1).
- Chunk subscriptions at 100 instruments/message.
- Reconnect: exponential backoff 1 s → 60 s cap, jittered; resubscribe all on
  reconnect; log a `feed_gap` event with wall-clock gap duration into the journal
  (backtests must know where data holes are).
- Maintain per-instrument `last_packet_at`; a watchdog task logs WARNING if a
  tradeable instrument is silent > 60 s during its session.

### 4.2 `data/depth20.py`

- Same skeleton as feed.py, URL/header/payload per §1.2.
- Builds `OrderBook20` per instrument: apply bid message (code 41) and ask
  message (code 51); a snapshot is `clean` only when both sides have the same
  or adjacent sequence numbers; otherwise `degraded`.
- **Only subscribe NSE_CURRENCY contracts** (cap 50 — universe fits easily).
- Emits `DepthSnapshot(ts, security_id, bids: list[Level], asks: list[Level],
  clean: bool)` at most every `DEPTH_SNAPSHOT_INTERVAL_MS` (default 500 ms —
  throttle, do not persist every update).

### 4.3 `data/recorder.py`

Standalone process: `python -m fx_commodities.data.recorder`.

- Subscribes Full feed for the whole universe + depth20 for NSE currency.
- Buffers rows in memory; flushes to Parquet every `FLUSH_INTERVAL_S` (default
  60 s) — append via new row-group files, one file per flush:
  `data_root/{table}/symbol={key}/date={YYYY-MM-DD}/part-{HHMMSS}.parquet`.
- Tables: `ticks`, `quotes`, `depth5`, `depth20`, `oi` (schemas §4.5).
- On SIGTERM: flush and exit cleanly.
- Must survive the full MCX session unattended (09:00–23:55). Log heartbeat
  with row counts every 5 min.
- Crash-safety: a partially written file is discarded on next startup if its
  footer is invalid (validate on boot, quarantine to `_corrupt/`).

### 4.4 `data/store.py`

- `read_ticks(key, date_from, date_to) -> pd.DataFrame` etc., one reader per table.
- `backfill_ohlcv(contract, interval, years) -> pd.DataFrame`: paginate
  `POST /charts/intraday` in ≤ 90-day windows; de-duplicate on timestamp; persist
  to `ohlcv` Parquet table so backfills are incremental.
- All readers return timezone-aware IST timestamps.

### 4.5 Parquet schemas (`data/schemas.py`, PyArrow)

```
ticks:   ts (timestamp[us, tz=Asia/Kolkata]), security_id int32, ltp float32,
         ltq int32, side int8 (+1 buy-aggressor / -1 sell / 0 unknown, §7.1)
quotes:  ts, security_id, bid float32, ask float32, bid_qty int32, ask_qty int32,
         volume int32, atp float32, total_buy_qty int32, total_sell_qty int32
depth5:  ts, security_id, level int8 (0-4), bid_px float32, bid_qty int32,
         bid_orders int16, ask_px float32, ask_qty int32, ask_orders int16
depth20: ts, security_id, side int8 (+1 bid/-1 ask), level int8 (0-19),
         px float64, qty int32, orders int32, seq int64, clean bool
oi:      ts, security_id, oi int32
ohlcv:   ts, security_id, interval int8, open/high/low/close float32,
         volume int64, oi int64
```

---

## 5. (reserved)

Numbering gap intentional — kept for future amendments without renumbering.

---

## 6. Bars (`features/bars.py`)

- `time_bars(ticks, quotes, interval_s) -> pd.DataFrame` with columns:
  `open, high, low, close, volume, buy_volume, sell_volume, delta
  (= buy_volume − sell_volume), n_trades, vwap, ts_open, ts_close`.
- Bars are built strictly from **exchange timestamps**, aligned to wall-clock
  boundaries (e.g. a 60 s bar covers [10:01:00, 10:02:00)). Never from arrival time.
- Also implement `volume_bars(ticks, bucket_volume)` — used by the model layer
  (volume bars have better statistical properties for ML labels).

---

## 7. Feature engineering — exact definitions

Every feature function is pure (`DataFrame in → Series/DataFrame out`), unit-
tested against small hand-computed fixtures. **No feature may look ahead**: value
at bar *t* uses only data with `ts < t.close`. A dedicated test asserts this by
recomputing features on truncated data.

### 7.1 Orderflow (`features/orderflow.py`)

**Aggressor classification (tick rule + quote rule):** for each tick, using the
prevailing best bid/ask (last quote with `ts ≤ tick.ts`):
`side = +1` if `ltp ≥ ask`; `-1` if `ltp ≤ bid`; else tick rule: sign of
`ltp − prev_ltp` (0 if unchanged, inherit previous side).

**CVD:** `cvd_t = Σ (side_i × ltq_i)` cumulative within session. Feature columns:
`cvd`, `cvd_slope_n` (linear-regression slope over last n bars), and
`cvd_divergence`: sign(price change over n bars) ≠ sign(cvd change over n bars).

**Footprint per bar:** aggregate per price level within the bar:
`buy_vol[p], sell_vol[p]`. Derived:
- *Diagonal imbalance*: `buy_vol[p] ≥ IMBALANCE_RATIO × sell_vol[p − tick]`
  (default ratio 3.0). Count of stacked imbalances (≥ 3 consecutive levels)
  per side per bar.
- *Absorption*: total bar volume in top quintile of trailing 20-bar volumes AND
  bar range ≤ 0.25 × ATR(14) → flag.

**OFI (order flow imbalance, Cont–Kukanov–Stoikov):** on each best-quote update:

```
e_n = 1{bid_n ≥ bid_{n-1}}·bidqty_n − 1{bid_n ≤ bid_{n-1}}·bidqty_{n-1}
    − 1{ask_n ≤ ask_{n-1}}·askqty_n + 1{ask_n ≥ ask_{n-1}}·askqty_{n-1}
OFI_bar = Σ e_n over the bar
```

Feature columns: `ofi`, `ofi_zscore` (vs trailing 100 bars).

### 7.2 Liquidity heatmap (`features/heatmap.py`)

Works on either `depth20` (NSE currency) or `depth5` (MCX) — the constructor
takes `n_levels` and behaves identically otherwise.

- Maintain a rolling matrix `H[price_bucket, t]` of resting quantity over the
  last `HEATMAP_WINDOW_BARS` bars (price bucketed to tick size × bucket factor).
- **Wall detection:** a price bucket whose average resting qty over the window
  is ≥ `WALL_PERCENTILE` (default 95th) of all buckets, persisting ≥
  `WALL_MIN_BARS` bars → emit as support (bid side) / resistance (ask side)
  level with strength score = qty percentile.
- **Pull/stack:** rate of change of qty at the 3 nearest levels each side;
  `pull_ask` (ask liquidity withdrawing while price rises) is a bullish
  confirmation feature; symmetric for bids.
- **Depth imbalance:** `(Σ bid_qty − Σ ask_qty) / (Σ bid_qty + Σ ask_qty)` over
  visible levels; feature per bar = time-weighted mean.
- Feature columns exported per bar: `dist_to_support_atr, dist_to_resistance_atr,
  support_strength, resistance_strength, depth_imbalance, pull_stack_score`.

### 7.3 Volume profile & VWAP (`features/volume_profile.py`)

- Session volume profile from ticks: histogram of volume by price bucket.
  `POC` = max-volume bucket; `VAH/VAL` = smallest price range around POC holding
  70% of volume. Computed *developing* (recomputed each bar, using session-to-date
  data only).
- Session VWAP and ±1σ, ±2σ bands (σ = volume-weighted std of price).
- Feature columns: `dist_to_poc_atr, above_vah (bool), below_val (bool),
  dist_to_vwap_sigma`.

### 7.4 Classical indicators (`features/indicators.py`)

- **DEMA** `2·EMA(n) − EMA(EMA(n))` for n ∈ {10, 20, 95} (port from
  `indices/indicators.py` — reimplement, do not import across modules).
- **ATR(14)** Wilder. **ADX(14)** Wilder.
- **Yang-Zhang realized volatility** over 20 bars (handles overnight gaps —
  needed because MCX/US sessions create gaps for FX).
- **Hurst exponent** via rescaled-range over rolling 200 bars.
- Time features: minute-of-day sin/cos encoding, plus one-hot flags:
  `in_london (13:30–16:30 IST)`, `in_ny_overlap (18:30–21:30 IST)`,
  `mcx_evening (after 17:00 IST)`.

---

## 8. Model layer

### 8.1 Labeling (`model/labeling.py`) — triple-barrier

For each bar *t* (on volume bars preferably, time bars acceptable for v1):
- Upper barrier: `close_t + PT_MULT × ATR_t` (default PT_MULT = 2.0)
- Lower barrier: `close_t − SL_MULT × ATR_t` (default SL_MULT = 1.0)
- Vertical barrier: `t + MAX_HOLD_BARS` (default 60 bars)
- Label `y = +1` if upper touched first, `−1` if lower first, `0` if vertical
  expires first. Also record `t_touch` (needed for purging, §8.2).
- Symmetric labels for shorts are implicit (predicting +1 means long edge;
  −1 means short edge).

### 8.2 Dataset assembly (`model/dataset.py`)

- Feature matrix X: every column exported by §7 modules + `key` one-hot.
- **Purged K-fold with embargo** (López de Prado): when splitting, drop any
  training sample whose label window `[t, t_touch]` overlaps a test sample's
  window; then embargo `EMBARGO_PCT` (default 1%) of samples after each test
  block. This is mandatory — naive K-fold on overlapping labels is leakage.
- Persist datasets to Parquet with a `dataset_version` hash of (feature list,
  label params, date range).

### 8.3 Training (`model/train.py`)

- LightGBM binary classifier per direction (or single 3-class; implementer's
  choice, justify in code docstring). Baseline hyperparameters:
  `num_leaves=31, max_depth=6, learning_rate=0.05, n_estimators=400,
  min_child_samples=100, subsample=0.8, colsample_bytree=0.8`. Tune only via
  the purged CV of §8.2 — never on the walk-forward test windows.
- **Isotonic calibration** on a held-out calibration fold: predicted 0.65 must
  empirically mean ≈ 65%. Persist calibrated model with `joblib` to
  `models/{key or 'pooled'}/{dataset_version}/model.joblib` + a `metrics.json`.
- Class imbalance: use `scale_pos_weight` or class weights — vertical-barrier
  zeros will dominate.
- Feature importance report (gain) written next to the model; alert if a single
  feature exceeds 40% of gain (usually leakage).

### 8.4 Kalman trend filter (`model/kalman.py`)

Local-linear-trend state space on bar closes:
state `[level, velocity]`, `F = [[1,1],[0,1]]`, `H = [1,0]`. Process/observation
noise `q, r` from config (`KALMAN_Q=1e-5, KALMAN_R=1e-2` defaults, per-instrument
override). Outputs per bar: `kf_level, kf_velocity, kf_velocity_z` (z-scored).
Used (a) as model features, (b) for trailing stops in §9.4.

---

## 9. Ensemble, risk, and signal output

### 9.1 Decision logic (`model/ensemble.py`) — exact pseudocode

```
inputs per bar: p_up (calibrated), features row f, position state
L0_long  = DEMA10 crossed above DEMA20 on this bar AND close > DEMA95
L0_short = DEMA10 crossed below DEMA20 on this bar AND close < DEMA95
L1_long  = p_up ≥ P_ENTRY            (default 0.60)
L1_short = p_up ≤ 1 − P_ENTRY
L2_long  = ofi_zscore > 0 AND cvd_slope_n > 0      # orderflow agrees
L2_short = ofi_zscore < 0 AND cvd_slope_n < 0

signal = BUY  if (L1_long  and L2_long)  or (L0_long  and L2_long)
signal = SELL if (L1_short and L2_short) or (L0_short and L2_short)
else HOLD
confidence = p_up if BUY else (1 − p_up) if SELL else 0.5
# ML path and baseline path both require the orderflow gate. Baseline path
# exists so the system still trades (with reduced size, §9.3) before/without
# a trained model.
```

Exits: opposite ensemble signal, SL/TP touch, session-end square-off
(`SQUARE_OFF_MIN_BEFORE_CLOSE`, default 10 min), or trailing stop
(`kf_level − TRAIL_MULT × ATR` for longs once ≥ 1R in profit).

### 9.2 Regime guard

Skip all entries when: `ADX < 15` **and** `|hurst − 0.5| < 0.05` (dead chop), or
when the instrument's realized spread over the last 30 min exceeds
`MAX_LIVE_SPREAD_PCT` (default 0.08%).

### 9.3 Risk & sizing (`execution/risk.py`)

- Module capital: `capital = CAPITAL_TOTAL × ALLOC_FX_COMMODITIES`.
- Per-trade risk budget: `risk_rupees = capital × RISK_PER_TRADE_PCT`
  (default 0.5%).
- Stop distance in rupees/lot: `stop_rupees = SL_MULT × ATR × rupee_value_per_point
  × lot_size` (rupee value per point comes from `Contract`; for INR-quoted MCX
  contracts it is direct, for cross pairs apply the USDINR conversion from the
  live USDINR quote).
- Lots: `floor(risk_rupees / stop_rupees)`, min 0, hard cap `MAX_LOTS` (default 4).
- Kelly modulation: `size_mult = clip(2 × confidence − 1, 0, 1) × KELLY_FRACTION`
  (default 0.5). Baseline-path trades (no ML) use `size_mult = 0.25` fixed.
- Margin check: computed lots × margin-per-lot must fit within
  `capital × MAX_MARGIN_UTILIZATION` (default 60%); query margin via Dhan fund
  limits endpoint before entry, block if insufficient.
- **Correlation guard**: instruments grouped by USD exposure sign:
  `{long EURUSD, long GBPUSD, short USDJPY, long GOLDM, long SILVERM}` all imply
  short-USD. Net same-direction USD-group positions capped at
  `MAX_CORRELATED_POSITIONS` (default 2). Also global cap
  `MAX_OPEN_POSITIONS` (default 3).
- Loss limits: per-instrument daily loss `MAX_DAILY_LOSS_PER_INSTRUMENT_PCT`
  (default 1% of capital), module daily loss `MAX_DAILY_LOSS_PCT` (default 2%);
  breach ⇒ flatten (module-wide breach flattens everything) and halt entries
  until next session. Halt state persists in the journal (survives restart).

### 9.4 Signal schema (`signals.py`)

```python
@dataclass(frozen=True)
class Signal:
    ts: datetime            # bar close, IST
    contract_key: str       # "GOLDM"
    security_id: str
    action: Literal["BUY", "SELL", "HOLD", "EXIT"]
    confidence: float       # calibrated, 0..1
    entry: float            # reference price (bar close)
    stop_loss: float
    target: float
    lots: int
    source: Literal["ensemble", "baseline", "risk_flatten", "session_end"]
    features_digest: dict   # {p_up, ofi_z, cvd_slope, adx, hurst, spread_pct}
```

Every signal (including HOLDs when a position is open) is journaled. An optional
`SIGNAL_WEBHOOK_URL` posts non-HOLD signals as JSON for the user's notification
channel.

---

## 10. Backtesting

### 10.1 Engine (`backtest/engine.py`)

- Event-driven over bars (v1) with the same ensemble/risk code paths as live —
  **the live trader and the backtester must call the identical
  `ensemble.decide()` and `risk.size()` functions**. No duplicated logic.
- Fills: market entry fills at `bar_close + slippage` (§10.2); SL/TP fills use
  intra-bar OHLC conservative rules (if both SL and TP inside one bar, assume
  SL hit first).
- Outputs: trade list (entry/exit ts, prices, lots, costs, PnL), equity curve,
  and metrics: net PnL, profit factor, hit rate, avg win/loss, max drawdown,
  Sharpe (annualized on daily returns), turnover, total costs.

### 10.2 Cost model (`backtest/costs.py`)

Per side, config-driven with these defaults (operator must verify against their
Dhan contract note and update `.env`):
- Brokerage: ₹20 flat per executed order
- Exchange transaction charges: NSE currency futures ~0.00035% of turnover;
  MCX futures ~0.0021% of turnover
- SEBI fee 0.0001%, stamp duty 0.00015% (buy side), GST 18% on
  (brokerage + transaction charges); CTT 0.01% on sell side for MCX non-agri
- **Slippage**: half-spread per side, where spread = median recorded spread for
  that instrument and time-of-day bucket (from Phase 1 recordings); fallback
  `DEFAULT_SLIPPAGE_TICKS=1`.

### 10.3 Walk-forward (`backtest/walkforward.py`)

- Rolling: train 60 trading days → test 10 trading days → step 10. Minimum 6
  test windows before any deployment decision.
- Retrain (incl. recalibration) at every step; hyperparameters frozen across
  steps (tuned only once, on the first train window, via purged CV).
- Report: `reports/walkforward_{date}.json` + human-readable MD with per-window
  and aggregate metrics, equity curve PNG.
- **Deployment gate encoded in code**: `aggregate.profit_factor_after_costs ≥ 1.1`
  and `max_drawdown ≤ 10% of module capital`. `trader.py --live` reads the latest
  report and refuses to start otherwise.

---

## 11. Execution & journaling

### 11.1 SQLite journal (`common/journal.py`), file `journal.db`

```sql
CREATE TABLE orders (
  correlation_id TEXT PRIMARY KEY, order_id TEXT, ts_created TEXT NOT NULL,
  module TEXT NOT NULL,            -- 'fx_commodities'
  contract_key TEXT NOT NULL, security_id TEXT NOT NULL,
  side TEXT NOT NULL, lots INTEGER NOT NULL, qty INTEGER NOT NULL,
  order_type TEXT NOT NULL, limit_price REAL, trigger_price REAL,
  status TEXT NOT NULL,            -- mirrors Dhan statuses
  filled_qty INTEGER DEFAULT 0, avg_fill_price REAL, ts_terminal TEXT
);
CREATE TABLE positions (
  id INTEGER PRIMARY KEY, contract_key TEXT, security_id TEXT,
  direction TEXT, lots INTEGER, entry_price REAL, stop_loss REAL, target REAL,
  ts_open TEXT, ts_close TEXT, exit_price REAL, realized_pnl REAL,
  exit_reason TEXT, signal_json TEXT
);
CREATE TABLE risk_state (
  session_date TEXT PRIMARY KEY, daily_pnl REAL, halted INTEGER,
  halt_reason TEXT, updated_ts TEXT
);
CREATE TABLE events (
  ts TEXT, level TEXT, kind TEXT, payload_json TEXT   -- feed_gap, reconcile_mismatch, ...
);
```

### 11.2 Order lifecycle (`execution/executor.py`)

1. Insert journal row (status `LOCAL_NEW`) with fresh `correlation_id` **before**
   calling Dhan. Duplicate correlation_id ⇒ refuse (idempotency).
2. Place order; store `order_id`; poll `GET /orders/{id}` every 2 s until
   terminal (max 60 s → alert + treat as unknown, reconcile).
3. PnL only ever computed from **actual** `avg_fill_price × filled_qty`.
4. Entry orders: MARKET. Protective stop: place a real `STOP_LOSS_MARKET` order
   at the SL immediately after entry fill confirmation (never software-only
   stops for MCX evening sessions — the process might die). Target: software-
   managed; on target touch, cancel the SL order then flatten. On any flatten,
   cancel outstanding protective orders first.
5. **Startup reconciliation**: fetch Dhan positions; diff against `positions`
   where `ts_close IS NULL`. Unknown broker position ⇒ CRITICAL alert + halt
   (do not auto-flatten someone's manual trade). Journal position missing at
   broker ⇒ mark closed with reason `reconcile_lost`, alert.

---

## 12. Common infrastructure (`common/`)

### 12.1 `auth.py`

Holds token in a small state file (`.token.json`, git-ignored) with expiry.
Background renewal via `POST /RenewToken` every 12 h; on failure → CRITICAL
alert with the runbook line "generate new token at web.dhan.co and restart".

### 12.2 `http.py`

`dhan_request(method, path, json, *, budget: str)`:
- Retries on 429/5xx/timeouts: 3 attempts, backoff 1 s/2 s/4 s + jitter.
- Circuit breaker per host: opens after 5 consecutive failures, half-open probe
  after 30 s. While open, callers get `CircuitOpenError` — the trader treats
  this as "cannot manage risk" and refuses **new entries** (exits still
  attempted, once, directly).
- Client-side rate budgets (token bucket): `orders: 5/s`, `data: 1/s`,
  `option_chain: 1 per 3 s` (shared constants; conservative vs Dhan's limits).

### 12.3 `alerts.py`

`alert(level, msg, **ctx)` → structured CRITICAL log line + optional POST to
`ALERT_WEBHOOK_URL` (Telegram bot / generic). Stub is fine; interface matters.

---

## 13. Configuration (`.env` spec — full list)

```
# credentials
DHAN_CLIENT_ID=            DHAN_ACCESS_TOKEN=
# capital
CAPITAL_TOTAL=500000       ALLOC_FX_COMMODITIES=0.50
RISK_PER_TRADE_PCT=0.5     MAX_DAILY_LOSS_PCT=2.0
MAX_DAILY_LOSS_PER_INSTRUMENT_PCT=1.0
MAX_LOTS=4                 MAX_OPEN_POSITIONS=3   MAX_CORRELATED_POSITIONS=2
MAX_MARGIN_UTILIZATION=0.6 KELLY_FRACTION=0.5
# universe
UNIVERSE=EURUSD,GBPUSD,USDJPY,USDINR,EURINR,GBPINR,JPYINR,GOLDM,SILVERM,CRUDEOILM,NATURALGAS,COPPER
ROLLOVER_BUFFER_DAYS=2     MAX_MEDIAN_SPREAD_PCT=0.05  MAX_LIVE_SPREAD_PCT=0.08
# strategy / model
BAR_INTERVAL_S=60          PT_MULT=2.0   SL_MULT=1.0   MAX_HOLD_BARS=60
P_ENTRY=0.60               IMBALANCE_RATIO=3.0
TRAIL_MULT=1.5             SQUARE_OFF_MIN_BEFORE_CLOSE=10
KALMAN_Q=1e-5              KALMAN_R=1e-2
EMBARGO_PCT=0.01
# heatmap
DEPTH_SNAPSHOT_INTERVAL_MS=500   HEATMAP_WINDOW_BARS=120
WALL_PERCENTILE=95         WALL_MIN_BARS=10
# data
DATA_ROOT=./data_root      FLUSH_INTERVAL_S=60
# costs (verify against your contract note)
BROKERAGE_PER_ORDER=20     DEFAULT_SLIPPAGE_TICKS=1
# ops
TIMEZONE=Asia/Kolkata      ALERT_WEBHOOK_URL=    SIGNAL_WEBHOOK_URL=
HEALTH_PORT=8081           LOG_LEVEL=INFO
```

`config.py` loads this into a frozen pydantic settings object; missing
credentials fail fast with a clear message.

---

## 14. Build phases, deliverables, and acceptance gates

### Phase 1 — Data foundation (build first, everything depends on it)
Deliverables: `common/` (auth, http, alerts), `instruments.py`, `data/*`,
health endpoint, unit tests for packet parsing (binary fixtures!) and resolver.
**Gate:** recorder runs 5 consecutive trading days across full MCX sessions with
< 0.1% feed-gap time; spread report per contract produced
(`reports/spread_report.md`) and the tradeable universe finalized per D1.

### Phase 2 — Features + baseline + backtester
Deliverables: `features/*`, `bars.py`, backtest engine + cost model, baseline
DEMA strategy running through the backtester on 2 years of backfilled OHLCV
(features restricted to OHLCV-derivable ones for the historical period; orderflow
features only over the recorded window).
**Gate:** backtest report for the baseline exists; all feature unit tests and
the no-lookahead test pass.

### Phase 3 — Model + walk-forward
Deliverables: `model/*`, walk-forward runner + report.
**Gate:** aggregate OOS profit factor after costs ≥ 1.1 and max DD ≤ 10% of
module capital. If failed: iterate features/labels, or ship baseline-only mode
at reduced size — do NOT lower the gate.

### Phase 4 — Paper, then live
Deliverables: `paper.py` (same loop as trader.py, orders simulated at live
quotes + modeled slippage, journaled identically), `trader.py --live`.
**Gate to live:** ≥ 10 paper sessions; paper profit factor within 30% of the
backtest expectation; zero reconciliation mismatches; then live at
`MAX_LOTS=1` for the first two weeks regardless of sizing output.

### Testing requirements (all phases)
- pytest; coverage of `features/`, `model/labeling`, `risk` ≥ 90% lines.
- Binary parser fixtures constructed by hand from §1.1/§1.2 tables.
- A `tests/test_no_lookahead.py` that truncates input data and asserts feature
  values at earlier bars are unchanged.
- Risk tests: daily-loss halt persists across simulated restart; correlation
  guard blocks the 3rd same-direction USD position; sizing never exceeds caps.

---

## 15. Operator runbook items (write as `fx_commodities/RUNBOOK.md`)

1. IP whitelisting at web.dhan.co before first order (7-day lock after change).
2. Daily token flow and what the expiry alert means.
3. How to start/stop recorder and trader (systemd units or `docker compose` —
   provide one of them).
4. What a `reconcile_mismatch` CRITICAL alert requires (manual position check).
5. How to re-run walk-forward and interpret the deployment gate.
6. Cost-model verification against the first real contract note.

---

## 16. Explicitly out of scope (v1)

- Options on FX/commodities (futures only).
- Tick-level backtesting of the ML layer (bar-level with recorded-spread
  slippage is the v1 standard).
- Cross-venue data (no international FX feeds; Dhan/Indian exchanges only).
- Any UI beyond the health endpoint and MD reports.
- Modifying or migrating `indices/` (separate roadmap, PLAN.md Part 2).
