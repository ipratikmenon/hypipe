"""Crypto recorder entry point — Linux-native, no broker account needed.

    python -m fx_commodities.data.record_crypto

Records BTC/ETH (config CRYPTO_UNIVERSE) trades + quotes + 20-level depth from
Binance public streams into the same Parquet lake the MT5 recorder uses. Also
writes reports/capabilities.json entries: crypto is T1+T2 natively.
"""

from __future__ import annotations

import json
import logging
import os
import signal

from ..broker.crypto_adapter import BinanceDataAdapter, default_symbol_map
from ..config import load_settings
from .recorder import Recorder

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s %(message)s")
    cfg = load_settings()

    universe = tuple(
        s.strip().upper()
        for s in os.getenv("CRYPTO_UNIVERSE", "BTCUSD,ETHUSD").split(",")
        if s.strip()
    )
    raw_map = os.getenv("CRYPTO_SYMBOL_MAP", "")
    if raw_map:
        symbol_map = dict(p.split("=", 1) for p in raw_map.split(","))
    else:
        symbol_map = default_symbol_map(universe)

    adapter = BinanceDataAdapter(symbol_map)
    adapter.connect()

    # crypto capabilities are intrinsic — record them for the feature tiers
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    caps_path = cfg.reports_dir / "capabilities.json"
    caps = json.loads(caps_path.read_text()) if caps_path.exists() else {}
    for key in universe:
        caps[key] = {"broker_symbol": symbol_map[key], "has_dom": True,
                     "has_last_ticks": True, "has_real_volume": True,
                     "venue": "binance-public"}
    caps_path.write_text(json.dumps(caps, indent=2))

    recorder = Recorder(
        adapter=adapter,
        tick_symbols=list(universe),
        dom_symbols=list(universe),
        data_root=cfg.data_root,
        flush_interval_s=cfg.flush_interval_s,
    )
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop())
    logger.info("crypto recorder starting: %s", universe)
    recorder.run()


if __name__ == "__main__":
    main()
