# EVOLUTION.md — The Self-Evolving Layer for hypipe

**Status: DESIGN — build after fx_commodities Phase 4 (a system must trade
before it can evolve).**

## 0. Honest framing — what "self-evolving" can and cannot mean

**Cannot mean:** a bot that freely rewrites its own logic from live results.
Live trading at 1–2 trades/day yields ~500 fat-tailed samples a year — an
unsupervised learner on that feedback learns luck, not skill, and every
famous automation disaster is a system that could act faster than it could be
supervised. No design can honestly promise to "outperform every other bot";
markets are adversarial and non-stationary, and anyone's edge decays.

**Can mean — and what we build:** a **closed research loop** where the system
continuously generates its own improvement hypotheses, tests them against the
statistical immune system we already built (purged walk-forward, PSR, DSR
trial accounting, paper soak), and promotes only survivors — while the live
trader itself stays deterministic, auditable, and instantly revertible. This
is evolution *with natural selection*, where fitness = out-of-sample
robustness, never in-sample performance. The system gets better the way
science does: by killing bad ideas cheaply, not by trusting good streaks.

The four levels below are ordered by autonomy. Each level is only enabled
after the one below has run stably for a month.

---

## Level 0 — Adaptation (already designed; Phase 3/4 deliverables)

Continuous parameter re-estimation inside a *fixed* model structure:
- Weekly refits: HAR-RV, HMM regimes, isotonic calibration, conformal q̂
- Walk-forward retraining of primary + meta models on schedule
- Drift monitor (§8.10) → forced retrain or entry-block
- Confirmation study → per-symbol gate enable/disable
- Universe pruning: symbols with OOS PF < 1.0 drop from live

This is adaptation, not evolution: the system tracks a moving target but
cannot change what it *is*.

## Level 1 — Champion–Challenger (the selection mechanism)

At all times: one **champion** (trades live) and N **challengers** (identical
infrastructure, paper-only, different configs — feature sets, label params,
thresholds, model classes). All see the same data; all journal identically.

**Promotion test (the math).** A challenger replaces the champion only if,
over the same evaluation windows:
1. Walk-forward gates pass (PF ≥ 1.1, PSR ≥ 0.95, DD ≤ 10%) — as always;
2. **Paired superiority:** block-bootstrap the *daily PnL differences*
   (challenger − champion) over ≥ 60 shared trading days; require
   `P(mean difference > 0) ≥ 0.95`. Paired, because both face the same
   market days — this removes the market's own variance from the comparison;
3. ≥ 15 paper sessions of live-condition execution with zero reconciliation
   errors.

**Demotion (edge-decay detection — runs on the live champion daily).**
CUSUM (Page's test) on standardized trade outcomes: with per-trade PnL in R
units `x_i`, expected edge `μ₀` from the walk-forward report,

```
S_0 = 0;   S_i = max(0, S_{i−1} + (μ₀ − x_i − k))     k = 0.25·μ₀
alarm when S_i > h                                     h ≈ 5·σ_x
```

CUSUM detects a *shift* in mean edge far faster than a rolling PF window.
Alarm ⇒ champion demoted to half size; second alarm ⇒ flat, best challenger
(or the DEMA baseline at 0.25 size) takes over pending review. **Falling back
to less automation is always the failure mode — never "try harder".**

## Level 2 — The Scientist Loop (automated hypothesis generation)

A scheduled research agent — an LLM (Claude via the Agent SDK on cron, or a
local model) with **read access to everything and write access to nothing
live**:

Weekly cycle:
1. **Post-mortem:** read the journal — every losing trade's decision digest
   (§9.4 makes each trade auditable). Attribute losses: which gate passed
   that shouldn't have? Which feature was most confidently wrong? Cluster
   recurring failure patterns (e.g. "losses concentrate in the 30 min after
   London open on gap days").
2. **Hypothesize:** propose bounded mutations from a **whitelisted grammar**:
   new features composed from existing primitives (windows, ratios, z-scores,
   interactions), label-parameter variants, threshold variants, challenger
   model classes. The grammar is the safety boundary — the agent composes
   within it; it does not write arbitrary code into the trading path.
3. **Experiment:** run each hypothesis through the identical purged
   walk-forward harness. Every run increments `reports/trials.json` — the
   DSR discount applies to the agent's search exactly as to a human's.
   (This matters more here: an agent can run 500 experiments a week; DSR is
   what keeps 500 experiments from manufacturing a fake genius.)
4. **Report & queue:** survivors become *challenger candidates* with a
   written rationale, expected improvement, and DSR-adjusted confidence.
   They enter Level 1 as paper challengers — nothing the agent produces
   touches live capital directly.

Human approval is required for champion promotion initially
(`EVOLVE_AUTO_PROMOTE=false`); after ≥ 3 consecutive successful hand-approved
promotions, the flag may be flipped to allow auto-promotion **to paper-vetted
live at quarter size**, never to full size.

## Level 3 — Population methods (GPU research track, extends §8.11 R6/R7)

- **Population-based training:** a population of model configs trains in
  parallel (one GPU suffices); underperformers copy hyperparameters from
  survivors + perturb (exploit/explore). Natural fit with walk-forward:
  each generation = one walk-forward step.
- **Regime-conditional model zoo:** separate challengers per HMM regime;
  the ensemble routes by filtered regime probability. Evolution then acts
  per-regime — an idea can win in trend and die in chop on its own merits.
- **Genetic alpha mining (R7)** feeds Level 2's hypothesis queue rather than
  trading directly.

## The Constitution (invariants no level may ever mutate)

Enforced structurally — these live outside anything the loop can modify:
1. Deployment gates (PF/PSR/DD) and their thresholds.
2. Risk caps: per-trade risk %, daily loss halts, position/correlation caps,
   the D5 profile's trade-count backstop.
3. The executor and journal code paths (order placement, reconciliation).
4. The no-lookahead test suite and purged-CV protocol.
5. Trial accounting: every experiment, human or agent, increments the DSR
   counter. No experiment is free.
6. The kill switch and the fallback ladder (champion → half size → baseline
   → flat) — the loop can move DOWN the ladder, only humans move it up... 
   and every mutation, promotion, demotion, and alarm is journaled with its
   full evidence, so the system's evolution is as auditable as its trades.

## Implementation hooks (when the time comes)

- `evolve/challenger.py` — config-defined challenger fleet on the existing
  paper harness; shared data feed, separate journals.
- `evolve/promotion.py` — paired block-bootstrap test + gate checks.
- `evolve/cusum.py` — decay detector on the live journal (a ~40-line module;
  test fixtures: simulated edge-loss detected within ~20 trades).
- `evolve/scientist/` — agent runbook + whitelisted feature grammar +
  experiment harness CLI the agent invokes; Claude Agent SDK on a weekly
  cron, or invoked manually as a Claude Code session with the prompt in
  `evolve/scientist/PROMPT.md`.
- Dashboard: an "Evolution" panel — champion vs challenger equity, CUSUM
  level vs alarm threshold, pending proposals with DSR-adjusted confidence.

## Sequencing

Level 0 ships with Phase 3/4 (already specced). Level 1 after one month of
stable live/paper champion. Level 2 after Level 1 has executed ≥ 2 clean
promotion cycles. Level 3 alongside the R6 GPU track. Patience here is not
caution theater — each level's data (journals, trials, decay history) is the
training substrate for the level above it.
