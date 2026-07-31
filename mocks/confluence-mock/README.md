# confluence-mock

Mock MCP server that mirrors the **Confluence Cloud REST API v2**
([developer.atlassian.com](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/)).
Tool names, parameter shapes, and response payloads track the real
v2 API so the mock is a drop-in replacement for an Atlassian-issued
MCP wrapper.

## Tool surface (16 + 2 mock helpers)

| Group     | Tool                          | REST endpoint                                  |
|-----------|-------------------------------|------------------------------------------------|
| Spaces    | `get_spaces`                  | GET  /wiki/api/v2/spaces                       |
|           | `get_space`                   | GET  /wiki/api/v2/spaces/{id}                  |
| Pages     | `get_pages`                   | GET  /wiki/api/v2/pages                        |
|           | `get_page_by_id`              | GET  /wiki/api/v2/pages/{id}                   |
|           | `create_page`                 | POST /wiki/api/v2/pages                        |
|           | `update_page`                 | PUT  /wiki/api/v2/pages/{id}                   |
|           | `delete_page`                 | DELETE /wiki/api/v2/pages/{id}                 |
|           | `get_page_children`           | GET  /wiki/api/v2/pages/{id}/children          |
|           | `get_page_versions`           | GET  /wiki/api/v2/pages/{id}/versions          |
| Blog      | `get_blog_posts`              | GET  /wiki/api/v2/blogposts                    |
|           | `create_blog_post`            | POST /wiki/api/v2/blogposts                    |
| Comments  | `get_page_footer_comments`    | GET  /wiki/api/v2/pages/{id}/footer-comments   |
|           | `create_footer_comment`       | POST /wiki/api/v2/footer-comments              |
|           | `get_page_inline_comments`    | GET  /wiki/api/v2/pages/{id}/inline-comments   |
| Labels    | `get_page_labels`             | GET  /wiki/api/v2/pages/{id}/labels            |
|           | `add_label_to_page`           | POST /wiki/rest/api/content/{id}/label         |
| Mock-only | `mock_debug_state`            | (return full state for inspection)             |
|           | `mock_debug_seed`             | (bulk-seed spaces/pages/blogs/comments/labels) |

## Response shapes

Lists return the Confluence v2 envelope:
```json
{ "results": [ ... ], "_links": { "self": "...", "next": "..." } }
```

Item endpoints return the object directly. Errors are returned (not
raised) using the Confluence v2 error body:
```json
{ "errors": [ { "status": 404, "code": "NOT_FOUND",
                "title": "No page found with id: 999" } ] }
```

Pages expose:
- `id`, `status`, `title`, `spaceId`, `parentId`, `parentType`,
  `authorId`, `ownerId`, `createdAt`
- `version`: `{number, message, minorEdit, authorId, createdAt}`
- `body`: `{<format>: {representation, value}}` (when `body_format`
  requested) — formats: `storage` (XHTML), `atlas_doc_format`,
  `view`, `anonymous_export_view`
- `_links`: `{webui, editui, tinyui}`

## Body input

Every tool that accepts `body` (create_page / update_page /
create_blog_post / create_footer_comment) accepts either:
- a plain string (treated as the value of the default representation,
  `storage` by default — pass `representation="atlas_doc_format"` to
  override), or
- a dict shaped `{"representation": "storage|atlas_doc_format|view",
  "value": "..."}`.

The mock does not validate XHTML/ADF — it stores and returns the
value verbatim.

## State

Lives at `$CONFLUENCE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/confluence_mock`). Shape:

```jsonc
{
  "site": {"base_url": "https://mock.atlassian.net/wiki", "cloud_id": "..."},
  "self": {"accountId": "...", "displayName": "Mock Bot", ...},
  "users":      {"<accountId>": {...}},
  "spaces":     {"<space_id>":  {"id","key","name","type","status",
                                  "homepageId", ...}},
  "space_keys": {"DOCS": "<space_id>"},
  "pages":      {"<page_id>":   {"id","spaceId","title","parentId",
                                  "status","body":{"representation",
                                  "value"},"labels":[...],"version", ...}},
  "blog_posts": {"<id>":        {...}},
  "comments":   {"<id>":        {"pageId","commentType":"footer|inline",
                                  "body", ...}},
  "labels":     {"<label_id>":  {"id","name","prefix"}},
  "label_names":{"global:tag":  "<label_id>"},
  "page_versions": {"<page_id>": [{"number","title","body",...}]},
  "next_id":    {"space":N,"page":N,"blog":N,"comment":N,"label":N},
  "calls":      [{"op":"...","ts":"...",...}]
}
```

The `calls` log is what the verifier consumes — every tool (reads
included) appends an entry. File-locking via `fcntl.flock` makes
concurrent calls safe; per-rollout isolation should reset the state
dir between rollouts.

Seed a starting state by setting `CONFLUENCE_MOCK_SEED_PATH` to a
JSON file in the same shape — it is loaded once if no `state.json`
exists. For per-task fixtures, prefer the `mock_debug_seed` tool,
which accepts a slimmer input shape:

```python
mock_debug_seed(
    spaces=[{"key": "DOCS", "name": "Docs"}],
    pages=[
        {"spaceKey": "DOCS", "title": "Home",
         "body": "<p>Welcome.</p>"},
        {"spaceKey": "DOCS", "title": "Setup",
         "body": "<p>Install steps.</p>",
         "labels": ["onboarding", {"name": "tier-1", "prefix": "team"}]},
    ],
)
```

## Behavior notes / known mock-vs-real gaps

- Title uniqueness within a space is enforced (matches Confluence).
- `update_page` requires `version.number == current + 1` if supplied
  (matches Confluence's optimistic-concurrency contract); if omitted
  the mock auto-increments.
- `delete_page` is soft by default (sets `status` to `"trashed"`);
  pass `purge=True` to remove the row entirely.
- Cursor pagination uses the previous-page's last item id as an
  opaque cursor — adequate for verifier replay but not byte-identical
  to Confluence's encoded cursors.
- No authentication / scope checking.
- `get_page_versions` returns a synthetic version log derived from
  `create_page` + `update_page` calls; no diff payload is computed.
- Inline-comment anchoring (`inlineMarkerRef`,
  `inlineOriginalSelection`, `resolutionStatus`) is stored verbatim
  if provided via seed, but not produced from page-body parsing.

## Env

| var                           | default                          | purpose                          |
|-------------------------------|----------------------------------|----------------------------------|
| `CONFLUENCE_MOCK_STATE_DIR`   | `~/.openclaw/confluence_mock`    | state.json directory             |
| `CONFLUENCE_MOCK_SEED_PATH`   | unset                            | preload state.json on first start |

The Dockerfile sets
`CONFLUENCE_MOCK_STATE_DIR=/workspace/output/end_state/confluence`
to match the openclaw rollout layout.

## Run

```bash
# local
CONFLUENCE_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  confluence-mock:
    build:
      context: ../../mcp_servers/confluence-mock
      dockerfile: Dockerfile
    image: mcp-env/confluence-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      CONFLUENCE_MOCK_STATE_DIR: /workspace/output/end_state/confluence
      CONFLUENCE_MOCK_SEED_PATH: /workspace/input/confluence_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
