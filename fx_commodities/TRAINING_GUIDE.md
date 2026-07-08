# Training Guide — from zero to a trained trading model

Written for a newcomer to AI training. Every step says **what** to do, **why**
it matters, and **what "done" looks like**. It also serves as the onboarding
document for any AI assistant continuing this work — the authoritative build
spec is [REQUIREMENTS.md](REQUIREMENTS.md); section references below (§) point
into it.

---

## Part 0 — The ideas, in plain language

You only need eight concepts to understand this entire system:

| Term | What it actually means here |
|------|------------------------------|
| **Feature** | A number computed from market data that might carry information — e.g. "how fast is buying pressure growing" (CVD slope) or "how far is price from the volume point-of-control". The model's *inputs*. |
| **Label** | The answer we want predicted. Ours: "after this bar, did price rise 0.8×ATR before falling 1.6×ATR?" — yes/no. (§8.1 triple-barrier) |
| **Training** | Showing the model millions of (features → label) examples so it learns which feature patterns precede a "yes". |
| **Overfitting** | The model memorizing noise instead of learning patterns. It looks brilliant on past data and loses money live. **This is the #1 enemy** — half our spec exists to fight it. |
| **Backtest** | Replaying history through the strategy, with realistic costs, to estimate how it would have done. |
| **Walk-forward** | The honest way to backtest a model: train on days 1–60, test on days 61–70, slide forward, repeat. The model is never graded on data it saw in training. (§10.3) |
| **Calibration** | Making the model's confidence honest: when it says "80%", it must be right about 80% of the time. Our entry math depends on this. (§8.8) |
| **Paper trading** | Running live with fake orders. Mandatory before real money. (Phase 4) |

The pipeline you are building, end to end:

```
ticks (recorded) → bars → features (§7) → labels (§8.1)
      → model training (§8.6–8.7) → calibration (§8.8)
      → walk-forward report (§10.3) → GATES pass? → paper → live
```

---

## Part 1 — Set up your machine (one-time, ~30 min)

Any Linux machine or Windows-with-WSL2 works for everything except MT5
(which needs native Windows — crypto needs no Windows at all).

```bash
# 1. Python 3.11+
python3 --version

# 2. Clone and install
git clone <this-repo> && cd hypipe
python3 -m venv .venv && source .venv/bin/activate
pip install -r fx_commodities/requirements.txt websockets

# 3. Configure
cp fx_commodities/.env.example .env
# For crypto-only you can leave the MT5_* fields empty.

# 4. Verify everything works
python -m pytest fx_commodities/tests -q     # expect: all passed
```

**Done when:** tests pass.

---

## Part 2 — Start the data moat (do this TODAY; everything waits on it)

Models are only as good as their data, and nobody sells the tick + order-book
history we need — we record our own. The earlier this starts, the sooner
training is possible.

```bash
# Crypto (works on any Linux box, no account needed):
python -m fx_commodities.data.record_crypto
```

- Watch the log: a heartbeat line every 5 minutes with row counts.
- Files appear under `data_root/ticks/...` and `data_root/dom/...` as
  Parquet — one folder per symbol per UTC day.
- Health check from another terminal: `python -m fx_commodities.health`
  then open `http://localhost:8081/health`.
- If Binance returns HTTP 451 in your region, set `BINANCE_REST_BASE` /
  `BINANCE_WS_BASE` in `.env` to a mirror that serves you.
- Run it under `tmux`/`systemd` so it survives logout. Expect roughly
  1–3 GB/day for BTC+ETH. Disk is the cheapest alpha you will ever buy.

**Also backfill candles** (history for the baseline backtest — one-off):
```bash
python - <<'PY'
from fx_commodities.broker.crypto_adapter import BinanceDataAdapter, default_symbol_map
from fx_commodities.data.store import Store
from fx_commodities.config import load_settings
cfg = load_settings()
a = BinanceDataAdapter(default_symbol_map(("BTCUSD","ETHUSD"))); 
s = Store(cfg.data_root)
for k in ("BTCUSD","ETHUSD"):
    print(k, s.backfill_m1(a, k, years=3), "bars")
PY
```

**Done when:** the recorder has run ≥ 5 consecutive days with heartbeats, and
`data_root/ohlcv/` holds ~3 years of M1 per symbol.
**Target before ML training:** 2–4 weeks of recorded ticks (§3.4 of PLAN.md).

---

## Part 3 — Run the baseline backtest (no AI yet — deliberately)

Before any machine learning, measure the simple DEMA strategy and the
zone-confirmation gate. This gives the number every model must beat, and
teaches you to read the reports.

The runner script is a Phase-2 remainder; ask your AI assistant:
*"Build backtest/run_baseline.py per §10 using Store + bars_from_ohlcv +
DemaBaseline and ZoneConfirmedBaseline, write reports/baseline_<symbol>.json"*.

How to read the output (`reports/baseline_*.json`):

- `profit_factor` — gross wins ÷ gross losses, **after costs**. Below 1.0 =
  losing. The deploy gate is ≥ 1.1 (§10.3).
