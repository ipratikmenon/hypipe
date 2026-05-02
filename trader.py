"""
Main trading loop.

Run:
    python trader.py            # live trading
    python trader.py --dry-run  # paper mode (signals printed, no orders placed)
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Optional

import pytz
import schedule

from config import config
from dhan_client import DhanClient
from strategy import DEMACrossoverStrategy, Signal, TradeSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trader.log"),
    ],
)
logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.daily_pnl: float = 0.0
        self.trade_count: int = 0

    def can_trade(self) -> bool:
        if self.daily_pnl <= -config.MAX_DAILY_LOSS:
            logger.warning("Daily loss limit reached (%.2f). No more trades today.", self.daily_pnl)
            return False
        return True

    def record_trade(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.trade_count += 1
        logger.info("Trade #%d | PnL: %.2f | Daily PnL: %.2f", self.trade_count, pnl, self.daily_pnl)


class Trader:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.client = DhanClient()
        self.strategy = DEMACrossoverStrategy(
            fast=config.DEMA_FAST,
            slow=config.DEMA_SLOW,
            trend=config.DEMA_TREND,
        )
        self.risk = RiskManager()
        self.tz = pytz.timezone(config.TIMEZONE)

        # Track in-memory position (source of truth is Dhan; this avoids extra API calls)
        self._position: Optional[str] = None  # "LONG", "SHORT", or None
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._target: float = 0.0

        if dry_run:
            logger.info("*** DRY RUN MODE — no real orders will be placed ***")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _market_is_open(self) -> bool:
        now = self._now()
        open_h, open_m = map(int, config.MARKET_OPEN.split(":"))
        cutoff_h, cutoff_m = map(int, config.TRADE_CUTOFF.split(":"))
        market_open = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        trade_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)
        return market_open <= now <= trade_cutoff

    def _fetch_candles(self):
        return self.client.get_candles(
            security_id=config.SECURITY_ID,
            exchange_segment=config.EXCHANGE_SEGMENT,
            instrument_type=config.INSTRUMENT_TYPE,
            interval=config.CANDLE_INTERVAL,
        )

    # ── Order helpers ──────────────────────────────────────────────────────

    def _buy(self, price: float) -> None:
        sl = round(price - config.MAX_LOSS_PER_TRADE / config.QUANTITY, 2)
        target = round(price + (price - sl) * config.PROFIT_TARGET_MULTIPLIER, 2)

        logger.info("ENTER LONG @ %.2f | SL=%.2f | Target=%.2f", price, sl, target)

        if not self.dry_run:
            resp = self.client.place_market_order(
                security_id=config.SECURITY_ID,
                exchange_segment=config.EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=config.QUANTITY,
                product_type=config.PRODUCT_TYPE,
            )
            logger.info("Order response: %s", resp)

        self._position = "LONG"
        self._entry_price = price
        self._stop_loss = sl
        self._target = target

    def _sell_short(self, price: float) -> None:
        sl = round(price + config.MAX_LOSS_PER_TRADE / config.QUANTITY, 2)
        target = round(price - (sl - price) * config.PROFIT_TARGET_MULTIPLIER, 2)

        logger.info("ENTER SHORT @ %.2f | SL=%.2f | Target=%.2f", price, sl, target)

        if not self.dry_run:
            resp = self.client.place_market_order(
                security_id=config.SECURITY_ID,
                exchange_segment=config.EXCHANGE_SEGMENT,
                transaction_type="SELL",
                quantity=config.QUANTITY,
                product_type=config.PRODUCT_TYPE,
            )
            logger.info("Order response: %s", resp)

        self._position = "SHORT"
        self._entry_price = price
        self._stop_loss = sl
        self._target = target

    def _exit(self, price: float) -> None:
        pnl = (
            (price - self._entry_price) * config.QUANTITY
            if self._position == "LONG"
            else (self._entry_price - price) * config.QUANTITY
        )
        logger.info(
            "EXIT %s @ %.2f | Entry=%.2f | PnL=%.2f", self._position, price, self._entry_price, pnl
        )

        if not self.dry_run:
            side = "SELL" if self._position == "LONG" else "BUY"
            resp = self.client.place_market_order(
                security_id=config.SECURITY_ID,
                exchange_segment=config.EXCHANGE_SEGMENT,
                transaction_type=side,
                quantity=config.QUANTITY,
                product_type=config.PRODUCT_TYPE,
            )
            logger.info("Order response: %s", resp)

        self.risk.record_trade(pnl)
        self._position = None
        self._entry_price = 0.0

    # ── Stop loss / target checker ─────────────────────────────────────────

    def _check_sl_target(self, ltp: float) -> bool:
        if self._position == "LONG":
            if ltp <= self._stop_loss:
                logger.warning("STOP LOSS HIT @ %.2f", ltp)
                self._exit(ltp)
                return True
            if ltp >= self._target:
                logger.info("TARGET HIT @ %.2f", ltp)
                self._exit(ltp)
                return True

        elif self._position == "SHORT":
            if ltp >= self._stop_loss:
                logger.warning("STOP LOSS HIT @ %.2f", ltp)
                self._exit(ltp)
                return True
            if ltp <= self._target:
                logger.info("TARGET HIT @ %.2f", ltp)
                self._exit(ltp)
                return True

        return False

    # ── Core tick ──────────────────────────────────────────────────────────

    def tick(self) -> None:
        if not self._market_is_open():
            logger.debug("Market closed. Skipping tick.")
            return

        if not self.risk.can_trade() and self._position is None:
            return

        df = self._fetch_candles()
        if df.empty or len(df) < config.MIN_CANDLES:
            logger.warning("Not enough candle data yet (%d rows).", len(df))
            return

        # Check SL/target intrabar using LTP
        if self._position:
            ltp = self.client.get_ltp(config.SECURITY_ID, config.EXCHANGE_SEGMENT)
            if ltp and self._check_sl_target(ltp):
                return

        signal: TradeSignal = self.strategy.evaluate(df, position=self._position)
        logger.info("Signal: %s", signal)

        if signal.signal == Signal.BUY and self.risk.can_trade():
            if self._position == "SHORT":
                self._exit(signal.price)
            self._buy(signal.price)

        elif signal.signal == Signal.SELL and self.risk.can_trade():
            if self._position == "LONG":
                self._exit(signal.price)
            self._sell_short(signal.price)

        elif signal.signal == Signal.EXIT_LONG and self._position == "LONG":
            self._exit(signal.price)

        elif signal.signal == Signal.EXIT_SHORT and self._position == "SHORT":
            self._exit(signal.price)

    # ── Square-off at end of day ───────────────────────────────────────────

    def square_off(self) -> None:
        if self._position:
            logger.info("End-of-day square-off — closing %s position.", self._position)
            ltp = self.client.get_ltp(config.SECURITY_ID, config.EXCHANGE_SEGMENT)
            price = ltp or self._entry_price
            self._exit(price)
        logger.info(
            "Day complete. Trades: %d | Daily PnL: %.2f",
            self.risk.trade_count,
            self.risk.daily_pnl,
        )

    # ── Scheduler ─────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            "Starting DEMA crossover trader | DEMA %d/%d/%d | %s | interval=%dm",
            config.DEMA_FAST, config.DEMA_SLOW, config.DEMA_TREND,
            config.TRADING_SYMBOL,
            config.CANDLE_INTERVAL,
        )

        interval = config.CANDLE_INTERVAL
        schedule.every(interval).minutes.do(self.tick)
        schedule.every().day.at(config.TRADE_CUTOFF).do(self.square_off)

        # Run immediately on start so we don't wait a full interval
        self.tick()

        while True:
            schedule.run_pending()
            time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description="DEMA crossover algo trader via Dhan API")
    parser.add_argument("--dry-run", action="store_true", help="Paper trade — no real orders")
    args = parser.parse_args()

    trader = Trader(dry_run=args.dry_run)
    trader.run()


if __name__ == "__main__":
    main()
