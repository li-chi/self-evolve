"""HuggingFace mock MCP server.

Mirrors the tool surface of `huggingface/hf-mcp-server` (the official
Hugging Face MCP server hosted at https://huggingface.co/mcp). The
upstream is a TypeScript server; this is a Python FastMCP port that
serves the same tool *names*, *parameter shapes*, and *output format*
(markdown text strings wrapped as MCP text content) so an RL training
rollout never touches a real Hugging Face account.

Tool surface (canonical names from
github.com/huggingface/hf-mcp-server/blob/main/packages/mcp/src/tool-ids.ts):
    space_search, model_search, hub_repo_search, create_repo,
    model_details, paper_search, dataset_search, dataset_details,
    hub_repo_details, hf_doc_search, hf_doc_fetch, duplicate_space,
    space_info, space_files, use_space, hf_jobs

(`dynamic_space_tool` and the gradio/MCP-UI surface are intentionally
omitted — none of the Toolathlon HF tasks invoke them.)

State is a single JSON file at `$HF_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/hf_mock`). Every mutating tool appends an entry
to `state["calls"]` (the verifier consumes that log).

IMPORTANT scope caveat (see README): the official HF MCP server does
*not* expose a file-upload tool. Real Toolathlon tasks like
`huggingface-upload` upload via `huggingface_hub` / the `hf` CLI from
the terminal MCP, which talks directly to `https://huggingface.co/api`
via HTTPS — that traffic does not pass through this server. We expose
two mock-only debug tools (`mock_debug_upload_file`,
`mock_debug_seed_repo`) so a side-car harness can simulate uploads
into our state.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "HF_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/hf_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "user": {
            "type": "user",
            "id": "mock-user-id",
            "name": "mock-user",
            "fullname": "Mock User",
            "email": "mock@hf.co",
            "isPro": False,
            "avatarUrl": "https://huggingface.co/avatars/mock.png",
            "orgs": [],
            "auth": {"type": "access_token",
                     "accessToken": {"role": "write", "createdAt": _now_iso()}},
        },
        "models": {},      # id -> model dict
        "datasets": {},    # id -> dataset dict
        "spaces": {},      # id -> space dict
        "papers": {},      # arxiv_id -> paper dict
        "collections": {}, # slug -> collection dict
        # files keyed by "<repo_type>/<id>/<path>"
        # e.g. "datasets/mock-user/foo/README.md"
        "files": {},
        "docs": {},        # url -> {title, content}
        "jobs": {},        # job_id -> job dict
        "next_id": {"job": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("HF_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Formatting helpers (mirror packages/mcp/src/utilities.ts in upstream)
# ---------------------------------------------------------------------------

def _format_number(n: int | float | None) -> str:
    if n is None:
        return "0"
    n = int(n)
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _format_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        # accept ISO-8601 with or without timezone
        s = d.rstrip("Z")
        dt = datetime.datetime.fromisoformat(s)
        return dt.strftime("%B %d, %Y")
    except Exception:
        return str(d)


def _format_bytes(n: int | float | None) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _matches_query(repo: dict, query: str | None) -> bool:
    if not query:
        return True
    q = query.lower()
    hay = " ".join([
        repo.get("id", ""),
        repo.get("name", "") or "",
        repo.get("description", "") or "",
        " ".join(repo.get("tags", []) or []),
    ]).lower()
    return q in hay


def _sort_repos(repos: list, sort: str | None) -> list:
    if not sort:
        return repos
    key_map = {
        "downloads": ("downloads", 0),
        "likes": ("likes", 0),
        "trendingScore": ("trendingScore", 0),
        "createdAt": ("createdAt", ""),
        "lastModified": ("lastModified", ""),
    }
    if sort not in key_map:
        return repos
    field, default = key_map[sort]
    return sorted(repos, key=lambda r: r.get(field) or default, reverse=True)


def _split_id(repo_id: str) -> tuple[str, str]:
    parts = repo_id.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", repo_id


def _err_text(msg: str) -> str:
    """Errors are raised as exceptions in upstream, which FastMCP converts
    into MCP `isError: true` text content. We follow the same approach by
    returning a string starting with 'Error:' to keep error text inline."""
    return msg


# ---------------------------------------------------------------------------
# Default repo skeletons (used when seeding or auto-creating)
# ---------------------------------------------------------------------------

def _make_model(repo_id: str, *, private: bool = False) -> dict:
    author, _ = _split_id(repo_id)
    now = _now_iso()
    return {
        "_id": hashlib.sha1(repo_id.encode()).hexdigest()[:24],
        "id": repo_id,
        "modelId": repo_id,
        "name": repo_id,
        "author": author,
        "private": private,
        "gated": False,
        "downloads": 0,
        "downloadsAllTime": 0,
        "likes": 0,
        "trendingScore": 0,
        "tags": [],
        "pipeline_tag": None,
        "library_name": None,
        "createdAt": now,
        "lastModified": now,
        "updatedAt": now,
        "sha": "0" * 40,
        "siblings": [],
        "cardData": {},
        "config": {},
        "safetensors": None,
        "spaces": [],
        "inferenceProviderMapping": [],
    }


def _make_dataset(repo_id: str, *, private: bool = False) -> dict:
    author, _ = _split_id(repo_id)
    now = _now_iso()
    return {
        "_id": hashlib.sha1(repo_id.encode()).hexdigest()[:24],
        "id": repo_id,
        "name": repo_id,
        "author": author,
        "private": private,
        "gated": False,
        "downloads": 0,
        "downloadsAllTime": 0,
        "likes": 0,
        "trendingScore": 0,
        "tags": [],
        "description": "",
        "createdAt": now,
        "lastModified": now,
        "updatedAt": now,
        "sha": "0" * 40,
        "siblings": [],
        "cardData": {},
    }


def _make_space(repo_id: str, *, private: bool = False,
                sdk: str = "static") -> dict:
    author, name = _split_id(repo_id)
    now = _now_iso()
    return {
        "_id": hashlib.sha1(repo_id.encode()).hexdigest()[:24],
        "id": repo_id,
        "name": repo_id,
        "author": author,
        "private": private,
        "gated": False,
        "sdk": sdk,
        "title": name,
        "emoji": "🤗",
        "shortDescription": "",
        "likes": 0,
        "trendingScore": 0,
        "tags": [],
        "createdAt": now,
        "lastModified": now,
        "updatedAt": now,
        "runtime": {"stage": "RUNNING"},
        "subdomain": name.lower().replace("_", "-"),
        "siblings": [],
        "cardData": {},
    }


def _repo_collection(state: dict, repo_type: str) -> dict:
    """Return the dict of repos keyed by id for a given repo_type."""
    return {
        "model": state["models"],
        "dataset": state["datasets"],
        "space": state["spaces"],
    }[repo_type]


def _detect_repo_type(state: dict, repo_id: str) -> str | None:
    for t in ("model", "dataset", "space"):
        if repo_id in _repo_collection(state, t):
            return t
    return None


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("huggingface-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# create_repo  (POST /api/repos/create)
# ---------------------------------------------------------------------------

@mcp.tool(name="create_repo")
def create_repo(name: str,
                repo_type: str = "model",
                private: bool = False,
                sdk: str = "static") -> str:
    """HF MCP: create a Hugging Face model, dataset, space, or bucket
    repository. `name` must be `namespace/repo-name`. `repo_type` is
    one of: model, dataset, space, bucket (bucket aliases to model in
    this mock — the upstream uses it for the new `hf bucket` flow).

    Mirrors `CREATE_REPO_TOOL_CONFIG` in packages/mcp/src/create-repo.ts.
    """
    with _lock():
        s = _load_state()
        if "/" not in name or any(not p for p in name.split("/")):
            _record(s, "create_repo", name=name, result="invalid_name")
            _save_state(s)
            return ("Error: name must be fully qualified in "
                    "'namespace/repo-name' format.")
        t = repo_type or "model"
        if t == "bucket":
            t = "model"
        if t not in ("model", "dataset", "space"):
            _record(s, "create_repo", name=name, repo_type=repo_type,
                    result="invalid_type")
            _save_state(s)
            return f"Error: invalid repo_type '{repo_type}'."
        coll = _repo_collection(s, t)
        if name in coll:
            _record(s, "create_repo", name=name, repo_type=t,
                    result="already_exists")
            _save_state(s)
            return (f"Error: repository '{name}' already exists "
                    f"as a {t}.")
        if t == "model":
            coll[name] = _make_model(name, private=private)
        elif t == "dataset":
            coll[name] = _make_dataset(name, private=private)
        else:
            coll[name] = _make_space(name, private=private, sdk=sdk or "static")
        url = (f"https://huggingface.co/{name}"
               if t == "model"
               else f"https://huggingface.co/{t}s/{name}")
        _record(s, "create_repo", name=name, repo_type=t, private=private)
        _save_state(s)
        return "\n".join([
            "Repository created.",
            f"Name: {name}",
            f"Type: {t}",
            f"URL: {url}",
            f"ID: {coll[name]['_id']}",
        ])


# ---------------------------------------------------------------------------
# update_repo_card  (real HF: POST /api/{type}s/{name}/settings & cardData PUT)
# ---------------------------------------------------------------------------

@mcp.tool(name="update_repo_card")
def update_repo_card(name: str,
                     repo_type: str = "model",
                     description: str | None = None,
                     tags: list[str] | None = None,
                     card_data: dict | None = None) -> str:
    """HF MCP: update a repository's card metadata. Sets the
    repo's `description` and/or `tags` and/or merges fields into
    `cardData`. Mirrors the real HF flow where an agent commits a
    README.md with YAML front-matter (card metadata) to the repo —
    this mock-only shortcut lets agents attach paper-derived
    metadata (title, author, license tag) without needing to
    upload a file blob.

    Returns an error string if the repo doesn't exist."""
    with _lock():
        s = _load_state()
        t = repo_type or "model"
        if t == "bucket":
            t = "model"
        if t not in ("model", "dataset", "space"):
            return f"Error: invalid repo_type '{repo_type}'."
        coll = _repo_collection(s, t)
        if name not in coll:
            _record(s, "update_repo_card", name=name, repo_type=t,
                    result="not_found")
            _save_state(s)
            return f"Error: repository '{name}' not found as a {t}."
        repo = coll[name]
        if description is not None:
            repo["description"] = str(description)
        if tags is not None:
            if isinstance(tags, list):
                repo["tags"] = [str(t) for t in tags]
            else:
                return "Error: tags must be a list of strings."
        if card_data is not None:
            if not isinstance(card_data, dict):
                return "Error: card_data must be a dict."
            cd = repo.setdefault("cardData", {})
            for k, v in card_data.items():
                cd[str(k)] = v
        _record(s, "update_repo_card", name=name, repo_type=t,
                description_set=description is not None,
                tags_count=len(tags) if isinstance(tags, list) else 0,
                card_keys=list((card_data or {}).keys()))
        _save_state(s)
        return "\n".join([
            "Repository card updated.",
            f"Name: {name}",
            f"Type: {t}",
            f"Description: {repo.get('description', '')[:80]}",
            f"Tags: {repo.get('tags', [])}",
        ])


