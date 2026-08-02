"""The algorithm under test. `hook.py` is plumbing; this file is the experiment.

Everything here is a no-op in the `log` arm. Arms are selected with
`EVOLVE_ARM`:

  log           pass-through + ledger (baseline; must reproduce harbor exactly)
  cards         `build_note` injects retrieved experience as a prompt suffix
  audit         completion gate: one extra model call before a `task_complete`

Arms compose by substring, e.g. `cards+audit`.

`parse` and `strip` arms existed to undo, client-side, a server missing
`--reasoning-parser qwen3`. Fixed on the server 2026-08-02 and removed; the
hook now only flags the condition (`_reasoning_leaked`) rather than papering
over it.

Keep `build_note` a pure function of (session, messages) plus whatever store
you read — that is what makes `replay.py` able to score it offline against the
logged next-turn observation, with no containers and no GPU.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------
# Arm: cards
# --------------------------------------------------------------------------


def build_note(session: dict[str, Any], messages: list[dict[str, Any]]) -> str | None:
    """Return text to append after the latest observation, or None.

    Appended as a suffix so the prefix stays in SGLang's radix cache; the
    rollouts are prefill-dominated (up to ~55k prompt tokens/turn), so a
    front-injected note that changes each turn would force a full re-prefill.
    """
    return None


# --------------------------------------------------------------------------
# Arm: audit
# --------------------------------------------------------------------------

_TASK_COMPLETE = re.compile(r'"task_complete"\s*:\s*true', re.I)


def parse_terminus(content: str) -> dict[str, Any] | None:
    """Extract the terminus-2 JSON object from a raw completion, or None.

    Mirrors TerminusJSONPlainParser's brace scan: the model very often emits
    prose before the object (2,383 "Extra text detected before JSON object"
    warnings across the current 139-rollout baseline).
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(content):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    obj = json.loads(content[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def claims_done(content: str) -> bool:
    obj = parse_terminus(content)
    if obj is not None:
        return bool(obj.get("task_complete"))
    return bool(_TASK_COMPLETE.search(content))


AUDIT_TEMPLATE = """\
STOP. You just declared this task complete. Before that is accepted, audit it.

Work only from the terminal output actually present in this conversation. Do \
not assume a command succeeded because you issued it, and do not treat \
truncated output as evidence.

1. Restate every distinct outcome the original instruction requires, as a list.
2. For each one, quote the specific terminal output that proves it is done.
3. Mark any requirement with no such evidence as UNMET — including ones where \
the output scrolled off, was cut mid-line, or was never re-read after a write.

Then reply in the normal response format:

- If every requirement has evidence, repeat your completion response \
unchanged, with "task_complete": true.
- Otherwise set "task_complete": false and give the command batch that \
gathers the missing evidence or does the missing work. Put the audit itself \
in "analysis".
"""


def audit_prompt(messages: list[dict[str, Any]], response: str) -> str:
    return AUDIT_TEMPLATE


def accept_audit(verdict: str) -> bool:
    """True iff the audit found work remaining and returned a usable batch.

    A verdict that confirms completion is discarded and the agent's original
    response is returned untouched, so the audit can only ever add work.
    """
    obj = parse_terminus(verdict)
    if obj is None:
        return False
    if obj.get("task_complete"):
        return False
    return bool(obj.get("commands"))
