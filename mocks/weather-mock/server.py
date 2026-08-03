"""Weather mock MCP server.

Mirrors the common `weather` MCP tool surface (the Model Context Protocol
weather quickstart / NWS+Open-Meteo family the atlas registers as `weather` /
`weather-data`): point forecasts by latitude/longitude and US state alerts.
The real servers query NOAA/NWS and Open-Meteo (no API key); this mock serves
the same shapes from a seeded JSON state file so runs are deterministic and
offline.

Tool surface:

  get_current_weather(latitude, longitude)
  get_forecast(latitude, longitude, days=7)
  get_alerts(state)

`get_current_weather`/`get_forecast` look up the seeded point whose
(lat, lon) — rounded to 2 decimals — matches the request; `get_alerts` returns
the seeded active alerts for a 2-letter US state code. State:
`$WEATHER_MOCK_STATE_DIR/state.json`, seeded from `$WEATHER_MOCK_SEED_PATH`
(built by `synth/mock_seed/weather.py`). Calls append to `state["calls"]`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


def _state_path() -> str:
    d = os.environ.get("WEATHER_MOCK_STATE_DIR",
                       os.path.expanduser("~/.openclaw/weather_mock"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {"points": {}, "alerts": {}, "calls": []}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("WEATHER_MOCK_SEED_PATH")
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


def _pt_key(lat: float, lon: float) -> str:
    return f"{round(float(lat), 2)},{round(float(lon), 2)}"


def _find_point(state: dict, lat: float, lon: float) -> dict | None:
    pts = state.get("points", {})
    key = _pt_key(lat, lon)
    if key in pts:
        return pts[key]
    # tolerant fallback: nearest seeded point within ~0.05 deg.
    best, bestd = None, 0.051
    for k, v in pts.items():
        try:
            plat, plon = (float(x) for x in k.split(","))
        except ValueError:
            continue
        d = abs(plat - float(lat)) + abs(plon - float(lon))
        if d < bestd:
            best, bestd = v, d
    return best


mcp = FastMCP("weather-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



@mcp.tool(
    name="get_current_weather",
    description="Get the current weather conditions for a location given its "
    "latitude and longitude (temperature, conditions, wind, humidity).")
def get_current_weather(latitude: float, longitude: float) -> dict:
    """Returns the seeded current-conditions dict for the matching point."""
    with _lock():
        s = _load_state()
        pt = _find_point(s, latitude, longitude)
        _record(s, "get_current_weather", latitude=latitude,
                longitude=longitude, result="ok" if pt else "not_found")
        _save_state(s)
    if not pt:
        return {"error": f"No weather data for ({latitude}, {longitude})"}
    cur = dict(pt.get("current") or {})
    cur.setdefault("location", pt.get("location"))
    cur.setdefault("latitude", latitude)
    cur.setdefault("longitude", longitude)
    return cur


@mcp.tool(
    name="get_forecast",
    description="Get the weather forecast for a location given its latitude "
    "and longitude. `days` bounds how many forecast periods to return.")
def get_forecast(latitude: float, longitude: float, days: int = 7) -> dict:
    """Returns `{location, latitude, longitude, periods:[...]}` where each
    period mirrors the NWS shape (name/date, temperature, temperatureUnit,
    windSpeed, windDirection, shortForecast, detailedForecast)."""
    with _lock():
        s = _load_state()
        pt = _find_point(s, latitude, longitude)
        _record(s, "get_forecast", latitude=latitude, longitude=longitude,
                days=days, result="ok" if pt else "not_found")
        _save_state(s)
    if not pt:
        return {"error": f"No forecast data for ({latitude}, {longitude})"}
    periods = list(pt.get("forecast") or [])
    n = max(1, int(days or 7))
    return {
        "location": pt.get("location"),
        "latitude": latitude,
        "longitude": longitude,
        "periods": periods[:n],
    }


@mcp.tool(
    name="get_alerts",
    description="Get active weather alerts for a US state, given its "
    "two-letter state code (e.g. CA, NY).")
def get_alerts(state: str) -> dict:
    """Returns `{state, alerts:[{event, area, severity, description,
    instruction}]}` for the seeded alerts of that state code."""
    with _lock():
        s = _load_state()
        code = (state or "").upper().strip()
        alerts = list((s.get("alerts") or {}).get(code, []))
        _record(s, "get_alerts", state_code=code, count=len(alerts))
        _save_state(s)
    return {"state": code, "alerts": alerts}


@_debug_tool(name="mock_debug_state",
          description="Mock-only: return the persisted state dict.")
def mock_debug_state() -> dict:
    with _lock():
        return _load_state()


if __name__ == "__main__":
    mcp.run()
