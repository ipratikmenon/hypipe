"""Anti-overfitting statistics (REQUIREMENTS.md §10.3, PHASE3_PLAN Step 12).

Bailey & López de Prado:
- PSR — Probabilistic Sharpe Ratio: P(true SR > SR*) given sample length and
  the non-normality of returns.
- DSR — Deflated Sharpe Ratio: PSR evaluated against SR*, the Sharpe you'd
  expect the BEST of N random trials to show. Deflation uses both the number
  of trials and their variance: search harder (larger N) or noisier (larger
  Var(SR)) and the bar rises. This is the defense against the "Sharpe 3.5
  found after 2,000 backtests" false positive — a high Sharpe means nothing
  until it clears what luck alone produces at that search intensity.

All Sharpe values here are per-observation (per-trade or per-day) — annualize
only for display, never before testing.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.stats import norm

EULER_GAMMA = 0.5772156649015329


def sharpe(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    s = r.std(ddof=1)
    return float(r.mean() / s) if s > 0 else 0.0


def psr(returns: np.ndarray, sr_star: float = 0.0) -> float:
    """P(true SR > sr_star). Accounts for skew/kurtosis — fat-tailed trade
    returns make an observed SR less trustworthy than normal ones."""
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n < 3:
        return 0.0
    sr = sharpe(r)
    mu, sd = r.mean(), r.std(ddof=1)
    if sd == 0:
        return 0.0
    z = (r - mu) / sd
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())            # non-excess (normal → 3)
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2
    if denom <= 0:
        return 0.0
    stat = (sr - sr_star) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(norm.cdf(stat))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of n_trials zero-skill strategies whose SR estimates have
    variance var_sr — the benchmark luck sets. Grows with BOTH arguments."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    n = max(n_trials, 2)
    return float(np.sqrt(var_sr) * (
        (1.0 - EULER_GAMMA) * norm.ppf(1.0 - 1.0 / n)
        + EULER_GAMMA * norm.ppf(1.0 - 1.0 / (n * np.e))
    ))


def dsr(returns: np.ndarray, n_trials: int, var_sr: float) -> float:
    """Deflated Sharpe Ratio: PSR against the luck benchmark. DSR ≈ 0.5 means
    'exactly as good as the best of N random tries' — i.e. nothing."""
    return psr(returns, sr_star=expected_max_sharpe(n_trials, var_sr))


def required_sharpe_bar(n_trials: int, var_sr: float, n_obs: int,
                        confidence: float = 0.95) -> float:
    """The acceptance bar for the autoresearch loop (`harness ledger-bar`):
    the per-observation SR a candidate must exceed so that DSR ≥ confidence,
    under approximate normality. Rises with trials count and trial variance;
    falls as evaluation length n_obs grows."""
    sr_star = expected_max_sharpe(n_trials, var_sr)
    if n_obs < 3:
        return float("inf")
    return float(sr_star + norm.ppf(confidence) / np.sqrt(n_obs - 1))


# ── trial ledger integration (evolve/ledger/trials.json) ───────────────────

def ledger_stats(ledger_path: str | Path) -> tuple[int, float]:
    """Returns (n_trials, var_sr) from the append-only ledger. Variance of
    recorded trial Sharpes IS the var_sr the deflator needs — the more widely
    our own experiments scatter, the higher the luck benchmark."""
    p = Path(ledger_path)
    if not p.exists():
        return 0, 0.0
    data = json.loads(p.read_text())
    srs = [t["sharpe"] for t in data.get("trials", [])
           if isinstance(t.get("sharpe"), (int, float))]
    n = int(data.get("count", len(data.get("trials", []))))
    var = float(np.var(srs, ddof=1)) if len(srs) >= 2 else 0.0
    return max(n, len(srs)), var


def record_trial(ledger_path: str | Path, exp_id: str, sharpe_val: float,
                 meta: dict | None = None) -> int:
    """Append one trial. Returns the new count. Append-only by construction —
    trials are never removed; a deleted failure is a manufactured success."""
    p = Path(ledger_path)
    data = (json.loads(p.read_text()) if p.exists()
            else {"trials": [], "count": 0})
    data["trials"].append({"exp_id": exp_id, "sharpe": float(sharpe_val),
                           **(meta or {})})
    data["count"] = int(data.get("count", 0)) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    return data["count"]
