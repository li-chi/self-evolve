"""Tier-B replay-proxy *recording* MCP server.

This is the missing-piece counterpart to ``server.py``. ``server.py``
*replays* cassettes; this server *populates* them. It sits between an
agent (the MCP client) and a real upstream MCP server (the
subprocess), forwarding every ``tool/call`` to the upstream and
appending each ``(tool, args, response)`` triple to a JSONL cassette
on the way back.

Operationally:

  agent ──stdio──► record_server.py ──stdio──► real upstream MCP server
                          │
                          ▼
                  cassettes/<server>.jsonl

The agent sees a normal MCP server: same tool names, same parameter
schemas, same responses. The cassette grows as a side effect.

Configured via ``$REPLAY_RECORDER_CONFIG`` (JSON):

    {
      "server":   "yahoo-finance",
      "cassette": "/workspace/cassettes/yahoo-finance.jsonl",
      "upstream": {
        "command": "uv",
        "args":    ["run", "/path/to/upstream/server.py"],
        "env":     {"YAHOO_API_KEY": "..."},
        "cwd":     "/path/to/upstream"
      },
      "tool_allowlist": ["get_stock_quote", "get_history"],   // optional
      "tool_blocklist": []                                     // optional
    }

The recorder *does not dedupe* — duplicates are tolerated, and the
replay proxy uses last-wins on read. Run ``replay validate`` /
``replay merge`` (or a future ``dedupe`` command) before freezing.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import signal
import sys
import textwrap
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from replay.cassette import (
    CassetteEntry,
    args_hash,
    canonicalize_args,
    write_entry,
)


# ---------------------------------------------------------------------------
# Env + paths
# ---------------------------------------------------------------------------

DEFAULT_STATE_DIR = os.path.expanduser("~/.openclaw/replay_recorder")
LOG = logging.getLogger("replay.record")


def _state_dir() -> str:
    d = os.environ.get("REPLAY_RECORDER_STATE_DIR", DEFAULT_STATE_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _errors_path() -> str:
    return os.path.join(_state_dir(), "errors.jsonl")


def _calls_path() -> str:
    return os.path.join(_state_dir(), "recorded_calls.jsonl")


def _utc_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _append_jsonl(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False,
                           separators=(",", ":")))
        f.write("\n")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("REPLAY_RECORDER_CONFIG")
    if not path:
        raise SystemExit(
            "REPLAY_RECORDER_CONFIG is unset and no --config given; "
            "see recorder.config.example.json")
    if not os.path.exists(path):
        raise SystemExit(f"recorder config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("server", "cassette", "upstream"):
        if k not in cfg:
            raise SystemExit(
                f"recorder config missing required key {k!r}: {path}")
    up = cfg["upstream"]
    if not isinstance(up, dict) or "command" not in up:
        raise SystemExit(
            f"recorder config 'upstream' must be {{command, args?, env?}}: {path}")
    return cfg


# ---------------------------------------------------------------------------
# Result unwrapping
# ---------------------------------------------------------------------------

def _unwrap_call_result(result: Any) -> Any:
    """Convert an MCP ``CallToolResult`` into the plain Python value we
    want to cache. Preference order:

      1. ``structuredContent`` if present (the upstream explicitly
         returned a typed object).
      2. Single-block ``TextContent`` whose text is valid JSON →
         parsed JSON.
      3. Single-block ``TextContent`` → raw string.
      4. Multi-block content → list of dicts (TextContent.model_dump).

    If the result is an error we return a ``{"error": ..., "content": ...}``
    envelope so cassettes preserve upstream error responses too.
    """
    is_error = bool(getattr(result, "isError", False))
    structured = getattr(result, "structuredContent", None)
    content = getattr(result, "content", None) or []

    payload: Any
    if structured is not None:
        payload = structured
    elif len(content) == 1 and isinstance(content[0], TextContent):
        text = content[0].text
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = text
    else:
        payload = [
            (c.model_dump(mode="json", exclude_none=True)
             if hasattr(c, "model_dump") else c)
            for c in content
        ]

    if is_error:
        return {"error": "upstream_tool_error", "content": payload}
    return payload


# ---------------------------------------------------------------------------
# JSON-Schema → Python signature synthesis
# ---------------------------------------------------------------------------

_JSON_TYPE_MAP = {
    "string":  "str",
    "number":  "float",
    "integer": "int",
    "boolean": "bool",
    "array":   "list",
    "object":  "dict",
    "null":    "type(None)",
}


def _py_annotation(schema: dict) -> str:
    """Best-effort mapping from a JSON-schema property entry to a
    Python type annotation. We deliberately keep this small — FastMCP
    only uses the annotation for schema *display*; runtime validation
    is loose."""
    t = schema.get("type")
    if isinstance(t, list):
        # nullable / union — fall back to Any
        return "Any"
    if isinstance(t, str):
        return _JSON_TYPE_MAP.get(t, "Any")
    if "anyOf" in schema or "oneOf" in schema:
        return "Any"
    return "Any"


def _safe_ident(name: str) -> str:
    """Tool/param names can contain hyphens. Convert to a valid Python
    identifier for the wrapper signature; we map back at call time."""
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    if not out or out[0].isdigit():
        out = "p_" + out
    return out


def _build_wrapper(
    tool_name: str,
    input_schema: dict,
    forward: Callable[[dict[str, Any]], Any],
) -> Callable[..., Any]:
    """Synthesize an async function with a parameter list mirroring
    the upstream tool's JSON schema. The body forwards into
    ``forward`` (which takes a single dict of the original arg names).

    We can't just declare ``async def f(**kwargs)`` because FastMCP
    introspects the signature to build the schema it advertises to
    the client. If we want the agent to see the upstream's parameter
    list, we have to recreate it on a Python ``def``.
    """
    props: dict = (input_schema or {}).get("properties") or {}
    required: list[str] = list((input_schema or {}).get("required") or [])

    # Param order: required first (alphabetized), then optional
    # (alphabetized) — agnostic to the order JSON schema declared.
    req_names = sorted(p for p in props if p in required)
    opt_names = sorted(p for p in props if p not in required)
    ordered = req_names + opt_names

    # Map sanitized identifier → original key
    py_to_orig: dict[str, str] = {}
    sig_parts: list[str] = []
    for orig in ordered:
        ident = _safe_ident(orig)
        # Avoid collisions between sanitized names.
        while ident in py_to_orig or ident in ("self", "cls"):
            ident += "_"
        py_to_orig[ident] = orig
        ann = _py_annotation(props.get(orig) or {})
        if orig in required:
            sig_parts.append(f"{ident}: {ann}")
        else:
            sig_parts.append(f"{ident}: {ann} | None = None")

    sig = ", ".join(sig_parts) if sig_parts else ""
    fn_ident = _safe_ident(tool_name)

    src = textwrap.dedent(f"""
    async def {fn_ident}({sig}):
        # Reconstruct the upstream-shaped args dict, dropping Nones
        # so omitted optionals match the canonicalizer's treatment.
        _local = locals()
        _args = {{}}
        for _py_name, _orig in _PY_TO_ORIG.items():
            _v = _local.get(_py_name)
            if _v is not None:
                _args[_orig] = _v
        return await _FORWARD(_args)
    """).strip()

    ns: dict = {
        "_FORWARD": forward,
        "_PY_TO_ORIG": py_to_orig,
        "Any": Any,
    }
    try:
        exec(src, ns)
    except SyntaxError:
        # Fallback: generic **kwargs forwarder. FastMCP will still
        # register it, just without typed params.
        async def _generic(**kwargs: Any) -> Any:
            return await forward(
                {k: v for k, v in kwargs.items() if v is not None})
        _generic.__name__ = fn_ident
        return _generic

    wrapped = ns[fn_ident]
    wrapped.__name__ = fn_ident
    return wrapped


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    """Bridges a FastMCP server (agent-facing stdio) to a single
    upstream MCP server (subprocess stdio), recording every call.

    Lifecycle:
      ``async with Recorder(cfg) as rec:``
        - spawns upstream
        - initialize() + list_tools()
        - rec.register_tools(fastmcp)
        - rec serves until cancellation
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.server_name: str = cfg["server"]
        self.cassette_path: str = cfg["cassette"]
        self.allowlist: set[str] | None = (
            set(cfg["tool_allowlist"]) if cfg.get("tool_allowlist") else None)
        self.blocklist: set[str] = set(cfg.get("tool_blocklist") or [])
        self.session: ClientSession | None = None
        self._cm = None                    # async context manager
        self._streams_cm = None            # stdio_client cm
        self.tools: list[Any] = []          # populated after init
        # Serialize forward calls so we don't interleave on the
        # upstream session (MCP sessions are not concurrency-safe by
        # default and we keep cassette write order matching call order).
        self._lock = asyncio.Lock()

    # -- subprocess management --------------------------------------------

    async def start(self) -> None:
        up = self.cfg["upstream"]
        env = dict(os.environ)
        if up.get("env"):
            env.update({str(k): str(v) for k, v in up["env"].items()})
        params = StdioServerParameters(
            command=up["command"],
            args=list(up.get("args") or []),
            env=env,
            cwd=up.get("cwd"),
        )
        LOG.info("spawning upstream: %s %s",
                 params.command, " ".join(params.args))
        self._streams_cm = stdio_client(params)
        read, write = await self._streams_cm.__aenter__()
        self._cm = ClientSession(read, write)
        self.session = await self._cm.__aenter__()
        await self.session.initialize()
        tools_res = await self.session.list_tools()
        self.tools = list(tools_res.tools)
        LOG.info("upstream advertises %d tool(s): %s",
                 len(self.tools), ", ".join(t.name for t in self.tools))

    async def stop(self) -> None:
        # Tear down in reverse order; swallow errors during shutdown.
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)
            self._cm = None
            self.session = None
        if self._streams_cm is not None:
            with contextlib.suppress(Exception):
                await self._streams_cm.__aexit__(None, None, None)
            self._streams_cm = None

    async def __aenter__(self) -> "Recorder":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    # -- per-tool registration --------------------------------------------

    def selected_tools(self) -> list[Any]:
        out = []
        for t in self.tools:
            if self.allowlist is not None and t.name not in self.allowlist:
                continue
            if t.name in self.blocklist:
                continue
            out.append(t)
        return out

    def _make_forward(self, tool_name: str) -> Callable[[dict], Any]:
        async def _forward(args: dict[str, Any]) -> Any:
            if self.session is None:
                return {
                    "error": "upstream_not_ready",
                    "tool": tool_name,
                    "server": self.server_name,
                }
            canon = canonicalize_args(args)
            h = args_hash(canon)
            try:
                async with self._lock:
                    result = await self.session.call_tool(tool_name, args)
                payload = _unwrap_call_result(result)
            except Exception as exc:  # noqa: BLE001 — we want any failure
                err = {
                    "ts": _utc_now(),
                    "server": self.server_name,
                    "tool": tool_name,
                    "args_hash": h,
                    "args": canon,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                LOG.exception("upstream call failed: %s", tool_name)
                _append_jsonl(_errors_path(), err)
                return {
                    "error": "upstream_call_failed",
                    "tool": tool_name,
                    "server": self.server_name,
                    "detail": err["error"],
                }

            entry = CassetteEntry(
                server=self.server_name,
                tool=tool_name,
                args_hash=h,
                args=canon,
                response=payload,
                recorded_at=_utc_now(),
            )
            try:
                write_entry(self.cassette_path, entry)
            except OSError as exc:
                LOG.error("cassette write failed (%s): %s",
                          self.cassette_path, exc)
                _append_jsonl(_errors_path(), {
                    "ts": _utc_now(),
                    "server": self.server_name,
                    "tool": tool_name,
                    "args_hash": h,
                    "error": f"cassette_write_failed: {exc}",
                })

            _append_jsonl(_calls_path(), {
                "ts": _utc_now(),
                "server": self.server_name,
                "tool": tool_name,
                "args_hash": h,
                "cassette": self.cassette_path,
            })
            return payload
        return _forward

    def register_tools(self, mcp: FastMCP) -> int:
        count = 0
        for t in self.selected_tools():
            fwd = self._make_forward(t.name)
            wrapper = _build_wrapper(
                tool_name=t.name,
                input_schema=t.inputSchema or {},
                forward=fwd,
            )
            description = (t.description
                           or f"[recorder] {self.server_name}.{t.name} "
                              f"— forwards to upstream and appends to "
                              f"{self.cassette_path}")
            mcp.add_tool(wrapper, name=t.name, description=description)
            count += 1
        return count


# ---------------------------------------------------------------------------
# Server entrypoint (long-lived recorder)
# ---------------------------------------------------------------------------

async def _run_server_async(cfg: dict) -> None:
    """Build the FastMCP server, register every upstream tool as a
    recording forwarder, then serve stdio until cancelled."""
    mcp = FastMCP(f"replay-recorder[{cfg['server']}]")

    rec = Recorder(cfg)
    await rec.start()
    n = rec.register_tools(mcp)
    LOG.info("registered %d tool(s) on recorder; cassette=%s",
             n, rec.cassette_path)

    # Debug tool — agent-visible introspection.
    @mcp.tool(
        name="mock_debug_state",
        description="Recorder introspection: upstream tools, cassette path, counters.",
    )
    def mock_debug_state() -> dict:
        recorded = 0
        if os.path.exists(_calls_path()):
            with open(_calls_path(), "r", encoding="utf-8") as f:
                recorded = sum(1 for _ in f if _.strip())
        return {
            "mode": "recorder",
            "server": rec.server_name,
            "cassette": rec.cassette_path,
            "upstream": cfg["upstream"],
            "registered_tools": [t.name for t in rec.selected_tools()],
            "state_dir": _state_dir(),
            "totals": {"recorded_calls": recorded},
        }

    # Wire signal handlers for graceful shutdown of the upstream subprocess.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sig() -> None:
        LOG.info("received shutdown signal; draining upstream")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_sig)

    server_task = asyncio.create_task(mcp.run_stdio_async())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            {server_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None and not isinstance(
                        exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    LOG.error("server task crashed: %r", exc)
    finally:
        await rec.stop()


def run_server(config_path: str | None = None) -> None:
    """Synchronous entrypoint — what the CLI calls."""
    logging.basicConfig(
        level=os.environ.get("REPLAY_RECORDER_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config(config_path)
    try:
        asyncio.run(_run_server_async(cfg))
    except KeyboardInterrupt:
        LOG.info("interrupted")


# ---------------------------------------------------------------------------
# Trajectory replay → cassette
# ---------------------------------------------------------------------------

def _iter_trajectory(path: str):
    """Yield ``(tool, args)`` tuples from a trajectory JSONL.

    Tolerated shapes per line:

      * ``{"tool": "...", "args": {...}}``
      * ``{"name": "...", "arguments": {...}}``
      * ``{"tool_calls": [{"function": {"name": "...",
                                          "arguments": "<json-string>"}}]}``
      * ``{"function": {"name": "...", "arguments": "<json|dict>"}}``

    Lines that don't match are skipped with a warning so a single
    malformed event doesn't kill the whole run.
    """
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                LOG.warning("trajectory %s:%d: not JSON (%s)", path, i, exc)
                continue
            for tool, args in _extract_tool_calls(obj):
                yield tool, args


def _coerce_args(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"_raw": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {"_raw": raw}


def _extract_tool_calls(obj: Any):
    if not isinstance(obj, dict):
        return
    # Native shape
    if "tool" in obj and ("args" in obj or "arguments" in obj):
        yield obj["tool"], _coerce_args(obj.get("args", obj.get("arguments")))
        return
    if "name" in obj and "arguments" in obj and "tool_calls" not in obj:
        yield obj["name"], _coerce_args(obj["arguments"])
        return
    # OpenAI-style
    tcs = obj.get("tool_calls")
    if isinstance(tcs, list):
        for tc in tcs:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name")
            if name:
                yield name, _coerce_args(fn.get("arguments"))
        return
    if "function" in obj:
        fn = obj["function"] or {}
        if fn.get("name"):
            yield fn["name"], _coerce_args(fn.get("arguments"))


async def _replay_trajectory_async(
    trajectory: str,
    server: str,
    cassette: str,
    upstream_cmd: str,
    upstream_args: list[str],
    upstream_env: dict[str, str] | None,
    upstream_cwd: str | None,
) -> dict:
    cfg = {
        "server": server,
        "cassette": cassette,
        "upstream": {
            "command": upstream_cmd,
            "args": upstream_args,
            "env": upstream_env or {},
            "cwd": upstream_cwd,
        },
    }
    rec = Recorder(cfg)
    await rec.start()
    stats = {"ok": 0, "errors": 0, "skipped": 0, "total": 0}
    try:
        upstream_tool_names = {t.name for t in rec.tools}
        for tool, args in _iter_trajectory(trajectory):
            stats["total"] += 1
            if tool not in upstream_tool_names:
                LOG.warning(
                    "skipping %s — not in upstream tool list", tool)
                stats["skipped"] += 1
                continue
            fwd = rec._make_forward(tool)  # noqa: SLF001 — internal use ok
            try:
                resp = await fwd(args)
                if (isinstance(resp, dict)
                        and resp.get("error") == "upstream_call_failed"):
                    stats["errors"] += 1
                else:
                    stats["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                LOG.exception("replay step failed: %s %s", tool, args)
                stats["errors"] += 1
                _append_jsonl(_errors_path(), {
                    "ts": _utc_now(),
                    "server": server,
                    "tool": tool,
                    "args": args,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    finally:
        await rec.stop()
    return stats


def replay_trajectory(
    trajectory: str,
    server: str,
    cassette: str,
    upstream_cmd: str,
    upstream_args: list[str] | None = None,
    upstream_env: dict[str, str] | None = None,
    upstream_cwd: str | None = None,
) -> dict:
    """Bootstrap a cassette by replaying every tool call in a
    trajectory against a real upstream. Returns counters."""
    logging.basicConfig(
        level=os.environ.get("REPLAY_RECORDER_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_replay_trajectory_async(
        trajectory=trajectory,
        server=server,
        cassette=cassette,
        upstream_cmd=upstream_cmd,
        upstream_args=list(upstream_args or []),
        upstream_env=upstream_env,
        upstream_cwd=upstream_cwd,
    ))


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_server()
