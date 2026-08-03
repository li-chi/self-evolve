"""Vercel mock MCP server.

Mirrors the Vercel REST API surface (https://vercel.com/docs/rest-api).
Tool names match the camelCase Vercel REST operationIds (e.g.
`listProjects`, `getDeployment`, `createDeployment`), and tool
parameter names + response shapes match the real REST API.

Implemented operations (16 + 2 mock helpers):

  Projects
    listProjects, getProject, createProject, updateProject, deleteProject
  Deployments
    listDeployments, getDeployment, createDeployment, cancelDeployment,
    deleteDeployment, listDeploymentFiles
  Domains
    listProjectDomains, addProjectDomain, removeProjectDomain
  Environment variables
    listProjectEnv, createProjectEnv, deleteProjectEnv
  Teams
    listTeams, getTeam
  User
    getAuthUser

  Mock-only
    mock_debug_state, mock_debug_seed

State lives at `$VERCEL_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/vercel_mock`). Per-rollout isolation should clear the
state dir between rollouts. Optional `VERCEL_MOCK_SEED_PATH` preloads
state when no state.json exists yet.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace.

Errors follow Vercel REST shape:
    {"error": {"code": "not_found", "message": "..."}}

Vercel id formats:
    prj_<random> — project
    dpl_<random> — deployment
    dom_<random> — domain
    env_<random> — environment variable
    team_<random> — team
    user_<random> — user
    file_<random> — deployment file
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import random
import re
import string
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "VERCEL_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/vercel_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    user_id = "user_mockuser000000000000"
    return {
        "user": {
            "id": user_id,
            "uid": user_id,
            "username": "mockuser",
            "email": "mockuser@example.com",
            "name": "Mock User",
            "avatar": None,
            "defaultTeamId": None,
            "createdAt": _now_ms(),
        },
        "teams": {},          # team_id -> team object
        "projects": {},       # prj_id -> project object
        "deployments": {},    # dpl_id -> deployment object
        "domains": {},        # dom_id -> domain object (project domains)
        "env_vars": {},       # env_id -> env-var object
        "files": {},          # file_id -> file object {deploymentId, name, ...}
        "next_seq": {
            "prj": 1, "dpl": 1, "dom": 1, "env": 1,
            "team": 1, "user": 1, "file": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("VERCEL_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

_RAND_ALPHA = string.ascii_lowercase + string.digits


def _rand_suffix(n: int = 24) -> str:
    # Deterministic-ish: seed off the next_seq counter so tests stay stable
    return "".join(random.choices(_RAND_ALPHA, k=n))


def _new_id(state: dict, prefix: str, length: int = 24) -> str:
    n = state["next_seq"].get(prefix, 1)
    state["next_seq"][prefix] = n + 1
    # Pad sequence into the suffix so ids are unique and inspectable
    seq = f"{n:08d}"
    suffix = (seq + _rand_suffix(length - len(seq)))[:length]
    return f"{prefix}_{suffix}"


def _err(code: str, message: str) -> dict:
    """Return a Vercel-shaped error object."""
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Lookups / shared helpers
# ---------------------------------------------------------------------------

def _resolve_project(state: dict, ref: str) -> dict | None:
    """Resolve a project by id (prj_xxx) or by name."""
    if not ref:
        return None
    p = state["projects"].get(ref)
    if p:
        return p
    for proj in state["projects"].values():
        if proj.get("name") == ref:
            return proj
    return None


def _resolve_team(state: dict, ref: str) -> dict | None:
    if not ref:
        return None
    t = state["teams"].get(ref)
    if t:
        return t
    for team in state["teams"].values():
        if team.get("slug") == ref:
            return team
    return None


def _filter_team(items: list[dict], team_id: str | None) -> list[dict]:
    """Filter items by teamId scope. If team_id is None/empty, return
    personal-scope items (teamId is None). Otherwise return items
    matching team_id."""
    if not team_id:
        return [i for i in items if not i.get("teamId")]
    return [i for i in items if i.get("teamId") == team_id]


def _paginate(items: list[dict], limit: int, since: int | None,
              until: int | None, key: str = "createdAt") -> tuple[list, dict]:
    """Vercel-style pagination by createdAt epoch ms.

    Returns (page, pagination_obj). `until` is exclusive (older than),
    `since` is exclusive (newer than). Results are sorted newest first.
    """
    items = sorted(items, key=lambda x: x.get(key, 0), reverse=True)
    if since is not None:
        items = [i for i in items if i.get(key, 0) > since]
    if until is not None:
        items = [i for i in items if i.get(key, 0) < until]
    if limit <= 0:
        limit = 20
    if limit > 100:
        limit = 100
    page = items[:limit]
    next_until = page[-1].get(key) if len(items) > limit and page else None
    pagination = {
        "count": len(page),
        "next": next_until,
        "prev": page[0].get(key) if page else None,
    }
    return page, pagination


def _strip_private(obj: dict) -> dict:
    """Drop internal-only keys (those starting with _)."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("vercel-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

