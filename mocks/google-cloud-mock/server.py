"""Google Cloud mock MCP server.

Mirrors the tool surface of `lockon-n/google-cloud-mcp`
(github.com/lockon-n/google-cloud-mcp), the upstream that Toolathlon
invokes via `uvx google-cloud-mcp --project-id ...
--service-account-path ... --allowed-buckets ... --allowed-datasets
... --allowed-log-buckets ... --allowed-instances ...`.

Tool surface and arg names are verbatim copies of `src/server.py`
@mcp.tool functions in the upstream so a snapshot built against the
real server replays unchanged. The string return shapes are also
verbatim copies (the upstream returns formatted human-readable
strings — not JSON — for every tool).

Backed by:
  - $GCP_MOCK_STATE_DIR/state.json  (objects, configs, call log)
  - $GCP_MOCK_STATE_DIR/db.sqlite3    (BigQuery row data, real SQL
    engine for query-result fidelity across calls)

BigQuery datasets/tables live in SQLite under table names
`<dataset>__<table>`; the project component of fully-qualified
references (`project.dataset.table`) is unwrapped at parse time
(SQLite has no project namespace). bigquery_run_query rewrites
fully-qualified `project.dataset.table` and backticked
`dataset.table` references to the SQLite table name before execution.

Access control mirrors the upstream's wildcard-prefix matcher:
`prefix*` matches `prefix-anything`, exact match otherwise.

Seed the initial state via $GCP_MOCK_SEED_PATH (JSON in the same
shape as state.json) plus pre-populated tables via
`mock_debug_create_dataset` / `mock_debug_load_table_rows` /
`mock_debug_seed_bucket_object` debug tools.
"""

from __future__ import annotations

import argparse
import base64
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

# BigQuery-dialect compatibility for the SQLite backend (float division,
# COUNTIF, EXTRACT, SAFE_DIVIDE, ...). Vendored copy shared with the
# gcp-sdk-shim used by task preprocess/graders — edit both together.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bq_sqlite


# ---------------------------------------------------------------------------
# Configuration (populated from argv at startup)
# ---------------------------------------------------------------------------

PROJECT_ID: str = "mock-project"
SERVICE_ACCOUNT_PATH: str | None = None
ALLOWED_BUCKETS: set[str] = set()
ALLOWED_DATASETS: set[str] = set()
ALLOWED_LOG_BUCKETS: set[str] = set()
ALLOWED_INSTANCES: set[str] = set()


def _matches_allowed_pattern(name: str, patterns: set[str]) -> bool:
    """Upstream wildcard semantics: empty allowlist = no restriction;
    `prefix*` matches any name starting with `prefix`; otherwise
    exact match."""
    if not patterns:
        return True
    for p in patterns:
        if p.endswith("*"):
            if name.startswith(p[:-1]):
                return True
        elif name == p:
            return True
    return False


def validate_dataset_access(dataset_id: str) -> bool:
    return _matches_allowed_pattern(dataset_id, ALLOWED_DATASETS)


def validate_bucket_access(bucket_name: str) -> bool:
    return _matches_allowed_pattern(bucket_name, ALLOWED_BUCKETS)


def validate_log_bucket_access(name: str) -> bool:
    return _matches_allowed_pattern(name, ALLOWED_LOG_BUCKETS)


