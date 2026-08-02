"""Linear mock MCP server.

Mirrors Linear's GraphQL API surface
(https://developers.linear.app/docs/graphql/working-with-the-graphql-api).
Each MCP tool corresponds 1:1 to a top-level GraphQL query/mutation in
Linear's schema. Parameter shapes (e.g. the `input` argument on
`issueCreate`, GraphQL connection args `first`/`after`/`filter`) and
response shapes (`{"data": {...}}` for queries, `{"success": True,
"<entity>": {...}, "lastSyncId": N}` for mutations) match the real API
so an agent trained on real Linear sees the same interface.

State lives at `$LINEAR_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/linear_mock`). Per-rollout isolation should clear
the state dir between rollouts. Optional `LINEAR_MOCK_SEED_PATH`
preloads state when no state.json exists yet.

Every call (queries and mutations) appends to `state["calls"]` so
verifiers can replay the trace.

Queries (return `{"data": {...}}`):
  viewer, teams, team, users, user, issues, issue, projects, project,
  workflowStates, comments

Mutations (return `{"success": True, "<entity>": {...},
                    "lastSyncId": N}` on success or
            `{"success": False, "errors": [...]}` on failure):
  issueCreate, issueUpdate, issueDelete, commentCreate, projectCreate,
  projectUpdate

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.

Linear ID conventions:
  - Internal ids: UUID v4 (e.g. "a1b2c3d4-...")
  - Issue identifiers: "<TEAMKEY>-<NUM>"  (e.g. "ENG-123")
  - Teams: UUID id + short `key` (e.g. "ENG")
  - Priorities: 0 (None), 1 (Urgent), 2 (High), 3 (Medium), 4 (Low)
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "LINEAR_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/linear_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _new_id() -> str:
    return str(uuid.uuid4())


def _empty_state() -> dict:
    viewer_id = "00000000-0000-0000-0000-0000000000aa"
    org_id = "00000000-0000-0000-0000-0000000000bb"
    return {
        "organization": {
            "id": org_id,
            "name": "Mock Organization",
            "urlKey": "mock",
            "createdAt": _now(),
        },
        "viewer": {
            "id": viewer_id,
            "name": "Mock Bot",
            "displayName": "mockbot",
            "email": "mockbot@example.com",
            "active": True,
            "admin": True,
            "createdAt": _now(),
        },
        "users": {
            viewer_id: {
                "id": viewer_id,
                "name": "Mock Bot",
                "displayName": "mockbot",
                "email": "mockbot@example.com",
                "active": True,
                "admin": True,
                "createdAt": _now(),
            },
        },
        "teams": {},          # id -> team
        "workflowStates": {}, # id -> workflow state
        "labels": {},         # id -> label
        "projects": {},       # id -> project
        "issues": {},         # id -> issue (key: UUID)
        "issues_by_identifier": {},  # "ENG-1" -> UUID
        "comments": {},       # id -> comment
        "next_issue_number": {},  # team_id -> int
        "last_sync_id": 1,
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("LINEAR_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must
    # not KeyError downstream - merge the skeleton's missing keys.
    for k, v in _empty_state().items():
        s.setdefault(k, v)
    return s


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
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


def _bump_sync(state: dict) -> int:
    state["last_sync_id"] = int(state.get("last_sync_id", 0)) + 1
    return state["last_sync_id"]


# ---------------------------------------------------------------------------
# Helpers — Linear shape builders
# ---------------------------------------------------------------------------

VALID_PRIORITIES = {0, 1, 2, 3, 4}
PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High",
                   3: "Medium", 4: "Low"}


def _err(message: str, code: str = "INVALID_INPUT",
         path: list | None = None) -> dict:
    """GraphQL-shaped error entry for mutation failures."""
    return {
        "message": message,
        "extensions": {"code": code, "type": "invalid input",
                       "userPresentableMessage": message},
        "path": path or [],
    }


def _data_err(message: str, code: str = "INVALID_INPUT") -> dict:
    """GraphQL-shaped error envelope for query failures."""
    return {"data": None, "errors": [_err(message, code)]}


def _mut_err(message: str, code: str = "INVALID_INPUT") -> dict:
    """Linear mutation-style failure envelope."""
    return {"success": False, "lastSyncId": 0,
            "errors": [_err(message, code)]}


def _connection(nodes: list, page_size: int,
                cursor: str | None) -> dict:
    """Build a Relay-style GraphQL connection (Linear uses
    `nodes` + `pageInfo`)."""
    page_size = max(1, min(int(page_size or 50), 250))
    start = 0
    if cursor:
        for i, n in enumerate(nodes):
            if isinstance(n, dict) and n.get("id") == cursor:
                start = i + 1
                break
    page = nodes[start: start + page_size]
    has_next = start + page_size < len(nodes)
    end_cursor = page[-1]["id"] if page and isinstance(page[-1], dict) else None
    return {
        "nodes": page,
        "pageInfo": {
            "hasNextPage": has_next,
            "hasPreviousPage": start > 0,
            "startCursor": page[0]["id"] if page and isinstance(page[0], dict) else None,
            "endCursor": end_cursor,
        },
    }


def _team_view(t: dict) -> dict:
    return {
        "id": t["id"],
        "name": t.get("name", ""),
        "key": t.get("key", ""),
        "description": t.get("description"),
        "color": t.get("color"),
        "icon": t.get("icon"),
        "private": bool(t.get("private", False)),
        "createdAt": t.get("createdAt"),
        "updatedAt": t.get("updatedAt") or t.get("createdAt"),
    }


def _user_view(u: dict) -> dict:
    return {
        "id": u["id"],
        "name": u.get("name", ""),
        "displayName": u.get("displayName", u.get("name", "")),
        "email": u.get("email", ""),
        "active": bool(u.get("active", True)),
        "admin": bool(u.get("admin", False)),
        "avatarUrl": u.get("avatarUrl"),
        "createdAt": u.get("createdAt"),
    }


def _state_view(st: dict) -> dict:
    return {
        "id": st["id"],
        "name": st.get("name", ""),
        "type": st.get("type", "unstarted"),
        "color": st.get("color"),
        "position": st.get("position", 0),
        "team": {"id": st.get("teamId")},
    }


def _label_view(lb: dict) -> dict:
    return {
        "id": lb["id"],
        "name": lb.get("name", ""),
        "color": lb.get("color"),
        "team": {"id": lb.get("teamId")} if lb.get("teamId") else None,
    }


def _project_view(p: dict) -> dict:
    return {
        "id": p["id"],
        "name": p.get("name", ""),
        "description": p.get("description", ""),
        "state": p.get("state", "planned"),
        "slugId": p.get("slugId", ""),
        "color": p.get("color"),
        "icon": p.get("icon"),
        "startDate": p.get("startDate"),
        "targetDate": p.get("targetDate"),
        "progress": p.get("progress", 0.0),
        "createdAt": p.get("createdAt"),
        "updatedAt": p.get("updatedAt") or p.get("createdAt"),
        "teams": {"nodes": [{"id": tid} for tid in p.get("teamIds", [])]},
        "lead": {"id": p["leadId"]} if p.get("leadId") else None,
    }


def _issue_view(state: dict, it: dict) -> dict:
    labels = [state["labels"][lid] for lid in it.get("labelIds", [])
              if lid in state["labels"]]
    return {
        "id": it["id"],
        "identifier": it.get("identifier", ""),
        "number": it.get("number", 0),
        "title": it.get("title", ""),
        "description": it.get("description", ""),
        "priority": it.get("priority", 0),
        "priorityLabel": PRIORITY_LABELS.get(it.get("priority", 0),
                                             "No priority"),
        "estimate": it.get("estimate"),
        "url": (f"https://linear.app/mock/issue/{it.get('identifier','')}"
                f"/{it.get('slug', it.get('id',''))}"),
        "branchName": it.get("branchName", ""),
        "createdAt": it.get("createdAt"),
        "updatedAt": it.get("updatedAt") or it.get("createdAt"),
        "completedAt": it.get("completedAt"),
        "canceledAt": it.get("canceledAt"),
        "archivedAt": it.get("archivedAt"),
        "dueDate": it.get("dueDate"),
        "team": {"id": it.get("teamId")} if it.get("teamId") else None,
        "state": ({"id": it["stateId"],
                   "name": state["workflowStates"].get(it["stateId"], {}).get("name", ""),
                   "type": state["workflowStates"].get(it["stateId"], {}).get("type", "unstarted")}
                  if it.get("stateId") else None),
        "assignee": ({"id": it["assigneeId"],
                      "name": state["users"].get(it["assigneeId"], {}).get("name", ""),
                      "displayName": state["users"].get(it["assigneeId"], {}).get("displayName", "")}
                     if it.get("assigneeId") else None),
        "creator": ({"id": it["creatorId"]}
                    if it.get("creatorId") else None),
        "project": ({"id": it["projectId"],
                     "name": state["projects"].get(it["projectId"], {}).get("name", "")}
                    if it.get("projectId") else None),
        "parent": ({"id": it["parentId"]}
                   if it.get("parentId") else None),
        "labels": {"nodes": [_label_view(lb) for lb in labels]},
    }


def _comment_view(c: dict) -> dict:
    return {
        "id": c["id"],
        "body": c.get("body", ""),
        "createdAt": c.get("createdAt"),
        "updatedAt": c.get("updatedAt") or c.get("createdAt"),
        "issue": {"id": c["issueId"]} if c.get("issueId") else None,
        "user": {"id": c["userId"]} if c.get("userId") else None,
        "parent": {"id": c["parentId"]} if c.get("parentId") else None,
    }


def _resolve_issue(state: dict, ref: str) -> dict | None:
    """Resolve by UUID id OR identifier (ENG-123)."""
    if not ref:
        return None
    if ref in state["issues"]:
        return state["issues"][ref]
    uid = state.get("issues_by_identifier", {}).get(ref)
    if uid and uid in state["issues"]:
        return state["issues"][uid]
    return None


def _filter_issues(state: dict, issues: list[dict],
                   flt: dict | None) -> list[dict]:
    """Subset of Linear's `IssueFilter` input — top-level field
    filters with `eq`/`in`/`contains`/`null`. Linear's full filter
    AST (and/or/and-nested) is not modeled — single conjunctive set
    of conditions only."""
    if not isinstance(flt, dict):
        return issues
    out = list(issues)

    def _match_eq(node: dict, field: str, cond: dict) -> bool:
        # Handles {"eq": v}, {"in": [v,...]}, {"null": bool},
        # {"contains": s} on strings, {"gte"/"lte": v} on numbers.
        actual = node.get(field)
        if "eq" in cond and actual != cond["eq"]:
            return False
        if "neq" in cond and actual == cond["neq"]:
            return False
        if "in" in cond and actual not in (cond["in"] or []):
            return False
        if "nin" in cond and actual in (cond["nin"] or []):
            return False
        if "null" in cond:
            want_null = bool(cond["null"])
            if want_null and actual is not None:
                return False
            if not want_null and actual is None:
                return False
        if "contains" in cond:
            if not isinstance(actual, str) or cond["contains"] not in actual:
                return False
        if "containsIgnoreCase" in cond:
            if not isinstance(actual, str):
                return False
            if cond["containsIgnoreCase"].lower() not in actual.lower():
                return False
        if "gte" in cond:
            try:
                if float(actual) < float(cond["gte"]):
                    return False
            except (TypeError, ValueError):
                return False
        if "lte" in cond:
            try:
                if float(actual) > float(cond["lte"]):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    # Direct scalar fields
    scalar_fields = {
        "title": "title", "description": "description",
        "priority": "priority", "number": "number",
        "estimate": "estimate", "dueDate": "dueDate",
        "createdAt": "createdAt", "updatedAt": "updatedAt",
        "completedAt": "completedAt",
    }
    for f, attr in scalar_fields.items():
        if f in flt and isinstance(flt[f], dict):
            out = [i for i in out if _match_eq(i, attr, flt[f])]

    # Nested {"team": {"id": {"eq": ...}}}
    if "team" in flt and isinstance(flt["team"], dict):
        tcond = flt["team"].get("id")
        if isinstance(tcond, dict):
            out = [i for i in out if _match_eq(i, "teamId", tcond)]
    if "state" in flt and isinstance(flt["state"], dict):
        scond = flt["state"].get("id")
        if isinstance(scond, dict):
            out = [i for i in out if _match_eq(i, "stateId", scond)]
        ncond = flt["state"].get("name")
        if isinstance(ncond, dict):
            def _state_name_match(it):
                st = state["workflowStates"].get(it.get("stateId") or "", {})
                return _match_eq({"name": st.get("name")}, "name", ncond)
            out = [i for i in out if _state_name_match(i)]
        tcond = flt["state"].get("type")
        if isinstance(tcond, dict):
            def _state_type_match(it):
                st = state["workflowStates"].get(it.get("stateId") or "", {})
                return _match_eq({"type": st.get("type")}, "type", tcond)
            out = [i for i in out if _state_type_match(i)]
    if "assignee" in flt and isinstance(flt["assignee"], dict):
        acond = flt["assignee"].get("id")
        if isinstance(acond, dict):
            out = [i for i in out if _match_eq(i, "assigneeId", acond)]
    if "project" in flt and isinstance(flt["project"], dict):
        pcond = flt["project"].get("id")
        if isinstance(pcond, dict):
            out = [i for i in out if _match_eq(i, "projectId", pcond)]
    if "creator" in flt and isinstance(flt["creator"], dict):
        ccond = flt["creator"].get("id")
        if isinstance(ccond, dict):
            out = [i for i in out if _match_eq(i, "creatorId", ccond)]
    return out


def _sort_issues(issues: list[dict],
                 order_by: str | None) -> list[dict]:
    """Linear's `orderBy` accepts "createdAt" / "updatedAt"
    (descending by default in the real API)."""
    key = (order_by or "updatedAt")
    if key not in ("createdAt", "updatedAt", "priority", "number"):
        key = "updatedAt"
    issues = sorted(issues,
                    key=lambda i: (i.get(key) or ""),
                    reverse=True)
    return issues


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("linear-mock")


# ===========================================================================
# QUERIES — return {"data": {<root>: ...}}
# ===========================================================================

@mcp.tool(name="viewer")
def viewer() -> dict:
    """Linear GraphQL: `viewer` — return the authenticated user
    (the integration's API key owner). Mocked as a single bot user."""
    with _lock():
        s = _load_state()
        _record(s, "viewer")
        _save_state(s)
        return {"data": {"viewer": _user_view(s["viewer"])}}


@mcp.tool(name="teams")
def teams(filter: dict | None = None,
          first: int = 50,
          after: str | None = None,
          orderBy: str | None = None,
          includeArchived: bool = False) -> dict:
    """Linear GraphQL: `teams(filter, first, after, orderBy)` —
    paginated list of teams. Returns a `TeamConnection`
    (`{data:{teams:{nodes:[...], pageInfo:{...}}}}`).

    Supported filter keys: `name`, `key`, `private` with
    `eq`/`contains`/`in` conditions."""
    with _lock():
        s = _load_state()
        rows = list(s["teams"].values())
        if not includeArchived:
            rows = [t for t in rows if not t.get("archivedAt")]
        if isinstance(filter, dict):
            def _ok(t):
                for f in ("name", "key"):
                    cond = filter.get(f)
                    if isinstance(cond, dict):
                        v = t.get(f, "")
                        if "eq" in cond and v != cond["eq"]:
                            return False
                        if "in" in cond and v not in (cond["in"] or []):
                            return False
                        if "contains" in cond and cond["contains"] not in v:
                            return False
                cond = filter.get("private")
                if isinstance(cond, dict) and "eq" in cond:
                    if bool(t.get("private")) != bool(cond["eq"]):
                        return False
                return True
            rows = [t for t in rows if _ok(t)]
        key = orderBy if orderBy in ("createdAt", "updatedAt",
                                      "name") else "updatedAt"
        rows.sort(key=lambda t: (t.get(key) or ""), reverse=(key != "name"))
        nodes = [_team_view(t) for t in rows]
        conn = _connection(nodes, first, after)
        _record(s, "teams", count=len(conn["nodes"]),
                filter_keys=list((filter or {}).keys()))
        _save_state(s)
        return {"data": {"teams": conn}}


@mcp.tool(name="team")
def team(id: str) -> dict:
    """Linear GraphQL: `team(id)` — fetch one team by id or by key.
    Linear accepts either the UUID or the short `key` (e.g. "ENG")."""
    with _lock():
        s = _load_state()
        t = s["teams"].get(id)
        if not t:
            # key lookup
            t = next((tt for tt in s["teams"].values()
                      if tt.get("key") == id), None)
        _record(s, "team", id=id, result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _data_err(f"Entity not found: Team - {id}",
                             "ENTITY_NOT_FOUND")
        # Include nested connections agents commonly request
        team_states = [st for st in s["workflowStates"].values()
                       if st.get("teamId") == t["id"]]
        team_labels = [lb for lb in s["labels"].values()
                       if lb.get("teamId") == t["id"]]
        team_issues = [i for i in s["issues"].values()
                       if i.get("teamId") == t["id"]
                       and not i.get("archivedAt")]
        view = _team_view(t)
        view["states"] = {"nodes": [_state_view(st) for st in team_states]}
        view["labels"] = {"nodes": [_label_view(lb) for lb in team_labels]}
        view["issues"] = {"nodes": [_issue_view(s, i) for i in team_issues]}
        view["members"] = {"nodes": [_user_view(s["users"][uid])
                                      for uid in t.get("memberIds", [])
                                      if uid in s["users"]]}
        return {"data": {"team": view}}


@mcp.tool(name="users")
def users(filter: dict | None = None,
          first: int = 50,
          after: str | None = None,
          includeArchived: bool = False,
          includeDisabled: bool = False) -> dict:
    """Linear GraphQL: `users(filter, first, after)` — paginated
    list of workspace users. Supported filter keys: `name`,
    `displayName`, `email`, `active`."""
    with _lock():
        s = _load_state()
        rows = list(s["users"].values())
        if not includeDisabled:
            rows = [u for u in rows if u.get("active", True)]
        if isinstance(filter, dict):
            def _ok(u):
                for f in ("name", "displayName", "email"):
                    cond = filter.get(f)
                    if isinstance(cond, dict):
                        v = u.get(f, "")
                        if "eq" in cond and v != cond["eq"]:
                            return False
                        if "contains" in cond and cond["contains"] not in v:
                            return False
                        if "containsIgnoreCase" in cond:
                            if cond["containsIgnoreCase"].lower() not in (v or "").lower():
                                return False
                cond = filter.get("active")
                if isinstance(cond, dict) and "eq" in cond:
                    if bool(u.get("active", True)) != bool(cond["eq"]):
                        return False
                return True
            rows = [u for u in rows if _ok(u)]
        rows.sort(key=lambda u: u.get("name", ""))
        nodes = [_user_view(u) for u in rows]
        conn = _connection(nodes, first, after)
        _record(s, "users", count=len(conn["nodes"]))
        _save_state(s)
        return {"data": {"users": conn}}


@mcp.tool(name="user")
def user(id: str) -> dict:
    """Linear GraphQL: `user(id)` — fetch one user. Accepts UUID,
    email, or displayName (Linear's API allows `me` for the viewer)."""
    with _lock():
        s = _load_state()
        if id == "me":
            u = s["viewer"]
        else:
            u = s["users"].get(id)
            if not u:
                u = next((uu for uu in s["users"].values()
                          if uu.get("email") == id
                          or uu.get("displayName") == id), None)
        _record(s, "user", id=id, result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _data_err(f"Entity not found: User - {id}",
                             "ENTITY_NOT_FOUND")
        return {"data": {"user": _user_view(u)}}


@mcp.tool(name="issues")
def issues(filter: dict | None = None,
           first: int = 50,
           after: str | None = None,
           orderBy: str | None = None,
           includeArchived: bool = False) -> dict:
    """Linear GraphQL: `issues(filter, first, after, orderBy,
    includeArchived)` — paginated list of issues.

    Filter is a subset of Linear's `IssueFilter` input: top-level
    scalar fields (`title`, `description`, `priority`, `number`,
    `estimate`, `dueDate`, `createdAt`, `updatedAt`, `completedAt`)
    with `eq`/`neq`/`in`/`nin`/`null`/`contains`/`containsIgnoreCase`/
    `gte`/`lte` and nested filters on `team`, `state`, `assignee`,
    `project`, `creator` (each takes `{id|name|type: {eq|in|...}}`).
    Boolean AND/OR composition is not implemented."""
    with _lock():
        s = _load_state()
        rows = list(s["issues"].values())
        if not includeArchived:
            rows = [i for i in rows if not i.get("archivedAt")]
        rows = _filter_issues(s, rows, filter)
        rows = _sort_issues(rows, orderBy)
        nodes = [_issue_view(s, i) for i in rows]
        conn = _connection(nodes, first, after)
        _record(s, "issues", count=len(conn["nodes"]),
                filter_keys=list((filter or {}).keys()))
        _save_state(s)
        return {"data": {"issues": conn}}


@mcp.tool(name="issue")
def issue(id: str) -> dict:
    """Linear GraphQL: `issue(id)` — fetch one issue. Accepts the
    UUID id OR the human identifier ("ENG-123")."""
    with _lock():
        s = _load_state()
        it = _resolve_issue(s, id)
        _record(s, "issue", id=id, result="ok" if it else "not_found")
        _save_state(s)
        if not it:
            return _data_err(f"Entity not found: Issue - {id}",
                             "ENTITY_NOT_FOUND")
        view = _issue_view(s, it)
        # Eager-load comments connection (commonly requested in the
        # same query as the issue)
        comments = [c for c in s["comments"].values()
                    if c.get("issueId") == it["id"]]
        comments.sort(key=lambda c: c.get("createdAt") or "")
        view["comments"] = {"nodes": [_comment_view(c) for c in comments]}
        return {"data": {"issue": view}}


@mcp.tool(name="projects")
def projects(filter: dict | None = None,
             first: int = 50,
             after: str | None = None,
             orderBy: str | None = None,
             includeArchived: bool = False) -> dict:
    """Linear GraphQL: `projects(filter, first, after, orderBy)` —
    paginated list of projects. Supported filter keys: `name`,
    `state`, `slugId` with `eq`/`in`/`contains`."""
    with _lock():
        s = _load_state()
        rows = list(s["projects"].values())
        if not includeArchived:
            rows = [p for p in rows if not p.get("archivedAt")]
        if isinstance(filter, dict):
            def _ok(p):
                for f in ("name", "slugId", "state"):
                    cond = filter.get(f)
                    if isinstance(cond, dict):
                        v = p.get(f, "")
                        if "eq" in cond and v != cond["eq"]:
                            return False
                        if "in" in cond and v not in (cond["in"] or []):
                            return False
                        if "contains" in cond and cond["contains"] not in v:
                            return False
                return True
            rows = [p for p in rows if _ok(p)]
        key = orderBy if orderBy in ("createdAt", "updatedAt",
                                      "name") else "updatedAt"
        rows.sort(key=lambda p: (p.get(key) or ""), reverse=(key != "name"))
        nodes = [_project_view(p) for p in rows]
        conn = _connection(nodes, first, after)
        _record(s, "projects", count=len(conn["nodes"]))
        _save_state(s)
        return {"data": {"projects": conn}}


@mcp.tool(name="project")
def project(id: str) -> dict:
    """Linear GraphQL: `project(id)` — fetch one project by UUID or
    by `slugId`."""
    with _lock():
        s = _load_state()
        p = s["projects"].get(id)
        if not p:
            p = next((pp for pp in s["projects"].values()
                      if pp.get("slugId") == id), None)
        _record(s, "project", id=id, result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            return _data_err(f"Entity not found: Project - {id}",
                             "ENTITY_NOT_FOUND")
        view = _project_view(p)
        # Include attached issues for convenience
        proj_issues = [i for i in s["issues"].values()
                       if i.get("projectId") == p["id"]
                       and not i.get("archivedAt")]
        view["issues"] = {"nodes": [_issue_view(s, i) for i in proj_issues]}
        return {"data": {"project": view}}


@mcp.tool(name="workflowStates")
def workflow_states(filter: dict | None = None,
                    first: int = 50,
                    after: str | None = None,
                    includeArchived: bool = False) -> dict:
    """Linear GraphQL: `workflowStates(filter, first, after)` —
    paginated list of workflow states. Supported filter keys: `team`
    (with nested `id`/`key` `eq`), `type` (`eq`/`in`)."""
    with _lock():
        s = _load_state()
        rows = list(s["workflowStates"].values())
        if isinstance(filter, dict):
            tf = filter.get("team")
            if isinstance(tf, dict):
                idcond = tf.get("id")
                keycond = tf.get("key")
                if isinstance(idcond, dict) and "eq" in idcond:
                    rows = [st for st in rows
                            if st.get("teamId") == idcond["eq"]]
                if isinstance(keycond, dict) and "eq" in keycond:
                    team_ids = {t["id"] for t in s["teams"].values()
                                if t.get("key") == keycond["eq"]}
                    rows = [st for st in rows
                            if st.get("teamId") in team_ids]
            tcond = filter.get("type")
            if isinstance(tcond, dict):
                if "eq" in tcond:
                    rows = [st for st in rows
                            if st.get("type") == tcond["eq"]]
                if "in" in tcond:
                    allowed = set(tcond["in"] or [])
                    rows = [st for st in rows
                            if st.get("type") in allowed]
        rows.sort(key=lambda st: st.get("position", 0))
        nodes = [_state_view(st) for st in rows]
        conn = _connection(nodes, first, after)
        _record(s, "workflowStates", count=len(conn["nodes"]))
        _save_state(s)
        return {"data": {"workflowStates": conn}}


@mcp.tool(name="comments")
def comments(filter: dict | None = None,
             first: int = 50,
             after: str | None = None) -> dict:
    """Linear GraphQL: `comments(filter, first, after)` — paginated
    list of comments. Supported filter keys: `issue` (nested `id`
    `eq`), `user` (nested `id` `eq`), `body` (`contains`/
    `containsIgnoreCase`)."""
    with _lock():
        s = _load_state()
        rows = list(s["comments"].values())
        if isinstance(filter, dict):
            issf = filter.get("issue")
            if isinstance(issf, dict):
                idcond = issf.get("id")
                if isinstance(idcond, dict) and "eq" in idcond:
                    # Allow identifier or UUID
                    target = idcond["eq"]
                    iss = _resolve_issue(s, target)
                    target_uuid = iss["id"] if iss else target
                    rows = [c for c in rows
                            if c.get("issueId") == target_uuid]
            uf = filter.get("user")
            if isinstance(uf, dict):
                idcond = uf.get("id")
                if isinstance(idcond, dict) and "eq" in idcond:
                    rows = [c for c in rows
                            if c.get("userId") == idcond["eq"]]
            bf = filter.get("body")
            if isinstance(bf, dict):
                if "contains" in bf:
                    rows = [c for c in rows
                            if bf["contains"] in (c.get("body") or "")]
                if "containsIgnoreCase" in bf:
                    needle = bf["containsIgnoreCase"].lower()
                    rows = [c for c in rows
                            if needle in (c.get("body") or "").lower()]
        rows.sort(key=lambda c: c.get("createdAt") or "")
        nodes = [_comment_view(c) for c in rows]
        conn = _connection(nodes, first, after)
        _record(s, "comments", count=len(conn["nodes"]))
        _save_state(s)
        return {"data": {"comments": conn}}


# ===========================================================================
# MUTATIONS — return {"success": True/False, "<entity>": {...}, ...}
# ===========================================================================

def _next_issue_number(state: dict, team_id: str) -> int:
    nums = state.setdefault("next_issue_number", {})
    if team_id not in nums:
        existing = [i.get("number", 0) for i in state["issues"].values()
                    if i.get("teamId") == team_id]
        nums[team_id] = (max(existing) if existing else 0) + 1
    else:
        nums[team_id] += 1
    return nums[team_id]


@mcp.tool(name="issueCreate")
def issue_create(input: dict) -> dict:
    """Linear GraphQL mutation: `issueCreate(input: IssueCreateInput!)`.

    `input` shape (Linear's IssueCreateInput):
      - title (required)
      - teamId (required)
      - description (markdown)
      - priority (0..4)
      - stateId, assigneeId, projectId, parentId
      - labelIds (list of label UUIDs)
      - estimate, dueDate (ISO date)
      - createAsUser (display-name override)

    Returns `{"success": True, "issue": {...}, "lastSyncId": N}`.
    Validation errors return
    `{"success": False, "errors": [...], "lastSyncId": 0}`."""
    if not isinstance(input, dict):
        return _mut_err("input must be an object")
    with _lock():
        s = _load_state()
        title = input.get("title")
        team_id = input.get("teamId")
        if not title or not isinstance(title, str):
            _record(s, "issueCreate", result="missing_title")
            _save_state(s)
            return _mut_err("title is required", "INVALID_INPUT")
        if not team_id or team_id not in s["teams"]:
            _record(s, "issueCreate", team_id=team_id,
                    result="invalid_team")
            _save_state(s)
            return _mut_err(f"Entity not found: Team - {team_id}",
                            "ENTITY_NOT_FOUND")
        team = s["teams"][team_id]

        priority = input.get("priority", 0)
        if priority not in VALID_PRIORITIES:
            _record(s, "issueCreate", result="invalid_priority")
            _save_state(s)
            return _mut_err(
                f"priority must be one of {sorted(VALID_PRIORITIES)}",
                "INVALID_INPUT")

        state_id = input.get("stateId")
        if state_id is not None:
            st = s["workflowStates"].get(state_id)
            if not st or st.get("teamId") != team_id:
                _record(s, "issueCreate", result="invalid_state",
                        state_id=state_id)
                _save_state(s)
                return _mut_err(
                    f"WorkflowState {state_id} not in team {team_id}",
                    "ENTITY_NOT_FOUND")
        else:
            # Default to first "unstarted"-type state of the team
            backlog = sorted(
                (st for st in s["workflowStates"].values()
                 if st.get("teamId") == team_id),
                key=lambda st: st.get("position", 0))
            state_id = backlog[0]["id"] if backlog else None

        assignee_id = input.get("assigneeId")
        if assignee_id and assignee_id not in s["users"]:
            _record(s, "issueCreate", result="invalid_assignee",
                    assignee_id=assignee_id)
            _save_state(s)
            return _mut_err(f"Entity not found: User - {assignee_id}",
                            "ENTITY_NOT_FOUND")

        project_id = input.get("projectId")
        if project_id and project_id not in s["projects"]:
            _record(s, "issueCreate", result="invalid_project",
                    project_id=project_id)
            _save_state(s)
            return _mut_err(f"Entity not found: Project - {project_id}",
                            "ENTITY_NOT_FOUND")

        parent_id = input.get("parentId")
        if parent_id and parent_id not in s["issues"]:
            iss = _resolve_issue(s, parent_id)
            parent_id = iss["id"] if iss else None
            if not parent_id:
                _record(s, "issueCreate", result="invalid_parent")
                _save_state(s)
                return _mut_err("Parent issue not found",
                                "ENTITY_NOT_FOUND")

        label_ids = list(input.get("labelIds") or [])
        for lid in label_ids:
            if lid not in s["labels"]:
                _record(s, "issueCreate", result="invalid_label",
                        label=lid)
                _save_state(s)
                return _mut_err(f"Entity not found: Label - {lid}",
                                "ENTITY_NOT_FOUND")

        num = _next_issue_number(s, team_id)
        identifier = f"{team['key']}-{num}"
        new_id = _new_id()
        now = _now()
        issue_obj = {
            "id": new_id,
            "identifier": identifier,
            "number": num,
            "title": title,
            "description": input.get("description", ""),
            "priority": priority,
            "estimate": input.get("estimate"),
            "dueDate": input.get("dueDate"),
            "teamId": team_id,
            "stateId": state_id,
            "assigneeId": assignee_id,
            "projectId": project_id,
            "parentId": parent_id,
            "labelIds": label_ids,
            "creatorId": s["viewer"]["id"],
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
            "canceledAt": None,
            "archivedAt": None,
            "branchName": (f"{team['key'].lower()}/{identifier.lower()}-"
                           f"{title.lower().replace(' ', '-')[:30]}"),
            "slug": title.lower().replace(" ", "-")[:50],
        }
        s["issues"][new_id] = issue_obj
        s.setdefault("issues_by_identifier", {})[identifier] = new_id
        sync = _bump_sync(s)
        _record(s, "issueCreate", id=new_id, identifier=identifier,
                team_id=team_id)
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "issue": _issue_view(s, issue_obj)}


@mcp.tool(name="issueUpdate")
def issue_update(id: str, input: dict) -> dict:
    """Linear GraphQL mutation: `issueUpdate(id, input: IssueUpdateInput!)`.

    `id` accepts UUID or identifier ("ENG-123").
    `input` shape: any subset of `{title, description, priority,
    stateId, assigneeId, projectId, parentId, labelIds, estimate,
    dueDate}`. Setting `assigneeId: null` unassigns. Setting
    `stateId` to a completed-type state stamps `completedAt`."""
    if not isinstance(input, dict):
        return _mut_err("input must be an object")
    with _lock():
        s = _load_state()
        it = _resolve_issue(s, id)
        if not it:
            _record(s, "issueUpdate", id=id, result="not_found")
            _save_state(s)
            return _mut_err(f"Entity not found: Issue - {id}",
                            "ENTITY_NOT_FOUND")
        if "priority" in input and input["priority"] not in VALID_PRIORITIES:
            _record(s, "issueUpdate", id=id, result="invalid_priority")
            _save_state(s)
            return _mut_err(
                f"priority must be one of {sorted(VALID_PRIORITIES)}",
                "INVALID_INPUT")
        if "stateId" in input and input["stateId"] is not None:
            st = s["workflowStates"].get(input["stateId"])
            if not st or st.get("teamId") != it["teamId"]:
                _record(s, "issueUpdate", id=id, result="invalid_state")
                _save_state(s)
                return _mut_err(
                    f"WorkflowState {input['stateId']} not in team "
                    f"{it['teamId']}", "ENTITY_NOT_FOUND")
        if ("assigneeId" in input and input["assigneeId"] is not None
                and input["assigneeId"] not in s["users"]):
            _record(s, "issueUpdate", id=id, result="invalid_assignee")
            _save_state(s)
            return _mut_err(
                f"Entity not found: User - {input['assigneeId']}",
                "ENTITY_NOT_FOUND")
        if ("projectId" in input and input["projectId"] is not None
                and input["projectId"] not in s["projects"]):
            _record(s, "issueUpdate", id=id, result="invalid_project")
            _save_state(s)
            return _mut_err(
                f"Entity not found: Project - {input['projectId']}",
                "ENTITY_NOT_FOUND")
        if "labelIds" in input and input["labelIds"] is not None:
            for lid in input["labelIds"]:
                if lid not in s["labels"]:
                    _record(s, "issueUpdate", id=id, result="invalid_label")
                    _save_state(s)
                    return _mut_err(f"Entity not found: Label - {lid}",
                                    "ENTITY_NOT_FOUND")

        for k in ("title", "description", "priority", "stateId",
                  "assigneeId", "projectId", "parentId", "labelIds",
                  "estimate", "dueDate"):
            if k in input:
                it[k] = input[k]
        it["updatedAt"] = _now()
        # If moved into a completed state, stamp completedAt
        if it.get("stateId"):
            st = s["workflowStates"].get(it["stateId"], {})
            if st.get("type") == "completed":
                it["completedAt"] = it["updatedAt"]
            elif st.get("type") == "canceled":
                it["canceledAt"] = it["updatedAt"]
        sync = _bump_sync(s)
        _record(s, "issueUpdate", id=it["id"],
                identifier=it["identifier"],
                fields=list(input.keys()))
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "issue": _issue_view(s, it)}


@mcp.tool(name="issueDelete")
def issue_delete(id: str) -> dict:
    """Linear GraphQL mutation: `issueDelete(id)` — soft-delete
    (archive) an issue. Linear's mutation returns an
    `ArchivePayload`. We return `{"success": True, "entity":
    {"id","archivedAt"}, "lastSyncId": N}`."""
    with _lock():
        s = _load_state()
        it = _resolve_issue(s, id)
        if not it:
            _record(s, "issueDelete", id=id, result="not_found")
            _save_state(s)
            return _mut_err(f"Entity not found: Issue - {id}",
                            "ENTITY_NOT_FOUND")
        now = _now()
        it["archivedAt"] = now
        it["updatedAt"] = now
        sync = _bump_sync(s)
        _record(s, "issueDelete", id=it["id"],
                identifier=it["identifier"])
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "entity": {"id": it["id"], "archivedAt": now}}


@mcp.tool(name="commentCreate")
def comment_create(input: dict) -> dict:
    """Linear GraphQL mutation: `commentCreate(input:
    CommentCreateInput!)`.

    `input` shape: `{issueId, body, parentId?}` — `issueId` accepts
    UUID or identifier ("ENG-123"). `body` is markdown."""
    if not isinstance(input, dict):
        return _mut_err("input must be an object")
    with _lock():
        s = _load_state()
        body = input.get("body")
        if not isinstance(body, str) or not body:
            _record(s, "commentCreate", result="missing_body")
            _save_state(s)
            return _mut_err("body is required", "INVALID_INPUT")
        issue_ref = input.get("issueId")
        it = _resolve_issue(s, issue_ref) if issue_ref else None
        if not it:
            _record(s, "commentCreate", result="invalid_issue",
                    issue=issue_ref)
            _save_state(s)
            return _mut_err(f"Entity not found: Issue - {issue_ref}",
                            "ENTITY_NOT_FOUND")
        parent_id = input.get("parentId")
        if parent_id and parent_id not in s["comments"]:
            _record(s, "commentCreate", result="invalid_parent")
            _save_state(s)
            return _mut_err(f"Entity not found: Comment - {parent_id}",
                            "ENTITY_NOT_FOUND")
        cid = _new_id()
        now = _now()
        comment_obj = {
            "id": cid,
            "body": body,
            "issueId": it["id"],
            "userId": s["viewer"]["id"],
            "parentId": parent_id,
            "createdAt": now,
            "updatedAt": now,
        }
        s["comments"][cid] = comment_obj
        # Bump issue updatedAt
        it["updatedAt"] = now
        sync = _bump_sync(s)
        _record(s, "commentCreate", id=cid, issue=it["identifier"])
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "comment": _comment_view(comment_obj)}


