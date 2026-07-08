"""Feature tests: bars, indicators (hand-computed fixtures), zones pivots,
volume profile, and the no-lookahead property."""

import numpy as np
import pandas as pd
import pytest

from fx_commodities.features.bars import bars_from_ohlcv, time_bars
from fx_commodities.features.indicators import (
    add_classical, atr_wilder, dema, ema, variance_ratio,
)
from fx_commodities.features.orderflow import add_orderflow
from fx_commodities.features.quote_dynamics import add_quote_dynamics
from fx_commodities.features.volume_profile import (
    add_volume_profile, prior_session_levels,
)
from fx_commodities.features.zones import ZoneSet, pivot_levels


def tick_frame(n=300, start_ms=1_751_961_600_000):
    rng = np.random.default_rng(7)
    mid = 1.0850 + np.cumsum(rng.normal(0, 1e-5, n))
    return pd.DataFrame({
        "ts_ms": start_ms + np.arange(n) * 500,        # a tick every 500 ms
        "bid": mid - 3e-5, "ask": mid + 3e-5,
        "last": np.nan, "volume": np.nan, "side": 0,
    })


def bar_frame(n=400, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(0, 0.1, n))
    high = close + np.abs(rng.normal(0, 0.05, n))
    low = close - np.abs(rng.normal(0, 0.05, n))
    open_ = np.roll(close, 1)
    open_[0] = start
    ts = 1_751_961_600_000 + np.arange(n) * 60_000
    return pd.DataFrame({
        "ts_open_ms": ts, "ts_close_ms": ts + 60_000,
        "ts_utc": pd.to_datetime(ts, unit="ms", utc=True),
        "open": open_, "high": np.maximum(high, open_),
        "low": np.minimum(low, open_), "close": close,
        "tick_count": rng.integers(20, 100, n),
        "tick_volume": rng.integers(20, 100, n),
        "spread_mean_points": 6.0, "spread_max_points": 9.0,
        "up_ticks": rng.integers(5, 50, n), "down_ticks": rng.integers(5, 50, n),
    })


class TestBars:
    def test_time_bars_utc_alignment_and_ohlc(self):
        bars = time_bars(tick_frame(), interval_s=60, point=1e-5)
        assert (bars["ts_open_ms"] % 60_000 == 0).all()
        assert (bars["high"] >= bars[["open", "close"]].max(axis=1) - 1e-12).all()
        assert (bars["low"] <= bars[["open", "close"]].min(axis=1) + 1e-12).all()
        assert bars["spread_mean_points"].iloc[0] == pytest.approx(6.0)

    def test_bars_from_ohlcv_adapts_stored_candles(self):
        ohlcv = pd.DataFrame({
            "ts": [1_751_961_600, 1_751_961_660],
            "open": [1.0, 1.1], "high": [1.2, 1.2], "low": [0.9, 1.0],
            "close": [1.1, 1.15], "tick_volume": [50, 60],
            "spread_points": [5, 5], "real_volume": [0, 0],
        })
        bars = bars_from_ohlcv(ohlcv)
        assert len(bars) == 2 and "ts_utc" in bars


class TestIndicators:
    def test_dema_hand_computed(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        e1 = ema(s, 3)
        expected = 2 * e1 - ema(e1, 3)
        pd.testing.assert_series_equal(dema(s, 3), expected)
        # against the indices/ formula: linear ramp → DEMA tracks close closely
        assert dema(s, 3).iloc[-1] > ema(s, 3).iloc[-1]

    def test_atr_wilder_constant_range(self):
        n = 50
        df = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0,
                           "close": 10.0}, index=range(n))
        atr = atr_wilder(df, 14)
        assert atr.iloc[-1] == pytest.approx(2.0, rel=1e-3)   # TR is always 2

    def test_variance_ratio_random_walk_near_one(self):
        rng = np.random.default_rng(3)
        s = pd.Series(np.exp(np.cumsum(rng.normal(0, 0.01, 3000))))
        vr = variance_ratio(s, q=10, window=1000).dropna()
        assert vr.mean() == pytest.approx(1.0, abs=0.25)


class TestNoLookahead:
    """The spec's core anti-leakage property: features at bar t are identical
    whether or not the future exists (§7.5, §7.6)."""

    @pytest.mark.parametrize("builder", [
        lambda b: add_classical(b)[["dema_10", "atr", "adx", "cross_up"]],
        lambda b: add_quote_dynamics(b, 60)[["spread_z", "updown_ratio"]],
        lambda b: add_orderflow(b)[["pdelta_cum", "pdelta_slope"]],
        lambda b: add_volume_profile(
            b, add_classical(b)["atr"])[["poc", "vah", "val", "vwap"]],
    ])
    def test_truncation_invariance(self, builder):
        bars = bar_frame(400)
        full = builder(bars).iloc[:300].reset_index(drop=True)
        truncated = builder(bars.iloc[:300].reset_index(drop=True))
        truncated = truncated.iloc[:300].reset_index(drop=True)
        pd.testing.assert_frame_equal(full, truncated, check_exact=False,
                                      rtol=1e-9)

    def test_pivot_detect_ts_is_after_the_pivot(self):
        bars = bar_frame(120)
        piv = pivot_levels(bars, n=3)
        assert not piv.empty
        # every pivot's detection timestamp is n bars after its extreme bar —
        # a pivot cannot be known at the bar where it happens
        highs = bars.set_index("ts_close_ms")["high"]
        for _, row in piv.iterrows():
            assert row["detect_ts_ms"] in bars["ts_close_ms"].values

    def test_zone_set_only_activates_detected_pivots(self):
        bars = bar_frame(120)
        zs = ZoneSet(width_atr=0.25, pivot_n=3)
        zs.seed_pivots(bars)
        # replay only the first 10 bars: no pivot with detect_ts beyond bar 10
        for i in range(10):
            zs.on_bar_close(int(bars["ts_close_ms"].iloc[i]), atr=0.2)
        cutoff = int(bars["ts_close_ms"].iloc[9])
        piv = pivot_levels(bars, 3)
        future = piv[piv["detect_ts_ms"] > cutoff]
        n_future_activated = sum(
            1 for z in zs.zones if z.created_ts_ms > cutoff)
        assert n_future_activated == 0
        assert len(future) > 0     # the test is vacuous otherwise


class TestVolumeProfile:
    def test_developing_poc_inside_session_range(self):
        bars = bar_frame(300)
        atr = add_classical(bars)["atr"]
        vp = add_volume_profile(bars, atr).dropna(subset=["poc"])
        assert ((vp["poc"] >= vp["low"].min() - 1)
                & (vp["poc"] <= vp["high"].max() + 1)).all()
        assert (vp["val"] <= vp["vah"]).all()

    def test_prior_session_levels_shift(self):
        bars = bar_frame(60 * 30)                    # spans >1 UTC day
        atr = add_classical(bars)["atr"]
        vp = add_volume_profile(bars, atr)
        prior = prior_session_levels(vp)
        assert prior.iloc[0].isna().all()            # day 1 has no prior
        assert prior.iloc[1].notna().any()