def validate_instance_access(name: str) -> bool:
    return _matches_allowed_pattern(name, ALLOWED_INSTANCES)


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    d = os.environ.get(
        "GCP_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gcp_mock"),
    )
    os.makedirs(d, exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(_state_dir(), "state.json")


def _db_path() -> str:
    return os.path.join(_state_dir(), "db.sqlite3")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="microseconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "project_id": PROJECT_ID,
        "config": {
            "allowed_buckets": sorted(ALLOWED_BUCKETS),
            "allowed_datasets": sorted(ALLOWED_DATASETS),
            "allowed_log_buckets": sorted(ALLOWED_LOG_BUCKETS),
            "allowed_instances": sorted(ALLOWED_INSTANCES),
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


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GCP_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                s = json.load(f)
        else:
            s = _empty_state()
        s.setdefault("project_id", PROJECT_ID)
        return s
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must not
    # KeyError downstream — fill in the skeleton's missing top-level keys.
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


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    bq_sqlite.register_functions(conn)
    return conn


# ---------------------------------------------------------------------------
# BigQuery <-> SQLite helpers
# ---------------------------------------------------------------------------

# BQ types we accept and how they map to SQLite affinity / Python coercion.
BQ_TYPES = {
    "STRING": ("TEXT", str),
    "BYTES": ("BLOB", lambda v: v),
    "INTEGER": ("INTEGER", lambda v: int(v) if v is not None and v != "" else None),
    "INT64": ("INTEGER", lambda v: int(v) if v is not None and v != "" else None),
    "FLOAT": ("REAL", lambda v: float(v) if v is not None and v != "" else None),
    "FLOAT64": ("REAL", lambda v: float(v) if v is not None and v != "" else None),
    "NUMERIC": ("REAL", lambda v: float(v) if v is not None and v != "" else None),
    "BIGNUMERIC": ("REAL", lambda v: float(v) if v is not None and v != "" else None),
    "BOOLEAN": ("INTEGER", lambda v: 1 if str(v).lower() in ("1", "true", "yes") else 0),
    "BOOL": ("INTEGER", lambda v: 1 if str(v).lower() in ("1", "true", "yes") else 0),
    "DATE": ("TEXT", str),
    "DATETIME": ("TEXT", str),
    "TIMESTAMP": ("TEXT", str),
    "TIME": ("TEXT", str),
    "RECORD": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "STRUCT": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "JSON": ("TEXT", lambda v: json.dumps(v) if not isinstance(v, str) else v),
    "GEOGRAPHY": ("TEXT", str),
}


def _sqlite_table_name(dataset: str, table: str) -> str:
    # SQLite identifier: replace dashes/dots
    safe = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)
    return f"{safe(dataset)}__{safe(table)}"


def _ensure_table(conn: sqlite3.Connection, dataset: str, table: str,
                  schema: list[dict]) -> None:
    name = _sqlite_table_name(dataset, table)
    cols = []
    for f in schema:
        t = (f.get("type") or "STRING").upper()
        affinity = BQ_TYPES.get(t, ("TEXT", str))[0]
        cols.append(f'"{f["name"]}" {affinity}')
    ddl = f'CREATE TABLE IF NOT EXISTS "{name}" ({", ".join(cols)})'
    conn.execute(ddl)
    conn.commit()


def _coerce_row(row: dict, schema: list[dict]) -> tuple:
    """Apply BQ-type-aware Python coercion before SQLite insert."""
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


def _rewrite_query(query: str, state: dict) -> tuple[str, list[str]]:
    """Translate BigQuery table references to SQLite names.

    Returns (rewritten_query, referenced_dataset_ids).
    Handles: `proj.ds.tbl`, proj.ds.tbl, `ds.tbl`, ds.tbl.
    """
    referenced: set[str] = set()
    tables: set = set()
    known = {ds for ds in state["datasets"]}

    def _sub_fq(m: re.Match) -> str:
        ds, tbl = m.group("ds"), m.group("tbl")
        referenced.add(ds)
        tables.add((ds, tbl))
        return f'"{_sqlite_table_name(ds, tbl)}"'

    q = _FQ_TABLE_RE.sub(_sub_fq, query)
    q = _BACKTICK_DS_TABLE_RE.sub(_sub_fq, q)

    def _sub_bare(m: re.Match) -> str:
        ds, tbl = m.group("ds"), m.group("tbl")
        if ds in known:
            referenced.add(ds)
            tables.add((ds, tbl))
            return f'"{_sqlite_table_name(ds, tbl)}"'
        return m.group(0)

    q = _BARE_DS_TABLE_RE.sub(_sub_bare, q)
    # BigQuery dialect -> SQLite; keeps mock query results identical to
    # what real BigQuery returns for the SQL agents actually write.
    record_cols = set()
    for ds_id, tbl_id in tables:
        tbl = (state.get("datasets", {}).get(ds_id, {})
               .get("tables", {}).get(tbl_id, {}))
        for field in tbl.get("schema", []):
            if (field.get("type") or "").upper() in ("RECORD", "STRUCT"):
                record_cols.add(field["name"])
    q = bq_sqlite.rewrite_struct_access(q, record_cols)
    return bq_sqlite.prepare_query(q), sorted(referenced), sorted(tables)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("google-cloud-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ===========================================================================
# BigQuery tools
# ===========================================================================

@mcp.tool()
def bigquery_run_query(query: str, dry_run: bool = False,
                       max_results: int = 1000) -> str:
    """Execute a BigQuery SQL query.

    Args:
        query: SQL query to execute
        dry_run: If True, only validate query without running it
        max_results: Maximum number of results to return (default: 1000)
    """
    with _lock():
        s = _load_state()
        rewritten, referenced, ref_tables = _rewrite_query(query, s)
        for ds in referenced:
            if not validate_dataset_access(ds):
                _record(s, "bigquery_run_query", query=query,
                        result="access_denied", dataset=ds)
                _save_state(s)
                return (f"Error executing BigQuery query: "
                        f"Access denied: Dataset '{ds}' is not in allowed "
                        f"datasets list")

        if dry_run:
            _record(s, "bigquery_run_query", query=query, dry_run=True,
                    referenced=referenced)
            _save_state(s)
            return ("Query validation successful.\n"
                    "Estimated bytes processed: 0\n"
                    "Estimated cost: $0.0000 USD")

        conn = _db()
        try:
            cur = conn.execute(rewritten)
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if cur.description else []
            if bq_sqlite.is_write_statement(rewritten):
                # DDL/DML: BigQuery would register the new/changed table, so
                # reconcile the catalog with what SQLite now holds.
                conn.commit()
                bq_sqlite.sync_catalog(conn, s, ref_tables,
                                       _sqlite_table_name, _now)
        except sqlite3.Error as e:
            conn.close()
            _record(s, "bigquery_run_query", query=query, error=str(e))
            _save_state(s)
            return f"Error executing BigQuery query: {e}"
        conn.close()

        rows = [dict(r) for r in rows]
        total_rows = len(rows)
        if max_results:
            rows = rows[: int(max_results)]

        _record(s, "bigquery_run_query", query=query,
                rewritten=rewritten, referenced=referenced,
                total_rows=total_rows, returned=len(rows))
        _save_state(s)

        response = "Query executed successfully.\n"
        response += f"Total rows: {total_rows}, Returned: {len(rows)}\n"
        response += "Bytes processed: 0, Execution time: 0ms\n\n"
        if rows:
            response += "Sample results (first 5 rows):\n"
            for i, row in enumerate(rows[:5]):
                response += f"Row {i + 1}: {row}\n"
            if len(rows) > 5:
                response += f"... and {len(rows) - 5} more rows"
        else:
            response += "No results returned."
        # Append the full result set as JSON so agents that need
        # all rows can parse it (the upstream truncates to 5 rows
        # in the string body, but real callers pull `results` from
        # the structured return). We include both.
        response += "\n\n__FULL_RESULTS_JSON__\n" + json.dumps(
            {"columns": col_names, "rows": rows,
             "total_rows": total_rows}, default=str)
        return response


@mcp.tool()
def bigquery_list_datasets() -> str:
    """List all BigQuery datasets in the project."""
    with _lock():
        s = _load_state()
        names = sorted(s["datasets"].keys())
        visible = [n for n in names if validate_dataset_access(n)]
        _record(s, "bigquery_list_datasets", count=len(visible))
        _save_state(s)
        if not visible:
            return "No datasets found or no access to allowed datasets"
        return (f"Found {len(visible)} datasets:\n"
                + "\n".join(f"- {n} (Project: {PROJECT_ID})" for n in visible))


@mcp.tool()
def bigquery_create_dataset(dataset_id: str, description: str = "",
                            location: str = "US") -> str:
    """Create a new BigQuery dataset.

    Args:
        dataset_id: ID for the new dataset
        description: Optional description for the dataset
        location: Dataset location (default: US)
    """
    if not validate_dataset_access(dataset_id):
        return (f"Access denied: Dataset '{dataset_id}' is not in allowed "
                f"datasets list")
    with _lock():
        s = _load_state()
        if dataset_id in s["datasets"]:
            _record(s, "bigquery_create_dataset", dataset_id=dataset_id,
                    result="exists")
            _save_state(s)
            return f"Error creating BigQuery dataset: 409 Already Exists: {dataset_id}"
        s["datasets"][dataset_id] = {
            "dataset_id": dataset_id,
            "description": description,
            "location": location,
            "created": _now(),
            "modified": _now(),
            "tables": {},
            "labels": {},
        }
        _record(s, "bigquery_create_dataset", dataset_id=dataset_id,
                location=location)
        _save_state(s)
        return (f"Successfully created dataset '{dataset_id}' in location "
                f"'{location}'")


@mcp.tool()
def bigquery_get_dataset_info(dataset_id: str) -> str:
    """Get detailed information about a BigQuery dataset.

    Args:
        dataset_id: ID of the dataset
    """
    if not validate_dataset_access(dataset_id):
        return (f"Access denied: Dataset '{dataset_id}' is not in allowed "
                f"datasets list")
    with _lock():
        s = _load_state()
        ds = s["datasets"].get(dataset_id)
        _record(s, "bigquery_get_dataset_info", dataset_id=dataset_id,
                result="ok" if ds else "not_found")
        _save_state(s)
        if not ds:
            return f"Error getting dataset info for '{dataset_id}': 404 Not Found"
        result = f"Dataset Information for '{dataset_id}':\n"
        result += f"Full Name: {PROJECT_ID}:{dataset_id}\n"
        result += f"Location: {ds.get('location', 'Unknown')}\n"
        result += f"Description: {ds.get('description') or 'No description'}\n"
        result += f"Created: {ds.get('created', 'Unknown')}\n"
        result += f"Modified: {ds.get('modified', 'Unknown')}\n"
        result += f"Default Table Expiration: None ms\n"
        result += f"Labels: {ds.get('labels') or 'None'}"
        return result


@mcp.tool()
def bigquery_load_csv_data(dataset_id: str, table_id: str,
                           csv_file_path: str, skip_header: bool = True,
                           write_mode: str = "WRITE_TRUNCATE") -> str:
    """Load data from CSV file to BigQuery table.

    Args:
        dataset_id: Dataset ID
        table_id: Table ID
        csv_file_path: Path to CSV file
        skip_header: Whether to skip the first row (header)
        write_mode: Write mode (WRITE_TRUNCATE, WRITE_APPEND, WRITE_EMPTY)
    """
    if not validate_dataset_access(dataset_id):
        return (f"Access denied: Dataset '{dataset_id}' is not in allowed "
                f"datasets list")
    if not os.path.exists(csv_file_path):
        return f"Error loading CSV data to BigQuery: CSV file not found: {csv_file_path}"
    import csv as _csv
    with _lock():
        s = _load_state()
        if dataset_id not in s["datasets"]:
            return f"Error loading CSV data to BigQuery: 404 Not Found: dataset {dataset_id}"
        # utf-8-sig: real BigQuery strips a leading UTF-8 BOM from the header
        with open(csv_file_path, "r", encoding="utf-8-sig") as f:
            reader = _csv.reader(f)
            try:
                header = next(reader) if skip_header else None
            except StopIteration:
                header = []
            rows_in = list(reader)
        if not header:
            n = max((len(r) for r in rows_in), default=0)
            header = [f"col_{i}" for i in range(n)]
        # Autodetect: try INTEGER -> FLOAT -> STRING per column
        types = []
        for ci, name in enumerate(header):
            col_vals = [r[ci] for r in rows_in if ci < len(r) and r[ci] != ""]
            t = "INTEGER"
            for v in col_vals:
                try:
                    int(v)
                except ValueError:
                    t = "FLOAT"
                    try:
                        float(v)
                    except ValueError:
                        t = "STRING"
                        break
            if t == "FLOAT":
                for v in col_vals:
                    try:
                        float(v)
                    except ValueError:
                        t = "STRING"
                        break
            types.append(t)
        schema = [{"name": h, "type": t, "mode": "NULLABLE"}
                  for h, t in zip(header, types)]
        ds = s["datasets"][dataset_id]
        tbl = ds["tables"].get(table_id) or {
            "table_id": table_id,
            "schema": schema,
            "num_rows": 0,
            "created": _now(),
            "modified": _now(),
        }
        if write_mode == "WRITE_EMPTY" and tbl.get("num_rows", 0) > 0:
            return f"Error loading CSV data to BigQuery: WRITE_EMPTY but table not empty"
        # Schema follows CSV when truncating; preserve existing on append
        if write_mode != "WRITE_APPEND" or not tbl.get("schema"):
            tbl["schema"] = schema
        ds["tables"][table_id] = tbl

        conn = _db()
        if write_mode == "WRITE_TRUNCATE":
            conn.execute(
                f'DROP TABLE IF EXISTS "{_sqlite_table_name(dataset_id, table_id)}"')
            conn.commit()
        _ensure_table(conn, dataset_id, table_id, tbl["schema"])
        cols = ", ".join(f'"{f["name"]}"' for f in tbl["schema"])
        placeholders = ", ".join("?" for _ in tbl["schema"])
        sql = (f'INSERT INTO "{_sqlite_table_name(dataset_id, table_id)}" '
               f'({cols}) VALUES ({placeholders})')
        n_loaded = 0
        for r in rows_in:
            row_dict = {f["name"]: (r[i] if i < len(r) else None)
                        for i, f in enumerate(tbl["schema"])}
            conn.execute(sql, _coerce_row(row_dict, tbl["schema"]))
            n_loaded += 1
        conn.commit()
        # update count
        n_total = conn.execute(
            f'SELECT COUNT(*) FROM "{_sqlite_table_name(dataset_id, table_id)}"'
        ).fetchone()[0]
        conn.close()
        tbl["num_rows"] = n_total
        tbl["modified"] = _now()
        _record(s, "bigquery_load_csv_data", dataset_id=dataset_id,
                table_id=table_id, rows_loaded=n_loaded, write_mode=write_mode)
        _save_state(s)
        return (f"Successfully loaded CSV data from '{csv_file_path}' to "
                f"table '{dataset_id}.{table_id}'")


@mcp.tool()
def bigquery_export_table(dataset_id: str, table_id: str,
                          destination_bucket: str, destination_path: str,
                          file_format: str = "CSV") -> str:
    """Export BigQuery table to Cloud Storage.

    Args:
        dataset_id: Dataset ID
        table_id: Table ID
        destination_bucket: Destination bucket name
        destination_path: Destination path in bucket
        file_format: Export format (CSV, JSON, AVRO)
    """
    if not validate_dataset_access(dataset_id):
        return (f"Access denied: Dataset '{dataset_id}' is not in allowed "
                f"datasets list")
    if not validate_bucket_access(destination_bucket):
        return (f"Access denied: Bucket '{destination_bucket}' is not in "
                f"allowed buckets list")
    fmt = (file_format or "CSV").upper()
    if fmt not in ("CSV", "JSON"):
        return (f"Export format '{file_format}' not yet supported. "
                f"Currently only CSV is supported.")
    with _lock():
        s = _load_state()
        ds = s["datasets"].get(dataset_id)
        if not ds or table_id not in ds["tables"]:
            return f"Error exporting BigQuery table: 404 Not Found"
        schema = ds["tables"][table_id]["schema"]
        conn = _db()
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM "{_sqlite_table_name(dataset_id, table_id)}"'
        ).fetchall()]
        conn.close()
        if fmt == "CSV":
            import io
            import csv as _csv
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerow([f["name"] for f in schema])
            for r in rows:
                w.writerow([r.get(f["name"]) for f in schema])
            payload = buf.getvalue().encode("utf-8")
        else:
            payload = ("\n".join(json.dumps(r, default=str) for r in rows)
                       + "\n").encode("utf-8")
        s["buckets"].setdefault(destination_bucket, {
            "name": destination_bucket, "location": "US",
            "storage_class": "STANDARD", "created": _now(),
            "versioning_enabled": False, "labels": {},
            "lifecycle_rules": [], "objects": {}})
        s["buckets"][destination_bucket]["objects"][destination_path] = {
            "name": destination_path,
            "size": len(payload),
            "content_b64": base64.b64encode(payload).decode("ascii"),
            "content_type": "text/csv" if fmt == "CSV" else "application/json",
            "updated": _now(),
        }
        _record(s, "bigquery_export_table", dataset_id=dataset_id,
                table_id=table_id, destination_bucket=destination_bucket,
                destination_path=destination_path, format=fmt,
                bytes=len(payload))
        _save_state(s)
        return (f"Successfully exported table '{dataset_id}.{table_id}' to "
                f"'gs://{destination_bucket}/{destination_path}'")


