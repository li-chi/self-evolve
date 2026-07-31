"""Slack mock MCP server.

Mirrors the tool surface of `slack-mcp-server@1.1.23`
(github.com/korotovsky/slack-mcp-server), which is the registered Slack
MCP server in `mcp-atlas`'s mcp_server_template.json. That server
returns *CSV strings* (via gocsv) for list/search/history tools and
plain "Successfully ..." strings for action tools — we match those
exact return shapes so the mock is a drop-in stand-in for the real
server during rollouts. (We deliberately do NOT return Slack Web API
JSON; that would not match the registered server.)

Tools implemented (one per registration in `pkg/server/server.go`):

  Channels / DMs
    channels_list, channels_me, conversations_join, conversations_leave,
    conversations_mark
  Messages
    conversations_history, conversations_replies,
    conversations_add_message, conversations_search_messages,
    conversations_unreads
  Reactions
    reactions_add, reactions_remove
  Users
    users_search
  Usergroups
    usergroups_list, usergroups_me, usergroups_create, usergroups_update,
    usergroups_users_update
  Saved-for-later
    saved_list, saved_update, saved_clear_completed
  Attachments
    attachment_get_data

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.

State lives at `$SLACK_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/slack_mock`). Per-rollout isolation should clear the state
dir between rollouts. Optional `SLACK_MOCK_SEED_PATH` preloads state
when no state.json exists yet.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import fcntl
import io
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "SLACK_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/slack_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "workspace": {
            "id": "T0000000000",
            "name": "Mock Workspace",
            "domain": "mock",
            "url": "https://mock.slack.com/",
        },
        "self": {"id": "USELF000000", "name": "mockbot"},
        "users": {},
        "channels": {},
        "messages": {},          # channel_id -> list[message dict]
        "usergroups": {},
        "saved": [],             # list[dict] for saved_list
        "files": {},             # file_id -> {"name","mimetype","content"(base64),"size"}
        "next_id": {
            "channel": 1, "user": 1, "usergroup": 1, "file": 1,
            "ts": 1_700_000_000,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("SLACK_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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
# Helpers
# ---------------------------------------------------------------------------

def _new_channel_id(state: dict, prefix: str = "C") -> str:
    n = state["next_id"]["channel"]
    state["next_id"]["channel"] = n + 1
    return f"{prefix}{n:010d}"


def _new_user_id(state: dict) -> str:
    n = state["next_id"]["user"]
    state["next_id"]["user"] = n + 1
    return f"U{n:010d}"


def _new_usergroup_id(state: dict) -> str:
    n = state["next_id"]["usergroup"]
    state["next_id"]["usergroup"] = n + 1
    return f"S{n:010d}"


def _new_file_id(state: dict) -> str:
    n = state["next_id"]["file"]
    state["next_id"]["file"] = n + 1
    return f"F{n:010d}"


def _new_ts(state: dict) -> str:
    n = state["next_id"]["ts"]
    state["next_id"]["ts"] = n + 1
    return f"{n}.000000"


def _ts_to_rfc3339(ts: str) -> str:
    try:
        sec = float(ts)
    except (TypeError, ValueError):
        return ""
    return (datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _resolve_channel(state: dict, ref: str) -> str | None:
    """Resolve `Cxxxx`, `#name`, `name`, `@username` (DM), or `Dxxxx`
    to a canonical channel id present in state.

    Bare-name (`name` without `#`) is accepted because synthesized
    task instructions and agents both produce it naturally; rejecting
    it forces the agent to guess the project's hash-prefix convention
    and is a frequent source of would-be-passing tasks failing on
    channel-naming alone."""
    if not ref:
        return None
    if ref in state["channels"]:
        return ref
    if ref.startswith("#"):
        name = ref[1:]
        for cid, ch in state["channels"].items():
            if ch.get("name") == name:
                return cid
        return None
    # Bare channel name (no `#`, no `@`, not a `Cxxxx` id).
    if not ref.startswith("@"):
        for cid, ch in state["channels"].items():
            if ch.get("name") == ref:
                return cid
    if ref.startswith("@"):
        # DM lookup: find IM channel whose member matches the username
        username = ref[1:]
        target_uid = None
        for uid, u in state["users"].items():
            if u.get("name") == username:
                target_uid = uid
                break
        if not target_uid:
            return None
        for cid, ch in state["channels"].items():
            if ch.get("is_im") and target_uid in ch.get("members", []):
                return cid
        return None
    return None


def _resolve_user(state: dict, ref: str) -> str | None:
    if not ref:
        return None
    if ref in state["users"]:
        return ref
    if ref.startswith("@"):
        ref = ref[1:]
    for uid, u in state["users"].items():
        if u.get("name") == ref or u.get("profile", {}).get("display_name") == ref:
            return uid
    return None


def _csv_dump(rows: list[dict], columns: list[str]) -> str:
    """gocsv-style CSV: header row + values. Empty list returns just the
    header."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c))
                    for c in columns})
    return buf.getvalue()


def _format_reactions(reactions: list[dict]) -> str:
    """Match upstream: 'thumbsup:2|heart:1'."""
    parts = [f"{r.get('name','')}:{int(r.get('count',0))}"
             for r in reactions or []]
    return "|".join(parts)


def _user_display(state: dict, uid: str) -> tuple[str, str]:
    u = state["users"].get(uid)
    if not u:
        return ("", "")
    return (u.get("name", ""), u.get("real_name", ""))


