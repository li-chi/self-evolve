# mailgun-mock

State-backed mock MCP server that mirrors the [Mailgun API v3 REST
surface](https://documentation.mailgun.com/docs/mailgun/api-reference/).
Tool names match Mailgun's REST operations and responses use Mailgun's
JSON shapes (`{"items": [...], "paging": {...}}` for lists,
`{"id": "<...>", "message": "Queued. Thank you."}` for sends, etc.) so
the mock is a drop-in stand-in for a Mailgun client during rollouts.

This is **not** a wrapper around the older `mailgun-integration` CLI
mock; it implements the REST surface directly.

## Tools

| Group         | Tool                       | Mailgun operation                                  |
|---------------|----------------------------|----------------------------------------------------|
| Messages      | `send_message`             | `POST /v3/{domain}/messages`                       |
| Messages      | `retrieve_stored_message`  | `GET /v3/domains/{domain}/messages/{storage_key}`  |
| Domains       | `list_domains`             | `GET /v4/domains`                                  |
| Domains       | `get_domain`               | `GET /v4/domains/{name}`                           |
| Domains       | `create_domain`            | `POST /v4/domains`                                 |
| Domains       | `delete_domain`            | `DELETE /v3/domains/{name}`                        |
| Mailing lists | `list_mailing_lists`       | `GET /v3/lists/pages`                              |
| Mailing lists | `create_mailing_list`      | `POST /v3/lists`                                   |
| Mailing lists | `list_list_members`        | `GET /v3/lists/{address}/members/pages`            |
| Mailing lists | `add_list_member`          | `POST /v3/lists/{address}/members`                 |
| Mailing lists | `remove_list_member`       | `DELETE /v3/lists/{address}/members/{member}`      |
| Events/stats  | `list_events`              | `GET /v3/{domain}/events`                          |
| Events/stats  | `get_stats`                | `GET /v3/{domain}/stats/total`                     |
| Suppressions  | `list_bounces`             | `GET /v3/{domain}/bounces`                         |
| Suppressions  | `add_bounce`               | `POST /v3/{domain}/bounces`                        |
| Suppressions  | `delete_bounce`            | `DELETE /v3/{domain}/bounces/{address}`            |
| Suppressions  | `list_unsubscribes`        | `GET /v3/{domain}/unsubscribes`                    |
| Suppressions  | `list_complaints`          | `GET /v3/{domain}/complaints`                      |
| Tags          | `list_tags`                | `GET /v3/{domain}/tags`                            |
| Tags          | `get_tag`                  | `GET /v3/{domain}/tags/{tag}`                      |
| Debug         | `mock_debug_state`         | full state dump                                    |
| Debug         | `mock_debug_seed`          | preload domains/lists/events/etc.                  |

## State

All state lives in a single JSON file at:

```
$MAILGUN_MOCK_STATE_DIR/state.json   # default ~/.openclaw/mailgun_mock
```

Locking uses `fcntl.flock` on a sibling `.lock` file so concurrent
tool calls are safe. Every call (including reads) appends an entry to
`state["calls"]` for verifier replay.

To preload state at startup (when no `state.json` exists), set:

```
MAILGUN_MOCK_SEED_PATH=/path/to/seed.json
```

## Notes on shapes

- `send_message` returns Mailgun's exact send acknowledgement:

  ```json
  {"id": "<20240120120000.abc123def456@example.com>", "message": "Queued. Thank you."}
  ```

  Mailgun message ids are RFC822 `Message-ID` strings, angle-bracketed.

- List endpoints return `{"items": [...], "paging": {first, previous,
  next, last}}` — Mailgun's pagination shape.

- `list_domains` returns Mailgun v4's `{"items": [...], "total_count":
  N}` shape instead of `paging`.

- Events have `event`, `timestamp` (unix float), `recipient`,
  `recipient-domain`, `message`, `storage`, `tags`, `user-variables`,
  `flags`, `envelope`, `log-level` — matching Mailgun's events log.

- Errors are raised as `ValueError(...)` with Mailgun-ish reason
  strings (e.g. `domain_not_found: example.com`,
  `member_already_exists: ...`). MCP surfaces these as tool errors.

## Parameter naming

Because Python reserves `from`, we accept `from_` for the sender
field. Mailgun's `o:tag`/`o:tracking`/`h:Reply-To`/`v:my-var` form
fields become `o_tag`/`o_tracking`/`h_reply_to`/`v_my_var`. State
filter for `list_domains` is `state_filter` to avoid shadowing the
local `state` dict.
