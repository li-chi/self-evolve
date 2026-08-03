"""Wikipedia mock MCP server.

Mirrors the tool surface of `wikipedia-mcp`
(https://github.com/Rudra-ravi/wikipedia-mcp — the atlas name `wikipedia`).
The real server queries the Wikipedia REST/API; this mock serves the same tool
shapes from a seeded JSON state file so runs are deterministic and offline.

Tool surface (verbatim names from upstream, each also exposed with a
`wikipedia_`-prefixed alias for cross-server discoverability):

  search_wikipedia(query, limit=10, language=None)
  get_article(title, language=None)
  get_summary(title, language=None)
  get_sections(title, language=None)
  get_links(title, language=None)
  get_related_topics(title, limit=10, language=None)
  extract_key_facts(title, topic_within_article=None, count=5, language=None)
  summarize_article_for_query(title, query, max_length=250, language=None)
  summarize_article_section(title, section_title, max_length=150, language=None)

Every tool returns a dict (title + payload) or `{"error": ...}` for a missing
article, matching upstream. State: `$WIKIPEDIA_MOCK_STATE_DIR/state.json`,
seeded from `$WIKIPEDIA_MOCK_SEED_PATH` (built by `synth/mock_seed/wikipedia.py`).
Calls append to `state["calls"]`.
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
    d = os.environ.get("WIKIPEDIA_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/wikipedia_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"articles": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("WIKIPEDIA_MOCK_SEED_PATH")
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


def _find(state: dict, title: str) -> dict | None:
    if not title:
        return None
    arts = state.get("articles", {})
    key = title.strip().lower()
    if key in arts:
        return arts[key]
    for e in arts.values():
        if (e.get("title") or "").strip().lower() == key:
            return e
    return None


def _truncate(text: str, max_length: int) -> str:
    text = text or ""
    if max_length and len(text) > max_length:
        return text[:max_length].rstrip() + "…"
    return text


mcp = FastMCP("wikipedia-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)


# Free-text `search_wikipedia` is a query-phrasing-dependent ranking (not
# reconstructable under recompute verification), so it is OFF by default — the
# fair surface is exact get-by-title. Re-enable via WIKIPEDIA_MOCK_ENABLE_SEARCH=1.
_SEARCH_ENABLED = os.environ.get(
    "WIKIPEDIA_MOCK_ENABLE_SEARCH", "").lower() in ("1", "true", "yes", "on")


# Tool functions carry their public names so they are also importable module
# attributes (in-process callers + the smoke test), then each is registered
# under both its primary name and a `wikipedia_`-prefixed alias below.
def search_wikipedia(query: str, limit: int = 10,
                     language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        terms = [t for t in (query or "").lower().split() if t]
        results = []
        for e in s.get("articles", {}).values():
            hay = (str(e.get("title", "")) + " " + str(e.get("summary", ""))
                   + " " + str(e.get("content", ""))).lower()
            if not terms or all(t in hay for t in terms):
                results.append({
                    "title": e.get("title"),
                    "snippet": _truncate(e.get("summary", ""), 200),
                    "url": e.get("url"),
                })
        n = max(1, min(int(limit or 10), 50))
        results = results[:n]
        _record(s, "search_wikipedia", query=query, count=len(results))
        _save_state(s)
        return {"query": query, "results": results}


def get_article(title: str, language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "get_article", title=title, result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    return {
        "title": e.get("title"),
        "url": e.get("url"),
        "summary": e.get("summary", ""),
        "content": e.get("content", ""),
        "sections": e.get("sections", []),
        "links": e.get("links", []),
        "categories": e.get("categories", []),
        "exists": True,
        # seeded enrichment columns surfaced as top-level fields (keyed-enrichment source)
        **(e.get("_row") or {}),
    }


def get_summary(title: str, language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "get_summary", title=title, result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    return {"title": e.get("title"), "summary": e.get("summary", "")}


def get_sections(title: str, language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "get_sections", title=title, result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    return {"title": e.get("title"), "sections": e.get("sections", [])}


def get_links(title: str, language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "get_links", title=title, result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    return {"title": e.get("title"), "links": e.get("links", [])}


def get_related_topics(title: str, limit: int = 10,
                       language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "get_related_topics", title=title,
                result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    n = max(1, min(int(limit or 10), 50))
    return {"title": e.get("title"), "related": (e.get("related", []))[:n]}


def extract_key_facts(title: str, topic_within_article: str | None = None,
                      count: int = 5, language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "extract_key_facts", title=title,
                result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    facts = list(e.get("key_facts", []))
    if topic_within_article:
        tl = topic_within_article.lower()
        facts = [f for f in facts if tl in str(f).lower()] or facts
    n = max(1, min(int(count or 5), 50))
    return {"title": e.get("title"), "key_facts": facts[:n]}


def summarize_article_for_query(title: str, query: str, max_length: int = 250,
                                language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "summarize_article_for_query", title=title, query=query,
                result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    terms = [t for t in (query or "").lower().split() if t]
    sentences = [p.strip() for p in str(e.get("content", "")).split(".") if p.strip()]
    picked = [x for x in sentences if any(t in x.lower() for t in terms)]
    text = ". ".join(picked or sentences[:2])
    return {"title": e.get("title"), "query": query,
            "summary": _truncate(text, max_length)}


def summarize_article_section(title: str, section_title: str,
                              max_length: int = 150,
                              language: str | None = None) -> dict:
    with _lock():
        s = _load_state()
        e = _find(s, title)
        _record(s, "summarize_article_section", title=title,
                section_title=section_title, result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"Article '{title}' not found"}
    sec = next((x for x in e.get("sections", [])
                if (x.get("title") or "").lower() == (section_title or "").lower()),
               None)
    if not sec:
        return {"error": f"Section '{section_title}' not found in '{title}'"}
    return {"title": e.get("title"), "section": sec.get("title"),
            "summary": _truncate(sec.get("text", ""), max_length)}


@_debug_tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


# Register each tool under its primary name AND a `wikipedia_`-prefixed alias
# (upstream ships both for cross-server discoverability).
_TOOLS = {
    "search_wikipedia": (search_wikipedia, "Search Wikipedia for articles matching a query."),
    "get_article": (get_article, "Get the full content of a Wikipedia article."),
    "get_summary": (get_summary, "Get a concise summary of a Wikipedia article."),
    "get_sections": (get_sections, "Get the sections of a Wikipedia article."),
    "get_links": (get_links, "Get the links contained within a Wikipedia article."),
    "get_related_topics": (get_related_topics,
                           "Get topics related to a Wikipedia article."),
    "extract_key_facts": (extract_key_facts,
                          "Extract key facts from a Wikipedia article."),
    "summarize_article_for_query": (summarize_article_for_query,
                                    "Summarize a Wikipedia article with respect to a query."),
    "summarize_article_section": (summarize_article_section,
                                  "Summarize a specific section of a Wikipedia article."),
}
for _name, (_fn, _desc) in _TOOLS.items():
    if _name == "search_wikipedia" and not _SEARCH_ENABLED:
        continue                      # free-text search gated off by default
    mcp.tool(name=_name, description=_desc)(_fn)
    mcp.tool(name=f"wikipedia_{_name}", description=_desc)(_fn)


if __name__ == "__main__":
    mcp.run()
