# bluesky-mock

Mock MCP server mirroring the **Bluesky / AT Protocol** XRPC surface
(api.bsky.app + bsky.social). Bluesky is a Tier-A integration: real
PDS instances don't parallelize for RL rollouts, so this server runs a
deterministic in-memory mock that returns the same JSON shapes as the
real AppView.

Tool names are the **dot-namespaced XRPC method names verbatim**
(e.g. `app.bsky.feed.getTimeline`). This matches the AT Protocol
lexicon convention; nothing here pretends to be a CLI wrapper.

## Implemented tools (29 + 2 mock helpers)

| Namespace                      | XRPC method (= MCP tool name)               |
|--------------------------------|---------------------------------------------|
| `com.atproto.server`           | `com.atproto.server.createSession`          |
|                                | `com.atproto.server.refreshSession`         |
|                                | `com.atproto.server.getSession`             |
|                                | `com.atproto.server.deleteSession`          |
| `app.bsky.actor`               | `app.bsky.actor.getProfile`                 |
|                                | `app.bsky.actor.getProfiles`                |
|                                | `app.bsky.actor.searchActors`               |
|                                | `app.bsky.actor.getPreferences`             |
| `app.bsky.feed` (reads)        | `app.bsky.feed.getTimeline`                 |
|                                | `app.bsky.feed.getAuthorFeed`               |
|                                | `app.bsky.feed.getPostThread`               |
|                                | `app.bsky.feed.getPosts`                    |
|                                | `app.bsky.feed.getLikes`                    |
|                                | `app.bsky.feed.getRepostedBy`               |
|                                | `app.bsky.feed.searchPosts`                 |
| `app.bsky.feed` (writes)       | `app.bsky.feed.post`                        |
|                                | `app.bsky.feed.repost`                      |
|                                | `app.bsky.feed.like`                        |
|                                | `app.bsky.feed.deletePost`                  |
| `app.bsky.graph`               | `app.bsky.graph.getFollows`                 |
|                                | `app.bsky.graph.getFollowers`               |
|                                | `app.bsky.graph.follow`                     |
|                                | `app.bsky.graph.unfollow`                   |
|                                | `app.bsky.graph.mute`                       |
|                                | `app.bsky.graph.unmute`                     |
|                                | `app.bsky.graph.block`                      |
|                                | `app.bsky.graph.unblock`                    |
| `app.bsky.notification`        | `app.bsky.notification.listNotifications`   |
|                                | `app.bsky.notification.updateSeen`          |
|                                | `app.bsky.notification.getUnreadCount`      |
| Mock-only                      | `mock_debug_state`, `mock_debug_seed`       |

Write tools that are implemented under the hood as
`com.atproto.repo.createRecord` (post, repost, like, follow, block)
are exposed under their familiar `app.bsky.*` names — this matches
how the official `@atproto/api` client wraps them.

## Identifier formats

The mock generates AT-Protocol-shaped identifiers throughout:

- **DIDs** — `did:plc:<24 hex chars>` (sha256-derived from a counter).
- **Handles** — `<name>.bsky.social` by default; arbitrary domains
  accepted.
- **AT URIs** — `at://<did>/<collection>/<rkey>`, where collection is
  one of `app.bsky.feed.post`, `app.bsky.feed.like`,
  `app.bsky.feed.repost`, `app.bsky.graph.follow`, or
  `app.bsky.graph.block`.
- **rkeys** — `3` + 12 random base32 chars (TID-shaped).
- **CIDs** — `bafyrei` + sha256 prefix of the record payload.

Records returned in postViews use the real lexicon field names:
`text`, `createdAt`, `langs`, `facets`, `reply.{root,parent}`, `embed`,
`labels`, `tags`. PostViews include `replyCount`, `repostCount`,
`likeCount`, `quoteCount`, `indexedAt`, and `viewer.{like, repost,
threadMuted, ...}`. Feed view items wrap `{post, reply?, reason?}`
where `reason` is a `#reasonRepost` for boosted items.

## Responses & errors

- Successful XRPC calls return **plain JSON** matching the lexicon
  output schema (e.g. `getTimeline` returns `{feed, cursor}`).
- Failures are returned (not raised) as AT Protocol XRPC error bodies:
  `{"error": "InvalidRequest", "message": "..."}`. Standard names used:
  `AccountNotFound`, `ActorNotFound`, `AuthRequired`, `ExpiredToken`,
  `AuthMissing`, `Forbidden`, `InvalidRequest`, `NotFound`.

