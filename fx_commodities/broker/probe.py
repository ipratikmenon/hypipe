"""Broker capability probe (REQUIREMENTS.md §4.0).

Run once per broker (on the Windows machine with the terminal up):

    python -m fx_commodities.broker.probe

Writes reports/capabilities.json recording, per symbol: DOM availability,
last-trade tick flags, real volume, tick/M1 history depth, spread snapshot,
and allowed filling modes. Feature tiers (§7.0) are gated on this file.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fx_commodities.config import load_settings

logger = logging.getLogger(__name__)


def probe_symbol(mt5, broker_symbol: str) -> dict:
    out: dict = {"broker_symbol": broker_symbol}

    si = mt5.symbol_info(broker_symbol)
    if si is None:
        out["error"] = f"symbol_info failed: {mt5.last_error()}"
        return out
    out["median_spread_points"] = si.spread
    fillings = []
    if si.filling_mode & 1:
        fillings.append("FOK")
    if si.filling_mode & 2:
        fillings.append("IOC")
    out["filling_modes"] = fillings or ["RETURN"]
    out["expiration_ts"] = int(getattr(si, "expiration_time", 0) or 0)

    # DOM?
    has_dom = False
    if mt5.market_book_add(broker_symbol):
        time.sleep(1.0)
        book = mt5.market_book_get(broker_symbol)
        has_dom = bool(book)
        mt5.market_book_release(broker_symbol)
    out["has_dom"] = has_dom

    # Trade ticks / real volume? Sample last 3 days of ticks.
    now = datetime.now(timezone.utc)
    ticks = mt5.copy_ticks_range(
        broker_symbol, now - timedelta(days=3), now, mt5.COPY_TICKS_ALL)
    has_last = False
    has_real_vol = False
    if ticks is not None and len(ticks):
        flags = ticks["flags"]
        has_last = bool(((flags & 8) | (flags & 32) | (flags & 64)).any())
        has_real_vol = bool((ticks["volume_real"] > 0).any())
    out["has_last_ticks"] = has_last
    out["has_real_volume"] = has_real_vol

    # History depth: how far back do ticks / M1 candles actually go?
    out["tick_history_days"] = _history_days(
        lambda d: mt5.copy_ticks_range(
            broker_symbol, now - timedelta(days=d),
            now - timedelta(days=d - 1), mt5.COPY_TICKS_ALL))
    out["m1_history_days"] = _history_days(
        lambda d: mt5.copy_rates_range(
            broker_symbol, mt5.TIMEFRAME_M1,
            now - timedelta(days=d), now - timedelta(days=d - 1)))
    return out


def _history_days(fetch, max_days: int = 3650) -> int:
    """Binary-search the earliest day with data available."""
    lo, hi = 1, max_days
    if _has_data(fetch, hi):
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _has_data(fetch, mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _has_data(fetch, days: int) -> bool:
    try:
        rows = fetch(days)
        return rows is not None and len(rows) > 0
    except Exception:
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_settings()
    try:
        import MetaTrader5 as mt5
    except ImportError:
        logger.error("MetaTrader5 package not available — run the probe on the "
                     "Windows machine that hosts the terminal.")
        return 2

    kwargs = {"timeout": 10_000}
    if cfg.mt5_terminal_path:
        kwargs["path"] = cfg.mt5_terminal_path
    if cfg.mt5_login:
        kwargs.update(login=cfg.mt5_login, password=cfg.mt5_password,
                      server=cfg.mt5_server)
    if not mt5.initialize(**kwargs):
        logger.error("initialize failed: %s", mt5.last_error())
        return 1

    results: dict[str, dict] = {}
    try:
        for key in cfg.universe:
            sym = cfg.symbol_map.get(key)
            if not sym:
                results[key] = {"error": "missing from SYMBOL_MAP"}
                continue
            mt5.symbol_select(sym, True)
            time.sleep(0.5)
            logger.info("probing %s (%s)…", key, sym)
            results[key] = probe_symbol(mt5, sym)
    finally:
        mt5.shutdown()

    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.reports_dir / "capabilities.json"
    out_path.write_text(json.dumps(results, indent=2))
    logger.info("wrote %s", out_path)
    for key, caps in results.items():
        logger.info(
            "%-8s dom=%s last_ticks=%s real_vol=%s tick_days=%s m1_days=%s",
            key, caps.get("has_dom"), caps.get("has_last_ticks"),
            caps.get("has_real_volume"), caps.get("tick_history_days"),
            caps.get("m1_history_days"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
