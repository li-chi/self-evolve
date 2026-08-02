"""Bluesky / AT Protocol mock MCP server.

Mirrors the AT Protocol XRPC surface exposed by the public Bluesky
PDS / AppView (api.bsky.app + bsky.social). Each XRPC method is
namespaced (e.g. `app.bsky.feed.getTimeline`), so we expose the
dot-name verbatim as the MCP tool name. AT Protocol returns JSON
responses with strongly-typed records (lexicons); the mock returns
the same shape (DIDs, handles, AT URIs, CIDs, ViewerState, embeds,
facets, reasons, etc.).

Tool namespaces implemented (XRPC method -> MCP tool name):

  com.atproto.server
    com.atproto.server.createSession   (login)
    com.atproto.server.refreshSession
    com.atproto.server.getSession
    com.atproto.server.deleteSession

  app.bsky.actor
    app.bsky.actor.getProfile
    app.bsky.actor.getProfiles
    app.bsky.actor.searchActors
    app.bsky.actor.getPreferences

  app.bsky.feed (reads)
    app.bsky.feed.getTimeline
    app.bsky.feed.getAuthorFeed
    app.bsky.feed.getPostThread
    app.bsky.feed.getPosts
    app.bsky.feed.getLikes
    app.bsky.feed.getRepostedBy
    app.bsky.feed.searchPosts

  app.bsky.feed (writes, modeled via com.atproto.repo.createRecord)
    app.bsky.feed.post           (create a post record)
    app.bsky.feed.repost         (create a repost record)
    app.bsky.feed.like           (create a like record)
    app.bsky.feed.deletePost     (delete a post record)

  app.bsky.graph
    app.bsky.graph.getFollows
    app.bsky.graph.getFollowers
    app.bsky.graph.follow
    app.bsky.graph.unfollow
    app.bsky.graph.mute
    app.bsky.graph.unmute
    app.bsky.graph.block
    app.bsky.graph.unblock

  app.bsky.notification
    app.bsky.notification.listNotifications
    app.bsky.notification.updateSeen
    app.bsky.notification.getUnreadCount

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.

State lives at `$BLUESKY_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/bluesky_mock`). Per-rollout isolation should clear the
state dir between rollouts. Optional `BLUESKY_MOCK_SEED_PATH` preloads
state when no state.json exists yet.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace.

Errors are returned as AT Protocol JSON error bodies, not raised:
    {"error": "InvalidRequest", "message": "..."}
This matches what the real XRPC HTTP layer returns to clients.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "BLUESKY_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/bluesky_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    self_did = "did:plc:mockself0000000000000"
    self_handle = "mockbot.bsky.social"
    return {
        "service": {
            "name": "Mock Bluesky AppView",
            "endpoint": "https://mock.bsky.social",
        },
        "self": {
            "did": self_did,
            "handle": self_handle,
            "email": "mockbot@mock.bsky.social",
        },
        "session": {
            "active": True,
            "accessJwt": "mock.access.jwt.0",
            "refreshJwt": "mock.refresh.jwt.0",
            "did": self_did,
            "handle": self_handle,
        },
        # actors: did -> profile dict
        "actors": {
            self_did: _new_actor(self_did, self_handle, "Mock Bot"),
        },
        # handle -> did lookup
        "handles": {self_handle: self_did},
        # posts: at_uri -> post record (with viewer aggregates)
        "posts": {},
        # follow/like/repost graph records keyed by their own AT URI
        "follows": {},   # at_uri -> {subject, createdAt, author}
        "likes": {},     # at_uri -> {subject:{uri,cid}, createdAt, author}
        "reposts": {},   # at_uri -> {subject:{uri,cid}, createdAt, author}
        # mutes/blocks are simple sets of (author_did, target_did)
        "mutes": [],     # list[{actor_did, target_did}]
        "blocks": {},    # at_uri -> {subject, createdAt, author}
        # notifications for self
        "notifications": [],
        "seen_at": "1970-01-01T00:00:00.000Z",
        # actor-specific preferences blob (per AT Proto getPreferences)
        "preferences": [
            {"$type": "app.bsky.actor.defs#adultContentPref",
             "enabled": False},
        ],
        "next_id": {
            "actor": 1, "post": 1, "follow": 1, "like": 1,
            "repost": 1, "block": 1, "notif": 1,
        },
        "calls": [],
    }


def _new_actor(did: str, handle: str, display_name: str = "") -> dict:
    return {
        "did": did,
        "handle": handle,
        "displayName": display_name or handle.split(".")[0],
        "description": "",
        "avatar": None,
        "banner": None,
        "followersCount": 0,
        "followsCount": 0,
        "postsCount": 0,
        "indexedAt": _now_iso(),
        "createdAt": _now_iso(),
        "viewer": {
            "muted": False,
            "blockedBy": False,
            "following": None,    # AT URI of follow record if self follows
            "followedBy": None,   # AT URI of their follow back, if any
        },
        "labels": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("BLUESKY_MOCK_SEED_PATH")
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
# AT Protocol identifiers
# ---------------------------------------------------------------------------

def _err(name: str, message: str) -> dict:
    """Return an AT Protocol XRPC error body (matches HTTP error JSON)."""
    return {"error": name, "message": message}


def _new_did(state: dict) -> str:
    n = state["next_id"]["actor"]
    state["next_id"]["actor"] = n + 1
    # 24 char base32-ish suffix to look like did:plc:abc123...
    suffix = hashlib.sha256(f"mock-actor-{n}".encode()).hexdigest()[:24]
    return f"did:plc:{suffix}"


def _new_cid(payload: str = "") -> str:
    """Return a CID-looking string. Real CIDs are base32 CIDv1; we
    only need a stable-looking string so we hash the payload."""
    h = hashlib.sha256(payload.encode()).hexdigest()[:48]
    return f"bafyrei{h}"


def _new_record_key() -> str:
    """TID-like rkey (13-char base32). The real format encodes a
    timestamp; we use random ascii for the mock."""
    alphabet = "234567abcdefghijklmnopqrstuvwxyz"
    return "3" + "".join(secrets.choice(alphabet) for _ in range(12))


def _at_uri(did: str, collection: str, rkey: str) -> str:
    return f"at://{did}/{collection}/{rkey}"


_AT_URI_RE = re.compile(
    r"^at://(?P<did>did:[^/]+)/(?P<collection>[^/]+)/(?P<rkey>[^/]+)$"
)


def _parse_at_uri(uri: str) -> tuple[str, str, str] | None:
    if not uri:
        return None
    m = _AT_URI_RE.match(uri)
    if not m:
        return None
    return m.group("did"), m.group("collection"), m.group("rkey")


def _norm_handle(h: str) -> str:
    h = (h or "").strip()
    if h.startswith("@"):
        h = h[1:]
    if h and "." not in h and not h.startswith("did:"):
        h = f"{h}.bsky.social"
    return h


def _resolve_actor(state: dict, ref: str) -> str | None:
    """Resolve an actor reference (DID, handle, @handle) to a DID."""
    if not ref:
        return None
    if ref.startswith("did:"):
        return ref if ref in state["actors"] else None
    norm = _norm_handle(ref)
    did = state["handles"].get(norm)
    if did:
        return did
    # case-insensitive fallback
    low = norm.lower()
    for handle, d in state["handles"].items():
        if handle.lower() == low:
            return d
    return None


# ---------------------------------------------------------------------------
# Views (lexicon-flavored response shapes)
# ---------------------------------------------------------------------------

def _profile_view(state: dict, did: str,
                  detailed: bool = False) -> dict:
    a = state["actors"].get(did)
    if not a:
        return {}
    me = state["self"]["did"]
    viewer = dict(a.get("viewer") or {})
    # recompute viewer state vs the active session's self
    viewer["muted"] = any(m["actor_did"] == me and m["target_did"] == did
                          for m in state["mutes"])
    viewer["blockedBy"] = any(
        b["author"] == did
        and b["subject"] == me
        for b in state["blocks"].values()
    )
    viewer["blocking"] = next(
        (uri for uri, b in state["blocks"].items()
         if b["author"] == me and b["subject"] == did),
        None,
    )
    viewer["following"] = next(
        (uri for uri, f in state["follows"].items()
         if f["author"] == me and f["subject"] == did),
        None,
    )
    viewer["followedBy"] = next(
        (uri for uri, f in state["follows"].items()
         if f["author"] == did and f["subject"] == me),
        None,
    )
    base = {
        "did": a["did"],
        "handle": a["handle"],
        "displayName": a.get("displayName", ""),
        "avatar": a.get("avatar"),
        "indexedAt": a.get("indexedAt"),
        "viewer": viewer,
        "labels": a.get("labels", []),
        "createdAt": a.get("createdAt"),
    }
    if detailed:
        base.update({
            "description": a.get("description", ""),
            "banner": a.get("banner"),
            "followersCount": a.get("followersCount", 0),
            "followsCount": a.get("followsCount", 0),
            "postsCount": a.get("postsCount", 0),
        })
    return base


def _post_view(state: dict, uri: str) -> dict | None:
    """Build a postView (app.bsky.feed.defs#postView)."""
    rec = state["posts"].get(uri)
    if not rec:
        return None
    author_did = rec["author"]
    author = _profile_view(state, author_did, detailed=False)
    # Trim author to profileViewBasic shape
    author_basic = {k: author[k] for k in
                    ("did", "handle", "displayName", "avatar",
                     "viewer", "labels") if k in author}
    me = state["self"]["did"]
    viewer_like = next(
        (u for u, lk in state["likes"].items()
         if lk["author"] == me and lk["subject"]["uri"] == uri),
        None,
    )
    viewer_repost = next(
        (u for u, rp in state["reposts"].items()
         if rp["author"] == me and rp["subject"]["uri"] == uri),
        None,
    )
    return {
        "uri": uri,
        "cid": rec["cid"],
        "author": author_basic,
        "record": rec["record"],
        "embed": rec.get("embed"),
        "replyCount": rec.get("replyCount", 0),
        "repostCount": rec.get("repostCount", 0),
        "likeCount": rec.get("likeCount", 0),
        "quoteCount": rec.get("quoteCount", 0),
        "indexedAt": rec.get("indexedAt"),
        "viewer": {
            "like": viewer_like,
            "repost": viewer_repost,
            "threadMuted": False,
            "replyDisabled": False,
            "embeddingDisabled": False,
        },
        "labels": rec.get("labels", []),
    }


def _feed_view_item(state: dict, uri: str,
                    reason: dict | None = None) -> dict | None:
    """Build a feedViewPost (app.bsky.feed.defs#feedViewPost)."""
    pv = _post_view(state, uri)
    if not pv:
        return None
    out: dict[str, Any] = {"post": pv}
    rec = state["posts"][uri]["record"]
    parent_ref = (rec.get("reply") or {}).get("parent")
    root_ref = (rec.get("reply") or {}).get("root")
    if parent_ref and root_ref:
        parent_view = _post_view(state, parent_ref["uri"]) or {
            "$type": "app.bsky.feed.defs#notFoundPost",
            "uri": parent_ref["uri"],
            "notFound": True,
        }
        root_view = _post_view(state, root_ref["uri"]) or {
            "$type": "app.bsky.feed.defs#notFoundPost",
            "uri": root_ref["uri"],
            "notFound": True,
        }
        out["reply"] = {"root": root_view, "parent": parent_view}
    if reason:
        out["reason"] = reason
    return out


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("bluesky-mock")


# ---------------------------------------------------------------------------
# com.atproto.server.*
# ---------------------------------------------------------------------------

@mcp.tool(name="com.atproto.server.createSession")
def com_atproto_server_create_session(identifier: str,
                                      password: str = "",
                                      authFactorToken: str = "") -> dict:
    """XRPC: com.atproto.server.createSession — Create an authenticated
    session. `identifier` is a handle, DID, or email. The mock does
    not verify passwords (it always succeeds for known accounts and
    auto-creates an account for unknown handles)."""
    with _lock():
        s = _load_state()
        did = _resolve_actor(s, identifier)
        if not did and identifier and "@" not in identifier:
            # auto-create the account so logins always succeed
            did = _new_did(s)
            handle = _norm_handle(identifier)
            s["actors"][did] = _new_actor(did, handle, handle.split(".")[0])
            s["handles"][handle] = did
        if not did:
            _record(s, "createSession", identifier=identifier,
                    result="not_found")
            _save_state(s)
            return _err("AccountNotFound",
                        f"Account not found: {identifier}")
        actor = s["actors"][did]
        s["self"] = {
            "did": did,
            "handle": actor["handle"],
            "email": actor.get("email", f"{actor['handle']}@example.com"),
        }
        s["session"] = {
            "active": True,
            "accessJwt": f"mock.access.{secrets.token_hex(8)}",
            "refreshJwt": f"mock.refresh.{secrets.token_hex(8)}",
            "did": did,
            "handle": actor["handle"],
        }
        _record(s, "createSession", identifier=identifier, did=did)
        _save_state(s)
        return {
            "did": did,
            "handle": actor["handle"],
            "email": s["self"]["email"],
            "accessJwt": s["session"]["accessJwt"],
            "refreshJwt": s["session"]["refreshJwt"],
            "active": True,
            "emailConfirmed": True,
            "emailAuthFactor": False,
        }


@mcp.tool(name="com.atproto.server.refreshSession")
def com_atproto_server_refresh_session() -> dict:
    """XRPC: com.atproto.server.refreshSession — Rotate the access
    JWT using the refresh JWT (no parameters; the mock just generates
    fresh tokens for the current session)."""
    with _lock():
        s = _load_state()
        if not s["session"].get("active"):
            _record(s, "refreshSession", result="no_session")
            _save_state(s)
            return _err("ExpiredToken", "No active session")
        s["session"]["accessJwt"] = f"mock.access.{secrets.token_hex(8)}"
        s["session"]["refreshJwt"] = f"mock.refresh.{secrets.token_hex(8)}"
        _record(s, "refreshSession", did=s["session"]["did"])
        _save_state(s)
        return {
            "did": s["session"]["did"],
            "handle": s["session"]["handle"],
            "accessJwt": s["session"]["accessJwt"],
            "refreshJwt": s["session"]["refreshJwt"],
            "active": True,
        }


@mcp.tool(name="com.atproto.server.getSession")
def com_atproto_server_get_session() -> dict:
    """XRPC: com.atproto.server.getSession — Return info about the
    current authenticated session."""
    with _lock():
        s = _load_state()
        if not s["session"].get("active"):
            _record(s, "getSession", result="no_session")
            _save_state(s)
            return _err("AuthMissing", "No active session")
        _record(s, "getSession", did=s["session"]["did"])
        _save_state(s)
        return {
            "did": s["session"]["did"],
            "handle": s["session"]["handle"],
            "email": s["self"].get("email"),
            "emailConfirmed": True,
            "active": True,
        }


@mcp.tool(name="com.atproto.server.deleteSession")
def com_atproto_server_delete_session() -> dict:
    """XRPC: com.atproto.server.deleteSession — Sign out (invalidate
    the current refresh token). Returns an empty body on success."""
    with _lock():
        s = _load_state()
        s["session"]["active"] = False
        s["session"]["accessJwt"] = ""
        s["session"]["refreshJwt"] = ""
        _record(s, "deleteSession")
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# app.bsky.actor.*
# ---------------------------------------------------------------------------

@mcp.tool(name="app.bsky.actor.getProfile")
def app_bsky_actor_get_profile(actor: str) -> dict:
    """XRPC: app.bsky.actor.getProfile — Detailed profile view of an
    actor (handle or DID). Returns profileViewDetailed."""
    with _lock():
        s = _load_state()
        did = _resolve_actor(s, actor)
        if not did:
            _record(s, "getProfile", actor=actor, result="not_found")
            _save_state(s)
            return _err("ActorNotFound", f"Actor not found: {actor}")
        _record(s, "getProfile", actor=actor, did=did)
        _save_state(s)
        return _profile_view(s, did, detailed=True)


@mcp.tool(name="app.bsky.actor.getProfiles")
def app_bsky_actor_get_profiles(actors: list) -> dict:
    """XRPC: app.bsky.actor.getProfiles — Batch profile lookup
    (max 25). Unknown actors are silently omitted, matching the real
    AppView behavior."""
    with _lock():
        s = _load_state()
        if not isinstance(actors, list):
            return _err("InvalidRequest", "actors must be a list")
        if len(actors) > 25:
            return _err("InvalidRequest",
                        "actors length must be <= 25")
        out = []
        for ref in actors:
            did = _resolve_actor(s, ref)
            if did:
                out.append(_profile_view(s, did, detailed=True))
        _record(s, "getProfiles", count=len(out))
        _save_state(s)
        return {"profiles": out}


@mcp.tool(name="app.bsky.actor.searchActors")
def app_bsky_actor_search_actors(q: str = "",
                                 term: str = "",
                                 limit: int = 25,
                                 cursor: str = "") -> dict:
    """XRPC: app.bsky.actor.searchActors — Search actors by handle,
    DID, or display name. `q` is the new param name; `term` is the
    deprecated alias (the real server accepts both)."""
    with _lock():
        s = _load_state()
        needle = (q or term or "").lower().strip()
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        actors = list(s["actors"].values())
        actors.sort(key=lambda a: a["did"])
        hits = []
        for a in actors:
            hay = " ".join([
                a.get("handle", ""),
                a.get("did", ""),
                a.get("displayName", ""),
                a.get("description", ""),
            ]).lower()
            if needle and needle not in hay:
                continue
            hits.append(a)
        start = 0
        if cursor:
            for i, a in enumerate(hits):
                if a["did"] == cursor:
                    start = i + 1
                    break
        page = hits[start: start + limit]
        next_cursor = (page[-1]["did"]
                       if start + limit < len(hits) and page else None)
        results = [_profile_view(s, a["did"], detailed=False) for a in page]
        _record(s, "searchActors", q=q or term, count=len(results))
        _save_state(s)
        return {"actors": results, "cursor": next_cursor}


@mcp.tool(name="app.bsky.actor.getPreferences")
def app_bsky_actor_get_preferences() -> dict:
    """XRPC: app.bsky.actor.getPreferences — Return the authenticated
    user's preferences blob (saved feeds, content moderation, etc.)."""
    with _lock():
        s = _load_state()
        _record(s, "getPreferences")
        _save_state(s)
        return {"preferences": list(s.get("preferences", []))}


# ---------------------------------------------------------------------------
# app.bsky.feed.* — reads
# ---------------------------------------------------------------------------

def _paginate_by_index(items: list, cursor: str,
                       limit: int) -> tuple[list, str | None]:
    """Cursor format here is just str(index) — convenient and stable
    for in-memory ordering."""
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    end = start + limit
    page = items[start:end]
    next_cursor = str(end) if end < len(items) else None
    return page, next_cursor


def _all_posts_sorted(state: dict) -> list[str]:
    """All post URIs newest-first by indexedAt."""
    posts = [(uri, p) for uri, p in state["posts"].items()
             if not p.get("deleted")]
    posts.sort(key=lambda kv: kv[1].get("indexedAt", ""), reverse=True)
    return [uri for uri, _ in posts]


@mcp.tool(name="app.bsky.feed.getTimeline")
def app_bsky_feed_get_timeline(algorithm: str = "reverse-chronological",
                               limit: int = 50,
                               cursor: str = "") -> dict:
    """XRPC: app.bsky.feed.getTimeline — Authenticated user's home
    timeline (posts by self + followed accounts + their reposts).
    Returns {feed: feedViewPost[], cursor}."""
    with _lock():
        s = _load_state()
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        me = s["session"]["did"]
        followed = {f["subject"] for f in s["follows"].values()
                    if f["author"] == me}
        followed.add(me)
        # collect posts authored by followed + reposts by followed
        feed_items = []
        for uri in _all_posts_sorted(s):
            p = s["posts"][uri]
            if p["author"] in followed:
                feed_items.append((p.get("indexedAt", ""), uri, None))
        for rp_uri, rp in s["reposts"].items():
            if rp["author"] not in followed:
                continue
            tgt = rp["subject"]["uri"]
            if tgt not in s["posts"]:
                continue
            reason = {
                "$type": "app.bsky.feed.defs#reasonRepost",
                "by": _profile_view(s, rp["author"], detailed=False),
                "uri": rp_uri,
                "cid": _new_cid(rp_uri),
                "indexedAt": rp.get("createdAt", _now_iso()),
            }
            feed_items.append((rp.get("createdAt", ""), tgt, reason))
        feed_items.sort(key=lambda t: t[0], reverse=True)
        page, next_cursor = _paginate_by_index(feed_items, cursor, limit)
        feed = []
        for _, uri, reason in page:
            item = _feed_view_item(s, uri, reason=reason)
            if item:
                feed.append(item)
        _record(s, "getTimeline", count=len(feed),
                algorithm=algorithm)
        _save_state(s)
        return {"feed": feed, "cursor": next_cursor}


@mcp.tool(name="app.bsky.feed.getAuthorFeed")
def app_bsky_feed_get_author_feed(actor: str,
                                  limit: int = 50,
                                  cursor: str = "",
                                  filter: str = "posts_with_replies",
                                  includePins: bool = False) -> dict:
    """XRPC: app.bsky.feed.getAuthorFeed — Posts authored or reposted
    by `actor`. `filter` ∈ {posts_with_replies, posts_no_replies,
    posts_with_media, posts_and_author_threads, posts_with_video}."""
    with _lock():
        s = _load_state()
        did = _resolve_actor(s, actor)
        if not did:
            _record(s, "getAuthorFeed", actor=actor, result="not_found")
            _save_state(s)
            return _err("ActorNotFound", f"Actor not found: {actor}")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        items: list[tuple[str, str, dict | None]] = []
        for uri in _all_posts_sorted(s):
            p = s["posts"][uri]
            if p["author"] != did:
                continue
            rec = p.get("record", {})
            is_reply = bool(rec.get("reply"))
            has_media = bool(rec.get("embed"))
            if filter == "posts_no_replies" and is_reply:
                continue
            if filter == "posts_with_media" and not has_media:
                continue
            items.append((p.get("indexedAt", ""), uri, None))
        for rp_uri, rp in s["reposts"].items():
            if rp["author"] != did:
                continue
            tgt = rp["subject"]["uri"]
            if tgt not in s["posts"]:
                continue
            reason = {
                "$type": "app.bsky.feed.defs#reasonRepost",
                "by": _profile_view(s, did, detailed=False),
                "uri": rp_uri,
                "cid": _new_cid(rp_uri),
                "indexedAt": rp.get("createdAt", _now_iso()),
            }
            items.append((rp.get("createdAt", ""), tgt, reason))
        items.sort(key=lambda t: t[0], reverse=True)
        page, next_cursor = _paginate_by_index(items, cursor, limit)
        feed = [fvi for fvi in
                (_feed_view_item(s, uri, reason=reason)
                 for _, uri, reason in page) if fvi]
        _record(s, "getAuthorFeed", actor=actor, filter=filter,
                count=len(feed))
        _save_state(s)
        return {"feed": feed, "cursor": next_cursor}


def _thread_children(state: dict, parent_uri: str) -> list[str]:
    """Direct replies whose record.reply.parent.uri == parent_uri,
    ordered by indexedAt ascending."""
    kids = []
    for uri, p in state["posts"].items():
        if p.get("deleted"):
            continue
        rec_reply = (p.get("record") or {}).get("reply")
        if rec_reply and rec_reply.get("parent", {}).get("uri") == parent_uri:
            kids.append((p.get("indexedAt", ""), uri))
    kids.sort(key=lambda t: t[0])
    return [uri for _, uri in kids]


def _build_thread_view(state: dict, uri: str, depth: int,
                       parent_height: int) -> dict:
    pv = _post_view(state, uri)
    if not pv:
        return {
            "$type": "app.bsky.feed.defs#notFoundPost",
            "uri": uri,
            "notFound": True,
        }
    node: dict[str, Any] = {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": pv,
    }
    if depth > 0:
        replies = []
        for child_uri in _thread_children(state, uri):
            replies.append(_build_thread_view(state, child_uri,
                                              depth - 1, 0))
        node["replies"] = replies
    if parent_height > 0:
        parent_ref = (state["posts"][uri]["record"].get("reply")
                      or {}).get("parent")
        if parent_ref:
            node["parent"] = _build_thread_view(
                state, parent_ref["uri"], 0, parent_height - 1)
    return node


@mcp.tool(name="app.bsky.feed.getPostThread")
def app_bsky_feed_get_post_thread(uri: str,
                                  depth: int = 6,
                                  parentHeight: int = 80) -> dict:
    """XRPC: app.bsky.feed.getPostThread — Return a post and its
    surrounding thread (parents up + children down). Response wraps
    a threadViewPost in {"thread": ...}."""
    with _lock():
        s = _load_state()
        if depth < 0 or depth > 1000:
            depth = 6
        if parentHeight < 0 or parentHeight > 1000:
            parentHeight = 80
        if uri not in s["posts"] or s["posts"][uri].get("deleted"):
            _record(s, "getPostThread", uri=uri, result="not_found")
            _save_state(s)
            return _err("NotFound", f"Post not found: {uri}")
        thread = _build_thread_view(s, uri, depth, parentHeight)
        _record(s, "getPostThread", uri=uri)
        _save_state(s)
        return {"thread": thread}


@mcp.tool(name="app.bsky.feed.getPosts")
def app_bsky_feed_get_posts(uris: list) -> dict:
    """XRPC: app.bsky.feed.getPosts — Batch fetch postViews by AT URI
    (max 25). Unknown URIs are silently omitted."""
    with _lock():
        s = _load_state()
        if not isinstance(uris, list):
            return _err("InvalidRequest", "uris must be a list")
        if len(uris) > 25:
            return _err("InvalidRequest", "uris length must be <= 25")
        out = []
        for u in uris:
            pv = _post_view(s, u)
            if pv:
                out.append(pv)
        _record(s, "getPosts", count=len(out))
        _save_state(s)
        return {"posts": out}


@mcp.tool(name="app.bsky.feed.getLikes")
def app_bsky_feed_get_likes(uri: str,
                            cid: str = "",
                            limit: int = 50,
                            cursor: str = "") -> dict:
    """XRPC: app.bsky.feed.getLikes — List actors who liked a subject
    post. Returns {uri, cid, likes: [{indexedAt, createdAt, actor}]}.
    """
    with _lock():
        s = _load_state()
        if uri not in s["posts"] or s["posts"][uri].get("deleted"):
            _record(s, "getLikes", uri=uri, result="not_found")
            _save_state(s)
            return _err("NotFound", f"Post not found: {uri}")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        likes = [lk for lk in s["likes"].values()
                 if lk["subject"]["uri"] == uri]
        likes.sort(key=lambda lk: lk.get("createdAt", ""), reverse=True)
        page, next_cursor = _paginate_by_index(likes, cursor, limit)
        items = []
        for lk in page:
            actor = _profile_view(s, lk["author"], detailed=False)
            items.append({
                "indexedAt": lk.get("createdAt"),
                "createdAt": lk.get("createdAt"),
                "actor": actor,
            })
        _record(s, "getLikes", uri=uri, count=len(items))
        _save_state(s)
        return {"uri": uri,
                "cid": cid or s["posts"][uri]["cid"],
                "likes": items,
                "cursor": next_cursor}


@mcp.tool(name="app.bsky.feed.getRepostedBy")
def app_bsky_feed_get_reposted_by(uri: str,
                                  cid: str = "",
                                  limit: int = 50,
                                  cursor: str = "") -> dict:
    """XRPC: app.bsky.feed.getRepostedBy — List actors who reposted
    a subject post. Returns {uri, cid, repostedBy: profileView[]}."""
    with _lock():
        s = _load_state()
        if uri not in s["posts"] or s["posts"][uri].get("deleted"):
            _record(s, "getRepostedBy", uri=uri, result="not_found")
            _save_state(s)
            return _err("NotFound", f"Post not found: {uri}")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        reposts = [rp for rp in s["reposts"].values()
                   if rp["subject"]["uri"] == uri]
        reposts.sort(key=lambda rp: rp.get("createdAt", ""), reverse=True)
        page, next_cursor = _paginate_by_index(reposts, cursor, limit)
        items = [_profile_view(s, rp["author"], detailed=False)
                 for rp in page]
        _record(s, "getRepostedBy", uri=uri, count=len(items))
        _save_state(s)
        return {"uri": uri,
                "cid": cid or s["posts"][uri]["cid"],
                "repostedBy": items,
                "cursor": next_cursor}


@mcp.tool(name="app.bsky.feed.searchPosts")
def app_bsky_feed_search_posts(q: str,
                               sort: str = "latest",
                               since: str = "",
                               until: str = "",
                               mentions: str = "",
                               author: str = "",
                               lang: str = "",
                               domain: str = "",
                               url: str = "",
                               tag: list | None = None,
                               limit: int = 25,
                               cursor: str = "") -> dict:
    """XRPC: app.bsky.feed.searchPosts — Full-text search posts.
    `sort` ∈ {top, latest}. Mock matches substring against record.text
    (case-insensitive) plus optional author/lang/tag/date filters."""
    with _lock():
        s = _load_state()
        if not q:
            return _err("InvalidRequest", "q is required")
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        needle = q.lower()
        author_did = _resolve_actor(s, author) if author else None
        mentions_did = _resolve_actor(s, mentions) if mentions else None
        tags_filter = {t.lower() for t in (tag or [])}
        results = []
        for uri in _all_posts_sorted(s):
            p = s["posts"][uri]
            rec = p.get("record", {})
            text = (rec.get("text") or "").lower()
            if needle not in text:
                continue
            if author_did and p["author"] != author_did:
                continue
            if lang and lang not in (rec.get("langs") or []):
                continue
            if since and (p.get("indexedAt") or "") < since:
                continue
            if until and (p.get("indexedAt") or "") >= until:
                continue
            if mentions_did:
                facets = rec.get("facets") or []
                mentioned = set()
                for f in facets:
                    for feat in f.get("features") or []:
                        if feat.get("$type") == "app.bsky.richtext.facet#mention":
                            mentioned.add(feat.get("did"))
                if mentions_did not in mentioned:
                    continue
            if tags_filter:
                facets = rec.get("facets") or []
                post_tags = set()
                for f in facets:
                    for feat in f.get("features") or []:
                        if feat.get("$type") == "app.bsky.richtext.facet#tag":
                            post_tags.add((feat.get("tag") or "").lower())
                if not (tags_filter & post_tags):
                    continue
            if domain and domain.lower() not in text:
                continue
            if url and url.lower() not in text:
                continue
            results.append((p.get("likeCount", 0),
                            p.get("indexedAt", ""), uri))
        if sort == "top":
            results.sort(key=lambda t: (t[0], t[1]), reverse=True)
        else:
            results.sort(key=lambda t: t[1], reverse=True)
        page, next_cursor = _paginate_by_index(results, cursor, limit)
        posts = [_post_view(s, uri) for _, _, uri in page]
        posts = [pv for pv in posts if pv]
        _record(s, "searchPosts", q=q, count=len(posts))
        _save_state(s)
        return {"posts": posts, "cursor": next_cursor,
                "hitsTotal": len(results)}


# ---------------------------------------------------------------------------
# app.bsky.feed.* — writes
# ---------------------------------------------------------------------------

def _ensure_session(s: dict) -> str | None:
    """Return the active session DID or None if unauthenticated."""
    if not s["session"].get("active"):
        return None
    return s["session"].get("did")


@mcp.tool(name="app.bsky.feed.post")
def app_bsky_feed_post(text: str,
                       createdAt: str = "",
                       langs: list | None = None,
                       facets: list | None = None,
                       reply: dict | None = None,
                       embed: dict | None = None,
                       labels: dict | None = None,
                       tags: list | None = None) -> dict:
    """XRPC: com.atproto.repo.createRecord (collection=app.bsky.feed.post)
    — Create a new post record. Mirrors the AppView convention of
    `app.bsky.feed.post`. Returns {uri, cid, commit:{...}, validationStatus}.

    `text` must be <=300 graphemes (mock checks chars). `reply.parent`
    and `reply.root` are AT URI/CID strong refs; if `reply.root` is
    omitted, the parent's root (or the parent itself) is used.
    `embed` may be an embedImages, embedExternal, embedRecord, or
    embedRecordWithMedia variant — mock stores it verbatim.
    """
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        if len(text or "") > 300:
            return _err("InvalidRequest",
                        f"text too long: {len(text)} chars (max 300)")
        # validate reply
        normalized_reply = None
        if reply:
            parent = reply.get("parent") or {}
            parent_uri = parent.get("uri")
            if parent_uri not in s["posts"]:
                return _err("InvalidRequest",
                            f"reply parent not found: {parent_uri}")
            root = reply.get("root") or {}
            if not root.get("uri"):
                # walk up to find the root
                cur_uri = parent_uri
                while True:
                    p = s["posts"][cur_uri]
                    rr = (p.get("record") or {}).get("reply")
                    if not rr:
                        break
                    nxt = (rr.get("root") or {}).get("uri")
                    if not nxt or nxt == cur_uri:
                        break
                    cur_uri = nxt
                root = {"uri": cur_uri,
                        "cid": s["posts"][cur_uri]["cid"]}
            normalized_reply = {
                "root": {"uri": root["uri"], "cid": root.get("cid")
                         or s["posts"][root["uri"]]["cid"]},
                "parent": {"uri": parent_uri,
                           "cid": parent.get("cid")
                           or s["posts"][parent_uri]["cid"]},
            }
        rkey = _new_record_key()
        uri = _at_uri(me, "app.bsky.feed.post", rkey)
        ts = createdAt or _now_iso()
        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text or "",
            "createdAt": ts,
        }
        if langs:
            record["langs"] = list(langs)
        if facets:
            record["facets"] = facets
        if normalized_reply:
            record["reply"] = normalized_reply
        if embed:
            record["embed"] = embed
        if labels:
            record["labels"] = labels
        if tags:
            record["tags"] = list(tags)
        cid = _new_cid(json.dumps(record, sort_keys=True))
        post = {
            "uri": uri,
            "cid": cid,
            "author": me,
            "record": record,
            "embed": embed,
            "replyCount": 0,
            "repostCount": 0,
            "likeCount": 0,
            "quoteCount": 0,
            "indexedAt": ts,
            "labels": [],
            "deleted": False,
        }
        s["posts"][uri] = post
        if normalized_reply:
            parent_uri = normalized_reply["parent"]["uri"]
            s["posts"][parent_uri]["replyCount"] = (
                s["posts"][parent_uri].get("replyCount", 0) + 1
            )
            parent_author = s["posts"][parent_uri]["author"]
            if parent_author != me:
                _push_notification(s, parent_author, me, "reply",
                                   record_uri=uri, subject_uri=parent_uri,
                                   cid=cid)
        s["actors"][me]["postsCount"] = (
            s["actors"][me].get("postsCount", 0) + 1
        )
        _record(s, "createRecord", collection="app.bsky.feed.post",
                uri=uri, cid=cid)
        _save_state(s)
        return {
            "uri": uri,
            "cid": cid,
            "commit": {"cid": cid, "rev": secrets.token_hex(8)},
            "validationStatus": "valid",
        }


