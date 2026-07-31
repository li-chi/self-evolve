"""Smoke test for toolathlon-locals + agent_loop harness coupling.

Mounts only the toolathlon-locals MCP server (no other servers), stubs
`anthropic.AsyncAnthropic` to return canned tool calls, and asserts:

  (a) local-python-execute returns '4' for print(2+2)
  (b) local-check_context_status returns a payload with usage_percentage
  (c) local-claim_done creates claimed_done.flag
  (d) the loop breaks within 3 turns

Then a second mini-scenario exercises the overlong spool path by
asking python_execute to print a huge string and verifying the runner
spools it under overlong/.

Migrated from a litellm.acompletion stub when the runner moved to the
Anthropic SDK; the stub now imitates Anthropic's Messages response
shape (content blocks with type=text|tool_use, usage with
input_tokens/output_tokens).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent  # mcp-env/
sys.path.insert(0, str(REPO_ROOT))

import anthropic

from runner.agent_loop import RunConfig, Runner
from runner.locals import default_local_server_spec


# ---------------------------------------------------------------------
# Canned-LLM stub (Anthropic Messages shape)
# ---------------------------------------------------------------------

class _Block:
    """One content block: either type='text' or type='tool_use'."""
    def __init__(self, btype: str, *, text: str = "", id: str = "",
                 name: str = "", input: dict | None = None):
        self.type = btype
        if btype == "text":
            self.text = text
        else:  # tool_use
            self.id = id
            self.name = name
            self.input = input or {}


class _Usage:
    def __init__(self, in_tokens: int = 10, out_tokens: int = 5):
        self.input_tokens = in_tokens
        self.output_tokens = out_tokens


class _Resp:
    def __init__(self, content_blocks: list[_Block], stop_reason: str):
        self.content = content_blocks
        self.stop_reason = stop_reason
        self.usage = _Usage()


def _make_resp(text: str = "",
               tool_calls: list[dict] | None = None) -> _Resp:
    """Build an Anthropic-shaped Messages response.

    `tool_calls` is a list of {"name": str, "arguments": dict|str};
    each becomes a `tool_use` block. If empty, we emit a single text
    block and stop_reason='end_turn' so the runner exits the loop."""
    blocks: list[_Block] = []
    if text:
        blocks.append(_Block("text", text=text))
    for i, c in enumerate(tool_calls or []):
        args = c["arguments"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        blocks.append(_Block("tool_use", id=f"toolu_{i}",
                             name=c["name"], input=args))
    stop = "tool_use" if (tool_calls or []) else "end_turn"
    if not blocks:
        blocks.append(_Block("text", text="done"))
    return _Resp(blocks, stop)


class _StubMessages:
    """Mimics `client.messages` — only `create()` is needed."""
    def __init__(self, parent: "CannedAnthropic"):
        self._parent = parent

    async def create(self, **_kwargs):
        return self._parent._next()


class _StubClient:
    def __init__(self, parent: "CannedAnthropic"):
        self.messages = _StubMessages(parent)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class CannedAnthropic:
    """Replacement for `anthropic.AsyncAnthropic`.

    Returns a stub client whose `messages.create` returns successive
    scripted responses. Once the script is exhausted, returns a plain
    `text='done'` end-turn message so the agent loop terminates."""

    def __init__(self, scripts: list[dict]):
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        return _StubClient(self)

    def _next(self) -> _Resp:
        if self.calls >= len(self.scripts):
            self.calls += 1
            return _make_resp(text="done")
        s = self.scripts[self.calls]
        self.calls += 1
        return _make_resp(
            text=s.get("content", ""),
            tool_calls=s.get("tool_calls"),
        )


# ---------------------------------------------------------------------
# Scenario A: python-execute + check_context_status + claim_done
# ---------------------------------------------------------------------

async def scenario_a(workspace: Path) -> dict:
    print("\n--- scenario A: python + context + claim_done ---")
    scripts = [
        {"content": "",
         "tool_calls": [{"name": "local-python-execute",
                         "arguments": {"code": "print(2 + 2)"}}]},
        {"content": "",
         "tool_calls": [{"name": "local-check_context_status",
                         "arguments": {}}]},
        {"content": "",
         "tool_calls": [{"name": "local-claim_done",
                         "arguments": {}}]},
    ]
    canned = CannedAnthropic(scripts)
    orig = anthropic.AsyncAnthropic
    anthropic.AsyncAnthropic = canned
    try:
        cfg = RunConfig(
            model="stub",
            instruction="compute 2+2, check context, then claim done",
            servers=[default_local_server_spec(workspace)],
            max_turns=5,
            system_prompt=None,
            spool_locals=True,
            spool_dir=str(workspace / "output"),
            overlong_threshold=4000,
            context_limit=128000,
        )
        async with Runner(cfg) as r:
            traj = await r.run()
    finally:
        anthropic.AsyncAnthropic = orig

    py_out = next((e["content"] for e in traj
                   if e.get("role") == "tool"
                   and e.get("name") == "local-python-execute"), None)
    ctx_out = next((e["content"] for e in traj
                    if e.get("role") == "tool"
                    and e.get("name") == "local-check_context_status"),
                   None)
    done_out = next((e["content"] for e in traj
                     if e.get("role") == "tool"
                     and e.get("name") == "local-claim_done"), None)

    assert py_out is not None, "no python_execute tool result in trajectory"
    assert "4" in py_out, f"expected '4' in python output, got: {py_out!r}"
    print("(a) python_execute returned 4: OK")

    assert ctx_out is not None, "no check_context_status tool result"
    assert "usage_percentage" in ctx_out, \
        f"expected usage_percentage field, got: {ctx_out!r}"
    print("(b) context_status has usage_percentage: OK")

    flag = workspace / "output" / "_runner" / "claimed_done.flag"
    assert flag.exists(), f"claimed_done.flag not created at {flag}"
    print("(c) claimed_done.flag created: OK")

    assert canned.calls <= 3, f"loop ran {canned.calls} times (>3)"
    print(f"(d) loop broke within {canned.calls} turns: OK")

    return {
        "py_out": py_out,
        "ctx_out": ctx_out,
        "done_out": done_out,
        "turns": canned.calls,
    }


# ---------------------------------------------------------------------
# Scenario B: overlong spool + view
# ---------------------------------------------------------------------

async def scenario_b(workspace: Path) -> dict:
    print("\n--- scenario B: overlong spool + view ---")
    scripts_p1 = [
        {"content": "",
         "tool_calls": [{"name": "local-python-execute",
                         "arguments": {"code": "print('X' * 6000)"}}]},
        {"content": "",
         "tool_calls": [{"name": "local-claim_done",
                         "arguments": {}}]},
    ]
    canned = CannedAnthropic(scripts_p1)
    orig = anthropic.AsyncAnthropic
    anthropic.AsyncAnthropic = canned
    try:
        cfg = RunConfig(
            model="stub",
            instruction="print 6000 X's",
            servers=[default_local_server_spec(workspace)],
            max_turns=4,
            system_prompt=None,
            spool_locals=True,
            spool_dir=str(workspace / "output"),
            overlong_threshold=4000,
            context_limit=128000,
        )
        async with Runner(cfg) as r1:
            traj1 = await r1.run()
    finally:
        anthropic.AsyncAnthropic = orig

    py_out = next((e["content"] for e in traj1
                   if e.get("role") == "tool"
                   and e.get("name") == "local-python-execute"), None)
    assert py_out is not None, "no python_execute tool result"
    assert "tool output too large to inline" in py_out, \
        f"expected spool stub, got: {py_out!r}"
    print(f"(b1) model-visible result is spool stub: OK ({len(py_out)} chars)")

    overlong_dir = workspace / "output" / "_runner" / "overlong"
    files = list(overlong_dir.glob("*.txt"))
    assert len(files) == 1, f"expected 1 spool file, got {files}"
    shortuuid = files[0].stem
    full = files[0].read_text()
    assert full.count("X") >= 6000, \
        f"spool file too short: {len(full)} chars"
    print(f"(b2) spool file under overlong/ contains full text: OK "
          f"({len(full)} chars, shortuuid={shortuuid})")

    scripts_p2 = [
        {"content": "",
         "tool_calls": [{"name": "local-view_overlong_tooloutput",
                         "arguments": {"shortuuid": shortuuid,
                                       "page_size": 200}}]},
        {"content": "",
         "tool_calls": [{"name": "local-claim_done",
                         "arguments": {}}]},
    ]
    canned2 = CannedAnthropic(scripts_p2)
    anthropic.AsyncAnthropic = canned2
    try:
        cfg = RunConfig(
            model="stub",
            instruction="view the spooled output",
            servers=[default_local_server_spec(workspace)],
            max_turns=4,
            system_prompt=None,
            spool_locals=True,
            spool_dir=str(workspace / "output"),
            overlong_threshold=4000,
            context_limit=128000,
        )
        async with Runner(cfg) as r2:
            traj2 = await r2.run()
    finally:
        anthropic.AsyncAnthropic = orig

    view_out = next((e["content"] for e in traj2
                     if e.get("role") == "tool"
                     and e.get("name") == "local-view_overlong_tooloutput"),
                    None)
    assert view_out is not None, "no view_overlong tool result"
    assert "XXXXX" in view_out, \
        f"expected XXXXX in view output, got: {view_out[:200]!r}"
    print("(b3) local-view_overlong_tooloutput returned the spooled "
          "content: OK")
    return {"shortuuid": shortuuid, "spool_size": len(full)}


async def main():
    with tempfile.TemporaryDirectory(prefix="toolathlon-locals-smoke-") as td:
        workspace = Path(td)
        (workspace / "output").mkdir(parents=True, exist_ok=True)
        a = await scenario_a(workspace)
        b = await scenario_b(workspace)
        print("\n=== smoke summary ===")
        print(json.dumps({"scenario_a": {k: (str(v)[:80] if v else v)
                                          for k, v in a.items()},
                          "scenario_b": b}, indent=2))
        print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
