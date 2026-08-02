"""Discord mock MCP server (Discord REST API v10).

Mirrors the Discord REST API v10 (https://discord.com/developers/docs/reference)
endpoint-for-endpoint for the chat-and-guild surface the synth dataset
exercises. Every tool is named after a Discord REST endpoint, accepts
parameter names that mirror the upstream query/body fields, and returns
the canonical Discord JSON resource shape (Guild, Channel, Message,
User, Member, Role, Thread) — not a wrapper envelope.

Implemented tool surface:

  Guilds
    list_guilds, get_guild, list_guild_channels,
    list_guild_members, get_guild_member, list_guild_roles,
    add_member_role, list_active_threads
  Channels
    get_channel, create_channel, modify_channel, delete_channel
  Messages
    list_messages, get_message, create_message, edit_message,
    delete_message
  Reactions
    add_reaction, remove_reaction, list_reactions
  Threads
    create_thread_from_message, create_thread
  Users / DMs
    get_user, get_current_user, create_dm
  Mock-only debug helpers
    mock_debug_state, mock_debug_seed_guild, mock_debug_seed_channel,
    mock_debug_seed_user, mock_debug_seed_member, mock_debug_seed_message,
    mock_debug_seed_role

Snowflake ids: 17-19 digit decimal strings encoding
`(time_ms - DISCORD_EPOCH) << 22 | worker << 17 | process << 12 |
increment`. `DISCORD_EPOCH = 1420070400000` (2015-01-01T00:00:00Z).
Every public id (guild, channel, message, user, role, thread, custom
emoji) is minted via `_gen_snowflake(state)` so the persisted state and
the values agent / verifier observe stay consistent across calls.

State file: `$DISCORD_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/discord_mock`). Optional `DISCORD_MOCK_SEED_PATH` preloads
state when no `state.json` exists yet. Every call (including reads)
appends to `state["calls"]` so verifiers can replay the trace.

Deliberately out of scope (the real API has these; the mock doesn't):
  - Gateway / WebSocket events, voice connections
  - Webhooks, Interactions, Slash commands, Auto-moderation
  - Stage instances, Scheduled events, GuildPreview, Invites
  - Audit log, integrations, Stickers, Application Commands

Bot identity: every `create_message` is authored by `state["self"]`
(the seeded bot user, `bot: true`) — mirroring real Discord where the
bot token authenticates the caller.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_EPOCH = 1_420_070_400_000  # ms (2015-01-01T00:00:00Z)
DEFAULT_BOT_ID = "100000000000000001"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "DISCORD_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/discord_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _empty_state() -> dict:
    state: dict[str, Any] = {
        "self": {
            "id": DEFAULT_BOT_ID,
            "username": "mockbot",
            "discriminator": "0",
            "global_name": "Mock Bot",
            "avatar": None,
            "bot": True,
            "system": False,
            "verified": True,
            "email": None,
            "locale": "en-US",
            "public_flags": 0,
        },
        "guilds": {},
        "channels": {},
        "messages": {},
        "users": {},
        "members": {},
        "roles": {},
        "threads": {},
        "dms": {},
        "next_snowflake": {
            "worker": 1, "process": 1, "increment": 0,
            "last_ms": DISCORD_EPOCH,
        },
        "calls": [],
    }
    state["users"][DEFAULT_BOT_ID] = dict(state["self"])
    return state


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("DISCORD_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _iso_now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Snowflake minter
# ---------------------------------------------------------------------------

def _gen_snowflake(state: dict) -> str:
    """Mint the next snowflake id from the persisted counter.

    Same layout as Discord's real ids: 42-bit relative ms timestamp,
    5-bit worker, 5-bit process, 12-bit increment.
    """
    bucket = state.setdefault("next_snowflake", {
        "worker": 1, "process": 1, "increment": 0,
        "last_ms": DISCORD_EPOCH,
    })
    now = _now_ms()
    if now <= bucket.get("last_ms", DISCORD_EPOCH):
        bucket["increment"] = int(bucket.get("increment", 0)) + 1
        if bucket["increment"] >= (1 << 12):
            bucket["increment"] = 0
            bucket["last_ms"] = int(bucket.get("last_ms", DISCORD_EPOCH)) + 1
        now = bucket["last_ms"]
    else:
        bucket["increment"] = 0
        bucket["last_ms"] = now
    worker = int(bucket.get("worker", 1)) & 0x1F
    process = int(bucket.get("process", 1)) & 0x1F
    inc = int(bucket.get("increment", 0)) & 0xFFF
    ms_part = (now - DISCORD_EPOCH) & ((1 << 42) - 1)
    sid = (ms_part << 22) | (worker << 17) | (process << 12) | inc
    return str(sid)


# ---------------------------------------------------------------------------
# Error envelopes (Discord API shape)
# ---------------------------------------------------------------------------

def _api_error(code: int, message: str, *, http: int = 404) -> dict:
    """Discord API error envelope: {"code", "message"} with optional
    HTTP-status hint. The real API surfaces {code, message, errors?}
    inside a non-2xx HTTP response — we return it as the tool result so
    callers can introspect."""
    return {"code": code, "message": message, "_http_status": http}


# ---------------------------------------------------------------------------
# Resolvers / view helpers
# ---------------------------------------------------------------------------

def _resolve_user_ref(state: dict, ref: str) -> str | None:
    """Accept '@me', a snowflake, or a username; return a user id."""
    if not ref:
        return None
    if ref == "@me":
        return state["self"]["id"]
    if ref in state["users"]:
        return ref
    # username fallback (no `#` discriminator in modern Discord)
    for uid, u in state["users"].items():
        if u.get("username") == ref or u.get("global_name") == ref:
            return uid
    return None


def _user_view(state: dict, user_id: str) -> dict:
    """Return a public User object — strip mock-internal underscores."""
    u = state["users"].get(user_id)
    if not u:
        return {"id": user_id, "username": "unknown",
                "discriminator": "0", "global_name": None,
                "avatar": None, "bot": False}
    return {k: v for k, v in u.items() if not k.startswith("_")}


def _author_view(state: dict, user_id: str) -> dict:
    """Message-author shape: same as a User but with bot flag forced."""
    u = _user_view(state, user_id)
    u["bot"] = bool(u.get("bot", False))
    return u


def _public_role(role: dict) -> dict:
    return {k: v for k, v in role.items() if not k.startswith("_")}


def _public_member(state: dict, member: dict) -> dict:
    """Member view embeds a full User object on `user`."""
    uid = member.get("_user_id") or member.get("user", {}).get("id")
    out = {k: v for k, v in member.items() if not k.startswith("_")}
    out["user"] = _user_view(state, uid)
    return out


def _public_channel(channel: dict) -> dict:
    return {k: v for k, v in channel.items() if not k.startswith("_")}


def _public_message(state: dict, msg: dict) -> dict:
    """Resolve author + referenced_message; strip mock-internal keys."""
    out = {k: v for k, v in msg.items() if not k.startswith("_")}
    aid = msg.get("author", {}).get("id")
    if aid:
        out["author"] = _author_view(state, aid)
    ref = msg.get("message_reference")
    if ref and ref.get("message_id"):
        cid = ref.get("channel_id")
        target = None
        for m in state["messages"].get(cid, []):
            if m.get("id") == ref["message_id"]:
                target = m
                break
        if target is not None:
            out["referenced_message"] = _public_message(state, target)
    return out


def _public_guild(state: dict, guild: dict) -> dict:
    """Guild view inflates channels/roles/members lists when callers
    request them via get_guild (we keep id-arrays in state)."""
    out = dict(guild)
    # leave id arrays as-is (Discord's actual GET /guilds/{id} returns
    # roles[] inline as full objects but the channels/members are paged;
    # we follow get_guild's documented behaviour of returning full
    # role objects plus id arrays for the rest).
    out["roles"] = [_public_role(state["roles"].get(guild["id"], {}).get(rid, {}))
                    for rid in guild.get("roles", [])
                    if rid in state["roles"].get(guild["id"], {})]
    return out


# ---------------------------------------------------------------------------
# Snowflake comparisons for before/after/around pagination
# ---------------------------------------------------------------------------

def _snowflake_int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _paginate_messages(msgs: list[dict], *,
                       before: str | None,
                       after: str | None,
                       around: str | None,
                       limit: int) -> list[dict]:
    """Mirror Discord's message-pagination semantics.

    - `before`: messages with id < before, newest first
    - `after`: messages with id > after, oldest first then truncated
      (Discord returns newest-first overall but the page is anchored
      on after; we follow Discord's docs)
    - `around`: limit/2 either side of around
    """
    sorted_desc = sorted(msgs, key=lambda m: _snowflake_int(m["id"]),
                         reverse=True)
    if around:
        a = _snowflake_int(around)
        before_block = [m for m in sorted_desc
                        if _snowflake_int(m["id"]) > a]
        at_block = [m for m in sorted_desc
                    if _snowflake_int(m["id"]) == a]
        after_block = [m for m in sorted_desc
                       if _snowflake_int(m["id"]) < a]
        half = max(1, limit // 2)
        page = (before_block[-half:] + at_block + after_block[:half])
        return page[:limit]
    if after:
        a = _snowflake_int(after)
        filt = [m for m in sorted_desc if _snowflake_int(m["id"]) > a]
        # Discord returns newest first even with `after`.
        return filt[:limit]
    if before:
        b = _snowflake_int(before)
        filt = [m for m in sorted_desc if _snowflake_int(m["id"]) < b]
        return filt[:limit]
    return sorted_desc[:limit]


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("discord-mock")


# ---------------------------------------------------------------------------
# Guilds
# ---------------------------------------------------------------------------

@mcp.tool(name="list_guilds")
def list_guilds() -> list:
    """Discord REST: GET /users/@me/guilds — partial guild objects the
    current (bot) user is in. Returns a JSON array."""
    with _lock():
        s = _load_state()
        out = []
        for gid, g in s["guilds"].items():
            out.append({
                "id": gid,
                "name": g.get("name", ""),
                "icon": g.get("icon"),
                "owner": g.get("owner_id") == s["self"]["id"],
                "permissions": g.get("permissions", "0"),
                "features": list(g.get("features", [])),
            })
        _record(s, "list_guilds", count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="get_guild")
def get_guild(guildId: str) -> dict:
    """Discord REST: GET /guilds/{guild.id} — full Guild object."""
    with _lock():
        s = _load_state()
        g = s["guilds"].get(guildId)
        if not g:
            _record(s, "get_guild", guild_id=guildId, result="not_found")
            _save_state(s)
            return _api_error(10004, "Unknown Guild")
        _record(s, "get_guild", guild_id=guildId)
        _save_state(s)
        return _public_guild(s, g)


@mcp.tool(name="list_guild_channels")
def list_guild_channels(guildId: str) -> list:
    """Discord REST: GET /guilds/{guild.id}/channels — array of Channel
    objects (does not include threads)."""
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "list_guild_channels", guild_id=guildId,
                    result="not_found")
            _save_state(s)
            return [_api_error(10004, "Unknown Guild")]
        out = []
        for cid in s["guilds"][guildId].get("channels", []):
            ch = s["channels"].get(cid)
            if ch is None:
                continue
            # threads are returned by list_active_threads, not here
            if int(ch.get("type", 0)) in (10, 11, 12):
                continue
            out.append(_public_channel(ch))
        out.sort(key=lambda c: (c.get("position", 0),
                                _snowflake_int(c.get("id", "0"))))
        _record(s, "list_guild_channels", guild_id=guildId, count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="list_guild_members")
def list_guild_members(guildId: str, limit: int = 1, after: str = "0") -> list:
    """Discord REST: GET /guilds/{guild.id}/members?limit={limit}&after={after}.
    Returns an array of Member objects sorted by user id ascending."""
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "list_guild_members", guild_id=guildId,
                    result="not_found")
            _save_state(s)
            return [_api_error(10004, "Unknown Guild")]
        if limit <= 0:
            limit = 1
        if limit > 1000:
            limit = 1000
        after_int = _snowflake_int(after or "0")
        members = s["members"].get(guildId, {})
        ids = sorted(members.keys(),
                     key=_snowflake_int)
        ids = [uid for uid in ids if _snowflake_int(uid) > after_int]
        page = ids[:limit]
        out = [_public_member(s, members[uid]) for uid in page]
        _record(s, "list_guild_members", guild_id=guildId, count=len(out),
                after=after)
        _save_state(s)
        return out


@mcp.tool(name="get_guild_member")
def get_guild_member(guildId: str, userId: str) -> dict:
    """Discord REST: GET /guilds/{guild.id}/members/{user.id}."""
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "get_guild_member", guild_id=guildId,
                    result="guild_not_found")
            _save_state(s)
            return _api_error(10004, "Unknown Guild")
        uid = _resolve_user_ref(s, userId) or userId
        m = s["members"].get(guildId, {}).get(uid)
        if not m:
            _record(s, "get_guild_member", guild_id=guildId,
                    user_id=userId, result="not_found")
            _save_state(s)
            return _api_error(10007, "Unknown Member")
        _record(s, "get_guild_member", guild_id=guildId, user_id=uid)
        _save_state(s)
        return _public_member(s, m)


@mcp.tool(name="list_guild_roles")
def list_guild_roles(guildId: str) -> list:
    """Discord REST: GET /guilds/{guild.id}/roles — array of Role objects."""
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "list_guild_roles", guild_id=guildId,
                    result="not_found")
            _save_state(s)
            return [_api_error(10004, "Unknown Guild")]
        out = [_public_role(r) for r in s["roles"].get(guildId, {}).values()]
        out.sort(key=lambda r: r.get("position", 0))
        _record(s, "list_guild_roles", guild_id=guildId, count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="add_member_role")
def add_member_role(guildId: str, userId: str, roleId: str) -> dict:
    """Discord REST: PUT /guilds/{guild.id}/members/{user.id}/roles/{role.id}.
    Returns the updated Member."""
    with _lock():
        s = _load_state()
        uid = _resolve_user_ref(s, userId) or userId
        if guildId not in s["guilds"]:
            _record(s, "add_member_role", guild_id=guildId,
                    result="guild_not_found")
            _save_state(s)
            return _api_error(10004, "Unknown Guild")
        m = s["members"].get(guildId, {}).get(uid)
        if not m:
            _record(s, "add_member_role", guild_id=guildId, user_id=userId,
                    result="member_not_found")
            _save_state(s)
            return _api_error(10007, "Unknown Member")
        if roleId not in s["roles"].get(guildId, {}):
            _record(s, "add_member_role", guild_id=guildId, role_id=roleId,
                    result="role_not_found")
            _save_state(s)
            return _api_error(10011, "Unknown Role")
        if roleId not in m.get("roles", []):
            m.setdefault("roles", []).append(roleId)
        _record(s, "add_member_role", guild_id=guildId,
                user_id=uid, role_id=roleId)
        _save_state(s)
        return _public_member(s, m)


@mcp.tool(name="list_active_threads")
def list_active_threads(guildId: str) -> dict:
    """Discord REST: GET /guilds/{guild.id}/threads/active.
    Returns {threads:[Channel], members:[ThreadMember]}."""
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "list_active_threads", guild_id=guildId,
                    result="not_found")
            _save_state(s)
            return _api_error(10004, "Unknown Guild")
        threads = []
        members = []
        me = s["self"]["id"]
        for tid, ch in s["channels"].items():
            if int(ch.get("type", 0)) not in (10, 11, 12):
                continue
            if ch.get("guild_id") != guildId:
                continue
            meta = ch.get("thread_metadata", {})
            if meta.get("archived"):
                continue
            threads.append(_public_channel(ch))
            tm = s["threads"].get(tid, {}).get("members", {}).get(me)
            if tm:
                members.append(tm)
        _record(s, "list_active_threads", guild_id=guildId,
                count=len(threads))
        _save_state(s)
        return {"threads": threads, "members": members}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@mcp.tool(name="get_channel")
def get_channel(channelId: str) -> dict:
    """Discord REST: GET /channels/{channel.id} — Channel object."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "get_channel", channel_id=channelId,
                    result="not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        _record(s, "get_channel", channel_id=channelId)
        _save_state(s)
        return _public_channel(ch)


_VALID_CREATE_TYPES = {0, 2, 4, 5, 11, 13, 15}


@mcp.tool(name="create_channel")
def create_channel(guildId: str, name: str,
                   type: int = 0,
                   parent_id: str | None = None,
                   topic: str | None = None,
                   nsfw: bool = False) -> dict:
    """Discord REST: POST /guilds/{guild.id}/channels — create a guild
    channel.

    `type` is the integer channel-type enum:
      0=GUILD_TEXT, 2=GUILD_VOICE, 4=GUILD_CATEGORY, 5=GUILD_ANNOUNCEMENT,
      11=PUBLIC_THREAD, 13=GUILD_STAGE_VOICE, 15=GUILD_FORUM.
    """
    with _lock():
        s = _load_state()
        if guildId not in s["guilds"]:
            _record(s, "create_channel", guild_id=guildId,
                    result="guild_not_found")
            _save_state(s)
            return _api_error(10004, "Unknown Guild")
        if int(type) not in _VALID_CREATE_TYPES:
            _record(s, "create_channel", guild_id=guildId,
                    result="invalid_type", type=type)
            _save_state(s)
            return _api_error(50035, f"Invalid channel type: {type}",
                              http=400)
        if parent_id and parent_id not in s["channels"]:
            _record(s, "create_channel", guild_id=guildId,
                    parent_id=parent_id, result="parent_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Parent Channel", http=400)
        cid = _gen_snowflake(s)
        ch: dict[str, Any] = {
            "id": cid,
            "type": int(type),
            "guild_id": guildId,
            "position": len(s["guilds"][guildId].get("channels", [])),
            "permission_overwrites": [],
            "name": name,
            "topic": topic,
            "nsfw": bool(nsfw),
            "last_message_id": None,
            "rate_limit_per_user": 0,
            "parent_id": parent_id,
        }
        if int(type) in (2, 13):
            ch["bitrate"] = 64000
            ch["user_limit"] = 0
        s["channels"][cid] = ch
        s["messages"][cid] = []
        s["guilds"][guildId].setdefault("channels", []).append(cid)
        _record(s, "create_channel", guild_id=guildId, channel_id=cid,
                name=name, type=type)
        _save_state(s)
        return _public_channel(ch)


@mcp.tool(name="modify_channel")
def modify_channel(channelId: str,
                   name: str | None = None,
                   topic: str | None = None,
                   nsfw: bool | None = None,
                   parent_id: str | None = None,
                   position: int | None = None) -> dict:
    """Discord REST: PATCH /channels/{channel.id} — update a channel's
    metadata."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "modify_channel", channel_id=channelId,
                    result="not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        if name is not None:
            ch["name"] = name
        if topic is not None:
            ch["topic"] = topic
        if nsfw is not None:
            ch["nsfw"] = bool(nsfw)
        if parent_id is not None:
            ch["parent_id"] = parent_id or None
        if position is not None:
            ch["position"] = int(position)
        _record(s, "modify_channel", channel_id=channelId)
        _save_state(s)
        return _public_channel(ch)


@mcp.tool(name="delete_channel")
def delete_channel(channelId: str) -> dict:
    """Discord REST: DELETE /channels/{channel.id} — delete a channel.
    Returns the deleted Channel object."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "delete_channel", channel_id=channelId,
                    result="not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        gid = ch.get("guild_id")
        if gid and gid in s["guilds"]:
            chans = s["guilds"][gid].get("channels", [])
            if channelId in chans:
                chans.remove(channelId)
        s["channels"].pop(channelId, None)
        s["messages"].pop(channelId, None)
        s["threads"].pop(channelId, None)
        _record(s, "delete_channel", channel_id=channelId)
        _save_state(s)
        return _public_channel(ch)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@mcp.tool(name="list_messages")
def list_messages(channelId: str,
                  limit: int = 50,
                  before: str | None = None,
                  after: str | None = None,
                  around: str | None = None) -> list:
    """Discord REST: GET /channels/{channel.id}/messages.
    Returns up to `limit` messages (default 50, max 100), newest first."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "list_messages", channel_id=channelId,
                    result="not_found")
            _save_state(s)
            return [_api_error(10003, "Unknown Channel")]
        if limit <= 0:
            limit = 50
        if limit > 100:
            limit = 100
        msgs = s["messages"].get(channelId, [])
        page = _paginate_messages(msgs, before=before, after=after,
                                  around=around, limit=limit)
        out = [_public_message(s, m) for m in page]
        _record(s, "list_messages", channel_id=channelId,
                count=len(out), limit=limit)
        _save_state(s)
        return out


@mcp.tool(name="get_message")
def get_message(channelId: str, messageId: str) -> dict:
    """Discord REST: GET /channels/{channel.id}/messages/{message.id}."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "get_message", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        m = next((mm for mm in s["messages"].get(channelId, [])
                  if mm.get("id") == messageId), None)
        if not m:
            _record(s, "get_message", channel_id=channelId,
                    message_id=messageId, result="not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        _record(s, "get_message", channel_id=channelId,
                message_id=messageId)
        _save_state(s)
        return _public_message(s, m)


