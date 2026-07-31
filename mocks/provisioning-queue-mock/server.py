"""Provisioning-queue mock — an evolving-state MCP server.

Defeats the "bulk-read everything, solve offline, emit" strategy: the queue
reveals one item at a time (future items + their team are hidden until claimed),
and a resource's eligibility for an item depends on what has ALREADY been
assigned to it (path-dependent). So an agent cannot pre-fetch the data or
simulate it in local code — it must interleave claim -> decide -> assign for
every item, threading the evolving capacity/eligibility across the whole run.

Protocol:
  claim_next() -> the next item, its team, the currently-eligible resources, and
                  the current remaining capacity of every resource. Errors if a
                  claimed item is still unassigned.
  assign(item_id, resource_id) -> accept (advance) or reject (ineligible:
                  full, or a same-team item already sits on that resource).

Selection policy (the agent must follow it, checked by the verifier): assign the
claimed item to the eligible resource with the MOST remaining capacity; break
ties by lowest resource id.

State at $PROVQ_MOCK_STATE_DIR/state.json.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("provisioning-queue-mock")


def _state_path() -> str:
    state_dir = os.environ.get("PROVQ_MOCK_STATE_DIR", ".")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _load() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        return {"resources": {}, "items": [], "cursor": 0, "claimed": None,
                "assignments": {}, "calls": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    path = _state_path() + ".lock"
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _team_of(state: dict, item_id: str) -> str | None:
    for it in state["items"]:
        if it["id"] == item_id:
            return it["team"]
    return None


def _remaining(state: dict) -> dict[str, int]:
    used: dict[str, int] = {r: 0 for r in state["resources"]}
    for res in state["assignments"].values():
        used[res] = used.get(res, 0) + 1
    return {r: state["resources"][r]["capacity"] - used.get(r, 0) for r in state["resources"]}


def _teams_on(state: dict, resource_id: str) -> set[str]:
    return {
        _team_of(state, item)
        for item, res in state["assignments"].items()
        if res == resource_id
    }


def _eligible(state: dict, item_id: str) -> list[str]:
    rem = _remaining(state)
    team = _team_of(state, item_id)
    out = []
    for r in sorted(state["resources"]):
        if rem[r] > 0 and team not in _teams_on(state, r):
            out.append(r)
    return out


@mcp.tool(name="claim_next")
def claim_next() -> dict:
    """Claim the next item in the queue. Returns the item, its team, the
    currently eligible resources, and every resource's remaining capacity.
    A claimed item must be assigned before the next claim."""
    with _lock():
        s = _load()
        if s["claimed"] is not None:
            s["calls"].append({"op": "claim_next", "error": "unassigned_claim"})
            _save(s)
            return {"error": f"item {s['claimed']} is claimed but not yet assigned"}
        if s["cursor"] >= len(s["items"]):
            _save(s)
            return {"done": True, "remaining_items": 0}
        item = s["items"][s["cursor"]]
        s["claimed"] = item["id"]
        eligible = _eligible(s, item["id"])
        s["calls"].append({"op": "claim_next", "item": item["id"]})
        _save(s)
        return {
            "item": item["id"],
            "team": item["team"],
            "eligible": eligible,
            "remaining_capacity": _remaining(s),
            "remaining_items": len(s["items"]) - s["cursor"],
        }


@mcp.tool(name="assign")
def assign(item_id: str, resource_id: str) -> dict:
    """Assign the currently-claimed item to a resource. Rejected if the item is
    not the claimed one, or the resource is ineligible (full, or already holds a
    same-team item)."""
    with _lock():
        s = _load()
        if s["claimed"] != item_id:
            s["calls"].append({"op": "assign", "item": item_id, "rejected": "not_claimed"})
            _save(s)
            return {"accepted": False,
                    "reason": f"the claimed item is {s['claimed']!r}; assign that one"}
        if resource_id not in _eligible(s, item_id):
            s["calls"].append({"op": "assign", "item": item_id, "resource": resource_id,
                               "rejected": "ineligible"})
            _save(s)
            return {"accepted": False,
                    "reason": f"{resource_id} is ineligible (full or same-team conflict)",
                    "eligible": _eligible(s, item_id)}
        s["assignments"][item_id] = resource_id
        s["cursor"] += 1
        s["claimed"] = None
        s["calls"].append({"op": "assign", "item": item_id, "resource": resource_id, "accepted": True})
        _save(s)
        return {"accepted": True, "remaining_items": len(s["items"]) - s["cursor"]}


@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    return _load()


if __name__ == "__main__":
    mcp.run()
