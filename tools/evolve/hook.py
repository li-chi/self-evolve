"""In-process interception of every model call Harbor makes.

Harbor funnels all LLM traffic through one coroutine,
`harbor.llms.lite_llm.LiteLLM.call`: terminus-2's main loop reaches it via
`Chat.chat`, and the three summarization subagents call it directly from
`Terminus2._run_subagent`. Wrapping that coroutine gives the complete
provider-visible request/response stream with no proxy, no ports and no edit
to Harbor.

Two properties this hook point has, both verified against harbor 0.20.0:

- **Injection is invisible to the harness.** `Chat.chat` appends its *own*
  local `prompt` to the history after the call returns (chat.py:118), so a
  prompt we modify here never enters the conversation Harbor replays next
  turn. Every turn must re-inject, exactly as with a proxy.
- **One process, one event loop.** `Job._run` drives all trials through a
  single `asyncio.TaskGroup` (job.py:956), so the store below is a plain dict
  shared by all `-n 16` concurrent trials, and agent construction is
  synchronous — no interleaving between `BaseAgent.__init__` and the
  `LiteLLM.__init__` that follows it.

Ledger format: one JSONL file per trial under
`$EVOLVE_STORE/<arm>/<job>/<trial>.jsonl`, one record per model call. The job
name is part of the path because two runs of the same arm — a baseline before
and after a server config change, say — are different experiments. Message
history is stored as a delta against the previous call (a normal turn adds
exactly two messages), which also detects harness-side compaction for free:
when the common prefix is shorter than the previous history, the harness
dropped or rewrote something. `replay.reconstruct` rebuilds full histories.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.llms.base import LLMResponse
from harbor.llms.lite_llm import LiteLLM

from tools.evolve import policy

ARM = os.environ.get("EVOLVE_ARM", "log")
STORE = Path(os.environ.get("EVOLVE_STORE", "jobs/_evolve"))

_installed = False

# Set by BaseAgent.__init__ and read by the LiteLLM.__init__ that follows it in
# the same synchronous constructor (terminus_2.py:243 then :255). Safe because
# nothing awaits in between and the loop is single-threaded.
_pending_logs_dir: Path | None = None

# session key -> session state, live for the duration of the process.
sessions: dict[str, dict[str, Any]] = {}


# Harbor gives each attempt its own hash (`<task>__<hash>`); `-#k` shows up in
# the archived layout and, defensively, as a trailing attempt index.
_ATTEMPT_SUFFIX = re.compile(r"-#\d+$")
_HASH_SUFFIX = re.compile(r"__[A-Za-z0-9]{5,}$")


def _task_from_run(name: str) -> str:
    return _HASH_SUFFIX.sub("", _ATTEMPT_SUFFIX.sub("", name))

TASKS_ROOT = Path(os.environ.get("EVOLVE_TASKS_ROOT", "datasets/toolathlon"))


def _identity(logs_dir: Path | None) -> tuple[str, str, str]:
    """(job, task, run) for an agent's `logs_dir`.

    Harbor writes two layouts and the task name sits at a different depth in
    each, so decide by which candidate names a real task directory:

        live      jobs/<job>/<task>__<hash>[-#k]/agent
        archived  jobs/<dataset>/<task>/<model>-#k/agent   (archive_rollouts.py)

    That way one trial keeps one identity whether or not it has been archived.
    """
    if logs_dir is None:
        return ("?", "?", "?")
    run_dir = logs_dir.parent
    group = run_dir.parent.name
    from_run = _task_from_run(run_dir.name)
    if group and (TASKS_ROOT / group).is_dir():          # archived
        return (run_dir.parent.parent.name, group, run_dir.name)
    if from_run and (TASKS_ROOT / from_run).is_dir():    # live
        return (group, from_run, run_dir.name)
    return (group or "?", from_run or "?", run_dir.name)


def _fingerprints(messages: list[Any]) -> list[str]:
    out = []
    for m in messages:
        d = m if isinstance(m, dict) else getattr(m, "__dict__", {"content": str(m)})
        raw = f"{d.get('role', '')}\x00{d.get('content', '')}"
        out.append(hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16])
    return out


def _common_prefix(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _as_dict(m: Any) -> dict[str, Any]:
    if isinstance(m, dict):
        return m
    return {"role": getattr(m, "role", "?"), "content": getattr(m, "content", str(m))}


def _new_session(
    logs_dir: Path | None, session_id: str | None, model: str | None = None
) -> dict[str, Any]:
    job, task, run = _identity(logs_dir)
    key = run if run.startswith(task) else f"{task}__{run}"
    s = {
        "key": key,
        "job": job,
        "task": task,
        "run": run,
        "harbor_session_id": session_id,
        "model": model or "?",
        "logs_dir": str(logs_dir) if logs_dir else None,
        "arm": ARM,
        "turn": 0,
        # Message fingerprints of the previous call, per role. The
        # summarization subagents run their own histories through the same
        # LiteLLM instance, so a single chain would read every subagent call
        # as a compaction and corrupt the next main-loop delta.
        "fps": {"main": [], "subagent": []},
        "facts": [],        # whatever policy accumulates within the session
        "extra_calls": 0,   # model calls we added on top of the harness's
    }
    sessions[key] = s
    return s


def _ledger_path(s: dict[str, Any]) -> Path:
    # Keyed by job as well as arm: two runs of the same arm (e.g. a baseline
    # before and after a server config change) are different experiments and
    # must not land in one directory.
    d = STORE / ARM / s["job"]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{s['key']}.jsonl"


def _write(s: dict[str, Any], record: dict[str, Any]) -> None:
    """Append one record. Never raises: losing a log line must not kill a trial.

    The `log` arm is the control the whole experiment is measured against, so
    the hook has to be at least as reliable as plain Harbor. A full disk or an
    unencodable value costs us a ledger line, not a rollout.
    """
    try:
        with _ledger_path(s).open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:  # noqa: BLE001 - deliberately swallowing everything
        if not s.get("write_failed"):
            s["write_failed"] = True
            print(f"[evolve] ledger write failed for {s['key']}: {e!r}")


def _usage(r: LLMResponse) -> dict[str, Any] | None:
    u = r.usage
    if u is None:
        return None
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "cache_tokens": getattr(u, "cache_tokens", None),
        "cost_usd": getattr(u, "cost_usd", None),
    }


def _reasoning_leaked(r: LLMResponse) -> bool:
    """True if the server is emitting chain-of-thought inside `content`.

    Qwen3.6's chat template prefills the opening `<think>`, so a completion
    carries a closing `</think>` with no opener. With SGLang launched
    `--reasoning-parser qwen3` that prefix arrives as `reasoning_content` and
    `content` is clean; without it, Harbor stores the reasoning in the history
    and re-sends it every turn (~10% of prefill) while warning the model about
    "Extra text detected before JSON object" on most turns.

    Detected and flagged, deliberately not repaired: a silent client-side fix
    would hide a misconfigured server for a whole run.
    """
    return r.reasoning_content is None and "</think>" in (r.content or "")


def install() -> None:
    """Patch Harbor. Idempotent; call before importing the CLI app."""
    global _installed
    if _installed:
        return
    _installed = True

    base_init = BaseAgent.__init__

    @functools.wraps(base_init)
    def patched_base_init(self, logs_dir, *a, **kw):
        global _pending_logs_dir
        _pending_logs_dir = Path(logs_dir)
        return base_init(self, logs_dir, *a, **kw)

    BaseAgent.__init__ = patched_base_init

    llm_init = LiteLLM.__init__

    @functools.wraps(llm_init)
    def patched_llm_init(self, *a, **kw):
        llm_init(self, *a, **kw)
        self._evolve = _new_session(
            _pending_logs_dir,
            getattr(self, "_session_id", None),
            getattr(self, "_model_name", None),
        )

    LiteLLM.__init__ = patched_llm_init

    orig_call = LiteLLM.call

    @functools.wraps(orig_call)
    async def patched_call(self, prompt, message_history=[], **kw):
        s = getattr(self, "_evolve", None)
        if s is None:  # constructed before install(); log nothing, stay transparent
            return await orig_call(self, prompt, message_history, **kw)

        s["turn"] += 1
        # Only Chat.chat passes previous_response_id (chat.py:88); the
        # summarization subagents call LiteLLM.call directly without it.
        role = "main" if "previous_response_id" in kw else "subagent"

        messages = [_as_dict(m) for m in message_history] + [
            {"role": "user", "content": prompt}
        ]
        fps = _fingerprints(messages)
        prev = s["fps"][role]
        reused = _common_prefix(prev, fps)
        dropped = len(prev) - reused
        s["fps"][role] = fps

        # ---- WRITE: suffix-only, so SGLang's radix cache keeps the prefix ----
        note = policy.build_note(s, messages) if "cards" in ARM else None
        sent = f"{prompt}\n\n{note}" if note else prompt

        t0 = time.time()
        r = await orig_call(self, sent, message_history, **kw)
        latency = time.time() - t0

        leaked = _reasoning_leaked(r)
        if leaked and not s.get("leak_warned"):
            s["leak_warned"] = True
            print(
                f"[evolve] {s['task']}: reasoning is arriving inside content — "
                "SGLang is missing --reasoning-parser qwen3"
            )

        # ---- pre-execution guard: the proposed action has not run yet ----
        guard = None
        if "guard" in ARM and role == "main":
            g = policy.guard(s, messages, r.content)
            if g:
                note, hits = g
                before = r.content
                r = await orig_call(self, f"{sent}\n\n{note}", message_history, **kw)
                s["extra_calls"] += 1
                # counterfactual: the same context with and without the card,
                # so card influence is measurable per turn without any reward
                guard = {
                    "cards": [c["name"] for c in hits],
                    "before": before,
                    "changed": policy.commands_of(before)
                    != policy.commands_of(r.content),
                }

        # ---- extra compute: another call on the same endpoint, harness-invisible ----
        audit = None
        if "audit" in ARM and role == "main" and policy.claims_done(r.content):
            v = await orig_call(
                self, policy.audit_prompt(messages, r.content), message_history, **kw
            )
            s["extra_calls"] += 1
            audit = {"verdict": v.content, "usage": _usage(v), "applied": False}
            if policy.accept_audit(v.content):
                audit["applied"] = True
                r = LLMResponse(
                    content=v.content,
                    reasoning_content=v.reasoning_content,
                    model_name=r.model_name,
                    usage=r.usage,
                )

        _write(
            s,
            {
                "task": s["task"],
                "run": s["run"],
                "job": s["job"],
                "logs_dir": s["logs_dir"],
                "model": s["model"],
                "arm": ARM,
                "turn": s["turn"],
                "role": role,
                "ts": time.time(),
                "latency_s": round(latency, 3),
                "prefix_reused": reused,
                "dropped": dropped,
                "compacted": dropped > 0 and role == "main",
                "new_messages": messages[reused:],
                "injected": note,
                "guard": guard,
                "reasoning_leak": leaked,
                "response": r.content,
                "reasoning": r.reasoning_content,
                "usage": _usage(r),
                "audit": audit,
            },
        )
        return r

    LiteLLM.call = patched_call
