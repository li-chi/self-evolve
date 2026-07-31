"""Asana mock MCP server.

Mirrors the Asana REST API v1
(https://developers.asana.com/reference/rest-api-reference). Tool
names follow Asana's official path verbs (`list_projects`, `get_task`,
`create_task`, `search_tasks`, `add_followers_to_task`, ...). Every
response is wrapped in `{"data": ...}` (or `{"data": [...]}` for
lists) exactly like the real API. Paginated list endpoints include a
`next_page` envelope (`null` when there is no next page, otherwise
`{"offset","path","uri"}`).

GIDs: Asana uses numeric string GIDs (e.g. "1199692632478001"). The
mock generates 16-digit zero-padded ints. Every entity carries a
`resource_type` field (`"task"`, `"project"`, `"user"`, `"workspace"`,
`"team"`, `"section"`, `"tag"`, `"story"`, `"custom_field"`).

`opt_fields`: when set, responses include only the listed fields plus
`gid` and `resource_type`. When absent, list endpoints return the
"compact" representation (e.g. for a task: `{gid, resource_type, name}`)
and single-resource GETs (`get_task`, `get_project`, ...) return the
full representation.

State plumbing matches the linear-mock / jira-mock pattern: one JSON
state file at `$ASANA_MOCK_STATE_DIR/state.json`, `fcntl.flock`-
guarded, optionally seeded from `$ASANA_MOCK_SEED_PATH`, with every
tool call appended to `state["calls"]` for verifier replay.

Tools implemented:

  Workspace   list_workspaces, get_workspace
  User        list_users, get_user, get_me
  Team        list_teams, get_team
  Project     list_projects, get_project, create_project, update_project
  Section     list_sections, create_section
  Task        list_tasks, get_task, create_task, update_task,
              delete_task, search_tasks, add_task_to_section,
              add_followers_to_task
  Story       list_stories, create_story
  Tag         list_tags, create_tag, add_tag_to_task
  Custom fld  list_custom_fields

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed_workspace`,
`mock_debug_seed_user`, `mock_debug_seed_project`,
`mock_debug_seed_section`, `mock_debug_seed_task`, `mock_debug_seed_tag`.

Intentionally NOT modeled (the real API has them; the mock leaves
them out — out of scope for the synth flow):
  - webhooks, events stream
  - OAuth (no auth modeled)
  - attachments (no file upload)
  - batch API, allocations, goals, portfolios
  - typeahead / search advanced filters beyond the documented subset
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import random
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "ASANA_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/asana_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    """Asana style ISO 8601 with millis + Z suffix."""
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


_ME_GID = "1100000000000001"
_DEFAULT_WORKSPACE_GID = "1200000000000001"
_DEFAULT_TEAM_GID = "1300000000000001"


def _empty_state() -> dict:
    return {
        "me": {
            "gid": _ME_GID,
            "resource_type": "user",
            "name": "Mock Bot",
            "email": "mockbot@example.com",
            "photo": None,
            "workspaces": [_DEFAULT_WORKSPACE_GID],
        },
        "workspaces": {
            _DEFAULT_WORKSPACE_GID: {
                "gid": _DEFAULT_WORKSPACE_GID,
                "resource_type": "workspace",
                "name": "Mock Workspace",
                "is_organization": True,
                "email_domains": ["example.com"],
            },
        },
        "teams": {
            _DEFAULT_TEAM_GID: {
                "gid": _DEFAULT_TEAM_GID,
                "resource_type": "team",
                "name": "Mock Team",
                "description": "",
                "organization": {
                    "gid": _DEFAULT_WORKSPACE_GID,
                    "resource_type": "workspace",
                    "name": "Mock Workspace",
                },
                "html_description": "",
                "visibility": "request_to_join",
            },
        },
        "users": {
            _ME_GID: {
                "gid": _ME_GID,
                "resource_type": "user",
                "name": "Mock Bot",
                "email": "mockbot@example.com",
                "photo": None,
                "workspaces": [_DEFAULT_WORKSPACE_GID],
            },
        },
        "projects": {},        # gid -> project
        "sections": {},        # gid -> section
        "tasks": {},           # gid -> task
        "stories": {},         # gid -> story
        "tags": {},            # gid -> tag
        "custom_fields": {},   # gid -> custom field definition
        # task_gid -> [section_gid, ...] (membership index for fast lookup)
        "memberships": {},
        # project_gid -> [section_gid] ordering, task_gid lists per section
        "section_tasks": {},   # section_gid -> [task_gid]
        "next_gid_seq": 14000000000000_01,
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ASANA_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def _next_gid(state: dict) -> str:
    n = int(state.get("next_gid_seq", 14000000000000_01)) + 1
    state["next_gid_seq"] = n
    return f"{n:016d}"


# ---------------------------------------------------------------------------
# Error shaping (Asana REST v1)
# ---------------------------------------------------------------------------

def _err(status: int, message: str) -> dict:
    """Asana error envelope.

    Real Asana errors look like:
      {"errors":[{"message":"Not Found: ...","help":"..."}]}
    """
    return {"errors": [{"message": message, "help": ""}]}


def _not_found(resource: str, gid: str) -> dict:
    return _err(404, f"Not Found: {resource} {gid!r} could not be found.")


# ---------------------------------------------------------------------------
# Object shaping
# ---------------------------------------------------------------------------

# Compact representations (Asana's default when `opt_fields` is absent on
# list endpoints — includes only `gid`, `resource_type`, and `name`).

def _compact(entity: dict, extra_fields: tuple[str, ...] = ()) -> dict:
    out = {"gid": entity["gid"],
           "resource_type": entity.get("resource_type", "")}
    if "name" in entity:
        out["name"] = entity.get("name", "")
    for f in extra_fields:
        if f in entity:
            out[f] = entity[f]
    return out


def _apply_opt_fields(full: dict, opt_fields: str | list | None) -> dict:
    """Subset a full representation to gid + resource_type + requested
    fields. Asana's `opt_fields` is a comma-separated string in the
    real API; we accept either."""
    if opt_fields is None:
        return full
    if isinstance(opt_fields, str):
        fields = [f.strip() for f in opt_fields.split(",") if f.strip()]
    else:
        fields = [str(f).strip() for f in (opt_fields or []) if f]
    if not fields:
        return full
    out = {"gid": full.get("gid"),
           "resource_type": full.get("resource_type")}
    for f in fields:
        # Dotted subfields are allowed in real Asana (e.g.
        # "assignee.name"). Mock surfaces just the top-level key.
        top = f.split(".", 1)[0]
        if top in full:
            out[top] = full[top]
    return out


def _workspace_view(state: dict, gid: str) -> dict | None:
    w = state["workspaces"].get(gid)
    if not w:
        return None
    return dict(w)


def _team_view(state: dict, gid: str) -> dict | None:
    t = state["teams"].get(gid)
    if not t:
        return None
    return dict(t)


def _user_view(state: dict, gid: str) -> dict | None:
    u = state["users"].get(gid)
    if not u:
        return None
    return {
        "gid": u["gid"],
        "resource_type": "user",
        "name": u.get("name", ""),
        "email": u.get("email"),
        "photo": u.get("photo"),
        "workspaces": [
            {"gid": w, "resource_type": "workspace",
             "name": state["workspaces"].get(w, {}).get("name", "")}
            for w in u.get("workspaces", [])
            if w in state["workspaces"]
        ],
    }


def _project_view(state: dict, gid: str) -> dict | None:
    p = state["projects"].get(gid)
    if not p:
        return None
    workspace_gid = p.get("workspace_gid")
    team_gid = p.get("team_gid")
    owner_gid = p.get("owner_gid")
    return {
        "gid": p["gid"],
        "resource_type": "project",
        "name": p.get("name", ""),
        "notes": p.get("notes", ""),
        "html_notes": p.get("html_notes", ""),
        "archived": bool(p.get("archived", False)),
        "color": p.get("color"),
        "current_status": None,
        "created_at": p.get("created_at"),
        "modified_at": p.get("modified_at"),
        "due_date": p.get("due_date"),
        "due_on": p.get("due_on"),
        "start_on": p.get("start_on"),
        "public": bool(p.get("public", True)),
        "workspace": ({"gid": workspace_gid,
                       "resource_type": "workspace",
                       "name": state["workspaces"].get(
                           workspace_gid, {}).get("name", "")}
                      if workspace_gid else None),
        "team": ({"gid": team_gid, "resource_type": "team",
                  "name": state["teams"].get(team_gid, {}).get("name", "")}
                 if team_gid else None),
        "owner": ({"gid": owner_gid, "resource_type": "user",
                   "name": state["users"].get(owner_gid, {}).get("name", "")}
                  if owner_gid else None),
        "members": [
            {"gid": uid, "resource_type": "user",
             "name": state["users"].get(uid, {}).get("name", "")}
            for uid in p.get("member_gids", [])
            if uid in state["users"]
        ],
        "followers": [
            {"gid": uid, "resource_type": "user",
             "name": state["users"].get(uid, {}).get("name", "")}
            for uid in p.get("follower_gids", [])
            if uid in state["users"]
        ],
        "permalink_url": (
            f"https://app.asana.com/0/{p['gid']}/list"),
    }


def _section_view(state: dict, gid: str) -> dict | None:
    s = state["sections"].get(gid)
    if not s:
        return None
    project_gid = s.get("project_gid")
    return {
        "gid": s["gid"],
        "resource_type": "section",
        "name": s.get("name", ""),
        "created_at": s.get("created_at"),
        "project": ({"gid": project_gid, "resource_type": "project",
                     "name": state["projects"].get(
                         project_gid, {}).get("name", "")}
                    if project_gid else None),
    }


def _tag_view(state: dict, gid: str) -> dict | None:
    t = state["tags"].get(gid)
    if not t:
        return None
    workspace_gid = t.get("workspace_gid")
    return {
        "gid": t["gid"],
        "resource_type": "tag",
        "name": t.get("name", ""),
        "color": t.get("color"),
        "created_at": t.get("created_at"),
        "workspace": ({"gid": workspace_gid, "resource_type": "workspace",
                       "name": state["workspaces"].get(
                           workspace_gid, {}).get("name", "")}
                      if workspace_gid else None),
    }


def _story_view(state: dict, gid: str) -> dict | None:
    s = state["stories"].get(gid)
    if not s:
        return None
    by_gid = s.get("created_by_gid")
    task_gid = s.get("task_gid")
    return {
        "gid": s["gid"],
        "resource_type": "story",
        "type": s.get("type", "comment"),
        "text": s.get("text", ""),
        "html_text": s.get("html_text", s.get("text", "")),
        "created_at": s.get("created_at"),
        "created_by": ({"gid": by_gid, "resource_type": "user",
                        "name": state["users"].get(by_gid, {}).get("name", "")}
                       if by_gid else None),
        "target": ({"gid": task_gid, "resource_type": "task",
                    "name": state["tasks"].get(task_gid, {}).get("name", "")}
                   if task_gid else None),
    }


def _custom_field_value_view(state: dict, cf: dict) -> dict:
    """Render a per-task custom-field value entry. `cf` carries the
    stored {gid, type, text_value | number_value | enum_value (gid) |
    multi_enum_values | date_value} plus a `definition_gid` pointing
    to the workspace-level field definition."""
    defn = state["custom_fields"].get(cf.get("definition_gid"), {})
    out: dict[str, Any] = {
        "gid": cf.get("definition_gid"),
        "resource_type": "custom_field",
        "name": defn.get("name", ""),
        "type": defn.get("type", "text"),
    }
    t = defn.get("type", "text")
    if t == "text":
        out["text_value"] = cf.get("text_value")
    elif t == "number":
        out["number_value"] = cf.get("number_value")
    elif t == "enum":
        ev_gid = cf.get("enum_value_gid")
        ev = next((o for o in defn.get("enum_options", [])
                   if o.get("gid") == ev_gid), None)
        out["enum_value"] = (
            {"gid": ev["gid"], "resource_type": "enum_option",
             "name": ev.get("name", ""), "color": ev.get("color")}
            if ev else None)
        out["enum_options"] = [
            {"gid": o["gid"], "resource_type": "enum_option",
             "name": o.get("name", ""), "color": o.get("color")}
            for o in defn.get("enum_options", [])]
    elif t == "multi_enum":
        wanted = list(cf.get("multi_enum_value_gids", []))
        opts = [o for o in defn.get("enum_options", [])
                if o.get("gid") in wanted]
        out["multi_enum_values"] = [
            {"gid": o["gid"], "resource_type": "enum_option",
             "name": o.get("name", ""), "color": o.get("color")}
            for o in opts]
        out["enum_options"] = [
            {"gid": o["gid"], "resource_type": "enum_option",
             "name": o.get("name", ""), "color": o.get("color")}
            for o in defn.get("enum_options", [])]
    elif t == "date":
        out["date_value"] = cf.get("date_value")
    return out


def _task_view(state: dict, gid: str) -> dict | None:
    t = state["tasks"].get(gid)
    if not t:
        return None
    assignee_gid = t.get("assignee_gid")
    parent_gid = t.get("parent_gid")
    workspace_gid = t.get("workspace_gid")
    project_gids = list(t.get("project_gids", []))
    memberships = []
    for m in t.get("memberships", []):
        pgid = m.get("project_gid")
        sgid = m.get("section_gid")
        memberships.append({
            "project": ({"gid": pgid, "resource_type": "project",
                         "name": state["projects"].get(
                             pgid, {}).get("name", "")}
                        if pgid else None),
            "section": ({"gid": sgid, "resource_type": "section",
                         "name": state["sections"].get(
                             sgid, {}).get("name", "")}
                        if sgid else None),
        })
    tag_gids = list(t.get("tag_gids", []))
    follower_gids = list(t.get("follower_gids", []))
    return {
        "gid": t["gid"],
        "resource_type": "task",
        "name": t.get("name", ""),
        "notes": t.get("notes", ""),
        "html_notes": t.get("html_notes", ""),
        "completed": bool(t.get("completed", False)),
        "completed_at": t.get("completed_at"),
        "completed_by": (
            {"gid": t["completed_by_gid"], "resource_type": "user",
             "name": state["users"].get(
                 t["completed_by_gid"], {}).get("name", "")}
            if t.get("completed_by_gid") else None),
        "due_on": t.get("due_on"),
        "due_at": t.get("due_at"),
        "start_on": t.get("start_on"),
        "start_at": t.get("start_at"),
        "created_at": t.get("created_at"),
        "modified_at": t.get("modified_at"),
        "assignee": ({"gid": assignee_gid, "resource_type": "user",
                      "name": state["users"].get(
                          assignee_gid, {}).get("name", "")}
                     if assignee_gid else None),
        "assignee_status": t.get("assignee_status", "inbox"),
        "parent": ({"gid": parent_gid, "resource_type": "task",
                    "name": state["tasks"].get(parent_gid, {}).get("name", "")}
                   if parent_gid else None),
        "workspace": ({"gid": workspace_gid, "resource_type": "workspace",
                       "name": state["workspaces"].get(
                           workspace_gid, {}).get("name", "")}
                      if workspace_gid else None),
        "projects": [
            {"gid": pgid, "resource_type": "project",
             "name": state["projects"].get(pgid, {}).get("name", "")}
            for pgid in project_gids if pgid in state["projects"]
        ],
        "memberships": memberships,
        "tags": [
            {"gid": tgid, "resource_type": "tag",
             "name": state["tags"].get(tgid, {}).get("name", "")}
            for tgid in tag_gids if tgid in state["tags"]
        ],
        "followers": [
            {"gid": uid, "resource_type": "user",
             "name": state["users"].get(uid, {}).get("name", "")}
            for uid in follower_gids if uid in state["users"]
        ],
        "num_subtasks": sum(
            1 for tt in state["tasks"].values()
            if tt.get("parent_gid") == gid),
        "custom_fields": [_custom_field_value_view(state, cf)
                          for cf in t.get("custom_fields", [])],
        "permalink_url": (
            f"https://app.asana.com/0/0/{t['gid']}/f"),
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _paginate(items: list, limit: int | None,
              offset: str | None,
              path: str) -> dict:
    """Build an Asana paginated envelope.

    Real Asana uses opaque offset cursors; we treat the offset as the
    gid of the last item from the previous page."""
    limit = max(1, min(int(limit or 100), 100))
    start = 0
    if offset:
        for i, it in enumerate(items):
            if isinstance(it, dict) and it.get("gid") == offset:
                start = i + 1
                break
    page = items[start: start + limit]
    has_next = (start + limit) < len(items)
    next_page = None
    if has_next and page:
        last_gid = page[-1].get("gid") if isinstance(page[-1], dict) else None
        if last_gid:
            next_page = {
                "offset": last_gid,
                "path": f"{path}?offset={last_gid}",
                "uri": f"https://app.asana.com/api/1.0{path}?offset={last_gid}",
            }
    return {"data": page, "next_page": next_page}


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("asana-mock")


# ===========================================================================
# Workspaces
# ===========================================================================

@mcp.tool(name="list_workspaces")
def list_workspaces(opt_fields: str | None = None,
                    limit: int | None = None,
                    offset: str | None = None) -> dict:
    """Asana REST: GET /workspaces

    Returns a paginated list of workspaces the authenticated user is a
    member of. Default representation is compact (`gid`,
    `resource_type`, `name`).
    """
    with _lock():
        s = _load_state()
        rows = [_apply_opt_fields(_workspace_view(s, gid), opt_fields)
                if opt_fields else _compact(s["workspaces"][gid])
                for gid in s["workspaces"]]
        env = _paginate(rows, limit, offset, "/workspaces")
        _record(s, "list_workspaces", count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="get_workspace")
def get_workspace(workspaceGid: str,
                  opt_fields: str | None = None) -> dict:
    """Asana REST: GET /workspaces/{workspace_gid}"""
    with _lock():
        s = _load_state()
        w = _workspace_view(s, workspaceGid)
        _record(s, "get_workspace", workspace=workspaceGid,
                result="ok" if w else "not_found")
        _save_state(s)
        if not w:
            return _not_found("workspace", workspaceGid)
        return {"data": _apply_opt_fields(w, opt_fields)}


# ===========================================================================
# Users
# ===========================================================================

@mcp.tool(name="list_users")
def list_users(workspace: str | None = None,
               team: str | None = None,
               opt_fields: str | None = None,
               limit: int | None = None,
               offset: str | None = None) -> dict:
    """Asana REST: GET /users (also /workspaces/{w}/users when
    `workspace` is supplied). Returns a paginated list of users.
    `team` filters to users in a specific team."""
    with _lock():
        s = _load_state()
        rows: list[dict] = []
        for uid, u in s["users"].items():
            if workspace and workspace not in u.get("workspaces", []):
                continue
            if team:
                team_obj = s["teams"].get(team, {})
                if uid not in team_obj.get("member_gids", []):
                    continue
            full = _user_view(s, uid)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset,
                        f"/workspaces/{workspace}/users"
                        if workspace else "/users")
        _record(s, "list_users", workspace=workspace, team=team,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="get_user")
def get_user(userGid: str,
             opt_fields: str | None = None) -> dict:
    """Asana REST: GET /users/{user_gid}. `userGid` accepts `"me"`
    (the authenticated user)."""
    with _lock():
        s = _load_state()
        if userGid == "me":
            u = _user_view(s, s["me"]["gid"])
        else:
            u = _user_view(s, userGid)
        _record(s, "get_user", user=userGid,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _not_found("user", userGid)
        return {"data": _apply_opt_fields(u, opt_fields)}


@mcp.tool(name="get_me")
def get_me(opt_fields: str | None = None) -> dict:
    """Asana REST: GET /users/me. Returns the current authenticated
    user."""
    with _lock():
        s = _load_state()
        u = _user_view(s, s["me"]["gid"])
        _record(s, "get_me", user=s["me"]["gid"])
        _save_state(s)
        return {"data": _apply_opt_fields(u, opt_fields)}


# ===========================================================================
# Teams
# ===========================================================================

@mcp.tool(name="list_teams")
def list_teams(workspaceGid: str,
               opt_fields: str | None = None,
               limit: int | None = None,
               offset: str | None = None) -> dict:
    """Asana REST: GET /workspaces/{workspace_gid}/teams"""
    with _lock():
        s = _load_state()
        rows: list[dict] = []
        for tid, t in s["teams"].items():
            org = t.get("organization", {}) or {}
            if org.get("gid") != workspaceGid:
                continue
            full = _team_view(s, tid)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset,
                        f"/workspaces/{workspaceGid}/teams")
        _record(s, "list_teams", workspace=workspaceGid,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="get_team")
def get_team(teamGid: str,
             opt_fields: str | None = None) -> dict:
    """Asana REST: GET /teams/{team_gid}"""
    with _lock():
        s = _load_state()
        t = _team_view(s, teamGid)
        _record(s, "get_team", team=teamGid,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _not_found("team", teamGid)
        return {"data": _apply_opt_fields(t, opt_fields)}


# ===========================================================================
# Projects
# ===========================================================================

@mcp.tool(name="list_projects")
def list_projects(workspace: str | None = None,
                  team: str | None = None,
                  archived: bool | None = None,
                  opt_fields: str | None = None,
                  limit: int | None = None,
                  offset: str | None = None) -> dict:
    """Asana REST: GET /projects. Filters: `workspace`, `team`,
    `archived` (bool)."""
    with _lock():
        s = _load_state()
        rows: list[dict] = []
        for pid, p in s["projects"].items():
            if workspace and p.get("workspace_gid") != workspace:
                continue
            if team and p.get("team_gid") != team:
                continue
            if archived is not None and bool(p.get("archived",
                                                   False)) != bool(archived):
                continue
            full = _project_view(s, pid)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset, "/projects")
        _record(s, "list_projects", workspace=workspace, team=team,
                archived=archived, count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="get_project")
def get_project(projectGid: str,
                opt_fields: str | None = None) -> dict:
    """Asana REST: GET /projects/{project_gid}"""
    with _lock():
        s = _load_state()
        p = _project_view(s, projectGid)
        _record(s, "get_project", project=projectGid,
                result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            return _not_found("project", projectGid)
        return {"data": _apply_opt_fields(p, opt_fields)}


@mcp.tool(name="create_project")
def create_project(workspaceGid: str | None = None,
                   name: str | None = None,
                   team: str | None = None,
                   notes: str | None = None,
                   color: str | None = None,
                   archived: bool = False,
                   opt_fields: str | None = None) -> dict:
    """Asana REST: POST /projects (or
    POST /workspaces/{workspace_gid}/projects).

    Required: `workspaceGid` and `name`. `team` is required when the
    workspace is an organization (real Asana enforces this; the mock
    accepts no team for either workspace type)."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_project", result="missing_name")
            _save_state(s)
            return _err(400, "name is required.")
        if not workspaceGid or workspaceGid not in s["workspaces"]:
            _record(s, "create_project", result="invalid_workspace",
                    workspace=workspaceGid)
            _save_state(s)
            return _not_found("workspace", workspaceGid or "")
        if team is not None and team not in s["teams"]:
            _record(s, "create_project", result="invalid_team",
                    team=team)
            _save_state(s)
            return _not_found("team", team)
        gid = _next_gid(s)
        now = _now()
        proj = {
            "gid": gid,
            "resource_type": "project",
            "name": name,
            "notes": notes or "",
            "html_notes": f"<body>{notes or ''}</body>" if notes else "",
            "archived": bool(archived),
            "color": color,
            "workspace_gid": workspaceGid,
            "team_gid": team,
            "owner_gid": s["me"]["gid"],
            "member_gids": [s["me"]["gid"]],
            "follower_gids": [s["me"]["gid"]],
            "created_at": now,
            "modified_at": now,
            "due_date": None,
            "due_on": None,
            "start_on": None,
            "public": True,
        }
        s["projects"][gid] = proj
        # Default sections: every Asana project starts with an
        # "Untitled section" board column.
        section_gid = _next_gid(s)
        s["sections"][section_gid] = {
            "gid": section_gid,
            "resource_type": "section",
            "name": "Untitled section",
            "project_gid": gid,
            "created_at": now,
        }
        s["section_tasks"].setdefault(section_gid, [])
        proj["default_section_gid"] = section_gid
        _record(s, "create_project", project=gid, name=name,
                workspace=workspaceGid, team=team)
        _save_state(s)
        full = _project_view(s, gid)
        return {"data": _apply_opt_fields(full, opt_fields)}


