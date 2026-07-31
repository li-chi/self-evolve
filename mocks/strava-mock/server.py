"""Strava mock MCP server.

Mirrors the public Strava API v3 surface
(https://developers.strava.com/docs/reference/). Tools are named after
upstream `operationId` values so this server is a drop-in for any
agent code that targets Strava's REST endpoints. Responses match
Strava's JSON shapes (id as long integer, snake_case fields, athlete
short representation embedded in activities, etc.).

Implemented operationIds:

  Athletes
    getLoggedInAthlete, getAthleteStats,
    getLoggedInAthleteActivities, getAthleteZones,
    updateLoggedInAthlete
  Activities
    getActivityById, createActivity, updateActivityById,
    getCommentsByActivityId, getKudoersByActivityId,
    getLapsByActivityId, getZonesByActivityId
  Clubs
    getLoggedInAthleteClubs, getClubById, getClubMembersById,
    getClubActivitiesById
  Routes
    getRoutesByAthleteId, getRouteById
  Segments
    getLoggedInAthleteStarredSegments, getSegmentById,
    exploreSegments

Plus mock-only helpers `mock_debug_state` and `mock_debug_seed`.

State at `$STRAVA_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/strava_mock`). Every call (including reads) appends to
`state["calls"]` so verifiers can replay the trace.

Errors are returned as Strava-shaped error JSON dicts (not raised):
    {"message": "Authorization Error",
     "errors": [{"resource":"Athlete","field":"access_token",
                 "code":"invalid"}]}
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
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "STRAVA_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/strava_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    self_id = 1000001
    return {
        "self": {
            "id": self_id,
            "username": "mockathlete",
            "firstname": "Mock",
            "lastname": "Athlete",
            "bio": "",
            "city": "",
            "state": "",
            "country": "",
            "sex": "M",
            "premium": True,
            "summit": True,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "weight": 70.0,
            "ftp": None,
            "profile_medium": "https://mock.strava.com/avatar/medium.jpg",
            "profile": "https://mock.strava.com/avatar/large.jpg",
            "resource_state": 3,
        },
        "athletes": {},            # id(str) -> athlete dict
        "activities": {},          # id(str) -> activity dict
        "comments": {},            # activity_id(str) -> list[comment]
        "kudoers": {},             # activity_id(str) -> list[athlete summary]
        "laps": {},                # activity_id(str) -> list[lap]
        "activity_zones": {},      # activity_id(str) -> list[zone bucket]
        "athlete_zones": {
            "heart_rate": {"custom_zones": False, "zones": []},
            "power": {"zones": []},
        },
        "athlete_stats": {},       # athlete_id(str) -> stats dict
        "clubs": {},               # id(str) -> club dict
        "club_members": {},        # club_id(str) -> list[athlete summary]
        "club_activities": {},     # club_id(str) -> list[activity]
        "routes": {},              # id(str) -> route dict
        "segments": {},            # id(str) -> segment dict
        "starred_segments": [],    # list[segment_id(int)]
        "next_id": {
            "athlete": self_id + 1,
            "activity": 5000000001,
            "club": 200001,
            "route": 3000001,
            "segment": 800001,
            "comment": 70000001,
            "lap": 90000001,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("STRAVA_MOCK_SEED_PATH")
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

def _new_id(state: dict, kind: str) -> int:
    n = state["next_id"][kind]
    state["next_id"][kind] = n + 1
    return n


def _err(message: str, resource: str = "", field: str = "",
         code: str = "invalid") -> dict:
    """Strava REST error body shape."""
    return {
        "message": message,
        "errors": [{
            "resource": resource or "Application",
            "field": field,
            "code": code,
        }],
    }


def _athlete_summary(athlete: dict) -> dict:
    """The `athlete` short-representation embedded in activities/clubs.

    Strava returns `{"id": <int>, "resource_state": 1}` in many sub-objects.
    """
    if not isinstance(athlete, dict):
        return {"id": 0, "resource_state": 1}
    return {"id": athlete.get("id", 0), "resource_state": 1}


def _athlete_meta(athlete: dict) -> dict:
    """Slightly fuller athlete embed used in comments/kudoers/club members."""
    return {
        "id": athlete.get("id", 0),
        "resource_state": 2,
        "firstname": athlete.get("firstname", ""),
        "lastname": athlete.get("lastname", ""),
        "username": athlete.get("username", ""),
        "profile_medium": athlete.get("profile_medium", ""),
        "profile": athlete.get("profile", ""),
        "city": athlete.get("city", ""),
        "state": athlete.get("state", ""),
        "country": athlete.get("country", ""),
        "sex": athlete.get("sex", ""),
        "premium": athlete.get("premium", False),
    }


def _get_athlete(state: dict, athlete_id: int | str) -> dict | None:
    aid = str(athlete_id)
    if str(state["self"].get("id")) == aid:
        return state["self"]
    return state["athletes"].get(aid)


def _paginate(items: list, page: int, per_page: int) -> list:
    """Strava REST uses 1-indexed `page` with `per_page` (default 30)."""
    if per_page <= 0:
        per_page = 30
    if per_page > 200:
        per_page = 200
    if page <= 0:
        page = 1
    start = (page - 1) * per_page
    return items[start: start + per_page]


def _epoch_to_iso(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return (datetime.datetime.fromtimestamp(float(epoch),
                                                tz=datetime.timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    except (TypeError, ValueError):
        return None


def _parse_iso_date(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("strava-mock")


# ---------------------------------------------------------------------------
# Athletes
# ---------------------------------------------------------------------------

@mcp.tool(name="getLoggedInAthlete")
def get_logged_in_athlete() -> dict:
    """Strava REST: GET /athlete — returns the currently authenticated
    athlete (DetailedAthlete)."""
    with _lock():
        s = _load_state()
        _record(s, "getLoggedInAthlete")
        _save_state(s)
        return dict(s["self"])


@mcp.tool(name="getAthleteStats")
def get_athlete_stats(id: int, page: int = 1, per_page: int = 30) -> dict:
    """Strava REST: GET /athletes/{id}/stats — returns the activity
    stats of the athlete (recent/y-t-d/all-time totals for ride/run/swim)."""
    with _lock():
        s = _load_state()
        athlete = _get_athlete(s, id)
        if not athlete:
            _record(s, "getAthleteStats", athlete_id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Athlete",
                        field="id", code="not_found")
        # Strava only returns stats for the currently-authenticated athlete
        if str(id) != str(s["self"].get("id")):
            _record(s, "getAthleteStats", athlete_id=id, result="forbidden")
            _save_state(s)
            return _err("Forbidden", resource="Athlete",
                        field="id", code="forbidden")
        stats = s["athlete_stats"].get(str(id))
        if not stats:
            stats = _default_stats()
        _record(s, "getAthleteStats", athlete_id=id)
        _save_state(s)
        return dict(stats)


def _default_stats() -> dict:
    """Strava ActivityStats default shape."""
    zero_totals = {
        "count": 0, "distance": 0.0, "moving_time": 0, "elapsed_time": 0,
        "elevation_gain": 0.0, "achievement_count": 0,
    }
    return {
        "biggest_ride_distance": 0.0,
        "biggest_climb_elevation_gain": 0.0,
        "recent_ride_totals": dict(zero_totals),
        "recent_run_totals": dict(zero_totals),
        "recent_swim_totals": dict(zero_totals),
        "ytd_ride_totals": dict(zero_totals),
        "ytd_run_totals": dict(zero_totals),
        "ytd_swim_totals": dict(zero_totals),
        "all_ride_totals": dict(zero_totals),
        "all_run_totals": dict(zero_totals),
        "all_swim_totals": dict(zero_totals),
    }


@mcp.tool(name="getLoggedInAthleteActivities")
def get_logged_in_athlete_activities(before: int | None = None,
                                     after: int | None = None,
                                     page: int = 1,
                                     per_page: int = 30) -> list:
    """Strava REST: GET /athlete/activities — list activities for the
    currently authenticated athlete.

    `before` / `after` are Unix epoch seconds (Strava uses Unix
    timestamps for these query params). Returns an array of
    SummaryActivity objects."""
    with _lock():
        s = _load_state()
        self_id = s["self"].get("id")
        items = [a for a in s["activities"].values()
                 if (a.get("athlete") or {}).get("id") == self_id]
        if after is not None:
            cutoff = _epoch_to_iso(after)
            if cutoff:
                items = [a for a in items
                         if (a.get("start_date") or "") > cutoff]
        if before is not None:
            cutoff = _epoch_to_iso(before)
            if cutoff:
                items = [a for a in items
                         if (a.get("start_date") or "") < cutoff]
        items.sort(key=lambda a: a.get("start_date") or "", reverse=True)
        page_items = _paginate(items, page, per_page)
        _record(s, "getLoggedInAthleteActivities",
                page=page, per_page=per_page, count=len(page_items))
        _save_state(s)
        return [_summary_activity(a) for a in page_items]


def _summary_activity(a: dict) -> dict:
    """Strip detail-only fields to match Strava's SummaryActivity shape."""
    keep = [
        "id", "external_id", "upload_id", "athlete", "name", "distance",
        "moving_time", "elapsed_time", "total_elevation_gain",
        "elev_high", "elev_low", "type", "sport_type", "start_date",
        "start_date_local", "timezone", "utc_offset", "start_latlng",
        "end_latlng", "achievement_count", "kudos_count", "comment_count",
        "athlete_count", "photo_count", "total_photo_count", "map",
        "trainer", "commute", "manual", "private", "flagged",
        "workout_type", "average_speed", "max_speed", "has_kudoed",
        "gear_id", "average_watts", "kilojoules", "device_watts",
        "average_heartrate", "max_heartrate", "pr_count", "visibility",
        "average_cadence", "average_temp", "suffer_score", "resource_state",
    ]
    return {k: a.get(k) for k in keep if k in a}


