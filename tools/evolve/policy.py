"""The algorithm under test. `hook.py` is plumbing; this file is the experiment.

Everything here is a no-op in the `log` arm. Arms are selected with
`EVOLVE_ARM`:

  log           pass-through + ledger (baseline; must reproduce harbor exactly)
  teach         inject the teacher's distilled rules once, at turn 1
  cards         `build_note` injects retrieved experience as a prompt suffix
  guard         intercept a *proposed* action, and if a card matches the
                command itself, regenerate the turn with that card attached
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
import os
import re
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Arm: cards
# --------------------------------------------------------------------------


# induced action keys start with the family verb (`call`, `tools`, `schema`);
# those name the invoker, not the tool, so they never take part in matching.
_VERBS = {"call", "tools", "schema"}

CARDS_DIR = os.environ.get("EVOLVE_CARDS", "jobs/_cards")
TEACHER_DIR = os.environ.get("EVOLVE_TEACHER_DIR", "jobs/_teacher")
_teacher: dict[str, list[dict[str, Any]]] = {}
MAX_CARDS = int(os.environ.get("EVOLVE_MAX_CARDS", "3"))
_store: dict[str, list[dict[str, Any]]] = {}


def _cards_for(task: str) -> list[dict[str, Any]]:
    """Cards for a task, loaded from its leave-one-task-out file.

    The file for task T is built from every *other* task's rollouts, so an
    injected card can never be T's own lesson played back at it.
    """
    if task not in _store:
        path = Path(CARDS_DIR) / f"{task}.json"
        try:
            _store[task] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            _store[task] = []
    return _store[task]


def teacher_prior(task: str) -> str | None:
    """Rules distilled from *other* tasks' rollouts, injected once per session.

    These are prose with no machine-checkable precondition, so they cannot go
    through `guard` (which needs a violation to fire on). They go in once at
    turn 1: the cheapest placement, and the model restates what it uses in its
    own analysis, which Harbor does keep in the history.
    """
    if task not in _teacher:
        try:
            _teacher[task] = json.loads((Path(TEACHER_DIR) / f"{task}.json").read_text())
        except (OSError, json.JSONDecodeError):
            _teacher[task] = []
    rules = _teacher[task]
    if not rules:
        return None
    lines = ["[operator note] Lessons from prior runs against this same "
             "environment, on other tasks. They are observations about how "
             "these tools behave, not instructions about your current task:"]
    lines += [f"- {r['rule']}" for r in rules]
    return "\n".join(lines)


def build_note(session: dict[str, Any], messages: list[dict[str, Any]]) -> str | None:
    """Return text to append after the latest observation, or None.

    Appended as a suffix so the prefix stays in SGLang's radix cache; the
    rollouts are prefill-dominated (up to ~55k prompt tokens/turn), so a
    front-injected note that changed each turn would force a full re-prefill.

    Retrieval is by action key, not prompt similarity: a card is relevant when
    the tokens naming its action (service, tool) are present in the task
    instruction or the recent transcript — i.e. when that tool is in play.
    """
    if "teach" in os.environ.get("EVOLVE_ARM", ""):
        # once per session, at the top of the run
        return teacher_prior(session.get("task", "?")) if session.get("turn") == 1 else None

    cards = _cards_for(session.get("task", "?"))
    if not cards:
        return None

    # The whole transcript, not a recent window. Only 15% of first-time tool
    # calls name the tool in the instruction; another 31% have it appear in an
    # earlier observation (the `mcp-tool tools <svc>` discovery step), which a
    # four-message window has almost always scrolled past.
    haystack = "\n".join(m.get("content") or "" for m in messages)

    scored = [(s, c.get("rollouts", 0), len(c.get("tasks", [])), c)
              for c in cards for s in (_relevance(c, haystack),) if s]
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))

    picked = [c for _, _, _, c in scored[:MAX_CARDS]]
    session.setdefault("cards_injected", set()).update(c["name"] for c in picked)
    from tools.evolve.mine import as_note

    return as_note(picked)


def _scope_and_tool(card: dict[str, Any]) -> tuple[list[str], str]:
    """(namespace tokens, tool token) from an induced action key."""
    tokens = [t for t in card.get("key", []) if t not in _VERBS]
    return (tokens[:-1], tokens[-1]) if tokens else ([], "")


def _relevance(card: dict[str, Any], haystack: str) -> int:
    """2 when the tool itself is in play, 1 when only its service is named.

    Service-level matching is what makes a card usable *before* the first
    call: an instruction names `woocommerce` long before `woo_products_get`
    ever appears.
    """
    scope, tool = _scope_and_tool(card)
    if not tool:
        return 0
    if tool in haystack and all(s in haystack for s in scope):
        return 2
    if scope and all(s in haystack for s in scope):
        return 1
    return 0


# A JSON string escape that is not one of the legal ones. This is the fault
# behind `mcp-tool: argument JSON is invalid: Invalid \escape` — the model
# writes \` around an identifier, which the shell strips and JSON rejects.
_BAD_ESCAPE = re.compile(r'\\[^"\\/bfnrtu]')


def violates(card: dict[str, Any], commands: str) -> bool:
    """Does the proposed command actually exhibit this card's fault?

    Matching on the tool alone fires on every correct call too. Measured over
    892 fires of the tool-only version: the violation was present in *zero* of
    them, and the model still rewrote its action 93% of the time — so the arm
    was noise, and it cost 11 points of reward.
    """
    kind = card.get("kind")
    if kind == "arg-name":
        try:
            bad = set(json.loads(card["example_failed"]))
            good = set(json.loads(card["example_fixed"]))
        except (json.JSONDecodeError, TypeError):
            return False
        return any(f'"{k}"' in commands for k in bad - good)
    if kind == "syntax":
        return bool(_BAD_ESCAPE.search(commands))
    return False


def commands_of(response: str) -> list[str]:
    obj = parse_terminus(response) or {}
    return [c.get("keystrokes", "") for c in (obj.get("commands") or [])
            if isinstance(c, dict)]


def guard(session: dict[str, Any], messages: list[dict[str, Any]], response: str
          ) -> tuple[str, list[dict[str, Any]]] | None:
    """Cards matching an action the model has *proposed but not yet run*.

    Retrieval before the fact can only ever cover the 46% of first-time calls
    whose tool is named somewhere earlier; the other 54% are cold. But the
    endpoint sees the proposed command in the completion before the harness
    executes it, so matching against the command itself covers everything —
    and costs tokens only on the turns where a card actually applies.
    """
    cards = _cards_for(session.get("task", "?"))
    if not cards:
        return None
    obj = parse_terminus(response) or {}
    commands = " ".join(
        c.get("keystrokes", "") for c in (obj.get("commands") or [])
        if isinstance(c, dict)
    )
    if not commands.strip():
        return None

    hits = []
    for c in cards:
        scope, tool = _scope_and_tool(c)
        if not (tool and tool in commands and all(s in commands for s in scope)):
            continue
        if not violates(c, commands):
            continue
        hits.append(c)
    if not hits:
        return None
    hits.sort(key=lambda c: (-c.get("rollouts", 0), -len(c.get("tasks", []))))
    seen: set[str] = set()          # one card per tool, best-evidenced first
    picked = [c for c in hits
              if not (c["name"] in seen or seen.add(c["name"]))][:MAX_CARDS]
    from tools.evolve.mine import as_note

    note = (as_note(picked) + "\n\nOne of the commands you just proposed breaks "
            "that contract, and it has not run yet. Reissue this turn's response "
            "with that one command corrected; leave everything else unchanged.")
    return note, picked


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
