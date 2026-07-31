# airtable-mock

Mock MCP server mirroring [`@felores/airtable-mcp-server`](https://github.com/felores/airtable-mcp),
which is what `mcp-atlas` registers as its `airtable` server (see
`mcp_server_template.json` → `airtable` → `npx @felores/airtable-mcp-server@0.3.0`).
Every tool name and argument matches the upstream verbatim; responses
match the JSON shapes the upstream produces, which in turn track the
Airtable REST + Metadata API responses (with one wrapper quirk —
`list_bases`, `list_tables`, `list_records`, and `search_records` return
the bare array, because the upstream unwraps `.data.{bases|tables|records}`).

## Tool surface (12)

| group   | tool             | endpoint                                                                 |
|---------|------------------|--------------------------------------------------------------------------|
| Meta    | `list_bases`     | GET    /v0/meta/bases                                                    |
|         | `list_tables`    | GET    /v0/meta/bases/{base_id}/tables                                   |
|         | `create_table`   | POST   /v0/meta/bases/{base_id}/tables                                   |
|         | `update_table`   | PATCH  /v0/meta/bases/{base_id}/tables/{table_id}                        |
|         | `create_field`   | POST   /v0/meta/bases/{base_id}/tables/{table_id}/fields                 |
|         | `update_field`   | PATCH  /v0/meta/bases/{base_id}/tables/{table_id}/fields/{field_id}      |
| Records | `list_records`   | GET    /v0/{base_id}/{table_name}                                        |
|         | `get_record`     | GET    /v0/{base_id}/{table_name}/{record_id}                            |
|         | `create_record`  | POST   /v0/{base_id}/{table_name}                                        |
|         | `update_record`  | PATCH  /v0/{base_id}/{table_name}/{record_id}                            |
|         | `delete_record`  | DELETE /v0/{base_id}/{table_name}/{record_id}                            |
|         | `search_records` | GET    /v0/{base_id}/{table_name}?filterByFormula={field}="value"        |

Plus two mock-only debug tools:

- `mock_debug_state` — return the full persisted state.
- `mock_debug_seed_base(base)` — insert a complete base fixture
  (bypasses validation; used by per-task preprocessing).

## State

`$AIRTABLE_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/airtable/state.json` inside the container,
`~/.openclaw/airtable_mock/state.json` on host). Layout:

```jsonc
{
  "bases": {
    "appXXXXXXXXXXXXXX": {
      "id": "appXXXXXXXXXXXXXX",
      "name": "My Base",
      "permissionLevel": "create",
      "tables": {
        "tblXXXXXXXXXXXXXX": {
          "id": "tblXXXXXXXXXXXXXX",
          "name": "Tasks",
          "primaryFieldId": "fldXXXXXXXXXXXXXX",
          "fields": [{"id":"fldXXXXXXXXXXXXXX","name":"Title","type":"singleLineText"}, ...],
          "views":  [{"id":"viwXXXXXXXXXXXXXX","name":"Grid view","type":"grid"}],
          "records": {
            "recXXXXXXXXXXXXXX": {
              "id":"recXXXXXXXXXXXXXX",
              "createdTime":"2026-05-19T..Z",
              "fields": {"Title":"...", "Status":"Done"}
            }
          }
        }
      }
    }
  },
  "next_id": {"app": N, "tbl": N, "fld": N, "rec": N},
  "calls": [{"op": "...", "ts": "...", ...}]
}
```

Records are stored as a dict keyed by record id (not the wire-shape
array) so lookups/updates/deletes are O(1). They are projected into the
upstream shape (`{id, createdTime, fields}`) on every read.

Set `AIRTABLE_MOCK_SEED_PATH` to a JSON in the same shape to preload
state at first start (only if `state.json` does not yet exist).

## ID generation

Airtable IDs use a prefix + 14 alphanumeric chars:

- `app...` — base id
- `tbl...` — table id
- `fld...` — field id
- `rec...` — record id
- `viw...` — view id

## Errors

Returned (not raised) as the Airtable REST error envelope:

```json
{"error": {"type": "NOT_FOUND", "message": "Could not find ..."}}
```

The upstream MCP wraps Airtable's HTTP errors with `McpError`, but we
keep the underlying Airtable shape so verifiers can pattern-match on
`error.type`.

## Behavior notes

- `list_records`, `list_tables`, `list_bases`, `search_records` return
  bare arrays (no `{records: [...]}` wrapper) because the upstream
  unwraps `.data.<key>`.
- `table_name` arguments accept either the table id (`tblXXX`) or the
  table's `name`, matching Airtable's data-endpoint behavior.
- `create_table` defaults to a single `Name` (singleLineText) primary
  field if no `fields` array is supplied. Real Airtable rejects this;
  the mock is lenient so tasks can seed minimal tables.
- `create_field` mirrors the upstream `validateField`: option-less field
  types (singleLineText/multilineText/email/...) have their `options`
  stripped; option-required types (number, date, currency, ...) get a
  default options object if none was provided.
- `search_records` supports exactly the formula upstream emits:
  `{<field>} = "<value>"`. Numeric equality and `FIND("needle", {field})`
  are also recognized for hand-crafted formulas in seeded tasks; any
  other formula returns no matches.
- `update_record` is merge-by-key, not replace — matches the real
  PATCH behavior.
- `create_record` accepts unknown fields silently (lenient, matching
  Airtable's default `typecast=false` behavior of just storing them).

## Skipped vs upstream

The upstream exposes only the 12 tools above, so there is no v1 gap.
What is *not* in the upstream and therefore not here either:
attachments, batch create/update, list views, base/table deletion,
collaborators, comments, webhooks. Add as needed.
