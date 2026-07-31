"""PostgreSQL mock MCP server.

Mirrors the tool surface of Anthropic's `@modelcontextprotocol/server-postgres`
(github.com/modelcontextprotocol/servers/tree/main/src/postgres) plus the
conventions used by `crystaldba/postgres-mcp` and similar Postgres MCP
servers. The user-facing tools are: `query`, `execute`, `list_databases`,
`list_schemas`, `list_tables`, `describe_table`, `list_indexes`,
`list_views`, `explain_query`, `list_functions`, `current_settings`.

Backed by a real SQLite database at `$PG_MOCK_STATE_DIR/state.db` so that
SELECT/INSERT/UPDATE/DELETE/CREATE/DROP issued by the agent stay coherent
across calls. The catalog (databases / schemas / table definitions, with
Postgres column metadata: type name, oid, nullability, default, primary
key, foreign keys) lives in a JSON file at `$PG_MOCK_STATE_DIR/state.json`
alongside the sqlite db.

Postgres-isms emulated:
  - Schema-qualified names (`myschema.mytable`) — rewritten to a flat
    sqlite table `<schema>__<table>`. search_path defaults to
    `"$user, public"`; unqualified names fall back to `public`.
  - Postgres data types: text/varchar/char/uuid/bytea/jsonb/json/date/
    timestamp[tz]/numeric/decimal/integer family/serial/boolean/array.
    Reported as Postgres type names in column metadata; storage maps to
    sqlite native types.
  - Standard Postgres OIDs in column metadata (`dataTypeID`):
    text=25, varchar=1043, int4=23, int8=20, bool=16, float8=701,
    numeric=1700, timestamp=1114, timestamptz=1184, date=1082,
    jsonb=3802, uuid=2950, bytea=17.
  - `information_schema.*` and a subset of `pg_catalog.*` (tables,
    columns, schemata, pg_tables, pg_namespace, pg_class, pg_database)
    served from the catalog JSON, not sqlite.
  - `RETURNING` clause on INSERT/UPDATE/DELETE — implemented by
    re-querying after the write.
  - `ILIKE` rewritten to LIKE with LOWER() in sqlite.
  - `::cast` syntax stripped (`'1'::int` -> `'1'`).
  - `||` string concat (already supported by sqlite).
  - `ON CONFLICT` UPSERT (already supported by sqlite).
  - Parameterized queries: both `$1, $2, ...` (Postgres) and `?`
    (sqlite) styles. `$N` placeholders converted to `?` before
    execution, preserving param-list order.

NOT supported (out of scope for the mock — agents should not depend on
these): PL/pgSQL functions, triggers, materialized views, table
inheritance, LISTEN/NOTIFY, COPY, prepared statements lifecycle, full
sequence DDL (only serial column auto-increment via sqlite ROWID),
GIN/GiST indexes, tsvector full-text search, table partitioning,
transactions spanning multiple tool calls. CTEs and window functions
are passed through to sqlite which supports a useful subset (sqlite
3.25+ has WITH and window functions, sqlite 3.8.3+ has WITH).

A JSON state file at `$PG_MOCK_STATE_DIR/state.json` records the
catalog and a `calls` log used by the verifier:

  state = {
    "databases": {
      "<db_name>": {
        "schemas": {
          "<schema_name>": {
            "tables": {
              "<table_name>": {
                "columns": [{"name", "type", "oid", "nullable",
                             "default", "primary_key",
                             "foreign_key": {"schema","table","column"}}],
                "indexes": [{"name", "columns", "is_unique",
                             "is_primary"}],
                "constraints": [...]
              }
            },
            "views": {"<view_name>": {"definition": "..."}},
            "functions": {"<fn_name>": {"signature": "..."}}
          }
        }
      }
    },
    "current_database": "postgres",
    "search_path": ["$user", "public"],
    "calls": [...]
  }
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import sqlite3
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State / locking
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    d = os.environ.get(
        "PG_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/postgres_mock"),
    )
    os.makedirs(d, exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(_state_dir(), "state.json")


def _db_path() -> str:
    return os.path.join(_state_dir(), "state.db")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "databases": {
            "postgres": {
                "schemas": {
                    "public": {
                        "tables": {},
                        "views": {},
                        "functions": {},
                    },
                },
            },
        },
        "current_database": "postgres",
        "search_path": ["$user", "public"],
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed_path = os.environ.get("PG_MOCK_SEED_PATH")
        if seed_path and os.path.exists(seed_path):
            with open(seed_path, "r", encoding="utf-8") as f:
                seeded = json.load(f)
            # Caller is responsible for shape; merge with empty
            base = _empty_state()
            base.update(seeded)
            base.setdefault("calls", [])
            _save_state(base)
            return base
        s = _empty_state()
        _save_state(s)
        return s
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    lock_path = os.path.join(_state_dir(), "state.lock")
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
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Postgres type system
# ---------------------------------------------------------------------------

# Standard Postgres OIDs. See src/include/catalog/pg_type_d.h.
PG_TYPE_OIDS: dict[str, int] = {
    "bool": 16, "boolean": 16,
    "bytea": 17,
    "char": 18,
    "name": 19,
    "int8": 20, "bigint": 20,
    "int2": 21, "smallint": 21,
    "int4": 23, "integer": 23, "int": 23,
    "text": 25,
    "oid": 26,
    "json": 114,
    "xml": 142,
    "float4": 700, "real": 700,
    "float8": 701, "double precision": 701,
    "money": 790,
    "bpchar": 1042,  # blank-padded char
    "varchar": 1043, "character varying": 1043,
    "date": 1082,
    "time": 1083,
    "timestamp": 1114,
    "timestamptz": 1184, "timestamp with time zone": 1184,
    "interval": 1186,
    "timetz": 1266,
    "numeric": 1700, "decimal": 1700,
    "uuid": 2950,
    "jsonb": 3802,
}


def _normalize_pg_type(raw: str) -> str:
    """Lowercase and strip parens (varchar(255) -> varchar). Preserve
    array marker."""
    if not raw:
        return "text"
    t = raw.strip().lower()
    is_array = t.endswith("[]")
    if is_array:
        t = t[:-2].strip()
    # Strip parens: varchar(255), numeric(10,2), char(8) -> bare name
    t = re.sub(r"\s*\(.*\)\s*$", "", t)
    # Normalize multi-word aliases
    if t in ("character varying",):
        t = "varchar"
    if t in ("double precision",):
        t = "float8"
    if t in ("timestamp with time zone",):
        t = "timestamptz"
    if t in ("timestamp without time zone",):
        t = "timestamp"
    if t in ("time with time zone",):
        t = "timetz"
    if t in ("time without time zone",):
        t = "time"
    if is_array:
        # Arrays of unknown type still report OID 0; the catalog stores
        # the bare name so callers can re-stringify.
        return t + "[]"
    return t


def _pg_type_oid(pg_type: str) -> int:
    """Return the OID for a Postgres type string. Arrays return 0 in the
    mock (the real OIDs are per-element-type +1000 by convention, but
    we don't carry the array OID table)."""
    t = _normalize_pg_type(pg_type)
    if t.endswith("[]"):
        return 0
    return PG_TYPE_OIDS.get(t, 0)


def _pg_to_sqlite_type(pg_type: str) -> str:
    """Map a Postgres type to a sqlite affinity. Used for CREATE TABLE
    DDL rewriting and for `describe_table` reporting only — sqlite
    affinities are advisory."""
    t = _normalize_pg_type(pg_type)
    if t.endswith("[]"):
        return "TEXT"  # JSON-encoded array
    if t in ("int2", "smallint", "int4", "integer", "int", "int8",
             "bigint", "serial", "bigserial", "smallserial", "oid"):
        return "INTEGER"
    if t in ("bool", "boolean"):
        return "INTEGER"
    if t in ("real", "float4", "float8", "double precision"):
        return "REAL"
    # Everything else (numeric, text, varchar, char, uuid, bytea, json,
    # jsonb, date, timestamp, timestamptz, time, interval, money, ...)
    # stores as TEXT so we never lose precision.
    return "TEXT"


def _is_serial(pg_type: str) -> bool:
    return _normalize_pg_type(pg_type) in ("serial", "bigserial",
                                            "smallserial")


# ---------------------------------------------------------------------------
# Flat-name + search-path resolution
# ---------------------------------------------------------------------------

def _flat_name(schema: str, table: str) -> str:
    return f"{schema}__{table}"


def _resolve_search_path(state: dict) -> list[str]:
    """Resolve search_path entries — replace `$user` with current_user
    (we use 'postgres' as the only user)."""
    out = []
    for p in state.get("search_path", ["$user", "public"]):
        if p == "$user":
            out.append("postgres")
        else:
            out.append(p)
    return out


def _resolve_unqualified(state: dict, db: str, name: str
                         ) -> tuple[str, str] | None:
    """Look up an unqualified table name through search_path. Returns
    (schema, table) if found, else None."""
    schemas = (state["databases"].get(db, {}).get("schemas") or {})
    for sch in _resolve_search_path(state):
        body = schemas.get(sch) or {}
        if name in (body.get("tables") or {}):
            return sch, name
        if name in (body.get("views") or {}):
            return sch, name
    return None


# ---------------------------------------------------------------------------
# SQL rewriting (Postgres -> sqlite)
# ---------------------------------------------------------------------------

_SCHEMA_TABLE_RE = re.compile(
    r'(?<!["\w.])'
    r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)'
    r'(?!["\w])'
)

