"""Binance public-data adapter (REQUIREMENTS.md §2.4) — Linux-native, no keys.

DATA-ONLY in this phase: market data comes from Binance's public websocket and
REST endpoints, which require no account or API key. Execution methods raise
NotImplementedError until Phase 4 wires an authenticated venue (or
NautilusTrader, §15b) behind the same BrokerAdapter seam.

Crypto is natively T1+T2: the @trade stream carries the aggressor side, and
@depth20@100ms carries 20-level book snapshots — the full §7 feature set works
here from day one.

Streams per symbol (combined endpoint):
  <sym>@trade         — every trade: T (ms), p, q, m (isBuyerMaker)
                        m=true → buyer was passive → SELLER was the aggressor
  <sym>@bookTicker    — best bid/ask updates (quotes)
  <sym>@depth20@100ms — 20-level partial book snapshot every 100 ms
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Iterator

import requests

from .base import (
    DOM_ASK,
    DOM_BID,
    SIDE_BUY,
    SIDE_SELL,
    SIDE_UNKNOWN,
    BrokerError,
    Candle,
    DomLevel,
    SymbolSpec,
    Tick,
)

logger = logging.getLogger(__name__)

# Overridable for regional mirrors (api1/api2.binance.com) or other regions.
# Note: Binance geo-blocks some regions with HTTP 451 — pick the mirror that
# serves yours, or swap venue entirely; only these two constants change.
import os as _os

REST_BASE = _os.getenv("BINANCE_REST_BASE", "https://api.binance.com")
WS_BASE = _os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443/stream")


# ── pure parsers (unit-tested offline) ──────────────────────────────────────

def default_symbol_map(universe: tuple[str, ...] | list[str]) -> dict[str, str]:
    """BTCUSD → BTCUSDT etc. Override via CRYPTO_SYMBOL_MAP when needed."""
    return {k: k.replace("USD", "USDT") if not k.endswith("USDT") else k
            for k in universe}


def parse_trade(symbol_key: str, d: dict[str, Any],
                bid: float, ask: float) -> Tick:
    price = float(d["p"])
    # isBuyerMaker=true: the resting order was a buy → the aggressor SOLD
    side = SIDE_SELL if d.get("m") else SIDE_BUY
    return Tick(ts_ms=int(d["T"]), symbol_key=symbol_key,
                bid=bid or price, ask=ask or price,
                last=price, volume=float(d["q"]), side=side)


def parse_book_ticker(symbol_key: str, d: dict[str, Any],
                      ts_ms: int) -> Tick:
    """Quote update — a quotes-only Tick (last=None), like FX ticks."""
    return Tick(ts_ms=ts_ms, symbol_key=symbol_key,
                bid=float(d["b"]), ask=float(d["a"]),
                last=None, volume=None, side=SIDE_UNKNOWN)


def parse_depth20(d: dict[str, Any]) -> list[DomLevel]:
    levels = [DomLevel(side=DOM_BID, price=float(p), volume=float(q))
              for p, q in d.get("bids", [])]
    levels += [DomLevel(side=DOM_ASK, price=float(p), volume=float(q))
               for p, q in d.get("asks", [])]
    return levels


def parse_kline_row(row: list) -> Candle:
    """Binance kline: [openTime, o, h, l, c, volume, closeTime, quoteVol,
    nTrades, takerBuyBase, takerBuyQuote, ignore]"""
    return Candle(ts=int(row[0]) // 1000,
                  open=float(row[1]), high=float(row[2]),
                  low=float(row[3]), close=float(row[4]),
                  tick_volume=int(row[8]), spread_points=0,
                  real_volume=int(float(row[5])))


def spec_from_filters(broker_symbol: str, filters: list[dict]) -> SymbolSpec:
    fmap = {f["filterType"]: f for f in filters}
    tick = float(fmap.get("PRICE_FILTER", {}).get("tickSize", 0.01))
    lot = fmap.get("LOT_SIZE", {})
    return SymbolSpec(
        broker_symbol=broker_symbol,
        digits=max(0, f"{tick:.10f}".rstrip("0")[::-1].find(".")),
        point=tick, tick_size=tick,
        tick_value=tick,               # quote-ccy value of one tick per 1 unit
        contract_size=1.0,
        volume_min=float(lot.get("minQty", 0.0)),
        volume_max=float(lot.get("maxQty", 1e9)),
        volume_step=float(lot.get("stepSize", 0.0)),
        stops_level_points=0, spread_points=0,
        swap_long=0.0, swap_short=0.0, swap_triple_day=0,
        filling_modes=("IOC",), expiration_ts=0,
    )


# ── websocket worker ────────────────────────────────────────────────────────

class _WsWorker(threading.Thread):
    """One background asyncio loop reading the combined stream and fanning
    messages out to thread-safe queues. Reconnects with capped backoff."""

    def __init__(self, streams: list[str], on_message):
        super().__init__(name="binance-ws", daemon=True)
        self.url = f"{WS_BASE}?streams={'/'.join(streams)}"
        self.on_message = on_message
        self.stop_event = threading.Event()
        self.connected = threading.Event()

    def run(self) -> None:
        import asyncio
        import websockets

        async def _consume() -> None:
            backoff = 1.0
            while not self.stop_event.is_set():
                try:
                    async with websockets.connect(
                            self.url, ping_interval=20, max_size=2**22) as ws:
                        self.connected.set()
                        backoff = 1.0
                        async for raw in ws:
                            if self.stop_event.is_set():
                                return
                            self.on_message(json.loads(raw))
                except Exception as exc:
                    self.connected.clear()
                    logger.warning("binance ws dropped (%s); retry in %.0fs",
                                   exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)

        asyncio.run(_consume())

    def stop(self) -> None:
        self.stop_event.set()


# ── adapter ─────────────────────────────────────────────────────────────────

class BinanceDataAdapter:
    """Data subset of BrokerAdapter for Binance spot public feeds."""

    def __init__(self, symbol_map: dict[str, str], queue_max: int = 100_000):
        self._symbol_map = dict(symbol_map)          # key → BTCUSDT
        self._reverse = {v.lower(): k for k, v in symbol_map.items()}
        self._tick_q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._dom_q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._best: dict[str, tuple[float, float]] = {}   # key → (bid, ask)
        self._worker: _WsWorker | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def connect(self) -> None:
        streams = []
        for sym in self._symbol_map.values():
            s = sym.lower()
            streams += [f"{s}@trade", f"{s}@bookTicker", f"{s}@depth20@100ms"]
        self._worker = _WsWorker(streams, self._dispatch)
        self._worker.start()
        if not self._worker.connected.wait(timeout=20):
            raise BrokerError("binance websocket did not connect within 20s")
        logger.info("binance connected: %s", list(self._symbol_map))

    def shutdown(self) -> None:
        if self._worker:
            self._worker.stop()

    def is_connected(self) -> bool:
        return bool(self._worker and self._worker.connected.is_set())

    # ── dispatch ──────────────────────────────────────────────────────────

    def _dispatch(self, msg: dict) -> None:
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        sym, _, kind = stream.partition("@")
        key = self._reverse.get(sym)
        if key is None:
            return
        try:
            if kind == "trade":
                bid, ask = self._best.get(key, (0.0, 0.0))
                self._tick_q.put_nowait(parse_trade(key, data, bid, ask))
            elif kind == "bookTicker":
                bid, ask = float(data["b"]), float(data["a"])
                self._best[key] = (bid, ask)
                self._tick_q.put_nowait(
                    parse_book_ticker(key, data, ts_ms=int(time.time() * 1000)))
            elif kind.startswith("depth20"):
                self._dom_q.put_nowait((key, parse_depth20(data)))
        except queue.Full:
            logger.warning("drop: %s queue full (consumer too slow)", kind)

    # ── data API (BrokerAdapter subset) ───────────────────────────────────

    def symbol_spec(self, symbol_key: str) -> SymbolSpec:
        sym = self._symbol_map[symbol_key]
        resp = requests.get(f"{REST_BASE}/api/v3/exchangeInfo",
                            params={"symbol": sym}, timeout=10)
        resp.raise_for_status()
        info = resp.json()["symbols"][0]
        return spec_from_filters(sym, info["filters"])

    def find_symbols(self, pattern: str) -> list[str]:
        resp = requests.get(f"{REST_BASE}/api/v3/exchangeInfo", timeout=10)
        resp.raise_for_status()
        needle = pattern.strip("*").upper()
        return [s["symbol"] for s in resp.json()["symbols"]
                if needle in s["symbol"]][:20]

    def candles(self, symbol_key: str, timeframe_min: int, count: int,
                from_ts: int | None = None) -> list[Candle]:
        sym = self._symbol_map[symbol_key]
        interval = {1: "1m", 5: "5m", 15: "15m", 60: "1h",
                    1440: "1d"}[timeframe_min]
        out: list[Candle] = []
        start = from_ts * 1000 if from_ts else None
        while len(out) < count:
            params: dict = {"symbol": sym, "interval": interval,
                            "limit": min(1000, count - len(out))}
            if start:
                params["startTime"] = start
            resp = requests.get(f"{REST_BASE}/api/v3/klines",
                                params=params, timeout=10)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            out.extend(parse_kline_row(r) for r in rows)
            start = rows[-1][6] + 1        # next after last closeTime
            if len(rows) < params["limit"]:
                break
            time.sleep(0.15)               # stay far under REST rate limits
        return out[:count]

    def stream_ticks(self, symbol_keys: list[str]) -> Iterator[Tick]:
        wanted = set(symbol_keys)
        while True:
            t: Tick = self._tick_q.get()
            if t.symbol_key in wanted:
                yield t

    def stream_dom(self, symbol_keys: list[str]
                   ) -> Iterator[tuple[str, list[DomLevel]]]:
        wanted = set(symbol_keys)
        while True:
            key, levels = self._dom_q.get()
            if key in wanted:
                yield key, levels

    # ── execution: Phase 4 ────────────────────────────────────────────────

    def account(self):
        raise NotImplementedError("crypto execution is Phase 4 (§2.4/§15b)")

    market_order = modify_sltp = close_position = account
    positions = deals = account