@mcp.tool()
def bigquery_list_jobs(max_results: int = 50, state_filter: str = "") -> str:
    """List BigQuery jobs.

    Args:
        max_results: Maximum number of jobs to return
        state_filter: Filter by job state (RUNNING, DONE, PENDING)
    """
    with _lock():
        s = _load_state()
        jobs = list(s["jobs"].values())
        if state_filter:
            jobs = [j for j in jobs if j.get("state") == state_filter]
        jobs = jobs[: int(max_results)]
        _record(s, "bigquery_list_jobs", count=len(jobs))
        _save_state(s)
        if not jobs:
            return "No BigQuery jobs found"
        result = f"Found {len(jobs)} BigQuery jobs:\n"
        for j in jobs[:10]:
            result += (f"- {j.get('job_id', 'Unknown')}: "
                       f"{j.get('state', 'Unknown')} "
                       f"({j.get('job_type', 'Unknown')})\n"
                       f"  Created: {j.get('created', 'Unknown')}\n")
        if len(jobs) > 10:
            result += f"... and {len(jobs) - 10} more jobs"
        return result


@mcp.tool()
def bigquery_cancel_job(job_id: str) -> str:
    """Cancel a BigQuery job.

    Args:
        job_id: ID of the job to cancel
    """
    with _lock():
        s = _load_state()
        j = s["jobs"].get(job_id)
        if not j:
            _record(s, "bigquery_cancel_job", job_id=job_id, result="not_found")
            _save_state(s)
            return (f"Could not cancel BigQuery job '{job_id}' (may already be "
                    f"completed)")
        j["state"] = "DONE"
        j["cancelled"] = True
        _record(s, "bigquery_cancel_job", job_id=job_id)
        _save_state(s)
        return f"Successfully cancelled BigQuery job '{job_id}'"


