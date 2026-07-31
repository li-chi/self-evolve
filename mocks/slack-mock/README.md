# slack-mock

Mock MCP server mirroring `slack-mcp-server@1.1.23`
(github.com/korotovsky/slack-mcp-server) — the Slack entry registered
in `mcp-atlas`'s `mcp_server_template.json`. Slack is **Tier A**:
real workspaces don't parallelize for RL rollouts, so we run a
deterministic in-memory mock instead.

## Why match this server (not the Anthropic reference Slack MCP)

Two well-known "slack MCP" servers exist:

| Server                                       | Output style                  |
|----------------------------------------------|-------------------------------|
| `@modelcontextprotocol/server-slack`         | Slack Web API JSON            |
| `slack-mcp-server` (korotovsky, **registered**) | CSV-ish text + plain strings |

The registered one returns **CSV** for list/search/history tools (via
gocsv) and **plain "Successfully ..." strings** for action tools.
This mock matches that contract exactly, so it's a drop-in replacement
for the binary npm package the harness would otherwise spawn.

## Implemented tools (21 + 2 mock helpers)

| group       | tool                              |
|-------------|-----------------------------------|
| Channels    | `channels_list`                   |
|             | `channels_me`                     |
|             | `conversations_join`              |
|             | `conversations_leave`             |
|             | `conversations_mark`              |
| Messages    | `conversations_history`           |
|             | `conversations_replies`           |
|             | `conversations_add_message`       |
|             | `conversations_search_messages`   |
|             | `conversations_unreads`           |
| Reactions   | `reactions_add`                   |
|             | `reactions_remove`                |
| Users       | `users_search`                    |
| Usergroups  | `usergroups_list`                 |
|             | `usergroups_me`                   |
|             | `usergroups_create`               |
|             | `usergroups_update`               |
|             | `usergroups_users_update`         |
| Saved       | `saved_list`                      |
|             | `saved_update`                    |
|             | `saved_clear_completed`           |
| Files       | `attachment_get_data`             |
| Mock-only   | `mock_debug_state`, `mock_debug_seed` |

Tool names and parameter names match the upstream Go registrations in
`pkg/server/server.go` verbatim.

## State

A single JSON file at `$SLACK_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/slack_mock`). Layout:

```jsonc
{
  "workspace": {"id":"T0000000000","name":"Mock Workspace","domain":"mock","url":"..."},
  "self":      {"id":"USELF000000","name":"mockbot"},
  "users":     {"<uid>": {"id","name","real_name","profile":{...}}},
  "channels":  {"<cid>": {"id","name","is_private","is_im","is_mpim",
                          "is_general","topic","purpose","members":[...]}},
  "messages":  {"<cid>": [{"ts","user","text","thread_ts","reactions":[...],
                           "reply_count":N}]},
  "usergroups":{"<sid>": {"id","name","handle","users":[...]}},
  "saved":     [{"item_id","ts","state","date_due","date_completed"}],
  "files":     {"<fid>": {"name","mimetype","content","encoding","size"}},
  "next_id":   {"channel":N,"user":N,"usergroup":N,"file":N,"ts":N},
  "calls":     [{"op":"...","ts":"...",...}]
}
```

Set `SLACK_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist (per-rollout
isolation should clear the state dir between rollouts). Per-task
fixtures are typically loaded via the `mock_debug_seed` tool instead.

## Behavior notes / known mock-vs-real gaps

- `limit` on `conversations_history` accepts the upstream range format
  (`1d`, `7d`, `30d`, `1w`, `1m`) as well as plain counts (`50`).
- `cursor` pagination uses the message `ts` (history/replies/search)
  or item `id` (channels/usergroups) — matches the upstream "value of
  last row" convention.
- Date filters in `conversations_search_messages` accept ISO `YYYY-MM-DD`
  only; the natural-language values the real server resolves
  (`"Yesterday"`, `"July"`, …) are not interpreted.
- `attachment_get_data` returns whatever content was seeded — no real
  Slack download.
- The upstream's `SLACK_MCP_ADD_MESSAGE_TOOL` / `SLACK_MCP_REACTION_TOOL`
  channel allow-lists are **not** enforced; every tool is always enabled.
- `conversations_unreads` "partner vs internal" distinction is collapsed
  (no Enterprise Connect concept).
- Authentication is not modeled — there is no xoxc/xoxp/xoxb check.

## Env

| var                      | default                       | purpose                          |
|--------------------------|-------------------------------|----------------------------------|
| `SLACK_MOCK_STATE_DIR`   | `~/.openclaw/slack_mock`      | state.json directory             |
| `SLACK_MOCK_SEED_PATH`   | unset                         | preload state.json on first start |

The Dockerfile sets `SLACK_MOCK_STATE_DIR=/workspace/output/end_state/slack`
to match the openclaw rollout layout.
