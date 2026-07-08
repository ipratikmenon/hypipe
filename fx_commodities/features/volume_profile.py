"""Volume profile & VWAP (REQUIREMENTS.md §7.4) — T0, tick_volume weighted.

Everything is *developing*: values at bar t use session data up to and
including bar t only. Prior-session levels (for zones §7.6) come from the
previous completed UTC day.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _value_area(hist: dict[float, float], poc: float,
                coverage: float = 0.70) -> tuple[float, float]:
    total = sum(hist.values())
    if total <= 0:
        return poc, poc
    prices = sorted(hist)
    i = prices.index(poc)
    lo_i = hi_i = i
    covered = hist[poc]
    while covered < coverage * total and (lo_i > 0 or hi_i < len(prices) - 1):
        vol_lo = hist[prices[lo_i - 1]] if lo_i > 0 else -1.0
        vol_hi = hist[prices[hi_i + 1]] if hi_i < len(prices) - 1 else -1.0
        if vol_hi >= vol_lo:
            hi_i += 1
            covered += hist[prices[hi_i]]
        else:
            lo_i -= 1
            covered += hist[prices[lo_i]]
    return prices[lo_i], prices[hi_i]


def add_volume_profile(bars: pd.DataFrame, atr: pd.Series,
                       bucket_frac_atr: float = 0.1) -> pd.DataFrame:
    """Adds developing poc/vah/val, session vwap ± σ, and the distance
    features of §7.4. One pass, session = UTC day."""
    df = bars.copy()
    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)
    vwap = np.full(n, np.nan)
    vwap_sigma = np.full(n, np.nan)

    session = df["ts_utc"].dt.strftime("%Y-%m-%d").values
    close = df["close"].values
    vol = df["tick_volume"].astype(float).values

    hist: dict[float, float] = {}
    cur_session = None
    sum_pv = sum_v = sum_pv2 = 0.0
    bucket = np.nan

    for i in range(n):
        if session[i] != cur_session:
            cur_session = session[i]
            hist = {}
            sum_pv = sum_v = sum_pv2 = 0.0
            a = atr.iloc[i]
            bucket = a * bucket_frac_atr if np.isfinite(a) and a > 0 else np.nan
        if not np.isfinite(bucket):
            a = atr.iloc[i]
            bucket = a * bucket_frac_atr if np.isfinite(a) and a > 0 else np.nan
            if not np.isfinite(bucket):
                continue
        p = round(close[i] / bucket) * bucket
        hist[p] = hist.get(p, 0.0) + vol[i]
        sum_pv += close[i] * vol[i]
        sum_v += vol[i]
        sum_pv2 += vol[i] * close[i] ** 2

        poc[i] = max(hist, key=hist.get)
        val[i], vah[i] = _value_area(hist, poc[i])
        if sum_v > 0:
            vwap[i] = sum_pv / sum_v
            var = max(sum_pv2 / sum_v - vwap[i] ** 2, 0.0)
            vwap_sigma[i] = np.sqrt(var)

    df["poc"], df["vah"], df["val"] = poc, vah, val
    df["vwap"], df["vwap_sigma"] = vwap, vwap_sigma
    safe_atr = atr.replace(0, np.nan)
    df["dist_to_poc_atr"] = (df["close"] - df["poc"]) / safe_atr
    df["above_vah"] = (df["close"] > df["vah"]).astype(int)
    df["below_val"] = (df["close"] < df["val"]).astype(int)
    sigma = df["vwap_sigma"].replace(0, np.nan)
    df["dist_to_vwap_sigma"] = (df["close"] - df["vwap"]) / sigma
    return df


def prior_session_levels(bars_with_profile: pd.DataFrame) -> pd.DataFrame:
    """Per UTC session, the PREVIOUS session's final poc/vah/val/high/low —
    the levels that seed zones for the current day (§7.6)."""
    df = bars_with_profile
    day = df["ts_utc"].dt.strftime("%Y-%m-%d")
    last = df.groupby(day).agg(
        poc=("poc", "last"), vah=("vah", "last"), val=("val", "last"),
        high=("high", "max"), low=("low", "min"),
    )
    prev = last.shift(1)
    prev.columns = ["prior_poc", "prior_vah", "prior_val",
                    "prior_high", "prior_low"]
    return prev
