# overnight/ — Indian Index Overnight-Gap Engine

**Version 1.0 — 2026-07-09 — Status: APPROVED FOR BUILD (after fx_commodities Phase 3)**

Predicts whether NIFTY / BANKNIFTY / MIDCPNIFTY / FINNIFTY will **open higher
or lower tomorrow** from the previous afternoon's session (decision at
15:15–15:25 IST), and — when the edge clears the option's overnight carrying
cost — buys an index option at ~15:20 to sell at next morning's open
(09:16–09:20). Broker: Dhan (reuses the `indices/` API knowledge; `indices/`
itself stays frozen). Model machinery: **imported from `fx_commodities.model`**
(labeling/calibration/walk-forward are asset-agnostic — do not duplicate them).

---

## 1. Honest framing — what 3 PM action can and cannot predict

The dominant driver of Indian index opening gaps is **what happens overseas
overnight** (US session, global risk events) — unknowable at 15:15 IST. The
afternoon session contributes a real but *modest* signal: last-hour momentum
persists into the open (documented continuation effect), futures basis and OI
buildup encode positioned money's overnight bet, and PCR/VIX encode hedging
demand. Expect calibrated directional accuracy in the **55–62%** band on
tradeable days, not more. The strategy survives because it is *selective*
(trades only when predicted edge clears the cost hurdle below) — not because
the crystal ball is good.

## 2. The trade's mathematics — theta is the toll gate

Buy an option at 15:20, sell at next open. P&L per lot (premium points):

```
PnL ≈ Δ·S·G  −  Θ_on  +  V·ΔIV  −  c
     direction   overnight   vega     spread+fees
     payoff      theta       (IV change)
```

`G` = gap fraction, `S` = spot, `Δ` = option delta, `Θ_on` = overnight theta
decay in premium points, `c` = round-trip costs. Assume symmetric typical gap
magnitude `g = E[|G|]` and calibrated directional probability `p`:

```
E[PnL] = Δ·S·g·(2p − 1) − Θ_on − c   >  0
   ⟺   p  >  p*  =  ½ · ( 1 + (Θ_on + c) / (Δ·S·g) )
```

**Worked example (NIFTY ≈ 25,000, ATM Δ=0.5, median |gap| g=0.35% →
Δ·S·g ≈ 44 pts; Θ_on ≈ 12 pts, c ≈ 3 pts):** `p* ≈ 0.67`. The model must be
~67% confident at the median gap — which is why most nights are NO TRADE and
the system only fires on high-conviction afternoons or when Θ_on is low.
`p*` is computed **live nightly** from the actual chain (theta from Dhan's
option-chain greeks, spread from top-of-book) — never hardcoded.

