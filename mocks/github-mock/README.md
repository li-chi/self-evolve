# github-mock

Mock MCP server that mirrors [`github/github-mcp-server`](https://github.com/github/github-mcp-server)
— specifically the [`lockon-n/github-mcp-server`](https://github.com/lockon-n/github-mcp-server)
fork pinned at commit `ef07feb` that Toolathlon uses. Tool names and
parameter shapes match that fork verbatim so an agent trained on the
real server sees the same surface.

Responses follow the GitHub REST API JSON shapes (repository, issue,
pull request, content with `content` text or base64). Errors are
returned as GitHub-shaped error objects (`{"message":"Not Found",
"status":"404","documentation_url":"..."}`), not raised, so the trace
looks like a real failed HTTP response.

## Tool surface

| tool                                       | GitHub REST endpoint                                        |
|--------------------------------------------|-------------------------------------------------------------|
| `get_me`                                   | GET    /user                                                |
| `create_repository`                        | POST   /user/repos                                          |
| `fork_repository`                          | POST   /repos/{owner}/{repo}/forks                          |
| `get_file_contents`                        | GET    /repos/{owner}/{repo}/contents/{path}                |
| `create_or_update_file`                    | PUT    /repos/{owner}/{repo}/contents/{path}                |
| `delete_file`                              | DELETE /repos/{owner}/{repo}/contents/{path}                |
| `push_files`                               | (composite — git refs + tree + commit)                      |
| `list_branches`                            | GET    /repos/{owner}/{repo}/branches                       |
| `create_branch`                            | POST   /repos/{owner}/{repo}/git/refs                       |
| `list_commits`                             | GET    /repos/{owner}/{repo}/commits                        |
| `get_commit`                               | GET    /repos/{owner}/{repo}/commits/{ref}                  |
| `get_issue`                                | GET    /repos/{owner}/{repo}/issues/{issue_number}          |
| `list_issues`                              | GET    /repos/{owner}/{repo}/issues (GraphQL-shaped)        |
| `create_issue`                             | POST   /repos/{owner}/{repo}/issues                         |
| `update_issue`                             | PATCH  /repos/{owner}/{repo}/issues/{issue_number}          |
| `add_issue_comment`                        | POST   /repos/{owner}/{repo}/issues/{n}/comments            |
| `get_issue_comments`                       | GET    /repos/{owner}/{repo}/issues/{n}/comments            |
| `get_pull_request`                         | GET    /repos/{owner}/{repo}/pulls/{pull_number}            |
| `list_pull_requests`                       | GET    /repos/{owner}/{repo}/pulls                          |
| `create_pull_request`                      | POST   /repos/{owner}/{repo}/pulls                          |
| `update_pull_request`                      | PATCH  /repos/{owner}/{repo}/pulls/{pull_number}            |
| `merge_pull_request`                       | PUT    /repos/{owner}/{repo}/pulls/{pull_number}/merge      |
| `get_pull_request_files`                   | GET    /repos/{owner}/{repo}/pulls/{pull_number}/files      |
| `get_pull_request_reviews`                 | GET    /repos/{owner}/{repo}/pulls/{pull_number}/reviews    |
| `create_and_submit_pull_request_review`    | POST   /repos/{owner}/{repo}/pulls/{pull_number}/reviews    |
| `search_repositories`                      | GET    /search/repositories?q=                              |
| `search_code`                              | GET    /search/code?q=                                      |
| `search_issues`                            | GET    /search/issues?q= (scoped to is:issue)               |

Plus mock-only debug helpers (not present in the real server):

- `mock_debug_state` — dump the entire persisted state dict.
- `mock_debug_seed_repo` — directly insert a repo with optional
  initial files; for per-task seeders.

### Parameter quirks worth noting (match the official server exactly)

- Pull-request tools use **`pullNumber`** (camelCase) for the PR
  number, while issue tools use **`issue_number`** (snake). Don't
  unify these — agents trained on the real server pick the matching
  one per tool.
- `create_or_update_file` takes raw UTF-8 **`content`** (a string),
  not pre-base64. The mock encodes internally.
- `push_files` `files` is an array of `{"path": str, "content": str}`
  objects.
- `create_repository` uses **`autoInit`** (camelCase) — odd man out
  among the snake-cased params.
- `list_issues` is **GraphQL-shaped**: `state` is `"OPEN" | "CLOSED"`,
  ordering is `orderBy` + `direction` (`CREATED_AT|UPDATED_AT|COMMENTS`
  / `ASC|DESC`), pagination is cursor-based (`after` + `perPage`),
  response is `{items, pageInfo, totalCount}`.
- `list_pull_requests` is **REST-shaped**: `state` is
  `"open"|"closed"|"all"`, ordering is `sort` + `direction`,
  pagination is page-based.
- `create_and_submit_pull_request_review` `event` is one of
  `APPROVE | REQUEST_CHANGES | COMMENT`; submitted review state is
  the past-tense `APPROVED|CHANGES_REQUESTED|COMMENTED`.

## Skipped in v1

Whole toolsets the 7 in-scope Toolathlon github tasks
(`dataset-license-issue`, `email-paper-homepage`, `git-repo`,
`personal-website-construct`, `sync-todo-to-readme`, `task-tracker`,
`youtube-repo`) never invoke — not shipped:

- **Actions** — workflows, runs, jobs, artifacts, secrets.
- **Copilot** — `assign_copilot_to_issue`, `request_copilot_review`.
- **Code scanning / Secret scanning / Dependabot / Security advisories**.
- **Discussions** (REST/GQL).
- **Gists**.
- **Notifications**.
- **Projects (v2)** — board/field tools.
- **Releases** (`get_latest_release`, `list_releases`, `get_release_by_tag`).
- **Tags** (`list_tags`, `get_tag`).
- **Teams** (`get_teams`, `get_team_members`).
- **Users/orgs search** (`search_users`, `search_orgs`).
- **PR diff/status/branch ops**: `get_pull_request_diff`,
  `get_pull_request_status`, `update_pull_request_branch`,
  `get_pull_request_comments`.
- **Pending PR review flow**: `create_pending_pull_request_review`,
  `add_comment_to_pending_review`, `submit_pending_pull_request_review`,
  `delete_pending_pull_request_review` (we ship the one-shot
  `create_and_submit_pull_request_review` instead).
- **Sub-issues**: `add_sub_issue`, `list_sub_issues`,
  `remove_sub_issue`, `reprioritize_sub_issue`, `list_issue_types`.
- **`rename_repository`** — no in-scope task renames.

`get_branch` is not in the fork either — `list_branches` + filter is
the canonical pattern.

## State

State lives in `$GITHUB_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/github/state.json` inside the container;
`~/.openclaw/github_mock/state.json` outside). Schema:

```jsonc
{
  "user": {"login":"mock-user","id":1, ...},
  "repos": {
    "<owner>/<name>": {
      // standard GitHub repo object (id, full_name, owner, ...)
      "default_branch": "main",
      // mock-internal (stripped on response):
      "files":    {"<branch>": {"<path>": {"sha","size","content_b64"}}},
      "branches": {"<branch>": {"name","sha","protected"}},
      "commits":  {"<sha>": <commit_object>},
      "commit_order": ["<sha>", ...],          // newest first
      "issues_index": [<number>, ...],
      "pulls_index":  [<number>, ...],
      "next_issue_number": 1
    }
  },
  "issues": {
    "<owner>/<name>#<n>": {
      // standard issue object (id, number, title, body, state, ...)
      "_comments": [<comment>, ...]            // stripped on response
    }
  },
  "pulls": {
    "<owner>/<name>#<n>": {
      // standard PR object (id, number, head, base, state, merged, ...)
      "_reviews": [<review>, ...],             // stripped on response
      "_files":   [<file>, ...]                // synthesised if empty
    }
  },
  "next_id": {"repo": N, "issue": N, "pull": N, "comment": N, "review": N},
  "calls":   [{"op":"...","ts":"...", ...}]
}
```

The `calls` log is what the verifier consumes — every tool appends
an entry. File-locking via `fcntl.flock` makes concurrent calls safe;
per-rollout isolation should reset the state dir between rollouts.

Seed an initial state by setting `GITHUB_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.

## Run

```bash
# local
GITHUB_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  github-mock:
    build:
      context: ../../mcp_servers/github-mock
      dockerfile: Dockerfile
    image: mcp-env/github-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      GITHUB_MOCK_STATE_DIR: /workspace/output/end_state/github
      GITHUB_MOCK_SEED_PATH: /workspace/input/github_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
