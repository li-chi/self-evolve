# outlook-mock

Mock MCP server mirroring the **Microsoft Graph v1.0** mail + calendar
surface (https://learn.microsoft.com/en-us/graph/api/overview). It
returns the same JSON shapes the real Graph API uses
(`@odata.context`, `value` collections, `error.code`/`error.message`
envelopes) so the mock is a drop-in stand-in for a Graph-backed MCP
server during rollouts.

## Why this server (not the CLI mock)

A simpler `outlook-microsoft` CLI mock exists under
`terminal-tool-use/mocks/outlook-microsoft/` — it returns
`{"status":"ok","data":...}` and is shaped for a single-binary
agent. This server, in contrast, mirrors the **real Graph REST
shapes** so it can stand in for a Graph-driven MCP server. We do not
wrap the CLI mock.

## Implemented tools (22 + 2 mock helpers)

| group         | tool                          | Graph operation                              |
|---------------|-------------------------------|----------------------------------------------|
| Mail messages | `list_messages`               | `GET /me/messages` / mailFolder messages     |
|               | `get_message`                 | `GET /me/messages/{id}`                      |
|               | `send_mail`                   | `POST /me/sendMail`                          |
|               | `reply_mail`                  | `POST /me/messages/{id}/reply` (+replyAll)   |
|               | `forward_mail`                | `POST /me/messages/{id}/forward`             |
|               | `delete_message`              | `DELETE /me/messages/{id}`                   |
|               | `move_message`                | `POST /me/messages/{id}/move`                |
|               | `create_draft`                | `POST /me/messages` (draft)                  |
|               | `send_draft`                  | `POST /me/messages/{id}/send`                |
| Mail folders  | `list_mail_folders`           | `GET /me/mailFolders`                        |
|               | `create_mail_folder`          | `POST /me/mailFolders`                       |
| Calendar      | `list_events`                 | `GET /me/events` / `GET /me/calendarView`    |
|               | `get_event`                   | `GET /me/events/{id}`                        |
|               | `create_event`                | `POST /me/events`                            |
|               | `update_event`                | `PATCH /me/events/{id}`                      |
|               | `delete_event`                | `DELETE /me/events/{id}`                     |
|               | `accept_event`                | `POST /me/events/{id}/accept`                |
|               | `decline_event`               | `POST /me/events/{id}/decline`               |
|               | `tentatively_accept_event`    | `POST /me/events/{id}/tentativelyAccept`     |
| Calendars     | `list_calendars`              | `GET /me/calendars`                          |
|               | `get_calendar`                | `GET /me/calendar(s)/{id}`                   |
| Contacts      | `list_contacts`               | `GET /me/contacts`                           |
| Mock-only     | `mock_debug_state`, `mock_debug_seed` | n/a                                  |

Tool names follow the snake_case form of the Graph operation IDs
(e.g. `user_list_messages` → `list_messages`).

## Response shapes

List responses:

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('me')/messages",
  "value": [ ... ],
  "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=10&$top=10"
}
```

Single-entity responses:

```json
{
  "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('me')/messages/$entity",
  "id": "AAMkAD...",
  "subject": "...",
  "body": {"contentType": "HTML", "content": "..."},
  "from": {"emailAddress": {"name": "...", "address": "..."}},
  "toRecipients": [ {"emailAddress": {"name": "...", "address": "..."}} ],
  ...
}
```

Errors (instead of raising, like notion-mock):

```json
{
  "error": {
    "code": "ErrorItemNotFound",
    "message": "...",
    "innerError": {"date": "...", "request-id": "...", "client-request-id": "..."}
  },
  "_status": 404
}
```

`_status` is a non-wire hint so verifiers can distinguish the HTTP
status the real API would have used. The real Graph signals via
HTTP status code only; this field can be ignored.

Empty responses (real Graph 202/204) are returned as `{}`.

## State

A single JSON file at `$OUTLOOK_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/outlook_mock`):

```jsonc
{
  "user":      {"id","displayName","mail","userPrincipalName","mailboxSettings"},
  "default_calendar_id": "...",
  "folders":   {"<fid>": {"id","displayName","parentFolderId",...}},
  "messages":  {"<mid>": {"id","subject","body","from","toRecipients",...}},
  "events":    {"<eid>": {"id","subject","start","end","attendees",...}},
  "calendars": {"<cid>": {"id","name","isDefaultCalendar",...}},
  "contacts":  {"<cid>": {"id","displayName","emailAddresses",...}},
  "calls":     [{"op":"...","ts":"...",...}]
}
```

`OUTLOOK_MOCK_SEED_PATH` may point at a JSON file in the same shape
to preload state on first start. Per-task fixtures are typically
loaded via the `mock_debug_seed` tool instead.

The mailbox is initialized with the canonical well-known folders
(`inbox`, `drafts`, `sentitems`, `deleteditems`, `junkemail`,
`outbox`, `archive`). Folder ids accept either the well-known name
or the opaque id.

## Behavior notes / known mock-vs-real gaps

- `$filter` is implemented as a small subset: `<key> eq '<val>'`,
  `<key> eq <bool|num>`, and `contains(<key>, '<val>')`, joined by
  ` and `. Unrecognized expressions match-all.
- `$search` is a case-insensitive substring scan over the most
  obvious fields per resource (subject + bodyPreview/body for
  messages and events; displayName/givenName/surname for contacts).
- `$orderby` supports a single field with `asc`/`desc` (including the
  Graph-special `start/dateTime` path on `list_events`).
- Move semantics: `move_message` issues a new id for the moved
  message (matches Graph), then deletes the old id.
- `send_mail` with `saveToSentItems=False` returns 202 without
  persisting a sent record.
- `accept_event` / `decline_event` / `tentatively_accept_event`
  flip the signed-in user's attendee status and the event's
  `responseStatus`; they do not actually send invitation responses.
- Recurrence (`recurrence` property) and timezone math are not
  modeled — events use the timezone supplied in `start`/`end`.
- Attachments, extended properties, immutable IDs, batching, and
  delta queries are not implemented.

## Env

| var                       | default                          | purpose                            |
|---------------------------|----------------------------------|------------------------------------|
| `OUTLOOK_MOCK_STATE_DIR`  | `~/.openclaw/outlook_mock`       | state.json directory               |
| `OUTLOOK_MOCK_SEED_PATH`  | unset                            | preload state.json on first start  |

The Dockerfile sets `OUTLOOK_MOCK_STATE_DIR=/workspace/output/end_state/outlook`
to match the openclaw rollout layout.
