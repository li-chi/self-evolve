"""Telegram Bot API mock MCP server.

Mirrors the surface of the real Telegram Bot API
(https://core.telegram.org/bots/api). Each tool is named after a real
Bot API method (camelCase, e.g. `sendMessage`, `getChat`), accepts the
same parameter names, and returns the canonical Telegram envelope:

    Success:  {"ok": true,  "result": <object>}
    Error:    {"ok": false, "error_code": <int>, "description": <str>}

Error envelopes are *returned*, not raised, so a verifier's trace
looks identical to a real failed HTTPS request to api.telegram.org.

State lives at `$TELEGRAM_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/telegram_mock`). Optional `TELEGRAM_MOCK_SEED_PATH`
preloads state when no state.json exists yet (per-rollout isolation
should clear the state dir between rollouts). Every call (including
reads) appends to `state["calls"]` so verifiers can replay the trace.

Implemented tool surface (28 + 2 mock helpers):

  Bot / Updates
    getMe, getUpdates
  Sending
    sendMessage, forwardMessage, copyMessage,
    sendPhoto, sendDocument, sendVideo, sendAudio, sendLocation,
    sendChatAction
  Editing
    editMessageText, editMessageReplyMarkup, deleteMessage
  Callback queries
    answerCallbackQuery
  Chat / Members
    getChat, getChatMember, getChatAdministrators, getChatMemberCount,
    leaveChat, banChatMember, unbanChatMember,
    pinChatMessage, unpinChatMessage
  Bot commands
    setMyCommands, getMyCommands, deleteMyCommands
  Webhook
    setWebhook, deleteWebhook, getWebhookInfo
  Mock-only
    mock_debug_state, mock_debug_seed

Telegram conventions reproduced here:
  - integer user/chat ids (e.g., 123456789); message_id is per-chat
  - Chat object: id, type (private/group/supergroup/channel), title,
    username, first_name, last_name
  - User object: id, is_bot, first_name, last_name, username,
    language_code
  - Message object: message_id, from (User), chat (Chat), date
    (epoch seconds), text, entities, etc.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
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
        "TELEGRAM_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/telegram_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_epoch() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def _empty_state() -> dict:
    bot_id = 700000001
    return {
        "self": {
            "id": bot_id,
            "is_bot": True,
            "first_name": "Mock Bot",
            "username": "mockbot",
            "can_join_groups": True,
            "can_read_all_group_messages": False,
            "supports_inline_queries": False,
            "can_connect_to_business": False,
            "has_main_web_app": False,
        },
        "users": {},          # str(user_id) -> User dict
        "chats": {},          # str(chat_id) -> Chat dict with extras
        "messages": {},       # str(chat_id) -> list[Message dict]
        "updates": [],        # list[Update dict]
        "callback_queries": {},  # callback_query_id -> dict (answered, etc.)
        "commands": {},       # scope_key -> {"commands":[...], "language_code":""}
        "webhook": {
            "url": "",
            "has_custom_certificate": False,
            "pending_update_count": 0,
            "max_connections": 40,
            "allowed_updates": [],
            "ip_address": "",
            "last_error_date": 0,
            "last_error_message": "",
        },
        "next_id": {
            "update": 1,
            "message": {},    # per-chat next message_id (str(chat_id) -> int)
            "user": 100001,
            "chat": -1000000000001,  # supergroups/channels use large negatives
            "file": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("TELEGRAM_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now_epoch()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Telegram response envelopes
# ---------------------------------------------------------------------------

def _ok(result: Any) -> dict:
    return {"ok": True, "result": result}


def _err(error_code: int, description: str, **extra: Any) -> dict:
    out = {"ok": False, "error_code": error_code, "description": description}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chat_key(chat_id: Any) -> str:
    return str(chat_id)


def _resolve_chat(state: dict, chat_id: Any) -> tuple[str | None, dict | None]:
    """Resolve `chat_id` (int, str of int, or "@username") to a canonical
    chat key + chat dict, or (None, None) if not found."""
    if chat_id is None or chat_id == "":
        return None, None
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        uname = chat_id[1:].lower()
        for k, ch in state["chats"].items():
            if (ch.get("username") or "").lower() == uname:
                return k, ch
        return None, None
    key = str(chat_id)
    ch = state["chats"].get(key)
    if ch is not None:
        return key, ch
    return None, None


def _next_message_id(state: dict, chat_key: str) -> int:
    bucket = state["next_id"].setdefault("message", {})
    n = int(bucket.get(chat_key, 1))
    bucket[chat_key] = n + 1
    return n


def _next_user_id(state: dict) -> int:
    n = int(state["next_id"].get("user", 100001))
    state["next_id"]["user"] = n + 1
    return n


def _next_chat_id(state: dict) -> int:
    """Allocate a fresh chat id for synthetic supergroup/channel chats."""
    n = int(state["next_id"].get("chat", -1000000000001))
    state["next_id"]["chat"] = n - 1
    return n


def _next_file_id(state: dict) -> str:
    n = int(state["next_id"].get("file", 1))
    state["next_id"]["file"] = n + 1
    return f"FILE{n:08d}"


def _next_update_id(state: dict) -> int:
    n = int(state["next_id"].get("update", 1))
    state["next_id"]["update"] = n + 1
    return n


def _user_view(state: dict, user_id: int | str) -> dict:
    """Return a User object dict; falls back to the bot if unknown."""
    if user_id is None:
        return dict(state["self"])
    u = state["users"].get(str(user_id))
    if u:
        return dict(u)
    if str(user_id) == str(state["self"]["id"]):
        return dict(state["self"])
    return {"id": int(user_id) if str(user_id).lstrip("-").isdigit()
            else user_id,
            "is_bot": False,
            "first_name": "Unknown"}


def _chat_view(chat: dict) -> dict:
    """Return a Telegram Chat object (strip mock-only extras)."""
    drop = {"members", "administrators", "banned", "pinned_message_ids",
            "permissions_user_ids"}
    return {k: v for k, v in chat.items() if k not in drop}


def _ensure_chat_msgs(state: dict, chat_key: str) -> list:
    return state["messages"].setdefault(chat_key, [])


def _make_message(state: dict, chat_key: str, *, from_user: dict,
                  text: str | None = None,
                  caption: str | None = None,
                  reply_to_message_id: int | None = None,
                  entities: list | None = None,
                  caption_entities: list | None = None,
                  reply_markup: dict | None = None,
                  extra: dict | None = None) -> dict:
    chat = state["chats"][chat_key]
    mid = _next_message_id(state, chat_key)
    msg: dict[str, Any] = {
        "message_id": mid,
        "from": from_user,
        "chat": _chat_view(chat),
        "date": _now_epoch(),
    }
    if text is not None:
        msg["text"] = text
        if entities:
            msg["entities"] = entities
    if caption is not None:
        msg["caption"] = caption
        if caption_entities:
            msg["caption_entities"] = caption_entities
    if reply_to_message_id is not None:
        parent = next((m for m in _ensure_chat_msgs(state, chat_key)
                       if m.get("message_id") == reply_to_message_id), None)
        if parent is not None:
            msg["reply_to_message"] = parent
    if reply_markup is not None:
        msg["reply_markup"] = reply_markup
    if extra:
        msg.update(extra)
    _ensure_chat_msgs(state, chat_key).append(msg)
    return msg


def _push_update(state: dict, **fields: Any) -> dict:
    upd = {"update_id": _next_update_id(state)}
    upd.update(fields)
    state["updates"].append(upd)
    return upd


def _make_file(state: dict, *, mime_type: str = "",
               file_size: int = 0, file_name: str = "") -> dict:
    fid = _next_file_id(state)
    # unique_id is a stable token; we just reuse fid with a prefix tweak
    fuid = "U" + fid
    return {
        "file_id": fid,
        "file_unique_id": fuid,
        "file_size": file_size,
        "mime_type": mime_type,
        "file_name": file_name,
    }


def _commands_scope_key(scope: dict | None, language_code: str | None) -> str:
    """Stable string key for a BotCommandScope+language_code pair."""
    if not isinstance(scope, dict):
        scope = {"type": "default"}
    stype = scope.get("type", "default")
    extras = []
    for k in ("chat_id", "user_id"):
        if k in scope:
            extras.append(f"{k}={scope[k]}")
    lc = language_code or ""
    return f"{stype}|{','.join(extras)}|{lc}"


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("telegram-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Bot identity / Updates
# ---------------------------------------------------------------------------

@mcp.tool(name="getMe")
def get_me() -> dict:
    """Telegram Bot API: getMe — return basic information about the bot
    as a User object."""
    with _lock():
        s = _load_state()
        _record(s, "getMe")
        _save_state(s)
        return _ok(dict(s["self"]))


@mcp.tool(name="getUpdates")
def get_updates(offset: int = 0,
                limit: int = 100,
                timeout: int = 0,
                allowed_updates: list | None = None) -> dict:
    """Telegram Bot API: getUpdates — long-polling endpoint returning
    pending Update objects. The mock returns whatever updates have been
    queued (via incoming messages from non-bot users seeded into state,
    or callback queries). `offset` confirms updates up to offset-1.
    """
    with _lock():
        s = _load_state()
        if offset:
            s["updates"] = [u for u in s["updates"]
                            if int(u.get("update_id", 0)) >= int(offset)]
        if limit <= 0 or limit > 100:
            limit = 100
        page = s["updates"][:limit]
        if allowed_updates:
            allowed = set(allowed_updates)
            page = [u for u in page
                    if any(k in allowed for k in u
                           if k != "update_id")]
        _record(s, "getUpdates", offset=offset, limit=limit,
                returned=len(page))
        _save_state(s)
        return _ok(page)


# ---------------------------------------------------------------------------
# Sending messages
# ---------------------------------------------------------------------------

@mcp.tool(name="sendMessage")
def send_message(chat_id: Any,
                 text: str,
                 parse_mode: str | None = None,
                 entities: list | None = None,
                 disable_notification: bool = False,
                 protect_content: bool = False,
                 reply_to_message_id: int | None = None,
                 reply_markup: dict | None = None,
                 message_thread_id: int | None = None,
                 link_preview_options: dict | None = None) -> dict:
    """Telegram Bot API: sendMessage — send a text message to a chat.
    Returns the resulting Message object."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "sendMessage", chat_id=chat_id, result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if text is None or text == "":
            _record(s, "sendMessage", chat_id=chat_id, result="empty_text")
            _save_state(s)
            return _err(400, "Bad Request: message text is empty")
        if len(text) > 4096:
            _record(s, "sendMessage", chat_id=chat_id, result="too_long")
            _save_state(s)
            return _err(400, "Bad Request: message is too long")
        extra: dict[str, Any] = {}
        if message_thread_id is not None:
            extra["message_thread_id"] = message_thread_id
            extra["is_topic_message"] = True
        if link_preview_options is not None:
            extra["link_preview_options"] = link_preview_options
        if disable_notification:
            extra["disable_notification"] = True
        if protect_content:
            extra["has_protected_content"] = True
        msg = _make_message(s, ck, from_user=dict(s["self"]),
                            text=text, entities=entities,
                            reply_to_message_id=reply_to_message_id,
                            reply_markup=reply_markup, extra=extra)
        _record(s, "sendMessage", chat_id=ck, message_id=msg["message_id"])
        _save_state(s)
        return _ok(msg)


