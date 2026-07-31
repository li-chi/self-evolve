# pagerduty-mock

PagerDuty REST API v2-shaped mock MCP server. Tools mirror real
PagerDuty operation IDs (`list_incidents`, `manage_incidents`,
`list_on_calls`, ...) and return PD-shaped JSON envelopes:

- Singletons: `{"incident": {...}}`, `{"service": {...}}`, etc.
- Lists: `{"incidents": [...], "limit": 25, "offset": 0, "total": null, "more": false}`
- Object references use the canonical PD shape:
  `{"id":"PT4KHLK","type":"service_reference","summary":"...","self":"...","html_url":"..."}`
- Errors: `{"error": {"message":"...","code":2100,"errors":[...]}}`

Reference: https://developer.pagerduty.com/api-reference/

## Tools (18 + 2 mock-only)

Incidents:
- `list_incidents`, `get_incident`, `create_incident`,
  `manage_incidents` (acknowledge/resolve via PUT /incidents),
  `create_incident_note`, `list_incident_alerts`

Services:
- `list_services`, `get_service`, `create_service`

Escalation policies:
- `list_escalation_policies`, `get_escalation_policy`

Users / current user:
- `list_users`, `get_user`, `get_current_user` (GET /users/me)

Teams:
- `list_teams`, `get_team`

Schedules / on-call:
- `list_schedules`, `get_schedule`, `list_on_calls` (who's on-call now)

Mock-only:
- `mock_debug_state` — dump full state for verifier introspection
- `mock_debug_seed` — load PagerDuty-ish fixture data

## State

State persists at `$PAGERDUTY_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/pagerduty_mock`). Per-rollout isolation should
clear the state dir between rollouts. If no state.json exists, the
server tries `$PAGERDUTY_MOCK_SEED_PATH` first.

Every tool call (including reads) appends to `state["calls"]` so
verifiers can replay the trace.

## ID format

PagerDuty uses 7-char uppercase alphanumeric IDs prefixed with `P`
(e.g. `PT4KHLK`). The mock generates ids deterministically from the
state sequence so seeded scenarios are reproducible.