## State

A single JSON file at `$BLUESKY_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/bluesky_mock`). Layout:

```jsonc
{
  "service":       {"name":"...", "endpoint":"https://mock.bsky.social"},
  "self":          {"did":"did:plc:...", "handle":"...", "email":"..."},
  "session":       {"active":true, "accessJwt":"...", "refreshJwt":"...",
                    "did":"...", "handle":"..."},
  "actors":        {"<did>": { ...profileViewDetailed-shaped... }},
  "handles":       {"<handle>": "<did>"},
  "posts":         {"<at-uri>": {"uri","cid","author","record",
                                 "embed","replyCount","repostCount",
                                 "likeCount","quoteCount","indexedAt",
                                 "labels","deleted"}},
  "follows":       {"<at-uri>": {"author","subject","createdAt"}},
  "likes":         {"<at-uri>": {"author","subject":{"uri","cid"},
                                 "createdAt"}},
  "reposts":       {"<at-uri>": {"author","subject":{"uri","cid"},
                                 "createdAt"}},
  "blocks":        {"<at-uri>": {"author","subject","createdAt"}},
  "mutes":         [{"actor_did","target_did","createdAt"}],
  "notifications": [{"id","recipient","uri","cid","author","reason",
                     "reasonSubject","isRead","indexedAt"}],
  "seen_at":       "1970-01-01T00:00:00.000Z",
  "preferences":   [ ... AT Proto preference items ... ],
  "next_id":       {"actor":N,"post":N,"follow":N,"like":N,...},
  "calls":         [{"op":"...","ts":"...",...}]
}
```

Set `BLUESKY_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist (per-rollout
isolation should clear the state dir between rollouts). Per-task
fixtures are typically loaded via the `mock_debug_seed` tool instead.

Every tool call (including reads) appends an entry to `state.calls`
so verifiers can replay the trace.

## Behavior notes / known mock-vs-real gaps

- **Auth is not enforced.** `createSession` accepts any password, and
  unknown handles are auto-created on login. The session DID drives
  ownership checks on writes (you can only delete your own posts /
  unfollow your own follow records).
- `getTimeline` returns posts authored by the session DID + followed
  DIDs + their reposts, sorted by `indexedAt` desc. No personalized
  ranking / "discover" algorithm.
- `searchPosts` is a case-insensitive substring match on
  `record.text`. The optional `mentions`, `lang`, `tag`, `since`,
  `until`, `domain`, and `url` filters are honored; `top` vs `latest`
  sort selects between `likeCount`-then-time and time-only ordering.
- `getPostThread`'s `depth`/`parentHeight` are clamped to `[0, 1000]`.
- `cursor` for feed reads is a plain integer offset string (the AT
  Proto spec only requires that cursors round-trip; clients don't
  parse them).
- `app.bsky.feed.like` / `app.bsky.feed.repost` / `app.bsky.graph.follow`
  / `app.bsky.graph.block` are **idempotent**: re-creating the same
  edge returns the existing record URI rather than a duplicate.
- Blocking auto-removes follow records in both directions.
- Notifications are pushed for: like, repost, follow, reply. Mentions
  and quotes are stored in posts but the mock does not auto-parse
  facets to push mention/quote notifications (use `mock_debug_seed`
  notifications= to add them explicitly).
- Email/2FA/`authFactorToken`, app passwords, repo blob uploads,
  feed generators, lists, starterpacks, labelers, video uploads, and
  service auth are **not** modeled.

## Env

| var                       | default                          | purpose                          |
|---------------------------|----------------------------------|----------------------------------|
| `BLUESKY_MOCK_STATE_DIR`  | `~/.openclaw/bluesky_mock`       | state.json directory             |
| `BLUESKY_MOCK_SEED_PATH`  | unset                            | preload state.json on first start |

The Dockerfile sets
`BLUESKY_MOCK_STATE_DIR=/workspace/output/end_state/bluesky` to match
the openclaw rollout layout.

## Reference

- AT Protocol lexicons: https://atproto.com/specs/lexicon
- Bluesky API docs: https://docs.bsky.app/docs/api
- AppView XRPC methods: https://docs.bsky.app/docs/category/http-reference