@mcp.tool(name="projectCreate")
def project_create(input: dict) -> dict:
    """Linear GraphQL mutation: `projectCreate(input:
    ProjectCreateInput!)`.

    `input` shape: `{name (required), teamIds (required, list),
    description?, state?, color?, icon?, startDate?, targetDate?,
    leadId?, slugId?}`. `state` is one of "planned", "started",
    "paused", "completed", "canceled", "backlog"."""
    if not isinstance(input, dict):
        return _mut_err("input must be an object")
    with _lock():
        s = _load_state()
        name = input.get("name")
        team_ids = input.get("teamIds") or []
        if not name or not isinstance(name, str):
            _record(s, "projectCreate", result="missing_name")
            _save_state(s)
            return _mut_err("name is required", "INVALID_INPUT")
        if not team_ids or not isinstance(team_ids, list):
            _record(s, "projectCreate", result="missing_teams")
            _save_state(s)
            return _mut_err("teamIds is required", "INVALID_INPUT")
        for tid in team_ids:
            if tid not in s["teams"]:
                _record(s, "projectCreate", result="invalid_team",
                        team_id=tid)
                _save_state(s)
                return _mut_err(f"Entity not found: Team - {tid}",
                                "ENTITY_NOT_FOUND")
        lead_id = input.get("leadId")
        if lead_id and lead_id not in s["users"]:
            _record(s, "projectCreate", result="invalid_lead")
            _save_state(s)
            return _mut_err(f"Entity not found: User - {lead_id}",
                            "ENTITY_NOT_FOUND")
        valid_states = {"planned", "started", "paused", "completed",
                        "canceled", "backlog"}
        state_val = input.get("state", "planned")
        if state_val not in valid_states:
            _record(s, "projectCreate", result="invalid_state",
                    state=state_val)
            _save_state(s)
            return _mut_err(
                f"state must be one of {sorted(valid_states)}",
                "INVALID_INPUT")
        pid = _new_id()
        now = _now()
        slug = (input.get("slugId")
                or name.lower().replace(" ", "-")[:50])
        proj = {
            "id": pid,
            "name": name,
            "description": input.get("description", ""),
            "state": state_val,
            "slugId": slug,
            "color": input.get("color"),
            "icon": input.get("icon"),
            "startDate": input.get("startDate"),
            "targetDate": input.get("targetDate"),
            "progress": 0.0,
            "teamIds": list(team_ids),
            "leadId": lead_id,
            "createdAt": now,
            "updatedAt": now,
            "archivedAt": None,
        }
        s["projects"][pid] = proj
        sync = _bump_sync(s)
        _record(s, "projectCreate", id=pid, name=name,
                team_ids=team_ids)
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "project": _project_view(proj)}


