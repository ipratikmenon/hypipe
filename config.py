import os
from dotenv import load_dotenv

load_dotenv()


def _parse_list(val: str, default: list[str]) -> list[str]:
    if not val:
        return default
    return [v.strip().upper() for v in val.split(",") if v.strip()]


class Config:
    # Dhan credentials
    CLIENT_ID: str = os.environ["DHAN_CLIENT_ID"]
    ACCESS_TOKEN: str = os.environ["DHAN_ACCESS_TOKEN"]

    # Which indices to scan (comma-separated; leave blank for all four)
    # Valid values: NIFTY, BANKNIFTY, MIDCPNIFTY, FINNIFTY
    INDICES: list[str] = _parse_list(
        os.getenv("INDICES", "NIFTY,BANKNIFTY,MIDCPNIFTY,FINNIFTY"),
        ["NIFTY", "BANKNIFTY", "MIDCPNIFTY", "FINNIFTY"],
    )

    # Which option sides to trade (CE = calls, PE = puts, or both)
    OPTION_TYPES: list[str] = _parse_list(os.getenv("OPTION_TYPES", "CE,PE"), ["CE", "PE"])

    # Order sizing — lot size is fetched dynamically per instrument
    PRODUCT_TYPE: str = os.getenv("PRODUCT_TYPE", "INTRADAY")

    # Risk management
    # MAX_LOSS_PER_TRADE also acts as the maximum cost per trade:
    #   only options where LTP × lot_size ≤ MAX_LOSS_PER_TRADE are considered
    MAX_LOSS_PER_TRADE: float = float(os.getenv("MAX_LOSS_PER_TRADE", "5000"))
    MAX_DAILY_LOSS: float = float(os.getenv("MAX_DAILY_LOSS", "15000"))
    PROFIT_TARGET_MULTIPLIER: float = float(os.getenv("PROFIT_TARGET_MULTIPLIER", "2.0"))

    # Strategy parameters
    CANDLE_INTERVAL: int = int(os.getenv("CANDLE_INTERVAL", "2"))
    DEMA_FAST: int = int(os.getenv("DEMA_FAST", "10"))
    DEMA_SLOW: int = int(os.getenv("DEMA_SLOW", "20"))
    DEMA_TREND: int = int(os.getenv("DEMA_TREND", "95"))

    # Timing (IST 24h)
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")
    MARKET_OPEN: str = os.getenv("MARKET_OPEN", "09:15")
    MARKET_CLOSE: str = os.getenv("MARKET_CLOSE", "15:30")
    TRADE_CUTOFF: str = os.getenv("TRADE_CUTOFF", "15:15")

    @property
    def MIN_CANDLES(self) -> int:
        return self.DEMA_TREND + 10


config = Config()
