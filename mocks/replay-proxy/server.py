"""Tier-B replay-proxy MCP server.

Time-varying public APIs (stock quotes, search engines, browser
snapshots, weather, …) can't be backed by a stateful mock — their
"correct" answer drifts. They also can't be hit live during RL
training because they're non-deterministic and rate-limited. This
server is the third path: it replays *pre-recorded* responses keyed
by (tool, canonical args hash).

Behaviour:
  - At startup, read $REPLAY_PROXY_CONFIG (JSON). For each declared
    server+tool combo, register a FastMCP tool with the same name
    and parameter list as the real upstream tool. The tool body
    canonicalizes its args, hashes them, and looks them up in the
    associated cassette.
  - Cassette hit → return the recorded response.
  - Cassette miss → behaviour depends on $REPLAY_PROXY_MISS_POLICY:
        "error"       (default) — return a structured cassette_miss
                                   error.
        "null"        — return null.
        "passthrough" — reserved; currently equivalent to "error"
                        but logs intent to call upstream.
    In every case, the miss is appended to
    $REPLAY_PROXY_STATE_DIR/misses.jsonl so future recording runs
    can fill the gap.
  - All calls (hit + miss) are appended to
    $REPLAY_PROXY_STATE_DIR/calls.jsonl for audit.

Config schema (JSON):

    {
      "servers": [
        {
          "name": "yahoo-finance",
          "cassette": "/workspace/cassettes/yahoo-finance.jsonl",
          "tools": [
            {
              "name": "get_stock_quote",
              "description": "Get latest quote for a ticker.",
              "params": {
                  "ticker": {"type": "string", "required": true}
              }
            },
            {
              "name": "get_history",
              "params": {
                  "ticker": "string",
                  "period": "string"
              }
            }
          ]
        }
      ]
    }

Each `params` entry can be either a type string (`"string"`,
`"number"`, `"integer"`, `"boolean"`, `"array"`, `"object"`) — in
which case the parameter is optional — or a dict with `type` and
optional `required` + `default` keys.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from replay.cassette import (
    Cassette,
    args_hash,
    canonicalize_args,
    load_cassette,
)
from replay.transforms import TRANSFORMS


# ---------------------------------------------------------------------------
# Env & paths
# ---------------------------------------------------------------------------

DEFAULT_STATE_DIR = os.path.expanduser("~/.openclaw/replay_proxy")


def _state_dir() -> str:
    d = os.environ.get("REPLAY_PROXY_STATE_DIR", DEFAULT_STATE_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _misses_path() -> str:
    return os.path.join(_state_dir(), "misses.jsonl")


def _calls_path() -> str:
    return os.path.join(_state_dir(), "calls.jsonl")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


@contextlib.contextmanager
def _lock(name: str):
    path = os.path.join(_state_dir(), f".{name}.lock")
    fd = open(path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _append_jsonl(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False,
                           separators=(",", ":")))
        f.write("\n")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    path = os.environ.get("REPLAY_PROXY_CONFIG")
    if not path:
        return {"servers": []}
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"REPLAY_PROXY_CONFIG points to missing file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or "servers" not in cfg:
        raise ValueError("config must be {'servers': [...]}")
    return cfg


def _miss_policy() -> str:
    return os.environ.get("REPLAY_PROXY_MISS_POLICY", "error").lower()


# ---------------------------------------------------------------------------
# Param spec normalization
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string":  str,
    "number":  float,
    "integer": int,
    "int":     int,
    "boolean": bool,
    "bool":    bool,
    "array":   list,
    "list":    list,
    "object":  dict,
    "dict":    dict,
}


def _normalize_param_spec(raw: Any) -> dict:
    """Accept either a type string or a {type, required, default}
    dict; return a normalized dict."""
    if isinstance(raw, str):
        return {"type": raw, "required": False, "default": None}
    if isinstance(raw, dict):
        return {
            "type": raw.get("type", "string"),
            "required": bool(raw.get("required", False)),
            "default": raw.get("default", None),
        }
    return {"type": "string", "required": False, "default": None}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

# Global, populated at startup by _build_server(). Tool functions
# reference these by closure.
_CASSETTES: dict[str, Cassette] = {}        # server_name -> Cassette
_TOOLS: list[dict] = []                     # config-shaped tool dicts
_CONFIG: dict = {"servers": []}
# server_name -> alias_tool -> list of alias config dicts. Populated
# from configs/<name>.json's "tool_aliases" block. Each alias dict:
#   {alias_tool, canonical_tool, arg_map, response_transform}
_ALIASES: dict[str, dict[str, list[dict]]] = {}


def _make_tool_fn(server_name: str, tool_name: str,
                  param_specs: dict[str, dict]) -> Callable:
    """Build a closure that handles one (server, tool). The closure
    accepts **kwargs (FastMCP can introspect the wrapper's signature
    below). We canonicalize, hash, lookup, log."""

    required_keys = [k for k, s in param_specs.items() if s["required"]]

    def _impl(**kwargs: Any) -> Any:
        # Identity args = exactly what the caller passed (with None
        # dropped). We do NOT inject server-side defaults into the
        # hashed args — defaults are a presentation concern and
        # would force cassette authors to record every default
        # explicitly, which is brittle and surprises operators.
        # If a recorder wants a particular default baked in, they
        # can pass it explicitly when recording.
        args: dict[str, Any] = {k: v for k, v in kwargs.items()
                                if v is not None}

        # Unwrap a single top-level {"kwargs": {...}} envelope —
        # some agents pass args as one kwargs dict instead of
        # discrete keyword args. Both forms should be treated as
        # equivalent for required-arg checks and cassette lookup.
        if (len(args) == 1 and "kwargs" in args
                and isinstance(args["kwargs"], dict)):
            args = {k: v for k, v in args["kwargs"].items()
                    if v is not None}

        # Apply defaults only for *required-missing* check, so we
        # still surface a meaningful error if the caller omits a
        # required arg that has no default.
        effective = dict(args)
        for k, spec in param_specs.items():
            if k not in effective and spec["default"] is not None:
                effective[k] = spec["default"]
        missing = [k for k in required_keys if k not in effective]
        if missing:
            return {
                "error": "missing_required_args",
                "tool": tool_name,
                "server": server_name,
                "missing": missing,
            }

        canon = canonicalize_args(args)
        h = args_hash(canon)
        cas = _CASSETTES.get(server_name)
        entry = cas.lookup(tool_name, canon) if cas else None
        match_kind = "exact" if entry else "miss"
        transform_fn = None  # populated only by alias path
        # Loose-match fallback for opt-in read-only cassettes. If
        # strict lookup misses, try the longest-arg-subset match;
        # log the hit as "loose" so audit can distinguish.
        if (entry is None and cas is not None
                and getattr(cas, "loose_args_match", False)):
            entry = cas.loose_lookup(tool_name, canon)
            if entry is not None:
                match_kind = "loose"
        # Alias-tool fallback: still miss → check tool_aliases for this
        # server. If the called tool aliases another canonical tool,
        # rewrite args (arg_map) and look up under the canonical tool.
        # On hit, apply the named response_transform to reshape the
        # response back to the alias-tool's expected schema.
        if entry is None:
            aliases = _ALIASES.get(server_name, {}).get(tool_name, [])
            for alias in aliases:
                arg_map = alias.get("arg_map") or {}
                rewritten = {arg_map.get(k, k): v for k, v in args.items()}
                canon_r = canonicalize_args(rewritten)
                canonical_tool = alias.get("canonical_tool")
                if not canonical_tool:
                    continue
                cand = cas.lookup(canonical_tool, canon_r)
                if cand is None and cas.loose_args_match:
                    cand = cas.loose_lookup(canonical_tool, canon_r)
                if cand is not None:
                    entry = cand
                    match_kind = f"alias({canonical_tool})"
                    tname = alias.get("response_transform")
                    transform_fn = TRANSFORMS.get(tname) if tname else None
                    break

        with _lock("calls"):
            _append_jsonl(_calls_path(), {
                "ts": _now(),
                "server": server_name,
                "tool": tool_name,
                "args_hash": h,
                "args": canon,
                "result": match_kind,
            })

        if entry is not None:
            resp = entry.response
            if transform_fn is not None:
                try:
                    resp = transform_fn(resp, args)
                except Exception:
                    # Fall through to miss path if transform crashes.
                    entry = None
            if entry is not None:
                return resp

        # Miss path.
        with _lock("misses"):
            _append_jsonl(_misses_path(), {
                "ts": _now(),
                "server": server_name,
                "tool": tool_name,
                "args_hash": h,
                "args": canon,
                "policy": _miss_policy(),
            })

        policy = _miss_policy()
        if policy == "null":
            return None
        # "passthrough" is reserved; we surface the same structured
        # error so failures are loud during training, but the miss
        # log captures intent for an offline recorder.
        return {
            "error": "cassette_miss",
            "server": server_name,
            "tool": tool_name,
            "args_hash": h,
            "canonical_args": canon,
            "hint": ("no recorded response — re-run the recorder for "
                     "this (tool,args) pair, then restart the proxy"),
        }

    return _impl


def _wrap_with_signature(fn: Callable, tool_name: str,
                         param_specs: dict[str, dict]) -> Callable:
    """FastMCP introspects function signatures to build a JSON schema.
    We synthesize a function with the declared parameters by exec'ing
    a wrapper that forwards into `fn`."""
    import textwrap

    params: list[str] = []
    for name, spec in param_specs.items():
        py_type = _TYPE_MAP.get(spec["type"], str).__name__
        if spec["required"]:
            params.append(f"{name}: {py_type}")
        else:
            # Always default to None at the wrapper layer. The real
            # default lives in param_specs and is applied inside
            # _impl only for the required-args check — never for
            # hashing — so an omitted optional arg hashes the same
            # as it would have if the spec had no default at all.
            params.append(f"{name}: {py_type} | None = None")
    sig = ", ".join(params)
    src = textwrap.dedent(f"""
    def {tool_name.replace('-', '_')}({sig}):
        return _fn(**{{k: v for k, v in locals().items()}})
    """).strip()
    ns: dict = {"_fn": fn}
    try:
        exec(src, ns)
    except SyntaxError:
        # Tool name not a valid Python identifier — fall back to
        # generic **kwargs signature. FastMCP will register it but
        # without a typed schema.
        def _generic(**kwargs: Any) -> Any:
            return fn(**kwargs)
        _generic.__name__ = tool_name
        return _generic
    wrapped = ns[tool_name.replace("-", "_")]
    wrapped.__name__ = tool_name.replace("-", "_")
    return wrapped


def _build_server() -> FastMCP:
    cfg = _load_config()
    global _CONFIG
    _CONFIG = cfg

    mcp = FastMCP("replay-proxy")

    for srv in cfg.get("servers", []):
        name = srv["name"]
        cassette_path = srv.get("cassette")
        # Per-server opt-in: read-only search cassettes (arxiv,
        # brave-search, …) set this to true so that extra refinement
        # args (max_results, sort_by, …) don't force a cassette_miss.
        # State-mutating cassettes leave it false — every unrecorded
        # arg should be loud.
        loose = bool(srv.get("loose_args_match", False))
        cas = (load_cassette(cassette_path, server=name,
                             loose_args_match=loose)
               if cassette_path else
               Cassette(server=name, loose_args_match=loose))
        _CASSETTES[name] = cas
        # tool_aliases: list of {alias_tool, canonical_tool, arg_map,
        # response_transform}. Build alias_tool -> list-of-aliases.
        aliases_by_tool: dict[str, list[dict]] = {}
        for alias in (srv.get("tool_aliases") or []):
            at = alias.get("alias_tool")
            if at:
                aliases_by_tool.setdefault(at, []).append(alias)
        _ALIASES[name] = aliases_by_tool

        for tool in srv.get("tools", []):
            tname = tool["name"]
            raw_params = tool.get("params") or {}
            specs = {k: _normalize_param_spec(v)
                     for k, v in raw_params.items()}
            description = tool.get("description") or (
                f"[replay-proxy] {name}.{tname} — replays recorded "
                f"responses from cassette {cassette_path}")
            impl = _make_tool_fn(name, tname, specs)
            wrapped = _wrap_with_signature(impl, tname, specs)
            wrapped.__doc__ = description
            mcp.tool(name=tname, description=description)(wrapped)
            _TOOLS.append({
                "server": name, "tool": tname,
                "params": specs, "cassette": cassette_path,
            })

    # Debug tool (always registered) ---------------------------------------

    @_debug_tool(name="mock_debug_state")
    def mock_debug_state() -> dict:
        """Return loaded cassettes, declared tools, and miss/call
        counters. Not part of any upstream server's tool surface."""
        miss_count = 0
        call_count = 0
        if os.path.exists(_misses_path()):
            with open(_misses_path(), "r", encoding="utf-8") as f:
                miss_count = sum(1 for _ in f if _.strip())
        if os.path.exists(_calls_path()):
            with open(_calls_path(), "r", encoding="utf-8") as f:
                call_count = sum(1 for _ in f if _.strip())
        cassettes = []
        for srv_name, cas in _CASSETTES.items():
            cassettes.append({
                "server": srv_name,
                "path": cas.path,
                "entries": len(cas),
                "tools": cas.tools(),
            })
        return {
            "config": _CONFIG,
            "miss_policy": _miss_policy(),
            "state_dir": _state_dir(),
            "cassettes": cassettes,
            "registered_tools": _TOOLS,
            "totals": {"calls": call_count, "misses": miss_count},
        }

    return mcp


# Entrypoint -----------------------------------------------------------------

mcp = _build_server()

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



if __name__ == "__main__":
    mcp.run()