@mcp.tool(name="getAuthUser")
def get_auth_user() -> dict:
    """Vercel REST: GET /v2/user — retrieve the authenticated user."""
    with _lock():
        s = _load_state()
        _record(s, "getAuthUser")
        _save_state(s)
        return {"user": dict(s["user"])}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@mcp.tool(name="listTeams")
def list_teams(limit: int = 20, since: int | None = None,
               until: int | None = None) -> dict:
    """Vercel REST: GET /v2/teams — list teams the user belongs to."""
    with _lock():
        s = _load_state()
        teams = list(s["teams"].values())
        page, pagination = _paginate(teams, limit, since, until)
        _record(s, "listTeams", count=len(page))
        _save_state(s)
        return {"teams": [_strip_private(t) for t in page],
                "pagination": pagination}


@mcp.tool(name="getTeam")
def get_team(teamId: str) -> dict:
    """Vercel REST: GET /v2/teams/{teamId} — retrieve a team by id or slug."""
    with _lock():
        s = _load_state()
        team = _resolve_team(s, teamId)
        _record(s, "getTeam", teamId=teamId,
                result="ok" if team else "not_found")
        _save_state(s)
        if not team:
            return _err("not_found", f"Team not found: {teamId}")
        return _strip_private(team)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _make_project(state: dict, name: str, framework: str | None,
                  git_repository: dict | None, team_id: str | None,
                  env: list | None = None,
                  root_directory: str | None = None,
                  build_command: str | None = None,
                  dev_command: str | None = None,
                  output_directory: str | None = None,
                  install_command: str | None = None,
                  public_source: bool | None = None) -> dict:
    pid = _new_id(state, "prj")
    now = _now_ms()
    account_id = team_id or state["user"]["id"]
    proj = {
        "id": pid,
        "name": name,
        "accountId": account_id,
        "teamId": team_id,
        "createdAt": now,
        "updatedAt": now,
        "framework": framework,
        "gitRepository": git_repository,
        "rootDirectory": root_directory,
        "buildCommand": build_command,
        "devCommand": dev_command,
        "outputDirectory": output_directory,
        "installCommand": install_command,
        "publicSource": public_source,
        "nodeVersion": "20.x",
        "live": False,
        "env": list(env or []),
        "targets": {},
        "latestDeployments": [],
    }
    return proj


@mcp.tool(name="listProjects")
def list_projects(teamId: str | None = None,
                  search: str | None = None,
                  limit: int = 20,
                  since: int | None = None,
                  until: int | None = None) -> dict:
    """Vercel REST: GET /v9/projects — list projects, optionally scoped
    to a team and filtered by name substring."""
    with _lock():
        s = _load_state()
        projects = _filter_team(list(s["projects"].values()), teamId)
        if search:
            q = search.lower()
            projects = [p for p in projects if q in (p.get("name") or "").lower()]
        page, pagination = _paginate(projects, limit, since, until)
        _record(s, "listProjects", teamId=teamId, search=search,
                count=len(page))
        _save_state(s)
        return {"projects": [_strip_private(p) for p in page],
                "pagination": pagination}


