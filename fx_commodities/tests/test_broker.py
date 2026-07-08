"""Tests for broker types, tick normalization, and universe resolution.

The MetaTrader5 package cannot run on Linux, so pure helpers are tested
directly and the adapter Protocol is exercised through a fake.
"""

import pytest

from fx_commodities.broker.base import (
    SIDE_BUY, SIDE_SELL, SIDE_UNKNOWN, BrokerError, SymbolSpec, Tick,
)
from fx_commodities.broker.mt5_adapter import (
    TICK_FLAG_BUY, TICK_FLAG_SELL, normalize_tick, tick_side_from_flags,
)
from fx_commodities.instruments import Contract, resolve_universe


def make_spec(symbol="EURUSD", **over) -> SymbolSpec:
    base = dict(
        broker_symbol=symbol, digits=5, point=0.00001, tick_size=0.00001,
        tick_value=1.0, contract_size=100_000, volume_min=0.01,
        volume_max=100.0, volume_step=0.01, stops_level_points=10,
        spread_points=6, swap_long=-4.2, swap_short=1.1, swap_triple_day=2,
        filling_modes=("IOC",), expiration_ts=0,
    )
    base.update(over)
    return SymbolSpec(**base)


class TestTickNormalization:
    def test_side_from_flags(self):
        assert tick_side_from_flags(TICK_FLAG_BUY) == SIDE_BUY
        assert tick_side_from_flags(TICK_FLAG_SELL) == SIDE_SELL
        assert tick_side_from_flags(2 | 4) == SIDE_UNKNOWN  # bid/ask update only

    def test_quotes_only_tick_has_no_last_or_volume(self):
        t = normalize_tick("EURUSD", {
            "time_msc": 1_720_000_000_123, "bid": 1.08500, "ask": 1.08506,
            "last": 0.0, "volume": 0, "volume_real": 0.0, "flags": 6,
        })
        assert t.last is None and t.volume is None
        assert t.side == SIDE_UNKNOWN
        assert t.mid == pytest.approx(1.08503)

    def test_trade_tick_prefers_real_volume(self):
        t = normalize_tick("XAUUSD", {
            "time_msc": 1_720_000_000_456, "bid": 2391.10, "ask": 2391.40,
            "last": 2391.20, "volume": 3, "volume_real": 2.5,
            "flags": 8 | TICK_FLAG_BUY,
        })
        assert t.last == 2391.20
        assert t.volume == 2.5
        assert t.side == SIDE_BUY


class FakeAdapter:
    """Minimal BrokerAdapter test double for resolution tests."""

    def __init__(self, specs: dict[str, SymbolSpec],
                 catalogue: list[str] | None = None):
        self._specs = specs
        self._catalogue = catalogue or []

    def symbol_spec(self, key: str) -> SymbolSpec:
        if key not in self._specs:
            raise BrokerError(f"unknown {key}")
        return self._specs[key]

    def find_symbols(self, pattern: str) -> list[str]:
        needle = pattern.strip("*").lower()
        return [s for s in self._catalogue if needle[:4] in s.lower()]


class TestResolveUniverse:
    def test_resolves_and_applies_capabilities(self):
        adapter = FakeAdapter({
            "EURUSD": make_spec("EURUSD.r"),
            "XAUUSD": make_spec("GOLD", digits=2, point=0.01),
        })
        caps = {"EURUSD": {"has_dom": True, "has_last_ticks": False}}
        contracts = resolve_universe(
            adapter, ("EURUSD", "XAUUSD"),
            {"EURUSD": "EURUSD.r", "XAUUSD": "GOLD"}, caps)
        by_key = {c.key: c for c in contracts}
        assert by_key["EURUSD"].has_dom is True
        assert by_key["EURUSD"].broker_symbol == "EURUSD.r"
        assert by_key["XAUUSD"].has_dom is False

    def test_bad_mapping_fails_fast_with_candidates(self):
        adapter = FakeAdapter({}, catalogue=["EURUSDm", "EURUSD.pro"])
        with pytest.raises(BrokerError, match="EURUSDm"):
            resolve_universe(adapter, ("EURUSD",), {"EURUSD": "EURUSD_WRONG"})

    def test_missing_from_symbol_map_fails(self):
        adapter = FakeAdapter({"EURUSD": make_spec()})
        with pytest.raises(BrokerError, match="missing from SYMBOL_MAP"):
            resolve_universe(adapter, ("EURUSD", "WTI"), {"EURUSD": "EURUSD"})

    def test_fx_vs_commodity_spread_threshold(self):
        fx = Contract(key="EURUSD", spec=make_spec())
        cmd = Contract(key="WTI", spec=make_spec("USOIL"))
        assert fx.max_median_spread_pct < cmd.max_median_spread_pct
