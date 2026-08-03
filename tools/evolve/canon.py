"""Reduce a rollout to the evidence a reader actually needs.

A reconstructed trace is mostly redundancy. Measured over 171 rollouts:
observations are 59% of the bytes — tmux re-renders overlapping scrollback on
every capture, and each command is echoed back after already appearing in the
assistant turn. The canonical form below is 22% of the original (median 3,969
tokens vs 17,915) and is easier to reason over, not just cheaper.

Nothing here knows what MCP or a shell prompt is: actions come from the grammar
`contract.py` induced, results from the delimiter it induced, and the task
instruction is separated from the harness boilerplate by diffing first messages
across tasks — the boilerplate is whatever they all share.

Two levels:
    L1  instruction + action/result log            (judging: did it finish?)
    L2  L1 plus the model's stated plan per turn   (distilling: why did it go wrong?)

Truncation is the dangerous part. Evidence that a task *was* completed usually
sits in a long listing or a final verification read, so long results are cut in
the middle (head and tail kept) and the last turns are never cut at all — a
judge that can't see the evidence reports its absence, which looks identical to
the work not having been done.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from tools.evolve import contract, policy

HEAD_LINES = int(os.environ.get("EVOLVE_CANON_HEAD", "14"))
TAIL_LINES = int(os.environ.get("EVOLVE_CANON_TAIL", "6"))
PROTECTED_TURNS = int(os.environ.get("EVOLVE_CANON_PROTECT", "4"))


def _common_affixes(texts: list[str]) -> tuple[str, str]:
    """(shared prefix, shared suffix) across first messages of different tasks."""
    if len(texts) < 2:
        return ("", "")
    pre = texts[0]
    for t in texts[1:]:
        i = 0
        while i < len(pre) and i < len(t) and pre[i] == t[i]:
            i += 1
        pre = pre[:i]
        if not pre:
            break
    suf = texts[0]
    for t in texts[1:]:
        i = 0
        while i < len(suf) and i < len(t) and suf[-1 - i] == t[-1 - i]:
            i += 1
        suf = suf[len(suf) - i:] if i else ""
        if not suf:
            break
    return (pre, suf)


@dataclass
class Boilerplate:
    """The harness's own preamble, learned rather than hardcoded."""

    prefix: str = ""
    suffix: str = ""

    def instruction(self, first_message: str) -> str:
        body = first_message
        if self.prefix and body.startswith(self.prefix):
            body = body[len(self.prefix):]
        if self.suffix and body.endswith(self.suffix):
            body = body[: len(body) - len(self.suffix)]
        return body.strip()


def learn_boilerplate(rollouts: dict[tuple[str, str], list]) -> Boilerplate:
    """Diff first messages across *different tasks* — what they share is harness."""
    by_task: dict[str, str] = {}
    for (task, _), turns in rollouts.items():
        if task in by_task or not turns:
            continue
        msgs = min(turns, key=lambda t: t.turn).messages
        if msgs:
            by_task[task] = msgs[0].get("content") or ""
    return Boilerplate(*_common_affixes(list(by_task.values())))


def _elide(text: str, protected: bool) -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    if protected or len(lines) <= HEAD_LINES + TAIL_LINES:
        return "\n".join(lines)
    cut = len(lines) - HEAD_LINES - TAIL_LINES
    return "\n".join(
        lines[:HEAD_LINES] + [f"... [{cut} lines elided] ..."] + lines[-TAIL_LINES:]
    )


def canonical(turns: Iterable[Any], ct: contract.Contract,
              boiler: Boilerplate | None = None, level: str = "L1") -> str:
    """Render one rollout as an action/result log."""
    turns = sorted(turns, key=lambda t: t.turn)
    if not turns:
        return ""
    out: list[str] = []

    first = turns[0].messages[0].get("content", "") if turns[0].messages else ""
    instruction = boiler.instruction(first) if boiler else first
    out.append(f"# TASK\n{instruction}\n\n# TRANSCRIPT")

    last_protected = turns[-1].turn - PROTECTED_TURNS
    for t in turns:
        protected = t.turn > last_protected
        obj = policy.parse_terminus(t.response) or {}
        if level == "L2" and obj.get("plan"):
            out.append(f"\n[turn {t.turn}] plan: {str(obj['plan']).strip()}")
        for cmd in t.commands:
            if cmd.strip():
                out.append(f"$ {cmd.strip()}")
        segments = ct.segment(t.observation)
        for seg in segments:
            body = _elide(seg, protected)
            if body:
                out.append(body)
        if obj.get("task_complete"):
            out.append("[agent declared the task complete]")
    return "\n".join(out)


def rollouts_of(turns: Iterable[Any]) -> dict[tuple[str, str], list]:
    out: dict[tuple[str, str], list] = {}
    for t in turns:
        if getattr(t, "role", "main") == "main":
            out.setdefault((t.task, t.run), []).append(t)
    return out