# ---------------------------------------------------------------------------
# model_search  (GET /api/models)
# ---------------------------------------------------------------------------

@mcp.tool(name="model_search")
def model_search(query: str | None = None,
                 author: str | None = None,
                 task: str | None = None,
                 library: str | None = None,
                 sort: str | None = None,
                 limit: int = 20) -> str:
    """HF MCP: find Machine Learning models hosted on Hugging Face.
    Returns markdown with downloads, likes, tags, and direct links.
    Mirrors `MODEL_SEARCH_TOOL_CONFIG` in
    packages/mcp/src/model-search.ts.
    """
    with _lock():
        s = _load_state()
        models = list(s["models"].values())
        if author:
            models = [m for m in models if m.get("author") == author]
        if task:
            models = [m for m in models
                      if m.get("pipeline_tag") == task
                      or task in (m.get("tags") or [])]
        if library:
            models = [m for m in models
                      if m.get("library_name") == library
                      or library in (m.get("tags") or [])]
        if query:
            models = [m for m in models if _matches_query(m, query)]
        models = _sort_repos(models, sort)
        limit = max(1, min(int(limit or 20), 100))
        page = models[:limit]
        _record(s, "model_search", query=query, author=author, task=task,
                library=library, sort=sort, count=len(page))
        _save_state(s)
        return _format_models(page, query=query, author=author, task=task,
                              library=library, sort=sort, limit=limit)


