"""GitHub mock MCP server.

Mirrors the tool surface of the official `github/github-mcp-server`
(Go) used by Toolathlon — specifically the `lockon-n/github-mcp-server`
fork pinned at commit ef07feb. Tool names and parameter shapes match
that fork verbatim so an agent trained on the real server sees the
same surface.

Backed by a single JSON state file (default
$GITHUB_MOCK_STATE_DIR/state.json) that holds repos, files, branches,
commits, issues, comments, pull requests, and reviews — plus a call
log used by the verifier. File-locked with `fcntl.flock` for
concurrent-safe access. Seed state by setting GITHUB_MOCK_SEED_PATH.

Responses follow GitHub REST API JSON shapes (repository, issue, pull
request, content). Errors are returned as GitHub-shaped error objects
`{"message":"...","status":404,"documentation_url":"..."}` rather than
raised, so the trace looks like a real failed HTTP call.
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
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State storage helpers
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "GITHUB_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/github_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _blob_sha(content: bytes) -> str:
    """Mimic GitHub's git blob SHA: 'blob <size>\\0<content>'."""
    header = f"blob {len(content)}\0".encode("utf-8")
    return _sha1(header + content)


def _commit_sha(parents: list, tree_sha: str, message: str) -> str:
    body = f"{tree_sha}\n{':'.join(parents)}\n{message}\n{_now()}\n{_new_uuid()}".encode()
    return _sha1(body)


_DEFAULT_USER = {
    "login": "mock-user",
    "id": 1,
    "node_id": "U_kgDOAAAAAQ",
    "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
    "html_url": "https://github.com/mock-user",
    "type": "User",
    "site_admin": False,
    "name": "Mock User",
    "company": None,
    "blog": "",
    "location": None,
    "email": "mock-user@example.com",
    "bio": None,
    "twitter_username": None,
    "public_repos": 0,
    "public_gists": 0,
    "followers": 0,
    "following": 0,
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2020-01-01T00:00:00Z",
}


