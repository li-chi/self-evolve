# wandb-mock

Mock MCP server that mirrors
[`wandb-mcp-server`](https://github.com/wandb/wandb-mcp-server) — the
Weights & Biases server Toolathlon invokes as
`uvx --from wandb-mcp-server wandb_mcp_server`. Tool names match the
official `@mcp.tool` decorators verbatim so the mock is a drop-in
substitute during RL training (and for the 3 Toolathlon wandb tasks:
`experiments-recordings`, `wandb-best-score`, `wandb-shortest-length`).

## Tool surface

| tool                              | role                                                                     |
|-----------------------------------|--------------------------------------------------------------------------|
| `query_wandb_tool`                | GraphQL query against the W&B Models API (runs / sweeps / artifacts).    |
| `query_wandb_entity_projects`     | List projects for a W&B entity (or the viewer + their teams).            |
| `query_weave_traces_tool`         | Query Weave LLM traces. Stub: returns an empty result.                   |
| `count_weave_traces_tool`         | Count Weave traces. Stub: returns 0/0.                                   |
| `query_wandb_support_bot`         | wandbot RAG QA. Stub: returns a canned message.                          |
| `create_wandb_report_tool`        | Create a W&B Report. Returns a fake URL and persists the report.         |

Plus two mock-only debug tools (not on the real server):

- `mock_debug_state` — return the full state dict.
- `mock_debug_seed` — merge or replace state from a JSON fixture (for
  per-task preprocessing).

## GraphQL coverage

`query_wandb_tool` runs a small GraphQL evaluator that handles the
queries the official server's docs and the wandb python client
actually emit:

- `viewer { id username entity ... }`
- `project(name, entityName) { id name runCount ... }`
- `project(...) { runs(first, after, filters, order) { edges { node { ... } } pageInfo { endCursor hasNextPage } } }`
- `project(...) { run(name) { id name displayName state createdAt summaryMetrics config historyKeys historyLineCount sampledHistory(specs) ... } }`
- `project(...) { sweeps(...) { ... } }` / `sweep(name) { ... }`

### Filter syntax

`filters` is a JSON string with W&B's operator DSL — `_matches_filter`
implements `$eq` / `$ne` / `$gt` / `$gte` / `$lt` / `$lte` / `$in` /
`$nin` / `$contains` / `$regex` / `$and` / `$or` / `$not`. Plain
`{"field": "value"}` is equality. Paths drill into `config.*` /
`summary_metrics.*` / `summaryMetrics.*`.

### Sort syntax

`order` is a `+field` / `-field` string. `+` = ascending, `-` =
descending (W&B default). The field can be a top-level run attribute
(`createdAt`, `state`, `displayName`) or a nested
`summary_metrics.<key>` / `config.<key>` path.

## State

State lives in `$WANDB_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/wandb/state.json` in the container;
`~/.openclaw/wandb_mock/state.json` outside). Shape:

```jsonc
{
  "viewer": {"id", "username", "name", "email", "entity", "teams": [...]},
  "projects": {
    "<entity>/<name>": {
      "id", "name", "entity", "entityName", "description",
      "visibility", "createdAt", "updatedAt", "tags", "runCount"
    }
  },
  "runs": {
    "<entity>/<project>/<run_id>": {
      "id", "name" (the 8-char run id),
      "displayName" (human-readable name),
      "state" ("finished" | "running" | "failed" | "crashed" | ...),
      "createdAt", "updatedAt", "heartbeatAt",
      "config" (JSON string),
      "summaryMetrics" (JSON string),
      "historyKeys" (["loss", "val_acc", ...]),
      "history" ([{"_step": 0, "loss": 1.2, ...}, ...]),
      "tags", "sweep" ("<sweep_id>" | null),
      "user": {"username", "name"},
      "entity", "project",
      "historyLineCount"
    }
  },
  "sweeps": {
    "<entity>/<project>/<sweep_id>": {
      "id", "name", "displayName", "state", "createdAt",
      "method", "config" (JSON string), "bestLoss",
      "runs": ["<run_id>", ...]
    }
  },
  "reports": {"<id>": {...}},
  "next_id": {"report": N, "run": N, "sweep": N, "project": N},
  "calls": [{"op", "ts", ...}]
}
```

`config` and `summaryMetrics` are stored as JSON strings (matching
W&B's GraphQL `JSONString` scalar). The mock parses them transparently
when filtering / sorting, but returns them as strings to clients —
exactly like the real schema.

The `calls` log is the verifier's source of truth — every tool call
appends an entry.

Seed a starting state via `WANDB_MOCK_SEED_PATH` (a JSON file in the
above shape, partial dicts are merged onto an empty skeleton). Or use
`mock_debug_seed` at runtime.

## Run

```bash
# local
WANDB_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  wandb-mock:
    build:
      context: ../../mcp_servers/wandb-mock
      dockerfile: Dockerfile
    image: mcp-env/wandb-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      WANDB_MOCK_STATE_DIR: /workspace/output/end_state/wandb
      WANDB_MOCK_SEED_PATH: /workspace/input/wandb_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```

## What isn't modelled

- Artifacts / artifact files (returns `None` in the GraphQL resolver).
- Real Weave traces (`query_weave_traces_tool` / `count_weave_traces_tool`
  return empty results — no Toolathlon wandb task uses Weave).
- W&B Reports rendering (`create_wandb_report_tool` returns a fake URL
  and persists the markdown verbatim).
- `upsertBucket` mutation — implemented: creates or updates a run in
  state. Passes `entity`, `project`, `name`, `displayName`, `config`,
  `summaryMetrics`, `state`, `tags` args; returns `{ bucket { ... } }`.
  Other mutations still return `None`.

Extend any of these if a new task needs them; the in-memory state
model already has slots for them.
