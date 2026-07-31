# harvest-mock

Mock MCP server that mirrors the [Harvest REST API
v2](https://help.getharvest.com/api-v2/) tool surface. Each tool name
matches a Harvest REST operation and accepts the same parameter shape
the real API does; responses match Harvest's JSON shapes (numeric
integer `id`, nested `client`/`project`/`task`/`user` summary dicts,
list envelopes with `per_page`/`total_pages`/`next_page`/etc., and
ISO-8601 `created_at`/`updated_at` timestamps).

This is **not** a wrapper around the Membrane-CLI Harvest mock — it is
a drop-in replacement for the real Harvest server during rollouts.

## Tool surface

28 Harvest-v2 tools plus 2 mock-only debug helpers:

| group        | tool                                     | REST endpoint                                                          |
|--------------|------------------------------------------|------------------------------------------------------------------------|
| Clients      | `list_clients`                           | GET    /v2/clients                                                     |
|              | `get_client`                             | GET    /v2/clients/{client_id}                                         |
|              | `create_client`                          | POST   /v2/clients                                                     |
|              | `update_client`                          | PATCH  /v2/clients/{client_id}                                         |
|              | `delete_client`                          | DELETE /v2/clients/{client_id}                                         |
| Projects     | `list_projects`                          | GET    /v2/projects                                                    |
|              | `get_project`                            | GET    /v2/projects/{project_id}                                       |
|              | `create_project`                         | POST   /v2/projects                                                    |
|              | `update_project`                         | PATCH  /v2/projects/{project_id}                                       |
|              | `delete_project`                         | DELETE /v2/projects/{project_id}                                       |
| Tasks        | `list_tasks`                             | GET    /v2/tasks                                                       |
|              | `get_task`                               | GET    /v2/tasks/{task_id}                                             |
|              | `create_task`                            | POST   /v2/tasks                                                       |
|              | `update_task`                            | PATCH  /v2/tasks/{task_id}                                             |
|              | `delete_task`                            | DELETE /v2/tasks/{task_id}                                             |
| Time Entries | `list_time_entries`                      | GET    /v2/time_entries                                                |
|              | `get_time_entry`                         | GET    /v2/time_entries/{time_entry_id}                                |
|              | `create_time_entry`                      | POST   /v2/time_entries                                                |
|              | `update_time_entry`                      | PATCH  /v2/time_entries/{time_entry_id}                                |
|              | `delete_time_entry`                      | DELETE /v2/time_entries/{time_entry_id}                                |
|              | `restart_time_entry`                     | PATCH  /v2/time_entries/{time_entry_id}/restart                        |
|              | `stop_time_entry`                        | PATCH  /v2/time_entries/{time_entry_id}/stop                           |
| Users        | `list_users`                             | GET    /v2/users                                                       |
|              | `get_current_user`                       | GET    /v2/users/me                                                    |
|              | `get_user`                               | GET    /v2/users/{user_id}                                             |
| Invoices     | `list_invoices`                          | GET    /v2/invoices                                                    |
|              | `get_invoice`                            | GET    /v2/invoices/{invoice_id}                                       |
|              | `create_invoice`                         | POST   /v2/invoices                                                    |
| Expenses     | `list_expenses`                          | GET    /v2/expenses                                                    |
|              | `create_expense`                         | POST   /v2/expenses                                                    |
| Assignments  | `list_project_user_assignments`          | GET    /v2/projects/{project_id}/user_assignments                      |
|              | `list_task_assignments`                  | GET    /v2/task_assignments  (or /v2/projects/{id}/task_assignments)   |

Plus two mock-only debug helpers:

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed` — bulk-seed clients / projects / tasks / users /
  time entries / invoices / expenses / assignments. Used by per-task
  setup to load fixtures.

## Response shapes

List endpoints return Harvest's pagination envelope:

```jsonc
{
  "clients": [...],
  "per_page": 100,
  "total_pages": 1,
  "total_entries": 3,
  "next_page": null,
  "previous_page": null,
  "page": 1,
  "links": {
    "first":    "https://api.harvestapp.com/v2/clients?page=1&per_page=100",
    "last":     "...",
    "next":     null,
    "previous": null
  }
}
```

Time entries match the real Harvest body, including `spent_date`,
`hours`, `hours_without_timer`, `rounded_hours`, `is_running`,
`is_billed`, `is_locked`, `billable`, `budgeted`, `billable_rate`,
`cost_rate`, `timer_started_at`, `started_time`, `ended_time`, and
the nested summaries:

```jsonc
{
  "id": 1000001,
  "spent_date": "2026-05-20",
  "user":    {"id": 9000001, "name": "Mock User"},
  "client":  {"id": 4000001, "name": "Acme Co."},
  "project": {"id": 5000001, "name": "Website", "code": "WEB"},
  "task":    {"id": 6000001, "name": "Design"},
  "hours": 1.5,
  "hours_without_timer": 1.5,
  "rounded_hours": 1.5,
  "notes": "Hero layout",
  "is_locked": false,
  "is_running": false,
  "is_billed": false,
  "billable": true,
  "budgeted": false,
  "billable_rate": null,
  "cost_rate": null,
  "timer_started_at": null,
  "started_time": null,
  "ended_time": null,
  "created_at": "...",
  "updated_at": "..."
}
```

## Errors

- 404 (record not found): raised as `{"message": "Not Found"}`.
- Validation errors (missing parent, blank name, etc.):
  `{"errors": [{"resource": "...", "message": "..."}]}`.

Both are surfaced as a `ValueError` whose `args[0]` is the JSON
string, so the FastMCP trace shows the same body the real Harvest
server would return.

## State

State lives in `$HARVEST_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/harvest/state.json` inside the container;
`~/.openclaw/harvest_mock/state.json` outside). The file holds:

```jsonc
{
  "account":   {...},
  "self":      {...},
  "users":     {"<id>": {...}},
  "clients":   {"<id>": {...}},
  "projects":  {"<id>": {...}},
  "tasks":     {"<id>": {...}},
  "time_entries": {"<id>": {...}},
  "invoices":  {"<id>": {...}},
  "expenses":  {"<id>": {...}},
  "project_user_assignments": {"<id>": {...}},
  "task_assignments": {"<id>": {...}},
  "next_id":   {...},
  "calls":     [{"op": "...", "ts": "...", ...}]
}
```

The `calls` log is what the verifier consumes — every tool (reads
included) appends an entry. File-locking via `fcntl.flock` makes
concurrent calls safe; per-rollout isolation should reset the state
dir between rollouts.

Seed a starting state by setting `HARVEST_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.

## Id ranges

To make collisions obvious, the mock mints ids in per-kind ranges:

| kind                    | range            |
|-------------------------|------------------|
| time_entry              | 1000001…1999999  |
| invoice                 | 2000001…2999999  |
| expense                 | 3000001…3999999  |
| client                  | 4000001…4999999  |
| project                 | 5000001…5999999  |
| task                    | 6000001…6999999  |
| project_user_assignment | 7000001…7999999  |
| task_assignment         | 8000001…8999999  |
| user                    | 9000001…9999999  |

## Run

```bash
# local
HARVEST_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  harvest-mock:
    build:
      context: ../../mcp_servers/harvest-mock
      dockerfile: Dockerfile
    image: mcp-env/harvest-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      HARVEST_MOCK_STATE_DIR: /workspace/output/end_state/harvest
      HARVEST_MOCK_SEED_PATH: /workspace/input/harvest_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
