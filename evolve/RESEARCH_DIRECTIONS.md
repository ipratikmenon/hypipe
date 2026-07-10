# Research Directions

The human-written steering file for the autoresearch loop (see
AUTORESEARCH_RUNBOOK.md). One direction per section. The agent picks the
smallest testable hypothesis under an ACTIVE direction each iteration.
Status: ACTIVE | EXHAUSTED | HOLD.

---

## D-001 · Feature pipeline performance — ACTIVE (E0, engineering)
Make `features/` computation measurably faster on the starter dataset
WITHOUT changing any output value (no-lookahead + feature tests are the
referee; add a benchmark first, then optimize). Candidates: vectorize
`_rolling_slope`, cache session groupbys, numba where it pays.
Honest eval: wall-clock on frozen data. Greedy selection permitted.

## D-002 · Backtest engine throughput — ACTIVE (E0, engineering)
Engine run on 17k bars should drop well under 1s. Profile first; the
per-bar iloc loop is the suspect. Outputs must be bit-identical on the
existing engine tests plus a golden-run fixture (add it first).

## D-003 · Label geometry on crypto (5m bars) — HOLD until E2
Vary (PT_MULT, SL_MULT, MAX_HOLD) jointly on starter data; the D5 inverted
geometry (0.8/1.6) was chosen for FX-session behaviour — crypto's fat tails
may prefer different asymmetry. Alpha objective: deflated bar + incubation
apply. Pre-register the grid; do NOT cherry-pick cells.

## D-004 · Volatility-conditioned confirmation windows — HOLD until E2
Hypothesis: N_CONFIRM should shrink in high-vol regimes (2 closes at high
ATR wait too long — δ grows) and stretch in chop. Test N_CONFIRM as a
function of HAR vol-surprise. Alpha objective.

## D-005 · pdelta features: earn their seat or lose it — HOLD until E2
The T0 quote-pressure proxies were flagged as "let feature importance
decide" (§7.1). Decide: ablation on real data — if pdelta_* contribute
< 2% total gain across symbols, remove them (fewer features = less DSR tax).
