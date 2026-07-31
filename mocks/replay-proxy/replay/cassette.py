"""Cassette read/write/hash primitives.

A *cassette* is a JSONL file. Each line is one recorded call:

    {
      "server":      "yahoo-finance",
      "tool":        "get_stock_quote",
      "args_hash":   "sha256:<hex>",
      "args":        {...canonical args dict...},
      "response":    {...whatever the real tool returned...},
      "recorded_at": "2026-05-19T12:34:56.789Z"
    }

Args are *canonicalized* before hashing so that semantically-identical
calls map to the same entry regardless of:

  - key ordering             ({"a":1,"b":2} == {"b":2,"a":1})
  - surrounding whitespace   ("AAPL " == "AAPL")
  - case for known case-insensitive params (e.g. tickers — opt-in)
  - omitted vs explicit null defaults for declared params

The cassette format is intentionally append-only and grep-friendly so
operators can hand-curate / hand-merge cassettes.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
from typing import Any, Iterable, Iterator


# ---------------------------------------------------------------------------
# Canonicalization & hashing
# ---------------------------------------------------------------------------

# Param keys whose values should be uppercased before hashing. Add new
# entries here when a server's tools take case-insensitive identifiers
# (ticker symbols, currency codes, ...).
CASE_INSENSITIVE_KEYS: set[str] = {
    "ticker", "symbol", "tickers", "symbols",
    "currency", "from_currency", "to_currency",
    "country_code", "lang", "language",
}


def _norm_scalar(key: str, value: Any) -> Any:
    """Per-key scalar normalization. Strings are stripped; known
    case-insensitive keys are uppercased. Numbers and bools pass
    through. Nones become None (not stripped from the dict — see
    canonicalize_args)."""
    if isinstance(value, str):
        v = value.strip()
        if key in CASE_INSENSITIVE_KEYS:
            v = v.upper()
        return v
    return value


def _canon(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        # Recurse with each key as the local key context. Drop None
        # values so omitted-vs-explicit-null doesn't change the hash.
        return {
            k: _canon(v, k)
            for k, v in sorted(value.items())
            if v is not None
        }
    if isinstance(value, list):
        # Lists keep order (semantically meaningful for many APIs:
        # e.g. coordinate pairs, sort keys). Normalize elements.
        out = []
        for item in value:
            if isinstance(item, str) and key in CASE_INSENSITIVE_KEYS:
                out.append(_norm_scalar(key, item))
            elif isinstance(item, (dict, list)):
                out.append(_canon(item))
            elif isinstance(item, str):
                out.append(item.strip())
            else:
                out.append(item)
        return out
    return _norm_scalar(key, value)


def canonicalize_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Return a canonical, hashable view of an args dict. Idempotent.

    Unwraps a single top-level {"kwargs": {...}} envelope produced by
    FastMCP when an agent passes args as a kwargs dict instead of
    discrete keyword args — both forms must map to the same canonical
    representation for cassette match.
    """
    if not args:
        return {}
    if (len(args) == 1 and "kwargs" in args
            and isinstance(args["kwargs"], dict)):
        return _canon(dict(args["kwargs"]))
    return _canon(dict(args))


def args_hash(args: dict[str, Any] | None) -> str:
    """SHA-256 of the canonical args dict, prefixed with `sha256:`."""
    canon = canonicalize_args(args)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Entry & cassette objects
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CassetteEntry:
    server: str
    tool: str
    args_hash: str
    args: dict[str, Any]
    response: Any
    recorded_at: str

    @classmethod
    def from_dict(cls, raw: dict) -> "CassetteEntry":
        return cls(
            server=raw["server"],
            tool=raw["tool"],
            args_hash=raw["args_hash"],
            args=raw.get("args") or {},
            response=raw.get("response"),
            recorded_at=raw.get("recorded_at") or "",
        )

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "tool": self.tool,
            "args_hash": self.args_hash,
            "args": self.args,
            "response": self.response,
            "recorded_at": self.recorded_at,
        }


