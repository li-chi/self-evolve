"""PubMed mock MCP server.

Mirrors the tool surface of `PubMed-MCP-Server`
(https://github.com/JackKuo666/PubMed-MCP-Server — the atlas name `pubmed`).
The real server queries NCBI E-utilities; this mock serves the same tool
shapes from a seeded JSON state file so runs are deterministic and offline.

Tool surface (verbatim names from upstream):

  search_pubmed_key_words(key_words, num_results=10)
  search_pubmed_advanced(term=None, title=None, author=None, journal=None,
                         start_date=None, end_date=None, num_results=10)
  get_pubmed_article_metadata(pmid)
  download_pubmed_pdf(pmid)
  deep_paper_analysis(pmid)

Search tools return a list of article-metadata dicts; `get_pubmed_article_metadata`
returns one dict; `download_pubmed_pdf` returns a status message string;
`deep_paper_analysis` returns a structured analysis dict built from the seeded
article. State: `$PUBMED_MOCK_STATE_DIR/state.json`, seeded from
`$PUBMED_MOCK_SEED_PATH` (built by `synth/mock_seed/pubmed.py`). Calls append
to `state["calls"]`.
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
    d = os.environ.get("PUBMED_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/pubmed_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"articles": {}, "downloaded": [], "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("PUBMED_MOCK_SEED_PATH")
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


def _meta(e: dict) -> dict:
    return {
        "pmid": e.get("pmid"),
        "title": e.get("title"),
        "authors": e.get("authors", []),
        "journal": e.get("journal"),
        "pub_date": e.get("pub_date"),
        "abstract": e.get("abstract", ""),
        "doi": e.get("doi"),
        "keywords": e.get("keywords", []),
    }


mcp = FastMCP("pubmed-mock")

# Free-text search is a query-phrasing-dependent ranking (not reconstructable
# under recompute verification), so it is OFF by default — the fair surface is
# exact get-by-PMID. Re-enable via PUBMED_MOCK_ENABLE_SEARCH=1 once the corpus
# passes a search-determinism gate.
_SEARCH_ENABLED = os.environ.get(
    "PUBMED_MOCK_ENABLE_SEARCH", "").lower() in ("1", "true", "yes", "on")


def search_pubmed_key_words(key_words: str, num_results: int = 10) -> list:
    """Case-insensitive term match over title+abstract+keywords, most recent
    (by pub_date) first, capped at `num_results`."""
    with _lock():
        s = _load_state()
        terms = [t for t in (key_words or "").lower().split() if t]
        hits = []
        for e in s.get("articles", {}).values():
            hay = (str(e.get("title", "")) + " " + str(e.get("abstract", ""))
                   + " " + " ".join(e.get("keywords", []))).lower()
            if not terms or all(t in hay for t in terms):
                hits.append(e)
        hits.sort(key=lambda e: e.get("pub_date") or "", reverse=True)
        n = max(1, min(int(num_results or 10), 100))
        out = [_meta(e) for e in hits[:n]]
        _record(s, "search_pubmed_key_words", key_words=key_words,
                count=len(out))
        _save_state(s)
        return out


def search_pubmed_advanced(term: str | None = None, title: str | None = None,
                           author: str | None = None,
                           journal: str | None = None,
                           start_date: str | None = None,
                           end_date: str | None = None,
                           num_results: int = 10) -> list:
    """AND across every supplied filter; date filters compare the article's
    `pub_date` normalized to YYYY-MM-DD."""
    def _norm(d: str | None) -> str | None:
        return d.replace("/", "-")[:10] if d else None

    lo, hi = _norm(start_date), _norm(end_date)
    with _lock():
        s = _load_state()
        hits = []
        for e in s.get("articles", {}).values():
            hay = (str(e.get("title", "")) + " " + str(e.get("abstract", ""))).lower()
            if term and term.lower() not in hay:
                continue
            if title and title.lower() not in str(e.get("title", "")).lower():
                continue
            if author and not any(author.lower() in a.lower()
                                  for a in e.get("authors", [])):
                continue
            if journal and journal.lower() not in str(e.get("journal", "")).lower():
                continue
            pub = _norm(e.get("pub_date")) or ""
            if lo and pub < lo:
                continue
            if hi and pub > hi:
                continue
            hits.append(e)
        hits.sort(key=lambda e: e.get("pub_date") or "", reverse=True)
        n = max(1, min(int(num_results or 10), 100))
        out = [_meta(e) for e in hits[:n]]
        _record(s, "search_pubmed_advanced", term=term, author=author,
                count=len(out))
        _save_state(s)
        return out


@mcp.tool(
    name="get_pubmed_article_metadata",
    description="Fetch metadata for a single PubMed article by its PMID.")
def get_pubmed_article_metadata(pmid: str) -> dict:
    """Returns the article-metadata dict, or an error envelope if the PMID is
    not seeded."""
    with _lock():
        s = _load_state()
        e = s.get("articles", {}).get(str(pmid))
        _record(s, "get_pubmed_article_metadata", pmid=str(pmid),
                result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"No article found with PMID {pmid}"}
    # a seeded article may carry enrichment columns (a `_row` dict) — surface them as
    # top-level fields so the record is a keyed-enrichment source for the synth engine.
    return {**_meta(e), **(e.get("_row") or {})}


@mcp.tool(
    name="download_pubmed_pdf",
    description="Attempt to download the full-text PDF for a PubMed article by "
    "its PMID. Returns a status message.")
def download_pubmed_pdf(pmid: str) -> str:
    """Returns a success/failure message string (matching upstream, which
    reports whether an open-access PDF was retrievable)."""
    with _lock():
        s = _load_state()
        e = s.get("articles", {}).get(str(pmid))
        if not e:
            _record(s, "download_pubmed_pdf", pmid=str(pmid), result="not_found")
            _save_state(s)
            return f"No article found with PMID {pmid}"
        if not e.get("pdf_available", False):
            _record(s, "download_pubmed_pdf", pmid=str(pmid), result="no_pdf")
            _save_state(s)
            return (f"Full-text PDF for PMID {pmid} is not openly available.")
        if str(pmid) not in s["downloaded"]:
            s["downloaded"].append(str(pmid))
        _record(s, "download_pubmed_pdf", pmid=str(pmid), result="ok")
        _save_state(s)
        return (f"Successfully downloaded PDF for PMID {pmid} "
                f"to {str(pmid)}.pdf")


@mcp.tool(
    name="deep_paper_analysis",
    description="Perform a structured analysis of a PubMed article by its PMID "
    "(summary, key findings, methods, conclusions).")
def deep_paper_analysis(pmid: str) -> dict:
    """Returns a structured analysis dict derived from the seeded article's
    fields (deterministic; no LLM call in the mock)."""
    with _lock():
        s = _load_state()
        e = s.get("articles", {}).get(str(pmid))
        _record(s, "deep_paper_analysis", pmid=str(pmid),
                result="ok" if e else "not_found")
        _save_state(s)
    if not e:
        return {"error": f"No article found with PMID {pmid}"}
    return {
        "pmid": e.get("pmid"),
        "title": e.get("title"),
        "summary": e.get("abstract", ""),
        "key_findings": e.get("key_findings", []),
        "methods": e.get("methods", ""),
        "conclusions": e.get("conclusions", ""),
        "journal": e.get("journal"),
        "pub_date": e.get("pub_date"),
    }


@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if _SEARCH_ENABLED:
    mcp.tool(
        name="search_pubmed_key_words",
        description="Search PubMed for articles matching the given keywords "
        "and return their metadata (num_results caps the list).")(
            search_pubmed_key_words)
    mcp.tool(
        name="search_pubmed_advanced",
        description="Advanced PubMed search with optional term/title/author/"
        "journal and start_date/end_date filters.")(search_pubmed_advanced)


if __name__ == "__main__":
    mcp.run()