@mcp.tool(name="getAthleteZones")
def get_athlete_zones() -> dict:
    """Strava REST: GET /athlete/zones — returns the heart-rate and
    power zones of the currently authenticated athlete (Zones)."""
    with _lock():
        s = _load_state()
        _record(s, "getAthleteZones")
        _save_state(s)
        return dict(s["athlete_zones"])


@mcp.tool(name="updateLoggedInAthlete")
def update_logged_in_athlete(weight: float | None = None) -> dict:
    """Strava REST: PUT /athlete — update the currently authenticated
    athlete. Only `weight` is settable per the v3 spec."""
    with _lock():
        s = _load_state()
        if weight is not None:
            try:
                s["self"]["weight"] = float(weight)
            except (TypeError, ValueError):
                _record(s, "updateLoggedInAthlete", result="invalid_weight")
                _save_state(s)
                return _err("Bad Request", resource="Athlete",
                            field="weight", code="invalid")
        s["self"]["updated_at"] = _now_iso()
        _record(s, "updateLoggedInAthlete", weight=weight)
        _save_state(s)
        return dict(s["self"])


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

@mcp.tool(name="getActivityById")
def get_activity_by_id(id: int,
                       include_all_efforts: bool = False) -> dict:
    """Strava REST: GET /activities/{id} — returns the detailed
    representation of an activity (DetailedActivity)."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "getActivityById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        out = dict(a)
        if not include_all_efforts:
            out.pop("segment_efforts", None)
        _record(s, "getActivityById", id=id)
        _save_state(s)
        return out


@mcp.tool(name="createActivity")
def create_activity(name: str,
                    sport_type: str,
                    start_date_local: str,
                    elapsed_time: int,
                    type: str | None = None,
                    description: str | None = None,
                    distance: float | None = None,
                    trainer: int | None = None,
                    commute: int | None = None) -> dict:
    """Strava REST: POST /activities — create a manual activity for
    the currently authenticated athlete.

    `sport_type` is the canonical (post-2022) field; `type` is the
    legacy enum, accepted for backwards compatibility. `elapsed_time`
    is in seconds; `start_date_local` is ISO8601 in the athlete's
    local timezone."""
    with _lock():
        s = _load_state()
        if not name or not sport_type or not start_date_local:
            _record(s, "createActivity", result="missing_required")
            _save_state(s)
            return _err("Bad Request", resource="Activity",
                        field="name", code="required")
        if elapsed_time is None or int(elapsed_time) < 0:
            _record(s, "createActivity", result="bad_elapsed")
            _save_state(s)
            return _err("Bad Request", resource="Activity",
                        field="elapsed_time", code="invalid")
        aid = _new_id(s, "activity")
        now = _now_iso()
        activity = {
            "id": aid,
            "resource_state": 3,
            "external_id": None,
            "upload_id": None,
            "athlete": _athlete_summary(s["self"]),
            "name": name,
            "distance": float(distance or 0.0),
            "moving_time": int(elapsed_time),
            "elapsed_time": int(elapsed_time),
            "total_elevation_gain": 0.0,
            "type": type or sport_type,
            "sport_type": sport_type,
            "start_date": start_date_local,
            "start_date_local": start_date_local,
            "timezone": "(GMT+00:00) UTC",
            "utc_offset": 0.0,
            "start_latlng": [],
            "end_latlng": [],
            "achievement_count": 0,
            "kudos_count": 0,
            "comment_count": 0,
            "athlete_count": 1,
            "photo_count": 0,
            "total_photo_count": 0,
            "map": {"id": f"a{aid}", "summary_polyline": "",
                    "resource_state": 2},
            "trainer": bool(trainer),
            "commute": bool(commute),
            "manual": True,
            "private": False,
            "flagged": False,
            "average_speed": (float(distance or 0.0) / int(elapsed_time))
            if int(elapsed_time) else 0.0,
            "max_speed": 0.0,
            "has_kudoed": False,
            "description": description or "",
            "calories": 0.0,
            "segment_efforts": [],
            "splits_metric": [],
            "splits_standard": [],
            "laps": [],
            "best_efforts": [],
            "gear_id": None,
            "device_name": "",
            "embed_token": "",
            "photos": {"primary": None, "count": 0},
            "created_at": now,
            "updated_at": now,
        }
        s["activities"][str(aid)] = activity
        _record(s, "createActivity", id=aid, sport_type=sport_type)
        _save_state(s)
        return activity


@mcp.tool(name="updateActivityById")
def update_activity_by_id(id: int,
                          name: str | None = None,
                          type: str | None = None,
                          sport_type: str | None = None,
                          description: str | None = None,
                          trainer: bool | None = None,
                          commute: bool | None = None,
                          gear_id: str | None = None,
                          hide_from_home: bool | None = None) -> dict:
    """Strava REST: PUT /activities/{id} — update an activity owned by
    the authenticated athlete. Returns the updated DetailedActivity."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "updateActivityById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        if name is not None:
            a["name"] = name
        if type is not None:
            a["type"] = type
        if sport_type is not None:
            a["sport_type"] = sport_type
        if description is not None:
            a["description"] = description
        if trainer is not None:
            a["trainer"] = bool(trainer)
        if commute is not None:
            a["commute"] = bool(commute)
        if gear_id is not None:
            a["gear_id"] = gear_id
        if hide_from_home is not None:
            a["hide_from_home"] = bool(hide_from_home)
        a["updated_at"] = _now_iso()
        _record(s, "updateActivityById", id=id,
                fields=[k for k, v in {
                    "name": name, "type": type, "sport_type": sport_type,
                    "description": description, "trainer": trainer,
                    "commute": commute, "gear_id": gear_id,
                    "hide_from_home": hide_from_home,
                }.items() if v is not None])
        _save_state(s)
        return dict(a)


