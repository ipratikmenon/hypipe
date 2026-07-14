"""Zone-confirmation entry gate (REQUIREMENTS.md §9.1b).

The user's rule, encoded: never enter on a live touch. Demand proof — closed
candles only — that the zone is holding, then enter at the NEXT bar's open,
accepting a worse price for a higher win probability.

The economics: entering after confirmation is worse by δ (ATR units) but
raises win probability by Δp; with target π and stop σ anchored to the zone,

    confirmation is worth it  ⟺  Δp > δ / (π + σ)

The gate therefore records touch prices so the backtester can measure Δp and δ
empirically per symbol and disable the gate where the inequality fails.

States per (zone, direction):

    IDLE → TOUCHED → CONFIRMING → CONFIRMED (entry window) → IDLE
                  ↘ FAILED (violation close: zone broken, flips role)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .features.zones import KIND_RESISTANCE, KIND_SUPPORT, Zone


class ConfState(Enum):
    IDLE = "idle"
    TOUCHED = "touched"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class Bar:
    ts_close_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class EntrySignal:
    """Emitted on the close of the N-th confirming bar. Execution must fill at
    the NEXT bar's open — never this bar, never intrabar."""
    zone: Zone
    direction: str            # "LONG" (support held) | "SHORT" (resistance held)
    confirm_ts_ms: int
    touch_price: float        # for the Δp/δ study
    touch_ts_ms: int
    stop_loss: float          # beyond the violated edge of the zone
    confirm_bars: int
    had_defence_wick: bool


@dataclass
class _Tracking:
    state: ConfState = ConfState.IDLE
    touch_price: float = 0.0
    touch_ts_ms: int = 0
    confirm_count: int = 0
    wick_seen: bool = False
    window_left: int = 0


class ZoneConfirmationGate:
    def __init__(self, n_confirm: int = 2, violate_frac: float = 0.5,
                 entry_window_bars: int = 3, min_wick_ratio: float = 0.5,
                 sl_buffer_frac: float = 0.25):
        self.n_confirm = n_confirm
        self.violate_frac = violate_frac
        self.entry_window_bars = entry_window_bars
        self.min_wick_ratio = min_wick_ratio
        self.sl_buffer_frac = sl_buffer_frac
        self._track: dict[int, _Tracking] = {}   # id(zone) → tracking

    # ── helpers ───────────────────────────────────────────────────────────

    def _violation_level(self, zone: Zone) -> float:
        tol = self.violate_frac * zone.width
        return zone.lo - tol if zone.kind == KIND_SUPPORT else zone.hi + tol

    def _stop_loss(self, zone: Zone) -> float:
        buf = self.sl_buffer_frac * zone.width
        v = self._violation_level(zone)
        return v - buf if zone.kind == KIND_SUPPORT else v + buf

    def _is_confirming_close(self, zone: Zone, bar: Bar) -> bool:
        if zone.kind == KIND_SUPPORT:
            return bar.close > zone.hi and bar.low >= self._violation_level(zone)
        return bar.close < zone.lo and bar.high <= self._violation_level(zone)

    def _is_violation_close(self, zone: Zone, bar: Bar) -> bool:
        if zone.kind == KIND_SUPPORT:
            return bar.close < self._violation_level(zone)
        return bar.close > self._violation_level(zone)

    def _defence_wick(self, zone: Zone, bar: Bar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return False
        if zone.kind == KIND_SUPPORT:
            return (bar.close - bar.low) / rng >= self.min_wick_ratio
        return (bar.high - bar.close) / rng >= self.min_wick_ratio

    # ── driver: call once per CLOSED bar, per active zone ─────────────────

    def on_bar_close(self, zones: list[Zone], bar: Bar) -> list[EntrySignal]:
        signals: list[EntrySignal] = []
        for zone in zones:
            if zone.broken:
                continue
            t = self._track.setdefault(id(zone), _Tracking())

            if self._is_violation_close(zone, bar):
                # Break → role flip. The flipped zone RE-ARMS: broken support
                # becomes live resistance, and a later touch-and-reject on it
                # is the classic break-and-retest continuation entry, handled
                # by this same state machine in the new direction. A second
                # violation (whipsaw both ways) retires the zone for good.
                zone.kind = (KIND_RESISTANCE if zone.kind == KIND_SUPPORT
                             else KIND_SUPPORT)
                zone.flips += 1
                zone.broken = zone.flips >= 2
                self._track[id(zone)] = _Tracking()   # fresh state, new role
                continue

            if t.state == ConfState.IDLE:
                if zone.intersects(bar.low, bar.high):
                    t.state = ConfState.TOUCHED
                    t.touch_price = (zone.hi if zone.kind == KIND_SUPPORT
                                     else zone.lo)
                    t.touch_ts_ms = bar.ts_close_ms
                    t.confirm_count = 0
                    t.wick_seen = False
                    # a touch bar can itself confirm (wick into zone, close out)
                    if self._is_confirming_close(zone, bar):
                        t.confirm_count = 1
                        t.wick_seen = self._defence_wick(zone, bar)
                    self._maybe_confirm(zone, bar, t, signals)

            elif t.state == ConfState.TOUCHED:
                if self._is_confirming_close(zone, bar):
                    t.confirm_count += 1
                    t.wick_seen = t.wick_seen or self._defence_wick(zone, bar)
                elif zone.intersects(bar.low, bar.high):
                    t.confirm_count = 0      # back inside — restart the count
                else:
                    t.state = ConfState.IDLE  # drifted away without confirming
                    continue
                self._maybe_confirm(zone, bar, t, signals)

            elif t.state == ConfState.CONFIRMED:
                t.window_left -= 1
                if t.window_left <= 0:
                    t.state = ConfState.IDLE

        return signals

    def _maybe_confirm(self, zone: Zone, bar: Bar, t: _Tracking,
                       signals: list[EntrySignal]) -> None:
        if t.confirm_count >= self.n_confirm and t.wick_seen:
            zone.holds += 1
            zone.strength += 1.0
            t.state = ConfState.CONFIRMED
            t.window_left = self.entry_window_bars
            signals.append(EntrySignal(
                zone=zone,
                direction="LONG" if zone.kind == KIND_SUPPORT else "SHORT",
                confirm_ts_ms=bar.ts_close_ms,
                touch_price=t.touch_price,
                touch_ts_ms=t.touch_ts_ms,
                stop_loss=self._stop_loss(zone),
                confirm_bars=t.confirm_count,
                had_defence_wick=t.wick_seen,
            ))

    def entry_window_open(self, zone: Zone) -> bool:
        t = self._track.get(id(zone))
        return bool(t and t.state == ConfState.CONFIRMED and t.window_left > 0)
