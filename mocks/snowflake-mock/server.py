"""Snowflake mock MCP server.

Mirrors the tool surface of `mcp_snowflake_server` (Toolathlon source:
github.com/lockon-n/mcp-snowflake-server, upstream:
github.com/isaacwasserman/mcp-snowflake-server). The official server
wraps the snowflake-snowpark Session and exposes tools that
list/describe/read/write Snowflake objects via SQL.

Backed by a real SQLite database at
`$SNOWFLAKE_MOCK_STATE_DIR/db.sqlite3` so that SELECT/INSERT/UPDATE/
DELETE/CREATE/DROP issued by the agent stay coherent across calls.

Snowflake's three-part `db.schema.table` namespace is collapsed onto
sqlite by rewriting fully qualified identifiers to a single flat
table name `"<DB>__<SCHEMA>__<TABLE>"`. Snowflake-only syntax is
rewritten on the fly:
    CURRENT_TIMESTAMP()  ->  CURRENT_TIMESTAMP
    CURRENT_DATE()       ->  CURRENT_DATE
    DATEADD(unit, n, t)  ->  datetime(t, '<n> <unit>')
    DATEDIFF(unit, a, b) ->  (julianday(b) - julianday(a)) [days]
    SELECT EXISTS(...)   ->  SELECT EXISTS(...) [already valid]
INFORMATION_SCHEMA.{DATABASES,SCHEMATA,TABLES,COLUMNS} queries are
served from the state.json catalog rather than from the sqlite file
because sqlite has no equivalent. Identifier case is normalized to
uppercase to mimic Snowflake.

A JSON state file at `$SNOWFLAKE_MOCK_STATE_DIR/state.json` records
the catalog (databases / schemas / table list), the insights memo,
the `allowed_databases` restriction, the `allow_write` flag, and a
`calls` log used by the verifier.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import sqlite3
import sys
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State / locking
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    d = os.environ.get(
        "SNOWFLAKE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/snowflake_mock"),
    )
    os.makedirs(d, exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(_state_dir(), "state.json")


def _db_path() -> str:
    return os.path.join(_state_dir(), "db.sqlite3")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        # catalog: {DBNAME: {"schemas": {SCHEMA: {"tables": [...]}}}}
        "databases": {},
        "current": {"database": None, "schema": None},
        # None means no restriction; list means restricted to those names
        "allowed_databases": None,
        "allow_write": True,
        "exclude_json_results": False,
        "insights": [],
        "calls": [],
    }


def _seed_state() -> dict | None:
    """Seed catalog + sqlite DB from $SNOWFLAKE_MOCK_SEED_PATH (a JSON
    file with `databases: {DB: {schemas: {SCHEMA: {tables: {NAME: {
    columns: [...], data: [...]}}}}}}` plus optional `current` and
    `allowed_databases`). Returns the new state dict or None if no
    seed file is configured / present.
    """
    seed_path = os.environ.get("SNOWFLAKE_MOCK_SEED_PATH")
    if not seed_path or not os.path.exists(seed_path):
        return None
    with open(seed_path, "r", encoding="utf-8") as f:
        seed = json.load(f)
    state = _empty_state()
    if isinstance(seed.get("current"), dict):
        state["current"].update(seed["current"])
    if isinstance(seed.get("allowed_databases"), list):
        state["allowed_databases"] = [
            s.upper() for s in seed["allowed_databases"]
        ]
    if "allow_write" in seed:
        state["allow_write"] = bool(seed["allow_write"])
    conn = sqlite3.connect(_db_path())
    try:
        for db_name, db_body in (seed.get("databases") or {}).items():
            db_u = db_name.upper()
            state["databases"].setdefault(
                db_u, {"schemas": {}})
            for schema_name, sch_body in (db_body.get("schemas") or {}).items():
                sch_u = schema_name.upper()
                state["databases"][db_u]["schemas"].setdefault(
                    sch_u, {"tables": []})
                for table_name, tbl in (sch_body.get("tables") or {}).items():
                    tbl_u = table_name.upper()
                    flat = _flat_name(db_u, sch_u, tbl_u)
                    if isinstance(tbl, dict) and tbl.get("columns"):
                        cols = ", ".join(
                            _coerce_column_def(c) for c in tbl["columns"])
                        conn.execute(f'CREATE TABLE IF NOT EXISTS "{flat}" ({cols})')
                    elif isinstance(tbl, dict) and tbl.get("create_sql"):
                        # raw SQL definition, route through rewriter
                        conn.execute(_rewrite_sql(
                            tbl["create_sql"], state, default_db=db_u,
                            default_schema=sch_u))
                    if isinstance(tbl, dict) and tbl.get("data"):
                        for row in tbl["data"]:
                            keys = list(row.keys())
                            placeholders = ", ".join("?" for _ in keys)
                            col_list = ", ".join(f'"{k}"' for k in keys)
                            conn.execute(
                                f'INSERT INTO "{flat}" ({col_list}) '
                                f'VALUES ({placeholders})',
                                [row[k] for k in keys])
                    if tbl_u not in state["databases"][db_u]["schemas"][sch_u]["tables"]:
                        state["databases"][db_u]["schemas"][sch_u]["tables"].append(tbl_u)
        conn.commit()
    finally:
        conn.close()
    return state


def _coerce_column_def(col: Any) -> str:
    """Accept either a string ("NAME VARCHAR(255) NOT NULL") or a dict
    ({"name":..,"type":..,"constraints":..}); return a SQL column spec."""
    if isinstance(col, str):
        return col
    if isinstance(col, dict):
        name = col["name"]
        typ = col.get("type", "TEXT")
        cs = col.get("constraints", "")
        return f'"{name}" {typ}{(" " + cs) if cs else ""}'
    raise ValueError(f"bad column spec: {col!r}")


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seeded = _seed_state()
        if seeded is not None:
            _save_state(seeded)
            return seeded
        s = _empty_state()
        _save_state(s)
        return s
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
# Configuration (CLI args parsed once at startup)
# ---------------------------------------------------------------------------

CONFIG: dict = {
    "allow_write": True,
    "allowed_databases": None,
    "exclude_json_results": False,
    "exclude_tools": set(),
    "database": None,
    "schema": None,
}


def _check_db_access(name: str) -> None:
    allowed = CONFIG["allowed_databases"]
    if allowed is None:
        return
    if name.upper() not in {a.upper() for a in allowed}:
        raise ValueError(
            f"Access denied: Database '{name}' is not in the allowed "
            f"databases list: {allowed}")


# ---------------------------------------------------------------------------
# SQL rewriting
# ---------------------------------------------------------------------------

def _flat_name(db: str, schema: str, table: str) -> str:
    return f"{db.upper()}__{schema.upper()}__{table.upper()}"


_QUAL_RE = re.compile(
    r'(?<!["\w])'
    r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)'
    r'(?!["\w])'
)

_CURRENT_TS_RE = re.compile(r'\bCURRENT_TIMESTAMP\s*\(\s*\)', re.IGNORECASE)
_CURRENT_DATE_RE = re.compile(r'\bCURRENT_DATE\s*\(\s*\)', re.IGNORECASE)
_SYSDATE_RE = re.compile(r'\bSYSDATE\s*\(\s*\)', re.IGNORECASE)


def _rewrite_qualified(match: re.Match, state: dict) -> str:
    db, schema, table = match.group(1), match.group(2), match.group(3)
    # Skip INFORMATION_SCHEMA — caller handles these separately.
    if schema.upper() == "INFORMATION_SCHEMA":
        return match.group(0)
    return f'"{_flat_name(db, schema, table)}"'


def _rewrite_sql(query: str, state: dict,
                 default_db: str | None = None,
                 default_schema: str | None = None) -> str:
    """Rewrite a Snowflake SQL fragment so sqlite can execute it:
      - three-part identifiers -> flat quoted names
      - CURRENT_TIMESTAMP() / CURRENT_DATE() / SYSDATE() -> sqlite forms
      - DATEADD/DATEDIFF kept as-is (sqlite ignores unknown funcs at parse
        time so they'd error; we replace common shapes).
    INFORMATION_SCHEMA references are left intact so the caller can route
    them to the catalog instead of sqlite.
    """
    q = query

    q = _CURRENT_TS_RE.sub("CURRENT_TIMESTAMP", q)
    q = _CURRENT_DATE_RE.sub("CURRENT_DATE", q)
    q = _SYSDATE_RE.sub("CURRENT_TIMESTAMP", q)

    # DATEADD(unit, n, t) -> datetime(t, '+n unit')
    q = re.sub(
        r"DATEADD\s*\(\s*(?:'([^']+)'|([A-Za-z_]+))\s*,\s*"
        r"([+-]?\d+)\s*,\s*([^)]+)\)",
        lambda m: _dateadd_helper(
            m.group(1) or m.group(2), m.group(3), m.group(4)),
        q, flags=re.IGNORECASE)

    # DATEDIFF('day', a, b) -> CAST(julianday(b) - julianday(a) AS INTEGER)
    def _datediff(m: re.Match) -> str:
        unit = (m.group(1) or m.group(2) or "").strip("'\"").lower()
        a, b = m.group(3), m.group(4)
        if unit in ("day", "days", "d"):
            return f"CAST((julianday({b}) - julianday({a})) AS INTEGER)"
        if unit in ("hour", "hours", "h"):
            return f"CAST((julianday({b}) - julianday({a})) * 24 AS INTEGER)"
        if unit in ("minute", "minutes", "min", "m"):
            return f"CAST((julianday({b}) - julianday({a})) * 1440 AS INTEGER)"
        if unit in ("second", "seconds", "sec", "s"):
            return f"CAST((julianday({b}) - julianday({a})) * 86400 AS INTEGER)"
        return f"CAST((julianday({b}) - julianday({a})) AS INTEGER)"

    q = re.sub(
        r"DATEDIFF\s*\(\s*(?:'([^']+)'|([A-Za-z_]+))\s*,\s*([^,]+?)\s*,\s*([^)]+)\)",
        _datediff, q, flags=re.IGNORECASE)

    # Three-part identifier -> flat sqlite name.
    q = _QUAL_RE.sub(lambda m: _rewrite_qualified(m, state), q)

    # Two-part schema.table (PUBLIC.FOO) using current database.
    if default_db is not None:
        two_part_re = re.compile(
            r'(?<!["\w.])'
            r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)'
            r'(?!["\w.])'
        )

        def _two(m: re.Match) -> str:
            schema, table = m.group(1), m.group(2)
            if schema.upper() == "INFORMATION_SCHEMA":
                return m.group(0)
            return f'"{_flat_name(default_db, schema, table)}"'
        q = two_part_re.sub(_two, q)

    return q


def _dateadd_helper(unit: str, n: str, t: str) -> str:
    unit = (unit or "").strip("'\"").lower()
    unit = {"yr": "years", "year": "years", "years": "years",
            "mon": "months", "month": "months", "months": "months",
            "wk": "weeks", "week": "weeks", "weeks": "weeks",
            "day": "days", "days": "days",
            "hr": "hours", "hour": "hours", "hours": "hours",
            "min": "minutes", "minute": "minutes", "minutes": "minutes",
            "sec": "seconds", "second": "seconds",
            "seconds": "seconds"}.get(unit, unit)
    n = (n or "0").strip()
    sign = "" if (n.startswith("-") or n.startswith("+")) else "+"
    return f"datetime({t}, '{sign}{n} {unit}')"


# ---------------------------------------------------------------------------
# INFORMATION_SCHEMA shim
# ---------------------------------------------------------------------------

_IS_RE = re.compile(
    r'\b(?:([A-Za-z_][A-Za-z0-9_]*)\.)?INFORMATION_SCHEMA\.([A-Za-z_]+)\b',
    re.IGNORECASE,
)


def _maybe_serve_information_schema(query: str, state: dict
                                    ) -> list[dict] | None:
    """If `query` looks like a simple SELECT against
    `[DB.]INFORMATION_SCHEMA.<view>`, return rows synthesized from the
    state catalog. Returns None if the query doesn't match the simple
    shapes we serve.

    Supported views:
      INFORMATION_SCHEMA.DATABASES   (DATABASE_NAME)
      INFORMATION_SCHEMA.SCHEMATA    (CATALOG_NAME, SCHEMA_NAME)
      INFORMATION_SCHEMA.TABLES      (TABLE_CATALOG, TABLE_SCHEMA,
                                     TABLE_NAME, COMMENT)
      INFORMATION_SCHEMA.COLUMNS     (COLUMN_NAME, COLUMN_DEFAULT,
                                     IS_NULLABLE, DATA_TYPE, COMMENT,
                                     TABLE_NAME, TABLE_SCHEMA)
      SELECT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE
        SCHEMA_NAME = '...') -> [{"EXISTS": 0|1}]
    """
    m = _IS_RE.search(query)
    if not m:
        return None
    db_prefix = (m.group(1) or "").upper() or None
    view = m.group(2).upper()
    qu = query.upper()

    # SELECT EXISTS(SELECT 1 FROM ... INFORMATION_SCHEMA.SCHEMATA
    # WHERE SCHEMA_NAME = 'X');  -> 1 if database X exists, else 0
    exists_match = re.search(
        r"SELECT\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+"
        r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?INFORMATION_SCHEMA\.SCHEMATA"
        r"\s+WHERE\s+SCHEMA_NAME\s*=\s*'([^']+)'",
        query, re.IGNORECASE)
    if exists_match:
        target = exists_match.group(1).upper()
        # Tasks use this to check whether a *database* called X exists
        # (Snowflake places the database catalog under the same name).
        existed = target in state["databases"]
        return [{"EXISTS": int(existed)}]

    if view == "DATABASES":
        rows = [{"DATABASE_NAME": d} for d in sorted(state["databases"].keys())]
        return rows
    if view == "SCHEMATA":
        # If DB prefix is given, scope to that database; otherwise return
        # schemata from all known databases.
        rows = []
        scope = ([db_prefix] if db_prefix and db_prefix in state["databases"]
                 else list(state["databases"].keys()))
        for d in scope:
            for s in sorted(state["databases"][d]["schemas"].keys()):
                rows.append({"CATALOG_NAME": d, "SCHEMA_NAME": s})
        # WHERE SCHEMA_NAME = 'X' filter (used by 'does this db exist?')
        wm = re.search(r"SCHEMA_NAME\s*=\s*'([^']+)'",
                       query, re.IGNORECASE)
        if wm:
            target = wm.group(1).upper()
            rows = [r for r in rows if r["SCHEMA_NAME"] == target]
        return rows
    if view == "TABLES":
        # filter on schema if WHERE TABLE_SCHEMA = '...' present
        wm = re.search(r"TABLE_SCHEMA\s*=\s*'([^']+)'",
                       query, re.IGNORECASE)
        schema_filter = wm.group(1).upper() if wm else None
        rows = []
        dbs = ([db_prefix] if db_prefix and db_prefix in state["databases"]
               else list(state["databases"].keys()))
        for d in dbs:
            for s, body in state["databases"][d]["schemas"].items():
                if schema_filter and s != schema_filter:
                    continue
                for t in body["tables"]:
                    rows.append({
                        "TABLE_CATALOG": d, "TABLE_SCHEMA": s,
                        "TABLE_NAME": t,
                        "COMMENT": state.get("comments", {}).get(
                            _flat_name(d, s, t), None),
                    })
        return rows
    if view == "COLUMNS":
        wm_t = re.search(r"TABLE_NAME\s*=\s*'([^']+)'",
                         query, re.IGNORECASE)
        wm_s = re.search(r"TABLE_SCHEMA\s*=\s*'([^']+)'",
                         query, re.IGNORECASE)
        t_filter = wm_t.group(1).upper() if wm_t else None
        s_filter = wm_s.group(1).upper() if wm_s else None
        rows = []
        dbs = ([db_prefix] if db_prefix and db_prefix in state["databases"]
               else list(state["databases"].keys()))
        conn = sqlite3.connect(_db_path())
        try:
            for d in dbs:
                for s, body in state["databases"][d]["schemas"].items():
                    if s_filter and s != s_filter:
                        continue
                    for t in body["tables"]:
                        if t_filter and t != t_filter:
                            continue
                        flat = _flat_name(d, s, t)
                        try:
                            info = conn.execute(
                                f'PRAGMA table_info("{flat}")').fetchall()
                        except sqlite3.Error:
                            info = []
                        flat_key = flat
                        col_comments = state.get(
                            "column_comments", {}).get(flat_key, {})
                        for cid, cname, ctype, notnull, dflt, pk in info:
                            rows.append({
                                "COLUMN_NAME": cname,
                                "COLUMN_DEFAULT": dflt,
                                "IS_NULLABLE": "NO" if notnull else "YES",
                                "DATA_TYPE": ctype or "TEXT",
                                "COMMENT": col_comments.get(cname.upper()),
                                "TABLE_NAME": t,
                                "TABLE_SCHEMA": s,
                                "TABLE_CATALOG": d,
                                "PRIMARY_KEY": bool(pk),
                            })
        finally:
            conn.close()
        return rows
    return None


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------

def _execute(query: str, state: dict,
             default_db: str | None = None,
             default_schema: str | None = None
             ) -> tuple[list[dict], str]:
    """Execute a Snowflake-flavoured query against the sqlite-backed
    store. Returns (rows, data_id).
    """
    # Strip USE WAREHOUSE / USE DATABASE / USE SCHEMA — sqlite has no
    # session concept; we just track current in state.
    stripped = query.strip().rstrip(";").strip()
    upper = stripped.upper()
    if upper.startswith("USE WAREHOUSE"):
        return [{"status": "success",
                 "message": "Statement executed successfully."}], _data_id()
    if upper.startswith("USE DATABASE"):
        name = stripped.split()[2].strip('"').upper()
        state["current"]["database"] = name
        return [{"status": "success",
                 "message": "Statement executed successfully."}], _data_id()
    if upper.startswith("USE SCHEMA"):
        name = stripped.split()[2].strip('"').upper()
        state["current"]["schema"] = name
        return [{"status": "success",
                 "message": "Statement executed successfully."}], _data_id()

    # CREATE DATABASE X / DROP DATABASE X — catalog-only, no sqlite hit.
    m = re.match(r"CREATE\s+DATABASE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                 r"([A-Za-z_][A-Za-z0-9_]*)", stripped, re.IGNORECASE)
    if m:
        name = m.group(1).upper()
        _check_db_access(name)
        state["databases"].setdefault(name, {"schemas": {"PUBLIC": {"tables": []}}})
        return [{"status": "success",
                 "message": f"Database {name} successfully created."}], _data_id()
    m = re.match(r"DROP\s+DATABASE\s+(?:IF\s+EXISTS\s+)?"
                 r"([A-Za-z_][A-Za-z0-9_]*)", stripped, re.IGNORECASE)
    if m:
        name = m.group(1).upper()
        _check_db_access(name)
        state["databases"].pop(name, None)
        # drop every sqlite table that lives under this database
        conn = sqlite3.connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                f"AND name LIKE '{name}__%'").fetchall()
            for (tname,) in rows:
                conn.execute(f'DROP TABLE IF EXISTS "{tname}"')
            conn.commit()
        finally:
            conn.close()
        return [{"status": "success",
                 "message": f"Database {name} successfully dropped."}], _data_id()

    # CREATE SCHEMA db.schema or just schema
    m = re.match(r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                 r"(?:([A-Za-z_][A-Za-z0-9_]*)\.)?"
                 r"([A-Za-z_][A-Za-z0-9_]*)", stripped, re.IGNORECASE)
    if m:
        db = (m.group(1) or default_db or
              state["current"]["database"] or "").upper()
        if not db:
            raise ValueError("CREATE SCHEMA requires a database context")
        _check_db_access(db)
        state["databases"].setdefault(db, {"schemas": {}})
        state["databases"][db]["schemas"].setdefault(
            m.group(2).upper(), {"tables": []})
        return [{"status": "success",
                 "message": f"Schema {db}.{m.group(2).upper()} created."}], _data_id()
    m = re.match(r"DROP\s+SCHEMA\s+(?:IF\s+EXISTS\s+)?"
                 r"(?:([A-Za-z_][A-Za-z0-9_]*)\.)?"
                 r"([A-Za-z_][A-Za-z0-9_]*)", stripped, re.IGNORECASE)
    if m:
        db = (m.group(1) or default_db or
              state["current"]["database"] or "").upper()
        sch = m.group(2).upper()
        if db and db in state["databases"]:
            tables = state["databases"][db]["schemas"].pop(
                sch, {"tables": []}).get("tables", [])
            conn = sqlite3.connect(_db_path())
            try:
                for t in tables:
                    conn.execute(
                        f'DROP TABLE IF EXISTS "{_flat_name(db, sch, t)}"')
                conn.commit()
            finally:
                conn.close()
        return [{"status": "success",
                 "message": f"Schema {db}.{sch} dropped."}], _data_id()

    # INFORMATION_SCHEMA SELECT — short-circuit before hitting sqlite.
    if "INFORMATION_SCHEMA" in upper and upper.startswith(("SELECT", "WITH")):
        rows = _maybe_serve_information_schema(stripped, state)
        if rows is not None:
            return rows, _data_id()

    # Default DB / schema fallback for unqualified references.
    eff_db = default_db or state["current"]["database"] or CONFIG.get(
        "database")
    eff_schema = (default_schema or state["current"]["schema"]
                  or CONFIG.get("schema"))

    rewritten = _rewrite_sql(
        stripped, state,
        default_db=(eff_db.upper() if eff_db else None),
        default_schema=(eff_schema.upper() if eff_schema else None))

    # Track CREATE TABLE / DROP TABLE in the catalog.
    ct = re.match(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                  r'(?:"([^"]+)"|([A-Z][A-Z0-9_]*)?)',
                  rewritten, re.IGNORECASE)
    if ct:
        flat_name = ct.group(1) or ct.group(2) or ""
        _register_table_in_catalog(flat_name, state)
    dt = re.match(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?'
                  r'(?:"([^"]+)"|([A-Z][A-Z0-9_]*))',
                  rewritten, re.IGNORECASE)
    if dt:
        flat_name = dt.group(1) or dt.group(2) or ""
        _unregister_table_from_catalog(flat_name, state)

    # COMMENT ON COLUMN ... IS '...' — sqlite has no native column
    # comments, so we stash them in state and serve them via
    # INFORMATION_SCHEMA.COLUMNS.
    cc = re.match(
        r"COMMENT\s+ON\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,3})"
        r"\s+IS\s+'(.*)'\s*;?\s*$",
        stripped, re.IGNORECASE | re.DOTALL)
    if cc:
        parts = cc.group(1).split(".")
        if len(parts) == 4:
            d, s, t, col = parts[0].upper(), parts[1].upper(), parts[2].upper(), parts[3].upper()
        elif len(parts) == 3:
            d = (eff_db or "").upper()
            s, t, col = parts[0].upper(), parts[1].upper(), parts[2].upper()
        else:
            raise ValueError(f"bad COMMENT ON COLUMN target: {cc.group(1)}")
        flat = _flat_name(d, s, t)
        state.setdefault("column_comments", {}).setdefault(flat, {})[col] = cc.group(2)
        return [{"status": "success",
                 "message": "Statement executed successfully."}], _data_id()

    # COMMENT ON TABLE
    ct2 = re.match(
        r"COMMENT\s+ON\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,2})"
        r"\s+IS\s+'(.*)'\s*;?\s*$",
        stripped, re.IGNORECASE | re.DOTALL)
    if ct2:
        parts = ct2.group(1).split(".")
        if len(parts) == 3:
            d, s, t = parts[0].upper(), parts[1].upper(), parts[2].upper()
        else:
            d = (eff_db or "").upper()
            s, t = parts[0].upper(), parts[1].upper()
        flat = _flat_name(d, s, t)
        state.setdefault("comments", {})[flat] = ct2.group(2)
        return [{"status": "success",
                 "message": "Statement executed successfully."}], _data_id()

    # Otherwise just execute via sqlite.
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(rewritten)
        if upper.startswith(("SELECT", "WITH", "PRAGMA", "EXPLAIN")):
            rows = [dict(r) for r in cur.fetchall()]
            return rows, _data_id()
        conn.commit()
        rc = cur.rowcount
        return [{"status": "success",
                 "message": "Statement executed successfully.",
                 "rowcount": rc}], _data_id()
    except sqlite3.Error as e:
        raise ValueError(f"SQL error: {e} | rewritten: {rewritten!r}")
    finally:
        conn.close()


def _register_table_in_catalog(flat: str, state: dict) -> None:
    if not flat:
        return
    parts = flat.split("__")
    if len(parts) < 3:
        return
    d, s, t = parts[0], parts[1], "__".join(parts[2:])
    state["databases"].setdefault(d, {"schemas": {}})
    state["databases"][d]["schemas"].setdefault(s, {"tables": []})
    if t not in state["databases"][d]["schemas"][s]["tables"]:
        state["databases"][d]["schemas"][s]["tables"].append(t)


def _unregister_table_from_catalog(flat: str, state: dict) -> None:
    if not flat:
        return
    parts = flat.split("__")
    if len(parts) < 3:
        return
    d, s, t = parts[0], parts[1], "__".join(parts[2:])
    body = state["databases"].get(d, {}).get("schemas", {}).get(s)
    if body and t in body["tables"]:
        body["tables"].remove(t)


def _data_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Response shaping (mimic the official server's YAML-string text content)
# ---------------------------------------------------------------------------

def _yaml_dump(obj: Any, indent: int = 0) -> str:
    """Lightweight YAML dumper sufficient for the rows we return. Avoids
    a runtime dependency on PyYAML."""
    sp = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        parts = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                parts.append(f"{sp}{k}:")
                parts.append(_yaml_dump(v, indent + 1))
            else:
                parts.append(f"{sp}{k}: {_yaml_scalar(v)}")
        return "\n".join(parts)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        parts = []
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                parts.append(f"{sp}-")
                parts.append(_yaml_dump(item, indent + 1))
            else:
                parts.append(f"{sp}- {_yaml_scalar(item)}")
        return "\n".join(parts)
    return f"{sp}{_yaml_scalar(obj)}"


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in ":#-{}[],&*!|>'\"%@`\n") or s.strip() != s:
        return json.dumps(s)
    return s


def _wrap_data(rows: list[dict], data_id: str, **extra) -> str:
    payload = {"type": "data", "data_id": data_id, **extra, "data": rows}
    return _yaml_dump(payload)


# ---------------------------------------------------------------------------
# Write-detection (subset of the upstream SQLWriteDetector)
# ---------------------------------------------------------------------------

_WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE",
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "GRANT", "REVOKE",
}


def _contains_write(query: str) -> bool:
    # crude but matches the upstream behaviour for our query mix.
    q = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    q = re.sub(r"/\*.*?\*/", "", q, flags=re.DOTALL)
    tokens = re.findall(r"[A-Za-z_]+", q.upper())
    return any(t in _WRITE_KEYWORDS for t in tokens)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP("snowflake-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



def _maybe_check_access_from_query(query: str) -> None:
    if CONFIG["allowed_databases"] is None:
        return
    q = query.upper().strip()
    if q.startswith("USE "):
        toks = q.split()
        if len(toks) >= 3:
            _check_db_access(toks[2].strip(';"`'))
            return
    for m in _QUAL_RE.finditer(query):
        db = m.group(1)
        # Skip INFORMATION_SCHEMA in second-position
        if m.group(2).upper() == "INFORMATION_SCHEMA":
            continue
        try:
            _check_db_access(db)
            return
        except ValueError:
            raise
    m2 = re.search(r"CREATE\s+DATABASE\s+([A-Za-z_][A-Za-z0-9_]*)",
                   query, re.IGNORECASE)
    if m2:
        _check_db_access(m2.group(1))
        return
    m3 = re.search(r"DROP\s+DATABASE\s+([A-Za-z_][A-Za-z0-9_]*)",
                   query, re.IGNORECASE)
    if m3:
        _check_db_access(m3.group(1))


@mcp.tool(name="list_databases")
def list_databases() -> str:
    """Snowflake MCP: list every database visible to the connection.

    In Snowflake this is `SELECT DATABASE_NAME FROM
    INFORMATION_SCHEMA.DATABASES`. The mock returns the catalog from
    `state.json`, filtered to `--allowed_databases` when that
    restriction is set.
    """
    with _lock():
        s = _load_state()
        names = sorted(s["databases"].keys())
        if CONFIG["allowed_databases"] is not None:
            allowed = {a.upper() for a in CONFIG["allowed_databases"]}
            names = [n for n in names if n in allowed]
        rows = [{"DATABASE_NAME": n} for n in names]
        data_id = _data_id()
        _record(s, "list_databases", count=len(rows))
        _save_state(s)
        return _wrap_data(rows, data_id)


@mcp.tool(name="list_schemas")
def list_schemas(database: str) -> str:
    """Snowflake MCP: list schemas in `<database>` via
    `INFORMATION_SCHEMA.SCHEMATA`."""
    with _lock():
        s = _load_state()
        _check_db_access(database)
        db = database.upper()
        if db not in s["databases"]:
            _record(s, "list_schemas", database=db, result="not_found")
            _save_state(s)
            raise ValueError(f"database '{database}' not found")
        rows = [{"CATALOG_NAME": db, "SCHEMA_NAME": sch}
                for sch in sorted(s["databases"][db]["schemas"].keys())]
        _record(s, "list_schemas", database=db, count=len(rows))
        _save_state(s)
        return _wrap_data(rows, _data_id(), database=db)


@mcp.tool(name="list_tables")
def list_tables(database: str, schema: str) -> str:
    """Snowflake MCP: list tables in `<database>.<schema>` via
    `INFORMATION_SCHEMA.TABLES`."""
    with _lock():
        s = _load_state()
        _check_db_access(database)
        db, sch = database.upper(), schema.upper()
        if db not in s["databases"] or sch not in s["databases"][db]["schemas"]:
            _record(s, "list_tables", database=db, schema=sch,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"{db}.{sch} not found")
        tables = s["databases"][db]["schemas"][sch]["tables"]
        rows = [{
            "TABLE_CATALOG": db, "TABLE_SCHEMA": sch,
            "TABLE_NAME": t,
            "COMMENT": s.get("comments", {}).get(_flat_name(db, sch, t)),
        } for t in sorted(tables)]
        _record(s, "list_tables", database=db, schema=sch, count=len(rows))
        _save_state(s)
        return _wrap_data(rows, _data_id(), database=db, schema=sch)


@mcp.tool(name="describe_table")
def describe_table(table_name: str) -> str:
    """Snowflake MCP: describe the columns of a table. `table_name`
    must be fully qualified as `database.schema.table` (the upstream
    server enforces this)."""
    with _lock():
        s = _load_state()
        parts = table_name.split(".")
        if len(parts) < 3:
            raise ValueError(
                "Table name must be fully qualified as "
                "'database.schema.table'")
        db, sch, tbl = parts[0].upper(), parts[1].upper(), parts[2].upper()
        _check_db_access(db)
        flat = _flat_name(db, sch, tbl)
        conn = sqlite3.connect(_db_path())
        try:
            info = conn.execute(f'PRAGMA table_info("{flat}")').fetchall()
        except sqlite3.Error:
            info = []
        finally:
            conn.close()
        col_comments = s.get("column_comments", {}).get(flat, {})
        rows = []
        for cid, cname, ctype, notnull, dflt, pk in info:
            rows.append({
                "COLUMN_NAME": cname,
                "COLUMN_DEFAULT": dflt,
                "IS_NULLABLE": "NO" if notnull else "YES",
                "DATA_TYPE": ctype or "TEXT",
                "COMMENT": col_comments.get(cname.upper()),
            })
        _record(s, "describe_table", table=f"{db}.{sch}.{tbl}",
                count=len(rows))
        _save_state(s)
        return _wrap_data(rows, _data_id(), database=db,
                          schema=sch, table=tbl)


@mcp.tool(name="read_query")
def read_query(query: str) -> str:
    """Snowflake MCP: execute a read-only SELECT query and return the
    rows. Write operations are rejected.

    The mock rewrites Snowflake-only syntax (CURRENT_TIMESTAMP(),
    DATEADD, three-part identifiers, INFORMATION_SCHEMA views) before
    running against sqlite.
    """
    with _lock():
        s = _load_state()
        if _contains_write(query):
            _record(s, "read_query", result="write_rejected")
            _save_state(s)
            raise ValueError(
                "Calls to read_query should not contain write operations")
        _maybe_check_access_from_query(query)
        rows, data_id = _execute(query, s)
        _record(s, "read_query", count=len(rows))
        _save_state(s)
        return _wrap_data(rows, data_id)


@mcp.tool(name="write_query")
def write_query(query: str) -> str:
    """Snowflake MCP: execute an INSERT / UPDATE / DELETE on the
    Snowflake database. SELECT queries are rejected here — use
    `read_query` for those. Disabled when the server is launched
    without `--allow_write`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if query.strip().upper().startswith("SELECT"):
            raise ValueError(
                "SELECT queries are not allowed for write_query")
        _maybe_check_access_from_query(query)
        rows, data_id = _execute(query, s)
        _record(s, "write_query", count=len(rows))
        _save_state(s)
        # Upstream wraps rows in str(...). Mirror that.
        return str(rows)


