"""Regime detection (REQUIREMENTS.md §8.3): 3-state Gaussian HMM + variance ratio.

CRITICAL no-lookahead note: hmmlearn's predict_proba runs forward-BACKWARD
(smoothing) — probabilities at t would use future observations. We use
hmmlearn ONLY to fit parameters (EM on a training window) and implement the
FORWARD pass manually, so filtered probabilities at t depend on x_{1..t} alone.
The truncation test in tests/ asserts this property directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import multivariate_normal


class RegimeHMM:
    """3 states, observation vector x_t = [bar log-return, log RV proxy]."""

    def __init__(self, n_states: int = 3, seed: int = 7):
        self.n_states = n_states
        self.seed = seed
        self.startprob_: np.ndarray | None = None
        self.transmat_: np.ndarray | None = None
        self.means_: np.ndarray | None = None
        self.covars_: np.ndarray | None = None
        self.state_names_: list[str] | None = None

    # ── fitting (EM via hmmlearn; parameters only) ────────────────────────

    def fit(self, obs: np.ndarray, n_restarts: int = 5) -> "RegimeHMM":
        """EM is a local optimizer — restart from several seeds and keep the
        best likelihood (a single unlucky init can merge regimes)."""
        from hmmlearn.hmm import GaussianHMM
        best, best_score = None, -np.inf
        for r in range(n_restarts):
            m = GaussianHMM(n_components=self.n_states,
                            covariance_type="full", n_iter=300,
                            random_state=self.seed + r)
            try:
                m.fit(obs)
                score = m.score(obs)
            except Exception:
                continue
            if score > best_score:
                best, best_score = m, score
        if best is None:
            raise RuntimeError("HMM fit failed on all restarts")
        self.startprob_ = best.startprob_
        self.transmat_ = best.transmat_
        self.means_ = best.means_
        self.covars_ = best.covars_
        self._name_states()
        return self

    def _name_states(self) -> None:
        """Post-hoc naming by moments (§8.3): highest obs-variance = crisis;
        of the rest, highest |mean return|/sigma = trend; remainder = chop."""
        total_var = np.array([np.trace(c) for c in self.covars_])
        crisis = int(np.argmax(total_var))
        rest = [k for k in range(self.n_states) if k != crisis]
        snr = [abs(self.means_[k][0]) / np.sqrt(self.covars_[k][0, 0])
               for k in rest]
        trend = rest[int(np.argmax(snr))]
        names = {}
        names[crisis] = "crisis"
        names[trend] = "trend"
        for k in rest:
            if k != trend:
                names[k] = "chop"
        self.state_names_ = [names[k] for k in range(self.n_states)]

    # ── forward pass (filtered, causal) ───────────────────────────────────

    def filtered_probs(self, obs: np.ndarray) -> pd.DataFrame:
        """alpha_t(k) = P(s_t=k | x_{1..t}) via the normalized forward
        recursion — never forward-backward."""
        if self.transmat_ is None:
            raise RuntimeError("fit() first")
        n, K = len(obs), self.n_states
        pdfs = np.column_stack([
            multivariate_normal(self.means_[k], self.covars_[k],
                                allow_singular=True).pdf(obs)
            for k in range(K)
        ])
        pdfs = np.maximum(pdfs.reshape(n, K), 1e-300)

        alpha = np.zeros((n, K))
        a = self.startprob_ * pdfs[0]
        alpha[0] = a / a.sum()
        for t in range(1, n):
            a = (alpha[t - 1] @ self.transmat_) * pdfs[t]
            alpha[t] = a / a.sum()

        cols = {f"p_{name}": alpha[:, k]
                for k, name in enumerate(self.state_names_)}
        df = pd.DataFrame(cols)
        top = alpha.argmax(axis=1)
        df["state_persistence"] = self.transmat_[top, top]
        return df


def regime_features(returns: pd.Series, log_rv: pd.Series,
                    model: RegimeHMM) -> pd.DataFrame:
    obs = np.column_stack([returns.to_numpy(float), log_rv.to_numpy(float)])
    out = model.filtered_probs(obs)
    out.index = returns.index
    return out


# ── Lo–MacKinlay variance ratio (§8.3b) ─────────────────────────────────────

def variance_ratio_features(close: pd.Series, q: int = 10,
                            window: int = 200) -> pd.DataFrame:
    """vr_10 = Var(q-bar returns)/(q·Var(1-bar)); z-stat under the
    homoskedastic null: z = (VR−1)·sqrt(3qn / (2(2q−1)(q−1)))."""
    r1 = np.log(close / close.shift(1))
    rq = np.log(close / close.shift(q))
    vr = rq.rolling(window).var() / (q * r1.rolling(window).var())
    z = (vr - 1.0) * np.sqrt(3.0 * q * window / (2.0 * (2 * q - 1) * (q - 1)))
    return pd.DataFrame({f"vr_{q}": vr, "vr_z": z}, index=close.index)
