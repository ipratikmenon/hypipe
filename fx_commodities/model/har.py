"""HAR-RV volatility forecaster (REQUIREMENTS.md §8.2).

HAR (Corsi 2009): tomorrow's realized variance regressed on today's, this
week's mean, and this month's mean — fitted in log space to stabilize
heteroskedasticity. Volatility is the most predictable market quantity; this
model sets barrier widths and the sizing denominator.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def realized_variance(bars: pd.DataFrame) -> pd.Series:
    """Daily RV = sum of squared 1-bar log mid returns per UTC day."""
    r = np.log(bars["close"] / bars["close"].shift(1))
    day = bars["ts_utc"].dt.strftime("%Y-%m-%d")
    rv = (r ** 2).groupby(day).sum()
    rv.index = pd.to_datetime(rv.index, utc=True)
    return rv.replace(0, np.nan).dropna()


def _har_design(log_rv: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """X rows: [1, RV_d, RV_w, RV_m] (all in log), y: next-day log RV."""
    rv_d = log_rv.shift(0)
    rv_w = log_rv.rolling(5).mean()
    rv_m = log_rv.rolling(22).mean()
    df = pd.DataFrame({"d": rv_d, "w": rv_w, "m": rv_m,
                       "y": log_rv.shift(-1)}).dropna()
    X = np.column_stack([np.ones(len(df)), df["d"], df["w"], df["m"]])
    return X, df["y"].to_numpy()


class HarRV:
    """OLS HAR on log RV. Refit weekly (HAR_REFIT_DAYS) by the caller."""

    def __init__(self) -> None:
        self.beta: np.ndarray | None = None

    def fit(self, rv: pd.Series) -> "HarRV":
        log_rv = np.log(rv.dropna())
        if len(log_rv) < 30:
            raise ValueError(f"HAR needs >=30 daily RV points, got {len(log_rv)}")
        X, y = _har_design(log_rv)
        self.beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        return self

    def forecast(self, rv: pd.Series) -> float:
        """Next-period RV forecast (linear space) from the series tail."""
        if self.beta is None:
            raise RuntimeError("fit() first")
        log_rv = np.log(rv.dropna())
        x = np.array([1.0, log_rv.iloc[-1],
                      log_rv.iloc[-5:].mean(), log_rv.iloc[-22:].mean()])
        return float(np.exp(x @ self.beta))

    def forecast_series(self, rv: pd.Series) -> pd.Series:
        """In-sample-style rolling forecasts (uses data ≤ t only per point) —
        for feature columns rv_forecast / vol_surprise."""
        if self.beta is None:
            raise RuntimeError("fit() first")
        log_rv = np.log(rv.dropna())
        rv_w = log_rv.rolling(5).mean()
        rv_m = log_rv.rolling(22).mean()
        X = np.column_stack([np.ones(len(log_rv)), log_rv, rv_w, rv_m])
        pred = np.exp(X @ self.beta)
        out = pd.Series(pred, index=log_rv.index).shift(1)  # forecast FOR t uses ≤ t−1
        return out


def oos_r2_vs_random_walk(rv: pd.Series, train_frac: float = 0.7) -> dict:
    """Acceptance metric (§8.2): HAR must beat the RW forecast RV̂_{t+1}=RV_t.
    Returns log-space OOS R² of both."""
    log_rv = np.log(rv.dropna())
    split = int(len(log_rv) * train_frac)
    model = HarRV().fit(np.exp(log_rv.iloc[:split]))

    X, y = _har_design(log_rv)
    # align design rows to positions ≥ split (design drops first 21 + last 1)
    offset = len(log_rv) - len(y) - 1
    test_pos = np.arange(len(y)) + offset >= split
    y_true = y[test_pos]
    y_har = (X @ model.beta)[test_pos]
    y_rw = X[test_pos, 1]                     # log RV_d as the naive forecast

    ss = lambda e: float(np.sum(e ** 2))
    var = ss(y_true - y_true.mean())
    return {"r2_har": 1 - ss(y_true - y_har) / var,
            "r2_rw": 1 - ss(y_true - y_rw) / var,
            "n_test": int(test_pos.sum())}


def har_atr(atr: pd.Series, bars: pd.DataFrame, rv_forecast_daily: pd.Series,
            bars_per_day: int = 1440) -> pd.Series:
    """Barrier/sizing ATR (§8.1, §9.3): max(trailing ATR, forecast-implied
    per-bar sigma). Keeps barriers honest when vol regime shifts faster than
    the trailing window."""
    day = bars["ts_utc"].dt.normalize()
    fc = day.map(rv_forecast_daily).astype(float)
    implied_bar_sigma = np.sqrt(fc / bars_per_day) * bars["close"]
    return pd.concat([atr, implied_bar_sigma], axis=1).max(axis=1)