@mcp.tool(name="getCommentsByActivityId")
def get_comments_by_activity_id(id: int,
                                page: int = 1,
                                per_page: int = 30,
                                page_size: int | None = None,
                                after_cursor: str | None = None) -> list:
    """Strava REST: GET /activities/{id}/comments — list comments on
    an activity. The newer cursor-based pagination uses `page_size`
    + `after_cursor`; legacy page/per_page still accepted."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "getCommentsByActivityId", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        comments = list(s["comments"].get(str(id), []))
        comments.sort(key=lambda c: c.get("created_at", ""))
        if after_cursor:
            for i, c in enumerate(comments):
                if str(c.get("id")) == str(after_cursor):
                    comments = comments[i + 1:]
                    break
        size = page_size if page_size is not None else per_page
        page_items = _paginate(comments, page, size)
        _record(s, "getCommentsByActivityId", id=id, count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="getKudoersByActivityId")
def get_kudoers_by_activity_id(id: int,
                               page: int = 1,
                               per_page: int = 30) -> list:
    """Strava REST: GET /activities/{id}/kudos — list athletes who
    have kudoed the activity. Returns an array of SummaryAthlete."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "getKudoersByActivityId", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        kudoers = list(s["kudoers"].get(str(id), []))
        page_items = _paginate(kudoers, page, per_page)
        _record(s, "getKudoersByActivityId", id=id, count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="getLapsByActivityId")