_DOLLAR_PARAM_RE = re.compile(r"\$(\d+)")

# `expr::type` cast — strip the cast (we don't enforce types in sqlite).
# Capture handles paren-wrapped types like ::varchar(255).
_CAST_RE = re.compile(
    r"::\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?(?:\s*\[\s*\])?"
)

_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)


def _rewrite_sql(query: str, state: dict, *,
                 current_db: str | None = None) -> str:
    """Rewrite a Postgres-flavoured query so sqlite can execute it:
      - `schema.table` -> `"schema__table"` (excluding information_schema
        and pg_catalog, which are intercepted before sqlite).
      - `::type` casts stripped.
      - `ILIKE` -> `LIKE` (with LOWER() wrapping handled by caller's
        operands; we only swap the keyword).
      - `$N` placeholders left in place (converted right before execute,
        in concert with param reordering).
    """
    q = query

    # Strip ::cast (must precede schema-qualifier rewrite so we don't
    # confuse `foo::int` with `schema.table` patterns).
    q = _CAST_RE.sub("", q)

    # ILIKE -> LIKE. SQLite's LIKE is case-insensitive for ASCII by
    # default (PRAGMA case_sensitive_like=0). Good enough for the mock.
    q = _ILIKE_RE.sub("LIKE", q)

    # schema.table -> "schema__table"  (skip information_schema /
    # pg_catalog — those are intercepted upstream).
    def _qualify(m: re.Match) -> str:
        schema, table = m.group(1), m.group(2)
        if schema.lower() in ("information_schema", "pg_catalog"):
            return m.group(0)
        return f'"{_flat_name(schema.lower(), table.lower())}"'

    q = _SCHEMA_TABLE_RE.sub(_qualify, q)

    return q


def _convert_dollar_params(query: str,
                           params: list[Any] | None
                           ) -> tuple[str, list[Any]]:
    """Convert `$1, $2, ...` to `?` placeholders, reordering `params`
    to match the textual order of placeholders. If `params` is None,
    return the original query and an empty list."""
    if "$" not in query or params is None:
        return query, list(params or [])
    out_params: list[Any] = []

    def _sub(m: re.Match) -> str:
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(params):
            raise ValueError(
                f"placeholder ${idx + 1} out of range "
                f"(have {len(params)} params)")
        out_params.append(params[idx])
        return "?"

    new_q = _DOLLAR_PARAM_RE.sub(_sub, query)
    return new_q, out_params


# ---------------------------------------------------------------------------
# information_schema / pg_catalog shim
# ---------------------------------------------------------------------------

