"""Offline loop: iterate on the algorithm without running a single container.

Two sources, one shape:

- `iter_trajectory_turns()` reads the rollouts already in `jobs/`. Harbor's
  `trajectory.json` keeps the *parsed* agent
  message, not the raw completion, so this is enough to mine cards and to
  measure the harness-visible effect of a turn, but not to replay a prompt
  byte-for-byte.
- `iter_ledger_turns()` reads what `hook.py` wrote, which *is* byte-exact, and
  reconstructs the full message list from the per-turn deltas.

Both yield a `Turn`: the request as the model saw it, the response, and the
observation the harness produced next — the transition an experience layer
learns from. Rewards are joined from `verifier/reward.txt` and are for
evaluation only; keep them out of anything that updates memory.

    .venv/bin/python -m tools.evolve.replay stats
    .venv/bin/python -m tools.evolve.replay stats --arm log
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

JOBS = Path("jobs")
STORE = Path("jobs/_evolve")


@dataclass
class Turn:
    task: str
    run: str
    turn: int
    role: str
    messages: list[dict[str, Any]]   # the request, oldest first
    response: str                    # raw completion (ledger) or parsed message
    commands: list[str]              # shell keystrokes the harness executed
    observation: str                 # what came back before the next call
    reward: float | None
    source: str
    agent: str = "?"
    model: str = "?"


@dataclass
class Rollout:
    task: str
    run: str
    reward: float | None
    agent: str = "?"
    model: str = "?"
    turns: list[Turn] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return self.reward == 1.0


def reward_for(agent_dir: Path) -> float | None:
    p = agent_dir.parent / "verifier" / "reward.txt"
    try:
        return float(p.read_text().strip())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# Source 1: Harbor trajectories already on disk
# --------------------------------------------------------------------------


def iter_trajectory_turns(jobs: Path = JOBS,
                          exclude_jobs: set[str] | None = None) -> Iterator[Turn]:
    from tools.evolve.hook import _identity

    exclude_jobs = exclude_jobs or set()
    for tj in sorted(jobs.rglob("agent/trajectory.json")):
        job, task, run = _identity(tj.parent)
        if job in exclude_jobs:
            continue
        reward = reward_for(tj.parent)
        try:
            traj = json.loads(tj.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        meta = traj.get("agent") or {}
        agent, model = meta.get("name", "?"), meta.get("model_name", "?")
        history: list[dict[str, Any]] = []
        n = 0
        for step in traj.get("steps", []):
            if step.get("source") == "user":
                history.append({"role": "user", "content": step.get("message", "")})
                continue
            n += 1
            msg = step.get("message", "")
            calls = step.get("tool_calls") or []
            cmds = [c.get("arguments", {}).get("keystrokes", "") for c in calls]
            obs = "\n".join(
                (r.get("content") or "")
                for r in (step.get("observation") or {}).get("results", [])
            )
            yield Turn(task, run, n, "main", list(history), msg, cmds, obs,
                       reward, "trajectory", agent, model)
            history.append({"role": "assistant", "content": msg})
            if obs:
                history.append({"role": "user", "content": obs})


# --------------------------------------------------------------------------
# Source 2: the hook's ledger
# --------------------------------------------------------------------------


def reconstruct(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each ledger record with `messages` filled back in from the deltas.

    One chain per role, matching how `hook.py` diffs them: the summarization
    subagents share the LiteLLM instance but not the main loop's history.
    """
    chains: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        role = rec.get("role", "main")
        prev = chains.get(role, [])
        messages = prev[: rec["prefix_reused"]] + rec["new_messages"]
        chains[role] = messages
        rec["messages"] = list(messages)
        yield rec


def iter_ledger_turns(store: Path = STORE, arm: str = "log") -> Iterator[Turn]:
    # `<arm>/<job>/<trial>.jsonl`; older ledgers wrote `<arm>/<trial>.jsonl`.
    for path in sorted((store / arm).rglob("*.jsonl")):
        records = list(reconstruct(path))
        if not records:
            continue
        task, run = records[0]["task"], records[0]["run"]
        logs_dir = records[0].get("logs_dir")
        reward = reward_for(Path(logs_dir)) if logs_dir else None
        for i, rec in enumerate(records):
            nxt = records[i + 1] if i + 1 < len(records) else None
            obs = ""
            if nxt:
                # the observation is the trailing user message of the next request
                for m in reversed(nxt["new_messages"]):
                    if m.get("role") == "user":
                        obs = m.get("content", "")
                        break
            yield Turn(task, run, rec["turn"], rec["role"], rec["messages"],
                       rec["response"], _commands(rec["response"]), obs,
                       reward, f"ledger:{arm}", "terminus-2", rec.get("model", "?"))


def _commands(response: str) -> list[str]:
    from tools.evolve.policy import parse_terminus

    obj = parse_terminus(response) or {}
    return [c.get("keystrokes", "") for c in obj.get("commands", []) or []]


def rollouts(turns: Iterator[Turn]) -> list[Rollout]:
    out: dict[tuple[str, str], Rollout] = {}
    for t in turns:
        r = out.setdefault(
            (t.task, t.run), Rollout(t.task, t.run, t.reward, t.agent, t.model)
        )
        r.turns.append(t)
    return list(out.values())


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

WARN = re.compile(r"Previous response had warnings:\n(.*?)\n\nNew Terminal Output", re.S)


def stats(rs: list[Rollout]) -> None:
    by: dict[str, list[Rollout]] = {}
    for r in rs:
        by.setdefault(r.task, []).append(r)

    # Mixing agents or models here would quietly average two systems together.
    mix: dict[tuple[str, str], int] = {}
    for r in rs:
        mix[(r.agent, r.model)] = mix.get((r.agent, r.model), 0) + 1
    for (agent, model), n in sorted(mix.items()):
        print(f"# {n:4d} rollouts  {agent} / {model}")
    if len(mix) > 1:
        print("# WARNING: more than one agent/model in this set; filter with --model")
    print()

    print(f"{'task':32} {'k':>2} {'pass@1':>7} {'p@k':>4} {'p^k':>4} "
          f"{'turns':>6} {'warn':>6}")
    n = solved = attempts = any_ = all_ = 0
    for task, group in sorted(by.items()):
        k = len(group)
        s = sum(1 for r in group if r.solved)
        w = sum(len(WARN.findall(t.observation)) for r in group for t in r.turns) / k
        turns = sum(len(r.turns) for r in group) / k
        print(f"{task:32} {k:2d} {s / k:7.2f} {int(s > 0):4d} {int(s == k):4d} "
              f"{turns:6.1f} {w:6.1f}")
        n += 1
        solved += s
        attempts += k
        any_ += s > 0
        all_ += s == k
    if not n:
        print("no rollouts found")
        return
    print(f"\nTASKS={n}  pass@1={solved / attempts:.3f} ({solved}/{attempts})  "
          f"pass@k={any_ / n:.3f}  pass^k={all_ / n:.3f}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "stats"
    arm = argv[argv.index("--arm") + 1] if "--arm" in argv else None
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    if cmd != "stats":
        print(__doc__)
        return 1
    turns = iter_ledger_turns(arm=arm) if arm else iter_trajectory_turns()
    rs = rollouts(turns)
    if model:
        rs = [r for r in rs if model in r.model]
    stats(rs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
