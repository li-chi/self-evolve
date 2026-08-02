"""The algorithm under test. `hook.py` is plumbing; this file is the experiment.

Everything here is a no-op in the `log` arm. Arms are selected with
`EVOLVE_ARM`:

  log           pass-through + ledger (baseline; must reproduce harbor exactly)
  parse         split `</think>` out of the response, as SGLang's reasoning
                parser would; Harbor then stores clean content in history and
                stops emitting format warnings
  strip         history-only ablation of `parse`: leave the response alone but
                drop reasoning from assistant turns we re-send
  cards         `build_note` injects retrieved experience as a prompt suffix
  audit         completion gate: one extra model call before a `task_complete`

Arms compose by substring, e.g. `strip+cards+audit`.

Keep `build_note` a pure function of (session, messages) plus whatever store
you read — that is what makes `replay.py` able to score it offline against the
logged next-turn observation, with no containers and no GPU.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------
# Arms: parse / strip
# --------------------------------------------------------------------------

# Fixed on the server 2026-08-02 by launching SGLang with
# `--reasoning-parser qwen3`; `reasoning_content` now arrives separated and
# these two arms are no-ops. They stay as a safety net for the case where the
# parser is off again, and to reproduce the pre-fix baseline.
#
# Before the fix: the chat template prefills the
# opening `<think>`, so completions arrive as
#     <reasoning prose></think>\n\n```json\n{...}\n```
# with `reasoning_content` unset. Harbor's parser recovers the JSON (it scans
# for the first balanced brace) but `Chat.chat` appends the *whole* content to
# the history, so every turn's reasoning is re-sent on every later turn.
# Measured on a 7-turn rollout: 45% of assistant content, 14.5% of the final
# prompt, ~8.5% of total billed prefill, growing with turn count.
#
# Stripping here is what `--reasoning-parser qwen3` would do server-side. It is
# applied to the history we send, not to what Harbor stores, and it is
# deterministic, so the prefix stays stable across turns and the radix cache
# still hits.


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.S)


def split_reasoning(content: str) -> tuple[str, str | None]:
    """Split a completion into (content, reasoning) the way SGLang would.

    Emulates `--reasoning-parser qwen3` at the serving layer: everything up to
    and including `</think>` is reasoning, the rest is the answer. Also unwraps
    a surrounding ```json fence — the reasoning parser would leave that in
    place, and it is the *second* cause of Harbor's "Extra text detected
    before JSON object" warning (4,710 of them across the current baseline).
    """
    reasoning = None
    end = content.find("</think>")
    if end != -1:
        cut = end + len("</think>")
        reasoning, content = content[:cut], content[cut:]
    content = content.strip()
    m = _FENCE.match(content)
    if m:
        content = m.group(1)
    return content, reasoning


def strip_reasoning(messages: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Return (history with assistant reasoning removed, chars removed)."""
    out: list[dict[str, Any]] = []
    removed = 0
    for m in messages:
        d = m if isinstance(m, dict) else {
            "role": getattr(m, "role", "?"), "content": getattr(m, "content", str(m))
        }
        content = d.get("content") or ""
        if d.get("role") == "assistant" and isinstance(content, str):
            end = content.find("</think>")
            if end != -1:
                cut = end + len("</think>")
                removed += cut
                d = {**d, "content": content[cut:].lstrip()}
        out.append(d)
    return out, removed


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
