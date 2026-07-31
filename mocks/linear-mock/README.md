# linear-mock

Mock MCP server mirroring **Linear's GraphQL API surface**
(https://developers.linear.app/docs/graphql/working-with-the-graphql-api).
Each MCP tool corresponds 1:1 to a top-level GraphQL query/mutation in
Linear's schema, so an agent trained against the real Linear API sees
the same operation names, input shapes, and response shapes.

## Why a real-API-shaped mock (not a CLI wrapper)

Linear is **Tier A**: real workspaces can't parallelize for RL
rollouts, so we run a deterministic in-memory mock. Unlike the
`linear-skill` CLI elsewhere in this repo, this server matches the
real GraphQL **operation names** (`issueCreate`, not `createIssue`)
and the real **response envelope** (`{"data": {...}}` for queries,
`{"success": true, "<entity>": {...}, "lastSyncId": N}` for
mutations).

## Implemented tools (17 + 2 mock helpers)

| group       | tool             | kind     |
|-------------|------------------|----------|
| Identity    | `viewer`         | query    |
| Teams       | `teams`          | query    |
|             | `team`           | query    |
| Users       | `users`          | query    |
|             | `user`           | query    |
| Issues      | `issues`         | query    |
|             | `issue`          | query    |
|             | `issueCreate`    | mutation |
|             | `issueUpdate`    | mutation |
|             | `issueDelete`    | mutation |
| Projects    | `projects`       | query    |
|             | `project`        | query    |
|             | `projectCreate`  | mutation |
|             | `projectUpdate`  | mutation |
| States      | `workflowStates` | query    |
| Comments    | `comments`       | query    |
|             | `commentCreate`  | mutation |
| Mock-only   | `mock_debug_state`, `mock_debug_seed` | |

Tool names and `input` parameter shapes match Linear's GraphQL
schema. Queries return Relay-style connections (`{nodes, pageInfo}`)
under `{"data": {<root>: ...}}`. Mutations return Linear's payload
shape `{"success": true, "lastSyncId": N, "<entity>": {...}}` on
success and `{"success": false, "lastSyncId": 0, "errors": [...]}`
on failure (never raise — errors are returned as data so the trace
looks like a real failed GraphQL response).

## ID conventions

- Internal ids: UUID v4 (e.g. `a1b2c3d4-...`)
- Issue identifiers: `<TEAMKEY>-<NUM>` (e.g. `ENG-123`) — accepted
  anywhere an issue UUID is accepted (`issue`, `issueUpdate`,
  `issueDelete`, `commentCreate.input.issueId`, `comments` filter).
- Teams: UUID + short `key`. The `team(id)` query accepts either.
- Projects: UUID + `slugId`. The `project(id)` query accepts either.

## State

A single JSON file at `$LINEAR_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/linear_mock`). Layout:

```jsonc
{
  "organization": {"id","name","urlKey"},
  "viewer":       {"id","name","displayName","email","admin"},
  "users":        {"<uid>": {...}},
  "teams":        {"<tid>": {"id","key","name","memberIds":[...]}},
  "workflowStates": {"<sid>": {"id","teamId","name","type","position"}},
  "labels":       {"<lid>": {"id","teamId","name","color"}},
  "projects":     {"<pid>": {"id","name","slugId","state","teamIds":[...]}},
  "issues":       {"<iid>": {"id","identifier","number","title",
                              "teamId","stateId","assigneeId",...}},
  "issues_by_identifier": {"ENG-1": "<iid>"},
  "comments":     {"<cid>": {"id","body","issueId","userId"}},
  "next_issue_number": {"<teamId>": N},
  "last_sync_id": N,
  "calls":        [{"op":"...","ts":"...",...}]
}
```

Set `LINEAR_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist (per-rollout
isolation should clear the state dir between rollouts). Per-task
fixtures are typically loaded via the `mock_debug_seed` tool instead.

## Filter / pagination support

Queries that list things (`teams`, `users`, `issues`, `projects`,
`workflowStates`, `comments`) accept Linear-shaped GraphQL
connection arguments: `first` (page size), `after` (cursor =
last node's id), `orderBy` (`createdAt`/`updatedAt`/`name`/`priority`
where applicable), `filter`.

`issues.filter` is a *subset* of Linear's `IssueFilter`:
- top-level scalar fields (`title`, `description`, `priority`,
  `number`, `estimate`, `dueDate`, `createdAt`, `updatedAt`,
  `completedAt`) with `eq`/`neq`/`in`/`nin`/`null`/`contains`/
  `containsIgnoreCase`/`gte`/`lte`,
- nested filters on `team`, `state`, `assignee`, `project`,
  `creator` (each takes a `{id|name|type: {eq|in|...}}` condition).

Boolean AND/OR composition is not modeled.

## Behavior notes / known mock-vs-real gaps

- No webhook delivery, no rate limiting, no `lastSyncId` consistency
  guarantees beyond a monotonic counter.
- `priorityLabel` is computed locally from `priority` — Linear uses
  the same mapping (0 No priority, 1 Urgent, 2 High, 3 Medium, 4 Low).
- Moving an issue into a `completed`-type workflow state stamps
  `completedAt`; into a `canceled`-type stamps `canceledAt`. Linear
  also runs additional triggers (notifications, SLA timers, …) that
  are not modeled.
- Authentication is not modeled — no API key check, no OAuth scopes.

## Env

| var                      | default                       | purpose                          |
|--------------------------|-------------------------------|----------------------------------|
| `LINEAR_MOCK_STATE_DIR`  | `~/.openclaw/linear_mock`     | state.json directory             |
| `LINEAR_MOCK_SEED_PATH`  | unset                         | preload state.json on first start |

The Dockerfile sets `LINEAR_MOCK_STATE_DIR=/workspace/output/end_state/linear`
to match the openclaw rollout layout.
