"""Frozen settings loaded from .env / environment.

Defaults encode the D5 operating profile from REQUIREMENTS.md §0.5:
low capital, high win rate via asymmetric barriers (PT 0.8 / SL 1.6 ATR),
extreme selectivity (EDGE_MARGIN 0.07, CONFORMAL_EPS 0.15), and a hard
2-trades-per-day backstop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def _symbol_map(raw: str) -> dict[str, str]:
    """Parse 'EURUSD=EURUSD.r,XAUUSD=GOLD' → {key: broker_symbol}."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"SYMBOL_MAP entry '{pair}' must be KEY=BROKER_SYMBOL")
        key, sym = pair.split("=", 1)
        out[key.strip().upper()] = sym.strip()
    return out


_DEFAULT_SYMBOL_MAP = (
    "EURUSD=EURUSD,GBPUSD=GBPUSD,USDJPY=USDJPY,"
    "XAUUSD=XAUUSD,XAGUSD=XAGUSD,WTI=USOIL,NATGAS=NATGAS,COPPER=COPPER"
)


@dataclass(frozen=True)
class Settings:
    # broker
    mt5_terminal_path: str = os.getenv("MT5_TERMINAL_PATH", "")
    mt5_login: int = _i("MT5_LOGIN", 0)
    mt5_password: str = os.getenv("MT5_PASSWORD", "")
    mt5_server: str = os.getenv("MT5_SERVER", "")
    magic: int = _i("MAGIC", 520025)
    symbol_map: dict[str, str] = field(
        default_factory=lambda: _symbol_map(os.getenv("SYMBOL_MAP", _DEFAULT_SYMBOL_MAP))
    )

    # capital — low-capital profile defaults (account currency)
    capital_total: float = _f("CAPITAL_TOTAL", 1000.0)
    alloc_fx_commodities: float = _f("ALLOC_FX_COMMODITIES", 1.0)
    risk_per_trade_pct: float = _f("RISK_PER_TRADE_PCT", 1.0)
    max_daily_loss_pct: float = _f("MAX_DAILY_LOSS_PCT", 2.0)
    max_daily_loss_per_symbol_pct: float = _f("MAX_DAILY_LOSS_PER_SYMBOL_PCT", 1.0)
    max_lots: float = _f("MAX_LOTS", 0.10)
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 2)
    max_correlated_positions: int = _i("MAX_CORRELATED_POSITIONS", 1)
    max_trades_per_day: int = _i("MAX_TRADES_PER_DAY", 2)
    kelly_fraction: float = _f("KELLY_FRACTION", 0.5)

    # universe / sessions
    universe: tuple[str, ...] = tuple(
        s.strip().upper()
        for s in os.getenv(
            "UNIVERSE", "EURUSD,GBPUSD,USDJPY,XAUUSD,XAGUSD,WTI,NATGAS,COPPER"
        ).split(",")
        if s.strip()
    )
    max_median_spread_pct_fx: float = _f("MAX_MEDIAN_SPREAD_PCT_FX", 0.03)
    max_median_spread_pct_cmd: float = _f("MAX_MEDIAN_SPREAD_PCT_CMD", 0.06)
    max_live_spread_pct: float = _f("MAX_LIVE_SPREAD_PCT", 0.08)
    allow_overnight: bool = _b("ALLOW_OVERNIGHT", False)
    square_off_min_before_swap: int = _i("SQUARE_OFF_MIN_BEFORE_SWAP", 15)
    skip_min_after_open: int = _i("SKIP_MIN_AFTER_OPEN", 30)

    # strategy / model — high-win-rate profile (§0.5): inverted barriers
    bar_interval_s: int = _i("BAR_INTERVAL_S", 60)
    pt_mult: float = _f("PT_MULT", 0.8)
    sl_mult: float = _f("SL_MULT", 1.6)
    max_hold_bars: int = _i("MAX_HOLD_BARS", 60)
    edge_margin: float = _f("EDGE_MARGIN", 0.07)
    primary_delta: float = _f("PRIMARY_DELTA", 0.03)
    conformal_eps: float = _f("CONFORMAL_EPS", 0.15)
    imbalance_ratio: float = _f("IMBALANCE_RATIO", 3.0)
    trail_mult: float = _f("TRAIL_MULT", 1.5)
    kalman_q: float = _f("KALMAN_Q", 1e-5)
    kalman_r: float = _f("KALMAN_R", 1e-2)
    embargo_pct: float = _f("EMBARGO_PCT", 0.01)
    hmm_states: int = _i("HMM_STATES", 3)
    hmm_window_bars: int = _i("HMM_WINDOW_BARS", 2000)
    har_refit_days: int = _i("HAR_REFIT_DAYS", 7)
    ffd_adf_alpha: float = _f("FFD_ADF_ALPHA", 0.05)
    drift_psi_warn: float = _f("DRIFT_PSI_WARN", 0.25)
    drift_psi_features_block: int = _i("DRIFT_PSI_FEATURES_BLOCK", 6)

    # zone-confirmation gate (§9.1b)
    zone_width_atr: float = _f("ZONE_WIDTH_ATR", 0.25)
    zone_round_grid_points: int = _i("ZONE_ROUND_GRID_POINTS", 0)  # 0 = off
    n_confirm: int = _i("N_CONFIRM", 2)
    confirm_violate_frac: float = _f("CONFIRM_VIOLATE_FRAC", 0.5)
    entry_window_bars: int = _i("ENTRY_WINDOW_BARS", 3)
    min_wick_ratio: float = _f("MIN_WICK_RATIO", 0.5)
    pivot_n: int = _i("PIVOT_N", 3)

    # data
    data_root: Path = Path(os.getenv("DATA_ROOT", "./data_root"))
    flush_interval_s: int = _i("FLUSH_INTERVAL_S", 60)
    poll_interval_ms: int = _i("POLL_INTERVAL_MS", 200)
    dom_snapshot_interval_ms: int = _i("DOM_SNAPSHOT_INTERVAL_MS", 500)

    # costs
    commission_per_lot_side: float = _f("COMMISSION_PER_LOT_SIDE", 3.5)
    default_slippage_points: int = _i("DEFAULT_SLIPPAGE_POINTS", 2)

    # ops
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "")
    signal_webhook_url: str = os.getenv("SIGNAL_WEBHOOK_URL", "")
    health_port: int = _i("HEALTH_PORT", 8081)
    journal_path: Path = Path(os.getenv("JOURNAL_PATH", "./journal.db"))
    reports_dir: Path = Path(os.getenv("REPORTS_DIR", "./reports"))

    @property
    def module_capital(self) -> float:
        return self.capital_total * self.alloc_fx_commodities

    def require_broker_credentials(self) -> None:
        missing = [n for n, v in (
            ("MT5_LOGIN", self.mt5_login),
            ("MT5_PASSWORD", self.mt5_password),
            ("MT5_SERVER", self.mt5_server),
        ) if not v]
        if missing:
            raise RuntimeError(
                f"Missing broker credentials in .env: {', '.join(missing)}"
            )


def load_settings() -> Settings:
    return Settings()