@mcp.tool(name="update_project")
def update_project(projectGid: str,
                   name: str | None = None,
                   notes: str | None = None,
                   archived: bool | None = None,
                   color: str | None = None,
                   opt_fields: str | None = None) -> dict:
    """Asana REST: PUT /projects/{project_gid}"""
    with _lock():
        s = _load_state()
        p = s["projects"].get(projectGid)
        if not p:
            _record(s, "update_project", project=projectGid,
                    result="not_found")
            _save_state(s)
            return _not_found("project", projectGid)
        if name is not None:
            p["name"] = name
        if notes is not None:
            p["notes"] = notes
            p["html_notes"] = f"<body>{notes}</body>"
        if archived is not None:
            p["archived"] = bool(archived)
        if color is not None:
            p["color"] = color
        p["modified_at"] = _now()
        _record(s, "update_project", project=projectGid,
                fields=[f for f, v in
                        (("name", name), ("notes", notes),
                         ("archived", archived), ("color", color))
                        if v is not None])
        _save_state(s)
        full = _project_view(s, projectGid)
        return {"data": _apply_opt_fields(full, opt_fields)}


# ===========================================================================
# Sections
# ===========================================================================

@mcp.tool(name="list_sections")
def list_sections(projectGid: str,
                  opt_fields: str | None = None,
                  limit: int | None = None,
                  offset: str | None = None) -> dict:
    """Asana REST: GET /projects/{project_gid}/sections"""
    with _lock():
        s = _load_state()
        if projectGid not in s["projects"]:
            _record(s, "list_sections", project=projectGid,
                    result="not_found")
            _save_state(s)
            return _not_found("project", projectGid)
        rows: list[dict] = []
        for sid, sec in s["sections"].items():
            if sec.get("project_gid") != projectGid:
                continue
            full = _section_view(s, sid)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset,
                        f"/projects/{projectGid}/sections")
        _record(s, "list_sections", project=projectGid,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="create_section")
