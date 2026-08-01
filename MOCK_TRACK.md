# The mock track — self-contained Toolathlon tasks

A Toolathlon task that talks to a logged-in service (GCP, GitHub, Notion,
Google Sheets, W&B, HuggingFace, WooCommerce…) cannot run hermetically: it
needs credentials, a live account, and it mutates shared state, so parallel
rollouts collide and results drift with the account.

The mock track removes that dependency **without changing the task**. The
only thing that differs from official Toolathlon is what sits behind the
API.

```
                        ┌──────────────────────────────┐
   agent ── mcp-tool ──▶│  mocks/<svc>-mock  (MCP)     │──┐
                        └──────────────────────────────┘  │   one shared
                        ┌──────────────────────────────┐  ├──▶ state on disk
 upstream preprocess ──▶│  /opt/sdk-shims/<svc>        │──┘   state.json
 upstream grader     ──▶│  (google.cloud, gspread, …)  │      db.sqlite3
                        └──────────────────────────────┘
```

Both sides are views of one store, so a bucket the agent creates through
its MCP tool is a bucket the upstream grader sees through
`storage.Client().list_buckets()`.

## What stays identical to upstream

| upstream | mock track |
|---|---|
| task image | same (`lockon0927/toolathlon-task-image` via `toolathlon-harbor-base:v3`) |
| instruction | upstream `docs/task.md`, verbatim (plus a tools preamble, see below) |
| initial workspace | upstream `initial_workspace/`, copied at container start |
| environment setup | upstream `preprocess/main.py`, **run verbatim** at container start |
| grader | upstream `evaluation/main.py`, **run verbatim**, same CLI args |
| service tool surface | same tool names, parameters and response shapes |
| service scoping | same `--allowed-buckets/--allowed-datasets/...`, resolved from the task's own `token_key_session.py` **after** preprocess, as upstream does |
| launch_time | real container-start time, `%Y-%m-%d %H:%M:%S %A` |

## What differs, and why

1. **Backend.** Real service → mock. This is the sanctioned difference.
2. **Tool transport for shell agents.** Upstream hands the agent MCP tools
   natively. Harbor's `terminus-2` is shell-only, so the same servers are
   exposed through `mcp-tool` (`mcp-tool tools/schema/call <server> …`), and
   `instruction.md` gains a short appendix explaining it. Tool names and
   argument schemas are unchanged — `mcp-tool schema` prints the server's
   own JSON Schema. MCP-capable agents can be wired to the same servers via
   `[[environment.mcp_servers]]` instead.
3. **Seeds.** Account state a human created once upstream (a pre-existing
   log bucket, an archive bucket) ships as `environment/mock_seed/*.json`.
   Everything preprocess can build, preprocess still builds.
4. **groundtruth_workspace plumbing.** Upstream's preprocess and grader
   share one directory, and several preprocesses write generated names into
   it (`bucket_name.txt`). The port hides that directory from the agent, so
   init.sh stashes whatever preprocess wrote and `test.sh` overlays it onto
   the grader's copy — same values, same reads.

## Fidelity work in the mock itself

The mock has to be wrong in *no way that changes a grade*. Gaps found and
closed while porting the google-cloud cluster:

- **Integer division.** BigQuery divides as FLOAT64, SQLite as INTEGER, so
  `SUM(clicks)/SUM(views)` returned `0`. `bq_sqlite.prepare_query` forces
  float division outside strings/comments.
- **BigQuery-only SQL**: `COUNTIF`, `SAFE_DIVIDE`, `EXTRACT(part FROM x)`,
  `DATE_DIFF(a, b, DAY)` (bare keyword unit), `INTERVAL n DAY`,
  `CAST(x AS INT64/FLOAT64)`, `LOGICAL_AND/OR`, `STRING_AGG`, `STDDEV`.
- **STRUCT access.** `scores.online_score` on a RECORD column becomes
  `json_extract("scores", '$.online_score')`.
- **Catalog registration.** A table an agent creates with `CREATE TABLE …`
  must appear in `get_table`/`list_tables`, so DDL/DML resyncs the catalog —
  while preserving the richer BigQuery types already recorded (a resync must
  not downgrade RECORD to STRING).
- **Load-job schema rules.** With no schema and no autodetect, BigQuery
  keeps the destination table's schema even on WRITE_TRUNCATE.
- **Typed results.** DATE/TIMESTAMP columns come back as `date`/`datetime`
  (graders call `.isoformat()`), RECORD as dict.
- **UTF-8 BOM** is stripped from CSV headers on load, as BigQuery does.
- **Log filters.** `list_entries(filter_=…)` parses the real filter
  language subset graders use: AND/OR/NOT, parenthesised groups, the `:`
  contains operator, bare-field existence tests, `logName`, `severity`,
  `timestamp` comparisons, `jsonPayload.<path>`.
- **Reference parsing.** `get_dataset("project.dataset")` vs
  `get_table("dataset.table")` — a two-part string means different things
  per call, exactly as in the real SDK.

Behaviour that looks like a bug but is faithful, so it is **kept**:
FastMCP pre-parses JSON-looking strings, so passing a JSON document to a
`message: str` tool argument fails — the real server does the same, and the
graders have a text-payload path for it.

## Three substitution patterns

Which one a service gets depends on where its clients can be redirected.

| pattern | when | example |
|---|---|---|
| **client-library shim** | clients import a library that hardcodes the endpoint | `google.cloud.*` → `/opt/sdk-shims/gcp` |
| **protocol server** | clients speak a wire protocol to a host/port | poste.io → `mocks/poste-mock/mailserver.py` (SMTP + IMAP4rev1) |
| **REST facade** | clients take a base URL | WooCommerce → `mocks/woocommerce-mock/rest_facade.py` |

The protocol and REST patterns are the strongest: nothing on the client side
is touched at all, not even an import path, and the agent can keep using the
*real* MCP server. The emails track does exactly that — upstream's own
`emails-mcp` is installed in the image and pointed at the local mail server,
so the agent's tool surface is not a mock at all.

Services with a background backend declare a `daemon` in `MOCK_SPECS`;
init.sh starts it and waits for its port before running preprocess.

## Adding a service to the mock track

1. Make sure `mocks/<svc>-mock` mirrors the real server's tool surface.
2. Write `/opt/sdk-shims/<svc>`: the client library upstream's preprocess
   and grader import, backed by the mock's state. Implement only what those
   two actually call, and raise on the rest so gaps are loud.
3. Add an entry to `MOCK_SPECS` in `tools/mock_track.py` (mock path, state
   dir + env var, shim path, `${token.*}` scoping args).
4. Add the mock to `MOCKS` in `tools/build_base_image.sh`, rebuild.
5. `python3 tools/port_task.py <task> --mock <svc>`, write
   `environment/mock_seed/<svc>_seed.json` and `solution/solve.sh`.
6. Validate: oracle → 1.0, nop → 0.0. The oracle must work through the same
   tool surface the agent has (discover randomised names by listing, don't
   read them out of the seed).
