# jira-mock

Mock MCP server mirroring [Atlassian Jira Cloud REST API
v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/).
Tool names follow Jira's operationId-style identifiers (e.g.
`getIssue`, `searchForIssuesUsingJql`, `doTransition`) and accept the
same parameter shapes as the real REST API, so an agent trained on
real Jira sees the same tool surface.

Responses follow Jira's REST v3 JSON shapes — issues are
`{"id","key","self","fields":{"summary","status","assignee", ...}}`,
search results are `{"startAt","maxResults","total","issues":[...]}`,
paginated list endpoints use the same `startAt`/`maxResults`/`total`
shape. Errors are returned as Jira error objects
(`{"errorMessages":[...],"errors":{...},"status":404}`), not raised,
so the trace looks like a real failed HTTP response.

## Tool surface

| tool                       | Jira REST endpoint                                              |
|----------------------------|------------------------------------------------------------------|
| `getIssue`                 | GET    /rest/api/3/issue/{issueIdOrKey}                          |
| `createIssue`              | POST   /rest/api/3/issue                                         |
| `editIssue`                | PUT    /rest/api/3/issue/{issueIdOrKey}                          |
| `deleteIssue`              | DELETE /rest/api/3/issue/{issueIdOrKey}                          |
| `searchForIssuesUsingJql`  | POST   /rest/api/3/search                                        |
| `getTransitions`           | GET    /rest/api/3/issue/{issueIdOrKey}/transitions              |
| `doTransition`             | POST   /rest/api/3/issue/{issueIdOrKey}/transitions              |
| `assignIssue`              | PUT    /rest/api/3/issue/{issueIdOrKey}/assignee                 |
| `getComments`              | GET    /rest/api/3/issue/{issueIdOrKey}/comment                  |
| `addComment`               | POST   /rest/api/3/issue/{issueIdOrKey}/comment                  |
| `getProject`               | GET    /rest/api/3/project/{projectIdOrKey}                      |
| `getAllProjects`           | GET    /rest/api/3/project                                       |
| `findUsers`                | GET    /rest/api/3/user/search                                   |
| `getUser`                  | GET    /rest/api/3/user?accountId=...                            |
| `getIssueAllTypes`         | GET    /rest/api/3/issuetype                                     |
| `getStatuses`              | GET    /rest/api/3/status                                        |
| `getPriorities`            | GET    /rest/api/3/priority                                      |

Plus mock-only debug helpers (not in the real surface):

- `mock_debug_state` — dump the entire persisted state dict.
- `mock_debug_seed`  — bulk-load users, projects, issue types,
  statuses, priorities, workflow, issues, and comments. Used by
  per-task preprocessing to seed fixtures.

### Identifiers (match real Jira format)

- **Issue key** — `<projectKey>-<seq>`, e.g. `PROJ-123`.
- **Issue id**  — numeric string, e.g. `"10042"`.
- **Project key** — uppercase, e.g. `PROJ`.
- **Account id** — opaque 24-char hex-style, e.g.
  `5b10ac8d82e05b22cc7d4ef5` (mock generates
  `5b10ac8d82e05b22cc7d4NNNN`).
- **Issue type / status / priority / transition id** — numeric
  string.

Most issue-targeting tools accept *either* the key or the id via the
single `issueIdOrKey` parameter, matching real Jira.

### ADF (Atlassian Document Format)

Fields like `description` and comment `body` use ADF in real Jira.
Mock tools accept *either*:

- a plain string — auto-wrapped into a minimal ADF doc
  (`{"type":"doc","version":1,"content":[{"type":"paragraph",
  "content":[{"type":"text","text":"..."}]}]}`)
- a full ADF document dict — stored as-is.

Returned values are always ADF.

### JQL support (subset)

`searchForIssuesUsingJql` parses a documented subset of JQL:

- Clauses joined by `AND` only (no `OR`, no parentheses grouping).
- Operators: `=`, `!=`, `~`, `!~`, `in`, `not in`, `is`, `is not`.
- Fields: `project`, `key`, `status`, `priority`, `issuetype`,
  `assignee`, `reporter`, `labels`, `summary`, `description`,
  `text`, `created`, `updated`, `resolution`, plus any
  `customfield_*`.
- `currentUser()` resolves to the configured `self.accountId`.
- `ORDER BY <field> ASC|DESC` supported with a single sort key.

Invalid / unsupported JQL returns a 400 error object.

## State

State lives in `$JIRA_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/jira/state.json` inside the container;
`~/.openclaw/jira_mock/state.json` outside). Schema:

```jsonc
{
  "base_url": "https://mock.atlassian.net",
  "self": {"accountId":"5b10...","displayName":"Mock Bot", ...},
  "projects":    {"<key>": {id, key, name, leadAccountId, ...,
                            nextSeq, issueTypeIds, initialStatusId}},
  "users":       {"<accountId>": {accountId, displayName, ...}},
  "issue_types": {"<id>": {id, name, subtask, ...}},
  "statuses":    {"<id>": {id, name, statusCategory, ...}},
  "priorities":  {"<id>": {id, name, ...}},
  "workflow":    {"<status_name>": [{"id","name","to"}]},
  "issues":      {"<key>": {id, key, project, created, updated,
                            fields: {summary, description (ADF),
                                     issuetype_id, status_id,
                                     priority_id, assignee_id,
                                     reporter_id, labels, ...,
                                     customfield_*}}},
  "comments":    {"<issue_key>": [{id, body (ADF), author_id, ...}]},
  "next_id":     {"issue": N, "comment": N, "project": N, ...},
  "calls":       [{"op":"...","ts":"...", ...}]
}
```

The `calls` log is what the verifier consumes — every tool appends
an entry. File-locking via `fcntl.flock` makes concurrent calls
safe; per-rollout isolation should reset the state dir between
rollouts.

Seed an initial state by setting `JIRA_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json`
exists. Alternatively, call `mock_debug_seed` at task setup with
high-level `users` / `projects` / `issues` lists.

## Run

```bash
# local
JIRA_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  jira-mock:
    build:
      context: ../../mcp_servers/jira-mock
      dockerfile: Dockerfile
    image: mcp-env/jira-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      JIRA_MOCK_STATE_DIR: /workspace/output/end_state/jira
      JIRA_MOCK_SEED_PATH: /workspace/input/jira_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
