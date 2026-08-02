"""Shared access to the google-cloud-mock state, for the SDK shim.

The shim reads/writes exactly the same files the `google-cloud-mock` MCP
server uses, so the agent (via MCP tools) and the harness code (upstream
preprocess + upstream grader, via `google.cloud.*`) see one single state:

    $GCP_MOCK_STATE_DIR/state.json    buckets, log buckets, log entries,
                                      dataset/table metadata, call log
    $GCP_MOCK_STATE_DIR/db.sqlite3    BigQuery table rows

Nothing here talks to Google. Keep the on-disk shapes in sync with
mocks/google-cloud-mock/server.py.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import sqlite3

from . import bq_sqlite

PROJECT_ID = os.environ.get("GCP_MOCK_PROJECT_ID", "mcp-bench0606")


def state_dir() -> str:
    d = os.environ.get(
        "GCP_MOCK_STATE_DIR", os.path.expanduser("~/.openclaw/gcp_mock")
    )
    os.makedirs(d, exist_ok=True)
    return d


def state_path() -> str:
    return os.path.join(state_dir(), "state.json")


def db_path() -> str:
    return os.path.join(state_dir(), "db.sqlite3")


def now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_ts(value) -> datetime.datetime:
    """Parse a mock timestamp string into an aware datetime."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(
            tzinfo=datetime.timezone.utc)
    if not value:
        return datetime.datetime.now(datetime.timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return datetime.datetime.now(datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def empty_state() -> dict:
    return {
        "project_id": PROJECT_ID,
        "config": {
            "allowed_buckets": [],
            "allowed_datasets": [],
            "allowed_log_buckets": [],
            "allowed_instances": [],
        },
        "datasets": {},
        "buckets": {},
        "log_buckets": {},
        "log_sinks": {},
        "logs": {},
        "instances": {},
        "jobs": {},
        "calls": [],
    }


def load_state() -> dict:
    path = state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GCP_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                s = json.load(f)
        else:
            s = empty_state()
        s.setdefault("project_id", PROJECT_ID)
        return s
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must not
    # KeyError downstream — fill in the skeleton's missing top-level keys.
    for k, v in empty_state().items():
        s.setdefault(k, v)
    return s


def save_state(state: dict) -> None:
    path = state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


@contextlib.contextmanager
def lock():
    fd = open(state_path() + ".lock", "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


@contextlib.contextmanager
def mutate():
    """Load state, yield it, save it — under the shared file lock."""
    with lock():
        s = load_state()
        yield s
        save_state(s)


def record(state: dict, op: str, **kwargs) -> None:
    entry = {"op": op, "ts": now(), "via": "sdk_shim"}
    entry.update(kwargs)
    state.setdefault("calls", []).append(entry)


# --------------------------------------------------------------------------
# BigQuery <-> SQLite (mirrors google-cloud-mock/server.py)
# --------------------------------------------------------------------------

BQ_TYPES = {
    "STRING": ("TEXT", str),
    "BYTES": ("BLOB", lambda v: v),
    "INTEGER": ("INTEGER", lambda v: int(v) if v not in (None, "") else None),
    "INT64": ("INTEGER", lambda v: int(v) if v not in (None, "") else None),
    "FLOAT": ("REAL", lambda v: float(v) if v not in (None, "") else None),
    "FLOAT64": ("REAL", lambda v: float(v) if v not in (None, "") else None),
    "NUMERIC": ("REAL", lambda v: float(v) if v not in (None, "") else None),
    "BIGNUMERIC": ("REAL", lambda v: float(v) if v not in (None, "") else None),
    "BOOLEAN": ("INTEGER",
                lambda v: 1 if str(v).lower() in ("1", "true", "yes") else 0),
    "BOOL": ("INTEGER",
             lambda v: 1 if str(v).lower() in ("1", "true", "yes") else 0),
    "DATE": ("TEXT", str),
    "DATETIME": ("TEXT", str),
    "TIMESTAMP": ("TEXT", str),
    "TIME": ("TEXT", str),
    "RECORD": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "STRUCT": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "JSON": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "GEOGRAPHY": ("TEXT", str),
}


def sqlite_table_name(dataset: str, table: str) -> str:
    safe = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)
    return f"{safe(dataset)}__{safe(table)}"


def db() -> sqlite3.Connection:
    """SQLite connection with the BigQuery-compat functions installed."""
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    bq_sqlite.register_functions(conn)
    return conn


def ensure_table(conn: sqlite3.Connection, dataset: str, table: str,
                 schema: list) -> None:
    name = sqlite_table_name(dataset, table)
    cols = []
    for f in schema:
        t = (f.get("type") or "STRING").upper()
        cols.append(f'"{f["name"]}" {BQ_TYPES.get(t, ("TEXT", str))[0]}')
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({", ".join(cols)})')
    conn.commit()


def coerce_row(row: dict, schema: list) -> tuple:
    out = []
    for f in schema:
        v = row.get(f["name"])
        if v is None:
            out.append(None)
            continue
        t = (f.get("type") or "STRING").upper()
        coercer = BQ_TYPES.get(t, ("TEXT", str))[1]
        try:
            out.append(coercer(v))
        except (TypeError, ValueError):
            out.append(v)
    return tuple(out)


_FQ_TABLE_RE = re.compile(
    r"`?(?P<proj>[A-Za-z0-9_\-]+)\.(?P<ds>[A-Za-z0-9_\-]+)\.(?P<tbl>[A-Za-z0-9_\-]+)`?"
)
_BACKTICK_DS_TABLE_RE = re.compile(
    r"`(?P<ds>[A-Za-z0-9_\-]+)\.(?P<tbl>[A-Za-z0-9_\-]+)`"
)
_BARE_DS_TABLE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<ds>[A-Za-z0-9_]+)\.(?P<tbl>[A-Za-z0-9_]+)(?![A-Za-z0-9_])"
)


