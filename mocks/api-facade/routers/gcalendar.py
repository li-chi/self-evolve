"""Google Calendar v3 served from the google-calendar-mock state."""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

gc = _load_mock("google-calendar-mock")


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = gc._load_state()
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    if parts[:3] == ["calendar", "v3", "calendars"]:
        parts = parts[3:]
    elif parts[:2] == ["calendar", "v3"]:
        parts = parts[2:]

    if parts[:2] == ["users", "me"]:
        return 200, {"items": list(state.get("calendars", {}).values())}

    if not parts:
        return 200, {"items": list(state.get("calendars", {}).values())}

    cal_id = parts[0]
    tail = parts[1:]
    events = state.setdefault("events", {})

    if tail[:1] == ["events"]:
        if len(tail) == 1 and method == "GET":
            items = [e for e in events.values()
                     if e.get("calendarId", cal_id) == cal_id]
            tmin, tmax = query.get("timeMin"), query.get("timeMax")
            def start(e):
                s = e.get("start") or {}
                return s.get("dateTime") or s.get("date") or ""
            if tmin:
                items = [e for e in items if start(e) >= tmin]
            if tmax:
                items = [e for e in items if start(e) <= tmax]
            items.sort(key=start)
            return 200, {"kind": "calendar#events", "items": items}
        if len(tail) == 1 and method == "POST":
            event = dict(body)
            event["id"] = event.get("id") or f"evt{len(events) + 1}"
            event["calendarId"] = cal_id
            event.setdefault("status", "confirmed")
            events[event["id"]] = event
            gc._save_state(state)
            return 200, event
        if len(tail) == 2:
            event = events.get(tail[1])
            if method == "GET":
                return (200, event) if event else (404, {"error": "not found"})
            if method in ("PATCH", "PUT") and event:
                event.update(body)
                gc._save_state(state)
                return 200, event
            if method == "DELETE" and event:
                del events[tail[1]]
                gc._save_state(state)
                return 204, {}
            return 404, {"error": {"code": 404, "message": "Not Found"}}

    raise NotImplementedError(f"calendar facade: {method} {path}")