# ===========================================================================
# Cloud Storage tools
# ===========================================================================

def _bucket_or_404(s: dict, name: str) -> dict | None:
    return s["buckets"].get(name)


@mcp.tool()
def storage_list_buckets() -> str:
    """List all Cloud Storage buckets."""
    with _lock():
        s = _load_state()
        buckets = list(s["buckets"].values())
        buckets = [b for b in buckets if validate_bucket_access(b["name"])]
        _record(s, "storage_list_buckets", count=len(buckets))
        _save_state(s)
        return (f"Found {len(buckets)} buckets:\n" + "\n".join(
            f"- {b['name']}: {b.get('location', 'Unknown location')}"
            for b in buckets))


@mcp.tool()
def storage_create_bucket(bucket_name: str, location: str = "US") -> str:
    """Create a new Cloud Storage bucket.

    Args:
        bucket_name: Name for the new bucket
        location: Location for the bucket (default: US)
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        if bucket_name in s["buckets"]:
            _record(s, "storage_create_bucket", bucket_name=bucket_name,
                    result="exists")
            _save_state(s)
            return f"Error creating Cloud Storage bucket: 409 Already Exists"
        s["buckets"][bucket_name] = {
            "name": bucket_name, "location": location,
            "storage_class": "STANDARD", "created": _now(),
            "versioning_enabled": False, "labels": {},
            "lifecycle_rules": [], "objects": {},
        }
        _record(s, "storage_create_bucket", bucket_name=bucket_name,
                location=location)
        _save_state(s)
        return (f"Successfully created bucket '{bucket_name}' in location "
                f"'{location}'")


@mcp.tool()
def storage_list_objects(bucket_name: str, prefix: str = "") -> str:
    """List objects in a Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        prefix: Optional prefix to filter objects
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b:
            _record(s, "storage_list_objects", bucket_name=bucket_name,
                    result="not_found")
            _save_state(s)
            return f"Error listing objects in bucket '{bucket_name}': 404 Not Found"
        objs = [o for o in b["objects"].values()
                if not prefix or o["name"].startswith(prefix)]
        _record(s, "storage_list_objects", bucket_name=bucket_name,
                prefix=prefix, count=len(objs))
        _save_state(s)
        out = (f"Found {len(objs)} objects in bucket '{bucket_name}':\n"
               + "\n".join(f"- {o['name']}: {o.get('size', 0)} bytes"
                           for o in objs[:20]))
        if len(objs) > 20:
            out += "..."
        return out


@mcp.tool()
def storage_upload_file(bucket_name: str, source_file_path: str,
                        destination_blob_name: str) -> str:
    """Upload a file to Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        source_file_path: Local path to the file to upload
        destination_blob_name: Name for the blob in the bucket
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    if not os.path.exists(source_file_path):
        return (f"Error uploading file to bucket '{bucket_name}': source file "
                f"not found: {source_file_path}")
    with open(source_file_path, "rb") as f:
        data = f.read()
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b:
            _record(s, "storage_upload_file", bucket_name=bucket_name,
                    result="bucket_not_found")
            _save_state(s)
            return (f"Error uploading file to bucket '{bucket_name}': 404 "
                    f"Bucket Not Found")
        # Guess content-type from extension
        ext = os.path.splitext(destination_blob_name)[1].lower()
        ct = {
            ".csv": "text/csv", ".json": "application/json",
            ".txt": "text/plain", ".pdf": "application/pdf",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        }.get(ext, "application/octet-stream")
        b["objects"][destination_blob_name] = {
            "name": destination_blob_name, "size": len(data),
            "content_b64": base64.b64encode(data).decode("ascii"),
            "content_type": ct, "updated": _now(),
        }
        _record(s, "storage_upload_file", bucket_name=bucket_name,
                destination_blob_name=destination_blob_name, bytes=len(data))
        _save_state(s)
        return (f"Successfully uploaded '{source_file_path}' to "
                f"'{bucket_name}/{destination_blob_name}'")


@mcp.tool()
def storage_download_file(bucket_name: str, source_blob_name: str,
                          destination_file_path: str) -> str:
    """Download a file from Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        source_blob_name: Name of the blob in the bucket
        destination_file_path: Local path where to save the file
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b or source_blob_name not in b["objects"]:
            _record(s, "storage_download_file", bucket_name=bucket_name,
                    blob=source_blob_name, result="not_found")
            _save_state(s)
            return f"Error downloading file from bucket '{bucket_name}': 404 Not Found"
        data = base64.b64decode(b["objects"][source_blob_name]["content_b64"])
        os.makedirs(os.path.dirname(os.path.abspath(destination_file_path)),
                    exist_ok=True)
        with open(destination_file_path, "wb") as f:
            f.write(data)
        _record(s, "storage_download_file", bucket_name=bucket_name,
                blob=source_blob_name, bytes=len(data))
        _save_state(s)
        return (f"Successfully downloaded '{bucket_name}/{source_blob_name}' "
                f"to '{destination_file_path}'")


@mcp.tool()
def storage_delete_object(bucket_name: str, blob_name: str) -> str:
    """Delete an object from Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        blob_name: Name of the blob to delete
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b or blob_name not in b["objects"]:
            _record(s, "storage_delete_object", bucket_name=bucket_name,
                    blob=blob_name, result="not_found")
            _save_state(s)
            return f"Object '{blob_name}' not found in bucket '{bucket_name}'"
        del b["objects"][blob_name]
        _record(s, "storage_delete_object", bucket_name=bucket_name,
                blob=blob_name)
        _save_state(s)
        return f"Successfully deleted '{blob_name}' from bucket '{bucket_name}'"


@mcp.tool()
def storage_get_bucket_info(bucket_name: str) -> str:
    """Get detailed information about a Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        _record(s, "storage_get_bucket_info", bucket_name=bucket_name,
                result="ok" if b else "not_found")
        _save_state(s)
        if not b:
            return f"Error getting bucket info for '{bucket_name}': 404 Not Found"
        result = f"Bucket Information for '{bucket_name}':\n"
        result += f"Location: {b.get('location', 'Unknown')}\n"
        result += f"Storage Class: {b.get('storage_class', 'Unknown')}\n"
        result += f"Created: {b.get('created', 'Unknown')}\n"
        result += (f"Versioning: "
                   f"{'Enabled' if b.get('versioning_enabled') else 'Disabled'}\n")
        result += f"Labels: {b.get('labels', {})}\n"
        result += f"Lifecycle Rules: {len(b.get('lifecycle_rules', []))}"
        return result


