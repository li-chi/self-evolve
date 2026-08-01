"""Hugging Face Hub API served from the huggingface-mock state.

`huggingface_hub` (upstream preprocess and graders) talks to
huggingface.co over `requests`; netredirect points that host here. Repos,
files and cards come from the mock module, so a dataset the agent uploads
through its MCP tool is the dataset the grader downloads.

Implemented: whoami, repo create/delete, repo info, file tree, the
preupload+commit upload flow, raw file resolve, and model/dataset search —
the surface Toolathlon's four hugging-face tasks exercise.
"""

from __future__ import annotations

import base64
import json
import sys

from mockmod import load as _load_mock  # noqa: E402

hf = _load_mock("huggingface-mock")

_TYPES = {"model": "models", "dataset": "datasets", "space": "spaces"}


def _collection(state, repo_type):
    return state.setdefault(_TYPES[repo_type], {})


def _repo(state, repo_type, repo_id):
    return _collection(state, repo_type).get(repo_id)


def _files(repo):
    return repo.setdefault("files", {})


def _decode(entry) -> bytes:
    if isinstance(entry, dict):
        if entry.get("content_b64") is not None:
            return base64.b64decode(entry["content_b64"])
        return str(entry.get("content", "")).encode()
    return str(entry).encode()


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = hf._load_state()
    parts = [p for p in path.split("/") if p]

    # ---- /api/... --------------------------------------------------------
    if parts[:1] == ["api"]:
        api = parts[1:]

        if api[:1] == ["whoami-v2"] and method == "GET":
            return 200, state["user"]

        if api[:2] == ["repos", "create"] and method == "POST":
            data = body if isinstance(body, dict) else {}
            repo_type = data.get("type", "model")
            name = data.get("name", "")
            org = data.get("organization")
            repo_id = f"{org}/{name}" if org and "/" not in name else name
            if "/" not in repo_id:
                repo_id = f"{state['user']['name']}/{repo_id}"
            factory = {"model": hf._make_model, "dataset": hf._make_dataset,
                       "space": hf._make_space}[repo_type]
            repo = factory(repo_id, private=bool(data.get("private")))
            _collection(state, repo_type)[repo_id] = repo
            hf._save_state(state)
            return 200, {"url": f"https://huggingface.co/{repo_id}",
                         "name": repo_id, "id": repo_id}

        if api[:2] == ["repos", "delete"] and method == "DELETE":
            data = body if isinstance(body, dict) else {}
            repo_type = data.get("type", "model")
            name = data.get("name", "")
            org = data.get("organization")
            repo_id = f"{org}/{name}" if org and "/" not in name else name
            removed = _collection(state, repo_type).pop(repo_id, None)
            hf._save_state(state)
            return (200, {}) if removed else (404, {"error": "Repo not found"})

        for repo_type, plural in _TYPES.items():
            if api[:1] != [plural]:
                continue
            rest = api[1:]

            if not rest and method == "GET":       # search
                q = (query.get("search") or "").lower()
                out = [r for r in _collection(state, repo_type).values()
                       if not q or q in r.get("id", "").lower()]
                limit = int(query.get("limit") or 100)
                return 200, out[:limit]

            # repo id may contain a slash: <org>/<name>
            if len(rest) >= 2 and rest[1] not in ("tree", "commit",
                                                  "preupload", "resolve"):
                repo_id, rest = "/".join(rest[:2]), rest[2:]
            else:
                repo_id, rest = rest[0], rest[1:]
            repo = _repo(state, repo_type, repo_id)

            if not rest and method == "GET":
                if not repo:
                    return 404, {"error": "Repository not found"}
                return 200, repo

            if not repo:
                return 404, {"error": "Repository not found"}

            if rest[:1] == ["tree"] and method == "GET":
                out = [{"type": "file", "path": p,
                        "size": len(_decode(e)),
                        "oid": ""}
                       for p, e in _files(repo).items()]
                return 200, out

            if rest[:1] == ["preupload"] and method == "POST":
                data = body if isinstance(body, dict) else {}
                return 200, {"files": [
                    {"path": f.get("path"), "uploadMode": "regular",
                     "shouldIgnore": False}
                    for f in data.get("files", [])]}

            if rest[:1] == ["commit"] and method == "POST":
                # NDJSON: a header line then one line per file operation
                lines = []
                raw = body if isinstance(body, (bytes, bytearray)) else \
                    json.dumps(body).encode()
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                written = 0
                for op in lines:
                    kind, value = op.get("key"), op.get("value") or {}
                    if kind == "file":
                        content = value.get("content", "")
                        encoding = value.get("encoding", "base64")
                        data = (base64.b64decode(content)
                                if encoding == "base64"
                                else str(content).encode())
                        _files(repo)[value.get("path")] = {
                            "content_b64": base64.b64encode(data).decode(),
                            "size": len(data),
                        }
                        written += 1
                    elif kind == "deletedFile":
                        _files(repo).pop(value.get("path"), None)
                repo["lastModified"] = hf._now_iso()
                hf._save_state(state)
                return 200, {"commitUrl":
                             f"https://huggingface.co/{repo_id}/commit/main",
                             "commitOid": "mockcommit", "success": True,
                             "written": written}

    # ---- raw file access: /<repo_id>/resolve/<revision>/<path> ----------
    if "resolve" in parts:
        i = parts.index("resolve")
        repo_id = "/".join(parts[:i])
        file_path = "/".join(parts[i + 2:])
        prefix = {"datasets": "dataset", "spaces": "space"}
        repo_type = "model"
        if parts[0] in prefix:
            repo_type = prefix[parts[0]]
            repo_id = "/".join(parts[1:i])
        repo = _repo(state, repo_type, repo_id)
        if not repo or file_path not in _files(repo):
            return 404, {"error": "Entry not found"}
        return 200, _decode(_files(repo)[file_path])

    raise NotImplementedError(f"hf facade: {method} {path}")