def rewrite_query(query: str, state: dict) -> tuple:
    referenced = set()
    tables = set()
    known = set(state.get("datasets", {}))

    def _sub_fq(m):
        ds, tbl = m.group("ds"), m.group("tbl")
        referenced.add(ds)
        tables.add((ds, tbl))
        return f'"{sqlite_table_name(ds, tbl)}"'

    q = _FQ_TABLE_RE.sub(_sub_fq, query)
    q = _BACKTICK_DS_TABLE_RE.sub(_sub_fq, q)

    def _sub_bare(m):
        ds, tbl = m.group("ds"), m.group("tbl")
        if ds in known:
            referenced.add(ds)
            tables.add((ds, tbl))
            return f'"{sqlite_table_name(ds, tbl)}"'
        return m.group(0)

    q = _BARE_DS_TABLE_RE.sub(_sub_bare, q)
    # BigQuery dialect -> SQLite (float division, COUNTIF, EXTRACT, ...)
    record_cols = set()
    for ds_id, tbl_id in tables:
        tbl = (state.get("datasets", {}).get(ds_id, {})
               .get("tables", {}).get(tbl_id, {}))
        for field in tbl.get("schema", []):
            if (field.get("type") or "").upper() in ("RECORD", "STRUCT"):
                record_cols.add(field["name"])
    q = bq_sqlite.rewrite_struct_access(q, record_cols)
    return bq_sqlite.prepare_query(q), sorted(referenced), sorted(tables)


def infer_schema_from_rows(rows: list) -> list:
    """Autodetect a BQ-ish schema from sampled JSON/CSV rows."""
    fields: dict = {}
    for row in rows[:500]:
        for k, v in row.items():
            cur = fields.get(k)
            t = _infer_type(v)
            if cur is None or (cur != t and t != "STRING"):
                # widen INTEGER -> FLOAT -> STRING
                order = {"INTEGER": 0, "FLOAT": 1, "BOOLEAN": 0,
                         "TIMESTAMP": 2, "STRING": 3}
                if cur is None or order.get(t, 3) > order.get(cur, 3):
                    fields[k] = t
            elif cur != t:
                fields[k] = "STRING"
    return [{"name": k, "type": v, "mode": "NULLABLE"}
            for k, v in fields.items()]


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")


def _infer_type(v) -> str:
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "FLOAT"
    s = str(v).strip()
    if s == "":
        return "STRING"
    try:
        int(s)
        return "INTEGER"
    except ValueError:
        pass
    try:
        float(s)
        return "FLOAT"
    except ValueError:
        pass
    if s.lower() in ("true", "false"):
        return "BOOLEAN"
    if _TS_RE.match(s):
        return "TIMESTAMP"
    return "STRING"
