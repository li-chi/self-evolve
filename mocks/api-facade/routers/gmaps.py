"""Google Maps Platform served from the google-maps-mock state.

Toolathlon's map tasks are agent-side (directions, place search); the
graders check the agent's written output rather than calling Maps. This
router exists so any harness-side call still lands on the mock rather than
the public API, and so an agent using plain HTTP gets the same answers its
MCP tool would.
"""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

gm = _load_mock("google-maps-mock")


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = gm._load_state()
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["maps", "api"]:
        parts = parts[2:]
    kind = parts[0] if parts else ""

    if kind == "geocode":
        results = [p for p in state.get("places", {}).values()
                   if (query.get("address", "").lower()
                       in str(p.get("formatted_address", "")).lower())]
        return 200, {"status": "OK" if results else "ZERO_RESULTS",
                     "results": results}
    if kind == "place":
        results = list(state.get("places", {}).values())
        q = (query.get("query") or query.get("input") or "").lower()
        if q:
            results = [p for p in results
                       if q in str(p.get("name", "")).lower()]
        return 200, {"status": "OK", "results": results,
                     "candidates": results}
    if kind == "directions":
        routes = list(state.get("routes", {}).values())
        return 200, {"status": "OK" if routes else "ZERO_RESULTS",
                     "routes": routes}
    if kind == "distancematrix":
        return 200, {"status": "OK",
                     "rows": list(state.get("distance_matrix", {}).values())}

    raise NotImplementedError(f"maps facade: {method} {path}")
