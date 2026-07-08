"""Crypto adapter tests — pure parsers only, no network."""

import pytest

from fx_commodities.broker.base import DOM_ASK, DOM_BID, SIDE_BUY, SIDE_SELL
from fx_commodities.broker.crypto_adapter import (
    default_symbol_map,
    parse_book_ticker,
    parse_depth20,
    parse_kline_row,
    parse_trade,
    spec_from_filters,
)


class TestParsers:
    def test_trade_aggressor_side(self):
        # isBuyerMaker=true → buyer was passive → SELLER aggressed
        d = {"T": 1_720_000_000_000, "p": "65000.10", "q": "0.5", "m": True}
        t = parse_trade("BTCUSD", d, bid=65000.0, ask=65000.2)
        assert t.side == SIDE_SELL
        assert t.last == pytest.approx(65000.10)
        assert t.volume == pytest.approx(0.5)

        d["m"] = False
        assert parse_trade("BTCUSD", d, 0, 0).side == SIDE_BUY

    def test_book_ticker_is_quotes_only_tick(self):
        d = {"b": "65000.0", "B": "2.1", "a": "65000.2", "A": "1.7"}
        t = parse_book_ticker("BTCUSD", d, ts_ms=123)
        assert t.last is None and t.volume is None
        assert t.mid == pytest.approx(65000.1)

    def test_depth20_both_sides(self):
        d = {"bids": [["64999.9", "3.0"], ["64999.8", "1.0"]],
             "asks": [["65000.1", "2.0"]]}
        levels = parse_depth20(d)
        assert sum(1 for l in levels if l.side == DOM_BID) == 2
        assert sum(1 for l in levels if l.side == DOM_ASK) == 1
        assert levels[0].price == pytest.approx(64999.9)

    def test_kline_row(self):
        row = [1_720_000_000_000, "65000", "65100", "64900", "65050",
               "123.45", 1_720_000_059_999, "8000000", 4321, "60", "39", "0"]
        c = parse_kline_row(row)
        assert c.ts == 1_720_000_000
        assert c.close == pytest.approx(65050)
        assert c.tick_volume == 4321          # trade count
        assert c.real_volume == 123           # base-asset volume

    def test_spec_from_filters(self):
        filters = [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001000",
             "maxQty": "9000.0", "stepSize": "0.00001000"},
        ]
        spec = spec_from_filters("BTCUSDT", filters)
        assert spec.tick_size == pytest.approx(0.01)
        assert spec.volume_min == pytest.approx(1e-5)
        assert spec.expiration_ts == 0

    def test_default_symbol_map(self):
        m = default_symbol_map(("BTCUSD", "ETHUSD"))
        assert m == {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}
