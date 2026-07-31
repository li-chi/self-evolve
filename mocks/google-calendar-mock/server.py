"""Google Calendar mock MCP server.

Mirrors `@gongrzhe/server-calendar-autoauth-mcp`
(github.com/GongRzhe/Calendar-Autoauth-MCP-Server), which is what
Toolathlon uses as its `google_calendar` server. Every tool name and
parameter matches the official server; the underlying object shapes
match Google Calendar API v3 (events.insert / events.get /
events.list / events.patch / events.delete), which the upstream
returns verbatim through its Google client.

Backed by a single JSON state file (default
`$GCAL_MOCK_STATE_DIR/state.json`) that stores the user's calendar
list, all events keyed by calendarId, an auto-increment id counter,
and a `calls` log used by the verifier.

The upstream server always operates on `calendarId = "primary"`, so
none of the public tools accept a calendarId argument. The mock
honors that: every operation routes to the `primary` calendar. The
state model still keys events under calendarId so additional
calendars can be seeded by tests via `mock_debug_seed_event`.

Return shape note: the upstream wraps each Google Calendar API v3
response inside an MCP `content: [{type: "text", text: ...}]`
envelope. `get_event` and `list_events` return the raw v3 JSON as a
stringified payload; `create_event` / `update_event` / `delete_event`
return a short human-readable text. To make verifier checks more
ergonomic this mock returns the *structured* dict directly from each
tool (FastMCP will still serialize it for the wire) — the v3 shape
is preserved so JSON path assertions keep working. The text envelope
is not load-bearing for the two Toolathlon tasks that consume this
server (`set-conf-cr-ddl`, `student-interview`).

Errors are returned as Google Calendar API v3 error objects, not
raised:
    {"error": {"code": 404, "message": "Not Found",
               "errors": [{"reason": "notFound", "message": "..."}]}}
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

PRIMARY = "primary"
DEFAULT_TZ = "America/Los_Angeles"


def _state_path() -> str:
    state_dir = os.environ.get(
        "GCAL_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gcal_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _empty_state() -> dict:
    return {
        "user": {"email": "mock@user.test", "name": "Mock User"},
        "calendars": {
            PRIMARY: {
                "id": PRIMARY,
                "summary": "mock@user.test",
                "timeZone": DEFAULT_TZ,
                "accessRole": "owner",
                "colorId": "7",
            },
        },
        "events": {PRIMARY: {}},
        "next_id": {"event": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GCAL_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _gen_event_id(state: dict) -> str:
    """Mimic Google Calendar's opaque base32-style event ids."""
    n = _next_id(state, "event")
    return f"evt{n:010d}{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error(code: int, reason: str, message: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "errors": [{"reason": reason, "message": message,
                        "domain": "global"}],
        }
    }


# ---------------------------------------------------------------------------
# Time helpers (timezone-aware ISO parsing, RFC3339 round-trip)
# ---------------------------------------------------------------------------