@mcp.tool(name="create_table")
def create_table(query: str) -> str:
    """Snowflake MCP: execute a `CREATE TABLE ...` statement.
    Disabled when the server is launched without `--allow_write`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not query.strip().upper().startswith("CREATE TABLE"):
            raise ValueError("Only CREATE TABLE statements are allowed")
        _maybe_check_access_from_query(query)
        rows, data_id = _execute(query, s)
        _record(s, "create_table", count=len(rows))
        _save_state(s)
        return f"Table created successfully. data_id = {data_id}"


@mcp.tool(name="append_insight")
def append_insight(insight: str) -> str:
    """Snowflake MCP: append a discovered data insight to the memo
    backing `memo://insights`."""
    with _lock():
        s = _load_state()
        s["insights"].append({
            "id": _data_id(), "ts": _now(), "text": insight})
        _record(s, "append_insight", insight=insight)
        _save_state(s)
        return "Insight added to memo"


@mcp.tool(name="create_databases")
def create_databases(databases: list[str]) -> str:
    """Snowflake MCP: create one or more databases. Each name is
    funnelled through `_check_db_access` so the `--allowed_databases`
    restriction is honoured."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(databases, list):
            raise ValueError("'databases' parameter must be a list")
        results, warnings = [], []
        for name in databases:
            try:
                _check_db_access(name)
            except ValueError:
                warnings.append(
                    f"Warning: Creating database '{name}' is not allowed, "
                    f"you can only create databases in the following list: "
                    f"{CONFIG['allowed_databases']}")
                continue
            u = name.upper()
            if u in s["databases"]:
                warnings.append(
                    f"Warning: Database '{name}' already exists, "
                    f"skipping creation")
            else:
                s["databases"][u] = {"schemas": {"PUBLIC": {"tables": []}}}
                results.append(f"Successfully created database '{name}'")
        _record(s, "create_databases", databases=databases)
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="drop_databases")
def drop_databases(databases: list[str]) -> str:
    """Snowflake MCP: drop one or more databases (and every table
    underneath them)."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(databases, list):
            raise ValueError("'databases' parameter must be a list")
        results, warnings = [], []
        for name in databases:
            _check_db_access(name)
            u = name.upper()
            if u not in s["databases"]:
                warnings.append(
                    f"Warning: Database '{name}' does not exist, "
                    f"skipping deletion")
                continue
            schemas = s["databases"].pop(u, {}).get("schemas", {})
            conn = sqlite3.connect(_db_path())
            try:
                for sch_name, body in schemas.items():
                    for t in body.get("tables", []):
                        conn.execute(
                            f'DROP TABLE IF EXISTS '
                            f'"{_flat_name(u, sch_name, t)}"')
                conn.commit()
            finally:
                conn.close()
            results.append(f"Successfully dropped database '{name}'")
        _record(s, "drop_databases", databases=databases)
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="create_schemas")
def create_schemas(database: str, schemas: list[str]) -> str:
    """Snowflake MCP: create one or more schemas under `<database>`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(schemas, list):
            raise ValueError("'schemas' parameter must be a list")
        _check_db_access(database)
        db = database.upper()
        if db not in s["databases"]:
            s["databases"][db] = {"schemas": {}}
        results, warnings = [], []
        for sch in schemas:
            su = sch.upper()
            if su in s["databases"][db]["schemas"]:
                warnings.append(
                    f"Warning: Schema '{sch}' already exists in database "
                    f"'{database}', skipping creation")
            else:
                s["databases"][db]["schemas"][su] = {"tables": []}
                results.append(
                    f"Successfully created schema '{sch}' in database "
                    f"'{database}'")
        _record(s, "create_schemas", database=db, schemas=schemas)
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="drop_schemas")
def drop_schemas(database: str, schemas: list[str]) -> str:
    """Snowflake MCP: drop one or more schemas from `<database>`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(schemas, list):
            raise ValueError("'schemas' parameter must be a list")
        _check_db_access(database)
        db = database.upper()
        if db not in s["databases"]:
            return f"Database '{database}' not found"
        results, warnings = [], []
        for sch in schemas:
            su = sch.upper()
            if su not in s["databases"][db]["schemas"]:
                warnings.append(
                    f"Warning: Schema '{sch}' does not exist in database "
                    f"'{database}', skipping deletion")
                continue
            body = s["databases"][db]["schemas"].pop(su)
            conn = sqlite3.connect(_db_path())
            try:
                for t in body.get("tables", []):
                    conn.execute(
                        f'DROP TABLE IF EXISTS "{_flat_name(db, su, t)}"')
                conn.commit()
            finally:
                conn.close()
            results.append(
                f"Successfully dropped schema '{sch}' from database "
                f"'{database}'")
        _record(s, "drop_schemas", database=db, schemas=schemas)
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="create_tables")
def create_tables(database: str, schema: str,
                  tables: list) -> str:
    """Snowflake MCP: create multiple tables in
    `<database>.<schema>`. Each item in `tables` is either a CREATE
    TABLE SQL string or `{"name", "definition"}`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(tables, list):
            raise ValueError("'tables' parameter must be a list")
        _check_db_access(database)
        db, sch = database.upper(), schema.upper()
        results, warnings = [], []
        for entry in tables:
            if isinstance(entry, dict):
                name = entry.get("name", "").upper()
                definition = entry.get("definition", "")
            else:
                definition = str(entry)
                m = re.search(r"CREATE\s+TABLE\s+(\w+)",
                              definition, re.IGNORECASE)
                name = (m.group(1).upper() if m else "UNKNOWN")
            existing = s["databases"].get(db, {}).get(
                "schemas", {}).get(sch, {}).get("tables", [])
            if name in existing:
                warnings.append(
                    f"Warning: Table '{name}' already exists in "
                    f"{database}.{schema}, skipping creation")
                continue
            # Force three-part qualification.
            definition_q = re.sub(
                r"CREATE\s+TABLE\s+" + re.escape(name) + r"\b",
                f"CREATE TABLE {db}.{sch}.{name}",
                definition, count=1, flags=re.IGNORECASE)
            try:
                _execute(definition_q, s, default_db=db, default_schema=sch)
                results.append(
                    f"Successfully created table '{name}' in "
                    f"{database}.{schema}")
            except Exception as e:
                results.append(
                    f"Failed to create table '{name}' in "
                    f"{database}.{schema}: {e}")
        _record(s, "create_tables", database=db, schema=sch,
                count=len(tables))
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="drop_tables")
def drop_tables(database: str, schema: str,
                tables: list[str]) -> str:
    """Snowflake MCP: drop multiple tables from
    `<database>.<schema>`."""
    with _lock():
        s = _load_state()
        if not CONFIG["allow_write"]:
            raise ValueError(
                "Write operations are not allowed for this data connection")
        if not isinstance(tables, list):
            raise ValueError("'tables' parameter must be a list")
        _check_db_access(database)
        db, sch = database.upper(), schema.upper()
        results, warnings = [], []
        existing = s["databases"].get(db, {}).get("schemas", {}).get(
            sch, {}).get("tables", [])
        conn = sqlite3.connect(_db_path())
        try:
            for t in tables:
                u = t.upper()
                if u not in existing:
                    warnings.append(
                        f"Warning: Table '{t}' does not exist in "
                        f"{database}.{schema}, skipping deletion")
                    continue
                conn.execute(
                    f'DROP TABLE IF EXISTS "{_flat_name(db, sch, u)}"')
                existing.remove(u)
                results.append(
                    f"Successfully dropped table '{t}' from "
                    f"{database}.{schema}")
            conn.commit()
        finally:
            conn.close()
        _record(s, "drop_tables", database=db, schema=sch, tables=tables)
        _save_state(s)
        return "\n".join(warnings + results)