@mcp.tool(name="projectUpdate")
def project_update(id: str, input: dict) -> dict:
    """Linear GraphQL mutation: `projectUpdate(id, input:
    ProjectUpdateInput!)`.

    `input` shape: any subset of `{name, description, state, color,
    icon, startDate, targetDate, leadId, teamIds, progress, slugId}`.
    """
    if not isinstance(input, dict):
        return _mut_err("input must be an object")
    with _lock():
        s = _load_state()
        p = s["projects"].get(id)
        if not p:
            p = next((pp for pp in s["projects"].values()
                      if pp.get("slugId") == id), None)
        if not p:
            _record(s, "projectUpdate", id=id, result="not_found")
            _save_state(s)
            return _mut_err(f"Entity not found: Project - {id}",
                            "ENTITY_NOT_FOUND")
        if "teamIds" in input and input["teamIds"] is not None:
            for tid in input["teamIds"]:
                if tid not in s["teams"]:
                    _record(s, "projectUpdate", id=p["id"],
                            result="invalid_team", team_id=tid)
                    _save_state(s)
                    return _mut_err(f"Entity not found: Team - {tid}",
                                    "ENTITY_NOT_FOUND")
        if ("leadId" in input and input["leadId"] is not None
                and input["leadId"] not in s["users"]):
            _record(s, "projectUpdate", id=p["id"],
                    result="invalid_lead")
            _save_state(s)
            return _mut_err(
                f"Entity not found: User - {input['leadId']}",
                "ENTITY_NOT_FOUND")
        if "state" in input:
            valid_states = {"planned", "started", "paused", "completed",
                            "canceled", "backlog"}
            if input["state"] not in valid_states:
                _record(s, "projectUpdate", id=p["id"],
                        result="invalid_state")
                _save_state(s)
                return _mut_err(
                    f"state must be one of {sorted(valid_states)}",
                    "INVALID_INPUT")
        for k in ("name", "description", "state", "color", "icon",
                  "startDate", "targetDate", "leadId", "teamIds",
                  "progress", "slugId"):
            if k in input:
                p[k] = input[k]
        p["updatedAt"] = _now()
        sync = _bump_sync(s)
        _record(s, "projectUpdate", id=p["id"],
                fields=list(input.keys()))
        _save_state(s)
        return {"success": True, "lastSyncId": sync,
                "project": _project_view(p)}


