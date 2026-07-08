"""Classical indicators (REQUIREMENTS.md §7.5). All functions are pure:
value at row t uses only rows ≤ t (no-lookahead test enforces this).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def dema(s: pd.Series, n: int) -> pd.Series:
    e = ema(s, n)
    return 2.0 * e - ema(e, n)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()


def adx_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_s = true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / tr_s
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False).mean()


def yang_zhang_vol(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Yang-Zhang realized volatility — handles gaps between bars/sessions."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    overnight = np.log(o / prev_c)
    open_close = np.log(c / o)
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var_on = overnight.rolling(n).var()
    var_oc = open_close.rolling(n).var()
    var_rs = rs.rolling(n).mean()
    return np.sqrt(var_on + k * var_oc + (1 - k) * var_rs)


def hurst_exponent(s: pd.Series, window: int = 200) -> pd.Series:
    """Rolling Hurst via variance-of-aggregated-returns regression:
    Var(r_q) ~ q^{2H}. Lightweight and O(window) per step."""
    lags = np.array([2, 4, 8, 16])
    log_lags = np.log(lags)

    def _h(x: np.ndarray) -> float:
        r = np.diff(np.log(x))
        if len(r) < lags[-1] * 2 or np.all(r == 0):
            return np.nan
        variances = []
        for q in lags:
            agg = np.add.reduceat(r, np.arange(0, len(r) - len(r) % q, q))
            v = np.var(agg)
            variances.append(v if v > 0 else np.nan)
        lv = np.log(variances)
        if np.any(~np.isfinite(lv)):
            return np.nan
        slope = np.polyfit(log_lags, lv, 1)[0]
        return slope / 2.0

    return s.rolling(window).apply(_h, raw=True)


def variance_ratio(s: pd.Series, q: int = 10, window: int = 200) -> pd.Series:
    """Lo-MacKinlay VR(q) = Var(q-bar returns)/(q · Var(1-bar returns))."""
    r1 = np.log(s / s.shift(1))
    rq = np.log(s / s.shift(q))
    return rq.rolling(window).var() / (q * r1.rolling(window).var())


def time_features(bars: pd.DataFrame) -> pd.DataFrame:
    """UTC session flags (§2.3) + minute-of-day encoding."""
    ts = bars["ts_utc"]
    minute = ts.dt.hour * 60 + ts.dt.minute
    out = pd.DataFrame(index=bars.index)
    out["mod_sin"] = np.sin(2 * np.pi * minute / 1440)
    out["mod_cos"] = np.cos(2 * np.pi * minute / 1440)
    out["in_london"] = ((ts.dt.hour >= 7) & (ts.dt.hour < 16)).astype(int)
    out["in_ny"] = ((ts.dt.hour >= 12) & (ts.dt.hour < 21)).astype(int)
    out["in_overlap"] = ((ts.dt.hour >= 12) & (ts.dt.hour < 16)).astype(int)
    return out


def add_classical(bars: pd.DataFrame, fast: int = 10, slow: int = 20,
                  trend: int = 95, atr_n: int = 14) -> pd.DataFrame:
    df = bars.copy()
    df[f"dema_{fast}"] = dema(df["close"], fast)
    df[f"dema_{slow}"] = dema(df["close"], slow)
    df[f"dema_{trend}"] = dema(df["close"], trend)
    df["atr"] = atr_wilder(df, atr_n)
    df["adx"] = adx_wilder(df, atr_n)
    df = pd.concat([df, time_features(df)], axis=1)
    prev_f, prev_s = df[f"dema_{fast}"].shift(1), df[f"dema_{slow}"].shift(1)
    df["cross_up"] = (prev_f <= prev_s) & (df[f"dema_{fast}"] > df[f"dema_{slow}"])
    df["cross_down"] = (prev_f >= prev_s) & (df[f"dema_{fast}"] < df[f"dema_{slow}"])
    return df
