"""Backfill 1m OHLCV from OKX public REST into the Parquet lake.

    python -m fx_commodities.data.backfill_public --days 180 \
        --symbols BTCUSD,ETHUSD

Why OKX: no API key needed, deep 1m history via /market/history-candles, and
not geo-blocked from most regions (Binance returns 451 in some). The output
lands in the same `ohlcv` table the Store/backtester reads, keyed by our
canonical symbols.

Volume caveat (documented, not hidden): OKX candles carry base-asset and
quote-asset volume but no trade count. We store quote volume (whole USD) in
BOTH tick_volume and real_volume — features built on tick_volume remain
meaningful as relative-activity measures, but they are volume, not tick
counts.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

import requests

from ..broker.base import Candle
from ..config import load_settings
from .store import Store

logger = logging.getLogger(__name__)

OKX = "https://www.okx.com/api/v5/market/history-candles"
OKX_MAP = {"BTCUSD": "BTC-USDT", "ETHUSD": "ETH-USDT", "SOLUSD": "SOL-USDT"}
UA = {"User-Agent": "hypipe-backfill/1.0"}


def fetch_page(inst: str, before_ms: int) -> list[Candle]:
    r = requests.get(OKX, params={"instId": inst, "bar": "1m",
                                  "after": str(before_ms), "limit": "100"},
                     headers=UA, timeout=15)
    r.raise_for_status()
    rows = r.json().get("data", [])
    out = []
    for ts, o, h, l, c, vol, vol_ccy, vol_quote, confirm in rows:
        if confirm != "1":            # skip the still-forming candle
            continue
        qv = int(float(vol_quote))
        out.append(Candle(ts=int(ts) // 1000, open=float(o), high=float(h),
                          low=float(l), close=float(c),
                          tick_volume=qv, spread_points=0, real_volume=qv))
    return out                        # newest-first per OKX convention


def backfill(store: Store, key: str, days: int) -> int:
    inst = OKX_MAP[key]
    target_start = int(time.time()) - days * 86400
    cursor_ms = int(time.time() * 1000)
    total, batch = 0, []
    existing = store._existing_ts(key)

    while True:
        try:
            page = fetch_page(inst, cursor_ms)
        except Exception as exc:
            logger.warning("%s page failed (%s) — backing off 5s", key, exc)
            time.sleep(5)
            continue
        if not page:
            break
        fresh = [c for c in page if c.ts not in existing]
        batch.extend(fresh)
        existing.update(c.ts for c in fresh)
        oldest = page[-1].ts
        cursor_ms = oldest * 1000
        if len(batch) >= 20_000:
            store._write_ohlcv(key, batch)
            total += len(batch)
            logger.info("%s: %d bars written (back to %s)", key, total,
                        datetime.fromtimestamp(oldest, tz=timezone.utc).date())
            batch = []
        if oldest <= target_start:
            break
        time.sleep(0.12)              # ~8 req/s, well under OKX's 20/2s

    if batch:
        store._write_ohlcv(key, batch)
        total += len(batch)
    logger.info("%s backfill complete: %d new bars", key, total)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD")
    args = ap.parse_args()

    store = Store(load_settings().data_root)
    for key in [s.strip().upper() for s in args.symbols.split(",")]:
        backfill(store, key, args.days)


if __name__ == "__main__":
    main()
