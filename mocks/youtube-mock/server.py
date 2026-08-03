"""YouTube Data API v3 mock MCP server.

Mirrors the resource/method surface of the real YouTube Data API v3
(developers.google.com/youtube/v3/docs). Tool names match the
official `{resource}.{method}` pairs flattened to
`{resource}_{method}` (e.g. `videos_list`, `commentThreads_insert`).

Responses follow the real API's JSON envelopes:

    {
      "kind": "youtube#videoListResponse",
      "etag": "...",
      "items": [ {"kind": "youtube#video", "etag": "...",
                  "id": "...", "snippet": {...}, ...}, ... ],
      "pageInfo": {"totalResults": N, "resultsPerPage": M},
      "nextPageToken": "..."   # only when more results exist
    }

Errors are returned as Google-style error envelopes (not raised) so
the wire trace matches a real failed HTTP response:

    {"error": {"code": 404, "message": "Not Found",
               "errors": [{"reason":"videoNotFound", "message":"..."}]}}

State lives at `$YOUTUBE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/youtube_mock`). Per-rollout isolation should clear the
state dir between rollouts. `YOUTUBE_MOCK_SEED_PATH` preloads state
on first start if no state.json exists yet.

Every call (reads + writes) appends to `state["calls"]` so verifiers
can replay the trace.

Tool surface (24 + 2 mock helpers):

  Videos        videos_list, videos_insert, videos_update,
                videos_delete, videos_rate, videos_getRating
  Channels      channels_list
  Playlists     playlists_list, playlists_insert, playlists_update,
                playlists_delete
  PlaylistItems playlistItems_list, playlistItems_insert,
                playlistItems_update, playlistItems_delete
  Search        search_list
  Comments      commentThreads_list, commentThreads_insert,
                comments_list, comments_insert
  Subscriptions subscriptions_list, subscriptions_insert,
                subscriptions_delete
  Captions      captions_list
  Mock helpers  mock_debug_state, mock_debug_seed
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import random
import string
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "YOUTUBE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/youtube_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    """Seed state with a single self channel matching YouTube's
    `mine=true` semantics."""
    self_channel_id = "UC" + "MockSelf" + "0" * 14  # 24 chars total: UC + 22
    self_channel_id = self_channel_id[:24]
    return {
        "self": {
            "channelId": self_channel_id,
            "username": "mockbot",
        },
        "channels": {
            self_channel_id: {
                "kind": "youtube#channel",
                "etag": _etag("channel", self_channel_id),
                "id": self_channel_id,
                "snippet": {
                    "title": "Mock Channel",
                    "description": "The authenticated mock channel.",
                    "customUrl": "@mockbot",
                    "publishedAt": "2020-01-01T00:00:00Z",
                    "thumbnails": _default_thumbnails(),
                    "defaultLanguage": "en",
                    "country": "US",
                },
                "statistics": {
                    "viewCount": "0",
                    "subscriberCount": "0",
                    "hiddenSubscriberCount": False,
                    "videoCount": "0",
                },
                "contentDetails": {
                    "relatedPlaylists": {
                        "likes": "",
                        "uploads": "UU" + self_channel_id[2:],
                    },
                },
                "status": {
                    "privacyStatus": "public",
                    "isLinked": True,
                    "longUploadsStatus": "allowed",
                    "madeForKids": False,
                },
            },
        },
        "videos": {},          # videoId -> video resource
        "playlists": {},       # playlistId -> playlist resource
        "playlistItems": {},   # playlistItemId -> playlistItem resource
        "commentThreads": {},  # threadId -> commentThread resource
        "comments": {},        # commentId -> comment resource
        "subscriptions": {},   # subscriptionId -> subscription resource
        "captions": {},        # captionId -> caption resource
        "ratings": {},         # videoId -> "like"|"dislike"|"none"
        "next_id": {
            "playlistItem": 1,
            "commentThread": 1,
            "comment": 1,
            "subscription": 1,
            "caption": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("YOUTUBE_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must
    # not KeyError downstream - merge the skeleton's missing keys.
    for k, v in _empty_state().items():
        s.setdefault(k, v)
    return s


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    lock_path = _state_path() + ".lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kwargs) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# ID + etag helpers
# ---------------------------------------------------------------------------

_ALNUM = string.ascii_letters + string.digits + "-_"


def _hash_id(seed: str, length: int) -> str:
    """Deterministic-but-pseudo-random id from a seed. Used so re-
    seeding the same fixture produces the same ids."""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(h[:8], "big"))
    return "".join(rng.choice(_ALNUM) for _ in range(length))


def _new_video_id(state: dict) -> str:
    """11-char YouTube video id (alphanumeric + - _)."""
    while True:
        vid = "".join(random.choices(_ALNUM, k=11))
        if vid not in state["videos"]:
            return vid


def _new_channel_id(state: dict) -> str:
    """24-char channel id starting with 'UC'."""
    while True:
        cid = "UC" + "".join(random.choices(_ALNUM, k=22))
        if cid not in state["channels"]:
            return cid


def _new_playlist_id(state: dict) -> str:
    """Playlist id starting with 'PL'."""
    while True:
        pid = "PL" + "".join(random.choices(_ALNUM, k=32))
        if pid not in state["playlists"]:
            return pid


def _etag(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts) + _now_iso()
    return '"' + base64.urlsafe_b64encode(
        hashlib.sha1(raw.encode("utf-8")).digest()[:14]).decode("ascii") + '"'


def _default_thumbnails(video_id: str | None = None) -> dict:
    base = (f"https://i.ytimg.com/vi/{video_id}/"
            if video_id else "https://yt3.ggpht.com/")
    if video_id:
        return {
            "default": {"url": base + "default.jpg",
                        "width": 120, "height": 90},
            "medium": {"url": base + "mqdefault.jpg",
                       "width": 320, "height": 180},
            "high": {"url": base + "hqdefault.jpg",
                     "width": 480, "height": 360},
        }
    return {
        "default": {"url": base + "default.jpg",
                    "width": 88, "height": 88},
        "medium": {"url": base + "medium.jpg",
                   "width": 240, "height": 240},
        "high": {"url": base + "high.jpg",
                 "width": 800, "height": 800},
    }


# ---------------------------------------------------------------------------
# Error helpers (Google-style error envelope)
# ---------------------------------------------------------------------------

def _err(code: int, reason: str, message: str,
         domain: str = "youtube.common") -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "errors": [{
                "domain": domain,
                "reason": reason,
                "message": message,
            }],
            "status": _status_for(code),
        }
    }


def _status_for(code: int) -> str:
    return {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        409: "ALREADY_EXISTS",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
    }.get(code, "UNKNOWN")


# ---------------------------------------------------------------------------
# Part / pagination helpers
# ---------------------------------------------------------------------------

def _parse_parts(part: str | None) -> set[str]:
    if not part:
        return {"snippet"}
    return {p.strip() for p in part.split(",") if p.strip()}


def _project(resource: dict, parts: set[str], default_keep: list[str]) -> dict:
    """Strip a resource to only kind/etag/id + requested `parts`."""
    out = {k: resource[k] for k in default_keep if k in resource}
    for p in parts:
        if p in resource:
            out[p] = resource[p]
    return out


def _encode_token(offset: int) -> str:
    if offset <= 0:
        return ""
    return base64.urlsafe_b64encode(
        f"offset:{offset}".encode("ascii")).decode("ascii").rstrip("=")


def _decode_token(token: str | None) -> int:
    if not token:
        return 0
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode("ascii")
        if raw.startswith("offset:"):
            return int(raw.split(":", 1)[1])
    except (ValueError, UnicodeDecodeError):
        return 0
    return 0


def _paginate(items: list, page_token: str | None,
              max_results: int) -> tuple[list, str, str, int]:
    """Returns (page, next_token, prev_token, total)."""
    total = len(items)
    if max_results <= 0:
        max_results = 5
    if max_results > 50:
        max_results = 50
    start = max(_decode_token(page_token), 0)
    end = start + max_results
    page = items[start:end]
    next_tok = _encode_token(end) if end < total else ""
    prev_tok = _encode_token(max(start - max_results, 0)) if start > 0 else ""
    return page, next_tok, prev_tok, total


def _list_envelope(kind: str, items: list, page_token: str | None,
                   max_results: int) -> dict:
    page, next_tok, prev_tok, total = _paginate(
        items, page_token, max_results)
    out: dict[str, Any] = {
        "kind": kind,
        "etag": _etag(kind, len(page), total),
        "items": page,
        "pageInfo": {"totalResults": total,
                     "resultsPerPage": len(page)},
    }
    if next_tok:
        out["nextPageToken"] = next_tok
    if prev_tok:
        out["prevPageToken"] = prev_tok
    return out


# ---------------------------------------------------------------------------
# Resource constructors
# ---------------------------------------------------------------------------

def _ensure_video_defaults(v: dict) -> dict:
    """Fill in YouTube-shaped defaults on a video resource so list/get
    responses always include the standard fields."""
    vid = v["id"]
    snippet = v.setdefault("snippet", {})
    snippet.setdefault("publishedAt", _now_iso())
    snippet.setdefault("channelId", "")
    snippet.setdefault("title", "")
    snippet.setdefault("description", "")
    snippet.setdefault("thumbnails", _default_thumbnails(vid))
    snippet.setdefault("channelTitle", "")
    snippet.setdefault("tags", [])
    snippet.setdefault("categoryId", "22")
    snippet.setdefault("liveBroadcastContent", "none")
    snippet.setdefault("defaultLanguage", "en")
    snippet.setdefault("defaultAudioLanguage", "en")
    cd = v.setdefault("contentDetails", {})
    cd.setdefault("duration", "PT0S")
    cd.setdefault("dimension", "2d")
    cd.setdefault("definition", "hd")
    cd.setdefault("caption", "false")
    cd.setdefault("licensedContent", False)
    cd.setdefault("projection", "rectangular")
    stats = v.setdefault("statistics", {})
    stats.setdefault("viewCount", "0")
    stats.setdefault("likeCount", "0")
    stats.setdefault("favoriteCount", "0")
    stats.setdefault("commentCount", "0")
    status = v.setdefault("status", {})
    status.setdefault("uploadStatus", "processed")
    status.setdefault("privacyStatus", "public")
    status.setdefault("license", "youtube")
    status.setdefault("embeddable", True)
    status.setdefault("publicStatsViewable", True)
    status.setdefault("madeForKids", False)
    v.setdefault("kind", "youtube#video")
    v.setdefault("etag", _etag("video", vid))
    return v


def _ensure_channel_defaults(c: dict) -> dict:
    cid = c["id"]
    snippet = c.setdefault("snippet", {})
    snippet.setdefault("title", "")
    snippet.setdefault("description", "")
    snippet.setdefault("publishedAt", _now_iso())
    snippet.setdefault("thumbnails", _default_thumbnails())
    snippet.setdefault("country", "US")
    c.setdefault("statistics", {
        "viewCount": "0", "subscriberCount": "0",
        "hiddenSubscriberCount": False, "videoCount": "0",
    })
    c.setdefault("contentDetails", {
        "relatedPlaylists": {"uploads": "UU" + cid[2:], "likes": ""},
    })
    c.setdefault("status", {"privacyStatus": "public",
                            "isLinked": True,
                            "madeForKids": False})
    c.setdefault("kind", "youtube#channel")
    c.setdefault("etag", _etag("channel", cid))
    return c


def _ensure_playlist_defaults(p: dict) -> dict:
    pid = p["id"]
    snippet = p.setdefault("snippet", {})
    snippet.setdefault("publishedAt", _now_iso())
    snippet.setdefault("channelId", "")
    snippet.setdefault("title", "")
    snippet.setdefault("description", "")
    snippet.setdefault("thumbnails", _default_thumbnails())
    snippet.setdefault("channelTitle", "")
    snippet.setdefault("defaultLanguage", "en")
    p.setdefault("status", {"privacyStatus": "public"})
    p.setdefault("contentDetails", {"itemCount": 0})
    p.setdefault("kind", "youtube#playlist")
    p.setdefault("etag", _etag("playlist", pid))
    return p


def _make_playlist_item(state: dict, playlist_id: str,
                        resource_id: dict,
                        position: int,
                        note: str = "") -> dict:
    pli_id = _hash_id(
        f"pli:{playlist_id}:{resource_id.get('videoId')}:"
        f"{state['next_id']['playlistItem']}", 26)
    state["next_id"]["playlistItem"] += 1
    pl = state["playlists"].get(playlist_id, {})
    channel_id = pl.get("snippet", {}).get("channelId", "")
    vid = resource_id.get("videoId")
    video = state["videos"].get(vid, {})
    vsnip = video.get("snippet", {})
    item = {
        "kind": "youtube#playlistItem",
        "etag": _etag("playlistItem", pli_id),
        "id": pli_id,
        "snippet": {
            "publishedAt": _now_iso(),
            "channelId": channel_id,
            "title": vsnip.get("title", ""),
            "description": vsnip.get("description", ""),
            "thumbnails": vsnip.get("thumbnails",
                                    _default_thumbnails(vid)),
            "channelTitle": vsnip.get("channelTitle", ""),
            "playlistId": playlist_id,
            "position": position,
            "resourceId": {"kind": "youtube#video", "videoId": vid},
            "videoOwnerChannelTitle": vsnip.get("channelTitle", ""),
            "videoOwnerChannelId": vsnip.get("channelId", ""),
        },
        "contentDetails": {
            "videoId": vid,
            "videoPublishedAt": vsnip.get("publishedAt", _now_iso()),
            "note": note,
        },
        "status": {"privacyStatus": video.get("status", {}).get(
            "privacyStatus", "public")},
    }
    return item


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("youtube-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

@mcp.tool(name="videos_list")
def videos_list(part: str = "snippet",
                id: str = "",
                chart: str = "",
                myRating: str = "",
                maxResults: int = 5,
                pageToken: str = "",
                regionCode: str = "US",
                videoCategoryId: str = "") -> dict:
    """YouTube Data API v3: videos.list — retrieve a list of videos.

    Provide exactly one of `id` (comma-separated video ids),
    `chart` (`mostPopular`), or `myRating` (`like` | `dislike`).
    `part` is a comma-separated list of resource parts to include
    (`snippet,contentDetails,statistics,status`).
    """
    with _lock():
        s = _load_state()
        parts = _parse_parts(part)
        filters = sum(1 for f in (id, chart, myRating) if f)
        if filters != 1:
            _record(s, "videos_list", result="missing_filter",
                    id=id, chart=chart, myRating=myRating)
            _save_state(s)
            return _err(400, "missingRequiredParameter",
                        "Exactly one of `id`, `chart`, or `myRating` "
                        "must be specified.",
                        domain="youtube.parameter")
        items: list = []
        if id:
            ids = [x.strip() for x in id.split(",") if x.strip()]
            for vid in ids:
                v = s["videos"].get(vid)
                if not v:
                    continue
                items.append(_project(
                    _ensure_video_defaults(v), parts,
                    ["kind", "etag", "id"]))
        elif chart == "mostPopular":
            vids = list(s["videos"].values())
            if videoCategoryId:
                vids = [v for v in vids if v.get(
                    "snippet", {}).get("categoryId") == videoCategoryId]
            vids.sort(key=lambda v: int(v.get("statistics", {})
                                        .get("viewCount", "0")),
                      reverse=True)
            for v in vids:
                items.append(_project(
                    _ensure_video_defaults(v), parts,
                    ["kind", "etag", "id"]))
        elif myRating in ("like", "dislike"):
            for vid, rating in s.get("ratings", {}).items():
                if rating == myRating and vid in s["videos"]:
                    items.append(_project(
                        _ensure_video_defaults(s["videos"][vid]),
                        parts, ["kind", "etag", "id"]))
        else:
            _record(s, "videos_list", result="bad_chart_or_rating")
            _save_state(s)
            return _err(400, "invalidValue",
                        f"Unsupported chart/myRating: "
                        f"{chart or myRating}",
                        domain="youtube.parameter")
        _record(s, "videos_list", id=id, chart=chart, myRating=myRating,
                count=len(items))
        _save_state(s)
        return _list_envelope("youtube#videoListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="videos_insert")
def videos_insert(part: str = "snippet,status",
                  snippet: dict | None = None,
                  status: dict | None = None,
                  contentDetails: dict | None = None) -> dict:
    """YouTube Data API v3: videos.insert — upload a new video.

    `snippet` must contain at least `title`. `status.privacyStatus`
    defaults to `private`. Returns the newly-created video resource
    (mock: no real upload, the file payload is ignored).
    """
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        status = dict(status or {})
        if not snippet.get("title"):
            _record(s, "videos_insert", result="missing_title")
            _save_state(s)
            return _err(400, "invalidVideoMetadata",
                        "snippet.title is required.",
                        domain="youtube.video")
        vid = _new_video_id(s)
        me = s["self"]["channelId"]
        snippet.setdefault("channelId", me)
        snippet.setdefault("channelTitle",
                           s["channels"].get(me, {}).get(
                               "snippet", {}).get("title", ""))
        snippet.setdefault("publishedAt", _now_iso())
        snippet.setdefault("categoryId", "22")
        snippet.setdefault("liveBroadcastContent", "none")
        status.setdefault("privacyStatus", "private")
        status.setdefault("uploadStatus", "uploaded")
        status.setdefault("license", "youtube")
        status.setdefault("embeddable", True)
        status.setdefault("publicStatsViewable", True)
        status.setdefault("madeForKids", False)
        v = {
            "kind": "youtube#video",
            "etag": _etag("video", vid),
            "id": vid,
            "snippet": snippet,
            "status": status,
            "contentDetails": dict(contentDetails or {}),
            "statistics": {"viewCount": "0", "likeCount": "0",
                           "favoriteCount": "0", "commentCount": "0"},
        }
        _ensure_video_defaults(v)
        s["videos"][vid] = v
        # bump owner channel videoCount
        ch = s["channels"].get(snippet["channelId"])
        if ch is not None:
            try:
                ch["statistics"]["videoCount"] = str(int(
                    ch["statistics"].get("videoCount", "0")) + 1)
            except (KeyError, ValueError):
                pass
        _record(s, "videos_insert", videoId=vid,
                privacyStatus=status["privacyStatus"])
        _save_state(s)
        return _project(v, _parse_parts(part),
                        ["kind", "etag", "id"])


@mcp.tool(name="videos_update")
def videos_update(part: str = "snippet",
                  id: str = "",
                  snippet: dict | None = None,
                  status: dict | None = None,
                  contentDetails: dict | None = None) -> dict:
    """YouTube Data API v3: videos.update — update an existing
    video's metadata. The request body's `id` selects the target;
    `part` lists which resource parts are being supplied (and
    therefore overwritten)."""
    with _lock():
        s = _load_state()
        if not id:
            _record(s, "videos_update", result="missing_id")
            _save_state(s)
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        v = s["videos"].get(id)
        if not v:
            _record(s, "videos_update", id=id, result="not_found")
            _save_state(s)
            return _err(404, "videoNotFound",
                        f"Video not found: {id}",
                        domain="youtube.video")
        parts = _parse_parts(part)
        if "snippet" in parts and snippet is not None:
            v.setdefault("snippet", {}).update(snippet)
        if "status" in parts and status is not None:
            v.setdefault("status", {}).update(status)
        if "contentDetails" in parts and contentDetails is not None:
            v.setdefault("contentDetails", {}).update(contentDetails)
        v["etag"] = _etag("video", id)
        _ensure_video_defaults(v)
        _record(s, "videos_update", id=id,
                parts=sorted(parts))
        _save_state(s)
        return _project(v, parts, ["kind", "etag", "id"])


@mcp.tool(name="videos_delete")
def videos_delete(id: str) -> dict:
    """YouTube Data API v3: videos.delete — delete a video by id.
    Returns an empty dict on success (HTTP 204 in the real API)."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        v = s["videos"].pop(id, None)
        if not v:
            _record(s, "videos_delete", id=id, result="not_found")
            _save_state(s)
            return _err(404, "videoNotFound",
                        f"Video not found: {id}",
                        domain="youtube.video")
        s.get("ratings", {}).pop(id, None)
        ch = s["channels"].get(v.get("snippet", {}).get("channelId", ""))
        if ch is not None:
            try:
                cnt = int(ch["statistics"].get("videoCount", "0"))
                ch["statistics"]["videoCount"] = str(max(cnt - 1, 0))
            except (KeyError, ValueError):
                pass
        _record(s, "videos_delete", id=id)
        _save_state(s)
        return {}


