"""Micro-window features (REQUIREMENTS.md §7.9) — "microstructure eyes,
macro hands."

These traits live in 1–30 second windows inside each bar: arrival bursts,
tick runs, sweeps, end-of-bar pressure. We do NOT trade at that speed (no
colocation — §8.11 not-pursued list); we aggregate each micro-trait to the
bar close and feed it to minute-scale decisions. The information content of
micro behaviour survives aggregation far better than the ability to act on
it — that asymmetry is the whole point.

Inputs: tick frame (ts_ms, price, side, volume — side/volume tier-gated) and
bar boundaries. Output: one row of micro features per bar, joined on
ts_close_ms, computed strictly from ticks with ts_ms < ts_close_ms.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EOB_WINDOW_MS = 5_000       # "who won the close" window
BURST_BUCKET_MS = 1_000     # arrival-burst granularity


def _runs_max(signs: np.ndarray) -> int:
    """Longest run of identical non-zero signs."""
    best = cur = 0
    prev = 0
    for s in signs:
        if s == 0:
            continue
        cur = cur + 1 if s == prev else 1
        prev = s
        best = max(best, cur)
    return best


def _bar_micro(ts: np.ndarray, px: np.ndarray, side: np.ndarray,
               vol: np.ndarray, t_open: int, t_close: int) -> dict:
    n = len(ts)
    out = {
        "micro_n_trades": n, "burst_ratio": np.nan, "arrival_accel": np.nan,
        "tick_run_max": np.nan, "flip_rate": np.nan, "sweep_1s_max": np.nan,
        "micro_mom_eob": np.nan, "eob_delta_share": np.nan,
    }
    if n < 5:
        return out

    # ── arrival bursts: max 1s bucket vs mean (self-excitation proxy, pre-R3)
    buckets = np.bincount(((ts - t_open) // BURST_BUCKET_MS).astype(int))
    nz = buckets[buckets > 0]
    out["burst_ratio"] = float(buckets.max() / nz.mean()) if len(nz) else np.nan

    # ── arrival acceleration: 2nd-half rate / 1st-half rate
    mid_t = (t_open + t_close) // 2
    first = int((ts < mid_t).sum())
    second = n - first
    out["arrival_accel"] = float(second / first) if first > 0 else np.nan

    # ── tick runs & flips (micro momentum vs micro chop)
    d = np.sign(np.diff(px))
    moves = d[d != 0]
    out["tick_run_max"] = float(_runs_max(d))
    if len(moves) > 1:
        out["flip_rate"] = float((moves[1:] != moves[:-1]).mean())

    # ── sweep proxy: max absolute price traversal inside any 1s window
    order = np.argsort(ts)
    ts_s, px_s = ts[order], px[order]
    j = 0
    sweep = 0.0
    lo = hi = px_s[0]
    for i in range(len(ts_s)):
        while ts_s[i] - ts_s[j] > BURST_BUCKET_MS:
            j += 1
            lo, hi = px_s[j:i + 1].min(), px_s[j:i + 1].max()
        lo, hi = min(lo, px_s[i]), max(hi, px_s[i])
        sweep = max(sweep, hi - lo)
    out["sweep_1s_max"] = float(sweep)

    # ── end-of-bar: who won the last 5 seconds
    eob = ts >= (t_close - EOB_WINDOW_MS)
    if eob.any():
        px_eob = px[eob]
        out["micro_mom_eob"] = float((px_eob[-1] - px_eob[0]) / px_eob[0])
        if side is not None and (side != 0).any():
            v = vol if vol is not None else np.ones(n)
            delta_eob = float((side[eob] * v[eob]).sum())
            gross = float(np.abs(side * v).sum())
            out["eob_delta_share"] = delta_eob / gross if gross > 0 else np.nan
    return out


def micro_features(ticks: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Per-bar micro traits. T0 columns always; eob_delta_share requires
    aggressor sides (T1). Join result to bars on index."""
    ts_all = ticks["ts_ms"].to_numpy(np.int64)
    px_all = (ticks["last"].fillna((ticks["bid"] + ticks["ask"]) / 2)
              if "last" in ticks.columns else ticks["price"]).to_numpy(float)
    side_all = (ticks["side"].to_numpy(np.int64)
                if "side" in ticks.columns else None)
    vol_all = (ticks["volume"].fillna(1.0).to_numpy(float)
               if "volume" in ticks.columns else None)

    order = np.argsort(ts_all)
    ts_all, px_all = ts_all[order], px_all[order]
    if side_all is not None:
        side_all = side_all[order]
    if vol_all is not None:
        vol_all = vol_all[order]

    rows = []
    for _, b in bars.iterrows():
        t0, t1 = int(b["ts_open_ms"]), int(b["ts_close_ms"])
        i = np.searchsorted(ts_all, t0, side="left")
        j = np.searchsorted(ts_all, t1, side="left")   # strictly < close
        rows.append(_bar_micro(
            ts_all[i:j], px_all[i:j],
            side_all[i:j] if side_all is not None else None,
            vol_all[i:j] if vol_all is not None else None,
            t0, t1))
    out = pd.DataFrame(rows, index=bars.index)

    # z-scores against trailing history — the model consumes surprise, not level
    for col in ("burst_ratio", "sweep_1s_max"):
        m = out[col].rolling(200, min_periods=50).mean()
        s = out[col].rolling(200, min_periods=50).std().replace(0, np.nan)
        out[f"{col}_z"] = (out[col] - m) / s
    return out
