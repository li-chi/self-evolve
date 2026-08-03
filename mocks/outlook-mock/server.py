"""Outlook mock MCP server.

Mirrors the Microsoft Graph v1.0 mail + calendar surface
(https://learn.microsoft.com/en-us/graph/api/overview). Tool names map
to Graph operation IDs (snake_case) and return JSON payloads that
match the real Graph response shapes: list responses come back as
`{"@odata.context": "...", "value": [...], "@odata.nextLink": "..."}`
and errors as `{"error": {"code": "...", "message": "..."}}`.

State lives in a single JSON file at
`$OUTLOOK_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/outlook_mock`). Per-rollout isolation should clear the
state dir between rollouts. `OUTLOOK_MOCK_SEED_PATH` preloads state
when no state.json exists yet.

Every tool call appends an entry to `state["calls"]` so verifiers
can replay the trace.

Tools (one per Graph operation):

  Mail messages
    list_messages, get_message, send_mail, reply_mail, forward_mail,
    delete_message, move_message, create_draft, send_draft
  Mail folders
    list_mail_folders, create_mail_folder
  Calendar events
    list_events, get_event, create_event, update_event, delete_event,
    accept_event, decline_event, tentatively_accept_event
  Calendars
    list_calendars, get_calendar
  Contacts
    list_contacts

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import json
import os
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "OUTLOOK_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/outlook_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _graph_id(prefix: str = "") -> str:
    """Return a long base64-ish Graph-style id (URL-safe, no padding)."""
    raw = secrets.token_bytes(64)
    s = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{prefix}{s}" if prefix else s


# Stable well-known Graph mailFolder ids (Graph uses opaque strings, but
# also accepts well-known names like "inbox", "sentitems", "drafts", etc.
# — we keep the well-known names as canonical ids for readability).
_WELL_KNOWN_FOLDERS = [
    ("inbox",        "Inbox"),
    ("drafts",       "Drafts"),
    ("sentitems",    "Sent Items"),
    ("deleteditems", "Deleted Items"),
    ("junkemail",    "Junk Email"),
    ("outbox",       "Outbox"),
    ("archive",      "Archive"),
]


def _default_user() -> dict:
    return {
        "id": _graph_id(),
        "displayName": "Mock User",
        "userPrincipalName": "mockuser@mock.onmicrosoft.com",
        "mail": "mockuser@mock.onmicrosoft.com",
        "mailboxSettings": {"timeZone": "UTC"},
    }


def _empty_state() -> dict:
    user = _default_user()
    folders = {}
    for fid, name in _WELL_KNOWN_FOLDERS:
        folders[fid] = {
            "id": fid,
            "displayName": name,
            "parentFolderId": None,
            "childFolderCount": 0,
            "unreadItemCount": 0,
            "totalItemCount": 0,
            "isHidden": False,
            "wellKnownName": fid,
        }
    default_cal_id = _graph_id()
    calendars = {
        default_cal_id: {
            "id": default_cal_id,
            "name": "Calendar",
            "color": "auto",
            "hexColor": "",
            "isDefaultCalendar": True,
            "changeKey": _graph_id()[:22],
            "canShare": True,
            "canViewPrivateItems": True,
            "canEdit": True,
            "allowedOnlineMeetingProviders": ["teamsForBusiness"],
            "defaultOnlineMeetingProvider": "teamsForBusiness",
            "isTallyingResponses": True,
            "isRemovable": False,
            "owner": {
                "name": user["displayName"],
                "address": user["mail"],
            },
        }
    }
    return {
        "user": user,
        "default_calendar_id": default_cal_id,
        "folders": folders,            # folder_id -> folder dict
        "messages": {},                # message_id -> message dict
        "events": {},                  # event_id -> event dict
        "calendars": calendars,        # cal_id -> calendar dict
        "contacts": {},                # contact_id -> contact dict
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("OUTLOOK_MOCK_SEED_PATH")
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
# Graph response helpers
# ---------------------------------------------------------------------------

def _err(code: str, message: str, *, status: int = 400) -> dict:
    """Graph-style error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "innerError": {
                "date": _now_iso(),
                "request-id": _graph_id()[:36],
                "client-request-id": _graph_id()[:36],
            },
        },
        # status is not part of the wire body but useful for callers; the
        # real REST surface signals via HTTP status. We include it as a
        # hint and verifiers can ignore it.
        "_status": status,
    }


def _list(value: list[dict], context_path: str,
          next_link: str | None = None,
          extra: dict | None = None) -> dict:
    out: dict[str, Any] = {
        "@odata.context": f"{GRAPH_BASE}/$metadata#{context_path}",
        "value": value,
    }
    if next_link:
        out["@odata.nextLink"] = next_link
    if extra:
        out.update(extra)
    return out


def _resolve_folder_id(state: dict, folder: str) -> str | None:
    """Accept either a well-known name or a real folder id."""
    if not folder:
        return None
    if folder in state["folders"]:
        return folder
    # case-insensitive well-known match
    fl = folder.lower()
    for fid, f in state["folders"].items():
        if (f.get("wellKnownName", "").lower() == fl
                or f.get("displayName", "").lower() == fl):
            return fid
    return None


def _addr(name_or_addr: str | dict | None) -> dict | None:
    """Normalize an address into Graph emailAddress shape."""
    if name_or_addr is None:
        return None
    if isinstance(name_or_addr, dict):
        ea = name_or_addr.get("emailAddress", name_or_addr)
        return {
            "name": ea.get("name", ea.get("address", "")),
            "address": ea.get("address", ""),
        }
    return {"name": name_or_addr, "address": name_or_addr}


def _recipient(r: str | dict) -> dict:
    """Coerce a recipient input into Graph's `{emailAddress: {name, address}}`."""
    if isinstance(r, dict):
        if "emailAddress" in r:
            ea = r["emailAddress"]
            return {"emailAddress": {
                "name": ea.get("name", ea.get("address", "")),
                "address": ea.get("address", ""),
            }}
        return {"emailAddress": {
            "name": r.get("name", r.get("address", "")),
            "address": r.get("address", ""),
        }}
    return {"emailAddress": {"name": r, "address": r}}


def _recipients(rs: Any) -> list[dict]:
    if rs is None:
        return []
    if isinstance(rs, str):
        # comma-separated string
        return [_recipient(p.strip()) for p in rs.split(",") if p.strip()]
    if isinstance(rs, list):
        return [_recipient(r) for r in rs]
    return []


