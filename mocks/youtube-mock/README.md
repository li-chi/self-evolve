# youtube-mock

Mock MCP server mirroring the **YouTube Data API v3**
(developers.google.com/youtube/v3/docs). Tool names match the
official `{resource}.{method}` pairs flattened to
`{resource}_{method}` (e.g. `videos_list`, `commentThreads_insert`),
and request/response bodies follow YouTube's real JSON shapes.

This is **not** a wrapper around the `terminal-tool-use/mocks/youtube/`
CLI — it is a fresh implementation that imitates the REST surface
directly so verifier traces look like real YouTube API calls.

## Why a mock

Real YouTube usage costs quota and depends on a public account; both
make rollouts non-deterministic and rate-limited. The mock holds a
single JSON state file and serves every list/insert/update/delete
purely from that file.

## Tools (24 + 2 mock helpers)

| group         | tool                                                   |
|---------------|--------------------------------------------------------|
| Videos        | `videos_list`                                          |
|               | `videos_insert`                                        |
|               | `videos_update`                                        |
|               | `videos_delete`                                        |
|               | `videos_rate`                                          |
|               | `videos_getRating`                                     |
| Channels      | `channels_list`                                        |
| Playlists     | `playlists_list`                                       |
|               | `playlists_insert`                                     |
|               | `playlists_update`                                     |
|               | `playlists_delete`                                     |
| PlaylistItems | `playlistItems_list`                                   |
|               | `playlistItems_insert`                                 |
|               | `playlistItems_update`                                 |
|               | `playlistItems_delete`                                 |
| Search        | `search_list`                                          |
| Comments      | `commentThreads_list`                                  |
|               | `commentThreads_insert`                                |
|               | `comments_list`                                        |
|               | `comments_insert`                                      |
| Subscriptions | `subscriptions_list`                                   |
|               | `subscriptions_insert`                                 |
|               | `subscriptions_delete`                                 |
| Captions      | `captions_list`                                        |
| Mock-only     | `mock_debug_state`, `mock_debug_seed`                  |

Parameters match the upstream Google API:
`part`, `id`, `mine`, `forUsername`, `forHandle`, `channelId`,
`playlistId`, `videoId`, `q`, `type`, `order`, `maxResults`,
`pageToken`, `regionCode`, etc.

## Response shape

All list endpoints return Google's standard list envelope:

```jsonc
{
  "kind": "youtube#videoListResponse",
  "etag": "...",
  "items": [
    {"kind": "youtube#video", "etag": "...", "id": "dQw4w9WgXcQ",
     "snippet": {"publishedAt", "channelId", "title", "description",
                 "thumbnails", "channelTitle", "tags", "categoryId",
                 "liveBroadcastContent", "defaultLanguage"},
     "contentDetails": {...},
     "statistics": {"viewCount", "likeCount", "commentCount", ...},
     "status": {"privacyStatus", "uploadStatus", ...}}
  ],
  "pageInfo": {"totalResults": 1, "resultsPerPage": 1},
  "nextPageToken": "..." // only when more results exist
}
```

Errors are returned as a Google error envelope (not raised):

```json
{"error": {"code": 404, "message": "Video not found: abc",
           "errors": [{"domain": "youtube.video",
                       "reason": "videoNotFound",
                       "message": "Video not found: abc"}],
           "status": "NOT_FOUND"}}
```

Insert/update/delete return either the new/updated resource or
`{}` (matching the real API's 204 on delete).

## ID formats

The mock uses YouTube's real id formats so seed/verifier code can be
copy-pasted from real-world data:

| Resource     | Format                                              |
|--------------|-----------------------------------------------------|
| video        | 11-char alphanumeric incl. `-_` (e.g. `dQw4w9WgXcQ`) |
| channel      | 24-char prefix `UC` (e.g. `UCuAXFkgsw1L7xaCfnd5JJOw`) |
| playlist     | prefix `PL` + 32 chars (e.g. `PLxxxxxxxxxxxxx...`)  |
| commentThread| prefix `UgC`                                        |
| comment      | `{threadId}.{suffix}`                               |

## State

A single JSON file at `$YOUTUBE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/youtube_mock`). Layout:

```jsonc
{
  "self":          {"channelId": "UC...", "username": "mockbot"},
  "channels":      {"<channelId>": {kind, etag, id, snippet, statistics, status, contentDetails}},
  "videos":        {"<videoId>":   {kind, etag, id, snippet, contentDetails, statistics, status}},
  "playlists":     {"<playlistId>":  {...}},
  "playlistItems": {"<playlistItemId>": {...}},
  "commentThreads":{"<threadId>": {snippet:{topLevelComment, totalReplyCount, ...}, replies}},
  "comments":      {"<commentId>": {...}},
  "subscriptions": {"<subscriptionId>": {snippet:{resourceId, subscriberChannelId, ...}}},
  "captions":      {"<captionId>": {snippet:{videoId, language, ...}}},
  "ratings":       {"<videoId>": "like"|"dislike"},
  "next_id":       {"playlistItem": N, "commentThread": N, "comment": N,
                    "subscription": N, "caption": N},
  "calls":         [{"op":"...", "ts":"...", ...}]
}
```

Set `YOUTUBE_MOCK_SEED_PATH` to a JSON file in the same shape; it
preloads state only when `state.json` does not yet exist. Per-task
fixtures are typically loaded via the `mock_debug_seed` tool instead.

## Behavior notes / mock-vs-real gaps

- **No quota accounting.** Every operation succeeds regardless of
  quota cost; the real API enforces a daily unit budget.
- **No uploads.** `videos_insert` creates a metadata-only video; the
  multipart file body is ignored.
- **No transcripts.** `captions_list` returns track metadata only.
  Use the data-model hint in `terminal-tool-use/mocks/youtube/youtube.py`
  for a transcript-aware variant.
- **Search is substring.** `search_list` ranks by title-substring +
  view count, not by YouTube's real relevance signal. Filters like
  `regionCode`/`videoCategoryId` are accepted but only loosely
  applied.
- **Date filters require ISO 8601.** `publishedAfter` /
  `publishedBefore` use string comparison on RFC 3339 timestamps.
- **Pagination tokens** are opaque base64(`offset:N`) strings. The
  real API uses different (also opaque) tokens — verifiers should
  round-trip them rather than parse.
- **OAuth/scopes not modeled.** Every tool is always callable; there
  is no separate `mine=true` quota or write permission check beyond
  basic argument validation.

## Env

| var                       | default                       | purpose                            |
|---------------------------|-------------------------------|------------------------------------|
| `YOUTUBE_MOCK_STATE_DIR`  | `~/.openclaw/youtube_mock`    | state.json directory                |
| `YOUTUBE_MOCK_SEED_PATH`  | unset                         | preload state.json on first start   |

The Dockerfile sets `YOUTUBE_MOCK_STATE_DIR=/workspace/output/end_state/youtube`
to match the openclaw rollout layout.
