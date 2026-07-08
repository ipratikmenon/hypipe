"""Tick + DOM recorder (REQUIREMENTS.md §4.3).

Standalone always-on process:

    python -m fx_commodities.data.recorder

Consumes the adapter's tick stream (all symbols) and DOM stream (symbols with
has_dom), buffers in memory, and flushes Parquet part-files every
FLUSH_INTERVAL_S. Crash-safe: each flush is an independent immutable file;
files with invalid footers are quarantined at startup.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from common.alerts import alert

from ..broker.base import BrokerAdapter, DomLevel, Tick
from . import schemas

logger = logging.getLogger(__name__)


def utc_date_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class ParquetSink:
    """Buffers rows per (table, symbol, utc-date) and flushes atomic part files."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self._buffers: dict[tuple[str, str, str], list[dict]] = {}
        self._lock = threading.Lock()
        self.rows_written = 0

    def add(self, table: str, symbol_key: str, ts_ms: int, row: dict) -> None:
        key = (table, symbol_key, utc_date_str(ts_ms))
        with self._lock:
            self._buffers.setdefault(key, []).append(row)

    def flush(self) -> int:
        with self._lock:
            buffers, self._buffers = self._buffers, {}
        written = 0
        for (table, symbol, date), rows in buffers.items():
            if not rows:
                continue
            out_dir = self.data_root / table / f"symbol={symbol}" / f"date={date}"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")
            path = out_dir / f"part-{stamp}.parquet"
            tmp = path.with_suffix(".parquet.tmp")
            table_obj = pa.Table.from_pylist(rows, schema=schemas.TABLES[table])
            pq.write_table(table_obj, tmp)
            tmp.rename(path)          # atomic publish — readers never see partials
            written += len(rows)
        self.rows_written += written
        return written

    def quarantine_corrupt(self) -> int:
        """Validate footers of existing part files; move unreadable ones aside."""
        bad = 0
        for path in self.data_root.rglob("part-*.parquet"):
            try:
                pq.read_metadata(path)
            except Exception:
                corrupt_dir = self.data_root / "_corrupt"
                corrupt_dir.mkdir(exist_ok=True)
                path.rename(corrupt_dir / path.name)
                bad += 1
        for tmp in self.data_root.rglob("*.parquet.tmp"):
            tmp.unlink()               # incomplete writes from a crash
        if bad:
            alert("WARN", "quarantined corrupt parquet part files", count=bad)
        return bad


class Recorder:
    def __init__(self, adapter: BrokerAdapter, tick_symbols: list[str],
                 dom_symbols: list[str], data_root: Path,
                 flush_interval_s: int = 60,
                 heartbeat_path: Path | None = None):
        self.adapter = adapter
        self.tick_symbols = tick_symbols
        self.dom_symbols = dom_symbols
        self.sink = ParquetSink(data_root)
        self.flush_interval_s = flush_interval_s
        self.heartbeat_path = heartbeat_path or (Path(data_root) / "heartbeat.json")
        self._stop = threading.Event()
        self._last_tick_at: dict[str, float] = {}

    # ── consumers ─────────────────────────────────────────────────────────

    def on_tick(self, t: Tick) -> None:
        self._last_tick_at[t.symbol_key] = time.monotonic()
        self.sink.add("ticks", t.symbol_key, t.ts_ms, {
            "ts_ms": t.ts_ms, "symbol_key": t.symbol_key,
            "bid": t.bid, "ask": t.ask,
            "last": t.last if t.last is not None else float("nan"),
            "volume": t.volume if t.volume is not None else float("nan"),
            "side": t.side,
        })

    def on_dom(self, symbol_key: str, levels: list[DomLevel]) -> None:
        ts_ms = int(time.time() * 1000)
        bids = sorted((l for l in levels if l.side > 0),
                      key=lambda l: -l.price)
        asks = sorted((l for l in levels if l.side < 0),
                      key=lambda l: l.price)
        for side_levels in (bids, asks):
            for i, l in enumerate(side_levels):
                self.sink.add("dom", symbol_key, ts_ms, {
                    "ts_ms": ts_ms, "symbol_key": symbol_key,
                    "side": l.side, "level": i,
                    "price": l.price, "volume": l.volume,
                })

    # ── threads ───────────────────────────────────────────────────────────

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for t in self.adapter.stream_ticks(self.tick_symbols):
                    self.on_tick(t)
                    if self._stop.is_set():
                        break
            except Exception as exc:
                alert("WARN", "tick stream died — restarting", error=str(exc))
                time.sleep(5)

    def _dom_loop(self) -> None:
        if not self.dom_symbols:
            return
        while not self._stop.is_set():
            try:
                for key, levels in self.adapter.stream_dom(self.dom_symbols):
                    self.on_dom(key, levels)
                    if self._stop.is_set():
                        break
            except Exception as exc:
                alert("WARN", "dom stream died — restarting", error=str(exc))
                time.sleep(5)

    def _flush_loop(self) -> None:
        last_heartbeat = 0.0
        while not self._stop.is_set():
            self._stop.wait(self.flush_interval_s)
            n = self.sink.flush()
            logger.debug("flushed %d rows", n)
            now = time.monotonic()
            # silent-symbol watchdog (§4.3): FX is never quiet for 120 s
            for sym in self.tick_symbols:
                seen = self._last_tick_at.get(sym)
                if seen and now - seen > 120:
                    alert("WARN", "symbol silent > 120s", symbol=sym)
            if now - last_heartbeat > 300:
                last_heartbeat = now
                self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rows_written": self.sink.rows_written,
            "last_tick_age_s": {
                sym: round(time.monotonic() - t, 1)
                for sym, t in self._last_tick_at.items()
            },
        }
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(json.dumps(payload, indent=2))
        logger.info("heartbeat: %d rows total", self.sink.rows_written)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self.sink.quarantine_corrupt()
        threads = [
            threading.Thread(target=self._tick_loop, name="ticks", daemon=True),
            threading.Thread(target=self._dom_loop, name="dom", daemon=True),
            threading.Thread(target=self._flush_loop, name="flush", daemon=True),
        ]
        for t in threads:
            t.start()
        logger.info("recorder running: ticks=%s dom=%s",
                    self.tick_symbols, self.dom_symbols)
        try:
            while not self._stop.is_set():
                time.sleep(1)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.sink.flush()
        logger.info("recorder stopped; final flush complete")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s %(message)s")
    from ..broker.mt5_adapter import Mt5Adapter
    from ..config import load_settings
    from ..instruments import load_capabilities, resolve_universe

    cfg = load_settings()
    cfg.require_broker_credentials()
    adapter = Mt5Adapter(
        terminal_path=cfg.mt5_terminal_path, login=cfg.mt5_login,
        password=cfg.mt5_password, server=cfg.mt5_server,
        symbol_map=cfg.symbol_map, magic=cfg.magic,
        poll_interval_ms=cfg.poll_interval_ms,
        dom_snapshot_interval_ms=cfg.dom_snapshot_interval_ms,
    )
    adapter.connect()
    caps = load_capabilities(cfg.reports_dir / "capabilities.json")
    contracts = resolve_universe(adapter, cfg.universe, cfg.symbol_map, caps)

    recorder = Recorder(
        adapter=adapter,
        tick_symbols=[c.key for c in contracts],
        dom_symbols=[c.key for c in contracts if c.has_dom],
        data_root=cfg.data_root,
        flush_interval_s=cfg.flush_interval_s,
    )
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop())
    recorder.run()


if __name__ == "__main__":
    main()
