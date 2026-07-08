"""Orderflow features (REQUIREMENTS.md §7.1).

T1 (has_last_ticks): true aggressor CVD from bar `delta`.
T0 fallback: pdelta_* from up/down quote ticks — directional quote pressure,
honestly named; it is NOT traded volume and never pretends to be (§7.0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _rolling_slope(s: pd.Series, n: int) -> pd.Series:
    """OLS slope of s vs bar index over a rolling window (vectorized)."""
    x = np.arange(n, dtype=float)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()

    def _slope(y: np.ndarray) -> float:
        return float(((x - x_mean) * (y - y.mean())).sum() / x_var)

    return s.rolling(n).apply(_slope, raw=True)


def add_orderflow(bars: pd.DataFrame, slope_n: int = 20,
                  z_window: int = 100) -> pd.DataFrame:
    df = bars.copy()
    session = df["ts_utc"].dt.strftime("%Y-%m-%d")

    # T0 proxy delta — always present
    df["pdelta"] = df["up_ticks"] - df["down_ticks"]
    df["pdelta_cum"] = df.groupby(session)["pdelta"].cumsum()
    df["pdelta_slope"] = _rolling_slope(df["pdelta_cum"], slope_n)

    # T1 true CVD — only when the bar frame carries real aggressor delta
    if "delta" in df.columns:
        df["cvd"] = df.groupby(session)["delta"].cumsum()
        df["cvd_slope"] = _rolling_slope(df["cvd"], slope_n)
        px_chg = df["close"] - df["close"].shift(slope_n)
        cvd_chg = df["cvd"] - df["cvd"].shift(slope_n)
        df["cvd_divergence"] = (np.sign(px_chg) != np.sign(cvd_chg)).astype(int)
        d_mean = df["delta"].rolling(z_window).mean()
        d_std = df["delta"].rolling(z_window).std().replace(0, np.nan)
        df["delta_z"] = (df["delta"] - d_mean) / d_std

    return df


def absorption_flag(bars: pd.DataFrame, atr: pd.Series,
                    vol_window: int = 20, range_frac: float = 0.25) -> pd.Series:
    """High effort, no result: volume in the top quintile of the trailing
    window while the bar range stays under range_frac × ATR."""
    vol_q80 = bars["tick_volume"].rolling(vol_window).quantile(0.8)
    bar_range = bars["high"] - bars["low"]
    return ((bars["tick_volume"] >= vol_q80)
            & (bar_range <= range_frac * atr)).astype(int)