def _format_models(models: list, **params) -> str:
    if not models:
        return "No models found for the given criteria."
    r: list[str] = []
    terms = []
    if params.get("query"):
        terms.append(f'query "{params["query"]}"')
    if params.get("author"):
        terms.append(f'author "{params["author"]}"')
    if params.get("task"):
        terms.append(f'task "{params["task"]}"')
    if params.get("library"):
        terms.append(f'library "{params["library"]}"')
    if params.get("sort"):
        terms.append(f'sorted by {params["sort"]} (descending)')
    desc = f" matching {', '.join(terms)}" if terms else ""
    limit = params.get("limit") or 0
    if len(models) == limit:
        r.append(f"Showing first {len(models)} models{desc}:")
    else:
        r.append(f"Found {len(models)} models{desc}:")
    r.append("")
    for m in models:
        r.append(f"## {m['id']}")
        r.append("")
        info = []
        if m.get("pipeline_tag"):
            info.append(f"**Task:** {m['pipeline_tag']}")
        if m.get("library_name"):
            info.append(f"**Library:** {m['library_name']}")
        if m.get("downloads"):
            info.append(f"**Downloads:** {_format_number(m['downloads'])}")
        if m.get("likes"):
            info.append(f"**Likes:** {m['likes']}")
        if m.get("trendingScore"):
            info.append(f"**Trending Score:** {m['trendingScore']}")
        if info:
            r.append(" | ".join(info))
            r.append("")
        tags = m.get("tags") or []
        if tags:
            shown = tags[:20]
            r.append(f"**Tags:** {', '.join(shown)}")
            if len(tags) > 20:
                r.append(f"*and {len(tags) - 20} more...*")
            r.append("")
        status = []
        if m.get("private"):
            status.append("🔐 Private")
        if status:
            r.append(" | ".join(status))
            r.append("")
        if m.get("createdAt"):
            r.append(f"**Created:** {_format_date(m['createdAt'])}")
        r.append(f"**Link:** [https://hf.co/{m['id']}](https://hf.co/{m['id']})")
        r.append("")
        r.append("---")
        r.append("")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# dataset_search  (GET /api/datasets)
# ---------------------------------------------------------------------------

@mcp.tool(name="dataset_search")
def dataset_search(query: str | None = None,
                   author: str | None = None,
                   tags: list[str] | None = None,
                   sort: str | None = None,
                   limit: int = 20) -> str:
    """HF MCP: find Datasets hosted on the Hugging Face hub. Mirrors
    `DATASET_SEARCH_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        datasets = list(s["datasets"].values())
        if author:
            datasets = [d for d in datasets if d.get("author") == author]
        if tags:
            datasets = [d for d in datasets
                        if all(t in (d.get("tags") or []) for t in tags)]
        if query:
            datasets = [d for d in datasets if _matches_query(d, query)]
        datasets = _sort_repos(datasets, sort)
        limit = max(1, min(int(limit or 20), 100))
        page = datasets[:limit]
        _record(s, "dataset_search", query=query, author=author, tags=tags,
                sort=sort, count=len(page))
        _save_state(s)
        return _format_datasets(page, query=query, author=author, tags=tags,
                                sort=sort, limit=limit)


def _format_datasets(datasets: list, **params) -> str:
    if not datasets:
        return "No datasets found for the given criteria."
    r: list[str] = []
    terms = []
    if params.get("query"):
        terms.append(f'query "{params["query"]}"')
    if params.get("author"):
        terms.append(f'author "{params["author"]}"')
    if params.get("tags"):
        terms.append(f'tags [{", ".join(params["tags"])}]')
    if params.get("sort"):
        terms.append(f'sorted by {params["sort"]} (descending)')
    desc = f" matching {', '.join(terms)}" if terms else ""
    limit = params.get("limit") or 0
    if len(datasets) == limit:
        r.append(f"Showing first {len(datasets)} datasets{desc}:")
    else:
        r.append(f"Found {len(datasets)} datasets{desc}:")
    r.append("")
    for d in datasets:
        r.append(f"## {d['id']}")
        r.append("")
        if d.get("description"):
            desc_text = d["description"]
            r.append(desc_text[:200] + ("..." if len(desc_text) > 200 else ""))
            r.append("")
        info = []
        if d.get("downloads"):
            info.append(f"**Downloads:** {_format_number(d['downloads'])}")
        if d.get("likes"):
            info.append(f"**Likes:** {d['likes']}")
        if d.get("trendingScore"):
            info.append(f"**Trending Score:** {d['trendingScore']}")
        if info:
            r.append(" | ".join(info))
            r.append("")
        tags = d.get("tags") or []
        if tags:
            shown = tags[:20]
            r.append(f"**Tags:** {', '.join(shown)}")
            if len(tags) > 20:
                r.append(f"*and {len(tags) - 20} more...*")
            r.append("")
        status = []
        if d.get("gated"):
            status.append("🔒 Gated")
        if d.get("private"):
            status.append("🔐 Private")
        if status:
            r.append(" | ".join(status))
            r.append("")
        if d.get("createdAt"):
            r.append(f"**Created:** {_format_date(d['createdAt'])}")
        if d.get("lastModified") and d["lastModified"] != d.get("createdAt"):
            r.append(f"**Last Modified:** {_format_date(d['lastModified'])}")
        r.append(f"**Link:** [https://hf.co/datasets/{d['id']}]"
                 f"(https://hf.co/datasets/{d['id']})")
        r.append("")
        r.append("---")
        r.append("")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# hub_repo_search  (aggregated GET /api/{models,datasets,spaces})
# ---------------------------------------------------------------------------

@mcp.tool(name="hub_repo_search")
def hub_repo_search(query: str | None = None,
                    repo_types: list[str] | None = None,
                    author: str | None = None,
                    filters: list[str] | None = None,
                    sort: str | None = None,
                    limit: int = 20) -> str:
    """HF MCP: search Hugging Face repositories with a shared query
    interface. Targets models, datasets, spaces, or aggregates across
    multiple repo types. Mirrors `REPO_SEARCH_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        types = repo_types or ["model", "dataset"]
        types = [t for t in types if t in ("model", "dataset", "space")]
        limit = max(1, min(int(limit or 20), 100))
        sections: list[str] = []
        type_counts = {}
        for t in types:
            repos = list(_repo_collection(s, t).values())
            if author:
                repos = [r for r in repos if r.get("author") == author]
            if filters:
                repos = [r for r in repos
                         if all(f in (r.get("tags") or []) for f in filters)]
            if query:
                repos = [r for r in repos if _matches_query(r, query)]
            repos = _sort_repos(repos, sort)
            page = repos[:limit]
            type_counts[t] = len(page)
            label = {"model": "Models", "dataset": "Datasets",
                     "space": "Spaces"}[t]
            if not page:
                sections.append(f"# {label}\n\nNo {label.lower()} found.")
                continue
            if t == "model":
                sections.append(f"# {label}\n\n"
                                + _format_models(page, query=query,
                                                 author=author,
                                                 sort=sort, limit=limit))
            elif t == "dataset":
                sections.append(f"# {label}\n\n"
                                + _format_datasets(page, query=query,
                                                   author=author,
                                                   tags=filters, sort=sort,
                                                   limit=limit))
            else:
                sections.append(f"# {label}\n\n"
                                + _format_spaces(page, query=query,
                                                 author=author, sort=sort,
                                                 limit=limit))
        _record(s, "hub_repo_search", query=query, types=types,
                author=author, filters=filters, counts=type_counts)
        _save_state(s)
        return "\n\n".join(sections) if sections else "No repositories found."


def _format_spaces(spaces: list, **params) -> str:
    if not spaces:
        return "No spaces found for the given criteria."
    r: list[str] = []
    limit = params.get("limit") or 0
    if len(spaces) == limit:
        r.append(f"Showing first {len(spaces)} spaces:")
    else:
        r.append(f"Found {len(spaces)} spaces:")
    r.append("")
    for sp in spaces:
        r.append(f"## {sp['id']}")
        r.append("")
        if sp.get("shortDescription"):
            r.append(sp["shortDescription"])
            r.append("")
        info = []
        if sp.get("sdk"):
            info.append(f"**SDK:** {sp['sdk']}")
        if sp.get("likes"):
            info.append(f"**Likes:** {sp['likes']}")
        runtime = (sp.get("runtime") or {}).get("stage")
        if runtime:
            info.append(f"**Status:** {runtime}")
        if info:
            r.append(" | ".join(info))
            r.append("")
        r.append(f"**Link:** [https://hf.co/spaces/{sp['id']}]"
                 f"(https://hf.co/spaces/{sp['id']})")
        r.append("")
        r.append("---")
        r.append("")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# model_details  (modelInfo)
# ---------------------------------------------------------------------------

@mcp.tool(name="model_details")
def model_details(model_id: str) -> str:
    """HF MCP: get detailed information about a model from the Hugging
    Face Hub. Mirrors `MODEL_DETAIL_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        m = s["models"].get(model_id)
        _record(s, "model_details", model_id=model_id,
                result="ok" if m else "not_found")
        _save_state(s)
        if not m:
            return (f"Error: Model '{model_id}' not found. "
                    "Please check the model ID.")
        return _format_model_details(m)


