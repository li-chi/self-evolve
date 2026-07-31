# zotero-mock

Mock MCP server that mirrors the **Zotero Web API v3**
(https://www.zotero.org/support/dev/web_api/v3/start). Tool names,
parameters, and response envelopes match the real REST surface so
this server is a drop-in replacement for a live Zotero account during
rollouts.

This mock **does not** wrap the terminal-tool-use CLI. It speaks the
REST shape directly: items/collections/searches come back wrapped as
`{key, version, library, links, meta, data}`, and errors are returned
as `{"message": "...", "status": <int>}` matching how the real API
surfaces 400/404/409/412 responses.

## Tool surface (22 + 2 mock helpers)

| group       | tool                       | REST endpoint                                              |
|-------------|----------------------------|------------------------------------------------------------|
| Items       | `get_items`                | GET    /users/{id}/items                                   |
|             | `get_item`                 | GET    /users/{id}/items/{itemKey}                         |
|             | `get_top_items`            | GET    /users/{id}/items/top                               |
|             | `get_trash_items`          | GET    /users/{id}/items/trash                             |
|             | `get_item_children`        | GET    /users/{id}/items/{itemKey}/children                |
|             | `create_items`             | POST   /users/{id}/items                                   |
|             | `update_item`              | PATCH  /users/{id}/items/{itemKey}                         |
|             | `delete_item`              | DELETE /users/{id}/items/{itemKey}                         |
| Collections | `get_collections`          | GET    /users/{id}/collections                             |
|             | `get_top_collections`      | GET    /users/{id}/collections/top                         |
|             | `get_collection`           | GET    /users/{id}/collections/{collectionKey}             |
|             | `get_collection_items`     | GET    /users/{id}/collections/{collectionKey}/items       |
|             | `create_collection`        | POST   /users/{id}/collections                             |
|             | `update_collection`        | PATCH  /users/{id}/collections/{collectionKey}             |
|             | `delete_collection`        | DELETE /users/{id}/collections/{collectionKey}             |
| Tags        | `get_tags`                 | GET    /users/{id}/tags                                    |
|             | `get_item_tags`            | GET    /users/{id}/items/{itemKey}/tags                    |
| Searches    | `get_searches`             | GET    /users/{id}/searches                                |
| Groups      | `get_groups`               | GET    /users/{id}/groups                                  |
|             | `get_group`                | GET    /groups/{groupID}                                   |
| Mock-only   | `mock_debug_state`         | dump persisted state                                       |
|             | `mock_debug_seed`          | bulk-seed library / items / collections / searches / groups |

## Object shape

Items and collections follow Zotero's response envelope:

```jsonc
{
  "key":     "ABCD1234",                         // 8-char alphanumeric
  "version": 17,                                  // monotonically increasing
  "library": {
    "type": "user", "id": 12345, "name": "Mock User",
    "links": {"alternate": {"href":"...","type":"text/html"}}
  },
  "links":   {"self":     {"href":"...", "type":"application/json"},
              "alternate":{"href":"...", "type":"text/html"}},
  "meta":    {"creatorSummary": "Smith",
              "parsedDate":     "2024-01-15",
              "numChildren":    0},
  "data":    {
    "key": "ABCD1234", "version": 17,
    "itemType": "journalArticle", "title": "...",
    "creators": [{"creatorType":"author","firstName":"Jane",
                  "lastName":"Smith"}],
    "date":     "2024-01-15",
    "url":      "https://...",
    "DOI":      "10.xxxx/yyyy",
    "tags":     [{"tag":"theory","type":0}],
    "collections": ["COLL5678"],
    "relations": {},
    "dateAdded":    "...", "dateModified": "..."
  }
}
```

Errors are returned (not raised) as `{"message": "...", "status":
<int>}` so the call trace looks like a real failed HTTP response.

## Behavior notes / mock-vs-real gaps

- `q` / `qmode`: `titleCreatorYear` (default) scans title + creators +
  date; `everything` additionally scans abstract, publicationTitle,
  and tag names. The real API's full-text on attachments is **not**
  modeled.
- `sort`: `dateAdded`, `dateModified`, `title`, `creator`, `date`,
  `itemType`. `direction` defaults to `desc` for date keys, `asc`
  otherwise (matching the real API).
- `if_unmodified_since_version` is honored on `update_item`,
  `delete_item`, `update_collection`, `delete_collection` —
  mismatch returns `{"message": "...", "status": 412}`.
- `create_items` returns the batch shape `{successful, success,
  unchanged, failed}` keyed by index, just like the real API.
- `delete_item(permanent=False)` moves to trash; `get_trash_items`
  lists items with `deleted=True`. `permanent=True` also cascades
  to children.
- `delete_collection` strips the key from every item's
  `collections` list and reparents nested collections to top-level —
  no items are deleted.
- Group library separation is not enforced — `library.type` is just a
  label; all items live in one bucket. Set `library` via
  `mock_debug_seed({"library": {"type":"group","id":NNN,"name":"..."}})`
  if a task needs a group context.
- Permissions/API keys are not modeled. The real API gates writes by
  key scope; this mock always allows writes.
- Atom output (`format=atom`) is not implemented — every response is
  Zotero's JSON shape.

## State

A single JSON file at `$ZOTERO_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/zotero_mock`). Layout:

```jsonc
{
  "api_version": 3,
  "library":     {"type":"user","id":12345,"name":"...","links":{...}},
  "version":     N,                          // last assigned version
  "items":       {"<key>": {"key","version","itemType","title",
                            "creators","date","tags","collections",
                            "parentItem?","deleted",...}},
  "collections": {"<key>": {"key","version","name",
                            "parentCollection","relations"}},
  "searches":    {"<key>": {"key","version","name","conditions":[...]}},
  "groups":      {"<group_id>": {"name","owner","type","members",...}},
  "next_version": N+1,
  "calls":        [{"op":"...","ts":"...",...}],
  "_rng_seed":    N
}
```

The `calls` log is what the verifier consumes — every tool (reads
included) appends an entry. File-locking via `fcntl.flock` makes
concurrent calls safe; per-rollout isolation should reset the state
dir between rollouts.

Seed a starting state by setting `ZOTERO_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.
Per-task fixtures are typically loaded via the `mock_debug_seed` tool
instead.

## Env

| var                     | default                       | purpose                            |
|-------------------------|-------------------------------|------------------------------------|
| `ZOTERO_MOCK_STATE_DIR` | `~/.openclaw/zotero_mock`     | state.json directory               |
| `ZOTERO_MOCK_SEED_PATH` | unset                         | preload state.json on first start  |

The Dockerfile sets `ZOTERO_MOCK_STATE_DIR=/workspace/output/end_state/zotero`
to match the openclaw rollout layout.

## Run

```bash
# local
ZOTERO_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  zotero-mock:
    build:
      context: ../../mcp_servers/zotero-mock
      dockerfile: Dockerfile
    image: mcp-env/zotero-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      ZOTERO_MOCK_STATE_DIR: /workspace/output/end_state/zotero
      ZOTERO_MOCK_SEED_PATH: /workspace/input/zotero_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
