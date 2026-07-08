"""Tick → bar aggregation (REQUIREMENTS.md §6).

Bars are built on MID price from broker/exchange timestamps (ts_ms), aligned
to UTC wall-clock boundaries: a 60 s bar stamped 10:01:00 covers
[10:01:00, 10:02:00). Aggregation on arrival time is forbidden.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def time_bars(ticks: pd.DataFrame, interval_s: int, point: float = 1e-5) -> pd.DataFrame:
    """Columns in: ts_ms, bid, ask, [last, volume, side]. Columns out per §6."""
    if ticks.empty:
        return pd.DataFrame()
    df = ticks.sort_values("ts_ms").copy()
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    df["spread_points"] = (df["ask"] - df["bid"]) / point
    bucket_ms = interval_s * 1000
    df["bar_ts"] = (df["ts_ms"] // bucket_ms) * bucket_ms

    mid_diff = df.groupby("bar_ts")["mid"].diff()
    df["up_tick"] = (mid_diff > 0).astype(int)
    df["down_tick"] = (mid_diff < 0).astype(int)

    has_side = "side" in df.columns and (df["side"] != 0).any()
    if has_side:
        vol = df["volume"].fillna(1.0) if "volume" in df.columns else 1.0
        df["signed_vol"] = df["side"] * vol
        df["buy_vol"] = np.where(df["side"] > 0, vol, 0.0)
        df["sell_vol"] = np.where(df["side"] < 0, vol, 0.0)

    agg = {
        "mid": ["first", "max", "min", "last", "count"],
        "spread_points": ["mean", "max"],
        "up_tick": "sum",
        "down_tick": "sum",
    }
    if has_side:
        agg.update({"signed_vol": "sum", "buy_vol": "sum", "sell_vol": "sum"})

    g = df.groupby("bar_ts").agg(agg)
    bars = pd.DataFrame({
        "ts_open_ms": g.index,
        "open": g[("mid", "first")].values,
        "high": g[("mid", "max")].values,
        "low": g[("mid", "min")].values,
        "close": g[("mid", "last")].values,
        "tick_count": g[("mid", "count")].values.astype(int),
        "spread_mean_points": g[("spread_points", "mean")].values,
        "spread_max_points": g[("spread_points", "max")].values,
        "up_ticks": g[("up_tick", "sum")].values.astype(int),
        "down_ticks": g[("down_tick", "sum")].values.astype(int),
    })
    bars["tick_volume"] = bars["tick_count"]
    if has_side:
        bars["delta"] = g[("signed_vol", "sum")].values
        bars["buy_ticks"] = g[("buy_vol", "sum")].values
        bars["sell_ticks"] = g[("sell_vol", "sum")].values
    bars["ts_close_ms"] = bars["ts_open_ms"] + bucket_ms
    bars["ts_utc"] = pd.to_datetime(bars["ts_open_ms"], unit="ms", utc=True)
    return bars.reset_index(drop=True)


def bars_from_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Adapt stored M1 candles (§4.5 schema) to the bar frame the features and
    backtester consume — used for historical backfill where no ticks exist."""
    if ohlcv.empty:
        return pd.DataFrame()
    bars = pd.DataFrame({
        "ts_open_ms": ohlcv["ts"].values * 1000,
        "open": ohlcv["open"].values,
        "high": ohlcv["high"].values,
        "low": ohlcv["low"].values,
        "close": ohlcv["close"].values,
        "tick_count": ohlcv["tick_volume"].values,
        "tick_volume": ohlcv["tick_volume"].values,
        "spread_mean_points": ohlcv["spread_points"].values.astype(float),
        "spread_max_points": ohlcv["spread_points"].values.astype(float),
        "up_ticks": 0, "down_ticks": 0,
    })
    bars["ts_close_ms"] = bars["ts_open_ms"] + 60_000
    bars["ts_utc"] = pd.to_datetime(bars["ts_open_ms"], unit="ms", utc=True)
    return bars.sort_values("ts_open_ms").reset_index(drop=True)


def volume_bars(ticks: pd.DataFrame, bucket_ticks: int, point: float = 1e-5) -> pd.DataFrame:
    """Bars closing every `bucket_ticks` ticks — better-behaved sampling for
    the model layer. Same output columns as time_bars."""
    if ticks.empty:
        return pd.DataFrame()
    df = ticks.sort_values("ts_ms").reset_index(drop=True).copy()
    df["bar_ts"] = (np.arange(len(df)) // bucket_ticks)
    df = df.rename(columns={"bar_ts": "_bucket"})
    out = time_bars(
        df.assign(ts_ms=df["_bucket"]), interval_s=1, point=point
    ).rename(columns={"ts_open_ms": "bucket_id"})
    # restore real timestamps: last tick time per bucket
    ts_last = df.groupby("_bucket")["ts_ms"].last()
    out["ts_close_ms"] = ts_last.values
    out["ts_utc"] = pd.to_datetime(out["ts_close_ms"], unit="ms", utc=True)
    return out
