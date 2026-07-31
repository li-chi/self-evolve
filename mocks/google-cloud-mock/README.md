# google-cloud-mock

Mock MCP server mirroring [`lockon-n/google-cloud-mcp`](https://github.com/lockon-n/google-cloud-mcp),
the upstream Toolathlon invokes as
`uvx google-cloud-mcp --project-id ... --service-account-path ... --allowed-buckets ... --allowed-datasets ... --allowed-log-buckets ... --allowed-instances ...`.
Substitutes for the real google-cloud-mcp during RL training.

## Tool surface (39 official + 5 debug)

Tool names and parameter names match the upstream's `src/server.py`
@mcp.tool functions verbatim. Return shapes are the upstream's
human-readable strings, so a snapshot built against the real server
replays unchanged. The `bigquery_run_query` response additionally
appends a `__FULL_RESULTS_JSON__` block (JSON of `columns`/`rows`/
`total_rows`) so callers that need the full result set can parse it —
the upstream's string only prints 5 sample rows.

### BigQuery (8)

| tool                          | upstream behavior                                   |
|-------------------------------|-----------------------------------------------------|
| `bigquery_run_query`          | Execute SQL; runs on SQLite (table refs rewritten)  |
| `bigquery_list_datasets`      | List datasets, filtered by `--allowed-datasets`     |
| `bigquery_create_dataset`     | Create dataset                                      |
| `bigquery_get_dataset_info`   | Dataset metadata                                    |
| `bigquery_load_csv_data`      | Load CSV into table (autodetect schema)             |
| `bigquery_export_table`       | Export table to GCS (CSV or JSON)                   |
| `bigquery_list_jobs`          | List BQ jobs (mock returns empty unless seeded)     |
| `bigquery_cancel_job`         | Cancel a BQ job                                     |

### Cloud Storage (13)

| tool                            |
|---------------------------------|
| `storage_list_buckets`          |
| `storage_create_bucket`         |
| `storage_list_objects`          |
| `storage_upload_file`           |
| `storage_download_file`         |
| `storage_delete_object`         |
| `storage_get_bucket_info`       |
| `storage_generate_signed_url`   |
| `storage_copy_object`           |
| `storage_move_object`           |
| `storage_enable_versioning`     |
| `storage_get_bucket_size`       |
| `storage_set_bucket_lifecycle`  |

### Cloud Logging (9)

| tool                                  |
|---------------------------------------|
| `logging_write_log`                   |
| `logging_read_logs`                   |
| `logging_list_logs`                   |
| `logging_delete_log`                  |
| `logging_create_log_sink`             |
| `logging_list_log_sinks`              |
| `logging_delete_log_sink`             |
| `logging_export_logs_to_bigquery`     |
| `logging_create_log_bucket`           |

### Compute Engine (9)

| tool                          |
|-------------------------------|
| `compute_list_instances`      |
| `compute_create_instance`     |
| `compute_delete_instance`     |
| `compute_start_instance`      |
| `compute_stop_instance`       |
| `compute_restart_instance`    |
| `compute_get_instance`        |
| `compute_list_zones`          |
| `compute_wait_for_operation`  |

### Debug (mock-only, not in upstream)

- `mock_debug_state` — return the full state.json dict
- `mock_debug_seed_dataset(dataset_id, location?, description?)`
- `mock_debug_seed_table(dataset_id, table_id, schema, rows?)` — schema
  is `[{name,type,mode?}]`; rows are row-dicts keyed by column name.
- `mock_debug_seed_bucket_object(bucket_name, object_name, content_b64?,
  text?, content_type?)` — bypasses the allowlist
- `mock_debug_seed_log_bucket(bucket_id, location?, retention_days?)`

## Access control

Mirrors upstream's wildcard-prefix matcher: empty allowlist =
unrestricted; pattern `prefix*` matches names starting with `prefix`;
otherwise exact match. Enforced on the user-facing tools; debug tools
bypass it for fixture seeding.

## BigQuery on SQLite

Real-SQL fidelity matters because `bigquery_run_query` results must be
coherent across calls (joins, aggregations, follow-up filters). Storage
layout:

```
$GCP_MOCK_STATE_DIR/
  state.json    # metadata, allowlists, bucket objects, logs, calls log
  db.sqlite3    # actual BQ row data (SQLite tables "<dataset>__<table>")
```

`bigquery_run_query` rewrites BQ-style table references before
executing on SQLite:

- `` `project.dataset.table` `` → `"dataset__table"`
- `project.dataset.table`       → `"dataset__table"`
- `` `dataset.table` ``         → `"dataset__table"`
- `dataset.table` (if dataset is known) → `"dataset__table"`

Allowed-dataset enforcement runs on every dataset referenced.

### GCP-vs-SQLite divergences

- BQ types are mapped to SQLite affinities (INTEGER/INT64→INTEGER,
  FLOAT/NUMERIC→REAL, BOOLEAN→INTEGER 0/1, all date/timestamp/JSON
  types→TEXT). RECORD/STRUCT/JSON values are stored as JSON text.
- BigQuery-specific SQL (`STRUCT(...)`, `ARRAY<...>`, `UNNEST`,
  `EXTRACT(epoch FROM ...)`, `SAFE_CAST`, window functions over
  `PARTITION BY` with named windows, geography fns, ML.* fns) is not
  supported — SQLite syntax only. Most analytics-style queries
  (`SELECT … GROUP BY … HAVING … ORDER BY … JOIN …`, `WITH` CTEs,
  date string comparisons) work fine.
- `bytes_processed`/`bytes_billed`/`execution_time_ms` are always 0
  (mock).
- `bigquery_list_jobs` returns nothing unless the test pre-seeds
  `state["jobs"]`.
- `logging_read_logs` filter only supports `logName="..."` substring
  and a single `severity=...` clause; complex Cloud Logging filter
  expressions are not parsed.

## State model

`state.json`:

```jsonc
{
  "project_id": "mock-project",
  "config": {
    "allowed_buckets": [...], "allowed_datasets": [...],
    "allowed_log_buckets": [...], "allowed_instances": [...]
  },
  "datasets": {
    "<dataset_id>": {
      "dataset_id": "...", "description": "...", "location": "US",
      "created": "...", "modified": "...",
      "tables": {"<table_id>": {"schema": [...], "num_rows": N, ...}}
    }
  },
  "buckets": {
    "<bucket_name>": {
      "name": "...", "location": "US", "storage_class": "STANDARD",
      "created": "...", "versioning_enabled": false,
      "labels": {}, "lifecycle_rules": [],
      "objects": {"<path>": {"name", "size", "content_b64",
                             "content_type", "updated"}}
    }
  },
  "log_buckets": {"<bucket_id>": {name, location, retention_days, ...}},
  "log_sinks":   {"<sink_name>": {name, destination, filter, ...}},
  "logs":        {"<log_name>": [{log_name, severity, timestamp,
                                  text_payload, json_payload,
                                  resource}, ...]},
  "instances":   {"<name>": {name, zone, machine_type, status, ...}},
  "jobs":        {"<job_id>": {job_id, state, job_type, created, ...}},
  "calls":       [{op, ts, ...}]
}
```

The `calls` log is what a verifier consumes — every tool appends an
entry. File-locking via `fcntl.flock` makes concurrent calls safe;
per-rollout isolation should clear the state dir between rollouts.

## Run

```bash
# local
GCP_MOCK_STATE_DIR=$PWD/state python server.py \
  --project-id mock-prj \
  --allowed-datasets ab_testing,sales \
  --allowed-buckets 'promo-assets-for-b*,logs-bucket' \
  --allowed-log-buckets 'abtesting_logging*,exam_log'

# docker (per-task compose snippet)
services:
  google-cloud-mock:
    build:
      context: ../../mcp_servers/google-cloud-mock
      dockerfile: Dockerfile
    image: mcp-env/google-cloud-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      GCP_MOCK_STATE_DIR: /workspace/output/end_state/gcp
      GCP_MOCK_SEED_PATH: /workspace/input/gcp_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
    command:
      - "--project-id"
      - "mock-project"
      - "--allowed-datasets"
      - "${ALLOWED_DATASETS}"
      - "--allowed-buckets"
      - "${ALLOWED_BUCKETS}"
      - "--allowed-log-buckets"
      - "${ALLOWED_LOG_BUCKETS}"
      - "--allowed-instances"
      - ""
```

Pre-populate BigQuery tables with `mock_debug_seed_dataset` +
`mock_debug_seed_table` (or load via `bigquery_load_csv_data`), and
storage objects with `mock_debug_seed_bucket_object`.

## Env vars

- `GCP_MOCK_STATE_DIR` (default `~/.openclaw/gcp_mock`) — state dir
- `GCP_MOCK_SEED_PATH` — optional JSON in `state.json` shape, loaded
  once if no `state.json` exists