class Cassette:
    """In-memory index over a JSONL cassette. Keyed by (tool, hash).

    Multiple entries with the same key are tolerated (later writes
    override earlier ones — useful for re-recording). `entries`
    preserves insertion order for `list`/`validate` output.

    `loose_args_match` is opt-in (see server.py config schema). When
    True, `loose_lookup` is consulted on a strict miss: it picks the
    recorded entry whose canonical args are the longest *subset* of
    the caller's canonical args (all of entry.args keys appear in
    caller.args with matching values). Use for read-only search-style
    cassettes (arxiv, brave-search, etc.) where args like
    `max_results` / `sort_by` are refinements that don't change the
    answer materially; never enable for state-mutating cassettes
    where ANY unrecorded arg should be loud.
    """

    def __init__(self, path: str | None = None,
                 server: str | None = None,
                 loose_args_match: bool = False) -> None:
        self.path = path
        self.server = server
        self.loose_args_match = loose_args_match
        self.entries: list[CassetteEntry] = []
        self._index: dict[tuple[str, str], CassetteEntry] = {}

    # -- mutation ----------------------------------------------------------

    def add(self, entry: CassetteEntry) -> None:
        self.entries.append(entry)
        self._index[(entry.tool, entry.args_hash)] = entry

    def record(self, tool: str, args: dict, response: Any,
               server: str | None = None,
               recorded_at: str | None = None) -> CassetteEntry:
        canon = canonicalize_args(args)
        entry = CassetteEntry(
            server=server or self.server or "",
            tool=tool,
            args_hash=args_hash(canon),
            args=canon,
            response=response,
            recorded_at=recorded_at or _utc_now(),
        )
        self.add(entry)
        return entry

    # -- lookup ------------------------------------------------------------

    def lookup(self, tool: str, args: dict) -> CassetteEntry | None:
        return self._index.get((tool, args_hash(args)))

    def loose_lookup(self, tool: str, args: dict) -> CassetteEntry | None:
        """Find the recorded entry whose args are the longest *subset*
        of `args` (all of entry.args keys must appear in args with
        canonically-equal values). Picks the most-specific match
        (highest key count); ties broken by recency (last insertion).
        Returns None if no subset entry exists.

        Use case: a read-only search cassette has
        `search_papers(query="X")` recorded; caller passes
        `search_papers(query="X", max_results=5, sort_by="relevance")`.
        Strict lookup misses; loose lookup hits the recorded entry."""
        canon = canonicalize_args(args)
        best: CassetteEntry | None = None
        best_score = -1
        for entry in self.entries:
            if entry.tool != tool:
                continue
            e_args = entry.args or {}
            # Subset check: every key in entry.args must be present in
            # canon with the same canonical value.
            ok = True
            for k, v in e_args.items():
                if k not in canon or canon[k] != v:
                    ok = False
                    break
            if not ok:
                continue
            score = len(e_args)
            # >= so later insertions win on ties (mirrors strict-lookup
            # semantics: later writes override earlier ones).
            if score >= best_score:
                best = entry
                best_score = score
        return best

    def tools(self) -> list[str]:
        return sorted({e.tool for e in self.entries})

    def __len__(self) -> int:
        return len(self.entries)

    # -- io ----------------------------------------------------------------

    def save(self, path: str | None = None) -> str:
        target = path or self.path
        if not target:
            raise ValueError("Cassette has no path; pass one to save()")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False,
                                   separators=(",", ":")))
                f.write("\n")
        os.replace(tmp, target)
        self.path = target
        return target


def _utc_now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _iter_jsonl(path: str) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{i}: malformed JSONL ({exc.msg})"
                ) from exc


def load_cassette(path: str, server: str | None = None,
                  loose_args_match: bool = False) -> Cassette:
    """Load a JSONL cassette into memory. Missing file returns an empty
    cassette so callers can declare a path before it exists."""
    cas = Cassette(path=path, server=server,
                   loose_args_match=loose_args_match)
    if not os.path.exists(path):
        return cas
    for raw in _iter_jsonl(path):
        cas.add(CassetteEntry.from_dict(raw))
    return cas


def write_entry(path: str, entry: CassetteEntry) -> None:
    """Append a single entry to a cassette file. Used by the recorder
    hook so we don't have to keep the whole cassette in memory."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False,
                           separators=(",", ":")))
        f.write("\n")


def validate_cassette(path: str) -> list[str]:
    """Return a list of human-readable issues (empty == clean).

    Checks:
      - file parses as JSONL
      - each entry has required fields
      - args_hash matches recomputed hash of canonicalized args
      - no duplicate (tool, args_hash) entries (warning only)
    """
    issues: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    required = {"server", "tool", "args_hash", "args", "response"}
    for i, raw in enumerate(_iter_jsonl(path), 1):
        missing = required - set(raw.keys())
        if missing:
            issues.append(
                f"line {i}: missing fields {sorted(missing)}")
            continue
        expected = args_hash(raw["args"])
        if expected != raw["args_hash"]:
            issues.append(
                f"line {i}: args_hash mismatch "
                f"(stored={raw['args_hash']}, recomputed={expected})")
        key = (raw["tool"], raw["args_hash"])
        if key in seen:
            issues.append(
                f"line {i}: duplicate of line {seen[key]} "
                f"(tool={raw['tool']}, hash={raw['args_hash'][:16]}…)")
        else:
            seen[key] = i
    return issues