@mcp.tool(name="getProject")
def get_project(idOrName: str, teamId: str | None = None) -> dict:
    """Vercel REST: GET /v9/projects/{idOrName} — retrieve a project by
    id or name."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        _record(s, "getProject", idOrName=idOrName, teamId=teamId,
                result="ok" if proj else "not_found")
        _save_state(s)
        if not proj:
            return _err("not_found", f"Project not found: {idOrName}")
        return _strip_private(proj)


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_.]{0,99}$")


@mcp.tool(name="createProject")
def create_project(name: str,
                   framework: str | None = None,
                   gitRepository: dict | None = None,
                   teamId: str | None = None,
                   environmentVariables: list | None = None,
                   rootDirectory: str | None = None,
                   buildCommand: str | None = None,
                   devCommand: str | None = None,
                   outputDirectory: str | None = None,
                   installCommand: str | None = None,
                   publicSource: bool | None = None) -> dict:
    """Vercel REST: POST /v10/projects — create a new project."""
    with _lock():
        s = _load_state()
        if not name or not _NAME_RE.match(name):
            _record(s, "createProject", name=name, result="invalid_name")
            _save_state(s)
            return _err("invalid_request",
                        "Project name must match ^[a-z0-9][a-z0-9-_.]{0,99}$")
        # Duplicate name within scope?
        for p in s["projects"].values():
            if p.get("name") == name and p.get("teamId") == teamId:
                _record(s, "createProject", name=name, result="conflict")
                _save_state(s)
                return _err("conflict",
                            f"A project with the name {name!r} already exists")
        if teamId and teamId not in s["teams"]:
            _record(s, "createProject", name=name, teamId=teamId,
                    result="team_not_found")
            _save_state(s)
            return _err("not_found", f"Team not found: {teamId}")
        env_specs = []
        for e in environmentVariables or []:
            if not isinstance(e, dict):
                continue
            env_specs.append({
                "key": e.get("key"),
                "value": e.get("value"),
                "target": e.get("target") or ["production", "preview", "development"],
                "type": e.get("type") or "encrypted",
            })
        proj = _make_project(
            s, name, framework, gitRepository, teamId,
            env=env_specs,
            root_directory=rootDirectory,
            build_command=buildCommand,
            dev_command=devCommand,
            output_directory=outputDirectory,
            install_command=installCommand,
            public_source=publicSource,
        )
        s["projects"][proj["id"]] = proj
        # Mirror env vars into env_vars index for env-var tools
        for e in env_specs:
            eid = _new_id(s, "env")
            s["env_vars"][eid] = {
                "id": eid,
                "key": e["key"],
                "value": e["value"],
                "target": e["target"],
                "type": e["type"],
                "projectId": proj["id"],
                "teamId": teamId,
                "createdAt": _now_ms(),
                "updatedAt": _now_ms(),
            }
        _record(s, "createProject", projectId=proj["id"], name=name,
                teamId=teamId)
        _save_state(s)
        return _strip_private(proj)


@mcp.tool(name="updateProject")
def update_project(idOrName: str,
                   name: str | None = None,
                   framework: str | None = None,
                   buildCommand: str | None = None,
                   devCommand: str | None = None,
                   outputDirectory: str | None = None,
                   installCommand: str | None = None,
                   rootDirectory: str | None = None,
                   nodeVersion: str | None = None,
                   publicSource: bool | None = None,
                   teamId: str | None = None) -> dict:
    """Vercel REST: PATCH /v9/projects/{idOrName} — update project
    settings. Only fields supplied are updated."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "updateProject", idOrName=idOrName,
                    result="not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        if name is not None:
            if not _NAME_RE.match(name):
                _record(s, "updateProject", idOrName=idOrName,
                        result="invalid_name")
                _save_state(s)
                return _err("invalid_request",
                            "Project name must match ^[a-z0-9][a-z0-9-_.]{0,99}$")
            # Check conflict on rename
            for p in s["projects"].values():
                if (p.get("name") == name
                        and p.get("teamId") == proj.get("teamId")
                        and p.get("id") != proj.get("id")):
                    _record(s, "updateProject", idOrName=idOrName,
                            result="conflict")
                    _save_state(s)
                    return _err("conflict",
                                f"A project with the name {name!r} already exists")
            proj["name"] = name
        if framework is not None:
            proj["framework"] = framework
        if buildCommand is not None:
            proj["buildCommand"] = buildCommand
        if devCommand is not None:
            proj["devCommand"] = devCommand
        if outputDirectory is not None:
            proj["outputDirectory"] = outputDirectory
        if installCommand is not None:
            proj["installCommand"] = installCommand
        if rootDirectory is not None:
            proj["rootDirectory"] = rootDirectory
        if nodeVersion is not None:
            proj["nodeVersion"] = nodeVersion
        if publicSource is not None:
            proj["publicSource"] = bool(publicSource)
        proj["updatedAt"] = _now_ms()
        _record(s, "updateProject", projectId=proj["id"])
        _save_state(s)
        return _strip_private(proj)


