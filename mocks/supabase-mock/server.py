"""Supabase mock MCP server.

Mirrors the tool surface of `@supabase/mcp-server-supabase`, the
official Supabase MCP server. Tool names line up 1:1 where the
upstream exposes a single tool per concept; product-specific tools
gain a product prefix (``auth_*``, ``storage_*``) so the surface is
self-explaining when several products coexist on the same server.

Supabase APIs mocked
--------------------

  * Management API (https://api.supabase.com/v1/...)
      list_projects, get_project, create_project,
      list_organizations, get_project_api_keys, pause_project,
      restore_project, get_project_url

  * PostgREST + database access (``{project_url}/rest/v1/``)
      list_tables, execute_sql, apply_migration, list_migrations,
      select_rows, insert_rows, update_rows, delete_rows, rpc_call

  * Auth API (``{project_url}/auth/v1/``)
      auth_list_users, auth_get_user, auth_create_user,
      auth_update_user, auth_delete_user

  * Storage API (``{project_url}/storage/v1/``)
      storage_list_buckets, storage_create_bucket,
      storage_get_bucket, storage_delete_bucket,
      storage_list_objects, storage_upload_object,
      storage_get_object, storage_delete_object,
      storage_get_public_url, storage_get_signed_url

  * Edge Functions API
      list_functions, get_function, deploy_function,
      delete_function, invoke_function

  * Logs API
      get_logs

Postgres backend
----------------

Per project, the database is backed by a real sqlite file at
``$SUPABASE_MOCK_STATE_DIR/projects/<ref>.db`` (one file per project).
The table catalog (column types, primary keys, foreign keys) lives in
``state.json`` alongside the bucket metadata, auth users, edge
functions and migration log. This mirrors the postgres-mock pattern
(``catalog in state.json`` + ``sqlite sidecar``).

PostgREST filters supported by ``select_rows`` / ``update_rows`` /
``delete_rows``::

    eq.value     neq.value   gt.value   gte.value   lt.value   lte.value
    like.pat     ilike.pat   in.(a,b,c) is.null     is.true    is.false
    not.eq.v     not.in.(..)

Storage API objects are stored inline (content as string) — adequate
for the agent tasks the mock services (small text payloads, manifest
files, etc).

NOT supported (out of scope for the mock — agents must not depend on
these): realtime/broadcast channels, RLS policy enforcement, vector
embeddings (``pgvector``), row-level security checks against JWT
claims, the full PostgREST embedded-resources query syntax
(``select=...(...)`` joins), multi-region replication, point-in-time
recovery, billing / usage APIs, the Supabase dashboard signing-key
rotation surface, Studio API.

State at ``$SUPABASE_MOCK_STATE_DIR/state.json``::

    state = {
      "organizations": {<org_id>: {id, name, slug}},
      "projects": {
        "<ref>": {
          "info": {id, ref, name, organization_id, region, status,
                   created_at, db_password, project_url,
                   anon_key, service_role_key},
          "db_catalog": {schemas: {<schema>: {tables: {<table>:
                            {columns, primary_key, foreign_keys}}}}},
          "auth_users": {<user_id>: {...}},
          "storage_buckets": {<name>: {info, objects: {<path>: {...}}}},
          "functions": {<slug>: {...}},
          "migrations": [{version, name, query, created_at}],
          "logs": [{id, service, timestamp, event_message, metadata}]
        }
      },
      "calls": [...]
    }
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import sqlite3
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    d = os.environ.get(
        "SUPABASE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/supabase_mock"),
    )
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "projects"), exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(_state_dir(), "state.json")


def _project_db_path(ref: str) -> str:
    return os.path.join(_state_dir(), "projects", f"{ref}.db")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _gen_ref() -> str:
    """Supabase project refs are 20-char lowercase alphanumeric."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def _gen_uuid() -> str:
    return str(uuid.uuid4())


def _gen_org_id() -> str:
    """Org IDs in Supabase are short opaque slugs (~20 hex)."""
    return secrets.token_hex(10)


def _gen_numeric_id() -> str:
    """Numeric Supabase project id as a string (mirrors the API)."""
    return str(secrets.randbelow(10**8) + 10**7)


