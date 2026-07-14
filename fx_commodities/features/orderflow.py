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

        # delta CHANGE — bar-over-bar acceleration of aggression; a large
        # flip (e.g. +1958 → −4292 in footprint terms) marks the moment
        # control changes hands, often before price confirms
        df["delta_change"] = df["delta"].diff()
        dc_std = df["delta_change"].rolling(z_window).std().replace(0, np.nan)
        df["delta_change_z"] = df["delta_change"] / dc_std
        df["delta_flip"] = ((np.sign(df["delta"]) != np.sign(df["delta"].shift(1)))
                            & (df["delta_change"].abs() > 2 * dc_std)).astype(int)

    # buy-pressure %: share of aggressive buying over a rolling window —
    # the "buy pressure 34.7%" style reading; T1 uses real signed volume,
    # T0 falls back to up/down tick counts (named the same, tier recorded
    # in capabilities — never imputed)
    if "buy_ticks" in df.columns:
        b = df["buy_ticks"].rolling(slope_n).sum()
        s = df["sell_ticks"].rolling(slope_n).sum()
    else:
        b = df["up_ticks"].rolling(slope_n).sum()
        s = df["down_ticks"].rolling(slope_n).sum()
    df["buy_pressure_pct"] = 100.0 * b / (b + s).replace(0, np.nan)

    return df


def absorption_flag(bars: pd.DataFrame, atr: pd.Series,
                    vol_window: int = 20, range_frac: float = 0.25) -> pd.Series:
    """High effort, no result: volume in the top quintile of the trailing
    window while the bar range stays under range_frac × ATR."""
    vol_q80 = bars["tick_volume"].rolling(vol_window).quantile(0.8)
    bar_range = bars["high"] - bars["low"]
    return ((bars["tick_volume"] >= vol_q80)
            & (bar_range <= range_frac * atr)).astype(int)


# ── bid×ask footprint (§7.1) — per-price imbalance grid, bar-aggregated ─────

def footprint_features(ticks: pd.DataFrame, bars: pd.DataFrame,
                       grid: float, imbalance_ratio: float = 3.0,
                       min_stack: int = 3) -> pd.DataFrame:
    """The footprint chart, reduced to model-consumable numbers per bar.

    For each bar, trades are bucketed to a price grid and split by aggressor
    side. A BUY diagonal imbalance exists at level p when buy volume at p
    ≥ ratio × sell volume at (p − grid) — buyers lifting offers faster than
    sellers defend one tick below (SELL case symmetric, one tick above).
    Boxed-cells-in-a-column becomes: count of imbalances and the longest
    same-side stack. Requires T1 ticks (side ≠ 0); bars without trade data
    yield NaN — never imputed.

    Exports per bar: fp_buy_imb, fp_sell_imb (level counts),
    fp_stacked_buy, fp_stacked_sell (longest consecutive runs),
    fp_poc_dist_ticks (bar's max-volume price vs close, in grid units).
    """
    out_cols = ("fp_buy_imb", "fp_sell_imb", "fp_stacked_buy",
                "fp_stacked_sell", "fp_poc_dist_ticks")
    t = ticks[(ticks.get("side", 0) != 0)].copy() if "side" in ticks.columns \
        else ticks.iloc[0:0]
    if t.empty or grid <= 0:
        return pd.DataFrame(np.nan, index=bars.index, columns=list(out_cols))

    px = (t["last"].fillna((t["bid"] + t["ask"]) / 2)
          if "last" in t.columns else t["price"]).to_numpy(float)
    ts = t["ts_ms"].to_numpy(np.int64)
    side = t["side"].to_numpy(np.int64)
    vol = (t["volume"].fillna(1.0).to_numpy(float)
           if "volume" in t.columns else np.ones(len(t)))
    order = np.argsort(ts)
    ts, px, side, vol = ts[order], px[order], side[order], vol[order]
    levels_all = np.round(px / grid).astype(np.int64)

    rows = []
    for _, b in bars.iterrows():
        i = np.searchsorted(ts, int(b["ts_open_ms"]), side="left")
        j = np.searchsorted(ts, int(b["ts_close_ms"]), side="left")
        if j - i < 2:
            rows.append(dict.fromkeys(out_cols, np.nan))
            continue
        lv, sd, v = levels_all[i:j], side[i:j], vol[i:j]
        lo, hi = lv.min(), lv.max()
        n_lv = hi - lo + 1
        buy = np.zeros(n_lv)
        sell = np.zeros(n_lv)
        np.add.at(buy, lv[sd > 0] - lo, v[sd > 0])
        np.add.at(sell, lv[sd < 0] - lo, v[sd < 0])

        # diagonal comparisons: buy[p] vs sell[p-1]; sell[p] vs buy[p+1]
        buy_imb = np.zeros(n_lv, dtype=bool)
        sell_imb = np.zeros(n_lv, dtype=bool)
        if n_lv >= 2:
            opp_dn = sell[:-1]
            buy_imb[1:] = (buy[1:] > 0) & (buy[1:] >= imbalance_ratio *
                                           np.where(opp_dn > 0, opp_dn, 1e-12)) \
                          & ((opp_dn > 0) | (buy[1:] >= v.mean()))
            opp_up = buy[1:]
            sell_imb[:-1] = (sell[:-1] > 0) & (sell[:-1] >= imbalance_ratio *
                                               np.where(opp_up > 0, opp_up, 1e-12)) \
                            & ((opp_up > 0) | (sell[:-1] >= v.mean()))

        def _longest(mask: np.ndarray) -> int:
            best = cur = 0
            for m in mask:
                cur = cur + 1 if m else 0
                best = max(best, cur)
            return best

        total = buy + sell
        poc_level = lo + int(np.argmax(total))
        close_level = int(round(b["close"] / grid))
        rows.append({
            "fp_buy_imb": float(buy_imb.sum()),
            "fp_sell_imb": float(sell_imb.sum()),
            "fp_stacked_buy": float(_longest(buy_imb)),
            "fp_stacked_sell": float(_longest(sell_imb)),
            "fp_poc_dist_ticks": float(close_level - poc_level),
        })
    return pd.DataFrame(rows, index=bars.index)
