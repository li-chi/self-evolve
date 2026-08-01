"""BigQuery-on-SQLite compatibility layer.

The google-cloud-mock executes BigQuery SQL on SQLite. SQLite's dialect
differs from BigQuery's in ways that silently change results, which would
make a mocked task grade differently from the real one. This module closes
the gaps that matter:

  * `/` is integer division in SQLite, float division in BigQuery
    (`SUM(clicks)/SUM(views)` → 0 instead of 0.25). Rewritten to force
    float, skipping string literals and comments.
  * BigQuery-only functions/syntax: SAFE_DIVIDE, COUNTIF, IFNULL/NULLIF
    variants, EXTRACT(part FROM x), DATE()/DATETIME(), SAFE_CAST,
    CAST(x AS INT64/FLOAT64/STRING/BOOL), LOGICAL_AND/LOGICAL_OR,
    CURRENT_DATE/CURRENT_TIMESTAMP, TIMESTAMP_DIFF/DATE_DIFF, ROUND with
    negative digits, STDDEV/VARIANCE, STRING_AGG.

VENDORED COPY: this file is duplicated verbatim at
mocks/gcp-sdk-shim/google/cloud/bq_sqlite.py so the SDK shim (used by
upstream preprocess + graders) and the MCP server (used by the agent)
evaluate identical SQL. Edit both together.
"""

from __future__ import annotations

import datetime
import math
import re
import sqlite3
import statistics

__all__ = ["prepare_query", "rewrite_struct_access",
           "register_functions", "connect_ready", "sync_catalog",
           "is_write_statement"]


