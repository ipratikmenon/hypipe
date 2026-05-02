"""
OptionSelector
==============
Scans the weekly option chain for NIFTY, BANKNIFTY, MIDCPNIFTY, and FINNIFTY
and returns every contract whose cost (LTP × lot_size) fits inside the user's
MAX_LOSS_PER_TRADE budget.

The selected options are what the DEMA strategy will then run on — so the algo
always trades the cheapest viable contract rather than a hardcoded security_id.

Key design decisions
--------------------
* Lot sizes are fetched dynamically from the Dhan instrument master CSV so the
  code stays correct after SEBI-mandated lot-size revisions.
* Only the nearest weekly expiry (≥ today) is considered per index.
* A 3-second pause is inserted between option-chain calls to respect Dhan's
  rate limit of one unique request per 3 seconds per underlying/expiry pair.
* Both CE and PE are surfaced; the DEMA strategy decides which side to trade.
"""

import io
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pandas as pd
import requests

from dhan_client import DhanClient

logger = logging.getLogger(__name__)

# ── Index metadata ──────────────────────────────────────────────────────────
# UnderlyingScrip IDs are fixed by Dhan for index underlyings.
# Lot sizes are defaults; get_lot_sizes() overrides them from the live CSV.
INDEX_META: dict[str, dict] = {
    "NIFTY": {
        "scrip": 13,
        "segment": "IDX_I",
        "symbol_pattern": "NIFTY",
        "default_lot": 75,
    },
    "BANKNIFTY": {
        "scrip": 25,
        "segment": "IDX_I",
        "symbol_pattern": "BANKNIFTY",
        "default_lot": 30,
    },
    "MIDCPNIFTY": {
        "scrip": 442,
        "segment": "IDX_I",
        "symbol_pattern": "MIDCPNIFTY",
        "default_lot": 50,
    },
    "FINNIFTY": {
        "scrip": 27,
        "segment": "IDX_I",
        "symbol_pattern": "FINNIFTY",
        "default_lot": 40,
    },
}

_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
_OPTION_CHAIN_PAUSE = 3.5  # seconds — Dhan rate limit


@dataclass
class OptionCandidate:
    index: str                   # "NIFTY", "BANKNIFTY", etc.
    security_id: str
    trading_symbol: str
    strike: float
    option_type: str             # "CE" or "PE"
    expiry: str                  # "YYYY-MM-DD"
    lot_size: int
    ltp: float
    cost_per_lot: float          # ltp × lot_size
    bid: float = 0.0
    ask: float = 0.0
    oi: int = 0
    iv: float = 0.0

    @property
    def exchange_segment(self) -> str:
        return "NSE_FNO"

    @property
    def instrument_type(self) -> str:
        return "OPTIDX"

    def __str__(self) -> str:
        return (
            f"{self.index} {self.strike}{self.option_type} exp={self.expiry} "
            f"LTP={self.ltp:.2f} lot={self.lot_size} cost=₹{self.cost_per_lot:.0f}"
        )