def create_section(projectGid: str,
                   name: str,
                   opt_fields: str | None = None) -> dict:
    """Asana REST: POST /projects/{project_gid}/sections"""
    with _lock():
        s = _load_state()
        if projectGid not in s["projects"]:
            _record(s, "create_section", project=projectGid,
                    result="not_found")
            _save_state(s)
            return _not_found("project", projectGid)
        if not name:
            _record(s, "create_section", result="missing_name")
            _save_state(s)
            return _err(400, "name is required.")
        gid = _next_gid(s)
        s["sections"][gid] = {
            "gid": gid,
            "resource_type": "section",
            "name": name,
            "project_gid": projectGid,
            "created_at": _now(),
        }
        s["section_tasks"].setdefault(gid, [])
        _record(s, "create_section", project=projectGid, section=gid,
                name=name)
        _save_state(s)
        full = _section_view(s, gid)
        return {"data": _apply_opt_fields(full, opt_fields)}


# ===========================================================================
# Tasks
# ===========================================================================

def _resolve_task(state: dict, ref: str) -> dict | None:
    if not ref:
        return None
    return state["tasks"].get(ref)


def _default_section_for_project(state: dict,
                                  project_gid: str) -> str | None:
    """Return the gid of the default "Untitled section" for a project,
    creating it if necessary."""
    p = state["projects"].get(project_gid)
    if not p:
        return None
    sgid = p.get("default_section_gid")
    if sgid and sgid in state["sections"]:
        return sgid
    # Fall back: first section in the project
    for sid, sec in state["sections"].items():
        if sec.get("project_gid") == project_gid:
            p["default_section_gid"] = sid
            return sid
    return None


