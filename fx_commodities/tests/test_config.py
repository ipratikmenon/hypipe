"""Config parsing + the D5 operating-profile math (REQUIREMENTS.md §0.5, §8.0)."""

import pytest

from fx_commodities.config import Settings, _symbol_map, load_settings


class TestSymbolMap:
    def test_parses_pairs(self):
        m = _symbol_map("EURUSD=EURUSD.r, XAUUSD=GOLD ,WTI=USOIL")
        assert m == {"EURUSD": "EURUSD.r", "XAUUSD": "GOLD", "WTI": "USOIL"}

    def test_rejects_malformed_entry(self):
        with pytest.raises(ValueError, match="KEY=BROKER_SYMBOL"):
            _symbol_map("EURUSD")


class TestD5Profile:
    """The high-win-rate profile must be internally consistent (§0.5)."""

    def test_defaults_encode_inverted_barriers(self):
        cfg = load_settings()
        assert cfg.pt_mult < cfg.sl_mult          # small target, wide stop
        assert cfg.max_trades_per_day == 2

    def test_breakeven_probability_math(self):
        """p* = (σ + c)/(π + σ) with the profile's barriers and typical cost."""
        cfg = load_settings()
        c = 0.15                                   # typical round-trip, ATR units
        p_star = (cfg.sl_mult + c) / (cfg.pt_mult + cfg.sl_mult)
        assert p_star == pytest.approx(0.729, abs=0.001)
        # entry threshold ≈ 0.80 → the 85% target has real margin over it
        assert p_star + cfg.edge_margin < 0.85

    def test_expectancy_positive_at_target_win_rate(self):
        cfg = load_settings()
        p, c = 0.85, 0.15
        ev = p * cfg.pt_mult - (1 - p) * cfg.sl_mult - c
        assert ev == pytest.approx(0.29, abs=0.01)  # +0.29 ATR per trade
        # and negative at 70% — the asymmetry cuts both ways; this is the
        # honesty check that guards against loosening thresholds for volume
        ev_70 = 0.70 * cfg.pt_mult - 0.30 * cfg.sl_mult - c
        assert ev_70 < 0

    def test_kelly_fraction_at_profile_point(self):
        """f* = (p(b+1) − 1)/b with b = π/σ; sanity at the operating point."""
        cfg = load_settings()
        b = cfg.pt_mult / cfg.sl_mult              # 0.5 reward:risk odds
        p = 0.85
        f_star = (p * (b + 1) - 1) / b
        assert f_star == pytest.approx(0.55, abs=0.01)
        f = cfg.kelly_fraction * f_star            # half-Kelly
        # the hard per-trade cap must bind — small accounts never bet Kelly-size
        assert min(f, cfg.risk_per_trade_pct / 100) == cfg.risk_per_trade_pct / 100

    def test_module_capital_split(self):
        cfg = Settings()
        assert cfg.module_capital == cfg.capital_total * cfg.alloc_fx_commodities
