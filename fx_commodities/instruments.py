"""Instrument universe resolution (REQUIREMENTS.md §2).

Contracts are resolved from the broker's live symbol specs at startup. Symbol
naming differs across brokers, so SYMBOL_MAP in .env maps canonical keys
("EURUSD", "XAUUSD", "WTI") to the broker's names; a bad mapping fails fast
listing near-miss candidates from the broker's symbol catalogue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .broker.base import BrokerAdapter, BrokerError, SymbolSpec

logger = logging.getLogger(__name__)

FX_KEYS = frozenset({"EURUSD", "GBPUSD", "USDJPY"})
METAL_KEYS = frozenset({"XAUUSD", "XAGUSD"})
COMMODITY_KEYS = frozenset({"WTI", "NATGAS", "COPPER"})

# USD-leg direction groups for the correlation guard (§9.3): a LONG in any of
# these keys is a short-USD bet; USDJPY inverts. Commodities count half-weight.
SHORT_USD_ON_LONG = frozenset({"EURUSD", "GBPUSD", "XAUUSD", "XAGUSD"})
USD_INVERTED = frozenset({"USDJPY"})
HALF_WEIGHT = COMMODITY_KEYS


@dataclass(frozen=True)
class Contract:
    key: str
    spec: SymbolSpec
    has_dom: bool = False
    has_last_ticks: bool = False
    has_real_volume: bool = False

    @property
    def broker_symbol(self) -> str:
        return self.spec.broker_symbol

    @property
    def is_fx(self) -> bool:
        return self.key in FX_KEYS

    @property
    def max_median_spread_pct(self) -> float:
        # thresholds injected by caller via config; class carries the class split
        return 0.03 if self.is_fx else 0.06


def load_capabilities(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def resolve_universe(
    adapter: BrokerAdapter,
    universe: tuple[str, ...],
    symbol_map: dict[str, str],
    capabilities: dict[str, dict] | None = None,
) -> list[Contract]:
    """Validate every configured key against the broker and build Contracts.

    Raises BrokerError listing near-miss broker symbols when a mapping is wrong,
    so the operator can fix SYMBOL_MAP without spelunking the terminal.
    """
    capabilities = capabilities or {}
    contracts: list[Contract] = []
    problems: list[str] = []

    for key in universe:
        if key not in symbol_map:
            problems.append(f"{key}: missing from SYMBOL_MAP")
            continue
        try:
            spec = adapter.symbol_spec(key)
        except BrokerError:
            candidates = adapter.find_symbols(f"*{key[:6]}*")
            problems.append(
                f"{key}={symbol_map[key]}: not found at broker. "
                f"Near matches: {candidates[:8] or 'none'}"
            )
            continue

        caps = capabilities.get(key, {})
        contracts.append(Contract(
            key=key,
            spec=spec,
            has_dom=bool(caps.get("has_dom", False)),
            has_last_ticks=bool(caps.get("has_last_ticks", False)),
            has_real_volume=bool(caps.get("has_real_volume", False)),
        ))

        if spec.expiration_ts:
            logger.warning(
                "%s (%s) carries an expiry — futures-based CFD; rollover "
                "handling required (§2.2)", key, spec.broker_symbol,
            )

    if problems:
        raise BrokerError("Universe resolution failed:\n  " + "\n  ".join(problems))

    logger.info("Universe resolved: %s", [c.key for c in contracts])
    return contracts