@mcp.tool(name="create_message")
def create_message(channelId: str,
                   content: str = "",
                   tts: bool = False,
                   embeds: list | None = None,
                   message_reference: dict | None = None,
                   allowed_mentions: dict | None = None,
                   flags: int = 0) -> dict:
    """Discord REST: POST /channels/{channel.id}/messages — post a
    message. The bot user (state['self']) is the author."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "create_message", channel_id=channelId,
                    result="not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        if not (content or embeds):
            _record(s, "create_message", channel_id=channelId,
                    result="empty_message")
            _save_state(s)
            return _api_error(50006, "Cannot send an empty message",
                              http=400)
        mid = _gen_snowflake(s)
        gid = ch.get("guild_id")
        msg: dict[str, Any] = {
            "id": mid,
            "channel_id": channelId,
            "author": {"id": s["self"]["id"]},
            "content": content or "",
            "timestamp": _iso_now(),
            "edited_timestamp": None,
            "tts": bool(tts),
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": list(embeds or []),
            "reactions": [],
            "pinned": False,
            "type": 19 if message_reference else 0,  # 19 = REPLY
            "flags": int(flags or 0),
        }
        if gid:
            msg["guild_id"] = gid
        if message_reference:
            ref = {
                "message_id": message_reference.get("message_id"),
                "channel_id": message_reference.get("channel_id") or channelId,
            }
            if message_reference.get("guild_id"):
                ref["guild_id"] = message_reference["guild_id"]
            msg["message_reference"] = ref
        if allowed_mentions is not None:
            msg["allowed_mentions"] = allowed_mentions
        s["messages"].setdefault(channelId, []).append(msg)
        ch["last_message_id"] = mid
        _record(s, "create_message", channel_id=channelId,
                message_id=mid, content_len=len(content or ""))
        _save_state(s)
        return _public_message(s, msg)


@mcp.tool(name="edit_message")
def edit_message(channelId: str, messageId: str,
                 content: str | None = None,
                 embeds: list | None = None) -> dict:
    """Discord REST: PATCH /channels/{channel.id}/messages/{message.id}."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "edit_message", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        m = next((mm for mm in s["messages"].get(channelId, [])
                  if mm.get("id") == messageId), None)
        if not m:
            _record(s, "edit_message", channel_id=channelId,
                    message_id=messageId, result="not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        if m.get("author", {}).get("id") != s["self"]["id"]:
            _record(s, "edit_message", channel_id=channelId,
                    message_id=messageId,
                    result="cannot_edit_others")
            _save_state(s)
            return _api_error(50005, "Cannot edit a message authored "
                              "by another user", http=403)
        if content is not None:
            m["content"] = content
        if embeds is not None:
            m["embeds"] = list(embeds)
        m["edited_timestamp"] = _iso_now()
        _record(s, "edit_message", channel_id=channelId,
                message_id=messageId)
        _save_state(s)
        return _public_message(s, m)


@mcp.tool(name="delete_message")
def delete_message(channelId: str, messageId: str) -> dict:
    """Discord REST: DELETE /channels/{channel.id}/messages/{message.id}.
    Returns an empty dict (HTTP 204 on the real API)."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "delete_message", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        msgs = s["messages"].get(channelId, [])
        before = len(msgs)
        s["messages"][channelId] = [mm for mm in msgs
                                    if mm.get("id") != messageId]
        if len(s["messages"][channelId]) == before:
            _record(s, "delete_message", channel_id=channelId,
                    message_id=messageId, result="not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        _record(s, "delete_message", channel_id=channelId,
                message_id=messageId)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

def _parse_emoji(emoji: str) -> dict:
    """Discord URL-encodes emoji as either `name` (unicode) or
    `name:id` (custom). Return the Reaction.emoji object."""
    if ":" in emoji:
        name, _, eid = emoji.partition(":")
        return {"id": eid or None, "name": name, "animated": False}
    return {"id": None, "name": emoji, "animated": False}


def _find_message(state: dict, channel_id: str,
                  message_id: str) -> dict | None:
    return next((m for m in state["messages"].get(channel_id, [])
                 if m.get("id") == message_id), None)


def _emoji_key(emoji_obj: dict) -> str:
    if emoji_obj.get("id"):
        return f"{emoji_obj.get('name','')}:{emoji_obj['id']}"
    return emoji_obj.get("name", "")


@mcp.tool(name="add_reaction")
def add_reaction(channelId: str, messageId: str, emoji: str) -> dict:
    """Discord REST: PUT /channels/{channel.id}/messages/{message.id}/
    reactions/{emoji}/@me. Returns an empty dict (HTTP 204)."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "add_reaction", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        msg = _find_message(s, channelId, messageId)
        if not msg:
            _record(s, "add_reaction", channel_id=channelId,
                    message_id=messageId, result="message_not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        emoji_obj = _parse_emoji(emoji)
        key = _emoji_key(emoji_obj)
        me = s["self"]["id"]
        rs = msg.setdefault("reactions", [])
        entry = next((r for r in rs if _emoji_key(r["emoji"]) == key), None)
        if entry is None:
            rs.append({"emoji": emoji_obj, "count": 1, "me": True,
                       "_users": [me]})
        else:
            users = entry.setdefault("_users", [])
            if me not in users:
                users.append(me)
                entry["count"] = len(users)
            entry["me"] = me in users
        _record(s, "add_reaction", channel_id=channelId,
                message_id=messageId, emoji=emoji)
        _save_state(s)
        return {}


@mcp.tool(name="remove_reaction")
def remove_reaction(channelId: str, messageId: str, emoji: str,
                    userId: str = "@me") -> dict:
    """Discord REST: DELETE /channels/{channel.id}/messages/{message.id}/
    reactions/{emoji}/{userId|@me}."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "remove_reaction", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        msg = _find_message(s, channelId, messageId)
        if not msg:
            _record(s, "remove_reaction", channel_id=channelId,
                    message_id=messageId, result="message_not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        emoji_obj = _parse_emoji(emoji)
        key = _emoji_key(emoji_obj)
        rs = msg.setdefault("reactions", [])
        entry = next((r for r in rs if _emoji_key(r["emoji"]) == key), None)
        if entry is None:
            _record(s, "remove_reaction", channel_id=channelId,
                    message_id=messageId, emoji=emoji,
                    result="reaction_not_found")
            _save_state(s)
            return _api_error(10014, "Unknown Reaction")
        target = s["self"]["id"] if userId == "@me" else userId
        users = entry.setdefault("_users", [])
        if target in users:
            users.remove(target)
            entry["count"] = len(users)
            entry["me"] = s["self"]["id"] in users
            if not users:
                rs.remove(entry)
        _record(s, "remove_reaction", channel_id=channelId,
                message_id=messageId, emoji=emoji, user_id=target)
        _save_state(s)
        return {}


@mcp.tool(name="list_reactions")
def list_reactions(channelId: str, messageId: str, emoji: str,
                   limit: int = 25) -> list:
    """Discord REST: GET /channels/{channel.id}/messages/{message.id}/
    reactions/{emoji}. Returns the users who reacted with that emoji."""
    with _lock():
        s = _load_state()
        if channelId not in s["channels"]:
            _record(s, "list_reactions", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return [_api_error(10003, "Unknown Channel")]
        msg = _find_message(s, channelId, messageId)
        if not msg:
            _record(s, "list_reactions", channel_id=channelId,
                    message_id=messageId, result="message_not_found")
            _save_state(s)
            return [_api_error(10008, "Unknown Message")]
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        emoji_obj = _parse_emoji(emoji)
        key = _emoji_key(emoji_obj)
        entry = next((r for r in msg.get("reactions", [])
                      if _emoji_key(r["emoji"]) == key), None)
        users = [] if entry is None else list(entry.get("_users", []))
        out = [_user_view(s, uid) for uid in users[:limit]]
        _record(s, "list_reactions", channel_id=channelId,
                message_id=messageId, emoji=emoji, count=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

_VALID_AUTO_ARCHIVE = {60, 1440, 4320, 10080}


def _make_thread(state: dict, *, name: str, parent_channel: dict,
                 thread_type: int, auto_archive_duration: int,
                 starter_message_id: str | None) -> dict:
    tid = _gen_snowflake(state)
    if auto_archive_duration not in _VALID_AUTO_ARCHIVE:
        auto_archive_duration = 1440
    now = _iso_now()
    thread: dict[str, Any] = {
        "id": tid,
        "type": thread_type,
        "guild_id": parent_channel.get("guild_id"),
        "parent_id": parent_channel["id"],
        "name": name,
        "owner_id": state["self"]["id"],
        "last_message_id": None,
        "rate_limit_per_user": 0,
        "message_count": 0,
        "member_count": 1,
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": auto_archive_duration,
            "archive_timestamp": now,
            "locked": False,
            "invitable": True,
            "create_timestamp": now,
        },
    }
    state["channels"][tid] = thread
    state["messages"][tid] = []
    state["threads"][tid] = {
        "starter_message_id": starter_message_id,
        "members": {
            state["self"]["id"]: {
                "id": tid,
                "user_id": state["self"]["id"],
                "join_timestamp": now,
                "flags": 0,
            },
        },
    }
    if thread["guild_id"] and thread["guild_id"] in state["guilds"]:
        state["guilds"][thread["guild_id"]].setdefault(
            "channels", []).append(tid)
    return thread


@mcp.tool(name="create_thread_from_message")
def create_thread_from_message(channelId: str, messageId: str,
                               name: str,
                               auto_archive_duration: int = 1440) -> dict:
    """Discord REST: POST /channels/{channel.id}/messages/{message.id}/threads.
    Creates a PUBLIC_THREAD (type 11) anchored on the given message."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "create_thread_from_message",
                    channel_id=channelId, result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        msg = _find_message(s, channelId, messageId)
        if not msg:
            _record(s, "create_thread_from_message",
                    channel_id=channelId, message_id=messageId,
                    result="message_not_found")
            _save_state(s)
            return _api_error(10008, "Unknown Message")
        thread = _make_thread(
            s, name=name, parent_channel=ch, thread_type=11,
            auto_archive_duration=int(auto_archive_duration),
            starter_message_id=messageId)
        _record(s, "create_thread_from_message", channel_id=channelId,
                message_id=messageId, thread_id=thread["id"])
        _save_state(s)
        return _public_channel(thread)


@mcp.tool(name="create_thread")
def create_thread(channelId: str, name: str,
                  type: int = 11,
                  auto_archive_duration: int = 1440) -> dict:
    """Discord REST: POST /channels/{channel.id}/threads — start a
    thread without an anchor message. Default `type` is 11
    (PUBLIC_THREAD); 12 = PRIVATE_THREAD."""
    with _lock():
        s = _load_state()
        ch = s["channels"].get(channelId)
        if not ch:
            _record(s, "create_thread", channel_id=channelId,
                    result="channel_not_found")
            _save_state(s)
            return _api_error(10003, "Unknown Channel")
        if int(type) not in (10, 11, 12):
            _record(s, "create_thread", channel_id=channelId,
                    result="invalid_type", type=type)
            _save_state(s)
            return _api_error(50035, f"Invalid thread type: {type}",
                              http=400)
        thread = _make_thread(
            s, name=name, parent_channel=ch, thread_type=int(type),
            auto_archive_duration=int(auto_archive_duration),
            starter_message_id=None)
        _record(s, "create_thread", channel_id=channelId,
                thread_id=thread["id"])
        _save_state(s)
        return _public_channel(thread)


# ---------------------------------------------------------------------------
# Users / DMs
# ---------------------------------------------------------------------------

@mcp.tool(name="get_user")
def get_user(userId: str) -> dict:
    """Discord REST: GET /users/{user.id} — User object."""
    with _lock():
        s = _load_state()
        uid = _resolve_user_ref(s, userId) or userId
        if uid not in s["users"]:
            _record(s, "get_user", user_id=userId, result="not_found")
            _save_state(s)
            return _api_error(10013, "Unknown User")
        _record(s, "get_user", user_id=uid)
        _save_state(s)
        return _user_view(s, uid)


@mcp.tool(name="get_current_user")
def get_current_user() -> dict:
    """Discord REST: GET /users/@me — the bot's own User object."""
    with _lock():
        s = _load_state()
        _record(s, "get_current_user")
        _save_state(s)
        return _user_view(s, s["self"]["id"])


@mcp.tool(name="create_dm")
def create_dm(recipient_id: str) -> dict:
    """Discord REST: POST /users/@me/channels — open (or return existing)
    DM channel with `recipient_id`. Returns a DM Channel object
    (type=1)."""
    with _lock():
        s = _load_state()
        uid = _resolve_user_ref(s, recipient_id) or recipient_id
        if uid not in s["users"]:
            _record(s, "create_dm", recipient_id=recipient_id,
                    result="user_not_found")
            _save_state(s)
            return _api_error(10013, "Unknown User")
        existing = s["dms"].get(uid)
        if existing and existing in s["channels"]:
            _record(s, "create_dm", recipient_id=uid,
                    channel_id=existing, result="existing")
            _save_state(s)
            return _public_channel(s["channels"][existing])
        cid = _gen_snowflake(s)
        dm = {
            "id": cid,
            "type": 1,
            "last_message_id": None,
            "recipients": [_user_view(s, uid)],
        }
        s["channels"][cid] = dm
        s["messages"][cid] = []
        s["dms"][uid] = cid
        _record(s, "create_dm", recipient_id=uid, channel_id=cid)
        _save_state(s)
        return _public_channel(dm)


# ---------------------------------------------------------------------------
# Mock-only debug helpers (not part of the real surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (verifier
    introspection). Not in the real Discord API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_user")
def mock_debug_seed_user(id: str | None = None,
                         username: str = "user",
                         global_name: str | None = None,
                         discriminator: str = "0",
                         bot: bool = False,
                         avatar: str | None = None) -> dict:
    """Mock-only: insert a User into state. Returns the User."""
    with _lock():
        s = _load_state()
        uid = id or _gen_snowflake(s)
        u = {
            "id": uid,
            "username": username,
            "discriminator": discriminator,
            "global_name": global_name if global_name is not None else username,
            "avatar": avatar,
            "bot": bool(bot),
            "system": False,
            "verified": True,
            "email": None,
            "locale": "en-US",
            "public_flags": 0,
        }
        s["users"][uid] = u
        _record(s, "debug_seed_user", user_id=uid, username=username)
        _save_state(s)
        return u


@mcp.tool(name="mock_debug_seed_guild")
def mock_debug_seed_guild(id: str | None = None,
                          name: str = "Mock Guild",
                          owner_id: str | None = None) -> dict:
    """Mock-only: insert a Guild + the @everyone role into state."""
    with _lock():
        s = _load_state()
        gid = id or _gen_snowflake(s)
        owner = owner_id or s["self"]["id"]
        g = {
            "id": gid, "name": name, "icon": None, "splash": None,
            "owner_id": owner, "region": "deprecated",
            "permissions": "0", "features": [],
            "verification_level": 0,
            "default_message_notifications": 0,
            "explicit_content_filter": 0,
            "roles": [gid], "channels": [], "members": [],
            "member_count": 0, "mfa_level": 0,
            "system_channel_id": None, "premium_tier": 0,
            "premium_subscription_count": 0,
            "preferred_locale": "en-US",
        }
        s["guilds"][gid] = g
        s["roles"].setdefault(gid, {})[gid] = {
            "id": gid, "name": "@everyone", "color": 0, "hoist": False,
            "icon": None, "unicode_emoji": None, "position": 0,
            "permissions": "0", "managed": False, "mentionable": False,
            "tags": {}, "_guild_id": gid,
        }
        s["members"].setdefault(gid, {})
        _record(s, "debug_seed_guild", guild_id=gid, name=name)
        _save_state(s)
        return g


@mcp.tool(name="mock_debug_seed_channel")
def mock_debug_seed_channel(guild_id: str | None = None,
                            id: str | None = None,
                            name: str = "general",
                            type: int = 0,
                            parent_id: str | None = None,
                            topic: str | None = None,
                            nsfw: bool = False,
                            position: int = 0) -> dict:
    """Mock-only: insert a Channel. `guild_id=None` creates a DM-like
    channel; otherwise registers the channel under that guild."""
    with _lock():
        s = _load_state()
        cid = id or _gen_snowflake(s)
        ch: dict[str, Any] = {
            "id": cid, "type": int(type), "guild_id": guild_id,
            "position": int(position),
            "permission_overwrites": [], "name": name,
            "topic": topic, "nsfw": bool(nsfw),
            "last_message_id": None, "rate_limit_per_user": 0,
            "parent_id": parent_id,
        }
        if int(type) in (2, 13):
            ch["bitrate"] = 64000
            ch["user_limit"] = 0
        s["channels"][cid] = ch
        s["messages"].setdefault(cid, [])
        if guild_id and guild_id in s["guilds"]:
            s["guilds"][guild_id].setdefault("channels", []).append(cid)
        _record(s, "debug_seed_channel", channel_id=cid, name=name)
        _save_state(s)
        return ch


@mcp.tool(name="mock_debug_seed_role")
def mock_debug_seed_role(guild_id: str,
                         id: str | None = None,
                         name: str = "role",
                         color: int = 0,
                         position: int = 1,
                         permissions: str = "0") -> dict:
    """Mock-only: insert a Role into a guild."""
    with _lock():
        s = _load_state()
        if guild_id not in s["guilds"]:
            return _api_error(10004, "Unknown Guild")
        rid = id or _gen_snowflake(s)
        role = {
            "id": rid, "name": name, "color": int(color),
            "hoist": False, "icon": None, "unicode_emoji": None,
            "position": int(position), "permissions": str(permissions),
            "managed": False, "mentionable": False, "tags": {},
            "_guild_id": guild_id,
        }
        s["roles"].setdefault(guild_id, {})[rid] = role
        if rid not in s["guilds"][guild_id].get("roles", []):
            s["guilds"][guild_id].setdefault("roles", []).append(rid)
        _record(s, "debug_seed_role", guild_id=guild_id, role_id=rid,
                name=name)
        _save_state(s)
        return _public_role(role)


@mcp.tool(name="mock_debug_seed_member")
def mock_debug_seed_member(guild_id: str, user_id: str,
                           nick: str | None = None,
                           roles: list[str] | None = None,
                           joined_at: str | None = None) -> dict:
    """Mock-only: insert a Member into a guild."""
    with _lock():
        s = _load_state()
        if guild_id not in s["guilds"]:
            return _api_error(10004, "Unknown Guild")
        if user_id not in s["users"]:
            return _api_error(10013, "Unknown User")
        member = {
            "user": {"id": user_id},
            "nick": nick,
            "avatar": None,
            "roles": list(roles or []),
            "joined_at": joined_at or _iso_now(),
            "premium_since": None,
            "deaf": False, "mute": False, "flags": 0,
            "_guild_id": guild_id, "_user_id": user_id,
        }
        s["members"].setdefault(guild_id, {})[user_id] = member
        if user_id not in s["guilds"][guild_id].get("members", []):
            s["guilds"][guild_id].setdefault("members", []).append(user_id)
            s["guilds"][guild_id]["member_count"] = (
                s["guilds"][guild_id].get("member_count", 0) + 1)
        _record(s, "debug_seed_member", guild_id=guild_id, user_id=user_id)
        _save_state(s)
        return _public_member(s, member)


@mcp.tool(name="mock_debug_seed_message")
def mock_debug_seed_message(channel_id: str,
                            author_id: str | None = None,
                            content: str = "",
                            id: str | None = None,
                            timestamp: str | None = None,
                            reactions: list[dict] | None = None) -> dict:
    """Mock-only: insert a Message into a channel."""
    with _lock():
        s = _load_state()
        if channel_id not in s["channels"]:
            return _api_error(10003, "Unknown Channel")
        mid = id or _gen_snowflake(s)
        aid = author_id or s["self"]["id"]
        ch = s["channels"][channel_id]
        msg = {
            "id": mid, "channel_id": channel_id,
            "author": {"id": aid},
            "content": content, "timestamp": timestamp or _iso_now(),
            "edited_timestamp": None, "tts": False,
            "mention_everyone": False, "mentions": [],
            "mention_roles": [], "attachments": [], "embeds": [],
            "reactions": list(reactions or []), "pinned": False,
            "type": 0, "flags": 0,
        }
        if ch.get("guild_id"):
            msg["guild_id"] = ch["guild_id"]
        s["messages"].setdefault(channel_id, []).append(msg)
        ch["last_message_id"] = mid
        _record(s, "debug_seed_message", channel_id=channel_id,
                message_id=mid)
        _save_state(s)
        return _public_message(s, msg)


if __name__ == "__main__":
    mcp.run()
