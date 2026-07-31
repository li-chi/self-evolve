"""Airtable mock MCP server.

Mirrors `@felores/airtable-mcp-server` (Toolathlon's official `airtable`
server, source: github.com/felores/airtable-mcp). That server is a thin
wrapper over the Airtable REST API + Metadata API; every tool here
accepts the same arguments and returns the same JSON shape as the
underlying endpoint.

Tool surface (12, matches upstream verbatim):

  list_bases, list_tables, create_table, update_table,
  create_field, update_field,
  list_records, get_record, create_record, update_record,
  delete_record, search_records

Plus two mock-only debug tools (`mock_debug_state`,
`mock_debug_seed_base`).

State is a single JSON file at `$AIRTABLE_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/airtable_mock`). Every call appends to
`state["calls"]` for verifier consumption.

Response shapes (per upstream wrapper + Airtable REST):

  list_bases       -> [{"id":"appXXX","name":"...","permissionLevel":"..."}]
                      (upstream unwraps `.data.bases`)
  list_tables      -> [{"id":"tblXXX","name":"...","primaryFieldId":"fldXXX",
                        "fields":[...],"views":[...]}]
                      (upstream unwraps `.data.tables`)
  create_table     -> full table object
  update_table     -> full table object
  create_field     -> field object
  update_field     -> field object
  list_records     -> [{"id":"recXXX","createdTime":"...","fields":{...}}]
                      (upstream unwraps `.data.records`)
  get_record       -> single record object
  create_record    -> single record object {"id","createdTime","fields"}
  update_record    -> single record object
  delete_record    -> {"id":"recXXX","deleted":true}
  search_records   -> records list (filterByFormula behind the scenes)

Errors follow the Airtable REST shape:
  {"error":{"type":"NOT_FOUND","message":"..."}}
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# Field types and required-options behavior — mirrors src/types.ts in the
# upstream felores/airtable-mcp server.
FIELD_TYPES = {
    "singleLineText", "multilineText", "number", "singleSelect",
    "multiSelect", "date", "checkbox", "email", "phoneNumber",
    "currency",
}


def _field_requires_options(t: str) -> bool:
    return t in {"number", "singleSelect", "multiSelect", "date", "currency"}


def _default_field_options(t: str) -> dict | None:
    if t == "number":
        return {"precision": 0}
    if t == "date":
        return {"dateFormat": {"name": "local"}}
    if t == "currency":
        return {"precision": 2, "symbol": "$"}
    return None


# ---------------------------------------------------------------------------
# State storage
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "AIRTABLE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/airtable_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _empty_state() -> dict:
    return {
        "bases": {},
        "next_id": {"app": 1, "tbl": 1, "fld": 1, "rec": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("AIRTABLE_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# ID generation — Airtable uses prefix + 14 base-62 chars (e.g. appXXXXXXXXXXXXXX)
# ---------------------------------------------------------------------------

_ID_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)


def _gen_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ID_ALPHABET) for _ in range(14))


# ---------------------------------------------------------------------------
# Error shape — Airtable REST: {"error":{"type":"NOT_FOUND","message":"..."}}
# ---------------------------------------------------------------------------

def _err(error_type: str, message: str) -> dict:
    return {"error": {"type": error_type, "message": message}}


# ---------------------------------------------------------------------------
# Resolver helpers — Airtable accepts either a table id OR a table name in
# `/{baseId}/{tableIdOrName}` (the data endpoints, not /meta/).
# ---------------------------------------------------------------------------

def _resolve_table(base: dict, table_id_or_name: str) -> dict | None:
    tables = base.get("tables", {})
    if table_id_or_name in tables:
        return tables[table_id_or_name]
    for tbl in tables.values():
        if tbl.get("name") == table_id_or_name:
            return tbl
    return None


def _record_strip(rec: dict) -> dict:
    """Project a stored record into the wire shape."""
    return {
        "id": rec["id"],
        "createdTime": rec["createdTime"],
        "fields": dict(rec.get("fields", {})),
    }


# ---------------------------------------------------------------------------
# filterByFormula — minimal subset sufficient for search_records, which
# always emits `{field} = "value"`. We also support a handful of common
# patterns so seed tasks that hand-craft formulas don't bounce.
# ---------------------------------------------------------------------------

_FORMULA_EQ_RE = re.compile(r'^\s*\{([^}]+)\}\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')
_FORMULA_NUM_RE = re.compile(r'^\s*\{([^}]+)\}\s*=\s*(-?\d+(?:\.\d+)?)\s*$')
_FORMULA_FIND_RE = re.compile(
    r'^\s*FIND\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*\{([^}]+)\}\s*\)\s*(?:>\s*0)?\s*$'
)


def _match_formula(rec: dict, formula: str) -> bool:
    fields = rec.get("fields", {})
    m = _FORMULA_EQ_RE.match(formula)
    if m:
        name, val = m.group(1), m.group(2).encode().decode("unicode_escape")
        v = fields.get(name)
        return str(v) == val if v is not None else (val == "")
    m = _FORMULA_NUM_RE.match(formula)
    if m:
        name, val = m.group(1), float(m.group(2))
        v = fields.get(name)
        try:
            return float(v) == val
        except (TypeError, ValueError):
            return False
    m = _FORMULA_FIND_RE.match(formula)
    if m:
        needle, name = m.group(1), m.group(2)
        v = fields.get(name)
        return isinstance(v, str) and needle in v
    # Unsupported / unknown formula → conservatively no match.
    return False


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------

def _validate_field(field: dict) -> dict:
    """Mirror src/index.ts validateField: strip options on field types that
    don't need them, populate defaults for those that do."""
    t = field.get("type")
    if not _field_requires_options(t):
        return {k: v for k, v in field.items() if k != "options"}
    if "options" not in field or field["options"] is None:
        out = dict(field)
        defaults = _default_field_options(t)
        if defaults is not None:
            out["options"] = defaults
        return out
    return dict(field)


