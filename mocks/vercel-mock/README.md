# vercel-mock

Mock MCP server that mirrors the public Vercel REST API
(https://vercel.com/docs/rest-api). Tool names match the Vercel REST
operationIds (camelCase, e.g. `listProjects`, `createDeployment`),
and tool parameters + response shapes match the real API so the mock
is a drop-in stand-in during RL rollouts.

This server does **not** wrap the Vercel CLI. It models the REST
surface directly: deployments, projects, domains, environment
variables, teams, and the auth user.

## Implemented tools (20 + 2 mock helpers)

| group           | tool                              |
|-----------------|-----------------------------------|
| Projects        | `listProjects`                    |
|                 | `getProject`                      |
|                 | `createProject`                   |
|                 | `updateProject`                   |
|                 | `deleteProject`                   |
| Deployments     | `listDeployments`                 |
|                 | `getDeployment`                   |
|                 | `createDeployment`                |
|                 | `cancelDeployment`                |
|                 | `deleteDeployment`                |
|                 | `listDeploymentFiles`             |
| Project domains | `listProjectDomains`              |
|                 | `addProjectDomain`                |
|                 | `removeProjectDomain`             |
| Env variables   | `listProjectEnv`                  |
|                 | `createProjectEnv`                |
|                 | `deleteProjectEnv`                |
| Teams           | `listTeams`                       |
|                 | `getTeam`                         |
| User            | `getAuthUser`                     |
| Mock-only       | `mock_debug_state`, `mock_debug_seed` |

## Response shape

All responses match Vercel REST JSON. Errors follow:

```json
{"error": {"code": "not_found", "message": "Project not found: foo"}}
```

List endpoints return `{"<plural>": [...], "pagination": {"count","next","prev"}}`
(e.g. `{"projects": [...], "pagination": {...}}`), where `next`/`prev`
are `createdAt` epoch-ms cursors.

Project objects carry `id` (`prj_...`), `name`, `accountId`,
`createdAt`, `updatedAt`, `framework`, `gitRepository`, plus build
settings. Deployment objects carry `uid` (`dpl_...`), `name`, `url`,
`state` (`BUILDING|READY|ERROR|CANCELED|QUEUED`), `target`
(`production|preview|staging|null`), and `createdAt`.

## ID formats

| prefix    | type              |
|-----------|-------------------|
| `prj_*`   | project           |
| `dpl_*`   | deployment        |
| `dom_*`   | project domain    |
| `env_*`   | environment var   |
| `team_*`  | team              |
| `user_*`  | user              |
| `file_*`  | deployment file   |

## State

A single JSON file at `$VERCEL_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/vercel_mock`). Layout:

```jsonc
{
  "user":        {"id":"user_...","username":"...","email":"...","name":"..."},
  "teams":       {"team_...": {"id","slug","name","createdAt", ...}},
  "projects":    {"prj_...":  {"id","name","accountId","teamId",
                               "createdAt","updatedAt","framework",
                               "gitRepository","buildCommand",...}},
  "deployments": {"dpl_...":  {"uid","name","url","state","target",
                               "createdAt","projectId","teamId",
                               "gitSource","meta", ...}},
  "domains":     {"dom_...":  {"id","name","projectId","gitBranch",
                               "redirect","verified", ...}},
  "env_vars":    {"env_...":  {"id","key","value","target","type",
                               "projectId", ...}},
  "files":       {"file_...": {"uid","name","mode","deploymentId"}},
  "next_seq":    {"prj":N,"dpl":N,"dom":N,"env":N,"team":N,"file":N},
  "calls":       [{"op":"...","ts":"...",...}]
}
```

Set `VERCEL_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist (per-rollout
isolation should clear the state dir between rollouts). Per-task
fixtures are typically loaded via the `mock_debug_seed` tool.

## Behavior notes / known mock-vs-real gaps

- Deployments are created in state `BUILDING`. They do not transition
  to `READY` automatically — use `mock_debug_seed` to inject a deployment
  with `state: "READY"` (and `readyAt`) for fixtures that need a
  pre-built deploy. `cancelDeployment` moves to `CANCELED`.
- `createDeployment` accepts `files` as a list (each item with a `file`
  field) and stores them as file objects, but does not actually upload
  or hash content. `listDeploymentFiles` returns the flat list as
  recorded.
- Domain verification is always `true` — DNS/cert flow not modeled.
- Env vars marked `encrypted`/`secret`/`sensitive` are masked
  (`value: null`) by `listProjectEnv` unless `decrypt=true`.
- Pagination uses `createdAt` epoch-ms cursors (`since`/`until`), matching
  Vercel's `v6/deployments` pagination convention.
- `teamId` scoping is enforced: passing `teamId=null` returns
  personal-scope items only; passing a team id filters to that team.

## Env

| var                       | default                       | purpose                          |
|---------------------------|-------------------------------|----------------------------------|
| `VERCEL_MOCK_STATE_DIR`   | `~/.openclaw/vercel_mock`     | state.json directory             |
| `VERCEL_MOCK_SEED_PATH`   | unset                         | preload state.json on first start |

The Dockerfile sets `VERCEL_MOCK_STATE_DIR=/workspace/output/end_state/vercel`
to match the openclaw rollout layout.
