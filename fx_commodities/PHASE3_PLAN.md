# Phase 3 Build Plan — Model Layer (executable by any AI model)

**Read first:** REQUIREMENTS.md §8 (all math), §9 (decision logic), §10.3
(gates). This document is the file-by-file build order with signatures, test
fixtures, and acceptance criteria. House rules from TRAINING_GUIDE.md Part 7
apply throughout (closed-bar rule, no imputed features, no-lookahead test per
feature, gates never loosened, `indices/` frozen).

**Precondition note:** ALL Phase 3 code is buildable and testable on synthetic
data on any machine — recorded market data is only needed for *real* training.
Do not block the build on the recorder.

**New dependencies** (append to requirements.txt):
`hmmlearn>=0.3`, `statsmodels>=0.14`, `joblib>=1.3`, `matplotlib>=3.8` (equity
PNG only).

---

## Step 1 — `model/labeling.py` (§8.1)

```python
def triple_barrier_labels(bars: pd.DataFrame, atr: pd.Series,
                          pt_mult: float, sl_mult: float,
                          max_hold: int) -> pd.DataFrame:
    """Returns index-aligned frame: y (int: +1/-1/0), t_touch (int bar index),
    cost_atr (float: spread_mean_points*point/ATR at t, filled by caller)."""
```
- Long-side convention: upper = close + pt·ATR, lower = close − sl·ATR;
  y=+1 upper first, −1 lower first, 0 vertical.
- Vectorize with a forward scan per bar (numpy loop acceptable; O(n·max_hold)).
- Tests: hand-built 10-bar frames where each outcome is forced; ATR=0 rows
  yield y=NaN and are dropped; t_touch correctness.

## Step 2 — `model/har.py` (§8.2)

```python
class HarRV:
    def fit(self, rv_daily: pd.Series) -> "HarRV":      # OLS on log RV
    def forecast(self, rv_daily: pd.Series) -> float     # next-period log-space
def realized_variance(bars: pd.DataFrame, freq: str = "1D") -> pd.Series
def har_atr(bars, atr, har_forecast) -> pd.Series        # max(ATR, k·√RV̂)
```
- Regressors: RV_t, mean(RV_{t-4..t}), mean(RV_{t-21..t}) on log RV.
- Acceptance: on synthetic GARCH-like data, OOS R² of log-RV forecast beats
  the random-walk forecast (assert in test).