@mcp.tool(name="app.bsky.feed.repost")
def app_bsky_feed_repost(uri: str,
                         cid: str = "",
                         createdAt: str = "") -> dict:
    """XRPC: com.atproto.repo.createRecord (collection=app.bsky.feed.repost)
    — Create a repost record referencing a subject post. The subject's
    repostCount increments and the subject author is notified."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        if uri not in s["posts"] or s["posts"][uri].get("deleted"):
            return _err("InvalidRequest", f"subject not found: {uri}")
        # de-dup: reposting same subject twice no-ops to the existing URI
        existing = next(
            (u for u, r in s["reposts"].items()
             if r["author"] == me and r["subject"]["uri"] == uri),
            None,
        )
        if existing:
            return {"uri": existing,
                    "cid": _new_cid(existing),
                    "validationStatus": "valid"}
        rkey = _new_record_key()
        record_uri = _at_uri(me, "app.bsky.feed.repost", rkey)
        ts = createdAt or _now_iso()
        subject = {"uri": uri, "cid": cid or s["posts"][uri]["cid"]}
        s["reposts"][record_uri] = {
            "author": me,
            "subject": subject,
            "createdAt": ts,
        }
        s["posts"][uri]["repostCount"] = (
            s["posts"][uri].get("repostCount", 0) + 1
        )
        author = s["posts"][uri]["author"]
        if author != me:
            _push_notification(s, author, me, "repost",
                               record_uri=record_uri,
                               subject_uri=uri, cid=subject["cid"])
        rcid = _new_cid(record_uri)
        _record(s, "createRecord", collection="app.bsky.feed.repost",
                uri=record_uri, subject=uri)
        _save_state(s)
        return {
            "uri": record_uri,
            "cid": rcid,
            "commit": {"cid": rcid, "rev": secrets.token_hex(8)},
            "validationStatus": "valid",
        }


@mcp.tool(name="app.bsky.feed.like")
def app_bsky_feed_like(uri: str,
                       cid: str = "",
                       createdAt: str = "") -> dict:
    """XRPC: com.atproto.repo.createRecord (collection=app.bsky.feed.like)
    — Create a like record on a subject post. Likes are idempotent
    per (author, subject); a second like returns the existing record."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        if uri not in s["posts"] or s["posts"][uri].get("deleted"):
            return _err("InvalidRequest", f"subject not found: {uri}")
        existing = next(
            (u for u, lk in s["likes"].items()
             if lk["author"] == me and lk["subject"]["uri"] == uri),
            None,
        )
        if existing:
            return {"uri": existing,
                    "cid": _new_cid(existing),
                    "validationStatus": "valid"}
        rkey = _new_record_key()
        record_uri = _at_uri(me, "app.bsky.feed.like", rkey)
        ts = createdAt or _now_iso()
        subject = {"uri": uri, "cid": cid or s["posts"][uri]["cid"]}
        s["likes"][record_uri] = {
            "author": me,
            "subject": subject,
            "createdAt": ts,
        }
        s["posts"][uri]["likeCount"] = (
            s["posts"][uri].get("likeCount", 0) + 1
        )
        author = s["posts"][uri]["author"]
        if author != me:
            _push_notification(s, author, me, "like",
                               record_uri=record_uri,
                               subject_uri=uri, cid=subject["cid"])
        rcid = _new_cid(record_uri)
        _record(s, "createRecord", collection="app.bsky.feed.like",
                uri=record_uri, subject=uri)
        _save_state(s)
        return {
            "uri": record_uri,
            "cid": rcid,
            "commit": {"cid": rcid, "rev": secrets.token_hex(8)},
            "validationStatus": "valid",
        }


