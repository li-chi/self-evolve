# strava-mock

Mock MCP server mirroring the public **Strava API v3** surface
(https://developers.strava.com/docs/reference/). Tool names match
the upstream `operationId` values, parameter names match the REST
spec, and response bodies use Strava's JSON shapes (long integer ids,
snake_case fields, athlete short representation embedded in activities,
etc.).

This is a Strava REST mock — **not** a wrapper around the
[`terminal-tool-use` strava-api CLI mock](../../../terminal-tool-use/mocks/strava-api/).
The two live side-by-side; the CLI mock predates this and uses a
different state shape and a small fixed subcommand set.

## Implemented operationIds (20 + 2 mock helpers)

| group     | operationId                              |
|-----------|------------------------------------------|
| Athletes  | `getLoggedInAthlete`                     |
|           | `getAthleteStats`                        |
|           | `getLoggedInAthleteActivities`           |
|           | `getAthleteZones`                        |
|           | `updateLoggedInAthlete`                  |
| Activities| `getActivityById`                        |
|           | `createActivity`                         |
|           | `updateActivityById`                     |
|           | `getCommentsByActivityId`                |
|           | `getKudoersByActivityId`                 |
|           | `getLapsByActivityId`                    |
|           | `getZonesByActivityId`                   |
| Clubs     | `getLoggedInAthleteClubs`                |
|           | `getClubById`                            |
|           | `getClubMembersById`                     |
|           | `getClubActivitiesById`                  |
| Routes    | `getRoutesByAthleteId`                   |
|           | `getRouteById`                           |
| Segments  | `getLoggedInAthleteStarredSegments`      |
|           | `getSegmentById`                         |
|           | `exploreSegments`                        |
| Mock-only | `mock_debug_state`, `mock_debug_seed`    |

## State

A single JSON file at `$STRAVA_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/strava_mock`). Layout:

```jsonc
{
  "self": {"id": 1000001, "username", "firstname", "lastname",
           "bio", "city", "state", "country", "sex",
           "premium", "created_at", "updated_at", "weight",
           "profile_medium", "profile", "resource_state": 3},
  "athletes":         {"<id>": <athlete>},
  "activities":       {"<id>": <activity>},
  "comments":         {"<activity_id>": [<comment>]},
  "kudoers":          {"<activity_id>": [<athlete_summary>]},
  "laps":             {"<activity_id>": [<lap>]},
  "activity_zones":   {"<activity_id>": [<zone_bucket>]},
  "athlete_zones":    {"heart_rate": {...}, "power": {...}},
  "athlete_stats":    {"<athlete_id>": <stats>},
  "clubs":            {"<id>": <club>},
  "club_members":     {"<club_id>": [<athlete_summary>]},
  "club_activities":  {"<club_id>": [<club_activity>]},
  "routes":           {"<id>": <route>},
  "segments":         {"<id>": <segment>},
  "starred_segments": [<segment_id>],
  "next_id":          {"athlete":N,"activity":N,"club":N,
                       "route":N,"segment":N,"comment":N,"lap":N},
  "calls":            [{"op":"...","ts":"...",...}]
}
```

Set `STRAVA_MOCK_SEED_PATH` to a JSON file in the same shape and the
server preloads from it on first start (only when `state.json` does
not yet exist). Per-task fixtures are usually loaded via the
`mock_debug_seed` tool instead.

## Error shape

Errors return Strava-style JSON dicts (no exceptions raised), e.g.

```json
{
  "message": "Authorization Error",
  "errors": [{"resource":"Athlete","field":"access_token","code":"invalid"}]
}
```

## Behavior notes / known mock-vs-real gaps

- Pagination follows Strava's 1-indexed `page` / `per_page` (max 200);
  `getCommentsByActivityId` also accepts the newer cursor pair
  (`page_size` + `after_cursor`).
- `getLoggedInAthleteActivities` filters by `before` / `after` Unix
  epoch seconds (REST semantics) against `start_date`.
- `getAthleteStats` returns the default all-zero shape if the athlete
  has no stored stats; matches the real API for new athletes.
- `getAthleteStats` enforces that the queried athlete id matches
  `state["self"]["id"]` (real Strava only returns stats for the
  authenticated athlete).
- `updateLoggedInAthlete` only accepts `weight` (per the v3 spec).
- `createActivity` requires `name`, `sport_type`, `start_date_local`,
  `elapsed_time`. `type` is accepted as legacy alias for `sport_type`.
- `exploreSegments` takes `bounds` as the upstream comma-string
  (`sw_lat,sw_lng,ne_lat,ne_lng`); returns at most 10 matches.
- No OAuth scope / rate-limit / token-expiry modeling.

## Env

| var                     | default                       | purpose                          |
|-------------------------|-------------------------------|----------------------------------|
| `STRAVA_MOCK_STATE_DIR` | `~/.openclaw/strava_mock`     | state.json directory             |
| `STRAVA_MOCK_SEED_PATH` | unset                         | preload state.json on first start |

The Dockerfile sets `STRAVA_MOCK_STATE_DIR=/workspace/output/end_state/strava`
to match the openclaw rollout layout.
