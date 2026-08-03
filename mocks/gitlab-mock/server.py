"""GitLab mock MCP server.

Mirrors the surface of GitLab's REST API v4
(https://docs.gitlab.com/ee/api/rest/) — tool names follow the
conventions used by GitLab's reference MCP server (e.g.
`list_projects`, `get_project`, `create_issue`, `create_merge_request`)
and parameter / response shapes match the REST documentation so an
agent trained on the real surface sees a faithful stand-in.

Key GitLab idioms that this mock honors verbatim:

  * **Issue identifiers**: every issue has a global `id` AND a
    project-scoped `iid` — the *iid* is what shows up in URLs
    (`/projects/<pid>/issues/<iid>`) and what callers pass to
    `get_issue` / `update_issue` / `close_issue`. Same pattern for
    merge requests.
  * **Project identifiers**: callers may pass either a numeric `id`
    (e.g. `42`) or a URL-encoded path (`group/project` or
    `group%2Fproject`). Both resolve to the same project.
  * **Errors**: GitLab returns `{"message":"404 Not Found"}` (or a
    field-keyed dict for 4xx validation errors). We return these as
    plain dict bodies — *not* raised exceptions — so the trace looks
    like a real failed HTTP call.

Backed by a single JSON state file (default
`$GITLAB_MOCK_STATE_DIR/state.json`) holding users, projects (with
branches/files/commits), issues, merge requests, plus a `calls` log
the verifier consumes. File-locked with `fcntl.flock` for safe
concurrent access. Set `GITLAB_MOCK_SEED_PATH` to preload state.

Plus mock-only helpers (`mock_debug_state`, `mock_debug_seed`) that
are *not* part of the real GitLab API surface — they only exist so
the verifier and per-task seeders can introspect and prepare state.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import urllib.parse
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State storage helpers
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "GITLAB_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gitlab_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    """GitLab uses RFC3339 with millisecond precision, e.g.
    `2024-01-15T10:23:00.000Z`."""
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _blob_sha(content: bytes) -> str:
    """Mimic git blob SHA: 'blob <size>\\0<content>'."""
    header = f"blob {len(content)}\0".encode("utf-8")
    return _sha1(header + content)


def _commit_sha(parents: list, tree_sha: str, message: str) -> str:
    body = (f"{tree_sha}\n{':'.join(parents)}\n{message}\n{_now()}"
            ).encode()
    return _sha1(body)


_DEFAULT_USER = {
    "id": 1,
    "username": "mockuser",
    "name": "Mock User",
    "state": "active",
    "locked": False,
    "avatar_url": "https://gitlab.example.com/uploads/-/system/user/avatar/1/avatar.png",
    "web_url": "https://gitlab.example.com/mockuser",
    "email": "mockuser@example.com",
    "public_email": "",
    "created_at": "2020-01-01T00:00:00.000Z",
    "bio": "",
    "location": None,
    "linkedin": "",
    "twitter": "",
    "discord": "",
    "website_url": "",
    "organization": None,
    "job_title": "",
    "pronouns": None,
    "bot": False,
    "work_information": None,
    "followers": 0,
    "following": 0,
    "is_followed": False,
    "local_time": None,
    "last_sign_in_at": "2024-01-01T00:00:00.000Z",
    "confirmed_at": "2020-01-01T00:00:00.000Z",
    "last_activity_on": "2024-01-01",
    "theme_id": 1,
    "color_scheme_id": 1,
    "projects_limit": 100000,
    "current_sign_in_at": "2024-01-01T00:00:00.000Z",
    "identities": [],
    "can_create_group": True,
    "can_create_project": True,
    "two_factor_enabled": False,
    "external": False,
    "private_profile": False,
    "commit_email": "mockuser@example.com",
}


def _empty_state() -> dict:
    return {
        "current_user": dict(_DEFAULT_USER),
        "users": {1: dict(_DEFAULT_USER)},      # id -> user dict
        "projects": {},                          # id -> project dict
        "path_to_id": {},                        # "group/project" -> id
        "issues": {},                            # (project_id, iid) -> issue dict
        "merge_requests": {},                    # (project_id, iid) -> mr dict
        "next_id": {
            "user": 2,
            "project": 1,
            "issue": 1,             # global issue id
            "mr": 1,                # global mr id
            "note": 1,              # comment ids
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GITLAB_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return _rehydrate(json.load(f))
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return _rehydrate(json.load(f))


def _rehydrate(state: dict) -> dict:
    """JSON can't represent tuple keys — convert string keys back to
    tuples for issues / merge_requests after loading."""
    out = dict(state)
    issues = {}
    for k, v in (state.get("issues") or {}).items():
        if isinstance(k, tuple):
            issues[k] = v
        else:
            # "pid|iid" string form
            pid, iid = k.split("|", 1)
            issues[(int(pid), int(iid))] = v
    out["issues"] = issues
    mrs = {}
    for k, v in (state.get("merge_requests") or {}).items():
        if isinstance(k, tuple):
            mrs[k] = v
        else:
            pid, iid = k.split("|", 1)
            mrs[(int(pid), int(iid))] = v
    out["merge_requests"] = mrs
    # users / projects keys may be JSON strings; coerce to int
    out["users"] = {int(k): v for k, v in (state.get("users") or {}).items()}
    out["projects"] = {int(k): v for k, v in
                       (state.get("projects") or {}).items()}
    return out


def _save_state(state: dict) -> None:
    """Encode tuple keys to `"pid|iid"` strings since JSON has no
    tuple key type."""
    encoded = dict(state)
    encoded["issues"] = {f"{pid}|{iid}": v
                        for (pid, iid), v in state.get("issues", {}).items()}
    encoded["merge_requests"] = {
        f"{pid}|{iid}": v
        for (pid, iid), v in state.get("merge_requests", {}).items()
    }
    encoded["users"] = {str(k): v for k, v in state.get("users", {}).items()}
    encoded["projects"] = {str(k): v for k, v in
                           state.get("projects", {}).items()}
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(encoded, f, indent=2)
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


def _record(_state: dict, _op: str, **kwargs) -> None:
    """Append an op entry to the call log. Leading underscores avoid
    keyword collisions with caller-supplied fields like `state` or `op`."""
    entry = {"op": _op, "ts": _now()}
    entry.update(kwargs)
    _state["calls"].append(entry)


def _err_404(message: str = "404 Not Found") -> dict:
    """GitLab 404 body."""
    return {"message": message}


def _err_400(message: str | dict) -> dict:
    """GitLab 400 body — either `{"error":"..."}` for plain errors or
    a field-keyed `{"<field>":["msg"]}` shape for validation."""
    if isinstance(message, dict):
        return {"message": message}
    return {"error": message}


def _err_403(message: str = "403 Forbidden") -> dict:
    return {"message": message}


def _err_409(message: str = "409 Conflict") -> dict:
    return {"message": message}


# ---------------------------------------------------------------------------
# ID / path resolution
# ---------------------------------------------------------------------------

def _resolve_project_id(state: dict, project_id: str | int) -> int | None:
    """Accepts numeric id (int or str-int) or URL-encoded path
    (`group/project` or `group%2Fproject`). Returns the project's
    numeric id or None if not found."""
    if project_id is None:
        return None
    if isinstance(project_id, int):
        return project_id if project_id in state["projects"] else None
    s = str(project_id)
    # Try numeric first.
    if s.isdigit():
        pid = int(s)
        return pid if pid in state["projects"] else None
    # URL-decoded path: GitLab accepts both `group/project` and
    # `group%2Fproject`.
    decoded = urllib.parse.unquote(s)
    return state["path_to_id"].get(decoded)


def _resolve_user(state: dict, ref: str | int) -> dict | None:
    if ref is None:
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return state["users"].get(int(ref))
    for u in state["users"].values():
        if u.get("username") == ref or u.get("name") == ref \
                or u.get("email") == ref:
            return u
    return None


def _alloc(state: dict, key: str) -> int:
    n = state["next_id"][key]
    state["next_id"][key] = n + 1
    return n


# ---------------------------------------------------------------------------
# Project / issue / MR factories
# ---------------------------------------------------------------------------

def _user_ref(u: dict) -> dict:
    """Short-form user reference embedded in projects/issues/MRs."""
    return {
        "id": u["id"],
        "username": u.get("username", ""),
        "name": u.get("name", ""),
        "state": u.get("state", "active"),
        "locked": u.get("locked", False),
        "avatar_url": u.get("avatar_url", ""),
        "web_url": u.get("web_url", ""),
    }


def _namespace_for(owner: dict) -> dict:
    """A minimal Namespace object — GitLab embeds this on projects."""
    return {
        "id": owner["id"],
        "name": owner.get("name", owner["username"]),
        "path": owner["username"],
        "kind": "user",
        "full_path": owner["username"],
        "parent_id": None,
        "avatar_url": owner.get("avatar_url", ""),
        "web_url": f"https://gitlab.example.com/{owner['username']}",
    }


def _make_project(state: dict, owner: dict, name: str,
                  path: str | None = None,
                  description: str = "",
                  visibility: str = "private",
                  default_branch: str = "main",
                  initialize_with_readme: bool = False) -> dict:
    pid = _alloc(state, "project")
    now = _now()
    slug = path or re.sub(r"[^a-z0-9-]+", "-",
                         name.lower()).strip("-") or f"project-{pid}"
    full_path = f"{owner['username']}/{slug}"
    project = {
        "id": pid,
        "description": description or None,
        "name": name,
        "name_with_namespace": f"{owner.get('name', owner['username'])} / {name}",
        "path": slug,
        "path_with_namespace": full_path,
        "created_at": now,
        "default_branch": default_branch,
        "tag_list": [],
        "topics": [],
        "ssh_url_to_repo": f"git@gitlab.example.com:{full_path}.git",
        "http_url_to_repo": f"https://gitlab.example.com/{full_path}.git",
        "web_url": f"https://gitlab.example.com/{full_path}",
        "readme_url": (f"https://gitlab.example.com/{full_path}/-/blob/"
                       f"{default_branch}/README.md")
                      if initialize_with_readme else None,
        "forks_count": 0,
        "avatar_url": None,
        "star_count": 0,
        "last_activity_at": now,
        "namespace": _namespace_for(owner),
        "_links": {
            "self":
                f"https://gitlab.example.com/api/v4/projects/{pid}",
            "issues":
                f"https://gitlab.example.com/api/v4/projects/{pid}/issues",
            "merge_requests":
                f"https://gitlab.example.com/api/v4/projects/{pid}/merge_requests",
            "repo_branches":
                f"https://gitlab.example.com/api/v4/projects/{pid}/repository/branches",
            "labels":
                f"https://gitlab.example.com/api/v4/projects/{pid}/labels",
            "events":
                f"https://gitlab.example.com/api/v4/projects/{pid}/events",
            "members":
                f"https://gitlab.example.com/api/v4/projects/{pid}/members",
            "cluster_agents":
                f"https://gitlab.example.com/api/v4/projects/{pid}/cluster_agents",
        },
        "packages_enabled": True,
        "empty_repo": not initialize_with_readme,
        "archived": False,
        "visibility": visibility,
        "owner": _user_ref(owner),
        "resolve_outdated_diff_discussions": False,
        "container_expiration_policy": None,
        "repository_object_format": "sha1",
        "issues_enabled": True,
        "merge_requests_enabled": True,
        "wiki_enabled": True,
        "jobs_enabled": True,
        "snippets_enabled": True,
        "open_issues_count": 0,
        "creator_id": owner["id"],
        "forked_from_project": None,
        "import_status": "none",
        "approvals_before_merge": 0,
        "mirror": False,
        # mock-internal — stripped on response by _strip_project:
        "_branches": {},
        "_files": {},        # branch -> {path: {sha,size,content_b64}}
        "_commits": {},
        "_commit_order": [],
        "_issues_index": [],
        "_mrs_index": [],
        "_next_issue_iid": 1,
        "_next_mr_iid": 1,
    }
    state["projects"][pid] = project
    state["path_to_id"][full_path] = pid
    if initialize_with_readme:
        readme = f"# {name}\n\n{description}\n".encode("utf-8")
        _write_file(project, default_branch, "README.md", readme,
                    "Initial commit", author=owner)
        project["empty_repo"] = False
    return project


def _strip_project(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _ensure_branch(p: dict, branch: str) -> dict | None:
    if branch in p["_branches"]:
        return p["_branches"][branch]
    if not p["_branches"] and branch == p["default_branch"]:
        p["_branches"][branch] = {"name": branch, "commit": None,
                                  "protected": False, "default": True,
                                  "developers_can_push": False,
                                  "developers_can_merge": False,
                                  "can_push": True, "web_url":
                                  f"{p['web_url']}/-/tree/{branch}"}
        p["_files"][branch] = {}
        return p["_branches"][branch]
    return None


def _new_commit(p: dict, branch: str, message: str,
                author: dict) -> dict:
    parent_sha = (p["_branches"][branch].get("commit") or {}).get("id")
    files = p["_files"].get(branch, {})
    tree_sha = _sha1(("\n".join(
        f"{path}:{f['sha']}" for path, f in sorted(files.items())
    )).encode("utf-8") or b"empty")
    parents = [parent_sha] if parent_sha else []
    sha = _commit_sha(parents, tree_sha, message)
    now = _now()
    full = p["path_with_namespace"]
    short = sha[:8]
    commit_obj = {
        "id": sha,
        "short_id": short,
        "created_at": now,
        "parent_ids": parents,
        "title": message.split("\n", 1)[0],
        "message": message,
        "author_name": author.get("name", ""),
        "author_email": author.get("email", ""),
        "authored_date": now,
        "committer_name": author.get("name", ""),
        "committer_email": author.get("email", ""),
        "committed_date": now,
        "web_url": (f"https://gitlab.example.com/{full}/-/commit/{sha}"),
        "trailers": {},
        "extended_trailers": {},
    }
    p["_commits"][sha] = commit_obj
    p["_commit_order"].insert(0, sha)
    p["_branches"][branch]["commit"] = commit_obj
    p["last_activity_at"] = now
    return commit_obj


def _write_file(p: dict, branch: str, file_path: str, content: bytes,
                message: str, author: dict,
                operation: str = "create") -> dict:
    br = _ensure_branch(p, branch)
    if br is None:
        # auto-create the branch from default
        if p["default_branch"] in p["_branches"]:
            src = p["default_branch"]
            p["_branches"][branch] = {
                "name": branch,
                "commit": p["_branches"][src].get("commit"),
                "protected": False, "default": False,
                "developers_can_push": False,
                "developers_can_merge": False,
                "can_push": True,
                "web_url": f"{p['web_url']}/-/tree/{branch}",
            }
            p["_files"][branch] = json.loads(json.dumps(
                p["_files"].get(src, {})))
        else:
            p["_branches"][branch] = {"name": branch, "commit": None,
                                      "protected": False, "default": True,
                                      "developers_can_push": False,
                                      "developers_can_merge": False,
                                      "can_push": True,
                                      "web_url":
                                      f"{p['web_url']}/-/tree/{branch}"}
            p["_files"][branch] = {}
    files = p["_files"].setdefault(branch, {})
    sha = _blob_sha(content)
    size = len(content)
    files[file_path] = {
        "sha": sha,
        "size": size,
        "content_b64": base64.b64encode(content).decode("ascii"),
    }
    commit = _new_commit(p, branch, message, author)
    return {
        "file_path": file_path,
        "branch": branch,
        # GitLab returns this shape from POST/PUT
        # /projects/:id/repository/files/:file_path
        "file_name": file_path.rsplit("/", 1)[-1],
        "commit_id": commit["id"],
        "blob_id": sha,
    }


def _make_issue(state: dict, p: dict, title: str, description: str,
                assignee_ids: list, label_names: list,
                milestone_id: int | None, due_date: str | None,
                author: dict, confidential: bool = False,
                issue_type: str = "issue") -> dict:
    iid = p["_next_issue_iid"]
    p["_next_issue_iid"] += 1
    gid = _alloc(state, "issue")
    now = _now()
    full = p["path_with_namespace"]
    assignees = [_user_ref(state["users"][aid])
                 for aid in assignee_ids
                 if aid in state["users"]]
    issue = {
        "id": gid,
        "iid": iid,
        "project_id": p["id"],
        "title": title,
        "description": description or "",
        "state": "opened",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "closed_by": None,
        "labels": list(label_names or []),
        "milestone": ({"id": milestone_id,
                       "title": f"Milestone {milestone_id}"}
                      if milestone_id else None),
        "assignees": assignees,
        "author": _user_ref(author),
        "type": (issue_type or "ISSUE").upper(),
        "assignee": assignees[0] if assignees else None,
        "user_notes_count": 0,
        "merge_requests_count": 0,
        "upvotes": 0,
        "downvotes": 0,
        "due_date": due_date,
        "confidential": bool(confidential),
        "discussion_locked": None,
        "issue_type": issue_type,
        "web_url": f"https://gitlab.example.com/{full}/-/issues/{iid}",
        "time_stats": {
            "time_estimate": 0,
            "total_time_spent": 0,
            "human_time_estimate": None,
            "human_total_time_spent": None,
        },
        "task_completion_status": {"count": 0, "completed_count": 0},
        "weight": None,
        "blocking_issues_count": 0,
        "has_tasks": False,
        "_notes": [],
        "references": {
            "short": f"#{iid}",
            "relative": f"#{iid}",
            "full": f"{full}#{iid}",
        },
        "moved_to_id": None,
        "service_desk_reply_to": None,
        "epic_iid": None,
        "epic": None,
        "iteration": None,
    }
    state["issues"][(p["id"], iid)] = issue
    p["_issues_index"].append(iid)
    p["open_issues_count"] += 1
    return issue


def _strip_issue(issue: dict) -> dict:
    return {k: v for k, v in issue.items() if not k.startswith("_")}


def _make_mr(state: dict, p: dict, title: str, description: str,
             source_branch: str, target_branch: str,
             assignee_ids: list, reviewer_ids: list,
             label_names: list, milestone_id: int | None,
             remove_source_branch: bool, squash: bool,
             allow_collaboration: bool, draft: bool,
             author: dict) -> dict:
    iid = p["_next_mr_iid"]
    p["_next_mr_iid"] += 1
    gid = _alloc(state, "mr")
    now = _now()
    full = p["path_with_namespace"]
    assignees = [_user_ref(state["users"][aid])
                 for aid in assignee_ids
                 if aid in state["users"]]
    reviewers = [_user_ref(state["users"][rid])
                 for rid in reviewer_ids
                 if rid in state["users"]]
    head_sha = (p["_branches"].get(source_branch, {}).get("commit")
                or {}).get("id") or _sha1(source_branch.encode())
    base_sha = (p["_branches"].get(target_branch, {}).get("commit")
                or {}).get("id") or _sha1(target_branch.encode())
    mr = {
        "id": gid,
        "iid": iid,
        "project_id": p["id"],
        "title": (f"Draft: {title}" if draft and not title.lower().startswith(
            "draft:") else title),
        "description": description or "",
        "state": "opened",
        "created_at": now,
        "updated_at": now,
        "merged_by": None,
        "merge_user": None,
        "merged_at": None,
        "closed_by": None,
        "closed_at": None,
        "target_branch": target_branch,
        "source_branch": source_branch,
        "user_notes_count": 0,
        "upvotes": 0,
        "downvotes": 0,
        "author": _user_ref(author),
        "assignees": assignees,
        "assignee": assignees[0] if assignees else None,
        "reviewers": reviewers,
        "source_project_id": p["id"],
        "target_project_id": p["id"],
        "labels": list(label_names or []),
        "draft": bool(draft),
        "work_in_progress": bool(draft),
        "imported": False,
        "imported_from": "none",
        "milestone": ({"id": milestone_id,
                       "title": f"Milestone {milestone_id}"}
                      if milestone_id else None),
        "merge_when_pipeline_succeeds": False,
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable",
        "sha": head_sha,
        "merge_commit_sha": None,
        "squash_commit_sha": None,
        "discussion_locked": None,
        "should_remove_source_branch": bool(remove_source_branch),
        "force_remove_source_branch": bool(remove_source_branch),
        "prepared_at": now,
        "reference": f"!{iid}",
        "references": {
            "short": f"!{iid}",
            "relative": f"!{iid}",
            "full": f"{full}!{iid}",
        },
        "web_url": f"https://gitlab.example.com/{full}/-/merge_requests/{iid}",
        "time_stats": {
            "time_estimate": 0,
            "total_time_spent": 0,
            "human_time_estimate": None,
            "human_total_time_spent": None,
        },
        "squash": bool(squash),
        "squash_on_merge": bool(squash),
        "task_completion_status": {"count": 0, "completed_count": 0},
        "has_conflicts": False,
        "blocking_discussions_resolved": True,
        "approvals_before_merge": None,
        "allow_collaboration": bool(allow_collaboration),
        "allow_maintainer_to_push": bool(allow_collaboration),
        "diff_refs": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": base_sha,
        },
        "_notes": [],
    }
    state["merge_requests"][(p["id"], iid)] = mr
    p["_mrs_index"].append(iid)
    return mr


def _strip_mr(mr: dict) -> dict:
    return {k: v for k, v in mr.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("gitlab-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool(name="get_current_user")
def get_current_user() -> dict:
    """GitLab REST: GET /user — get the currently authenticated user."""
    with _lock():
        s = _load_state()
        _record(s, "get_current_user")
        _save_state(s)
        return dict(s["current_user"])


@mcp.tool(name="search_users")
def search_users(search: str = "",
                 username: str = "",
                 active: bool | None = None,
                 per_page: int = 20,
                 page: int = 1) -> list:
    """GitLab REST: GET /users?search=&username= — list/search users.

    Returns an array of user objects (`id`, `username`, `name`,
    `state`, `avatar_url`, `web_url`)."""
    with _lock():
        s = _load_state()
        rows = list(s["users"].values())
        if username:
            rows = [u for u in rows if u.get("username") == username]
        if search:
            q = search.lower()
            rows = [u for u in rows
                    if q in (u.get("username") or "").lower()
                    or q in (u.get("name") or "").lower()
                    or q in (u.get("email") or "").lower()]
        if active is True:
            rows = [u for u in rows if u.get("state") == "active"]
        elif active is False:
            rows = [u for u in rows if u.get("state") != "active"]
        per = max(1, min(int(per_page or 20), 100))
        start = max(0, (int(page or 1) - 1)) * per
        page_items = rows[start:start + per]
        _record(s, "search_users", search=search, count=len(page_items))
        _save_state(s)
        return [_user_ref(u) for u in page_items]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@mcp.tool(name="list_projects")
def list_projects(search: str = "",
                  visibility: str = "",
                  owned: bool = False,
                  membership: bool = False,
                  starred: bool = False,
                  archived: bool | None = None,
                  order_by: str = "created_at",
                  sort: str = "desc",
                  per_page: int = 20,
                  page: int = 1) -> list:
    """GitLab REST: GET /projects — list projects accessible to the
    authenticated user, with the usual search / visibility / ownership
    filters. Returns an array of project objects."""
    with _lock():
        s = _load_state()
        rows = list(s["projects"].values())
        me_id = s["current_user"]["id"]
        if search:
            q = search.lower()
            rows = [p for p in rows
                    if q in (p.get("name") or "").lower()
                    or q in (p.get("path_with_namespace") or "").lower()
                    or q in (p.get("description") or "").lower()]
        if visibility:
            rows = [p for p in rows if p.get("visibility") == visibility]
        if owned:
            rows = [p for p in rows if p.get("creator_id") == me_id]
        if membership:
            # In the mock everyone is implicitly a member of every project.
            pass
        if starred:
            rows = []  # no starring in the mock
        if archived is True:
            rows = [p for p in rows if p.get("archived")]
        elif archived is False:
            rows = [p for p in rows if not p.get("archived")]
        keyf = {
            "id": lambda p: p["id"],
            "name": lambda p: p.get("name", ""),
            "path": lambda p: p.get("path_with_namespace", ""),
            "created_at": lambda p: p.get("created_at", ""),
            "updated_at": lambda p: p.get("last_activity_at", ""),
            "last_activity_at": lambda p: p.get("last_activity_at", ""),
        }.get(order_by, lambda p: p.get("created_at", ""))
        rev = (sort or "desc").lower() != "asc"
        rows.sort(key=keyf, reverse=rev)
        per = max(1, min(int(per_page or 20), 100))
        start = max(0, (int(page or 1) - 1)) * per
        page_items = rows[start:start + per]
        _record(s, "list_projects", search=search, count=len(page_items))
        _save_state(s)
        return [_strip_project(p) for p in page_items]


@mcp.tool(name="get_project")
def get_project(id: str | int) -> dict:
    """GitLab REST: GET /projects/:id. `id` may be a numeric id or a
    URL-encoded path like `group%2Fproject` (or just `group/project`)."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, id)
        if pid is None:
            _record(s, "get_project", id=id, result="not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        _record(s, "get_project", id=id, project_id=pid)
        _save_state(s)
        return _strip_project(s["projects"][pid])


@mcp.tool(name="create_project")
def create_project(name: str,
                   path: str | None = None,
                   description: str = "",
                   visibility: str = "private",
                   default_branch: str = "main",
                   initialize_with_readme: bool = False,
                   namespace_id: int | None = None) -> dict:
    """GitLab REST: POST /projects. Creates a new project under the
    current user (or `namespace_id`). Returns the project object."""
    with _lock():
        s = _load_state()
        owner_id = namespace_id or s["current_user"]["id"]
        owner = s["users"].get(owner_id)
        if not owner:
            _record(s, "create_project", name=name, result="bad_namespace")
            _save_state(s)
            return _err_404("404 Namespace Not Found")
        slug = path or re.sub(r"[^a-z0-9-]+", "-",
                             name.lower()).strip("-")
        if not slug:
            slug = f"project-{s['next_id']['project']}"
        full_path = f"{owner['username']}/{slug}"
        if full_path in s["path_to_id"]:
            _record(s, "create_project", name=name, result="exists")
            _save_state(s)
            return _err_400({"name": ["has already been taken"]})
        p = _make_project(s, owner, name, path=slug,
                         description=description,
                         visibility=visibility,
                         default_branch=default_branch,
                         initialize_with_readme=initialize_with_readme)
        _record(s, "create_project", project_id=p["id"],
                path_with_namespace=p["path_with_namespace"])
        _save_state(s)
        return _strip_project(p)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

@mcp.tool(name="list_issues")
def list_issues(project_id: str | int | None = None,
                state: str = "opened",
                labels: str = "",
                milestone: str = "",
                assignee_id: int | None = None,
                author_id: int | None = None,
                search: str = "",
                order_by: str = "created_at",
                sort: str = "desc",
                per_page: int = 20,
                page: int = 1) -> list:
    """GitLab REST: GET /projects/:id/issues (or GET /issues if
    `project_id` is omitted). `state` is `opened|closed|all`; `labels`
    is a comma-separated string. Returns an array of issue objects."""
    with _lock():
        s = _load_state()
        if project_id is not None:
            pid = _resolve_project_id(s, project_id)
            if pid is None:
                _record(s, "list_issues", project_id=project_id,
                        result="not_found")
                _save_state(s)
                return _err_404("404 Project Not Found")
            rows = [s["issues"][(pid, iid)]
                    for iid in s["projects"][pid]["_issues_index"]
                    if (pid, iid) in s["issues"]]
        else:
            rows = list(s["issues"].values())
        if state and state != "all":
            target = state.lower()
            rows = [x for x in rows if x.get("state") == target]
        if labels:
            wanted = {l.strip() for l in labels.split(",") if l.strip()}
            rows = [x for x in rows
                    if wanted.issubset(set(x.get("labels", [])))]
        if milestone:
            rows = [x for x in rows
                    if (x.get("milestone") or {}).get("title") == milestone]
        if assignee_id:
            rows = [x for x in rows
                    if assignee_id in [a["id"] for a in x.get("assignees", [])]]
        if author_id:
            rows = [x for x in rows
                    if (x.get("author") or {}).get("id") == author_id]
        if search:
            q = search.lower()
            rows = [x for x in rows
                    if q in (x.get("title") or "").lower()
                    or q in (x.get("description") or "").lower()]
        keyf = {
            "created_at": lambda x: x.get("created_at", ""),
            "updated_at": lambda x: x.get("updated_at", ""),
            "priority": lambda x: x.get("iid", 0),
            "due_date": lambda x: x.get("due_date") or "",
        }.get(order_by, lambda x: x.get("created_at", ""))
        rev = (sort or "desc").lower() != "asc"
        rows.sort(key=keyf, reverse=rev)
        per = max(1, min(int(per_page or 20), 100))
        start = max(0, (int(page or 1) - 1)) * per
        page_items = rows[start:start + per]
        _record(s, "list_issues", project_id=project_id,
                count=len(page_items))
        _save_state(s)
        return [_strip_issue(x) for x in page_items]


@mcp.tool(name="get_issue")
def get_issue(project_id: str | int, issue_iid: int) -> dict:
    """GitLab REST: GET /projects/:id/issues/:issue_iid.

    NOTE: GitLab uses the *project-scoped iid* (not the global `id`)
    here. The returned object exposes both."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "get_issue", project_id=project_id,
                    result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        i = s["issues"].get((pid, int(issue_iid)))
        if not i:
            _record(s, "get_issue", project_id=project_id,
                    issue_iid=issue_iid, result="not_found")
            _save_state(s)
            return _err_404("404 Issue Not Found")
        _record(s, "get_issue", project_id=project_id,
                issue_iid=issue_iid)
        _save_state(s)
        return _strip_issue(i)


@mcp.tool(name="create_issue")
def create_issue(project_id: str | int,
                 title: str,
                 description: str = "",
                 assignee_ids: list | None = None,
                 labels: str | list = "",
                 milestone_id: int | None = None,
                 due_date: str | None = None,
                 confidential: bool = False,
                 issue_type: str = "issue") -> dict:
    """GitLab REST: POST /projects/:id/issues. Returns the new issue
    object. `labels` accepts either a list of strings or a
    comma-separated string (GitLab supports both)."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "create_issue", project_id=project_id,
                    result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        if not title:
            _record(s, "create_issue", project_id=project_id,
                    result="missing_title")
            _save_state(s)
            return _err_400({"title": ["can't be blank"]})
        if isinstance(labels, str):
            label_list = [l.strip() for l in labels.split(",") if l.strip()]
        else:
            label_list = list(labels or [])
        author = s["users"][s["current_user"]["id"]]
        issue = _make_issue(s, s["projects"][pid], title, description,
                            list(assignee_ids or []), label_list,
                            milestone_id, due_date, author,
                            confidential=confidential,
                            issue_type=issue_type)
        _record(s, "create_issue", project_id=pid,
                issue_iid=issue["iid"], issue_id=issue["id"])
        _save_state(s)
        return _strip_issue(issue)


@mcp.tool(name="update_issue")
def update_issue(project_id: str | int,
                 issue_iid: int,
                 title: str | None = None,
                 description: str | None = None,
                 assignee_ids: list | None = None,
                 labels: str | list | None = None,
                 milestone_id: int | None = None,
                 state_event: str | None = None,
                 due_date: str | None = None,
                 confidential: bool | None = None,
                 discussion_locked: bool | None = None) -> dict:
    """GitLab REST: PUT /projects/:id/issues/:issue_iid.

    `state_event` is `close` or `reopen` (matches the real API — there
    is no direct `state` field on update)."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "update_issue", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        i = s["issues"].get((pid, int(issue_iid)))
        if not i:
            _record(s, "update_issue", project_id=pid,
                    issue_iid=issue_iid, result="not_found")
            _save_state(s)
            return _err_404("404 Issue Not Found")
        p = s["projects"][pid]
        if title is not None:
            i["title"] = title
        if description is not None:
            i["description"] = description
        if assignee_ids is not None:
            i["assignees"] = [_user_ref(s["users"][a])
                              for a in assignee_ids if a in s["users"]]
            i["assignee"] = i["assignees"][0] if i["assignees"] else None
        if labels is not None:
            if isinstance(labels, str):
                i["labels"] = [l.strip() for l in labels.split(",")
                              if l.strip()]
            else:
                i["labels"] = list(labels)
        if milestone_id is not None:
            i["milestone"] = ({"id": milestone_id,
                              "title": f"Milestone {milestone_id}"}
                             if milestone_id else None)
        if due_date is not None:
            i["due_date"] = due_date
        if confidential is not None:
            i["confidential"] = bool(confidential)
        if discussion_locked is not None:
            i["discussion_locked"] = bool(discussion_locked)
        if state_event:
            ev = state_event.lower()
            if ev == "close" and i["state"] != "closed":
                i["state"] = "closed"
                i["closed_at"] = _now()
                i["closed_by"] = _user_ref(s["users"][s["current_user"]["id"]])
                p["open_issues_count"] = max(0, p["open_issues_count"] - 1)
            elif ev == "reopen" and i["state"] == "closed":
                i["state"] = "opened"
                i["closed_at"] = None
                i["closed_by"] = None
                p["open_issues_count"] += 1
        i["updated_at"] = _now()
        _record(s, "update_issue", project_id=pid,
                issue_iid=issue_iid, state_event=state_event)
        _save_state(s)
        return _strip_issue(i)


@mcp.tool(name="close_issue")
def close_issue(project_id: str | int, issue_iid: int) -> dict:
    """Close an issue. Convenience wrapper over `update_issue` with
    `state_event=close` (GitLab itself only exposes the update endpoint
    — this matches common MCP-server conveniences)."""
    return update_issue(project_id=project_id, issue_iid=issue_iid,
                        state_event="close")


@mcp.tool(name="add_issue_comment")
def add_issue_comment(project_id: str | int,
                      issue_iid: int,
                      body: str) -> dict:
    """GitLab REST: POST /projects/:id/issues/:issue_iid/notes.
    GitLab calls comments *notes*; this tool name follows the
    GitLab-MCP-server convention. Returns the new Note object."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "add_issue_comment", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        i = s["issues"].get((pid, int(issue_iid)))
        if not i:
            _record(s, "add_issue_comment", project_id=pid,
                    issue_iid=issue_iid, result="not_found")
            _save_state(s)
            return _err_404("404 Issue Not Found")
        if not body:
            _record(s, "add_issue_comment", project_id=pid,
                    issue_iid=issue_iid, result="missing_body")
            _save_state(s)
            return _err_400({"body": ["can't be blank"]})
        nid = _alloc(s, "note")
        now = _now()
        author = _user_ref(s["users"][s["current_user"]["id"]])
        note = {
            "id": nid,
            "type": None,
            "body": body,
            "attachment": None,
            "author": author,
            "created_at": now,
            "updated_at": now,
            "system": False,
            "noteable_id": i["id"],
            "noteable_type": "Issue",
            "noteable_iid": i["iid"],
            "project_id": pid,
            "resolvable": False,
            "confidential": False,
            "internal": False,
        }
        i.setdefault("_notes", []).append(note)
        i["user_notes_count"] = len(i["_notes"])
        i["updated_at"] = now
        _record(s, "add_issue_comment", project_id=pid,
                issue_iid=issue_iid, note_id=nid)
        _save_state(s)
        return note


# ---------------------------------------------------------------------------
# Merge requests
# ---------------------------------------------------------------------------

@mcp.tool(name="list_merge_requests")
def list_merge_requests(project_id: str | int | None = None,
                        state: str = "opened",
                        labels: str = "",
                        milestone: str = "",
                        assignee_id: int | None = None,
                        author_id: int | None = None,
                        source_branch: str = "",
                        target_branch: str = "",
                        search: str = "",
                        order_by: str = "created_at",
                        sort: str = "desc",
                        per_page: int = 20,
                        page: int = 1) -> list:
    """GitLab REST: GET /projects/:id/merge_requests (or GET
    /merge_requests if project omitted). `state` is
    `opened|closed|merged|all`. Returns an array of MR objects."""
    with _lock():
        s = _load_state()
        if project_id is not None:
            pid = _resolve_project_id(s, project_id)
            if pid is None:
                _record(s, "list_merge_requests", result="not_found")
                _save_state(s)
                return _err_404("404 Project Not Found")
            rows = [s["merge_requests"][(pid, iid)]
                    for iid in s["projects"][pid]["_mrs_index"]
                    if (pid, iid) in s["merge_requests"]]
        else:
            rows = list(s["merge_requests"].values())
        if state and state != "all":
            rows = [x for x in rows if x.get("state") == state]
        if labels:
            wanted = {l.strip() for l in labels.split(",") if l.strip()}
            rows = [x for x in rows
                    if wanted.issubset(set(x.get("labels", [])))]
        if milestone:
            rows = [x for x in rows
                    if (x.get("milestone") or {}).get("title") == milestone]
        if assignee_id:
            rows = [x for x in rows
                    if assignee_id in [a["id"]
                                       for a in x.get("assignees", [])]]
        if author_id:
            rows = [x for x in rows
                    if (x.get("author") or {}).get("id") == author_id]
        if source_branch:
            rows = [x for x in rows if x.get("source_branch") == source_branch]
        if target_branch:
            rows = [x for x in rows if x.get("target_branch") == target_branch]
        if search:
            q = search.lower()
            rows = [x for x in rows
                    if q in (x.get("title") or "").lower()
                    or q in (x.get("description") or "").lower()]
        keyf = {
            "created_at": lambda x: x.get("created_at", ""),
            "updated_at": lambda x: x.get("updated_at", ""),
            "priority": lambda x: x.get("iid", 0),
        }.get(order_by, lambda x: x.get("created_at", ""))
        rev = (sort or "desc").lower() != "asc"
        rows.sort(key=keyf, reverse=rev)
        per = max(1, min(int(per_page or 20), 100))
        start = max(0, (int(page or 1) - 1)) * per
        page_items = rows[start:start + per]
        _record(s, "list_merge_requests", project_id=project_id,
                count=len(page_items))
        _save_state(s)
        return [_strip_mr(x) for x in page_items]


@mcp.tool(name="get_merge_request")
def get_merge_request(project_id: str | int,
                      merge_request_iid: int) -> dict:
    """GitLab REST: GET /projects/:id/merge_requests/:merge_request_iid.
    Uses the project-scoped iid, like issues."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "get_merge_request", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        m = s["merge_requests"].get((pid, int(merge_request_iid)))
        if not m:
            _record(s, "get_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="not_found")
            _save_state(s)
            return _err_404("404 Merge Request Not Found")
        _record(s, "get_merge_request", project_id=pid,
                merge_request_iid=merge_request_iid)
        _save_state(s)
        return _strip_mr(m)


@mcp.tool(name="create_merge_request")
def create_merge_request(project_id: str | int,
                         source_branch: str,
                         target_branch: str,
                         title: str,
                         description: str = "",
                         assignee_ids: list | None = None,
                         reviewer_ids: list | None = None,
                         labels: str | list = "",
                         milestone_id: int | None = None,
                         remove_source_branch: bool = False,
                         squash: bool = False,
                         allow_collaboration: bool = False,
                         draft: bool = False) -> dict:
    """GitLab REST: POST /projects/:id/merge_requests."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "create_merge_request", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        if source_branch not in p["_branches"]:
            _record(s, "create_merge_request", project_id=pid,
                    source_branch=source_branch,
                    result="source_branch_not_found")
            _save_state(s)
            return _err_400({"source_branch": ["does not exist"]})
        if target_branch not in p["_branches"]:
            _record(s, "create_merge_request", project_id=pid,
                    target_branch=target_branch,
                    result="target_branch_not_found")
            _save_state(s)
            return _err_400({"target_branch": ["does not exist"]})
        if source_branch == target_branch:
            _record(s, "create_merge_request", project_id=pid,
                    result="same_branch")
            _save_state(s)
            return _err_400({"branch_conflict":
                             ["You can't use same project/branch for source and target"]})
        if isinstance(labels, str):
            label_list = [l.strip() for l in labels.split(",") if l.strip()]
        else:
            label_list = list(labels or [])
        author = s["users"][s["current_user"]["id"]]
        mr = _make_mr(s, p, title, description, source_branch,
                      target_branch, list(assignee_ids or []),
                      list(reviewer_ids or []), label_list,
                      milestone_id, remove_source_branch, squash,
                      allow_collaboration, draft, author)
        _record(s, "create_merge_request", project_id=pid,
                merge_request_iid=mr["iid"], mr_id=mr["id"])
        _save_state(s)
        return _strip_mr(mr)


@mcp.tool(name="update_merge_request")
def update_merge_request(project_id: str | int,
                         merge_request_iid: int,
                         title: str | None = None,
                         description: str | None = None,
                         target_branch: str | None = None,
                         assignee_ids: list | None = None,
                         reviewer_ids: list | None = None,
                         labels: str | list | None = None,
                         milestone_id: int | None = None,
                         state_event: str | None = None,
                         remove_source_branch: bool | None = None,
                         squash: bool | None = None,
                         discussion_locked: bool | None = None,
                         allow_collaboration: bool | None = None) -> dict:
    """GitLab REST: PUT /projects/:id/merge_requests/:merge_request_iid.

    `state_event` is `close` or `reopen`."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "update_merge_request", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        m = s["merge_requests"].get((pid, int(merge_request_iid)))
        if not m:
            _record(s, "update_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="not_found")
            _save_state(s)
            return _err_404("404 Merge Request Not Found")
        if title is not None:
            m["title"] = title
        if description is not None:
            m["description"] = description
        if target_branch is not None:
            m["target_branch"] = target_branch
        if assignee_ids is not None:
            m["assignees"] = [_user_ref(s["users"][a])
                              for a in assignee_ids if a in s["users"]]
            m["assignee"] = m["assignees"][0] if m["assignees"] else None
        if reviewer_ids is not None:
            m["reviewers"] = [_user_ref(s["users"][r])
                              for r in reviewer_ids if r in s["users"]]
        if labels is not None:
            if isinstance(labels, str):
                m["labels"] = [l.strip() for l in labels.split(",")
                              if l.strip()]
            else:
                m["labels"] = list(labels)
        if milestone_id is not None:
            m["milestone"] = ({"id": milestone_id,
                              "title": f"Milestone {milestone_id}"}
                             if milestone_id else None)
        if remove_source_branch is not None:
            m["should_remove_source_branch"] = bool(remove_source_branch)
            m["force_remove_source_branch"] = bool(remove_source_branch)
        if squash is not None:
            m["squash"] = bool(squash)
            m["squash_on_merge"] = bool(squash)
        if discussion_locked is not None:
            m["discussion_locked"] = bool(discussion_locked)
        if allow_collaboration is not None:
            m["allow_collaboration"] = bool(allow_collaboration)
            m["allow_maintainer_to_push"] = bool(allow_collaboration)
        if state_event:
            ev = state_event.lower()
            if ev == "close" and m["state"] != "closed":
                m["state"] = "closed"
                m["closed_at"] = _now()
                m["closed_by"] = _user_ref(s["users"][s["current_user"]["id"]])
            elif ev == "reopen" and m["state"] == "closed":
                m["state"] = "opened"
                m["closed_at"] = None
                m["closed_by"] = None
        m["updated_at"] = _now()
        _record(s, "update_merge_request", project_id=pid,
                merge_request_iid=merge_request_iid,
                state_event=state_event)
        _save_state(s)
        return _strip_mr(m)


@mcp.tool(name="accept_merge_request")
def accept_merge_request(project_id: str | int,
                         merge_request_iid: int,
                         merge_commit_message: str | None = None,
                         squash_commit_message: str | None = None,
                         squash: bool | None = None,
                         should_remove_source_branch: bool | None = None,
                         merge_when_pipeline_succeeds: bool = False,
                         sha: str | None = None) -> dict:
    """GitLab REST: PUT /projects/:id/merge_requests/:iid/merge —
    accept and merge a merge request. Returns the merged MR object."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "accept_merge_request", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        m = s["merge_requests"].get((pid, int(merge_request_iid)))
        if not m:
            _record(s, "accept_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="not_found")
            _save_state(s)
            return _err_404("404 Merge Request Not Found")
        if m.get("state") == "merged":
            _record(s, "accept_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="already_merged")
            _save_state(s)
            return _err_409("405 Method Not Allowed")
        if m.get("state") != "opened":
            _record(s, "accept_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="not_open")
            _save_state(s)
            return _err_409("405 Method Not Allowed")
        if sha and sha != m.get("sha"):
            _record(s, "accept_merge_request", project_id=pid,
                    merge_request_iid=merge_request_iid,
                    result="sha_mismatch")
            _save_state(s)
            return _err_409("409 SHA does not match HEAD of source branch")
        p = s["projects"][pid]
        source = m["source_branch"]
        target = m["target_branch"]
        # Merge files (source onto target) when both branches exist
        if source in p["_files"] and target in p["_files"]:
            src_files = p["_files"][source]
            tgt_files = p["_files"].setdefault(target, {})
            tgt_files.update(src_files)
        # Create a merge commit on the target branch
        author = s["users"][s["current_user"]["id"]]
        msg = (merge_commit_message
               or (squash_commit_message if squash else None)
               or f"Merge branch '{source}' into '{target}'")
        commit = _new_commit(p, target, msg, author)
        m["merged_at"] = _now()
        m["merge_commit_sha"] = commit["id"]
        if squash:
            m["squash_commit_sha"] = commit["id"]
        m["state"] = "merged"
        m["merged_by"] = _user_ref(author)
        m["merge_user"] = _user_ref(author)
        m["updated_at"] = _now()
        # Remove source branch if requested
        remove = (should_remove_source_branch
                  if should_remove_source_branch is not None
                  else m.get("should_remove_source_branch"))
        if remove and source in p["_branches"]:
            p["_branches"].pop(source, None)
            p["_files"].pop(source, None)
        _record(s, "accept_merge_request", project_id=pid,
                merge_request_iid=merge_request_iid,
                merge_commit_sha=commit["id"])
        _save_state(s)
        return _strip_mr(m)


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def _branch_obj(p: dict, br: dict) -> dict:
    """Public Branch object — GitLab embeds a Commit summary
    under `commit`."""
    return {
        "name": br["name"],
        "commit": br.get("commit"),
        "merged": False,
        "protected": br.get("protected", False),
        "developers_can_push": br.get("developers_can_push", False),
        "developers_can_merge": br.get("developers_can_merge", False),
        "can_push": br.get("can_push", True),
        "default": (br["name"] == p["default_branch"]),
        "web_url": br.get("web_url", f"{p['web_url']}/-/tree/{br['name']}"),
    }


@mcp.tool(name="list_branches")
def list_branches(project_id: str | int,
                  search: str = "",
                  per_page: int = 20,
                  page: int = 1) -> list:
    """GitLab REST: GET /projects/:id/repository/branches."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "list_branches", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        branches = list(p["_branches"].values())
        if search:
            q = search.lower()
            branches = [b for b in branches if q in b["name"].lower()]
        branches.sort(key=lambda b: b["name"])
        per = max(1, min(int(per_page or 20), 100))
        start = max(0, (int(page or 1) - 1)) * per
        page_items = branches[start:start + per]
        out = [_branch_obj(p, b) for b in page_items]
        _record(s, "list_branches", project_id=pid, count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_branch")
def get_branch(project_id: str | int, branch: str) -> dict:
    """GitLab REST: GET /projects/:id/repository/branches/:branch."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "get_branch", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        # Branch name in the URL can be URL-encoded.
        name = urllib.parse.unquote(branch)
        br = p["_branches"].get(name)
        if not br:
            _record(s, "get_branch", project_id=pid, branch=name,
                    result="not_found")
            _save_state(s)
            return _err_404("404 Branch Not Found")
        _record(s, "get_branch", project_id=pid, branch=name)
        _save_state(s)
        return _branch_obj(p, br)


@mcp.tool(name="create_branch")
def create_branch(project_id: str | int,
                  branch: str,
                  ref: str) -> dict:
    """GitLab REST: POST /projects/:id/repository/branches.

    `ref` is the source branch / commit SHA / tag to branch from."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "create_branch", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        if branch in p["_branches"]:
            _record(s, "create_branch", project_id=pid, branch=branch,
                    result="exists")
            _save_state(s)
            return _err_400({"message": "Branch already exists"})
        # Resolve ref to a source branch (commit SHAs and tags also accepted)
        src_branch = ref if ref in p["_branches"] else None
        src_commit = None
        if src_branch:
            src_commit = (p["_branches"][src_branch].get("commit") or {})
        elif ref in p["_commits"]:
            src_commit = p["_commits"][ref]
        else:
            _record(s, "create_branch", project_id=pid, branch=branch,
                    ref=ref, result="ref_not_found")
            _save_state(s)
            return _err_400({"message": f"Invalid reference name: {ref}"})
        p["_branches"][branch] = {
            "name": branch,
            "commit": src_commit,
            "protected": False,
            "default": False,
            "developers_can_push": False,
            "developers_can_merge": False,
            "can_push": True,
            "web_url": f"{p['web_url']}/-/tree/{branch}",
        }
        # Copy file tree if branching from another branch
        if src_branch:
            p["_files"][branch] = json.loads(json.dumps(
                p["_files"].get(src_branch, {})))
        else:
            p["_files"][branch] = {}
        _record(s, "create_branch", project_id=pid, branch=branch, ref=ref)
        _save_state(s)
        return _branch_obj(p, p["_branches"][branch])


# ---------------------------------------------------------------------------
# Files / Repository content
# ---------------------------------------------------------------------------

@mcp.tool(name="get_file_contents")
def get_file_contents(project_id: str | int,
                      file_path: str,
                      ref: str | None = None) -> dict:
    """GitLab REST: GET /projects/:id/repository/files/:file_path.

    `file_path` is URL-encoded by the real API (e.g. `src%2Fmain.py`);
    we accept either form. Returns a File object with base64-encoded
    `content` and a `file_name`/`file_path`/`size`/`encoding`/`blob_id`
    bundle, GitLab-style."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "get_file_contents", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        path = urllib.parse.unquote(file_path).lstrip("/")
        branch = ref or p["default_branch"]
        files = p["_files"].get(branch, {})
        info = files.get(path)
        if not info:
            _record(s, "get_file_contents", project_id=pid, path=path,
                    result="not_found")
            _save_state(s)
            return _err_404("404 File Not Found")
        commit = (p["_branches"].get(branch, {}).get("commit")
                  or {})
        out = {
            "file_name": path.rsplit("/", 1)[-1],
            "file_path": path,
            "size": info["size"],
            "encoding": "base64",
            "content": info["content_b64"],
            "content_sha256": hashlib.sha256(
                base64.b64decode(info["content_b64"])).hexdigest(),
            "ref": branch,
            "blob_id": info["sha"],
            "commit_id": commit.get("id"),
            "last_commit_id": commit.get("id"),
            "execute_filemode": False,
        }
        _record(s, "get_file_contents", project_id=pid, path=path,
                ref=branch)
        _save_state(s)
        return out


@mcp.tool(name="create_or_update_file")
def create_or_update_file(project_id: str | int,
                          file_path: str,
                          branch: str,
                          content: str,
                          commit_message: str,
                          encoding: str = "text",
                          start_branch: str | None = None,
                          author_email: str | None = None,
                          author_name: str | None = None) -> dict:
    """Create *or* update a file via a single commit.

    GitLab actually splits this into two endpoints — POST (create) and
    PUT (update) — under
    `/projects/:id/repository/files/:file_path`. This single tool
    matches the convention used by the GitLab MCP server: it picks
    create vs update based on whether the file already exists. Returns
    `{file_path, branch, file_name, commit_id, blob_id}`. `encoding`
    is `text` (raw UTF-8) or `base64`."""
    with _lock():
        s = _load_state()
        pid = _resolve_project_id(s, project_id)
        if pid is None:
            _record(s, "create_or_update_file", result="project_not_found")
            _save_state(s)
            return _err_404("404 Project Not Found")
        p = s["projects"][pid]
        path = urllib.parse.unquote(file_path).lstrip("/")
        try:
            if encoding == "base64":
                raw = base64.b64decode(content)
            else:
                raw = content.encode("utf-8")
        except Exception as exc:
            _record(s, "create_or_update_file", project_id=pid,
                    path=path, result="bad_encoding")
            _save_state(s)
            return _err_400({"content": [f"invalid {encoding}: {exc}"]})
        author = dict(s["users"][s["current_user"]["id"]])
        if author_email:
            author["email"] = author_email
        if author_name:
            author["name"] = author_name
        # If branch doesn't exist and start_branch is provided, create
        # the branch off start_branch first.
        if branch not in p["_branches"] and start_branch:
            if start_branch not in p["_branches"]:
                _record(s, "create_or_update_file", project_id=pid,
                        branch=branch, start_branch=start_branch,
                        result="start_branch_not_found")
                _save_state(s)
                return _err_400({"start_branch":
                                 ["does not exist"]})
            p["_branches"][branch] = {
                "name": branch,
                "commit": p["_branches"][start_branch].get("commit"),
                "protected": False, "default": False,
                "developers_can_push": False,
                "developers_can_merge": False,
                "can_push": True,
                "web_url": f"{p['web_url']}/-/tree/{branch}",
            }
            p["_files"][branch] = json.loads(json.dumps(
                p["_files"].get(start_branch, {})))
        result = _write_file(p, branch, path, raw, commit_message, author)
        _record(s, "create_or_update_file", project_id=pid,
                path=path, branch=branch, commit_id=result["commit_id"])
        _save_state(s)
        return result


# ---------------------------------------------------------------------------
# Mock-only helpers (NOT part of the real GitLab surface)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state. Used by the
    verifier and for fixture inspection."""
    with _lock():
        s = _load_state()
        # Re-encode tuple keys to strings for JSON-friendly output.
        out = dict(s)
        out["issues"] = {f"{pid}|{iid}": v
                         for (pid, iid), v in s.get("issues", {}).items()}
        out["merge_requests"] = {
            f"{pid}|{iid}": v
            for (pid, iid), v in s.get("merge_requests", {}).items()
        }
        return out


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(current_user: dict | None = None,
                    users: list | None = None,
                    projects: list | None = None,
                    issues: list | None = None,
                    merge_requests: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed state with users/projects/issues/MRs.

    Shapes (all keys optional unless marked):

      users:    [{id?, username (req), name?, email?, state?}]
      projects: [{id?, name (req), path?, owner_username?,
                  description?, visibility?, default_branch?,
                  initialize_with_readme?, files?: {path: content_str}}]
      issues:   [{project (id or path, req), title (req), description?,
                  labels?, assignee_usernames?, state?, due_date?}]
      merge_requests:
                [{project (req), title (req), source_branch (req),
                  target_branch (req), description?, labels?,
                  assignee_usernames?, reviewer_usernames?, state?}]

    Returns `{ok, project_ids, user_ids}` for the verifier to grab the
    allocated ids."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if current_user:
            s["current_user"].update(current_user)
            uid = s["current_user"]["id"]
            s["users"][uid] = dict(s["current_user"])
        for u in users or []:
            uid = u.get("id") or _alloc(s, "user")
            user_dict = dict(_DEFAULT_USER)
            user_dict.update({
                "id": uid,
                "username": u["username"],
                "name": u.get("name", u["username"]),
                "email": u.get("email", f"{u['username']}@example.com"),
                "state": u.get("state", "active"),
                "web_url": f"https://gitlab.example.com/{u['username']}",
                "commit_email": u.get("email",
                                      f"{u['username']}@example.com"),
            })
            s["users"][uid] = user_dict
        for proj in projects or []:
            owner_username = proj.get("owner_username") \
                or s["current_user"]["username"]
            owner = next((u for u in s["users"].values()
                          if u["username"] == owner_username), None)
            if not owner:
                continue
            p = _make_project(s, owner, proj["name"],
                             path=proj.get("path"),
                             description=proj.get("description", ""),
                             visibility=proj.get("visibility", "private"),
                             default_branch=proj.get("default_branch",
                                                     "main"),
                             initialize_with_readme=proj.get(
                                 "initialize_with_readme", False))
            for fpath, content in (proj.get("files") or {}).items():
                content_bytes = (content.encode("utf-8")
                                 if isinstance(content, str)
                                 else bytes(content))
                _write_file(p, p["default_branch"], fpath, content_bytes,
                            f"seed: {fpath}", author=owner)
            p["empty_repo"] = not p["_files"].get(p["default_branch"])
        for it in issues or []:
            pid = _resolve_project_id(s, it["project"])
            if pid is None:
                continue
            p = s["projects"][pid]
            author = s["users"][s["current_user"]["id"]]
            assignee_ids = []
            for uname in it.get("assignee_usernames") or []:
                u = next((x for x in s["users"].values()
                          if x["username"] == uname), None)
                if u:
                    assignee_ids.append(u["id"])
            issue = _make_issue(s, p, it["title"], it.get("description", ""),
                               assignee_ids,
                               it.get("labels") or [],
                               it.get("milestone_id"),
                               it.get("due_date"), author,
                               confidential=it.get("confidential", False))
            if it.get("state") == "closed":
                issue["state"] = "closed"
                issue["closed_at"] = _now()
                p["open_issues_count"] = max(0, p["open_issues_count"] - 1)
        for mr_in in merge_requests or []:
            pid = _resolve_project_id(s, mr_in["project"])
            if pid is None:
                continue
            p = s["projects"][pid]
            author = s["users"][s["current_user"]["id"]]
            # Ensure both branches exist (cheap auto-seed for testing).
            for br in (mr_in["source_branch"], mr_in["target_branch"]):
                if br not in p["_branches"]:
                    # branch off default (or create empty default)
                    src = (p["default_branch"]
                           if p["default_branch"] in p["_branches"]
                           else None)
                    p["_branches"][br] = {
                        "name": br,
                        "commit": (p["_branches"][src].get("commit")
                                   if src else None),
                        "protected": False,
                        "default": br == p["default_branch"],
                        "developers_can_push": False,
                        "developers_can_merge": False,
                        "can_push": True,
                        "web_url": f"{p['web_url']}/-/tree/{br}",
                    }
                    p["_files"][br] = json.loads(json.dumps(
                        p["_files"].get(src, {}))) if src else {}
            assignee_ids = []
            for uname in mr_in.get("assignee_usernames") or []:
                u = next((x for x in s["users"].values()
                          if x["username"] == uname), None)
                if u:
                    assignee_ids.append(u["id"])
            reviewer_ids = []
            for uname in mr_in.get("reviewer_usernames") or []:
                u = next((x for x in s["users"].values()
                          if x["username"] == uname), None)
                if u:
                    reviewer_ids.append(u["id"])
            mr = _make_mr(s, p, mr_in["title"],
                         mr_in.get("description", ""),
                         mr_in["source_branch"], mr_in["target_branch"],
                         assignee_ids, reviewer_ids,
                         mr_in.get("labels") or [],
                         mr_in.get("milestone_id"),
                         mr_in.get("remove_source_branch", False),
                         mr_in.get("squash", False),
                         mr_in.get("allow_collaboration", False),
                         mr_in.get("draft", False),
                         author)
            if mr_in.get("state") in ("closed", "merged"):
                mr["state"] = mr_in["state"]
                if mr_in["state"] == "merged":
                    mr["merged_at"] = _now()
                else:
                    mr["closed_at"] = _now()
        _record(s, "debug_seed",
                counts={"users": len(users or []),
                        "projects": len(projects or []),
                        "issues": len(issues or []),
                        "merge_requests": len(merge_requests or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "project_ids": list(s["projects"].keys()),
            "user_ids": list(s["users"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
