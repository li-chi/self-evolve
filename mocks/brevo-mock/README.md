# brevo-mock

Mock MCP server mirroring the [Brevo](https://developers.brevo.com/reference/getting-started-1)
(formerly Sendinblue) REST API v3 tool surface. Brevo is **Tier**
state_json: real Brevo workspaces do not parallelize for RL rollouts,
so we run a deterministic in-memory mock instead.

## Tool surface

Tool names follow Brevo's REST `operationId` convention verbatim
(`sendTransacEmail`, `getContactInfo`, `createSmtpTemplate`, ...). Tool
parameters and response shapes match the real Brevo v3 endpoints:
integer `id` fields for contacts/lists/templates/senders, the
`<datetime.seq@smtp-relay.mailin.fr>` style `messageId` strings for
transactional emails, and Brevo-shaped error bodies
(`{"code": "document_not_found", "message": "..."}`) instead of raised
exceptions.

### Transactional Email
| tool                        | REST endpoint                                |
|-----------------------------|----------------------------------------------|
| `sendTransacEmail`          | POST /smtp/email                             |
| `getTransacEmailsList`      | GET  /smtp/emails                            |
| `getTransacEmailContent`    | GET  /smtp/emails/{uuid}                     |
| `getTransacBlockedContacts` | GET  /smtp/blockedContacts                   |
| `getSmtpReport`             | GET  /smtp/statistics/reports                |

### Templates
| tool                  | REST endpoint                                |
|-----------------------|----------------------------------------------|
| `getSmtpTemplates`    | GET  /smtp/templates                         |
| `getSmtpTemplate`     | GET  /smtp/templates/{templateId}            |
| `createSmtpTemplate`  | POST /smtp/templates                         |
| `updateSmtpTemplate`  | PUT  /smtp/templates/{templateId}            |
| `deleteSmtpTemplate`  | DELETE /smtp/templates/{templateId}          |
| `sendTestTemplate`    | POST /smtp/templates/{templateId}/sendTest   |

### Contacts
| tool             | REST endpoint                       |
|------------------|-------------------------------------|
| `getContacts`    | GET  /contacts                      |
| `getContactInfo` | GET  /contacts/{identifier}         |
| `createContact`  | POST /contacts                      |
| `updateContact`  | PUT  /contacts/{identifier}         |
| `deleteContact`  | DELETE /contacts/{identifier}       |

### Lists
| tool                      | REST endpoint                                              |
|---------------------------|------------------------------------------------------------|
| `getLists`                | GET  /contacts/lists                                       |
| `getList`                 | GET  /contacts/lists/{listId}                              |
| `createList`              | POST /contacts/lists                                       |
| `updateList`              | PUT  /contacts/lists/{listId}                              |
| `deleteList`              | DELETE /contacts/lists/{listId}                            |
| `getContactsFromList`     | GET  /contacts/lists/{listId}/contacts                     |
| `addContactToList`        | POST /contacts/lists/{listId}/contacts/add                 |
| `removeContactFromList`   | POST /contacts/lists/{listId}/contacts/remove              |

### Senders
| tool            | REST endpoint     |
|-----------------|-------------------|
| `getSenders`    | GET  /senders     |
| `createSender`  | POST /senders     |

### SMS
| tool                     | REST endpoint                                  |
|--------------------------|------------------------------------------------|
| `sendTransacSms`         | POST /transactionalSMS/sms                     |
| `getTransacSmsActivity`  | GET  /transactionalSMS/statistics/events       |
| `getSmsCampaigns`        | GET  /smsCampaigns                             |

### Mock-only helpers (not part of the real Brevo surface)

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed` — bulk-insert Brevo-shaped objects (contacts, lists,
  templates, senders, transactional emails, blocked contacts, SMTP events,
  SMS messages, SMS campaigns) bypassing validation, for fixture seeding.
  Pass `replace=True` to reset state first.

## State

A single JSON file at `$BREVO_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/brevo_mock`). Layout:

```jsonc
{
  "account":          {"email","firstName","lastName","companyName","plan":[...]},
  "contacts":         {"<id>": {"id","email","emailBlacklisted","smsBlacklisted",
                                "createdAt","modifiedAt","listIds":[...],
                                "attributes":{...}}},
  "contacts_by_email": {"<email>": <id>},
  "lists":            {"<id>": {"id","name","folderId","totalSubscribers",
                                "totalBlacklisted","uniqueSubscribers","createdAt"}},
  "folders":          {"<id>": {"id","name",...}},
  "templates":        {"<id>": {"id","name","subject","isActive","testSent",
                                "sender","replyTo","toField","tag","htmlContent",
                                "createdAt","modifiedAt"}},
  "senders":          {"<id>": {"id","name","email","active","ips":[...]}},
  "transac_emails":   [{"messageId","to","subject","htmlContent",
                        "templateId","date","status",...}],
  "blocked_contacts": [{"email","reason","senderEmail","blockedAt"}],
  "smtp_events":      [{"date","email","event","messageId","tag","templateId"}],
  "sms_messages":     [{"messageId","recipient","content","smsCount",
                        "status","date","tag"}],
  "sms_campaigns":    {"<id>": {"id","name","status","content","scheduledAt",...}},
  "next_id":          {"contact":N,"list":N,"template":N,"sender":N,
                       "sms_campaign":N,"message_seq":N,"folder":N},
  "calls":            [{"op":"...","ts":"...",...}]
}
```

The `calls` log is what the verifier consumes — every tool (reads
included) appends an entry. File-locking via `fcntl.flock` makes
concurrent calls safe; per-rollout isolation should reset the state
dir between rollouts.

Seed a starting state by setting `BREVO_MOCK_SEED_PATH` to a JSON file
in the same shape — it is loaded once if no `state.json` exists.

## ID formats (matches real Brevo)

- **contact / list / template / sender id**: positive integers.
- **transactional email `messageId`**:
  `"<YYYYMMDDHHMM.NNNNNNNNNNNNNN@smtp-relay.mailin.fr>"`.
- **transactional email `uuid`** (used by `getTransacEmailContent`):
  md5 of the messageId (mock-only convenience; real API uses its own
  uuid).
- **SMS `messageId`**: 64-bit integer (10_000_000_000 + sequence) —
  Brevo SMS uses integers rather than the angle-bracket string format.

## Behavior notes / known mock-vs-real gaps

- Authentication / `api-key` headers are not modeled.
- Email verification flow for new senders is collapsed — `createSender`
  marks the sender `active=true` immediately.
- Template handlebars (`{{ params.NAME }}`) substitution is **not**
  rendered: `sendTransacEmail` records the raw `htmlContent` and the
  `params` payload, but does not interpolate.
- `getSmtpReport` bucketing is per-calendar-day in UTC; `aggregations`
  field of the real API is not produced.
- DOI (double-opt-in) flows are not implemented.
- Webhooks and inbound email parsing are not implemented.
- SMS credit accounting uses a constant 0.045 credits/segment — useful
  for relative comparisons only.

## Env

| var                     | default                      | purpose                          |
|-------------------------|------------------------------|----------------------------------|
| `BREVO_MOCK_STATE_DIR`  | `~/.openclaw/brevo_mock`     | state.json directory             |
| `BREVO_MOCK_SEED_PATH`  | unset                        | preload state.json on first start |

The Dockerfile sets `BREVO_MOCK_STATE_DIR=/workspace/output/end_state/brevo`
to match the openclaw rollout layout.

## Run

```bash
# local
BREVO_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  brevo-mock:
    build:
      context: ../../mcp_servers/brevo-mock
      dockerfile: Dockerfile
    image: mcp-env/brevo-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      BREVO_MOCK_STATE_DIR: /workspace/output/end_state/brevo
      BREVO_MOCK_SEED_PATH: /workspace/input/brevo_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