@mcp.tool(name="videos_rate")
def videos_rate(id: str, rating: str) -> dict:
    """YouTube Data API v3: videos.rate — set the authenticated
    user's rating on a video. `rating` in {like, dislike, none}.
    Returns empty dict on success."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        if rating not in ("like", "dislike", "none"):
            return _err(400, "invalidRating",
                        f"Unsupported rating: {rating}",
                        domain="youtube.video")
        if id not in s["videos"]:
            _record(s, "videos_rate", id=id, result="not_found")
            _save_state(s)
            return _err(404, "videoNotFound",
                        f"Video not found: {id}",
                        domain="youtube.video")
        prev = s.setdefault("ratings", {}).get(id, "none")
        if rating == "none":
            s["ratings"].pop(id, None)
        else:
            s["ratings"][id] = rating
        # adjust statistics
        stats = s["videos"][id].setdefault("statistics", {})
        try:
            likes = int(stats.get("likeCount", "0"))
            if prev == "like" and rating != "like":
                likes -= 1
            if rating == "like" and prev != "like":
                likes += 1
            stats["likeCount"] = str(max(likes, 0))
        except ValueError:
            pass
        _record(s, "videos_rate", id=id, rating=rating, prev=prev)
        _save_state(s)
        return {}


@mcp.tool(name="videos_getRating")
def videos_getRating(id: str) -> dict:
    """YouTube Data API v3: videos.getRating — retrieve the
    authenticated user's ratings for one or more videos (comma-
    separated `id`)."""
    with _lock():
        s = _load_state()
        ids = [x.strip() for x in (id or "").split(",") if x.strip()]
        items = []
        for vid in ids:
            if vid not in s["videos"]:
                items.append({"videoId": vid, "rating": "none"})
                continue
            items.append({
                "videoId": vid,
                "rating": s.get("ratings", {}).get(vid, "none"),
            })
        _record(s, "videos_getRating", ids=ids)
        _save_state(s)
        return {
            "kind": "youtube#videoGetRatingResponse",
            "etag": _etag("getRating", id),
            "items": items,
        }


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@mcp.tool(name="channels_list")
def channels_list(part: str = "snippet",
                  id: str = "",
                  mine: bool = False,
                  forUsername: str = "",
                  forHandle: str = "",
                  managedByMe: bool = False,
                  maxResults: int = 5,
                  pageToken: str = "") -> dict:
    """YouTube Data API v3: channels.list — retrieve channel
    resources. Exactly one filter: `id`, `mine=true`, `forUsername`,
    or `forHandle`."""
    with _lock():
        s = _load_state()
        parts = _parse_parts(part)
        filters = sum(1 for f in (id, mine, forUsername, forHandle,
                                  managedByMe) if f)
        if filters != 1:
            _record(s, "channels_list", result="missing_filter",
                    id=id, mine=mine, forUsername=forUsername,
                    forHandle=forHandle)
            _save_state(s)
            return _err(400, "missingRequiredParameter",
                        "Exactly one of `id`, `mine`, `forUsername`, "
                        "`forHandle`, or `managedByMe` must be set.",
                        domain="youtube.parameter")
        items: list = []
        if id:
            for cid in [x.strip() for x in id.split(",") if x.strip()]:
                ch = s["channels"].get(cid)
                if ch:
                    items.append(_project(
                        _ensure_channel_defaults(ch), parts,
                        ["kind", "etag", "id"]))
        elif mine or managedByMe:
            me = s["self"]["channelId"]
            ch = s["channels"].get(me)
            if ch:
                items.append(_project(
                    _ensure_channel_defaults(ch), parts,
                    ["kind", "etag", "id"]))
        elif forUsername:
            for ch in s["channels"].values():
                if ch.get("snippet", {}).get("customUrl", "").lstrip("@") \
                        == forUsername.lstrip("@"):
                    items.append(_project(
                        _ensure_channel_defaults(ch), parts,
                        ["kind", "etag", "id"]))
                    break
        elif forHandle:
            handle = forHandle.lstrip("@")
            for ch in s["channels"].values():
                cu = ch.get("snippet", {}).get("customUrl", "").lstrip("@")
                if cu == handle:
                    items.append(_project(
                        _ensure_channel_defaults(ch), parts,
                        ["kind", "etag", "id"]))
                    break
        _record(s, "channels_list", id=id, mine=mine,
                forUsername=forUsername, forHandle=forHandle,
                count=len(items))
        _save_state(s)
        return _list_envelope("youtube#channelListResponse",
                              items, pageToken, maxResults)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

@mcp.tool(name="playlists_list")
def playlists_list(part: str = "snippet",
                   id: str = "",
                   channelId: str = "",
                   mine: bool = False,
                   maxResults: int = 5,
                   pageToken: str = "") -> dict:
    """YouTube Data API v3: playlists.list — retrieve playlists.
    Provide exactly one filter: `id`, `channelId`, or `mine`."""
    with _lock():
        s = _load_state()
        parts = _parse_parts(part)
        filters = sum(1 for f in (id, channelId, mine) if f)
        if filters != 1:
            _record(s, "playlists_list", result="missing_filter")
            _save_state(s)
            return _err(400, "missingRequiredParameter",
                        "Exactly one of `id`, `channelId`, or `mine` "
                        "must be set.",
                        domain="youtube.parameter")
        items: list = []
        if id:
            for pid in [x.strip() for x in id.split(",") if x.strip()]:
                pl = s["playlists"].get(pid)
                if pl:
                    items.append(_project(
                        _ensure_playlist_defaults(pl), parts,
                        ["kind", "etag", "id"]))
        else:
            target = (s["self"]["channelId"] if mine else channelId)
            for pl in s["playlists"].values():
                if pl.get("snippet", {}).get("channelId") == target:
                    items.append(_project(
                        _ensure_playlist_defaults(pl), parts,
                        ["kind", "etag", "id"]))
        items.sort(key=lambda p: p.get("snippet", {})
                   .get("publishedAt", ""), reverse=True)
        _record(s, "playlists_list", id=id, channelId=channelId,
                mine=mine, count=len(items))
        _save_state(s)
        return _list_envelope("youtube#playlistListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="playlists_insert")
def playlists_insert(part: str = "snippet,status",
                     snippet: dict | None = None,
                     status: dict | None = None) -> dict:
    """YouTube Data API v3: playlists.insert — create a new playlist."""
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        status = dict(status or {})
        if not snippet.get("title"):
            return _err(400, "invalidTitle",
                        "snippet.title is required.",
                        domain="youtube.playlist")
        pid = _new_playlist_id(s)
        me = s["self"]["channelId"]
        snippet.setdefault("channelId", me)
        snippet.setdefault("channelTitle",
                           s["channels"].get(me, {}).get(
                               "snippet", {}).get("title", ""))
        snippet.setdefault("publishedAt", _now_iso())
        status.setdefault("privacyStatus", "private")
        pl = {
            "kind": "youtube#playlist",
            "etag": _etag("playlist", pid),
            "id": pid,
            "snippet": snippet,
            "status": status,
            "contentDetails": {"itemCount": 0},
        }
        _ensure_playlist_defaults(pl)
        s["playlists"][pid] = pl
        _record(s, "playlists_insert", playlistId=pid)
        _save_state(s)
        return _project(pl, _parse_parts(part),
                        ["kind", "etag", "id"])


@mcp.tool(name="playlists_update")
def playlists_update(part: str = "snippet",
                     id: str = "",
                     snippet: dict | None = None,
                     status: dict | None = None) -> dict:
    """YouTube Data API v3: playlists.update — update an existing
    playlist's snippet/status."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        pl = s["playlists"].get(id)
        if not pl:
            _record(s, "playlists_update", id=id, result="not_found")
            _save_state(s)
            return _err(404, "playlistNotFound",
                        f"Playlist not found: {id}",
                        domain="youtube.playlist")
        parts = _parse_parts(part)
        if "snippet" in parts and snippet is not None:
            pl.setdefault("snippet", {}).update(snippet)
        if "status" in parts and status is not None:
            pl.setdefault("status", {}).update(status)
        pl["etag"] = _etag("playlist", id)
        _ensure_playlist_defaults(pl)
        _record(s, "playlists_update", id=id, parts=sorted(parts))
        _save_state(s)
        return _project(pl, parts, ["kind", "etag", "id"])


