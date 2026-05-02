import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Dhan credentials
    CLIENT_ID: str = os.environ["DHAN_CLIENT_ID"]
    ACCESS_TOKEN: str = os.environ["DHAN_ACCESS_TOKEN"]

    # Instrument
    SECURITY_ID: str = os.getenv("SECURITY_ID", "49081")
    EXCHANGE_SEGMENT: str = os.getenv("EXCHANGE_SEGMENT", "NSE_FNO")
    INSTRUMENT_TYPE: str = os.getenv("INSTRUMENT_TYPE", "OPTIDX")
    TRADING_SYMBOL: str = os.getenv("TRADING_SYMBOL", "NIFTY26MAY2525000CE")

    # Order sizing
    QUANTITY: int = int(os.getenv("QUANTITY", "1"))
    PRODUCT_TYPE: str = os.getenv("PRODUCT_TYPE", "INTRADAY")

    # Risk management
    MAX_LOSS_PER_TRADE: float = float(os.getenv("MAX_LOSS_PER_TRADE", "500"))
    MAX_DAILY_LOSS: float = float(os.getenv("MAX_DAILY_LOSS", "2000"))
    PROFIT_TARGET_MULTIPLIER: float = float(os.getenv("PROFIT_TARGET_MULTIPLIER", "2.0"))

    # Strategy parameters
    CANDLE_INTERVAL: int = int(os.getenv("CANDLE_INTERVAL", "2"))
    DEMA_FAST: int = int(os.getenv("DEMA_FAST", "10"))
    DEMA_SLOW: int = int(os.getenv("DEMA_SLOW", "20"))
    DEMA_TREND: int = int(os.getenv("DEMA_TREND", "95"))

    # Timing
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")
    MARKET_OPEN: str = os.getenv("MARKET_OPEN", "09:15")
    MARKET_CLOSE: str = os.getenv("MARKET_CLOSE", "15:30")
    TRADE_CUTOFF: str = os.getenv("TRADE_CUTOFF", "15:15")

    # Minimum candles needed before trading
    @property
    def MIN_CANDLES(self) -> int:
        return self.DEMA_TREND + 10


config = Config()
