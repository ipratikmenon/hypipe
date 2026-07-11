"""PSR/DSR tests — including the manufactured 'Sharpe 3.5 from pure noise'
false positive that deflation must catch (the whole reason DSR exists)."""

import numpy as np
import pytest

from fx_commodities.backtest.stats import (
    dsr, expected_max_sharpe, ledger_stats, psr, record_trial,
    required_sharpe_bar, sharpe,
)


class TestPSR:
    def test_true_edge_scores_high(self):
        rng = np.random.default_rng(1)
        r = rng.normal(0.05, 0.1, 400)          # real, strong per-trade edge
        assert psr(r) > 0.99

    def test_zero_edge_scores_half(self):
        rng = np.random.default_rng(2)
        vals = [psr(rng.normal(0, 0.1, 300)) for _ in range(50)]
        assert 0.35 < float(np.mean(vals)) < 0.65

    def test_fat_tails_reduce_confidence(self):
        """Same Sharpe, fatter tails → PSR must drop (kurtosis term)."""
        rng = np.random.default_rng(3)
        normal_r = rng.normal(0.03, 0.1, 300)
        t_r = 0.03 + 0.1 * rng.standard_t(df=3, size=300) / np.sqrt(3)
        t_r = t_r * (normal_r.std() / t_r.std())         # match vol
        t_r = t_r - t_r.mean() + normal_r.mean()          # match mean/SR
        assert psr(t_r) < psr(normal_r)


class TestDeflation:
    def test_expected_max_grows_with_trials_and_variance(self):
        assert expected_max_sharpe(100, 0.01) > expected_max_sharpe(10, 0.01)
        assert expected_max_sharpe(100, 0.04) > expected_max_sharpe(100, 0.01)
        assert expected_max_sharpe(1, 0.01) == 0.0

    def test_sharpe_3_5_false_positive_is_killed(self):
        """THE user scenario: search 2,000 zero-skill strategies, take the
        best. Its annualized Sharpe exceeds 3.5 and naive PSR calls it
        significant — DSR, deflated by trial count AND trial variance,
        correctly reports it as luck."""
        rng = np.random.default_rng(42)
        n_trials, n_obs = 2000, 60
        trials = rng.normal(0.0, 0.01, size=(n_trials, n_obs))  # pure noise
        srs = np.array([sharpe(t) for t in trials])
        best = trials[np.argmax(srs)]

        ann_sharpe = sharpe(best) * np.sqrt(252)
        assert ann_sharpe > 3.5                   # looks spectacular…

        naive = psr(best, sr_star=0.0)
        assert naive > 0.95                       # …and naive PSR is fooled

        deflated = dsr(best, n_trials=n_trials, var_sr=float(np.var(srs, ddof=1)))
        assert deflated < 0.95                    # fails the deployment gate
        assert deflated < naive - 0.2             # the claim collapses
        # (measured: ann. Sharpe 9.15, naive PSR 0.9999, DSR 0.78 — a result
        # that would sail through any naive review is correctly rejected)

    def test_true_edge_survives_deflation(self):
        """Deflation must not kill genuine skill: a real edge evaluated once
        among the same 2,000-trial search still clears DSR."""
        rng = np.random.default_rng(7)
        real = rng.normal(0.008, 0.01, 500)       # SR≈0.8/√obs… strong, long
        noise_srs = rng.normal(0, 1 / np.sqrt(60), 2000)
        deflated = dsr(real, n_trials=2000, var_sr=float(np.var(noise_srs, ddof=1)))
        assert deflated > 0.95

    def test_required_bar_monotonicity(self):
        assert required_sharpe_bar(500, 0.02, 200) > required_sharpe_bar(50, 0.02, 200)
        assert required_sharpe_bar(500, 0.02, 800) < required_sharpe_bar(500, 0.02, 200)


class TestLedger:
    def test_record_and_stats_roundtrip(self, tmp_path):
        ledger = tmp_path / "trials.json"
        for i, s in enumerate([0.1, -0.05, 0.22, 0.03]):
            n = record_trial(ledger, f"EXP-{i}", s)
        assert n == 4
        count, var = ledger_stats(ledger)
        assert count == 4
        assert var == pytest.approx(np.var([0.1, -0.05, 0.22, 0.03], ddof=1))

    def test_bar_rises_as_ledger_grows(self, tmp_path):
        ledger = tmp_path / "trials.json"
        rng = np.random.default_rng(5)
        bars = []
        for i in range(300):
            record_trial(ledger, f"EXP-{i}", float(rng.normal(0, 0.12)))
            if i in (20, 100, 299):
                n, v = ledger_stats(ledger)
                bars.append(required_sharpe_bar(n, v, n_obs=250))
        assert bars[0] < bars[1] < bars[2]       # search more ⇒ bar rises
