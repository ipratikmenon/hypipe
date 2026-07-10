"""Run Phase 3 Steps 1-3 against real backfilled data — the smoke test that
proves labels, HAR, and regimes work end-to-end on market data, and writes
reports/real_data_demo.md.

    python -m fx_commodities.model.demo_real_data --symbol BTCUSD
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..config import load_settings
from ..data.store import Store
from ..features.bars import bars_from_ohlcv
from ..features.indicators import add_classical
from .har import HarRV, oos_r2_vs_random_walk, realized_variance
from .labeling import label_stats, triple_barrier_labels
from .regime import RegimeHMM, variance_ratio_features

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSD")
    args = ap.parse_args()
    cfg = load_settings()

    store = Store(cfg.data_root)
    ohlcv = store.read_ohlcv(args.symbol, date.today() - timedelta(days=400),
                             date.today())
    if ohlcv.empty:
        raise SystemExit("no ohlcv data — run backfill_public first")
    bars = bars_from_ohlcv(ohlcv.drop_duplicates("ts"))
    log.info("%s: %d one-minute bars, %s → %s", args.symbol, len(bars),
             bars["ts_utc"].iloc[0], bars["ts_utc"].iloc[-1])

    # resample to the strategy bar interval to keep the demo fast
    bars5 = bars.set_index("ts_utc").resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "tick_volume": "sum", "spread_mean_points": "mean",
         "ts_open_ms": "first", "ts_close_ms": "last",
         "up_ticks": "sum", "down_ticks": "sum", "tick_count": "sum"}
    ).dropna(subset=["close"]).reset_index()
    feats = add_classical(bars5)

    # ── Step 1: labels ─────────────────────────────────────────────────────
    labels = triple_barrier_labels(feats, feats["atr"], cfg.pt_mult,
                                   cfg.sl_mult, cfg.max_hold_bars)
    lstats = label_stats(labels)

    # ── Step 2: HAR ────────────────────────────────────────────────────────
    rv = realized_variance(bars)
    har = oos_r2_vs_random_walk(rv)
    fc = HarRV().fit(rv).forecast(rv)
    ann_vol_fc = float(np.sqrt(fc * 365))

    # ── Step 3: regimes ────────────────────────────────────────────────────
    r = np.log(feats["close"] / feats["close"].shift(1))
    day = feats["ts_utc"].dt.strftime("%Y-%m-%d")
    rv_bar = (r ** 2).groupby(day).transform("sum")
    obs_df = pd.DataFrame({"r": (r * 100), "lrv": np.log(rv_bar)}).dropna()
    obs = obs_df.to_numpy()
    split = int(len(obs) * 0.7)
    hmm = RegimeHMM().fit(obs[:split])
    probs = hmm.filtered_probs(obs)
    occupancy = probs[["p_trend", "p_chop", "p_crisis"]].idxmax(axis=1) \
        .value_counts(normalize=True).to_dict()
    vr = variance_ratio_features(feats["close"]).dropna()

    report = f"""# Real-data pipeline demo — {args.symbol}
Data: {len(bars):,} 1m bars ({bars['ts_utc'].iloc[0].date()} → {bars['ts_utc'].iloc[-1].date()}), OKX public.

## Step 1 — Triple-barrier labels (5m bars, PT {cfg.pt_mult}×ATR / SL {cfg.sl_mult}×ATR)
- samples: {lstats['n']:,}
- up-barrier first: {lstats['frac_up']:.1%} | down-first: {lstats['frac_down']:.1%} | vertical: {lstats['frac_vertical']:.1%}
- median bars to resolve: {lstats['median_bars_to_resolve']:.0f}

## Step 2 — HAR-RV
- OOS R² (log RV): HAR {har['r2_har']:.3f} vs random-walk {har['r2_rw']:.3f} ({'HAR WINS' if har['r2_har']>har['r2_rw'] else 'RW wins — investigate'}) on {har['n_test']} test days
- next-day annualized vol forecast: {ann_vol_fc:.1%}

## Step 3 — Regime HMM (filtered, causal)
- occupancy: {{{', '.join(f"{k}: {v:.1%}" for k,v in occupancy.items())}}}
- variance ratio VR(10): mean {vr['vr_10'].mean():.2f}
"""
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.reports_dir / f"real_data_demo_{args.symbol}.md"
    out.write_text(report)
    print(report)
    log.info("written %s", out)


if __name__ == "__main__":
    main()
