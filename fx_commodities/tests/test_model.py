"""Phase 3 Steps 1-3 tests: labeling, HAR-RV, regime HMM + variance ratio."""

import numpy as np
import pandas as pd
import pytest

from fx_commodities.model.har import (
    HarRV, har_atr, oos_r2_vs_random_walk, realized_variance,
)
from fx_commodities.model.labeling import label_stats, triple_barrier_labels
from fx_commodities.model.regime import (
    RegimeHMM, variance_ratio_features,
)


def frame(rows, start_ms=1_751_961_600_000):
    ts = start_ms + np.arange(len(rows)) * 60_000
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts_utc"] = pd.to_datetime(ts, unit="ms", utc=True)
    return df


class TestTripleBarrier:
    def test_upper_first(self):
        bars = frame([(100, 100.5, 99.5, 100),      # t=0: barriers 102 / 96
                      (100, 103, 99, 102.5),        # hits upper at j=1
                      (102, 102, 101, 101.5)])
        lab = triple_barrier_labels(bars, pd.Series([2.0, 2.0, 2.0]),
                                    pt_mult=1.0, sl_mult=2.0, max_hold=5)
        assert lab["y"].iloc[0] == 1.0
        assert lab["t_touch"].iloc[0] == 1

    def test_lower_first_and_adverse_when_both(self):
        bars = frame([(100, 100.5, 99.5, 100),
                      (100, 103, 95, 100)])          # spans BOTH barriers
        lab = triple_barrier_labels(bars, pd.Series([2.0, 2.0]),
                                    pt_mult=1.0, sl_mult=2.0, max_hold=5)
        assert lab["y"].iloc[0] == -1.0               # adverse-first rule

    def test_vertical_barrier(self):
        bars = frame([(100, 100.2, 99.8, 100)] * 6)
        lab = triple_barrier_labels(bars, pd.Series([2.0] * 6),
                                    pt_mult=1.0, sl_mult=2.0, max_hold=3)
        assert lab["y"].iloc[0] == 0.0
        assert lab["t_touch"].iloc[0] == 3

    def test_bad_atr_yields_nan(self):
        bars = frame([(100, 101, 99, 100)] * 3)
        lab = triple_barrier_labels(bars, pd.Series([0.0, np.nan, 1.0]),
                                    pt_mult=1.0, sl_mult=1.0, max_hold=2)
        assert np.isnan(lab["y"].iloc[0]) and np.isnan(lab["y"].iloc[1])

    def test_stats(self):
        bars = frame([(100, 100.2, 99.8, 100)] * 10)
        lab = triple_barrier_labels(bars, pd.Series([1.0] * 10), 1, 1, 3)
        s = label_stats(lab)
        assert s["frac_vertical"] == 1.0


def synthetic_vol_clustered_bars(n_days=400, bars_per_day=100, seed=5):
    """True HAR data-generating process for daily log-vol: tomorrow's vol
    depends on daily, weekly, and monthly components — the structure HAR
    exists to capture and a plain random walk cannot."""
    rng = np.random.default_rng(seed)
    log_sig = np.zeros(n_days)
    for t in range(22, n_days):
        log_sig[t] = (0.4 * log_sig[t - 1]
                      + 0.3 * log_sig[t - 5:t].mean()
                      + 0.25 * log_sig[t - 22:t].mean()
                      + rng.normal(0, 0.35))
    sigma_day = 0.001 * np.exp(log_sig)

    rows, ts = [], []
    price, t0 = 100.0, 1_751_961_600_000
    for d in range(n_days):
        for b in range(bars_per_day):
            r = rng.normal(0, sigma_day[d])
            new = price * np.exp(r)
            rows.append((price, max(price, new), min(price, new), new))
            ts.append(t0 + (d * 1440 + b) * 60_000)
            price = new
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts_utc"] = pd.to_datetime(ts, unit="ms", utc=True)
    return df


