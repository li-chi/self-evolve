"""Assert the `log` arm is a genuine pass-through.

    .venv/bin/python -m tools.evolve.selftest

Every result the experiment produces is `cards` measured against `log`, so if
the hook perturbs the control the baseline is wrong and so is every delta. The
useful check is not a statistical one — sampling is stochastic and 25 trials a
side resolve nothing smaller than a landslide — but an exact one: the wrapper
must hand the real `LiteLLM.call` the *same objects* it was given, and hand
back the response untouched.

Run before trusting any arm comparison, and after touching `hook.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("EVOLVE_ARM", "log")
os.environ["EVOLVE_STORE"] = tempfile.mkdtemp(prefix="evolve-selftest-")

from pathlib import Path  # noqa: E402

from harbor.llms.base import LLMResponse  # noqa: E402
from harbor.llms.lite_llm import LiteLLM  # noqa: E402

from tools.evolve import hook, replay  # noqa: E402

FAILURES: list[str] = []
SEEN: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def install_recorder() -> None:
    """Put a recorder where the real network call sits, then wrap it."""

    async def recorder(self, prompt, message_history=[], **kw):
        r = LLMResponse(content='{"analysis":"a","plan":"p","commands":[]}',
                        model_name="stub")
        SEEN.append({"prompt": prompt, "history": message_history, "kw": kw,
                     "response": r})
        return r

    LiteLLM.call = recorder
    hook._installed = False
    hook.install()


def new_llm(task: str = "sales-accounting") -> LiteLLM:
    llm = LiteLLM.__new__(LiteLLM)
    llm._evolve = hook._new_session(
        Path(f"jobs/selftest-job/{task}__aB3xQ9z/agent"), "trial__agent", "stub-model"
    )
    return llm


async def main() -> int:
    install_recorder()

    # ---- 1. the request reaches the real call unchanged -------------------
    print("\nlog arm forwards the request untouched")
    llm = new_llm()
    prompt = "terminal output here"
    history = [{"role": "system", "content": "SYS"},
               {"role": "assistant", "content": "prev"}]
    kw = {"previous_response_id": None, "logging_path": None}
    SEEN.clear()
    out = await LiteLLM.call(llm, prompt, history, **kw)
    got = SEEN[-1]
    check("prompt is the same object", got["prompt"] is prompt)
    check("message_history is the same object", got["history"] is history)
    check("kwargs forwarded verbatim", got["kw"] == kw, repr(got["kw"]))
    check("response returned unmodified", out is got["response"])
    check("history not mutated", history == [{"role": "system", "content": "SYS"},
                                             {"role": "assistant", "content": "prev"}])

    # ---- 2. same for the summarization subagent path ----------------------
    print("\nsubagent path (no previous_response_id) also untouched")
    SEEN.clear()
    sub_prompt = "summarize the work so far"
    out = await LiteLLM.call(llm, sub_prompt, history)
    got = SEEN[-1]
    check("prompt is the same object", got["prompt"] is sub_prompt)
    check("message_history is the same object", got["history"] is history)
    check("no kwargs invented", got["kw"] == {})
    check("response returned unmodified", out is got["response"])

    # ---- 3. a broken ledger must not kill a trial -------------------------
    print("\nledger failure is non-fatal")
    original = hook._ledger_path
    hook._ledger_path = lambda s: (_ for _ in ()).throw(OSError("disk full"))
    try:
        out = await LiteLLM.call(new_llm(), "obs", [], previous_response_id=None)
        check("call still returns", out is not None)
    except Exception as e:  # noqa: BLE001
        check("call still returns", False, repr(e))
    finally:
        hook._ledger_path = original

    # ---- 4. reasoning-leak canary -----------------------------------------
    print("\nreasoning-leak canary")
    clean = LLMResponse(content='{"ok":1}', reasoning_content="thought", model_name="m")
    leaked = LLMResponse(content='reasoning\n</think>\n{"ok":1}', model_name="m")
    check("clean response not flagged", hook._reasoning_leaked(clean) is False)
    check("un-split response flagged", hook._reasoning_leaked(leaked) is True)

    # ---- 5. per-role delta chains (regression: subagents corrupted main) --
    print("\ndelta chains are tracked per role")
    llm = new_llm("imagenet")
    hist = [{"role": "system", "content": "SYS"}]
    for i in range(3):
        r = await LiteLLM.call(llm, f"obs {i}", hist, previous_response_id=None)
        hist = hist + [{"role": "user", "content": f"obs {i}"},
                       {"role": "assistant", "content": r.content}]
    await LiteLLM.call(llm, "summarize", hist)                       # subagent
    await LiteLLM.call(llm, "obs 3", hist, previous_response_id=None)  # main resumes
    recs = list(replay.reconstruct(hook._ledger_path(llm._evolve)))
    main = [r for r in recs if r["role"] == "main"]
    check("no spurious compaction on the main chain",
          not any(r["compacted"] for r in main),
          f"compacted={[r['compacted'] for r in main]}")
    check("main chain keeps growing after a subagent call",
          main[-1]["prefix_reused"] == 6, f"reused={main[-1]['prefix_reused']}")

    # ---- 5b. the induced grammar still parses a tool call -----------------
    # A grammar that stops matching makes the miner silently return nothing.
    # It broke once: `_induce` extended into a 5th token position that almost
    # no command has, froze a straggler in as a literal, and the family went
    # from 11,718 matches to 2.
    print("\ninduced grammar parses a real tool call")
    from tools.evolve import contract, mine, replay as _r
    ct = contract.learn(list(_r.iter_trajectory_turns(exclude_jobs=set(mine.INTERVENED))))
    parsed = ct.action_parts(
        "mcp-tool call google-cloud bigquery_run_query '{\"query\": \"SELECT 1\"}'")
    check("tool call parses to (service, tool) + args",
          parsed is not None and parsed[0] == ("google-cloud", "bigquery_run_query"),
          repr(parsed)[:80])
    matched = sum(1 for t in _r.iter_trajectory_turns(exclude_jobs=set(mine.INTERVENED))
                  for c in t.commands if ct.action_key(c))
    check("grammar matches a bulk of the corpus", matched > 1000, f"{matched} commands")

    # ---- 6. identity resolution for both Harbor layouts -------------------
    print("\ntrial identity")
    cases = {
        "jobs/ab-log/sales-accounting__6uaKCiu/agent":
            ("ab-log", "sales-accounting"),
        "jobs/postfix-log/imagenet__aB3xQ9z-#2/agent":
            ("postfix-log", "imagenet"),
        "jobs/toolathlon/imagenet/qwen3.6-35b-#3/agent":
            ("toolathlon", "imagenet"),
    }
    for path, (want_job, want_task) in cases.items():
        job, task, _ = hook._identity(Path(path))
        check(f"{path.split('/')[1]:12} -> job={want_job}, task={want_task}",
              (job, task) == (want_job, want_task), f"got ({job}, {task})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed — the log arm is a pass-through")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