@mcp.tool(name="deleteProject")
def delete_project(idOrName: str, teamId: str | None = None) -> dict:
    """Vercel REST: DELETE /v9/projects/{idOrName} — delete a project
    and its deployments/domains/env vars. Returns an empty object on
    success."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "deleteProject", idOrName=idOrName,
                    result="not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        pid = proj["id"]
        # Cascade delete
        for did in [d for d, dep in s["deployments"].items()
                    if dep.get("projectId") == pid]:
            del s["deployments"][did]
        for dom in [d for d, dom in s["domains"].items()
                    if dom.get("projectId") == pid]:
            del s["domains"][dom]
        for eid in [e for e, env in s["env_vars"].items()
                    if env.get("projectId") == pid]:
            del s["env_vars"][eid]
        del s["projects"][pid]
        _record(s, "deleteProject", projectId=pid)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------

_VALID_STATES = {"QUEUED", "BUILDING", "READY", "ERROR", "CANCELED",
                 "INITIALIZING"}
_VALID_TARGETS = {"production", "preview", "staging", None}


def _make_deployment(state: dict, project: dict, name: str,
                     target: str | None, files: list | None,
                     git_source: dict | None,
                     project_settings: dict | None,
                     meta: dict | None) -> dict:
    did = _new_id(state, "dpl")
    now = _now_ms()
    safe_name = name or project.get("name") or "deployment"
    url = f"{safe_name}-{did.split('_', 1)[1][:9]}.vercel.app"
    deployment = {
        "uid": did,
        "id": did,
        "name": safe_name,
        "url": url,
        "state": "BUILDING",
        "readyState": "BUILDING",
        "type": "LAMBDAS",
        "target": target,
        "source": "api-trigger-git-deploy" if git_source else "cli",
        "createdAt": now,
        "buildingAt": now,
        "readyAt": None,
        "creator": {
            "uid": state["user"]["id"],
            "username": state["user"]["username"],
            "email": state["user"]["email"],
        },
        "projectId": project["id"],
        "ownerId": project.get("teamId") or state["user"]["id"],
        "teamId": project.get("teamId"),
        "meta": meta or {},
        "gitSource": git_source,
        "projectSettings": project_settings or {},
        "regions": ["iad1"],
        "_files": [],   # internal — exposed via listDeploymentFiles
    }
    # Stash files as separate file objects (mirrors the real API where
    # listDeploymentFiles enumerates the upload tree).
    for f in files or []:
        fid = _new_id(state, "file")
        entry = {
            "uid": fid,
            "name": f.get("file") if isinstance(f, dict) else str(f),
            "type": "file",
            "mode": 100644,
            "deploymentId": did,
            "children": [],
        }
        state["files"][fid] = entry
        deployment["_files"].append(fid)
    return deployment


@mcp.tool(name="listDeployments")
def list_deployments(projectId: str | None = None,
                     teamId: str | None = None,
                     state: str | None = None,
                     target: str | None = None,
                     limit: int = 20,
                     since: int | None = None,
                     until: int | None = None) -> dict:
    """Vercel REST: GET /v6/deployments — list deployments, optionally
    filtered by project, state (QUEUED/BUILDING/READY/ERROR/CANCELED),
    or target (production/preview)."""
    with _lock():
        s = _load_state()
        deployments = list(s["deployments"].values())
        if projectId:
            # Accept project id OR project name
            proj = _resolve_project(s, projectId)
            pid = proj["id"] if proj else projectId
            deployments = [d for d in deployments if d.get("projectId") == pid]
        if teamId is not None:
            deployments = _filter_team(deployments, teamId)
        if state:
            wanted = {st.strip().upper() for st in state.split(",")
                      if st.strip()}
            deployments = [d for d in deployments
                           if (d.get("state") or "").upper() in wanted]
        if target:
            deployments = [d for d in deployments
                           if (d.get("target") or "") == target]
        page, pagination = _paginate(deployments, limit, since, until)
        _record(s, "listDeployments", projectId=projectId,
                teamId=teamId, count=len(page))
        _save_state(s)
        return {"deployments": [_strip_private(d) for d in page],
                "pagination": pagination}


@mcp.tool(name="getDeployment")
def get_deployment(idOrUrl: str, teamId: str | None = None) -> dict:
    """Vercel REST: GET /v13/deployments/{idOrUrl} — retrieve a
    deployment by uid or by its public url (with or without https://)."""
    with _lock():
        s = _load_state()
        ref = idOrUrl
        if ref.startswith("https://"):
            ref = ref[len("https://"):]
        if ref.startswith("http://"):
            ref = ref[len("http://"):]
        dep = s["deployments"].get(ref)
        if not dep:
            for d in s["deployments"].values():
                if d.get("url") == ref or d.get("uid") == ref:
                    dep = d
                    break
        if dep and teamId is not None and dep.get("teamId") != teamId:
            dep = None
        _record(s, "getDeployment", idOrUrl=idOrUrl, teamId=teamId,
                result="ok" if dep else "not_found")
        _save_state(s)
        if not dep:
            return _err("not_found", f"Deployment not found: {idOrUrl}")
        return _strip_private(dep)


@mcp.tool(name="createDeployment")
def create_deployment(name: str | None = None,
                      project: str | None = None,
                      target: str | None = None,
                      files: list | None = None,
                      gitSource: dict | None = None,
                      projectSettings: dict | None = None,
                      meta: dict | None = None,
                      teamId: str | None = None) -> dict:
    """Vercel REST: POST /v13/deployments — create a deployment for an
    existing project. Deployments start in state BUILDING."""
    with _lock():
        s = _load_state()
        if target is not None and target not in _VALID_TARGETS:
            _record(s, "createDeployment", result="invalid_target",
                    target=target)
            _save_state(s)
            return _err("invalid_request",
                        f"target must be one of production/preview/staging: {target}")
        ref = project or name
        if not ref:
            _record(s, "createDeployment", result="missing_project")
            _save_state(s)
            return _err("invalid_request",
                        "project (id or name) is required")
        proj = _resolve_project(s, ref)
        if not proj:
            _record(s, "createDeployment", project=ref, result="not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {ref}")
        if teamId is not None and proj.get("teamId") != teamId:
            _record(s, "createDeployment", project=ref, teamId=teamId,
                    result="team_mismatch")
            _save_state(s)
            return _err("forbidden",
                        f"Project {ref!r} does not belong to team {teamId!r}")
        dep = _make_deployment(s, proj, name or proj.get("name"),
                               target, files, gitSource,
                               projectSettings, meta)
        s["deployments"][dep["uid"]] = dep
        # Track on the project so latestDeployments reflects it
        proj.setdefault("latestDeployments", []).insert(0, {
            "id": dep["uid"], "url": dep["url"], "state": dep["state"],
            "target": dep["target"], "createdAt": dep["createdAt"],
        })
        proj["latestDeployments"] = proj["latestDeployments"][:10]
        proj["updatedAt"] = _now_ms()
        _record(s, "createDeployment", deploymentId=dep["uid"],
                projectId=proj["id"], target=target)
        _save_state(s)
        return _strip_private(dep)


@mcp.tool(name="cancelDeployment")
def cancel_deployment(id: str, teamId: str | None = None) -> dict:
    """Vercel REST: PATCH /v12/deployments/{id}/cancel — cancel an
    in-progress deployment. Idempotent on terminal states."""
    with _lock():
        s = _load_state()
        dep = s["deployments"].get(id)
        if not dep:
            for d in s["deployments"].values():
                if d.get("url") == id:
                    dep = d
                    break
        if dep and teamId is not None and dep.get("teamId") != teamId:
            dep = None
        if not dep:
            _record(s, "cancelDeployment", id=id, result="not_found")
            _save_state(s)
            return _err("not_found", f"Deployment not found: {id}")
        if dep.get("state") in ("READY", "ERROR", "CANCELED"):
            _record(s, "cancelDeployment", id=id,
                    result="already_terminal", state=dep["state"])
            _save_state(s)
            return _strip_private(dep)
        dep["state"] = "CANCELED"
        dep["readyState"] = "CANCELED"
        dep["canceledAt"] = _now_ms()
        _record(s, "cancelDeployment", deploymentId=dep["uid"])
        _save_state(s)
        return _strip_private(dep)


@mcp.tool(name="deleteDeployment")
def delete_deployment(id: str, url: str | None = None,
                      teamId: str | None = None) -> dict:
    """Vercel REST: DELETE /v13/deployments/{id} — delete a
    deployment by id (or by its url query param)."""
    with _lock():
        s = _load_state()
        ref = id or url
        dep = s["deployments"].get(ref) if ref else None
        if not dep and ref:
            for d in s["deployments"].values():
                if d.get("url") == ref or d.get("uid") == ref:
                    dep = d
                    break
        if dep and teamId is not None and dep.get("teamId") != teamId:
            dep = None
        if not dep:
            _record(s, "deleteDeployment", id=id, result="not_found")
            _save_state(s)
            return _err("not_found", f"Deployment not found: {id}")
        # Cleanup files
        for fid in list(dep.get("_files", [])):
            s["files"].pop(fid, None)
        uid = dep["uid"]
        del s["deployments"][uid]
        _record(s, "deleteDeployment", deploymentId=uid)
        _save_state(s)
        return {"uid": uid, "state": "DELETED"}


@mcp.tool(name="listDeploymentFiles")
def list_deployment_files(id: str, teamId: str | None = None) -> list:
    """Vercel REST: GET /v6/deployments/{id}/files — list files
    uploaded for a deployment. Returns a flat list of file objects."""
    with _lock():
        s = _load_state()
        dep = s["deployments"].get(id)
        if not dep:
            for d in s["deployments"].values():
                if d.get("url") == id or d.get("uid") == id:
                    dep = d
                    break
        if dep and teamId is not None and dep.get("teamId") != teamId:
            dep = None
        if not dep:
            _record(s, "listDeploymentFiles", id=id, result="not_found")
            _save_state(s)
            # Real API returns a 404 error body; mirror as a single-item list
            return [_err("not_found", f"Deployment not found: {id}")]
        files = [s["files"][fid] for fid in dep.get("_files", [])
                 if fid in s["files"]]
        _record(s, "listDeploymentFiles", deploymentId=dep["uid"],
                count=len(files))
        _save_state(s)
        return files


# ---------------------------------------------------------------------------
# Project Domains
# ---------------------------------------------------------------------------

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                        r"(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$",
                        re.IGNORECASE)