- Refit cadence: weekly (caller's job, `HAR_REFIT_DAYS`).

## Step 3 — `model/regime.py` (§8.3)

```python
class RegimeHMM:
    def fit(self, returns: pd.Series, log_rv: pd.Series) -> "RegimeHMM"
    def filtered_probs(self, returns, log_rv) -> pd.DataFrame
        # columns p_trend, p_chop, p_crisis + state_persistence
def variance_ratio_feature(close: pd.Series, q=10, window=200) -> pd.DataFrame
        # vr_10, vr_z (heteroskedasticity-robust z per Lo-MacKinlay)
```
- Use `hmmlearn.hmm.GaussianHMM(n_components=3, covariance_type="full")` for
  FITTING only. **CRITICAL:** hmmlearn's `predict_proba` returns SMOOTHED
  posteriors (forward-backward) — that is lookahead. Implement the FORWARD
  pass manually from the fitted params (startprob_, transmat_, means_,
  covars_): α_t ∝ N(x_t|μ_k,Σ_k)·Σ_j α_{t-1,j}A_jk, normalized per t.
- State naming post-hoc by moments (§8.3). Test: simulate a 3-regime series;
  filtered probs identify regimes with >70% accuracy AND a truncation test
  proves filtered probs at t don't change when future data is appended.

## Step 4 — `model/frac_diff.py` (§8.4)

```python
def ffd_weights(d: float, tol: float = 1e-4) -> np.ndarray
def frac_diff(series: pd.Series, d: float, tol=1e-4) -> pd.Series
def select_d(series: pd.Series, alpha=0.05,
             grid=(0.1,...,1.0)) -> float   # smallest d passing ADF
```
- ADF via `statsmodels.tsa.stattools.adfuller`.
- Test: weights fixture w = [1, −d, d(d−1)/2, …] hand-computed for d=0.5;
  select_d on a random walk returns d<1; on white noise returns 0.1.

## Step 5 — `model/dataset.py` (§8.5) — the anti-overfitting core

```python
def average_uniqueness(t_start: np.ndarray, t_touch: np.ndarray) -> np.ndarray
def time_decay_weights(n: int, floor=0.5) -> np.ndarray
def purged_kfold_indices(n, t_touch, n_splits=5,
                         embargo_pct=0.01) -> list[tuple[train_idx, test_idx]]
def build_dataset(bars_with_features, labels, tier_columns) -> (X, y, w, meta)
```
- Purge rule: drop train i if [i, t_touch[i]] overlaps [min(test), max(t_touch[test])];
  then embargo `int(n*embargo_pct)` samples after the test block.
- Tests: overlapping-label toy case where naive KFold leaks and purged does
  not (assert no train label window intersects test); uniqueness of
  non-overlapping labels = 1.0, of two fully-overlapping = 0.5.

## Step 6 — `model/train.py` (§8.6)

```python
def train_primary(X, y, w, params=DEFAULT_LGBM) -> lgb.Booster
def save_model(model, calibrator, meta: dict, path: Path)  # + metrics.json
def feature_gain_report(model) -> pd.DataFrame              # alert >40% single
```
- Binary target: y==+1 vs rest. `scale_pos_weight` from class balance.
- Persist under `models/{symbol|pooled}/{dataset_version}/`.

## Step 7 — `model/meta.py` (§8.7)

```python
def primary_events(bars, p_up: pd.Series, delta: float,
                   l0_cross: pd.Series) -> pd.DataFrame   # index, direction, source
def meta_labels(bars, atr, events, pt, sl, max_hold) -> pd.Series  # 1/0 per event
def train_meta(X_events, m, w) -> lgb.Booster
```
- Meta features = full row features + p_up, |p_up−0.5|, direction, source.
- Test: with a perfect primary on synthetic trending data, meta precision ≥
  primary precision on the event subset (the whole point — assert it).

## Step 8 — `model/conformal.py` (§8.8)

```python
def fit_isotonic(p_raw, y_cal) -> IsotonicRegression
def ece(p, y, bins=10) -> float                      # gate: < 0.05
def conformal_quantile(p_cal, y_cal, eps) -> float   # q̂ = ⌈(n+1)(1−ε)⌉/n quantile of 1−p(true)
def allow_bet(p_hat, q_hat) -> bool                  # 1 − p̂ ≤ q̂
```
- Test on synthetic exchangeable data: realized error of allowed bets ≤ ε
  + 3% tolerance over 10k samples.

## Step 9 — `model/kalman.py` (§8.9)
Local-linear-trend filter exactly as spec'd; expose `kf_level, kf_velocity,
kf_velocity_z`. Test: on y = t + noise, velocity converges to ~1.

## Step 10 — `model/drift.py` (§8.10)
`psi(expected, actual, bins=10)`, `ks_pvalue(a, b)`, `DriftMonitor` with WARN
(≥3 features PSI>0.25) / BLOCK (≥6) levels. Test PSI≈0 on identical dists,
large on shifted.

## Step 11 — `model/ensemble.py` (§9.1 + §9.1b + §0.5)

```python
@dataclass
class Decision:  # action, direction, p_hat, p_star, edge, kelly_f, lots, sl, tp, digest
def decide(row_features, p_up, p_hat_meta, q_hat, spec, cfg,
           zone_signal: EntrySignal | None) -> Decision
```
- p* = (sl_mult + c_live)/(pt_mult + sl_mult); c_live from live spread/ATR.
- All gates in spec order: crisis, regime guard, orderflow agreement, edge
  margin, conformal, zone confirmation (when zone path), calendar blackout
  (R1 — implement `features/calendar.py` with a static high-impact list file
  the operator maintains; NEWS_BLACKOUT_MIN config).
- Kelly: f* = (p̂(b+1)−1)/b, b=pt/sl; f = 0.5·f*; cap RISK_PER_TRADE_PCT;
  MAX_TRADES_PER_DAY enforced via journal count.
- Tests: each gate independently blocks; digest contains all §9.4 fields.

## Step 12 — `backtest/walkforward.py` + `backtest/stats.py` (§10.3)

```python
def psr(returns, sr_star=0.0) -> float      # exact formula in §10.3
def dsr(returns, n_trials, var_sr) -> float
def walkforward(bars, cfg, train_days=60, test_days=10) -> dict  # report
```
- Retrain + recalibrate each step; hyperparams frozen after window 1.
- Report JSON: aggregate + per-window + per-symbol; equity PNG; the three
  gates evaluated with pass/fail booleans; trials.json counter read/incremented.
- Tests: PSR on N(0.1, 1) sample matches closed form within tolerance; a
  known-profitable synthetic passes gates, a random one fails PSR.

## Step 13 — `backtest/run_baseline.py` (Phase 2 remainder)
CLI: `--symbol BTCUSD [--confirmed]`. Loads Store→bars_from_ohlcv→add_classical,
runs DemaBaseline / ZoneConfirmedBaseline, writes reports/baseline_<sym>.json
including metrics + confirmation_study. Smoke test with synthetic bars.

## Step 14 — `dashboard.py` (serves the UI in `dashboard/index.html`)
FastAPI: GET / serves the static dashboard; GET /api/state returns JSON
assembled from journal.db (open positions, today's signals+digests, risk
state) and reports/ (gates, walk-forward, confirmation study, drift). The
dashboard HTML renders demo payloads inline — replace them with /api/state.

**Price chart:** the committed page ships a hand-built SVG demo chart. In the
real locally-served dashboard, replace it with **TradingView
lightweight-charts** (open-source, Apache-2.0, `npm i lightweight-charts` or
vendored single JS file — no TradingView account needed): candlestick series
from `store.read_ohlcv`/live bars; `series.setMarkers` for entries/exits;
`series.createPriceLine` for SL and TP (update the SL line as the trailing
stop moves); the active zone as a translucent area series between z_lo/z_hi.
Add GET /api/ohlcv?symbol=&from=&to= to feed it. (The full tradingview.com
widget cannot overlay our trades — lightweight-charts can, which is why it
is the chosen library.)

## Step 15 — Integration gate (Definition of Done for Phase 3)

```bash
python -m pytest fx_commodities/tests -q          # 100% pass, new tests included
python -m fx_commodities.backtest.walkforward --synthetic  # runs end-to-end
```
- Synthetic end-to-end: generate a trending+noise series where the pipeline
  MUST produce: labels balanced sanely, HAR beating RW, calibration ECE<0.05
  on the calibration fold, a walk-forward report with all fields populated.
- Coverage ≥ 90% on model/labeling, model/dataset, model/conformal,
  execution/risk (once Step 11 lands in risk.py).
- Real-data training happens only after the recorder has ≥ 2 weeks of ticks;
  gates (§10.3) then decide paper trading.
```
