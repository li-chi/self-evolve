# evolve — experience-conditioned serving

A stateful inference layer for a **frozen** checkpoint and an **unmodified**
harness. Harbor funnels every model call through one coroutine,
`harbor.llms.lite_llm.LiteLLM.call`, so there is no proxy here: `hook.py`
wraps that coroutine in-process and gets the complete provider-visible
request/response stream, plus the ability to change what reaches SGLang and
to spend extra model calls the harness never sees.

```
tools/evolve/
  hook.py     plumbing: patch, ledger, session state, arm dispatch
  policy.py   the algorithm under test — build_note / the completion audit
  replay.py   offline loop: iterate on policy.py with no containers, no GPU
  run.py      harbor's CLI with the hook installed
```

## Running

Identical to `harbor`, with an arm selected by env var:

```bash
EVOLVE_ARM=log .venv/bin/python -m tools.evolve.run run \
  -p datasets/toolathlon/sales-accounting -a terminus-2 \
  -m openai/qwen3.6-35b -k 5 -n 16 --env-file .env
```

| arm | behaviour |
|---|---|
| `log` | pass-through + ledger. Must reproduce plain `harbor` exactly — this is the control. |
| `cards` | `policy.build_note` appends retrieved experience to the prompt |
| `audit` | completion gate: one extra model call before any `task_complete: true` |
| `cards+audit` | both |

`EVOLVE_STORE` (default `jobs/_evolve`) sets the ledger root.

## Ledger

One JSONL file per trial at `$EVOLVE_STORE/<arm>/<job>/<trial>.jsonl`, one
record per model call. The job name is in the path because two runs of the
same arm are different experiments and must not merge. History is stored as a **delta** against the previous call on the
same chain — a normal turn adds exactly two messages — which keeps the ledger
small on prefill-heavy rollouts (up to ~55k prompt tokens/turn) and detects
harness-side compaction for free: when the common prefix is shorter than the
previous history, the harness dropped or rewrote something.

Chains are tracked **per role**. terminus-2's three summarization subagents
call `LiteLLM.call` directly on the same instance with their own histories
(`Terminus2._run_subagent`), so a single chain would read every subagent call
as a compaction and corrupt the next main-loop delta. `role` is `main` when
the call came through `Chat.chat` (the only caller that passes
`previous_response_id`) and `subagent` otherwise.

`replay.reconstruct` rebuilds full message lists from the deltas.

## Two properties worth keeping

- **Injection is invisible to the harness.** `Chat.chat` appends its own local
  `prompt` to the history after the call returns, so anything the hook adds
  never enters the conversation Harbor replays next turn. Every turn must
  re-inject, exactly as with a proxy.
- **Inject as a suffix.** Rollouts are prefill-dominated, so a front-injected
  note that changes each turn would invalidate SGLang's radix cache and force
  a full re-prefill. `build_note`'s return value is appended after the latest
  observation, which is also the strongest position for instruction-following.

## Offline iteration

`replay.py` yields the same `Turn` shape from two sources: the rollouts
already in `jobs/` (Harbor keeps the *parsed* agent message, so good for
mining, not for byte-exact replay) and the hook's ledger (byte-exact). Each
`Turn` carries the request, the response, the commands the harness ran, and
the observation that came back — the transition an experience layer learns
from. Develop `build_note` against these; spend live rollouts only to measure.

```bash
.venv/bin/python -m tools.evolve.replay stats              # rollouts in jobs/
.venv/bin/python -m tools.evolve.replay stats --arm log    # a ledger arm
```

Rewards are joined from `verifier/reward.txt` for **evaluation only**. Keep
them out of anything that updates memory: the claim is that the layer improves
from interaction, not from benchmark labels.
