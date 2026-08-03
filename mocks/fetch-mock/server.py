"""Web-fetch mock MCP server.

Mirrors the tool surface of `@tokenizin/mcp-npx-fetch`
(https://github.com/tokenizin-agency/mcp-npx-fetch — the atlas name `fetch`).
The real server fetches a live URL and converts it to HTML / JSON / text /
Markdown; this mock serves the same shapes from a seeded "mini-web" corpus so
runs are deterministic and offline.

Tool surface (verbatim names + signatures):

  fetch_html(url, headers=None)      -> raw HTML string
  fetch_json(url, headers=None)      -> parsed JSON (object)
  fetch_txt(url, headers=None)       -> clean plain text
  fetch_markdown(url, headers=None)  -> Markdown string

Only `url` is used by the mock; `headers` is accepted-and-ignored. A page that
was seeded with just one representation derives the others (text from HTML by
tag-stripping; Markdown from text). State: `$FETCH_MOCK_STATE_DIR/state.json`,
seeded from `$FETCH_MOCK_SEED_PATH` (built by `synth/mock_seed/fetch.py`).
Calls append to `state["calls"]`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP


def _state_path() -> str:
    d = os.environ.get("FETCH_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/fetch_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"pages": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("FETCH_MOCK_SEED_PATH")
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


def _page(state: dict, url: str) -> dict | None:
    pages = state.get("pages", {})
    key = _norm(url)
    if key in pages:
        return pages[key]
    return pages.get(url)


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


mcp = FastMCP("fetch-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



@mcp.tool(name="fetch_html",
          description="Fetch a website and return the content as raw HTML.")
def fetch_html(url: str, headers: dict | None = None) -> str:
    with _lock():
        s = _load_state()
        p = _page(s, url)
        _record(s, "fetch_html", url=url, result="ok" if p else "not_found")
        _save_state(s)
    if not p:
        return f"Error: failed to fetch {url}"
    if p.get("html") is not None:
        return p["html"]
    return f"<html><body>{p.get('text', '')}</body></html>"


@mcp.tool(name="fetch_json",
          description="Fetch a JSON file from a URL and return the parsed "
          "JSON content.")
def fetch_json(url: str, headers: dict | None = None) -> Any:
    with _lock():
        s = _load_state()
        p = _page(s, url)
        _record(s, "fetch_json", url=url, result="ok" if p else "not_found")
        _save_state(s)
    if not p or p.get("json") is None:
        return {"error": f"failed to fetch JSON from {url}"}
    return p["json"]


@mcp.tool(name="fetch_txt",
          description="Fetch a website and return the content as plain text "
          "(no HTML tags or scripts).")
def fetch_txt(url: str, headers: dict | None = None) -> str:
    with _lock():
        s = _load_state()
        p = _page(s, url)
        _record(s, "fetch_txt", url=url, result="ok" if p else "not_found")
        _save_state(s)
    if not p:
        return f"Error: failed to fetch {url}"
    if p.get("text") is not None:
        return p["text"]
    return _strip_html(p.get("html", ""))


@mcp.tool(name="fetch_markdown",
          description="Fetch a website and return the content as Markdown.")
def fetch_markdown(url: str, headers: dict | None = None) -> str:
    with _lock():
        s = _load_state()
        p = _page(s, url)
        _record(s, "fetch_markdown", url=url, result="ok" if p else "not_found")
        _save_state(s)
    if not p:
        return f"Error: failed to fetch {url}"
    if p.get("markdown") is not None:
        return p["markdown"]
    if p.get("text") is not None:
        return p["text"]
    return _strip_html(p.get("html", ""))


@_debug_tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if __name__ == "__main__":
    mcp.run()