@mcp.tool(name="forwardMessage")
def forward_message(chat_id: Any,
                    from_chat_id: Any,
                    message_id: int,
                    disable_notification: bool = False,
                    protect_content: bool = False,
                    message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: forwardMessage — forward a message of any kind.
    Service messages and messages with protected content can't be forwarded."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        fk, from_chat = _resolve_chat(s, from_chat_id)
        if not chat:
            _record(s, "forwardMessage", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if not from_chat:
            _record(s, "forwardMessage", from_chat_id=from_chat_id,
                    result="from_chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: source chat not found")
        src_msg = next((m for m in _ensure_chat_msgs(s, fk)
                        if m.get("message_id") == int(message_id)), None)
        if not src_msg:
            _record(s, "forwardMessage", from_chat_id=fk,
                    message_id=message_id, result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to forward not found")
        if src_msg.get("has_protected_content"):
            _record(s, "forwardMessage", from_chat_id=fk,
                    message_id=message_id, result="protected")
            _save_state(s)
            return _err(400,
                        "Bad Request: message can't be forwarded "
                        "(protected content)")
        forward_origin = {
            "type": "user" if from_chat.get("type") == "private"
            else "chat",
            "date": src_msg.get("date", _now_epoch()),
        }
        if from_chat.get("type") == "private":
            forward_origin["sender_user"] = src_msg.get("from") or _user_view(
                s, src_msg.get("from", {}).get("id"))
        else:
            forward_origin["sender_chat"] = _chat_view(from_chat)
            forward_origin["message_id"] = src_msg.get("message_id")
        extra = {
            "forward_origin": forward_origin,
            "forward_date": src_msg.get("date", _now_epoch()),
            "forward_from_chat": _chat_view(from_chat),
            "forward_from_message_id": src_msg.get("message_id"),
        }
        if disable_notification:
            extra["disable_notification"] = True
        if protect_content:
            extra["has_protected_content"] = True
        if message_thread_id is not None:
            extra["message_thread_id"] = message_thread_id
        msg = _make_message(s, ck, from_user=dict(s["self"]),
                            text=src_msg.get("text"),
                            caption=src_msg.get("caption"),
                            extra=extra)
        for carry in ("photo", "document", "video", "audio", "location"):
            if carry in src_msg:
                msg[carry] = src_msg[carry]
        _record(s, "forwardMessage", chat_id=ck, from_chat_id=fk,
                src_message_id=message_id, message_id=msg["message_id"])
        _save_state(s)
        return _ok(msg)


@mcp.tool(name="copyMessage")
def copy_message(chat_id: Any,
                 from_chat_id: Any,
                 message_id: int,
                 caption: str | None = None,
                 parse_mode: str | None = None,
                 caption_entities: list | None = None,
                 disable_notification: bool = False,
                 protect_content: bool = False,
                 reply_to_message_id: int | None = None,
                 reply_markup: dict | None = None,
                 message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: copyMessage — copy a message of any kind.
    Returns {message_id: <new_id>} (NOT a full Message object — this
    matches the real API)."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        fk, from_chat = _resolve_chat(s, from_chat_id)
        if not chat:
            _record(s, "copyMessage", chat_id=chat_id, result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if not from_chat:
            _record(s, "copyMessage", from_chat_id=from_chat_id,
                    result="from_chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: source chat not found")
        src_msg = next((m for m in _ensure_chat_msgs(s, fk)
                        if m.get("message_id") == int(message_id)), None)
        if not src_msg:
            _record(s, "copyMessage", from_chat_id=fk,
                    message_id=message_id, result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to copy not found")
        text = src_msg.get("text")
        new_caption = (caption if caption is not None
                       else src_msg.get("caption"))
        extra: dict[str, Any] = {}
        if message_thread_id is not None:
            extra["message_thread_id"] = message_thread_id
        if disable_notification:
            extra["disable_notification"] = True
        if protect_content:
            extra["has_protected_content"] = True
        msg = _make_message(s, ck, from_user=dict(s["self"]),
                            text=text, caption=new_caption,
                            caption_entities=caption_entities,
                            reply_to_message_id=reply_to_message_id,
                            reply_markup=reply_markup, extra=extra)
        for carry in ("photo", "document", "video", "audio", "location"):
            if carry in src_msg:
                msg[carry] = src_msg[carry]
        _record(s, "copyMessage", chat_id=ck, from_chat_id=fk,
                src_message_id=message_id, message_id=msg["message_id"])
        _save_state(s)
        return _ok({"message_id": msg["message_id"]})


def _send_media_common(method: str, chat_id: Any, caption: str | None,
                       reply_to_message_id: int | None,
                       reply_markup: dict | None,
                       message_thread_id: int | None,
                       disable_notification: bool,
                       protect_content: bool,
                       media_kind: str,
                       media_payload: dict,
                       caption_entities: list | None) -> dict:
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, method, chat_id=chat_id, result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        extra: dict[str, Any] = {media_kind: media_payload}
        if message_thread_id is not None:
            extra["message_thread_id"] = message_thread_id
        if disable_notification:
            extra["disable_notification"] = True
        if protect_content:
            extra["has_protected_content"] = True
        msg = _make_message(s, ck, from_user=dict(s["self"]),
                            caption=caption,
                            caption_entities=caption_entities,
                            reply_to_message_id=reply_to_message_id,
                            reply_markup=reply_markup, extra=extra)
        _record(s, method, chat_id=ck, message_id=msg["message_id"],
                media_kind=media_kind)
        _save_state(s)
        return _ok(msg)


@mcp.tool(name="sendPhoto")
def send_photo(chat_id: Any,
               photo: str,
               caption: str | None = None,
               parse_mode: str | None = None,
               caption_entities: list | None = None,
               has_spoiler: bool = False,
               disable_notification: bool = False,
               protect_content: bool = False,
               reply_to_message_id: int | None = None,
               reply_markup: dict | None = None,
               message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendPhoto — send a photo. `photo` may be a
    file_id, an HTTPS URL, or attach://<name>. Returns a Message with a
    `photo` field (array of PhotoSize)."""
    with _lock():
        s = _load_state()
        file = _make_file(s, mime_type="image/jpeg")
        _save_state(s)
    size = {"file_id": file["file_id"],
            "file_unique_id": file["file_unique_id"],
            "width": 1280, "height": 720, "file_size": 100_000}
    extra: dict[str, Any] = {}
    if has_spoiler:
        extra["has_media_spoiler"] = True
    payload = [size]
    return _send_media_common("sendPhoto", chat_id, caption,
                              reply_to_message_id, reply_markup,
                              message_thread_id, disable_notification,
                              protect_content, "photo", payload,
                              caption_entities)


@mcp.tool(name="sendDocument")
def send_document(chat_id: Any,
                  document: str,
                  thumbnail: str | None = None,
                  caption: str | None = None,
                  parse_mode: str | None = None,
                  caption_entities: list | None = None,
                  disable_content_type_detection: bool = False,
                  disable_notification: bool = False,
                  protect_content: bool = False,
                  reply_to_message_id: int | None = None,
                  reply_markup: dict | None = None,
                  message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendDocument — send a general file (up to 50 MB
    on the real API). Returns a Message with a `document` field."""
    with _lock():
        s = _load_state()
        file = _make_file(s,
                          mime_type="application/octet-stream",
                          file_name=os.path.basename(str(document)))
        _save_state(s)
    payload = {"file_id": file["file_id"],
               "file_unique_id": file["file_unique_id"],
               "file_name": file["file_name"],
               "mime_type": file["mime_type"],
               "file_size": file["file_size"]}
    return _send_media_common("sendDocument", chat_id, caption,
                              reply_to_message_id, reply_markup,
                              message_thread_id, disable_notification,
                              protect_content, "document", payload,
                              caption_entities)


@mcp.tool(name="sendVideo")
def send_video(chat_id: Any,
               video: str,
               duration: int | None = None,
               width: int | None = None,
               height: int | None = None,
               thumbnail: str | None = None,
               caption: str | None = None,
               parse_mode: str | None = None,
               caption_entities: list | None = None,
               has_spoiler: bool = False,
               supports_streaming: bool = False,
               disable_notification: bool = False,
               protect_content: bool = False,
               reply_to_message_id: int | None = None,
               reply_markup: dict | None = None,
               message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendVideo — send a video (mp4). Returns a
    Message with a `video` field."""
    with _lock():
        s = _load_state()
        file = _make_file(s, mime_type="video/mp4")
        _save_state(s)
    payload = {"file_id": file["file_id"],
               "file_unique_id": file["file_unique_id"],
               "width": width or 1280,
               "height": height or 720,
               "duration": duration or 0,
               "mime_type": file["mime_type"],
               "file_size": file["file_size"]}
    return _send_media_common("sendVideo", chat_id, caption,
                              reply_to_message_id, reply_markup,
                              message_thread_id, disable_notification,
                              protect_content, "video", payload,
                              caption_entities)


@mcp.tool(name="sendAudio")
def send_audio(chat_id: Any,
               audio: str,
               caption: str | None = None,
               parse_mode: str | None = None,
               caption_entities: list | None = None,
               duration: int | None = None,
               performer: str | None = None,
               title: str | None = None,
               thumbnail: str | None = None,
               disable_notification: bool = False,
               protect_content: bool = False,
               reply_to_message_id: int | None = None,
               reply_markup: dict | None = None,
               message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendAudio — send an audio file to be displayed
    in the music player. Returns a Message with an `audio` field."""
    with _lock():
        s = _load_state()
        file = _make_file(s, mime_type="audio/mpeg")
        _save_state(s)
    payload = {"file_id": file["file_id"],
               "file_unique_id": file["file_unique_id"],
               "duration": duration or 0,
               "mime_type": file["mime_type"],
               "file_size": file["file_size"]}
    if performer:
        payload["performer"] = performer
    if title:
        payload["title"] = title
    return _send_media_common("sendAudio", chat_id, caption,
                              reply_to_message_id, reply_markup,
                              message_thread_id, disable_notification,
                              protect_content, "audio", payload,
                              caption_entities)


@mcp.tool(name="sendLocation")
def send_location(chat_id: Any,
                  latitude: float,
                  longitude: float,
                  horizontal_accuracy: float | None = None,
                  live_period: int | None = None,
                  heading: int | None = None,
                  proximity_alert_radius: int | None = None,
                  disable_notification: bool = False,
                  protect_content: bool = False,
                  reply_to_message_id: int | None = None,
                  reply_markup: dict | None = None,
                  message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendLocation — send a point on the map. Returns
    a Message with a `location` field."""
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return _err(400, "Bad Request: latitude and longitude must be numeric")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return _err(400, "Bad Request: latitude/longitude out of range")
    payload: dict[str, Any] = {"latitude": lat, "longitude": lng}
    if horizontal_accuracy is not None:
        payload["horizontal_accuracy"] = horizontal_accuracy
    if live_period is not None:
        payload["live_period"] = live_period
    if heading is not None:
        payload["heading"] = heading
    if proximity_alert_radius is not None:
        payload["proximity_alert_radius"] = proximity_alert_radius
    return _send_media_common("sendLocation", chat_id, None,
                              reply_to_message_id, reply_markup,
                              message_thread_id, disable_notification,
                              protect_content, "location", payload, None)


_VALID_CHAT_ACTIONS = {
    "typing", "upload_photo", "record_video", "upload_video",
    "record_voice", "upload_voice", "upload_document", "choose_sticker",
    "find_location", "record_video_note", "upload_video_note",
}


@mcp.tool(name="sendChatAction")
def send_chat_action(chat_id: Any,
                     action: str,
                     message_thread_id: int | None = None) -> dict:
    """Telegram Bot API: sendChatAction — tell the user something is
    happening on the bot's side. Returns `true` on success."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "sendChatAction", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if action not in _VALID_CHAT_ACTIONS:
            _record(s, "sendChatAction", chat_id=ck, action=action,
                    result="invalid_action")
            _save_state(s)
            return _err(400, "Bad Request: unsupported chat action")
        _record(s, "sendChatAction", chat_id=ck, action=action,
                message_thread_id=message_thread_id)
        _save_state(s)
        return _ok(True)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def _find_message(state: dict, chat_key: str,
                  message_id: int) -> dict | None:
    return next((m for m in _ensure_chat_msgs(state, chat_key)
                 if m.get("message_id") == int(message_id)), None)


@mcp.tool(name="editMessageText")
def edit_message_text(text: str,
                      chat_id: Any = None,
                      message_id: int | None = None,
                      inline_message_id: str | None = None,
                      parse_mode: str | None = None,
                      entities: list | None = None,
                      link_preview_options: dict | None = None,
                      reply_markup: dict | None = None) -> dict:
    """Telegram Bot API: editMessageText — edit text messages. Either
    (chat_id+message_id) OR `inline_message_id` is required.

    Returns the edited Message on success (or `true` when editing an
    inline message)."""
    with _lock():
        s = _load_state()
        if inline_message_id and not (chat_id and message_id):
            _record(s, "editMessageText",
                    inline_message_id=inline_message_id)
            _save_state(s)
            return _ok(True)
        if not (chat_id and message_id):
            _record(s, "editMessageText", result="missing_ids")
            _save_state(s)
            return _err(400, "Bad Request: chat_id and message_id required")
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "editMessageText", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        msg = _find_message(s, ck, int(message_id))
        if not msg:
            _record(s, "editMessageText", chat_id=ck, message_id=message_id,
                    result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to edit not found")
        if msg.get("text") == text and (entities is None
                                        or msg.get("entities") == entities):
            _record(s, "editMessageText", chat_id=ck, message_id=message_id,
                    result="not_modified")
            _save_state(s)
            return _err(400,
                        "Bad Request: message is not modified: specified "
                        "new message content and reply markup are exactly "
                        "the same as a current content and reply markup "
                        "of the message")
        msg["text"] = text
        if entities is not None:
            msg["entities"] = entities
        if reply_markup is not None:
            msg["reply_markup"] = reply_markup
        if link_preview_options is not None:
            msg["link_preview_options"] = link_preview_options
        msg["edit_date"] = _now_epoch()
        _record(s, "editMessageText", chat_id=ck, message_id=message_id)
        _save_state(s)
        return _ok(msg)


@mcp.tool(name="editMessageReplyMarkup")
def edit_message_reply_markup(chat_id: Any = None,
                              message_id: int | None = None,
                              inline_message_id: str | None = None,
                              reply_markup: dict | None = None) -> dict:
    """Telegram Bot API: editMessageReplyMarkup — edit only the inline
    keyboard / reply markup of a message."""
    with _lock():
        s = _load_state()
        if inline_message_id and not (chat_id and message_id):
            _record(s, "editMessageReplyMarkup",
                    inline_message_id=inline_message_id)
            _save_state(s)
            return _ok(True)
        if not (chat_id and message_id):
            _record(s, "editMessageReplyMarkup", result="missing_ids")
            _save_state(s)
            return _err(400, "Bad Request: chat_id and message_id required")
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "editMessageReplyMarkup", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        msg = _find_message(s, ck, int(message_id))
        if not msg:
            _record(s, "editMessageReplyMarkup", chat_id=ck,
                    message_id=message_id, result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to edit not found")
        if reply_markup is None:
            msg.pop("reply_markup", None)
        else:
            msg["reply_markup"] = reply_markup
        msg["edit_date"] = _now_epoch()
        _record(s, "editMessageReplyMarkup", chat_id=ck,
                message_id=message_id)
        _save_state(s)
        return _ok(msg)


@mcp.tool(name="deleteMessage")
def delete_message(chat_id: Any, message_id: int) -> dict:
    """Telegram Bot API: deleteMessage — delete a message. Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "deleteMessage", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        msgs = _ensure_chat_msgs(s, ck)
        before = len(msgs)
        s["messages"][ck] = [m for m in msgs
                             if m.get("message_id") != int(message_id)]
        if len(s["messages"][ck]) == before:
            _record(s, "deleteMessage", chat_id=ck, message_id=message_id,
                    result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to delete not found")
        # also strip from pinned list
        pinned = s["chats"][ck].get("pinned_message_ids", [])
        if int(message_id) in pinned:
            pinned.remove(int(message_id))
        _record(s, "deleteMessage", chat_id=ck, message_id=message_id)
        _save_state(s)
        return _ok(True)


# ---------------------------------------------------------------------------
# Callback queries
# ---------------------------------------------------------------------------

@mcp.tool(name="answerCallbackQuery")
def answer_callback_query(callback_query_id: str,
                          text: str | None = None,
                          show_alert: bool = False,
                          url: str | None = None,
                          cache_time: int = 0) -> dict:
    """Telegram Bot API: answerCallbackQuery — respond to a callback
    query sent from an inline keyboard. Returns `true`."""
    with _lock():
        s = _load_state()
        cq = s["callback_queries"].get(callback_query_id)
        if cq is None:
            # The real API accepts any id; we still record but don't error.
            cq = {"id": callback_query_id, "answered": False}
            s["callback_queries"][callback_query_id] = cq
        cq["answered"] = True
        cq["answer"] = {
            "text": text or "",
            "show_alert": bool(show_alert),
            "url": url or "",
            "cache_time": int(cache_time or 0),
        }
        _record(s, "answerCallbackQuery",
                callback_query_id=callback_query_id,
                show_alert=show_alert)
        _save_state(s)
        return _ok(True)


# ---------------------------------------------------------------------------
# Chat / Members
# ---------------------------------------------------------------------------

def _chat_member_view(state: dict, chat: dict, user_id: int) -> dict | None:
    members = chat.get("members", [])
    entry = next((m for m in members if int(m.get("user_id", 0)) ==
                  int(user_id)), None)
    if entry is None:
        if int(user_id) in chat.get("banned", []):
            return {"status": "kicked",
                    "user": _user_view(state, user_id),
                    "until_date": 0}
        return None
    status = entry.get("status", "member")
    out: dict[str, Any] = {
        "status": status,
        "user": _user_view(state, user_id),
    }
    if status in ("administrator", "creator"):
        out["is_anonymous"] = entry.get("is_anonymous", False)
        if status == "administrator":
            out["can_be_edited"] = entry.get("can_be_edited", False)
            out["can_manage_chat"] = entry.get("can_manage_chat", True)
            out["can_delete_messages"] = entry.get("can_delete_messages", True)
            out["can_manage_video_chats"] = entry.get(
                "can_manage_video_chats", True)
            out["can_restrict_members"] = entry.get(
                "can_restrict_members", True)
            out["can_promote_members"] = entry.get(
                "can_promote_members", False)
            out["can_change_info"] = entry.get("can_change_info", True)
            out["can_invite_users"] = entry.get("can_invite_users", True)
            out["can_post_messages"] = entry.get("can_post_messages", False)
            out["can_edit_messages"] = entry.get("can_edit_messages", False)
            out["can_pin_messages"] = entry.get("can_pin_messages", True)
            out["can_post_stories"] = entry.get("can_post_stories", False)
            out["can_edit_stories"] = entry.get("can_edit_stories", False)
            out["can_delete_stories"] = entry.get("can_delete_stories", False)
            out["can_manage_topics"] = entry.get("can_manage_topics", False)
            if entry.get("custom_title"):
                out["custom_title"] = entry["custom_title"]
    return out


@mcp.tool(name="getChat")
def get_chat(chat_id: Any) -> dict:
    """Telegram Bot API: getChat — get up-to-date information about a
    chat. Returns a ChatFullInfo object."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "getChat", chat_id=chat_id, result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        view = _chat_view(chat)
        # ChatFullInfo extras
        view["accent_color_id"] = chat.get("accent_color_id", 0)
        view["max_reaction_count"] = chat.get("max_reaction_count", 11)
        if "description" in chat:
            view["description"] = chat["description"]
        if "invite_link" in chat:
            view["invite_link"] = chat["invite_link"]
        pinned_ids = chat.get("pinned_message_ids", [])
        if pinned_ids:
            top = _find_message(s, ck, pinned_ids[-1])
            if top:
                view["pinned_message"] = top
        if "permissions" in chat:
            view["permissions"] = chat["permissions"]
        _record(s, "getChat", chat_id=ck)
        _save_state(s)
        return _ok(view)


@mcp.tool(name="getChatMember")
def get_chat_member(chat_id: Any, user_id: int) -> dict:
    """Telegram Bot API: getChatMember — get information about a member
    of a chat. Returns a ChatMember object."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "getChatMember", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        view = _chat_member_view(s, chat, int(user_id))
        if view is None:
            _record(s, "getChatMember", chat_id=ck, user_id=user_id,
                    result="user_not_found")
            _save_state(s)
            return _err(400,
                        "Bad Request: user not found in the chat")
        _record(s, "getChatMember", chat_id=ck, user_id=user_id,
                status=view["status"])
        _save_state(s)
        return _ok(view)


@mcp.tool(name="getChatAdministrators")
def get_chat_administrators(chat_id: Any) -> dict:
    """Telegram Bot API: getChatAdministrators — get a list of
    administrators in a chat. Returns an array of ChatMember objects."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "getChatAdministrators", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if chat.get("type") == "private":
            _record(s, "getChatAdministrators", chat_id=ck,
                    result="not_supported")
            _save_state(s)
            return _err(400, "Bad Request: there are no administrators "
                        "in the private chat")
        out = []
        for m in chat.get("members", []):
            if m.get("status") in ("creator", "administrator"):
                view = _chat_member_view(s, chat, int(m["user_id"]))
                if view:
                    out.append(view)
        _record(s, "getChatAdministrators", chat_id=ck, count=len(out))
        _save_state(s)
        return _ok(out)


@mcp.tool(name="getChatMemberCount")
def get_chat_member_count(chat_id: Any) -> dict:
    """Telegram Bot API: getChatMemberCount — get the number of members
    in a chat."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "getChatMemberCount", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if chat.get("type") == "private":
            count = 2
        else:
            count = len(chat.get("members", []))
        _record(s, "getChatMemberCount", chat_id=ck, count=count)
        _save_state(s)
        return _ok(count)


@mcp.tool(name="leaveChat")
def leave_chat(chat_id: Any) -> dict:
    """Telegram Bot API: leaveChat — use this method for the bot to
    leave a group, supergroup or channel. Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "leaveChat", chat_id=chat_id, result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if chat.get("type") == "private":
            _record(s, "leaveChat", chat_id=ck, result="cant_leave_private")
            _save_state(s)
            return _err(400,
                        "Bad Request: chat type can't be 'private'")
        me_id = int(s["self"]["id"])
        members = chat.get("members", [])
        before = len(members)
        chat["members"] = [m for m in members
                           if int(m.get("user_id", 0)) != me_id]
        if len(chat["members"]) == before:
            _record(s, "leaveChat", chat_id=ck, result="not_member")
            _save_state(s)
            return _err(400, "Bad Request: bot is not a member of the chat")
        _record(s, "leaveChat", chat_id=ck)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="banChatMember")
def ban_chat_member(chat_id: Any,
                    user_id: int,
                    until_date: int | None = None,
                    revoke_messages: bool = False) -> dict:
    """Telegram Bot API: banChatMember — ban a user in a group, a
    supergroup or a channel. The bot must be an administrator with the
    appropriate rights. Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "banChatMember", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        if chat.get("type") == "private":
            _record(s, "banChatMember", chat_id=ck, result="not_supported")
            _save_state(s)
            return _err(400, "Bad Request: method not available in "
                        "private chats")
        chat["members"] = [m for m in chat.get("members", [])
                           if int(m.get("user_id", 0)) != int(user_id)]
        banned = chat.setdefault("banned", [])
        if int(user_id) not in banned:
            banned.append(int(user_id))
        if revoke_messages:
            msgs = _ensure_chat_msgs(s, ck)
            s["messages"][ck] = [m for m in msgs
                                 if int((m.get("from") or {}).get(
                                     "id", 0)) != int(user_id)]
        _record(s, "banChatMember", chat_id=ck, user_id=user_id,
                until_date=until_date, revoke_messages=revoke_messages)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="unbanChatMember")
def unban_chat_member(chat_id: Any,
                      user_id: int,
                      only_if_banned: bool = False) -> dict:
    """Telegram Bot API: unbanChatMember — unban a previously banned
    user. Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "unbanChatMember", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        banned = chat.setdefault("banned", [])
        was_banned = int(user_id) in banned
        if was_banned:
            banned.remove(int(user_id))
        elif only_if_banned:
            _record(s, "unbanChatMember", chat_id=ck, user_id=user_id,
                    result="not_banned")
            _save_state(s)
            return _ok(True)
        _record(s, "unbanChatMember", chat_id=ck, user_id=user_id,
                was_banned=was_banned)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="pinChatMessage")
def pin_chat_message(chat_id: Any,
                     message_id: int,
                     disable_notification: bool = False,
                     business_connection_id: str | None = None) -> dict:
    """Telegram Bot API: pinChatMessage — add a message to the list of
    pinned messages in a chat. Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "pinChatMessage", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        msg = _find_message(s, ck, int(message_id))
        if not msg:
            _record(s, "pinChatMessage", chat_id=ck, message_id=message_id,
                    result="message_not_found")
            _save_state(s)
            return _err(400, "Bad Request: message to pin not found")
        pinned = chat.setdefault("pinned_message_ids", [])
        if int(message_id) not in pinned:
            pinned.append(int(message_id))
        _record(s, "pinChatMessage", chat_id=ck, message_id=message_id)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="unpinChatMessage")
def unpin_chat_message(chat_id: Any,
                       message_id: int | None = None,
                       business_connection_id: str | None = None) -> dict:
    """Telegram Bot API: unpinChatMessage — remove a message from the
    list of pinned messages (or the most recent pin if message_id is
    omitted). Returns `true`."""
    with _lock():
        s = _load_state()
        ck, chat = _resolve_chat(s, chat_id)
        if not chat:
            _record(s, "unpinChatMessage", chat_id=chat_id,
                    result="chat_not_found")
            _save_state(s)
            return _err(400, "Bad Request: chat not found")
        pinned = chat.setdefault("pinned_message_ids", [])
        if message_id is None:
            if not pinned:
                _record(s, "unpinChatMessage", chat_id=ck,
                        result="no_pinned_messages")
                _save_state(s)
                return _err(400, "Bad Request: there are no pinned messages")
            removed = pinned.pop()
            _record(s, "unpinChatMessage", chat_id=ck,
                    message_id=removed)
            _save_state(s)
            return _ok(True)
        if int(message_id) not in pinned:
            _record(s, "unpinChatMessage", chat_id=ck,
                    message_id=message_id, result="not_pinned")
            _save_state(s)
            return _err(400, "Bad Request: message to unpin not found")
        pinned.remove(int(message_id))
        _record(s, "unpinChatMessage", chat_id=ck, message_id=message_id)
        _save_state(s)
        return _ok(True)


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------

_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")


@mcp.tool(name="setMyCommands")
def set_my_commands(commands: list,
                    scope: dict | None = None,
                    language_code: str | None = None) -> dict:
    """Telegram Bot API: setMyCommands — change the list of the bot's
    commands. Returns `true` on success.

    `commands` is a list of BotCommand objects: {command, description}.
    `scope` is a BotCommandScope object (default = {type: "default"}).
    """
    if not isinstance(commands, list):
        return _err(400, "Bad Request: commands must be an array")
    for c in commands:
        if not isinstance(c, dict):
            return _err(400, "Bad Request: each command must be an object")
        cmd = (c.get("command") or "").strip()
        desc = (c.get("description") or "").strip()
        if not _COMMAND_RE.match(cmd):
            return _err(400,
                        "Bad Request: command names must match "
                        "[a-z0-9_]{1,32}")
        if not 1 <= len(desc) <= 256:
            return _err(400,
                        "Bad Request: description length must be 1..256")
    with _lock():
        s = _load_state()
        key = _commands_scope_key(scope, language_code)
        s["commands"][key] = {
            "commands": [{"command": c["command"],
                          "description": c["description"]}
                         for c in commands],
            "scope": scope or {"type": "default"},
            "language_code": language_code or "",
        }
        _record(s, "setMyCommands", scope=scope,
                language_code=language_code, count=len(commands))
        _save_state(s)
        return _ok(True)


@mcp.tool(name="getMyCommands")
def get_my_commands(scope: dict | None = None,
                    language_code: str | None = None) -> dict:
    """Telegram Bot API: getMyCommands — get the current list of the
    bot's commands. Returns an array of BotCommand objects.

    If no commands are set for the given scope+language, falls back to
    the default scope (matches real-API behavior)."""
    with _lock():
        s = _load_state()
        key = _commands_scope_key(scope, language_code)
        entry = s["commands"].get(key)
        if entry is None:
            entry = s["commands"].get(
                _commands_scope_key({"type": "default"}, None))
        out = entry["commands"] if entry else []
        _record(s, "getMyCommands", scope=scope,
                language_code=language_code, count=len(out))
        _save_state(s)
        return _ok(out)


@mcp.tool(name="deleteMyCommands")
def delete_my_commands(scope: dict | None = None,
                       language_code: str | None = None) -> dict:
    """Telegram Bot API: deleteMyCommands — delete the list of the bot's
    commands for the given scope and user language. After deletion,
    higher-level commands will be shown. Returns `true`."""
    with _lock():
        s = _load_state()
        key = _commands_scope_key(scope, language_code)
        existed = key in s["commands"]
        if existed:
            s["commands"].pop(key, None)
        _record(s, "deleteMyCommands", scope=scope,
                language_code=language_code, existed=existed)
        _save_state(s)
        return _ok(True)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@mcp.tool(name="setWebhook")
def set_webhook(url: str,
                certificate: str | None = None,
                ip_address: str | None = None,
                max_connections: int = 40,
                allowed_updates: list | None = None,
                drop_pending_updates: bool = False,
                secret_token: str | None = None) -> dict:
    """Telegram Bot API: setWebhook — specify a URL and receive updates
    via outgoing webhook. Returns `true`."""
    with _lock():
        s = _load_state()
        if url and not (url.startswith("https://") or url == ""):
            _record(s, "setWebhook", url=url, result="bad_scheme")
            _save_state(s)
            return _err(400,
                        "Bad Request: bad webhook: HTTPS url must be provided")
        if not 1 <= int(max_connections or 40) <= 100:
            _record(s, "setWebhook", result="bad_max_connections")
            _save_state(s)
            return _err(400, "Bad Request: bad max_connections (1..100)")
        wh = s.setdefault("webhook", {})
        wh.update({
            "url": url or "",
            "has_custom_certificate": bool(certificate),
            "ip_address": ip_address or "",
            "max_connections": int(max_connections or 40),
            "allowed_updates": list(allowed_updates or []),
            "secret_token_set": bool(secret_token),
        })
        if drop_pending_updates:
            s["updates"] = []
            wh["pending_update_count"] = 0
        _record(s, "setWebhook", url=url,
                drop_pending_updates=drop_pending_updates)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="deleteWebhook")
def delete_webhook(drop_pending_updates: bool = False) -> dict:
    """Telegram Bot API: deleteWebhook — remove webhook integration if
    you decide to switch back to getUpdates. Returns `true`."""
    with _lock():
        s = _load_state()
        wh = s.setdefault("webhook", {})
        wh.update({
            "url": "",
            "has_custom_certificate": False,
            "ip_address": "",
            "max_connections": 40,
            "allowed_updates": [],
            "secret_token_set": False,
        })
        if drop_pending_updates:
            s["updates"] = []
            wh["pending_update_count"] = 0
        _record(s, "deleteWebhook",
                drop_pending_updates=drop_pending_updates)
        _save_state(s)
        return _ok(True)


@mcp.tool(name="getWebhookInfo")
def get_webhook_info() -> dict:
    """Telegram Bot API: getWebhookInfo — get current webhook status as
    a WebhookInfo object."""
    with _lock():
        s = _load_state()
        wh = s.get("webhook", {})
        info = {
            "url": wh.get("url", ""),
            "has_custom_certificate": wh.get("has_custom_certificate", False),
            "pending_update_count": len(s.get("updates", [])),
            "max_connections": wh.get("max_connections", 40),
            "allowed_updates": list(wh.get("allowed_updates", []) or []),
        }
        if wh.get("ip_address"):
            info["ip_address"] = wh["ip_address"]
        if wh.get("last_error_date"):
            info["last_error_date"] = wh["last_error_date"]
            info["last_error_message"] = wh.get("last_error_message", "")
        _record(s, "getWebhookInfo")
        _save_state(s)
        return _ok(info)


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection). Not exposed by the real Telegram Bot API."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(self_user: dict | None = None,
                    users: list | None = None,
                    chats: list | None = None,
                    messages: list | None = None,
                    updates: list | None = None,
                    commands: list | None = None,
                    webhook: dict | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed state with Telegram-shaped fixtures.

    - `self_user`: {id?, username?, first_name?, ...} — overrides the bot
      identity.
    - `users`: [{id?, is_bot?, first_name, last_name?, username?,
                 language_code?}]
    - `chats`: [{id?, type ("private"|"group"|"supergroup"|"channel"),
                 title?, username?, first_name?, last_name?,
                 description?, members?: [{user_id, status?,
                 is_anonymous?, custom_title?, can_*?}],
                 banned?: [user_id], pinned_message_ids?: [...],
                 permissions?, invite_link?}]
    - `messages`: [{chat_id, message_id?, from_id?, text?, caption?,
                    photo?, document?, video?, audio?, location?,
                    date?, reply_to_message_id?, entities?,
                    reply_markup?}]
    - `updates`: [{...}] — raw Update objects to queue for getUpdates.
    - `commands`: [{scope?, language_code?, commands:[{command,
                    description},...]}]
    - `webhook`: full webhook dict to overlay.

    If `replace` is true, state is reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if self_user:
            s["self"].update(self_user)
            s["users"][str(s["self"]["id"])] = dict(s["self"])
        for u in users or []:
            uid = int(u.get("id") or _next_user_id(s))
            s["users"][str(uid)] = {
                "id": uid,
                "is_bot": bool(u.get("is_bot", False)),
                "first_name": u.get("first_name", f"User{uid}"),
                "last_name": u.get("last_name", ""),
                "username": u.get("username", ""),
                "language_code": u.get("language_code", "en"),
            }
        for c in chats or []:
            cid = c.get("id")
            if cid is None:
                ctype = c.get("type", "supergroup")
                if ctype == "private":
                    cid = _next_user_id(s)
                else:
                    cid = _next_chat_id(s)
            cid = int(cid)
            entry: dict[str, Any] = {
                "id": cid,
                "type": c.get("type", "supergroup"),
            }
            for k in ("title", "username", "first_name", "last_name",
                      "description", "invite_link", "accent_color_id",
                      "permissions", "max_reaction_count"):
                if k in c:
                    entry[k] = c[k]
            entry["members"] = list(c.get("members") or [])
            entry["banned"] = list(c.get("banned") or [])
            entry["pinned_message_ids"] = list(
                c.get("pinned_message_ids") or [])
            s["chats"][str(cid)] = entry
            s["messages"].setdefault(str(cid), [])
        for m in messages or []:
            chat_id = m.get("chat_id")
            ck, chat = _resolve_chat(s, chat_id)
            if not chat:
                continue
            mid = m.get("message_id") or _next_message_id(s, ck)
            from_id = m.get("from_id", s["self"]["id"])
            entry = {
                "message_id": int(mid),
                "from": _user_view(s, from_id),
                "chat": _chat_view(chat),
                "date": int(m.get("date", _now_epoch())),
            }
            for k in ("text", "caption", "entities", "caption_entities",
                      "reply_markup", "photo", "document", "video",
                      "audio", "location", "edit_date", "message_thread_id",
                      "has_protected_content"):
                if k in m:
                    entry[k] = m[k]
            if m.get("reply_to_message_id") is not None:
                parent = _find_message(s, ck, int(m["reply_to_message_id"]))
                if parent is not None:
                    entry["reply_to_message"] = parent
            _ensure_chat_msgs(s, ck).append(entry)
        for u in updates or []:
            uid = int(u.get("update_id") or _next_update_id(s))
            entry = dict(u)
            entry["update_id"] = uid
            s["updates"].append(entry)
        for cset in commands or []:
            scope = cset.get("scope")
            lc = cset.get("language_code")
            key = _commands_scope_key(scope, lc)
            s["commands"][key] = {
                "commands": list(cset.get("commands") or []),
                "scope": scope or {"type": "default"},
                "language_code": lc or "",
            }
        if webhook:
            s.setdefault("webhook", {}).update(webhook)
        _record(s, "debug_seed",
                counts={"users": len(users or []),
                        "chats": len(chats or []),
                        "messages": len(messages or []),
                        "updates": len(updates or []),
                        "commands": len(commands or [])},
                replace=replace)
        _save_state(s)
        return _ok({
            "chat_ids": [int(k) for k in s["chats"].keys()],
            "user_ids": [int(k) for k in s["users"].keys()],
            "bot_id": s["self"]["id"],
        })


if __name__ == "__main__":
    mcp.run()
