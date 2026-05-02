import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def dema(series: pd.Series, period: int) -> pd.Series:
    """
    Double Exponential Moving Average: 2*EMA(n) - EMA(EMA(n))
    Reduces lag compared to a plain EMA of the same period.
    """
    e = ema(series, period)
    return 2 * e - ema(e, period)


def add_demas(df: pd.DataFrame, fast: int, slow: int, trend: int) -> pd.DataFrame:
    """
    Adds DEMA_fast, DEMA_slow, DEMA_trend columns to a OHLCV dataframe.
    Expects a 'close' column.
    """
    df = df.copy()
    df[f"dema_{fast}"] = dema(df["close"], fast)
    df[f"dema_{slow}"] = dema(df["close"], slow)
    df[f"dema_{trend}"] = dema(df["close"], trend)
    return df


def add_crossover_flags(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    """
    Adds boolean columns:
      cross_up   — DEMA_fast just crossed above DEMA_slow (bullish)
      cross_down — DEMA_fast just crossed below DEMA_slow (bearish)
    """
    fast_col = f"dema_{fast}"
    slow_col = f"dema_{slow}"

    prev_fast = df[fast_col].shift(1)
    prev_slow = df[slow_col].shift(1)

    df["cross_up"] = (prev_fast <= prev_slow) & (df[fast_col] > df[slow_col])
    df["cross_down"] = (prev_fast >= prev_slow) & (df[fast_col] < df[slow_col])
    return df


def price_touches_dema(df: pd.DataFrame, trend: int, tolerance_pct: float = 0.003) -> pd.Series:
    """
    Returns True for rows where the candle low is within tolerance_pct of DEMA_trend,
    indicating price is bouncing off the trend DEMA (the yellow line in TradingView).
    """
    dema_col = f"dema_{trend}"
    band = df[dema_col] * tolerance_pct
    return (df["low"] <= df[dema_col] + band) & (df["low"] >= df[dema_col] - band)
