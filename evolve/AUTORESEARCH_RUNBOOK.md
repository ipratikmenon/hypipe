# AutoResearch on hypipe — Operating Manual

How the automated research loop runs on this repo with Claude as the agent.
The mechanics follow Karpathy's AutoResearch; the selection rules follow
EVOLUTION.md (deflated bar + gratitude/SPRT incubation). Read both before
operating.

---

## 1. The division of labor

| Role | Who | Does |
|------|-----|------|
| Direction | Human | Writes/edits `evolve/RESEARCH_DIRECTIONS.md` — the steering wheel |
| Judgment | Claude (agent) | Picks hypotheses, implements mutations, interprets results, writes findings |
| Determinism | Harness CLI | Runs evals identically every time; computes the deflated bar; keeps the ledger |
| Belief | The math | SPRT incubation decides what gets adopted — neither human hunches nor agent enthusiasm |

Claude never computes its own score and never grades its own homework: every
evaluation goes through the harness, every trial lands in the ledger.

## 2. Repo scaffolding

```
evolve/
├── AUTORESEARCH_RUNBOOK.md    # this file
├── RESEARCH_DIRECTIONS.md     # human-written; the only steering input
├── LOOP_PROMPT.md             # the exact per-session prompt (verbatim)
├── harness.py                 # BUILD (Phase E1): screen / full / ledger-bar / incubate
├── check_protected.py         # BUILD (Phase E1): refuses diffs touching the Constitution
├── ledger/trials.json         # append-only trial ledger (DSR accounting)
└── experiments/EXP-YYYYMMDD-NNN/
    ├── hypothesis.md          # one paragraph: what & why, from which direction
    ├── config.json            # exact mutation parameters
    └── result.json            # harness output, verbatim
```

**Protected paths (the Constitution — read-only to the loop, enforced by
`check_protected.py` run before every experiment commit):**
`execution/`, `common/journal.py`, gate thresholds in `backtest/walkforward.py`,
`tests/test_no_lookahead*`, risk caps in `config.py`, `evolve/harness.py`
itself, and this runbook. A research commit touching these fails the loop.

## 3. One iteration of the loop (what Claude does each cycle)

1. **Read state:** `RESEARCH_DIRECTIONS.md`, ledger tail, last 5 experiment
   results (learn from what already failed — do not re-run dead ideas).
2. **Pick ONE hypothesis** — the smallest testable change under an active
   direction. Write `hypothesis.md` + `config.json` BEFORE implementing
   (pre-registration: no moving the goalposts after seeing results).
3. **Implement** on branch `research/EXP-<id>`, inside the whitelist:
   `features/` params & new compositions, `model/` hyper/label params,
   threshold values that are NOT gates. Run `check_protected.py`.
4. **Referee:** `python -m pytest fx_commodities/tests -q` — any failure
   (especially no-lookahead) = experiment invalid, revert, log, next.
5. **Cheap screen:** `python -m evolve.harness screen --config <cfg>`
   (single purged-CV window, minutes). ~90% die here. Ledger++ either way.
6. **Full eval (survivors):** `python -m evolve.harness full --config <cfg>`
   → complete purged walk-forward + costs. Ledger++.
7. **Compare to the deflated bar:** `python -m evolve.harness ledger-bar`
   returns the current acceptance threshold (rises with ledger count).
   Below bar → write finding, revert, next.
8. **Above bar →** `python -m evolve.harness incubate --config <cfg>`:
   registers a paper challenger (EVOLUTION.md Level 1); the SPRT decides
   adoption over the coming weeks. **The loop's job ends here — no
   experiment ever merges itself to main or touches live config.**
9. Commit the experiment folder + code to the research branch with message
   `EXP-<id>: <hypothesis one-liner> — <screen/full/incubated/dead>`.
10. Append one line to the session digest; go to 1.

End of session: push research branches, write
`evolve/experiments/DIGEST-<date>.md` (5-line summary: tried / died-at-screen
/ died-at-full / incubated / interesting-failures).

## 4. How to actually run it with Claude

**Mode A — supervised session (start here):** open Claude Code on the repo:
> "Run the autoresearch loop per evolve/LOOP_PROMPT.md for up to N
> iterations or M hours, engineering objectives only."

**Mode B — nightly headless (after ≥3 clean supervised sessions):** cron on
the always-on box (same machine as the recorder):
```
15 1 * * 2-6  cd ~/hypipe && claude -p "$(cat evolve/LOOP_PROMPT.md)" \
              --permission-mode acceptEdits --max-turns 400 \
              >> evolve/experiments/nightly.log 2>&1
```
Overnight IST is ideal: FX/MCX closed, crypto recorder unaffected, results
digest ready with morning coffee.

**Mode C — parallel community (Level 3, later):** several headless workers,
each pinned to one direction via an env var the prompt reads, all writing to
the ONE shared ledger — deflation spans the whole community's search.

**Budgets (in LOOP_PROMPT, tune per experience):** ≤ 25 experiments/night,
≤ 10 min wall-clock per full eval, stop early if 8 consecutive screens die
in the same direction (direction exhausted — flag it in the digest instead
of grinding).

## 5. The weekly human ritual (~15 minutes — this is your whole job)

1. Read the digests. 2. Read the incubator dashboard panel (SPRT levels).
3. Edit RESEARCH_DIRECTIONS.md: retire exhausted directions, add new ones
   (loss post-mortems from the journal are the best source). 4. Approve/veto
   any SPRT-accepted candidate's promotion to live-quarter-size (while
   `EVOLVE_AUTO_PROMOTE=false`).

## 6. Staged rollout

| Stage | Objectives | Selection | Prereq |
|-------|-----------|-----------|--------|
| E0 (possible NOW) | Engineering: backtest runtime, feature-compute speed, calibration ECE on frozen folds, coverage — honest evals | Vanilla greedy (safe here) | none — tests are the referee |
| E1 | Build `harness.py`, `check_protected.py`, ledger | — | Phase 3 walk-forward exists |
| E2 | Alpha directions on real data | Deflated bar + SPRT incubator | E1 + recorder data + Phase 4 paper loop |
| E3 | Parallel workers, shared ledger | same as E2 | ≥ 2 clean E2 promotion cycles |

## 7. Failure modes and their tripwires

- **Agent gaming the eval** → it can't compute scores; harness only. Ledger
  is append-only; `ledger-bar` reads count, not content.
- **Prompt drift toward greed** ("this looks great, let's merge it") →
  step 8 is a hard stop; merges to main are human-only by branch protection.
- **Direction fatigue** (300 variations of one dead idea) → the 8-strikes
  rule + weekly human pruning.
- **Compute burn** → per-night caps; screens before fulls; 5-minute-
  experiment economics inherited from AutoResearch.
- **Silent test rot** (agent "fixes" a failing test to pass its experiment)
  → tests are in the protected set; a diff touching them invalidates the run.
