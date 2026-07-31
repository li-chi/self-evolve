# hubspot-mock

Mock MCP server mirroring the HubSpot CRM API v3
([developers.hubspot.com/docs/api/crm](https://developers.hubspot.com/docs/api/crm/)).
Tool names follow the per-objectType verb pattern
(`list_<type>` / `get_<type>` / `create_<type>` / `update_<type>` /
`archive_<type>` / `search_<type>`) and responses use the same
envelope shape HubSpot returns for REST calls:

```jsonc
// single object
{
  "id": "90000001",
  "properties": {"email": "alice@example.com", "firstname": "Alice", ...},
  "createdAt": "2026-05-20T12:00:00.000Z",
  "updatedAt": "2026-05-20T12:00:00.000Z",
  "archived": false
}

// list / search
{"results": [...], "paging": {"next": {"after": "<cursor>"}}}

// error (NOT raised — returned as-is so the trace matches a real 4xx body)
{"status": "error", "message": "...",
 "correlationId": "<uuid>", "category": "OBJECT_NOT_FOUND"}
```

This is **not** a wrapper around the
[`hubspot-crm-skill`](../../../terminal-tool-use/mocks/hubspot-crm-skill/)
CLI; that CLI uses a different (flat, action-style) shape. This server
implements the REST-shaped HubSpot CRM v3 API directly.

## Implemented tools (20 + 2 mock helpers)

| group       | tool                  | REST endpoint                                      |
|-------------|-----------------------|----------------------------------------------------|
| Contacts    | `list_contacts`       | GET    /crm/v3/objects/contacts                    |
|             | `get_contact`         | GET    /crm/v3/objects/contacts/{contactId}        |
|             | `create_contact`      | POST   /crm/v3/objects/contacts                    |
|             | `update_contact`      | PATCH  /crm/v3/objects/contacts/{contactId}        |
|             | `archive_contact`     | DELETE /crm/v3/objects/contacts/{contactId}        |
|             | `search_contacts`     | POST   /crm/v3/objects/contacts/search             |
| Companies   | `list_companies`      | GET    /crm/v3/objects/companies                   |
|             | `get_company`         | GET    /crm/v3/objects/companies/{companyId}       |
|             | `create_company`      | POST   /crm/v3/objects/companies                   |
|             | `update_company`      | PATCH  /crm/v3/objects/companies/{companyId}       |
| Deals       | `list_deals`          | GET    /crm/v3/objects/deals                       |
|             | `get_deal`            | GET    /crm/v3/objects/deals/{dealId}              |
|             | `create_deal`         | POST   /crm/v3/objects/deals                       |
|             | `update_deal`         | PATCH  /crm/v3/objects/deals/{dealId}              |
| Engagements | `create_note`         | POST   /crm/v3/objects/notes                       |
|             | `create_task`         | POST   /crm/v3/objects/tasks                       |
|             | `create_email`        | POST   /crm/v3/objects/emails                      |
| Associations| `create_association`  | PUT    /crm/v4/objects/{from}/{id}/associations/{to}/{id} |
| Pipelines   | `list_pipelines`      | GET    /crm/v3/pipelines/{objectType}              |
|             | `get_pipeline`        | GET    /crm/v3/pipelines/{objectType}/{pipelineId} |
| Mock-only   | `mock_debug_state`, `mock_debug_seed` | —                                  |

All property values are stringified on output (matching real HubSpot
behaviour). Ids are numeric strings (`"90000001"`, `"90000002"`, …)
allocated per object type from a starting offset of `90000001`.

## Search filter coverage

`search_contacts` implements HubSpot's filter-group shape:

```jsonc
{
  "filterGroups": [
    {"filters": [
      {"propertyName": "lifecyclestage", "operator": "EQ",
       "value": "customer"},
      {"propertyName": "createdate", "operator": "GTE",
       "value": "1700000000000"}
    ]}
  ],
  "sorts": [{"propertyName": "lastname", "direction": "ASCENDING"}],
  "query": "alice",
  "properties": ["email", "firstname", "lastname"],
  "limit": 10
}
```

Supported operators: `EQ`, `NEQ`, `LT`, `LTE`, `GT`, `GTE`, `BETWEEN`,
`IN`, `NOT_IN`, `HAS_PROPERTY`, `NOT_HAS_PROPERTY`, `CONTAINS_TOKEN`,
`NOT_CONTAINS_TOKEN`. Filters within a group AND, groups OR.

## Deal pipeline validation

`create_deal` and `update_deal` validate `dealstage` against the
pipeline (`pipeline` property, default `"default"`). The default
pipeline is preloaded with the 7 standard HubSpot stages
(`appointmentscheduled` → `closedwon` / `closedlost`). Invalid stages
return a `VALIDATION_ERROR` / `PROPERTY_VALUE_INVALID` error body.

## State

State lives at `$HUBSPOT_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/hubspot_mock`):

```jsonc
{
  "portal": {"id": "12345678", "domain": "mock.hubspot.com"},
  "objects": {
    "contacts":  {"<id>": {"id","properties":{...},"createdAt","updatedAt","archived"}},
    "companies": {...},
    "deals":     {...},
    "notes":     {...},
    "tasks":     {...},
    "emails":    {...}
  },
  "associations": [
    {"fromObjectType","fromObjectId","toObjectType","toObjectId",
     "associationTypes":[{...}], "createdAt"}
  ],
  "pipelines": {"deals": {"default": {...}}, "tickets": {}},
  "next_id":   {"contacts":N,"companies":N,"deals":N,"notes":N,"tasks":N,"emails":N},
  "calls":     [{"op":"...","ts":"...",...}]
}
```

File-locking via `fcntl.flock` makes concurrent calls safe. Every
tool (mutating or read-only) appends an entry to `state["calls"]` so
the verifier can replay the trace.

Set `HUBSPOT_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads `state.json` once when no state file exists yet. Per-task
fixtures are typically loaded via `mock_debug_seed` instead.

## Behaviour notes / known mock-vs-real gaps

- `create_contact` enforces `email` uniqueness; collisions return
  `CONFLICT` / `CONTACT_EXISTS` (matches the real `409 Conflict`).
- `create_contact` and `update_contact` accept `id_property` to
  resolve by a property like `email` instead of the numeric id.
- Inline `associations` on create endpoints accept HubSpot's
  `{"to":{"id":...},"types":[{"associationCategory","associationTypeId"}]}`
  shape; the `toObjectType` is inferred by looking up the id across
  object catalogues (the real API requires it explicitly).
- Search filters do not yet support compound AND-of-OR-of-AND nesting
  beyond `filterGroups` (groups OR, filters within a group AND).
- Authentication (private app tokens / OAuth) is not modeled.
- Custom object types (other than contacts/companies/deals/notes/
  tasks/emails) are not implemented.
- Workflows, lists, marketing emails, and the analytics APIs are not
  modeled.

## Env

| var                       | default                       | purpose                            |
|---------------------------|-------------------------------|------------------------------------|
| `HUBSPOT_MOCK_STATE_DIR`  | `~/.openclaw/hubspot_mock`    | state.json directory               |
| `HUBSPOT_MOCK_SEED_PATH`  | unset                         | preload state.json on first start  |

The Dockerfile sets
`HUBSPOT_MOCK_STATE_DIR=/workspace/output/end_state/hubspot` to match
the openclaw rollout layout.

## Run

```bash
# local
HUBSPOT_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  hubspot-mock:
    build:
      context: ../../mcp_servers/hubspot-mock
      dockerfile: Dockerfile
    image: mcp-env/hubspot-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      HUBSPOT_MOCK_STATE_DIR: /workspace/output/end_state/hubspot
      HUBSPOT_MOCK_SEED_PATH: /workspace/input/hubspot_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
