"""Gmail mock MCP server.

Mirrors the Gmail API v1 `users.*` surface (the surface every
well-known Gmail MCP wrapper — gongrzhe/gmail-mcp,
GongRzhe/Gmail-MCP-Server, and similar — wraps). Tool names follow
the de-facto MCP naming derived from Gmail method names
(`users.messages.send` -> `send_message`, etc.). Responses match the
Gmail API v1 resource shapes verbatim:

  Message
    {id, threadId, labelIds, snippet, historyId, internalDate,
     sizeEstimate, payload: {partId, mimeType, filename, headers,
                              body, parts}, raw?}
  Thread
    {id, historyId, messages: [Message]}
  Label
    {id, name, type, messageListVisibility, labelListVisibility,
     messagesTotal, messagesUnread, threadsTotal, threadsUnread,
     color?}
  Draft
    {id, message}

List responses:
    messages.list  -> {messages: [{id, threadId}], nextPageToken,
                       resultSizeEstimate}
    threads.list   -> {threads: [{id, snippet, historyId}],
                       nextPageToken, resultSizeEstimate}
    labels.list    -> {labels: [Label]}
    drafts.list    -> {drafts: [{id, message: {id, threadId}}], ...}

Upstream tool surface (12):

  send_message, list_messages, get_message, modify_message,
  trash_message, untrash_message,
  list_threads, get_thread,
  list_labels, create_label,
  create_draft, send_draft

Plus mock-only debug tools used by per-task setup/verification:

  mock_debug_state, mock_debug_seed_message, mock_debug_seed_thread,
  mock_debug_seed_label

State — one JSON file at $GMAIL_MOCK_STATE_DIR/state.json:

  state = {
    "user": {"emailAddress": "...", "messagesTotal": ..., ...},
    "messages": {
      "<id>": {"id", "threadId", "labelIds", "snippet", "historyId",
               "internalDate", "sizeEstimate", "payload": {...}}
    },
    "threads": {
      "<id>": {"id", "historyId", "snippet", "messageIds": [...]}
    },
    "labels": {
      "<id>": {"id", "name", "type", "messageListVisibility",
                "labelListVisibility", ...}
    },
    "drafts": {
      "<id>": {"id", "messageId"}
    },
    "next_id": {"history": 1, "user_label": 1},
    "calls": [...],
  }
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import email
import email.policy
import fcntl
import json
import os
import re
import secrets
from email.message import EmailMessage
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "GMAIL_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gmail_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _now_ms() -> int:
    """Gmail `internalDate` is epoch milliseconds, as a string."""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


# Gmail's well-known system labels. Every account has these — they are
# never created by users and their ids match their names.
_SYSTEM_LABELS = [
    ("INBOX",              "labelShow",       "show"),
    ("SENT",               "labelShow",       "show"),
    ("DRAFT",              "labelShow",       "show"),
    ("TRASH",              "labelHide",       "hide"),
    ("SPAM",               "labelHide",       "hide"),
    ("STARRED",            "labelShow",       "show"),
    ("UNREAD",             "labelShow",       "show"),
    ("IMPORTANT",          "labelShow",       "show"),
    ("CHAT",               "labelHide",       "hide"),
    ("CATEGORY_PERSONAL",  "labelShow",       "show"),
    ("CATEGORY_SOCIAL",    "labelShow",       "show"),
    ("CATEGORY_PROMOTIONS","labelShow",       "show"),
    ("CATEGORY_UPDATES",   "labelShow",       "show"),
    ("CATEGORY_FORUMS",    "labelShow",       "show"),
]


def _default_user() -> dict:
    return {
        "emailAddress": "me@gmail.mock",
        "messagesTotal": 0,
        "threadsTotal": 0,
        "historyId": "1",
    }


def _empty_state() -> dict:
    labels = {}
    for name, mlv, llv in _SYSTEM_LABELS:
        labels[name] = {
            "id": name,
            "name": name,
            "type": "system",
            "messageListVisibility": mlv,
            "labelListVisibility": llv,
            "messagesTotal": 0,
            "messagesUnread": 0,
            "threadsTotal": 0,
            "threadsUnread": 0,
        }
    return {
        "user": _default_user(),
        "messages": {},
        "threads": {},
        "labels": labels,
        "drafts": {},
        "next_id": {"history": 1, "user_label": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GMAIL_MOCK_SEED_PATH")
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
# ID generation
# ---------------------------------------------------------------------------

def _gen_message_id() -> str:
    """Gmail message ids are 16-hex-lowercase strings."""
    return secrets.token_hex(8)


def _gen_thread_id() -> str:
    """Gmail thread ids share the message-id shape (16-hex-lowercase)."""
    return secrets.token_hex(8)


def _gen_draft_id() -> str:
    """Gmail draft ids look like `r<digits>` in the real API.
    Mock keeps a similar visual shape."""
    return "r" + str(secrets.randbits(54))


def _gen_history_id(state: dict) -> str:
    n = int(state["next_id"].get("history", 1))
    state["next_id"]["history"] = n + 1
    return str(n)


def _gen_user_label_id(state: dict) -> str:
    n = int(state["next_id"].get("user_label", 1))
    state["next_id"]["user_label"] = n + 1
    return f"Label_{n}"


# ---------------------------------------------------------------------------
# Message / thread / label builders
# ---------------------------------------------------------------------------

def _b64url(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    if not s:
        return b""
    # Restore padding
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _make_headers(*, from_addr: str, to: str | list[str],
                  subject: str, cc: str | list[str] | None = None,
                  bcc: str | list[str] | None = None,
                  date: str | None = None,
                  message_id: str | None = None,
                  extra: list[dict] | None = None) -> list[dict]:
    def _join(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(v)
        return str(v)
    headers = [
        {"name": "From", "value": _join(from_addr)},
        {"name": "To", "value": _join(to)},
    ]
    if cc:
        headers.append({"name": "Cc", "value": _join(cc)})
    if bcc:
        headers.append({"name": "Bcc", "value": _join(bcc)})
    headers.append({"name": "Subject", "value": subject or ""})
    headers.append({"name": "Date", "value": date or _now_iso()})
    headers.append({
        "name": "Message-ID",
        "value": message_id or f"<{secrets.token_hex(12)}@mail.gmail.mock>",
    })
    headers.append({"name": "MIME-Version", "value": "1.0"})
    headers.append({
        "name": "Content-Type", "value": "text/plain; charset=\"UTF-8\""})
    for h in (extra or []):
        if isinstance(h, dict) and "name" in h and "value" in h:
            headers.append({"name": h["name"], "value": h["value"]})
    return headers


def _build_payload(*, headers: list[dict], body_text: str) -> dict:
    """Build a Gmail-API `payload` block. Plain-text single part."""
    body_data = _b64url(body_text or "")
    return {
        "partId": "",
        "mimeType": "text/plain",
        "filename": "",
        "headers": headers,
        "body": {
            "size": len((body_text or "").encode("utf-8")),
            "data": body_data,
        },
    }


def _snippet_of(body_text: str, limit: int = 200) -> str:
    s = " ".join((body_text or "").split())
    return s[:limit]


def _new_message(state: dict, *,
                 from_addr: str,
                 to: str | list[str],
                 subject: str,
                 body: str,
                 cc: str | list[str] | None = None,
                 bcc: str | list[str] | None = None,
                 label_ids: list[str] | None = None,
                 thread_id: str | None = None,
                 internal_date: int | None = None,
                 message_id: str | None = None,
                 date_header: str | None = None,
                 rfc822_message_id: str | None = None) -> dict:
    mid = message_id or _gen_message_id()
    tid = thread_id or _gen_thread_id()
    idate = int(internal_date) if internal_date is not None else _now_ms()
    headers = _make_headers(
        from_addr=from_addr, to=to, subject=subject,
        cc=cc, bcc=bcc, date=date_header,
        message_id=rfc822_message_id,
    )
    payload = _build_payload(headers=headers, body_text=body)
    size_est = len((body or "").encode("utf-8")) + sum(
        len(h["name"]) + len(str(h["value"])) + 4 for h in headers)
    return {
        "id": mid,
        "threadId": tid,
        "labelIds": list(label_ids or []),
        "snippet": _snippet_of(body),
        "historyId": _gen_history_id(state),
        "internalDate": str(idate),
        "sizeEstimate": size_est,
        "payload": payload,
    }


def _ensure_thread(state: dict, thread_id: str, *,
                   message_id: str, snippet: str) -> dict:
    th = state["threads"].get(thread_id)
    if th is None:
        th = {
            "id": thread_id,
            "historyId": _gen_history_id(state),
            "snippet": snippet,
            "messageIds": [],
        }
        state["threads"][thread_id] = th
    if message_id not in th["messageIds"]:
        th["messageIds"].append(message_id)
    th["historyId"] = _gen_history_id(state)
    th["snippet"] = snippet
    return th


def _label_counts_recompute(state: dict) -> None:
    """Recompute per-label messages/threads totals + unread counts."""
    # Build (label -> set(message_ids)) + (label -> set(thread_ids))
    label_msgs: dict[str, set[str]] = {l: set() for l in state["labels"]}
    label_msgs_unread: dict[str, set[str]] = {l: set() for l in state["labels"]}
    label_threads: dict[str, set[str]] = {l: set() for l in state["labels"]}
    label_threads_unread: dict[str, set[str]] = {l: set() for l in state["labels"]}
    for m in state["messages"].values():
        labels = m.get("labelIds") or []
        unread = "UNREAD" in labels
        for lid in labels:
            if lid not in label_msgs:
                continue
            label_msgs[lid].add(m["id"])
            if unread:
                label_msgs_unread[lid].add(m["id"])
            label_threads[lid].add(m["threadId"])
            if unread:
                label_threads_unread[lid].add(m["threadId"])
    for lid, lbl in state["labels"].items():
        lbl["messagesTotal"] = len(label_msgs.get(lid, set()))
        lbl["messagesUnread"] = len(label_msgs_unread.get(lid, set()))
        lbl["threadsTotal"] = len(label_threads.get(lid, set()))
        lbl["threadsUnread"] = len(label_threads_unread.get(lid, set()))
    state["user"]["messagesTotal"] = len(state["messages"])
    state["user"]["threadsTotal"] = len(state["threads"])
    state["user"]["historyId"] = str(
        max(int(state["next_id"].get("history", 1)) - 1, 1))


def _label_name_to_id(state: dict, name: str) -> str | None:
    if not name:
        return None
    if name in state["labels"]:
        return name
    lname = name.lower()
    for lid, lbl in state["labels"].items():
        if lbl.get("name", "").lower() == lname:
            return lid
    return None


def _header_value(payload: dict, name: str) -> str:
    if not payload:
        return ""
    nlower = name.lower()
    for h in payload.get("headers", []) or []:
        if (h.get("name") or "").lower() == nlower:
            return h.get("value", "") or ""
    return ""


def _payload_body_text(payload: dict) -> str:
    if not payload:
        return ""
    body = payload.get("body") or {}
    if body.get("data"):
        try:
            return _b64url_decode(body["data"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = payload.get("parts") or []
    chunks = []
    for p in parts:
        if (p.get("mimeType") or "").startswith("text/"):
            b = (p.get("body") or {}).get("data")
            if b:
                try:
                    chunks.append(
                        _b64url_decode(b).decode("utf-8", errors="replace"))
                except Exception:
                    pass
        elif p.get("parts"):
            chunks.append(_payload_body_text(p))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Search query parser ( q= )
# ---------------------------------------------------------------------------

_OPERATOR_RE = re.compile(
    r"(?P<op>from|to|cc|bcc|subject|label|has|is|in|after|before|"
    r"newer_than|older_than|filename|category):"
    r"(?P<val>\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)


def _parse_query(q: str) -> tuple[list[tuple[str, str]], str]:
    """Return (ops, free_text). Ops are (operator_lower, value_unquoted)."""
    if not q:
        return [], ""
    ops: list[tuple[str, str]] = []
    remainder: list[str] = []
    pos = 0
    for m in _OPERATOR_RE.finditer(q):
        if m.start() > pos:
            remainder.append(q[pos:m.start()])
        val = m.group("val")
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        ops.append((m.group("op").lower(), val))
        pos = m.end()
    if pos < len(q):
        remainder.append(q[pos:])
    free = " ".join(" ".join(remainder).split())
    return ops, free


def _message_matches_query(state: dict, msg: dict, q: str) -> bool:
    if not q:
        return True
    ops, free = _parse_query(q)
    payload = msg.get("payload") or {}
    from_h = _header_value(payload, "From").lower()
    to_h = _header_value(payload, "To").lower()
    cc_h = _header_value(payload, "Cc").lower()
    bcc_h = _header_value(payload, "Bcc").lower()
    subj_h = _header_value(payload, "Subject").lower()
    body_text = _payload_body_text(payload).lower()
    labels = [l.upper() for l in (msg.get("labelIds") or [])]
    has_attachment = any(
        (p.get("filename") or "")
        for p in (payload.get("parts") or [])
    )
    for op, val in ops:
        v = val.lower()
        if op == "from":
            if v not in from_h:
                return False
        elif op == "to":
            if v not in to_h:
                return False
        elif op == "cc":
            if v not in cc_h:
                return False
        elif op == "bcc":
            if v not in bcc_h:
                return False
        elif op == "subject":
            if v not in subj_h:
                return False
        elif op == "label":
            lid = _label_name_to_id(state, val)
            if not lid or lid not in (msg.get("labelIds") or []):
                return False
        elif op == "in":
            # in:inbox / in:sent / in:trash / in:spam / in:anywhere
            target = val.upper()
            if target == "ANYWHERE":
                continue
            mapped = {
                "INBOX": "INBOX", "SENT": "SENT", "DRAFTS": "DRAFT",
                "DRAFT": "DRAFT", "TRASH": "TRASH", "SPAM": "SPAM",
                "STARRED": "STARRED", "IMPORTANT": "IMPORTANT",
                "UNREAD": "UNREAD",
            }.get(target, target)
            if mapped not in labels:
                return False
        elif op == "is":
            v2 = val.lower()
            if v2 == "unread":
                if "UNREAD" not in labels:
                    return False
            elif v2 == "read":
                if "UNREAD" in labels:
                    return False
            elif v2 == "starred":
                if "STARRED" not in labels:
                    return False
            elif v2 == "important":
                if "IMPORTANT" not in labels:
                    return False
            elif v2 in ("draft", "drafts"):
                if "DRAFT" not in labels:
                    return False
            elif v2 == "sent":
                if "SENT" not in labels:
                    return False
            elif v2 == "trash":
                if "TRASH" not in labels:
                    return False
            elif v2 == "spam":
                if "SPAM" not in labels:
                    return False
        elif op == "has":
            if val.lower() == "attachment":
                if not has_attachment:
                    return False
        elif op == "category":
            lid = f"CATEGORY_{val.upper()}"
            if lid not in (msg.get("labelIds") or []):
                return False
        elif op in ("after", "newer_than"):
            # Best-effort: compare against internalDate.
            try:
                # `after:YYYY/MM/DD`
                if op == "after":
                    d = datetime.datetime.strptime(
                        val.replace("-", "/"), "%Y/%m/%d")
                    if int(msg.get("internalDate", "0")) < int(d.timestamp() * 1000):
                        return False
            except Exception:
                pass
        elif op in ("before", "older_than"):
            try:
                if op == "before":
                    d = datetime.datetime.strptime(
                        val.replace("-", "/"), "%Y/%m/%d")
                    if int(msg.get("internalDate", "0")) >= int(d.timestamp() * 1000):
                        return False
            except Exception:
                pass
        elif op == "filename":
            ok = False
            for p in (payload.get("parts") or []):
                if val.lower() in (p.get("filename") or "").lower():
                    ok = True
                    break
            if not ok:
                return False
    if free:
        hay = " ".join([from_h, to_h, cc_h, subj_h, body_text,
                        msg.get("snippet", "").lower()])
        for term in free.split():
            t = term.strip("'\"").lower()
            if not t:
                continue
            if t not in hay:
                return False
    return True


# ---------------------------------------------------------------------------
# RFC 2822 raw parsing (for users.messages.send with `raw`)
# ---------------------------------------------------------------------------

def _parse_raw_rfc822(raw_b64url: str) -> dict:
    """Decode a base64url RFC2822 message into a Gmail-like payload dict.

    Returns {"from", "to", "cc", "bcc", "subject", "body", "headers",
             "message_id_header", "date_header"}.
    Best-effort: malformed input returns empty fields.
    """
    out = {
        "from": "", "to": "", "cc": "", "bcc": "",
        "subject": "", "body": "", "headers": [],
        "message_id_header": None, "date_header": None,
    }
    if not raw_b64url:
        return out
    try:
        raw = _b64url_decode(raw_b64url)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return out
    out["from"] = str(msg.get("From", "") or "")
    out["to"] = str(msg.get("To", "") or "")
    out["cc"] = str(msg.get("Cc", "") or "")
    out["bcc"] = str(msg.get("Bcc", "") or "")
    out["subject"] = str(msg.get("Subject", "") or "")
    out["date_header"] = str(msg.get("Date", "") or "") or None
    out["message_id_header"] = (
        str(msg.get("Message-ID", "") or "") or None
    )
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type() or ""
            if ctype == "text/plain":
                try:
                    body_text = part.get_content()
                    break
                except Exception:
                    pass
        if not body_text:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        body_text = part.get_content()
                        break
                    except Exception:
                        pass
    else:
        try:
            body_text = msg.get_content()
        except Exception:
            body_text = msg.get_payload(decode=False) or ""
    if isinstance(body_text, bytes):
        try:
            body_text = body_text.decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
    out["body"] = body_text or ""
    # Pass through arbitrary headers verbatim
    for k, v in msg.items():
        out["headers"].append({"name": k, "value": str(v)})
    return out


def _build_raw_b64url(from_addr: str, to: str | list[str],
                      subject: str, body: str,
                      cc: str | list[str] | None = None) -> str:
    """Construct a minimal RFC2822 message and return base64url."""
    def _join(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(v)
        return str(v)
    em = EmailMessage()
    em["From"] = _join(from_addr)
    em["To"] = _join(to)
    if cc:
        em["Cc"] = _join(cc)
    em["Subject"] = subject or ""
    em.set_content(body or "")
    return _b64url(em.as_bytes())


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("gmail-mock")


# ===========================================================================
# Messages
# ===========================================================================

_FORMAT_VALUES = {"minimal", "metadata", "full", "raw"}


def _format_message(state: dict, msg: dict, *, format: str = "full",
                    metadataHeaders: list[str] | None = None) -> dict:
    """Render a message resource according to the format requested,
    matching the Gmail API `users.messages.get` format options."""
    fmt = (format or "full").lower()
    if fmt not in _FORMAT_VALUES:
        fmt = "full"
    base = {
        "id": msg["id"],
        "threadId": msg["threadId"],
        "labelIds": list(msg.get("labelIds") or []),
        "snippet": msg.get("snippet", ""),
        "historyId": msg.get("historyId", "1"),
        "internalDate": str(msg.get("internalDate", _now_ms())),
        "sizeEstimate": int(msg.get("sizeEstimate", 0)),
    }
    if fmt == "minimal":
        return base
    payload = msg.get("payload") or {}
    if fmt == "metadata":
        keep = {(h or "").lower() for h in (metadataHeaders or [])}
        headers = payload.get("headers", []) or []
        if keep:
            headers = [h for h in headers
                       if (h.get("name") or "").lower() in keep]
        return {
            **base,
            "payload": {
                "partId": payload.get("partId", ""),
                "mimeType": payload.get("mimeType", "text/plain"),
                "filename": payload.get("filename", ""),
                "headers": headers,
            },
        }
    if fmt == "raw":
        # Build a deterministic raw blob from the stored payload.
        from_h = _header_value(payload, "From")
        to_h = _header_value(payload, "To")
        cc_h = _header_value(payload, "Cc")
        subj_h = _header_value(payload, "Subject")
        body_text = _payload_body_text(payload)
        raw_b64 = _build_raw_b64url(from_h, to_h, subj_h, body_text,
                                    cc=cc_h or None)
        return {**base, "raw": raw_b64, "payload": payload}
    # full
    return {**base, "payload": payload}


@mcp.tool(name="send_message")
def send_message(userId: str = "me",
                 raw: str | None = None,
                 to: str | list[str] | None = None,
                 subject: str | None = None,
                 body: str | None = None,
                 cc: str | list[str] | None = None,
                 bcc: str | list[str] | None = None,
                 threadId: str | None = None) -> dict:
    """Gmail API: `users.messages.send` —
    POST /gmail/v1/users/{userId}/messages/send.

    Accepts either the raw RFC2822 message (base64url, `raw=`) the
    real API requires, or the convenience trio `to`/`subject`/`body`
    that most MCP wrappers expose. Returns the created Message
    resource (id + labelIds=[SENT]).
    """
    with _lock():
        s = _load_state()
        # Parse `raw` if provided, else fall back to convenience args.
        if raw:
            parsed = _parse_raw_rfc822(raw)
            from_addr = parsed["from"] or s["user"]["emailAddress"]
            to_v = parsed["to"] or (to or "")
            subject_v = parsed["subject"] or (subject or "")
            body_v = parsed["body"] or (body or "")
            cc_v = parsed["cc"] or cc
            bcc_v = parsed["bcc"] or bcc
            date_header = parsed["date_header"]
            rfc_mid = parsed["message_id_header"]
        else:
            from_addr = s["user"]["emailAddress"]
            to_v = to or ""
            subject_v = subject or ""
            body_v = body or ""
            cc_v = cc
            bcc_v = bcc
            date_header = None
            rfc_mid = None

        if not to_v:
            _record(s, "send_message", result="no_recipient")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message":
                        "Recipient address required (To header missing).",
                    "status": "INVALID_ARGUMENT",
                }
            }
        tid = threadId or _gen_thread_id()
        labels = ["SENT"]
        msg = _new_message(
            s,
            from_addr=from_addr,
            to=to_v, subject=subject_v, body=body_v,
            cc=cc_v, bcc=bcc_v,
            label_ids=labels, thread_id=tid,
            date_header=date_header,
            rfc822_message_id=rfc_mid,
        )
        s["messages"][msg["id"]] = msg
        _ensure_thread(s, tid,
                       message_id=msg["id"],
                       snippet=msg.get("snippet", ""))
        _label_counts_recompute(s)
        _record(s, "send_message",
                message_id=msg["id"], thread_id=tid,
                to=to_v if isinstance(to_v, str) else ", ".join(to_v),
                subject=subject_v)
        _save_state(s)
        return _format_message(s, msg, format="full")


@mcp.tool(name="list_messages")
def list_messages(userId: str = "me",
                  q: str = "",
                  labelIds: list[str] | None = None,
                  maxResults: int = 100,
                  pageToken: str | None = None,
                  includeSpamTrash: bool = False) -> dict:
    """Gmail API: `users.messages.list` —
    GET /gmail/v1/users/{userId}/messages.

    Returns `{messages: [{id, threadId}], nextPageToken,
    resultSizeEstimate}`. Filtering: `q` is a Gmail search query
    (`from:`/`to:`/`subject:`/`label:`/`is:`/`has:`/free-text);
    `labelIds` AND-filters the result; `includeSpamTrash=False`
    drops messages labeled SPAM or TRASH unless explicitly asked
    for via `q`.
    """
    with _lock():
        s = _load_state()
        msgs = list(s["messages"].values())
        # ordering: newest first (largest internalDate)
        msgs.sort(key=lambda m: int(m.get("internalDate", "0")),
                  reverse=True)
        if not includeSpamTrash:
            msgs = [m for m in msgs
                    if "SPAM" not in (m.get("labelIds") or [])
                    and "TRASH" not in (m.get("labelIds") or [])]
        if labelIds:
            need = set(labelIds)
            msgs = [m for m in msgs
                    if need.issubset(set(m.get("labelIds") or []))]
        if q:
            msgs = [m for m in msgs if _message_matches_query(s, m, q)]
        # pagination
        try:
            offset = int(pageToken) if pageToken else 0
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(int(maxResults or 100), 500))
        page = msgs[offset: offset + limit]
        next_token = (str(offset + limit)
                      if offset + limit < len(msgs) else None)
        out_msgs = [{"id": m["id"], "threadId": m["threadId"]} for m in page]
        result: dict[str, Any] = {
            "messages": out_msgs,
            "resultSizeEstimate": len(msgs),
        }
        if next_token:
            result["nextPageToken"] = next_token
        _record(s, "list_messages", count=len(out_msgs),
                total=len(msgs), q=q, labelIds=list(labelIds or []))
        _save_state(s)
        return result


@mcp.tool(name="get_message")
def get_message(messageId: str, userId: str = "me",
                format: str = "full",
                metadataHeaders: list[str] | None = None) -> dict:
    """Gmail API: `users.messages.get` —
    GET /gmail/v1/users/{userId}/messages/{id}.

    `format` is one of `minimal` | `metadata` | `full` | `raw`.
    Returns the Message resource.
    """
    with _lock():
        s = _load_state()
        msg = s["messages"].get(messageId)
        if not msg:
            _record(s, "get_message", message_id=messageId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {messageId}",
                    "status": "NOT_FOUND",
                }
            }
        _record(s, "get_message", message_id=messageId, format=format)
        _save_state(s)
        return _format_message(s, msg, format=format,
                               metadataHeaders=metadataHeaders)


@mcp.tool(name="modify_message")
def modify_message(messageId: str,
                   userId: str = "me",
                   addLabelIds: list[str] | None = None,
                   removeLabelIds: list[str] | None = None) -> dict:
    """Gmail API: `users.messages.modify` —
    POST /gmail/v1/users/{userId}/messages/{id}/modify.

    Adds and/or removes the named labels on a message. Returns the
    modified Message resource (full format).
    """
    with _lock():
        s = _load_state()
        msg = s["messages"].get(messageId)
        if not msg:
            _record(s, "modify_message", message_id=messageId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {messageId}",
                    "status": "NOT_FOUND",
                }
            }
        # Resolve label ids (allow case-insensitive name match for system
        # labels and user labels alike).
        cur = list(msg.get("labelIds") or [])
        for lid in (addLabelIds or []):
            resolved = _label_name_to_id(s, lid) or lid
            if resolved in s["labels"] and resolved not in cur:
                cur.append(resolved)
        for lid in (removeLabelIds or []):
            resolved = _label_name_to_id(s, lid) or lid
            if resolved in cur:
                cur.remove(resolved)
        msg["labelIds"] = cur
        msg["historyId"] = _gen_history_id(s)
        _label_counts_recompute(s)
        _record(s, "modify_message", message_id=messageId,
                add=list(addLabelIds or []),
                remove=list(removeLabelIds or []))
        _save_state(s)
        return _format_message(s, msg, format="full")


@mcp.tool(name="trash_message")
def trash_message(messageId: str, userId: str = "me") -> dict:
    """Gmail API: `users.messages.trash` —
    POST /gmail/v1/users/{userId}/messages/{id}/trash.

    Moves the message to the Trash by adding the TRASH label and
    removing INBOX. Returns the modified Message resource.
    """
    with _lock():
        s = _load_state()
        msg = s["messages"].get(messageId)
        if not msg:
            _record(s, "trash_message", message_id=messageId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {messageId}",
                    "status": "NOT_FOUND",
                }
            }
        cur = set(msg.get("labelIds") or [])
        cur.discard("INBOX")
        cur.add("TRASH")
        msg["labelIds"] = sorted(cur)
        msg["historyId"] = _gen_history_id(s)
        _label_counts_recompute(s)
        _record(s, "trash_message", message_id=messageId)
        _save_state(s)
        return _format_message(s, msg, format="full")


@mcp.tool(name="untrash_message")
def untrash_message(messageId: str, userId: str = "me") -> dict:
    """Gmail API: `users.messages.untrash` —
    POST /gmail/v1/users/{userId}/messages/{id}/untrash."""
    with _lock():
        s = _load_state()
        msg = s["messages"].get(messageId)
        if not msg:
            _record(s, "untrash_message", message_id=messageId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {messageId}",
                    "status": "NOT_FOUND",
                }
            }
        cur = set(msg.get("labelIds") or [])
        cur.discard("TRASH")
        cur.add("INBOX")
        msg["labelIds"] = sorted(cur)
        msg["historyId"] = _gen_history_id(s)
        _label_counts_recompute(s)
        _record(s, "untrash_message", message_id=messageId)
        _save_state(s)
        return _format_message(s, msg, format="full")


# ===========================================================================
# Threads
# ===========================================================================

@mcp.tool(name="list_threads")
def list_threads(userId: str = "me",
                 q: str = "",
                 labelIds: list[str] | None = None,
                 maxResults: int = 100,
                 pageToken: str | None = None,
                 includeSpamTrash: bool = False) -> dict:
    """Gmail API: `users.threads.list` —
    GET /gmail/v1/users/{userId}/threads.

    Returns `{threads: [{id, snippet, historyId}], nextPageToken,
    resultSizeEstimate}`. Filtering mirrors `users.messages.list`.
    """
    with _lock():
        s = _load_state()
        threads = list(s["threads"].values())

        def _thread_latest_ms(th: dict) -> int:
            best = 0
            for mid in th.get("messageIds") or []:
                m = s["messages"].get(mid)
                if m:
                    best = max(best, int(m.get("internalDate", "0")))
            return best

        threads.sort(key=_thread_latest_ms, reverse=True)

        def _thread_messages(th: dict) -> list[dict]:
            return [s["messages"][mid] for mid in (th.get("messageIds") or [])
                    if mid in s["messages"]]

        if not includeSpamTrash:
            kept = []
            for th in threads:
                msgs = _thread_messages(th)
                if any("SPAM" not in (m.get("labelIds") or [])
                       and "TRASH" not in (m.get("labelIds") or [])
                       for m in msgs):
                    kept.append(th)
            threads = kept
        if labelIds:
            need = set(labelIds)
            kept = []
            for th in threads:
                msgs = _thread_messages(th)
                thread_labels: set[str] = set()
                for m in msgs:
                    thread_labels.update(m.get("labelIds") or [])
                if need.issubset(thread_labels):
                    kept.append(th)
            threads = kept
        if q:
            kept = []
            for th in threads:
                msgs = _thread_messages(th)
                if any(_message_matches_query(s, m, q) for m in msgs):
                    kept.append(th)
            threads = kept
        try:
            offset = int(pageToken) if pageToken else 0
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(int(maxResults or 100), 500))
        page = threads[offset: offset + limit]
        next_token = (str(offset + limit)
                      if offset + limit < len(threads) else None)
        out = []
        for th in page:
            out.append({
                "id": th["id"],
                "snippet": th.get("snippet", ""),
                "historyId": th.get("historyId", "1"),
            })
        result: dict[str, Any] = {
            "threads": out,
            "resultSizeEstimate": len(threads),
        }
        if next_token:
            result["nextPageToken"] = next_token
        _record(s, "list_threads", count=len(out),
                total=len(threads), q=q)
        _save_state(s)
        return result


@mcp.tool(name="get_thread")
def get_thread(threadId: str, userId: str = "me",
               format: str = "full",
               metadataHeaders: list[str] | None = None) -> dict:
    """Gmail API: `users.threads.get` —
    GET /gmail/v1/users/{userId}/threads/{id}.

    Returns the thread + every message in it (formatted per the
    `format` arg, same options as `users.messages.get`).
    """
    with _lock():
        s = _load_state()
        th = s["threads"].get(threadId)
        if not th:
            _record(s, "get_thread", thread_id=threadId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {threadId}",
                    "status": "NOT_FOUND",
                }
            }
        msgs = []
        for mid in th.get("messageIds") or []:
            m = s["messages"].get(mid)
            if m:
                msgs.append(_format_message(s, m, format=format,
                                            metadataHeaders=metadataHeaders))
        _record(s, "get_thread", thread_id=threadId,
                messages=len(msgs))
        _save_state(s)
        return {
            "id": th["id"],
            "historyId": th.get("historyId", "1"),
            "messages": msgs,
        }


# ===========================================================================
# Labels
# ===========================================================================

@mcp.tool(name="list_labels")
def list_labels(userId: str = "me") -> dict:
    """Gmail API: `users.labels.list` —
    GET /gmail/v1/users/{userId}/labels.

    Returns every label visible to the user (system + user-created).
    Per the API, the response is `{labels: [Label]}` with NO
    pagination token.
    """
    with _lock():
        s = _load_state()
        labels = []
        # System labels first, sorted; then user labels by id-numeric.
        sys_labels = [l for l in s["labels"].values()
                      if l.get("type") == "system"]
        usr_labels = [l for l in s["labels"].values()
                      if l.get("type") == "user"]
        sys_labels.sort(key=lambda l: l["id"])
        usr_labels.sort(key=lambda l: l["id"])
        for l in sys_labels + usr_labels:
            labels.append(dict(l))
        _record(s, "list_labels", count=len(labels))
        _save_state(s)
        return {"labels": labels}


@mcp.tool(name="create_label")
def create_label(userId: str = "me",
                 name: str = "",
                 labelListVisibility: str = "labelShow",
                 messageListVisibility: str = "show",
                 color: dict | None = None) -> dict:
    """Gmail API: `users.labels.create` —
    POST /gmail/v1/users/{userId}/labels.

    Creates a new user label. `labelListVisibility` is one of
    `labelShow` | `labelShowIfUnread` | `labelHide`;
    `messageListVisibility` is `show` | `hide`. Returns the created
    Label resource.
    """
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_label", result="missing_name")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": "Label name is required.",
                    "status": "INVALID_ARGUMENT",
                }
            }
        # Reject duplicate names (case-sensitive, matches Gmail).
        for lbl in s["labels"].values():
            if lbl.get("name") == name:
                _record(s, "create_label", result="conflict", name=name)
                _save_state(s)
                return {
                    "error": {
                        "code": 409,
                        "message": f"Label name exists or conflicts: {name}",
                        "status": "ALREADY_EXISTS",
                    }
                }
        lid = _gen_user_label_id(s)
        label = {
            "id": lid,
            "name": name,
            "type": "user",
            "messageListVisibility": messageListVisibility,
            "labelListVisibility": labelListVisibility,
            "messagesTotal": 0,
            "messagesUnread": 0,
            "threadsTotal": 0,
            "threadsUnread": 0,
        }
        if color and isinstance(color, dict):
            label["color"] = {
                "textColor": color.get("textColor", "#000000"),
                "backgroundColor": color.get("backgroundColor", "#ffffff"),
            }
        s["labels"][lid] = label
        _record(s, "create_label", label_id=lid, name=name)
        _save_state(s)
        return dict(label)


# ===========================================================================
# Drafts
# ===========================================================================

@mcp.tool(name="create_draft")
def create_draft(userId: str = "me",
                 raw: str | None = None,
                 to: str | list[str] | None = None,
                 subject: str | None = None,
                 body: str | None = None,
                 cc: str | list[str] | None = None,
                 bcc: str | list[str] | None = None,
                 threadId: str | None = None) -> dict:
    """Gmail API: `users.drafts.create` —
    POST /gmail/v1/users/{userId}/drafts.

    Creates a draft message in the user's Drafts folder. Accepts
    either `raw` (base64url RFC2822) or the convenience args
    `to`/`subject`/`body`. Returns the Draft resource
    `{id, message}`.
    """
    with _lock():
        s = _load_state()
        if raw:
            parsed = _parse_raw_rfc822(raw)
            from_addr = parsed["from"] or s["user"]["emailAddress"]
            to_v = parsed["to"] or (to or "")
            subject_v = parsed["subject"] or (subject or "")
            body_v = parsed["body"] or (body or "")
            cc_v = parsed["cc"] or cc
            bcc_v = parsed["bcc"] or bcc
            date_header = parsed["date_header"]
            rfc_mid = parsed["message_id_header"]
        else:
            from_addr = s["user"]["emailAddress"]
            to_v = to or ""
            subject_v = subject or ""
            body_v = body or ""
            cc_v = cc
            bcc_v = bcc
            date_header = None
            rfc_mid = None
        tid = threadId or _gen_thread_id()
        msg = _new_message(
            s,
            from_addr=from_addr,
            to=to_v, subject=subject_v, body=body_v,
            cc=cc_v, bcc=bcc_v,
            label_ids=["DRAFT"],
            thread_id=tid,
            date_header=date_header,
            rfc822_message_id=rfc_mid,
        )
        s["messages"][msg["id"]] = msg
        _ensure_thread(s, tid, message_id=msg["id"],
                       snippet=msg.get("snippet", ""))
        did = _gen_draft_id()
        s["drafts"][did] = {"id": did, "messageId": msg["id"]}
        _label_counts_recompute(s)
        _record(s, "create_draft", draft_id=did, message_id=msg["id"],
                subject=subject_v)
        _save_state(s)
        return {
            "id": did,
            "message": _format_message(s, msg, format="full"),
        }


@mcp.tool(name="send_draft")
def send_draft(draftId: str, userId: str = "me") -> dict:
    """Gmail API: `users.drafts.send` —
    POST /gmail/v1/users/{userId}/drafts/send.

    Sends an existing draft. Removes DRAFT, adds SENT, removes the
    underlying draft record. Returns the resulting Message resource.
    """
    with _lock():
        s = _load_state()
        draft = s["drafts"].get(draftId)
        if not draft:
            _record(s, "send_draft", draft_id=draftId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Requested entity was not found: {draftId}",
                    "status": "NOT_FOUND",
                }
            }
        msg = s["messages"].get(draft["messageId"])
        if not msg:
            del s["drafts"][draftId]
            _record(s, "send_draft", draft_id=draftId,
                    result="orphan_draft")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message":
                        f"Draft references missing message: {draft['messageId']}",
                    "status": "NOT_FOUND",
                }
            }
        # Validate: must have at least one recipient.
        to_h = _header_value(msg.get("payload") or {}, "To")
        if not to_h:
            _record(s, "send_draft", draft_id=draftId,
                    result="no_recipient")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message":
                        "Recipient address required (To header missing).",
                    "status": "INVALID_ARGUMENT",
                }
            }
        labels = set(msg.get("labelIds") or [])
        labels.discard("DRAFT")
        labels.add("SENT")
        msg["labelIds"] = sorted(labels)
        msg["historyId"] = _gen_history_id(s)
        del s["drafts"][draftId]
        _label_counts_recompute(s)
        _record(s, "send_draft", draft_id=draftId,
                message_id=msg["id"])
        _save_state(s)
        return _format_message(s, msg, format="full")


# ===========================================================================
# Mock-only debug helpers
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not part of the real
    Gmail API surface."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_message")
def mock_debug_seed_message(messageId: str | None = None,
                            threadId: str | None = None,
                            from_addr: str = "",
                            to: str | list[str] = "",
                            subject: str = "",
                            body: str = "",
                            cc: str | list[str] | None = None,
                            bcc: str | list[str] | None = None,
                            labelIds: list[str] | None = None,
                            internalDate: int | None = None,
                            snippet: str | None = None) -> dict:
    """Mock-only: insert a fully-formed Message fixture.

    Returns the seeded Message resource (Gmail API v1 shape).
    """
    with _lock():
        s = _load_state()
        msg = _new_message(
            s,
            from_addr=from_addr or s["user"]["emailAddress"],
            to=to, subject=subject, body=body,
            cc=cc, bcc=bcc,
            label_ids=labelIds, thread_id=threadId,
            internal_date=internalDate,
            message_id=messageId,
        )
        if snippet:
            msg["snippet"] = snippet
        s["messages"][msg["id"]] = msg
        _ensure_thread(s, msg["threadId"],
                       message_id=msg["id"],
                       snippet=msg.get("snippet", ""))
        _label_counts_recompute(s)
        _record(s, "debug_seed_message",
                message_id=msg["id"], thread_id=msg["threadId"])
        _save_state(s)
        return _format_message(s, msg, format="full")


@mcp.tool(name="mock_debug_seed_thread")
def mock_debug_seed_thread(threadId: str | None = None,
                           messages: list[dict] | None = None) -> dict:
    """Mock-only: insert a multi-message thread fixture.

    `messages` is a list of {"from_addr","to","subject","body",
    "labelIds"?, "internalDate"?, "messageId"?, "cc"?, "bcc"?}.
    All messages share the resulting threadId.
    """
    with _lock():
        s = _load_state()
        tid = threadId or _gen_thread_id()
        created = []
        for m in messages or []:
            msg = _new_message(
                s,
                from_addr=m.get("from_addr") or s["user"]["emailAddress"],
                to=m.get("to", ""),
                subject=m.get("subject", ""),
                body=m.get("body", ""),
                cc=m.get("cc"), bcc=m.get("bcc"),
                label_ids=m.get("labelIds"),
                thread_id=tid,
                internal_date=m.get("internalDate"),
                message_id=m.get("messageId"),
            )
            s["messages"][msg["id"]] = msg
            _ensure_thread(s, tid, message_id=msg["id"],
                           snippet=msg.get("snippet", ""))
            created.append(msg["id"])
        _label_counts_recompute(s)
        _record(s, "debug_seed_thread",
                thread_id=tid, messages=created)
        _save_state(s)
        th = s["threads"].get(tid, {})
        return {
            "id": tid,
            "historyId": th.get("historyId", "1"),
            "messages": [_format_message(s, s["messages"][mid], format="full")
                         for mid in created],
        }


@mcp.tool(name="mock_debug_seed_label")
def mock_debug_seed_label(labelId: str | None = None,
                          name: str = "",
                          type_: str = "user",
                          labelListVisibility: str = "labelShow",
                          messageListVisibility: str = "show") -> dict:
    """Mock-only: insert a label fixture. `labelId` defaults to a
    fresh user-label id (`Label_N`) when omitted."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "debug_seed_label", result="missing_name")
            _save_state(s)
            return {"error": "name is required"}
        lid = labelId or (_gen_user_label_id(s)
                          if type_ != "system" else name.upper())
        label = {
            "id": lid,
            "name": name,
            "type": type_,
            "messageListVisibility": messageListVisibility,
            "labelListVisibility": labelListVisibility,
            "messagesTotal": 0,
            "messagesUnread": 0,
            "threadsTotal": 0,
            "threadsUnread": 0,
        }
        s["labels"][lid] = label
        _record(s, "debug_seed_label", label_id=lid, name=name)
        _save_state(s)
        return dict(label)


if __name__ == "__main__":
    mcp.run()