_IS_VIEW_RE = re.compile(
    r"\b(information_schema|pg_catalog)\.([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)


def _looks_like_catalog_query(query: str) -> bool:
    return bool(_IS_VIEW_RE.search(query))


def _serve_catalog_query(query: str, state: dict, db: str
                          ) -> list[dict] | None:
    """Crude SELECT against information_schema / pg_catalog views.
    Supports the common filter patterns the agent typically writes:
      WHERE table_schema = '...' [AND table_name = '...']
      WHERE schema_name = '...'
      WHERE datname = '...'
    Returns None if the query doesn't match a supported view."""
    m = _IS_VIEW_RE.search(query)
    if not m:
        return None
    namespace = m.group(1).lower()
    view = m.group(2).lower()

    def _extract(name: str) -> str | None:
        rx = re.search(rf"\b{name}\s*=\s*'([^']*)'", query, re.IGNORECASE)
        return rx.group(1) if rx else None

    schemas = (state["databases"].get(db, {}).get("schemas") or {})

    if namespace == "information_schema" and view == "schemata":
        rows = [{
            "catalog_name": db,
            "schema_name": s,
            "schema_owner": "postgres",
        } for s in sorted(schemas.keys())]
        wm = _extract("schema_name")
        if wm:
            rows = [r for r in rows if r["schema_name"] == wm]
        return rows

    if namespace == "information_schema" and view == "tables":
        s_filter = _extract("table_schema")
        t_filter = _extract("table_name")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for t in sorted((body.get("tables") or {}).keys()):
                if t_filter and t != t_filter:
                    continue
                rows.append({
                    "table_catalog": db, "table_schema": s,
                    "table_name": t, "table_type": "BASE TABLE",
                })
            for v in sorted((body.get("views") or {}).keys()):
                if t_filter and v != t_filter:
                    continue
                rows.append({
                    "table_catalog": db, "table_schema": s,
                    "table_name": v, "table_type": "VIEW",
                })
        return rows

    if namespace == "information_schema" and view == "columns":
        s_filter = _extract("table_schema")
        t_filter = _extract("table_name")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for t, tbl in (body.get("tables") or {}).items():
                if t_filter and t != t_filter:
                    continue
                for i, col in enumerate(tbl.get("columns") or [], 1):
                    pg_type = col.get("type", "text")
                    norm = _normalize_pg_type(pg_type)
                    rows.append({
                        "table_catalog": db,
                        "table_schema": s,
                        "table_name": t,
                        "column_name": col["name"],
                        "ordinal_position": i,
                        "column_default": col.get("default"),
                        "is_nullable": ("YES" if col.get(
                            "nullable", True) else "NO"),
                        "data_type": norm,
                        "udt_name": norm.replace("[]", ""),
                        "character_maximum_length":
                            _char_max_length(pg_type),
                        "numeric_precision":
                            _numeric_precision(pg_type),
                        "numeric_scale":
                            _numeric_scale(pg_type),
                    })
        return rows

    if namespace == "information_schema" and view == "views":
        s_filter = _extract("table_schema")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for v, vbody in (body.get("views") or {}).items():
                rows.append({
                    "table_catalog": db, "table_schema": s,
                    "table_name": v,
                    "view_definition": vbody.get("definition", ""),
                })
        return rows

    if namespace == "information_schema" and view == "key_column_usage":
        # Return primary key + unique key column references.
        s_filter = _extract("table_schema")
        t_filter = _extract("table_name")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for t, tbl in (body.get("tables") or {}).items():
                if t_filter and t != t_filter:
                    continue
                for i, col in enumerate(tbl.get("columns") or [], 1):
                    if col.get("primary_key"):
                        rows.append({
                            "table_catalog": db,
                            "table_schema": s,
                            "table_name": t,
                            "column_name": col["name"],
                            "ordinal_position": i,
                            "constraint_name": f"{t}_pkey",
                        })
        return rows

    if namespace == "pg_catalog" and view == "pg_database":
        rows = [{"datname": d, "datistemplate": False}
                for d in sorted(state["databases"].keys())]
        wm = _extract("datname")
        if wm:
            rows = [r for r in rows if r["datname"] == wm]
        return rows

    if namespace == "pg_catalog" and view == "pg_namespace":
        rows = [{"nspname": s, "nspowner": 10}
                for s in sorted(schemas.keys())]
        return rows

    if namespace == "pg_catalog" and view == "pg_tables":
        s_filter = _extract("schemaname")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for t in sorted((body.get("tables") or {}).keys()):
                rows.append({
                    "schemaname": s, "tablename": t,
                    "tableowner": "postgres", "hasindexes": True,
                })
        return rows

    if namespace == "pg_catalog" and view == "pg_class":
        rows = []
        for s, body in schemas.items():
            for t in (body.get("tables") or {}).keys():
                rows.append({
                    "relname": t, "relkind": "r",
                    "relnamespace": s,
                })
            for v in (body.get("views") or {}).keys():
                rows.append({
                    "relname": v, "relkind": "v",
                    "relnamespace": s,
                })
        return rows

    if namespace == "pg_catalog" and view == "pg_indexes":
        s_filter = _extract("schemaname")
        t_filter = _extract("tablename")
        rows = []
        for s, body in schemas.items():
            if s_filter and s != s_filter:
                continue
            for t, tbl in (body.get("tables") or {}).items():
                if t_filter and t != t_filter:
                    continue
                for ix in tbl.get("indexes") or []:
                    rows.append({
                        "schemaname": s,
                        "tablename": t,
                        "indexname": ix.get("name", ""),
                        "indexdef": _synth_indexdef(s, t, ix),
                    })
        return rows

    return None


def _char_max_length(pg_type: str) -> int | None:
    m = re.search(r"(?:varchar|char|character\s+varying|character|bpchar)"
                  r"\s*\(\s*(\d+)\s*\)", pg_type or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _numeric_precision(pg_type: str) -> int | None:
    m = re.search(r"(?:numeric|decimal)\s*\(\s*(\d+)",
                  pg_type or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _numeric_scale(pg_type: str) -> int | None:
    m = re.search(r"(?:numeric|decimal)\s*\(\s*\d+\s*,\s*(\d+)",
                  pg_type or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _synth_indexdef(schema: str, table: str, ix: dict) -> str:
    cols = ", ".join(ix.get("columns") or [])
    unique = "UNIQUE " if ix.get("is_unique") else ""
    return (f"CREATE {unique}INDEX {ix.get('name', '')} ON "
            f"{schema}.{table} ({cols})")


# ---------------------------------------------------------------------------
# CREATE / DROP catalog tracking
# ---------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))\.)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\(",
    re.IGNORECASE | re.DOTALL,
)


_DROP_TABLE_RE = re.compile(
    r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r"(?:(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))\.)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)


_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))?\s+ON\s+"
    r"(?:(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))\.)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\(([^)]+)\)",
    re.IGNORECASE,
)


_CREATE_VIEW_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+"
    r"(?:(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))\.)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s+AS\s+(.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_column_def(col_text: str) -> dict | None:
    """Parse one column definition from a CREATE TABLE column list.
    Returns {name, type, nullable, default, primary_key, foreign_key}
    or None if the fragment is a table-level constraint."""
    text = col_text.strip()
    if not text:
        return None
    upper = text.upper()
    # Skip table-level constraints — picked up by caller in a separate pass.
    if upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE",
                         "CHECK", "CONSTRAINT", "EXCLUDE")):
        return None
    # name [type] [constraints...]
    m = re.match(r'^(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s+([^,]+)$',
                 text)
    if not m:
        return None
    name = m.group(1) or m.group(2)
    rest = m.group(3).strip()
    # Type can be `int`, `varchar(255)`, `numeric(10,2)`, `text[]`,
    # `double precision`, `timestamp with time zone`, etc. Grab the
    # type token greedy-up-to a known constraint keyword.
    type_match = re.match(
        r"((?:[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:precision|varying|with\s+time\s+zone|without\s+time\s+zone))?)"
        r"(?:\s*\([^)]*\))?(?:\s*\[\s*\])?)",
        rest, re.IGNORECASE)
    type_str = type_match.group(1).strip() if type_match else "text"
    constraints = rest[len(type_str):].strip() if type_match else rest
    upper_c = constraints.upper()
    nullable = "NOT NULL" not in upper_c
    primary_key = "PRIMARY KEY" in upper_c
    default = None
    dm = re.search(r"DEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|NULL|PRIMARY|"
                   r"UNIQUE|REFERENCES|CHECK|$))",
                   constraints, re.IGNORECASE)
    if dm:
        default = dm.group(1).strip().rstrip(",").strip()
    fk = None
    fm = re.search(
        r"REFERENCES\s+(?:([A-Za-z_][A-Za-z0-9_]*)\.)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))?",
        constraints, re.IGNORECASE)
    if fm:
        fk = {
            "schema": (fm.group(1) or "public").lower(),
            "table": fm.group(2).lower(),
            "column": (fm.group(3) or "id").lower(),
        }
    return {
        "name": name,
        "type": type_str,
        "nullable": nullable,
        "default": default,
        "primary_key": primary_key,
        "foreign_key": fk,
    }