@mcp.tool(name="listProjectDomains")
def list_project_domains(idOrName: str,
                         teamId: str | None = None,
                         limit: int = 20,
                         since: int | None = None,
                         until: int | None = None) -> dict:
    """Vercel REST: GET /v9/projects/{idOrName}/domains — list domains
    attached to a project."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "listProjectDomains", idOrName=idOrName,
                    result="not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        doms = [d for d in s["domains"].values()
                if d.get("projectId") == proj["id"]]
        page, pagination = _paginate(doms, limit, since, until)
        _record(s, "listProjectDomains", projectId=proj["id"],
                count=len(page))
        _save_state(s)
        return {"domains": [_strip_private(d) for d in page],
                "pagination": pagination}


@mcp.tool(name="addProjectDomain")
def add_project_domain(idOrName: str,
                       name: str,
                       gitBranch: str | None = None,
                       redirect: str | None = None,
                       redirectStatusCode: int | None = None,
                       teamId: str | None = None) -> dict:
    """Vercel REST: POST /v10/projects/{idOrName}/domains — attach a
    domain to a project."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "addProjectDomain", idOrName=idOrName,
                    result="project_not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        if not name or not _DOMAIN_RE.match(name):
            _record(s, "addProjectDomain", name=name,
                    result="invalid_domain")
            _save_state(s)
            return _err("invalid_request", f"Invalid domain: {name}")
        # Already attached?
        for d in s["domains"].values():
            if (d.get("projectId") == proj["id"]
                    and d.get("name") == name):
                _record(s, "addProjectDomain", name=name,
                        result="conflict")
                _save_state(s)
                return _err("domain_already_in_use",
                            f"Domain {name!r} already attached")
        did = _new_id(s, "dom")
        now = _now_ms()
        dom = {
            "id": did,
            "name": name,
            "projectId": proj["id"],
            "teamId": proj.get("teamId"),
            "apexName": ".".join(name.split(".")[-2:]),
            "gitBranch": gitBranch,
            "redirect": redirect,
            "redirectStatusCode": redirectStatusCode,
            "verified": True,
            "createdAt": now,
            "updatedAt": now,
        }
        s["domains"][did] = dom
        _record(s, "addProjectDomain", projectId=proj["id"], name=name)
        _save_state(s)
        return _strip_private(dom)