@mcp.tool(name="playlists_delete")
def playlists_delete(id: str) -> dict:
    """YouTube Data API v3: playlists.delete — delete a playlist."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        if id not in s["playlists"]:
            _record(s, "playlists_delete", id=id, result="not_found")
            _save_state(s)
            return _err(404, "playlistNotFound",
                        f"Playlist not found: {id}",
                        domain="youtube.playlist")
        del s["playlists"][id]
        # cascade delete playlist items
        to_remove = [pli_id for pli_id, pli in s["playlistItems"].items()
                     if pli.get("snippet", {}).get("playlistId") == id]
        for pli_id in to_remove:
            del s["playlistItems"][pli_id]
        _record(s, "playlists_delete", id=id, items_removed=len(to_remove))
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# PlaylistItems
# ---------------------------------------------------------------------------

@mcp.tool(name="playlistItems_list")
def playlistItems_list(part: str = "snippet",
                       id: str = "",
                       playlistId: str = "",
                       videoId: str = "",
                       maxResults: int = 5,
                       pageToken: str = "") -> dict:
    """YouTube Data API v3: playlistItems.list — retrieve playlist
    items. Provide one of `id` or `playlistId`; `videoId` filters
    within a playlist."""
    with _lock():
        s = _load_state()
        parts = _parse_parts(part)
        if not (id or playlistId):
            _record(s, "playlistItems_list", result="missing_filter")
            _save_state(s)
            return _err(400, "missingRequiredParameter",
                        "Either `id` or `playlistId` is required.",
                        domain="youtube.parameter")
        items: list = []
        if id:
            for pli_id in [x.strip() for x in id.split(",")
                           if x.strip()]:
                pli = s["playlistItems"].get(pli_id)
                if pli:
                    items.append(_project(
                        pli, parts, ["kind", "etag", "id"]))
        else:
            if playlistId not in s["playlists"]:
                _record(s, "playlistItems_list",
                        playlistId=playlistId, result="not_found")
                _save_state(s)
                return _err(404, "playlistNotFound",
                            f"Playlist not found: {playlistId}",
                            domain="youtube.playlist")
            pli_list = [pli for pli in s["playlistItems"].values()
                        if pli.get("snippet", {}).get(
                            "playlistId") == playlistId]
            if videoId:
                pli_list = [pli for pli in pli_list
                            if pli.get("contentDetails", {}).get(
                                "videoId") == videoId]
            pli_list.sort(key=lambda p: p.get("snippet", {})
                          .get("position", 0))
            for pli in pli_list:
                items.append(_project(pli, parts,
                                      ["kind", "etag", "id"]))
        _record(s, "playlistItems_list", id=id, playlistId=playlistId,
                videoId=videoId, count=len(items))
        _save_state(s)
        return _list_envelope("youtube#playlistItemListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="playlistItems_insert")
def playlistItems_insert(part: str = "snippet",
                         snippet: dict | None = None,
                         contentDetails: dict | None = None) -> dict:
    """YouTube Data API v3: playlistItems.insert — add a video to
    a playlist. Body shape:
        snippet={playlistId, resourceId={kind:"youtube#video",
                                        videoId}, position?}"""
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        playlist_id = snippet.get("playlistId")
        resource_id = snippet.get("resourceId") or {}
        vid = resource_id.get("videoId")
        if not playlist_id:
            return _err(400, "missingRequiredParameter",
                        "snippet.playlistId is required.",
                        domain="youtube.parameter")
        if not vid:
            return _err(400, "missingRequiredParameter",
                        "snippet.resourceId.videoId is required.",
                        domain="youtube.parameter")
        if playlist_id not in s["playlists"]:
            return _err(404, "playlistNotFound",
                        f"Playlist not found: {playlist_id}",
                        domain="youtube.playlist")
        if vid not in s["videos"]:
            return _err(404, "videoNotFound",
                        f"Video not found: {vid}",
                        domain="youtube.video")
        existing = [p for p in s["playlistItems"].values()
                    if p.get("snippet", {}).get(
                        "playlistId") == playlist_id]
        position = snippet.get("position")
        if position is None:
            position = len(existing)
        else:
            position = int(position)
            # shift items at or after position
            for p in existing:
                if p["snippet"]["position"] >= position:
                    p["snippet"]["position"] += 1
        note = (contentDetails or {}).get("note", "")
        pli = _make_playlist_item(s, playlist_id, resource_id,
                                  position, note)
        s["playlistItems"][pli["id"]] = pli
        s["playlists"][playlist_id]["contentDetails"]["itemCount"] = (
            len(existing) + 1)
        _record(s, "playlistItems_insert",
                playlistId=playlist_id, videoId=vid,
                playlistItemId=pli["id"], position=position)
        _save_state(s)
        return _project(pli, _parse_parts(part),
                        ["kind", "etag", "id"])


@mcp.tool(name="playlistItems_update")
def playlistItems_update(part: str = "snippet",
                         id: str = "",
                         snippet: dict | None = None,
                         contentDetails: dict | None = None) -> dict:
    """YouTube Data API v3: playlistItems.update — update a
    playlist item (typically `position` or `contentDetails.note`)."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        pli = s["playlistItems"].get(id)
        if not pli:
            _record(s, "playlistItems_update", id=id, result="not_found")
            _save_state(s)
            return _err(404, "playlistItemNotFound",
                        f"Playlist item not found: {id}",
                        domain="youtube.playlistItem")
        parts = _parse_parts(part)
        if "snippet" in parts and snippet is not None:
            new_pos = snippet.get("position")
            if new_pos is not None:
                pli["snippet"]["position"] = int(new_pos)
            for k in ("title", "description"):
                if k in snippet:
                    pli["snippet"][k] = snippet[k]
        if "contentDetails" in parts and contentDetails is not None:
            pli.setdefault("contentDetails", {}).update(contentDetails)
        pli["etag"] = _etag("playlistItem", id)
        _record(s, "playlistItems_update", id=id, parts=sorted(parts))
        _save_state(s)
        return _project(pli, parts, ["kind", "etag", "id"])


