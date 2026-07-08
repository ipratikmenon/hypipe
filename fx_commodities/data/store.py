"""Query layer over the Parquet lake + M1 backfill (REQUIREMENTS.md §4.4).

All readers return UTC tz-aware pandas DataFrames. Backfill is incremental and
idempotent: existing (symbol, ts) bars are never duplicated.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pyarrow as pa

from ..broker.base import BrokerAdapter
from . import schemas

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)

    # ── readers ───────────────────────────────────────────────────────────

    def _read(self, table: str, symbol_key: str,
              date_from: date, date_to: date) -> pd.DataFrame:
        base = self.data_root / table / f"symbol={symbol_key}"
        if not base.exists():
            return pd.DataFrame()
        parts: list[str] = []
        d = date_from
        while d <= date_to:
            p = base / f"date={d.isoformat()}"
            if p.exists():
                parts.extend(str(f) for f in sorted(p.glob("part-*.parquet")))
            d += timedelta(days=1)
        if not parts:
            return pd.DataFrame()
        dataset = ds.dataset(parts, format="parquet")
        df = dataset.to_table().to_pandas()
        ts_col = "ts_ms" if "ts_ms" in df.columns else "ts"
        unit = "ms" if ts_col == "ts_ms" else "s"
        df["ts_utc"] = pd.to_datetime(df[ts_col], unit=unit, utc=True)
        return df.sort_values(ts_col).reset_index(drop=True)

    def read_ticks(self, symbol_key: str, date_from: date,
                   date_to: date) -> pd.DataFrame:
        return self._read("ticks", symbol_key, date_from, date_to)

    def read_dom(self, symbol_key: str, date_from: date,
                 date_to: date) -> pd.DataFrame:
        return self._read("dom", symbol_key, date_from, date_to)

    def read_ohlcv(self, symbol_key: str, date_from: date,
                   date_to: date) -> pd.DataFrame:
        return self._read("ohlcv", symbol_key, date_from, date_to)

    # ── spread report (Phase 1 gate + D1 universe decision) ──────────────

    def spread_report(self, symbol_keys: list[str], date_from: date,
                      date_to: date) -> pd.DataFrame:
        """Median relative spread per symbol per UTC hour, from recorded ticks."""
        rows = []
        for key in symbol_keys:
            ticks = self.read_ticks(key, date_from, date_to)
            if ticks.empty:
                continue
            mid = (ticks["bid"] + ticks["ask"]) / 2
            rel_spread_pct = (ticks["ask"] - ticks["bid"]) / mid * 100
            hours = ticks["ts_utc"].dt.hour
            grouped = rel_spread_pct.groupby(hours).median()
            for hour, med in grouped.items():
                rows.append({"symbol_key": key, "utc_hour": int(hour),
                             "median_spread_pct": float(med)})
            rows.append({"symbol_key": key, "utc_hour": -1,     # -1 = all hours
                         "median_spread_pct": float(rel_spread_pct.median())})
        return pd.DataFrame(rows)

    # ── backfill ──────────────────────────────────────────────────────────

    def backfill_m1(self, adapter: BrokerAdapter, symbol_key: str,
                    years: int = 3, chunk_bars: int = 50_000) -> int:
        """Pull M1 candles back `years` and merge into the ohlcv table."""
        existing = self._existing_ts(symbol_key)
        cutoff = datetime.now(timezone.utc) - timedelta(days=365 * years)
        from_ts = int(cutoff.timestamp())
        total_new = 0

        while True:
            candles = adapter.candles(symbol_key, 1, chunk_bars, from_ts=from_ts)
            if not candles:
                break
            fresh = [c for c in candles if c.ts not in existing]
            if fresh:
                self._write_ohlcv(symbol_key, fresh)
                existing.update(c.ts for c in fresh)
                total_new += len(fresh)
            last_ts = candles[-1].ts
            if last_ts <= from_ts or len(candles) < chunk_bars:
                break
            from_ts = last_ts + 60
        logger.info("backfill %s: %d new M1 bars", symbol_key, total_new)
        return total_new

    def _existing_ts(self, symbol_key: str) -> set[int]:
        base = self.data_root / "ohlcv" / f"symbol={symbol_key}"
        if not base.exists():
            return set()
        dataset = ds.dataset(str(base), format="parquet")
        return set(dataset.to_table(columns=["ts"])["ts"].to_pylist())

    def _write_ohlcv(self, symbol_key: str, candles) -> None:
        by_date: dict[str, list[dict]] = {}
        for c in candles:
            d = datetime.fromtimestamp(c.ts, tz=timezone.utc).strftime("%Y-%m-%d")
            by_date.setdefault(d, []).append({
                "ts": c.ts, "symbol_key": symbol_key, "tf_min": 1,
                "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                "tick_volume": c.tick_volume, "spread_points": c.spread_points,
                "real_volume": c.real_volume,
            })
        for d, rows in by_date.items():
            out_dir = self.data_root / "ohlcv" / f"symbol={symbol_key}" / f"date={d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
            table = pa.Table.from_pylist(rows, schema=schemas.OHLCV)
            pq.write_table(table, out_dir / f"part-{stamp}.parquet")