@mcp.tool()
def storage_generate_signed_url(bucket_name: str, blob_name: str,
                                expiration_minutes: int = 60,
                                method: str = "GET") -> str:
    """Generate a signed URL for temporary access to a Cloud Storage object.

    Args:
        bucket_name: Name of the bucket
        blob_name: Name of the blob
        expiration_minutes: URL expiration time in minutes (default: 60)
        method: HTTP method (GET, PUT, POST, DELETE)
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        token = uuid.uuid4().hex
        url = (f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
               f"?X-Goog-Signature=mock&X-Goog-Expires="
               f"{expiration_minutes * 60}&token={token}")
        _record(s, "storage_generate_signed_url", bucket_name=bucket_name,
                blob=blob_name, method=method)
        _save_state(s)
        return (f"Signed URL for '{bucket_name}/{blob_name}' (expires in "
                f"{expiration_minutes} minutes):\n{url}")


@mcp.tool()
def storage_copy_object(source_bucket: str, source_blob: str,
                        dest_bucket: str, dest_blob: str) -> str:
    """Copy an object between Cloud Storage buckets.

    Args:
        source_bucket: Source bucket name
        source_blob: Source blob name
        dest_bucket: Destination bucket name
        dest_blob: Destination blob name
    """
    if not validate_bucket_access(source_bucket):
        return (f"Access denied: Source bucket '{source_bucket}' is not in "
                f"allowed buckets list")
    if not validate_bucket_access(dest_bucket):
        return (f"Access denied: Destination bucket '{dest_bucket}' is not in "
                f"allowed buckets list")
    with _lock():
        s = _load_state()
        sb = _bucket_or_404(s, source_bucket)
        db = _bucket_or_404(s, dest_bucket)
        if not sb or not db or source_blob not in sb["objects"]:
            _record(s, "storage_copy_object", result="not_found")
            _save_state(s)
            return f"Error copying object: 404 Not Found"
        db["objects"][dest_blob] = dict(sb["objects"][source_blob])
        db["objects"][dest_blob]["name"] = dest_blob
        db["objects"][dest_blob]["updated"] = _now()
        _record(s, "storage_copy_object", source_bucket=source_bucket,
                source_blob=source_blob, dest_bucket=dest_bucket,
                dest_blob=dest_blob)
        _save_state(s)
        return (f"Successfully copied '{source_bucket}/{source_blob}' to "
                f"'{dest_bucket}/{dest_blob}'")


@mcp.tool()
def storage_move_object(source_bucket: str, source_blob: str,
                        dest_bucket: str, dest_blob: str) -> str:
    """Move an object between Cloud Storage buckets.

    Args:
        source_bucket: Source bucket name
        source_blob: Source blob name
        dest_bucket: Destination bucket name
        dest_blob: Destination blob name
    """
    if not validate_bucket_access(source_bucket):
        return (f"Access denied: Source bucket '{source_bucket}' is not in "
                f"allowed buckets list")
    if not validate_bucket_access(dest_bucket):
        return (f"Access denied: Destination bucket '{dest_bucket}' is not in "
                f"allowed buckets list")
    with _lock():
        s = _load_state()
        sb = _bucket_or_404(s, source_bucket)
        db = _bucket_or_404(s, dest_bucket)
        if not sb or not db or source_blob not in sb["objects"]:
            _record(s, "storage_move_object", result="not_found")
            _save_state(s)
            return f"Error moving object: 404 Not Found"
        db["objects"][dest_blob] = dict(sb["objects"][source_blob])
        db["objects"][dest_blob]["name"] = dest_blob
        db["objects"][dest_blob]["updated"] = _now()
        del sb["objects"][source_blob]
        _record(s, "storage_move_object", source_bucket=source_bucket,
                source_blob=source_blob, dest_bucket=dest_bucket,
                dest_blob=dest_blob)
        _save_state(s)
        return (f"Successfully moved '{source_bucket}/{source_blob}' to "
                f"'{dest_bucket}/{dest_blob}'")


@mcp.tool()
def storage_enable_versioning(bucket_name: str, enabled: bool = True) -> str:
    """Enable or disable versioning for a Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        enabled: Whether to enable (True) or disable (False) versioning
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b:
            return (f"Error updating versioning for bucket '{bucket_name}': "
                    f"404 Not Found")
        b["versioning_enabled"] = bool(enabled)
        _record(s, "storage_enable_versioning", bucket_name=bucket_name,
                enabled=enabled)
        _save_state(s)
        return (f"Successfully {'enabled' if enabled else 'disabled'} "
                f"versioning for bucket '{bucket_name}'")


