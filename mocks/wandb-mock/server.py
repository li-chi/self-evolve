"""Weights & Biases mock MCP server.

Mirrors the tool surface of `wandb-mcp-server`
(Toolathlon source: github.com/lockon-n/wandb-mcp-server,
upstream: github.com/wandb/wandb-mcp-server). Toolathlon invokes it as
`uvx --from wandb-mcp-server wandb_mcp_server`. This mock is a
drop-in replacement during RL training: tool names and parameter
shapes match the official server exactly.

Six tools exposed (verbatim names from the official `@mcp.tool`
decorators in src/wandb_mcp_server/server.py):

  query_wandb_tool             — execute a GraphQL query against the
                                 W&B Models API (projects, runs, sweeps,
                                 artifacts, etc.). This is the workhorse
                                 used by all three Toolathlon wandb tasks
                                 (experiments-recordings, wandb-best-score,
                                 wandb-shortest-length).
  query_wandb_entity_projects  — list projects for a W&B entity.
  query_weave_traces_tool      — query Weave LLM traces (not used by the
                                 3 target tasks; returns an empty list).
  count_weave_traces_tool      — count Weave traces (returns 0/0).
  query_wandb_support_bot      — proxy to wandbot (returns a canned reply).
  create_wandb_report_tool     — create a W&B Report (returns a fake URL,
                                 persists the report markdown in state).

Plus two mock-only debug tools (used by per-task preprocessing and
verification, never present on the real server):

  mock_debug_state             — return the full state dict.
  mock_debug_seed              — replace the state dict (or merge in
                                 projects/runs/sweeps fixture data).

The mock includes a small GraphQL evaluator that handles the subset of
queries actually used by W&B clients: the standard
`project(name, entityName) { runs/run/sweeps/sweep/... }` shape with
the `edges { node { ... } } pageInfo { endCursor hasNextPage }`
pagination pattern, plus `viewer { entity username ... }`. Filters
provided as JSON strings (W&B syntax: `{"state":"finished"}`,
`{"displayName":{"$eq":"..."}}`, `{"summary_metrics.acc":{"$gt":0.9}}`)
and the `order: "+/-field"` string are honoured.

State (single JSON file at $WANDB_MOCK_STATE_DIR/state.json,
default ~/.openclaw/wandb_mock/state.json):

  state = {
    "viewer": {"id", "username", "entity", "teams": [...]},
    "projects": {
      "<entity>/<name>": {
        "id", "name", "entity", "entityName", "description",
        "visibility", "createdAt", "updatedAt", "tags", "runCount"
      }
    },
    "runs": {
      "<entity>/<project>/<run_id>": {
        "id", "name" (run id), "displayName", "state",
        "createdAt", "updatedAt", "heartbeatAt",
        "config" (JSON string), "summaryMetrics" (JSON string),
        "historyKeys" ([str, ...]),
        "history" ([{"_step": N, "<metric>": val, ...}, ...]),
        "tags", "sweep" ("<sweep_id>" or None),
        "user": {"username", "name"},
        "entity", "project", "historyLineCount"
      }
    },
    "sweeps": {
      "<entity>/<project>/<sweep_id>": {
        "id", "name" (sweep_id), "displayName", "state",
        "createdAt", "method", "config" (JSON string),
        "bestLoss", "runs": ["<run_id>", ...]
      }
    },
    "reports": {"<id>": {...}},
    "next_id": {"report": N, "run": N, "sweep": N, "project": N},
    "calls": [{"op", "ts", ...}]
  }

Per-task preprocessing should reset $WANDB_MOCK_STATE_DIR between
rollouts and write a seed file pointed to by $WANDB_MOCK_SEED_PATH.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import math
import os
import re
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing (lock + load + save + record). Same pattern as the other
# mocks in this directory.
# ---------------------------------------------------------------------------


def _state_path() -> str:
    state_dir = os.environ.get(
        "WANDB_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/wandb_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _empty_state() -> dict:
    return {
        "viewer": {
            "id": "mock-user-id",
            "username": "mock-user",
            "name": "Mock User",
            "email": "mock@wandb.local",
            "entity": "mock-user",
            "teams": ["mock-team"],
        },
        "projects": {},
        "runs": {},
        "sweeps": {},
        "reports": {},
        "next_id": {"report": 1, "run": 1, "sweep": 1, "project": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("WANDB_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge seed onto an empty skeleton so callers can ship
            # partial fixtures (e.g. just `runs`).
            base = _empty_state()
            base.update(data)
            for k in ("projects", "runs", "sweeps", "reports"):
                base.setdefault(k, {})
            base.setdefault("calls", [])
            return base
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    lock_path = _state_path() + ".lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kwargs) -> None:
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# State accessors: look up projects / runs / sweeps using the W&B
# convention `<entity>/<project>` and `<entity>/<project>/<id>`.
# Seed JSON may omit the entity prefix on run/sweep keys when it is
# obvious from context — we resolve both forms.
# ---------------------------------------------------------------------------


def _project_key(entity: str, project: str) -> str:
    return f"{entity}/{project}"


def _run_key(entity: str, project: str, run_id: str) -> str:
    return f"{entity}/{project}/{run_id}"


def _find_project(state: dict, entity: str | None, project: str) -> dict | None:
    if entity:
        p = state["projects"].get(_project_key(entity, project))
        if p:
            return p
    # fall back to first project with that name
    for p in state["projects"].values():
        if p.get("name") == project and (not entity or p.get("entity") == entity):
            return p
    return None


def _project_runs(state: dict, entity: str, project: str) -> list[dict]:
    prefix = f"{entity}/{project}/"
    out: list[dict] = []
    for key, run in state["runs"].items():
        if key.startswith(prefix):
            out.append(run)
            continue
        # tolerate seed entries keyed by run-id only when entity+project
        # are encoded on the run itself.
        if run.get("entity") == entity and run.get("project") == project:
            out.append(run)
    return out


def _find_run(
    state: dict, entity: str, project: str, run_id_or_name: str
) -> dict | None:
    key = _run_key(entity, project, run_id_or_name)
    if key in state["runs"]:
        return state["runs"][key]
    for run in _project_runs(state, entity, project):
        if run.get("name") == run_id_or_name or run.get("id") == run_id_or_name:
            return run
    return None


def _project_sweeps(state: dict, entity: str, project: str) -> list[dict]:
    prefix = f"{entity}/{project}/"
    out: list[dict] = []
    for key, sw in state["sweeps"].items():
        if key.startswith(prefix):
            out.append(sw)
            continue
        if sw.get("entity") == entity and sw.get("project") == project:
            out.append(sw)
    return out


def _find_sweep(
    state: dict, entity: str, project: str, sweep_id: str
) -> dict | None:
    key = f"{entity}/{project}/{sweep_id}"
    if key in state["sweeps"]:
        return state["sweeps"][key]
    for sw in _project_sweeps(state, entity, project):
        if sw.get("name") == sweep_id or sw.get("id") == sweep_id:
            return sw
    return None


# ---------------------------------------------------------------------------
# Filter / sort: W&B's runs(filters: JSONString, order: String) DSL.
#
# Filter syntax in real W&B:
#   {"state": "finished"}                          -- equality
#   {"displayName": {"$eq": "my-run"}}             -- explicit operator
#   {"summary_metrics.acc": {"$gt": 0.9}}          -- nested path + op
#   {"$or": [{...}, {...}]}                        -- logical
#   {"config.lr": {"$in": [0.1, 0.01]}}            -- list op
# Order syntax:
#   "+createdAt"  ascending     "-createdAt"  descending
#   "+summary_metrics.loss"     "-summary_metrics.acc"
#   "+config.batch_size"        ...
# ---------------------------------------------------------------------------


_NUM_OPS = {
    "$eq": lambda a, b: a == b,
    "$ne": lambda a, b: a != b,
    "$gt": lambda a, b: _as_num(a) > _as_num(b),
    "$gte": lambda a, b: _as_num(a) >= _as_num(b),
    "$lt": lambda a, b: _as_num(a) < _as_num(b),
    "$lte": lambda a, b: _as_num(a) <= _as_num(b),
    "$in": lambda a, b: a in b if isinstance(b, list) else False,
    "$nin": lambda a, b: a not in b if isinstance(b, list) else True,
    "$contains": lambda a, b: isinstance(a, str) and b in a,
    "$regex": lambda a, b: isinstance(a, str)
    and re.search(b, a) is not None,
}


def _as_num(v: Any) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _get_path(run: dict, path: str) -> Any:
    """Resolve a W&B filter/sort path against a run dict.

    Paths look like:
      state, displayName, createdAt, name, tags
      config.lr           -> json.loads(run["config"])["lr"]
      summary_metrics.acc -> json.loads(run["summaryMetrics"])["acc"]
    """
    if "." not in path:
        # top-level field
        if path == "summary_metrics":
            return _parse_json(run.get("summaryMetrics"))
        if path == "config":
            return _parse_json(run.get("config"))
        return run.get(path)
    head, _, rest = path.partition(".")
    if head in ("summary_metrics", "summaryMetrics"):
        src = _parse_json(run.get("summaryMetrics")) or {}
    elif head == "config":
        src = _parse_json(run.get("config")) or {}
    else:
        src = run.get(head) or {}
    cur: Any = src
    for part in rest.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _parse_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return None
    return v


def _matches_filter(run: dict, flt: Any) -> bool:
    if flt is None or flt == {} or flt == "":
        return True
    if isinstance(flt, str):
        try:
            flt = json.loads(flt)
        except (TypeError, ValueError):
            return True
    if not isinstance(flt, dict):
        return True
    for key, cond in flt.items():
        if key == "$and":
            if not all(_matches_filter(run, c) for c in cond):
                return False
            continue
        if key == "$or":
            if not any(_matches_filter(run, c) for c in cond):
                return False
            continue
        if key == "$not":
            if _matches_filter(run, cond):
                return False
            continue
        actual = _get_path(run, key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                fn = _NUM_OPS.get(op)
                if fn is None:
                    continue
                try:
                    if not fn(actual, operand):
                        return False
                except Exception:
                    return False
        else:
            if actual != cond:
                return False
    return True


def _sort_key(run: dict, field: str) -> Any:
    val = _get_path(run, field)
    if val is None:
        return (1, "")
    if isinstance(val, (int, float)):
        if isinstance(val, float) and math.isnan(val):
            return (1, 0.0)
        return (0, float(val))
    return (0, str(val))


def _apply_order(runs: list[dict], order: str | None) -> list[dict]:
    if not order:
        # W&B default sort is by -createdAt
        return sorted(runs, key=lambda r: r.get("createdAt") or "", reverse=True)
    direction = "-"
    field = order
    if order and order[0] in "+-":
        direction = order[0]
        field = order[1:]
    return sorted(runs, key=lambda r: _sort_key(r, field), reverse=(direction == "-"))


# ---------------------------------------------------------------------------
# GraphQL evaluator. We don't run a real GraphQL parser — we walk the
# query string and extract field-selection regions with their arguments.
# This is enough for the queries the W&B Python client and the
# wandb-mcp-server query examples actually emit: `project(name,
# entityName)` containing `runs(...)`, `run(name:)`, `sweeps(...)`,
# `sweep(...)`, plus top-level `viewer`.
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(
    r'(?P<comment>\#[^\n]*)'
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r'|(?P<punct>[{}()\[\]:,!])'
    r'|(?P<dots>\.\.\.)'
    r'|(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
    r'|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r'|(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)'
    r'|(?P<ws>\s+)'
)


def _tokenize(s: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(s):
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        out.append((kind, m.group()))
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def eat(self) -> tuple[str, str]:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        tok = self.eat()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise ValueError(f"expected {kind} {value!r}, got {tok!r}")
        return tok

    def at(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        if not tok:
            return False
        if tok[0] != kind:
            return False
        if value is not None and tok[1] != value:
            return False
        return True


def _parse_value(p: _Parser, variables: dict) -> Any:
    tok = p.eat()
    kind, value = tok
    if kind == "var":
        return variables.get(value[1:])
    if kind == "string":
        return json.loads(value)
    if kind == "number":
        return float(value) if "." in value or "e" in value or "E" in value else int(value)
    if kind == "name":
        if value == "true":
            return True
        if value == "false":
            return False
        if value == "null":
            return None
        return value  # enum-like
    if kind == "punct" and value == "{":
        # object
        obj: dict[str, Any] = {}
        while p.peek() and p.peek()[1] != "}":
            name = p.expect("name")[1]
            p.expect("punct", ":")
            obj[name] = _parse_value(p, variables)
            if p.at("punct", ","):
                p.eat()
        p.expect("punct", "}")
        return obj
    if kind == "punct" and value == "[":
        lst: list[Any] = []
        while p.peek() and p.peek()[1] != "]":
            lst.append(_parse_value(p, variables))
            if p.at("punct", ","):
                p.eat()
        p.expect("punct", "]")
        return lst
    raise ValueError(f"unexpected value token {tok!r}")


def _parse_arguments(p: _Parser, variables: dict) -> dict:
    args: dict[str, Any] = {}
    if not p.at("punct", "("):
        return args
    p.eat()  # (
    while p.peek() and p.peek()[1] != ")":
        name = p.expect("name")[1]
        p.expect("punct", ":")
        args[name] = _parse_value(p, variables)
        if p.at("punct", ","):
            p.eat()
    p.expect("punct", ")")
    return args


def _parse_selection_set(p: _Parser, variables: dict) -> list[dict]:
    """Returns a list of {name, alias, args, selections}."""
    p.expect("punct", "{")
    selections: list[dict] = []
    while p.peek() and p.peek()[1] != "}":
        if p.at("dots"):
            # ignore inline fragments / spreads — selection through them
            p.eat()
            # could be `... on TypeName { ... }` or `...FragName`
            if p.at("name") and p.peek()[1] == "on":
                p.eat()  # on
                p.eat()  # TypeName
                if p.at("punct", "{"):
                    selections.extend(_parse_selection_set(p, variables))
                continue
            if p.at("name"):
                p.eat()  # frag name (ignored)
                continue
            continue
        name_tok = p.expect("name")
        name = name_tok[1]
        alias = None
        if p.at("punct", ":"):
            p.eat()
            alias = name
            name = p.expect("name")[1]
        args = _parse_arguments(p, variables)
        subs: list[dict] = []
        if p.at("punct", "{"):
            subs = _parse_selection_set(p, variables)
        selections.append(
            {"name": name, "alias": alias or name, "args": args, "selections": subs}
        )
    p.expect("punct", "}")
    return selections


def _parse_query(query: str, variables: dict | None) -> list[dict]:
    tokens = _tokenize(query)
    p = _Parser(tokens)
    vars_ = dict(variables or {})
    # Optional `query Name(...) { ... }` prefix.
    if p.at("name") and p.peek()[1] in ("query", "mutation"):
        p.eat()
        if p.at("name"):
            p.eat()  # name
        # variable definitions
        if p.at("punct", "("):
            p.eat()
            while p.peek() and p.peek()[1] != ")":
                if p.at("var"):
                    p.eat()
                if p.at("punct", ":"):
                    p.eat()
                # type (Name [!] or [Name!]! etc.) — just skip until ',' or ')'
                while p.peek() and p.peek()[1] not in (",", ")", "="):
                    p.eat()
                if p.at("punct", "="):
                    p.eat()
                    _parse_value(p, vars_)  # default
                if p.at("punct", ","):
                    p.eat()
            p.expect("punct", ")")
    return _parse_selection_set(p, vars_)


# ---------------------------------------------------------------------------
# Field resolvers. Map a parsed selection tree onto the in-memory state.
# We return the GraphQL `{ "data": {...} }` envelope; missing fields are
# emitted as `None`. Scalar fields not in our schema fall back to a
# best-effort lookup on the resource dict (so seed fixtures can stuff
# arbitrary extras into a run / sweep / project and have them returned).
# ---------------------------------------------------------------------------


def _select_scalar(obj: dict, sel: dict) -> Any:
    name = sel["name"]
    if name == "summaryMetrics":
        v = obj.get("summaryMetrics")
        return v if isinstance(v, str) else json.dumps(v or {})
    if name == "config":
        v = obj.get("config")
        return v if isinstance(v, str) else json.dumps(v or {})
    if name == "metadata":
        v = obj.get("metadata")
        return v if isinstance(v, str) else json.dumps(v or {})
    if name == "historyKeys":
        keys = obj.get("historyKeys")
        if keys is None:
            history = obj.get("history") or []
            seen: list[str] = []
            for row in history:
                for k in row.keys():
                    if k not in seen:
                        seen.append(k)
            keys = seen
        return list(keys)
    if name == "historyLineCount":
        if obj.get("historyLineCount") is not None:
            return obj["historyLineCount"]
        return len(obj.get("history") or [])
    if name == "sampledHistory":
        # sampledHistory(specs: [JSONString!]!) -> [[JSONString]]
        specs = sel["args"].get("specs") or []
        history = obj.get("history") or []
        out: list[list[str]] = []
        for spec in specs:
            spec_dict = _parse_json(spec) if isinstance(spec, str) else spec
            keys = (spec_dict or {}).get("keys") or []
            rows: list[str] = []
            for row in history:
                if not keys or any(k in row for k in keys):
                    kept = {k: row[k] for k in row if (not keys or k in keys or k == "_step")}
                    rows.append(json.dumps(kept))
            out.append(rows)
        return out
    if name == "history":
        # The deprecated `history` field — returns rows as JSON strings.
        return [json.dumps(r) for r in (obj.get("history") or [])]
    return obj.get(name)


def _resolve_field(state: dict, sel: dict, parent: Any = None) -> Any:
    name = sel["name"]
    args = sel["args"]
    if name == "viewer":
        return _resolve_object(state, sel["selections"], state["viewer"])
    if name == "project":
        proj = _find_project(state, args.get("entityName"), args.get("name"))
        if not proj:
            return None
        return _resolve_object(
            state,
            sel["selections"],
            proj,
            ctx={
                "entity": proj.get("entity") or args.get("entityName"),
                "project": proj.get("name") or args.get("name"),
            },
        )
    if name == "projects":
        # used by some clients: projects(entityName:...) { edges { node { ... } } }
        entity = args.get("entityName") or args.get("entity")
        projs = [
            p for p in state["projects"].values() if not entity or p.get("entity") == entity
        ]
        return _resolve_connection(state, sel, projs)
    if name in ("upsertBucket", "createRun", "createAnonymousRun"):
        # Mutation: create or update a run ("bucket" in W&B's internal naming).
        # Accept both flat args (our default) and the `input: {}` wrapper that
        # the real W&B GQL schema uses, since different LLMs emit both forms.
        _a = args.get("input") if isinstance(args.get("input"), dict) else args
        entity = (_a.get("entity") or _a.get("entityName")
                  or args.get("entity") or args.get("entityName")
                  or state["viewer"]["entity"])
        project = (_a.get("project") or _a.get("projectName")
                   or args.get("project") or args.get("projectName") or "")
        run_name = (_a.get("name") or _a.get("id")
                    or args.get("name") or args.get("id") or _new_id())
        display_name = _a.get("displayName") or args.get("displayName") or run_name
        now = _now()
        rkey = _run_key(entity, project, run_name)
        existing = state["runs"].get(rkey) or {}
        run = {
            **existing,
            "id": run_name,
            "name": run_name,
            "run_id": run_name,
            "displayName": display_name,
            "state": (_a.get("state") or args.get("state")
                      or existing.get("state") or "finished"),
            "createdAt": existing.get("createdAt") or now,
            "updatedAt": now,
            "heartbeatAt": now,
            "config": (_a.get("config") or args.get("config")
                       or existing.get("config") or "{}"),
            "summaryMetrics": (_a.get("summaryMetrics") or args.get("summaryMetrics")
                               or existing.get("summaryMetrics") or "{}"),
            "historyKeys": existing.get("historyKeys") or [],
            "history": existing.get("history") or [],
            "historyLineCount": existing.get("historyLineCount") or 0,
            "tags": (_a.get("tags") or args.get("tags")
                     or existing.get("tags") or []),
            "sweep": _a.get("sweep") or args.get("sweep"),
            "user": {"username": entity, "name": entity},
            "entity": entity,
            "project": project,
        }
        state["runs"][rkey] = run
        # Ensure project exists and keep runCount accurate.
        pkey = _project_key(entity, project)
        if pkey not in state["projects"]:
            nid = state["next_id"].get("project", 1)
            state["projects"][pkey] = {
                "id": str(nid),
                "name": project,
                "entity": entity,
                "entityName": entity,
                "description": "",
                "visibility": "private",
                "createdAt": now,
                "updatedAt": now,
                "tags": [],
                "runCount": 0,
            }
            state["next_id"]["project"] = nid + 1
        state["projects"][pkey]["runCount"] = sum(
            1 for r in state["runs"].values()
            if r.get("entity") == entity and r.get("project") == project
        )
        bucket_ctx = {"entity": entity, "project": project}
        if sel["selections"]:
            return _resolve_object(
                state, sel["selections"],
                {"bucket": run, "inserted": not bool(existing)},
                ctx=bucket_ctx,
            )
        return {"bucket": {"id": run_name, "name": run_name,
                           "displayName": display_name}}
    return None


def _resolve_object(
    state: dict, selections: list[dict], obj: dict, ctx: dict | None = None
) -> dict:
    out: dict[str, Any] = {}
    ctx = ctx or {}
    for sel in selections:
        n = sel["name"]
        if n == "runs":
            runs = _project_runs(state, ctx.get("entity", ""), ctx.get("project", ""))
            runs = [r for r in runs if _matches_filter(r, sel["args"].get("filters"))]
            runs = _apply_order(runs, sel["args"].get("order"))
            out[sel["alias"]] = _resolve_connection(
                state, sel, runs, ctx={**ctx, "kind": "run"}
            )
        elif n == "run":
            run_name = sel["args"].get("name")
            run = _find_run(state, ctx.get("entity", ""), ctx.get("project", ""), run_name)
            out[sel["alias"]] = (
                _resolve_object(state, sel["selections"], run, ctx=ctx) if run else None
            )
        elif n == "sweeps":
            sweeps = _project_sweeps(state, ctx.get("entity", ""), ctx.get("project", ""))
            out[sel["alias"]] = _resolve_connection(
                state, sel, sweeps, ctx={**ctx, "kind": "sweep"}
            )
        elif n == "sweep":
            sw_name = sel["args"].get("sweepName") or sel["args"].get("name")
            sw = _find_sweep(state, ctx.get("entity", ""), ctx.get("project", ""), sw_name)
            out[sel["alias"]] = (
                _resolve_object(state, sel["selections"], sw, ctx=ctx) if sw else None
            )
        elif n == "artifact":
            out[sel["alias"]] = None  # not modelled
        elif n == "user":
            user = obj.get("user") or state["viewer"]
            out[sel["alias"]] = _resolve_object(state, sel["selections"], user)
        elif n == "tags":
            out[sel["alias"]] = obj.get("tags") or []
        elif sel["selections"]:
            sub = obj.get(n)
            if isinstance(sub, dict):
                out[sel["alias"]] = _resolve_object(state, sel["selections"], sub, ctx=ctx)
            elif isinstance(sub, list):
                out[sel["alias"]] = [
                    _resolve_object(state, sel["selections"], x, ctx=ctx)
                    if isinstance(x, dict) else x
                    for x in sub
                ]
            else:
                out[sel["alias"]] = sub
        else:
            out[sel["alias"]] = _select_scalar(obj, sel)
    return out


def _resolve_connection(
    state: dict, sel: dict, items: list[dict], ctx: dict | None = None
) -> dict:
    """Render the W&B `edges { node { } } pageInfo {}` connection pattern."""
    args = sel["args"]
    first = int(args.get("first") or args.get("limit") or len(items) or 0)
    after = args.get("after")
    start = 0
    if after:
        for i, it in enumerate(items):
            cursor = it.get("id") or it.get("name") or str(i)
            if cursor == after:
                start = i + 1
                break
    page = items[start : start + first] if first else items[start:]
    has_next = start + len(page) < len(items)
    end_cursor = ""
    if page:
        last = page[-1]
        end_cursor = last.get("id") or last.get("name") or str(start + len(page) - 1)
    out: dict[str, Any] = {}
    # Walk the selection set to render edges / pageInfo / totalCount / count.
    for sub in sel["selections"]:
        n = sub["name"]
        if n == "edges":
            edges = []
            for it in page:
                edge: dict[str, Any] = {}
                for esub in sub["selections"]:
                    if esub["name"] == "node":
                        edge[esub["alias"]] = _resolve_object(
                            state, esub["selections"], it, ctx=ctx
                        )
                    elif esub["name"] == "cursor":
                        edge[esub["alias"]] = it.get("id") or it.get("name") or ""
                edges.append(edge)
            out[sub["alias"]] = edges
        elif n == "pageInfo":
            pi: dict[str, Any] = {}
            for esub in sub["selections"]:
                en = esub["name"]
                if en == "hasNextPage":
                    pi[esub["alias"]] = has_next
                elif en == "endCursor":
                    pi[esub["alias"]] = end_cursor
                elif en == "hasPreviousPage":
                    pi[esub["alias"]] = start > 0
                elif en == "startCursor":
                    pi[esub["alias"]] = (
                        page[0].get("id") or page[0].get("name") or "" if page else ""
                    )
                else:
                    pi[esub["alias"]] = None
            out[sub["alias"]] = pi
        elif n in ("totalCount", "count"):
            out[sub["alias"]] = len(items)
        else:
            out[sub["alias"]] = None
    return out


def _execute_graphql(
    state: dict, query: str, variables: dict | None
) -> dict:
    try:
        selections = _parse_query(query, variables)
    except Exception as e:
        return {"errors": [{"message": f"parse error: {e}"}], "data": None}
    data: dict[str, Any] = {}
    for sel in selections:
        try:
            data[sel["alias"]] = _resolve_field(state, sel)
        except Exception as e:
            return {
                "errors": [{"message": f"resolver error on {sel['name']}: {e}"}],
                "data": data or None,
            }
    return {"data": data}


# ---------------------------------------------------------------------------
# MCP server + tools. Names match wandb-mcp-server exactly.
# ---------------------------------------------------------------------------


mcp = FastMCP("wandb-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



@mcp.tool(name="query_wandb_tool")
def query_wandb_tool(
    query: str,
    variables: dict | None = None,
    max_items: int = 100,
    items_per_page: int = 20,
) -> dict:
    """W&B `query_wandb_tool`: execute an arbitrary GraphQL query against
    the Weights & Biases Models API and return the aggregated response.

    Supports the connection pattern that the real server requires:
    `runs(first, after, filters: JSONString, order: String)` with
    `edges { node { ... } } pageInfo { endCursor hasNextPage }`. Filters
    accept JSON strings using `{"$eq", "$gt", "$lt", "$in", "$or", ...}`.
    The `summaryMetrics` and `config` fields are returned as JSON strings
    to match the real schema. `sampledHistory(specs: [...])` returns the
    metric rows logged via `wandb.log({...})` for each spec.
    """
    with _lock():
        s = _load_state()
        result = _execute_graphql(s, query, variables or {})
        _record(
            s,
            "query_wandb_tool",
            variables=variables,
            max_items=max_items,
            items_per_page=items_per_page,
            ok="errors" not in result,
        )
        _save_state(s)
        return result


@mcp.tool(name="query_wandb_entity_projects")
def query_wandb_entity_projects(entity: str | None = None) -> dict:
    """W&B `query_wandb_entity_projects`: list projects for an entity
    (username or team name). When `entity` is None, returns projects for
    the viewer + teams. Response shape matches the real tool:
    `{entity: [{name, entity, description, visibility, created_at,
    updated_at, tags}, ...]}`.
    """
    with _lock():
        s = _load_state()
        if entity is None:
            entities = [s["viewer"]["entity"]] + list(s["viewer"].get("teams", []))
        else:
            entities = [entity]
        out: dict[str, list[dict]] = {}
        for ent in entities:
            projs = [p for p in s["projects"].values() if p.get("entity") == ent]
            out[ent] = [
                {
                    "name": p.get("name"),
                    "entity": p.get("entity"),
                    "description": p.get("description"),
                    "visibility": p.get("visibility", "private"),
                    "created_at": p.get("createdAt"),
                    "updated_at": p.get("updatedAt"),
                    "tags": p.get("tags", []),
                }
                for p in projs
            ]
        _record(s, "query_wandb_entity_projects", entity=entity)
        _save_state(s)
        return out


@mcp.tool(name="query_weave_traces_tool")
def query_weave_traces_tool(
    entity_name: str,
    project_name: str,
    filters: dict | None = None,
    sort_by: str = "started_at",
    sort_direction: str = "desc",
    limit: int = 10_000_000,
    include_costs: bool = True,
    include_feedback: bool = True,
    columns: list | None = None,
    expand_columns: list | None = None,
    truncate_length: int = 200,
    return_full_data: bool = False,
    metadata_only: bool = False,
) -> str:
    """W&B `query_weave_traces_tool`: query Weave LLM traces. Mock
    returns an empty result set serialized as a JSON string (matches the
    real tool, which also returns `str`). No Toolathlon wandb task uses
    Weave traces, so this is a stub that records the call for the
    verifier."""
    with _lock():
        s = _load_state()
        _record(
            s,
            "query_weave_traces_tool",
            entity_name=entity_name,
            project_name=project_name,
            filters=filters,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=limit,
            metadata_only=metadata_only,
        )
        _save_state(s)
        return json.dumps(
            {
                "traces": [],
                "total_count": 0,
                "metadata": {
                    "entity": entity_name,
                    "project": project_name,
                    "filters_applied": filters or {},
                    "sort_by": sort_by,
                    "sort_direction": sort_direction,
                    "returned": 0,
                },
            }
        )


@mcp.tool(name="count_weave_traces_tool")
def count_weave_traces_tool(
    entity_name: str,
    project_name: str,
    filters: dict | None = None,
) -> str:
    """W&B `count_weave_traces_tool`: count Weave traces (and root
    traces) matching `filters`. Mock returns 0/0; real shape is
    `{"total_count": int, "root_traces_count": int}`."""
    with _lock():
        s = _load_state()
        _record(
            s,
            "count_weave_traces_tool",
            entity_name=entity_name,
            project_name=project_name,
            filters=filters,
        )
        _save_state(s)
        return json.dumps({"total_count": 0, "root_traces_count": 0})


@mcp.tool(name="query_wandb_support_bot")
def query_wandb_support_bot(question: str) -> dict:
    """W&B `query_wandb_support_bot`: ask wandbot a free-form question
    about Weights & Biases. Mock returns a canned answer and records the
    question."""
    with _lock():
        s = _load_state()
        _record(s, "query_wandb_support_bot", question=question)
        _save_state(s)
        return {
            "answer": (
                "[mock wandbot] This is a stubbed support response. The "
                "wandb-mock server does not call the real wandbot API."
            ),
            "question": question,
            "sources": [],
        }


@mcp.tool(name="create_wandb_report_tool")
def create_wandb_report_tool(
    entity_name: str,
    project_name: str,
    title: str,
    description: str | None = None,
    markdown_report_text: str = "",
    plots_html: dict | str | None = None,
) -> str:
    """W&B `create_wandb_report_tool`: create a W&B Report with markdown
    text and HTML-rendered visualizations. Mock persists the report in
    state and returns a fake URL string (matches the real tool's
    `"The report was saved here: <url>"` return shape)."""
    with _lock():
        s = _load_state()
        rid = f"VmlldzoxMDk0{s['next_id']['report']:06d}"
        s["next_id"]["report"] += 1
        url = (
            f"https://wandb.ai/{entity_name}/{project_name}/reports/"
            f"{title.replace(' ', '-')}--{rid}"
        )
        s["reports"][rid] = {
            "id": rid,
            "entity": entity_name,
            "project": project_name,
            "title": title,
            "description": description,
            "markdown": markdown_report_text,
            "plots_html": plots_html,
            "url": url,
            "createdAt": _now(),
        }
        _record(
            s,
            "create_wandb_report_tool",
            report_id=rid,
            entity_name=entity_name,
            project_name=project_name,
            title=title,
        )
        _save_state(s)
        return f"The report was saved here: {url}"


# ---------------------------------------------------------------------------
# Mock-only debug helpers. Not exposed by the real wandb-mcp-server.
# Used by per-task preprocessing (seed fixtures) and verifier scripts.
# ---------------------------------------------------------------------------


@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state dict. Used by the verifier
    to inspect tool-call history and final fixture contents."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(
    state: dict | None = None,
    projects: dict | None = None,
    runs: dict | None = None,
    sweeps: dict | None = None,
    viewer: dict | None = None,
    replace: bool = False,
) -> dict:
    """Mock-only: seed fixtures into the state. If `replace` is True the
    state is replaced wholesale by `state` (which must be a full state
    dict). Otherwise merges `projects`/`runs`/`sweeps`/`viewer` into the
    existing state in-place. Keys follow the same `<entity>/<name>`
    convention as the live state."""
    with _lock():
        if replace and state is not None:
            s = state
            s.setdefault("calls", [])
            for k in ("projects", "runs", "sweeps", "reports"):
                s.setdefault(k, {})
            s.setdefault("next_id", {"report": 1, "run": 1, "sweep": 1, "project": 1})
            s.setdefault("viewer", _empty_state()["viewer"])
        else:
            s = _load_state()
            if projects:
                s["projects"].update(projects)
            if runs:
                s["runs"].update(runs)
            if sweeps:
                s["sweeps"].update(sweeps)
            if viewer:
                s["viewer"].update(viewer)
            if state:
                for k, v in state.items():
                    if isinstance(v, dict) and isinstance(s.get(k), dict):
                        s[k].update(v)
                    else:
                        s[k] = v
        _record(
            s,
            "mock_debug_seed",
            replace=replace,
            project_count=len(s["projects"]),
            run_count=len(s["runs"]),
            sweep_count=len(s["sweeps"]),
        )
        _save_state(s)
        return {
            "ok": True,
            "projects": len(s["projects"]),
            "runs": len(s["runs"]),
            "sweeps": len(s["sweeps"]),
        }


if __name__ == "__main__":
    mcp.run()