def _format_model_details(m: dict) -> str:
    r: list[str] = []
    r.append(f"# {m['name']}")
    r.append("")
    r.append("## Overview")
    if m.get("author"):
        r.append(f"- **Author:** {m['author']}")
    stats = []
    if m.get("downloadsAllTime"):
        stats.append(f"**Downloads:** {_format_number(m['downloadsAllTime'])}")
    if m.get("likes"):
        stats.append(f"**Likes:** {m['likes']}")
    if stats:
        r.append("- " + " | ".join(stats))
    if m.get("updatedAt"):
        r.append(f"- **Updated:** {_format_date(m['updatedAt'])}")
    status = []
    if m.get("gated"):
        status.append("🔒 Gated")
    if m.get("private"):
        status.append("🔐 Private")
    if status:
        r.append(f"- **Status:** {' | '.join(status)}")
    r.append("")
    if m.get("library_name"):
        r.append(f"## Library\n- **Library:** {m['library_name']}\n")
    if m.get("pipeline_tag"):
        r.append(f"## Task\n- **Pipeline:** {m['pipeline_tag']}\n")
    tags = m.get("tags") or []
    if tags:
        r.append("## Tags")
        r.append(" ".join(f"`{t}`" for t in tags[:20]))
        r.append("")
    card = m.get("cardData") or {}
    if card:
        meta = []
        if card.get("language"):
            lang = card["language"]
            meta.append("- **Language:** " + (", ".join(lang) if isinstance(lang, list) else str(lang)))
        if card.get("license"):
            lic = card["license"]
            meta.append("- **License:** " + (", ".join(lic) if isinstance(lic, list) else str(lic)))
        if card.get("datasets"):
            ds = card["datasets"]
            meta.append("- **Datasets:** " + (", ".join(ds) if isinstance(ds, list) else str(ds)))
        if meta:
            r.append("## Metadata")
            r.extend(meta)
            r.append("")
    r.append(f"**Link:** [https://hf.co/{m['id']}](https://hf.co/{m['id']})")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# dataset_details  (datasetInfo)
# ---------------------------------------------------------------------------

