"""GitHub REST API served from the github-mock state.

`utils.app_specific.github` (used by upstream preprocess and graders) talks
to https://api.github.com with `requests`; netredirect points that host
here. The mock's own module supplies the state accessors and object
factories, so a repo created by the agent's MCP tool and a repo created by
preprocess through REST are the same record.

Only the endpoints Toolathlon actually calls are implemented; anything else
raises NotImplementedError so a gap is visible instead of silently
returning something plausible.
"""

from __future__ import annotations

import base64
import os
import re
import sys

from mockmod import load as _load_mock  # noqa: E402

gh = _load_mock("github-mock")


def _state():
    return gh._load_state()


def _save(state):
    gh._save_state(state)


def _strip_internal(repo: dict) -> dict:
    return gh._strip_repo(repo)


def _repo(state, owner, name):
    return state["repos"].get(gh._repo_key(owner, name))


def _default_branch(repo: dict) -> str:
    return repo.get("default_branch", "main")


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = _state()
    user = state.get("user", {})
    parts = [p for p in path.split("/") if p]

    # -- /user ------------------------------------------------------------
    if parts == ["user"] and method == "GET":
        return 200, user

    if parts == ["user", "repos"]:
        if method == "GET":
            login = user.get("login")
            return 200, [_strip_internal(r) for r in state["repos"].values()
                         if r.get("owner", {}).get("login") == login]
        if method == "POST":
            name = (body or {}).get("name")
            if not name:
                return 422, gh._err(422, "name is required")
            key = gh._repo_key(user.get("login"), name)
            if key in state["repos"]:
                return 422, gh._err(422, "Repository already exists")
            repo = gh._make_repo(state, user.get("login"), name,
                                 private=bool((body or {}).get("private")),
                                 description=(body or {}).get("description", ""),
                                 auto_init=bool((body or {}).get("auto_init")))
            state["repos"][key] = repo       # the factory does not insert
            _save(state)
            return 201, _strip_internal(repo)

    # -- /repos/{owner}/{repo}[/...] --------------------------------------
    if len(parts) >= 3 and parts[0] == "repos":
        owner, name = parts[1], parts[2]
        tail = parts[3:]
        repo = _repo(state, owner, name)

        if not tail:
            if method == "GET":
                if not repo:
                    return 404, gh._err(404, "Not Found")
                return 200, _strip_internal(repo)
            if method == "DELETE":
                if not repo:
                    return 404, gh._err(404, "Not Found")
                key = gh._repo_key(owner, name)
                del state["repos"][key]
                for bucket in ("issues", "pulls"):
                    for k in [k for k in state[bucket] if k.startswith(key + "#")]:
                        del state[bucket][k]
                _save(state)
                return 204, {}
            if method == "PATCH":
                if not repo:
                    return 404, gh._err(404, "Not Found")
                repo.update({k: v for k, v in (body or {}).items()})
                _save(state)
                return 200, _strip_internal(repo)

        if tail == ["forks"] and method == "POST":
            if not repo:
                return 404, gh._err(404, "Not Found")
            import copy
            fork = copy.deepcopy(repo)
            login = user.get("login")
            fork["id"] = state["next_id"]["repo"] = state["next_id"]["repo"] + 1
            fork["owner"] = gh._user_obj(login)
            fork["full_name"] = f"{login}/{name}"
            fork["fork"] = True
            fork["parent"] = _strip_internal(repo)
            state["repos"][gh._repo_key(login, name)] = fork
            _save(state)
            return 202, _strip_internal(fork)

        if not repo:
            return 404, gh._err(404, "Not Found")

        # contents
        if tail and tail[0] == "contents":
            file_path = "/".join(tail[1:])
            branch = query.get("ref") or _default_branch(repo)
            files = repo.setdefault("files", {}).setdefault(branch, {})
            if method == "GET":
                if file_path in files:
                    entry = files[file_path]
                    return 200, {
                        "name": os.path.basename(file_path),
                        "path": file_path,
                        "sha": entry["sha"],
                        "size": entry["size"],
                        "type": "file",
                        "encoding": "base64",
                        "content": entry["content_b64"],
                    }
                # directory listing
                prefix = file_path.rstrip("/") + "/" if file_path else ""
                children = {}
                for p, entry in files.items():
                    if not p.startswith(prefix):
                        continue
                    rest = p[len(prefix):]
                    head = rest.split("/")[0]
                    is_dir = "/" in rest
                    children[head] = {
                        "name": head,
                        "path": prefix + head,
                        "type": "dir" if is_dir else "file",
                        "sha": "" if is_dir else entry["sha"],
                        "size": 0 if is_dir else entry["size"],
                    }
                if children:
                    return 200, sorted(children.values(),
                                       key=lambda c: c["name"])
                return 404, gh._err(404, "Not Found")
            if method == "PUT":
                content = (body or {}).get("content", "")
                try:
                    raw = base64.b64decode(content)
                except Exception:  # noqa: BLE001
                    raw = str(content).encode()
                gh._write_file(repo, branch, file_path, raw,
                               (body or {}).get("message", "update"))
                _save(state)
                return 200, {"content": {"path": file_path},
                             "commit": {"sha": repo.get("commit_order", [""])[0]}}
            if method == "DELETE":
                files.pop(file_path, None)
                _save(state)
                return 200, {"commit": {"sha": ""}}

        # branches
        if tail and tail[0] == "branches":
            branches = repo.setdefault("branches", {})
            if len(tail) == 1 and method == "GET":
                return 200, list(branches.values())
            if len(tail) == 2 and method == "GET":
                b = branches.get(tail[1])
                if not b:
                    return 404, gh._err(404, "Branch not found")
                return 200, {"name": b["name"],
                             "commit": {"sha": b["sha"]},
                             "protected": b.get("protected", False)}

        # git refs
        if tail[:2] == ["git", "refs"]:
            ref = "/".join(tail[2:])
            branch = ref.split("/")[-1]
            b = repo.get("branches", {}).get(branch)
            if method == "GET":
                if not b:
                    return 404, gh._err(404, "Not Found")
                return 200, {"ref": f"refs/{ref}",
                             "object": {"sha": b["sha"], "type": "commit"}}

        # commits + compare
        if tail == ["commits"] and method == "GET":
            order = repo.get("commit_order", [])
            commits = [repo["commits"][s] for s in order
                       if s in repo.get("commits", {})]
            return 200, commits
        if tail and tail[0] == "compare" and method == "GET":
            base_head = tail[1] if len(tail) > 1 else ""
            base, _, head = base_head.partition("...")
            branch_files = repo.get("files", {})
            base_files = branch_files.get(base, {})
            head_files = branch_files.get(head, {})
            diff = []
            for p, entry in head_files.items():
                if p not in base_files:
                    diff.append({"filename": p, "status": "added"})
                elif base_files[p]["sha"] != entry["sha"]:
                    diff.append({"filename": p, "status": "modified"})
            for p in base_files:
                if p not in head_files:
                    diff.append({"filename": p, "status": "removed"})
            return 200, {"files": diff, "status": "diverged",
                         "commits": []}

        # issues
        if tail and tail[0] == "issues":
            key_prefix = gh._repo_key(owner, name)
            if len(tail) == 1:
                if method == "GET":
                    want = query.get("state", "open")
                    out = [v for k, v in state["issues"].items()
                           if k.startswith(key_prefix + "#")
                           and (want == "all" or v.get("state") == want)]
                    return 200, [_public_issue(i) for i in out]
                if method == "POST":
                    issue = gh._make_issue(
                        state, repo,
                        (body or {}).get("title", ""),
                        (body or {}).get("body", ""),
                        (body or {}).get("assignees") or [],
                        (body or {}).get("labels") or [],
                        (body or {}).get("milestone"),
                        (body or {}).get("type"),
                        state.get("user", {}).get("login", "mock-user"))
                    _save(state)
                    return 201, _public_issue(issue)
            number = tail[1] if len(tail) > 1 else None
            issue = state["issues"].get(f"{key_prefix}#{number}")
            if len(tail) == 2:
                if not issue:
                    return 404, gh._err(404, "Not Found")
                if method == "GET":
                    return 200, _public_issue(issue)
                if method in ("PATCH", "POST"):
                    issue.update({k: v for k, v in (body or {}).items()})
                    _save(state)
                    return 200, _public_issue(issue)
            if len(tail) == 3 and tail[2] == "comments":
                if not issue:
                    return 404, gh._err(404, "Not Found")
                if method == "GET":
                    return 200, issue.get("_comments", [])
                if method == "POST":
                    cid = state["next_id"]["comment"]
                    state["next_id"]["comment"] = cid + 1
                    comment = {"id": cid, "body": (body or {}).get("body", ""),
                               "user": state.get("user", {}),
                               "created_at": gh._now(),
                               "updated_at": gh._now()}
                    issue.setdefault("_comments", []).append(comment)
                    _save(state)
                    return 201, comment

    raise NotImplementedError(f"github facade: {method} {path}")


def _public_issue(issue: dict) -> dict:
    return gh._strip_issue(issue)
