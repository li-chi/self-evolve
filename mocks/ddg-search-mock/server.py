"""DuckDuckGo-search mock MCP server.

Mirrors the tool surface of `duckduckgo-mcp-server`
(https://github.com/nickclyde/duckduckgo-mcp-server — the atlas name
`ddg-search`). The real server queries DuckDuckGo and fetches page content;
this mock serves a seeded search index + "mini-web" corpus so runs are
deterministic and offline.

Tool surface (verbatim names + signatures):

  search(query, max_results=10, region="")
      -> a formatted Markdown string of ranked results (title/URL/snippet)
  fetch_content(url, start_index=0, max_length=8000, backend=None)
      -> the page's cleaned text, sliced to [start_index : start_index+max_length]

State: `$DDG_SEARCH_MOCK_STATE_DIR/state.json`, seeded from
`$DDG_SEARCH_MOCK_SEED_PATH` (built by `synth/mock_seed/ddg_search.py`). Calls
append to `state["calls"]`.
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
    d = os.environ.get("DDG_SEARCH_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/ddg_search_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"results": [], "pages": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("DDG_SEARCH_MOCK_SEED_PATH")
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


def _norm(u: str) -> str:
    return (u or "").strip().rstrip("/")


mcp = FastMCP("ddg-search-mock")

# `search` is a query-phrasing-dependent ranking (not reconstructable under
# recompute verification), so it is OFF by default — the fair surface here is
# exact `fetch_content` by URL. Re-enable via DDG_SEARCH_MOCK_ENABLE_SEARCH=1
# for a corpus that passes a search-determinism gate.
_SEARCH_ENABLED = os.environ.get(
    "DDG_SEARCH_MOCK_ENABLE_SEARCH", "").lower() in ("1", "true", "yes", "on")


def search(query: str, max_results: int = 10, region: str = "") -> str:
    """Ranks seeded results by term overlap on title+snippet and returns the
    upstream-style formatted string."""
    with _lock():
        s = _load_state()
        terms = [t for t in (query or "").lower().split() if t]
        scored = []
        for r in s.get("results", []):
            hay = (str(r.get("title", "")) + " " + str(r.get("snippet", ""))
                   + " " + " ".join(r.get("keywords", []))).lower()
            score = sum(1 for t in terms if t in hay)
            if not terms or score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        n = max(1, min(int(max_results or 10), 25))
        hits = [r for _, r in scored[:n]]
        _record(s, "search", query=query, region=region, count=len(hits))
        _save_state(s)
    if not hits:
        return f"No results found for query: '{query}'"
    lines = [f"Found {len(hits)} search results:\n"]
    for i, r in enumerate(hits, 1):
        lines.append(f"{i}. {r.get('title', '')}\n"
                     f"   URL: {r.get('url', '')}\n"
                     f"   {r.get('snippet', '')}\n")
    return "\n".join(lines)


@mcp.tool(
    name="fetch_content",
    description="Fetch and parse content from a webpage URL. Args: url, "
    "start_index (default 0), max_length (default 8000), backend "
    "(optional: httpx|curl|auto).")
def fetch_content(url: str, start_index: int = 0, max_length: int = 8000,
                  backend: str | None = None) -> str:
    """Returns the seeded page's cleaned text, sliced like upstream (start
    offset + max_length window), with a truncation note when more remains."""
    with _lock():
        s = _load_state()
        pages = s.get("pages", {})
        body = pages.get(_norm(url), pages.get(url))
        _record(s, "fetch_content", url=url, start_index=start_index,
                result="ok" if body is not None else "not_found")
        _save_state(s)
    if body is None:
        return f"Error: could not fetch content from {url}"
    start = max(0, int(start_index or 0))
    end = start + max(1, int(max_length or 8000))
    chunk = body[start:end]
    note = ""
    if end < len(body):
        note = (f"\n\n[Content truncated. Use start_index={end} to continue "
                f"reading. Total length: {len(body)} chars.]")
    return f"Contents of {url}:\n\n{chunk}{note}"


@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if _SEARCH_ENABLED:
    mcp.tool(
        name="search",
        description="Search DuckDuckGo and return formatted results "
        "(query, max_results, region).")(search)


if __name__ == "__main__":
    mcp.run()