@mcp.tool(name="dataset_details")
def dataset_details(dataset_id: str) -> str:
    """HF MCP: get detailed information about a specific dataset on
    Hugging Face Hub. Mirrors `DATASET_DETAIL_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        d = s["datasets"].get(dataset_id)
        _record(s, "dataset_details", dataset_id=dataset_id,
                result="ok" if d else "not_found")
        _save_state(s)
        if not d:
            return (f"Error: Dataset '{dataset_id}' not found. "
                    "Please check the dataset ID.")
        return _format_dataset_details(d)


def _format_dataset_details(d: dict) -> str:
    r: list[str] = []
    r.append(f"# {d['name']}")
    r.append("")
    if d.get("description"):
        r.append("## Description")
        r.append(d["description"])
        r.append("")
    r.append("## Overview")
    if d.get("author"):
        r.append(f"- **Author:** {d['author']}")
    stats = []
    if d.get("downloadsAllTime"):
        stats.append(f"**Downloads:** {_format_number(d['downloadsAllTime'])}")
    if d.get("likes"):
        stats.append(f"**Likes:** {d['likes']}")
    if stats:
        r.append("- " + " | ".join(stats))
    if d.get("updatedAt"):
        r.append(f"- **Updated:** {_format_date(d['updatedAt'])}")
    status = []
    if d.get("gated"):
        status.append("🔒 Gated")
    if d.get("private"):
        status.append("🔐 Private")
    if status:
        r.append(f"- **Status:** {' | '.join(status)}")
    r.append("")
    tags = d.get("tags") or []
    if tags:
        r.append("## Tags")
        r.append(" ".join(f"`{t}`" for t in tags[:20]))
        r.append("")
    card = d.get("cardData") or {}
    if card:
        meta = []
        if card.get("language"):
            lang = card["language"]
            meta.append("- **Language:** " + (", ".join(lang) if isinstance(lang, list) else str(lang)))
        if card.get("license"):
            lic = card["license"]
            meta.append("- **License:** " + (", ".join(lic) if isinstance(lic, list) else str(lic)))
        if card.get("task_categories"):
            tc = card["task_categories"]
            meta.append("- **Task Categories:** " + (", ".join(tc) if isinstance(tc, list) else str(tc)))
        if card.get("size_categories"):
            sc = card["size_categories"]
            meta.append("- **Size Category:** " + (", ".join(sc) if isinstance(sc, list) else str(sc)))
        if meta:
            r.append("## Metadata")
            r.extend(meta)
            r.append("")
    r.append(f"**Link:** [https://hf.co/datasets/{d['id']}]"
             f"(https://hf.co/datasets/{d['id']})")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# hub_repo_details  (multi-id + per-repo-type dispatcher; supports
# overview / dataset_structure / dataset_preview operations)
# ---------------------------------------------------------------------------

@mcp.tool(name="hub_repo_details")
def hub_repo_details(repo_ids: list[str],
                     repo_type: str | None = None,
                     include_readme: bool = False,
                     operations: list[str] | None = None,
                     config: str | None = None,
                     split: str | None = None,
                     offset: int | None = None,
                     limit: int | None = None) -> str:
    """HF MCP: get details for one or more Hugging Face repos (model,
    dataset, or space). Auto-detects type unless specified. Supports
    operations: overview, dataset_structure, dataset_preview.
    Mirrors `HUB_REPO_DETAILS_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        ops = operations or ["overview"]
        sections: list[str] = []
        success = 0
        for rid in repo_ids:
            try:
                sections.append(_inspect_single(
                    s, rid, repo_type, include_readme, ops,
                    config, split, offset, limit))
                success += 1
            except KeyError as e:
                sections.append(f"# {rid}\n\n- Error: {e.args[0]}")
        _record(s, "hub_repo_details", repo_ids=repo_ids, repo_type=repo_type,
                ops=ops, success=success)
        _save_state(s)
        return "\n\n---\n\n".join(sections)


def _inspect_single(s: dict, repo_id: str, repo_type: str | None,
                    include_readme: bool, operations: list[str],
                    config: str | None, split: str | None,
                    offset: int | None, limit: int | None) -> str:
    has_dataset_op = any(o in operations
                         for o in ("dataset_structure", "dataset_preview"))
    if repo_type == "model":
        if has_dataset_op:
            return (f"# {repo_id}\n\n- Error: dataset_structure/"
                    "dataset_preview not valid for model repos.")
        m = s["models"].get(repo_id)
        if not m:
            raise KeyError(f"Model '{repo_id}' not found.")
        out = _format_model_details(m)
        if include_readme:
            out += "\n\n" + _readme_block(s, "model", repo_id)
        return out
    if repo_type == "dataset":
        return _inspect_dataset(s, repo_id, include_readme, operations,
                                config, split, offset, limit)
    if repo_type == "space":
        if has_dataset_op:
            return (f"# {repo_id}\n\n- Error: dataset_structure/"
                    "dataset_preview not valid for space repos.")
        sp = s["spaces"].get(repo_id)
        if not sp:
            raise KeyError(f"Space '{repo_id}' not found.")
        return _format_space_details(sp)
    # Auto-detect: try all three
    if has_dataset_op:
        return _inspect_dataset(s, repo_id, include_readme, operations,
                                config, split, offset, limit)
    matches = []
    if repo_id in s["models"]:
        m = s["models"][repo_id]
        block = "**Type: Model**\n\n" + _format_model_details(m)
        if include_readme:
            block += "\n\n" + _readme_block(s, "model", repo_id)
        matches.append(block)
    if repo_id in s["datasets"]:
        d = s["datasets"][repo_id]
        block = "**Type: Dataset**\n\n" + _format_dataset_details(d)
        if include_readme:
            block += "\n\n" + _readme_block(s, "dataset", repo_id)
        matches.append(block)
    if repo_id in s["spaces"]:
        sp = s["spaces"][repo_id]
        matches.append("**Type: Space**\n\n" + _format_space_details(sp))
    if not matches:
        raise KeyError(
            f"Could not find repo '{repo_id}' as model, dataset, or space.")
    return "\n\n---\n\n".join(matches)


def _inspect_dataset(s: dict, repo_id: str, include_readme: bool,
                     operations: list[str], config: str | None,
                     split: str | None, offset: int | None,
                     limit: int | None) -> str:
    d = s["datasets"].get(repo_id)
    if not d:
        raise KeyError(f"Dataset '{repo_id}' not found.")
    parts: list[str] = []
    if "overview" in operations or operations == ["overview"]:
        parts.append(_format_dataset_details(d))
    if "dataset_structure" in operations:
        parts.append(_dataset_structure_block(d))
    if "dataset_preview" in operations:
        parts.append(_dataset_preview_block(d, config, split, offset, limit))
    if include_readme:
        parts.append(_readme_block(s, "dataset", repo_id))
    return "\n\n".join(parts)


def _format_space_details(sp: dict) -> str:
    r: list[str] = []
    r.append(f"# {sp['id']}")
    r.append("")
    r.append("## Overview")
    if sp.get("author"):
        r.append(f"- **Author:** {sp['author']}")
    if sp.get("sdk"):
        r.append(f"- **SDK:** {sp['sdk']}")
    runtime = (sp.get("runtime") or {}).get("stage")
    if runtime:
        r.append(f"- **Status:** {runtime}")
    if sp.get("likes"):
        r.append(f"- **Likes:** {sp['likes']}")
    if sp.get("updatedAt"):
        r.append(f"- **Updated:** {_format_date(sp['updatedAt'])}")
    r.append("")
    r.append(f"**Link:** [https://hf.co/spaces/{sp['id']}]"
             f"(https://hf.co/spaces/{sp['id']})")
    return "\n".join(r)


def _readme_block(s: dict, repo_type: str, repo_id: str) -> str:
    key = f"{repo_type}s/{repo_id}/README.md"
    f = s["files"].get(key)
    if not f:
        # also try without trailing s for model singular form
        key = f"{repo_type}/{repo_id}/README.md"
        f = s["files"].get(key)
    if not f:
        return ""
    label = ("modelcard" if repo_type == "model"
             else "datasetcard" if repo_type == "dataset"
             else "spacecard")
    try:
        content = base64.b64decode(f["content_b64"]).decode("utf-8",
                                                            errors="replace")
    except Exception:
        content = ""
    return f"## README\n<{label}-readme>\n{content.strip()}\n</{label}-readme>"


def _dataset_structure_block(d: dict) -> str:
    info = (d.get("cardData") or {}).get("dataset_info") or {}
    r = ["## Dataset Structure"]
    if not info:
        r.append("(No structured dataset_info available; configure via "
                 "`cardData.dataset_info`.)")
        return "\n".join(r)
    if isinstance(info, dict) and "config_name" in info:
        info = [info]
    if isinstance(info, dict):
        info = list(info.values())
    for cfg in info if isinstance(info, list) else [info]:
        if not isinstance(cfg, dict):
            continue
        r.append(f"### Config: {cfg.get('config_name', 'default')}")
        for sp in cfg.get("splits", []) or []:
            r.append(f"- **Split:** {sp.get('name')} "
                     f"({sp.get('num_examples', 0)} examples, "
                     f"{_format_bytes(sp.get('num_bytes'))})")
        feats = cfg.get("features") or []
        if feats:
            r.append("- **Features:** "
                     + ", ".join(f.get("name", "?") for f in feats))
    return "\n".join(r)


def _dataset_preview_block(d: dict, config: str | None, split: str | None,
                           offset: int | None, limit: int | None) -> str:
    preview = (d.get("cardData") or {}).get("preview_rows") or {}
    rows = []
    if isinstance(preview, dict):
        cfg_key = config or "default"
        sp_key = split or "train"
        rows = preview.get(cfg_key, {}).get(sp_key, [])
    elif isinstance(preview, list):
        rows = preview
    off = int(offset or 0)
    lim = max(1, min(int(limit or 5), 100))
    page = rows[off: off + lim]
    r = [f"## Dataset Preview (config={config or 'default'}, "
         f"split={split or 'train'})"]
    if not page:
        r.append("(No preview rows available.)")
        return "\n".join(r)
    for i, row in enumerate(page, start=off):
        r.append(f"### Row {i}")
        r.append("```json")
        r.append(json.dumps(row, indent=2, ensure_ascii=False))
        r.append("```")
    return "\n".join(r)