@mcp.tool(name="list_tasks")
def list_tasks(project: str | None = None,
               section: str | None = None,
               assignee: str | None = None,
               workspace: str | None = None,
               completed_since: str | None = None,
               modified_since: str | None = None,
               opt_fields: str | None = None,
               limit: int | None = None,
               offset: str | None = None) -> dict:
    """Asana REST: GET /tasks. Requires one of:
      - `project`
      - `section`
      - `assignee` + `workspace`

    Filters: `completed_since` (`"now"` returns only incomplete tasks
    or those completed after that time), `modified_since` (ISO 8601
    timestamp). Pagination via `limit`/`offset` cursors."""
    with _lock():
        s = _load_state()
        if not project and not section and not (assignee and workspace):
            _record(s, "list_tasks", result="missing_filter")
            _save_state(s)
            return _err(400,
                        "You must specify one of project, section, "
                        "or assignee+workspace.")
        rows: list[dict] = []
        for tid, t in s["tasks"].items():
            if project and project not in t.get("project_gids", []):
                continue
            if section:
                if section not in [m.get("section_gid")
                                   for m in t.get("memberships", [])]:
                    continue
            if assignee and t.get("assignee_gid") != assignee:
                continue
            if workspace and t.get("workspace_gid") != workspace:
                continue
            if completed_since == "now":
                if t.get("completed"):
                    continue
            elif completed_since:
                # tasks completed_at > completed_since OR incomplete
                if t.get("completed") and (t.get("completed_at") or "") <= completed_since:
                    continue
            if modified_since and (t.get("modified_at") or "") <= modified_since:
                continue
            full = _task_view(s, tid)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset, "/tasks")
        _record(s, "list_tasks", project=project, section=section,
                assignee=assignee, workspace=workspace,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="get_task")
