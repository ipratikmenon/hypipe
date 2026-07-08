"""Tests for the Parquet sink, store readers, spread report, and backfill."""

from datetime import date

import pandas as pd
import pytest

from fx_commodities.broker.base import Candle, Tick
from fx_commodities.data.recorder import ParquetSink, Recorder, utc_date_str
from fx_commodities.data.store import Store


TS_MS = 1_751_961_600_123          # 2025-07-08 08:00:00.123 UTC
DAY = date(2025, 7, 8)


def make_tick(ts_ms=TS_MS, key="EURUSD", bid=1.085, ask=1.0851) -> Tick:
    return Tick(ts_ms=ts_ms, symbol_key=key, bid=bid, ask=ask,
                last=None, volume=None, side=0)


class TestSink:
    def test_tick_roundtrip(self, tmp_path):
        sink = ParquetSink(tmp_path)
        rec = Recorder.__new__(Recorder)          # only need on_tick + sink
        rec.sink = sink
        rec._last_tick_at = {}
        for i in range(10):
            rec.on_tick(make_tick(ts_ms=TS_MS + i * 1000))
        assert sink.flush() == 10

        store = Store(tmp_path)
        df = store.read_ticks("EURUSD", DAY, DAY)
        assert len(df) == 10
        assert df["bid"].iloc[0] == pytest.approx(1.085)
        assert str(df["ts_utc"].dt.tz) == "UTC"

    def test_partitioning_by_symbol_and_utc_date(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.add("ticks", "EURUSD", TS_MS, _tick_row(TS_MS, "EURUSD"))
        next_day = TS_MS + 86_400_000
        sink.add("ticks", "EURUSD", next_day, _tick_row(next_day, "EURUSD"))
        sink.add("ticks", "XAUUSD", TS_MS, _tick_row(TS_MS, "XAUUSD"))
        sink.flush()
        dirs = {str(p.relative_to(tmp_path)).rsplit("/", 1)[0]
                for p in tmp_path.rglob("part-*.parquet")}
        assert f"ticks/symbol=EURUSD/date={utc_date_str(TS_MS)}" in dirs
        assert f"ticks/symbol=EURUSD/date={utc_date_str(next_day)}" in dirs
        assert f"ticks/symbol=XAUUSD/date={utc_date_str(TS_MS)}" in dirs

    def test_corrupt_file_quarantined(self, tmp_path):
        sink = ParquetSink(tmp_path)
        bad_dir = tmp_path / "ticks" / "symbol=EURUSD" / "date=2025-07-08"
        bad_dir.mkdir(parents=True)
        (bad_dir / "part-bad.parquet").write_bytes(b"not parquet at all")
        assert sink.quarantine_corrupt() == 1
        assert (tmp_path / "_corrupt" / "part-bad.parquet").exists()


def _tick_row(ts_ms: int, key: str) -> dict:
    return {"ts_ms": ts_ms, "symbol_key": key, "bid": 1.0, "ask": 1.0001,
            "last": float("nan"), "volume": float("nan"), "side": 0}


class TestSpreadReport:
    def test_median_spread_per_symbol(self, tmp_path):
        sink = ParquetSink(tmp_path)
        rec = Recorder.__new__(Recorder)
        rec.sink = sink
        rec._last_tick_at = {}
        # EURUSD tight spread, WTI wide spread
        for i in range(50):
            rec.on_tick(make_tick(ts_ms=TS_MS + i * 1000, key="EURUSD",
                                  bid=1.08500, ask=1.08506))
            rec.on_tick(make_tick(ts_ms=TS_MS + i * 1000, key="WTI",
                                  bid=82.00, ask=82.08))
        sink.flush()

        report = Store(tmp_path).spread_report(["EURUSD", "WTI"], DAY, DAY)
        overall = report[report["utc_hour"] == -1].set_index("symbol_key")
        eur = overall.loc["EURUSD", "median_spread_pct"]
        wti = overall.loc["WTI", "median_spread_pct"]
        assert eur == pytest.approx(0.00553, rel=0.01)
        assert wti == pytest.approx(0.0975, rel=0.01)
        # D1 rule: EURUSD passes the 0.03% FX gate, WTI fails the 0.06% gate
        assert eur <= 0.03 and wti > 0.06


class FakeCandleAdapter:
    """Serves deterministic M1 candles for backfill tests."""

    def __init__(self, start_ts: int, n: int):
        self.all = [
            Candle(ts=start_ts + i * 60, open=1.0, high=1.1, low=0.9,
                   close=1.05, tick_volume=10, spread_points=5, real_volume=0)
            for i in range(n)
        ]

    def candles(self, key, tf_min, count, from_ts=None):
        rows = [c for c in self.all if from_ts is None or c.ts >= from_ts]
        return rows[:count]


class TestBackfill:
    def test_incremental_and_idempotent(self, tmp_path):
        import time as _time
        start = int(_time.time()) - 5 * 86400
        adapter = FakeCandleAdapter(start_ts=start, n=300)
        store = Store(tmp_path)
        n1 = store.backfill_m1(adapter, "EURUSD", years=1, chunk_bars=100)
        assert n1 == 300
        n2 = store.backfill_m1(adapter, "EURUSD", years=1, chunk_bars=100)
        assert n2 == 0     # idempotent — nothing duplicated
