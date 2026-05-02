"""
Thin wrapper around the dhanhq SDK that normalises candle data into a
pandas DataFrame and provides simple order helpers.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from dhanhq import dhanhq

from config import config

logger = logging.getLogger(__name__)


class DhanClient:
    def __init__(self):
        self._dhan = dhanhq(config.CLIENT_ID, config.ACCESS_TOKEN)

    # ── Market data ────────────────────────────────────────────────────────

    def get_candles(
        self,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        interval: int = 2,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """
        Fetch intraday minute candles and return a clean OHLCV DataFrame.
        Dhan supports 1, 5, 15, 25, 60 minute intervals natively; for 2-minute
        we fetch 1-minute data and resample.
        """
        today = date.today()
        from_date = from_date or (today - timedelta(days=5))
        to_date = to_date or today

        # Fetch 1-minute bars (finest granularity Dhan supports)
        resp = self._dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
        )

        if resp.get("status") != "success":
            logger.error("Dhan candle fetch failed: %s", resp)
            return pd.DataFrame()

        data = resp.get("data", {})
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(data["timestamp"], unit="s", utc=True),
                "open": data["open"],
                "high": data["high"],
                "low": data["low"],
                "close": data["close"],
                "volume": data["volume"],
            }
        )
        df = df.sort_values("timestamp").reset_index(drop=True)

        if interval != 1:
            df = self._resample(df, interval)

        return df

    @staticmethod
    def _resample(df: pd.DataFrame, interval: int) -> pd.DataFrame:
        df = df.set_index("timestamp")
        rule = f"{interval}min"
        resampled = df.resample(rule).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
        return resampled.dropna(subset=["close"]).reset_index()

    def get_ltp(self, security_id: str, exchange_segment: str) -> Optional[float]:
        """Return last traded price for the instrument."""
        resp = self._dhan.get_ltp_data(
            securities={exchange_segment: [security_id]}
        )
        try:
            return float(resp["data"][exchange_segment][security_id]["last_price"])
        except (KeyError, TypeError, ValueError):
            logger.error("LTP fetch failed: %s", resp)
            return None

    # ── Orders ─────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,  # "BUY" or "SELL"
        quantity: int,
        product_type: str = "INTRADAY",
    ) -> dict:
        logger.info(
            "Placing %s market order: qty=%d security=%s",
            transaction_type, quantity, security_id,
        )
        return self._dhan.place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product_type=product_type,
            price=0,
        )

    def place_limit_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        price: float,
        product_type: str = "INTRADAY",
    ) -> dict:
        logger.info(
            "Placing %s limit order @ %.2f: qty=%d security=%s",
            transaction_type, price, quantity, security_id,
        )
        return self._dhan.place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="LIMIT",
            product_type=product_type,
            price=price,
        )

    def cancel_all_open_orders(self) -> None:
        orders = self._dhan.get_order_list().get("data", [])
        for order in orders:
            if order.get("orderStatus") in ("PENDING", "TRANSIT"):
                self._dhan.cancel_order(order["orderId"])
                logger.info("Cancelled order %s", order["orderId"])

    def get_positions(self) -> list[dict]:
        resp = self._dhan.get_positions()
        return resp.get("data", [])

    def get_open_position(self, security_id: str) -> Optional[dict]:
        for pos in self.get_positions():
            if str(pos.get("securityId")) == str(security_id) and int(pos.get("netQty", 0)) != 0:
                return pos
        return None