# Upstream uses CSV columns in this exact order — keep it stable.
_CHANNEL_COLS = ["id", "name", "topic", "purpose", "memberCount", "cursor"]
_MESSAGE_COLS = ["msgID", "userID", "userUser", "realName", "channelID",
                 "ThreadTs", "text", "time", "permalink", "reactions",
                 "botName", "fileCount", "attachmentIDs", "hasMedia",
                 "cursor"]
_USER_SEARCH_COLS = ["UserID", "UserName", "RealName", "DisplayName",
                     "Email", "Title", "DMChannelID"]
_USERGROUP_COLS = ["id", "name", "handle", "description", "user_count",
                   "is_external"]
_UNREAD_COLS = ["channelID", "channelName", "channelType", "unreadCount",
                "lastReadTs"]


def _paginate(items: list, cursor: str, limit: int) -> tuple[list, str]:
    """Cursor = the item id immediately AFTER which to resume (matches
    upstream Slack pagination convention). Returns (page, next_cursor)."""
    if limit <= 0:
        limit = 100
    start = 0
    if cursor:
        for i, it in enumerate(items):
            ident = (it.get("id") if isinstance(it, dict) else None)
            if ident == cursor:
                start = i + 1
                break
    end = start + limit
    page = items[start:end]
    next_cursor = ""
    if end < len(items) and page:
        last = page[-1]
        if isinstance(last, dict) and "id" in last:
            next_cursor = last["id"]
    return page, next_cursor


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("slack-mock")


# ---------------------------------------------------------------------------
# channels_list
# ---------------------------------------------------------------------------

_VALID_CHAN_TYPES = {"public_channel", "private_channel", "mpim", "im"}


def _channel_type(ch: dict) -> str:
    if ch.get("is_im"):
        return "im"
    if ch.get("is_mpim"):
        return "mpim"
    if ch.get("is_private"):
        return "private_channel"
    return "public_channel"


@mcp.tool(name="channels_list")
def channels_list(channel_types: str = "public_channel,private_channel",
                  sort: str = "",
                  limit: int = 100,
                  cursor: str = "",
                  query: str = "",
                  query_targets: str = "name") -> str:
    """Get list of channels. Returns CSV with columns: id, name, topic,
    purpose, memberCount, cursor (cursor populated only on the last
    row when more results are available)."""
    with _lock():
        s = _load_state()
        wanted = {t.strip() for t in (channel_types or "").split(",")
                  if t.strip() in _VALID_CHAN_TYPES}
        if not wanted:
            wanted = {"public_channel", "private_channel"}
        targets = {t.strip().lower() for t in (query_targets or "").split(",")
                   if t.strip().lower() in ("name", "topic", "purpose")}
        if not targets:
            targets = {"name"}
        rows = []
        for cid, ch in s["channels"].items():
            if _channel_type(ch) not in wanted:
                continue
            if query:
                q = query.lower()
                hit = False
                if "name" in targets and q in (ch.get("name") or "").lower():
                    hit = True
                if "topic" in targets and q in (ch.get("topic") or "").lower():
                    hit = True
                if "purpose" in targets and q in (ch.get("purpose") or "").lower():
                    hit = True
                if not hit:
                    continue
            rows.append({
                "id": cid,
                "name": ch.get("name", ""),
                "topic": ch.get("topic", ""),
                "purpose": ch.get("purpose", ""),
                "memberCount": len(ch.get("members", [])),
            })
        if sort == "popularity":
            rows.sort(key=lambda r: r["memberCount"], reverse=True)
        else:
            rows.sort(key=lambda r: r["id"])
        if limit <= 0:
            limit = 100
        if limit > 999:
            limit = 999
        page, next_cursor = _paginate(rows, cursor, limit)
        if page and next_cursor:
            page[-1]["cursor"] = next_cursor
        _record(s, "channels_list", channel_types=channel_types,
                query=query, count=len(page))
        _save_state(s)
        return _csv_dump(page, _CHANNEL_COLS)


@mcp.tool(name="channels_me")
def channels_me(channel_types: str = "public_channel,private_channel",
                limit: int = 100,
                cursor: str = "") -> str:
    """List channels the authenticated bot (self) is a member of."""
    with _lock():
        s = _load_state()
        me = s["self"]["id"]
        wanted = {t.strip() for t in (channel_types or "").split(",")
                  if t.strip() in _VALID_CHAN_TYPES}
        if not wanted:
            wanted = {"public_channel", "private_channel"}
        rows = []
        for cid, ch in s["channels"].items():
            if _channel_type(ch) not in wanted:
                continue
            if me not in ch.get("members", []):
                continue
            rows.append({
                "id": cid,
                "name": ch.get("name", ""),
                "topic": ch.get("topic", ""),
                "purpose": ch.get("purpose", ""),
                "memberCount": len(ch.get("members", [])),
            })
        rows.sort(key=lambda r: r["id"])
        if limit <= 0:
            limit = 100
        if limit > 999:
            limit = 999
        page, next_cursor = _paginate(rows, cursor, limit)
        if page and next_cursor:
            page[-1]["cursor"] = next_cursor
        _record(s, "channels_me", channel_types=channel_types,
                count=len(page))
        _save_state(s)
        return _csv_dump(page, _CHANNEL_COLS)


# ---------------------------------------------------------------------------
# conversations_join / leave / mark
# ---------------------------------------------------------------------------