def _body(body: Any, default_content_type: str = "HTML") -> dict:
    """Coerce a body argument into Graph `{contentType, content}`."""
    if isinstance(body, dict):
        return {
            "contentType": body.get("contentType", default_content_type),
            "content": body.get("content", ""),
        }
    return {"contentType": default_content_type, "content": body or ""}


def _strip_internal(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _now_iso_ms() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


# ---------------------------------------------------------------------------
# Message + event builders
# ---------------------------------------------------------------------------

def _new_message(state: dict, *,
                 subject: str,
                 body: Any,
                 to_recipients: Any,
                 cc_recipients: Any = None,
                 bcc_recipients: Any = None,
                 from_addr: dict | None = None,
                 importance: str = "normal",
                 is_draft: bool = False,
                 folder_id: str = "sentitems",
                 in_reply_to: str | None = None) -> dict:
    mid = _graph_id("AAMkAD")
    now = _now_iso()
    user = state["user"]
    msg = {
        "@odata.etag": f'W/"{_graph_id()[:22]}"',
        "id": mid,
        "createdDateTime": now,
        "lastModifiedDateTime": now,
        "changeKey": _graph_id()[:22],
        "categories": [],
        "receivedDateTime": now,
        "sentDateTime": now,
        "hasAttachments": False,
        "internetMessageId": f"<{_graph_id()[:32]}@mock.onmicrosoft.com>",
        "subject": subject or "",
        "bodyPreview": (
            (body.get("content") if isinstance(body, dict) else (body or ""))
            or ""
        )[:255],
        "importance": importance,
        "parentFolderId": folder_id,
        "conversationId": _graph_id(),
        "conversationIndex": base64.b64encode(secrets.token_bytes(22)).decode(),
        "isDeliveryReceiptRequested": None,
        "isReadReceiptRequested": False,
        "isRead": is_draft,
        "isDraft": is_draft,
        "webLink": (
            f"https://outlook.office365.com/owa/?ItemID={mid}"
            f"&exvsurl=1&viewmodel=ReadMessageItem"
        ),
        "inferenceClassification": "focused",
        "body": _body(body),
        "sender": {"emailAddress": _addr(from_addr) or {
            "name": user["displayName"], "address": user["mail"]}},
        "from": {"emailAddress": _addr(from_addr) or {
            "name": user["displayName"], "address": user["mail"]}},
        "toRecipients": _recipients(to_recipients),
        "ccRecipients": _recipients(cc_recipients),
        "bccRecipients": _recipients(bcc_recipients),
        "replyTo": [],
        "flag": {"flagStatus": "notFlagged"},
    }
    if in_reply_to:
        msg["inReplyTo"] = in_reply_to
    return msg


def _new_event(state: dict, *,
               subject: str,
               body: Any = None,
               start: dict | None = None,
               end: dict | None = None,
               is_all_day: bool = False,
               location: dict | str | None = None,
               attendees: Any = None,
               calendar_id: str | None = None,
               organizer: dict | None = None,
               importance: str = "normal",
               show_as: str = "busy",
               sensitivity: str = "normal") -> dict:
    eid = _graph_id("AAMkAE")
    now = _now_iso()
    user = state["user"]
    cal_id = calendar_id or state["default_calendar_id"]
    if isinstance(location, str):
        loc = {"displayName": location}
    elif isinstance(location, dict):
        loc = dict(location)
    else:
        loc = {"displayName": ""}
    loc.setdefault("locationType", "default")
    att_list = []
    for a in (attendees or []):
        if isinstance(a, dict) and "emailAddress" in a:
            ea = a["emailAddress"]
            att_list.append({
                "type": a.get("type", "required"),
                "status": a.get("status", {
                    "response": "none",
                    "time": "0001-01-01T00:00:00Z",
                }),
                "emailAddress": {
                    "name": ea.get("name", ea.get("address", "")),
                    "address": ea.get("address", ""),
                },
            })
        elif isinstance(a, dict):
            att_list.append({
                "type": a.get("type", "required"),
                "status": {"response": "none",
                           "time": "0001-01-01T00:00:00Z"},
                "emailAddress": {
                    "name": a.get("name", a.get("address", "")),
                    "address": a.get("address", ""),
                },
            })
        elif isinstance(a, str):
            att_list.append({
                "type": "required",
                "status": {"response": "none",
                           "time": "0001-01-01T00:00:00Z"},
                "emailAddress": {"name": a, "address": a},
            })
    ev = {
        "@odata.etag": f'W/"{_graph_id()[:22]}"',
        "id": eid,
        "createdDateTime": now,
        "lastModifiedDateTime": now,
        "changeKey": _graph_id()[:22],
        "categories": [],
        "transactionId": None,
        "originalStartTimeZone": (start or {}).get("timeZone", "UTC"),
        "originalEndTimeZone": (end or {}).get("timeZone", "UTC"),
        "iCalUId": _graph_id(),
        "reminderMinutesBeforeStart": 15,
        "isReminderOn": True,
        "hasAttachments": False,
        "subject": subject or "",
        "bodyPreview": (
            (body.get("content") if isinstance(body, dict) else (body or ""))
            or ""
        )[:255],
        "importance": importance,
        "sensitivity": sensitivity,
        "isAllDay": bool(is_all_day),
        "isCancelled": False,
        "isOrganizer": True,
        "responseRequested": True,
        "seriesMasterId": None,
        "showAs": show_as,
        "type": "singleInstance",
        "webLink": (
            f"https://outlook.office365.com/owa/?itemid={eid}"
            f"&exvsurl=1&path=/calendar/item"
        ),
        "onlineMeetingUrl": None,
        "isOnlineMeeting": False,
        "onlineMeetingProvider": "unknown",
        "allowNewTimeProposals": True,
        "occurrenceId": None,
        "isDraft": False,
        "hideAttendees": False,
        "responseStatus": {
            "response": "organizer",
            "time": "0001-01-01T00:00:00Z",
        },
        "body": _body(body, default_content_type="HTML"),
        "start": start or {"dateTime": now.replace("Z", ".0000000"),
                           "timeZone": "UTC"},
        "end": end or {"dateTime": now.replace("Z", ".0000000"),
                       "timeZone": "UTC"},
        "location": loc,
        "locations": [loc] if loc.get("displayName") else [],
        "recurrence": None,
        "attendees": att_list,
        "organizer": organizer or {
            "emailAddress": {"name": user["displayName"],
                             "address": user["mail"]},
        },
        "calendar@odata.bind": (
            f"{GRAPH_BASE}/users/{user['id']}/calendars/{cal_id}"),
        "_calendarId": cal_id,
    }
    return ev


# ---------------------------------------------------------------------------
# Filtering helpers (very small $filter subset)
# ---------------------------------------------------------------------------

def _matches_filter(item: dict, expr: str) -> bool:
    """Tiny subset of OData $filter: supports `<key> eq '<val>'`,
    `<key> eq <bool/num>`, plus `contains(<key>, '<val>')`, joined by
    `and`. Unsupported expressions match-all (returns True) so the
    mock never silently drops data."""
    if not expr:
        return True
    expr = expr.strip()
    parts = [p.strip() for p in expr.split(" and ")]
    for p in parts:
        if p.startswith("contains("):
            try:
                inside = p[len("contains("):p.rindex(")")]
                k, v = [x.strip() for x in inside.split(",", 1)]
                v = v.strip().strip("'").strip('"').lower()
                val = item.get(k, "")
                if isinstance(val, dict):
                    val = val.get("content", "") if "content" in val \
                        else json.dumps(val)
                if v not in str(val).lower():
                    return False
            except Exception:
                return True
            continue
        if " eq " in p:
            k, v = p.split(" eq ", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
                if str(item.get(k, "")) != v:
                    return False
            elif v in ("true", "false"):
                if bool(item.get(k)) != (v == "true"):
                    return False
            else:
                try:
                    if float(item.get(k, 0)) != float(v):
                        return False
                except (TypeError, ValueError):
                    return True
            continue
    return True


def _apply_search(items: list[dict], search: str, fields: list[str]) -> list[dict]:
    if not search:
        return items
    s = search.strip().strip('"').lower()
    out = []
    for it in items:
        hay_parts = []
        for f in fields:
            v = it.get(f)
            if isinstance(v, dict):
                v = v.get("content", "") if "content" in v else json.dumps(v)
            if isinstance(v, list):
                v = json.dumps(v)
            hay_parts.append(str(v or ""))
        hay = " ".join(hay_parts).lower()
        if s in hay:
            out.append(it)
    return out


def _paginate(items: list[dict], top: int, skip: int) -> tuple[list[dict], bool]:
    top = max(1, min(int(top or 10), 1000))
    skip = max(0, int(skip or 0))
    page = items[skip: skip + top]
    has_more = skip + top < len(items)
    return page, has_more


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("outlook-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ===========================================================================
# Mail folders
# ===========================================================================

@mcp.tool(name="list_mail_folders")
def list_mail_folders(top: int = 10,
                      skip: int = 0,
                      filter: str = "",
                      include_hidden_folders: bool = False) -> dict:
    """Graph: GET /me/mailFolders — list the signed-in user's mail
    folders. Mirrors operation `user_list_mailFolders`. Returns a
    `{"value":[...]}` collection."""
    with _lock():
        s = _load_state()
        folders = list(s["folders"].values())
        if not include_hidden_folders:
            folders = [f for f in folders if not f.get("isHidden")]
        if filter:
            folders = [f for f in folders if _matches_filter(f, filter)]
        folders.sort(key=lambda f: f.get("displayName", ""))
        page, has_more = _paginate(folders, top, skip)
        next_link = (f"{GRAPH_BASE}/me/mailFolders?$skip={skip + top}"
                     f"&$top={top}") if has_more else None
        _record(s, "list_mail_folders", count=len(page),
                top=top, skip=skip, filter=filter)
        _save_state(s)
        return _list([_strip_internal(f) for f in page],
                     "users('me')/mailFolders", next_link)


@mcp.tool(name="create_mail_folder")
def create_mail_folder(displayName: str,
                       isHidden: bool = False,
                       parentFolderId: str | None = None) -> dict:
    """Graph: POST /me/mailFolders — create a new mail folder under the
    signed-in user's mailbox (or under `parentFolderId`).
    Mirrors `user_post_mailFolders`."""
    with _lock():
        s = _load_state()
        if not displayName:
            _record(s, "create_mail_folder", result="missing_displayName")
            _save_state(s)
            return _err("ErrorInvalidParameter",
                        "displayName is required",
                        status=400)
        if parentFolderId:
            parent = _resolve_folder_id(s, parentFolderId)
            if not parent:
                _record(s, "create_mail_folder",
                        result="parent_not_found")
                _save_state(s)
                return _err("ErrorItemNotFound",
                            f"Parent folder not found: {parentFolderId}",
                            status=404)
            parentFolderId = parent
        fid = _graph_id()
        folder = {
            "id": fid,
            "displayName": displayName,
            "parentFolderId": parentFolderId,
            "childFolderCount": 0,
            "unreadItemCount": 0,
            "totalItemCount": 0,
            "isHidden": bool(isHidden),
        }
        s["folders"][fid] = folder
        if parentFolderId and parentFolderId in s["folders"]:
            s["folders"][parentFolderId]["childFolderCount"] = (
                s["folders"][parentFolderId].get("childFolderCount", 0) + 1)
        _record(s, "create_mail_folder",
                folder_id=fid, displayName=displayName)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/mailFolders/$entity",
                **folder}


# ===========================================================================
# Mail messages
# ===========================================================================

_MESSAGE_SEARCH_FIELDS = ["subject", "bodyPreview", "body"]


@mcp.tool(name="list_messages")
def list_messages(folder_id: str | None = None,
                  top: int = 10,
                  skip: int = 0,
                  filter: str = "",
                  search: str = "",
                  orderby: str = "receivedDateTime desc",
                  select: str = "") -> dict:
    """Graph: GET /me/messages  (or /me/mailFolders/{id}/messages).
    Mirrors `user_list_messages` / `user_list_mailFolder_messages`.

    Supports `$top`, `$skip`, `$filter` (small subset), `$search`,
    `$orderby` (receivedDateTime asc/desc), `$select` (comma list).
    """
    with _lock():
        s = _load_state()
        target_fid = _resolve_folder_id(s, folder_id) if folder_id else None
        msgs = list(s["messages"].values())
        if target_fid:
            msgs = [m for m in msgs if m.get("parentFolderId") == target_fid]
        if filter:
            msgs = [m for m in msgs if _matches_filter(m, filter)]
        if search:
            msgs = _apply_search(msgs, search, _MESSAGE_SEARCH_FIELDS)
        # orderby
        ob = (orderby or "receivedDateTime desc").strip()
        ob_field, _, ob_dir = ob.partition(" ")
        reverse = (ob_dir or "asc").lower() == "desc"
        msgs.sort(key=lambda m: str(m.get(ob_field, "")), reverse=reverse)
        page, has_more = _paginate(msgs, top, skip)
        if select:
            keep = {x.strip() for x in select.split(",") if x.strip()}
            keep.add("id")
            keep.add("@odata.etag")
            page = [{k: v for k, v in m.items() if k in keep} for m in page]
        else:
            page = [_strip_internal(m) for m in page]
        ctx = (f"users('me')/mailFolders('{target_fid}')/messages"
               if target_fid else "users('me')/messages")
        path = (f"{GRAPH_BASE}/me/mailFolders/{target_fid}/messages"
                if target_fid else f"{GRAPH_BASE}/me/messages")
        next_link = (f"{path}?$skip={skip + top}&$top={top}"
                     if has_more else None)
        _record(s, "list_messages", folder_id=target_fid,
                count=len(page), top=top, skip=skip,
                filter=filter, search=search)
        _save_state(s)
        return _list(page, ctx, next_link)


@mcp.tool(name="get_message")
def get_message(message_id: str, select: str = "") -> dict:
    """Graph: GET /me/messages/{id} — retrieve a single message.
    Mirrors `user_get_messages`."""
    with _lock():
        s = _load_state()
        msg = s["messages"].get(message_id)
        if not msg:
            _record(s, "get_message", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"The specified object was not found in the store: "
                        f"{message_id}", status=404)
        out = _strip_internal(msg)
        if select:
            keep = {x.strip() for x in select.split(",") if x.strip()}
            keep |= {"id", "@odata.etag"}
            out = {k: v for k, v in out.items() if k in keep}
        _record(s, "get_message", message_id=message_id)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/messages/$entity",
                **out}


@mcp.tool(name="send_mail")
def send_mail(message: dict, saveToSentItems: bool = True) -> dict:
    """Graph: POST /me/sendMail — send a mail in a single call.
    Mirrors operation `user_sendMail`.

    `message` must include at least `subject`, `body`, `toRecipients`.
    Returns `{}` on success (HTTP 202 Accepted in the real API)."""
    with _lock():
        s = _load_state()
        if not isinstance(message, dict):
            _record(s, "send_mail", result="invalid_payload")
            _save_state(s)
            return _err("ErrorInvalidParameter",
                        "message must be an object", status=400)
        to_r = message.get("toRecipients") or []
        if not to_r:
            _record(s, "send_mail", result="no_recipients")
            _save_state(s)
            return _err("ErrorMissingRequiredParameter",
                        "toRecipients is required", status=400)
        folder = "sentitems" if saveToSentItems else "outbox"
        msg = _new_message(
            s,
            subject=message.get("subject", ""),
            body=message.get("body", ""),
            to_recipients=to_r,
            cc_recipients=message.get("ccRecipients"),
            bcc_recipients=message.get("bccRecipients"),
            from_addr=message.get("from", {}).get("emailAddress")
            if isinstance(message.get("from"), dict) else None,
            importance=message.get("importance", "normal"),
            is_draft=False,
            folder_id=folder,
        )
        if saveToSentItems:
            s["messages"][msg["id"]] = msg
            s["folders"][folder]["totalItemCount"] = (
                s["folders"][folder].get("totalItemCount", 0) + 1)
        _record(s, "send_mail", message_id=msg["id"],
                to=[r["emailAddress"]["address"]
                    for r in msg["toRecipients"]],
                subject=msg["subject"], saved=saveToSentItems)
        _save_state(s)
        # Graph returns 202 with empty body. We mirror that with `{}`.
        return {}


@mcp.tool(name="reply_mail")
def reply_mail(message_id: str,
               comment: str = "",
               message: dict | None = None,
               replyAll: bool = False) -> dict:
    """Graph: POST /me/messages/{id}/reply  (or /replyAll).
    Mirrors `user_message_reply` / `user_message_replyAll`.

    `comment` is the reply text appended to the quoted thread; the
    optional `message` overrides recipients/body wholesale."""
    with _lock():
        s = _load_state()
        orig = s["messages"].get(message_id)
        if not orig:
            _record(s, "reply_mail", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Message not found: {message_id}", status=404)
        if replyAll:
            recipients = ([orig["from"]] + (orig.get("ccRecipients") or [])
                          if orig.get("from") else
                          (orig.get("ccRecipients") or []))
        else:
            recipients = [orig["from"]] if orig.get("from") else []
        subject = orig.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        body_content = comment or ""
        if message:
            recipients = message.get("toRecipients", recipients) or recipients
            subject = message.get("subject", subject)
            body_content = message.get("body", {"content": body_content})
        reply = _new_message(
            s,
            subject=subject,
            body=body_content,
            to_recipients=recipients,
            cc_recipients=(message or {}).get("ccRecipients"),
            in_reply_to=orig.get("internetMessageId"),
            is_draft=False,
            folder_id="sentitems",
        )
        reply["conversationId"] = orig.get("conversationId", reply["conversationId"])
        s["messages"][reply["id"]] = reply
        s["folders"]["sentitems"]["totalItemCount"] = (
            s["folders"]["sentitems"].get("totalItemCount", 0) + 1)
        _record(s, "reply_mail", message_id=message_id,
                reply_id=reply["id"], reply_all=replyAll)
        _save_state(s)
        return {}


@mcp.tool(name="forward_mail")
def forward_mail(message_id: str,
                 toRecipients: Any,
                 comment: str = "",
                 message: dict | None = None) -> dict:
    """Graph: POST /me/messages/{id}/forward — forward a message.
    Mirrors `user_message_forward`."""
    with _lock():
        s = _load_state()
        orig = s["messages"].get(message_id)
        if not orig:
            _record(s, "forward_mail", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Message not found: {message_id}", status=404)
        subject = orig.get("subject", "")
        if not subject.lower().startswith("fw:"):
            subject = f"FW: {subject}"
        body_content = comment or ""
        if message:
            subject = message.get("subject", subject)
            body_content = message.get("body", {"content": body_content})
            if message.get("toRecipients") and not toRecipients:
                toRecipients = message["toRecipients"]
        fwd = _new_message(
            s,
            subject=subject,
            body=body_content,
            to_recipients=toRecipients,
            is_draft=False,
            folder_id="sentitems",
        )
        fwd["conversationId"] = orig.get("conversationId", fwd["conversationId"])
        s["messages"][fwd["id"]] = fwd
        s["folders"]["sentitems"]["totalItemCount"] = (
            s["folders"]["sentitems"].get("totalItemCount", 0) + 1)
        _record(s, "forward_mail", message_id=message_id,
                forward_id=fwd["id"],
                to=[r["emailAddress"]["address"] for r in fwd["toRecipients"]])
        _save_state(s)
        return {}


@mcp.tool(name="delete_message")
def delete_message(message_id: str, hardDelete: bool = False) -> dict:
    """Graph: DELETE /me/messages/{id} — move to Deleted Items (or
    permanently delete when `hardDelete=True`).
    Mirrors `user_delete_messages`."""
    with _lock():
        s = _load_state()
        msg = s["messages"].get(message_id)
        if not msg:
            _record(s, "delete_message", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Message not found: {message_id}", status=404)
        old_folder = msg.get("parentFolderId")
        if hardDelete:
            del s["messages"][message_id]
        else:
            msg["parentFolderId"] = "deleteditems"
            msg["lastModifiedDateTime"] = _now_iso()
            s["folders"]["deleteditems"]["totalItemCount"] = (
                s["folders"]["deleteditems"].get("totalItemCount", 0) + 1)
        if old_folder and old_folder in s["folders"]:
            cnt = s["folders"][old_folder].get("totalItemCount", 0)
            s["folders"][old_folder]["totalItemCount"] = max(0, cnt - 1)
        _record(s, "delete_message", message_id=message_id,
                hard=hardDelete, from_folder=old_folder)
        _save_state(s)
        # Real Graph returns 204 No Content.
        return {}


@mcp.tool(name="move_message")
def move_message(message_id: str, destinationId: str) -> dict:
    """Graph: POST /me/messages/{id}/move — move a message to another
    folder. Mirrors `user_message_move`. Returns the moved message."""
    with _lock():
        s = _load_state()
        msg = s["messages"].get(message_id)
        if not msg:
            _record(s, "move_message", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Message not found: {message_id}", status=404)
        dest = _resolve_folder_id(s, destinationId)
        if not dest:
            _record(s, "move_message", message_id=message_id,
                    result="dest_not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Destination folder not found: {destinationId}",
                        status=404)
        old = msg.get("parentFolderId")
        msg["parentFolderId"] = dest
        msg["lastModifiedDateTime"] = _now_iso()
        if old and old in s["folders"]:
            s["folders"][old]["totalItemCount"] = max(
                0, s["folders"][old].get("totalItemCount", 0) - 1)
        s["folders"][dest]["totalItemCount"] = (
            s["folders"][dest].get("totalItemCount", 0) + 1)
        # Graph returns a NEW id for the moved message in the new folder;
        # we approximate by issuing a new id and storing the moved copy.
        new_id = _graph_id("AAMkAD")
        moved = dict(msg)
        moved["id"] = new_id
        del s["messages"][message_id]
        s["messages"][new_id] = moved
        _record(s, "move_message", message_id=message_id,
                new_id=new_id, dest=dest)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/messages/$entity",
                **_strip_internal(moved)}


@mcp.tool(name="create_draft")
def create_draft(subject: str = "",
                 body: Any = None,
                 toRecipients: Any = None,
                 ccRecipients: Any = None,
                 bccRecipients: Any = None,
                 importance: str = "normal") -> dict:
    """Graph: POST /me/messages — create a draft message in the
    Drafts folder. Mirrors `user_create_messages`."""
    with _lock():
        s = _load_state()
        msg = _new_message(
            s,
            subject=subject,
            body=body,
            to_recipients=toRecipients,
            cc_recipients=ccRecipients,
            bcc_recipients=bccRecipients,
            importance=importance,
            is_draft=True,
            folder_id="drafts",
        )
        s["messages"][msg["id"]] = msg
        s["folders"]["drafts"]["totalItemCount"] = (
            s["folders"]["drafts"].get("totalItemCount", 0) + 1)
        _record(s, "create_draft", message_id=msg["id"], subject=subject)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/messages/$entity",
                **_strip_internal(msg)}


@mcp.tool(name="send_draft")
def send_draft(message_id: str) -> dict:
    """Graph: POST /me/messages/{id}/send — send an existing draft.
    Mirrors `user_message_send`."""
    with _lock():
        s = _load_state()
        msg = s["messages"].get(message_id)
        if not msg:
            _record(s, "send_draft", message_id=message_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Message not found: {message_id}", status=404)
        if not msg.get("isDraft"):
            _record(s, "send_draft", message_id=message_id,
                    result="not_a_draft")
            _save_state(s)
            return _err("ErrorMessageNotDraft",
                        "Only draft messages can be sent.", status=400)
        if not msg.get("toRecipients"):
            _record(s, "send_draft", message_id=message_id,
                    result="no_recipients")
            _save_state(s)
            return _err("ErrorMissingRequiredParameter",
                        "toRecipients is required to send.", status=400)
        # Move from drafts to sentitems and mark not-draft.
        msg["isDraft"] = False
        msg["parentFolderId"] = "sentitems"
        msg["sentDateTime"] = _now_iso()
        msg["lastModifiedDateTime"] = _now_iso()
        s["folders"]["drafts"]["totalItemCount"] = max(
            0, s["folders"]["drafts"].get("totalItemCount", 0) - 1)
        s["folders"]["sentitems"]["totalItemCount"] = (
            s["folders"]["sentitems"].get("totalItemCount", 0) + 1)
        _record(s, "send_draft", message_id=message_id)
        _save_state(s)
        return {}


# ===========================================================================
# Calendars
# ===========================================================================

@mcp.tool(name="list_calendars")
def list_calendars(top: int = 10, skip: int = 0) -> dict:
    """Graph: GET /me/calendars — list the signed-in user's
    calendars. Mirrors `user_list_calendars`."""
    with _lock():
        s = _load_state()
        cals = list(s["calendars"].values())
        cals.sort(key=lambda c: (not c.get("isDefaultCalendar"),
                                 c.get("name", "")))
        page, has_more = _paginate(cals, top, skip)
        next_link = (f"{GRAPH_BASE}/me/calendars?$skip={skip + top}"
                     f"&$top={top}") if has_more else None
        _record(s, "list_calendars", count=len(page))
        _save_state(s)
        return _list(page, "users('me')/calendars", next_link)


@mcp.tool(name="get_calendar")
def get_calendar(calendar_id: str | None = None) -> dict:
    """Graph: GET /me/calendars/{id}  (or /me/calendar for default).
    Mirrors `user_get_calendar` / `user_get_calendars`."""
    with _lock():
        s = _load_state()
        cid = calendar_id or s["default_calendar_id"]
        cal = s["calendars"].get(cid)
        if not cal:
            _record(s, "get_calendar", calendar_id=cid,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Calendar not found: {cid}", status=404)
        _record(s, "get_calendar", calendar_id=cid)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/calendars/$entity",
                **cal}


# ===========================================================================
# Events
# ===========================================================================

_EVENT_SEARCH_FIELDS = ["subject", "bodyPreview", "body"]


def _event_overlaps(ev: dict, start_dt: str, end_dt: str) -> bool:
    """Check if event overlaps the [start_dt, end_dt) window
    (ISO strings, naive comparison)."""
    if not (start_dt or end_dt):
        return True
    e_start = (ev.get("start") or {}).get("dateTime", "")
    e_end = (ev.get("end") or {}).get("dateTime", "")
    if not e_start or not e_end:
        return True
    if end_dt and e_start >= end_dt:
        return False
    if start_dt and e_end <= start_dt:
        return False
    return True


@mcp.tool(name="list_events")
def list_events(calendar_id: str | None = None,
                top: int = 10,
                skip: int = 0,
                filter: str = "",
                search: str = "",
                orderby: str = "start/dateTime asc",
                startDateTime: str = "",
                endDateTime: str = "",
                select: str = "") -> dict:
    """Graph: GET /me/events  or  GET /me/calendarView (when
    `startDateTime`/`endDateTime` are supplied).
    Mirrors `user_list_events` / `user_list_calendarView`."""
    with _lock():
        s = _load_state()
        target_cid = (calendar_id if calendar_id
                      else s["default_calendar_id"])
        evs = [e for e in s["events"].values()
               if e.get("_calendarId") == target_cid]
        if startDateTime or endDateTime:
            evs = [e for e in evs
                   if _event_overlaps(e, startDateTime, endDateTime)]
        if filter:
            evs = [e for e in evs if _matches_filter(e, filter)]
        if search:
            evs = _apply_search(evs, search, _EVENT_SEARCH_FIELDS)
        # orderby on start/dateTime
        if "start/dateTime" in (orderby or ""):
            reverse = "desc" in orderby.lower()
            evs.sort(key=lambda e: (e.get("start") or {}).get("dateTime", ""),
                     reverse=reverse)
        else:
            ob_field, _, ob_dir = (orderby or "").partition(" ")
            reverse = (ob_dir or "asc").lower() == "desc"
            if ob_field:
                evs.sort(key=lambda e: str(e.get(ob_field, "")),
                         reverse=reverse)
        page, has_more = _paginate(evs, top, skip)
        if select:
            keep = {x.strip() for x in select.split(",") if x.strip()}
            keep |= {"id", "@odata.etag"}
            page = [{k: v for k, v in e.items() if k in keep} for e in page]
        else:
            page = [_strip_internal(e) for e in page]
        is_view = bool(startDateTime or endDateTime)
        ctx = ("users('me')/calendarView" if is_view
               else "users('me')/events")
        path = (f"{GRAPH_BASE}/me/calendarView" if is_view
                else f"{GRAPH_BASE}/me/events")
        next_link = (f"{path}?$skip={skip + top}&$top={top}"
                     if has_more else None)
        _record(s, "list_events", calendar_id=target_cid,
                count=len(page),
                startDateTime=startDateTime, endDateTime=endDateTime)
        _save_state(s)
        return _list(page, ctx, next_link)


@mcp.tool(name="get_event")
def get_event(event_id: str, select: str = "") -> dict:
    """Graph: GET /me/events/{id} — retrieve one event.
    Mirrors `user_get_events`."""
    with _lock():
        s = _load_state()
        ev = s["events"].get(event_id)
        if not ev:
            _record(s, "get_event", event_id=event_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Event not found: {event_id}", status=404)
        out = _strip_internal(ev)
        if select:
            keep = {x.strip() for x in select.split(",") if x.strip()}
            keep |= {"id", "@odata.etag"}
            out = {k: v for k, v in out.items() if k in keep}
        _record(s, "get_event", event_id=event_id)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/events/$entity",
                **out}


@mcp.tool(name="create_event")
def create_event(subject: str,
                 start: dict,
                 end: dict,
                 body: Any = None,
                 attendees: Any = None,
                 location: dict | str | None = None,
                 isAllDay: bool = False,
                 importance: str = "normal",
                 showAs: str = "busy",
                 sensitivity: str = "normal",
                 calendar_id: str | None = None) -> dict:
    """Graph: POST /me/events  (or /me/calendars/{cid}/events).
    Mirrors `user_create_events` / `user_calendar_create_events`.

    `start`/`end` must be `{dateTime, timeZone}` objects."""
    with _lock():
        s = _load_state()
        cid = calendar_id or s["default_calendar_id"]
        if cid not in s["calendars"]:
            _record(s, "create_event", calendar_id=cid,
                    result="calendar_not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Calendar not found: {cid}", status=404)
        if not isinstance(start, dict) or not isinstance(end, dict):
            _record(s, "create_event", result="invalid_start_end")
            _save_state(s)
            return _err("ErrorInvalidParameter",
                        "start and end must be {dateTime, timeZone} objects.",
                        status=400)
        ev = _new_event(s,
                        subject=subject, body=body,
                        start=start, end=end,
                        is_all_day=isAllDay, location=location,
                        attendees=attendees,
                        calendar_id=cid,
                        importance=importance,
                        show_as=showAs,
                        sensitivity=sensitivity)
        s["events"][ev["id"]] = ev
        _record(s, "create_event", event_id=ev["id"], subject=subject,
                calendar_id=cid)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/events/$entity",
                **_strip_internal(ev)}


@mcp.tool(name="update_event")
def update_event(event_id: str,
                 subject: str | None = None,
                 body: Any = None,
                 start: dict | None = None,
                 end: dict | None = None,
                 location: dict | str | None = None,
                 attendees: Any = None,
                 isAllDay: bool | None = None,
                 importance: str | None = None,
                 showAs: str | None = None,
                 sensitivity: str | None = None,
                 isCancelled: bool | None = None) -> dict:
    """Graph: PATCH /me/events/{id} — update an event.
    Mirrors `user_update_events`."""
    with _lock():
        s = _load_state()
        ev = s["events"].get(event_id)
        if not ev:
            _record(s, "update_event", event_id=event_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Event not found: {event_id}", status=404)
        if subject is not None:
            ev["subject"] = subject
        if body is not None:
            ev["body"] = _body(body)
            ev["bodyPreview"] = ev["body"]["content"][:255]
        if start is not None:
            ev["start"] = start
            ev["originalStartTimeZone"] = start.get("timeZone",
                                                   ev["originalStartTimeZone"])
        if end is not None:
            ev["end"] = end
            ev["originalEndTimeZone"] = end.get("timeZone",
                                                ev["originalEndTimeZone"])
        if location is not None:
            if isinstance(location, str):
                loc = {"displayName": location, "locationType": "default"}
            else:
                loc = dict(location)
                loc.setdefault("locationType", "default")
            ev["location"] = loc
            ev["locations"] = [loc] if loc.get("displayName") else []
        if attendees is not None:
            # Rebuild via _new_event helper for normalization
            tmp = _new_event(s, subject="x", attendees=attendees)
            ev["attendees"] = tmp["attendees"]
        if isAllDay is not None:
            ev["isAllDay"] = bool(isAllDay)
        if importance is not None:
            ev["importance"] = importance
        if showAs is not None:
            ev["showAs"] = showAs
        if sensitivity is not None:
            ev["sensitivity"] = sensitivity
        if isCancelled is not None:
            ev["isCancelled"] = bool(isCancelled)
        ev["lastModifiedDateTime"] = _now_iso()
        ev["changeKey"] = _graph_id()[:22]
        _record(s, "update_event", event_id=event_id)
        _save_state(s)
        return {"@odata.context":
                f"{GRAPH_BASE}/$metadata#users('me')/events/$entity",
                **_strip_internal(ev)}


@mcp.tool(name="delete_event")
def delete_event(event_id: str) -> dict:
    """Graph: DELETE /me/events/{id} — delete an event.
    Mirrors `user_delete_events`. Returns `{}` (204 No Content)."""
    with _lock():
        s = _load_state()
        ev = s["events"].pop(event_id, None)
        if not ev:
            _record(s, "delete_event", event_id=event_id,
                    result="not_found")
            _save_state(s)
            return _err("ErrorItemNotFound",
                        f"Event not found: {event_id}", status=404)
        _record(s, "delete_event", event_id=event_id)
        _save_state(s)
        return {}


def _set_response(state: dict, event_id: str, response: str,
                  comment: str, sendResponse: bool) -> dict:
    ev = state["events"].get(event_id)
    if not ev:
        _record(state, f"{response}_event", event_id=event_id,
                result="not_found")
        return _err("ErrorItemNotFound",
                    f"Event not found: {event_id}", status=404)
    # Mark the signed-in user's attendee status
    me_addr = state["user"]["mail"].lower()
    found_self = False
    for a in ev.get("attendees", []):
        ea = a.get("emailAddress", {})
        if ea.get("address", "").lower() == me_addr:
            a["status"] = {"response": response, "time": _now_iso()}
            found_self = True
            break
    ev["responseStatus"] = {"response": response, "time": _now_iso()}
    ev["lastModifiedDateTime"] = _now_iso()
    _record(state, f"{response}_event", event_id=event_id,
            comment=bool(comment), sendResponse=sendResponse,
            self_attendee=found_self)
    return {}


@mcp.tool(name="accept_event")
def accept_event(event_id: str,
                 comment: str = "",
                 sendResponse: bool = True) -> dict:
    """Graph: POST /me/events/{id}/accept — accept a meeting invitation.
    Mirrors `user_event_accept`. Returns `{}` (202 Accepted)."""
    with _lock():
        s = _load_state()
        out = _set_response(s, event_id, "accepted", comment, sendResponse)
        _save_state(s)
        return out


@mcp.tool(name="decline_event")
def decline_event(event_id: str,
                  comment: str = "",
                  sendResponse: bool = True,
                  proposedNewTime: dict | None = None) -> dict:
    """Graph: POST /me/events/{id}/decline — decline a meeting.
    Mirrors `user_event_decline`."""
    with _lock():
        s = _load_state()
        out = _set_response(s, event_id, "declined", comment, sendResponse)
        if out == {} and proposedNewTime:
            # No further state change needed; just log.
            _record(s, "decline_event_proposed",
                    event_id=event_id, proposed=proposedNewTime)
        _save_state(s)
        return out


@mcp.tool(name="tentatively_accept_event")
def tentatively_accept_event(event_id: str,
                             comment: str = "",
                             sendResponse: bool = True,
                             proposedNewTime: dict | None = None) -> dict:
    """Graph: POST /me/events/{id}/tentativelyAccept — tentatively
    accept a meeting. Mirrors `user_event_tentativelyAccept`."""
    with _lock():
        s = _load_state()
        out = _set_response(s, event_id, "tentativelyAccepted",
                            comment, sendResponse)
        _save_state(s)
        return out


# ===========================================================================
# Contacts (optional)
# ===========================================================================

@mcp.tool(name="list_contacts")
def list_contacts(top: int = 10,
                  skip: int = 0,
                  filter: str = "",
                  search: str = "",
                  orderby: str = "displayName asc") -> dict:
    """Graph: GET /me/contacts — list the signed-in user's personal
    contacts. Mirrors `user_list_contacts`."""
    with _lock():
        s = _load_state()
        contacts = list(s["contacts"].values())
        if filter:
            contacts = [c for c in contacts if _matches_filter(c, filter)]
        if search:
            contacts = _apply_search(
                contacts, search, ["displayName", "givenName", "surname"])
        ob_field, _, ob_dir = (orderby or "displayName asc").partition(" ")
        reverse = (ob_dir or "asc").lower() == "desc"
        contacts.sort(key=lambda c: str(c.get(ob_field, "")), reverse=reverse)
        page, has_more = _paginate(contacts, top, skip)
        next_link = (f"{GRAPH_BASE}/me/contacts?$skip={skip + top}"
                     f"&$top={top}") if has_more else None
        _record(s, "list_contacts", count=len(page))
        _save_state(s)
        return _list([_strip_internal(c) for c in page],
                     "users('me')/contacts", next_link)


# ===========================================================================
# Mock-only helpers
# ===========================================================================

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (for verifier introspection).
    Not part of the real Graph surface."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(user: dict | None = None,
                    folders: list | None = None,
                    messages: list | None = None,
                    calendars: list | None = None,
                    events: list | None = None,
                    contacts: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. Each input is a list of Graph-ish dicts.

    - `user`: {id?, displayName, mail, userPrincipalName?, timeZone?}
    - `folders`: [{id?, displayName, parentFolderId?, isHidden?,
                   wellKnownName?}]
    - `messages`: [{id?, subject, body, from?, toRecipients,
                    ccRecipients?, parentFolderId?, isRead?,
                    isDraft?, receivedDateTime?}]
    - `calendars`: [{id?, name, isDefaultCalendar?}]
    - `events`: [{id?, subject, start, end, body?, attendees?,
                  location?, calendar_id?, isAllDay?}]
    - `contacts`: [{id?, displayName, givenName?, surname?,
                    emailAddresses?: [{address,name}]}]

    If `replace=True` the state is fully reset before seeding.
    Returns ids of created objects."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if user:
            s["user"].update({k: v for k, v in user.items()
                              if k in ("id", "displayName",
                                       "userPrincipalName", "mail")})
            if user.get("timeZone"):
                s["user"].setdefault("mailboxSettings", {})
                s["user"]["mailboxSettings"]["timeZone"] = user["timeZone"]
        # Folders
        created_folders = []
        for f in folders or []:
            fid = f.get("id") or f.get("wellKnownName") or _graph_id()
            folder = {
                "id": fid,
                "displayName": f.get("displayName", fid),
                "parentFolderId": f.get("parentFolderId"),
                "childFolderCount": int(f.get("childFolderCount", 0)),
                "unreadItemCount": int(f.get("unreadItemCount", 0)),
                "totalItemCount": int(f.get("totalItemCount", 0)),
                "isHidden": bool(f.get("isHidden", False)),
            }
            if f.get("wellKnownName"):
                folder["wellKnownName"] = f["wellKnownName"]
            s["folders"][fid] = folder
            created_folders.append(fid)
        # Calendars
        created_calendars = []
        for c in calendars or []:
            cid = c.get("id") or _graph_id()
            cal = {
                "id": cid,
                "name": c.get("name", "Calendar"),
                "color": c.get("color", "auto"),
                "hexColor": c.get("hexColor", ""),
                "isDefaultCalendar": bool(c.get("isDefaultCalendar", False)),
                "changeKey": _graph_id()[:22],
                "canShare": c.get("canShare", True),
                "canViewPrivateItems": c.get("canViewPrivateItems", True),
                "canEdit": c.get("canEdit", True),
                "owner": c.get("owner") or {
                    "name": s["user"]["displayName"],
                    "address": s["user"]["mail"],
                },
            }
            s["calendars"][cid] = cal
            if cal["isDefaultCalendar"]:
                s["default_calendar_id"] = cid
            created_calendars.append(cid)
        # Messages
        created_messages = []
        for m in messages or []:
            folder_id = _resolve_folder_id(s, m.get("parentFolderId", "inbox")) \
                or "inbox"
            msg = _new_message(
                s,
                subject=m.get("subject", ""),
                body=m.get("body", ""),
                to_recipients=m.get("toRecipients") or [],
                cc_recipients=m.get("ccRecipients"),
                bcc_recipients=m.get("bccRecipients"),
                from_addr=m.get("from"),
                importance=m.get("importance", "normal"),
                is_draft=bool(m.get("isDraft", False)),
                folder_id=folder_id,
            )
            if m.get("id"):
                msg["id"] = m["id"]
            if "isRead" in m:
                msg["isRead"] = bool(m["isRead"])
            if m.get("receivedDateTime"):
                msg["receivedDateTime"] = m["receivedDateTime"]
            if m.get("sentDateTime"):
                msg["sentDateTime"] = m["sentDateTime"]
            if m.get("conversationId"):
                msg["conversationId"] = m["conversationId"]
            if m.get("hasAttachments"):
                msg["hasAttachments"] = bool(m["hasAttachments"])
            s["messages"][msg["id"]] = msg
            s["folders"][folder_id]["totalItemCount"] = (
                s["folders"][folder_id].get("totalItemCount", 0) + 1)
            if not msg.get("isRead"):
                s["folders"][folder_id]["unreadItemCount"] = (
                    s["folders"][folder_id].get("unreadItemCount", 0) + 1)
            created_messages.append(msg["id"])
        # Events
        created_events = []
        for e in events or []:
            cid = e.get("calendar_id") or s["default_calendar_id"]
            ev = _new_event(
                s,
                subject=e.get("subject", ""),
                body=e.get("body"),
                start=e.get("start"),
                end=e.get("end"),
                is_all_day=bool(e.get("isAllDay", False)),
                location=e.get("location"),
                attendees=e.get("attendees"),
                calendar_id=cid,
                organizer=e.get("organizer"),
                importance=e.get("importance", "normal"),
                show_as=e.get("showAs", "busy"),
                sensitivity=e.get("sensitivity", "normal"),
            )
            if e.get("id"):
                ev["id"] = e["id"]
            s["events"][ev["id"]] = ev
            created_events.append(ev["id"])
        # Contacts
        created_contacts = []
        for c in contacts or []:
            cid = c.get("id") or _graph_id()
            contact = {
                "id": cid,
                "createdDateTime": _now_iso(),
                "lastModifiedDateTime": _now_iso(),
                "changeKey": _graph_id()[:22],
                "categories": c.get("categories", []),
                "displayName": c.get("displayName", ""),
                "givenName": c.get("givenName", ""),
                "surname": c.get("surname", ""),
                "companyName": c.get("companyName", ""),
                "jobTitle": c.get("jobTitle", ""),
                "emailAddresses": [
                    {"name": e.get("name", e.get("address", "")),
                     "address": e.get("address", "")}
                    for e in c.get("emailAddresses", [])
                ],
                "businessPhones": c.get("businessPhones", []),
                "homePhones": c.get("homePhones", []),
                "mobilePhone": c.get("mobilePhone"),
            }
            s["contacts"][cid] = contact
            created_contacts.append(cid)
        _record(s, "debug_seed",
                counts={"folders": len(created_folders),
                        "calendars": len(created_calendars),
                        "messages": len(created_messages),
                        "events": len(created_events),
                        "contacts": len(created_contacts)},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "user_id": s["user"]["id"],
            "folder_ids": created_folders,
            "calendar_ids": created_calendars,
            "message_ids": created_messages,
            "event_ids": created_events,
            "contact_ids": created_contacts,
        }


if __name__ == "__main__":
    mcp.run()
