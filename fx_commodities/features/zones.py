"""Price zones for the confirmation gate (REQUIREMENTS.md §7.6).

A zone is a band, never a line. Pivots carry a detect_ts: a fractal high at
bar t needs n bars after it to exist, so it is only knowable — and only enters
the zone set — at bar t+n. Using it earlier is lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

KIND_SUPPORT = "support"
KIND_RESISTANCE = "resistance"


@dataclass
class Zone:
    center: float
    lo: float
    hi: float
    kind: str                  # support | resistance
    source: str                # pivot | prior_poc | prior_vah | prior_val |
                               # prior_high | prior_low | round | dom_wall
    created_ts_ms: int
    strength: float = 1.0
    holds: int = 0             # confirmed defences so far
    broken: bool = False

    def intersects(self, low: float, high: float) -> bool:
        return low <= self.hi and high >= self.lo

    @property
    def width(self) -> float:
        return self.hi - self.lo


def pivot_levels(bars: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Fractal pivots. Returns DataFrame [price, kind, detect_ts_ms] where
    detect_ts_ms is the close of bar t+n — the first moment the pivot exists."""
    high, low = bars["high"].values, bars["low"].values
    ts_close = bars["ts_close_ms"].values
    rows = []
    for t in range(n, len(bars) - n):
        window_h = high[t - n: t + n + 1]
        window_l = low[t - n: t + n + 1]
        if high[t] == window_h.max() and (window_h.argmax() == n):
            rows.append({"price": high[t], "kind": KIND_RESISTANCE,
                         "detect_ts_ms": int(ts_close[t + n])})
        if low[t] == window_l.min() and (window_l.argmin() == n):
            rows.append({"price": low[t], "kind": KIND_SUPPORT,
                         "detect_ts_ms": int(ts_close[t + n])})
    return pd.DataFrame(rows)


def round_levels(price_lo: float, price_hi: float, point: float,
                 grid_points: int) -> list[float]:
    if grid_points <= 0:
        return []
    grid = grid_points * point
    start = np.ceil(price_lo / grid) * grid
    return list(np.arange(start, price_hi, grid))


class ZoneSet:
    """Maintains live zones from streaming closed bars. Pure & deterministic:
    same bar sequence in, same zones out — required for backtest/live parity."""

    def __init__(self, width_atr: float = 0.25, pivot_n: int = 3,
                 point: float = 1e-5, round_grid_points: int = 0,
                 merge_frac: float = 0.5, max_zones: int = 40):
        self.width_atr = width_atr
        self.pivot_n = pivot_n
        self.point = point
        self.round_grid_points = round_grid_points
        self.merge_frac = merge_frac
        self.max_zones = max_zones
        self.zones: list[Zone] = []
        self._pending_pivots: list[dict] = []

    # ── construction ─────────────────────────────────────────────────────

    def _add_zone(self, price: float, kind: str, source: str,
                  atr: float, ts_ms: int) -> None:
        w = self.width_atr * atr
        if w <= 0:
            return
        # merge into an existing same-kind zone if centers are within w/2·merge
        for z in self.zones:
            if z.kind == kind and not z.broken and \
                    abs(z.center - price) <= w * self.merge_frac:
                z.strength += 1.0
                return
        self.zones.append(Zone(center=price, lo=price - w / 2, hi=price + w / 2,
                               kind=kind, source=source, created_ts_ms=ts_ms))
        if len(self.zones) > self.max_zones:
            self.zones.sort(key=lambda z: (z.broken, -z.strength))
            self.zones = self.zones[: self.max_zones]

    def seed_pivots(self, bars: pd.DataFrame) -> None:
        """Precompute pivots for a bar frame; each activates at detect_ts."""
        piv = pivot_levels(bars, self.pivot_n)
        self._pending_pivots = piv.to_dict("records") if not piv.empty else []

    def seed_round_numbers(self, price_lo: float, price_hi: float,
                           atr: float, ts_ms: int) -> None:
        for lvl in round_levels(price_lo, price_hi, self.point,
                                self.round_grid_points):
            for kind in (KIND_SUPPORT, KIND_RESISTANCE):
                self._add_zone(lvl, kind, "round", atr, ts_ms)

    def add_session_levels(self, levels: dict[str, float], atr: float,
                           ts_ms: int) -> None:
        """Prior-session structure: poc/vah/val/high/low → zones."""
        kind_map = {"prior_poc": None, "prior_vah": KIND_RESISTANCE,
                    "prior_val": KIND_SUPPORT, "prior_high": KIND_RESISTANCE,
                    "prior_low": KIND_SUPPORT}
        for name, price in levels.items():
            if price is None or not np.isfinite(price):
                continue
            kind = kind_map.get(name)
            if kind is None:            # POC acts as both
                self._add_zone(price, KIND_SUPPORT, name, atr, ts_ms)
                self._add_zone(price, KIND_RESISTANCE, name, atr, ts_ms)
            else:
                self._add_zone(price, kind, name, atr, ts_ms)

    # ── streaming update ─────────────────────────────────────────────────

    def on_bar_close(self, ts_close_ms: int, atr: float) -> None:
        """Activate any pivots whose detect_ts has arrived."""
        still_pending = []
        for p in self._pending_pivots:
            if p["detect_ts_ms"] <= ts_close_ms:
                self._add_zone(p["price"], p["kind"], "pivot", atr, ts_close_ms)
            else:
                still_pending.append(p)
        self._pending_pivots = still_pending

    def active(self, kind: str | None = None) -> list[Zone]:
        return [z for z in self.zones
                if not z.broken and (kind is None or z.kind == kind)]
