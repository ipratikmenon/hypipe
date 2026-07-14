"""Micro-window feature tests — hand-built tick fixtures force each trait."""

import numpy as np
import pandas as pd
import pytest

from fx_commodities.features.microstructure import micro_features

T0 = 1_751_961_600_000


def bar_frame(n_bars=1, interval_ms=60_000):
    ts = T0 + np.arange(n_bars) * interval_ms
    df = pd.DataFrame({"ts_open_ms": ts, "ts_close_ms": ts + interval_ms})
    df["close"] = 100.0
    return df


def ticks(rows):
    """rows: (ms_offset, price, side, volume)"""
    return pd.DataFrame({
        "ts_ms": [T0 + r[0] for r in rows],
        "price": [r[1] for r in rows],
        "side": [r[2] for r in rows],
        "volume": [r[3] for r in rows],
    })


class TestMicroTraits:
    def test_burst_ratio_detects_one_hot_second(self):
        # 10 quiet seconds (1 tick each) + one second with 20 ticks
        rows = [(i * 1000 + 10, 100.0, 0, 1.0) for i in range(10)]
        rows += [(15_000 + i * 40, 100.0, 0, 1.0) for i in range(20)]
        mf = micro_features(ticks(rows), bar_frame())
        assert mf["burst_ratio"].iloc[0] > 5.0

    def test_arrival_acceleration(self):
        # 5 ticks in first half, 25 in second half → accel = 5
        rows = [(i * 5000, 100.0, 0, 1.0) for i in range(5)]
        rows += [(31_000 + i * 1000, 100.0, 0, 1.0) for i in range(25)]
        mf = micro_features(ticks(rows), bar_frame())
        assert mf["arrival_accel"].iloc[0] == pytest.approx(5.0)

    def test_tick_run_and_flip_rate(self):
        # 8 consecutive up-ticks then pure alternation
        px = [100 + 0.1 * i for i in range(9)]           # run of 8 ups
        px += [px[-1] + (0.1 if i % 2 == 0 else -0.1) for i in range(10)]
        rows = [(1000 * i, p, 0, 1.0) for i, p in enumerate(px)]
        mf = micro_features(ticks(rows), bar_frame())
        assert mf["tick_run_max"].iloc[0] >= 8
        assert mf["flip_rate"].iloc[0] > 0.4

    def test_sweep_detects_fast_traversal(self):
        # slow drift, then 2.0 price units traversed within 600 ms
        rows = [(i * 2000, 100.0 + 0.01 * i, 0, 1.0) for i in range(10)]
        rows += [(30_000 + i * 100, 100.1 + 0.4 * i, 1, 1.0) for i in range(6)]
        mf = micro_features(ticks(rows), bar_frame())
        assert mf["sweep_1s_max"].iloc[0] == pytest.approx(2.0, abs=0.05)

    def test_end_of_bar_pressure(self):
        # flat bar, but the last 5s are all aggressive buys pushing price up
        rows = [(i * 1000, 100.0, -1, 1.0) for i in range(50)]
        rows += [(55_500 + i * 800, 100.0 + 0.05 * i, 1, 3.0) for i in range(5)]
        mf = micro_features(ticks(rows), bar_frame())
        assert mf["micro_mom_eob"].iloc[0] > 0
        assert mf["eob_delta_share"].iloc[0] > 0        # buys won the close

    def test_sparse_bar_yields_nan_not_garbage(self):
        rows = [(1000, 100.0, 0, 1.0), (2000, 100.1, 0, 1.0)]
        mf = micro_features(ticks(rows), bar_frame())
        assert np.isnan(mf["burst_ratio"].iloc[0])
        assert mf["micro_n_trades"].iloc[0] == 2

    def test_no_lookahead_tick_at_close_excluded(self):
        """A tick stamped exactly at ts_close belongs to the NEXT bar."""
        rows = [(i * 1000, 100.0 + i, 1, 1.0) for i in range(59)]
        rows.append((60_000, 999.0, 1, 1.0))            # at close boundary
        mf = micro_features(ticks(rows), bar_frame(n_bars=1))
        # the 999 print must not contaminate bar 0's end-of-bar momentum
        assert mf["micro_mom_eob"].iloc[0] < 0.2

    def test_truncation_invariance(self):
        rng = np.random.default_rng(9)
        rows = [(int(o), 100 + rng.normal(0, 0.1), int(rng.choice([-1, 1])), 1.0)
                for o in np.sort(rng.integers(0, 180_000, 600))]
        tk = ticks(rows)
        bars3 = bar_frame(n_bars=3)
        full = micro_features(tk, bars3).iloc[:2].reset_index(drop=True)
        trunc = micro_features(tk[tk["ts_ms"] < T0 + 120_000], bars3.iloc[:2])
        pd.testing.assert_frame_equal(
            full.drop(columns=[c for c in full.columns if c.endswith("_z")]),
            trunc.drop(columns=[c for c in trunc.columns if c.endswith("_z")]).reset_index(drop=True),
            rtol=1e-12)


class TestFootprint:
    """Bid×ask footprint (§7.1) — hand-built grids force each signature."""

    @staticmethod
    def _ticks(rows):
        return pd.DataFrame({
            "ts_ms": [T0 + r[0] for r in rows],
            "price": [r[1] for r in rows],
            "side": [r[2] for r in rows],
            "volume": [r[3] for r in rows],
        })

    def test_stacked_buy_imbalance_detected(self):
        from fx_commodities.features.orderflow import footprint_features
        # buyers 10x sellers on the diagonal at 3 consecutive levels
        rows = []
        for k, p in enumerate([100.0, 100.1, 100.2, 100.3]):
            rows.append((1000 + k * 4000, p, -1, 1.0))        # tiny sells
            rows += [(2000 + k * 4000 + i, p + 0.1, 1, 5.0) for i in range(2)]
        tk = self._ticks(rows)
        bars = bar_frame(1).assign(close=100.4)
        fp = footprint_features(tk, bars, grid=0.1, imbalance_ratio=3.0)
        assert fp["fp_buy_imb"].iloc[0] >= 3
        assert fp["fp_stacked_buy"].iloc[0] >= 3
        assert fp["fp_sell_imb"].iloc[0] == 0

    def test_balanced_grid_shows_no_imbalance(self):
        from fx_commodities.features.orderflow import footprint_features
        rows = []
        for k, p in enumerate([100.0, 100.1, 100.2]):
            rows.append((1000 + k * 3000, p, 1, 5.0))
            rows.append((1500 + k * 3000, p, -1, 5.0))
        fp = footprint_features(self._ticks(rows), bar_frame(1).assign(close=100.1),
                                grid=0.1)
        assert fp["fp_buy_imb"].iloc[0] == 0
        assert fp["fp_sell_imb"].iloc[0] == 0

    def test_quotes_only_yields_nan_not_fake_orderflow(self):
        from fx_commodities.features.orderflow import footprint_features
        rows = [(i * 1000, 100.0 + 0.01 * i, 0, 1.0) for i in range(30)]
        fp = footprint_features(self._ticks(rows), bar_frame(1), grid=0.1)
        assert fp["fp_buy_imb"].isna().all()      # T0 → NaN, never imputed