def _split_column_list(body: str) -> list[str]:
    """Split a CREATE TABLE column list on commas, respecting parens."""
    parts = []
    depth = 0
    cur = ""
    for ch in body:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_create_table(query: str) -> tuple[str, str, list[dict],
                                              list[dict]] | None:
    """Extract (schema, table, columns, table_constraints) from a
    CREATE TABLE statement. table_constraints is a list of {kind, body}
    for PRIMARY KEY / FOREIGN KEY / UNIQUE at table scope."""
    m = _CREATE_TABLE_RE.match(query)
    if not m:
        return None
    schema = (m.group(1) or m.group(2) or "public").lower()
    table = (m.group(3) or m.group(4) or "").lower()
    # Extract paren-balanced body following the (
    start = m.end() - 1
    depth = 0
    end = start
    for i in range(start, len(query)):
        if query[i] == "(":
            depth += 1
        elif query[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = query[start + 1:end]
    cols: list[dict] = []
    constraints: list[dict] = []
    for part in _split_column_list(body):
        ps = part.strip()
        if not ps:
            continue
        upper = ps.upper()
        if upper.startswith("PRIMARY KEY"):
            cols_pk = re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                 ps[len("PRIMARY KEY"):])
            constraints.append({"kind": "primary_key", "columns": cols_pk})
            continue
        if upper.startswith("FOREIGN KEY"):
            fk_cols = re.findall(
                r"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+"
                r"(?:([A-Za-z_][A-Za-z0-9_]*)\.)?"
                r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
                ps, re.IGNORECASE)
            if fk_cols:
                src_cols, ref_schema, ref_table, ref_cols = fk_cols[0]
                constraints.append({
                    "kind": "foreign_key",
                    "columns": [c.strip() for c in src_cols.split(",")],
                    "ref_schema": (ref_schema or "public").lower(),
                    "ref_table": ref_table.lower(),
                    "ref_columns": [c.strip() for c in ref_cols.split(",")],
                })
            continue
        if upper.startswith(("UNIQUE", "CHECK", "CONSTRAINT", "EXCLUDE")):
            constraints.append({"kind": "other", "body": ps})
            continue
        parsed = _parse_column_def(ps)
        if parsed:
            cols.append(parsed)
    return schema, table, cols, constraints


def _register_table(state: dict, db: str, schema: str, table: str,
                    columns: list[dict],
                    table_constraints: list[dict] | None = None) -> None:
    """Insert (or replace) a table entry in the catalog. Annotates each
    column with its OID and merges table-level PRIMARY KEY / FOREIGN KEY
    constraints into the column rows."""
    db_body = state["databases"].setdefault(
        db, {"schemas": {"public": {"tables": {}, "views": {},
                                     "functions": {}}}})
    schemas = db_body.setdefault("schemas", {})
    sch = schemas.setdefault(schema, {"tables": {}, "views": {},
                                       "functions": {}})
    sch.setdefault("tables", {})
    cols_out = []
    for col in columns:
        c = dict(col)
        c["oid"] = _pg_type_oid(c.get("type", "text"))
        cols_out.append(c)
    for con in (table_constraints or []):
        if con["kind"] == "primary_key":
            pk_cols = set(con.get("columns") or [])
            for c in cols_out:
                if c["name"] in pk_cols:
                    c["primary_key"] = True
        if con["kind"] == "foreign_key":
            src_cols = con.get("columns") or []
            ref_cols = con.get("ref_columns") or []
            for i, src in enumerate(src_cols):
                for c in cols_out:
                    if c["name"] == src:
                        c["foreign_key"] = {
                            "schema": con.get("ref_schema", "public"),
                            "table": con.get("ref_table", ""),
                            "column": (ref_cols[i] if i < len(ref_cols)
                                       else "id"),
                        }
    sch["tables"][table] = {
        "columns": cols_out,
        "indexes": sch["tables"].get(table, {}).get("indexes", []),
        "constraints": list(table_constraints or []),
    }
    # Auto-register primary-key index.
    pk_cols = [c["name"] for c in cols_out if c.get("primary_key")]
    if pk_cols:
        existing = sch["tables"][table].setdefault("indexes", [])
        if not any(ix.get("is_primary") for ix in existing):
            existing.append({
                "name": f"{table}_pkey",
                "columns": pk_cols,
                "is_unique": True,
                "is_primary": True,
            })


def _unregister_table(state: dict, db: str, schema: str,
                      table: str) -> None:
    body = (state["databases"].get(db, {})
            .get("schemas", {}).get(schema) or {})
    (body.get("tables") or {}).pop(table, None)


def _register_view(state: dict, db: str, schema: str, view: str,
                   definition: str) -> None:
    db_body = state["databases"].setdefault(
        db, {"schemas": {"public": {"tables": {}, "views": {},
                                     "functions": {}}}})
    schemas = db_body.setdefault("schemas", {})
    sch = schemas.setdefault(schema, {"tables": {}, "views": {},
                                       "functions": {}})
    sch.setdefault("views", {})[view] = {"definition": definition.strip()}