@mcp.tool()
def storage_get_bucket_size(bucket_name: str) -> str:
    """Get size statistics for a Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b:
            return f"Error getting bucket size for '{bucket_name}': 404 Not Found"
        total_bytes = sum(o.get("size", 0) for o in b["objects"].values())
        n_obj = len(b["objects"])
        _record(s, "storage_get_bucket_size", bucket_name=bucket_name,
                total_objects=n_obj, total_size=total_bytes)
        _save_state(s)
        result = f"Bucket Size Statistics for '{bucket_name}':\n"
        result += f"Total Objects: {n_obj}\n"
        result += f"Total Size: {total_bytes} bytes\n"
        result += f"Size (MB): {total_bytes / (1024 * 1024):.2f} MB\n"
        result += f"Size (GB): {total_bytes / (1024 * 1024 * 1024):.2f} GB"
        return result


@mcp.tool()
def storage_set_bucket_lifecycle(bucket_name: str, age_days: int = 30,
                                 action: str = "Delete") -> str:
    """Set lifecycle rules for a Cloud Storage bucket.

    Args:
        bucket_name: Name of the bucket
        age_days: Age in days after which to apply the action
        action: Action to take (Delete, SetStorageClass)
    """
    if not validate_bucket_access(bucket_name):
        return (f"Access denied: Bucket '{bucket_name}' is not in allowed "
                f"buckets list")
    with _lock():
        s = _load_state()
        b = _bucket_or_404(s, bucket_name)
        if not b:
            return (f"Error setting lifecycle rules for bucket "
                    f"'{bucket_name}': 404 Not Found")
        b["lifecycle_rules"] = [{
            "action": {"type": action},
            "condition": {"age": int(age_days)},
        }]
        _record(s, "storage_set_bucket_lifecycle", bucket_name=bucket_name,
                age_days=age_days, action=action)
        _save_state(s)
        return (f"Successfully set lifecycle rule for bucket '{bucket_name}': "
                f"{action} objects after {age_days} days")


# ===========================================================================
# Cloud Logging tools
# ===========================================================================

@mcp.tool()
def logging_write_log(log_name: str, message: str,
                      severity: str = "INFO") -> str:
    """Write a log entry to Cloud Logging.

    Args:
        log_name: Name of the log
        message: Log message
        severity: Log severity (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    if not validate_log_bucket_access(log_name):
        return (f"Access denied: Log '{log_name}' is not in allowed log "
                f"buckets list")
    with _lock():
        s = _load_state()
        entry = {
            "log_name": f"projects/{PROJECT_ID}/logs/{log_name}",
            "severity": severity,
            "timestamp": _now(),
            "text_payload": message if isinstance(message, str) else None,
            "json_payload": message if not isinstance(message, str) else None,
            "resource": {"type": "global",
                         "labels": {"project_id": PROJECT_ID}},
        }
        s["logs"].setdefault(log_name, []).append(entry)
        _record(s, "logging_write_log", log_name=log_name, severity=severity)
        _save_state(s)
        return (f"Successfully wrote log entry to '{log_name}' with severity "
                f"'{severity}'")


@mcp.tool()
def logging_read_logs(log_filter: str = "", max_entries: int = 50) -> str:
    """Read recent log entries from Cloud Logging.

    Args:
        log_filter: Optional filter for log entries
        max_entries: Maximum number of entries to return (default: 50)
    """
    with _lock():
        s = _load_state()
        all_entries: list[dict] = []
        for entries in s["logs"].values():
            all_entries.extend(entries)
        all_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        # Crude filter: support `logName="projects/X/logs/Y"` substring
        # and `severity=...` substring; bigger filters fall through.
        if log_filter:
            f = log_filter.strip()
            m = re.search(r'logName\s*=\s*"([^"]+)"', f)
            if m:
                wanted = m.group(1)
                all_entries = [e for e in all_entries
                               if e.get("log_name") == wanted]
            m = re.search(r'severity\s*[=:]?\s*"?([A-Z]+)"?', f)
            if m:
                sev = m.group(1)
                all_entries = [e for e in all_entries
                               if (e.get("severity") or "INFO") == sev]
        if ALLOWED_LOG_BUCKETS:
            kept = []
            for e in all_entries:
                ln = e.get("log_name", "")
                simple = ln.split("/logs/")[-1] if "/logs/" in ln else ln
                if validate_log_bucket_access(simple):
                    kept.append(e)
            all_entries = kept
        all_entries = all_entries[: int(max_entries)]
        _record(s, "logging_read_logs", filter=log_filter, count=len(all_entries))
        _save_state(s)
        if not all_entries:
            return "No log entries found matching the filter criteria"
        result = f"Found {len(all_entries)} log entries:\n"
        for e in all_entries[:10]:
            msg = e.get("text_payload") or str(e.get("json_payload")) or "No message"
            result += (f"[{e.get('timestamp', 'Unknown')}] "
                       f"{e.get('severity', 'INFO')}: {msg}\n")
        if len(all_entries) > 10:
            result += f"... and {len(all_entries) - 10} more entries"
        return result


@mcp.tool()
def logging_list_logs() -> str:
    """List all log names in the project."""
    with _lock():
        s = _load_state()
        if ALLOWED_LOG_BUCKETS:
            buckets = [
                {"display_name": name, **lb}
                for name, lb in s["log_buckets"].items()
                if validate_log_bucket_access(name)
            ]
            if not buckets:
                return (f"No accessible log buckets found. Allowed buckets: "
                        f"{', '.join(ALLOWED_LOG_BUCKETS)}")
            log_names = sorted(s["logs"].keys())
            _record(s, "logging_list_logs",
                    bucket_count=len(buckets), log_count=len(log_names))
            _save_state(s)
            result = (f"Access restricted to {len(buckets)} log bucket(s):\n\n")
            for b in buckets:
                result += f"Log Bucket: {b['display_name']}\n"
                result += (f"  Full path: projects/{PROJECT_ID}/locations/"
                           f"{b.get('location', 'global')}/buckets/"
                           f"{b['display_name']}\n")
                result += f"  Retention: {b.get('retention_days', 30)} days\n"
                result += "  Recent log names (last 7 days):\n"
                for ln in log_names[:10]:
                    result += f"    - {ln}\n"
                if len(log_names) > 10:
                    result += f"    ... and {len(log_names) - 10} more\n"
                result += "\n"
            return result
        logs = sorted(s["logs"].keys())
        _record(s, "logging_list_logs", count=len(logs))
        _save_state(s)
        if not logs:
            return "No logs found"
        out = f"Found {len(logs)} logs:\n" + "\n".join(f"- {n}" for n in logs[:20])
        if len(logs) > 20:
            out += "..."
        return out


@mcp.tool()
def logging_delete_log(log_name: str) -> str:
    """Delete a log.

    Args:
        log_name: Name of the log to delete
    """
    if not validate_log_bucket_access(log_name):
        return (f"Access denied: Log '{log_name}' is not in allowed log "
                f"buckets list")
    with _lock():
        s = _load_state()
        if log_name not in s["logs"]:
            _record(s, "logging_delete_log", log_name=log_name,
                    result="not_found")
            _save_state(s)
            return f"Log '{log_name}' not found or could not be deleted"
        del s["logs"][log_name]
        _record(s, "logging_delete_log", log_name=log_name)
        _save_state(s)
        return f"Successfully deleted log '{log_name}'"


@mcp.tool()
def logging_create_log_sink(sink_name: str, destination: str,
                            log_filter: str = "") -> str:
    """Create a log sink to export logs to another service.

    Args:
        sink_name: Name for the sink
        destination: Destination (e.g., bigquery://project.dataset, storage://bucket-name)
        log_filter: Optional filter for which logs to export
    """
    with _lock():
        s = _load_state()
        s["log_sinks"][sink_name] = {
            "name": sink_name, "destination": destination,
            "filter": log_filter, "created": _now(),
        }
        _record(s, "logging_create_log_sink", sink_name=sink_name,
                destination=destination)
        _save_state(s)
        return (f"Successfully created log sink '{sink_name}' to destination "
                f"'{destination}'")


@mcp.tool()
def logging_list_log_sinks() -> str:
    """List all log sinks in the project."""
    with _lock():
        s = _load_state()
        sinks = list(s["log_sinks"].values())
        _record(s, "logging_list_log_sinks", count=len(sinks))
        _save_state(s)
        if not sinks:
            return "No log sinks found"
        result = f"Found {len(sinks)} log sinks:\n"
        for sink in sinks:
            result += f"- {sink.get('name', 'Unknown')}\n"
            result += f"  Destination: {sink.get('destination', 'Unknown')}\n"
            result += f"  Filter: {sink.get('filter') or 'None'}\n"
        return result


@mcp.tool()
def logging_delete_log_sink(sink_name: str) -> str:
    """Delete a log sink.

    Args:
        sink_name: Name of the sink to delete
    """
    with _lock():
        s = _load_state()
        if sink_name not in s["log_sinks"]:
            _record(s, "logging_delete_log_sink", sink_name=sink_name,
                    result="not_found")
            _save_state(s)
            return f"Log sink '{sink_name}' not found or could not be deleted"
        del s["log_sinks"][sink_name]
        _record(s, "logging_delete_log_sink", sink_name=sink_name)
        _save_state(s)
        return f"Successfully deleted log sink '{sink_name}'"


@mcp.tool()
def logging_export_logs_to_bigquery(dataset_id: str, table_id: str,
                                    log_filter: str = "",
                                    days_back: int = 1) -> str:
    """Export logs to BigQuery.

    Args:
        dataset_id: BigQuery dataset ID
        table_id: BigQuery table ID
        log_filter: Filter for which logs to export
        days_back: Number of days back to export (default: 1)
    """
    if not validate_dataset_access(dataset_id):
        return (f"Access denied: Dataset '{dataset_id}' is not in allowed "
                f"datasets list")
    with _lock():
        s = _load_state()
        # Aggregate matching entries and write them as a table.
        all_entries = [e for entries in s["logs"].values() for e in entries]
        schema = [
            {"name": "log_name", "type": "STRING", "mode": "NULLABLE"},
            {"name": "severity", "type": "STRING", "mode": "NULLABLE"},
            {"name": "timestamp", "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "text_payload", "type": "STRING", "mode": "NULLABLE"},
            {"name": "json_payload", "type": "JSON", "mode": "NULLABLE"},
        ]
        ds = s["datasets"].setdefault(dataset_id, {
            "dataset_id": dataset_id, "description": "",
            "location": "US", "created": _now(), "modified": _now(),
            "tables": {}, "labels": {}})
        ds["tables"][table_id] = {
            "table_id": table_id, "schema": schema,
            "num_rows": len(all_entries),
            "created": _now(), "modified": _now()}
        conn = _db()
        conn.execute(
            f'DROP TABLE IF EXISTS "{_sqlite_table_name(dataset_id, table_id)}"')
        _ensure_table(conn, dataset_id, table_id, schema)
        for e in all_entries:
            conn.execute(
                f'INSERT INTO "{_sqlite_table_name(dataset_id, table_id)}" '
                f'(log_name, severity, timestamp, text_payload, json_payload) '
                f'VALUES (?, ?, ?, ?, ?)',
                (e.get("log_name"), e.get("severity"), e.get("timestamp"),
                 e.get("text_payload"),
                 json.dumps(e.get("json_payload"))
                 if e.get("json_payload") is not None else None))
        conn.commit()
        conn.close()
        _record(s, "logging_export_logs_to_bigquery", dataset_id=dataset_id,
                table_id=table_id, rows=len(all_entries))
        _save_state(s)
        return (f"Successfully exported logs to BigQuery table "
                f"'{dataset_id}.{table_id}'")


@mcp.tool()
def logging_create_log_bucket(bucket_id: str, location: str = "global",
                              retention_days: int = 30) -> str:
    """Create a log bucket for storing logs.

    Args:
        bucket_id: ID for the log bucket
        location: Location for the bucket (default: global)
        retention_days: Log retention period in days (default: 30)
    """
    if not validate_log_bucket_access(bucket_id):
        return (f"Access denied: Log bucket '{bucket_id}' is not in allowed "
                f"log buckets list")
    with _lock():
        s = _load_state()
        s["log_buckets"][bucket_id] = {
            "name": (f"projects/{PROJECT_ID}/locations/{location}/buckets/"
                     f"{bucket_id}"),
            "location": location, "retention_days": retention_days,
            "created": _now(), "lifecycle_state": "ACTIVE",
        }
        _record(s, "logging_create_log_bucket", bucket_id=bucket_id,
                location=location)
        _save_state(s)
        return (f"Successfully created log bucket '{bucket_id}' with "
                f"{retention_days} days retention")


# ===========================================================================
# Compute Engine tools
# ===========================================================================

@mcp.tool()
def compute_list_instances(zone: str = "") -> str:
    """List Compute Engine instances.

    Args:
        zone: Optional zone filter, if empty lists from all zones
    """
    with _lock():
        s = _load_state()
        insts = list(s["instances"].values())
        if zone:
            insts = [i for i in insts if i.get("zone") == zone]
        insts = [i for i in insts if validate_instance_access(i["name"])]
        _record(s, "compute_list_instances", zone=zone, count=len(insts))
        _save_state(s)
        return (f"Found {len(insts)} instances:\n" + "\n".join(
            f"- {i['name']}: {i.get('status', 'Unknown')} in "
            f"{i.get('zone', 'Unknown zone')}" for i in insts))


@mcp.tool()
def compute_create_instance(instance_name: str, zone: str,
                            machine_type: str = "e2-micro") -> str:
    """Create a new Compute Engine instance.

    Args:
        instance_name: Name for the new instance
        zone: Zone where to create the instance
        machine_type: Machine type (default: e2-micro)
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        s["instances"][instance_name] = {
            "name": instance_name, "zone": zone,
            "machine_type": machine_type, "status": "RUNNING",
            "creation_timestamp": _now(),
            "internal_ip": "10.0.0.1", "external_ip": "34.0.0.1",
            "boot_disk_size_gb": 10, "tags": [], "labels": {},
        }
        _record(s, "compute_create_instance", instance_name=instance_name,
                zone=zone, machine_type=machine_type)
        _save_state(s)
        return (f"Successfully initiated creation of instance "
                f"'{instance_name}' in zone '{zone}'")


@mcp.tool()
def compute_delete_instance(instance_name: str, zone: str) -> str:
    """Delete a Compute Engine instance.

    Args:
        instance_name: Name of the instance to delete
        zone: Zone where the instance is located
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        if instance_name in s["instances"]:
            del s["instances"][instance_name]
        _record(s, "compute_delete_instance", instance_name=instance_name,
                zone=zone)
        _save_state(s)
        return (f"Successfully initiated deletion of instance "
                f"'{instance_name}' in zone '{zone}'")


def _set_status(s: dict, name: str, status: str) -> None:
    if name in s["instances"]:
        s["instances"][name]["status"] = status


@mcp.tool()
def compute_start_instance(instance_name: str, zone: str) -> str:
    """Start a Compute Engine instance.

    Args:
        instance_name: Name of the instance to start
        zone: Zone where the instance is located
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        _set_status(s, instance_name, "RUNNING")
        _record(s, "compute_start_instance", instance_name=instance_name,
                zone=zone)
        _save_state(s)
        return (f"Successfully initiated start of instance "
                f"'{instance_name}' in zone '{zone}'")


@mcp.tool()
def compute_stop_instance(instance_name: str, zone: str) -> str:
    """Stop a Compute Engine instance.

    Args:
        instance_name: Name of the instance to stop
        zone: Zone where the instance is located
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        _set_status(s, instance_name, "TERMINATED")
        _record(s, "compute_stop_instance", instance_name=instance_name,
                zone=zone)
        _save_state(s)
        return (f"Successfully initiated stop of instance "
                f"'{instance_name}' in zone '{zone}'")


@mcp.tool()
def compute_restart_instance(instance_name: str, zone: str) -> str:
    """Restart a Compute Engine instance.

    Args:
        instance_name: Name of the instance to restart
        zone: Zone where the instance is located
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        inst = s["instances"].get(instance_name)
        if inst and inst.get("status") not in ("RUNNING", "STOPPING"):
            return (f"Cannot restart instance '{instance_name}': current "
                    f"status is '{inst.get('status')}'. Instance must be "
                    f"RUNNING or STOPPING to restart.")
        _set_status(s, instance_name, "RUNNING")
        _record(s, "compute_restart_instance", instance_name=instance_name,
                zone=zone)
        _save_state(s)
        return (f"Successfully initiated restart of instance "
                f"'{instance_name}' in zone '{zone}'")


@mcp.tool()
def compute_get_instance(instance_name: str, zone: str) -> str:
    """Get detailed information about a Compute Engine instance.

    Args:
        instance_name: Name of the instance
        zone: Zone where the instance is located
    """
    if not validate_instance_access(instance_name):
        return (f"Access denied: Instance '{instance_name}' is not in allowed "
                f"instances list")
    with _lock():
        s = _load_state()
        i = s["instances"].get(instance_name)
        _record(s, "compute_get_instance", instance_name=instance_name,
                zone=zone, result="ok" if i else "not_found")
        _save_state(s)
        if not i:
            return (f"Error getting instance info for '{instance_name}': 404 "
                    f"Not Found")
        result = f"Instance Information for '{instance_name}':\n"
        result += f"Status: {i.get('status', 'Unknown')}\n"
        result += f"Zone: {i.get('zone', 'Unknown')}\n"
        result += f"Machine Type: {i.get('machine_type', 'Unknown')}\n"
        result += f"Created: {i.get('creation_timestamp', 'Unknown')}\n"
        result += f"Internal IP: {i.get('internal_ip', 'None')}\n"
        result += f"External IP: {i.get('external_ip', 'None')}\n"
        result += f"Boot Disk: {i.get('boot_disk_size_gb', 'Unknown')} GB\n"
        result += f"Network Tags: {i.get('tags', [])}\n"
        result += f"Labels: {i.get('labels', {})}"
        return result


@mcp.tool()
def compute_list_zones() -> str:
    """List all available Compute Engine zones."""
    zones = [
        "us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f",
        "us-east1-b", "us-east1-c", "us-east1-d", "us-west1-a",
        "us-west1-b", "us-west1-c", "europe-west1-b", "europe-west1-c",
        "europe-west1-d", "asia-east1-a", "asia-east1-b", "asia-east1-c",
        "asia-northeast1-a", "asia-northeast1-b", "asia-northeast1-c",
        "asia-southeast1-a", "asia-southeast1-b",
    ]
    with _lock():
        s = _load_state()
        _record(s, "compute_list_zones", count=len(zones))
        _save_state(s)
    out = (f"Found {len(zones)} available zones:\n"
           + "\n".join(f"- {z}" for z in zones[:20]))
    if len(zones) > 20:
        out += "..."
    return out


@mcp.tool()
def compute_wait_for_operation(operation_name: str, zone: str,
                               timeout_minutes: int = 5) -> str:
    """Wait for a Compute Engine operation to complete.

    Args:
        operation_name: Name of the operation to wait for
        zone: Zone where the operation is running
        timeout_minutes: Maximum time to wait in minutes (default: 5)
    """
    # Mock: operations complete instantly.
    with _lock():
        s = _load_state()
        _record(s, "compute_wait_for_operation",
                operation_name=operation_name, zone=zone)
        _save_state(s)
    return f"Operation '{operation_name}' completed successfully"


# ===========================================================================
# Mock-only debug tools (not exposed by the real google-cloud-mcp)
# ===========================================================================

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state dict (no SQLite contents)."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed_bucket_object")
def mock_debug_seed_bucket_object(bucket_name: str, object_name: str,
                                  content_b64: str | None = None,
                                  text: str | None = None,
                                  content_type: str = "application/octet-stream"
                                  ) -> dict:
    """Mock-only: insert an object into a bucket bypassing allowlists.
    Either `content_b64` (raw bytes) or `text` may be given."""
    with _lock():
        s = _load_state()
        if content_b64 is None and text is not None:
            content_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        elif content_b64 is None:
            content_b64 = ""
        size = len(base64.b64decode(content_b64))
        s["buckets"].setdefault(bucket_name, {
            "name": bucket_name, "location": "US", "storage_class": "STANDARD",
            "created": _now(), "versioning_enabled": False, "labels": {},
            "lifecycle_rules": [], "objects": {}})
        s["buckets"][bucket_name]["objects"][object_name] = {
            "name": object_name, "size": size,
            "content_b64": content_b64,
            "content_type": content_type, "updated": _now()}
        _record(s, "debug_seed_bucket_object", bucket_name=bucket_name,
                object_name=object_name, bytes=size)
        _save_state(s)
        return {"ok": True, "bucket": bucket_name, "object": object_name,
                "size": size}


@_debug_tool(name="mock_debug_seed_dataset")
def mock_debug_seed_dataset(dataset_id: str, location: str = "US",
                            description: str = "") -> dict:
    """Mock-only: create a dataset bypassing the allowlist."""
    with _lock():
        s = _load_state()
        s["datasets"][dataset_id] = s["datasets"].get(dataset_id) or {
            "dataset_id": dataset_id, "description": description,
            "location": location, "created": _now(), "modified": _now(),
            "tables": {}, "labels": {}}
        _record(s, "debug_seed_dataset", dataset_id=dataset_id)
        _save_state(s)
        return {"ok": True, "dataset_id": dataset_id}


@_debug_tool(name="mock_debug_seed_table")
def mock_debug_seed_table(dataset_id: str, table_id: str,
                          schema: list, rows: list | None = None) -> dict:
    """Mock-only: create a BQ table with schema (and optional rows)
    bypassing the allowlist. Schema is a list of {name,type,mode?}
    dicts; rows is a list of dicts keyed by column name."""
    with _lock():
        s = _load_state()
        s["datasets"].setdefault(dataset_id, {
            "dataset_id": dataset_id, "description": "",
            "location": "US", "created": _now(), "modified": _now(),
            "tables": {}, "labels": {}})
        s["datasets"][dataset_id]["tables"][table_id] = {
            "table_id": table_id, "schema": schema,
            "num_rows": 0, "created": _now(), "modified": _now()}
        conn = _db()
        conn.execute(
            f'DROP TABLE IF EXISTS "{_sqlite_table_name(dataset_id, table_id)}"')
        _ensure_table(conn, dataset_id, table_id, schema)
        cols = ", ".join(f'"{f["name"]}"' for f in schema)
        ph = ", ".join("?" for _ in schema)
        sql = (f'INSERT INTO "{_sqlite_table_name(dataset_id, table_id)}" '
               f'({cols}) VALUES ({ph})')
        for row in rows or []:
            conn.execute(sql, _coerce_row(row, schema))
        conn.commit()
        n = conn.execute(
            f'SELECT COUNT(*) FROM "{_sqlite_table_name(dataset_id, table_id)}"'
        ).fetchone()[0]
        conn.close()
        s["datasets"][dataset_id]["tables"][table_id]["num_rows"] = n
        _record(s, "debug_seed_table", dataset_id=dataset_id,
                table_id=table_id, rows=n)
        _save_state(s)
        return {"ok": True, "dataset_id": dataset_id,
                "table_id": table_id, "num_rows": n}


@_debug_tool(name="mock_debug_seed_log_bucket")
def mock_debug_seed_log_bucket(bucket_id: str, location: str = "global",
                               retention_days: int = 30) -> dict:
    """Mock-only: create a log bucket bypassing the allowlist."""
    with _lock():
        s = _load_state()
        s["log_buckets"][bucket_id] = {
            "name": (f"projects/{PROJECT_ID}/locations/{location}/buckets/"
                     f"{bucket_id}"),
            "location": location, "retention_days": retention_days,
            "created": _now(), "lifecycle_state": "ACTIVE"}
        _record(s, "debug_seed_log_bucket", bucket_id=bucket_id)
        _save_state(s)
        return {"ok": True, "bucket_id": bucket_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Google Cloud mock MCP server")
    parser.add_argument("--project-id", default="mock-project")
    parser.add_argument("--service-account-path", default=None)
    parser.add_argument("--allowed-buckets", default="")
    parser.add_argument("--allowed-datasets", default="")
    parser.add_argument("--allowed-log-buckets", default="")
    parser.add_argument("--allowed-instances", default="")
    args = parser.parse_args()

    global PROJECT_ID, SERVICE_ACCOUNT_PATH, ALLOWED_BUCKETS, ALLOWED_DATASETS
    global ALLOWED_LOG_BUCKETS, ALLOWED_INSTANCES
    PROJECT_ID = args.project_id
    SERVICE_ACCOUNT_PATH = args.service_account_path
    parse_csv = lambda x: set(s.strip() for s in x.split(",") if s.strip())
    ALLOWED_BUCKETS = parse_csv(args.allowed_buckets)
    ALLOWED_DATASETS = parse_csv(args.allowed_datasets)
    ALLOWED_LOG_BUCKETS = parse_csv(args.allowed_log_buckets)
    ALLOWED_INSTANCES = parse_csv(args.allowed_instances)

    print(f"[google-cloud-mock] project={PROJECT_ID}, "
          f"state_dir={_state_dir()}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
