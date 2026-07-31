# telegram-mock

Mock MCP server mirroring the [Telegram Bot API](https://core.telegram.org/bots/api).
Each tool is named after a real Bot API method (camelCase, e.g.
`sendMessage`, `getChat`), accepts the same parameter names, and
returns the canonical Telegram envelope:

| Outcome | Shape |
|---------|-------|
| success | `{"ok": true,  "result": <object>}` |
| failure | `{"ok": false, "error_code": <int>, "description": "..."}` |

Error envelopes are **returned**, not raised — so the trace looks like
a real failed HTTPS request to `api.telegram.org`. This is the same
contract `notion-mock` uses for Notion REST.

## Implemented tools (30 = 28 Bot API + 2 mock helpers)

| group        | tool                                  |
|--------------|---------------------------------------|
| Bot/Updates  | `getMe`                               |
|              | `getUpdates`                          |
| Sending      | `sendMessage`                         |
|              | `forwardMessage`                      |
|              | `copyMessage`                         |
|              | `sendPhoto`                           |
|              | `sendDocument`                        |
|              | `sendVideo`                           |
|              | `sendAudio`                           |
|              | `sendLocation`                        |
|              | `sendChatAction`                      |
| Editing      | `editMessageText`                     |
|              | `editMessageReplyMarkup`              |
|              | `deleteMessage`                       |
| Callbacks    | `answerCallbackQuery`                 |
| Chat/Members | `getChat`                             |
|              | `getChatMember`                       |
|              | `getChatAdministrators`               |
|              | `getChatMemberCount`                  |
|              | `leaveChat`                           |
|              | `banChatMember`                       |
|              | `unbanChatMember`                     |
|              | `pinChatMessage`                      |
|              | `unpinChatMessage`                    |
| Commands     | `setMyCommands`                       |
|              | `getMyCommands`                       |
|              | `deleteMyCommands`                    |
| Webhook      | `setWebhook`                          |
|              | `deleteWebhook`                       |
|              | `getWebhookInfo`                      |
| Mock-only    | `mock_debug_state`, `mock_debug_seed` |

Tool names and parameter names match the Bot API method names verbatim
(see https://core.telegram.org/bots/api).

## Object shapes

- **User**: `{id, is_bot, first_name, last_name?, username?, language_code?}`
- **Chat**: `{id, type, title?, username?, first_name?, last_name?}`
  where `type ∈ {"private","group","supergroup","channel"}`.
- **Message**: `{message_id, from: User, chat: Chat, date,
  text?, entities?, caption?, photo?, document?, video?, audio?,
  location?, reply_to_message?, reply_markup?, edit_date?, ...}`
- **ChatMember**: `{status, user, ...}` with `status ∈ {creator,
  administrator, member, restricted, left, kicked}`.

Telegram uses **integer** ids: positive for users / private chats,
large negative (`-100…`) for supergroups & channels. `message_id` is
unique **per chat**. `chat_id` may also be a string starting with `@`
for public chats/channels — the mock resolves it via `chat.username`.

## State

A single JSON file at `$TELEGRAM_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/telegram_mock`). Layout:

```jsonc
{
  "self":      {"id": 700000001, "is_bot": true, "username": "mockbot", ...},
  "users":     {"<user_id>": {"id","is_bot","first_name",...}},
  "chats":     {"<chat_id>": {"id","type","title","username",
                              "members":[{"user_id","status",...}],
                              "banned":[user_id,...],
                              "pinned_message_ids":[...]}},
  "messages":  {"<chat_id>": [Message, ...]},
  "updates":   [Update, ...],
  "callback_queries": {"<cqid>": {"answered": bool, "answer": {...}}},
  "commands":  {"<scope_key>": {"commands": [...], "scope": {...},
                                "language_code": ""}},
  "webhook":   {"url","has_custom_certificate","max_connections",
                "allowed_updates","ip_address","last_error_date",
                "last_error_message"},
  "next_id":   {"update", "message": {<chat_id>: int}, "user",
                "chat", "file"},
  "calls":     [{"op","ts",...}]
}
```

Set `TELEGRAM_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist (per-rollout
isolation should clear the state dir between rollouts). Per-task
fixtures are typically loaded via the `mock_debug_seed` tool.

## Behavior notes / known mock-vs-real gaps

- File uploads (`sendPhoto`, `sendDocument`, etc.) **do not** actually
  parse `multipart/form-data` — they accept any string as the media
  argument (file_id, URL, or `attach://name`) and synthesize a fresh
  `file_id`/`file_unique_id` for the resulting Message.
- `getUpdates` is **not** long-polling. The `timeout` parameter is
  accepted and ignored — the call returns immediately with whatever
  updates have been queued (seeded via `mock_debug_seed`).
- `editMessageText` / `editMessageReplyMarkup` accept
  `inline_message_id` and return `true` for inline edits (no
  inline-message persistence).
- `parse_mode` is accepted but the mock does not actually parse
  HTML/Markdown into entities — pass `entities` explicitly if you need
  them on the resulting Message.
- `setMyCommands` validates command names match `[a-z0-9_]{1,32}` and
  descriptions are 1..256 chars (matches real-API validation).
- Permissions on `banChatMember`/`pinChatMessage`/etc. are **not**
  enforced — the bot is assumed to have whatever admin rights it needs.
- The bot's identity defaults to `{id: 700000001, username:
  "mockbot"}`; override via `mock_debug_seed(self_user=...)`.

## Env

| var                        | default                          | purpose                            |
|----------------------------|----------------------------------|------------------------------------|
| `TELEGRAM_MOCK_STATE_DIR`  | `~/.openclaw/telegram_mock`      | state.json directory               |
| `TELEGRAM_MOCK_SEED_PATH`  | unset                            | preload state.json on first start  |

The Dockerfile sets
`TELEGRAM_MOCK_STATE_DIR=/workspace/output/end_state/telegram` to
match the openclaw rollout layout.