- `hit_rate` — fraction of winners. Your D5 profile targets ~0.85 with small
  targets and wide stops; a high hit rate with PF < 1 means losses are too
  big — do NOT "fix" it by widening targets; investigate.
- `max_drawdown` — worst equity dip. Gate: ≤ 10% of module capital.
- `confirmation study` — the §9.1b numbers: `delta_p` (win-rate uplift from
  waiting for confirmation), `delta_atr` (price you paid for waiting), and
  `confirmation_value` (positive = the gate earns its keep on this symbol).

**Done when:** you can explain each number above to yourself.

---

## Part 4 — First ML training (CPU is fine; no GPU needed yet)

This is Phase 3 (§8). The model is LightGBM — gradient-boosted decision
trees, the workhorse of real quant desks, and it trains on a laptop CPU in
minutes. **You do not need a GPU for your first trained model.**

Build order for your AI assistant (each has full math in the spec):
1. `model/labeling.py` — triple-barrier labels (§8.1)
2. `model/har.py` — volatility forecaster (§8.2)
3. `model/regime.py` — HMM regimes (§8.3)
4. `model/dataset.py` — purged splits + uniqueness weights (§8.5) ← the
   anti-overfitting core; never skip or "simplify" this
5. `model/train.py` + `model/meta.py` + `model/conformal.py` (§8.6–8.8)
6. `backtest/walkforward.py` (§10.3)

Then:
```bash
python -m fx_commodities.backtest.walkforward --symbol BTCUSD
cat reports/walkforward_*.json
```

Reading the walk-forward report — the three gates (§10.3), in plain words:
- `profit_factor_after_costs ≥ 1.1` — it makes money after realistic costs.
- `max_dd ≤ 10%` — survivable losing streaks.
- `PSR ≥ 0.95` — statistically, ≥95% probability the edge is real rather
  than luck, given how many trades and how fat-tailed they are.

**All three pass → paper trading. Any fail → iterate features/labels.**
Never loosen a gate to pass it. The gates are the system's immune system.

---

## Part 5 — The golden rules (read twice)

1. **Every experiment costs statistical credibility.** Each time you test an
   idea against out-of-sample data, note it (reports/trials.json feeds the
   DSR, §10.3). Run 200 experiments and the best one will look great by pure
   luck — the DSR number tells you how discounted your results are.
2. **Never evaluate on data the model saw.** The purged walk-forward exists
   because financial labels overlap in time; ordinary cross-validation
   silently leaks and produces beautiful lies.
3. **If a result looks too good, it is.** A PF above ~1.5 after costs at this
   frequency almost always means a lookahead bug. Run the no-lookahead test;
   check the label join; check timestamp alignment.
4. **More trades is not the goal.** Your profile is 1–2 high-conviction
   trades/day. Loosening thresholds to trade more inverts the D5 math
   (at 70% win rate the profile LOSES — it's a unit test).
5. **Paper trade ≥ 10 sessions before real money**, then two weeks at
   minimum size, regardless of how good the backtest looks (Phase 4 gates).

---

## Part 6 — Adding the GPU (only after Part 4 passes)

The GPU unlocks R6 of the research track (§8.11): a DeepLOB-style neural
network that reads the raw 20-level order book — the data your recorder has
been stockpiling since Part 2.

1. **Hardware:** one NVIDIA card with ≥ 16 GB VRAM (RTX 4090-class ideal;
   a used 3090 also works). Nothing more until you have years of data.
2. **Install:** NVIDIA driver, then
   `pip install torch --index-url https://download.pytorch.org/whl/cu124`
   and verify: `python -c "import torch; print(torch.cuda.is_available())"`
   → `True`.
3. **The model:** CNN over the last 100 book snapshots × 20 levels
   (price+size, both sides), predicting the same triple-barrier label. Ask
   your AI assistant: *"Implement R6 per §8.11: DeepLOB-class model on the
   recorded dom table, trained under the identical purged walk-forward as
   train.py, with the promotion gate."*
4. **The promotion gate (non-negotiable):** the neural net replaces LightGBM
   only if it beats it out-of-sample by ≥ 2 points of precision at equal
   recall. Most published attempts fail this against a well-tuned tree
   model. If it fails, you lost nothing — LightGBM keeps trading.
5. **Track experiments:** local MLflow (`pip install mlflow`, `mlflow ui`);
   every run increments the DSR trial counter. GPU makes experiments cheap;
   the counter keeps them honest.

---

## Part 7 — How to direct an AI assistant on this codebase

Everything an AI needs is in the repo — point it at the right section:

- "Build X" → cite the § in `fx_commodities/REQUIREMENTS.md`; the spec has
  formulas, defaults, file paths, and acceptance gates.
- Current state: Phases 1–2 built (adapters, recorder, features, zones,
  confirmation gate, backtester + tests). Phase 3 (model layer) is next.
- House rules the AI must keep: closed-bar rule everywhere; tier-gated
  features (never impute missing orderflow); every new feature needs a
  no-lookahead test; gates never get loosened; `indices/` is frozen.
- After any build: `python -m pytest fx_commodities/tests -q` must pass,
  and commits go to the feature branch.