@mcp.tool(name="app.bsky.feed.deletePost")
def app_bsky_feed_delete_post(uri: str = "", rkey: str = "") -> dict:
    """XRPC: com.atproto.repo.deleteRecord (collection=app.bsky.feed.post)
    — Tombstone a post record owned by the current session. Provide
    either the full `uri` or just the `rkey` (resolved against the
    authenticated DID). Returns {} on success."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target_uri = uri
        if not target_uri and rkey:
            target_uri = _at_uri(me, "app.bsky.feed.post", rkey)
        if not target_uri:
            return _err("InvalidRequest", "uri or rkey is required")
        p = s["posts"].get(target_uri)
        if not p or p.get("deleted"):
            _record(s, "deleteRecord", uri=target_uri,
                    result="not_found")
            _save_state(s)
            return _err("NotFound", f"Post not found: {target_uri}")
        if p["author"] != me:
            _record(s, "deleteRecord", uri=target_uri,
                    result="forbidden")
            _save_state(s)
            return _err("Forbidden",
                        "Cannot delete a post you do not own")
        p["deleted"] = True
        # decrement counts where applicable
        rec_reply = (p.get("record") or {}).get("reply")
        if rec_reply:
            parent_uri = (rec_reply.get("parent") or {}).get("uri")
            if parent_uri in s["posts"]:
                s["posts"][parent_uri]["replyCount"] = max(
                    0, s["posts"][parent_uri].get("replyCount", 0) - 1)
        s["actors"][me]["postsCount"] = max(
            0, s["actors"][me].get("postsCount", 0) - 1)
        _record(s, "deleteRecord", collection="app.bsky.feed.post",
                uri=target_uri)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# app.bsky.graph.*
# ---------------------------------------------------------------------------

@mcp.tool(name="app.bsky.graph.getFollows")
def app_bsky_graph_get_follows(actor: str,
                               limit: int = 50,
                               cursor: str = "") -> dict:
    """XRPC: app.bsky.graph.getFollows — Profiles `actor` follows."""
    with _lock():
        s = _load_state()
        did = _resolve_actor(s, actor)
        if not did:
            return _err("ActorNotFound", f"Actor not found: {actor}")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        follows = [f for f in s["follows"].values()
                   if f["author"] == did]
        follows.sort(key=lambda f: f.get("createdAt", ""), reverse=True)
        page, next_cursor = _paginate_by_index(follows, cursor, limit)
        items = [_profile_view(s, f["subject"], detailed=False)
                 for f in page]
        _record(s, "getFollows", actor=actor, count=len(items))
        _save_state(s)
        return {
            "subject": _profile_view(s, did, detailed=False),
            "follows": items,
            "cursor": next_cursor,
        }


@mcp.tool(name="app.bsky.graph.getFollowers")
def app_bsky_graph_get_followers(actor: str,
                                 limit: int = 50,
                                 cursor: str = "") -> dict:
    """XRPC: app.bsky.graph.getFollowers — Profiles following `actor`."""
    with _lock():
        s = _load_state()
        did = _resolve_actor(s, actor)
        if not did:
            return _err("ActorNotFound", f"Actor not found: {actor}")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        follows = [f for f in s["follows"].values()
                   if f["subject"] == did]
        follows.sort(key=lambda f: f.get("createdAt", ""), reverse=True)
        page, next_cursor = _paginate_by_index(follows, cursor, limit)
        items = [_profile_view(s, f["author"], detailed=False)
                 for f in page]
        _record(s, "getFollowers", actor=actor, count=len(items))
        _save_state(s)
        return {
            "subject": _profile_view(s, did, detailed=False),
            "followers": items,
            "cursor": next_cursor,
        }


@mcp.tool(name="app.bsky.graph.follow")
def app_bsky_graph_follow(subject: str,
                          createdAt: str = "") -> dict:
    """XRPC: com.atproto.repo.createRecord (collection=app.bsky.graph.follow)
    — Follow another actor (DID or handle). Idempotent."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target_did = _resolve_actor(s, subject)
        if not target_did:
            return _err("ActorNotFound", f"Actor not found: {subject}")
        if target_did == me:
            return _err("InvalidRequest", "cannot follow self")
        existing = next(
            (u for u, f in s["follows"].items()
             if f["author"] == me and f["subject"] == target_did),
            None,
        )
        if existing:
            return {"uri": existing, "cid": _new_cid(existing),
                    "validationStatus": "valid"}
        rkey = _new_record_key()
        record_uri = _at_uri(me, "app.bsky.graph.follow", rkey)
        ts = createdAt or _now_iso()
        s["follows"][record_uri] = {
            "author": me,
            "subject": target_did,
            "createdAt": ts,
        }
        s["actors"][me]["followsCount"] = (
            s["actors"][me].get("followsCount", 0) + 1)
        s["actors"][target_did]["followersCount"] = (
            s["actors"][target_did].get("followersCount", 0) + 1)
        _push_notification(s, target_did, me, "follow",
                           record_uri=record_uri,
                           subject_uri=record_uri,
                           cid=_new_cid(record_uri))
        rcid = _new_cid(record_uri)
        _record(s, "createRecord", collection="app.bsky.graph.follow",
                uri=record_uri, subject=target_did)
        _save_state(s)
        return {"uri": record_uri, "cid": rcid,
                "commit": {"cid": rcid, "rev": secrets.token_hex(8)},
                "validationStatus": "valid"}


