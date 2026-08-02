"""Google Maps mock MCP server.

Mirrors the tool surface of `@modelcontextprotocol/server-google-maps`
(https://github.com/modelcontextprotocol/servers-archived/tree/main/src/google-maps
— the atlas name `google-maps` / `google_map`). The real server proxies the
Google Maps Platform REST APIs; this mock serves the same tool shapes from a
seeded JSON state file so runs are deterministic and offline (no API key).

Tool surface (verbatim names + signatures):

  maps_geocode(address)
  maps_reverse_geocode(latitude, longitude)
  maps_search_places(query, location=None, radius=None)
  maps_place_details(place_id)
  maps_distance_matrix(origins, destinations, mode="driving")
  maps_elevation(locations)
  maps_directions(origin, destination, mode="driving")

Distances/durations use a seeded route/leg when present, else fall back to a
deterministic great-circle estimate between two geocodable endpoints. State:
`$GOOGLE_MAPS_MOCK_STATE_DIR/state.json`, seeded from
`$GOOGLE_MAPS_MOCK_SEED_PATH` (built by `synth/mock_seed/google_maps.py`).
Calls append to `state["calls"]`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import math
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

_SPEED_KMH = {"driving": 50.0, "walking": 5.0, "bicycling": 15.0,
              "transit": 30.0}


def _state_path() -> str:
    d = os.environ.get("GOOGLE_MAPS_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/google_maps_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"places": {}, "geocode": {}, "routes": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GOOGLE_MAPS_MOCK_SEED_PATH")
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
    fd = open(_state_path() + ".lock", "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kw: Any) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kw)
    state["calls"].append(entry)


def _norm(a: str) -> str:
    return " ".join((a or "").lower().split())


def _resolve(state: dict, address: str) -> dict | None:
    """address -> place dict, via the geocode index or a place's own address."""
    key = _norm(address)
    pid = state.get("geocode", {}).get(key)
    if pid and pid in state.get("places", {}):
        return state["places"][pid]
    for p in state.get("places", {}).values():
        if _norm(p.get("formatted_address", "")) == key or _norm(p.get("name", "")) == key:
            return p
    return None


def _haversine_km(a: dict, b: dict) -> float:
    r = 6371.0
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lng"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lng"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def _leg(state: dict, origin: str, destination: str, mode: str) -> dict | None:
    key = f"{_norm(origin)}|{_norm(destination)}|{mode}"
    seeded = state.get("routes", {}).get(key)
    if seeded:
        return seeded
    a, b = _resolve(state, origin), _resolve(state, destination)
    if not a or not b or not a.get("location") or not b.get("location"):
        return None
    km = _haversine_km(a["location"], b["location"])
    hrs = km / _SPEED_KMH.get(mode, 50.0)
    meters = int(round(km * 1000))
    secs = int(round(hrs * 3600))
    return {
        "summary": f"{a.get('name') or origin} to {b.get('name') or destination}",
        "distance": {"text": f"{km:.1f} km", "value": meters},
        "duration": {"text": f"{int(round(secs / 60))} mins", "value": secs},
        "steps": seeded.get("steps") if seeded else [],
    }


mcp = FastMCP("google-maps-mock")

# Free-text `maps_search_places` is a query-phrasing-dependent ranking (not
# reconstructable under recompute verification), so it is OFF by default — the
# fair surface is exact geocode/place_details/directions by a supplied
# address/place_id. Re-enable via GOOGLE_MAPS_MOCK_ENABLE_SEARCH=1.
_SEARCH_ENABLED = os.environ.get(
    "GOOGLE_MAPS_MOCK_ENABLE_SEARCH", "").lower() in ("1", "true", "yes", "on")


@mcp.tool(name="maps_geocode",
          description="Convert an address into geographic coordinates. Returns "
          "location (lat/lng), formatted_address, and place_id.")
def maps_geocode(address: str) -> dict:
    with _lock():
        s = _load_state()
        p = _resolve(s, address)
        _record(s, "maps_geocode", address=address,
                result="ok" if p else "not_found")
        _save_state(s)
    if not p:
        return {"error": "ZERO_RESULTS",
                "message": f"No geocode result for '{address}'"}
    return {"location": p.get("location"),
            "formatted_address": p.get("formatted_address"),
            "place_id": p.get("place_id")}


@mcp.tool(name="maps_reverse_geocode",
          description="Convert coordinates into an address. Returns "
          "formatted_address, place_id, and address_components.")
def maps_reverse_geocode(latitude: float, longitude: float) -> dict:
    with _lock():
        s = _load_state()
        best, bestd = None, 0.051
        for p in s.get("places", {}).values():
            loc = p.get("location") or {}
            if "lat" not in loc:
                continue
            d = abs(loc["lat"] - float(latitude)) + abs(loc["lng"] - float(longitude))
            if d < bestd:
                best, bestd = p, d
        _record(s, "maps_reverse_geocode", latitude=latitude,
                longitude=longitude, result="ok" if best else "not_found")
        _save_state(s)
    if not best:
        return {"error": "ZERO_RESULTS",
                "message": f"No address for ({latitude}, {longitude})"}
    return {"formatted_address": best.get("formatted_address"),
            "place_id": best.get("place_id"),
            "address_components": best.get("address_components", [])}


