# Findings — provider-side experience, qwen3.6-35b on Toolathlon

Everything below was measured on 10 mock-track tasks at k=10 (100 rollouts per
arm) with `terminus-2` driving `openai/qwen3.6-35b`, unless stated otherwise.
Code is in git; the rollouts, ledgers and distillations are **gitignored** —
see *What lives where* at the bottom.

## ⚠ Numbers taken before `b557373` are contaminated

Every mock registered its fixture helpers with `@mcp.tool`, so `mock_debug_*`
appeared in the agent's own tool listing. `mock_debug_seed_table` writes rows
"bypassing the allowlist" — including into a table a grader reads.

Across the archived corpus **347 rollouts called a `mock_debug_*` tool and 138
of those were graded pass**, among them a `price-comparison` run that inserted
the finished analysis straight into the graded table. Hidden in `b557373`
(153 registrations across 56 servers, gated behind `MOCK_DEBUG_TOOLS`).

**`COVERAGE.md`'s qwen baseline table predates this and is stale**, as does
every arm comparison below marked *pre-fix*. Re-baselining cost only ~2 points
at the aggregate (44/100 → 42/100), so the contamination was not load-bearing
overall, but per-task shifts are larger.

## Results

| arm | what it does | reward | vs control |
|---|---|---|---|
| `big-log` | control, **pre-fix** | 44/100 | — |
| `v2-cards` | mined cards injected every turn | 41/100 | −0.03, CI [−0.14, +0.06] |
| `big-guard` | intercept on **tool match**, dirty store | 35/100 | −0.09 |
| `v2-guard` | intercept on tool match, clean store | 33/99 | −0.107 |
| `v3-guard` | intercept on **violation** | 51/100 | **+0.07**, CI [−0.07, +0.20], P=0.83 |
| `v3-audit` | completion gate (headroom probe, out of scope) | 49/100 | +0.05, cost 2h / 237M prefill |
| `postmock-log` | control, **post-fix** | 42/100 | — |
| `postmock-teach` | Opus-distilled rules, 1 prior at turn 1 | 46/100 | +0.04, CI [−0.05, +0.14], P=0.75 |

Three independent ways of acquiring tool knowledge, three effects in the same
small positive band, all inside the noise floor at k=10.

## The load-bearing finding

**Tool-contract knowledge is not the binding constraint.** Mined cards cut the
tool-call error rate 37.7% → 28.4% (holding inside both outcome strata) and
moved task success by zero. Of 56 failures in the control, **56 declared the
task complete** — not one ran out of turns. The three tasks stuck at 0/10
(`ab-testing`, `filter-low-selling-products`, `woocommerce-stock-alert`) all
fail on a missing deliverable, not on a malformed call.

The environment supplies a **validity** signal for free (an action errored or
it didn't) and never a **utility** signal (was the action useful). Toolathlon
supplies no utility channel at all — single-turn, no simulated user, no
follow-up session — so the only one available is the grader we hold out. That
is a property of the evaluation, not of the method.

## The teacher / judge split

Both roles are `claude-opus-5`, offline, over traces already collected. Neither
is in the serving path; at inference only retrieval runs.

- **Judge** — one trace in, pass/fail out. Manufactures the missing utility
  signal. **83/100 agreement with the grader having never seen it** ($11.60 for
  10 tasks).
- **Teacher** — N traces in, injectable rules out. Needs the judge's verdicts,
  because distillation is a contrast operation.

**The judge's blind spot is the failure it most needs to detect.** It
over-declares passes, and does so worst where the true pass rate is lowest:

```
task                        judge   grader pass   teacher says pass
woocommerce-update-cover    10/10        3               3
filter-low-selling-products 10/10        5               5
...
woocommerce-stock-alert      6/10        3               7
ab-testing                   4/10        0               6
```

An outcome that never reaches the terminal is invisible to it, so absence of
evidence reads as completion — the agent's own premature-completion failure,
one level up. Likely fix, untested: make the judge enumerate required
deliverables from the instruction *before* reading the transcript.

## Method notes worth not relearning

- **The teacher never sees a reward.** `teacher.build()` strips labels; the LOTO
  merge filters on the teacher's *own* verdicts. `reward.txt` is read only by
  the scoring code, to grade the judge.
- **Audit rules by polarity, not presence.** A rule cited by both passes and
  failures is the *strongest* evidence — passes show it followed, failures show
  the cost of breaking it. The test is asymmetric: a violator that passed anyway
  makes a rule unsound; a complier that failed is fine, since runs fail for many
  reasons.
- **`guard` must fire on the fault, not the tool.** Matching on the tool name
  fired on 42–55% of turns with the violation present in **zero** of them, and
  the model rewrote its action 93% of the time — worth −0.107. Violation-gating
  took the fire rate to 5.3% and the arm to +0.07, a 17-point swing.
- **Cards must state rules, not show examples.** 15% of guard rewrites drifted
  toward copying the example printed in the card.
- **Serving config was wrong for the first half of the day.** SGLang ran without
  `--reasoning-parser qwen3`, so chain-of-thought landed in `content`, was stored
  in history, and was re-sent every turn (~10% of prefill; 4,710 "Extra text
  detected before JSON object" warnings). Fixed on the server. It cost **no**
  correctness — 0 hard parse failures in 7,158 turns.
- LiteLLM has no entry for `qwen3.6-35b`, so harbor falls back to a **1M**
  context limit and proactive summarization can never fire. Not yet firing
  (peak 182k of a 262k window) but a longer task will error rather than compact.

## What lives where

| | |
|---|---|
| in git | `tools/evolve/*` (hook, canon, contract, mine, policy, teacher, replay, selftest), the mock fix, `datasets/`, `mocks/` |
| **gitignored, local disk only** | `jobs/_evolve` (166 MB, 916 trial ledgers), `jobs/_teacher` (10 raw distillations + 10 LOTO rule sets), `jobs/_cards` (34 stores), and every `jobs/<job>/` rollout tree with its `reward.txt` |

`jobs/toolathlon/` is the one tracked rollout archive. Losing `jobs/_evolve`
means re-running rollouts to rebuild the byte-exact corpus; the archived
trajectories in `jobs/toolathlon/` survive but carry harbor's *parsed* commands
rather than raw completions.

Run `.venv/bin/python -m tools.evolve.selftest` after touching `hook.py` — the
`log` arm is the control every number rests on, and two of its failure modes
(a dead grammar, a corrupted delta chain) are silent.

## Open

1. Judge deliverable-enumeration fix — cheapest path at the failure class that
   actually dominates.
2. `v3-guard` at +0.07 / P=0.83 is the most promising arm; ~10 tasks × k=20
   would settle whether it is real.
3. Task families via seed perturbation — still the binding constraint on
   resolving any effect smaller than a landslide at k=10.
4. Re-run the distillation on a post-fix corpus: 9 merged rules recommended
   `mock_debug_state`, which no longer exists.
