# notion-mock

Mock MCP server that mirrors `@notionhq/notion-mcp-server` (the
official OpenAPI-derived Notion MCP server used by Toolathlon).

## Tool surface

Every tool name and parameter shape matches the official server's
`API-<operationId>` convention, where `<operationId>` comes from
[`notion-openapi.json`](https://github.com/makenotion/notion-mcp-server/blob/main/scripts/notion-openapi.json).
Responses match the real Notion REST shapes (`object`, `id`,
`created_time`, `last_edited_time`, `parent`, `properties`, …); errors
are returned as Notion error objects (`{"object":"error", ...}`).

Implemented tools (subset that covers the 8 Toolathlon `notion` tasks
plus the atlas notion sample):

| tool                              | REST endpoint                                      |
|-----------------------------------|----------------------------------------------------|
| `API-get-self`                    | GET  /v1/users/me                                  |
| `API-get-users`                   | GET  /v1/users                                     |
| `API-get-user`                    | GET  /v1/users/{user_id}                           |
| `API-post-search`                 | POST /v1/search                                    |
| `API-post-page`                   | POST /v1/pages                                     |
| `API-retrieve-a-page`             | GET  /v1/pages/{page_id}                           |
| `API-patch-page`                  | PATCH /v1/pages/{page_id}                          |
| `API-retrieve-a-page-property`    | GET  /v1/pages/{page_id}/properties/{property_id}  |
| `API-move-page`                   | POST /v1/pages/{page_id}/move                      |
| `API-patch-block-children`        | PATCH /v1/blocks/{block_id}/children               |
| `API-get-block-children`          | GET  /v1/blocks/{block_id}/children                |
| `API-retrieve-a-block`            | GET  /v1/blocks/{block_id}                         |
| `API-update-a-block`              | PATCH /v1/blocks/{block_id}                        |
| `API-delete-a-block`              | DELETE /v1/blocks/{block_id}                       |
| `API-retrieve-a-database`         | GET  /v1/databases/{database_id}                   |
| `API-retrieve-a-data-source`      | GET  /v1/data_sources/{data_source_id}             |
| `API-create-a-data-source`        | POST /v1/data_sources                              |
| `API-update-a-data-source`        | PATCH /v1/data_sources/{data_source_id}            |
| `API-query-data-source`           | POST /v1/data_sources/{data_source_id}/query       |
| `API-list-data-source-templates`  | GET  /v1/data_sources/{data_source_id}/templates   |
| `API-retrieve-a-comment`          | GET  /v1/comments?block_id=...                     |
| `API-create-a-comment`            | POST /v1/comments                                  |

Plus two mock-only debug tools used by per-task setup/verification:

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed_object` — bulk-insert a Notion-shaped object
  (page / database / data_source / block) bypassing validation, for
  fixture seeding.

## Filter coverage

`API-query-data-source` implements single-condition top-level filters
(equals / does_not_equal / contains / does_not_contain / is_empty /
is_not_empty / starts_with / ends_with / greater_than / less_than) on
title, rich_text, number, checkbox, url, email, phone_number, select,
multi_select, status, date, people, and relation properties. AND/OR
compound filters are not yet implemented — they degrade to returning
all rows. Extend `_apply_filter` as tasks need it.

## State

State lives in `$NOTION_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/notion/state.json` inside the container;
`~/.openclaw/notion_mock/state.json` outside). The file holds:

```jsonc
{
  "version": "2022-06-28",
  "self": {...},
  "users": {"<id>": {...}},
  "objects": {
    "<id>": {
      "object": "page" | "database" | "data_source" | "block",
      ...real Notion shape...,
      "_children": ["<child_id>", ...]   // private, stripped on responses
    }
  },
  "comments": {"<id>": {...}},
  "calls": [{"op": "...", "ts": "...", ...}]
}
```

The `calls` log is what the verifier consumes — every mutating tool
appends an entry. File-locking via `fcntl.flock` makes concurrent
calls safe; per-rollout isolation should reset the state dir between
rollouts.

Seed a starting state by setting `NOTION_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.

## Run

```bash
# local
NOTION_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  notion-mock:
    build:
      context: ../../mcp_servers/notion-mock
      dockerfile: Dockerfile
    image: mcp-env/notion-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      NOTION_MOCK_STATE_DIR: /workspace/output/end_state/notion
      NOTION_MOCK_SEED_PATH: /workspace/input/notion_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