def _register_index(state: dict, db: str, schema: str, table: str,
                    index_name: str, columns: list[str],
                    is_unique: bool) -> None:
    body = (state["databases"].get(db, {})
            .get("schemas", {}).get(schema) or {})
    tbl = (body.get("tables") or {}).get(table)
    if not tbl:
        return
    tbl.setdefault("indexes", [])
    if any(ix.get("name") == index_name for ix in tbl["indexes"]):
        return
    tbl["indexes"].append({
        "name": index_name,
        "columns": columns,
        "is_unique": bool(is_unique),
        "is_primary": False,
    })


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def _column_meta_from_cursor(cur: sqlite3.Cursor,
                              state: dict, db: str,
                              query_text: str) -> list[dict]:
    """Build the Postgres-style column metadata block for a SELECT
    result. We look up the column type in the catalog if we can match
    the column name to a single table referenced in the query; otherwise
    we fall back to text/0."""
    cols_meta = []
    table_hits = _tables_referenced(query_text, state, db)
    for desc in cur.description or []:
        name = desc[0]
        pg_type = "text"
        oid = PG_TYPE_OIDS["text"]
        for (sch, tbl) in table_hits:
            tbl_body = (state["databases"].get(db, {})
                        .get("schemas", {}).get(sch, {})
                        .get("tables", {}).get(tbl))
            if not tbl_body:
                continue
            for c in tbl_body.get("columns") or []:
                if c["name"] == name:
                    pg_type = c.get("type", "text")
                    oid = c.get("oid", _pg_type_oid(pg_type))
                    break
        cols_meta.append({
            "name": name,
            "dataType": _normalize_pg_type(pg_type),
            "dataTypeID": oid,
        })
    return cols_meta


def _tables_referenced(query: str, state: dict, db: str
                        ) -> list[tuple[str, str]]:
    """Best-effort: extract (schema, table) pairs from a query. Used to
    annotate column types in result metadata."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in re.finditer(
            r"(?:FROM|JOIN|UPDATE|INTO)\s+"
            r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
            r"(?:\.(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*)))?",
            query, re.IGNORECASE):
        first = (m.group(1) or m.group(2) or "").lower()
        second = (m.group(3) or m.group(4) or "")
        if second:
            sch, tbl = first, second.lower()
        else:
            res = _resolve_unqualified(state, db, first)
            if not res:
                # Sometimes the table is referenced via the flat name
                # `"schema__table"` after rewrite; split it.
                if "__" in first:
                    sch, tbl = first.split("__", 1)
                else:
                    continue
            else:
                sch, tbl = res
        if (sch, tbl) not in seen:
            seen.add((sch, tbl))
            out.append((sch, tbl))
    return out


def _command_from_query(query: str) -> str:
    """Return the Postgres `command` tag for a query
    (SELECT/INSERT/UPDATE/DELETE/CREATE TABLE/...)."""
    s = query.strip().lstrip("(").lstrip().upper()
    m = re.match(r"([A-Z]+)(?:\s+([A-Z]+))?", s)
    if not m:
        return ""
    head = m.group(1)
    if head in ("CREATE", "DROP", "ALTER"):
        return f"{head} {m.group(2)}" if m.group(2) else head
    return head


def _ensure_flat_tables(state: dict, db: str) -> None:
    """For every catalog table not yet present in sqlite, materialize
    an empty sqlite table with the right column shape. Lets
    information_schema-driven workflows see the agent's INSERTs."""
    schemas = (state["databases"].get(db, {}).get("schemas") or {})
    conn = sqlite3.connect(_db_path())
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for sch, body in schemas.items():
            for t, tbl in (body.get("tables") or {}).items():
                flat = _flat_name(sch, t)
                if flat in existing:
                    continue
                cols = tbl.get("columns") or []
                if not cols:
                    continue
                ddl_parts = []
                for c in cols:
                    sql_type = _pg_to_sqlite_type(c.get("type", "text"))
                    pk = " PRIMARY KEY" if c.get("primary_key") else ""
                    notnull = (" NOT NULL" if not c.get("nullable", True)
                               else "")
                    serial = (" AUTOINCREMENT"
                              if _is_serial(c.get("type", ""))
                              and c.get("primary_key") else "")
                    ddl_parts.append(
                        f'"{c["name"]}" {sql_type}{pk}{serial}{notnull}')
                conn.execute(
                    f'CREATE TABLE IF NOT EXISTS "{flat}" '
                    f'({", ".join(ddl_parts)})')
        conn.commit()
    finally:
        conn.close()


