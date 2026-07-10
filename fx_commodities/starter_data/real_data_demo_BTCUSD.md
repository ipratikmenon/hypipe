# Real-data pipeline demo — BTCUSD
Data: 86,499 1m bars (2026-05-11 → 2026-07-10), OKX public.

## Step 1 — Triple-barrier labels (5m bars, PT 0.8×ATR / SL 1.6×ATR)
- samples: 17,300
- up-barrier first: 64.0% | down-first: 35.9% | vertical: 0.0%
- median bars to resolve: 3

## Step 2 — HAR-RV
- OOS R² (log RV): HAR -0.141 vs random-walk -0.274 (HAR WINS) on 18 test days
- next-day annualized vol forecast: 34.2%

## Step 3 — Regime HMM (filtered, causal)
- occupancy: {p_trend: 39.9%, p_crisis: 38.3%, p_chop: 21.8%}
- variance ratio VR(10): mean 0.89