def _gen_api_key(kind: str = "anon") -> str:
    """JWT-looking synthetic key. Real Supabase keys are real JWTs
    signed by the project; the mock just returns the 'eyJ...' shape."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"})
        .encode("utf-8")).decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"role": kind, "iss": "supabase-mock"})
        .encode("utf-8")).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return f"{header}.{payload}.{sig}"


def _project_url(ref: str) -> str:
    return f"https://{ref}.supabase.co"


def _empty_state() -> dict:
    org_id = _gen_org_id()
    return {
        "organizations": {
            org_id: {
                "id": org_id,
                "name": "Mock Org",
                "slug": "mock-org",
            },
        },
        "projects": {},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("SUPABASE_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            base = _empty_state()
            base.update(loaded)
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
# Project resolution
# ---------------------------------------------------------------------------

def _get_project(state: dict, ref: str) -> dict | None:
    return state["projects"].get(ref)


def _require_project(state: dict, ref: str) -> dict:
    proj = _get_project(state, ref)
    if not proj:
        raise ValueError(f"Project not found: {ref}")
    return proj


def _public_project(proj: dict) -> dict:
    """Strip internal fields (api keys, db password) from a project
    record before returning it from list/get."""
    info = proj.get("info", {})
    return {
        "id": info.get("id"),
        "ref": info.get("ref"),
        "name": info.get("name"),
        "organization_id": info.get("organization_id"),
        "region": info.get("region"),
        "status": info.get("status"),
        "created_at": info.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Project sqlite backend
# ---------------------------------------------------------------------------

_PG_TYPE_OIDS: dict[str, int] = {
    "bool": 16, "boolean": 16,
    "bytea": 17,
    "int8": 20, "bigint": 20,
    "int2": 21, "smallint": 21,
    "int4": 23, "integer": 23, "int": 23,
    "text": 25,
    "json": 114,
    "float4": 700, "real": 700,
    "float8": 701, "double precision": 701,
    "varchar": 1043,
    "date": 1082,
    "timestamp": 1114,
    "timestamptz": 1184,
    "numeric": 1700, "decimal": 1700,
    "uuid": 2950,
    "jsonb": 3802,
}


def _normalize_pg_type(raw: str) -> str:
    if not raw:
        return "text"
    t = raw.strip().lower()
    is_array = t.endswith("[]")
    if is_array:
        t = t[:-2].strip()
    t = re.sub(r"\s*\(.*\)\s*$", "", t)
    return t + ("[]" if is_array else "")


def _pg_to_sqlite_type(pg_type: str) -> str:
    t = _normalize_pg_type(pg_type)
    if t.endswith("[]"):
        return "TEXT"
    if t in ("int2", "smallint", "int4", "integer", "int", "int8",
             "bigint", "serial", "bigserial", "smallserial", "oid"):
        return "INTEGER"
    if t in ("bool", "boolean"):
        return "INTEGER"
    if t in ("real", "float4", "float8", "double precision"):
        return "REAL"
    return "TEXT"


def _flat_name(schema: str, table: str) -> str:
    return f"{schema}__{table}"


def _ensure_project_db(ref: str) -> None:
    """Touch the sqlite file for `ref` so SELECT/INSERT can attach
    even before any DDL ran."""
    path = _project_db_path(ref)
    if not os.path.exists(path):
        conn = sqlite3.connect(path)
        conn.close()


def _project_conn(ref: str) -> sqlite3.Connection:
    _ensure_project_db(ref)
    conn = sqlite3.connect(_project_db_path(ref))
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA_TABLE_RE = re.compile(
    r'(?<!["\w.])'
    r'([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)'
    r'(?!["\w])'
)


def _rewrite_sql_for_sqlite(query: str) -> str:
    """Strip ``::cast`` syntax, swap ``ILIKE`` for ``LIKE``, and rewrite
    ``schema.table`` to ``"schema__table"``. Skips
    ``information_schema``/``pg_catalog`` qualifiers (those are
    handled by the catalog shim)."""
    q = re.sub(
        r"::\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?(?:\s*\[\s*\])?",
        "", query)
    q = re.sub(r"\bILIKE\b", "LIKE", q, flags=re.IGNORECASE)

    def _qualify(m: re.Match) -> str:
        schema, table = m.group(1), m.group(2)
        if schema.lower() in ("information_schema", "pg_catalog"):
            return m.group(0)
        return f'"{_flat_name(schema.lower(), table.lower())}"'

    return _SCHEMA_TABLE_RE.sub(_qualify, q)


def _register_table(proj: dict, *, schema: str, table: str,
                    columns: list[dict]) -> None:
    """Register a catalog entry for a table. Each column is
    ``{name, type, nullable?, default?, primary_key?, foreign_key?}``."""
    cat = proj.setdefault("db_catalog", {"schemas": {}})
    schemas = cat.setdefault("schemas", {})
    sch = schemas.setdefault(schema, {"tables": {}})
    sch.setdefault("tables", {})
    norm_cols: list[dict] = []
    for c in columns or []:
        norm_cols.append({
            "name": c["name"],
            "type": c.get("type", "text"),
            "oid": _PG_TYPE_OIDS.get(
                _normalize_pg_type(c.get("type", "text")), 0),
            "nullable": bool(c.get("nullable", True)),
            "default": c.get("default"),
            "primary_key": bool(c.get("primary_key", False)),
            "foreign_key": c.get("foreign_key"),
        })
    sch["tables"][table] = {
        "columns": norm_cols,
        "primary_key": [c["name"] for c in norm_cols
                        if c.get("primary_key")],
    }


def _materialize_table(ref: str, *, schema: str, table: str,
                       columns: list[dict]) -> None:
    """Create the sqlite table for an already-catalogued table."""
    flat = _flat_name(schema, table)
    conn = _project_conn(ref)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if flat in existing:
            return
        ddl_parts = []
        for c in columns:
            sql_type = _pg_to_sqlite_type(c.get("type", "text"))
            pk = " PRIMARY KEY" if c.get("primary_key") else ""
            notnull = (" NOT NULL" if not c.get("nullable", True)
                       else "")
            ddl_parts.append(
                f'"{c["name"]}" {sql_type}{pk}{notnull}')
        if not ddl_parts:
            return
        conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{flat}" '
            f'({", ".join(ddl_parts)})')
        conn.commit()
    finally:
        conn.close()


def _ensure_all_catalog_tables(ref: str, proj: dict) -> None:
    """For every entry in the project's catalog, make sure the sqlite
    table exists. Cheap (skips existing tables)."""
    schemas = (proj.get("db_catalog") or {}).get("schemas") or {}
    for schema, body in schemas.items():
        for table, tbl in (body.get("tables") or {}).items():
            _materialize_table(ref, schema=schema, table=table,
                               columns=tbl.get("columns") or [])


# ---------------------------------------------------------------------------
# PostgREST filter parser
# ---------------------------------------------------------------------------

_OPS_NEEDING_VALUE = {
    "eq", "neq", "gt", "gte", "lt", "lte",
    "like", "ilike",
}


def _parse_filter(value: str) -> tuple[str, str, str]:
    """Parse a PostgREST filter spec like ``eq.42`` or ``not.eq.42``.
    Returns ``(negate, op, value)`` where ``negate`` is ``"not"`` or
    ``""``. Unknown ops raise ``ValueError``."""
    s = value
    negate = ""
    if s.startswith("not."):
        negate = "not"
        s = s[len("not."):]
    if "." not in s:
        raise ValueError(
            f"Invalid PostgREST filter: {value!r} "
            f"(expected '<op>.<value>')")
    op, _, rest = s.partition(".")
    op = op.lower()
    return negate, op, rest


def _split_in_list(spec: str) -> list[str]:
    """Parse the ``(a,b,c)`` payload of an ``in.(...)`` filter."""
    s = spec.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    if not s:
        return []
    return [x.strip() for x in s.split(",")]


def _filters_to_where(filters: dict[str, str]
                      ) -> tuple[str, list[Any]]:
    """Translate PostgREST-style ``{column: 'op.value'}`` filters into a
    sqlite WHERE clause + param list. Returns ``("", [])`` when no
    filters are supplied."""
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for col, spec in filters.items():
        if not isinstance(spec, str):
            # Convenience: ``{col: literal_value}`` becomes eq
            clauses.append(f'"{col}" = ?')
            params.append(spec)
            continue
        negate, op, rest = _parse_filter(spec)
        col_q = f'"{col}"'
        clause = ""
        if op in _OPS_NEEDING_VALUE:
            if op == "eq":
                clause = f"{col_q} = ?"
                params.append(rest)
            elif op == "neq":
                clause = f"{col_q} != ?"
                params.append(rest)
            elif op == "gt":
                clause = f"{col_q} > ?"
                params.append(rest)
            elif op == "gte":
                clause = f"{col_q} >= ?"
                params.append(rest)
            elif op == "lt":
                clause = f"{col_q} < ?"
                params.append(rest)
            elif op == "lte":
                clause = f"{col_q} <= ?"
                params.append(rest)
            elif op == "like":
                clause = f"{col_q} LIKE ?"
                params.append(rest.replace("*", "%"))
            elif op == "ilike":
                clause = f"LOWER({col_q}) LIKE LOWER(?)"
                params.append(rest.replace("*", "%"))
        elif op == "in":
            items = _split_in_list(rest)
            if not items:
                clauses.append("0")
                continue
            placeholders = ", ".join("?" for _ in items)
            clause = f"{col_q} IN ({placeholders})"
            params.extend(items)
        elif op == "is":
            target = rest.lower()
            if target == "null":
                clause = f"{col_q} IS NULL"
            elif target == "true":
                clause = f"{col_q} = 1"
            elif target == "false":
                clause = f"{col_q} = 0"
            else:
                raise ValueError(
                    f"Unsupported `is.{rest}` filter "
                    f"(expected null/true/false)")
        else:
            raise ValueError(f"Unsupported PostgREST operator: {op}")
        if negate == "not":
            clause = f"NOT ({clause})"
        clauses.append(clause)
    return "WHERE " + " AND ".join(clauses), params


def _parse_order(order: str | None) -> str:
    """``"col"`` -> ``ORDER BY "col" ASC``; ``"col.desc"`` ->
    ``ORDER BY "col" DESC``; multiple comma-separated entries
    supported."""
    if not order:
        return ""
    parts = []
    for chunk in order.split(","):
        c = chunk.strip()
        if not c:
            continue
        if "." in c:
            col, direction = c.split(".", 1)
            direction = direction.upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
        else:
            col, direction = c, "ASC"
        parts.append(f'"{col}" {direction}')
    return "ORDER BY " + ", ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("supabase-mock")


def _new_project(*, name: str, organization_id: str, region: str,
                 db_password: str, status: str = "ACTIVE_HEALTHY",
                 ref: str | None = None) -> dict:
    """Build the in-state project record (with synthetic api keys)."""
    proj_ref = ref or _gen_ref()
    info = {
        "id": _gen_numeric_id(),
        "ref": proj_ref,
        "name": name,
        "organization_id": organization_id,
        "region": region,
        "status": status,
        "created_at": _now(),
        "db_password": db_password,
        "project_url": _project_url(proj_ref),
        "anon_key": _gen_api_key("anon"),
        "service_role_key": _gen_api_key("service_role"),
    }
    return {
        "info": info,
        "db_catalog": {"schemas": {"public": {"tables": {}}}},
        "auth_users": {},
        "storage_buckets": {},
        "functions": {},
        "migrations": [],
        "logs": [],
    }


# ===========================================================================
# Management API
# ===========================================================================

@mcp.tool(name="list_projects")
def list_projects() -> list[dict]:
    """Management API: ``GET /v1/projects`` — list all projects on the
    account. Mirrors ``@supabase/mcp-server-supabase``'s
    ``list_projects``."""
    with _lock():
        s = _load_state()
        out = [_public_project(p) for p in s["projects"].values()]
        out.sort(key=lambda p: p.get("created_at") or "")
        _record(s, "list_projects", count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_project")
def get_project(projectRef: str) -> dict:
    """Management API: ``GET /v1/projects/{ref}`` — retrieve a single
    project by its 20-char ref."""
    with _lock():
        s = _load_state()
        proj = _get_project(s, projectRef)
        if not proj:
            _record(s, "get_project", projectRef=projectRef,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"Project not found: {projectRef}")
        _record(s, "get_project", projectRef=projectRef)
        _save_state(s)
        return _public_project(proj)


@mcp.tool(name="create_project")
def create_project(name: str, organizationId: str, region: str,
                   dbPassword: str) -> dict:
    """Management API: ``POST /v1/projects`` — provision a new project.

    Returns a project record with ``status="COMING_UP"`` (Supabase
    creates an async resource; the mock skips the wait and follows up
    with ``ACTIVE_HEALTHY`` on the next read)."""
    with _lock():
        s = _load_state()
        if organizationId not in s["organizations"]:
            _record(s, "create_project", name=name,
                    result="bad_org")
            _save_state(s)
            raise ValueError(
                f"Organization not found: {organizationId}")
        proj = _new_project(name=name,
                            organization_id=organizationId,
                            region=region,
                            db_password=dbPassword,
                            status="COMING_UP")
        ref = proj["info"]["ref"]
        s["projects"][ref] = proj
        _ensure_project_db(ref)
        _record(s, "create_project", ref=ref, name=name,
                organization_id=organizationId, region=region)
        _save_state(s)
        return {
            "id": proj["info"]["id"],
            "ref": ref,
            "name": name,
            "organization_id": organizationId,
            "region": region,
            "created_at": proj["info"]["created_at"],
            "status": "COMING_UP",
        }


@mcp.tool(name="list_organizations")
def list_organizations() -> list[dict]:
    """Management API: ``GET /v1/organizations`` — list every
    organization the caller belongs to. Mirrors
    ``@supabase/mcp-server-supabase``'s ``list_organizations``."""
    with _lock():
        s = _load_state()
        out = [{"id": o["id"], "name": o["name"], "slug": o["slug"]}
               for o in s["organizations"].values()]
        out.sort(key=lambda o: o["slug"])
        _record(s, "list_organizations", count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_project_api_keys")
def get_project_api_keys(projectRef: str) -> list[dict]:
    """Management API: ``GET /v1/projects/{ref}/api-keys`` — return the
    anon + service_role JWTs for the project."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        info = proj["info"]
        _record(s, "get_project_api_keys", projectRef=projectRef)
        _save_state(s)
        return [
            {"name": "anon", "api_key": info["anon_key"]},
            {"name": "service_role",
             "api_key": info["service_role_key"]},
        ]


@mcp.tool(name="pause_project")
def pause_project(projectRef: str) -> dict:
    """Management API: ``POST /v1/projects/{ref}/pause`` — pause a
    project's compute. Returns the new project status."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        proj["info"]["status"] = "PAUSED"
        _record(s, "pause_project", projectRef=projectRef)
        _save_state(s)
        return _public_project(proj)


@mcp.tool(name="restore_project")
def restore_project(projectRef: str) -> dict:
    """Management API: ``POST /v1/projects/{ref}/restore`` — restore a
    paused project. Returns the new project status."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        proj["info"]["status"] = "ACTIVE_HEALTHY"
        _record(s, "restore_project", projectRef=projectRef)
        _save_state(s)
        return _public_project(proj)


@mcp.tool(name="get_project_url")
def get_project_url(projectRef: str) -> dict:
    """Management API: ``GET /v1/projects/{ref}/api`` — return the
    project's PostgREST/Auth/Storage base URL."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _record(s, "get_project_url", projectRef=projectRef)
        _save_state(s)
        return {"url": proj["info"]["project_url"]}


# ===========================================================================
# PostgREST + database access
# ===========================================================================

@mcp.tool(name="list_tables")
def list_tables(projectRef: str,
                schemas: list[str] | None = None) -> list[dict]:
    """List tables in the project's database, optionally filtered to a
    set of ``schemas`` (defaults to ``["public"]``). Each entry is
    ``{schema, name, columns: [{name, type, nullable, primary_key,
    foreign_key}], primary_key: [...]}``."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        wanted = schemas or ["public"]
        out: list[dict] = []
        catalog_schemas = ((proj.get("db_catalog") or {})
                           .get("schemas") or {})
        for sch in wanted:
            tables = (catalog_schemas.get(sch) or {}).get("tables") or {}
            for name, tbl in sorted(tables.items()):
                out.append({
                    "schema": sch,
                    "name": name,
                    "columns": list(tbl.get("columns") or []),
                    "primary_key": list(tbl.get("primary_key") or []),
                })
        _record(s, "list_tables", projectRef=projectRef,
                schemas=list(wanted), count=len(out))
        _save_state(s)
        return out


def _split_statements(query: str) -> list[str]:
    """Split a multi-statement SQL string on ``;`` outside of strings.
    Naive but adequate for migrations the agent typically writes."""
    out: list[str] = []
    cur: list[str] = []
    in_single = False
    in_double = False
    for ch in query:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == ";" and not in_single and not in_double:
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
        else:
            cur.append(ch)
    last = "".join(cur).strip()
    if last:
        out.append(last)
    return out


_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))\.)?"
    r"(?:\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def _split_column_list(body: str) -> list[str]:
    parts, depth, cur = [], 0, ""
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


def _parse_column_def(text: str) -> dict | None:
    s = text.strip()
    if not s:
        return None
    upper = s.upper()
    if upper.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE",
                         "CHECK", "CONSTRAINT")):
        return None
    m = re.match(r'^(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s+([^,]+)$',
                 s)
    if not m:
        return None
    name = m.group(1) or m.group(2)
    rest = m.group(3).strip()
    type_match = re.match(
        r"((?:[A-Za-z_][A-Za-z0-9_]*"
        r"(?:\s+(?:precision|varying|with\s+time\s+zone|"
        r"without\s+time\s+zone))?)"
        r"(?:\s*\([^)]*\))?(?:\s*\[\s*\])?)",
        rest, re.IGNORECASE)
    type_str = type_match.group(1).strip() if type_match else "text"
    cons = rest[len(type_str):].strip() if type_match else rest
    upper_c = cons.upper()
    return {
        "name": name,
        "type": type_str,
        "nullable": "NOT NULL" not in upper_c,
        "primary_key": "PRIMARY KEY" in upper_c,
    }


def _maybe_register_create_table(proj: dict, stmt: str) -> bool:
    """Best-effort: parse a ``CREATE TABLE`` statement and add the
    catalog entry. Returns True when we matched."""
    m = _CREATE_TABLE_RE.match(stmt)
    if not m:
        return False
    schema = (m.group(1) or m.group(2) or "public").lower()
    table = (m.group(3) or m.group(4) or "").lower()
    if not table:
        return False
    start = m.end() - 1
    depth = 0
    end = start
    for i in range(start, len(stmt)):
        if stmt[i] == "(":
            depth += 1
        elif stmt[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = stmt[start + 1:end]
    cols: list[dict] = []
    pk_cols: list[str] = []
    for part in _split_column_list(body):
        ps = part.strip()
        if not ps:
            continue
        upper = ps.upper()
        if upper.startswith("PRIMARY KEY"):
            pk_cols.extend(re.findall(
                r"[A-Za-z_][A-Za-z0-9_]*", ps[len("PRIMARY KEY"):]))
            continue
        if upper.startswith(("FOREIGN KEY", "UNIQUE", "CHECK",
                             "CONSTRAINT")):
            continue
        parsed = _parse_column_def(ps)
        if parsed:
            cols.append(parsed)
    for c in cols:
        if c["name"] in pk_cols:
            c["primary_key"] = True
    _register_table(proj, schema=schema, table=table, columns=cols)
    return True


_CREATE_TABLE_STMT_RE = re.compile(
    r"^\s*CREATE\s+TABLE\b", re.IGNORECASE)


def _run_sql_in_project(ref: str, proj: dict, query: str
                        ) -> list[dict]:
    """Execute (possibly multi-statement) SQL against the project's
    sqlite db and return rows from the LAST statement (mirrors the
    upstream ``execute_sql`` shape).

    CREATE TABLE statements are NOT passed to sqlite directly: the
    catalog parser registers them and ``_ensure_all_catalog_tables``
    materializes the sqlite shape using sqlite-affine types. This
    avoids type-parse mismatches (``jsonb``, ``timestamptz``,
    ``numeric(10,2)``) that sqlite would reject."""
    _ensure_project_db(ref)
    # Catalog tracking first — every CREATE TABLE in the batch.
    for stmt in _split_statements(query):
        _maybe_register_create_table(proj, stmt)
    _ensure_all_catalog_tables(ref, proj)
    conn = _project_conn(ref)
    last_rows: list[dict] = []
    try:
        for raw in _split_statements(query):
            if not raw.strip():
                continue
            if _CREATE_TABLE_STMT_RE.match(raw):
                # Already handled by the catalog + sqlite materializer.
                continue
            stmt = _rewrite_sql_for_sqlite(raw)
            cur = conn.execute(stmt)
            try:
                fetched = cur.fetchall()
                last_rows = [dict(r) for r in fetched]
            except sqlite3.Error:
                last_rows = []
        conn.commit()
    finally:
        conn.close()
    return last_rows


@mcp.tool(name="execute_sql")
def execute_sql(projectRef: str, query: str) -> dict:
    """Run a raw SQL statement (or batch) against the project's
    database. Mirrors ``@supabase/mcp-server-supabase``'s
    ``execute_sql``.

    Returns ``{rows, rowCount}`` — ``rows`` from the trailing
    statement when it's a SELECT, otherwise the empty list."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        try:
            rows = _run_sql_in_project(projectRef, proj, query)
        except Exception as e:
            _record(s, "execute_sql", projectRef=projectRef,
                    error=str(e))
            _save_state(s)
            raise
        _record(s, "execute_sql", projectRef=projectRef,
                rowCount=len(rows))
        _save_state(s)
        return {"rows": rows, "rowCount": len(rows)}


@mcp.tool(name="apply_migration")
def apply_migration(projectRef: str, name: str, query: str) -> dict:
    """Apply a named migration to the project. Mirrors
    ``@supabase/mcp-server-supabase``'s ``apply_migration``: records
    ``{version, name, statements, created_at}`` in the migrations log
    and runs the statements against the project's db."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        try:
            _run_sql_in_project(projectRef, proj, query)
        except Exception as e:
            _record(s, "apply_migration", projectRef=projectRef,
                    name=name, error=str(e))
            _save_state(s)
            raise
        version = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%d%H%M%S")
        entry = {
            "version": version,
            "name": name,
            "statements": _split_statements(query),
            "created_at": _now(),
        }
        proj.setdefault("migrations", []).append(entry)
        _record(s, "apply_migration", projectRef=projectRef,
                name=name, version=version)
        _save_state(s)
        return entry


@mcp.tool(name="list_migrations")
def list_migrations(projectRef: str) -> list[dict]:
    """List the migrations applied to a project, oldest first. Mirrors
    ``@supabase/mcp-server-supabase``'s ``list_migrations``."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        out = list(proj.get("migrations") or [])
        _record(s, "list_migrations", projectRef=projectRef,
                count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="select_rows")
def select_rows(projectRef: str, table: str,
                select: str = "*",
                filters: dict | None = None,
                order: str | None = None,
                limit: int | None = None,
                offset: int | None = None,
                schema: str = "public") -> list[dict]:
    """PostgREST: ``GET {project_url}/rest/v1/{table}`` — read rows
    using PostgREST conventions.

    ``select`` is a comma-separated column list (or ``*``).
    ``filters`` map column -> ``"<op>.<value>"`` (eq/neq/gt/gte/lt/
    lte/like/ilike/in/is, with optional ``not.`` prefix).
    ``order`` is ``"col"`` or ``"col.desc"`` (multi-column comma list
    supported). ``limit`` / ``offset`` paginate."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _ensure_all_catalog_tables(projectRef, proj)
        cols_clause = ", ".join(
            f'"{c.strip()}"' for c in select.split(",")
        ) if select and select.strip() != "*" else "*"
        where, params = _filters_to_where(filters or {})
        order_clause = _parse_order(order)
        sql = (f'SELECT {cols_clause} FROM "{_flat_name(schema, table)}" '
               f'{where} {order_clause}').strip()
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"
        conn = _project_conn(projectRef)
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            _record(s, "select_rows", projectRef=projectRef,
                    table=table, error=str(e))
            _save_state(s)
            raise ValueError(f"SQL error: {e} | sql: {sql!r}")
        finally:
            conn.close()
        _record(s, "select_rows", projectRef=projectRef,
                table=table, schema=schema, count=len(rows))
        _save_state(s)
        return rows


def _columns_for(proj: dict, schema: str, table: str) -> list[dict]:
    schemas = (proj.get("db_catalog") or {}).get("schemas") or {}
    body = schemas.get(schema) or {}
    tbl = (body.get("tables") or {}).get(table) or {}
    return list(tbl.get("columns") or [])


@mcp.tool(name="insert_rows")
def insert_rows(projectRef: str, table: str,
                rows: list[dict],
                onConflict: str | None = None,
                schema: str = "public") -> list[dict]:
    """PostgREST: ``POST {project_url}/rest/v1/{table}`` — insert (or
    upsert via ``onConflict``) rows. Returns the inserted rows."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _ensure_all_catalog_tables(projectRef, proj)
        flat = _flat_name(schema, table)
        conn = _project_conn(projectRef)
        inserted: list[dict] = []
        try:
            for row in rows or []:
                if not isinstance(row, dict) or not row:
                    continue
                keys = list(row.keys())
                placeholders = ", ".join("?" for _ in keys)
                col_list = ", ".join(f'"{k}"' for k in keys)
                values: list[Any] = []
                for k in keys:
                    v = row[k]
                    if isinstance(v, (list, dict)):
                        v = json.dumps(v)
                    values.append(v)
                upsert = ""
                if onConflict:
                    update_assignments = ", ".join(
                        f'"{k}"=excluded."{k}"' for k in keys
                        if k != onConflict)
                    if update_assignments:
                        upsert = (
                            f' ON CONFLICT("{onConflict}") DO UPDATE '
                            f'SET {update_assignments}')
                    else:
                        upsert = (
                            f' ON CONFLICT("{onConflict}") DO NOTHING')
                conn.execute(
                    f'INSERT INTO "{flat}" ({col_list}) '
                    f'VALUES ({placeholders}){upsert}', values)
                inserted.append(dict(row))
            conn.commit()
        except sqlite3.Error as e:
            _record(s, "insert_rows", projectRef=projectRef,
                    table=table, error=str(e))
            _save_state(s)
            raise ValueError(f"SQL error: {e}")
        finally:
            conn.close()
        _record(s, "insert_rows", projectRef=projectRef,
                table=table, schema=schema, count=len(inserted))
        _save_state(s)
        return inserted


@mcp.tool(name="update_rows")
def update_rows(projectRef: str, table: str,
                filters: dict, values: dict,
                schema: str = "public") -> list[dict]:
    """PostgREST: ``PATCH {project_url}/rest/v1/{table}`` — update
    rows matching ``filters`` with the new ``values``. Returns the
    updated rows."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _ensure_all_catalog_tables(projectRef, proj)
        flat = _flat_name(schema, table)
        set_keys = list(values.keys())
        if not set_keys:
            raise ValueError("update_rows requires non-empty `values`")
        set_clause = ", ".join(f'"{k}" = ?' for k in set_keys)
        set_params: list[Any] = []
        for k in set_keys:
            v = values[k]
            if isinstance(v, (list, dict)):
                v = json.dumps(v)
            set_params.append(v)
        where, where_params = _filters_to_where(filters or {})
        conn = _project_conn(projectRef)
        try:
            sql_select = (f'SELECT * FROM "{flat}" {where}').strip()
            existing = [dict(r) for r in
                        conn.execute(sql_select, where_params).fetchall()]
            sql_update = (
                f'UPDATE "{flat}" SET {set_clause} {where}').strip()
            conn.execute(sql_update, set_params + where_params)
            conn.commit()
            # Re-select to capture the new values.
            updated = [dict(r) for r in
                       conn.execute(sql_select, where_params).fetchall()]
        except sqlite3.Error as e:
            _record(s, "update_rows", projectRef=projectRef,
                    table=table, error=str(e))
            _save_state(s)
            raise ValueError(f"SQL error: {e}")
        finally:
            conn.close()
        _record(s, "update_rows", projectRef=projectRef,
                table=table, schema=schema,
                matched=len(existing), updated=len(updated))
        _save_state(s)
        return updated


@mcp.tool(name="delete_rows")
def delete_rows(projectRef: str, table: str,
                filters: dict,
                schema: str = "public") -> list[dict]:
    """PostgREST: ``DELETE {project_url}/rest/v1/{table}`` — delete
    rows matching ``filters``. Returns the deleted rows."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _ensure_all_catalog_tables(projectRef, proj)
        flat = _flat_name(schema, table)
        where, params = _filters_to_where(filters or {})
        if not where:
            raise ValueError(
                "delete_rows requires at least one filter "
                "(refusing full-table delete)")
        conn = _project_conn(projectRef)
        try:
            sql_select = f'SELECT * FROM "{flat}" {where}'.strip()
            to_delete = [dict(r) for r in
                         conn.execute(sql_select, params).fetchall()]
            conn.execute(f'DELETE FROM "{flat}" {where}', params)
            conn.commit()
        except sqlite3.Error as e:
            _record(s, "delete_rows", projectRef=projectRef,
                    table=table, error=str(e))
            _save_state(s)
            raise ValueError(f"SQL error: {e}")
        finally:
            conn.close()
        _record(s, "delete_rows", projectRef=projectRef,
                table=table, schema=schema, deleted=len(to_delete))
        _save_state(s)
        return to_delete


@mcp.tool(name="rpc_call")
def rpc_call(projectRef: str, functionName: str,
             args: dict | None = None) -> dict:
    """PostgREST: ``POST {project_url}/rest/v1/rpc/{function_name}``
    — invoke a Postgres function exposed via PostgREST.

    The mock does not execute PL/pgSQL — it records the call and
    returns ``{ok: true, result: null}`` (or a recorded stub if the
    rpc was previously seeded). Used by agents whose workflow names
    the rpc but where verifier checks the call metadata, not the
    return value."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        rpc_log = proj.setdefault("rpc_calls", [])
        entry = {
            "function": functionName,
            "args": dict(args or {}),
            "ts": _now(),
        }
        rpc_log.append(entry)
        _record(s, "rpc_call", projectRef=projectRef,
                function=functionName)
        _save_state(s)
        return {"ok": True, "result": None, "call": entry}


# ===========================================================================
# Auth API
# ===========================================================================

def _new_auth_user(*, email: str, password: str | None,
                   phone: str | None,
                   email_confirm: bool,
                   user_metadata: dict | None,
                   user_id: str | None = None) -> dict:
    uid = user_id or _gen_uuid()
    now = _now()
    return {
        "id": uid,
        "aud": "authenticated",
        "role": "authenticated",
        "email": email,
        "email_confirmed_at": now if email_confirm else None,
        "phone": phone or "",
        "phone_confirmed_at": None,
        "confirmation_sent_at": None,
        "last_sign_in_at": None,
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": dict(user_metadata or {}),
        "identities": [
            {
                "id": uid,
                "user_id": uid,
                "identity_data": {"email": email, "sub": uid},
                "provider": "email",
                "created_at": now,
                "updated_at": now,
            },
        ],
        "created_at": now,
        "updated_at": now,
    }


@mcp.tool(name="auth_list_users")
def auth_list_users(projectRef: str,
                    page: int = 1,
                    perPage: int = 50) -> dict:
    """Auth API: ``GET {project_url}/auth/v1/admin/users`` — list every
    auth user in the project, paginated."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        users = list((proj.get("auth_users") or {}).values())
        users.sort(key=lambda u: u.get("created_at") or "")
        page = max(1, int(page or 1))
        per_page = max(1, min(int(perPage or 50), 1000))
        start = (page - 1) * per_page
        end = start + per_page
        page_users = users[start:end]
        _record(s, "auth_list_users", projectRef=projectRef,
                count=len(page_users))
        _save_state(s)
        return {"users": page_users, "aud": "authenticated"}


@mcp.tool(name="auth_get_user")
def auth_get_user(projectRef: str, userId: str) -> dict:
    """Auth API: ``GET {project_url}/auth/v1/admin/users/{user_id}`` —
    fetch one user by their UUID."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        user = (proj.get("auth_users") or {}).get(userId)
        if not user:
            _record(s, "auth_get_user", projectRef=projectRef,
                    userId=userId, result="not_found")
            _save_state(s)
            raise ValueError(f"Auth user not found: {userId}")
        _record(s, "auth_get_user", projectRef=projectRef,
                userId=userId)
        _save_state(s)
        return user


@mcp.tool(name="auth_create_user")
def auth_create_user(projectRef: str,
                     email: str,
                     password: str | None = None,
                     phone: str | None = None,
                     email_confirm: bool = True,
                     user_metadata: dict | None = None) -> dict:
    """Auth API: ``POST {project_url}/auth/v1/admin/users`` — create a
    new auth user. The mock auto-confirms email unless
    ``email_confirm=False``."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        users = proj.setdefault("auth_users", {})
        for existing in users.values():
            if existing.get("email") == email:
                _record(s, "auth_create_user", projectRef=projectRef,
                        email=email, result="duplicate")
                _save_state(s)
                raise ValueError(f"User already exists: {email}")
        user = _new_auth_user(email=email, password=password,
                              phone=phone,
                              email_confirm=email_confirm,
                              user_metadata=user_metadata)
        users[user["id"]] = user
        _record(s, "auth_create_user", projectRef=projectRef,
                userId=user["id"], email=email)
        _save_state(s)
        return user


@mcp.tool(name="auth_update_user")
def auth_update_user(projectRef: str, userId: str,
                     email: str | None = None,
                     password: str | None = None,
                     phone: str | None = None,
                     user_metadata: dict | None = None,
                     email_confirmed: bool | None = None) -> dict:
    """Auth API: ``PUT {project_url}/auth/v1/admin/users/{user_id}`` —
    partial update of an auth user."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        user = (proj.get("auth_users") or {}).get(userId)
        if not user:
            _record(s, "auth_update_user", projectRef=projectRef,
                    userId=userId, result="not_found")
            _save_state(s)
            raise ValueError(f"Auth user not found: {userId}")
        if email is not None:
            user["email"] = email
            for ident in user.get("identities") or []:
                ident.setdefault("identity_data", {})["email"] = email
        if phone is not None:
            user["phone"] = phone
        if user_metadata is not None:
            user["user_metadata"] = dict(user_metadata)
        if email_confirmed is True:
            user["email_confirmed_at"] = _now()
        elif email_confirmed is False:
            user["email_confirmed_at"] = None
        user["updated_at"] = _now()
        _record(s, "auth_update_user", projectRef=projectRef,
                userId=userId)
        _save_state(s)
        return user


@mcp.tool(name="auth_delete_user")
def auth_delete_user(projectRef: str, userId: str) -> dict:
    """Auth API: ``DELETE {project_url}/auth/v1/admin/users/{user_id}``
    — delete an auth user."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        users = proj.get("auth_users") or {}
        if userId not in users:
            _record(s, "auth_delete_user", projectRef=projectRef,
                    userId=userId, result="not_found")
            _save_state(s)
            raise ValueError(f"Auth user not found: {userId}")
        del users[userId]
        _record(s, "auth_delete_user", projectRef=projectRef,
                userId=userId)
        _save_state(s)
        return {"id": userId, "deleted": True}


# ===========================================================================
# Storage API
# ===========================================================================

def _new_bucket(*, name: str, public: bool,
                file_size_limit: int | None,
                allowed_mime_types: list[str] | None,
                owner: str | None) -> dict:
    now = _now()
    return {
        "info": {
            "id": name,
            "name": name,
            "owner": owner,
            "public": bool(public),
            "file_size_limit": file_size_limit,
            "allowed_mime_types": list(allowed_mime_types or []) or None,
            "created_at": now,
            "updated_at": now,
        },
        "objects": {},
    }


def _public_bucket(bucket: dict) -> dict:
    return dict(bucket.get("info") or {})


@mcp.tool(name="storage_list_buckets")
def storage_list_buckets(projectRef: str) -> list[dict]:
    """Storage API: ``GET {project_url}/storage/v1/bucket`` — list all
    buckets in the project."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        out = [_public_bucket(b)
               for b in (proj.get("storage_buckets") or {}).values()]
        out.sort(key=lambda b: b.get("created_at") or "")
        _record(s, "storage_list_buckets", projectRef=projectRef,
                count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="storage_create_bucket")
def storage_create_bucket(projectRef: str, name: str,
                          public: bool = False,
                          fileSizeLimit: int | None = None,
                          allowedMimeTypes: list[str] | None = None
                          ) -> dict:
    """Storage API: ``POST {project_url}/storage/v1/bucket`` — create a
    bucket. ``public=True`` makes objects readable via
    ``storage_get_public_url`` without a signed token."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        buckets = proj.setdefault("storage_buckets", {})
        if name in buckets:
            _record(s, "storage_create_bucket",
                    projectRef=projectRef, name=name,
                    result="duplicate")
            _save_state(s)
            raise ValueError(f"Bucket already exists: {name}")
        buckets[name] = _new_bucket(
            name=name, public=public,
            file_size_limit=fileSizeLimit,
            allowed_mime_types=allowedMimeTypes,
            owner=None)
        _record(s, "storage_create_bucket", projectRef=projectRef,
                name=name, public=bool(public))
        _save_state(s)
        return _public_bucket(buckets[name])


@mcp.tool(name="storage_get_bucket")
def storage_get_bucket(projectRef: str, bucket: str) -> dict:
    """Storage API: ``GET {project_url}/storage/v1/bucket/{bucket}`` —
    retrieve a bucket's metadata."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_get_bucket", projectRef=projectRef,
                    bucket=bucket, result="not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        _record(s, "storage_get_bucket", projectRef=projectRef,
                bucket=bucket)
        _save_state(s)
        return _public_bucket(b)


@mcp.tool(name="storage_delete_bucket")
def storage_delete_bucket(projectRef: str, bucket: str) -> dict:
    """Storage API: ``DELETE {project_url}/storage/v1/bucket/{bucket}``
    — delete a bucket and its objects."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        buckets = proj.get("storage_buckets") or {}
        if bucket not in buckets:
            _record(s, "storage_delete_bucket",
                    projectRef=projectRef, bucket=bucket,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        del buckets[bucket]
        _record(s, "storage_delete_bucket", projectRef=projectRef,
                bucket=bucket)
        _save_state(s)
        return {"name": bucket, "deleted": True}


@mcp.tool(name="storage_list_objects")
def storage_list_objects(projectRef: str, bucket: str,
                         prefix: str = "",
                         limit: int = 100,
                         offset: int = 0,
                         sortBy: dict | None = None) -> list[dict]:
    """Storage API: ``POST {project_url}/storage/v1/object/list/{bucket}``
    — list objects in a bucket. ``prefix`` filters by path prefix;
    ``sortBy = {column: "name"|"created_at"|"updated_at",
    order: "asc"|"desc"}``."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_list_objects", projectRef=projectRef,
                    bucket=bucket, result="not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        objects = [
            {k: v for k, v in obj.items() if k != "content"}
            for obj in (b.get("objects") or {}).values()
        ]
        if prefix:
            objects = [o for o in objects
                       if (o.get("name") or "").startswith(prefix)]
        sort_col = "name"
        sort_dir = "asc"
        if isinstance(sortBy, dict):
            sort_col = sortBy.get("column", "name") or "name"
            sort_dir = (sortBy.get("order", "asc") or "asc").lower()
        objects.sort(key=lambda o: o.get(sort_col) or "",
                     reverse=(sort_dir == "desc"))
        page = objects[int(offset or 0):
                       int(offset or 0) + int(limit or 100)]
        _record(s, "storage_list_objects", projectRef=projectRef,
                bucket=bucket, prefix=prefix, count=len(page))
        _save_state(s)
        return page


@mcp.tool(name="storage_upload_object")
def storage_upload_object(projectRef: str, bucket: str, path: str,
                          content: str,
                          contentType: str = "application/octet-stream"
                          ) -> dict:
    """Storage API: ``POST {project_url}/storage/v1/object/{bucket}/{path}``
    — upload an object. The mock stores ``content`` inline as a string
    (the runner uses small text payloads). Returns ``{Key, Id}``."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_upload_object", projectRef=projectRef,
                    bucket=bucket, path=path, result="bucket_not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        objects = b.setdefault("objects", {})
        now = _now()
        existing = objects.get(path)
        obj_id = existing.get("id") if existing else _gen_uuid()
        size = len((content or "").encode("utf-8"))
        etag = secrets.token_hex(16)
        obj = {
            "id": obj_id,
            "name": path,
            "bucket_id": bucket,
            "owner": None,
            "content": content or "",
            "content_type": contentType,
            "size": size,
            "etag": etag,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
            "last_accessed_at": now,
            "metadata": {"mimetype": contentType, "size": size},
        }
        objects[path] = obj
        _record(s, "storage_upload_object", projectRef=projectRef,
                bucket=bucket, path=path, size=size, replaced=bool(existing))
        _save_state(s)
        return {
            "Key": f"{bucket}/{path}",
            "Id": obj_id,
        }


@mcp.tool(name="storage_get_object")
def storage_get_object(projectRef: str, bucket: str, path: str
                       ) -> dict:
    """Storage API: ``GET {project_url}/storage/v1/object/{bucket}/{path}``
    — read an object's content + metadata."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_get_object", projectRef=projectRef,
                    bucket=bucket, path=path, result="bucket_not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        obj = (b.get("objects") or {}).get(path)
        if not obj:
            _record(s, "storage_get_object", projectRef=projectRef,
                    bucket=bucket, path=path, result="not_found")
            _save_state(s)
            raise ValueError(f"Object not found: {path}")
        _record(s, "storage_get_object", projectRef=projectRef,
                bucket=bucket, path=path)
        _save_state(s)
        return {
            "content": obj.get("content", ""),
            "contentType": obj.get("content_type"),
            "size": obj.get("size"),
            "etag": obj.get("etag"),
            "lastModified": obj.get("updated_at"),
        }


@mcp.tool(name="storage_delete_object")
def storage_delete_object(projectRef: str, bucket: str,
                          paths: list[str]) -> list[dict]:
    """Storage API: ``DELETE {project_url}/storage/v1/object/{bucket}/{path}``
    — delete one or more objects."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_delete_object",
                    projectRef=projectRef, bucket=bucket,
                    result="bucket_not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        objects = b.setdefault("objects", {})
        deleted: list[dict] = []
        for p in paths or []:
            obj = objects.pop(p, None)
            if obj is not None:
                deleted.append({k: v for k, v in obj.items()
                                if k != "content"})
        _record(s, "storage_delete_object", projectRef=projectRef,
                bucket=bucket, deleted=len(deleted))
        _save_state(s)
        return deleted


@mcp.tool(name="storage_get_public_url")
def storage_get_public_url(projectRef: str, bucket: str, path: str
                           ) -> dict:
    """Storage API: ``GET {project_url}/storage/v1/object/public/{bucket}/{path}``
    — return the canonical public URL for an object."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        info = proj["info"]
        url = (f"{info['project_url']}/storage/v1/object/public/"
               f"{bucket}/{path}")
        _record(s, "storage_get_public_url", projectRef=projectRef,
                bucket=bucket, path=path)
        _save_state(s)
        return {"publicUrl": url}


@mcp.tool(name="storage_get_signed_url")
def storage_get_signed_url(projectRef: str, bucket: str, path: str,
                           expiresIn: int = 3600) -> dict:
    """Storage API: ``POST {project_url}/storage/v1/object/sign/{bucket}/{path}``
    — issue a time-limited signed URL for an object. The mock returns
    a synthetic JWT-shaped token."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        b = (proj.get("storage_buckets") or {}).get(bucket)
        if not b:
            _record(s, "storage_get_signed_url",
                    projectRef=projectRef, bucket=bucket,
                    path=path, result="bucket_not_found")
            _save_state(s)
            raise ValueError(f"Bucket not found: {bucket}")
        token = _gen_api_key(kind="signed-url")
        url = (f"{proj['info']['project_url']}/storage/v1/object/sign/"
               f"{bucket}/{path}?token={token}")
        _record(s, "storage_get_signed_url", projectRef=projectRef,
                bucket=bucket, path=path, expiresIn=int(expiresIn))
        _save_state(s)
        return {"signedUrl": url, "token": token}


# ===========================================================================
# Edge Functions API
# ===========================================================================

def _new_function(*, slug: str, name: str, body: str,
                  verify_jwt: bool, version: int = 1) -> dict:
    now = _now()
    return {
        "id": _gen_uuid(),
        "slug": slug,
        "name": name,
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
        "version": version,
        "verify_jwt": bool(verify_jwt),
        "body": body,
    }


def _public_function(fn: dict) -> dict:
    return {k: v for k, v in fn.items() if k != "body"}


@mcp.tool(name="list_functions")
def list_functions(projectRef: str) -> list[dict]:
    """Edge Functions: ``GET /v1/projects/{ref}/functions`` — list the
    project's deployed edge functions (without body)."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        out = [_public_function(fn)
               for fn in (proj.get("functions") or {}).values()]
        out.sort(key=lambda fn: fn.get("created_at") or "")
        _record(s, "list_functions", projectRef=projectRef,
                count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_function")
def get_function(projectRef: str, slug: str) -> dict:
    """Edge Functions: ``GET /v1/projects/{ref}/functions/{slug}`` —
    retrieve one edge function including its Deno source body."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        fn = (proj.get("functions") or {}).get(slug)
        if not fn:
            _record(s, "get_function", projectRef=projectRef,
                    slug=slug, result="not_found")
            _save_state(s)
            raise ValueError(f"Function not found: {slug}")
        _record(s, "get_function", projectRef=projectRef, slug=slug)
        _save_state(s)
        return dict(fn)


@mcp.tool(name="deploy_function")
def deploy_function(projectRef: str, slug: str, name: str,
                    body: str, verifyJwt: bool = True) -> dict:
    """Edge Functions: ``POST /v1/projects/{ref}/functions`` — deploy
    (create or update) an edge function. Bumps ``version`` on update.
    """
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        funcs = proj.setdefault("functions", {})
        existing = funcs.get(slug)
        version = (int(existing.get("version", 0)) + 1
                   if existing else 1)
        fn = _new_function(slug=slug, name=name, body=body,
                           verify_jwt=verifyJwt, version=version)
        if existing:
            fn["id"] = existing.get("id") or fn["id"]
            fn["created_at"] = existing.get("created_at",
                                            fn["created_at"])
        funcs[slug] = fn
        _record(s, "deploy_function", projectRef=projectRef,
                slug=slug, version=version,
                replaced=bool(existing))
        _save_state(s)
        return _public_function(fn)


@mcp.tool(name="delete_function")
def delete_function(projectRef: str, slug: str) -> dict:
    """Edge Functions: ``DELETE /v1/projects/{ref}/functions/{slug}``
    — delete a deployed edge function."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        funcs = proj.get("functions") or {}
        if slug not in funcs:
            _record(s, "delete_function", projectRef=projectRef,
                    slug=slug, result="not_found")
            _save_state(s)
            raise ValueError(f"Function not found: {slug}")
        del funcs[slug]
        _record(s, "delete_function", projectRef=projectRef,
                slug=slug)
        _save_state(s)
        return {"slug": slug, "deleted": True}


@mcp.tool(name="invoke_function")
def invoke_function(projectRef: str, slug: str,
                    body: dict | None = None,
                    headers: dict | None = None) -> dict:
    """Edge Functions: ``POST /v1/projects/{ref}/functions/{slug}/invoke``
    — invoke a deployed function. The mock does not execute the Deno
    body; it records the invocation and returns a stub
    ``{status: 200, body: {ok: true, slug, echo: body}}`` so the agent
    can chain on it."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        fn = (proj.get("functions") or {}).get(slug)
        if not fn:
            _record(s, "invoke_function", projectRef=projectRef,
                    slug=slug, result="not_found")
            _save_state(s)
            raise ValueError(f"Function not found: {slug}")
        invocations = proj.setdefault("function_invocations", [])
        entry = {
            "slug": slug,
            "body": dict(body or {}),
            "headers": dict(headers or {}),
            "ts": _now(),
        }
        invocations.append(entry)
        _record(s, "invoke_function", projectRef=projectRef,
                slug=slug)
        _save_state(s)
        return {
            "status": 200,
            "body": {"ok": True, "slug": slug,
                     "echo": dict(body or {})},
        }


# ===========================================================================
# Logs API
# ===========================================================================

@mcp.tool(name="get_logs")
def get_logs(projectRef: str, service: str = "api",
             limit: int = 100) -> list[dict]:
    """Logs API: ``GET /v1/projects/{ref}/analytics/endpoints/logs.all``
    — fetch the most recent log lines for a service in
    ``{api, auth, storage, realtime, postgres, functions}``. The mock
    stores log entries seeded via ``mock_debug_state``-adjacent
    fixtures (or written here by the runner) and returns the newest
    ``limit`` rows."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        rows = [entry for entry in (proj.get("logs") or [])
                if entry.get("service") == service or not service]
        rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        out = rows[: int(limit or 100)]
        _record(s, "get_logs", projectRef=projectRef,
                service=service, count=len(out))
        _save_state(s)
        return out


# ===========================================================================
# Mock-only debug tools
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state JSON for verifier
    introspection. Not part of the upstream Supabase API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_project")
def mock_debug_seed_project(ref: str | None = None,
                            name: str = "Mock Project",
                            organization_id: str | None = None,
                            region: str = "us-east-1",
                            status: str = "ACTIVE_HEALTHY",
                            db_password: str = "password",
                            anon_key: str | None = None,
                            service_role_key: str | None = None
                            ) -> dict:
    """Mock-only: seed (or replace) a project record. Used by
    ``synth/mock_seed/supabase.py`` recipes."""
    with _lock():
        s = _load_state()
        if not organization_id:
            organization_id = next(iter(s["organizations"].keys()))
        if organization_id not in s["organizations"]:
            raise ValueError(
                f"Organization not found: {organization_id}")
        proj_ref = ref or _gen_ref()
        proj = _new_project(name=name,
                            organization_id=organization_id,
                            region=region,
                            db_password=db_password,
                            status=status, ref=proj_ref)
        if anon_key:
            proj["info"]["anon_key"] = anon_key
        if service_role_key:
            proj["info"]["service_role_key"] = service_role_key
        s["projects"][proj_ref] = proj
        _ensure_project_db(proj_ref)
        _record(s, "debug_seed_project", ref=proj_ref, name=name)
        _save_state(s)
        return _public_project(proj)


@mcp.tool(name="mock_debug_seed_organization")
def mock_debug_seed_organization(id: str | None = None,
                                 name: str = "Mock Org",
                                 slug: str | None = None) -> dict:
    """Mock-only: add (or replace) an organization."""
    with _lock():
        s = _load_state()
        oid = id or _gen_org_id()
        s["organizations"][oid] = {
            "id": oid,
            "name": name,
            "slug": slug or oid,
        }
        _record(s, "debug_seed_organization", id=oid, name=name)
        _save_state(s)
        return s["organizations"][oid]


@mcp.tool(name="mock_debug_seed_table")
def mock_debug_seed_table(projectRef: str,
                          table: str,
                          columns: list[dict],
                          schema: str = "public",
                          primary_key: list[str] | None = None
                          ) -> dict:
    """Mock-only: register a table in the catalog AND create it in the
    project's sqlite. ``columns`` is
    ``[{name, type, nullable?, default?, primary_key?, foreign_key?}]``.
    """
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        cols = list(columns or [])
        for c in cols:
            if primary_key and c["name"] in primary_key:
                c["primary_key"] = True
        _register_table(proj, schema=schema, table=table, columns=cols)
        _materialize_table(projectRef, schema=schema, table=table,
                           columns=cols)
        _record(s, "debug_seed_table", projectRef=projectRef,
                schema=schema, table=table, columns=len(cols))
        _save_state(s)
        return {"projectRef": projectRef, "schema": schema,
                "table": table, "columns": len(cols)}


@mcp.tool(name="mock_debug_seed_rows")
def mock_debug_seed_rows(projectRef: str, table: str,
                         rows: list[dict],
                         schema: str = "public") -> dict:
    """Mock-only: bulk-insert rows into a previously-seeded table.
    Bypasses PostgREST filter parsing and constraint checks."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        _ensure_all_catalog_tables(projectRef, proj)
        flat = _flat_name(schema, table)
        conn = _project_conn(projectRef)
        n = 0
        try:
            for row in rows or []:
                if not isinstance(row, dict) or not row:
                    continue
                keys = list(row.keys())
                placeholders = ", ".join("?" for _ in keys)
                col_list = ", ".join(f'"{k}"' for k in keys)
                values: list[Any] = []
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
        _record(s, "debug_seed_rows", projectRef=projectRef,
                schema=schema, table=table, inserted=n)
        _save_state(s)
        return {"inserted": n}


@mcp.tool(name="mock_debug_seed_user")
def mock_debug_seed_user(projectRef: str,
                         email: str,
                         id: str | None = None,
                         role: str = "authenticated",
                         email_confirm: bool = True,
                         user_metadata: dict | None = None) -> dict:
    """Mock-only: insert an auth user fixture."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        users = proj.setdefault("auth_users", {})
        user = _new_auth_user(email=email, password=None, phone=None,
                              email_confirm=email_confirm,
                              user_metadata=user_metadata,
                              user_id=id)
        user["role"] = role
        users[user["id"]] = user
        _record(s, "debug_seed_user", projectRef=projectRef,
                userId=user["id"], email=email)
        _save_state(s)
        return user


@mcp.tool(name="mock_debug_seed_bucket")
def mock_debug_seed_bucket(projectRef: str, name: str,
                           public: bool = False,
                           file_size_limit: int | None = None,
                           allowed_mime_types: list[str] | None = None,
                           owner: str | None = None) -> dict:
    """Mock-only: insert a storage bucket fixture (idempotent)."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        buckets = proj.setdefault("storage_buckets", {})
        if name in buckets:
            buckets[name]["info"].update({
                "public": bool(public),
                "file_size_limit": file_size_limit,
                "allowed_mime_types":
                    list(allowed_mime_types or []) or None,
                "owner": owner,
            })
        else:
            buckets[name] = _new_bucket(
                name=name, public=public,
                file_size_limit=file_size_limit,
                allowed_mime_types=allowed_mime_types,
                owner=owner)
        _record(s, "debug_seed_bucket", projectRef=projectRef,
                name=name)
        _save_state(s)
        return _public_bucket(buckets[name])


@mcp.tool(name="mock_debug_seed_object")
def mock_debug_seed_object(projectRef: str, bucket: str, path: str,
                           content: str = "",
                           content_type: str = "application/octet-stream"
                           ) -> dict:
    """Mock-only: insert a storage object fixture (creates the bucket
    if absent)."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        buckets = proj.setdefault("storage_buckets", {})
        b = buckets.setdefault(bucket, _new_bucket(
            name=bucket, public=False, file_size_limit=None,
            allowed_mime_types=None, owner=None))
        objects = b.setdefault("objects", {})
        size = len((content or "").encode("utf-8"))
        now = _now()
        existing = objects.get(path)
        obj_id = existing.get("id") if existing else _gen_uuid()
        objects[path] = {
            "id": obj_id,
            "name": path,
            "bucket_id": bucket,
            "owner": None,
            "content": content or "",
            "content_type": content_type,
            "size": size,
            "etag": secrets.token_hex(16),
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
            "last_accessed_at": now,
            "metadata": {"mimetype": content_type, "size": size},
        }
        _record(s, "debug_seed_object", projectRef=projectRef,
                bucket=bucket, path=path, size=size)
        _save_state(s)
        return {"id": obj_id, "name": path, "bucket": bucket}


@mcp.tool(name="mock_debug_seed_function")
def mock_debug_seed_function(projectRef: str, slug: str,
                             name: str,
                             body: str = "",
                             verify_jwt: bool = True,
                             version: int = 1) -> dict:
    """Mock-only: insert an edge function fixture."""
    with _lock():
        s = _load_state()
        proj = _require_project(s, projectRef)
        funcs = proj.setdefault("functions", {})
        fn = _new_function(slug=slug, name=name, body=body,
                           verify_jwt=verify_jwt, version=version)
        funcs[slug] = fn
        _record(s, "debug_seed_function", projectRef=projectRef,
                slug=slug)
        _save_state(s)
        return _public_function(fn)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    with _lock():
        _load_state()
    mcp.run()


if __name__ == "__main__":
    main()