# ---------------------------------------------------------------------------
# tokenizer: yields (is_code, text) so rewrites never touch strings/comments
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""(?P<sq>'(?:[^'\\]|\\.|'')*')      # single-quoted string
      | (?P<dq>"(?:[^"\\]|\\.|"")*")      # double-quoted identifier/string
      | (?P<bt>`[^`]*`)                   # backtick identifier
      | (?P<lc>--[^\n]*)                  # line comment
      | (?P<bc>/\*.*?\*/)                 # block comment
    """,
    re.VERBOSE | re.DOTALL,
)


def _segments(sql: str):
    pos = 0
    for m in _TOKEN_RE.finditer(sql):
        if m.start() > pos:
            yield True, sql[pos:m.start()]
        yield False, m.group(0)
        pos = m.end()
    if pos < len(sql):
        yield True, sql[pos:]


def _map_code(sql: str, fn) -> str:
    return "".join(fn(t) if code else t for code, t in _segments(sql))


# ---------------------------------------------------------------------------
# rewrites
# ---------------------------------------------------------------------------

def _force_float_division(code: str) -> str:
    """`a / b` -> `a * 1.0 / b` (BigQuery divides as FLOAT64)."""
    return re.sub(r"(?<![/*])/(?![/*])", "* 1.0 /", code)


_CAST_TYPES = {
    "INT64": "INTEGER", "INTEGER": "INTEGER", "SMALLINT": "INTEGER",
    "BIGINT": "INTEGER", "FLOAT64": "REAL", "FLOAT": "REAL",
    "NUMERIC": "REAL", "BIGNUMERIC": "REAL", "DECIMAL": "REAL",
    "STRING": "TEXT", "BYTES": "BLOB", "BOOL": "INTEGER",
    "BOOLEAN": "INTEGER",
}


def _rewrite_cast_types(code: str) -> str:
    def sub(m):
        return f"{m.group(1)}{_CAST_TYPES[m.group(2).upper()]}"
    return re.sub(
        r"(\bAS\s+)(" + "|".join(_CAST_TYPES) + r")\b",
        sub, code, flags=re.IGNORECASE)


def _split_args(inner: str) -> list:
    """Split a call's argument list on top-level commas."""
    args, depth, cur, in_str, quote = [], 0, [], False, ""
    for ch in inner:
        if in_str:
            cur.append(ch)
            if ch == quote:
                in_str = False
            continue
        if ch in "'\"":
            in_str, quote = True, ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur).strip())
    return args


def _rewrite_call(code: str, name: str, builder) -> str:
    """Rewrite `NAME(...)` with balanced-paren awareness."""
    pattern = re.compile(r"\b" + name + r"\s*\(", re.IGNORECASE)
    out, pos = [], 0
    while True:
        m = pattern.search(code, pos)
        if not m:
            out.append(code[pos:])
            return "".join(out)
        start = m.start()
        depth, i = 1, m.end()
        while i < len(code) and depth:
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
            i += 1
        inner = code[m.end():i - 1]
        out.append(code[pos:start])
        out.append(builder(_split_args(inner), inner))
        pos = i


def _rewrite_countif(code: str) -> str:
    return _rewrite_call(
        code, "COUNTIF",
        lambda args, inner: f"SUM(CASE WHEN ({inner}) THEN 1 ELSE 0 END)")


def _rewrite_logical_agg(code: str) -> str:
    code = _rewrite_call(
        code, "LOGICAL_AND",
        lambda a, inner: f"MIN(CASE WHEN ({inner}) THEN 1 ELSE 0 END)")
    return _rewrite_call(
        code, "LOGICAL_OR",
        lambda a, inner: f"MAX(CASE WHEN ({inner}) THEN 1 ELSE 0 END)")


def _rewrite_string_agg(code: str) -> str:
    return _rewrite_call(
        code, "STRING_AGG",
        lambda args, inner: f"GROUP_CONCAT({inner})")


_EXTRACT_PARTS = {
    "YEAR": "%Y", "MONTH": "%m", "DAY": "%d", "HOUR": "%H",
    "MINUTE": "%M", "SECOND": "%S", "DAYOFYEAR": "%j", "DAYOFWEEK": "%w",
    "WEEK": "%W",
}


def _rewrite_extract(code: str) -> str:
    def builder(args, inner):
        m = re.match(r"\s*([A-Za-z_]+)\s+FROM\s+(.+)$", inner,
                     re.IGNORECASE | re.DOTALL)
        if not m:
            return f"EXTRACT({inner})"
        part, expr = m.group(1).upper(), m.group(2)
        fmt = _EXTRACT_PARTS.get(part)
        if not fmt:
            return f"EXTRACT({inner})"
        cast = "" if part == "DAYOFWEEK" else ""
        return f"CAST(strftime('{fmt}', {expr}) AS INTEGER){cast}"
    return _rewrite_call(code, "EXTRACT", builder)


def rewrite_struct_access(sql: str, record_columns) -> str:
    """`scores.online_score` -> `json_extract("scores", '$.online_score')`.

    BigQuery RECORD columns are stored as JSON text in SQLite, so dotted
    field access has to become a JSON lookup. `record_columns` is the set of
    RECORD column names on the tables the statement references; anything
    else keeps its dotted form (it is a table/alias qualifier).
    """
    if not record_columns:
        return sql
    alt = "|".join(sorted((re.escape(c) for c in record_columns),
                          key=len, reverse=True))
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.])(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
        r"(?P<col>" + alt + r")\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)")

    def sub(m):
        return (f"json_extract(\"{m.group('col')}\", "
                f"'$.{m.group('field')}')")

    return _map_code(sql, lambda code: pattern.sub(sub, code))


_DIFF_FNS = ("TIMESTAMP_DIFF", "DATETIME_DIFF", "DATE_DIFF", "TIME_DIFF")
_UNITS = ("MICROSECOND", "MILLISECOND", "SECOND", "MINUTE", "HOUR", "DAY",
          "WEEK", "MONTH", "QUARTER", "YEAR")


def _quote_diff_units(code: str) -> str:
    """`DATE_DIFF(a, b, DAY)` -> `DATE_DIFF(a, b, 'DAY')`.

    BigQuery takes the date part as a keyword; SQLite would read it as a
    column name.
    """
    for fn in _DIFF_FNS:
        def builder(args, inner, _fn=fn):
            if len(args) == 3 and args[2].upper() in _UNITS:
                args = args[:2] + [f"'{args[2].upper()}'"]
            return f"{_fn}({', '.join(args)})"
        code = _rewrite_call(code, fn, builder)
    return code


def _rewrite_interval(code: str) -> str:
    """`INTERVAL 30 DAY` -> `30, 'DAY'` inside DATE_ADD/DATE_SUB calls."""
    return re.sub(
        r"\bINTERVAL\s+(\d+)\s+(" + "|".join(_UNITS) + r")\b",
        lambda m: f"{m.group(1)}, '{m.group(2).upper()}'",
        code, flags=re.IGNORECASE)


def prepare_query(sql: str) -> str:
    """Translate BigQuery SQL into SQLite-executable SQL."""
    def code_fixes(code: str) -> str:
        code = _rewrite_extract(code)
        code = _quote_diff_units(code)
        code = _rewrite_interval(code)
        code = _rewrite_countif(code)
        code = _rewrite_logical_agg(code)
        code = _rewrite_string_agg(code)
        code = _rewrite_cast_types(code)
        code = _force_float_division(code)
        return code
    return _map_code(sql, code_fixes)


# ---------------------------------------------------------------------------
# user-defined functions
# ---------------------------------------------------------------------------

def _safe_divide(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return None if b == 0 else a / b


def _to_dt(v):
    if v is None:
        return None
    s = str(v).replace("Z", "+00:00").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _date(v, *_a):
    dt = _to_dt(v)
    return dt.strftime("%Y-%m-%d") if dt else None


def _datetime_fn(v, *_a):
    dt = _to_dt(v)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


_DIFF_UNITS = {
    "SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400,
}


def _ts_diff(a, b, unit):
    da, db = _to_dt(a), _to_dt(b)
    if not da or not db:
        return None
    secs = (da - db).total_seconds()
    u = str(unit).upper()
    if u in _DIFF_UNITS:
        return int(secs // _DIFF_UNITS[u])
    if u == "WEEK":
        return int(secs // (86400 * 7))
    if u == "MONTH":
        return (da.year - db.year) * 12 + (da.month - db.month)
    if u == "YEAR":
        return da.year - db.year
    return None


_ADD_UNITS = {"SECOND": "seconds", "MINUTE": "minutes", "HOUR": "hours",
              "DAY": "days", "WEEK": "weeks"}


def _date_add(value, n, unit):
    dt = _to_dt(value)
    if dt is None:
        return None
    u = str(unit).upper()
    if u in _ADD_UNITS:
        dt = dt + datetime.timedelta(**{_ADD_UNITS[u]: int(n)})
    elif u == "MONTH":
        month = dt.month - 1 + int(n)
        dt = dt.replace(year=dt.year + month // 12, month=month % 12 + 1)
    elif u == "YEAR":
        dt = dt.replace(year=dt.year + int(n))
    else:
        return None
    has_time = len(str(value)) > 10
    return dt.strftime("%Y-%m-%d %H:%M:%S") if has_time \
        else dt.strftime("%Y-%m-%d")


def _safe_cast(v, _type=None):
    return v


def _round(v, digits=0):
    try:
        return round(float(v), int(digits))
    except (TypeError, ValueError):
        return None


class _Agg:
    """Base for aggregate UDFs collecting numeric values."""

    def __init__(self):
        self.values = []

    def step(self, value):
        try:
            if value is not None:
                self.values.append(float(value))
        except (TypeError, ValueError):
            pass


class _StdDevSamp(_Agg):
    def finalize(self):
        return statistics.stdev(self.values) if len(self.values) > 1 else None


class _StdDevPop(_Agg):
    def finalize(self):
        return statistics.pstdev(self.values) if self.values else None


class _VarSamp(_Agg):
    def finalize(self):
        return statistics.variance(self.values) if len(self.values) > 1 \
            else None


class _VarPop(_Agg):
    def finalize(self):
        return statistics.pvariance(self.values) if self.values else None


def register_functions(conn: sqlite3.Connection) -> None:
    """Install BigQuery-compatible scalar/aggregate functions."""
    conn.create_function("SAFE_DIVIDE", 2, _safe_divide)
    conn.create_function("IEEE_DIVIDE", 2, _safe_divide)
    conn.create_function("DATE", -1, _date)
    conn.create_function("DATETIME", -1, _datetime_fn)
    conn.create_function("TIMESTAMP", 1, lambda v: v)
    conn.create_function("PARSE_DATE", 2, lambda _f, v: _date(v))
    conn.create_function("FORMAT_DATE", 2,
                         lambda f, v: (_to_dt(v).strftime(
                             f.replace("%%", "%")) if _to_dt(v) else None))
    conn.create_function("TIMESTAMP_DIFF", 3, _ts_diff)
    conn.create_function("DATETIME_DIFF", 3, _ts_diff)
    conn.create_function("DATE_DIFF", 3, _ts_diff)
    conn.create_function("SAFE_CAST", 2, _safe_cast)
    conn.create_function("DATE_ADD", 3, _date_add)
    conn.create_function("DATE_SUB", 3, lambda v, n, u: _date_add(v, -n, u))
    conn.create_function("DATETIME_ADD", 3, _date_add)
    conn.create_function("DATETIME_SUB", 3,
                         lambda v, n, u: _date_add(v, -n, u))
    conn.create_function("TIMESTAMP_ADD", 3, _date_add)
    conn.create_function("TIMESTAMP_SUB", 3,
                         lambda v, n, u: _date_add(v, -n, u))
    conn.create_function("ROUND", 2, _round)
    conn.create_function("POW", 2, lambda a, b: math.pow(a, b))
    conn.create_function("POWER", 2, lambda a, b: math.pow(a, b))
    conn.create_function("SQRT", 1,
                         lambda a: math.sqrt(a) if a is not None and a >= 0
                         else None)
    conn.create_function("CURRENT_DATE", -1,
                         lambda *_a: datetime.date.today().isoformat())
    conn.create_function(
        "CURRENT_TIMESTAMP", -1,
        lambda *_a: datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S"))
    conn.create_function("GENERATE_UUID", 0,
                         lambda: __import__("uuid").uuid4().hex)
    conn.create_aggregate("STDDEV", 1, _StdDevSamp)
    conn.create_aggregate("STDDEV_SAMP", 1, _StdDevSamp)
    conn.create_aggregate("STDDEV_POP", 1, _StdDevPop)
    conn.create_aggregate("VARIANCE", 1, _VarSamp)
    conn.create_aggregate("VAR_SAMP", 1, _VarSamp)
    conn.create_aggregate("VAR_POP", 1, _VarPop)


def connect_ready(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    return conn


# ---------------------------------------------------------------------------
# catalog sync
# ---------------------------------------------------------------------------

# A column's declared type may be SQLite's (tables the mock created) or
# BigQuery's (tables an agent created with `CREATE TABLE ... FLOAT64`).
_SQLITE_TO_BQ = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "INT64": "INTEGER",
    "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "REAL": "FLOAT", "FLOAT": "FLOAT", "FLOAT64": "FLOAT",
    "DOUBLE": "FLOAT", "NUMERIC": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC",
    "DECIMAL": "NUMERIC",
    "TEXT": "STRING", "STRING": "STRING", "VARCHAR": "STRING",
    "CHAR": "STRING",
    "BLOB": "BYTES", "BYTES": "BYTES",
    "BOOL": "BOOLEAN", "BOOLEAN": "BOOLEAN",
    "DATE": "DATE", "DATETIME": "DATETIME", "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMP", "JSON": "JSON",
    "": "STRING",
}


def sync_catalog(conn, state, pairs, sqlite_name, now_fn) -> None:
    """Reconcile dataset/table metadata with what SQL actually did.

    BigQuery registers tables created by DDL (`CREATE TABLE ds.t AS SELECT
    ...`) and updates row counts on DML; the mock stores rows in SQLite and
    metadata in state.json, so after a non-SELECT statement the two must be
    reconciled or `get_table` / `list_tables` would not see the agent's work.

    `pairs` is the (dataset, table) list the statement referenced.
    """
    for ds_id, tbl_id in pairs:
        name = sqlite_name(ds_id, tbl_id)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        ds = state.setdefault("datasets", {}).get(ds_id)
        if row is None:
            if ds and tbl_id in ds.get("tables", {}):
                del ds["tables"][tbl_id]
            continue
        if ds is None:
            ds = state["datasets"][ds_id] = {
                "dataset_id": ds_id, "description": "", "location": "US",
                "created": now_fn(), "modified": now_fn(),
                "tables": {}, "labels": {},
            }
        entry = ds.setdefault("tables", {}).get(tbl_id) or {}
        # SQLite only remembers an affinity, so a resync must not clobber the
        # richer BigQuery type already recorded for a column (RECORD, DATE,
        # TIMESTAMP, ...). Keep known columns as they are; only pick up
        # columns the statement added.
        known = {f["name"]: f for f in entry.get("schema", [])}
        schema = []
        for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall():
            col = r[1]
            if col in known:
                schema.append(known[col])
            else:
                schema.append({
                    "name": col,
                    "type": _SQLITE_TO_BQ.get(
                        (r[2] or "").upper().split("(")[0].strip(), "STRING"),
                    "mode": "NULLABLE"})
        n_rows = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        entry = entry or {"table_id": tbl_id, "created": now_fn()}
        entry.setdefault("created", now_fn())
        entry.update({"table_id": tbl_id, "schema": schema,
                      "num_rows": n_rows, "modified": now_fn()})
        ds["tables"][tbl_id] = entry


_WRITE_STMT = ("CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
               "REPLACE", "MERGE", "TRUNCATE")


def is_write_statement(sql: str) -> bool:
    for line in sql.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        return line.split(None, 1)[0].upper() in _WRITE_STMT
    return False
