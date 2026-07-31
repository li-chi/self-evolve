# snowflake-mock

Mock MCP server that mirrors `mcp_snowflake_server` (Toolathlon
source: [`lockon-n/mcp-snowflake-server`](https://github.com/lockon-n/mcp-snowflake-server),
upstream: [`isaacwasserman/mcp-snowflake-server`](https://github.com/isaacwasserman/mcp-snowflake-server)).
The real server is invoked from Toolathlon as

```
uvx mcp_snowflake_server --account ... --warehouse ... --user ...
    --database ... --schema ... --allow_write --exclude-json-results
    --allowed_databases ...
```

This mock substitutes for it during RL training.

## Tool surface

Tool names and argument shapes match the official server. The mock
ships every tool the Toolathlon snowflake tasks use, plus two
debug-only tools that aren't part of the upstream surface.

| tool                  | arguments                              | semantic                                           |
|-----------------------|----------------------------------------|----------------------------------------------------|
| `list_databases`      | —                                      | `SELECT DATABASE_NAME FROM INFORMATION_SCHEMA.DATABASES` (catalog) |
| `list_schemas`        | `database`                             | `INFORMATION_SCHEMA.SCHEMATA` for `<database>`     |
| `list_tables`         | `database`, `schema`                   | `INFORMATION_SCHEMA.TABLES` for `<db>.<schema>`    |
| `describe_table`      | `table_name` (`db.schema.table`)       | `INFORMATION_SCHEMA.COLUMNS` for `<table>`         |
| `read_query`          | `query`                                | SELECT (write detection rejects mutating SQL)      |
| `write_query`         | `query`                                | INSERT / UPDATE / DELETE / MERGE                   |
| `create_table`        | `query`                                | `CREATE TABLE ...` (single statement)              |
| `append_insight`      | `insight`                              | append to insights memo, fires `memo://insights`   |
| `list_insights`       | —                                      | return the insights list (mock helper)             |
| `create_databases`    | `databases: [str]`                     | bulk create (with per-name `allowed_databases` check) |
| `drop_databases`      | `databases: [str]`                     | bulk drop (cascades to underlying sqlite tables)   |
| `create_schemas`      | `database`, `schemas: [str]`           | bulk create schemas under a database               |
| `drop_schemas`        | `database`, `schemas: [str]`           | bulk drop schemas (cascades)                       |
| `create_tables`       | `database`, `schema`, `tables`         | bulk CREATE TABLE (strings or `{name,definition}`) |
| `drop_tables`         | `database`, `schema`, `tables: [str]`  | bulk DROP TABLE                                    |
| `mock_debug_state`    | —                                      | dump persisted state for verifiers                 |
| `mock_debug_exec`     | `query`                                | run raw sqlite SQL bypassing the rewriter / detector|

The `write_query` / `create_table` / `create_databases` /
`drop_databases` / `create_schemas` / `drop_schemas` /
`create_tables` / `drop_tables` tools refuse to run when the server
is launched without `--allow_write` (matching the upstream behaviour
of stripping these tools from the listing). The mock keeps them
listed but raises so that test cases asserting the error message
still pass.

## SQL backend

Backed by a real sqlite3 file at `$SNOWFLAKE_MOCK_STATE_DIR/db.sqlite3`.
Snowflake's three-part `db.schema.table` namespace collapses onto a
single flat sqlite name `"<DB>__<SCHEMA>__<TABLE>"`. The rewriter
also patches a few Snowflake-only forms before handing the query to
sqlite:

| Snowflake                              | sqlite                                                     |
|----------------------------------------|------------------------------------------------------------|
| `CURRENT_TIMESTAMP()` / `SYSDATE()`    | `CURRENT_TIMESTAMP`                                        |
| `CURRENT_DATE()`                       | `CURRENT_DATE`                                             |
| `DATEADD(unit, n, t)`                  | `datetime(t, '+n unit')` (`years`/`months`/`days`/`hours`/`minutes`/`seconds`) |
| `DATEDIFF(unit, a, b)`                 | `CAST((julianday(b)-julianday(a))*<scale> AS INTEGER)`     |
| `DB.SCHEMA.TABLE`                      | `"DB__SCHEMA__TABLE"`                                      |
| `SCHEMA.TABLE` (with default DB)       | `"DB__SCHEMA__TABLE"`                                      |
| `[DB.]INFORMATION_SCHEMA.{DATABASES,SCHEMATA,TABLES,COLUMNS}` | served from `state.json` catalog (sqlite has no info_schema) |
| `SELECT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='X')` | catalog lookup → `[{"EXISTS": 0|1}]`              |
| `USE DATABASE` / `USE SCHEMA` / `USE WAREHOUSE` | no-op (only `current` is tracked in state)        |
| `COMMENT ON {TABLE|COLUMN} ... IS '...'` | stashed in `state.comments` / `state.column_comments` (sqlite has no native column comments) |

Identifier case is normalized to uppercase, mirroring Snowflake.

Unrecognised Snowflake functions fall through to sqlite and surface
their error message via `read_query` / `write_query` as
`SQL error: <sqlite error> | rewritten: <SQL>` — matching the upstream
behaviour of bubbling the underlying driver error.

## State

```jsonc
$SNOWFLAKE_MOCK_STATE_DIR/state.json
{
  "databases": {
    "PURCHASE_INVOICE": {
      "schemas": {"PUBLIC": {"tables": ["INVOICES", "INVOICE_PAYMENTS"]}}
    }
  },
  "current": {"database": "DEMO", "schema": "PUBLIC"},
  "allowed_databases": ["PURCHASE_INVOICE"],
  "allow_write": true,
  "exclude_json_results": true,
  "insights": [{"id":"...","ts":"...","text":"..."}],
  "comments": {"DB__SCH__T": "table comment"},
  "column_comments": {"DB__SCH__T": {"COLNAME": "col comment"}},
  "calls": [{"op": "...", "ts": "...", ...}]
}

$SNOWFLAKE_MOCK_STATE_DIR/db.sqlite3       # real sqlite tables
```

The `calls` log is what verifiers consume — every tool appends an
entry. File-locking via `fcntl.flock` makes concurrent calls safe;
per-rollout isolation should reset the state dir between rollouts.

## Seeding

Set `SNOWFLAKE_MOCK_SEED_PATH` to a JSON file in this shape; it's
loaded once if no `state.json` exists yet:

```jsonc
{
  "current": {"database": "DEMO", "schema": "PUBLIC"},
  "allowed_databases": ["DEMO"],
  "databases": {
    "DEMO": {
      "schemas": {
        "PUBLIC": {
          "tables": {
            "USERS": {
              "columns": [
                "ID INTEGER PRIMARY KEY",
                "NAME VARCHAR(255)",
                "EMAIL VARCHAR(255)",
                "CREATED_AT TIMESTAMP"
              ],
              "data": [
                {"ID": 1, "NAME": "Alice", "EMAIL": "alice@example.com",
                 "CREATED_AT": "2024-01-01T00:00:00Z"}
              ]
            }
          }
        }
      }
    }
  }
}
```

A column entry can also be an object: `{"name": "ID", "type":
"INTEGER", "constraints": "PRIMARY KEY"}`. Provide `create_sql`
instead of `columns` if you want full Snowflake DDL routed through
the rewriter.

## CLI args

The mock accepts (and ignores) every flag the real server takes, so
the existing Toolathlon snowflake invocation works unmodified. Flags
that actually change behaviour:

- `--allow_write` / `--no-allow_write` (default: allow_write=True)
- `--allowed_databases A,B,C`
- `--exclude-json-results`
- `--exclude_tools tool1 tool2`
- `--database X` / `--schema Y` (defaults applied to unqualified SQL)

Everything else (`--account`, `--warehouse`, `--user`, `--role`,
`--private_key_path`, `--connections-file`, `--connection-name`, …)
is silently dropped — no real Snowflake auth is performed.

## Run

```bash
# local
SNOWFLAKE_MOCK_STATE_DIR=$PWD/state python server.py \
    --account a --warehouse w --user u --database DEMO --schema PUBLIC \
    --allow_write --exclude-json-results \
    --allowed_databases DEMO

# docker (per-task compose snippet)
services:
  snowflake-mock:
    build:
      context: ../../mcp_servers/snowflake-mock
      dockerfile: Dockerfile
    image: mcp-env/snowflake-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      SNOWFLAKE_MOCK_STATE_DIR: /workspace/output/end_state/snowflake
      SNOWFLAKE_MOCK_SEED_PATH: /workspace/input/snowflake_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
    command:
      - "--account"
      - "mockaccount"
      - "--warehouse"
      - "MOCK_WH"
      - "--user"
      - "mockuser"
      - "--database"
      - "DEMO"
      - "--schema"
      - "PUBLIC"
      - "--allow_write"
      - "--exclude-json-results"
      - "--allowed_databases"
      - "DEMO"
```

## Covered Toolathlon tasks (v1)

- `landing-task-reminder` — read employees from a Snowflake catalog, write back task assignments / completion dates.
- `payable-invoice-checker` — `create_databases`, `create_table`, bulk `INSERT` into `PURCHASE_INVOICE.PUBLIC.{INVOICES,INVOICE_PAYMENTS}`, `COMMENT ON COLUMN` for the `OUTSTANDING_FLAG` description.
- `sla-timeout-monitor` — `SLA_MONITOR.PUBLIC.{USERS,SUPPORT_TICKETS}` with `TIMESTAMP DEFAULT CURRENT_TIMESTAMP()` columns; queries use `DATEDIFF`-style time math.
- `travel-expense-reimbursement` — write reviewed claims into `TRAVEL_EXPENSE_REIMBURSEMENT.PUBLIC."2024Q4REIMBURSEMENT"` (note the digit-prefixed identifier that needs quoting).