def _parse_iso(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    # Python 3.11 fromisoformat handles "Z" since 3.11.
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _event_start(ev: dict) -> datetime.datetime | None:
    s = ev.get("start") or {}
    return _parse_iso(s.get("dateTime") or s.get("date"))


def _event_end(ev: dict) -> datetime.datetime | None:
    s = ev.get("end") or {}
    return _parse_iso(s.get("dateTime") or s.get("date"))


# ---------------------------------------------------------------------------
# Event construction / shaping
# ---------------------------------------------------------------------------


def _build_event(state: dict, event_id: str, body: dict) -> dict:
    """Build a Google Calendar API v3 event resource from a request body."""
    creator_email = state["user"]["email"]
    now = _now()
    ev: dict = {
        "kind": "calendar#event",
        "etag": f'"{secrets.token_hex(8)}"',
        "id": event_id,
        "status": body.get("status", "confirmed"),
        "htmlLink":
            f"https://www.google.com/calendar/event?eid={event_id}",
        "created": now,
        "updated": now,
        "summary": body.get("summary", ""),
        "creator": {"email": creator_email, "self": True},
        "organizer": {"email": creator_email, "self": True},
        "start": _normalize_time(body.get("start")),
        "end": _normalize_time(body.get("end")),
        "iCalUID": f"{event_id}@google.com",
        "sequence": 0,
        "reminders": {"useDefault": True},
        "eventType": "default",
    }
    if "description" in body and body["description"] is not None:
        ev["description"] = body["description"]
    if "location" in body and body["location"] is not None:
        ev["location"] = body["location"]
    attendees = body.get("attendees")
    if attendees:
        ev["attendees"] = [_normalize_attendee(a) for a in attendees]
    recurrence = body.get("recurrence")
    if recurrence:
        ev["recurrence"] = list(recurrence)
    if "colorId" in body and body["colorId"] is not None:
        ev["colorId"] = str(body["colorId"])
    return ev


def _normalize_time(t: dict | None) -> dict:
    """Pass through a {dateTime,timeZone} or {date} block, filling tz."""
    if not t:
        return {}
    out = {}
    if "dateTime" in t and t["dateTime"]:
        out["dateTime"] = t["dateTime"]
        out["timeZone"] = t.get("timeZone") or DEFAULT_TZ
    elif "date" in t and t["date"]:
        out["date"] = t["date"]
    return out


def _normalize_attendee(a: dict) -> dict:
    out = {
        "email": a["email"],
        "responseStatus": a.get("responseStatus", "needsAction"),
    }
    if "displayName" in a:
        out["displayName"] = a["displayName"]
    if a.get("optional"):
        out["optional"] = True
    if a.get("organizer"):
        out["organizer"] = True
    if "comment" in a:
        out["comment"] = a["comment"]
    return out


def _patch_event(ev: dict, body: dict) -> dict:
    """Apply a partial update (events.patch semantics)."""
    for key in ("summary", "description", "location", "status",
                "colorId"):
        if key in body and body[key] is not None:
            ev[key] = body[key]
    if "start" in body and body["start"]:
        ev["start"] = _normalize_time(body["start"])
    if "end" in body and body["end"]:
        ev["end"] = _normalize_time(body["end"])
    if "attendees" in body and body["attendees"] is not None:
        ev["attendees"] = [_normalize_attendee(a)
                           for a in body["attendees"]]
    if "recurrence" in body and body["recurrence"] is not None:
        ev["recurrence"] = list(body["recurrence"])
    ev["updated"] = _now()
    ev["sequence"] = int(ev.get("sequence", 0)) + 1
    ev["etag"] = f'"{secrets.token_hex(8)}"'
    return ev


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("google-calendar-mock")


# ---------------------------------------------------------------------------
# Tool surface (mirrors @gongrzhe/server-calendar-autoauth-mcp v1.0.2)
#
#   create_event  →  calendar.events.insert
#   get_event     →  calendar.events.get
#   update_event  →  calendar.events.patch
#   delete_event  →  calendar.events.delete
#   list_events   →  calendar.events.list (singleEvents=True)
#
# All operate on calendarId = "primary".
# ---------------------------------------------------------------------------


@mcp.tool(name="create_event")
def create_event(summary: str,
                 start: dict,
                 end: dict,
                 description: str | None = None,
                 location: str | None = None,
                 attendees: list[dict] | None = None,
                 colorId: str | None = None,
                 recurrence: list[str] | None = None) -> dict:
    """Creates a new event in Google Calendar.

    Mirrors the upstream `create_event` tool. `summary` is the event
    title, `start`/`end` are `{"dateTime": ISO8601, "timeZone": "..."}`
    dicts (`timeZone` optional, defaults to America/Los_Angeles).

    Extra parameters beyond the upstream schema (`attendees`,
    `colorId`, `recurrence`) are accepted because the underlying
    Google Calendar API supports them and tasks may need them; the
    upstream simply doesn't expose them as explicit zod fields.

    Returns the Google Calendar API v3 event resource as inserted.
    """
    with _lock():
        s = _load_state()
        body = {
            "summary": summary,
            "start": start,
            "end": end,
            "description": description,
            "location": location,
            "attendees": attendees,
            "colorId": colorId,
            "recurrence": recurrence,
        }
        eid = _gen_event_id(s)
        ev = _build_event(s, eid, body)
        s["events"].setdefault(PRIMARY, {})[eid] = ev
        _record(s, "create_event", event_id=eid, summary=summary,
                start=ev["start"], end=ev["end"],
                attendees=[a["email"] for a in ev.get("attendees", [])])
        _save_state(s)
        return ev


@mcp.tool(name="get_event")
def get_event(eventId: str) -> dict:
    """Retrieves details of a specific event.

    Mirrors the upstream `get_event` tool. Returns the Google Calendar
    API v3 event resource. If the event does not exist returns a v3
    error object (`{"error": {"code": 404, ...}}`).
    """
    with _lock():
        s = _load_state()
        ev = s.get("events", {}).get(PRIMARY, {}).get(eventId)
        _record(s, "get_event", event_id=eventId,
                result="ok" if ev else "not_found")
        _save_state(s)
        if not ev:
            return _error(404, "notFound", f"Not Found: {eventId}")
        return ev


@mcp.tool(name="update_event")
def update_event(eventId: str,
                 summary: str | None = None,
                 start: dict | None = None,
                 end: dict | None = None,
                 description: str | None = None,
                 location: str | None = None,
                 attendees: list[dict] | None = None,
                 colorId: str | None = None,
                 recurrence: list[str] | None = None) -> dict:
    """Updates an existing event (events.patch semantics).

    Mirrors the upstream `update_event` tool. Only fields explicitly
    provided are touched; everything else is preserved. Returns the
    updated v3 event resource.
    """
    with _lock():
        s = _load_state()
        ev = s.get("events", {}).get(PRIMARY, {}).get(eventId)
        if not ev:
            _record(s, "update_event", event_id=eventId,
                    result="not_found")
            _save_state(s)
            return _error(404, "notFound", f"Not Found: {eventId}")
        body = {
            "summary": summary,
            "start": start,
            "end": end,
            "description": description,
            "location": location,
            "attendees": attendees,
            "colorId": colorId,
            "recurrence": recurrence,
        }
        _patch_event(ev, body)
        _record(s, "update_event", event_id=eventId,
                changed=[k for k, v in body.items() if v is not None])
        _save_state(s)
        return ev


@mcp.tool(name="delete_event")
def delete_event(eventId: str) -> dict:
    """Deletes an event from the calendar.

    Mirrors the upstream `delete_event` tool. Returns an empty dict
    on success (matches Google Calendar API v3 `events.delete`, which
    returns HTTP 204 / empty body). If the event does not exist
    returns a v3 error object.
    """
    with _lock():
        s = _load_state()
        events = s.get("events", {}).get(PRIMARY, {})
        if eventId not in events:
            _record(s, "delete_event", event_id=eventId,
                    result="not_found")
            _save_state(s)
            return _error(404, "notFound", f"Not Found: {eventId}")
        del events[eventId]
        _record(s, "delete_event", event_id=eventId, result="ok")
        _save_state(s)
        return {}


@mcp.tool(name="list_events")
def list_events(timeMin: str,
                timeMax: str,
                maxResults: int | None = None,
                orderBy: str | None = None) -> dict:
    """Lists events within a specified time range.

    Mirrors the upstream `list_events` tool. Uses
    `singleEvents=True` semantics (recurring expansions are not
    modeled; recurrence rules are stored verbatim but not expanded).

    Returns a Google Calendar API v3 `events.list` response:

        {"kind": "calendar#events", "etag": "...", "summary": ...,
         "timeZone": ..., "accessRole": "owner",
         "defaultReminders": [], "items": [<event>, ...]}
    """
    with _lock():
        s = _load_state()
        tmin = _parse_iso(timeMin)
        tmax = _parse_iso(timeMax)
        cal = s["calendars"].get(PRIMARY, {})
        items = []
        for ev in s.get("events", {}).get(PRIMARY, {}).values():
            es = _event_start(ev)
            ee = _event_end(ev)
            if tmin and ee is not None and ee <= tmin:
                continue
            if tmax and es is not None and es >= tmax:
                continue
            items.append(ev)
        order = orderBy or "startTime"
        if order == "startTime":
            items.sort(key=lambda e: (_event_start(e)
                                      or datetime.datetime.min.replace(
                                          tzinfo=datetime.timezone.utc)))
        elif order == "updated":
            items.sort(key=lambda e: e.get("updated", ""))
        n = int(maxResults) if maxResults else 10
        items = items[:n]
        _record(s, "list_events", timeMin=timeMin, timeMax=timeMax,
                returned=len(items))
        _save_state(s)
        return {
            "kind": "calendar#events",
            "etag": f'"{secrets.token_hex(8)}"',
            "summary": cal.get("summary", PRIMARY),
            "description": cal.get("description", ""),
            "updated": _now(),
            "timeZone": cal.get("timeZone", DEFAULT_TZ),
            "accessRole": cal.get("accessRole", "owner"),
            "defaultReminders": [],
            "nextSyncToken": None,
            "items": items,
        }


# ---------------------------------------------------------------------------
# Mock-only debug / fixture helpers (not in upstream)
# ---------------------------------------------------------------------------


@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state.

    Not in @gongrzhe/server-calendar-autoauth-mcp. Used by verifiers
    to assert on the calendar contents after a rollout.
    """
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_set_user")
def mock_debug_set_user(email: str, name: str | None = None,
                        timeZone: str | None = None) -> dict:
    """Mock-only: set the authenticated user's email/name and the
    primary calendar's timezone. Used by per-task setup."""
    with _lock():
        s = _load_state()
        s["user"]["email"] = email
        if name is not None:
            s["user"]["name"] = name
        cal = s["calendars"].setdefault(PRIMARY, {
            "id": PRIMARY, "accessRole": "owner", "colorId": "7"})
        cal["summary"] = email
        if timeZone is not None:
            cal["timeZone"] = timeZone
        _record(s, "debug_set_user", email=email,
                timeZone=timeZone)
        _save_state(s)
        return {"user": s["user"], "primary": cal}


@mcp.tool(name="mock_debug_seed_event")
def mock_debug_seed_event(event: dict,
                          calendarId: str = PRIMARY) -> dict:
    """Mock-only: insert a fully-formed event resource.

    `event` should be a Google Calendar API v3 event dict. If `id`
    is omitted one is generated. If `calendarId` is provided and not
    yet known, a basic calendar entry is auto-created.
    """
    with _lock():
        s = _load_state()
        if calendarId not in s["calendars"]:
            s["calendars"][calendarId] = {
                "id": calendarId, "summary": calendarId,
                "timeZone": DEFAULT_TZ, "accessRole": "owner",
                "colorId": "7",
            }
        events = s["events"].setdefault(calendarId, {})
        eid = event.get("id") or _gen_event_id(s)
        body = dict(event)
        body.pop("id", None)
        ev = _build_event(s, eid, body)
        # Preserve any extra fields the seed supplies verbatim.
        for k, v in event.items():
            if k not in ev:
                ev[k] = v
        events[eid] = ev
        _record(s, "debug_seed_event", calendarId=calendarId,
                event_id=eid)
        _save_state(s)
        return ev


if __name__ == "__main__":
    mcp.run()