@mcp.tool(name="app.bsky.graph.unfollow")
def app_bsky_graph_unfollow(uri: str = "",
                            subject: str = "",
                            rkey: str = "") -> dict:
    """XRPC: com.atproto.repo.deleteRecord (collection=app.bsky.graph.follow)
    — Remove a follow record. Provide either the follow record `uri`,
    or `subject` (DID/handle) to look it up. Returns {} on success."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target_uri = uri
        if not target_uri and rkey:
            target_uri = _at_uri(me, "app.bsky.graph.follow", rkey)
        if not target_uri and subject:
            target_did = _resolve_actor(s, subject)
            if not target_did:
                return _err("ActorNotFound",
                            f"Actor not found: {subject}")
            target_uri = next(
                (u for u, f in s["follows"].items()
                 if f["author"] == me and f["subject"] == target_did),
                None,
            )
        if not target_uri or target_uri not in s["follows"]:
            return _err("NotFound", "follow record not found")
        f = s["follows"][target_uri]
        if f["author"] != me:
            return _err("Forbidden", "not owned by session")
        target_did = f["subject"]
        del s["follows"][target_uri]
        s["actors"][me]["followsCount"] = max(
            0, s["actors"][me].get("followsCount", 0) - 1)
        s["actors"][target_did]["followersCount"] = max(
            0, s["actors"][target_did].get("followersCount", 0) - 1)
        _record(s, "deleteRecord", collection="app.bsky.graph.follow",
                uri=target_uri)
        _save_state(s)
        return {}


@mcp.tool(name="app.bsky.graph.mute")
def app_bsky_graph_mute(actor: str) -> dict:
    """XRPC: app.bsky.graph.muteActor — Mute an actor (no notifications,
    no timeline). Idempotent."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target = _resolve_actor(s, actor)
        if not target:
            return _err("ActorNotFound", f"Actor not found: {actor}")
        if not any(m["actor_did"] == me and m["target_did"] == target
                   for m in s["mutes"]):
            s["mutes"].append({"actor_did": me, "target_did": target,
                               "createdAt": _now_iso()})
        _record(s, "muteActor", actor=actor, target=target)
        _save_state(s)
        return {}