# ---------------------------------------------------------------------------
# paper_search  (GET /api/papers/search)
# ---------------------------------------------------------------------------

@mcp.tool(name="paper_search")
def paper_search(query: str,
                 results_limit: int = 12,
                 concise_only: bool = False) -> str:
    """HF MCP: find Machine Learning research papers on the Hugging
    Face hub. Mirrors `PAPER_SEARCH_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        if not query or len(query) < 3:
            return "Error: Supply at least one search term (min 3 chars)."
        q = query.lower()
        papers = [p for p in s["papers"].values()
                  if q in (p.get("title", "") + " "
                           + p.get("summary", "")
                           + " " + " ".join(p.get("ai_keywords") or [])).lower()]
        papers.sort(key=lambda p: p.get("upvotes", 0), reverse=True)
        limit = max(1, min(int(results_limit or 12), 100))
        page = papers[:limit]
        _record(s, "paper_search", query=query, count=len(page))
        _save_state(s)
        if not page:
            return f"No papers found matching '{query}'."
        r: list[str] = [f"Found {len(page)} papers matching '{query}':", ""]
        for p in page:
            r.append(f"## {p.get('title', p['id'])}")
            r.append("")
            authors = p.get("authors") or []
            if authors:
                names = ", ".join(a.get("name") or
                                  (a.get("user") or {}).get("user") or "?"
                                  for a in authors[:8])
                r.append(f"**Authors:** {names}")
            if p.get("publishedAt"):
                r.append(f"**Published:** {_format_date(p['publishedAt'])}")
            if p.get("upvotes"):
                r.append(f"**Upvotes:** {p['upvotes']}")
            r.append("")
            summary = (p.get("ai_summary")
                       if concise_only and p.get("ai_summary")
                       else p.get("summary", ""))
            if summary:
                r.append(summary if not concise_only
                         else summary[:240])
                r.append("")
            r.append(f"**Link to paper:** "
                     f"[https://hf.co/papers/{p['id']}]"
                     f"(https://hf.co/papers/{p['id']})")
            r.append("")
            r.append("---")
            r.append("")
        return "\n".join(r)


# ---------------------------------------------------------------------------
# space_search  (semantic search over Spaces)
# ---------------------------------------------------------------------------

@mcp.tool(name="space_search")
def space_search(query: str, limit: int = 10, mcp: bool = False) -> str:
    """HF MCP: find Hugging Face Spaces using semantic search. Mirrors
    `SEMANTIC_SEARCH_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        if not query or len(query) < 1:
            return "Error: Query is required."
        spaces = list(s["spaces"].values())
        if mcp:
            spaces = [sp for sp in spaces
                      if "mcp-server" in (sp.get("tags") or [])]
        q = query.lower()
        spaces = [sp for sp in spaces if _matches_query(sp, q)]
        spaces.sort(
            key=lambda sp: sp.get("semanticRelevancyScore",
                                  sp.get("trendingScore", 0)),
            reverse=True)
        limit = max(1, min(int(limit or 10), 100))
        page = spaces[:limit]
        _record(s, "space_search", query=query, mcp=mcp, count=len(page))
        _save_state(s)
        return _format_spaces(page, query=query, limit=limit)


# ---------------------------------------------------------------------------
# space_info  (per-user tabulation; GET /api/spaces?author=…)
# ---------------------------------------------------------------------------

@mcp.tool(name="space_info")
def space_info(username: str | None = None) -> str:
    """HF MCP: tabulate Hugging Face Spaces information for a user.
    Defaults to the authenticated user. Mirrors `SPACE_INFO_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        user = username or s["user"]["name"]
        spaces = [sp for sp in s["spaces"].values()
                  if sp.get("author") == user]
        _record(s, "space_info", username=user, count=len(spaces))
        _save_state(s)
        if not spaces:
            return f"No spaces found for user '{user}'."
        r = [f"# Spaces for {user}", "",
             "| Name | URL | SDK | Status | Likes | Last Modified |",
             "|------|-----|-----|--------|-------|---------------|"]
        for sp in spaces:
            r.append(
                f"| {sp['id']} | https://hf.co/spaces/{sp['id']} "
                f"| {sp.get('sdk', '?')} "
                f"| {(sp.get('runtime') or {}).get('stage', 'UNKNOWN')} "
                f"| {sp.get('likes', 0)} "
                f"| {_format_date(sp.get('lastModified', ''))} |"
            )
        return "\n".join(r)


# ---------------------------------------------------------------------------
# space_files  (list files in a static Space)
# ---------------------------------------------------------------------------