def _make_field(state: dict, field_in: dict) -> dict:
    f = _validate_field(dict(field_in))
    fid = _gen_id("fld")
    return {
        "id": fid,
        "name": f.get("name", ""),
        "type": f.get("type", "singleLineText"),
        **({"description": f["description"]}
           if f.get("description") is not None else {}),
        **({"options": f["options"]} if "options" in f else {}),
    }


def _make_table(state: dict, name: str, description: str | None,
                fields_in: list | None) -> dict:
    tid = _gen_id("tbl")
    fields: list[dict] = []
    for f in (fields_in or []):
        fields.append(_make_field(state, f))
    if not fields:
        # Airtable requires at least one field. Default to a primary Name
        # field of type singleLineText.
        fields.append(_make_field(
            state, {"name": "Name", "type": "singleLineText"}))
    primary = fields[0]["id"]
    return {
        "id": tid,
        "name": name,
        "description": description or "",
        "primaryFieldId": primary,
        "fields": fields,
        "views": [{"id": _gen_id("viw"), "name": "Grid view",
                   "type": "grid"}],
        "records": {},  # internal: dict keyed by record id
    }


def _table_meta(tbl: dict) -> dict:
    """Project a stored table into the meta-API wire shape (no records)."""
    out = {
        "id": tbl["id"],
        "name": tbl["name"],
        "primaryFieldId": tbl["primaryFieldId"],
        "fields": list(tbl["fields"]),
        "views": list(tbl.get("views", [])),
    }
    if tbl.get("description"):
        out["description"] = tbl["description"]
    return out


mcp = FastMCP("airtable-mock")


# ---------------------------------------------------------------------------
# Meta — bases and tables
# ---------------------------------------------------------------------------