@mcp.tool(name="conversations_join")
def conversations_join(channel_id: str) -> str:
    """Join a public channel."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_join", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        ch = s["channels"][cid]
        if ch.get("is_private"):
            _record(s, "conversations_join", channel=cid,
                    result="cant_join_private")
            _save_state(s)
            raise ValueError(f"method_not_supported_for_channel_type: {cid}")
        me = s["self"]["id"]
        members = ch.setdefault("members", [])
        if me not in members:
            members.append(me)
        _record(s, "conversations_join", channel=cid)
        _save_state(s)
        return f"Successfully joined {channel_id}"


@mcp.tool(name="conversations_leave")
def conversations_leave(channel_id: str) -> str:
    """Leave a channel/DM/group conversation. Cannot leave #general."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_leave", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        ch = s["channels"][cid]
        if ch.get("is_general") or ch.get("name") == "general":
            _record(s, "conversations_leave", channel=cid,
                    result="cant_leave_general")
            _save_state(s)
            raise ValueError("cant_leave_general")
        me = s["self"]["id"]
        members = ch.setdefault("members", [])
        if me not in members:
            _record(s, "conversations_leave", channel=cid,
                    result="not_in_channel")
            _save_state(s)
            return f"Not a member of {channel_id}"
        members.remove(me)
        _record(s, "conversations_leave", channel=cid)
        _save_state(s)
        return f"Successfully left {channel_id}"