@mcp.tool(name="space_files")
def space_files(spaceName: str | None = None,
                fileType: str = "all") -> str:
    """HF MCP: list all files in a static Hugging Face Space. Mirrors
    `SPACE_FILES_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        name = spaceName or f"{s['user']['name']}/filedrop"
        sp = s["spaces"].get(name)
        if not sp:
            _record(s, "space_files", space=name, result="not_found")
            _save_state(s)
            return f"Error: Space '{name}' not found."
        if sp.get("sdk") != "static":
            _record(s, "space_files", space=name, result="not_static")
            _save_state(s)
            return (f'Error: Space "{name}" is not a static space '
                    f"(found: {sp.get('sdk')}). This tool only works "
                    "with static spaces.")
        prefix = f"spaces/{name}/"
        files = [(k[len(prefix):], v) for k, v in s["files"].items()
                 if k.startswith(prefix)]
        if fileType in ("image", "audio"):
            ext_image = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff",
                         ".tif", ".webp", ".svg", ".ico", ".heic", ".heif"}
            ext_audio = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
                         ".wma", ".opus", ".aiff", ".au", ".ra"}
            wanted = ext_image if fileType == "image" else ext_audio
            files = [(p, f) for p, f in files
                     if any(p.lower().endswith(e) for e in wanted)]
        _record(s, "space_files", space=name, count=len(files),
                fileType=fileType)
        _save_state(s)
        if not files:
            return f"No files found in space '{name}'."
        sub = sp.get("subdomain") or name.split("/", 1)[1].lower()
        r = [f"# Files in {name}", ""]
        for path, f in sorted(files):
            url = f"https://{sub}.hf.space/{path}"
            r.append(f"- **{path}** ({_format_bytes(f.get('size', 0))}) "
                     f"— {url}")
        return "\n".join(r)


# ---------------------------------------------------------------------------
# use_space  (returns a link UI block; we return text)
# ---------------------------------------------------------------------------

@mcp.tool(name="use_space")
def use_space(space_id: str) -> str:
    """HF MCP: give the user access to a Hugging Face Space. Returns
    a link to the running Space. Mirrors `USE_SPACE_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        sp = s["spaces"].get(space_id)
        _record(s, "use_space", space_id=space_id,
                result="ok" if sp else "not_found")
        _save_state(s)
        if not sp:
            return f"Error: Space '{space_id}' not found."
        sub = sp.get("subdomain") or space_id.split("/", 1)[1].lower()
        return "\n".join([
            f"Use Space {space_id}",
            f"Status: {(sp.get('runtime') or {}).get('stage', 'UNKNOWN')}",
            f"URL: https://{sub}.hf.space/",
            f"Hub Page: https://hf.co/spaces/{space_id}",
        ])


# ---------------------------------------------------------------------------
# duplicate_space  (POST /api/spaces/{id}/duplicate)
# ---------------------------------------------------------------------------

@mcp.tool(name="duplicate_space")
def duplicate_space(sourceSpaceId: str,
                    newSpaceId: str | None = None,
                    hardware: str | None = None,
                    private: bool = False) -> str:
    """HF MCP: duplicate a Hugging Face Space into the current user's
    namespace. Mirrors `DUPLICATE_SPACE_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        src = s["spaces"].get(sourceSpaceId)
        if not src:
            _record(s, "duplicate_space", source=sourceSpaceId,
                    result="not_found")
            _save_state(s)
            return f"Error: source space '{sourceSpaceId}' not found."
        author = s["user"]["name"]
        if newSpaceId:
            new_id = (newSpaceId if "/" in newSpaceId
                      else f"{author}/{newSpaceId}")
        else:
            new_id = f"{author}/{sourceSpaceId.split('/', 1)[1]}"
        if new_id in s["spaces"]:
            _record(s, "duplicate_space", source=sourceSpaceId,
                    target=new_id, result="already_exists")
            _save_state(s)
            return f"Error: target space '{new_id}' already exists."
        new_space = _make_space(new_id, private=private,
                                sdk=src.get("sdk", "static"))
        new_space["tags"] = list(src.get("tags") or [])
        new_space["title"] = src.get("title")
        s["spaces"][new_id] = new_space
        # copy files
        src_prefix = f"spaces/{sourceSpaceId}/"
        dst_prefix = f"spaces/{new_id}/"
        for k in list(s["files"].keys()):
            if k.startswith(src_prefix):
                rel = k[len(src_prefix):]
                s["files"][dst_prefix + rel] = dict(s["files"][k])
        _record(s, "duplicate_space", source=sourceSpaceId, target=new_id,
                hardware=hardware, private=private)
        _save_state(s)
        return "\n".join([
            f"Space duplicated: {sourceSpaceId} -> {new_id}",
            f"URL: https://huggingface.co/spaces/{new_id}",
            f"Hardware: {hardware or 'freecpu'}",
            f"Private: {private}",
            "Instructions: visit the new Space URL to configure secrets.",
        ])


# ---------------------------------------------------------------------------
# hf_doc_search / hf_doc_fetch  (docs)
# ---------------------------------------------------------------------------

@mcp.tool(name="hf_doc_search")
def hf_doc_search(query: str = "", product: str | None = None) -> str:
    """HF MCP: search Hugging Face product and library documentation.
    Mirrors `DOCS_SEMANTIC_SEARCH_CONFIG`."""
    with _lock():
        s = _load_state()
        docs = list(s["docs"].values())
        if product:
            docs = [d for d in docs
                    if (d.get("product") or "").lower() == product.lower()]
        q = (query or "").strip().lower()
        if q:
            docs = [d for d in docs
                    if q in (d.get("title", "") + " "
                             + d.get("content", "")).lower()]
        docs = docs[:20]
        _record(s, "hf_doc_search", query=query, product=product,
                count=len(docs))
        _save_state(s)
        if not docs:
            return ("No documentation matched. Try an empty query to "
                    "discover available products.")
        r = ["# Documentation results", ""]
        for d in docs:
            r.append(f"## {d.get('title', d.get('url'))}")
            if d.get("product"):
                r.append(f"**Product:** {d['product']}")
            r.append(f"**URL:** {d.get('url')}")
            snippet = (d.get("content") or "")[:200]
            if snippet:
                r.append(snippet + ("..." if len(d.get("content") or "") > 200
                                    else ""))
            r.append("")
        return "\n".join(r)


@mcp.tool(name="hf_doc_fetch")
def hf_doc_fetch(doc_url: str, offset: int | None = None) -> str:
    """HF MCP: fetch a document from the Hugging Face documentation
    library. Mirrors `DOC_FETCH_CONFIG`."""
    with _lock():
        s = _load_state()
        d = s["docs"].get(doc_url)
        _record(s, "hf_doc_fetch", doc_url=doc_url, offset=offset,
                result="ok" if d else "not_found")
        _save_state(s)
        if not d:
            return f"Error: document '{doc_url}' not in the docs index."
        content = d.get("content") or ""
        off = int(offset or 0)
        # rough token chunking — 1 token ≈ 4 chars
        chunk_chars = 12500 * 4
        page = content[off: off + chunk_chars]
        r = [f"# {d.get('title', doc_url)}", "", page]
        if off + chunk_chars < len(content):
            r.append("")
            r.append(f"--- (truncated; next offset: {off + chunk_chars}) ---")
        return "\n".join(r)


# ---------------------------------------------------------------------------
# hf_jobs  (operations dispatcher: run, ps, logs, inspect, cancel, …)
# ---------------------------------------------------------------------------

