"""Event-driven bar backtester (REQUIREMENTS.md §10.1 + §9.1b).

Timing rules — identical to live, by construction:
- strategies see ONLY closed bars: `on_bar_close(i)` is called after bar i
  completes and may look at rows ≤ i;
- entries fill at bar i+1's OPEN, adjusted by half-spread + slippage;
- SL/TP are checked intrabar on bars > entry with the conservative rule:
  if both are touched within one bar, the stop is assumed hit first.

The engine also runs the §9.1b confirmation study: for every zone touch it
tracks the hypothetical at-touch entry alongside the real confirmed entry so
Δp and δ are measured, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .costs import CostModel


@dataclass
class Order:
    direction: str            # "LONG" | "SHORT"
    stop_loss: float
    target: float
    lots: float = 0.01
    tag: str = ""             # e.g. "baseline", "zone_confirmed"
    meta: dict = field(default_factory=dict)


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    direction: str
    entry_price: float
    exit_price: float
    lots: float
    pnl: float                # net of costs, account currency
    exit_reason: str          # TP | SL | TIMEOUT | EOD
    tag: str
    meta: dict


class Strategy(Protocol):
    warmup: int
    def on_bar_close(self, i: int, bars: pd.DataFrame) -> Order | None: ...


class Backtester:
    def __init__(self, bars: pd.DataFrame, costs: CostModel,
                 max_hold_bars: int = 60):
        self.bars = bars.reset_index(drop=True)
        self.costs = costs
        self.max_hold_bars = max_hold_bars

    def run(self, strategy: Strategy) -> "BacktestResult":
        bars = self.bars
        trades: list[Trade] = []
        pending: Order | None = None
        open_trade: dict | None = None

        for i in range(strategy.warmup, len(bars)):
            row = bars.iloc[i]
            is_last = i == len(bars) - 1

            # ── manage open position on this bar (intrabar SL-first rule) ──
            if open_trade is not None:
                t = open_trade
                exit_price = exit_reason = None
                if t["direction"] == "LONG":
                    if row["low"] <= t["sl"]:
                        exit_price, exit_reason = t["sl"], "SL"
                    elif row["high"] >= t["tp"]:
                        exit_price, exit_reason = t["tp"], "TP"
                else:
                    if row["high"] >= t["sl"]:
                        exit_price, exit_reason = t["sl"], "SL"
                    elif row["low"] <= t["tp"]:
                        exit_price, exit_reason = t["tp"], "TP"
                if exit_price is None and i - t["entry_i"] >= self.max_hold_bars:
                    exit_price, exit_reason = row["close"], "TIMEOUT"

                if exit_price is not None:
                    trades.append(self._close(t, i, exit_price, exit_reason, row))
                    open_trade = None

            # ── fill pending order at THIS bar's open (signal was last bar) ─
            if pending is not None and open_trade is None:
                slip = self.costs.cost_in_price_units(row["spread_mean_points"])
                fill = (row["open"] + slip if pending.direction == "LONG"
                        else row["open"] - slip)
                open_trade = {
                    "entry_i": i, "direction": pending.direction,
                    "entry": fill, "sl": pending.stop_loss,
                    "tp": pending.target, "lots": pending.lots,
                    "tag": pending.tag, "meta": pending.meta,
                    "spread_entry": row["spread_mean_points"],
                }
            pending = None

            # ── strategy sees the CLOSED bar i only after management ────────
            if open_trade is None and not is_last:
                order = strategy.on_bar_close(i, bars)
                if order is not None:
                    pending = order

        if open_trade is not None:   # square off at the end
            last = bars.iloc[-1]
            trades.append(self._close(open_trade, len(bars) - 1,
                                      last["close"], "EOD", last))
        return BacktestResult(trades, bars)

    def _close(self, t: dict, i: int, exit_price: float, reason: str,
               row: pd.Series) -> Trade:
        direction = 1.0 if t["direction"] == "LONG" else -1.0
        points = direction * (exit_price - t["entry"]) / self.costs.point
        gross = points * self.costs.tick_value_per_lot * t["lots"]
        cost = self.costs.entry_exit_cost(
            t["lots"], t["spread_entry"], row["spread_mean_points"])
        return Trade(entry_i=t["entry_i"], exit_i=i, direction=t["direction"],
                     entry_price=t["entry"], exit_price=exit_price,
                     lots=t["lots"], pnl=gross - cost, exit_reason=reason,
                     tag=t["tag"], meta=t["meta"])


@dataclass
class BacktestResult:
    trades: list[Trade]
    bars: pd.DataFrame

    def metrics(self) -> dict:
        if not self.trades:
            return {"n_trades": 0}
        pnl = np.array([t.pnl for t in self.trades])
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        equity = pnl.cumsum()
        peak = np.maximum.accumulate(equity)
        max_dd = float((peak - equity).max())
        pf = (float(wins.sum() / -losses.sum())
              if losses.sum() < 0 else float("inf"))
        return {
            "n_trades": len(pnl),
            "net_pnl": float(pnl.sum()),
            "profit_factor": pf,
            "hit_rate": float(len(wins) / len(pnl)),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "max_drawdown": max_dd,
            "sharpe_per_trade": (float(pnl.mean() / pnl.std())
                                 if pnl.std() > 0 else 0.0),
        }


# ── §9.1b confirmation study ────────────────────────────────────────────────

def confirmation_study(bars: pd.DataFrame, touches: list[dict],
                       pt_mult: float, sl_mult: float,
                       max_hold: int = 60) -> dict:
    """For each recorded zone touch, resolve the triple-barrier outcome for a
    hypothetical AT-TOUCH entry and, where confirmation completed, for the
    CONFIRMED entry. Returns empirical p0, p1, Δp, δ and
    confirmation_value = Δp·(π+σ) − δ  (must be > 0 for the gate to earn its keep).
    """
    def _resolve(i: int, price: float, direction: str, atr: float) -> int | None:
        d = 1.0 if direction == "LONG" else -1.0
        tp = price + d * pt_mult * atr
        sl = price - d * sl_mult * atr
        for j in range(i + 1, min(i + 1 + max_hold, len(bars))):
            row = bars.iloc[j]
            if d > 0:
                if row["low"] <= sl:
                    return 0
                if row["high"] >= tp:
                    return 1
            else:
                if row["high"] >= sl:
                    return 0
                if row["low"] <= tp:
                    return 1
        return None                      # vertical barrier — excluded

    touch_outcomes, conf_outcomes, deltas = [], [], []
    for t in touches:
        atr = t["atr"]
        if not np.isfinite(atr) or atr <= 0:
            continue
        o0 = _resolve(t["touch_i"], t["touch_price"], t["direction"], atr)
        if o0 is not None:
            touch_outcomes.append(o0)
        if t.get("confirm_i") is not None:
            entry_i = t["confirm_i"] + 1
            if entry_i < len(bars):
                entry_px = bars.iloc[entry_i]["open"]
                o1 = _resolve(entry_i, entry_px, t["direction"], atr)
                if o1 is not None:
                    conf_outcomes.append(o1)
                    d = 1.0 if t["direction"] == "LONG" else -1.0
                    deltas.append(d * (entry_px - t["touch_price"]) / atr)

    p0 = float(np.mean(touch_outcomes)) if touch_outcomes else float("nan")
    p1 = float(np.mean(conf_outcomes)) if conf_outcomes else float("nan")
    delta_atr = float(np.mean(deltas)) if deltas else float("nan")
    dp = p1 - p0
    value = dp * (pt_mult + sl_mult) - delta_atr
    return {
        "n_touches": len(touch_outcomes), "n_confirmed": len(conf_outcomes),
        "p0_at_touch": p0, "p1_confirmed": p1, "delta_p": dp,
        "delta_atr": delta_atr, "confirmation_value": value,
        "gate_worth_it": bool(np.isfinite(value) and value > 0),
    }