class OptionSelector:
    def __init__(self, client: DhanClient, budget: float):
        """
        client : DhanClient instance
        budget : maximum cost per trade in ₹ (= MAX_LOSS_PER_TRADE from config)
                 Only options where LTP × lot_size ≤ budget are returned.
        """
        self.client = client
        self.budget = budget
        self._lot_sizes: dict[str, int] = {}  # index_name → current lot size
        self._lot_sizes_fetched_on: Optional[date] = None

    # ── Lot size resolution ────────────────────────────────────────────────

    def _fetch_lot_sizes(self) -> dict[str, int]:
        """
        Download the Dhan scrip master CSV once per day and extract current
        lot sizes for each index.  Falls back to hardcoded defaults on failure.
        """
        today = date.today()
        if self._lot_sizes and self._lot_sizes_fetched_on == today:
            return self._lot_sizes

        try:
            resp = requests.get(_SCRIP_MASTER_URL, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text), low_memory=False)

            sizes: dict[str, int] = {}
            for idx_name, meta in INDEX_META.items():
                pattern = meta["symbol_pattern"]
                # Filter to FNO segment, options, matching symbol
                mask = (
                    df["SEM_SEGMENT"].astype(str).str.contains("D", na=False)
                    & df["SEM_OPTION_TYPE"].astype(str).isin(["CE", "PE"])
                    & df["SM_SYMBOL_NAME"].astype(str).str.startswith(pattern)
                )
                subset = df[mask]
                if not subset.empty:
                    lot = int(subset["SEM_LOT_UNITS"].iloc[0])
                    sizes[idx_name] = lot
                    logger.info("Lot size from CSV: %s = %d", idx_name, lot)
                else:
                    sizes[idx_name] = meta["default_lot"]
                    logger.warning(
                        "Could not find %s in scrip master, using default lot %d",
                        idx_name, meta["default_lot"],
                    )

            self._lot_sizes = sizes
            self._lot_sizes_fetched_on = today
        except Exception as exc:
            logger.warning("Scrip master download failed (%s), using defaults.", exc)
            self._lot_sizes = {k: v["default_lot"] for k, v in INDEX_META.items()}

        return self._lot_sizes

    # ── Expiry resolution ──────────────────────────────────────────────────

    def _nearest_weekly_expiry(self, expiries: list[str]) -> Optional[str]:
        """
        From a sorted list of expiry date strings, return the nearest one
        that is today or in the future.
        """
        today = date.today()
        for exp_str in expiries:
            try:
                exp_date = date.fromisoformat(exp_str[:10])
                if exp_date >= today:
                    return exp_str[:10]
            except ValueError:
                continue
        return None

    # ── Option chain scan ──────────────────────────────────────────────────

    def get_affordable_options(
        self,
        option_types: tuple[str, ...] = ("CE", "PE"),
        indices: Optional[list[str]] = None,
    ) -> list[OptionCandidate]:
        """
        Scan the weekly option chain for every index (or a subset) and return
        OptionCandidate objects where cost_per_lot ≤ budget.

        option_types : which sides to include — ("CE",), ("PE",), or ("CE","PE")
        indices      : subset of INDEX_META keys; defaults to all four
        """
        lot_sizes = self._fetch_lot_sizes()
        indices_to_scan = indices or list(INDEX_META.keys())
        candidates: list[OptionCandidate] = []

        for idx_name in indices_to_scan:
            meta = INDEX_META[idx_name]
            lot = lot_sizes.get(idx_name, meta["default_lot"])

            try:
                expiries = self.client.get_expiry_list(meta["scrip"], meta["segment"])
            except Exception as exc:
                logger.error("Expiry list failed for %s: %s", idx_name, exc)
                continue

            expiry = self._nearest_weekly_expiry(expiries)
            if not expiry:
                logger.warning("No upcoming expiry found for %s", idx_name)
                continue

            logger.info("Scanning %s weekly expiry %s (lot=%d, budget=₹%.0f)",
                        idx_name, expiry, lot, self.budget)

            try:
                chain = self.client.get_option_chain(meta["scrip"], meta["segment"], expiry)
            except Exception as exc:
                logger.error("Option chain fetch failed for %s: %s", idx_name, exc)
                time.sleep(_OPTION_CHAIN_PAUSE)
                continue

            for strike_str, sides in chain.items():
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue

                for ot in option_types:
                    opt = sides.get(ot.lower())
                    if not opt:
                        continue

                    ltp = float(opt.get("last_price", 0) or 0)
                    if ltp <= 0:
                        continue

                    cost = ltp * lot
                    if cost > self.budget:
                        continue

                    security_id = str(opt.get("security_id", ""))
                    if not security_id:
                        continue

                    candidates.append(
                        OptionCandidate(
                            index=idx_name,
                            security_id=security_id,
                            trading_symbol=f"{idx_name}{expiry.replace('-','')}{int(strike)}{ot}",
                            strike=strike,
                            option_type=ot,
                            expiry=expiry,
                            lot_size=lot,
                            ltp=ltp,
                            cost_per_lot=cost,
                            bid=float(opt.get("top_bid_price", 0) or 0),
                            ask=float(opt.get("top_ask_price", 0) or 0),
                            oi=int(opt.get("oi", 0) or 0),
                            iv=float(opt.get("implied_volatility", 0) or 0),
                        )
                    )

            # Respect Dhan's rate limit between chain calls
            time.sleep(_OPTION_CHAIN_PAUSE)

        candidates.sort(key=lambda c: c.cost_per_lot, reverse=True)
        logger.info(
            "Found %d affordable option(s) across %s within ₹%.0f budget",
            len(candidates), indices_to_scan, self.budget,
        )
        return candidates

    def refresh_ltp(self, candidates: list[OptionCandidate]) -> list[OptionCandidate]:
        """
        Bulk-refresh the LTP for a list of candidates in one API call and
        filter out any that now exceed the budget.
        """
        if not candidates:
            return []

        sec_ids = [c.security_id for c in candidates]
        try:
            ltp_data = self.client.get_ltp_bulk({"NSE_FNO": sec_ids})
            fno_data = ltp_data.get("NSE_FNO", {})
        except Exception as exc:
            logger.warning("Bulk LTP refresh failed: %s", exc)
            return candidates

        updated = []
        for c in candidates:
            entry = fno_data.get(c.security_id, {})
            new_ltp = float(entry.get("last_price", c.ltp) or c.ltp)
            new_cost = new_ltp * c.lot_size
            if new_cost <= self.budget and new_ltp > 0:
                c.ltp = new_ltp
                c.cost_per_lot = new_cost
                updated.append(c)

        return updated