@mcp.tool(name="hf_jobs")
def hf_jobs(operation: str, args: dict | None = None) -> str:
    """HF MCP: HuggingFace Jobs dispatcher. Supports `run`, `uv`, `ps`,
    `logs`, `inspect`, `cancel`. Mirrors `HF_JOBS_TOOL_CONFIG`."""
    with _lock():
        s = _load_state()
        args = args or {}
        op = (operation or "").strip().lower()
        if op == "run":
            jid = _new_job(s, args, kind="run")
            _record(s, "hf_jobs", op="run", job_id=jid)
            _save_state(s)
            return f"Job created. id={jid} status=QUEUED"
        if op == "uv":
            jid = _new_job(s, args, kind="uv")
            _record(s, "hf_jobs", op="uv", job_id=jid)
            _save_state(s)
            return f"UV job created. id={jid} status=QUEUED"
        if op == "ps":
            jobs = list(s["jobs"].values())
            _record(s, "hf_jobs", op="ps", count=len(jobs))
            _save_state(s)
            if not jobs:
                return "No jobs."
            r = ["| ID | Kind | Status | Image | Created |",
                 "|----|------|--------|-------|---------|"]
            for j in jobs:
                r.append(f"| {j['id']} | {j.get('kind')} "
                         f"| {j.get('status')} "
                         f"| {j.get('image', '')} | {j.get('createdAt')} |")
            return "\n".join(r)
        if op == "logs":
            jid = args.get("job_id")
            j = s["jobs"].get(jid)
            _record(s, "hf_jobs", op="logs", job_id=jid,
                    result="ok" if j else "not_found")
            _save_state(s)
            if not j:
                return f"Error: job '{jid}' not found."
            return j.get("logs") or "(no logs)"
        if op == "inspect":
            jid = args.get("job_id")
            j = s["jobs"].get(jid)
            _record(s, "hf_jobs", op="inspect", job_id=jid,
                    result="ok" if j else "not_found")
            _save_state(s)
            if not j:
                return f"Error: job '{jid}' not found."
            return json.dumps(j, indent=2)
        if op == "cancel":
            jid = args.get("job_id")
            j = s["jobs"].get(jid)
            if j:
                j["status"] = "CANCELLED"
            _record(s, "hf_jobs", op="cancel", job_id=jid,
                    result="ok" if j else "not_found")
            _save_state(s)
            return f"Job '{jid}' cancelled." if j else f"Error: job '{jid}' not found."
        _record(s, "hf_jobs", op=op, result="unknown_op")
        _save_state(s)
        return (f"Error: unknown operation '{operation}'. "
                "Supported: run, uv, ps, logs, inspect, cancel.")


def _new_job(s: dict, args: dict, *, kind: str) -> str:
    jid = f"job-{s['next_id']['job']:06d}"
    s["next_id"]["job"] += 1
    s["jobs"][jid] = {
        "id": jid,
        "kind": kind,
        "image": args.get("image"),
        "command": args.get("command"),
        "script": args.get("script"),
        "flavor": args.get("flavor", "cpu-basic"),
        "status": "QUEUED",
        "createdAt": _now_iso(),
        "logs": "",
    }
    return jid


# ---------------------------------------------------------------------------
# Mock-only debug tools (not exposed by the real HF MCP)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state dict (users, repos, files,
    call log). Used by verifiers / test harness — not exposed by the
    real HF MCP server."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed_repo")
def mock_debug_seed_repo(repo_type: str, repo_id: str,
                         data: dict | None = None) -> dict:
    """Mock-only: insert or overwrite a repo (model/dataset/space) with
    arbitrary fields. Use for per-task fixtures."""
    with _lock():
        s = _load_state()
        if repo_type not in ("model", "dataset", "space"):
            return {"error": f"invalid repo_type '{repo_type}'"}
        coll = _repo_collection(s, repo_type)
        if repo_type == "model":
            repo = _make_model(repo_id)
        elif repo_type == "dataset":
            repo = _make_dataset(repo_id)
        else:
            repo = _make_space(repo_id)
        if data:
            repo.update(data)
        coll[repo_id] = repo
        _record(s, "debug_seed_repo", repo_type=repo_type, repo_id=repo_id)
        _save_state(s)
        return repo


@_debug_tool(name="mock_debug_upload_file")
def mock_debug_upload_file(repo_type: str, repo_id: str, path: str,
                           content_b64: str) -> dict:
    """Mock-only: write a file blob into a repo's file tree. The real
    HF MCP has no upload tool — agents upload via huggingface_hub /
    `hf` CLI against `https://huggingface.co` directly. This shim is
    used by the per-task harness to simulate uploads (e.g. when the
    agent calls hf_hub_upload with HF_ENDPOINT pointed at a side-car
    HTTP mock that then writes to our state)."""
    with _lock():
        s = _load_state()
        if repo_type not in ("model", "dataset", "space"):
            return {"error": f"invalid repo_type '{repo_type}'"}
        coll = _repo_collection(s, repo_type)
        if repo_id not in coll:
            return {"error": f"{repo_type} '{repo_id}' not found"}
        try:
            blob = base64.b64decode(content_b64)
            size = len(blob)
        except Exception as e:
            return {"error": f"invalid base64: {e}"}
        sha = hashlib.sha256(blob).hexdigest()
        key = f"{repo_type}s/{repo_id}/{path}"
        s["files"][key] = {
            "path": path,
            "size": size,
            "sha": sha,
            "content_b64": content_b64,
            "uploaded_at": _now_iso(),
        }
        # mirror into the repo's siblings list
        siblings = coll[repo_id].setdefault("siblings", [])
        existing = next((sib for sib in siblings
                         if sib.get("rfilename") == path), None)
        if existing:
            existing.update({"size": size, "blob_id": sha})
        else:
            siblings.append({"rfilename": path, "size": size, "blob_id": sha})
        coll[repo_id]["lastModified"] = _now_iso()
        _record(s, "debug_upload_file", repo_type=repo_type, repo_id=repo_id,
                path=path, size=size)
        _save_state(s)
        return {"ok": True, "path": path, "size": size, "sha": sha}


@_debug_tool(name="mock_debug_set_user")
def mock_debug_set_user(name: str, fullname: str | None = None,
                        email: str | None = None) -> dict:
    """Mock-only: override the authenticated mock user identity."""
    with _lock():
        s = _load_state()
        s["user"]["name"] = name
        if fullname is not None:
            s["user"]["fullname"] = fullname
        if email is not None:
            s["user"]["email"] = email
        _record(s, "debug_set_user", name=name)
        _save_state(s)
        return s["user"]


# Note: the real HF MCP exposes a "whoami" implicitly via its system prompt
# rather than a tool. We don't add one because none of the Toolathlon tasks
# call it through MCP.


if __name__ == "__main__":
    mcp.run()
