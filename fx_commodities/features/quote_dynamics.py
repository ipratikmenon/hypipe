"""Quote-dynamics features (REQUIREMENTS.md §7.2) — T0, always available."""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_quote_dynamics(bars: pd.DataFrame, interval_s: int,
                       z_window: int = 200) -> pd.DataFrame:
    df = bars.copy()
    point_spread = df["spread_mean_points"]
    df["spread_mean_pct"] = np.nan
    # spread in % of price needs the point size only for absolute spread; the
    # z-score below is scale-free, which is what the model consumes.
    mean = point_spread.rolling(z_window).mean()
    std = point_spread.rolling(z_window).std().replace(0, np.nan)
    df["spread_z"] = (point_spread - mean) / std

    df["quote_intensity"] = df["tick_count"] / interval_s
    i_mean = df["quote_intensity"].rolling(z_window).mean()
    i_std = df["quote_intensity"].rolling(z_window).std().replace(0, np.nan)
    df["intensity_z"] = (df["quote_intensity"] - i_mean) / i_std

    downs = df["down_ticks"].replace(0, np.nan)
    df["updown_ratio"] = (df["up_ticks"] / downs).clip(upper=10.0).fillna(1.0)
    return df
