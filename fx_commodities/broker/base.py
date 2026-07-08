"""Broker abstraction (REQUIREMENTS.md §4.2, constraint D4).

Strategy, risk, backtest, and data code import ONLY the types in this file.
`MetaTrader5` may be imported by mt5_adapter.py alone — this keeps everything
except the adapter OS-independent and lets other brokers (OANDA, cTrader,
IBKR) plug in later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, NamedTuple, Protocol, runtime_checkable

# tick.side values
SIDE_BUY = 1
SIDE_SELL = -1
SIDE_UNKNOWN = 0

# DomLevel.side values
DOM_BID = 1
DOM_ASK = -1


class Tick(NamedTuple):
    ts_ms: int              # broker/exchange time, epoch ms
    symbol_key: str         # canonical key ("EURUSD"), not broker symbol
    bid: float
    ask: float
    last: float | None      # None when broker feed has no trades (quotes only)
    volume: float | None    # None when no trade volume available
    side: int               # SIDE_BUY / SIDE_SELL / SIDE_UNKNOWN

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class DomLevel(NamedTuple):
    side: int               # DOM_BID / DOM_ASK
    price: float
    volume: float


class Candle(NamedTuple):
    ts: int                 # epoch seconds, bar open time
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread_points: int
    real_volume: int


class OrderResult(NamedTuple):
    ok: bool
    retcode: int
    ticket: int | None      # position/order ticket
    deal_id: int | None
    fill_price: float | None
    fill_volume: float | None
    message: str


class Position(NamedTuple):
    ticket: int
    symbol_key: str
    broker_symbol: str
    direction: str          # "LONG" | "SHORT"
    lots: float
    price_open: float
    sl: float
    tp: float
    profit: float           # floating PnL, account currency
    swap: float
    magic: int
    comment: str


class Deal(NamedTuple):
    deal_id: int
    ticket: int             # position ticket this deal belongs to
    symbol_key: str
    side: str               # "BUY" | "SELL"
    lots: float
    price: float
    commission: float
    swap: float
    profit: float
    ts_ms: int
    magic: int
    comment: str


class AccountInfo(NamedTuple):
    login: int
    balance: float
    equity: float
    margin_free: float
    currency: str
    leverage: int
    is_demo: bool
    hedging: bool           # True = hedging mode, False = netting


@dataclass(frozen=True)
class SymbolSpec:
    """Static per-symbol contract terms from the broker."""
    broker_symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float       # account-currency value of one tick per 1.0 lot
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int  # min SL/TP distance the broker enforces
    spread_points: int       # current spread snapshot
    swap_long: float
    swap_short: float
    swap_triple_day: int     # 0=Mon..4=Fri per broker convention
    filling_modes: tuple[str, ...]  # subset of ("IOC", "FOK", "RETURN")
    expiration_ts: int       # 0 for spot/CFD; futures-based CFDs carry expiry


class BrokerError(RuntimeError):
    """Adapter-level failure (terminal down, request rejected pre-trade)."""


@runtime_checkable
class BrokerAdapter(Protocol):
    def connect(self) -> None: ...
    def shutdown(self) -> None: ...
    def is_connected(self) -> bool: ...
    def account(self) -> AccountInfo: ...
    def symbol_spec(self, symbol_key: str) -> SymbolSpec: ...
    def find_symbols(self, pattern: str) -> list[str]: ...
    def candles(self, symbol_key: str, timeframe_min: int, count: int,
                from_ts: int | None = None) -> list[Candle]: ...
    def stream_ticks(self, symbol_keys: list[str]) -> Iterator[Tick]: ...
    def stream_dom(self, symbol_keys: list[str]
                   ) -> Iterator[tuple[str, list[DomLevel]]]: ...
    def market_order(self, symbol_key: str, side: str, lots: float,
                     sl: float, tp: float, comment: str) -> OrderResult: ...
    def modify_sltp(self, ticket: int, sl: float, tp: float) -> OrderResult: ...
    def close_position(self, ticket: int, lots: float | None = None) -> OrderResult: ...
    def positions(self, magic: int | None = None) -> list[Position]: ...
    def deals(self, from_ts_ms: int, to_ts_ms: int,
              magic: int | None = None) -> list[Deal]: ...