def get_task(taskGid: str,
             opt_fields: str | None = None) -> dict:
    """Asana REST: GET /tasks/{task_gid}. Returns the full task
    representation by default."""
    with _lock():
        s = _load_state()
        t = _task_view(s, taskGid)
        _record(s, "get_task", task=taskGid,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _not_found("task", taskGid)
        return {"data": _apply_opt_fields(t, opt_fields)}


@mcp.tool(name="create_task")
def create_task(name: str | None = None,
                notes: str | None = None,
                projects: list[str] | None = None,
                assignee: str | None = None,
                due_on: str | None = None,
                due_at: str | None = None,
                completed: bool = False,
                tags: list[str] | None = None,
                parent: str | None = None,
                workspace: str | None = None,
                memberships: list[dict] | None = None,
                custom_fields: dict | None = None,
                opt_fields: str | None = None) -> dict:
    """Asana REST: POST /tasks.

    A new task must specify either `workspace` (top-level, for tasks
    not in a project) OR at least one `projects` entry. When
    `projects` is supplied the task is automatically inserted into
    each project's default section, unless `memberships` overrides
    section placement.

    `tags`, `assignee`, and `parent` must reference existing entities;
    otherwise a 404 error envelope is returned.

    `custom_fields` is a dict of `{custom_field_gid: value}` where
    value is the raw value (string / number / enum_option_gid /
    list of enum_option_gids / date string)."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_task", result="missing_name")
            _save_state(s)
            return _err(400, "name is required.")
        project_gids = list(projects or [])
        # validate referenced entities
        for pgid in project_gids:
            if pgid not in s["projects"]:
                _record(s, "create_task", result="invalid_project",
                        project=pgid)
                _save_state(s)
                return _not_found("project", pgid)
        ws_gid = workspace
        if not ws_gid and project_gids:
            ws_gid = s["projects"][project_gids[0]].get("workspace_gid")
        if not ws_gid:
            _record(s, "create_task", result="missing_workspace")
            _save_state(s)
            return _err(400,
                        "You must specify workspace or at least one "
                        "project.")
        if ws_gid not in s["workspaces"]:
            _record(s, "create_task", result="invalid_workspace",
                    workspace=ws_gid)
            _save_state(s)
            return _not_found("workspace", ws_gid)
        if assignee and assignee not in s["users"]:
            _record(s, "create_task", result="invalid_assignee",
                    user=assignee)
            _save_state(s)
            return _not_found("user", assignee)
        if parent and parent not in s["tasks"]:
            _record(s, "create_task", result="invalid_parent",
                    parent=parent)
            _save_state(s)
            return _not_found("task", parent)
        tag_gids = list(tags or [])
        for tg in tag_gids:
            if tg not in s["tags"]:
                _record(s, "create_task", result="invalid_tag",
                        tag=tg)
                _save_state(s)
                return _not_found("tag", tg)

        gid = _next_gid(s)
        now = _now()

        # Build memberships: explicit overrides default-section
        # placement, otherwise each `projects` entry gets the
        # project's default section.
        memberships_resolved: list[dict] = []
        if memberships:
            for m in memberships:
                pgid = m.get("project")
                sgid = m.get("section")
                if pgid and pgid not in s["projects"]:
                    _record(s, "create_task", result="invalid_project",
                            project=pgid)
                    _save_state(s)
                    return _not_found("project", pgid)
                if sgid and sgid not in s["sections"]:
                    _record(s, "create_task", result="invalid_section",
                            section=sgid)
                    _save_state(s)
                    return _not_found("section", sgid)
                if pgid and not sgid:
                    sgid = _default_section_for_project(s, pgid)
                memberships_resolved.append(
                    {"project_gid": pgid, "section_gid": sgid})
                if pgid and pgid not in project_gids:
                    project_gids.append(pgid)
        else:
            for pgid in project_gids:
                sgid = _default_section_for_project(s, pgid)
                memberships_resolved.append(
                    {"project_gid": pgid, "section_gid": sgid})

        # custom_fields: dict gid -> value
        cf_records: list[dict] = []
        for defn_gid, raw_val in (custom_fields or {}).items():
            defn = s["custom_fields"].get(defn_gid)
            if not defn:
                continue
            entry: dict[str, Any] = {"definition_gid": defn_gid}
            t_kind = defn.get("type", "text")
            if t_kind == "text":
                entry["text_value"] = (str(raw_val)
                                       if raw_val is not None else None)
            elif t_kind == "number":
                try:
                    entry["number_value"] = (float(raw_val)
                                             if raw_val is not None else None)
                except (TypeError, ValueError):
                    entry["number_value"] = None
            elif t_kind == "enum":
                entry["enum_value_gid"] = (str(raw_val)
                                           if raw_val is not None else None)
            elif t_kind == "multi_enum":
                entry["multi_enum_value_gids"] = [str(v)
                                                  for v in (raw_val or [])]
            elif t_kind == "date":
                if isinstance(raw_val, dict):
                    entry["date_value"] = raw_val
                else:
                    entry["date_value"] = (
                        {"date": str(raw_val)} if raw_val else None)
            cf_records.append(entry)

        task = {
            "gid": gid,
            "resource_type": "task",
            "name": name,
            "notes": notes or "",
            "html_notes": f"<body>{notes or ''}</body>" if notes else "",
            "completed": bool(completed),
            "completed_at": now if completed else None,
            "completed_by_gid": s["me"]["gid"] if completed else None,
            "due_on": due_on,
            "due_at": due_at,
            "start_on": None,
            "start_at": None,
            "assignee_gid": assignee,
            "assignee_status": "inbox",
            "parent_gid": parent,
            "workspace_gid": ws_gid,
            "project_gids": project_gids,
            "memberships": memberships_resolved,
            "tag_gids": tag_gids,
            "follower_gids": [s["me"]["gid"]] + (
                [assignee] if assignee and assignee != s["me"]["gid"]
                else []),
            "custom_fields": cf_records,
            "created_at": now,
            "modified_at": now,
        }
        s["tasks"][gid] = task
        # Update per-section task ordering
        for m in memberships_resolved:
            sgid = m.get("section_gid")
            if sgid:
                s["section_tasks"].setdefault(sgid, []).append(gid)
        _record(s, "create_task", task=gid, name=name,
                projects=project_gids, workspace=ws_gid,
                assignee=assignee)
        _save_state(s)
        full = _task_view(s, gid)
        return {"data": _apply_opt_fields(full, opt_fields)}


@mcp.tool(name="update_task")
def update_task(taskGid: str,
                name: str | None = None,
                notes: str | None = None,
                assignee: str | None = None,
                due_on: str | None = None,
                due_at: str | None = None,
                completed: bool | None = None,
                opt_fields: str | None = None) -> dict:
    """Asana REST: PUT /tasks/{task_gid}. Only the listed fields are
    accepted; pass `assignee=None` semantics by sending an empty
    string (the mock leaves the assignee untouched when the param is
    omitted entirely)."""
    with _lock():
        s = _load_state()
        t = s["tasks"].get(taskGid)
        if not t:
            _record(s, "update_task", task=taskGid, result="not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        if assignee is not None and assignee != "" and assignee not in s["users"]:
            _record(s, "update_task", task=taskGid,
                    result="invalid_assignee", user=assignee)
            _save_state(s)
            return _not_found("user", assignee)
        if name is not None:
            t["name"] = name
        if notes is not None:
            t["notes"] = notes
            t["html_notes"] = f"<body>{notes}</body>"
        if assignee is not None:
            t["assignee_gid"] = assignee or None
        if due_on is not None:
            t["due_on"] = due_on or None
        if due_at is not None:
            t["due_at"] = due_at or None
        if completed is not None:
            was = bool(t.get("completed", False))
            t["completed"] = bool(completed)
            if completed and not was:
                t["completed_at"] = _now()
                t["completed_by_gid"] = s["me"]["gid"]
            elif not completed:
                t["completed_at"] = None
                t["completed_by_gid"] = None
        t["modified_at"] = _now()
        _record(s, "update_task", task=taskGid,
                fields=[k for k, v in
                        (("name", name), ("notes", notes),
                         ("assignee", assignee),
                         ("due_on", due_on), ("due_at", due_at),
                         ("completed", completed))
                        if v is not None])
        _save_state(s)
        full = _task_view(s, taskGid)
        return {"data": _apply_opt_fields(full, opt_fields)}


@mcp.tool(name="delete_task")
def delete_task(taskGid: str) -> dict:
    """Asana REST: DELETE /tasks/{task_gid}. Returns `{"data": {}}`
    on success (Asana returns an empty data object)."""
    with _lock():
        s = _load_state()
        t = s["tasks"].get(taskGid)
        if not t:
            _record(s, "delete_task", task=taskGid, result="not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        # Remove from any section ordering lists
        for sgid, ordering in s["section_tasks"].items():
            if taskGid in ordering:
                s["section_tasks"][sgid] = [
                    x for x in ordering if x != taskGid]
        # Drop the task; drop its stories
        del s["tasks"][taskGid]
        s["stories"] = {sid: st for sid, st in s["stories"].items()
                        if st.get("task_gid") != taskGid}
        _record(s, "delete_task", task=taskGid)
        _save_state(s)
        return {"data": {}}


@mcp.tool(name="search_tasks")
def search_tasks(workspaceGid: str,
                 text: str | None = None,
                 assignee_any: list[str] | None = None,
                 projects_any: list[str] | None = None,
                 completed: bool | None = None,
                 sort_by: str | None = None,
                 sort_ascending: bool = False,
                 opt_fields: str | None = None,
                 limit: int | None = None,
                 offset: str | None = None) -> dict:
    """Asana REST: GET /workspaces/{workspace_gid}/tasks/search.

    Supported filters (subset of the real surface):
      - `text`        substring match against name / notes
      - `assignee_any` list of user gids — task matches if assignee
                       is in the list
      - `projects_any` list of project gids — task matches if any
                       project is in the list
      - `completed`   bool — filter by completion status
      - `sort_by`     `"created_at"` / `"modified_at"` /
                       `"completed_at"` / `"due_date"`
      - `sort_ascending` direction (default: descending)

    Note: real Asana's task-search endpoint does NOT use offset-based
    pagination — it caps at 100 results and returns no `next_page`.
    The mock follows that contract."""
    with _lock():
        s = _load_state()
        if workspaceGid not in s["workspaces"]:
            _record(s, "search_tasks", workspace=workspaceGid,
                    result="not_found")
            _save_state(s)
            return _not_found("workspace", workspaceGid)
        rows: list[dict] = []
        text_lc = (text or "").lower().strip()
        assignee_set = set(assignee_any or [])
        project_set = set(projects_any or [])
        for tid, t in s["tasks"].items():
            if t.get("workspace_gid") != workspaceGid:
                continue
            if text_lc:
                hay = " ".join([
                    (t.get("name") or ""), (t.get("notes") or "")
                ]).lower()
                if text_lc not in hay:
                    continue
            if assignee_set and t.get("assignee_gid") not in assignee_set:
                continue
            if project_set and not (project_set
                                     & set(t.get("project_gids", []))):
                continue
            if completed is not None and bool(t.get("completed",
                                                    False)) != bool(completed):
                continue
            rows.append(t)
        # Sort
        sort_key = sort_by if sort_by in ("created_at", "modified_at",
                                          "completed_at", "due_date",
                                          "due_on") else "modified_at"
        if sort_key == "due_date":
            sort_key = "due_on"
        rows.sort(key=lambda r: (r.get(sort_key) or ""),
                  reverse=(not sort_ascending))
        rows = rows[:100]
        out_rows = [(
            _apply_opt_fields(_task_view(s, r["gid"]), opt_fields)
            if opt_fields else _compact(r)) for r in rows]
        _record(s, "search_tasks", workspace=workspaceGid,
                text=text, count=len(out_rows))
        _save_state(s)
        return {"data": out_rows, "next_page": None}


@mcp.tool(name="add_task_to_section")
def add_task_to_section(sectionGid: str,
                        taskGid: str,
                        insert_before: str | None = None,
                        insert_after: str | None = None) -> dict:
    """Asana REST: POST /sections/{section_gid}/addTask.

    Moves a task into the named section. `insert_before` / `insert_after`
    accept other task gids in the section to control ordering. Returns
    `{"data": {}}` on success."""
    with _lock():
        s = _load_state()
        sec = s["sections"].get(sectionGid)
        if not sec:
            _record(s, "add_task_to_section", section=sectionGid,
                    result="section_not_found")
            _save_state(s)
            return _not_found("section", sectionGid)
        t = s["tasks"].get(taskGid)
        if not t:
            _record(s, "add_task_to_section", task=taskGid,
                    result="task_not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        project_gid = sec.get("project_gid")
        # Remove the task from any other section of the SAME project
        new_memberships = []
        for m in t.get("memberships", []):
            if m.get("project_gid") == project_gid:
                old_sgid = m.get("section_gid")
                if old_sgid and old_sgid in s["section_tasks"]:
                    s["section_tasks"][old_sgid] = [
                        x for x in s["section_tasks"][old_sgid]
                        if x != taskGid]
                continue
            new_memberships.append(m)
        new_memberships.append({"project_gid": project_gid,
                                "section_gid": sectionGid})
        t["memberships"] = new_memberships
        # Make sure the task lists the section's project
        if project_gid and project_gid not in t.get("project_gids", []):
            t.setdefault("project_gids", []).append(project_gid)
        # Insert into the section's ordering
        ordering = s["section_tasks"].setdefault(sectionGid, [])
        ordering = [x for x in ordering if x != taskGid]
        idx = len(ordering)
        if insert_before and insert_before in ordering:
            idx = ordering.index(insert_before)
        elif insert_after and insert_after in ordering:
            idx = ordering.index(insert_after) + 1
        ordering.insert(idx, taskGid)
        s["section_tasks"][sectionGid] = ordering
        t["modified_at"] = _now()
        _record(s, "add_task_to_section", task=taskGid,
                section=sectionGid)
        _save_state(s)
        return {"data": {}}


@mcp.tool(name="add_followers_to_task")
def add_followers_to_task(taskGid: str,
                          followers: list[str],
                          opt_fields: str | None = None) -> dict:
    """Asana REST: POST /tasks/{task_gid}/addFollowers."""
    with _lock():
        s = _load_state()
        t = s["tasks"].get(taskGid)
        if not t:
            _record(s, "add_followers_to_task", task=taskGid,
                    result="not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        added: list[str] = []
        for uid in (followers or []):
            if uid not in s["users"]:
                _record(s, "add_followers_to_task", task=taskGid,
                        result="invalid_user", user=uid)
                _save_state(s)
                return _not_found("user", uid)
            if uid not in t.get("follower_gids", []):
                t.setdefault("follower_gids", []).append(uid)
                added.append(uid)
        t["modified_at"] = _now()
        _record(s, "add_followers_to_task", task=taskGid,
                added=added)
        _save_state(s)
        full = _task_view(s, taskGid)
        return {"data": _apply_opt_fields(full, opt_fields)}


# ===========================================================================
# Stories (comments + activity)
# ===========================================================================

@mcp.tool(name="list_stories")
def list_stories(taskGid: str,
                 opt_fields: str | None = None,
                 limit: int | None = None,
                 offset: str | None = None) -> dict:
    """Asana REST: GET /tasks/{task_gid}/stories. Returns the task's
    stories — comments and system activity events — in
    chronological order."""
    with _lock():
        s = _load_state()
        if taskGid not in s["tasks"]:
            _record(s, "list_stories", task=taskGid, result="not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        items = [st for st in s["stories"].values()
                 if st.get("task_gid") == taskGid]
        items.sort(key=lambda c: c.get("created_at") or "")
        rows: list[dict] = []
        for st in items:
            full = _story_view(s, st["gid"])
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(
                            full, extra_fields=("text", "type",
                                                "created_at")))
        env = _paginate(rows, limit, offset,
                        f"/tasks/{taskGid}/stories")
        _record(s, "list_stories", task=taskGid,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="create_story")
def create_story(taskGid: str,
                 text: str | None = None,
                 html_text: str | None = None,
                 opt_fields: str | None = None) -> dict:
    """Asana REST: POST /tasks/{task_gid}/stories. Creates a comment
    story on the task."""
    with _lock():
        s = _load_state()
        if taskGid not in s["tasks"]:
            _record(s, "create_story", task=taskGid, result="not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        if not text and not html_text:
            _record(s, "create_story", result="missing_text")
            _save_state(s)
            return _err(400, "text or html_text is required.")
        gid = _next_gid(s)
        now = _now()
        story = {
            "gid": gid,
            "resource_type": "story",
            "type": "comment",
            "text": text or "",
            "html_text": html_text or (f"<body>{text}</body>" if text else ""),
            "task_gid": taskGid,
            "created_by_gid": s["me"]["gid"],
            "created_at": now,
        }
        s["stories"][gid] = story
        # Bump the parent task's modified_at
        s["tasks"][taskGid]["modified_at"] = now
        _record(s, "create_story", task=taskGid, story=gid)
        _save_state(s)
        full = _story_view(s, gid)
        return {"data": _apply_opt_fields(full, opt_fields)}


# ===========================================================================
# Tags
# ===========================================================================

@mcp.tool(name="list_tags")
def list_tags(workspaceGid: str,
              opt_fields: str | None = None,
              limit: int | None = None,
              offset: str | None = None) -> dict:
    """Asana REST: GET /workspaces/{workspace_gid}/tags"""
    with _lock():
        s = _load_state()
        if workspaceGid not in s["workspaces"]:
            _record(s, "list_tags", workspace=workspaceGid,
                    result="not_found")
            _save_state(s)
            return _not_found("workspace", workspaceGid)
        rows: list[dict] = []
        for tg, tag in s["tags"].items():
            if tag.get("workspace_gid") != workspaceGid:
                continue
            full = _tag_view(s, tg)
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset,
                        f"/workspaces/{workspaceGid}/tags")
        _record(s, "list_tags", workspace=workspaceGid,
                count=len(env["data"]))
        _save_state(s)
        return env


@mcp.tool(name="create_tag")
def create_tag(workspaceGid: str,
               name: str,
               color: str | None = None,
               opt_fields: str | None = None) -> dict:
    """Asana REST: POST /workspaces/{workspace_gid}/tags"""
    with _lock():
        s = _load_state()
        if workspaceGid not in s["workspaces"]:
            _record(s, "create_tag", workspace=workspaceGid,
                    result="not_found")
            _save_state(s)
            return _not_found("workspace", workspaceGid)
        if not name:
            _record(s, "create_tag", result="missing_name")
            _save_state(s)
            return _err(400, "name is required.")
        gid = _next_gid(s)
        s["tags"][gid] = {
            "gid": gid,
            "resource_type": "tag",
            "name": name,
            "color": color,
            "workspace_gid": workspaceGid,
            "created_at": _now(),
        }
        _record(s, "create_tag", workspace=workspaceGid, tag=gid,
                name=name)
        _save_state(s)
        full = _tag_view(s, gid)
        return {"data": _apply_opt_fields(full, opt_fields)}


@mcp.tool(name="add_tag_to_task")
def add_tag_to_task(taskGid: str,
                    tag: str) -> dict:
    """Asana REST: POST /tasks/{task_gid}/addTag. Returns `{"data":
    {}}` on success."""
    with _lock():
        s = _load_state()
        t = s["tasks"].get(taskGid)
        if not t:
            _record(s, "add_tag_to_task", task=taskGid,
                    result="task_not_found")
            _save_state(s)
            return _not_found("task", taskGid)
        if tag not in s["tags"]:
            _record(s, "add_tag_to_task", tag=tag,
                    result="tag_not_found")
            _save_state(s)
            return _not_found("tag", tag)
        if tag not in t.get("tag_gids", []):
            t.setdefault("tag_gids", []).append(tag)
        t["modified_at"] = _now()
        _record(s, "add_tag_to_task", task=taskGid, tag=tag)
        _save_state(s)
        return {"data": {}}


# ===========================================================================
# Custom fields
# ===========================================================================

@mcp.tool(name="list_custom_fields")
def list_custom_fields(workspaceGid: str,
                       opt_fields: str | None = None,
                       limit: int | None = None,
                       offset: str | None = None) -> dict:
    """Asana REST: GET /workspaces/{workspace_gid}/custom_fields"""
    with _lock():
        s = _load_state()
        if workspaceGid not in s["workspaces"]:
            _record(s, "list_custom_fields", workspace=workspaceGid,
                    result="not_found")
            _save_state(s)
            return _not_found("workspace", workspaceGid)
        rows: list[dict] = []
        for cf_gid, cf in s["custom_fields"].items():
            if cf.get("workspace_gid") != workspaceGid:
                continue
            full = {
                "gid": cf_gid,
                "resource_type": "custom_field",
                "name": cf.get("name", ""),
                "type": cf.get("type", "text"),
                "description": cf.get("description", ""),
                "enum_options": [
                    {"gid": o["gid"], "resource_type": "enum_option",
                     "name": o.get("name", ""), "color": o.get("color")}
                    for o in cf.get("enum_options", [])],
            }
            rows.append(_apply_opt_fields(full, opt_fields)
                        if opt_fields else _compact(full))
        env = _paginate(rows, limit, offset,
                        f"/workspaces/{workspaceGid}/custom_fields")
        _record(s, "list_custom_fields", workspace=workspaceGid,
                count=len(env["data"]))
        _save_state(s)
        return env


# ===========================================================================
# Mock-only debug helpers
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state. Not part of the
    real Asana surface."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_workspace")
def mock_debug_seed_workspace(gid: str | None = None,
                              name: str = "Mock Workspace",
                              is_organization: bool = True,
                              email_domains: list[str] | None = None) -> dict:
    """Mock-only: insert / update a workspace fixture."""
    with _lock():
        s = _load_state()
        wgid = gid or _next_gid(s)
        s["workspaces"][wgid] = {
            "gid": wgid,
            "resource_type": "workspace",
            "name": name,
            "is_organization": bool(is_organization),
            "email_domains": list(email_domains or ["example.com"]),
        }
        # Make sure `me` is a member.
        me = s["me"]
        if wgid not in me.get("workspaces", []):
            me.setdefault("workspaces", []).append(wgid)
            s["users"][me["gid"]]["workspaces"] = me["workspaces"]
        _record(s, "debug_seed_workspace", workspace=wgid, name=name)
        _save_state(s)
        return {"data": _workspace_view(s, wgid)}


@mcp.tool(name="mock_debug_seed_user")
def mock_debug_seed_user(gid: str | None = None,
                         name: str = "Mock User",
                         email: str | None = None,
                         workspaces: list[str] | None = None) -> dict:
    """Mock-only: insert a workspace member."""
    with _lock():
        s = _load_state()
        ugid = gid or _next_gid(s)
        s["users"][ugid] = {
            "gid": ugid,
            "resource_type": "user",
            "name": name,
            "email": email or f"{name.lower().replace(' ', '.')}@example.com",
            "photo": None,
            "workspaces": list(workspaces or [_DEFAULT_WORKSPACE_GID]),
        }
        _record(s, "debug_seed_user", user=ugid, name=name)
        _save_state(s)
        return {"data": _user_view(s, ugid)}


@mcp.tool(name="mock_debug_seed_project")
def mock_debug_seed_project(gid: str | None = None,
                            name: str = "Mock Project",
                            workspace_gid: str | None = None,
                            team_gid: str | None = None,
                            owner_gid: str | None = None,
                            color: str | None = None,
                            archived: bool = False,
                            notes: str = "",
                            section_names: list[str] | None = None) -> dict:
    """Mock-only: insert a project fixture, optionally with named
    sections. The first section is set as the project's default
    section (where new tasks land when `create_task(projects=[...])`
    is called without explicit memberships)."""
    with _lock():
        s = _load_state()
        pgid = gid or _next_gid(s)
        now = _now()
        ws_gid = workspace_gid or _DEFAULT_WORKSPACE_GID
        s["projects"][pgid] = {
            "gid": pgid,
            "resource_type": "project",
            "name": name,
            "notes": notes,
            "html_notes": f"<body>{notes}</body>" if notes else "",
            "archived": bool(archived),
            "color": color,
            "workspace_gid": ws_gid,
            "team_gid": team_gid,
            "owner_gid": owner_gid or s["me"]["gid"],
            "member_gids": [s["me"]["gid"]],
            "follower_gids": [s["me"]["gid"]],
            "created_at": now,
            "modified_at": now,
            "due_date": None,
            "due_on": None,
            "start_on": None,
            "public": True,
        }
        names = list(section_names or ["Untitled section"])
        first_sgid: str | None = None
        for sname in names:
            sgid = _next_gid(s)
            s["sections"][sgid] = {
                "gid": sgid,
                "resource_type": "section",
                "name": sname,
                "project_gid": pgid,
                "created_at": now,
            }
            s["section_tasks"].setdefault(sgid, [])
            if first_sgid is None:
                first_sgid = sgid
        s["projects"][pgid]["default_section_gid"] = first_sgid
        _record(s, "debug_seed_project", project=pgid, name=name,
                workspace=ws_gid)
        _save_state(s)
        return {"data": _project_view(s, pgid)}


@mcp.tool(name="mock_debug_seed_section")
def mock_debug_seed_section(gid: str | None = None,
                            name: str = "New Section",
                            project_gid: str = "") -> dict:
    """Mock-only: insert a section into a project."""
    with _lock():
        s = _load_state()
        if project_gid not in s["projects"]:
            _record(s, "debug_seed_section",
                    project=project_gid, result="not_found")
            _save_state(s)
            return _not_found("project", project_gid)
        sgid = gid or _next_gid(s)
        s["sections"][sgid] = {
            "gid": sgid,
            "resource_type": "section",
            "name": name,
            "project_gid": project_gid,
            "created_at": _now(),
        }
        s["section_tasks"].setdefault(sgid, [])
        _record(s, "debug_seed_section", project=project_gid,
                section=sgid, name=name)
        _save_state(s)
        return {"data": _section_view(s, sgid)}


@mcp.tool(name="mock_debug_seed_task")
def mock_debug_seed_task(gid: str | None = None,
                         name: str = "Mock Task",
                         notes: str = "",
                         workspace_gid: str | None = None,
                         assignee_gid: str | None = None,
                         completed: bool = False,
                         due_on: str | None = None,
                         due_at: str | None = None,
                         project_gids: list[str] | None = None,
                         section_gid: str | None = None,
                         tag_gids: list[str] | None = None,
                         parent_gid: str | None = None,
                         created_at: str | None = None) -> dict:
    """Mock-only: insert a task fixture. If `section_gid` is provided
    the task's membership pins the matching project/section pair;
    otherwise each project in `project_gids` gets the project's
    default section."""
    with _lock():
        s = _load_state()
        tgid = gid or _next_gid(s)
        now = created_at or _now()
        project_gids = list(project_gids or [])
        ws_gid = workspace_gid
        if not ws_gid and project_gids:
            ws_gid = s["projects"].get(project_gids[0], {}).get(
                "workspace_gid", _DEFAULT_WORKSPACE_GID)
        ws_gid = ws_gid or _DEFAULT_WORKSPACE_GID

        memberships: list[dict] = []
        if section_gid:
            sec = s["sections"].get(section_gid)
            pgid = sec.get("project_gid") if sec else None
            memberships.append({"project_gid": pgid,
                                "section_gid": section_gid})
            if pgid and pgid not in project_gids:
                project_gids.append(pgid)
        else:
            for pgid in project_gids:
                sgid = _default_section_for_project(s, pgid)
                memberships.append({"project_gid": pgid,
                                    "section_gid": sgid})

        s["tasks"][tgid] = {
            "gid": tgid,
            "resource_type": "task",
            "name": name,
            "notes": notes,
            "html_notes": f"<body>{notes}</body>" if notes else "",
            "completed": bool(completed),
            "completed_at": now if completed else None,
            "completed_by_gid": s["me"]["gid"] if completed else None,
            "due_on": due_on,
            "due_at": due_at,
            "start_on": None,
            "start_at": None,
            "assignee_gid": assignee_gid,
            "assignee_status": "inbox",
            "parent_gid": parent_gid,
            "workspace_gid": ws_gid,
            "project_gids": project_gids,
            "memberships": memberships,
            "tag_gids": list(tag_gids or []),
            "follower_gids": [s["me"]["gid"]],
            "custom_fields": [],
            "created_at": now,
            "modified_at": now,
        }
        for m in memberships:
            sgid = m.get("section_gid")
            if sgid:
                s["section_tasks"].setdefault(sgid, []).append(tgid)
        _record(s, "debug_seed_task", task=tgid, name=name,
                projects=project_gids)
        _save_state(s)
        return {"data": _task_view(s, tgid)}


@mcp.tool(name="mock_debug_seed_tag")
def mock_debug_seed_tag(gid: str | None = None,
                        name: str = "Mock Tag",
                        workspace_gid: str | None = None,
                        color: str | None = None) -> dict:
    """Mock-only: insert a tag fixture."""
    with _lock():
        s = _load_state()
        tgid = gid or _next_gid(s)
        s["tags"][tgid] = {
            "gid": tgid,
            "resource_type": "tag",
            "name": name,
            "color": color,
            "workspace_gid": workspace_gid or _DEFAULT_WORKSPACE_GID,
            "created_at": _now(),
        }
        _record(s, "debug_seed_tag", tag=tgid, name=name)
        _save_state(s)
        return {"data": _tag_view(s, tgid)}


if __name__ == "__main__":
    mcp.run()