def _execute(query: str, params: list[Any] | None,
             state: dict, db: str) -> dict:
    """Execute one statement. Returns
    `{rows, rowCount, columns, command}` (matching the
    @modelcontextprotocol/server-postgres `query` shape)."""
    stripped = query.strip().rstrip(";").strip()
    command = _command_from_query(stripped)

    # Catalog short-circuit (information_schema / pg_catalog).
    if (command in ("SELECT", "WITH")
            and _looks_like_catalog_query(stripped)):
        rows = _serve_catalog_query(stripped, state, db)
        if rows is not None:
            columns = ([{"name": k,
                          "dataType": _infer_pg_type(rows[0][k]),
                          "dataTypeID": _pg_type_oid(
                              _infer_pg_type(rows[0][k]))}
                         for k in rows[0].keys()] if rows else [])
            return {"rows": rows, "rowCount": len(rows),
                    "columns": columns, "command": command}

    # CREATE TABLE — track in the catalog. The sqlite table is built
    # from catalog metadata via _ensure_flat_tables; we DO NOT pass the
    # original CREATE TABLE through to sqlite (Postgres types like
    # `jsonb`, `numeric(10,2)`, `timestamptz` may not parse identically,
    # and the catalog-driven DDL uses sqlite affinities).
    if re.match(r"^\s*CREATE\s+TABLE\b", stripped, re.IGNORECASE):
        parsed = _parse_create_table(stripped)
        if parsed:
            schema, table, cols, constraints = parsed
            _register_table(state, db, schema, table, cols, constraints)
            _ensure_flat_tables(state, db)
            return {"rows": [], "rowCount": 0, "columns": [],
                    "command": "CREATE TABLE"}

    # CREATE INDEX — track in the catalog. Skipped at sqlite level
    # (the mock doesn't use indexes for query planning beyond reporting
    # them in list_indexes / pg_indexes).
    cim = _CREATE_INDEX_RE.match(stripped)
    if cim:
        is_unique = bool(cim.group(1))
        index_name = (cim.group(2) or cim.group(3) or "").lower()
        sch = (cim.group(4) or cim.group(5) or "public").lower()
        tbl = (cim.group(6) or cim.group(7) or "").lower()
        col_list = [c.strip().strip('"').lower()
                    for c in cim.group(8).split(",")]
        _register_index(state, db, sch, tbl, index_name,
                        col_list, is_unique)
        return {"rows": [], "rowCount": 0, "columns": [],
                "command": "CREATE INDEX"}

    # CREATE VIEW — track in the catalog. Not materialized as a sqlite
    # view (the view definition may contain Postgres-only syntax); the
    # catalog entry is enough for list_views / information_schema.views.
    cvm = _CREATE_VIEW_RE.match(stripped)
    if cvm:
        sch = (cvm.group(1) or cvm.group(2) or "public").lower()
        view = (cvm.group(3) or cvm.group(4) or "").lower()
        definition = cvm.group(5)
        _register_view(state, db, sch, view, definition)
        return {"rows": [], "rowCount": 0, "columns": [],
                "command": "CREATE VIEW"}

    # DROP TABLE — remove from catalog AND from sqlite.
    dtm = _DROP_TABLE_RE.match(stripped)
    if dtm:
        sch = (dtm.group(1) or dtm.group(2) or "public").lower()
        tbl = (dtm.group(3) or dtm.group(4) or "").lower()
        _unregister_table(state, db, sch, tbl)
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                f'DROP TABLE IF EXISTS "{_flat_name(sch, tbl)}"')
            conn.commit()
        finally:
            conn.close()
        return {"rows": [], "rowCount": 0, "columns": [],
                "command": "DROP TABLE"}

    # Rewrite Postgres-isms to sqlite-friendly form.
    rewritten = _rewrite_sql(stripped, state, current_db=db)
    # Convert $N to ? after rewriting (the rewriter is whitespace-agnostic
    # so $-positions are preserved).
    rewritten, pyparams = _convert_dollar_params(rewritten, params)

    # Unqualified table reference: rewrite to "schema__table" via
    # search_path.
    rewritten = _resolve_unqualified_tables(rewritten, state, db)

    # Ensure every catalog table has a corresponding sqlite table before
    # we execute (so newly-CREATEd tables work).
    _ensure_flat_tables(state, db)

    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        # Split RETURNING off — sqlite (3.35+) supports it, but mock
        # has to behave consistently across older sqlites: if the
        # statement is INSERT/UPDATE/DELETE with RETURNING, emulate by
        # running the write then re-querying. For simplicity we let
        # sqlite handle it natively; if that errors, we fall back.
        try:
            cur = conn.execute(rewritten, pyparams)
        except sqlite3.Error as e:
            if "RETURNING" in rewritten.upper():
                # Strip RETURNING and re-query.
                head, _, tail = rewritten.upper().partition(" RETURNING ")
                rewritten_no_ret = rewritten[:len(head)]
                ret_cols = tail.split()
                cur = conn.execute(rewritten_no_ret, pyparams)
                conn.commit()
                # Best-effort: pull the affected rows. Without a stable
                # row identifier we can't reliably reconstruct, so
                # return an empty result.
                return {"rows": [], "rowCount": cur.rowcount,
                        "columns": [], "command": command}
            raise ValueError(
                f"SQL error: {e} | rewritten: {rewritten!r}")
        if command in ("SELECT", "WITH", "PRAGMA", "EXPLAIN"):
            fetched = cur.fetchall()
            rows = [dict(r) for r in fetched]
            columns = _column_meta_from_cursor(cur, state, db, stripped)
            return {"rows": rows, "rowCount": len(rows),
                    "columns": columns, "command": command}
        # Try fetching even for INSERT/UPDATE/DELETE in case it had
        # RETURNING.
        rows: list[dict] = []
        columns: list[dict] = []
        try:
            fetched = cur.fetchall()
            if fetched:
                rows = [dict(r) for r in fetched]
                columns = _column_meta_from_cursor(
                    cur, state, db, stripped)
        except sqlite3.Error:
            pass
        conn.commit()
        rc = cur.rowcount if cur.rowcount is not None else len(rows)
        return {"rows": rows, "rowCount": rc,
                "columns": columns, "command": command}
    finally:
        conn.close()


def _resolve_unqualified_tables(query: str, state: dict, db: str) -> str:
    """For unqualified table refs (not already wrapped in flat
    `"schema__table"` quotes), try resolving via search_path and
    rewrite. Heuristic: tokens after FROM/JOIN/UPDATE/INTO that are bare
    identifiers (not quoted, no dot) get a search_path lookup."""
    def _sub(m: re.Match) -> str:
        kw = m.group(1)
        ident = m.group(2)
        # Skip if already quoted (flat name) or qualified.
        if ident.startswith('"') or "." in ident:
            return m.group(0)
        # Skip catalog views (handled separately).
        if ident.lower() in ("information_schema", "pg_catalog"):
            return m.group(0)
        # Skip subquery placeholder.
        if ident == "(":
            return m.group(0)
        res = _resolve_unqualified(state, db, ident.lower())
        if not res:
            return m.group(0)
        sch, tbl = res
        return f'{kw} "{_flat_name(sch, tbl)}"'

    return re.sub(
        r"\b(FROM|JOIN|UPDATE|INTO)\s+([A-Za-z_][A-Za-z0-9_]*)",
        _sub, query, flags=re.IGNORECASE)


def _infer_pg_type(v: Any) -> str:
    if v is None:
        return "text"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int4"
    if isinstance(v, float):
        return "float8"
    if isinstance(v, (list, dict)):
        return "jsonb"
    return "text"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP("postgres-mock")


def _current_db(state: dict) -> str:
    return state.get("current_database") or "postgres"


@mcp.tool(name="query")
def query(sql: str, params: list[Any] | None = None,
          database: str | None = None) -> dict:
    """Execute a read-only SQL query (SELECT / WITH). Mirrors the
    `query` tool from @modelcontextprotocol/server-postgres. Writes
    (INSERT/UPDATE/DELETE/DDL) are rejected — use `execute` for those.

    Parameterized queries support both `$1, $2, ...` (Postgres) and
    `?` (sqlite). Returns
    `{rows, rowCount, columns: [{name, dataType, dataTypeID}], command}`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        cmd = _command_from_query(sql)
        if cmd not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
            _record(s, "query", result="write_rejected", command=cmd)
            _save_state(s)
            raise ValueError(
                f"query() only accepts read-only statements; got "
                f"{cmd!r}. Use execute() for writes.")
        try:
            out = _execute(sql, params, s, db)
            _record(s, "query", database=db, command=cmd,
                    rowCount=out.get("rowCount", 0))
        except Exception as e:
            _record(s, "query", database=db, error=str(e))
            _save_state(s)
            raise
        _save_state(s)
        return out


@mcp.tool(name="execute")
def execute(sql: str, params: list[Any] | None = None,
            database: str | None = None) -> dict:
    """Execute a write SQL statement (INSERT / UPDATE / DELETE / CREATE
    TABLE / CREATE INDEX / CREATE VIEW / DROP / ALTER / COPY-style).
    Parameterized queries support both `$1, $2, ...` and `?` styles.

    Returns `{rowCount, command, rows, columns}` — `rows`/`columns` are
    populated when the statement uses RETURNING.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        try:
            out = _execute(sql, params, s, db)
            _record(s, "execute", database=db,
                    command=out.get("command", ""),
                    rowCount=out.get("rowCount", 0))
        except Exception as e:
            _record(s, "execute", database=db, error=str(e))
            _save_state(s)
            raise
        _save_state(s)
        return out


@mcp.tool(name="list_databases")
def list_databases() -> dict:
    """List every database visible to the connection. Postgres
    equivalent: `SELECT datname FROM pg_database WHERE NOT
    datistemplate;`.
    """
    with _lock():
        s = _load_state()
        names = sorted(s["databases"].keys())
        _record(s, "list_databases", count=len(names))
        _save_state(s)
        return {"databases": names}