def _empty_state() -> dict:
    return {
        "user": dict(_DEFAULT_USER),
        "repos": {},          # key = "<owner>/<name>"
        "issues": {},         # key = "<owner>/<name>#<number>"
        "pulls": {},          # key = "<owner>/<name>#<number>"
        "next_id": {
            "repo": 1000,
            "issue": 1,
            "issue_number": 1,
            "pull": 1,
            "comment": 1,
            "review": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GITHUB_MOCK_SEED_PATH")
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


def _record(_state: dict, _op: str, **kwargs) -> None:
    """Append an op entry to the call log. Leading underscores avoid
    keyword collisions with caller-supplied fields like `state` or `op`."""
    entry = {"op": _op, "ts": _now()}
    entry.update(kwargs)
    _state["calls"].append(entry)


def _err(status: int, message: str,
         doc_url: str = "https://docs.github.com/rest") -> dict:
    """GitHub-shaped error body; `status` is included so callers can
    inspect what HTTP code the real API would have returned."""
    return {
        "message": message,
        "status": str(status),
        "documentation_url": doc_url,
    }


# ---------------------------------------------------------------------------
# Repository / issue / pull factories
# ---------------------------------------------------------------------------

def _repo_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}"


def _issue_key(owner: str, repo: str, number: int) -> str:
    return f"{owner}/{repo}#{number}"


def _user_obj(login: str) -> dict:
    return {
        "login": login,
        "id": abs(hash(login)) % (10 ** 7),
        "avatar_url": f"https://avatars.githubusercontent.com/u/{abs(hash(login)) % (10 ** 7)}?v=4",
        "html_url": f"https://github.com/{login}",
        "type": "User",
        "site_admin": False,
    }


def _make_repo(state: dict, owner_login: str, name: str,
               description: str = "", private: bool = False,
               auto_init: bool = False, fork: bool = False,
               parent: dict | None = None) -> dict:
    rid = state["next_id"]["repo"]
    state["next_id"]["repo"] += 1
    now = _now()
    owner = _user_obj(owner_login)
    default_branch = "main"
    repo = {
        "id": rid,
        "node_id": f"R_{rid}",
        "name": name,
        "full_name": f"{owner_login}/{name}",
        "owner": owner,
        "private": bool(private),
        "html_url": f"https://github.com/{owner_login}/{name}",
        "description": description or None,
        "fork": bool(fork),
        "url": f"https://api.github.com/repos/{owner_login}/{name}",
        "clone_url": f"https://github.com/{owner_login}/{name}.git",
        "ssh_url": f"git@github.com:{owner_login}/{name}.git",
        "created_at": now,
        "updated_at": now,
        "pushed_at": now,
        "default_branch": default_branch,
        "archived": False,
        "disabled": False,
        "open_issues_count": 0,
        "forks_count": 0,
        "stargazers_count": 0,
        "language": None,
        "topics": [],
        # internal mock state (private fields keep the verifier-visible
        # repo shape clean — _strip_repo removes them on response):
        "files": {},          # branch -> {path: {sha, content, encoding, size}}
        "branches": {},       # branch_name -> {sha, protected}
        "commits": {},        # sha -> commit object
        "commit_order": [],   # ordered shas (newest first)
        "issues_index": [],   # numbers (open + closed)
        "pulls_index": [],
        "next_issue_number": 1,
    }
    if parent:
        repo["parent"] = parent
        repo["source"] = parent
    if auto_init:
        readme_content = f"# {name}\n"
        _write_file(repo, default_branch, "README.md",
                    readme_content.encode("utf-8"),
                    f"Initial commit", author=owner_login)
    return repo


def _strip_repo(repo: dict) -> dict:
    """Return only the user-visible repo fields (no mock internals)."""
    skip = {"files", "branches", "commits", "commit_order",
            "issues_index", "pulls_index", "next_issue_number"}
    return {k: v for k, v in repo.items() if k not in skip}


def _ensure_branch(repo: dict, branch: str) -> dict | None:
    """Return branch dict or None. Creates the default branch if it's
    missing and there are no branches yet (so an empty repo can still
    accept its first commit on the default branch)."""
    if branch in repo["branches"]:
        return repo["branches"][branch]
    if not repo["branches"] and branch == repo["default_branch"]:
        repo["branches"][branch] = {"name": branch, "sha": None,
                                    "protected": False}
        repo["files"][branch] = {}
        return repo["branches"][branch]
    return None


def _write_file(repo: dict, branch: str, path: str, content: bytes,
                message: str, author: str = "mock-user",
                expected_sha: str | None = None,
                committer: str | None = None) -> dict:
    """Write a file (create or update) and create a commit. Returns
    the GitHub `{"content":..., "commit":...}` response shape."""
    br = _ensure_branch(repo, branch)
    if br is None:
        # If from main / no existing branch, create it
        repo["branches"].setdefault(branch, {"name": branch, "sha": None,
                                             "protected": False})
        repo["files"].setdefault(branch, {})
        br = repo["branches"][branch]
    files = repo["files"].setdefault(branch, {})
    sha = _blob_sha(content)
    size = len(content)
    is_update = path in files
    if is_update and expected_sha is not None and files[path]["sha"] != expected_sha:
        return _err(409, "sha does not match")
    files[path] = {
        "sha": sha,
        "size": size,
        "content_b64": base64.b64encode(content).decode("ascii"),
    }
    commit = _new_commit(repo, branch, message, author, committer)
    full = repo["full_name"]
    content_obj = {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": sha,
        "size": size,
        "url": f"https://api.github.com/repos/{full}/contents/{path}?ref={branch}",
        "html_url": f"https://github.com/{full}/blob/{branch}/{path}",
        "git_url": f"https://api.github.com/repos/{full}/git/blobs/{sha}",
        "download_url": f"https://raw.githubusercontent.com/{full}/{branch}/{path}",
        "type": "file",
        "_links": {
            "self": f"https://api.github.com/repos/{full}/contents/{path}?ref={branch}",
            "git": f"https://api.github.com/repos/{full}/git/blobs/{sha}",
            "html": f"https://github.com/{full}/blob/{branch}/{path}",
        },
    }
    return {"content": content_obj, "commit": commit}


def _delete_file_internal(repo: dict, branch: str, path: str,
                          message: str,
                          author: str = "mock-user") -> dict:
    files = repo["files"].setdefault(branch, {})
    if path not in files:
        return _err(404, "Not Found")
    del files[path]
    commit = _new_commit(repo, branch, message, author)
    return {"content": None, "commit": commit}


def _new_commit(repo: dict, branch: str, message: str,
                author_login: str, committer_login: str | None = None) -> dict:
    full = repo["full_name"]
    parent_sha = repo["branches"][branch]["sha"]
    # Build a deterministic-ish tree sha
    files = repo["files"].get(branch, {})
    tree_sha = _sha1(("\n".join(
        f"{p}:{f['sha']}" for p, f in sorted(files.items())
    )).encode("utf-8") or b"empty")
    parents_list = [parent_sha] if parent_sha else []
    sha = _commit_sha(parents_list, tree_sha, message)
    now = _now()
    author = _user_obj(author_login)
    committer = _user_obj(committer_login or author_login)
    commit_obj = {
        "sha": sha,
        "node_id": f"C_{sha[:12]}",
        "url": f"https://api.github.com/repos/{full}/commits/{sha}",
        "html_url": f"https://github.com/{full}/commit/{sha}",
        "author": author,
        "committer": committer,
        "parents": [{"sha": p, "url": f"https://api.github.com/repos/{full}/commits/{p}",
                     "html_url": f"https://github.com/{full}/commit/{p}"}
                    for p in parents_list],
        "commit": {
            "author": {"name": author_login, "email": f"{author_login}@example.com",
                       "date": now},
            "committer": {"name": (committer_login or author_login),
                          "email": f"{committer_login or author_login}@example.com",
                          "date": now},
            "message": message,
            "tree": {"sha": tree_sha,
                     "url": f"https://api.github.com/repos/{full}/git/trees/{tree_sha}"},
            "url": f"https://api.github.com/repos/{full}/git/commits/{sha}",
            "comment_count": 0,
        },
        "files": [],
        "stats": {"additions": 0, "deletions": 0, "total": 0},
    }
    repo["commits"][sha] = commit_obj
    repo["commit_order"].insert(0, sha)
    repo["branches"][branch]["sha"] = sha
    repo["pushed_at"] = now
    repo["updated_at"] = now
    return commit_obj


def _make_issue(state: dict, repo: dict, title: str, body: str,
                assignees: list, labels: list,
                milestone: int | None, issue_type: str | None,
                author: str) -> dict:
    number = repo["next_issue_number"]
    repo["next_issue_number"] += 1
    iid = state["next_id"]["issue"]
    state["next_id"]["issue"] += 1
    now = _now()
    full = repo["full_name"]
    user = _user_obj(author)
    issue = {
        "id": iid,
        "node_id": f"I_{iid}",
        "number": number,
        "title": title,
        "body": body or "",
        "user": user,
        "labels": [{"name": l, "color": "ededed", "default": False}
                   for l in (labels or [])],
        "assignees": [_user_obj(a) for a in (assignees or [])],
        "assignee": _user_obj(assignees[0]) if assignees else None,
        "milestone": ({"number": milestone, "title": f"Milestone {milestone}"}
                      if milestone else None),
        "state": "open",
        "state_reason": None,
        "locked": False,
        "comments": 0,
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "author_association": "OWNER",
        "html_url": f"https://github.com/{full}/issues/{number}",
        "url": f"https://api.github.com/repos/{full}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{full}",
        "_comments": [],   # mock internal
    }
    if issue_type:
        issue["type"] = {"name": issue_type}
    repo["issues_index"].append(number)
    state["issues"][_issue_key(repo["owner"]["login"], repo["name"],
                               number)] = issue
    repo["open_issues_count"] += 1
    return issue


def _strip_issue(issue: dict) -> dict:
    return {k: v for k, v in issue.items() if not k.startswith("_")}


def _make_pull(state: dict, repo: dict, title: str, body: str,
               head: str, base: str, draft: bool,
               maintainer_can_modify: bool, author: str) -> dict:
    number = repo["next_issue_number"]
    repo["next_issue_number"] += 1
    pid = state["next_id"]["pull"]
    state["next_id"]["pull"] += 1
    now = _now()
    full = repo["full_name"]
    user = _user_obj(author)
    head_sha = repo["branches"].get(head, {}).get("sha") or _sha1(head.encode())
    base_sha = repo["branches"].get(base, {}).get("sha") or _sha1(base.encode())
    pull = {
        "id": pid,
        "node_id": f"PR_{pid}",
        "number": number,
        "title": title,
        "body": body or "",
        "state": "open",
        "draft": bool(draft),
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": user,
        "html_url": f"https://github.com/{full}/pull/{number}",
        "url": f"https://api.github.com/repos/{full}/pulls/{number}",
        "diff_url": f"https://github.com/{full}/pull/{number}.diff",
        "patch_url": f"https://github.com/{full}/pull/{number}.patch",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "maintainer_can_modify": bool(maintainer_can_modify),
        "head": {"label": f"{repo['owner']['login']}:{head}",
                 "ref": head, "sha": head_sha,
                 "user": user, "repo": _strip_repo(repo)},
        "base": {"label": f"{repo['owner']['login']}:{base}",
                 "ref": base, "sha": base_sha,
                 "user": user, "repo": _strip_repo(repo)},
        "comments": 0,
        "review_comments": 0,
        "commits": 0,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
        "author_association": "OWNER",
        "labels": [],
        "assignees": [],
        "requested_reviewers": [],
        "_reviews": [],
        "_files": [],
    }
    repo["pulls_index"].append(number)
    state["pulls"][_issue_key(repo["owner"]["login"], repo["name"],
                              number)] = pull
    return pull


def _strip_pull(pull: dict) -> dict:
    return {k: v for k, v in pull.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------

mcp = FastMCP("github-mock")


# ---------- context_tools.go --------------------------------------------------

@mcp.tool(name="get_me")
def get_me() -> dict:
    """GitHub REST: GET /user — get the authenticated user."""
    with _lock():
        s = _load_state()
        _record(s, "get_me")
        _save_state(s)
        return dict(s["user"])


# ---------- repositories.go ---------------------------------------------------

@mcp.tool(name="create_repository")
def create_repository(name: str,
                     description: str | None = None,
                     organization: str | None = None,
                     private: bool = False,
                     autoInit: bool = False) -> dict:
    """GitHub REST: POST /user/repos (or POST /orgs/{org}/repos).
    Create a new GitHub repository."""
    with _lock():
        s = _load_state()
        owner_login = organization or s["user"]["login"]
        key = _repo_key(owner_login, name)
        if key in s["repos"]:
            _record(s, "create_repository", name=name, result="exists")
            _save_state(s)
            return _err(422, "name already exists on this account")
        repo = _make_repo(s, owner_login, name,
                          description=description or "",
                          private=private, auto_init=autoInit)
        s["repos"][key] = repo
        _record(s, "create_repository", full_name=repo["full_name"],
                private=private, auto_init=autoInit)
        _save_state(s)
        return _strip_repo(repo)


@mcp.tool(name="fork_repository")
def fork_repository(owner: str, repo: str,
                    organization: str | None = None,
                    name: str | None = None) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/forks — fork to the
    authenticated user (or `organization`), optionally renaming."""
    with _lock():
        s = _load_state()
        src_key = _repo_key(owner, repo)
        src = s["repos"].get(src_key)
        if not src:
            _record(s, "fork_repository", source=src_key, result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        dst_owner = organization or s["user"]["login"]
        dst_name = name or repo
        dst_key = _repo_key(dst_owner, dst_name)
        if dst_key in s["repos"]:
            _record(s, "fork_repository", source=src_key, dest=dst_key,
                    result="exists")
            _save_state(s)
            return _err(422, "name already exists on this account")
        parent = _strip_repo(src)
        fork = _make_repo(s, dst_owner, dst_name,
                          description=src.get("description") or "",
                          private=src.get("private", False),
                          fork=True, parent=parent)
        # Copy files & branches by reference (deep copy via json roundtrip):
        fork["branches"] = json.loads(json.dumps(src["branches"]))
        fork["files"] = json.loads(json.dumps(src["files"]))
        fork["commits"] = json.loads(json.dumps(src["commits"]))
        fork["commit_order"] = list(src["commit_order"])
        s["repos"][dst_key] = fork
        _record(s, "fork_repository", source=src_key, dest=dst_key)
        _save_state(s)
        return _strip_repo(fork)


@mcp.tool(name="get_file_contents")
def get_file_contents(owner: str, repo: str, path: str = "/",
                     ref: str | None = None,
                     sha: str | None = None) -> dict | list:
    """GitHub REST: GET /repos/{owner}/{repo}/contents/{path}.

    Returns a single file content object (with `content` text or
    base64-encoded) or, if the path is a directory (ends in /),
    a list of directory entries.
    """
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "get_file_contents", path=path, result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        branch = ref or r["default_branch"]
        # Allow ref of form refs/heads/<branch> too.
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        files = r["files"].get(branch, {})
        norm = path.lstrip("/")
        # Directory listing
        if path.endswith("/") or norm == "":
            prefix = norm
            entries: dict[str, dict] = {}
            for fp, info in files.items():
                if not fp.startswith(prefix):
                    continue
                rest = fp[len(prefix):]
                if "/" in rest:
                    sub = rest.split("/", 1)[0]
                    entries.setdefault(sub, {
                        "name": sub,
                        "path": prefix + sub,
                        "sha": _sha1((prefix + sub).encode())[:40],
                        "size": 0,
                        "type": "dir",
                        "url": f"https://api.github.com/repos/{owner}/{repo}/contents/{prefix + sub}?ref={branch}",
                        "html_url": f"https://github.com/{owner}/{repo}/tree/{branch}/{prefix + sub}",
                        "git_url": "",
                        "download_url": None,
                    })
                else:
                    entries[rest] = {
                        "name": rest,
                        "path": fp,
                        "sha": info["sha"],
                        "size": info["size"],
                        "type": "file",
                        "url": f"https://api.github.com/repos/{owner}/{repo}/contents/{fp}?ref={branch}",
                        "html_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{fp}",
                        "git_url": f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{info['sha']}",
                        "download_url": f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fp}",
                    }
            _record(s, "get_file_contents", path=path, kind="dir",
                    count=len(entries))
            _save_state(s)
            return list(entries.values())
        # File
        info = files.get(norm)
        if not info:
            _record(s, "get_file_contents", path=path, result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        raw = base64.b64decode(info["content_b64"])
        # Best-effort text detection
        try:
            txt = raw.decode("utf-8")
            content_str = txt
            encoding = "text"
        except UnicodeDecodeError:
            content_str = info["content_b64"]
            encoding = "base64"
        out = {
            "name": norm.rsplit("/", 1)[-1],
            "path": norm,
            "sha": info["sha"],
            "size": info["size"],
            "type": "file",
            "encoding": encoding,
            "content": content_str,
            "url": f"https://api.github.com/repos/{owner}/{repo}/contents/{norm}?ref={branch}",
            "html_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{norm}",
            "git_url": f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{info['sha']}",
            "download_url": f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{norm}",
        }
        _record(s, "get_file_contents", path=norm, kind="file")
        _save_state(s)
        return out


@mcp.tool(name="create_or_update_file")
def create_or_update_file(owner: str, repo: str, path: str,
                          content: str, message: str, branch: str,
                          sha: str | None = None) -> dict:
    """GitHub REST: PUT /repos/{owner}/{repo}/contents/{path}.

    `content` is a raw UTF-8 string (the official server takes a raw
    string and base64-encodes for the API). Provide `sha` when
    updating an existing file."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "create_or_update_file", path=path,
                    result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        result = _write_file(r, branch, path.lstrip("/"),
                             content.encode("utf-8"), message,
                             author=s["user"]["login"],
                             expected_sha=sha)
        _record(s, "create_or_update_file",
                full_name=r["full_name"], path=path, branch=branch,
                sha=result.get("content", {}).get("sha")
                if isinstance(result, dict) and "content" in result else None)
        _save_state(s)
        return result


@mcp.tool(name="delete_file")
def delete_file(owner: str, repo: str, path: str,
                message: str, branch: str) -> dict:
    """GitHub REST: DELETE /repos/{owner}/{repo}/contents/{path}.
    Delete a file via a new commit on `branch`."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "delete_file", path=path, result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        result = _delete_file_internal(r, branch, path.lstrip("/"),
                                       message, author=s["user"]["login"])
        _record(s, "delete_file", full_name=r["full_name"],
                path=path, branch=branch,
                result="ok" if "commit" in result else "not_found")
        _save_state(s)
        return result


@mcp.tool(name="push_files")
def push_files(owner: str, repo: str, branch: str,
               files: list, message: str) -> dict:
    """GitHub REST: composite — push multiple files in a single
    commit. `files` is a list of {"path": str, "content": str}
    objects. Returns the new branch ref."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "push_files", branch=branch, result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        if not isinstance(files, list) or not files:
            _record(s, "push_files", branch=branch, result="bad_files")
            _save_state(s)
            return _err(422, "files must be a non-empty array")
        br = _ensure_branch(r, branch) or r["branches"].setdefault(
            branch, {"name": branch, "sha": None, "protected": False})
        r["files"].setdefault(branch, {})
        for f in files:
            if not isinstance(f, dict) or "path" not in f or "content" not in f:
                _record(s, "push_files", branch=branch, result="bad_file_entry")
                _save_state(s)
                return _err(422, "each file must be an object with path + content")
            content_bytes = f["content"].encode("utf-8") if isinstance(
                f["content"], str) else bytes(f["content"])
            r["files"][branch][f["path"].lstrip("/")] = {
                "sha": _blob_sha(content_bytes),
                "size": len(content_bytes),
                "content_b64": base64.b64encode(content_bytes).decode("ascii"),
            }
        commit = _new_commit(r, branch, message, s["user"]["login"])
        ref_obj = {
            "ref": f"refs/heads/{branch}",
            "node_id": f"REF_{branch}",
            "url": f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
            "object": {
                "sha": commit["sha"],
                "type": "commit",
                "url": commit["url"],
            },
        }
        _record(s, "push_files", full_name=r["full_name"], branch=branch,
                count=len(files), commit_sha=commit["sha"])
        _save_state(s)
        return ref_obj


@mcp.tool(name="list_branches")
def list_branches(owner: str, repo: str,
                  page: int = 1, perPage: int = 30) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/branches."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "list_branches", result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        branches = sorted(r["branches"].values(), key=lambda b: b["name"])
        start = max(0, (page - 1)) * max(1, perPage)
        page_items = branches[start:start + max(1, perPage)]
        out = [{
            "name": b["name"],
            "commit": {
                "sha": b.get("sha"),
                "url": f"https://api.github.com/repos/{owner}/{repo}/commits/{b.get('sha')}",
            },
            "protected": b.get("protected", False),
        } for b in page_items]
        _record(s, "list_branches", full_name=r["full_name"],
                count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="create_branch")
def create_branch(owner: str, repo: str, branch: str,
                  from_branch: str | None = None) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/git/refs — create a
    new branch off `from_branch` (defaults to the repo's default branch)."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "create_branch", branch=branch, result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        if branch in r["branches"]:
            _record(s, "create_branch", branch=branch, result="exists")
            _save_state(s)
            return _err(422, f"Reference already exists: refs/heads/{branch}")
        src = from_branch or r["default_branch"]
        src_branch = r["branches"].get(src)
        if not src_branch:
            _record(s, "create_branch", branch=branch, source=src,
                    result="source_not_found")
            _save_state(s)
            return _err(404, f"source branch not found: {src}")
        r["branches"][branch] = {"name": branch, "sha": src_branch["sha"],
                                 "protected": False}
        # Copy file tree
        r["files"][branch] = json.loads(json.dumps(r["files"].get(src, {})))
        ref_obj = {
            "ref": f"refs/heads/{branch}",
            "node_id": f"REF_{branch}",
            "url": f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
            "object": {
                "sha": src_branch["sha"],
                "type": "commit",
                "url": f"https://api.github.com/repos/{owner}/{repo}/commits/{src_branch['sha']}",
            },
        }
        _record(s, "create_branch", full_name=r["full_name"],
                branch=branch, from_branch=src)
        _save_state(s)
        return ref_obj


@mcp.tool(name="list_commits")
def list_commits(owner: str, repo: str,
                 sha: str | None = None,
                 author: str | None = None,
                 page: int = 1, perPage: int = 30) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/commits."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "list_commits", result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        # If sha is a branch name, resolve it to that branch's tip.
        order = list(r["commit_order"])
        if sha:
            if sha in r["branches"]:
                tip = r["branches"][sha]["sha"]
            elif sha in r["commits"]:
                tip = sha
            else:
                _record(s, "list_commits", sha=sha, result="ref_not_found")
                _save_state(s)
                return _err(404, "Not Found")
            if tip in order:
                idx = order.index(tip)
                order = order[idx:]
        commits = [r["commits"][cs] for cs in order]
        if author:
            commits = [c for c in commits
                       if c["commit"]["author"]["name"] == author
                       or (c.get("author") or {}).get("login") == author]
        start = max(0, (page - 1)) * max(1, perPage)
        commits = commits[start:start + max(1, perPage)]
        _record(s, "list_commits", full_name=r["full_name"],
                count=len(commits))
        _save_state(s)
        return commits


@mcp.tool(name="get_commit")
def get_commit(owner: str, repo: str, sha: str,
               include_diff: bool = True,
               page: int = 1, perPage: int = 30) -> dict:
    """GitHub REST: GET /repos/{owner}/{repo}/commits/{ref}.
    `sha` can be a commit SHA, branch name, or tag name."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "get_commit", result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        if sha in r["branches"]:
            sha = r["branches"][sha]["sha"]
        c = r["commits"].get(sha)
        if not c:
            _record(s, "get_commit", sha=sha, result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        out = dict(c)
        if not include_diff:
            out.pop("files", None)
            out.pop("stats", None)
        _record(s, "get_commit", full_name=r["full_name"], sha=sha)
        _save_state(s)
        return out


# ---------- issues.go ---------------------------------------------------------

@mcp.tool(name="get_issue")
def get_issue(owner: str, repo: str, issue_number: int) -> dict:
    """GitHub REST: GET /repos/{owner}/{repo}/issues/{issue_number}."""
    with _lock():
        s = _load_state()
        i = s["issues"].get(_issue_key(owner, repo, int(issue_number)))
        if not i:
            _record(s, "get_issue", owner=owner, repo=repo,
                    issue_number=issue_number, result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        _record(s, "get_issue", owner=owner, repo=repo,
                issue_number=issue_number)
        _save_state(s)
        return _strip_issue(i)


@mcp.tool(name="list_issues")
def list_issues(owner: str, repo: str,
                state: str | None = None,
                labels: list | None = None,
                orderBy: str | None = None,
                direction: str | None = None,
                since: str | None = None,
                after: str | None = None,
                perPage: int = 30) -> dict:
    """GitHub REST (GraphQL-shaped per the fork): list issues in a
    repo. Returns `{"items": [...], "pageInfo": {...}, "totalCount": N}`.
    `state` is `OPEN` or `CLOSED` (uppercase, GraphQL enum)."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "list_issues", result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        rows = []
        for num in r["issues_index"]:
            it = s["issues"].get(_issue_key(owner, repo, num))
            if it:
                rows.append(it)
        if state and state.upper() != "ALL":
            target = "open" if state.upper() == "OPEN" else "closed"
            rows = [x for x in rows if x.get("state") == target]
        if labels:
            wanted = set(labels)
            rows = [x for x in rows
                    if wanted.issubset({l["name"] for l in x.get("labels", [])})]
        if since:
            rows = [x for x in rows if x.get("updated_at", "") >= since]
        ob = (orderBy or "CREATED_AT").upper()
        key = {
            "CREATED_AT": lambda x: x.get("created_at", ""),
            "UPDATED_AT": lambda x: x.get("updated_at", ""),
            "COMMENTS": lambda x: x.get("comments", 0),
        }.get(ob, lambda x: x.get("created_at", ""))
        rev = (direction or "DESC").upper() == "DESC"
        rows.sort(key=key, reverse=rev)
        per = max(1, int(perPage or 30))
        start = 0
        if after:
            for idx, x in enumerate(rows):
                if str(x["id"]) == str(after):
                    start = idx + 1
                    break
        page = rows[start:start + per]
        end_cursor = str(page[-1]["id"]) if page else None
        has_next = (start + per) < len(rows)
        _record(s, "list_issues", full_name=r["full_name"], count=len(page))
        _save_state(s)
        return {
            "items": [_strip_issue(x) for x in page],
            "pageInfo": {
                "hasNextPage": has_next,
                "hasPreviousPage": start > 0,
                "startCursor": str(page[0]["id"]) if page else None,
                "endCursor": end_cursor,
            },
            "totalCount": len(rows),
        }


@mcp.tool(name="create_issue")
def create_issue(owner: str, repo: str, title: str,
                 body: str | None = None,
                 assignees: list | None = None,
                 labels: list | None = None,
                 milestone: int | None = None,
                 type: str | None = None) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/issues."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "create_issue", result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        issue = _make_issue(s, r, title, body or "",
                            assignees or [], labels or [],
                            milestone, type, s["user"]["login"])
        _record(s, "create_issue", full_name=r["full_name"],
                issue_number=issue["number"])
        _save_state(s)
        return _strip_issue(issue)


@mcp.tool(name="update_issue")
def update_issue(owner: str, repo: str, issue_number: int,
                 title: str | None = None,
                 body: str | None = None,
                 state: str | None = None,
                 labels: list | None = None,
                 assignees: list | None = None,
                 milestone: int | None = None,
                 type: str | None = None) -> dict:
    """GitHub REST: PATCH /repos/{owner}/{repo}/issues/{issue_number}."""
    with _lock():
        s = _load_state()
        i = s["issues"].get(_issue_key(owner, repo, int(issue_number)))
        if not i:
            _record(s, "update_issue", result="not_found",
                    issue_number=issue_number)
            _save_state(s)
            return _err(404, "Not Found")
        r = s["repos"].get(_repo_key(owner, repo))
        if title is not None and title != "":
            i["title"] = title
        if body is not None:
            i["body"] = body
        if state is not None and state != "":
            old = i.get("state")
            i["state"] = state
            if state == "closed" and old != "closed":
                i["closed_at"] = _now()
                if r:
                    r["open_issues_count"] = max(0, r["open_issues_count"] - 1)
            elif state == "open" and old == "closed":
                i["closed_at"] = None
                if r:
                    r["open_issues_count"] += 1
        if labels is not None:
            i["labels"] = [{"name": l, "color": "ededed", "default": False}
                           for l in labels]
        if assignees is not None:
            i["assignees"] = [_user_obj(a) for a in assignees]
            i["assignee"] = _user_obj(assignees[0]) if assignees else None
        if milestone is not None and milestone != 0:
            i["milestone"] = {"number": milestone,
                              "title": f"Milestone {milestone}"}
        if type:
            i["type"] = {"name": type}
        i["updated_at"] = _now()
        _record(s, "update_issue", owner=owner, repo=repo,
                issue_number=issue_number, state=state)
        _save_state(s)
        return _strip_issue(i)


@mcp.tool(name="add_issue_comment")
def add_issue_comment(owner: str, repo: str,
                      issue_number: int, body: str) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/issues/{issue_number}/comments."""
    with _lock():
        s = _load_state()
        i = s["issues"].get(_issue_key(owner, repo, int(issue_number)))
        if not i:
            _record(s, "add_issue_comment", result="not_found",
                    issue_number=issue_number)
            _save_state(s)
            return _err(404, "Not Found")
        cid = s["next_id"]["comment"]
        s["next_id"]["comment"] += 1
        now = _now()
        comment = {
            "id": cid,
            "node_id": f"IC_{cid}",
            "url": f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{cid}",
            "html_url": f"https://github.com/{owner}/{repo}/issues/{issue_number}#issuecomment-{cid}",
            "issue_url": f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
            "body": body,
            "user": dict(s["user"]),
            "created_at": now,
            "updated_at": now,
            "author_association": "OWNER",
        }
        i.setdefault("_comments", []).append(comment)
        i["comments"] = len(i["_comments"])
        i["updated_at"] = now
        _record(s, "add_issue_comment", owner=owner, repo=repo,
                issue_number=issue_number, comment_id=cid)
        _save_state(s)
        return comment


@mcp.tool(name="get_issue_comments")
def get_issue_comments(owner: str, repo: str, issue_number: int,
                       page: int = 1, perPage: int = 30) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/issues/{issue_number}/comments."""
    with _lock():
        s = _load_state()
        i = s["issues"].get(_issue_key(owner, repo, int(issue_number)))
        if not i:
            _record(s, "get_issue_comments", result="not_found",
                    issue_number=issue_number)
            _save_state(s)
            return _err(404, "Not Found")
        rows = list(i.get("_comments", []))
        start = max(0, (page - 1)) * max(1, perPage)
        out = rows[start:start + max(1, perPage)]
        _record(s, "get_issue_comments", owner=owner, repo=repo,
                issue_number=issue_number, count=len(out))
        _save_state(s)
        return out


# ---------- pullrequests.go --------------------------------------------------

@mcp.tool(name="get_pull_request")
def get_pull_request(owner: str, repo: str, pullNumber: int) -> dict:
    """GitHub REST: GET /repos/{owner}/{repo}/pulls/{pull_number}.
    NOTE: parameter is `pullNumber` (camelCase) per the official server."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "get_pull_request", result="not_found",
                    pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        _record(s, "get_pull_request", owner=owner, repo=repo,
                pullNumber=pullNumber)
        _save_state(s)
        return _strip_pull(p)


@mcp.tool(name="create_pull_request")
def create_pull_request(owner: str, repo: str, title: str,
                        head: str, base: str,
                        body: str | None = None,
                        draft: bool = False,
                        maintainer_can_modify: bool = False) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/pulls."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "create_pull_request", result="repo_not_found")
            _save_state(s)
            return _err(404, "Not Found")
        pull = _make_pull(s, r, title, body or "", head, base,
                          draft, maintainer_can_modify, s["user"]["login"])
        _record(s, "create_pull_request", full_name=r["full_name"],
                pullNumber=pull["number"], head=head, base=base)
        _save_state(s)
        return _strip_pull(pull)


@mcp.tool(name="update_pull_request")
def update_pull_request(owner: str, repo: str, pullNumber: int,
                        title: str | None = None,
                        body: str | None = None,
                        state: str | None = None,
                        base: str | None = None,
                        draft: bool | None = None,
                        maintainer_can_modify: bool | None = None,
                        reviewers: list | None = None) -> dict:
    """GitHub REST: PATCH /repos/{owner}/{repo}/pulls/{pull_number}."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "update_pull_request", result="not_found",
                    pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        if title is not None and title != "":
            p["title"] = title
        if body is not None:
            p["body"] = body
        if state is not None and state != "":
            p["state"] = state
            if state == "closed":
                p["closed_at"] = _now()
        if base is not None and base != "":
            p["base"]["ref"] = base
        if draft is not None:
            p["draft"] = bool(draft)
        if maintainer_can_modify is not None:
            p["maintainer_can_modify"] = bool(maintainer_can_modify)
        if reviewers is not None:
            p["requested_reviewers"] = [_user_obj(u) for u in reviewers]
        p["updated_at"] = _now()
        _record(s, "update_pull_request", owner=owner, repo=repo,
                pullNumber=pullNumber)
        _save_state(s)
        return _strip_pull(p)


@mcp.tool(name="list_pull_requests")
def list_pull_requests(owner: str, repo: str,
                       state: str | None = None,
                       head: str | None = None,
                       base: str | None = None,
                       sort: str | None = None,
                       direction: str | None = None,
                       page: int = 1, perPage: int = 30) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/pulls."""
    with _lock():
        s = _load_state()
        r = s["repos"].get(_repo_key(owner, repo))
        if not r:
            _record(s, "list_pull_requests", result="not_found")
            _save_state(s)
            return _err(404, "Not Found")
        rows = []
        for n in r["pulls_index"]:
            p = s["pulls"].get(_issue_key(owner, repo, n))
            if p:
                rows.append(p)
        if state and state != "all":
            rows = [x for x in rows if x.get("state") == state]
        if head:
            rows = [x for x in rows if x["head"]["ref"] == head
                    or x["head"]["label"] == head]
        if base:
            rows = [x for x in rows if x["base"]["ref"] == base]
        sort_key = (sort or "created").lower()
        keyf = {
            "created": lambda x: x.get("created_at", ""),
            "updated": lambda x: x.get("updated_at", ""),
        }.get(sort_key, lambda x: x.get("created_at", ""))
        rev = (direction or "desc").lower() == "desc"
        rows.sort(key=keyf, reverse=rev)
        start = max(0, (page - 1)) * max(1, perPage)
        page_items = rows[start:start + max(1, perPage)]
        _record(s, "list_pull_requests", full_name=r["full_name"],
                count=len(page_items))
        _save_state(s)
        return [_strip_pull(x) for x in page_items]


@mcp.tool(name="merge_pull_request")
def merge_pull_request(owner: str, repo: str, pullNumber: int,
                       commit_title: str | None = None,
                       commit_message: str | None = None,
                       merge_method: str | None = None) -> dict:
    """GitHub REST: PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "merge_pull_request", result="not_found",
                    pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        if p.get("merged"):
            _record(s, "merge_pull_request", result="already_merged")
            _save_state(s)
            return _err(405, "Pull request already merged")
        if p.get("state") != "open":
            _record(s, "merge_pull_request", result="not_open")
            _save_state(s)
            return _err(405, "Pull request is not open")
        r = s["repos"].get(_repo_key(owner, repo))
        head_branch = p["head"]["ref"]
        base_branch = p["base"]["ref"]
        merge_sha = _sha1(
            f"merge:{p['id']}:{head_branch}:{base_branch}:{_now()}".encode()
        )
        # Merge file trees (head onto base) when we have them
        if r and head_branch in r.get("files", {}):
            head_files = r["files"][head_branch]
            base_files = r["files"].setdefault(base_branch, {})
            base_files.update(head_files)
            if base_branch in r["branches"]:
                r["branches"][base_branch]["sha"] = merge_sha
            commit_msg = (commit_message or commit_title
                          or f"Merge pull request #{pullNumber}")
            full = r["full_name"]
            r["commits"][merge_sha] = {
                "sha": merge_sha,
                "node_id": f"C_{merge_sha[:12]}",
                "url": f"https://api.github.com/repos/{full}/commits/{merge_sha}",
                "html_url": f"https://github.com/{full}/commit/{merge_sha}",
                "commit": {
                    "author": {"name": s["user"]["login"], "date": _now()},
                    "committer": {"name": s["user"]["login"], "date": _now()},
                    "message": commit_msg,
                },
                "parents": [],
            }
            r["commit_order"].insert(0, merge_sha)
        p["merged"] = True
        p["merged_at"] = _now()
        p["merge_commit_sha"] = merge_sha
        p["state"] = "closed"
        p["closed_at"] = _now()
        p["updated_at"] = _now()
        out = {
            "sha": merge_sha,
            "merged": True,
            "message": "Pull Request successfully merged",
        }
        _record(s, "merge_pull_request", owner=owner, repo=repo,
                pullNumber=pullNumber, sha=merge_sha,
                merge_method=merge_method or "merge")
        _save_state(s)
        return out


@mcp.tool(name="get_pull_request_files")
def get_pull_request_files(owner: str, repo: str, pullNumber: int,
                           page: int = 1, perPage: int = 30) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/pulls/{pull_number}/files."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "get_pull_request_files", result="not_found",
                    pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        r = s["repos"].get(_repo_key(owner, repo))
        files = list(p.get("_files", []))
        # If we have no pre-recorded files, synthesize from branch diff.
        if not files and r:
            head_files = r["files"].get(p["head"]["ref"], {})
            base_files = r["files"].get(p["base"]["ref"], {})
            for path, info in head_files.items():
                if base_files.get(path, {}).get("sha") == info["sha"]:
                    continue
                status = "added" if path not in base_files else "modified"
                files.append({
                    "sha": info["sha"],
                    "filename": path,
                    "status": status,
                    "additions": info["size"],
                    "deletions": 0,
                    "changes": info["size"],
                    "blob_url": f"https://github.com/{owner}/{repo}/blob/{p['head']['ref']}/{path}",
                    "raw_url": f"https://raw.githubusercontent.com/{owner}/{repo}/{p['head']['ref']}/{path}",
                    "contents_url": f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={p['head']['ref']}",
                    "patch": "",
                })
            for path in base_files:
                if path not in head_files:
                    files.append({
                        "sha": base_files[path]["sha"],
                        "filename": path,
                        "status": "removed",
                        "additions": 0,
                        "deletions": base_files[path]["size"],
                        "changes": base_files[path]["size"],
                    })
        start = max(0, (page - 1)) * max(1, perPage)
        out = files[start:start + max(1, perPage)]
        _record(s, "get_pull_request_files", owner=owner, repo=repo,
                pullNumber=pullNumber, count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_pull_request_reviews")
def get_pull_request_reviews(owner: str, repo: str, pullNumber: int) -> list:
    """GitHub REST: GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "get_pull_request_reviews", result="not_found",
                    pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        rows = list(p.get("_reviews", []))
        _record(s, "get_pull_request_reviews", owner=owner, repo=repo,
                pullNumber=pullNumber, count=len(rows))
        _save_state(s)
        return rows


@mcp.tool(name="create_and_submit_pull_request_review")
def create_and_submit_pull_request_review(owner: str, repo: str,
                                          pullNumber: int,
                                          body: str, event: str,
                                          commitID: str | None = None) -> dict:
    """GitHub REST: POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews
    (creates a review and submits it in one step). `event` is one of
    `APPROVE`, `REQUEST_CHANGES`, `COMMENT`."""
    with _lock():
        s = _load_state()
        p = s["pulls"].get(_issue_key(owner, repo, int(pullNumber)))
        if not p:
            _record(s, "create_and_submit_pull_request_review",
                    result="not_found", pullNumber=pullNumber)
            _save_state(s)
            return _err(404, "Not Found")
        rid = s["next_id"]["review"]
        s["next_id"]["review"] += 1
        now = _now()
        review = {
            "id": rid,
            "node_id": f"PRR_{rid}",
            "user": dict(s["user"]),
            "body": body,
            "state": {"APPROVE": "APPROVED",
                      "REQUEST_CHANGES": "CHANGES_REQUESTED",
                      "COMMENT": "COMMENTED"}.get(event.upper(), event.upper()),
            "html_url": f"https://github.com/{owner}/{repo}/pull/{pullNumber}#pullrequestreview-{rid}",
            "pull_request_url": f"https://api.github.com/repos/{owner}/{repo}/pulls/{pullNumber}",
            "author_association": "OWNER",
            "submitted_at": now,
            "commit_id": commitID or p["head"]["sha"],
        }
        p.setdefault("_reviews", []).append(review)
        p["updated_at"] = now
        _record(s, "create_and_submit_pull_request_review",
                owner=owner, repo=repo, pullNumber=pullNumber,
                review_id=rid, event=event)
        _save_state(s)
        return review


# ---------- search.go --------------------------------------------------------

def _matches_query(text: str, query: str) -> bool:
    """Best-effort GitHub query matcher: strip qualifiers like
    `is:open`, `repo:owner/name`, `in:name`, then substring match
    the remaining tokens."""
    tokens = [t for t in query.split() if ":" not in t]
    needle = " ".join(tokens).strip().lower()
    if not needle:
        return True
    return needle in text.lower()


def _extract_qualifier(query: str, name: str) -> str | None:
    m = re.search(rf"\b{name}:(\S+)", query)
    return m.group(1) if m else None


@mcp.tool(name="search_repositories")
def search_repositories(query: str, minimal_output: bool = True,
                        page: int = 1, perPage: int = 30) -> dict:
    """GitHub REST: GET /search/repositories?q={query}.
    Supports `user:<login>`, `org:<login>`, `in:name`, plain substring
    on name+description+topics."""
    with _lock():
        s = _load_state()
        user_q = _extract_qualifier(query, "user") or \
                 _extract_qualifier(query, "org")
        items = []
        for r in s["repos"].values():
            if user_q and r["owner"]["login"] != user_q:
                continue
            text = " ".join([
                r["name"], r["full_name"], r.get("description") or "",
                " ".join(r.get("topics", []))
            ])
            if not _matches_query(text, query):
                continue
            items.append(r)
        start = max(0, (page - 1)) * max(1, perPage)
        page_items = items[start:start + max(1, perPage)]
        if minimal_output:
            payload = [{
                "id": r["id"], "name": r["name"],
                "full_name": r["full_name"],
                "description": r.get("description"),
                "html_url": r["html_url"],
                "language": r.get("language"),
                "stargazers_count": r.get("stargazers_count", 0),
                "forks_count": r.get("forks_count", 0),
                "open_issues_count": r.get("open_issues_count", 0),
                "private": r.get("private", False),
                "fork": r.get("fork", False),
                "archived": r.get("archived", False),
                "default_branch": r.get("default_branch", "main"),
                "updated_at": r.get("updated_at"),
                "created_at": r.get("created_at"),
                "topics": r.get("topics", []),
            } for r in page_items]
        else:
            payload = [_strip_repo(r) for r in page_items]
        _record(s, "search_repositories", query=query, count=len(page_items))
        _save_state(s)
        return {
            "total_count": len(items),
            "incomplete_results": False,
            "items": payload,
        }


@mcp.tool(name="search_code")
def search_code(query: str,
                sort: str | None = None,
                order: str | None = None,
                page: int = 1, perPage: int = 30) -> dict:
    """GitHub REST: GET /search/code?q={query}. Searches file content
    + paths across all repos in state. Supports `repo:owner/name` and
    `language:` (no-op) qualifiers; plain substring elsewhere."""
    with _lock():
        s = _load_state()
        repo_q = _extract_qualifier(query, "repo")
        items = []
        for key, r in s["repos"].items():
            if repo_q and r["full_name"] != repo_q:
                continue
            for branch, files in r.get("files", {}).items():
                if branch != r["default_branch"]:
                    continue
                for path, info in files.items():
                    try:
                        text = base64.b64decode(
                            info["content_b64"]).decode("utf-8", "ignore")
                    except Exception:
                        text = ""
                    haystack = f"{path}\n{text}"
                    if _matches_query(haystack, query):
                        items.append({
                            "name": path.rsplit("/", 1)[-1],
                            "path": path,
                            "sha": info["sha"],
                            "url": f"https://api.github.com/repos/{r['full_name']}/contents/{path}",
                            "git_url": f"https://api.github.com/repos/{r['full_name']}/git/blobs/{info['sha']}",
                            "html_url": f"https://github.com/{r['full_name']}/blob/{branch}/{path}",
                            "repository": {
                                "id": r["id"],
                                "name": r["name"],
                                "full_name": r["full_name"],
                                "owner": r["owner"],
                                "private": r["private"],
                                "html_url": r["html_url"],
                            },
                            "score": 1.0,
                        })
        start = max(0, (page - 1)) * max(1, perPage)
        page_items = items[start:start + max(1, perPage)]
        _record(s, "search_code", query=query, count=len(page_items))
        _save_state(s)
        return {
            "total_count": len(items),
            "incomplete_results": False,
            "items": page_items,
        }


@mcp.tool(name="search_issues")
def search_issues(query: str,
                  owner: str | None = None,
                  repo: str | None = None,
                  sort: str | None = None,
                  order: str | None = None,
                  page: int = 1, perPage: int = 30) -> dict:
    """GitHub REST: GET /search/issues?q={query}. Always scoped to
    `is:issue` (matching the official server). `owner` + `repo` narrow
    the search; query also supports `is:open`/`is:closed`/`label:`."""
    with _lock():
        s = _load_state()
        scope_repo = None
        if owner and repo:
            scope_repo = _repo_key(owner, repo)
        else:
            scope_repo = _extract_qualifier(query, "repo")
        state_q = _extract_qualifier(query, "is")
        label_q = _extract_qualifier(query, "label")
        items = []
        for key, i in s["issues"].items():
            r_key = key.rsplit("#", 1)[0]
            if scope_repo and r_key != scope_repo:
                continue
            if state_q == "open" and i.get("state") != "open":
                continue
            if state_q == "closed" and i.get("state") != "closed":
                continue
            if label_q and label_q not in {l["name"] for l in i.get("labels", [])}:
                continue
            text = f"{i.get('title', '')}\n{i.get('body', '')}"
            if not _matches_query(text, query):
                continue
            items.append(_strip_issue(i))
        start = max(0, (page - 1)) * max(1, perPage)
        page_items = items[start:start + max(1, perPage)]
        _record(s, "search_issues", query=query, count=len(page_items))
        _save_state(s)
        return {
            "total_count": len(items),
            "incomplete_results": False,
            "items": page_items,
        }


# ---------- mock-only debug surface ------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: dump the entire persisted state. Used by the
    verifier and for fixture inspection. NOT exposed by the real
    github-mcp-server."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_repo")
def mock_debug_seed_repo(owner: str, name: str,
                         description: str = "",
                         private: bool = False,
                         auto_init: bool = True,
                         files: dict | None = None) -> dict:
    """Mock-only: directly insert a repo into state, bypassing the
    create_repository tool. Optional `files` is {path: content_str}
    written on the default branch. Used by per-task seeders."""
    with _lock():
        s = _load_state()
        key = _repo_key(owner, name)
        if key in s["repos"]:
            return _err(422, "already exists")
        r = _make_repo(s, owner, name, description=description,
                       private=private, auto_init=auto_init)
        if files:
            for path, content in files.items():
                content_bytes = content.encode("utf-8") if isinstance(
                    content, str) else bytes(content)
                _write_file(r, r["default_branch"], path, content_bytes,
                            f"seed: {path}", author=owner)
        s["repos"][key] = r
        _record(s, "debug_seed_repo", full_name=r["full_name"])
        _save_state(s)
        return _strip_repo(r)


if __name__ == "__main__":
    mcp.run()
