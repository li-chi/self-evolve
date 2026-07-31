# gitlab-mock

Mock MCP server that mirrors the [GitLab REST API v4](https://docs.gitlab.com/ee/api/rest/)
surface — operation names, parameter shapes, and response bodies all
follow the conventions used by GitLab's reference MCP server (and by
the actual REST endpoints documented at docs.gitlab.com).

Responses follow the GitLab REST JSON shapes (project, issue, merge
request, branch, commit, repository file). Errors are returned as
GitLab-shaped bodies (`{"message":"404 Not Found"}` for 404s,
`{"error":"..."}` or `{"message":{"field":["msg"]}}` for 4xx
validation errors) rather than raised — so the trace looks like a
real failed HTTP call.

## Tool surface

| tool                       | GitLab REST endpoint                                                |
|----------------------------|---------------------------------------------------------------------|
| `get_current_user`         | GET    /user                                                        |
| `search_users`             | GET    /users?search=&username=                                     |
| `list_projects`            | GET    /projects                                                    |
| `get_project`              | GET    /projects/:id                                                |
| `create_project`           | POST   /projects                                                    |
| `list_issues`              | GET    /projects/:id/issues (or /issues)                            |
| `get_issue`                | GET    /projects/:id/issues/:issue_iid                              |
| `create_issue`             | POST   /projects/:id/issues                                         |
| `update_issue`             | PUT    /projects/:id/issues/:issue_iid                              |
| `close_issue`              | (convenience: PUT with state_event=close)                           |
| `add_issue_comment`        | POST   /projects/:id/issues/:issue_iid/notes                        |
| `list_merge_requests`      | GET    /projects/:id/merge_requests (or /merge_requests)            |
| `get_merge_request`        | GET    /projects/:id/merge_requests/:merge_request_iid              |
| `create_merge_request`     | POST   /projects/:id/merge_requests                                 |
| `update_merge_request`     | PUT    /projects/:id/merge_requests/:merge_request_iid              |
| `accept_merge_request`     | PUT    /projects/:id/merge_requests/:iid/merge                      |
| `list_branches`            | GET    /projects/:id/repository/branches                            |
| `get_branch`               | GET    /projects/:id/repository/branches/:branch                    |
| `create_branch`            | POST   /projects/:id/repository/branches                            |
| `get_file_contents`        | GET    /projects/:id/repository/files/:file_path                    |
| `create_or_update_file`    | POST/PUT /projects/:id/repository/files/:file_path                  |

Plus mock-only debug helpers (not exposed by GitLab itself):

- `mock_debug_state` — dump the entire persisted state dict.
- `mock_debug_seed` — bulk insert users / projects / issues /
  merge requests; for per-task seeders.

### GitLab-specific quirks the mock honors verbatim

- **Issues use `iid`, not `id`.** GitLab issues have *two* identifiers:
  a global `id` and a project-scoped `iid`. URLs and most issue tools
  refer to the issue by its `iid` (e.g.
  `/projects/42/issues/3` is iid=3 within project 42). `get_issue`,
  `update_issue`, `add_issue_comment` and friends all take
  `issue_iid`. Same pattern for merge requests (`merge_request_iid`).
  The returned objects expose both fields.
- **Project ids accept multiple forms.** Every tool that takes
  `project_id` accepts a numeric id (`42`), an URL-encoded path
  (`group%2Fproject`), or a plain path (`group/project`) — all three
  resolve to the same project. This matches GitLab's REST handling.
- **State transitions use `state_event`, not `state`.** `update_issue`
  and `update_merge_request` close/reopen via
  `state_event="close"|"reopen"` — there is no direct `state` field
  on PUT (GitLab's actual API works this way).
- **Comments are *notes*.** The internal endpoint is `…/notes`. The
  tool is named `add_issue_comment` per common MCP-server convention,
  but the returned object is a GitLab Note (`{id, body, noteable_id,
  noteable_type, noteable_iid, ...}`).
- **Labels accept either form.** Both a list of strings (`["bug","p1"]`)
  and a comma-separated string (`"bug,p1"`) work — GitLab accepts both
  in its API.
- **Merge requests have a `draft` flag *and* a `Draft:` title
  prefix.** When `draft=true`, the title gets prefixed if it isn't
  already, and both `draft` and `work_in_progress` are reported true.

## State

State lives at `$GITLAB_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/gitlab/state.json` inside the container;
`~/.openclaw/gitlab_mock/state.json` outside). Schema:

```jsonc
{
  "current_user": { /* full user object */ },
  "users":    { "<id>": { /* user object */ } },
  "projects": {
    "<id>": {
      // standard project object: id, name, path_with_namespace,
      // namespace, visibility, default_branch, web_url, ...
      // mock-internal (stripped on response):
      "_branches": { "<name>": {"name","commit","protected","default", ...} },
      "_files":    { "<branch>": { "<path>": {"sha","size","content_b64"} } },
      "_commits":  { "<sha>": <commit_object> },
      "_commit_order": ["<sha>", ...],
      "_issues_index": [<iid>, ...],
      "_mrs_index":    [<iid>, ...],
      "_next_issue_iid": 1,
      "_next_mr_iid":    1
    }
  },
  "path_to_id":     { "group/project": <id> },
  // tuple keys are encoded as "<pid>|<iid>" strings on disk:
  "issues":         { "<pid>|<iid>": { /* issue object + _notes */ } },
  "merge_requests": { "<pid>|<iid>": { /* MR object + _notes */ } },
  "next_id": {"user":N,"project":N,"issue":N,"mr":N,"note":N},
  "calls":   [{"op":"...","ts":"...", ...}]
}
```

The `calls` log is what the verifier consumes — every tool appends
an entry. File-locking via `fcntl.flock` keeps concurrent calls safe;
per-rollout isolation should reset the state dir between rollouts.

Seed an initial state by setting `GITLAB_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.

## Run

```bash
# local
GITLAB_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  gitlab-mock:
    build:
      context: ../../mcp_servers/gitlab-mock
      dockerfile: Dockerfile
    image: mcp-env/gitlab-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      GITLAB_MOCK_STATE_DIR: /workspace/output/end_state/gitlab
      GITLAB_MOCK_SEED_PATH: /workspace/input/gitlab_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