def get_laps_by_activity_id(id: int) -> list:
    """Strava REST: GET /activities/{id}/laps — return the laps of an
    activity. Returns an array of Lap objects."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "getLapsByActivityId", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        laps = list(s["laps"].get(str(id), []))
        _record(s, "getLapsByActivityId", id=id, count=len(laps))
        _save_state(s)
        return laps


@mcp.tool(name="getZonesByActivityId")
def get_zones_by_activity_id(id: int) -> list:
    """Strava REST: GET /activities/{id}/zones — return zone-bucket
    breakdowns (heartrate, power) for an activity. Returns an array
    of ActivityZone objects."""
    with _lock():
        s = _load_state()
        a = s["activities"].get(str(id))
        if not a:
            _record(s, "getZonesByActivityId", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Activity",
                        field="id", code="not_found")
        zones = list(s["activity_zones"].get(str(id), []))
        _record(s, "getZonesByActivityId", id=id, count=len(zones))
        _save_state(s)
        return zones


# ---------------------------------------------------------------------------
# Clubs
# ---------------------------------------------------------------------------

@mcp.tool(name="getLoggedInAthleteClubs")
def get_logged_in_athlete_clubs(page: int = 1,
                                per_page: int = 30) -> list:
    """Strava REST: GET /athlete/clubs — list clubs the authenticated
    athlete is a member of. Returns an array of SummaryClub."""
    with _lock():
        s = _load_state()
        self_id = s["self"].get("id")
        clubs = [c for c in s["clubs"].values()
                 if self_id in (c.get("_members") or [])]
        clubs.sort(key=lambda c: c.get("id", 0))
        page_items = _paginate(clubs, page, per_page)
        _record(s, "getLoggedInAthleteClubs",
                page=page, count=len(page_items))
        _save_state(s)
        return [_club_summary(c) for c in page_items]


def _club_summary(c: dict) -> dict:
    """Drop internal fields, drop detail-only fields."""
    keep = [
        "id", "resource_state", "name", "profile_medium", "profile",
        "cover_photo", "cover_photo_small", "sport_type", "activity_types",
        "city", "state", "country", "private", "member_count", "featured",
        "verified", "url",
    ]
    out = {k: c.get(k) for k in keep if k in c}
    return out


@mcp.tool(name="getClubById")
def get_club_by_id(id: int) -> dict:
    """Strava REST: GET /clubs/{id} — returns a detailed club
    representation (DetailedClub)."""
    with _lock():
        s = _load_state()
        c = s["clubs"].get(str(id))
        if not c:
            _record(s, "getClubById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Club",
                        field="id", code="not_found")
        out = {k: v for k, v in c.items() if not k.startswith("_")}
        _record(s, "getClubById", id=id)
        _save_state(s)
        return out


@mcp.tool(name="getClubMembersById")
def get_club_members_by_id(id: int,
                           page: int = 1,
                           per_page: int = 30) -> list:
    """Strava REST: GET /clubs/{id}/members — list club members.
    Returns an array of ClubAthlete (SummaryAthlete) objects."""
    with _lock():
        s = _load_state()
        c = s["clubs"].get(str(id))
        if not c:
            _record(s, "getClubMembersById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Club",
                        field="id", code="not_found")
        members = list(s["club_members"].get(str(id), []))
        page_items = _paginate(members, page, per_page)
        _record(s, "getClubMembersById", id=id, count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="getClubActivitiesById")
def get_club_activities_by_id(id: int,
                              page: int = 1,
                              per_page: int = 30) -> list:
    """Strava REST: GET /clubs/{id}/activities — list recent
    activities posted to the club. Returns an array of ClubActivity
    (without athlete id; only firstname + last initial)."""
    with _lock():
        s = _load_state()
        c = s["clubs"].get(str(id))
        if not c:
            _record(s, "getClubActivitiesById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Club",
                        field="id", code="not_found")
        acts = list(s["club_activities"].get(str(id), []))
        page_items = _paginate(acts, page, per_page)
        _record(s, "getClubActivitiesById", id=id, count=len(page_items))
        _save_state(s)
        return page_items


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@mcp.tool(name="getRoutesByAthleteId")
def get_routes_by_athlete_id(id: int,
                             page: int = 1,
                             per_page: int = 30) -> list:
    """Strava REST: GET /athletes/{id}/routes — list routes created by
    the given athlete. Returns an array of Route objects."""
    with _lock():
        s = _load_state()
        athlete = _get_athlete(s, id)
        if not athlete:
            _record(s, "getRoutesByAthleteId", athlete_id=id,
                    result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Athlete",
                        field="id", code="not_found")
        routes = [r for r in s["routes"].values()
                  if (r.get("athlete") or {}).get("id") == athlete.get("id")]
        routes.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        page_items = _paginate(routes, page, per_page)
        _record(s, "getRoutesByAthleteId", athlete_id=id,
                count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="getRouteById")
def get_route_by_id(id: int) -> dict:
    """Strava REST: GET /routes/{id} — returns a Route object."""
    with _lock():
        s = _load_state()
        r = s["routes"].get(str(id))
        if not r:
            _record(s, "getRouteById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Route",
                        field="id", code="not_found")
        _record(s, "getRouteById", id=id)
        _save_state(s)
        return dict(r)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

@mcp.tool(name="getLoggedInAthleteStarredSegments")
def get_logged_in_athlete_starred_segments(page: int = 1,
                                           per_page: int = 30) -> list:
    """Strava REST: GET /segments/starred — list the authenticated
    athlete's starred segments. Returns an array of SummarySegment."""
    with _lock():
        s = _load_state()
        starred_ids = list(s.get("starred_segments", []))
        starred = []
        for sid in starred_ids:
            seg = s["segments"].get(str(sid))
            if seg:
                starred.append(_segment_summary(seg))
        page_items = _paginate(starred, page, per_page)
        _record(s, "getLoggedInAthleteStarredSegments",
                count=len(page_items))
        _save_state(s)
        return page_items