@mcp.tool(name="app.bsky.graph.unmute")
def app_bsky_graph_unmute(actor: str) -> dict:
    """XRPC: app.bsky.graph.unmuteActor — Unmute an actor previously
    muted by the authenticated user."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target = _resolve_actor(s, actor)
        if not target:
            return _err("ActorNotFound", f"Actor not found: {actor}")
        before = len(s["mutes"])
        s["mutes"] = [m for m in s["mutes"]
                      if not (m["actor_did"] == me
                              and m["target_did"] == target)]
        _record(s, "unmuteActor", actor=actor, target=target,
                removed=before - len(s["mutes"]))
        _save_state(s)
        return {}


@mcp.tool(name="app.bsky.graph.block")
def app_bsky_graph_block(subject: str,
                         createdAt: str = "") -> dict:
    """XRPC: com.atproto.repo.createRecord (collection=app.bsky.graph.block)
    — Block another actor. Creates a block record (visible in the
    graph) and removes any reciprocal follow."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target_did = _resolve_actor(s, subject)
        if not target_did:
            return _err("ActorNotFound", f"Actor not found: {subject}")
        if target_did == me:
            return _err("InvalidRequest", "cannot block self")
        existing = next(
            (u for u, b in s["blocks"].items()
             if b["author"] == me and b["subject"] == target_did),
            None,
        )
        if existing:
            return {"uri": existing, "cid": _new_cid(existing),
                    "validationStatus": "valid"}
        rkey = _new_record_key()
        record_uri = _at_uri(me, "app.bsky.graph.block", rkey)
        ts = createdAt or _now_iso()
        s["blocks"][record_uri] = {
            "author": me,
            "subject": target_did,
            "createdAt": ts,
        }
        # auto-remove follows both ways
        for direction in ((me, target_did), (target_did, me)):
            to_remove = [u for u, f in s["follows"].items()
                         if f["author"] == direction[0]
                         and f["subject"] == direction[1]]
            for u in to_remove:
                del s["follows"][u]
                s["actors"][direction[0]]["followsCount"] = max(
                    0, s["actors"][direction[0]].get("followsCount", 0) - 1)
                s["actors"][direction[1]]["followersCount"] = max(
                    0, s["actors"][direction[1]].get("followersCount", 0) - 1)
        rcid = _new_cid(record_uri)
        _record(s, "createRecord", collection="app.bsky.graph.block",
                uri=record_uri, subject=target_did)
        _save_state(s)
        return {"uri": record_uri, "cid": rcid,
                "commit": {"cid": rcid, "rev": secrets.token_hex(8)},
                "validationStatus": "valid"}