def maps_search_places(query: str, location: dict | None = None,
                       radius: int | None = None) -> dict:
    with _lock():
        s = _load_state()
        terms = [t for t in (query or "").lower().split() if t]
        places = []
        for p in s.get("places", {}).values():
            hay = (str(p.get("name", "")) + " " + str(p.get("formatted_address", ""))
                   + " " + " ".join(p.get("types", []))).lower()
            if terms and not any(t in hay for t in terms):
                continue
            if location and radius and p.get("location"):
                km = _haversine_km(
                    {"lat": location.get("latitude", location.get("lat")),
                     "lng": location.get("longitude", location.get("lng"))},
                    p["location"])
                if km * 1000 > float(radius):
                    continue
            places.append({
                "name": p.get("name"),
                "formatted_address": p.get("formatted_address"),
                "location": p.get("location"),
                "place_id": p.get("place_id"),
                "types": p.get("types", []),
                "rating": p.get("rating"),
            })
        _record(s, "maps_search_places", query=query, count=len(places))
        _save_state(s)
        return {"places": places}


@mcp.tool(name="maps_place_details",
          description="Get detailed information about a place by its place_id "
          "(name, address, contact info, ratings, reviews, opening hours).")
def maps_place_details(place_id: str) -> dict:
    with _lock():
        s = _load_state()
        p = s.get("places", {}).get(place_id)
        _record(s, "maps_place_details", place_id=place_id,
                result="ok" if p else "not_found")
        _save_state(s)
    if not p:
        return {"error": "NOT_FOUND", "message": f"place_id '{place_id}' not found"}
    return {
        "place_id": p.get("place_id"),
        "name": p.get("name"),
        "formatted_address": p.get("formatted_address"),
        "location": p.get("location"),
        "formatted_phone_number": p.get("phone_number"),
        "website": p.get("website"),
        "rating": p.get("rating"),
        "user_ratings_total": p.get("user_ratings_total"),
        "opening_hours": p.get("opening_hours"),
        "reviews": p.get("reviews", []),
        "types": p.get("types", []),
        # seeded enrichment columns surfaced as top-level fields (keyed-enrichment source)
        **(p.get("_row") or {}),
    }


@mcp.tool(name="maps_distance_matrix",
          description="Calculate travel distance and time for multiple "
          "origins and destinations. `mode` is driving|walking|bicycling|"
          "transit.")
def maps_distance_matrix(origins: list[str], destinations: list[str],
                         mode: str = "driving") -> dict:
    with _lock():
        s = _load_state()
        rows = []
        for o in origins:
            elements = []
            for d in destinations:
                leg = _leg(s, o, d, mode)
                if leg:
                    elements.append({"status": "OK",
                                     "distance": leg["distance"],
                                     "duration": leg["duration"]})
                else:
                    elements.append({"status": "ZERO_RESULTS"})
            rows.append({"elements": elements})
        _record(s, "maps_distance_matrix", origins=origins,
                destinations=destinations, mode=mode)
        _save_state(s)
        return {"origin_addresses": list(origins),
                "destination_addresses": list(destinations),
                "rows": rows, "status": "OK"}


@mcp.tool(name="maps_elevation",
          description="Get elevation data for one or more locations "
          "(list of {latitude, longitude}). Returns elevation in meters.")
def maps_elevation(locations: list[dict]) -> dict:
    with _lock():
        s = _load_state()
        results = []
        for loc in locations:
            lat = loc.get("latitude", loc.get("lat"))
            lng = loc.get("longitude", loc.get("lng"))
            elev = None
            best, bestd = None, 0.051
            for p in s.get("places", {}).values():
                pl = p.get("location") or {}
                if "lat" not in pl:
                    continue
                dd = abs(pl["lat"] - float(lat)) + abs(pl["lng"] - float(lng))
                if dd < bestd:
                    best, bestd = p, dd
            if best is not None:
                elev = best.get("elevation")
            results.append({"elevation": elev if elev is not None else 0.0,
                            "location": {"lat": lat, "lng": lng}})
        _record(s, "maps_elevation", count=len(results))
        _save_state(s)
        return {"results": results, "status": "OK"}


@mcp.tool(name="maps_directions",
          description="Get turn-by-turn directions between two points. `mode` "
          "is driving|walking|bicycling|transit.")
def maps_directions(origin: str, destination: str,
                    mode: str = "driving") -> dict:
    with _lock():
        s = _load_state()
        leg = _leg(s, origin, destination, mode)
        _record(s, "maps_directions", origin=origin, destination=destination,
                mode=mode, result="ok" if leg else "not_found")
        _save_state(s)
    if not leg:
        return {"error": "ZERO_RESULTS",
                "message": f"No route from '{origin}' to '{destination}'"}
    return {"routes": [{
        "summary": leg.get("summary"),
        "distance": leg["distance"],
        "duration": leg["duration"],
        "steps": leg.get("steps", []),
    }], "status": "OK"}


@mcp.tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if _SEARCH_ENABLED:
    mcp.tool(
        name="maps_search_places",
        description="Search for places using a text query, optionally biased "
        "by a location and radius (meters, max 50000).")(maps_search_places)


if __name__ == "__main__":
    mcp.run()