def _segment_summary(seg: dict) -> dict:
    keep = [
        "id", "resource_state", "name", "activity_type", "distance",
        "average_grade", "maximum_grade", "elevation_high", "elevation_low",
        "start_latlng", "end_latlng", "climb_category", "city", "state",
        "country", "private", "hazardous", "starred",
    ]
    return {k: seg.get(k) for k in keep if k in seg}


@mcp.tool(name="getSegmentById")
def get_segment_by_id(id: int) -> dict:
    """Strava REST: GET /segments/{id} — returns the detailed
    representation of a segment (DetailedSegment)."""
    with _lock():
        s = _load_state()
        seg = s["segments"].get(str(id))
        if not seg:
            _record(s, "getSegmentById", id=id, result="not_found")
            _save_state(s)
            return _err("Record Not Found", resource="Segment",
                        field="id", code="not_found")
        _record(s, "getSegmentById", id=id)
        _save_state(s)
        return dict(seg)


@mcp.tool(name="exploreSegments")
def explore_segments(bounds: str,
                     activity_type: str | None = None,
                     min_cat: int | None = None,
                     max_cat: int | None = None) -> dict:
    """Strava REST: GET /segments/explore — returns up to 10 segments
    within a bounding box.

    `bounds` is a comma-separated string of four floats in the order
    `sw_lat,sw_lng,ne_lat,ne_lng` (matches the upstream query-param
    format). `activity_type` is "running" or "riding". Returns a
    `{"segments": [...]}` payload of ExplorerSegment objects."""
    with _lock():
        s = _load_state()
        try:
            parts = [float(x) for x in (bounds or "").split(",")]
        except ValueError:
            _record(s, "exploreSegments", result="bad_bounds")
            _save_state(s)
            return _err("Bad Request", resource="Segment",
                        field="bounds", code="invalid")
        if len(parts) != 4:
            _record(s, "exploreSegments", result="bad_bounds")
            _save_state(s)
            return _err("Bad Request", resource="Segment",
                        field="bounds", code="invalid")
        sw_lat, sw_lng, ne_lat, ne_lng = parts
        matches = []
        for seg in s["segments"].values():
            start = seg.get("start_latlng") or []
            if len(start) != 2:
                continue
            lat, lng = start
            if not (sw_lat <= lat <= ne_lat and sw_lng <= lng <= ne_lng):
                continue
            if activity_type:
                want = "Ride" if activity_type == "riding" else (
                    "Run" if activity_type == "running" else activity_type)
                if seg.get("activity_type") != want:
                    continue
            cat = seg.get("climb_category", 0) or 0
            if min_cat is not None and cat < int(min_cat):
                continue
            if max_cat is not None and cat > int(max_cat):
                continue
            matches.append({
                "id": seg.get("id"),
                "resource_state": 2,
                "name": seg.get("name"),
                "climb_category": cat,
                "climb_category_desc": ["NC", "4", "3", "2", "1",
                                       "HC"][min(cat, 5)],
                "avg_grade": seg.get("average_grade", 0.0),
                "start_latlng": seg.get("start_latlng"),
                "end_latlng": seg.get("end_latlng"),
                "elev_difference": (seg.get("elevation_high", 0.0)
                                    - seg.get("elevation_low", 0.0)),
                "distance": seg.get("distance", 0.0),
                "points": seg.get("points", ""),
            })
        matches = matches[:10]
        _record(s, "exploreSegments", count=len(matches))
        _save_state(s)
        return {"segments": matches}