@mcp.tool(name="list_bases")
def list_bases() -> list:
    """Airtable Meta REST: GET /v0/meta/bases — list all accessible bases.

    Upstream `felores/airtable-mcp` returns `response.data.bases`, so this
    tool returns the array directly (not the `{bases: [...]}` envelope).
    """
    with _lock():
        s = _load_state()
        bases = [
            {"id": b["id"], "name": b["name"],
             "permissionLevel": b.get("permissionLevel", "create")}
            for b in s["bases"].values()
        ]
        _record(s, "list_bases", count=len(bases))
        _save_state(s)
        return bases


@mcp.tool(name="list_tables")
def list_tables(base_id: str) -> list | dict:
    """Airtable Meta REST: GET /v0/meta/bases/{base_id}/tables.

    Upstream returns `response.data.tables` (array). On unknown base the
    real API returns an Airtable error body — same shape here.
    """
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            _record(s, "list_tables", base_id=base_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tables = [_table_meta(t) for t in base["tables"].values()]
        _record(s, "list_tables", base_id=base_id, count=len(tables))
        _save_state(s)
        return tables


@mcp.tool(name="create_table")
def create_table(base_id: str, table_name: str,
                 description: str | None = None,
                 fields: list | None = None) -> dict:
    """Airtable Meta REST: POST /v0/meta/bases/{base_id}/tables — create a
    new table. Returns the full table object (with generated `tblXXX`,
    `fldXXX`, and one default view)."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            _record(s, "create_table", base_id=base_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        if any(t["name"] == table_name for t in base["tables"].values()):
            _record(s, "create_table", base_id=base_id, table_name=table_name,
                    result="duplicate")
            _save_state(s)
            return _err("INVALID_REQUEST_UNKNOWN",
                        f"Duplicate table name: {table_name}")
        for f in (fields or []):
            t = f.get("type")
            if t not in FIELD_TYPES:
                _record(s, "create_table", base_id=base_id,
                        table_name=table_name, result="invalid_field_type",
                        type=t)
                _save_state(s)
                return _err("INVALID_REQUEST_UNKNOWN",
                            f"Unknown field type: {t!r}")
        tbl = _make_table(s, table_name, description, fields)
        base["tables"][tbl["id"]] = tbl
        _record(s, "create_table", base_id=base_id,
                table_id=tbl["id"], table_name=table_name)
        _save_state(s)
        return _table_meta(tbl)


@mcp.tool(name="update_table")
def update_table(base_id: str, table_id: str,
                 name: str | None = None,
                 description: str | None = None) -> dict:
    """Airtable Meta REST: PATCH /v0/meta/bases/{base_id}/tables/{table_id}
    — update a table's name/description. Returns the updated table."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            _record(s, "update_table", base_id=base_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = base["tables"].get(table_id)
        if not tbl:
            _record(s, "update_table", base_id=base_id, table_id=table_id,
                    result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find table with ID: {table_id}")
        if name is not None:
            tbl["name"] = name
        if description is not None:
            tbl["description"] = description
        _record(s, "update_table", base_id=base_id, table_id=table_id,
                fields=[k for k in ("name", "description")
                        if locals().get(k) is not None])
        _save_state(s)
        return _table_meta(tbl)


@mcp.tool(name="create_field")
def create_field(base_id: str, table_id: str, field: dict) -> dict:
    """Airtable Meta REST: POST /v0/meta/bases/{base_id}/tables/{table_id}/fields
    — create a new field on a table. Returns the created field object."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            _record(s, "create_field", base_id=base_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = base["tables"].get(table_id)
        if not tbl:
            _record(s, "create_field", base_id=base_id, table_id=table_id,
                    result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find table with ID: {table_id}")
        t = field.get("type")
        if t not in FIELD_TYPES:
            return _err("INVALID_REQUEST_UNKNOWN",
                        f"Unknown field type: {t!r}")
        if any(f["name"] == field.get("name") for f in tbl["fields"]):
            return _err("INVALID_REQUEST_UNKNOWN",
                        f"Duplicate field name: {field.get('name')}")
        new_field = _make_field(s, field)
        tbl["fields"].append(new_field)
        _record(s, "create_field", base_id=base_id, table_id=table_id,
                field_id=new_field["id"], field_name=new_field["name"])
        _save_state(s)
        return new_field


@mcp.tool(name="update_field")
def update_field(base_id: str, table_id: str, field_id: str,
                 updates: dict) -> dict:
    """Airtable Meta REST: PATCH /v0/meta/bases/{base_id}/tables/{table_id}/fields/{field_id}.
    Supports updating `name`, `description`, and `options`."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = base["tables"].get(table_id)
        if not tbl:
            return _err("NOT_FOUND",
                        f"Could not find table with ID: {table_id}")
        target = None
        for f in tbl["fields"]:
            if f["id"] == field_id:
                target = f
                break
        if target is None:
            _record(s, "update_field", base_id=base_id, table_id=table_id,
                    field_id=field_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find field with ID: {field_id}")
        for k in ("name", "description", "options"):
            if k in (updates or {}):
                target[k] = updates[k]
        _record(s, "update_field", base_id=base_id, table_id=table_id,
                field_id=field_id, keys=list((updates or {}).keys()))
        _save_state(s)
        return dict(target)


# ---------------------------------------------------------------------------
# Records — the data API
# ---------------------------------------------------------------------------

@mcp.tool(name="list_records")
def list_records(base_id: str, table_name: str,
                 max_records: int | None = None) -> list | dict:
    """Airtable REST: GET /v0/{base_id}/{table_name_or_id} — list records.

    Upstream returns `response.data.records` (array). `table_name` accepts
    either the table name or the `tblXXX` id, per Airtable's data endpoint.
    """
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            _record(s, "list_records", base_id=base_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            _record(s, "list_records", base_id=base_id, table=table_name,
                    result="table_not_found")
            _save_state(s)
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        records = [_record_strip(r) for r in tbl["records"].values()]
        if max_records is not None:
            records = records[: int(max_records)]
        _record(s, "list_records", base_id=base_id, table=table_name,
                count=len(records))
        _save_state(s)
        return records


@mcp.tool(name="get_record")
def get_record(base_id: str, table_name: str, record_id: str) -> dict:
    """Airtable REST: GET /v0/{base_id}/{table_name}/{record_id} — fetch a
    single record."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            _record(s, "get_record", base_id=base_id, table=table_name,
                    result="table_not_found")
            _save_state(s)
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        rec = tbl["records"].get(record_id)
        _record(s, "get_record", base_id=base_id, table=table_name,
                record_id=record_id, result="ok" if rec else "not_found")
        _save_state(s)
        if not rec:
            return _err("NOT_FOUND",
                        f"Record not found: {record_id}")
        return _record_strip(rec)


@mcp.tool(name="create_record")
def create_record(base_id: str, table_name: str, fields: dict) -> dict:
    """Airtable REST: POST /v0/{base_id}/{table_name} with body
    {"fields":{...}} — create a record. Returns the created record."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            _record(s, "create_record", base_id=base_id, table=table_name,
                    result="table_not_found")
            _save_state(s)
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        # Optional: validate that each supplied field exists on the table.
        # Airtable silently drops unknown fields on the real API depending
        # on `typecast`; we mirror the lenient default behavior (store
        # everything the caller sent).
        rid = _gen_id("rec")
        rec = {
            "id": rid,
            "createdTime": _now(),
            "fields": dict(fields or {}),
        }
        tbl["records"][rid] = rec
        _record(s, "create_record", base_id=base_id, table=table_name,
                record_id=rid, field_keys=list((fields or {}).keys()))
        _save_state(s)
        return _record_strip(rec)


@mcp.tool(name="update_record")
def update_record(base_id: str, table_name: str,
                  record_id: str, fields: dict) -> dict:
    """Airtable REST: PATCH /v0/{base_id}/{table_name}/{record_id} with
    body {"fields":{...}} — update a record (merge semantics; only the
    keys supplied are overwritten)."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        rec = tbl["records"].get(record_id)
        if not rec:
            _record(s, "update_record", base_id=base_id, table=table_name,
                    record_id=record_id, result="not_found")
            _save_state(s)
            return _err("NOT_FOUND",
                        f"Record not found: {record_id}")
        rec.setdefault("fields", {}).update(fields or {})
        _record(s, "update_record", base_id=base_id, table=table_name,
                record_id=record_id, field_keys=list((fields or {}).keys()))
        _save_state(s)
        return _record_strip(rec)


@mcp.tool(name="delete_record")
def delete_record(base_id: str, table_name: str, record_id: str) -> dict:
    """Airtable REST: DELETE /v0/{base_id}/{table_name}/{record_id} —
    delete a record. Returns `{"id": "...", "deleted": true}`."""
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        rec = tbl["records"].pop(record_id, None)
        _record(s, "delete_record", base_id=base_id, table=table_name,
                record_id=record_id, result="ok" if rec else "not_found")
        _save_state(s)
        if not rec:
            return _err("NOT_FOUND",
                        f"Record not found: {record_id}")
        return {"id": record_id, "deleted": True}


@mcp.tool(name="search_records")
def search_records(base_id: str, table_name: str,
                   field_name: str, value: str) -> list | dict:
    """Airtable REST: GET /v0/{base_id}/{table_name}?filterByFormula=
    {field_name}="value" — exact-match search on a single field.

    Upstream returns `response.data.records` (array). Matches upstream's
    formula exactly: `{<field_name>} = "<value>"`.
    """
    with _lock():
        s = _load_state()
        base = s["bases"].get(base_id)
        if not base:
            return _err("NOT_FOUND",
                        f"Could not find base with ID: {base_id}")
        tbl = _resolve_table(base, table_name)
        if not tbl:
            _record(s, "search_records", base_id=base_id, table=table_name,
                    result="table_not_found")
            _save_state(s)
            return _err("TABLE_NOT_FOUND",
                        f"Could not find table {table_name} in base "
                        f"{base_id}")
        formula = f'{{{field_name}}} = "{value}"'
        out = [_record_strip(r) for r in tbl["records"].values()
               if _match_formula(r, formula)]
        _record(s, "search_records", base_id=base_id, table=table_name,
                field=field_name, value=value, hits=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Debug helpers (mock-only)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not in the upstream server."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_base")
def mock_debug_seed_base(base: dict) -> dict:
    """Mock-only: insert a fully-formed base object into state, bypassing
    validation. Used by per-task preprocessing to seed fixtures.

    `base` shape:
      {"id": "appXXX", "name": "...",
       "permissionLevel": "create",
       "tables": {
         "tblXXX": {
            "id":"tblXXX", "name":"...",
            "primaryFieldId":"fldXXX",
            "fields":[{"id":"fldXXX","name":"Title","type":"singleLineText"}],
            "views":[...],
            "records": {"recXXX": {"id":"recXXX",
                                    "createdTime":"...",
                                    "fields":{...}}}
         }
       }}
    """
    with _lock():
        s = _load_state()
        if not isinstance(base, dict) or "id" not in base:
            return _err("INVALID_REQUEST_UNKNOWN",
                        "base must be a dict with an `id`")
        # Normalize: ensure each table has a `records` dict keyed by id.
        for tbl in base.get("tables", {}).values():
            recs = tbl.get("records")
            if isinstance(recs, list):
                tbl["records"] = {r["id"]: r for r in recs}
            elif recs is None:
                tbl["records"] = {}
        s["bases"][base["id"]] = base
        _record(s, "debug_seed_base", base_id=base["id"],
                tables=list(base.get("tables", {}).keys()))
        _save_state(s)
        return {"id": base["id"],
                "tables": list(base.get("tables", {}).keys())}


if __name__ == "__main__":
    mcp.run()
