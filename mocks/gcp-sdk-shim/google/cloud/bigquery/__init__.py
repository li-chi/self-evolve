"""`google.cloud.bigquery` shim backed by the google-cloud-mock state.

Only the surface Toolathlon's preprocess + graders actually use is
implemented; anything else raises so a gap is loud rather than silent.
Data lands in the same SQLite file the MCP mock queries, and dataset /
table metadata in the same state.json, so the agent's `bigquery_*` MCP
tools and this shim are two views of one store.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
from typing import Any

from .. import _mockstate as ms
from ..exceptions import Conflict, NotFound

__all__ = [
    "Client", "Dataset", "DatasetReference", "Table", "TableReference",
    "SchemaField", "LoadJobConfig", "SourceFormat", "WriteDisposition",
    "TimePartitioning", "TimePartitioningType", "QueryJobConfig",
    "ScalarQueryParameter",
]


class SourceFormat:
    CSV = "CSV"
    NEWLINE_DELIMITED_JSON = "NEWLINE_DELIMITED_JSON"
    JSON = "NEWLINE_DELIMITED_JSON"
    PARQUET = "PARQUET"
    AVRO = "AVRO"


class WriteDisposition:
    WRITE_TRUNCATE = "WRITE_TRUNCATE"
    WRITE_APPEND = "WRITE_APPEND"
    WRITE_EMPTY = "WRITE_EMPTY"


class CreateDisposition:
    CREATE_IF_NEEDED = "CREATE_IF_NEEDED"
    CREATE_NEVER = "CREATE_NEVER"


class TimePartitioningType:
    DAY = "DAY"
    HOUR = "HOUR"
    MONTH = "MONTH"
    YEAR = "YEAR"


class TimePartitioning:
    def __init__(self, type_=TimePartitioningType.DAY, field=None,
                 expiration_ms=None, require_partition_filter=None):
        self.type_ = type_
        self.field = field
        self.expiration_ms = expiration_ms
        self.require_partition_filter = require_partition_filter


class SchemaField:
    def __init__(self, name, field_type, mode="NULLABLE", description=None,
                 fields=(), **_kw):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.description = description
        self.fields = tuple(fields)

    def to_api_repr(self) -> dict:
        return {"name": self.name, "type": self.field_type, "mode": self.mode}

    def __repr__(self):
        return f"SchemaField({self.name!r}, {self.field_type!r}, {self.mode!r})"


def _schema_to_dicts(schema) -> list:
    out = []
    for f in schema or []:
        if isinstance(f, SchemaField):
            out.append(f.to_api_repr())
        elif isinstance(f, dict):
            out.append({"name": f["name"],
                        "type": f.get("type", "STRING"),
                        "mode": f.get("mode", "NULLABLE")})
    return out


def _dicts_to_schema(dicts) -> list:
    return [SchemaField(d["name"], d.get("type", "STRING"),
                        d.get("mode", "NULLABLE")) for d in dicts or []]


class LoadJobConfig:
    def __init__(self, source_format=None, skip_leading_rows=0,
                 autodetect=False, write_disposition=None, schema=None,
                 create_disposition=None, field_delimiter=",",
                 allow_quoted_newlines=False, max_bad_records=0,
                 time_partitioning=None, **_kw):
        self.source_format = source_format
        self.skip_leading_rows = skip_leading_rows
        self.autodetect = autodetect
        self.write_disposition = write_disposition or \
            WriteDisposition.WRITE_APPEND
        self.schema = schema
        self.create_disposition = create_disposition
        self.field_delimiter = field_delimiter
        self.allow_quoted_newlines = allow_quoted_newlines
        self.max_bad_records = max_bad_records
        self.time_partitioning = time_partitioning


class QueryJobConfig:
    def __init__(self, **kw):
        self.query_parameters = []
        self.__dict__.update(kw)


class ScalarQueryParameter:
    """Named scalar parameter, substituted into the SQL text by the shim's
    query() (SQLite has no @name binding for BigQuery-style parameters)."""

    def __init__(self, name, type_, value):
        self.name = name
        self.type_ = (type_ or "STRING").upper()
        self.value = value

    def sql_literal(self):
        if self.value is None:
            return "NULL"
        if self.type_ in ("INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC"):
            return str(self.value)
        if self.type_ in ("BOOL", "BOOLEAN"):
            return "TRUE" if self.value else "FALSE"
        return "'" + str(self.value).replace("'", "''") + "'"


class DatasetReference:
    def __init__(self, project, dataset_id):
        self.project = project
        self.dataset_id = dataset_id

    def table(self, table_id):
        return TableReference(self, table_id)

    def __str__(self):
        return f"{self.project}.{self.dataset_id}"


class TableReference:
    def __init__(self, dataset_ref, table_id):
        self.dataset_id = dataset_ref.dataset_id
        self.project = dataset_ref.project
        self.table_id = table_id

    def __str__(self):
        return f"{self.project}.{self.dataset_id}.{self.table_id}"


def _split_ref(ref, default_project, kind="table") -> tuple:
    """Normalise anything ref-ish into (project, dataset_id, table_id|None).

    A two-part string is ambiguous and the real SDK resolves it by the call
    being made: `get_dataset("project.dataset")` versus
    `get_table("dataset.table")`. `kind` selects the same reading.
    """
    if isinstance(ref, TableReference):
        return ref.project, ref.dataset_id, ref.table_id
    if isinstance(ref, Table):
        return ref.project, ref.dataset_id, ref.table_id
    if isinstance(ref, DatasetReference):
        return ref.project, ref.dataset_id, None
    if isinstance(ref, Dataset):
        return ref.project, ref.dataset_id, None
    parts = str(ref).split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        if kind == "dataset":
            return parts[0], parts[1], None
        return default_project, parts[0], parts[1]
    return default_project, parts[0], None


class Dataset:
    def __init__(self, dataset_ref):
        if isinstance(dataset_ref, str):
            parts = dataset_ref.split(".")
            dataset_ref = DatasetReference(
                parts[0] if len(parts) == 2 else ms.PROJECT_ID, parts[-1])
            # "project.dataset" (2 parts) or bare "dataset" (1 part)
        self.reference = dataset_ref
        self.project = dataset_ref.project
        self.dataset_id = dataset_ref.dataset_id
        self.location = None
        self.description = None
        self.default_table_expiration_ms = None
        self.labels = {}
        self.created = None
        self.modified = None
        self.full_dataset_id = f"{self.project}:{self.dataset_id}"

    def table(self, table_id):
        return TableReference(self.reference, table_id)

    @classmethod
    def _from_state(cls, project, entry):
        d = cls(DatasetReference(project, entry["dataset_id"]))
        d.location = entry.get("location", "US")
        d.description = entry.get("description", "")
        d.default_table_expiration_ms = entry.get("default_table_expiration_ms")
        d.labels = entry.get("labels", {})
        d.created = ms.parse_ts(entry.get("created"))
        d.modified = ms.parse_ts(entry.get("modified"))
        return d


class Table:
    def __init__(self, table_ref, schema=None):
        if isinstance(table_ref, str):
            parts = table_ref.split(".")
            proj = parts[0] if len(parts) == 3 else ms.PROJECT_ID
            table_ref = TableReference(
                DatasetReference(proj, parts[-2]), parts[-1])
        self.reference = table_ref
        self.project = table_ref.project
        self.dataset_id = table_ref.dataset_id
        self.table_id = table_ref.table_id
        self.schema = list(schema) if schema else []
        self.num_rows = 0
        self.table_type = "TABLE"
        self.created = None
        self.modified = None
        self.time_partitioning = None
        self.description = None
        self.full_table_id = (f"{self.project}:{self.dataset_id}."
                              f"{self.table_id}")

    @classmethod
    def _from_state(cls, project, dataset_id, entry):
        t = cls(TableReference(DatasetReference(project, dataset_id),
                               entry["table_id"]))
        t.schema = _dicts_to_schema(entry.get("schema"))
        t.num_rows = entry.get("num_rows", 0)
        t.table_type = entry.get("table_type", "TABLE")
        t.created = ms.parse_ts(entry.get("created"))
        t.modified = ms.parse_ts(entry.get("modified"))
        return t


class Row:
    """Mimics google.cloud.bigquery.table.Row: index, key and attr access."""

    def __init__(self, values: tuple, field_to_index: dict):
        self._values = values
        self._field_to_index = field_to_index

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._field_to_index[key]]

    def __getattr__(self, name):
        try:
            return self._values[self.__dict__["_field_to_index"][name]]
        except KeyError:
            raise AttributeError(name)

    def get(self, key, default=None):
        idx = self._field_to_index.get(key)
        return default if idx is None else self._values[idx]

    def keys(self):
        return list(self._field_to_index)

    def values(self):
        return self._values

    def items(self):
        return [(k, self._values[i]) for k, i in self._field_to_index.items()]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Row({self._values}, {self._field_to_index})"


class _RowIterator:
    def __init__(self, rows: list, schema: list, total_rows: int = None):
        self._rows = rows
        self.schema = schema
        self.total_rows = total_rows if total_rows is not None else len(rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def to_dataframe(self):
        import pandas as pd  # optional dependency, same as the real SDK
        return pd.DataFrame([dict(r.items()) for r in self._rows])


class _Job:
    def __init__(self, rows=None, schema=None, num_rows=0, errors=None):
        self._rows = rows or []
        self._schema = schema or []
        self.output_rows = num_rows
        self.errors = errors
        self.state = "DONE"
        self.job_id = "mock-job"

    def result(self, *_a, **_kw):
        return _RowIterator(self._rows, self._schema)

    def done(self):
        return True


def _typed(value, bq_type):
    """Coerce a SQLite value to the Python type BigQuery would return."""
    if value is None or not bq_type:
        return value
    if bq_type == "DATE" and isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return value
    if bq_type in ("TIMESTAMP", "DATETIME") and isinstance(value, str):
        return ms.parse_ts(value) if bq_type == "TIMESTAMP" \
            else ms.parse_ts(value).replace(tzinfo=None)
    if bq_type in ("RECORD", "STRUCT", "JSON") and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class Client:
    def __init__(self, project=None, credentials=None, location=None, **_kw):
        self.project = project or ms.PROJECT_ID
        self._credentials = credentials
        self.location = location

    # -- dataset ops -------------------------------------------------------

    def dataset(self, dataset_id, project=None):
        return DatasetReference(project or self.project, dataset_id)

    def get_dataset(self, dataset_ref):
        _p, ds_id, _t = _split_ref(dataset_ref, self.project, "dataset")
        s = ms.load_state()
        entry = s["datasets"].get(ds_id)
        if entry is None:
            raise NotFound(f"404 Dataset {self.project}:{ds_id} not found")
        return Dataset._from_state(self.project, entry)

    def create_dataset(self, dataset, exists_ok=False, timeout=None):
        if isinstance(dataset, (str, DatasetReference)):
            dataset = Dataset(dataset)
        with ms.mutate() as s:
            if dataset.dataset_id in s["datasets"]:
                if not exists_ok:
                    raise Conflict(
                        f"409 Already Exists: Dataset {dataset.dataset_id}")
            else:
                s["datasets"][dataset.dataset_id] = {
                    "dataset_id": dataset.dataset_id,
                    "description": dataset.description or "",
                    "location": dataset.location or "US",
                    "created": ms.now(),
                    "modified": ms.now(),
                    "tables": {},
                    "labels": dataset.labels or {},
                }
                ms.record(s, "bigquery_create_dataset",
                          dataset_id=dataset.dataset_id)
        return self.get_dataset(dataset.dataset_id)

    def delete_dataset(self, dataset_ref, delete_contents=False,
                       not_found_ok=False, timeout=None):
        _p, ds_id, _t = _split_ref(dataset_ref, self.project, "dataset")
        with ms.mutate() as s:
            entry = s["datasets"].pop(ds_id, None)
            if entry is None and not not_found_ok:
                raise NotFound(f"404 Dataset {ds_id} not found")
            ms.record(s, "bigquery_delete_dataset", dataset_id=ds_id)
        if entry:
            conn = ms.db()
            for table_id in entry.get("tables", {}):
                conn.execute(
                    f'DROP TABLE IF EXISTS "{ms.sqlite_table_name(ds_id, table_id)}"')
            conn.commit()
            conn.close()

    def list_datasets(self, project=None, max_results=None):
        s = ms.load_state()
        out = [Dataset._from_state(self.project, e)
               for e in s["datasets"].values()]
        return out[:max_results] if max_results else out

    # -- table ops ---------------------------------------------------------

    def get_table(self, table_ref):
        _p, ds_id, tbl_id = _split_ref(table_ref, self.project)
        s = ms.load_state()
        ds = s["datasets"].get(ds_id)
        if ds is None or tbl_id not in ds.get("tables", {}):
            raise NotFound(
                f"404 Table {self.project}:{ds_id}.{tbl_id} not found")
        return Table._from_state(self.project, ds_id, ds["tables"][tbl_id])

    def list_tables(self, dataset, max_results=None):
        _p, ds_id, _t = _split_ref(dataset, self.project, "dataset")
        s = ms.load_state()
        ds_entry = s["datasets"].get(ds_id)
        if ds_entry is None:
            raise NotFound(f"404 Dataset {ds_id} not found")
        out = [Table._from_state(self.project, ds_id, e)
               for e in ds_entry.get("tables", {}).values()]
        return out[:max_results] if max_results else out

    def create_table(self, table, exists_ok=False):
        if isinstance(table, (str, TableReference)):
            table = Table(table)
        ds_id, tbl_id = table.dataset_id, table.table_id
        schema = _schema_to_dicts(table.schema)
        with ms.mutate() as s:
            ds = s["datasets"].setdefault(ds_id, {
                "dataset_id": ds_id, "description": "", "location": "US",
                "created": ms.now(), "modified": ms.now(),
                "tables": {}, "labels": {}})
            if tbl_id in ds["tables"] and not exists_ok:
                raise Conflict(f"409 Already Exists: Table {tbl_id}")
            ds["tables"][tbl_id] = {
                "table_id": tbl_id, "schema": schema, "num_rows": 0,
                "created": ms.now(), "modified": ms.now()}
            ms.record(s, "bigquery_create_table", dataset_id=ds_id,
                      table_id=tbl_id)
        if schema:
            conn = ms.db()
            ms.ensure_table(conn, ds_id, tbl_id, schema)
            conn.close()
        return self.get_table(f"{ds_id}.{tbl_id}")

    def delete_table(self, table_ref, not_found_ok=False):
        _p, ds_id, tbl_id = _split_ref(table_ref, self.project)
        with ms.mutate() as s:
            ds = s["datasets"].get(ds_id, {})
            if tbl_id not in ds.get("tables", {}):
                if not not_found_ok:
                    raise NotFound(f"404 Table {tbl_id} not found")
            else:
                del ds["tables"][tbl_id]
                ms.record(s, "bigquery_delete_table", dataset_id=ds_id,
                          table_id=tbl_id)
        conn = ms.db()
        conn.execute(
            f'DROP TABLE IF EXISTS "{ms.sqlite_table_name(ds_id, tbl_id)}"')
        conn.commit()
        conn.close()

    # -- load / query ------------------------------------------------------

    def load_table_from_file(self, file_obj, destination, job_config=None,
                             rewind=False, size=None, num_retries=None):
        if rewind:
            file_obj.seek(0)
        raw = file_obj.read()
        if isinstance(raw, bytes):
            # utf-8-sig: real BigQuery strips a leading UTF-8 BOM from CSV
            # headers; without this the first column would be "\ufeffname".
            raw = raw.decode("utf-8-sig")
        else:
            raw = raw.lstrip("\ufeff")
        cfg = job_config or LoadJobConfig()
        if cfg.source_format == SourceFormat.NEWLINE_DELIMITED_JSON:
            rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
        else:
            reader = csv.reader(io.StringIO(raw),
                                delimiter=cfg.field_delimiter or ",")
            all_rows = [r for r in reader if r]
            if not all_rows:
                return _Job(num_rows=0)
            header = all_rows[0]
            body = all_rows[1:] if (cfg.skip_leading_rows or 0) >= 1 \
                else all_rows
            rows = [dict(zip(header, r)) for r in body]
        return self._load_rows(rows, destination, cfg)

    def load_table_from_json(self, json_rows, destination, job_config=None):
        return self._load_rows(list(json_rows), destination,
                               job_config or LoadJobConfig())

    def _load_rows(self, rows, destination, cfg):
        _p, ds_id, tbl_id = _split_ref(destination, self.project)
        schema = _schema_to_dicts(cfg.schema) if cfg.schema else None
        write = cfg.write_disposition or WriteDisposition.WRITE_APPEND

        with ms.mutate() as s:
            ds = s["datasets"].setdefault(ds_id, {
                "dataset_id": ds_id, "description": "", "location": "US",
                "created": ms.now(), "modified": ms.now(),
                "tables": {}, "labels": {}})
            tbl = ds["tables"].get(tbl_id)
            if schema is None:
                # No schema in the job config: BigQuery keeps the destination
                # table's schema (even for WRITE_TRUNCATE) and only infers one
                # when autodetect is set or the table does not exist yet.
                if tbl and tbl.get("schema") and not cfg.autodetect:
                    schema = tbl["schema"]
                else:
                    schema = ms.infer_schema_from_rows(rows)
            if tbl is None:
                tbl = {"table_id": tbl_id, "schema": schema, "num_rows": 0,
                       "created": ms.now(), "modified": ms.now()}
            if write == WriteDisposition.WRITE_EMPTY and tbl.get("num_rows"):
                raise Conflict("409 WRITE_EMPTY but table is not empty")
            if write != WriteDisposition.WRITE_APPEND or not tbl.get("schema"):
                tbl["schema"] = schema
            schema = tbl["schema"]

            conn = ms.db()
            sqlname = ms.sqlite_table_name(ds_id, tbl_id)
            if write == WriteDisposition.WRITE_TRUNCATE:
                conn.execute(f'DROP TABLE IF EXISTS "{sqlname}"')
                conn.commit()
                tbl["num_rows"] = 0
            ms.ensure_table(conn, ds_id, tbl_id, schema)
            cols = ", ".join(f'"{f["name"]}"' for f in schema)
            ph = ", ".join("?" for _ in schema)
            sql = f'INSERT INTO "{sqlname}" ({cols}) VALUES ({ph})'
            for r in rows:
                conn.execute(sql, ms.coerce_row(r, schema))
            conn.commit()
            conn.close()

            tbl["num_rows"] = tbl.get("num_rows", 0) + len(rows)
            tbl["modified"] = ms.now()
            ds["tables"][tbl_id] = tbl
            ms.record(s, "bigquery_load_table", dataset_id=ds_id,
                      table_id=tbl_id, rows=len(rows), write_disposition=write)
        return _Job(num_rows=len(rows))

    def query(self, query, job_config=None, location=None, project=None):
        for p in getattr(job_config, "query_parameters", None) or []:
            if isinstance(p, ScalarQueryParameter) and p.name:
                query = query.replace(f"@{p.name}", p.sql_literal())
        state = ms.load_state()
        rewritten, referenced, ref_tables = ms.rewrite_query(query, state)
        conn = ms.db()
        try:
            cur = conn.execute(rewritten)
            raw_rows = cur.fetchall()
            col_names = [d[0] for d in cur.description] if cur.description \
                else []
            is_write = ms.bq_sqlite.is_write_statement(rewritten)
            if is_write:
                conn.commit()
            with ms.mutate() as s:
                if is_write:
                    # keep the catalog in step with DDL/DML, as BigQuery does
                    ms.bq_sqlite.sync_catalog(conn, s, ref_tables,
                                              ms.sqlite_table_name, ms.now)
                ms.record(s, "bigquery_run_query", query=query,
                          rewritten=rewritten, referenced=referenced)
        finally:
            conn.close()
        # BigQuery hands back typed values; SQLite only has TEXT. Recover the
        # column types from the referenced tables so DATE/TIMESTAMP columns
        # arrive as date/datetime objects, which graders call .isoformat() on.
        col_types = {}
        for ds_id, tbl_id in ref_tables:
            tbl = (state.get("datasets", {}).get(ds_id, {})
                   .get("tables", {}).get(tbl_id, {}))
            for f in tbl.get("schema", []):
                col_types.setdefault(f["name"], (f.get("type") or "").upper())
        types = [col_types.get(c, "") for c in col_names]

        field_to_index = {c: i for i, c in enumerate(col_names)}
        rows = [Row(tuple(_typed(v, t) for v, t in zip(r, types)),
                    field_to_index)
                for r in raw_rows]
        schema = [SchemaField(c, col_types.get(c) or "STRING")
                  for c in col_names]
        return _Job(rows=rows, schema=schema, num_rows=len(rows))

    def close(self):
        pass
