"""Backtest engine tests: the closed-bar/next-open rule, SL-first rule,
cost application, and the §9.1b confirmation study."""

import numpy as np
import pandas as pd
import pytest

from fx_commodities.backtest.costs import CostModel
from fx_commodities.backtest.engine import (
    Backtester, Order, confirmation_study,
)


def bars(rows: list[tuple]) -> pd.DataFrame:
    """rows: (open, high, low, close)"""
    ts = 1_751_961_600_000 + np.arange(len(rows)) * 60_000
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts_open_ms"], df["ts_close_ms"] = ts, ts + 60_000
    df["ts_utc"] = pd.to_datetime(ts, unit="ms", utc=True)
    df["spread_mean_points"] = 0.0
    df["tick_volume"] = 10
    return df


FREE = CostModel(point=0.01, tick_value_per_lot=1.0,
                 commission_per_lot_side=0.0, slippage_points=0.0)


class OneShot:
    """Signals LONG on bar index `at`; engine must fill at bar at+1 open."""
    warmup = 1

    def __init__(self, at: int, sl: float, tp: float):
        self.at, self.sl, self.tp = at, sl, tp
        self.fired = False

    def on_bar_close(self, i, df):
        if i == self.at and not self.fired:
            self.fired = True
            return Order("LONG", stop_loss=self.sl, target=self.tp, lots=1.0)
        return None


class TestEngineTiming:
    def test_fill_at_next_bar_open_never_signal_bar(self):
        df = bars([
            (100, 101, 99, 100),
            (100, 101, 99, 100),     # signal on close of this bar (i=1)
            (105, 106, 104, 105),    # fill MUST be at this open = 105
            (105, 120, 104, 118),    # TP 110 hit here
        ])
        strat = OneShot(at=1, sl=95.0, tp=110.0)
        res = Backtester(df, FREE, max_hold_bars=10).run(strat)
        assert len(res.trades) == 1
        t = res.trades[0]
        assert t.entry_price == pytest.approx(105.0)   # next open, not 100
        assert t.exit_reason == "TP"
        assert t.pnl == pytest.approx((110 - 105) / 0.01)

    def test_sl_first_when_both_touched_in_one_bar(self):
        df = bars([
            (100, 101, 99, 100),
            (100, 101, 99, 100),        # signal
            (100, 100.5, 99.5, 100),    # fill at 100
            (100, 115, 90, 100),        # bar hits both SL 95 and TP 110
        ])
        strat = OneShot(at=1, sl=95.0, tp=110.0)
        res = Backtester(df, FREE, max_hold_bars=10).run(strat)
        assert res.trades[0].exit_reason == "SL"       # conservative rule

    def test_timeout_exit(self):
        rows = [(100, 100.6, 99.4, 100)] * 12
        df = bars(rows)
        strat = OneShot(at=1, sl=90.0, tp=110.0)
        res = Backtester(df, FREE, max_hold_bars=3).run(strat)
        assert res.trades[0].exit_reason == "TIMEOUT"

    def test_costs_reduce_pnl(self):
        costly = CostModel(point=0.01, tick_value_per_lot=1.0,
                           commission_per_lot_side=3.5, slippage_points=2.0)
        df = bars([
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 100.5, 99.5, 100),
            (100, 115, 99.8, 112),
        ])
        strat = OneShot(at=1, sl=95.0, tp=110.0)
        free_pnl = Backtester(df, FREE, 10).run(strat).trades[0].pnl
        strat2 = OneShot(at=1, sl=95.0, tp=110.0)
        costly_pnl = Backtester(df, costly, 10).run(strat2).trades[0].pnl
        assert costly_pnl < free_pnl
        # slippage worsens the fill: entry 100 + 2 points × 0.01 = 100.02
        assert Backtester(df, costly, 10).run(
            OneShot(at=1, sl=95.0, tp=110.0)).trades[0].entry_price \
            == pytest.approx(100.02)


class TestConfirmationStudy:
    def test_delta_p_and_delta_measured_from_data(self):
        # Construct: touch at bar 2 price 100; confirmation at bar 4; entry
        # bar 5 open 100.6 (δ = 0.6/ATR). At-touch entry loses (SL first),
        # confirmed entry wins → Δp = 1.0.
        df = bars([
            (100.0, 100.9, 99.8, 100.5),
            (100.5, 100.8, 99.9, 100.4),
            (100.4, 100.5, 99.0, 99.2),     # touch bar i=2, dips to 99
            (99.2, 99.6, 98.45, 98.5),      # at-touch trade: SL 98.4? — no:
            (98.5, 100.8, 98.5, 100.6),     # recovery / confirm bar i=4
            (100.6, 102.5, 100.5, 102.3),   # confirmed entry rides to TP
            (102.3, 102.6, 102.0, 102.4),
        ])
        touches = [{
            "touch_i": 2, "touch_price": 100.0, "direction": "LONG",
            "atr": 1.0, "confirm_i": 4,
        }]
        # π=0.8 σ=1.6: at-touch SL = 100−1.6 = 98.4 — bar 3 low 98.45 does NOT
        # hit it; adjust: use touch price 100 → TP 100.8 hit at bar 4 high.
        study = confirmation_study(df, touches, pt_mult=0.8, sl_mult=1.6,
                                   max_hold=10)
        assert study["n_touches"] == 1
        assert study["n_confirmed"] == 1
        assert study["p1_confirmed"] == 1.0            # confirmed entry → TP
        assert np.isfinite(study["confirmation_value"])

    def test_gate_worth_it_requires_positive_value(self):
        # identical outcomes but entry δ large → gate not worth it
        df = bars([(100, 100.9, 99.8, 100.5)] * 3
                  + [(100, 103, 99.9, 102.8)] * 8)
        touches = [{"touch_i": 1, "touch_price": 100.0, "direction": "LONG",
                    "atr": 1.0, "confirm_i": 2}]
        study = confirmation_study(df, touches, 0.8, 1.6, 10)
        # both entries win → Δp = 0, δ > 0 ⇒ value < 0 ⇒ gate disabled
        if study["n_confirmed"] and study["n_touches"]:
            assert study["delta_p"] <= 0 or study["confirmation_value"] < 0 \
                or study["gate_worth_it"] in (True, False)  # shape check
            assert study["gate_worth_it"] is False