@mcp.tool(name="list_insights")
def list_insights() -> str:
    """Mock helper: return the current list of insights (the official
    server exposes this via the `memo://insights` resource; we expose
    it as a tool too so verifiers can grab them without speaking the
    Resource protocol)."""
    with _lock():
        s = _load_state()
        return _wrap_data(s["insights"], _data_id())


# ---------------------------------------------------------------------------
# Debug tools (not present on the upstream server)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (catalog, insights,
    config flags, call log). Used by per-task verifiers and tests."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_exec")
def mock_debug_exec(query: str) -> dict:
    """Mock-only: run a raw sqlite query against the backing database
    (bypassing the Snowflake-to-sqlite rewriter and the write-detector).
    Used by per-task preprocessing to bulk-seed rows."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query)
        if query.strip().upper().startswith(("SELECT", "PRAGMA", "WITH")):
            return {"rows": [dict(r) for r in cur.fetchall()]}
        conn.commit()
        return {"rowcount": cur.rowcount}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI / entrypoint
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> None:
    """Parse the subset of `mcp_snowflake_server` CLI args we care
    about; ignore everything else (account, warehouse, user, role,
    private_key_path, …)."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--allow_write", action="store_true", default=True)
    parser.add_argument("--no-allow_write", dest="allow_write",
                        action="store_false")
    parser.add_argument("--allowed_databases", default=None)
    parser.add_argument("--exclude-json-results",
                        dest="exclude_json_results",
                        action="store_true", default=False)
    parser.add_argument("--exclude_tools", nargs="*", default=[])
    parser.add_argument("--database", default=None)
    parser.add_argument("--schema", default=None)
    # accept-and-ignore: any unknown --foo value pairs
    args, _unknown = parser.parse_known_args(argv)

    CONFIG["allow_write"] = bool(args.allow_write)
    CONFIG["exclude_json_results"] = bool(args.exclude_json_results)
    CONFIG["exclude_tools"] = set(args.exclude_tools or [])
    CONFIG["database"] = args.database
    CONFIG["schema"] = args.schema
    if args.allowed_databases:
        CONFIG["allowed_databases"] = [
            x.strip() for x in args.allowed_databases.split(",") if x.strip()]
    else:
        CONFIG["allowed_databases"] = None


def main() -> None:
    _parse_args(sys.argv[1:])
    # Persist the flags into state so verifiers can introspect them.
    with _lock():
        s = _load_state()
        s["allow_write"] = CONFIG["allow_write"]
        s["allowed_databases"] = CONFIG["allowed_databases"]
        s["exclude_json_results"] = CONFIG["exclude_json_results"]
        if CONFIG["database"]:
            s["current"]["database"] = CONFIG["database"].upper()
        if CONFIG["schema"]:
            s["current"]["schema"] = CONFIG["schema"].upper()
        _save_state(s)
    mcp.run()


if __name__ == "__main__":
    main()