@mcp.tool(name="removeProjectDomain")
def remove_project_domain(idOrName: str,
                          domain: str,
                          teamId: str | None = None) -> dict:
    """Vercel REST: DELETE /v9/projects/{idOrName}/domains/{domain}
    — detach a domain from a project."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "removeProjectDomain", idOrName=idOrName,
                    result="project_not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        target = None
        for did, d in s["domains"].items():
            if (d.get("projectId") == proj["id"]
                    and d.get("name") == domain):
                target = did
                break
        if not target:
            _record(s, "removeProjectDomain", projectId=proj["id"],
                    domain=domain, result="not_found")
            _save_state(s)
            return _err("not_found", f"Domain not attached: {domain}")
        del s["domains"][target]
        _record(s, "removeProjectDomain", projectId=proj["id"],
                domain=domain)
        _save_state(s)
        return {"uid": target, "deleted": True}


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

_VALID_ENV_TARGETS = {"production", "preview", "development"}
_VALID_ENV_TYPES = {"plain", "encrypted", "secret", "system", "sensitive"}


@mcp.tool(name="listProjectEnv")
def list_project_env(idOrName: str,
                     teamId: str | None = None,
                     decrypt: bool = False) -> dict:
    """Vercel REST: GET /v9/projects/{idOrName}/env — list environment
    variables for a project. Encrypted values are masked unless
    `decrypt=true`."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "listProjectEnv", idOrName=idOrName,
                    result="not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        envs = []
        for e in s["env_vars"].values():
            if e.get("projectId") != proj["id"]:
                continue
            out = _strip_private(e)
            if (out.get("type") in ("encrypted", "secret", "sensitive")
                    and not decrypt):
                out = dict(out)
                out["value"] = None
            envs.append(out)
        _record(s, "listProjectEnv", projectId=proj["id"],
                count=len(envs), decrypt=decrypt)
        _save_state(s)
        return {"envs": envs, "pagination": {"count": len(envs),
                                             "next": None, "prev": None}}