@mcp.tool(name="conversations_mark")
def conversations_mark(channel_id: str, ts: str = "") -> str:
    """Mark a channel or DM as read up through `ts` (or all if blank)."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_mark", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        msgs = s["messages"].get(cid, [])
        if not ts:
            ts = msgs[-1]["ts"] if msgs else "0.000000"
        if not msgs:
            _record(s, "conversations_mark", channel=cid, ts=ts,
                    result="no_messages")
            _save_state(s)
            return "No messages to mark as read"
        s["channels"][cid]["last_read"] = ts
        _record(s, "conversations_mark", channel=cid, ts=ts)
        _save_state(s)
        return f"Marked {channel_id} as read up to {ts}"


# ---------------------------------------------------------------------------
# conversations_history / replies
# ---------------------------------------------------------------------------

_LIMIT_RANGE_RE = re.compile(r"^(\d+)([dwm])$")


def _parse_limit(limit: str) -> tuple[int, float | None]:
    """Returns (n, cutoff_ts_or_None). For a numeric limit returns
    (int, None); for a range like '7d', '1w', '1m' returns
    (large_int, unix_cutoff)."""
    if not limit:
        return (100, None)
    m = _LIMIT_RANGE_RE.match(limit.strip().lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = {"d": 1, "w": 7, "m": 30}[unit] * n
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).timestamp()
        return (10_000, cutoff)
    try:
        return (int(limit), None)
    except ValueError:
        return (100, None)


def _filter_messages(msgs: list[dict], include_activity: bool) -> list[dict]:
    out = []
    for m in msgs:
        st = m.get("subtype", "")
        if (st and st not in ("bot_message", "thread_broadcast")
                and not include_activity):
            continue
        out.append(m)
    return out


def _message_row(state: dict, channel_id: str, m: dict) -> dict:
    uname, rname = _user_display(state, m.get("user", ""))
    return {
        "msgID": m.get("ts", ""),
        "userID": m.get("user", ""),
        "userUser": uname,
        "realName": rname,
        "channelID": channel_id,
        "ThreadTs": m.get("thread_ts", ""),
        "text": m.get("text", ""),
        "time": _ts_to_rfc3339(m.get("ts", "")),
        "permalink": (f"{state['workspace']['url']}archives/{channel_id}/"
                      f"p{m.get('ts','').replace('.','')}"),
        "reactions": _format_reactions(m.get("reactions", [])),
        "botName": m.get("bot_name", ""),
        "fileCount": len(m.get("files", [])),
        "attachmentIDs": ", ".join(f.get("id", "") for f in m.get("files", [])),
        "hasMedia": "true" if m.get("files") else "false",
    }


@mcp.tool(name="conversations_history")
def conversations_history(channel_id: str,
                          include_activity_messages: bool = False,
                          cursor: str = "",
                          limit: str = "") -> str:
    """Get messages from a channel/DM. `limit` is either a count
    ("50") or a range ("1d", "7d", "1m"). Returns CSV; the last row
    has `cursor` filled if more results exist."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_history", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        all_msgs = s["messages"].get(cid, [])
        # only top-level messages (no thread_ts != ts)
        top = [m for m in all_msgs
               if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")]
        top = _filter_messages(top, include_activity_messages)
        n, cutoff = _parse_limit(limit or "")
        if cutoff is not None:
            top = [m for m in top if float(m.get("ts", "0")) >= cutoff]
        # newest first
        top.sort(key=lambda m: float(m.get("ts", "0")), reverse=True)
        # paginate by cursor on ts
        start = 0
        if cursor:
            for i, m in enumerate(top):
                if m.get("ts") == cursor:
                    start = i + 1
                    break
        page = top[start: start + n]
        next_cursor = ""
        if start + n < len(top) and page:
            next_cursor = page[-1].get("ts", "")
        rows = [_message_row(s, cid, m) for m in page]
        if rows and next_cursor:
            rows[-1]["cursor"] = next_cursor
        _record(s, "conversations_history", channel=cid,
                count=len(page), cursor=cursor, limit=limit)
        _save_state(s)
        return _csv_dump(rows, _MESSAGE_COLS)


@mcp.tool(name="conversations_replies")
def conversations_replies(channel_id: str,
                          thread_ts: str,
                          include_activity_messages: bool = False,
                          cursor: str = "",
                          limit: str = "") -> str:
    """Get a thread of messages by channel_id + thread_ts. Returns
    CSV (same columns as conversations_history)."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_replies", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        all_msgs = s["messages"].get(cid, [])
        thread = [m for m in all_msgs
                  if m.get("ts") == thread_ts
                  or m.get("thread_ts") == thread_ts]
        if not thread:
            _record(s, "conversations_replies", channel=cid,
                    thread_ts=thread_ts, result="thread_not_found")
            _save_state(s)
            raise ValueError(f"thread_not_found: {thread_ts}")
        thread = _filter_messages(thread, include_activity_messages)
        thread.sort(key=lambda m: float(m.get("ts", "0")))
        n, _ = _parse_limit(limit or "")
        start = 0
        if cursor:
            for i, m in enumerate(thread):
                if m.get("ts") == cursor:
                    start = i + 1
                    break
        page = thread[start: start + n]
        next_cursor = ""
        if start + n < len(thread) and page:
            next_cursor = page[-1].get("ts", "")
        rows = [_message_row(s, cid, m) for m in page]
        if rows and next_cursor:
            rows[-1]["cursor"] = next_cursor
        _record(s, "conversations_replies", channel=cid,
                thread_ts=thread_ts, count=len(page))
        _save_state(s)
        return _csv_dump(rows, _MESSAGE_COLS)


# ---------------------------------------------------------------------------
# conversations_add_message
# ---------------------------------------------------------------------------

@mcp.tool(name="conversations_add_message")
def conversations_add_message(channel_id: str,
                              text: str = "",
                              thread_ts: str = "",
                              content_type: str = "text/markdown",
                              blocks: str = "") -> str:
    """Post a message (or thread reply if `thread_ts` provided).
    Returns: 'Successfully posted message to channel <id> (ts=<ts>)'
    or with thread suffix. Real server gates this tool behind
    SLACK_MCP_ADD_MESSAGE_TOOL; the mock always enables it."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "conversations_add_message", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        # if blocks provided, derive text fallback
        parsed_blocks: list | None = None
        if blocks:
            try:
                parsed_blocks = json.loads(blocks)
            except json.JSONDecodeError as exc:
                _record(s, "conversations_add_message", channel=cid,
                        result="invalid_blocks")
                _save_state(s)
                raise ValueError(f"invalid_blocks: {exc}") from exc
        ts = _new_ts(s)
        msg: dict[str, Any] = {
            "ts": ts,
            "user": s["self"]["id"],
            "text": text or "",
            "channel": cid,
            "content_type": content_type,
            "reactions": [],
        }
        if parsed_blocks is not None:
            msg["blocks"] = parsed_blocks
        if thread_ts:
            # confirm parent exists
            parent = next((m for m in s["messages"].get(cid, [])
                           if m.get("ts") == thread_ts), None)
            if not parent:
                _record(s, "conversations_add_message", channel=cid,
                        thread_ts=thread_ts, result="thread_not_found")
                _save_state(s)
                raise ValueError(f"thread_not_found: {thread_ts}")
            msg["thread_ts"] = thread_ts
            parent["reply_count"] = parent.get("reply_count", 0) + 1
            parent.setdefault("reply_users", [])
            if s["self"]["id"] not in parent["reply_users"]:
                parent["reply_users"].append(s["self"]["id"])
        s["messages"].setdefault(cid, []).append(msg)
        _record(s, "conversations_add_message", channel=cid, ts=ts,
                thread_ts=thread_ts or None)
        _save_state(s)
        if thread_ts:
            return (f"Successfully posted message to channel {cid} "
                    f"in thread {thread_ts} (ts={ts})")
        return f"Successfully posted message to channel {cid} (ts={ts})"


# ---------------------------------------------------------------------------
# reactions_add / remove
# ---------------------------------------------------------------------------

@mcp.tool(name="reactions_add")
def reactions_add(channel_id: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction to a message. Emoji name without colons."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "reactions_add", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        emoji = emoji.strip(":")
        if not emoji:
            _record(s, "reactions_add", channel=cid, result="no_emoji")
            _save_state(s)
            raise ValueError("emoji is required")
        msg = next((m for m in s["messages"].get(cid, [])
                    if m.get("ts") == timestamp), None)
        if not msg:
            _record(s, "reactions_add", channel=cid, ts=timestamp,
                    result="message_not_found")
            _save_state(s)
            raise ValueError(f"message_not_found: {timestamp}")
        me = s["self"]["id"]
        rs = msg.setdefault("reactions", [])
        entry = next((r for r in rs if r.get("name") == emoji), None)
        if entry is None:
            rs.append({"name": emoji, "users": [me], "count": 1})
        elif me in entry.get("users", []):
            _record(s, "reactions_add", channel=cid, ts=timestamp,
                    emoji=emoji, result="already_reacted")
            _save_state(s)
            raise ValueError("already_reacted")
        else:
            entry.setdefault("users", []).append(me)
            entry["count"] = len(entry["users"])
        _record(s, "reactions_add", channel=cid, ts=timestamp, emoji=emoji)
        _save_state(s)
        return (f"Successfully added :{emoji}: reaction to message "
                f"{timestamp} in channel {cid}")


@mcp.tool(name="reactions_remove")
def reactions_remove(channel_id: str, timestamp: str, emoji: str) -> str:
    """Remove a reaction the bot has added."""
    with _lock():
        s = _load_state()
        cid = _resolve_channel(s, channel_id)
        if not cid:
            _record(s, "reactions_remove", channel=channel_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"channel_not_found: {channel_id}")
        emoji = emoji.strip(":")
        if not emoji:
            raise ValueError("emoji is required")
        msg = next((m for m in s["messages"].get(cid, [])
                    if m.get("ts") == timestamp), None)
        if not msg:
            _record(s, "reactions_remove", channel=cid, ts=timestamp,
                    result="message_not_found")
            _save_state(s)
            raise ValueError(f"message_not_found: {timestamp}")
        me = s["self"]["id"]
        rs = msg.get("reactions", [])
        entry = next((r for r in rs if r.get("name") == emoji), None)
        if not entry or me not in entry.get("users", []):
            _record(s, "reactions_remove", channel=cid, ts=timestamp,
                    emoji=emoji, result="no_reaction")
            _save_state(s)
            raise ValueError("no_reaction")
        entry["users"].remove(me)
        entry["count"] = len(entry["users"])
        if entry["count"] == 0:
            rs.remove(entry)
        _record(s, "reactions_remove", channel=cid, ts=timestamp, emoji=emoji)
        _save_state(s)
        return (f"Successfully removed :{emoji}: reaction from message "
                f"{timestamp} in channel {cid}")


# ---------------------------------------------------------------------------
# conversations_search_messages
# ---------------------------------------------------------------------------

def _date_matches(msg_ts: str, before: str, after: str,
                  on: str, during: str) -> bool:
    if not (before or after or on or during):
        return True
    try:
        d = datetime.datetime.fromtimestamp(float(msg_ts),
                                            tz=datetime.timezone.utc).date()
    except (TypeError, ValueError):
        return False
    if before:
        try:
            if d >= datetime.date.fromisoformat(before):
                return False
        except ValueError:
            pass
    if after:
        try:
            if d <= datetime.date.fromisoformat(after):
                return False
        except ValueError:
            pass
    if on:
        try:
            if d != datetime.date.fromisoformat(on):
                return False
        except ValueError:
            pass
    if during:
        # only YYYY-MM month-prefix supported in the mock
        if len(during) >= 7 and d.isoformat()[:7] != during[:7]:
            return False
    return True


@mcp.tool(name="conversations_search_messages")
def conversations_search_messages(search_query: str = "",
                                  filter_in_channel: str = "",
                                  filter_in_im_or_mpim: str = "",
                                  filter_users_with: str = "",
                                  filter_users_from: str = "",
                                  filter_date_before: str = "",
                                  filter_date_after: str = "",
                                  filter_date_on: str = "",
                                  filter_date_during: str = "",
                                  filter_threads_only: bool = False,
                                  cursor: str = "",
                                  limit: int = 100) -> str:
    """Search messages with filters. Returns CSV (same columns as
    conversations_history)."""
    with _lock():
        s = _load_state()
        chan_filter = (_resolve_channel(s, filter_in_channel)
                       if filter_in_channel else None)
        im_filter = (_resolve_channel(s, filter_in_im_or_mpim)
                     if filter_in_im_or_mpim else None)
        from_uid = (_resolve_user(s, filter_users_from)
                    if filter_users_from else None)
        with_uid = (_resolve_user(s, filter_users_with)
                    if filter_users_with else None)
        q = (search_query or "").lower()
        hits = []
        for cid, msgs in s["messages"].items():
            ch = s["channels"].get(cid, {})
            if chan_filter and cid != chan_filter:
                continue
            if im_filter and cid != im_filter:
                continue
            for m in msgs:
                if filter_threads_only and not m.get("thread_ts"):
                    continue
                if from_uid and m.get("user") != from_uid:
                    continue
                if with_uid:
                    # match if the with-user appears in same thread
                    thread_users = {mm.get("user") for mm in msgs
                                    if mm.get("thread_ts") == m.get("ts")
                                    or mm.get("ts") == m.get("thread_ts")
                                    or mm.get("ts") == m.get("ts")}
                    if with_uid not in thread_users:
                        continue
                if not _date_matches(m.get("ts", "0"),
                                     filter_date_before, filter_date_after,
                                     filter_date_on, filter_date_during):
                    continue
                if q and q not in (m.get("text", "") or "").lower():
                    continue
                hits.append((cid, m))
        hits.sort(key=lambda t: float(t[1].get("ts", "0")), reverse=True)
        if limit <= 0 or limit > 100:
            limit = 100
        start = 0
        if cursor:
            for i, (_, m) in enumerate(hits):
                if m.get("ts") == cursor:
                    start = i + 1
                    break
        page = hits[start: start + limit]
        next_cursor = ""
        if start + limit < len(hits) and page:
            next_cursor = page[-1][1].get("ts", "")
        rows = [_message_row(s, cid, m) for cid, m in page]
        if rows and next_cursor:
            rows[-1]["cursor"] = next_cursor
        _record(s, "conversations_search_messages", q=search_query,
                count=len(page))
        _save_state(s)
        return _csv_dump(rows, _MESSAGE_COLS)


# ---------------------------------------------------------------------------
# conversations_unreads
# ---------------------------------------------------------------------------

@mcp.tool(name="conversations_unreads")
def conversations_unreads(include_messages: bool = True,
                          channel_types: str = "all",
                          max_channels: int = 50,
                          max_messages_per_channel: int = 10,
                          mentions_only: bool = False,
                          include_muted: bool = False) -> str:
    """Summarize unread messages across channels. Returns CSV (one row
    per channel with unread > 0)."""
    with _lock():
        s = _load_state()
        wanted = set()
        for t in (channel_types or "").split(","):
            t = t.strip()
            if t == "all":
                wanted.update({"public_channel", "private_channel",
                               "im", "mpim"})
            elif t == "dm":
                wanted.add("im")
            elif t == "group_dm":
                wanted.add("mpim")
            elif t in ("partner", "internal"):
                wanted.update({"public_channel", "private_channel"})
        if not wanted:
            wanted = {"public_channel", "private_channel", "im", "mpim"}
        rows = []
        for cid, ch in s["channels"].items():
            ctype = _channel_type(ch)
            if ctype not in wanted:
                continue
            if ch.get("is_muted") and not include_muted:
                continue
            last_read = float(ch.get("last_read", "0") or "0")
            unread = [m for m in s["messages"].get(cid, [])
                      if float(m.get("ts", "0")) > last_read
                      and m.get("user") != s["self"]["id"]]
            if mentions_only:
                mention = f"<@{s['self']['id']}>"
                unread = [m for m in unread if mention in (m.get("text", "") or "")]
            if not unread:
                continue
            rows.append({
                "channelID": cid,
                "channelName": ch.get("name", ""),
                "channelType": ctype,
                "unreadCount": len(unread),
                "lastReadTs": ch.get("last_read", "0"),
            })
        rows.sort(key=lambda r: r["unreadCount"], reverse=True)
        rows = rows[:max_channels]
        _record(s, "conversations_unreads", count=len(rows))
        _save_state(s)
        if not rows:
            return "No unread messages."
        cols = list(_UNREAD_COLS)
        if include_messages:
            cols = cols + ["sampleMessages"]
            for r in rows:
                cid = r["channelID"]
                last_read = float(s["channels"][cid].get("last_read", "0") or "0")
                sample = [m.get("text", "")
                          for m in s["messages"].get(cid, [])
                          if float(m.get("ts", "0")) > last_read
                          and m.get("user") != s["self"]["id"]
                          ][:max_messages_per_channel]
                r["sampleMessages"] = " | ".join(sample)
        return _csv_dump(rows, cols)


# ---------------------------------------------------------------------------
# users_search
# ---------------------------------------------------------------------------

@mcp.tool(name="users_search")
def users_search(query: str, limit: int = 10) -> str:
    """Search users by name, email, display name, or Slack user ID.
    Returns CSV with columns UserID, UserName, RealName, DisplayName,
    Email, Title, DMChannelID."""
    with _lock():
        s = _load_state()
        q = (query or "").lower().strip()
        results = []
        if limit <= 0 or limit > 100:
            limit = 10
        if query in s["users"]:
            users = [s["users"][query]]
        else:
            users = []
            for u in s["users"].values():
                if u.get("deleted"):
                    continue
                hay = " ".join([
                    u.get("name", ""),
                    u.get("real_name", ""),
                    u.get("profile", {}).get("display_name", ""),
                    u.get("profile", {}).get("email", ""),
                    u.get("profile", {}).get("title", ""),
                    u.get("id", ""),
                ]).lower()
                if q and q in hay:
                    users.append(u)
        for u in users[:limit]:
            dm = ""
            for cid, ch in s["channels"].items():
                if ch.get("is_im") and u["id"] in ch.get("members", []):
                    dm = cid
                    break
            results.append({
                "UserID": u["id"],
                "UserName": u.get("name", ""),
                "RealName": u.get("real_name", ""),
                "DisplayName": u.get("profile", {}).get("display_name", ""),
                "Email": u.get("profile", {}).get("email", ""),
                "Title": u.get("profile", {}).get("title", ""),
                "DMChannelID": dm,
            })
        _record(s, "users_search", q=query, count=len(results))
        _save_state(s)
        if not results:
            return "No users found matching the query."
        return _csv_dump(results, _USER_SEARCH_COLS)


# ---------------------------------------------------------------------------
# Usergroups
# ---------------------------------------------------------------------------

@mcp.tool(name="usergroups_list")
def usergroups_list(include_users: bool = False,
                    include_count: bool = True,
                    include_disabled: bool = False) -> str:
    """List user groups (subteams). Returns CSV id,name,handle,
    description,user_count,is_external (and `users` column if
    include_users)."""
    with _lock():
        s = _load_state()
        rows = []
        for gid, g in s["usergroups"].items():
            if g.get("disabled") and not include_disabled:
                continue
            row = {
                "id": gid,
                "name": g.get("name", ""),
                "handle": g.get("handle", ""),
                "description": g.get("description", ""),
                "is_external": "true" if g.get("is_external") else "false",
            }
            if include_count:
                row["user_count"] = len(g.get("users", []))
            if include_users:
                row["users"] = ",".join(g.get("users", []))
            rows.append(row)
        cols = list(_USERGROUP_COLS)
        if not include_count:
            cols.remove("user_count")
        if include_users:
            cols = cols + ["users"]
        _record(s, "usergroups_list", count=len(rows))
        _save_state(s)
        return _csv_dump(rows, cols)


@mcp.tool(name="usergroups_me")
def usergroups_me(action: str = "list", usergroup_id: str = "") -> str:
    """Manage the bot's own usergroup membership.
    `action` in {'list','join','leave'}."""
    with _lock():
        s = _load_state()
        me = s["self"]["id"]
        if action == "list":
            rows = []
            for gid, g in s["usergroups"].items():
                if me in g.get("users", []):
                    rows.append({
                        "id": gid,
                        "name": g.get("name", ""),
                        "handle": g.get("handle", ""),
                        "description": g.get("description", ""),
                        "user_count": len(g.get("users", [])),
                        "is_external": "true" if g.get("is_external") else "false",
                    })
            _record(s, "usergroups_me", action="list", count=len(rows))
            _save_state(s)
            return _csv_dump(rows, _USERGROUP_COLS)
        if not usergroup_id or usergroup_id not in s["usergroups"]:
            _record(s, "usergroups_me", action=action,
                    result="group_not_found")
            _save_state(s)
            raise ValueError(f"usergroup_not_found: {usergroup_id}")
        g = s["usergroups"][usergroup_id]
        users = g.setdefault("users", [])
        if action == "join":
            if me not in users:
                users.append(me)
            _record(s, "usergroups_me", action="join", group=usergroup_id)
            _save_state(s)
            return f"Joined {usergroup_id}"
        if action == "leave":
            if me in users:
                users.remove(me)
            _record(s, "usergroups_me", action="leave", group=usergroup_id)
            _save_state(s)
            return f"Left {usergroup_id}"
        raise ValueError(f"unknown_action: {action}")


@mcp.tool(name="usergroups_create")
def usergroups_create(name: str,
                      handle: str = "",
                      description: str = "",
                      channels: str = "") -> str:
    """Create a new usergroup."""
    with _lock():
        s = _load_state()
        gid = _new_usergroup_id(s)
        if not handle:
            handle = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        g = {
            "id": gid,
            "name": name,
            "handle": handle,
            "description": description,
            "channels": [c.strip() for c in (channels or "").split(",")
                         if c.strip()],
            "users": [],
            "is_external": False,
            "disabled": False,
            "created": _now_iso(),
        }
        s["usergroups"][gid] = g
        _record(s, "usergroups_create", group=gid, name=name, handle=handle)
        _save_state(s)
        return f"Created usergroup {gid} (@{handle})"


@mcp.tool(name="usergroups_update")
def usergroups_update(usergroup_id: str,
                      name: str = "",
                      handle: str = "",
                      description: str = "",
                      channels: str = "") -> str:
    """Update a usergroup's metadata (not members)."""
    with _lock():
        s = _load_state()
        g = s["usergroups"].get(usergroup_id)
        if not g:
            _record(s, "usergroups_update", group=usergroup_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"usergroup_not_found: {usergroup_id}")
        if name:
            g["name"] = name
        if handle:
            g["handle"] = handle
        if description:
            g["description"] = description
        if channels:
            g["channels"] = [c.strip() for c in channels.split(",")
                             if c.strip()]
        _record(s, "usergroups_update", group=usergroup_id)
        _save_state(s)
        return f"Updated usergroup {usergroup_id}"


@mcp.tool(name="usergroups_users_update")
def usergroups_users_update(usergroup_id: str, users: str) -> str:
    """Replace the full membership list of a usergroup."""
    with _lock():
        s = _load_state()
        g = s["usergroups"].get(usergroup_id)
        if not g:
            _record(s, "usergroups_users_update", group=usergroup_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"usergroup_not_found: {usergroup_id}")
        new_users = [u.strip() for u in (users or "").split(",") if u.strip()]
        unknown = [u for u in new_users if u not in s["users"]]
        if unknown:
            _record(s, "usergroups_users_update", group=usergroup_id,
                    result="unknown_users", unknown=unknown)
            _save_state(s)
            raise ValueError(f"unknown_users: {unknown}")
        g["users"] = new_users
        _record(s, "usergroups_users_update", group=usergroup_id,
                count=len(new_users))
        _save_state(s)
        return f"Replaced membership of {usergroup_id} ({len(new_users)} users)"


# ---------------------------------------------------------------------------
# Saved for Later
# ---------------------------------------------------------------------------

_SAVED_COLS = ["item_id", "ts", "state", "date_due", "date_completed"]


@mcp.tool(name="saved_list")
def saved_list(filter: str = "saved",
               limit: int = 50,
               include_messages: bool = True,
               max_messages_per_item: int = 5) -> str:
    """List Save-for-Later items. Filter in {saved, completed, archived}."""
    with _lock():
        s = _load_state()
        rows = []
        for item in s.get("saved", []):
            if item.get("state", "saved") != filter:
                continue
            row = {
                "item_id": item.get("item_id", ""),
                "ts": item.get("ts", ""),
                "state": item.get("state", "saved"),
                "date_due": item.get("date_due", 0),
                "date_completed": item.get("date_completed", 0),
            }
            if include_messages:
                cid = item.get("item_id")
                msgs = s["messages"].get(cid, [])
                m = next((mm for mm in msgs
                          if mm.get("ts") == item.get("ts")), None)
                if m:
                    row["text"] = m.get("text", "")[:200]
            rows.append(row)
        cols = list(_SAVED_COLS)
        if include_messages:
            cols.append("text")
        rows = rows[:max(1, limit)]
        _record(s, "saved_list", filter=filter, count=len(rows))
        _save_state(s)
        return _csv_dump(rows, cols)


@mcp.tool(name="saved_update")
def saved_update(item_id: str, ts: str, mark: str = "",
                 date_due: int = 0) -> str:
    """Update a saved item: mark completed and/or set due date."""
    with _lock():
        s = _load_state()
        item = next((i for i in s.get("saved", [])
                     if i.get("item_id") == item_id and i.get("ts") == ts),
                    None)
        if not item:
            _record(s, "saved_update", item_id=item_id, ts=ts,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"saved_item_not_found: {item_id}/{ts}")
        if mark == "completed":
            item["state"] = "completed"
            item["date_completed"] = int(datetime.datetime.now(
                datetime.timezone.utc).timestamp())
        if date_due is not None:
            item["date_due"] = int(date_due)
        _record(s, "saved_update", item_id=item_id, ts=ts, mark=mark,
                date_due=date_due)
        _save_state(s)
        return f"Updated saved item {item_id}/{ts}"


@mcp.tool(name="saved_clear_completed")
def saved_clear_completed() -> str:
    """Remove all saved items with state=completed."""
    with _lock():
        s = _load_state()
        before = len(s.get("saved", []))
        s["saved"] = [i for i in s.get("saved", [])
                      if i.get("state") != "completed"]
        removed = before - len(s["saved"])
        _record(s, "saved_clear_completed", removed=removed)
        _save_state(s)
        return f"Cleared {removed} completed saved items"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@mcp.tool(name="attachment_get_data")
def attachment_get_data(file_id: str) -> str:
    """Download an attachment by file ID. Returns a JSON-string with
    file_id, filename, mimetype, size, encoding, content."""
    with _lock():
        s = _load_state()
        f = s["files"].get(file_id)
        if not f:
            _record(s, "attachment_get_data", file_id=file_id,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"file_not_found: {file_id}")
        _record(s, "attachment_get_data", file_id=file_id)
        _save_state(s)
        return json.dumps({
            "file_id": file_id,
            "filename": f.get("name", ""),
            "mimetype": f.get("mimetype", "application/octet-stream"),
            "size": f.get("size", 0),
            "encoding": f.get("encoding", "none"),
            "content": f.get("content", ""),
        })


# ---------------------------------------------------------------------------
# Mock-only helpers (not part of the real surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (for verifier introspection)."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(workspace: dict | None = None,
                    self_user: dict | None = None,
                    users: list | None = None,
                    channels: list | None = None,
                    messages: list | None = None,
                    usergroups: list | None = None,
                    saved: list | None = None,
                    files: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. Each input collection holds Slack-ish dicts.

    - `users`: [{id?, name, real_name, profile?: {display_name,email,title}}]
    - `channels`: [{id?, name, is_private?, is_im?, is_mpim?, is_general?,
                    topic?, purpose?, members?: [user_id]}]
    - `messages`: [{channel, text, user?, thread_ts?, ts?}]
    - `usergroups`: [{id?, name, handle?, description?, users?: [uid]}]
    - `saved`: [{item_id, ts, state?}]
    - `files`: [{id?, name, mimetype, content, encoding?}]

    If `replace` is true, the state is fully reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if workspace:
            s["workspace"].update(workspace)
        if self_user:
            s["self"].update(self_user)
            if self_user.get("id") and self_user["id"] not in s["users"]:
                s["users"][self_user["id"]] = {
                    "id": self_user["id"],
                    "name": self_user.get("name", "mockbot"),
                    "real_name": self_user.get("real_name", "Mock Bot"),
                    "profile": {"display_name": self_user.get("name", "mockbot"),
                                "email": "", "title": "Bot"},
                    "is_bot": True,
                }
        for u in users or []:
            uid = u.get("id") or _new_user_id(s)
            s["users"][uid] = {
                "id": uid,
                "name": u.get("name", uid),
                "real_name": u.get("real_name", u.get("name", uid)),
                "profile": u.get("profile") or {
                    "display_name": u.get("name", uid),
                    "email": u.get("email", ""),
                    "title": u.get("title", ""),
                },
                "deleted": bool(u.get("deleted")),
                "is_bot": bool(u.get("is_bot")),
            }
        for c in channels or []:
            cid = c.get("id")
            if not cid:
                prefix = "D" if c.get("is_im") else "C"
                cid = _new_channel_id(s, prefix)
            s["channels"][cid] = {
                "id": cid,
                "name": c.get("name", cid),
                "is_private": bool(c.get("is_private")),
                "is_im": bool(c.get("is_im")),
                "is_mpim": bool(c.get("is_mpim")),
                "is_general": bool(c.get("is_general"))
                or c.get("name") == "general",
                "is_muted": bool(c.get("is_muted")),
                "topic": c.get("topic", ""),
                "purpose": c.get("purpose", ""),
                "members": list(c.get("members") or []),
                "last_read": c.get("last_read", "0"),
            }
            s["messages"].setdefault(cid, [])
        for m in messages or []:
            cid = _resolve_channel(s, m.get("channel", ""))
            if not cid:
                continue
            ts = m.get("ts") or _new_ts(s)
            entry = {
                "ts": ts,
                "user": m.get("user", s["self"]["id"]),
                "text": m.get("text", ""),
                "channel": cid,
                "reactions": m.get("reactions", []),
            }
            if m.get("thread_ts"):
                entry["thread_ts"] = m["thread_ts"]
            s["messages"].setdefault(cid, []).append(entry)
        for g in usergroups or []:
            gid = g.get("id") or _new_usergroup_id(s)
            s["usergroups"][gid] = {
                "id": gid,
                "name": g.get("name", gid),
                "handle": g.get("handle", gid.lower()),
                "description": g.get("description", ""),
                "channels": list(g.get("channels") or []),
                "users": list(g.get("users") or []),
                "is_external": bool(g.get("is_external")),
                "disabled": bool(g.get("disabled")),
            }
        for it in saved or []:
            s.setdefault("saved", []).append({
                "item_id": it["item_id"],
                "ts": it["ts"],
                "state": it.get("state", "saved"),
                "date_due": it.get("date_due", 0),
                "date_completed": it.get("date_completed", 0),
            })
        for f in files or []:
            fid = f.get("id") or _new_file_id(s)
            s["files"][fid] = {
                "id": fid,
                "name": f.get("name", fid),
                "mimetype": f.get("mimetype", "application/octet-stream"),
                "content": f.get("content", ""),
                "encoding": f.get("encoding", "none"),
                "size": f.get("size", len(f.get("content", ""))),
            }
        _record(s, "debug_seed",
                counts={"users": len(users or []),
                        "channels": len(channels or []),
                        "messages": len(messages or []),
                        "usergroups": len(usergroups or []),
                        "saved": len(saved or []),
                        "files": len(files or [])},
                replace=replace)
        _save_state(s)
        return {"ok": True,
                "channel_ids": list(s["channels"].keys()),
                "user_ids": list(s["users"].keys())}


if __name__ == "__main__":
    mcp.run()