@mcp.tool(name="list_schemas")
def list_schemas(database: str | None = None) -> dict:
    """List schemas in `<database>`. Postgres equivalent: `SELECT
    schema_name FROM information_schema.schemata;`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        if db not in s["databases"]:
            _record(s, "list_schemas", database=db, result="not_found")
            _save_state(s)
            raise ValueError(f"database {db!r} not found")
        names = sorted((s["databases"][db].get("schemas") or {}).keys())
        _record(s, "list_schemas", database=db, count=len(names))
        _save_state(s)
        return {"database": db, "schemas": names}


@mcp.tool(name="list_tables")
def list_tables(database: str | None = None,
                schema: str = "public") -> dict:
    """List base tables in `<database>.<schema>`. Postgres equivalent:
    `SELECT table_name FROM information_schema.tables WHERE
    table_schema=$1 AND table_type='BASE TABLE';`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        body = (s["databases"].get(db, {})
                .get("schemas", {}).get(schema))
        if body is None:
            _record(s, "list_tables", database=db, schema=schema,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{schema} not found")
        names = sorted((body.get("tables") or {}).keys())
        _record(s, "list_tables", database=db, schema=schema,
                count=len(names))
        _save_state(s)
        return {"database": db, "schema": schema, "tables": names}


@mcp.tool(name="describe_table")
def describe_table(table: str,
                   database: str | None = None,
                   schema: str = "public") -> dict:
    """Describe a table — returns the columns (in
    `information_schema.columns` shape), the primary key columns, and
    the foreign keys.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        tbl = (s["databases"].get(db, {}).get("schemas", {})
               .get(schema, {}).get("tables", {}).get(table))
        if not tbl:
            _record(s, "describe_table", database=db, schema=schema,
                    table=table, result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{schema}.{table} not found")
        cols = []
        pk_cols = []
        foreign_keys = []
        for i, c in enumerate(tbl.get("columns") or [], 1):
            pg_type = c.get("type", "text")
            norm = _normalize_pg_type(pg_type)
            cols.append({
                "column_name": c["name"],
                "ordinal_position": i,
                "data_type": norm,
                "udt_name": norm.replace("[]", ""),
                "is_nullable": ("YES" if c.get("nullable", True) else "NO"),
                "column_default": c.get("default"),
                "character_maximum_length": _char_max_length(pg_type),
                "numeric_precision": _numeric_precision(pg_type),
                "numeric_scale": _numeric_scale(pg_type),
                "dataTypeID": c.get("oid", _pg_type_oid(pg_type)),
            })
            if c.get("primary_key"):
                pk_cols.append(c["name"])
            if c.get("foreign_key"):
                foreign_keys.append({
                    "column": c["name"],
                    "references": c["foreign_key"],
                })
        _record(s, "describe_table", database=db, schema=schema,
                table=table, columns=len(cols))
        _save_state(s)
        return {
            "database": db, "schema": schema, "table": table,
            "columns": cols,
            "primary_key": pk_cols,
            "foreign_keys": foreign_keys,
        }


@mcp.tool(name="list_indexes")
def list_indexes(table: str,
                 database: str | None = None,
                 schema: str = "public") -> dict:
    """List indexes on a table. Postgres equivalent: query
    `pg_indexes` / `pg_class` + `pg_index`. Returns
    `[{index_name, column_names, is_unique, is_primary}]`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        tbl = (s["databases"].get(db, {}).get("schemas", {})
               .get(schema, {}).get("tables", {}).get(table))
        if not tbl:
            _record(s, "list_indexes", database=db, schema=schema,
                    table=table, result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{schema}.{table} not found")
        out = []
        for ix in tbl.get("indexes") or []:
            out.append({
                "index_name": ix.get("name", ""),
                "column_names": list(ix.get("columns") or []),
                "is_unique": bool(ix.get("is_unique")),
                "is_primary": bool(ix.get("is_primary")),
            })
        _record(s, "list_indexes", database=db, schema=schema,
                table=table, count=len(out))
        _save_state(s)
        return {"database": db, "schema": schema, "table": table,
                "indexes": out}


@mcp.tool(name="list_views")
def list_views(database: str | None = None,
               schema: str = "public") -> dict:
    """List views in `<database>.<schema>`. Returns
    `[{view_name, definition}]`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        body = (s["databases"].get(db, {})
                .get("schemas", {}).get(schema))
        if body is None:
            _record(s, "list_views", database=db, schema=schema,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{schema} not found")
        out = []
        for name, vbody in (body.get("views") or {}).items():
            out.append({
                "view_name": name,
                "definition": vbody.get("definition", ""),
            })
        _record(s, "list_views", database=db, schema=schema,
                count=len(out))
        _save_state(s)
        return {"database": db, "schema": schema, "views": out}


@mcp.tool(name="explain_query")
def explain_query(sql: str, format: str = "text") -> dict:
    """Return the EXPLAIN output for a query. Postgres equivalent:
    `EXPLAIN [(FORMAT JSON)] <sql>;`. The mock returns a synthetic
    plan (sqlite's EXPLAIN output reformatted for readability) since
    sqlite's query planner internals don't map cleanly to Postgres
    plan nodes.
    """
    with _lock():
        s = _load_state()
        db = _current_db(s)
        # Try sqlite's EXPLAIN QUERY PLAN — gives us a small per-statement
        # tree we can pretty-print.
        rewritten = _rewrite_sql(sql.strip().rstrip(";"), s,
                                  current_db=db)
        rewritten = _resolve_unqualified_tables(rewritten, s, db)
        _ensure_flat_tables(s, db)
        conn = sqlite3.connect(_db_path())
        try:
            plan_rows = conn.execute(
                f"EXPLAIN QUERY PLAN {rewritten}").fetchall()
        except sqlite3.Error as e:
            plan_rows = [(0, 0, 0, f"plan unavailable: {e}")]
        finally:
            conn.close()
        if format.lower() == "json":
            plan = {
                "Plan": {
                    "Node Type": "Mock Plan",
                    "Total Cost": float(len(plan_rows) * 1.0),
                    "Plans": [
                        {"Node Type": "Step",
                         "Step Id": r[0], "Detail": r[3]}
                        for r in plan_rows
                    ],
                }
            }
            _record(s, "explain_query", format="json")
            _save_state(s)
            return {"format": "json", "plan": plan}
        text_lines = [f"  -> {r[3]}" for r in plan_rows] or [
            "  -> Seq Scan (mock)"]
        _record(s, "explain_query", format="text")
        _save_state(s)
        return {"format": "text",
                "plan": "QUERY PLAN\n" + "\n".join(text_lines)}


@mcp.tool(name="list_functions")
def list_functions(database: str | None = None,
                   schema: str = "public") -> dict:
    """List user-defined functions in `<database>.<schema>`. Postgres
    equivalent: query `pg_proc` joined to `pg_namespace`. Returns
    `[{name, signature}]`.
    """
    with _lock():
        s = _load_state()
        db = database or _current_db(s)
        body = (s["databases"].get(db, {})
                .get("schemas", {}).get(schema))
        if body is None:
            _record(s, "list_functions", database=db, schema=schema,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{schema} not found")
        out = []
        for name, fbody in (body.get("functions") or {}).items():
            out.append({
                "name": name,
                "signature": fbody.get("signature", ""),
            })
        _record(s, "list_functions", database=db, schema=schema,
                count=len(out))
        _save_state(s)
        return {"database": db, "schema": schema, "functions": out}


@mcp.tool(name="current_settings")
def current_settings() -> dict:
    """Return the session-level GUC settings the mock simulates. Real
    Postgres exposes ~300 GUCs via `pg_settings`; the mock surfaces the
    handful agents typically reach for: server_version,
    server_version_num, encoding, current_database, current_user,
    search_path, timezone.
    """
    with _lock():
        s = _load_state()
        _record(s, "current_settings")
        _save_state(s)
        return {
            "server_version": "15.4 (mock)",
            "server_version_num": "150004",
            "encoding": "UTF8",
            "current_database": _current_db(s),
            "current_user": "postgres",
            "search_path": ", ".join(s.get("search_path",
                                            ["$user", "public"])),
            "timezone": "UTC",
        }


# ---------------------------------------------------------------------------
# Mock-only debug tools
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted catalog (databases / schemas /
    tables / views / functions) and the call log. Used by verifiers."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_database")
def mock_debug_seed_database(name: str) -> dict:
    """Mock-only: create a database (with an empty `public` schema)."""
    with _lock():
        s = _load_state()
        s["databases"].setdefault(name, {
            "schemas": {
                "public": {"tables": {}, "views": {}, "functions": {}},
            },
        })
        _record(s, "debug_seed_database", database=name)
        _save_state(s)
        return {"database": name,
                "schemas": list(s["databases"][name]["schemas"].keys())}


@mcp.tool(name="mock_debug_seed_table")
def mock_debug_seed_table(database: str, schema: str, table: str,
                          columns: list[dict],
                          primary_key: list[str] | None = None,
                          foreign_keys: list[dict] | None = None) -> dict:
    """Mock-only: register a table in the catalog AND create the matching
    sqlite table.

    `columns` is a list of
    `{name, type, nullable=True, default=None, primary_key=False}`.
    `foreign_keys` is `[{columns, ref_schema, ref_table, ref_columns}]`.
    """
    with _lock():
        s = _load_state()
        s["databases"].setdefault(database, {
            "schemas": {"public": {"tables": {}, "views": {},
                                    "functions": {}}}})
        s["databases"][database].setdefault("schemas", {}).setdefault(
            schema, {"tables": {}, "views": {}, "functions": {}})

        normalized: list[dict] = []
        for c in columns or []:
            normalized.append({
                "name": c["name"],
                "type": c.get("type", "text"),
                "nullable": bool(c.get("nullable", True)),
                "default": c.get("default"),
                "primary_key": bool(c.get("primary_key", False)),
                "foreign_key": c.get("foreign_key"),
            })
        constraints: list[dict] = []
        if primary_key:
            constraints.append({"kind": "primary_key",
                                "columns": list(primary_key)})
        for fk in foreign_keys or []:
            constraints.append({
                "kind": "foreign_key",
                "columns": list(fk.get("columns") or []),
                "ref_schema": fk.get("ref_schema", "public"),
                "ref_table": fk.get("ref_table", ""),
                "ref_columns": list(fk.get("ref_columns") or ["id"]),
            })
        _register_table(s, database, schema, table, normalized, constraints)

        # Build the sqlite table.
        flat = _flat_name(schema, table)
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{flat}"')
            ddl_parts = []
            for c in normalized:
                sql_type = _pg_to_sqlite_type(c.get("type", "text"))
                pk = " PRIMARY KEY" if c.get("primary_key") else ""
                serial = (" AUTOINCREMENT"
                          if _is_serial(c.get("type", ""))
                          and c.get("primary_key") else "")
                notnull = (" NOT NULL" if not c.get("nullable", True)
                           else "")
                ddl_parts.append(
                    f'"{c["name"]}" {sql_type}{pk}{serial}{notnull}')
            if primary_key and not any(c.get("primary_key")
                                        for c in normalized):
                ddl_parts.append(
                    f'PRIMARY KEY ({", ".join(primary_key)})')
            conn.execute(
                f'CREATE TABLE "{flat}" ({", ".join(ddl_parts)})')
            conn.commit()
        finally:
            conn.close()

        _record(s, "debug_seed_table", database=database, schema=schema,
                table=table, columns=len(normalized))
        _save_state(s)
        return {"database": database, "schema": schema, "table": table,
                "columns": len(normalized)}


@mcp.tool(name="mock_debug_seed_rows")
def mock_debug_seed_rows(database: str, schema: str, table: str,
                         rows: list[dict]) -> dict:
    """Mock-only: bulk-insert rows into a previously-seeded table.
    Bypasses all rewriting and write checks."""
    with _lock():
        s = _load_state()
        flat = _flat_name(schema, table)
        conn = sqlite3.connect(_db_path())
        n = 0
        try:
            for row in rows or []:
                if not isinstance(row, dict) or not row:
                    continue
                keys = list(row.keys())
                placeholders = ", ".join("?" for _ in keys)
                col_list = ", ".join(f'"{k}"' for k in keys)
                # Pre-serialize lists/dicts (jsonb-style storage).
                values = []
                for k in keys:
                    v = row[k]
                    if isinstance(v, (list, dict)):
                        v = json.dumps(v)
                    values.append(v)
                conn.execute(
                    f'INSERT INTO "{flat}" ({col_list}) '
                    f'VALUES ({placeholders})', values)
                n += 1
            conn.commit()
        finally:
            conn.close()
        _record(s, "debug_seed_rows", database=database, schema=schema,
                table=table, inserted=n)
        _save_state(s)
        return {"inserted": n}


@mcp.tool(name="mock_debug_reset")
def mock_debug_reset() -> dict:
    """Mock-only: wipe the catalog AND the sqlite db. Used between
    tasks/tests."""
    with _lock():
        s = _empty_state()
        _save_state(s)
        db_p = _db_path()
        if os.path.exists(db_p):
            os.remove(db_p)
        return {"reset": True}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    # Ensure state.json + state.db exist before the server registers.
    with _lock():
        _load_state()
    mcp.run()


if __name__ == "__main__":
    main()
