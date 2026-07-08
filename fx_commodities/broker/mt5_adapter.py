"""MetaTrader 5 implementation of BrokerAdapter (REQUIREMENTS.md §4.1, §1).

This is the ONLY file in the repository that may import MetaTrader5. The
package is an IPC bridge to a locally running MT5 terminal and is Windows-only;
it is imported lazily inside Mt5Adapter.__init__ so that every other module —
and the whole test suite — loads fine on Linux.

The MetaTrader5 package is not thread-safe: all calls are funneled through a
single worker thread (ThreadPoolExecutor(max_workers=1)).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, TypeVar

from common.retry import CircuitBreaker, with_retry

from .base import (
    DOM_ASK,
    DOM_BID,
    SIDE_BUY,
    SIDE_SELL,
    SIDE_UNKNOWN,
    AccountInfo,
    BrokerError,
    Candle,
    Deal,
    DomLevel,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# MT5 tick flag bits (mirrored so pure helpers are testable without the package)
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64

# Retcodes handled explicitly (§1.5)
RET_DONE = 10009
RET_REQUOTE = 10004
RET_INVALID_STOPS = 10016
RET_UNSUPPORTED_FILLING = 10030
RET_AUTOTRADING_DISABLED = 10027


def tick_side_from_flags(flags: int) -> int:
    """Pure helper: aggressor side from MT5 tick flags (testable on Linux)."""
    if flags & TICK_FLAG_BUY:
        return SIDE_BUY
    if flags & TICK_FLAG_SELL:
        return SIDE_SELL
    return SIDE_UNKNOWN


def normalize_tick(symbol_key: str, raw: dict[str, Any]) -> Tick:
    """Pure helper: MT5 tick record (as dict) → normalized Tick."""
    last = float(raw.get("last") or 0.0) or None
    vol_real = float(raw.get("volume_real") or 0.0)
    vol = vol_real if vol_real > 0 else (float(raw.get("volume") or 0.0) or None)
    return Tick(
        ts_ms=int(raw["time_msc"]),
        symbol_key=symbol_key,
        bid=float(raw["bid"]),
        ask=float(raw["ask"]),
        last=last,
        volume=vol if last is not None else None,
        side=tick_side_from_flags(int(raw.get("flags", 0))),
    )


class Mt5Adapter:
    def __init__(self, *, terminal_path: str, login: int, password: str,
                 server: str, symbol_map: dict[str, str],
                 magic: int = 520025,
                 poll_interval_ms: int = 200,
                 dom_snapshot_interval_ms: int = 500):
        self.magic = magic
        try:
            import MetaTrader5 as mt5  # noqa: N813
        except ImportError as exc:
            raise BrokerError(
                "MetaTrader5 package unavailable. It requires Windows with the "
                "MT5 terminal installed. On this machine only recorded-data / "
                "backtest workflows can run."
            ) from exc
        self._mt5 = mt5
        self._terminal_path = terminal_path
        self._login = login
        self._password = password
        self._server = server
        self._symbol_map = dict(symbol_map)          # key → broker symbol
        self._reverse_map = {v: k for k, v in symbol_map.items()}
        self._poll_interval_s = poll_interval_ms / 1000.0
        self._dom_interval_s = dom_snapshot_interval_ms / 1000.0
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._breaker = CircuitBreaker("mt5", failure_threshold=5, cooldown_s=30)
        self._stop_streams = False

    # ── plumbing ──────────────────────────────────────────────────────────

    def _call(self, fn: Callable[[], T]) -> T:
        """Run on the single MT5 worker thread."""
        return self._exec.submit(fn).result()

    def _sym(self, symbol_key: str) -> str:
        try:
            return self._symbol_map[symbol_key]
        except KeyError:
            raise BrokerError(f"Unknown symbol key '{symbol_key}' — not in SYMBOL_MAP")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def connect(self) -> None:
        mt5 = self._mt5

        def _init() -> None:
            kwargs: dict[str, Any] = {"timeout": 10_000}
            if self._terminal_path:
                kwargs["path"] = self._terminal_path
            if self._login:
                kwargs.update(login=self._login, password=self._password,
                              server=self._server)
            if not mt5.initialize(**kwargs):
                raise BrokerError(f"mt5.initialize failed: {mt5.last_error()}")
            for key, sym in self._symbol_map.items():
                if not mt5.symbol_select(sym, True):
                    raise BrokerError(
                        f"symbol_select failed for {key}={sym}: {mt5.last_error()}"
                    )

        with_retry(attempts=3, breaker=self._breaker)(
            lambda: self._call(_init)
        )()
        logger.info("MT5 connected: %s", self.account())

    def shutdown(self) -> None:
        self._stop_streams = True
        try:
            self._call(self._mt5.shutdown)
        finally:
            self._exec.shutdown(wait=False)

    def is_connected(self) -> bool:
        def _check() -> bool:
            ti = self._mt5.terminal_info()
            return bool(ti and ti.connected and self._mt5.account_info() is not None)
        try:
            return self._call(_check)
        except Exception:
            return False

    # ── account & symbols ─────────────────────────────────────────────────

    def account(self) -> AccountInfo:
        def _get() -> AccountInfo:
            ai = self._mt5.account_info()
            if ai is None:
                raise BrokerError(f"account_info failed: {self._mt5.last_error()}")
            return AccountInfo(
                login=ai.login, balance=ai.balance, equity=ai.equity,
                margin_free=ai.margin_free, currency=ai.currency,
                leverage=ai.leverage,
                is_demo=(ai.trade_mode == self._mt5.ACCOUNT_TRADE_MODE_DEMO),
                hedging=(ai.margin_mode
                         == self._mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING),
            )
        return self._call(_get)

    def symbol_spec(self, symbol_key: str) -> SymbolSpec:
        mt5 = self._mt5
        sym = self._sym(symbol_key)

        def _get() -> SymbolSpec:
            si = mt5.symbol_info(sym)
            if si is None:
                raise BrokerError(f"symbol_info({sym}) failed: {mt5.last_error()}")
            fillings: list[str] = []
            if si.filling_mode & 1:
                fillings.append("FOK")
            if si.filling_mode & 2:
                fillings.append("IOC")
            if not fillings:
                fillings.append("RETURN")
            return SymbolSpec(
                broker_symbol=sym, digits=si.digits, point=si.point,
                tick_size=si.trade_tick_size, tick_value=si.trade_tick_value,
                contract_size=si.trade_contract_size,
                volume_min=si.volume_min, volume_max=si.volume_max,
                volume_step=si.volume_step,
                stops_level_points=si.trade_stops_level,
                spread_points=si.spread,
                swap_long=si.swap_long, swap_short=si.swap_short,
                swap_triple_day=getattr(si, "swap_rollover3days", 2),
                filling_modes=tuple(fillings),
                expiration_ts=int(getattr(si, "expiration_time", 0) or 0),
            )
        return self._call(_get)

    def find_symbols(self, pattern: str) -> list[str]:
        def _get() -> list[str]:
            found = self._mt5.symbols_get(pattern)
            return [s.name for s in (found or [])]
        return self._call(_get)

    # ── market data ───────────────────────────────────────────────────────

    _TF_MAP = {1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15",
               30: "TIMEFRAME_M30", 60: "TIMEFRAME_H1", 1440: "TIMEFRAME_D1"}

    def candles(self, symbol_key: str, timeframe_min: int, count: int,
                from_ts: int | None = None) -> list[Candle]:
        mt5 = self._mt5
        sym = self._sym(symbol_key)
        tf = getattr(mt5, self._TF_MAP[timeframe_min])

        def _get() -> list[Candle]:
            if from_ts is not None:
                rates = mt5.copy_rates_from(
                    sym, tf, datetime.fromtimestamp(from_ts, tz=timezone.utc), count)
            else:
                rates = mt5.copy_rates_from_pos(sym, tf, 0, count)
            if rates is None:
                raise BrokerError(f"copy_rates failed for {sym}: {mt5.last_error()}")
            return [
                Candle(ts=int(r["time"]), open=float(r["open"]),
                       high=float(r["high"]), low=float(r["low"]),
                       close=float(r["close"]),
                       tick_volume=int(r["tick_volume"]),
                       spread_points=int(r["spread"]),
                       real_volume=int(r["real_volume"]))
                for r in rates
            ]
        return self._call(_get)

    def stream_ticks(self, symbol_keys: list[str]) -> Iterator[Tick]:
        """Round-robin poll copy_ticks_from with per-symbol time_msc watermarks."""
        mt5 = self._mt5
        watermarks: dict[str, int] = {
            k: int(time.time() * 1000) - 1000 for k in symbol_keys
        }
        self._stop_streams = False

        while not self._stop_streams:
            got_any = False
            for key in symbol_keys:
                sym = self._sym(key)
                wm = watermarks[key]

                def _poll(sym=sym, wm=wm):
                    return mt5.copy_ticks_from(
                        sym, datetime.fromtimestamp(wm / 1000.0, tz=timezone.utc),
                        10_000, mt5.COPY_TICKS_ALL)

                try:
                    raw = self._call(_poll)
                except Exception as exc:
                    logger.warning("tick poll failed for %s: %s", key, exc)
                    continue
                if raw is None or len(raw) == 0:
                    continue
                for r in raw:
                    ts_ms = int(r["time_msc"])
                    if ts_ms <= wm:
                        continue
                    got_any = True
                    yield normalize_tick(key, {n: r[n] for n in r.dtype.names})
                    watermarks[key] = max(watermarks[key], ts_ms)
            if not got_any:
                time.sleep(self._poll_interval_s)

    def stream_dom(self, symbol_keys: list[str]
                   ) -> Iterator[tuple[str, list[DomLevel]]]:
        mt5 = self._mt5
        syms = {k: self._sym(k) for k in symbol_keys}
        for sym in syms.values():
            self._call(lambda s=sym: mt5.market_book_add(s))
        self._stop_streams = False
        try:
            while not self._stop_streams:
                for key, sym in syms.items():
                    book = self._call(lambda s=sym: mt5.market_book_get(s))
                    if not book:
                        continue
                    levels = [
                        DomLevel(
                            side=DOM_BID if b.type == mt5.BOOK_TYPE_BUY else DOM_ASK,
                            price=b.price,
                            volume=b.volume_dbl if b.volume_dbl else float(b.volume),
                        )
                        for b in book
                        if b.type in (mt5.BOOK_TYPE_BUY, mt5.BOOK_TYPE_SELL)
                    ]
                    if levels:
                        yield key, levels
                time.sleep(self._dom_interval_s)
        finally:
            for sym in syms.values():
                try:
                    self._call(lambda s=sym: mt5.market_book_release(s))
                except Exception:
                    pass

    # ── trading ───────────────────────────────────────────────────────────

    def market_order(self, symbol_key: str, side: str, lots: float,
                     sl: float, tp: float, comment: str) -> OrderResult:
        mt5 = self._mt5
        sym = self._sym(symbol_key)
        spec = self.symbol_spec(symbol_key)
        filling = (mt5.ORDER_FILLING_IOC if "IOC" in spec.filling_modes
                   else mt5.ORDER_FILLING_FOK if "FOK" in spec.filling_modes
                   else mt5.ORDER_FILLING_RETURN)

        def _send(sl_=sl, tp_=tp, filling_=filling) -> Any:
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                raise BrokerError(f"no tick for {sym}")
            price = tick.ask if side == "BUY" else tick.bid
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": lots,
                "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                "price": price,
                "sl": sl_, "tp": tp_,
                "deviation": 20,
                "magic": self.magic,
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_,
            }
            chk = mt5.order_check(req)
            if chk is None or chk.retcode not in (0, RET_DONE):
                raise BrokerError(
                    f"order_check rejected: {chk.retcode if chk else 'None'} "
                    f"{getattr(chk, 'comment', '')}"
                )
            return mt5.order_send(req)

        res = self._call(_send)
        # Single, explicit second attempts per §11.2 retcode policy
        if res is not None and res.retcode == RET_REQUOTE:
            res = self._call(_send)                    # refreshed price inside
        elif res is not None and res.retcode == RET_INVALID_STOPS:
            widen = (spec.stops_level_points + 1) * spec.point
            if side == "BUY":
                res = self._call(lambda: _send(sl_=sl - widen, tp_=tp + widen))
            else:
                res = self._call(lambda: _send(sl_=sl + widen, tp_=tp - widen))
        elif res is not None and res.retcode == RET_UNSUPPORTED_FILLING:
            alt = (mt5.ORDER_FILLING_FOK if filling == mt5.ORDER_FILLING_IOC
                   else mt5.ORDER_FILLING_IOC)
            res = self._call(lambda: _send(filling_=alt))

        if res is None:
            return OrderResult(False, -1, None, None, None, None,
                               f"order_send returned None: {mt5.last_error()}")
        ok = res.retcode == RET_DONE
        return OrderResult(
            ok=ok, retcode=res.retcode,
            ticket=res.order or None, deal_id=res.deal or None,
            fill_price=res.price if ok else None,
            fill_volume=res.volume if ok else None,
            message=getattr(res, "comment", ""),
        )

    def modify_sltp(self, ticket: int, sl: float, tp: float) -> OrderResult:
        mt5 = self._mt5

        def _send() -> Any:
            pos = mt5.positions_get(ticket=ticket)
            if not pos:
                raise BrokerError(f"position {ticket} not found")
            req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                   "symbol": pos[0].symbol, "sl": sl, "tp": tp}
            return mt5.order_send(req)

        res = self._call(_send)
        ok = res is not None and res.retcode == RET_DONE
        return OrderResult(ok, res.retcode if res else -1, ticket, None, None,
                           None, getattr(res, "comment", "") if res else "")

    def close_position(self, ticket: int, lots: float | None = None) -> OrderResult:
        mt5 = self._mt5

        def _send() -> Any:
            pos_list = mt5.positions_get(ticket=ticket)
            if not pos_list:
                raise BrokerError(f"position {ticket} not found")
            pos = pos_list[0]
            volume = lots if lots is not None else pos.volume
            is_long = pos.type == mt5.POSITION_TYPE_BUY
            tick = mt5.symbol_info_tick(pos.symbol)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": ticket,
                "symbol": pos.symbol,
                "volume": volume,
                "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if is_long else tick.ask,
                "deviation": 20,
                "magic": pos.magic,
                "comment": "fxc-close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            return mt5.order_send(req)

        res = self._call(_send)
        ok = res is not None and res.retcode == RET_DONE
        return OrderResult(ok, res.retcode if res else -1, ticket,
                           res.deal if ok else None,
                           res.price if ok else None,
                           res.volume if ok else None,
                           getattr(res, "comment", "") if res else "")

    def positions(self, magic: int | None = None) -> list[Position]:
        mt5 = self._mt5

        def _get() -> list[Position]:
            raw = mt5.positions_get() or []
            out = []
            for p in raw:
                if magic is not None and p.magic != magic:
                    continue
                out.append(Position(
                    ticket=p.ticket,
                    symbol_key=self._reverse_map.get(p.symbol, p.symbol),
                    broker_symbol=p.symbol,
                    direction="LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
                    lots=p.volume, price_open=p.price_open,
                    sl=p.sl, tp=p.tp, profit=p.profit, swap=p.swap,
                    magic=p.magic, comment=p.comment,
                ))
            return out
        return self._call(_get)

    def deals(self, from_ts_ms: int, to_ts_ms: int,
              magic: int | None = None) -> list[Deal]:
        mt5 = self._mt5

        def _get() -> list[Deal]:
            raw = mt5.history_deals_get(
                datetime.fromtimestamp(from_ts_ms / 1000, tz=timezone.utc),
                datetime.fromtimestamp(to_ts_ms / 1000, tz=timezone.utc),
            ) or []
            out = []
            for d in raw:
                if magic is not None and d.magic != magic:
                    continue
                out.append(Deal(
                    deal_id=d.ticket, ticket=d.position_id,
                    symbol_key=self._reverse_map.get(d.symbol, d.symbol),
                    side="BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                    lots=d.volume, price=d.price,
                    commission=d.commission, swap=d.swap, profit=d.profit,
                    ts_ms=int(d.time_msc), magic=d.magic, comment=d.comment,
                ))
            return out
        return self._call(_get)