class TestHAR:
    def test_har_beats_random_walk_oos(self):
        bars = synthetic_vol_clustered_bars()
        rv = realized_variance(bars)
        res = oos_r2_vs_random_walk(rv)
        assert res["n_test"] > 30
        assert res["r2_har"] > res["r2_rw"]          # the §8.2 acceptance test
        assert res["r2_har"] > 0.3

    def test_forecast_positive_and_sane(self):
        bars = synthetic_vol_clustered_bars(n_days=120)
        rv = realized_variance(bars)
        f = HarRV().fit(rv).forecast(rv)
        assert f > 0
        assert 0.05 * rv.iloc[-1] < f < 20 * rv.iloc[-1]

    def test_har_atr_never_below_trailing_atr(self):
        bars = synthetic_vol_clustered_bars(n_days=40)
        atr = (bars["high"] - bars["low"]).rolling(14).mean()
        rv = realized_variance(bars)
        fc = pd.Series(rv.values, index=rv.index)     # identity "forecast"
        combo = har_atr(atr, bars, fc, bars_per_day=100)
        valid = atr.notna()
        assert (combo[valid] >= atr[valid] - 1e-12).all()


def synthetic_regime_obs(n=3000, seed=11):
    """3 planted regimes: trend (drifty), chop (flat), crisis (violent)."""
    rng = np.random.default_rng(seed)
    # regimes must be separable for EM to find them — that's what we test;
    # real-data separability is Phase 3's problem, not this unit test's
    means = {"trend": (1.2, -0.5), "chop": (0.0, -0.5), "crisis": (0.0, 2.0)}
    sds = {"trend": (0.5, 0.3), "chop": (0.5, 0.3), "crisis": (4.0, 0.5)}
    A = {"trend": [("trend", .95), ("chop", .04), ("crisis", .01)],
         "chop": [("chop", .95), ("trend", .04), ("crisis", .01)],
         "crisis": [("crisis", .90), ("chop", .10)]}
    state, states, obs = "chop", [], []
    for _ in range(n):
        names, probs = zip(*A[state])
        state = rng.choice(names, p=np.array(probs) / sum(probs))
        m, s = means[state], sds[state]
        obs.append([rng.normal(m[0], s[0]), rng.normal(m[1], s[1])])
        states.append(state)
    return np.array(obs), states


class TestRegimeHMM:
    def test_recovers_planted_regimes(self):
        obs, truth = synthetic_regime_obs()
        model = RegimeHMM().fit(obs[:2000])
        probs = model.filtered_probs(obs)
        pred = probs[["p_trend", "p_chop", "p_crisis"]].idxmax(axis=1)
        pred = pred.str.replace("p_", "")
        acc = float((pred.to_numpy() == np.array(truth)).mean())
        assert acc > 0.7

    def test_filtered_probs_are_causal(self):
        """THE no-lookahead property: probabilities over the first 500 obs are
        identical whether or not the next 500 exist. Smoothed posteriors
        (hmmlearn predict_proba) fail this — ours must not."""
        obs, _ = synthetic_regime_obs(n=1000)
        model = RegimeHMM().fit(obs[:800])
        full = model.filtered_probs(obs).iloc[:500].reset_index(drop=True)
        trunc = model.filtered_probs(obs[:500]).reset_index(drop=True)
        pd.testing.assert_frame_equal(full, trunc, rtol=1e-10)

    def test_state_names_assigned(self):
        obs, _ = synthetic_regime_obs(n=1500)
        model = RegimeHMM().fit(obs)
        assert sorted(model.state_names_) == ["chop", "crisis", "trend"]


class TestVarianceRatio:
    def test_random_walk_vr_near_one(self):
        rng = np.random.default_rng(3)
        close = pd.Series(np.exp(np.cumsum(rng.normal(0, 0.01, 4000))))
        vr = variance_ratio_features(close)["vr_10"].dropna()
        assert abs(vr.mean() - 1.0) < 0.2

    def test_trending_series_vr_above_one(self):
        rng = np.random.default_rng(4)
        # strong AR(1) momentum in returns → VR(10) > 1
        r = np.zeros(4000)
        for t in range(1, 4000):
            r[t] = 0.4 * r[t - 1] + rng.normal(0, 0.01)
        close = pd.Series(np.exp(np.cumsum(r)))
        out = variance_ratio_features(close).dropna()
        assert out["vr_10"].mean() > 1.5
        assert out["vr_z"].mean() > 2.0
