"""`snowflake.connector` shim backed by the snowflake-mock state.

Upstream preprocess and graders open a Snowflake connection and run SQL.
The mock keeps table data in the same SQLite file its MCP server queries,
so a table the agent writes through its tools is the table the grader
selects from. Only the DB-API surface Toolathlon uses is implemented:
connect(), cursor(), execute(), fetchone/fetchall/fetchmany, description,
rowcount, context managers.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/mocks/snowflake-mock")
import server as sf  # noqa: E402


class Error(Exception):
    pass


class ProgrammingError(Error):
    pass


class DatabaseError(Error):
    pass


def _interpolate(sql: str, params) -> str:
    """Inline bound parameters; the mock executor takes plain SQL."""
    def quote(v):
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    if isinstance(params, dict):
        for key, value in params.items():
            sql = sql.replace(f"%({key})s", quote(value))
        return sql
    out, rest = [], sql
    for value in params:
        head, sep, rest = rest.partition("%s") if "%s" in rest \
            else rest.partition("?")
        out.append(head + (quote(value) if sep else ""))
    return "".join(out) + rest


class Cursor:
    def __init__(self, connection):
        self._conn = connection
        self._rows: list = []
        self._pos = 0
        self.description = None
        self.rowcount = -1

    def execute(self, sql: str, params=None):
        # Go through the mock's own executor so Snowflake semantics (USE,
        # qualified identifiers, catalog DDL) behave as they do for the
        # agent's MCP tools, instead of hitting raw SQLite here.
        if params:
            sql = _interpolate(sql, params)
        with sf._lock():
            state = sf._load_state()
            try:
                rows, _data_id = sf._execute(
                    sql, state,
                    default_db=self._conn.database,
                    default_schema=self._conn.schema)
            except Exception as e:  # noqa: BLE001 - DB-API contract
                raise ProgrammingError(str(e)) from e
            sf._save_state(state)
        columns = list(rows[0].keys()) if rows else []
        self.description = [(c, None, None, None, None, None, None)
                            for c in columns]
        self._rows = [tuple(r.get(c) for c in columns) for r in rows]
        self.rowcount = len(self._rows)
        self._columns = columns
        self._pos = 0
        return self

    def fetch_pandas_all(self):
        import pandas as pd
        return pd.DataFrame(self._rows, columns=getattr(self, "_columns", []))

    def executemany(self, sql: str, seq):
        for params in seq:
            self.execute(sql, params)
        return self

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1):
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def close(self):
        self._rows = []

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Connection:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self.database = kwargs.get("database")
        self.schema = kwargs.get("schema")
        self.warehouse = kwargs.get("warehouse")
        self.role = kwargs.get("role")

    def cursor(self, *_a, **_kw):
        return Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(**kwargs):
    return Connection(**kwargs)


paramstyle = "qmark"
apilevel = "2.0"
threadsafety = 2
