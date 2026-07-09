"""Triple-barrier labeling (REQUIREMENTS.md §8.1).

For each bar t: upper barrier = close + pt_mult·ATR_t, lower = close −
sl_mult·ATR_t, vertical = t + max_hold bars. The label is decided by whichever
barrier is touched FIRST by subsequent bars (conservative intrabar rule: when
one bar spans both barriers, the adverse one counts).

`t_touch` (the bar index where the label resolves) is not optional metadata —
the purged cross-validation in dataset.py needs it to kill leakage from
overlapping label windows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier_labels(bars: pd.DataFrame, atr: pd.Series,
                          pt_mult: float, sl_mult: float,
                          max_hold: int) -> pd.DataFrame:
    """Long-side convention: y=+1 upper first, −1 lower first, 0 vertical.
    Rows with unusable ATR get y=NaN (caller drops them).

    Returns a frame aligned to `bars.index`: y (float, ±1/0/NaN),
    t_touch (int bar position, −1 where y is NaN).
    """
    n = len(bars)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    close = bars["close"].to_numpy(float)
    a = np.asarray(atr, dtype=float)

    y = np.full(n, np.nan)
    t_touch = np.full(n, -1, dtype=int)

    for t in range(n):
        if not np.isfinite(a[t]) or a[t] <= 0 or t + 1 >= n:
            continue
        upper = close[t] + pt_mult * a[t]
        lower = close[t] - sl_mult * a[t]
        end = min(t + max_hold, n - 1)

        label = 0.0
        touch = end
        for j in range(t + 1, end + 1):
            hit_lower = low[j] <= lower
            hit_upper = high[j] >= upper
            if hit_lower:                 # adverse-first when both in one bar
                label, touch = -1.0, j
                break
            if hit_upper:
                label, touch = 1.0, j
                break
        y[t] = label
        t_touch[t] = touch

    return pd.DataFrame({"y": y, "t_touch": t_touch}, index=bars.index)


def label_stats(labels: pd.DataFrame) -> dict:
    """Sanity numbers for reports: class balance and resolution speed."""
    valid = labels.dropna(subset=["y"])
    if valid.empty:
        return {"n": 0}
    counts = valid["y"].value_counts(normalize=True).to_dict()
    hold = (valid["t_touch"].to_numpy()
            - np.asarray(valid.index.get_indexer(valid.index)))
    return {
        "n": int(len(valid)),
        "frac_up": float(counts.get(1.0, 0.0)),
        "frac_down": float(counts.get(-1.0, 0.0)),
        "frac_vertical": float(counts.get(0.0, 0.0)),
        "median_bars_to_resolve": float(np.median(hold)) if len(hold) else 0.0,
    }
