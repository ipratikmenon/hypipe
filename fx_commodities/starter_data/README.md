# Starter dataset — 60 days of 1m BTC/ETH (OKX public)

Snapshot committed 2026-07-10 so Phase 3 development can start with zero
setup: 86,499 one-minute OHLCV bars per symbol (2026-05-11 → 2026-07-10),
fetched via `data/backfill_public.py`.

Use it by pointing the store at this directory:

    DATA_ROOT=./fx_commodities/starter_data python -m fx_commodities.model.demo_real_data --symbol BTCUSD

Or copy `ohlcv/` into your live `data_root/`. Extend history any time with:

    python -m fx_commodities.data.backfill_public --days 365 --symbols BTCUSD,ETHUSD

Caveats: volume columns are OKX quote volume (USD), not trade counts; this
is candle data only — orderflow/DOM features require the live recorder
(`data/record_crypto.py`), which must run on an always-on machine.
The included `real_data_demo_*.md` show the Phase 3 Steps 1–3 pipeline run
against this exact data.