@mcp.tool(name="app.bsky.graph.unblock")
def app_bsky_graph_unblock(uri: str = "",
                           subject: str = "",
                           rkey: str = "") -> dict:
    """XRPC: com.atproto.repo.deleteRecord (collection=app.bsky.graph.block)
    — Remove a block record. Returns {} on success."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        target_uri = uri
        if not target_uri and rkey:
            target_uri = _at_uri(me, "app.bsky.graph.block", rkey)
        if not target_uri and subject:
            target_did = _resolve_actor(s, subject)
            if not target_did:
                return _err("ActorNotFound",
                            f"Actor not found: {subject}")
            target_uri = next(
                (u for u, b in s["blocks"].items()
                 if b["author"] == me and b["subject"] == target_did),
                None,
            )
        if not target_uri or target_uri not in s["blocks"]:
            return _err("NotFound", "block record not found")
        b = s["blocks"][target_uri]
        if b["author"] != me:
            return _err("Forbidden", "not owned by session")
        del s["blocks"][target_uri]
        _record(s, "deleteRecord", collection="app.bsky.graph.block",
                uri=target_uri)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# app.bsky.notification.*
# ---------------------------------------------------------------------------

_NOTIF_REASONS = {"like", "repost", "follow", "mention", "reply",
                  "quote", "starterpack-joined"}


def _push_notification(state: dict, recipient_did: str,
                       author_did: str, reason: str,
                       record_uri: str, subject_uri: str,
                       cid: str = "") -> None:
    if recipient_did == author_did:
        return
    if reason not in _NOTIF_REASONS:
        return
    n = state["next_id"]["notif"]
    state["next_id"]["notif"] = n + 1
    state["notifications"].append({
        "id": f"notif{n:08d}",
        "recipient": recipient_did,
        "uri": record_uri,
        "cid": cid or _new_cid(record_uri),
        "author": author_did,
        "reason": reason,
        "reasonSubject": subject_uri if reason in (
            "like", "repost", "reply", "mention", "quote") else None,
        "isRead": False,
        "indexedAt": _now_iso(),
    })


@mcp.tool(name="app.bsky.notification.listNotifications")
def app_bsky_notification_list_notifications(limit: int = 50,
                                             cursor: str = "",
                                             seenAt: str = "",
                                             priority: bool = False,
                                             reasons: list | None = None
                                             ) -> dict:
    """XRPC: app.bsky.notification.listNotifications — List notifications
    for the authenticated user. Each item includes uri, cid, author,
    reason, reasonSubject, record-shape, isRead, indexedAt."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        seen_cutoff = seenAt or s.get("seen_at", "1970-01-01T00:00:00.000Z")
        notifs = [n for n in s["notifications"]
                  if n["recipient"] == me]
        if reasons:
            wanted = set(reasons)
            notifs = [n for n in notifs if n["reason"] in wanted]
        notifs.sort(key=lambda n: n["indexedAt"], reverse=True)
        page, next_cursor = _paginate_by_index(notifs, cursor, limit)
        results = []
        for n in page:
            author = _profile_view(s, n["author"], detailed=False)
            # try to fetch the record for the notification's source
            rec = None
            if n["uri"] in s["posts"]:
                rec = s["posts"][n["uri"]]["record"]
            elif n["uri"] in s["likes"]:
                lk = s["likes"][n["uri"]]
                rec = {"$type": "app.bsky.feed.like",
                       "subject": lk["subject"],
                       "createdAt": lk["createdAt"]}
            elif n["uri"] in s["reposts"]:
                rp = s["reposts"][n["uri"]]
                rec = {"$type": "app.bsky.feed.repost",
                       "subject": rp["subject"],
                       "createdAt": rp["createdAt"]}
            elif n["uri"] in s["follows"]:
                fl = s["follows"][n["uri"]]
                rec = {"$type": "app.bsky.graph.follow",
                       "subject": fl["subject"],
                       "createdAt": fl["createdAt"]}
            results.append({
                "uri": n["uri"],
                "cid": n["cid"],
                "author": author,
                "reason": n["reason"],
                "reasonSubject": n.get("reasonSubject"),
                "record": rec or {},
                "isRead": n["indexedAt"] <= seen_cutoff or n.get("isRead"),
                "indexedAt": n["indexedAt"],
                "labels": [],
            })
        _record(s, "listNotifications", count=len(results),
                seenAt=seen_cutoff)
        _save_state(s)
        return {
            "notifications": results,
            "cursor": next_cursor,
            "seenAt": s.get("seen_at"),
            "priority": bool(priority),
        }


