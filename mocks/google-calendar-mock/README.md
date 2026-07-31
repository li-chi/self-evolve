# google-calendar-mock

Mock MCP server mirroring [`@gongrzhe/server-calendar-autoauth-mcp`](https://github.com/GongRzhe/Calendar-Autoauth-MCP-Server),
which is what Toolathlon uses as its `google_calendar` server. Every
tool name and parameter shape matches the official server; responses
match the Google Calendar API v3 `events.*` resource shapes that
`@gongrzhe/server-calendar-autoauth-mcp` returns verbatim through
its `googleapis` client.

## Tool surface (5, plus 3 debug)

The upstream is intentionally minimal — only 5 user-facing tools,
all operating on `calendarId = "primary"`:

| tool           | maps to                                |
|----------------|----------------------------------------|
| `create_event` | `calendar.events.insert`               |
| `get_event`    | `calendar.events.get`                  |
| `update_event` | `calendar.events.patch`                |
| `delete_event` | `calendar.events.delete`               |
| `list_events`  | `calendar.events.list (singleEvents)`  |

Mock-only fixture helpers (not in upstream):

- `mock_debug_state` — return the full persisted state.
- `mock_debug_set_user(email, name?, timeZone?)` — set the
  authenticated user's email/name and the primary calendar's
  timezone (per-task setup).
- `mock_debug_seed_event(event, calendarId="primary")` — insert a
  fully-formed v3 event dict (per-task fixture loading).

## Parameter notes

`create_event` accepts the upstream zod schema (`summary`, `start`,
`end`, `description?`, `location?`) plus three optional extensions
(`attendees`, `colorId`, `recurrence`) that the underlying Google
Calendar API supports but the upstream tool doesn't surface. They
are accepted to give tasks a path to richer fixtures without
exceeding the upstream contract.

`update_event` uses `events.patch` semantics: only fields explicitly
provided are touched.

`list_events` honors `timeMin` / `timeMax` / `maxResults`
(default 10) / `orderBy` (`startTime` | `updated`, default
`startTime`). Recurrence rules are stored verbatim but **not
expanded** — the mock treats every event as a single instance. This
is fine for the two Toolathlon tasks that consume this server
(`set-conf-cr-ddl`, `student-interview`), neither of which uses
recurring events.

## Skipped vs. the user spec

The user spec speculated about additional tools (`list_calendars`,
`search_events`, `get_freebusy`, `list_colors`, `add_attendee`,
`respond_to_event`). The upstream
`@gongrzhe/server-calendar-autoauth-mcp` v1.0.2 does **not** expose
any of these — its `src/index.ts` registers exactly the 5 tools
listed above. The mock matches the upstream verbatim; if a future
task needs more it should be added intentionally.

## State

`$GCAL_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/gcal/state.json` inside the container,
`~/.openclaw/gcal_mock/state.json` on the host).

```jsonc
{
  "user": {"email": "...", "name": "..."},
  "calendars": {
    "primary": {
      "id": "primary",
      "summary": "<calendar title>",
      "timeZone": "America/Los_Angeles",
      "accessRole": "owner",
      "colorId": "7"
    }
  },
  "events": {
    "primary": {
      "<eventId>": {
        "kind": "calendar#event", "etag": "...", "id": "...",
        "status": "confirmed",
        "summary": "...", "description": "...", "location": "...",
        "start": {"dateTime": "...", "timeZone": "..."},
        "end":   {"dateTime": "...", "timeZone": "..."},
        "attendees": [{"email": "...", "responseStatus": "..."}],
        "creator": {"email": "...", "self": true},
        "organizer": {"email": "...", "self": true},
        "created": "...", "updated": "...",
        "htmlLink": "...", "iCalUID": "...",
        "sequence": 0, "reminders": {"useDefault": true},
        "eventType": "default",
        "recurrence": []
      }
    }
  },
  "next_id": {"event": N},
  "calls": [{"op", "ts", ...}]
}
```

Seed via `GCAL_MOCK_SEED_PATH` (loaded once if no `state.json`
exists). The `calls` list is appended to by every tool invocation
and is what verifier scripts read to assert side-effects.

## Errors

Errors are returned as Google Calendar API v3 error objects (not
raised, so the trace looks like a real failed HTTP response):

```json
{"error": {"code": 404, "message": "Not Found",
           "errors": [{"reason": "notFound",
                       "message": "Not Found: <id>",
                       "domain": "global"}]}}
```
