"""arXiv mock MCP server.

Mirrors the tool surface of `arxiv-mcp-server`
(https://github.com/blazickjp/arxiv-mcp-server — the atlas name `arxiv` /
`arxiv_local`). The real server queries the arXiv API and stores papers
locally; this mock serves the same tool shapes from a seeded JSON state file
so runs are deterministic and offline.

Tool surface (verbatim names + signatures from upstream tools/):

  search_papers(query, max_results=10, date_from=None, date_to=None,
                categories=None)
  download_paper(paper_id)
  list_papers()
  read_paper(paper_id)

`search_papers` returns `{"total_results", "papers":[{id, title, authors,
abstract, categories, published, resource_uri}]}`; `download_paper` marks a
paper stored and returns `{"status", "message", "resource_uri"}`; `read_paper`
returns `{"status", "paper_id", "content"}` (the paper's markdown body, which
requires a prior download in the real server — the mock enforces the same);
`list_papers` returns `{"total_papers", "papers":[id, ...]}`.

State: `$ARXIV_MOCK_STATE_DIR/state.json`, seeded from `$ARXIV_MOCK_SEED_PATH`.
Built by `synth/mock_seed/arxiv.py`. Calls append to `state["calls"]`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


def _state_path() -> str:
    d = os.environ.get("ARXIV_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/arxiv_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"papers": {}, "downloaded": [], "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ARXIV_MOCK_SEED_PATH")
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
    fd = open(_state_path() + ".lock", "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kw: Any) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kw)
    state["calls"].append(entry)


def _resource_uri(paper_id: str) -> str:
    return f"arxiv://{paper_id}"


def _meta(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "authors": entry.get("authors", []),
        "abstract": entry.get("abstract", ""),
        "categories": entry.get("categories", []),
        "published": entry.get("published"),
        "resource_uri": _resource_uri(entry.get("id", "")),
    }


mcp = FastMCP("arxiv-mock")

# Free-text search is a RANKING over a corpus — its result varies with query
# phrasing (token/Token/tokens), which is not reconstructable under recompute
# verification. It is therefore OFF by default (exact get-by-id is the fair,
# deterministic surface). Re-enable only for corpora that pass a search-
# determinism gate, via ARXIV_MOCK_ENABLE_SEARCH=1.
_SEARCH_ENABLED = os.environ.get(
    "ARXIV_MOCK_ENABLE_SEARCH", "").lower() in ("1", "true", "yes", "on")


def search_papers(query: str, max_results: int = 10,
                  date_from: str | None = None, date_to: str | None = None,
                  categories: list[str] | None = None) -> dict:
    """arXiv API search — case-insensitive term match over title+abstract,
    optionally filtered by `categories` (any-overlap) and published-date
    range. Returns the most recent `max_results` by `published`."""
    with _lock():
        s = _load_state()
        terms = [t for t in (query or "").lower().split() if t]
        cats = {c.lower() for c in (categories or [])}
        hits = []
        for entry in s.get("papers", {}).values():
            hay = (str(entry.get("title", "")) + " "
                   + str(entry.get("abstract", ""))).lower()
            if terms and not all(t in hay for t in terms):
                continue
            if cats and not (cats & {c.lower() for c in entry.get("categories", [])}):
                continue
            pub = entry.get("published") or ""
            if date_from and pub[:10] < date_from:
                continue
            if date_to and pub[:10] > date_to:
                continue
            hits.append(entry)
        hits.sort(key=lambda e: e.get("published") or "", reverse=True)
        n = max(1, min(int(max_results or 10), 50))
        papers = [_meta(e) for e in hits[:n]]
        _record(s, "search_papers", query=query, count=len(papers))
        _save_state(s)
        return {"total_results": len(papers), "papers": papers}


@mcp.tool(
    name="download_paper",
    description="Download a paper and create a resource for it, identified by "
    "its arXiv ID.")
def download_paper(paper_id: str) -> dict:
    """Marks the paper stored locally (so read_paper can serve it) and returns
    a status envelope matching upstream's completed-download branch."""
    with _lock():
        s = _load_state()
        entry = s.get("papers", {}).get(paper_id)
        if not entry:
            _record(s, "download_paper", paper_id=paper_id, result="not_found")
            _save_state(s)
            return {"status": "error",
                    "message": f"Paper {paper_id} not found on arXiv"}
        if paper_id not in s["downloaded"]:
            s["downloaded"].append(paper_id)
        _record(s, "download_paper", paper_id=paper_id, result="ok")
        _save_state(s)
        return {"status": "success",
                "message": f"Paper {paper_id} downloaded successfully",
                "resource_uri": _resource_uri(paper_id)}


@mcp.tool(
    name="list_papers",
    description="List all locally stored (downloaded) papers.")
def list_papers() -> dict:
    """Returns the arXiv IDs downloaded this session."""
    with _lock():
        s = _load_state()
        _record(s, "list_papers", count=len(s.get("downloaded", [])))
        _save_state(s)
        return {"total_papers": len(s.get("downloaded", [])),
                "papers": list(s.get("downloaded", []))}


@mcp.tool(
    name="read_paper",
    description="Read the full markdown content of a downloaded paper by its "
    "arXiv ID.")
def read_paper(paper_id: str) -> dict:
    """Returns the paper's markdown body. Upstream requires a prior
    download_paper; the mock enforces the same to keep the flow faithful."""
    with _lock():
        s = _load_state()
        entry = s.get("papers", {}).get(paper_id)
        if not entry:
            _record(s, "read_paper", paper_id=paper_id, result="not_found")
            _save_state(s)
            return {"status": "error",
                    "message": f"Paper {paper_id} not found"}
        if paper_id not in s.get("downloaded", []):
            _record(s, "read_paper", paper_id=paper_id, result="not_downloaded")
            _save_state(s)
            return {"status": "error",
                    "message": f"Paper {paper_id} not downloaded. "
                               "Use download_paper first."}
        content = entry.get("content")
        if not content:
            content = (f"# {entry.get('title', '')}\n\n"
                       f"**Authors:** {', '.join(entry.get('authors', []))}\n\n"
                       f"## Abstract\n\n{entry.get('abstract', '')}\n")
        _record(s, "read_paper", paper_id=paper_id, result="ok")
        _save_state(s)
        return {"status": "success", "paper_id": paper_id, "content": content}


@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if _SEARCH_ENABLED:
    mcp.tool(
        name="search_papers",
        description="Search for papers on arXiv with advanced filtering "
        "(query, max_results, date_from/date_to, categories).")(search_papers)


if __name__ == "__main__":
    mcp.run()
