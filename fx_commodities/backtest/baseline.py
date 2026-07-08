"""Baseline strategies for the Phase 2 gate.

Two variants share the DEMA logic so the confirmation gate's contribution is
isolated cleanly:

- DemaBaseline           — enter on the closed crossover bar (fill next open)
- ZoneConfirmedBaseline  — same trigger, but the entry must ALSO have a
  confirmed zone hold (§9.1b) in the trade direction; it records every zone
  touch so the engine's confirmation_study can measure Δp and δ.
"""

from __future__ import annotations

import pandas as pd

from ..confirmation import Bar, ZoneConfirmationGate
from ..features.zones import KIND_SUPPORT, ZoneSet
from .engine import Order


class DemaBaseline:
    def __init__(self, pt_mult: float = 0.8, sl_mult: float = 1.6,
                 fast: int = 10, slow: int = 20, trend: int = 95,
                 lots: float = 0.01):
        self.pt_mult, self.sl_mult = pt_mult, sl_mult
        self.fast, self.slow, self.trend = fast, slow, trend
        self.lots = lots
        self.warmup = trend + 10

    def on_bar_close(self, i: int, bars: pd.DataFrame) -> Order | None:
        row = bars.iloc[i]
        atr = row["atr"]
        if not (atr > 0):
            return None
        if row["cross_up"] and row["close"] > row[f"dema_{self.trend}"]:
            return Order("LONG", stop_loss=row["close"] - self.sl_mult * atr,
                         target=row["close"] + self.pt_mult * atr,
                         lots=self.lots, tag="baseline")
        if row["cross_down"] and row["close"] < row[f"dema_{self.trend}"]:
            return Order("SHORT", stop_loss=row["close"] + self.sl_mult * atr,
                         target=row["close"] - self.pt_mult * atr,
                         lots=self.lots, tag="baseline")
        return None


class ZoneConfirmedBaseline(DemaBaseline):
    """DEMA trigger + §9.1b zone-confirmation requirement.

    Entry rule: a confirmed zone hold opens an entry window; a DEMA-aligned
    close inside that window emits the order (filled at the NEXT bar's open by
    the engine — the user's wait-for-close rule holds everywhere).
    """

    def __init__(self, zone_set: ZoneSet, gate: ZoneConfirmationGate, **kw):
        super().__init__(**kw)
        self.zones = zone_set
        self.gate = gate
        self.touches: list[dict] = []          # feed for confirmation_study
        self._pending_touch_idx: dict[int, int] = {}   # id(zone) → touches idx

    def on_bar_close(self, i: int, bars: pd.DataFrame) -> Order | None:
        row = bars.iloc[i]
        atr = row["atr"]
        if not (atr > 0):
            return None

        self.zones.on_bar_close(int(row["ts_close_ms"]), atr)
        bar = Bar(ts_close_ms=int(row["ts_close_ms"]), open=row["open"],
                  high=row["high"], low=row["low"], close=row["close"])

        active = self.zones.active()
        # record fresh touches for the Δp/δ study (hypothetical at-touch entry)
        for z in active:
            key = id(z)
            if z.intersects(bar.low, bar.high) and key not in self._pending_touch_idx:
                self.touches.append({
                    "touch_i": i,
                    "touch_price": z.hi if z.kind == KIND_SUPPORT else z.lo,
                    "direction": "LONG" if z.kind == KIND_SUPPORT else "SHORT",
                    "atr": atr, "confirm_i": None,
                })
                self._pending_touch_idx[key] = len(self.touches) - 1

        signals = self.gate.on_bar_close(active, bar)
        for sig in signals:
            idx = self._pending_touch_idx.pop(id(sig.zone), None)
            if idx is not None:
                self.touches[idx]["confirm_i"] = i

            trend_ok = (row["close"] > row[f"dema_{self.trend}"]
                        if sig.direction == "LONG"
                        else row["close"] < row[f"dema_{self.trend}"])
            if not trend_ok:
                continue
            d = 1.0 if sig.direction == "LONG" else -1.0
            return Order(
                sig.direction,
                stop_loss=sig.stop_loss,
                target=row["close"] + d * self.pt_mult * atr,
                lots=self.lots, tag="zone_confirmed",
                meta={"zone_source": sig.zone.source,
                      "confirm_bars": sig.confirm_bars,
                      "touch_price": sig.touch_price},
            )
        # a failed/expired tracking without confirmation stays in the study as
        # touch-only — exactly what p0 measures
        for key in list(self._pending_touch_idx):
            idx = self._pending_touch_idx[key]
            if i - self.touches[idx]["touch_i"] > 20:
                self._pending_touch_idx.pop(key)
        return None