@mcp.tool(name="createProjectEnv")
def create_project_env(idOrName: str,
                       key: str,
                       value: str,
                       target: list | str | None = None,
                       type: str = "encrypted",
                       gitBranch: str | None = None,
                       comment: str | None = None,
                       teamId: str | None = None) -> dict:
    """Vercel REST: POST /v10/projects/{idOrName}/env — create one
    environment variable on a project. `target` may be a list (any of
    production, preview, development) or comma-separated string."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "createProjectEnv", idOrName=idOrName,
                    result="project_not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        if not key:
            _record(s, "createProjectEnv", result="missing_key")
            _save_state(s)
            return _err("invalid_request", "key is required")
        if isinstance(target, str):
            targets = [t.strip() for t in target.split(",") if t.strip()]
        elif isinstance(target, list):
            targets = [t for t in target if t]
        else:
            targets = ["production", "preview", "development"]
        bad = [t for t in targets if t not in _VALID_ENV_TARGETS]
        if bad:
            _record(s, "createProjectEnv", result="invalid_target",
                    bad=bad)
            _save_state(s)
            return _err("invalid_request",
                        f"invalid target(s): {bad}")
        if type not in _VALID_ENV_TYPES:
            _record(s, "createProjectEnv", result="invalid_type",
                    type=type)
            _save_state(s)
            return _err("invalid_request",
                        f"type must be one of {sorted(_VALID_ENV_TYPES)}")
        # Conflict on (key, target overlap)
        for e in s["env_vars"].values():
            if (e.get("projectId") == proj["id"]
                    and e.get("key") == key
                    and (set(e.get("target") or []) & set(targets))):
                _record(s, "createProjectEnv", key=key,
                        result="conflict")
                _save_state(s)
                return _err("conflict",
                            f"env var {key!r} already exists for one of "
                            f"the requested targets")
        eid = _new_id(s, "env")
        now = _now_ms()
        env = {
            "id": eid,
            "key": key,
            "value": value,
            "target": targets,
            "type": type,
            "gitBranch": gitBranch,
            "comment": comment,
            "projectId": proj["id"],
            "teamId": proj.get("teamId"),
            "createdAt": now,
            "updatedAt": now,
        }
        s["env_vars"][eid] = env
        _record(s, "createProjectEnv", projectId=proj["id"], key=key,
                envId=eid)
        _save_state(s)
        return {"created": _strip_private(env)}


@mcp.tool(name="deleteProjectEnv")
def delete_project_env(idOrName: str, id: str,
                       teamId: str | None = None) -> dict:
    """Vercel REST: DELETE /v9/projects/{idOrName}/env/{id} — remove
    one environment variable from a project."""
    with _lock():
        s = _load_state()
        proj = _resolve_project(s, idOrName)
        if proj and teamId is not None and proj.get("teamId") != teamId:
            proj = None
        if not proj:
            _record(s, "deleteProjectEnv", idOrName=idOrName,
                    result="project_not_found")
            _save_state(s)
            return _err("not_found", f"Project not found: {idOrName}")
        env = s["env_vars"].get(id)
        if not env:
            # also allow looking up by key
            for e in s["env_vars"].values():
                if (e.get("projectId") == proj["id"]
                        and e.get("key") == id):
                    env = e
                    break
        if not env or env.get("projectId") != proj["id"]:
            _record(s, "deleteProjectEnv", id=id, result="not_found")
            _save_state(s)
            return _err("not_found", f"Env var not found: {id}")
        eid = env["id"]
        del s["env_vars"][eid]
        _record(s, "deleteProjectEnv", projectId=proj["id"], envId=eid)
        _save_state(s)
        return {"id": eid, "deleted": True}


# ---------------------------------------------------------------------------
# Mock-only helpers (not part of the real surface)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection)."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(user: dict | None = None,
                    teams: list | None = None,
                    projects: list | None = None,
                    deployments: list | None = None,
                    domains: list | None = None,
                    env_vars: list | None = None,
                    files: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed Vercel state with fixture objects.

    - `user`: {id?, username, email, name?}
    - `teams`: [{id?, slug, name, ...}]
    - `projects`: [{id?, name, framework?, gitRepository?, teamId?, ...}]
    - `deployments`: [{uid?, project, target?, state?, url?, meta?,
                       gitSource?, files?}]
    - `domains`: [{id?, project, name, gitBranch?, redirect?,
                   redirectStatusCode?}]
    - `env_vars`: [{id?, project, key, value, target?, type?}]
    - `files`: [{id?, deploymentId, name, mode?}]

    `project` references accept project id or name. `target` accepts
    'production' or 'preview'. If `replace=True`, all state is reset
    before seeding.
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if user:
            s["user"].update(user)
        for t in teams or []:
            tid = t.get("id") or _new_id(s, "team")
            s["teams"][tid] = {
                "id": tid,
                "slug": t.get("slug", tid),
                "name": t.get("name", t.get("slug", tid)),
                "createdAt": t.get("createdAt", _now_ms()),
                "membership": t.get("membership",
                                    {"role": "OWNER", "uid": s["user"]["id"]}),
                "billing": t.get("billing", {"plan": "hobby"}),
            }
        # Index projects after teams so teamId references resolve
        for p in projects or []:
            pid = p.get("id") or _new_id(s, "prj")
            team_id = p.get("teamId")
            proj = _make_project(
                s, p.get("name", pid),
                p.get("framework"), p.get("gitRepository"),
                team_id,
                env=p.get("env"),
                root_directory=p.get("rootDirectory"),
                build_command=p.get("buildCommand"),
                dev_command=p.get("devCommand"),
                output_directory=p.get("outputDirectory"),
                install_command=p.get("installCommand"),
                public_source=p.get("publicSource"),
            )
            proj["id"] = pid
            for k, v in p.items():
                if k in ("id", "env"):
                    continue
                proj[k] = v
            s["projects"][pid] = proj
        for d in deployments or []:
            proj_ref = d.get("project") or d.get("projectId")
            proj = _resolve_project(s, proj_ref) if proj_ref else None
            if not proj:
                continue
            dep = _make_deployment(
                s, proj, d.get("name") or proj["name"],
                d.get("target"), d.get("files"),
                d.get("gitSource"), d.get("projectSettings"),
                d.get("meta"),
            )
            if d.get("uid"):
                dep["uid"] = d["uid"]
                dep["id"] = d["uid"]
            if d.get("url"):
                dep["url"] = d["url"]
            if d.get("state"):
                dep["state"] = d["state"]
                dep["readyState"] = d["state"]
                if d["state"] == "READY":
                    dep["readyAt"] = d.get("readyAt") or _now_ms()
            if d.get("createdAt"):
                dep["createdAt"] = d["createdAt"]
            s["deployments"][dep["uid"]] = dep
        for dom in domains or []:
            proj_ref = dom.get("project") or dom.get("projectId")
            proj = _resolve_project(s, proj_ref) if proj_ref else None
            if not proj or not dom.get("name"):
                continue
            did = dom.get("id") or _new_id(s, "dom")
            now = _now_ms()
            s["domains"][did] = {
                "id": did,
                "name": dom["name"],
                "projectId": proj["id"],
                "teamId": proj.get("teamId"),
                "apexName": ".".join(dom["name"].split(".")[-2:]),
                "gitBranch": dom.get("gitBranch"),
                "redirect": dom.get("redirect"),
                "redirectStatusCode": dom.get("redirectStatusCode"),
                "verified": dom.get("verified", True),
                "createdAt": dom.get("createdAt", now),
                "updatedAt": dom.get("updatedAt", now),
            }
        for e in env_vars or []:
            proj_ref = e.get("project") or e.get("projectId")
            proj = _resolve_project(s, proj_ref) if proj_ref else None
            if not proj or not e.get("key"):
                continue
            eid = e.get("id") or _new_id(s, "env")
            now = _now_ms()
            target = e.get("target")
            if isinstance(target, str):
                target = [t.strip() for t in target.split(",") if t.strip()]
            elif not target:
                target = ["production", "preview", "development"]
            s["env_vars"][eid] = {
                "id": eid,
                "key": e["key"],
                "value": e.get("value", ""),
                "target": target,
                "type": e.get("type", "encrypted"),
                "gitBranch": e.get("gitBranch"),
                "comment": e.get("comment"),
                "projectId": proj["id"],
                "teamId": proj.get("teamId"),
                "createdAt": e.get("createdAt", now),
                "updatedAt": e.get("updatedAt", now),
            }
        for f in files or []:
            if not f.get("deploymentId"):
                continue
            fid = f.get("id") or _new_id(s, "file")
            s["files"][fid] = {
                "uid": fid,
                "name": f.get("name", fid),
                "type": f.get("type", "file"),
                "mode": f.get("mode", 100644),
                "deploymentId": f["deploymentId"],
                "children": f.get("children", []),
            }
            # Attach to deployment's _files
            dep = s["deployments"].get(f["deploymentId"])
            if dep:
                dep.setdefault("_files", []).append(fid)
        _record(s, "debug_seed",
                counts={
                    "teams": len(teams or []),
                    "projects": len(projects or []),
                    "deployments": len(deployments or []),
                    "domains": len(domains or []),
                    "env_vars": len(env_vars or []),
                    "files": len(files or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "project_ids": list(s["projects"].keys()),
            "deployment_ids": list(s["deployments"].keys()),
            "team_ids": list(s["teams"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
