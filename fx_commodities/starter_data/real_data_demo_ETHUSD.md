# Real-data pipeline demo — ETHUSD
Data: 86,499 1m bars (2026-05-11 → 2026-07-10), OKX public.

## Step 1 — Triple-barrier labels (5m bars, PT 0.8×ATR / SL 1.6×ATR)
- samples: 17,299
- up-barrier first: 63.7% | down-first: 36.2% | vertical: 0.1%
- median bars to resolve: 3

## Step 2 — HAR-RV
- OOS R² (log RV): HAR -0.215 vs random-walk -0.354 (HAR WINS) on 18 test days
- next-day annualized vol forecast: 46.1%

## Step 3 — Regime HMM (filtered, causal)
- occupancy: {p_crisis: 37.2%, p_trend: 33.2%, p_chop: 29.6%}
- variance ratio VR(10): mean 0.89