@mcp.tool(name="app.bsky.notification.updateSeen")
def app_bsky_notification_update_seen(seenAt: str = "") -> dict:
    """XRPC: app.bsky.notification.updateSeen — Mark notifications as
    read up to `seenAt` (defaults to now). Returns {} on success."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        ts = seenAt or _now_iso()
        s["seen_at"] = ts
        for n in s["notifications"]:
            if n["recipient"] == me and n["indexedAt"] <= ts:
                n["isRead"] = True
        _record(s, "updateSeen", seenAt=ts)
        _save_state(s)
        return {}


@mcp.tool(name="app.bsky.notification.getUnreadCount")
def app_bsky_notification_get_unread_count(seenAt: str = "",
                                           priority: bool = False) -> dict:
    """XRPC: app.bsky.notification.getUnreadCount — Number of unread
    notifications for the authenticated user."""
    with _lock():
        s = _load_state()
        me = _ensure_session(s)
        if not me:
            return _err("AuthRequired", "No active session")
        cutoff = seenAt or s.get("seen_at", "1970-01-01T00:00:00.000Z")
        count = sum(1 for n in s["notifications"]
                    if n["recipient"] == me
                    and not n.get("isRead")
                    and n["indexedAt"] > cutoff)
        _record(s, "getUnreadCount", count=count)
        _save_state(s)
        return {"count": count}


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state (for verifier
    introspection). Not part of the AT Protocol surface."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(self_actor: dict | None = None,
                    actors: list | None = None,
                    posts: list | None = None,
                    follows: list | None = None,
                    likes: list | None = None,
                    reposts: list | None = None,
                    blocks: list | None = None,
                    mutes: list | None = None,
                    notifications: list | None = None,
                    preferences: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed mock state. Each input collection holds
    AT-Proto-shaped dicts.

    - `self_actor`: {did?, handle, displayName?, email?} — sets the
      active session.
    - `actors`: [{did?, handle, displayName?, description?,
                  followersCount?, followsCount?, postsCount?}]
    - `posts`:  [{uri?, author (did/handle), text, createdAt?,
                  langs?, facets?, reply?, embed?, indexedAt?}]
    - `follows`/`likes`/`reposts`/`blocks`: edges between actors/posts
      ({author, subject[, createdAt]}).
    - `mutes`:  [{actor, target}]
    - `notifications`: [{recipient, author, reason, uri?, subject_uri?}]
    - `preferences`: replaces the preferences blob if provided.

    If `replace` is true, the state is fully reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if self_actor:
            did = self_actor.get("did") or _new_did(s)
            handle = _norm_handle(
                self_actor.get("handle") or "mockbot.bsky.social")
            display = self_actor.get("displayName") or handle.split(".")[0]
            if did not in s["actors"]:
                s["actors"][did] = _new_actor(did, handle, display)
            else:
                s["actors"][did]["handle"] = handle
                s["actors"][did]["displayName"] = display
            s["handles"][handle] = did
            s["self"] = {"did": did, "handle": handle,
                         "email": self_actor.get("email",
                                                 f"{handle}@example.com")}
            s["session"] = {
                "active": True,
                "accessJwt": f"mock.access.{secrets.token_hex(8)}",
                "refreshJwt": f"mock.refresh.{secrets.token_hex(8)}",
                "did": did,
                "handle": handle,
            }
        for a in actors or []:
            did = a.get("did") or _new_did(s)
            handle = _norm_handle(a.get("handle") or f"{did}.bsky.social")
            if did not in s["actors"]:
                s["actors"][did] = _new_actor(
                    did, handle, a.get("displayName") or handle.split(".")[0])
            actor = s["actors"][did]
            actor["handle"] = handle
            if "displayName" in a:
                actor["displayName"] = a["displayName"]
            if "description" in a:
                actor["description"] = a["description"]
            for cnt in ("followersCount", "followsCount", "postsCount"):
                if cnt in a:
                    actor[cnt] = int(a[cnt])
            if "avatar" in a:
                actor["avatar"] = a["avatar"]
            s["handles"][handle] = did
        for p in posts or []:
            author_did = _resolve_actor(s, p.get("author", "")) or p.get("author")
            if not author_did or author_did not in s["actors"]:
                continue
            if p.get("uri"):
                uri = p["uri"]
                rkey = p["uri"].split("/")[-1]
            else:
                rkey = _new_record_key()
                uri = _at_uri(author_did, "app.bsky.feed.post", rkey)
            ts = p.get("createdAt") or p.get("indexedAt") or _now_iso()
            record = {
                "$type": "app.bsky.feed.post",
                "text": p.get("text", ""),
                "createdAt": ts,
            }
            for k in ("langs", "facets", "reply", "embed", "tags", "labels"):
                if k in p:
                    record[k] = p[k]
            cid = p.get("cid") or _new_cid(json.dumps(record, sort_keys=True))
            s["posts"][uri] = {
                "uri": uri,
                "cid": cid,
                "author": author_did,
                "record": record,
                "embed": record.get("embed"),
                "replyCount": int(p.get("replyCount", 0)),
                "repostCount": int(p.get("repostCount", 0)),
                "likeCount": int(p.get("likeCount", 0)),
                "quoteCount": int(p.get("quoteCount", 0)),
                "indexedAt": p.get("indexedAt") or ts,
                "labels": p.get("labels", []),
                "deleted": False,
            }
        for f in follows or []:
            au = _resolve_actor(s, f.get("author") or "")
            sub = _resolve_actor(s, f.get("subject") or "")
            if not (au and sub):
                continue
            rkey = _new_record_key()
            uri = _at_uri(au, "app.bsky.graph.follow", rkey)
            s["follows"][uri] = {"author": au, "subject": sub,
                                 "createdAt": f.get("createdAt", _now_iso())}
        for l in likes or []:
            au = _resolve_actor(s, l.get("author") or "")
            sub_uri = l.get("subject")
            if isinstance(sub_uri, dict):
                sub_uri = sub_uri.get("uri")
            if not (au and sub_uri and sub_uri in s["posts"]):
                continue
            rkey = _new_record_key()
            uri = _at_uri(au, "app.bsky.feed.like", rkey)
            s["likes"][uri] = {
                "author": au,
                "subject": {"uri": sub_uri,
                            "cid": s["posts"][sub_uri]["cid"]},
                "createdAt": l.get("createdAt", _now_iso()),
            }
        for r in reposts or []:
            au = _resolve_actor(s, r.get("author") or "")
            sub_uri = r.get("subject")
            if isinstance(sub_uri, dict):
                sub_uri = sub_uri.get("uri")
            if not (au and sub_uri and sub_uri in s["posts"]):
                continue
            rkey = _new_record_key()
            uri = _at_uri(au, "app.bsky.feed.repost", rkey)
            s["reposts"][uri] = {
                "author": au,
                "subject": {"uri": sub_uri,
                            "cid": s["posts"][sub_uri]["cid"]},
                "createdAt": r.get("createdAt", _now_iso()),
            }
        for b in blocks or []:
            au = _resolve_actor(s, b.get("author") or "")
            sub = _resolve_actor(s, b.get("subject") or "")
            if not (au and sub):
                continue
            rkey = _new_record_key()
            uri = _at_uri(au, "app.bsky.graph.block", rkey)
            s["blocks"][uri] = {"author": au, "subject": sub,
                                "createdAt": b.get("createdAt", _now_iso())}
        for m in mutes or []:
            au = _resolve_actor(s, m.get("actor") or "")
            tgt = _resolve_actor(s, m.get("target") or "")
            if au and tgt and not any(mm["actor_did"] == au
                                      and mm["target_did"] == tgt
                                      for mm in s["mutes"]):
                s["mutes"].append({"actor_did": au, "target_did": tgt,
                                   "createdAt": _now_iso()})
        for n in notifications or []:
            recipient = _resolve_actor(s, n.get("recipient") or "")
            author = _resolve_actor(s, n.get("author") or "")
            if not (recipient and author):
                continue
            _push_notification(s, recipient, author,
                               n.get("reason", "mention"),
                               record_uri=n.get("uri") or "",
                               subject_uri=n.get("subject_uri") or "")
        if preferences is not None:
            s["preferences"] = list(preferences)
        _record(s, "debug_seed",
                counts={
                    "actors": len(actors or []),
                    "posts": len(posts or []),
                    "follows": len(follows or []),
                    "likes": len(likes or []),
                    "reposts": len(reposts or []),
                    "blocks": len(blocks or []),
                    "mutes": len(mutes or []),
                    "notifications": len(notifications or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "self": s["self"],
            "actor_dids": list(s["actors"].keys()),
            "post_uris": list(s["posts"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