# ---------------------------------------------------------------------------
# Mock-only helpers (not part of the Strava REST surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (for verifier introspection).
    Not part of the real Strava API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(self_athlete: dict | None = None,
                    athletes: list | None = None,
                    activities: list | None = None,
                    comments: list | None = None,
                    kudoers: list | None = None,
                    laps: list | None = None,
                    activity_zones: list | None = None,
                    athlete_zones: dict | None = None,
                    athlete_stats: dict | None = None,
                    clubs: list | None = None,
                    club_members: list | None = None,
                    club_activities: list | None = None,
                    routes: list | None = None,
                    segments: list | None = None,
                    starred_segments: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. All inputs are Strava-shaped dicts.

    - `self_athlete`: merged into state["self"].
    - `athletes`: list of athlete dicts (each requires `id`).
    - `activities`: list of activity dicts; `athlete` short-rep
      is auto-filled from `athlete_id` (or current `self`) if missing.
    - `comments` / `kudoers` / `laps` / `activity_zones`: each item
      must have `activity_id`; the activity_id key is stripped before
      storing the payload.
    - `athlete_zones`: merged into state["athlete_zones"].
    - `athlete_stats`: {athlete_id: stats dict}.
    - `clubs`: list of club dicts (members tracked via `_members`).
    - `club_members` / `club_activities`: items with `club_id`.
    - `routes`: list of route dicts (each requires `id`).
    - `segments`: list of segment dicts (each requires `id`).
    - `starred_segments`: list of segment ids.

    `replace=True` resets the state first."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if self_athlete:
            s["self"].update(self_athlete)
        for a in athletes or []:
            if "id" not in a:
                a["id"] = _new_id(s, "athlete")
            s["athletes"][str(a["id"])] = a
        for act in activities or []:
            if "id" not in act:
                act["id"] = _new_id(s, "activity")
            # Auto-fill athlete short-rep if caller used `athlete_id` shortcut
            if "athlete" not in act:
                aid = act.pop("athlete_id", s["self"].get("id"))
                act["athlete"] = {"id": aid, "resource_state": 1}
            s["activities"][str(act["id"])] = act
        for c in comments or []:
            act_id = c.pop("activity_id", None)
            if act_id is None:
                continue
            if "id" not in c:
                c["id"] = _new_id(s, "comment")
            s["comments"].setdefault(str(act_id), []).append(c)
            act = s["activities"].get(str(act_id))
            if act is not None:
                act["comment_count"] = len(s["comments"][str(act_id)])
        for k in kudoers or []:
            act_id = k.pop("activity_id", None)
            if act_id is None:
                continue
            s["kudoers"].setdefault(str(act_id), []).append(k)
            act = s["activities"].get(str(act_id))
            if act is not None:
                act["kudos_count"] = len(s["kudoers"][str(act_id)])
        for lap in laps or []:
            act_id = lap.pop("activity_id", None)
            if act_id is None:
                continue
            if "id" not in lap:
                lap["id"] = _new_id(s, "lap")
            s["laps"].setdefault(str(act_id), []).append(lap)
        for z in activity_zones or []:
            act_id = z.pop("activity_id", None)
            if act_id is None:
                continue
            s["activity_zones"].setdefault(str(act_id), []).append(z)
        if athlete_zones:
            s["athlete_zones"].update(athlete_zones)
        for aid, stats in (athlete_stats or {}).items():
            s["athlete_stats"][str(aid)] = stats
        for c in clubs or []:
            if "id" not in c:
                c["id"] = _new_id(s, "club")
            s["clubs"][str(c["id"])] = c
        for m in club_members or []:
            club_id = m.pop("club_id", None)
            if club_id is None:
                continue
            s["club_members"].setdefault(str(club_id), []).append(m)
            club = s["clubs"].get(str(club_id))
            if club is not None:
                club["member_count"] = len(s["club_members"][str(club_id)])
        for ca in club_activities or []:
            club_id = ca.pop("club_id", None)
            if club_id is None:
                continue
            s["club_activities"].setdefault(str(club_id), []).append(ca)
        for r in routes or []:
            if "id" not in r:
                r["id"] = _new_id(s, "route")
            s["routes"][str(r["id"])] = r
        for seg in segments or []:
            if "id" not in seg:
                seg["id"] = _new_id(s, "segment")
            s["segments"][str(seg["id"])] = seg
        if starred_segments is not None:
            s["starred_segments"] = list(starred_segments)
        _record(s, "debug_seed",
                counts={"athletes": len(athletes or []),
                        "activities": len(activities or []),
                        "comments": len(comments or []),
                        "kudoers": len(kudoers or []),
                        "laps": len(laps or []),
                        "clubs": len(clubs or []),
                        "routes": len(routes or []),
                        "segments": len(segments or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "athlete_ids": list(s["athletes"].keys()),
            "activity_ids": list(s["activities"].keys()),
            "club_ids": list(s["clubs"].keys()),
            "route_ids": list(s["routes"].keys()),
            "segment_ids": list(s["segments"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