@mcp.tool(name="playlistItems_delete")
def playlistItems_delete(id: str) -> dict:
    """YouTube Data API v3: playlistItems.delete — remove a video
    from a playlist."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        pli = s["playlistItems"].pop(id, None)
        if not pli:
            _record(s, "playlistItems_delete", id=id, result="not_found")
            _save_state(s)
            return _err(404, "playlistItemNotFound",
                        f"Playlist item not found: {id}",
                        domain="youtube.playlistItem")
        playlist_id = pli.get("snippet", {}).get("playlistId")
        pl = s["playlists"].get(playlist_id)
        if pl is not None:
            siblings = [p for p in s["playlistItems"].values()
                        if p.get("snippet", {}).get(
                            "playlistId") == playlist_id]
            pl["contentDetails"]["itemCount"] = len(siblings)
            siblings.sort(key=lambda p: p["snippet"]["position"])
            for i, p in enumerate(siblings):
                p["snippet"]["position"] = i
        _record(s, "playlistItems_delete", id=id, playlistId=playlist_id)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool(name="search_list")
def search_list(part: str = "snippet",
                q: str = "",
                channelId: str = "",
                type: str = "video,channel,playlist",
                order: str = "relevance",
                maxResults: int = 5,
                pageToken: str = "",
                publishedAfter: str = "",
                publishedBefore: str = "",
                regionCode: str = "",
                videoCategoryId: str = "",
                forMine: bool = False) -> dict:
    """YouTube Data API v3: search.list — search videos, channels,
    and playlists. `q` is a free-text query (substring match
    against title/description in this mock). `type` is a
    comma-separated subset of {video, channel, playlist}.
    `order` in {relevance, date, viewCount, title}."""
    with _lock():
        s = _load_state()
        types = {t.strip() for t in (type or "").split(",")
                 if t.strip() in ("video", "channel", "playlist")}
        if not types:
            types = {"video", "channel", "playlist"}
        q_lower = (q or "").lower().strip()
        results: list = []

        def _hit(text_haystack: str) -> bool:
            if not q_lower:
                return True
            return q_lower in (text_haystack or "").lower()

        if "video" in types:
            for v in s["videos"].values():
                snip = v.get("snippet", {})
                if channelId and snip.get("channelId") != channelId:
                    continue
                if forMine and snip.get(
                        "channelId") != s["self"]["channelId"]:
                    continue
                if videoCategoryId and snip.get(
                        "categoryId") != videoCategoryId:
                    continue
                hay = (snip.get("title", "") + " "
                       + snip.get("description", "") + " "
                       + snip.get("channelTitle", ""))
                if not _hit(hay):
                    continue
                pub = snip.get("publishedAt", "")
                if publishedAfter and pub < publishedAfter:
                    continue
                if publishedBefore and pub > publishedBefore:
                    continue
                results.append({
                    "kind": "youtube#searchResult",
                    "etag": _etag("searchResult", v["id"]),
                    "id": {"kind": "youtube#video",
                           "videoId": v["id"]},
                    "snippet": {
                        "publishedAt": pub,
                        "channelId": snip.get("channelId", ""),
                        "title": snip.get("title", ""),
                        "description": snip.get("description", ""),
                        "thumbnails": snip.get(
                            "thumbnails", _default_thumbnails(v["id"])),
                        "channelTitle": snip.get("channelTitle", ""),
                        "liveBroadcastContent": snip.get(
                            "liveBroadcastContent", "none"),
                        "publishTime": pub,
                    },
                    "_view": int(v.get("statistics", {})
                                 .get("viewCount", "0") or "0"),
                })
        if "channel" in types and not forMine:
            for c in s["channels"].values():
                snip = c.get("snippet", {})
                hay = snip.get("title", "") + " " + snip.get(
                    "description", "")
                if channelId and c["id"] != channelId:
                    continue
                if not _hit(hay):
                    continue
                results.append({
                    "kind": "youtube#searchResult",
                    "etag": _etag("searchResult", c["id"]),
                    "id": {"kind": "youtube#channel",
                           "channelId": c["id"]},
                    "snippet": {
                        "publishedAt": snip.get("publishedAt", ""),
                        "channelId": c["id"],
                        "title": snip.get("title", ""),
                        "description": snip.get("description", ""),
                        "thumbnails": snip.get(
                            "thumbnails", _default_thumbnails()),
                        "channelTitle": snip.get("title", ""),
                        "liveBroadcastContent": "none",
                        "publishTime": snip.get("publishedAt", ""),
                    },
                    "_view": 0,
                })
        if "playlist" in types:
            for pl in s["playlists"].values():
                snip = pl.get("snippet", {})
                if channelId and snip.get("channelId") != channelId:
                    continue
                hay = snip.get("title", "") + " " + snip.get(
                    "description", "")
                if not _hit(hay):
                    continue
                results.append({
                    "kind": "youtube#searchResult",
                    "etag": _etag("searchResult", pl["id"]),
                    "id": {"kind": "youtube#playlist",
                           "playlistId": pl["id"]},
                    "snippet": {
                        "publishedAt": snip.get("publishedAt", ""),
                        "channelId": snip.get("channelId", ""),
                        "title": snip.get("title", ""),
                        "description": snip.get("description", ""),
                        "thumbnails": snip.get(
                            "thumbnails", _default_thumbnails()),
                        "channelTitle": snip.get("channelTitle", ""),
                        "liveBroadcastContent": "none",
                        "publishTime": snip.get("publishedAt", ""),
                    },
                    "_view": 0,
                })
        # ordering
        if order == "date":
            results.sort(key=lambda r: r["snippet"].get(
                "publishedAt", ""), reverse=True)
        elif order == "title":
            results.sort(key=lambda r: r["snippet"].get("title", ""))
        elif order == "viewCount":
            results.sort(key=lambda r: r.get("_view", 0), reverse=True)
        else:
            # relevance: rough — q matches in title rank higher
            def _rel(r):
                t = r["snippet"].get("title", "").lower()
                return (0 if q_lower and q_lower in t else 1,
                        -r.get("_view", 0))
            results.sort(key=_rel)
        for r in results:
            r.pop("_view", None)
        # part projection (search result snippet is small)
        parts = _parse_parts(part)
        if "snippet" not in parts:
            for r in results:
                r.pop("snippet", None)
        _record(s, "search_list", q=q, type=type, order=order,
                channelId=channelId, count=len(results))
        _save_state(s)
        out = _list_envelope("youtube#searchListResponse",
                             results, pageToken, maxResults)
        out["regionCode"] = regionCode or "US"
        return out


# ---------------------------------------------------------------------------
# Comment threads & comments
# ---------------------------------------------------------------------------

def _make_top_comment(comment_id: str, text: str, author_channel_id: str,
                      video_id: str | None,
                      parent_id: str | None = None) -> dict:
    return {
        "kind": "youtube#comment",
        "etag": _etag("comment", comment_id),
        "id": comment_id,
        "snippet": {
            "channelId": "",
            "videoId": video_id or "",
            "textDisplay": text,
            "textOriginal": text,
            "parentId": parent_id,
            "authorDisplayName": "Mock User",
            "authorProfileImageUrl": "",
            "authorChannelUrl": (f"https://www.youtube.com/channel/"
                                 f"{author_channel_id}"),
            "authorChannelId": {"value": author_channel_id},
            "canRate": True,
            "viewerRating": "none",
            "likeCount": 0,
            "publishedAt": _now_iso(),
            "updatedAt": _now_iso(),
        },
    }


@mcp.tool(name="commentThreads_list")
def commentThreads_list(part: str = "snippet",
                        id: str = "",
                        videoId: str = "",
                        channelId: str = "",
                        allThreadsRelatedToChannelId: str = "",
                        order: str = "time",
                        searchTerms: str = "",
                        maxResults: int = 20,
                        pageToken: str = "") -> dict:
    """YouTube Data API v3: commentThreads.list — list top-level
    comments. Exactly one filter: `id`, `videoId`, `channelId`,
    or `allThreadsRelatedToChannelId`."""
    with _lock():
        s = _load_state()
        filters = sum(1 for f in (id, videoId, channelId,
                                  allThreadsRelatedToChannelId) if f)
        if filters != 1:
            return _err(400, "missingRequiredParameter",
                        "Exactly one of `id`, `videoId`, `channelId`, "
                        "or `allThreadsRelatedToChannelId` is required.",
                        domain="youtube.parameter")
        parts = _parse_parts(part)
        threads: list = []
        if id:
            for tid in [x.strip() for x in id.split(",") if x.strip()]:
                t = s["commentThreads"].get(tid)
                if t:
                    threads.append(t)
        else:
            for t in s["commentThreads"].values():
                snip = t.get("snippet", {})
                if videoId and snip.get("videoId") != videoId:
                    continue
                if channelId and snip.get("channelId") != channelId:
                    continue
                if (allThreadsRelatedToChannelId
                        and snip.get("channelId")
                        != allThreadsRelatedToChannelId
                        and snip.get("videoOwnerChannelId")
                        != allThreadsRelatedToChannelId):
                    continue
                if searchTerms:
                    top_text = (snip.get("topLevelComment", {})
                                .get("snippet", {}).get("textOriginal", ""))
                    if searchTerms.lower() not in top_text.lower():
                        continue
                threads.append(t)
        if order == "relevance":
            threads.sort(key=lambda t: t.get("snippet", {})
                         .get("totalReplyCount", 0), reverse=True)
        else:
            threads.sort(key=lambda t: t.get("snippet", {})
                         .get("topLevelComment", {}).get("snippet", {})
                         .get("publishedAt", ""),
                         reverse=True)
        items = [_project(t, parts, ["kind", "etag", "id"])
                 for t in threads]
        _record(s, "commentThreads_list", videoId=videoId,
                channelId=channelId, count=len(items))
        _save_state(s)
        return _list_envelope("youtube#commentThreadListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="commentThreads_insert")
def commentThreads_insert(part: str = "snippet",
                          snippet: dict | None = None) -> dict:
    """YouTube Data API v3: commentThreads.insert — create a new
    top-level comment + thread. Body:
        snippet={videoId|channelId,
                 topLevelComment={snippet={textOriginal}}}"""
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        video_id = snippet.get("videoId")
        channel_id = snippet.get("channelId")
        top = (snippet.get("topLevelComment") or {}).get("snippet", {})
        text = top.get("textOriginal") or top.get("textDisplay")
        if not text:
            return _err(400, "commentTextRequired",
                        "topLevelComment.snippet.textOriginal required.",
                        domain="youtube.commentThread")
        if not (video_id or channel_id):
            return _err(400, "missingRequiredParameter",
                        "snippet.videoId or snippet.channelId is required.",
                        domain="youtube.parameter")
        if video_id and video_id not in s["videos"]:
            return _err(404, "videoNotFound",
                        f"Video not found: {video_id}",
                        domain="youtube.video")
        n = s["next_id"]["commentThread"]
        s["next_id"]["commentThread"] = n + 1
        tid = "UgC" + _hash_id(f"thread:{n}:{video_id or channel_id}", 24)
        cn = s["next_id"]["comment"]
        s["next_id"]["comment"] = cn + 1
        cid = tid + "." + _hash_id(f"comment:{cn}", 10)
        me = s["self"]["channelId"]
        top_comment = _make_top_comment(cid, text, me, video_id)
        if channel_id:
            top_comment["snippet"]["channelId"] = channel_id
        else:
            top_comment["snippet"]["channelId"] = s["videos"].get(
                video_id, {}).get("snippet", {}).get("channelId", "")
        thread = {
            "kind": "youtube#commentThread",
            "etag": _etag("commentThread", tid),
            "id": tid,
            "snippet": {
                "channelId": top_comment["snippet"]["channelId"],
                "videoId": video_id or "",
                "topLevelComment": top_comment,
                "canReply": True,
                "totalReplyCount": 0,
                "isPublic": True,
            },
            "replies": {"comments": []},
        }
        s["commentThreads"][tid] = thread
        s["comments"][cid] = top_comment
        # bump video commentCount
        if video_id and video_id in s["videos"]:
            stats = s["videos"][video_id].setdefault("statistics", {})
            try:
                stats["commentCount"] = str(int(
                    stats.get("commentCount", "0")) + 1)
            except ValueError:
                pass
        _record(s, "commentThreads_insert", threadId=tid,
                videoId=video_id, channelId=channel_id)
        _save_state(s)
        return _project(thread, _parse_parts(part),
                        ["kind", "etag", "id"])


@mcp.tool(name="comments_list")
def comments_list(part: str = "snippet",
                  id: str = "",
                  parentId: str = "",
                  maxResults: int = 20,
                  pageToken: str = "") -> dict:
    """YouTube Data API v3: comments.list — retrieve replies under
    a parent comment (or comments by id)."""
    with _lock():
        s = _load_state()
        if not (id or parentId):
            return _err(400, "missingRequiredParameter",
                        "Either `id` or `parentId` is required.",
                        domain="youtube.parameter")
        parts = _parse_parts(part)
        items: list = []
        if id:
            for cid in [x.strip() for x in id.split(",") if x.strip()]:
                c = s["comments"].get(cid)
                if c:
                    items.append(_project(c, parts,
                                          ["kind", "etag", "id"]))
        else:
            for c in s["comments"].values():
                if c.get("snippet", {}).get("parentId") == parentId:
                    items.append(_project(c, parts,
                                          ["kind", "etag", "id"]))
            items.sort(key=lambda c: c.get("snippet", {})
                       .get("publishedAt", ""))
        _record(s, "comments_list", id=id, parentId=parentId,
                count=len(items))
        _save_state(s)
        return _list_envelope("youtube#commentListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="comments_insert")
def comments_insert(part: str = "snippet",
                    snippet: dict | None = None) -> dict:
    """YouTube Data API v3: comments.insert — reply to an existing
    top-level comment. Body:
        snippet={parentId, textOriginal}"""
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        parent_id = snippet.get("parentId")
        text = snippet.get("textOriginal") or snippet.get("textDisplay")
        if not parent_id:
            return _err(400, "missingRequiredParameter",
                        "snippet.parentId is required.",
                        domain="youtube.parameter")
        if not text:
            return _err(400, "commentTextRequired",
                        "snippet.textOriginal is required.",
                        domain="youtube.comment")
        parent = s["comments"].get(parent_id)
        if not parent:
            return _err(404, "parentNotFound",
                        f"Parent comment not found: {parent_id}",
                        domain="youtube.comment")
        n = s["next_id"]["comment"]
        s["next_id"]["comment"] = n + 1
        cid = parent_id + ".r" + _hash_id(f"reply:{n}", 10)
        me = s["self"]["channelId"]
        c = _make_top_comment(cid, text, me,
                              parent["snippet"].get("videoId"),
                              parent_id=parent_id)
        c["snippet"]["channelId"] = parent["snippet"].get("channelId", "")
        s["comments"][cid] = c
        # attach to thread
        thread_id = parent_id.split(".", 1)[0]
        thread = s["commentThreads"].get(thread_id)
        if thread:
            replies = thread.setdefault("replies", {"comments": []})
            replies.setdefault("comments", []).append(c)
            snip = thread.setdefault("snippet", {})
            snip["totalReplyCount"] = int(snip.get(
                "totalReplyCount", 0)) + 1
        _record(s, "comments_insert", commentId=cid, parentId=parent_id)
        _save_state(s)
        return _project(c, _parse_parts(part), ["kind", "etag", "id"])


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@mcp.tool(name="subscriptions_list")
def subscriptions_list(part: str = "snippet",
                       id: str = "",
                       channelId: str = "",
                       mine: bool = False,
                       forChannelId: str = "",
                       order: str = "relevance",
                       maxResults: int = 5,
                       pageToken: str = "") -> dict:
    """YouTube Data API v3: subscriptions.list — list subscriptions.
    Exactly one filter: `id`, `channelId`, or `mine`. `forChannelId`
    further restricts to subscriptions targeting one of the listed
    channels (comma-separated)."""
    with _lock():
        s = _load_state()
        filters = sum(1 for f in (id, channelId, mine) if f)
        if filters != 1:
            return _err(400, "missingRequiredParameter",
                        "Exactly one of `id`, `channelId`, or `mine` "
                        "must be set.",
                        domain="youtube.parameter")
        parts = _parse_parts(part)
        items: list = []
        if id:
            for sid in [x.strip() for x in id.split(",") if x.strip()]:
                sub = s["subscriptions"].get(sid)
                if sub:
                    items.append(_project(sub, parts,
                                          ["kind", "etag", "id"]))
        else:
            owner = s["self"]["channelId"] if mine else channelId
            target_filter = {x.strip() for x in (forChannelId or "")
                             .split(",") if x.strip()}
            for sub in s["subscriptions"].values():
                snip = sub.get("snippet", {})
                if snip.get("subscriberChannelId") != owner:
                    continue
                if target_filter and snip.get("resourceId", {}).get(
                        "channelId") not in target_filter:
                    continue
                items.append(_project(sub, parts,
                                      ["kind", "etag", "id"]))
        if order == "alphabetical":
            items.sort(key=lambda i: i.get("snippet", {})
                       .get("title", ""))
        elif order == "unread":
            pass  # mock: no unread state
        else:
            items.sort(key=lambda i: i.get("snippet", {})
                       .get("publishedAt", ""), reverse=True)
        _record(s, "subscriptions_list", channelId=channelId,
                mine=mine, count=len(items))
        _save_state(s)
        return _list_envelope("youtube#subscriptionListResponse",
                              items, pageToken, maxResults)


@mcp.tool(name="subscriptions_insert")
def subscriptions_insert(part: str = "snippet",
                         snippet: dict | None = None) -> dict:
    """YouTube Data API v3: subscriptions.insert — subscribe the
    authenticated user to a channel. Body:
        snippet={resourceId={kind:"youtube#channel", channelId}}"""
    with _lock():
        s = _load_state()
        snippet = dict(snippet or {})
        resource_id = snippet.get("resourceId") or {}
        target_channel_id = resource_id.get("channelId")
        if not target_channel_id:
            return _err(400, "missingRequiredParameter",
                        "snippet.resourceId.channelId is required.",
                        domain="youtube.parameter")
        if target_channel_id not in s["channels"]:
            return _err(404, "subscriptionForbidden",
                        f"Channel not found: {target_channel_id}",
                        domain="youtube.subscription")
        me = s["self"]["channelId"]
        if target_channel_id == me:
            return _err(400, "subscriptionForbidden",
                        "Cannot subscribe to your own channel.",
                        domain="youtube.subscription")
        # dedupe
        for existing in s["subscriptions"].values():
            esnip = existing.get("snippet", {})
            if (esnip.get("subscriberChannelId") == me
                    and esnip.get("resourceId", {}).get(
                        "channelId") == target_channel_id):
                return _err(400, "subscriptionDuplicate",
                            "Subscription already exists.",
                            domain="youtube.subscription")
        n = s["next_id"]["subscription"]
        s["next_id"]["subscription"] = n + 1
        sid = _hash_id(f"sub:{me}:{target_channel_id}:{n}", 30)
        target = s["channels"][target_channel_id]
        sub = {
            "kind": "youtube#subscription",
            "etag": _etag("subscription", sid),
            "id": sid,
            "snippet": {
                "publishedAt": _now_iso(),
                "channelTitle": s["channels"].get(me, {}).get(
                    "snippet", {}).get("title", ""),
                "title": target.get("snippet", {}).get("title", ""),
                "description": target.get(
                    "snippet", {}).get("description", ""),
                "resourceId": {"kind": "youtube#channel",
                               "channelId": target_channel_id},
                "channelId": me,
                "subscriberChannelId": me,
                "thumbnails": target.get(
                    "snippet", {}).get("thumbnails",
                                       _default_thumbnails()),
            },
            "contentDetails": {"totalItemCount": 0,
                               "newItemCount": 0,
                               "activityType": "all"},
            "subscriberSnippet": {
                "title": s["channels"].get(me, {}).get(
                    "snippet", {}).get("title", ""),
                "description": s["channels"].get(me, {}).get(
                    "snippet", {}).get("description", ""),
                "channelId": me,
                "thumbnails": _default_thumbnails(),
            },
        }
        s["subscriptions"][sid] = sub
        # bump target subscriberCount
        try:
            target.setdefault("statistics", {})
            target["statistics"]["subscriberCount"] = str(int(
                target["statistics"].get("subscriberCount", "0")) + 1)
        except ValueError:
            pass
        _record(s, "subscriptions_insert",
                subscriptionId=sid, target=target_channel_id)
        _save_state(s)
        return _project(sub, _parse_parts(part),
                        ["kind", "etag", "id"])


@mcp.tool(name="subscriptions_delete")
def subscriptions_delete(id: str) -> dict:
    """YouTube Data API v3: subscriptions.delete — unsubscribe."""
    with _lock():
        s = _load_state()
        if not id:
            return _err(400, "missingRequiredParameter",
                        "id is required.",
                        domain="youtube.parameter")
        sub = s["subscriptions"].pop(id, None)
        if not sub:
            _record(s, "subscriptions_delete", id=id,
                    result="not_found")
            _save_state(s)
            return _err(404, "subscriptionNotFound",
                        f"Subscription not found: {id}",
                        domain="youtube.subscription")
        target_id = sub.get("snippet", {}).get(
            "resourceId", {}).get("channelId")
        target = s["channels"].get(target_id)
        if target is not None:
            try:
                cnt = int(target["statistics"].get(
                    "subscriberCount", "0"))
                target["statistics"]["subscriberCount"] = str(
                    max(cnt - 1, 0))
            except (KeyError, ValueError):
                pass
        _record(s, "subscriptions_delete", id=id, target=target_id)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

@mcp.tool(name="captions_list")
def captions_list(part: str = "snippet",
                  videoId: str = "",
                  id: str = "") -> dict:
    """YouTube Data API v3: captions.list — list caption tracks
    (metadata only; the mock does not return the actual track
    content). `videoId` is required when `id` is not given."""
    with _lock():
        s = _load_state()
        if not (videoId or id):
            return _err(400, "missingRequiredParameter",
                        "videoId is required.",
                        domain="youtube.parameter")
        parts = _parse_parts(part)
        items: list = []
        if id:
            for cid in [x.strip() for x in id.split(",") if x.strip()]:
                cap = s["captions"].get(cid)
                if cap:
                    items.append(_project(cap, parts,
                                          ["kind", "etag", "id"]))
        else:
            if videoId not in s["videos"]:
                _record(s, "captions_list", videoId=videoId,
                        result="not_found")
                _save_state(s)
                return _err(404, "videoNotFound",
                            f"Video not found: {videoId}",
                            domain="youtube.video")
            for cap in s["captions"].values():
                if cap.get("snippet", {}).get("videoId") == videoId:
                    items.append(_project(cap, parts,
                                          ["kind", "etag", "id"]))
        _record(s, "captions_list", videoId=videoId, id=id,
                count=len(items))
        _save_state(s)
        return {
            "kind": "youtube#captionListResponse",
            "etag": _etag("captions", videoId or id),
            "items": items,
        }


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (verifier introspection)."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(self_user: dict | None = None,
                    channels: list | None = None,
                    videos: list | None = None,
                    playlists: list | None = None,
                    playlistItems: list | None = None,
                    commentThreads: list | None = None,
                    subscriptions: list | None = None,
                    captions: list | None = None,
                    ratings: dict | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. Inputs use YouTube-shaped resource dicts.

    - `self_user`: {channelId?, username?}
    - `channels`: [{id?, snippet?, statistics?, status?, ...}]
    - `videos`:   [{id?, snippet?(title,channelId,...), status?,
                    contentDetails?, statistics?}]
    - `playlists`:[{id?, snippet?(title,channelId,...), status?}]
    - `playlistItems`: [{playlistId, videoId, position?, note?}]
    - `commentThreads`: [{videoId, text, channelId?}]
    - `subscriptions`:  [{channelId}]  # what the self channel is subscribed to
    - `captions`: [{videoId, language, name?, trackKind?}]
    - `ratings`: {videoId: "like"|"dislike"}

    If `replace` is true the state is wiped before seeding. IDs are
    auto-generated when not provided so seed fixtures stay terse."""
    with _lock():
        s = _empty_state() if replace else _load_state()

        if self_user:
            if self_user.get("channelId"):
                # rename the self channel id if requested
                old_id = s["self"]["channelId"]
                new_id = self_user["channelId"]
                if old_id in s["channels"] and old_id != new_id:
                    s["channels"][new_id] = s["channels"].pop(old_id)
                    s["channels"][new_id]["id"] = new_id
                s["self"]["channelId"] = new_id
            if self_user.get("username"):
                s["self"]["username"] = self_user["username"]

        for c in channels or []:
            cid = c.get("id") or _new_channel_id(s)
            ch = {"kind": "youtube#channel",
                  "etag": _etag("channel", cid),
                  "id": cid,
                  "snippet": dict(c.get("snippet") or {}),
                  "statistics": dict(c.get("statistics") or {}),
                  "status": dict(c.get("status") or {}),
                  "contentDetails": dict(c.get("contentDetails") or {})}
            if "title" in c:
                ch["snippet"]["title"] = c["title"]
            if "description" in c:
                ch["snippet"]["description"] = c["description"]
            _ensure_channel_defaults(ch)
            s["channels"][cid] = ch

        for v in videos or []:
            vid = v.get("id") or _new_video_id(s)
            snippet = dict(v.get("snippet") or {})
            for k in ("title", "description", "channelId", "channelTitle",
                      "publishedAt", "tags", "categoryId",
                      "defaultLanguage"):
                if k in v:
                    snippet[k] = v[k]
            video = {
                "kind": "youtube#video",
                "etag": _etag("video", vid),
                "id": vid,
                "snippet": snippet,
                "status": dict(v.get("status") or {}),
                "contentDetails": dict(v.get("contentDetails") or {}),
                "statistics": dict(v.get("statistics") or {}),
            }
            _ensure_video_defaults(video)
            s["videos"][vid] = video

        for p in playlists or []:
            pid = p.get("id") or _new_playlist_id(s)
            snippet = dict(p.get("snippet") or {})
            for k in ("title", "description", "channelId", "channelTitle"):
                if k in p:
                    snippet[k] = p[k]
            pl = {
                "kind": "youtube#playlist",
                "etag": _etag("playlist", pid),
                "id": pid,
                "snippet": snippet,
                "status": dict(p.get("status") or {}),
                "contentDetails": dict(p.get("contentDetails") or {}),
            }
            _ensure_playlist_defaults(pl)
            s["playlists"][pid] = pl

        for it in playlistItems or []:
            playlist_id = it.get("playlistId")
            vid = it.get("videoId")
            if not playlist_id or not vid:
                continue
            siblings = [x for x in s["playlistItems"].values()
                        if x.get("snippet", {}).get(
                            "playlistId") == playlist_id]
            pos = it.get("position", len(siblings))
            pli = _make_playlist_item(
                s, playlist_id,
                {"kind": "youtube#video", "videoId": vid},
                pos, it.get("note", ""))
            s["playlistItems"][pli["id"]] = pli
            if playlist_id in s["playlists"]:
                s["playlists"][playlist_id].setdefault(
                    "contentDetails", {})["itemCount"] = len(siblings) + 1

        me = s["self"]["channelId"]
        for t in commentThreads or []:
            text = t.get("text") or ""
            video_id = t.get("videoId")
            channel_id = t.get("channelId") or s["videos"].get(
                video_id, {}).get("snippet", {}).get("channelId", "")
            n = s["next_id"]["commentThread"]
            s["next_id"]["commentThread"] = n + 1
            tid = "UgC" + _hash_id(f"seed:{n}:{video_id}", 24)
            cn = s["next_id"]["comment"]
            s["next_id"]["comment"] = cn + 1
            cid = tid + "." + _hash_id(f"seed-c:{cn}", 10)
            top = _make_top_comment(cid, text, t.get("authorChannelId")
                                    or me, video_id)
            top["snippet"]["channelId"] = channel_id
            s["comments"][cid] = top
            s["commentThreads"][tid] = {
                "kind": "youtube#commentThread",
                "etag": _etag("commentThread", tid),
                "id": tid,
                "snippet": {
                    "channelId": channel_id,
                    "videoId": video_id or "",
                    "topLevelComment": top,
                    "canReply": True,
                    "totalReplyCount": 0,
                    "isPublic": True,
                },
                "replies": {"comments": []},
            }

        for sub in subscriptions or []:
            target_channel_id = sub.get("channelId")
            if not target_channel_id or target_channel_id == me:
                continue
            if target_channel_id not in s["channels"]:
                continue
            n = s["next_id"]["subscription"]
            s["next_id"]["subscription"] = n + 1
            sid = _hash_id(
                f"seed-sub:{me}:{target_channel_id}:{n}", 30)
            target = s["channels"][target_channel_id]
            s["subscriptions"][sid] = {
                "kind": "youtube#subscription",
                "etag": _etag("subscription", sid),
                "id": sid,
                "snippet": {
                    "publishedAt": _now_iso(),
                    "title": target.get("snippet", {}).get("title", ""),
                    "description": target.get(
                        "snippet", {}).get("description", ""),
                    "resourceId": {"kind": "youtube#channel",
                                   "channelId": target_channel_id},
                    "channelId": me,
                    "subscriberChannelId": me,
                    "thumbnails": target.get(
                        "snippet", {}).get("thumbnails",
                                           _default_thumbnails()),
                    "channelTitle": s["channels"].get(me, {}).get(
                        "snippet", {}).get("title", ""),
                },
            }

        for cap in captions or []:
            video_id = cap.get("videoId")
            if not video_id:
                continue
            n = s["next_id"]["caption"]
            s["next_id"]["caption"] = n + 1
            cap_id = _hash_id(f"caption:{video_id}:{n}", 36)
            s["captions"][cap_id] = {
                "kind": "youtube#caption",
                "etag": _etag("caption", cap_id),
                "id": cap_id,
                "snippet": {
                    "videoId": video_id,
                    "lastUpdated": _now_iso(),
                    "trackKind": cap.get("trackKind", "standard"),
                    "language": cap.get("language", "en"),
                    "name": cap.get("name", ""),
                    "audioTrackType": cap.get("audioTrackType",
                                              "unknown"),
                    "isCC": cap.get("isCC", False),
                    "isLarge": False,
                    "isEasyReader": False,
                    "isDraft": False,
                    "isAutoSynced": False,
                    "status": cap.get("status", "serving"),
                },
            }

        if ratings:
            s.setdefault("ratings", {}).update({
                vid: r for vid, r in ratings.items()
                if r in ("like", "dislike")
            })

        _record(s, "debug_seed",
                counts={
                    "channels": len(channels or []),
                    "videos": len(videos or []),
                    "playlists": len(playlists or []),
                    "playlistItems": len(playlistItems or []),
                    "commentThreads": len(commentThreads or []),
                    "subscriptions": len(subscriptions or []),
                    "captions": len(captions or []),
                    "ratings": len(ratings or {}),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "channelIds": list(s["channels"].keys()),
            "videoIds": list(s["videos"].keys()),
            "playlistIds": list(s["playlists"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
