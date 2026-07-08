"""
Main trading loop.

Run:
    python trader.py            # live trading
    python trader.py --dry-run  # paper mode (signals printed, no orders placed)

Flow each tick
--------------
1. OptionSelector scans all 4 index weekly chains and returns every option
   whose LTP × lot_size fits within MAX_LOSS_PER_TRADE.
2. The LTP list is refreshed cheaply via a single bulk call.
3. For each affordable candidate the DEMA strategy is evaluated on 2-min candles.
4. The first high-confidence signal (or any BUY/SELL if none is high-confidence)
   is acted upon.  Only one open position is held at a time.
"""

import argparse
import logging
import sys
import time
from typing import Optional

import pytz
import schedule
from datetime import datetime, date

from config import config
from dhan_client import DhanClient
from option_selector import OptionCandidate, OptionSelector
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
            logger.warning(
                "Daily loss limit hit (₹%.2f). No more trades today.", self.daily_pnl
            )
            return False
        return True

    def record_trade(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.trade_count += 1
        logger.info(
            "Trade #%d | PnL: ₹%.2f | Daily PnL: ₹%.2f",
            self.trade_count, pnl, self.daily_pnl,
        )


class Trader:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.client = DhanClient()
        self.selector = OptionSelector(self.client, budget=config.MAX_LOSS_PER_TRADE)
        self.strategy = DEMACrossoverStrategy(
            fast=config.DEMA_FAST,
            slow=config.DEMA_SLOW,
            trend=config.DEMA_TREND,
        )
        self.risk = RiskManager()
        self.tz = pytz.timezone(config.TIMEZONE)

        # Current open position state
        self._position: Optional[str] = None        # "LONG" | "SHORT" | None
        self._instrument: Optional[OptionCandidate] = None
        self._entry_price: float = 0.0
        self._stop_loss: float = 0.0
        self._target: float = 0.0

        # Cache affordable options; refreshed at start of each day
        self._candidates: list[OptionCandidate] = []
        self._candidates_date: Optional[date] = None

        if dry_run:
            logger.info("*** DRY RUN MODE — no real orders will be placed ***")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _market_is_open(self) -> bool:
        now = self._now()
        oh, om = map(int, config.MARKET_OPEN.split(":"))
        ch, cm = map(int, config.TRADE_CUTOFF.split(":"))
        opens_at = now.replace(hour=oh, minute=om, second=0, microsecond=0)
        cuts_at  = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        return opens_at <= now <= cuts_at

    def _get_candidates(self) -> list[OptionCandidate]:
        """
        Scan the option chains once per trading day; bulk-refresh LTP on
        every subsequent tick to stay current.
        """
        today = date.today()
        if self._candidates_date != today:
            logger.info("Scanning option chains for affordable contracts…")
            self._candidates = self.selector.get_affordable_options(
                option_types=tuple(config.OPTION_TYPES),
                indices=config.INDICES or None,
            )
            self._candidates_date = today
            logger.info("%d candidate(s) found within ₹%.0f budget.",
                        len(self._candidates), config.MAX_LOSS_PER_TRADE)
        else:
            self._candidates = self.selector.refresh_ltp(self._candidates)

        return self._candidates

    # ── Order helpers ──────────────────────────────────────────────────────

    def _enter(self, instrument: OptionCandidate, side: str, price: float) -> None:
        """Open a new position (LONG = buy calls, SHORT = buy puts)."""
        if side == "LONG":
            sl     = round(price - config.MAX_LOSS_PER_TRADE / instrument.lot_size, 2)
            target = round(price + (price - sl) * config.PROFIT_TARGET_MULTIPLIER, 2)
            tx     = "BUY"
        else:
            sl     = round(price + config.MAX_LOSS_PER_TRADE / instrument.lot_size, 2)
            target = round(price - (sl - price) * config.PROFIT_TARGET_MULTIPLIER, 2)
            tx     = "SELL"

        logger.info(
            "ENTER %s %s @ ₹%.2f | lot=%d cost=₹%.0f | SL=%.2f Target=%.2f",
            side, instrument, price, instrument.lot_size, instrument.cost_per_lot, sl, target,
        )

        if not self.dry_run:
            resp = self.client.place_market_order(
                security_id=instrument.security_id,
                exchange_segment=instrument.exchange_segment,
                transaction_type=tx,
                quantity=instrument.lot_size,
                product_type=config.PRODUCT_TYPE,
            )
            logger.info("Order response: %s", resp)

        self._position    = side
        self._instrument  = instrument
        self._entry_price = price
        self._stop_loss   = sl
        self._target      = target

    def _exit(self, price: float, reason: str = "") -> None:
        if not self._instrument or not self._position:
            return

        pnl = (
            (price - self._entry_price) * self._instrument.lot_size
            if self._position == "LONG"
            else (self._entry_price - price) * self._instrument.lot_size
        )
        label = f" [{reason}]" if reason else ""
        logger.info(
            "EXIT %s%s %s @ ₹%.2f | Entry=₹%.2f | PnL=₹%.2f",
            self._position, label, self._instrument, price, self._entry_price, pnl,
        )

        if not self.dry_run:
            tx = "SELL" if self._position == "LONG" else "BUY"
            resp = self.client.place_market_order(
                security_id=self._instrument.security_id,
                exchange_segment=self._instrument.exchange_segment,
                transaction_type=tx,
                quantity=self._instrument.lot_size,
                product_type=config.PRODUCT_TYPE,
            )
            logger.info("Order response: %s", resp)

        self.risk.record_trade(pnl)
        self._position   = None
        self._instrument = None
        self._entry_price = 0.0

    # ── Stop-loss / target check (intrabar via LTP) ────────────────────────

    def _check_sl_target(self) -> bool:
        if not self._instrument or not self._position:
            return False
        ltp = self.client.get_ltp(
            self._instrument.security_id, self._instrument.exchange_segment
        )
        if ltp is None:
            return False

        if self._position == "LONG":
            if ltp <= self._stop_loss:
                self._exit(ltp, "SL")
                return True
            if ltp >= self._target:
                self._exit(ltp, "TARGET")
                return True
        else:
            if ltp >= self._stop_loss:
                self._exit(ltp, "SL")
                return True
            if ltp <= self._target:
                self._exit(ltp, "TARGET")
                return True

        return False

    # ── Signal evaluation across all candidates ────────────────────────────

    def _best_signal(
        self, candidates: list[OptionCandidate]
    ) -> Optional[tuple[TradeSignal, OptionCandidate]]:
        """
        Evaluate DEMA strategy for every affordable option.
        Returns the highest-priority (signal, instrument) pair:
          priority: HIGH_CONFIDENCE BUY/SELL > regular BUY/SELL > HOLD
        """
        high_conf: list[tuple[TradeSignal, OptionCandidate]] = []
        normal:    list[tuple[TradeSignal, OptionCandidate]] = []

        for cand in candidates:
            df = self.client.get_candles(
                security_id=cand.security_id,
                exchange_segment=cand.exchange_segment,
                instrument_type=cand.instrument_type,
                interval=config.CANDLE_INTERVAL,
            )
            if df.empty or len(df) < config.MIN_CANDLES:
                continue

            sig = self.strategy.evaluate(df, position=None)

            if sig.signal in (Signal.BUY, Signal.SELL):
                if sig.high_confidence:
                    high_conf.append((sig, cand))
                else:
                    normal.append((sig, cand))

        if high_conf:
            return high_conf[0]
        if normal:
            return normal[0]
        return None

    # ── Core tick ──────────────────────────────────────────────────────────

    def tick(self) -> None:
        if not self._market_is_open():
            logger.debug("Market closed — skipping tick.")
            return

        # ── Check existing position first ──────────────────────────────────
        if self._position and self._instrument:
            if self._check_sl_target():
                return

            # Evaluate exit signal on the current held instrument
            df = self.client.get_candles(
                security_id=self._instrument.security_id,
                exchange_segment=self._instrument.exchange_segment,
                instrument_type=self._instrument.instrument_type,
                interval=config.CANDLE_INTERVAL,
            )
            if not df.empty and len(df) >= config.MIN_CANDLES:
                sig = self.strategy.evaluate(df, position=self._position)
                logger.info("Exit-check signal for %s: %s", self._instrument, sig)

                if sig.signal == Signal.EXIT_LONG and self._position == "LONG":
                    self._exit(sig.price, "DEMA CROSS")
                elif sig.signal == Signal.EXIT_SHORT and self._position == "SHORT":
                    self._exit(sig.price, "DEMA CROSS")

        # ── Look for new entry ─────────────────────────────────────────────
        if self._position:  # still in a trade, don't stack
            return

        if not self.risk.can_trade():
            return

        candidates = self._get_candidates()
        if not candidates:
            logger.warning("No affordable options found within ₹%.0f budget.", config.MAX_LOSS_PER_TRADE)
            return

        result = self._best_signal(candidates)
        if result is None:
            logger.info("No actionable signal this tick.")
            return

        sig, instrument = result
        logger.info("Acting on signal: %s → %s", sig, instrument)

        if sig.signal == Signal.BUY:
            self._enter(instrument, "LONG", sig.price)
        elif sig.signal == Signal.SELL:
            self._enter(instrument, "SHORT", sig.price)

    # ── EOD square-off ─────────────────────────────────────────────────────

    def square_off(self) -> None:
        if self._position and self._instrument:
            logger.info("EOD square-off — closing %s on %s.", self._position, self._instrument)
            ltp = self.client.get_ltp(
                self._instrument.security_id, self._instrument.exchange_segment
            )
            self._exit(ltp or self._entry_price, "EOD")

        logger.info(
            "Day complete. Trades: %d | Daily PnL: ₹%.2f",
            self.risk.trade_count, self.risk.daily_pnl,
        )

    # ── Scheduler ─────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            "Starting DEMA crossover trader | DEMA %d/%d/%d | "
            "Budget ₹%.0f/trade | Indices: %s | interval=%dm",
            config.DEMA_FAST, config.DEMA_SLOW, config.DEMA_TREND,
            config.MAX_LOSS_PER_TRADE,
            ", ".join(config.INDICES) if config.INDICES else "ALL",
            config.CANDLE_INTERVAL,
        )

        schedule.every(config.CANDLE_INTERVAL).minutes.do(self.tick)
        schedule.every().day.at(config.TRADE_CUTOFF).do(self.square_off)

        self.tick()  # run immediately on start

        while True:
            schedule.run_pending()
            time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description="DEMA crossover algo trader (Dhan API)")
    parser.add_argument("--dry-run", action="store_true", help="Paper trade — no real orders")
    args = parser.parse_args()
    Trader(dry_run=args.dry_run).run()


if __name__ == "__main__":
    main()
