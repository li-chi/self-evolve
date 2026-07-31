# huggingface-mock

Mock MCP server that mirrors `huggingface/hf-mcp-server` (the official
Hugging Face MCP hosted at `https://huggingface.co/mcp` and connected by
Toolathlon via `npx mcp-remote https://huggingface.co/mcp --header
Authorization: Bearer <token>`).

## Source of truth

- Repo: <https://github.com/huggingface/hf-mcp-server>
- Tool name registry: `packages/mcp/src/tool-ids.ts`
- Per-tool schemas: `packages/mcp/src/{create-repo,model-search,
  model-detail,dataset-search,dataset-detail,hub-inspect,repo-search,
  space-search,space-info,space-files,paper-search,duplicate-space,
  use-space,docs-search/*,jobs/jobs-tool}.ts`
- Tool registration call sites:
  `packages/app/src/server/mcp-server.ts`

Every public tool here uses the upstream's exact name, parameter names,
and produces the same markdown-formatted text response (the real server
returns a `ToolResult { formatted, totalResults, resultsShared }`; the
MCP wire transport surfaces `formatted` as `content[0].text` — we
return that string directly).

## Tool surface

| tool                | upstream config                  | notes                                  |
|---------------------|----------------------------------|----------------------------------------|
| `create_repo`       | `CREATE_REPO_TOOL_CONFIG`        | model / dataset / space / bucket       |
| `model_search`      | `MODEL_SEARCH_TOOL_CONFIG`       | query, author, task, library, sort     |
| `dataset_search`    | `DATASET_SEARCH_TOOL_CONFIG`     | query, author, tags, sort              |
| `hub_repo_search`   | `REPO_SEARCH_TOOL_CONFIG`        | aggregated cross-type search           |
| `model_details`     | `MODEL_DETAIL_TOOL_CONFIG`       | single model card                      |
| `dataset_details`   | `DATASET_DETAIL_TOOL_CONFIG`     | single dataset card                    |
| `hub_repo_details`  | `HUB_REPO_DETAILS_TOOL_CONFIG`   | bulk; supports dataset_structure/preview |
| `paper_search`      | `PAPER_SEARCH_TOOL_CONFIG`       | papers semantic search                 |
| `space_search`      | `SEMANTIC_SEARCH_TOOL_CONFIG`    | spaces semantic search                 |
| `space_info`        | `SPACE_INFO_TOOL_CONFIG`         | tabulate user's spaces                 |
| `space_files`       | `SPACE_FILES_TOOL_CONFIG`        | list files in a static space           |
| `use_space`         | `USE_SPACE_TOOL_CONFIG`          | UI link (returned as plain text here)  |
| `duplicate_space`   | `DUPLICATE_SPACE_TOOL_CONFIG`    | copy a space into the user's namespace |
| `hf_doc_search`     | `DOCS_SEMANTIC_SEARCH_CONFIG`    | docs semantic search                   |
| `hf_doc_fetch`      | `DOC_FETCH_CONFIG`               | fetch a documentation page             |
| `hf_jobs`           | `HF_JOBS_TOOL_CONFIG`            | operation dispatcher (run/ps/logs/…)   |

Mock-only debug surface (not exposed by upstream — used for fixtures
and verification):

- `mock_debug_state` — dump the full persisted state dict.
- `mock_debug_seed_repo(repo_type, repo_id, data)` — insert or
  overwrite a repo with arbitrary fields.
- `mock_debug_upload_file(repo_type, repo_id, path, content_b64)` —
  write a file blob into a repo's tree (see Upload caveat below).
- `mock_debug_set_user(name, fullname?, email?)` — change the
  authenticated user identity.

## Intentionally omitted

- `dynamic_space_tool` (Gradio-discovery surface) — no Toolathlon
  task invokes it.
- `gradio_files` / `mcp-ui` resource blocks — not relevant for
  text-only RL rollouts.

## Upload caveat (important)

The official HF MCP server **does not expose a file-upload tool**. Tasks
like `huggingface-upload` and `dataset-license-issue` upload files using
the `huggingface_hub` Python library or the `hf` CLI from the terminal
MCP — and that traffic talks directly to `https://huggingface.co/api`
over HTTPS, **not through this MCP server**.

To keep RL rollouts from touching real HF accounts during those tasks,
either:

1. Point `HF_ENDPOINT=http://<mock-host>:<port>` at a side-car HTTP
   mock that translates `/api/repos/...` calls into
   `mock_debug_upload_file` / `mock_debug_seed_repo` writes against
   this MCP server's state; or
2. Run the agent inside a sandbox whose DNS resolves `huggingface.co`
   to the side-car.

The `huggingface_hub` library honours `HF_ENDPOINT` for read+write Hub
calls — see `huggingface_hub.constants.ENDPOINT`. Implementing that
side-car is out of scope for this MCP mock; flag any task that uploads
files until it is in place.

## State

State lives in `$HF_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/huggingface/state.json` inside the
container; `~/.openclaw/hf_mock/state.json` outside). Shape:

```jsonc
{
  "user":     { "name": "mock-user", "fullname": "...", "email": "..." },
  "models":   { "<author>/<name>": { ...model object, siblings, cardData, ... } },
  "datasets": { "<author>/<name>": { ...dataset object } },
  "spaces":   { "<author>/<name>": { ...space object, sdk, runtime, ... } },
  "papers":   { "<arxiv_id>": { ... } },
  "collections": { ... },
  "files":    {
    "datasets/<author>/<name>/<path>": {
      "path": "...", "size": 123, "sha": "...", "content_b64": "...",
      "uploaded_at": "ISO-8601"
    }
  },
  "docs":     { "<url>": { "title": "...", "content": "...", "product": "..." } },
  "jobs":     { "<job_id>": { ... } },
  "next_id":  { "job": 1 },
  "calls":    [ { "op": "...", "ts": "...", ... } ]
}
```

Every tool call appends to `calls` (the verifier's primary read).
`fcntl.flock` makes concurrent calls safe; reset the state dir
between rollouts for isolation.

Seed a starting state by setting `HF_MOCK_SEED_PATH` to a JSON file
of the same shape — loaded once if no `state.json` exists.

## Error format

Upstream throws JS `Error`s which the MCP transport surfaces as
`isError: true` text. We return strings starting with `Error:` to
match. Where the real Hub API would return
`{"error":"Repository not found"}` with HTTP 404, we surface
`Error: Dataset '<id>' not found. Please check the dataset ID.` (the
exact phrasing upstream's `dataset_details` emits — see
`packages/mcp/src/dataset-detail.ts:isNotFound`).

## Run

```bash
# local
HF_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  huggingface-mock:
    build:
      context: ../../mcp_servers/huggingface-mock
      dockerfile: Dockerfile
    image: mcp-env/huggingface-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      HF_MOCK_STATE_DIR: /workspace/output/end_state/huggingface
      HF_MOCK_SEED_PATH: /workspace/input/hf_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