**Carry-cost rules (encode, don't leave to judgment):**
- NEVER hold current-week options overnight with ≤ 2 days to expiry (theta
  explodes); roll the instrument rule to **next-week expiry** ATM.
- Friday nights carry 3 calendar days of theta → require `p ≥ p* + 0.05` extra.
- Event nights (US CPI/FOMC overnight): morning IV crush adds negative vega
  P&L — flagged by the calendar (§5), default NO TRADE unless
  `EVENT_NIGHT_OK=true`.

## 3. Features (all computable by 15:15 IST from Dhan data)

Daily rows — one per index per day. **Labels never overlap** (each resolves at
next open), so ordinary walk-forward applies; purging is not needed here.

**Afternoon tape (from 1-min index candles):**
`r_1500_1515` (the "3 PM move"), `r_1430_1515`, `r_full_day`,
`close_vs_vwap_sigma`, `close_location = (close−low)/(high−low)`,
`last_hour_updown_ratio`, `last_hour_volume_vs_20d`, `pdelta_slope_last_hour`.

**Positioned-money signals:**
- `futures_basis = (near_future − spot)/spot` at 15:10 and its Δ vs yesterday
  — premium expansion into the close = longs paying up for overnight.
- Futures OI change classification: long-buildup / short-buildup /
  long-unwind / short-covering (price×OI 2×2).
- `pcr_oi` at 15:10 from the option chain + Δ vs yesterday.
- `india_vix` close + Δ (Dhan IDX security).
- `iv_atm` weekly ATM IV + Δ vs yesterday.

**Context:** day-of-week one-hots, expiry-day / expiry-eve flags per index,
gap yesterday (`G_{t−1}`), 20-day realized daily vol, month-end flag,
calendar event-night flags (§5).

## 4. Model & decision

- Binary classifier `P(G > 0)` (LightGBM, ~40 features, pooled across the 4
  indices with index one-hots) + a small quantile model for `E[|G| | features]`
  (LightGBM quantile α=0.5) so the hurdle uses tonight's *expected* gap, not
  the unconditional median.
- Isotonic calibration + the same walk-forward/PSR gates as fx_commodities
  (§10.3 there) — train 500 days, test 60, roll. ~1,200 daily rows per index
  from 5 years of Dhan history: pooled ≈ 4,800 rows. Enough for LightGBM,
  far too few for deep nets — don't try.
- **Decision at 15:20:** direction = sign(p−½); trade iff
  `max(p, 1−p) ≥ p*(Θ_on, c, Δ, ĝ) + EDGE_MARGIN_ON (0.03)`; instrument =
  ATM CE (gap-up) / ATM PE (gap-down), next-week expiry per §2 rules;
  size = premium such that full premium loss ≤ `RISK_PER_TRADE`.
- **Exit at 09:16–09:20 next day, unconditionally.** No holding winners into
  the day — that's a different (untested) strategy; discipline here is the
  edge. Square-off order at market open + 1 min, journaled like every trade.

## 5. News & calendar tracking (`news/` — shared with fx_commodities R1)

- `news/calendar.py`: ingest a free economic-calendar source into
  `data_root/calendar.parquet` (event, country, impact, ts_utc). Primary:
  Forex Factory weekly JSON (unofficial but stable); fallback: manual
  `calendar_manual.csv` the operator maintains (RBI policy, Union Budget,
  elections, index-expiry schedule are known months ahead).
- Exposes: `is_blackout(ts, symbol)` for intraday R1, and
  `event_night_flags(date)` → {us_cpi_tonight, fomc_tonight, rbi_tomorrow,
  budget_window, in_election_window} consumed as §3 features and §2 rules.
- **R9 (optional, later):** LLM headline sentiment. Honest note: useful as a
  *veto* (detect "something big broke after 15:00") — not as alpha; run a
  local model over a headlines RSS set, output one bounded feature
  `headline_risk ∈ [0,1]`. Never auto-trade on headlines alone.

## 6. The daily signal report (`report/daily.py`) — the user-facing product

Generated twice daily, written to `reports/daily/YYYY-MM-DD.md` + posted to
`SIGNAL_WEBHOOK_URL` (Telegram-friendly):

**15:25 IST — Overnight report:**
```
OVERNIGHT GAP REPORT — 2026-07-09 15:25 IST
NIFTY      p_up 0.71 | p* 0.66 | ĝ 0.42% | Θ_on 9.8 | → BUY 25100 CE (16-Jul) 1 lot   ✅
BANKNIFTY  p_up 0.58 | p* 0.69 | ĝ 0.31% | Θ_on 21.4 | → NO TRADE (edge < hurdle)
MIDCPNIFTY p_up 0.44 | p* 0.68 | ĝ 0.38% | ...      | → NO TRADE
FINNIFTY   expiry-eve — instrument rule blocks weekly; next-week IV rich → NO TRADE
Event tonight: US CPI 18:00 UTC → +0.05 hurdle applied to all.
```

**08:45 IST — Morning brief:** overnight position P&L plan (exit at open),
plus the *intraday* watchlist: yesterday's zones per index (from the zone
set), first-hour bias from the gap model's residual, calendar for the day.
Every recommendation carries its full decision digest (§9.4 style) so any
trade is auditable from the report alone.

## 7. Module layout & build order

```
overnight/
├── config.py            # RISK_PER_TRADE, EDGE_MARGIN_ON, event rules
├── data.py              # Dhan: 1-min index candles, futures OI, chain @15:10,
│                        # India VIX; daily feature-row builder (§3)
├── carry.py             # Θ_on, Δ, c from live chain → p* (the §2 formula)
├── model.py             # LightGBM direction + |gap| quantile; calibration
├── decide.py            # §4 decision; emits Signal (§9.4 schema, source="overnight")
├── execute.py           # 15:20 entry / 09:16 exit via Dhan orders, journaled
├── news/calendar.py     # §5 (shared import target for fx_commodities R1)
├── report/daily.py      # §6
└── tests/               # p* formula fixtures, feature no-lookahead (15:15
                         # cutoff!), instrument rule (expiry-eve), report render
```

Build order: data.py + carry.py (+ tests) → historical feature/label build
from 5y Dhan candles → model + walk-forward → **paper 20 sessions** (signals
in the report, no orders) → tiny size. Gates identical in spirit to
fx_commodities §10.3, with one addition: **directional hit rate on
p≥p* trades must exceed the p* they were taken at** (calibration in the only
place it matters).

## 8. Explicitly honest limitations

1. Gap direction is mostly set overseas after our decision time — the model
   is a selectivity filter, not a forecast of the US session.
2. FII/DII flow data publishes ~18:00 IST — AFTER the 15:20 decision; it may
   only be used as a *next-day* feature (yesterday's flows), never same-day.
3. GIFT Nifty overnight prices would nearly settle the question by 08:00 —
   but by then options can't be bought at yesterday's prices; useful only for
   the 08:45 morning brief, not the 15:20 decision.
4. Slippage at 09:16 open is real (wide spreads in the first minute); the
   backtest must price exits at 09:17–09:20 quotes, not the opening print.