# ===========================================================================
# Mock-only helpers (not part of the real Linear surface)
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection). Not part of the real Linear API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(organization: dict | None = None,
                    viewer: dict | None = None,
                    users: list | None = None,
                    teams: list | None = None,
                    workflow_states: list | None = None,
                    labels: list | None = None,
                    projects: list | None = None,
                    issues: list | None = None,
                    comments: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed Linear-shaped state. Each input collection
    holds dicts with the obvious fields.

    - `users`: [{id?, name, displayName?, email?, active?, admin?}]
    - `teams`: [{id?, key, name, description?, private?, memberIds?}]
    - `workflow_states`: [{id?, teamId, name, type (backlog|unstarted|
                            started|completed|canceled|triage),
                            position?, color?}]
    - `labels`: [{id?, teamId?, name, color?}]
    - `projects`: [{id?, name, slugId?, description?, state?,
                    teamIds, leadId?, startDate?, targetDate?}]
    - `issues`: [{id?, identifier? (overrides team counter), teamId,
                  title, description?, priority?, stateId?,
                  assigneeId?, projectId?, parentId?, labelIds?,
                  estimate?, dueDate?, createdAt?, completedAt?,
                  archivedAt?}]
    - `comments`: [{id?, issueId, body, userId?, parentId?}]

    Returns `{ok, ids: {teams, users, projects, issues}}`.
    If `replace`, the state is fully reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if organization:
            s["organization"].update(organization)
        if viewer:
            s["viewer"].update(viewer)
            if viewer.get("id"):
                s["users"][viewer["id"]] = {
                    "id": viewer["id"],
                    "name": viewer.get("name", "Mock Bot"),
                    "displayName": viewer.get("displayName", "mockbot"),
                    "email": viewer.get("email", ""),
                    "active": viewer.get("active", True),
                    "admin": viewer.get("admin", True),
                    "createdAt": viewer.get("createdAt", _now()),
                }
        now = _now()
        for u in users or []:
            uid = u.get("id") or _new_id()
            s["users"][uid] = {
                "id": uid,
                "name": u.get("name", uid),
                "displayName": u.get("displayName", u.get("name", uid)),
                "email": u.get("email", ""),
                "active": u.get("active", True),
                "admin": u.get("admin", False),
                "avatarUrl": u.get("avatarUrl"),
                "createdAt": u.get("createdAt", now),
            }
        for t in teams or []:
            tid = t.get("id") or _new_id()
            s["teams"][tid] = {
                "id": tid,
                "key": t.get("key", tid[:3].upper()),
                "name": t.get("name", tid),
                "description": t.get("description"),
                "color": t.get("color"),
                "icon": t.get("icon"),
                "private": bool(t.get("private", False)),
                "memberIds": list(t.get("memberIds") or []),
                "createdAt": t.get("createdAt", now),
                "updatedAt": t.get("updatedAt", now),
                "archivedAt": t.get("archivedAt"),
            }
        for st in workflow_states or []:
            sid = st.get("id") or _new_id()
            s["workflowStates"][sid] = {
                "id": sid,
                "teamId": st.get("teamId"),
                "name": st.get("name", sid),
                "type": st.get("type", "unstarted"),
                "color": st.get("color"),
                "position": st.get("position", 0),
            }
        for lb in labels or []:
            lid = lb.get("id") or _new_id()
            s["labels"][lid] = {
                "id": lid,
                "teamId": lb.get("teamId"),
                "name": lb.get("name", lid),
                "color": lb.get("color"),
            }
        for p in projects or []:
            pid = p.get("id") or _new_id()
            s["projects"][pid] = {
                "id": pid,
                "name": p.get("name", pid),
                "description": p.get("description", ""),
                "state": p.get("state", "planned"),
                "slugId": p.get("slugId",
                                 p.get("name", pid).lower().replace(" ", "-")),
                "color": p.get("color"),
                "icon": p.get("icon"),
                "startDate": p.get("startDate"),
                "targetDate": p.get("targetDate"),
                "progress": p.get("progress", 0.0),
                "teamIds": list(p.get("teamIds") or []),
                "leadId": p.get("leadId"),
                "createdAt": p.get("createdAt", now),
                "updatedAt": p.get("updatedAt", now),
                "archivedAt": p.get("archivedAt"),
            }
        for it in issues or []:
            iid = it.get("id") or _new_id()
            team_id = it.get("teamId")
            team = s["teams"].get(team_id, {})
            if it.get("identifier"):
                identifier = it["identifier"]
                # try to extract number from identifier "KEY-NUM"
                try:
                    num = int(identifier.split("-")[-1])
                except ValueError:
                    num = it.get("number", 1)
            else:
                num = _next_issue_number(s, team_id) if team_id else 1
                identifier = (f"{team.get('key','???')}-{num}"
                              if team else f"ISS-{num}")
            entry = {
                "id": iid,
                "identifier": identifier,
                "number": num,
                "title": it.get("title", ""),
                "description": it.get("description", ""),
                "priority": it.get("priority", 0),
                "estimate": it.get("estimate"),
                "dueDate": it.get("dueDate"),
                "teamId": team_id,
                "stateId": it.get("stateId"),
                "assigneeId": it.get("assigneeId"),
                "projectId": it.get("projectId"),
                "parentId": it.get("parentId"),
                "labelIds": list(it.get("labelIds") or []),
                "creatorId": it.get("creatorId") or s["viewer"]["id"],
                "createdAt": it.get("createdAt", now),
                "updatedAt": it.get("updatedAt", now),
                "completedAt": it.get("completedAt"),
                "canceledAt": it.get("canceledAt"),
                "archivedAt": it.get("archivedAt"),
                "branchName": it.get(
                    "branchName",
                    f"{team.get('key','???').lower()}/{identifier.lower()}"),
                "slug": it.get(
                    "slug",
                    it.get("title", "").lower().replace(" ", "-")[:50]),
            }
            s["issues"][iid] = entry
            s.setdefault("issues_by_identifier", {})[identifier] = iid
            # keep counter consistent
            if team_id:
                nums = s.setdefault("next_issue_number", {})
                if num >= nums.get(team_id, 0):
                    nums[team_id] = num
        for c in comments or []:
            cid = c.get("id") or _new_id()
            s["comments"][cid] = {
                "id": cid,
                "body": c.get("body", ""),
                "issueId": c.get("issueId"),
                "userId": c.get("userId") or s["viewer"]["id"],
                "parentId": c.get("parentId"),
                "createdAt": c.get("createdAt", now),
                "updatedAt": c.get("updatedAt", now),
            }
        _record(s, "debug_seed",
                counts={"users": len(users or []),
                        "teams": len(teams or []),
                        "workflowStates": len(workflow_states or []),
                        "labels": len(labels or []),
                        "projects": len(projects or []),
                        "issues": len(issues or []),
                        "comments": len(comments or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "ids": {
                "teams": list(s["teams"].keys()),
                "users": list(s["users"].keys()),
                "projects": list(s["projects"].keys()),
                "issues": list(s["issues"].keys()),
            },
        }


if __name__ == "__main__":
    mcp.run()
