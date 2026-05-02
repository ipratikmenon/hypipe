"""
DEMA Crossover Strategy
=======================
Signal logic (mirrors the TradingView setup in the screenshots):

  BUY  — DEMA-10 crosses above DEMA-20  AND  close is above DEMA-95
          (fast line punching through slow line while price is in an uptrend)

  SELL — DEMA-10 crosses below DEMA-20  AND  close is below DEMA-95
          (fast line dropping below slow line while price is in a downtrend)

  EXIT — opposite cross regardless of DEMA-95 position
          (used to close an open position before a new one is opened)

Confirmation layer (optional, matches the "yellow line touches red candles" pattern):
  If a BUY signal appears AND the previous candle low touched DEMA-95,
  the signal is tagged as HIGH_CONFIDENCE.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from indicators import add_demas, add_crossover_flags, price_touches_dema


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    signal: Signal
    price: float
    dema_fast: float
    dema_slow: float
    dema_trend: float
    high_confidence: bool = False

    def __str__(self) -> str:
        conf = " [HIGH CONFIDENCE]" if self.high_confidence else ""
        return (
            f"{self.signal.value}{conf} @ {self.price:.2f} | "
            f"DEMA fast={self.dema_fast:.2f} slow={self.dema_slow:.2f} trend={self.dema_trend:.2f}"
        )


class DEMACrossoverStrategy:
    def __init__(self, fast: int, slow: int, trend: int):
        self.fast = fast
        self.slow = slow
        self.trend = trend

        self._fast_col = f"dema_{fast}"
        self._slow_col = f"dema_{slow}"
        self._trend_col = f"dema_{trend}"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = add_demas(df, self.fast, self.slow, self.trend)
        df = add_crossover_flags(df, self.fast, self.slow)
        df["touch_trend"] = price_touches_dema(df, self.trend)
        return df

    def evaluate(self, df: pd.DataFrame, position: Optional[str] = None) -> TradeSignal:
        """
        Evaluate the latest completed candle (second-to-last row so the current
        candle is still forming).  `position` is 'LONG', 'SHORT', or None.
        """
        if len(df) < 3:
            return self._hold(df)

        df = self.prepare(df)

        # Use the last fully closed candle
        last = df.iloc[-2]
        prev_touch = df.iloc[-3]["touch_trend"]  # did prior candle touch DEMA-95?

        fast_val = last[self._fast_col]
        slow_val = last[self._slow_col]
        trend_val = last[self._trend_col]
        close = last["close"]

        cross_up = bool(last["cross_up"])
        cross_down = bool(last["cross_down"])
        above_trend = close > trend_val

        # ── Exit logic (always checked first) ──────────────────────────────
        if position == "LONG" and cross_down:
            return TradeSignal(Signal.EXIT_LONG, close, fast_val, slow_val, trend_val)

        if position == "SHORT" and cross_up:
            return TradeSignal(Signal.EXIT_SHORT, close, fast_val, slow_val, trend_val)

        # ── Entry logic ────────────────────────────────────────────────────
        if cross_up and above_trend and position != "LONG":
            high_conf = bool(prev_touch)
            return TradeSignal(Signal.BUY, close, fast_val, slow_val, trend_val, high_conf)

        if cross_down and not above_trend and position != "SHORT":
            return TradeSignal(Signal.SELL, close, fast_val, slow_val, trend_val)

        return self._hold(df)

    def _hold(self, df: pd.DataFrame) -> TradeSignal:
        last = df.iloc[-1]
        return TradeSignal(
            Signal.HOLD,
            float(last["close"]),
            float(last.get(self._fast_col, 0)),
            float(last.get(self._slow_col, 0)),
            float(last.get(self._trend_col, 0)),
        )
