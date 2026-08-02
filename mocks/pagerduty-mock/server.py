"""PagerDuty mock MCP server.

Mirrors the surface of PagerDuty REST API v2
(developer.pagerduty.com/api-reference/). Tools are named after the
real PagerDuty operation IDs (list_incidents, manage_incidents,
list_on_calls, ...) and accept/return the same JSON shapes:

  - Singletons wrapped under a typed key: `{"incident": {...}}`
  - Lists wrapped with cursor/offset envelope:
      `{"incidents": [...], "limit": 25, "offset": 0,
        "total": null, "more": false}`
  - Object references use the canonical PD shape:
      `{"id":"PT4KHLK","type":"service_reference",
        "summary":"...","self":"...","html_url":"..."}`
  - Errors are returned (not raised) using the PD error envelope:
      `{"error": {"message":"...","code":2100,"errors":[...]}}`

State lives at `$PAGERDUTY_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/pagerduty_mock`). Per-rollout isolation should clear
the state dir between rollouts. Optional `PAGERDUTY_MOCK_SEED_PATH`
preloads state when no state.json exists yet.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import random
import string
from typing import Any

from mcp.server.fastmcp import FastMCP


WEB_BASE = "https://mock.pagerduty.com"
API_BASE = "https://api.pagerduty.com"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "PAGERDUTY_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/pagerduty_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "account": {
            "subdomain": "mock",
            "name": "Mock Account",
        },
        "self_id": "PUSER001",
        "users": {},
        "teams": {},
        "services": {},
        "escalation_policies": {},
        "schedules": {},
        "incidents": {},         # incident id -> incident object
        "incident_notes": {},    # incident id -> list[note]
        "incident_alerts": {},   # incident id -> list[alert]
        "incident_log_entries": {},  # incident id -> list[log entry]
        "next_incident_number": 1,
        "id_seq": 1,
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("PAGERDUTY_MOCK_SEED_PATH")
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
# ID helpers — PD uses 7-char uppercase alphanumeric IDs like "PT4KHLK"
# ---------------------------------------------------------------------------

_ID_CHARS = string.ascii_uppercase + string.digits


def _new_id(state: dict, prefix: str = "P") -> str:
    """Generate a deterministic 7-char PD-style id from the state seq.

    Real PagerDuty IDs are random 7-char alphanumeric strings prefixed
    with P. We use the state sequence so seeded scenarios are
    reproducible while still matching the surface shape.
    """
    n = state["id_seq"]
    state["id_seq"] = n + 1
    # base-36-ish encoding into 6 chars after the prefix
    rest = []
    x = n
    for _ in range(6):
        rest.append(_ID_CHARS[x % len(_ID_CHARS)])
        x //= len(_ID_CHARS)
    return prefix + "".join(reversed(rest))


def _random_id(prefix: str = "P") -> str:
    return prefix + "".join(random.choice(_ID_CHARS) for _ in range(6))


# ---------------------------------------------------------------------------
# Reference helpers — PD wraps cross-object pointers in a typed reference
# ---------------------------------------------------------------------------

def _ref(obj: dict, ref_type: str) -> dict:
    """Build a PagerDuty `*_reference` envelope for an object."""
    if not obj:
        return {}
    oid = obj.get("id")
    return {
        "id": oid,
        "type": ref_type,
        "summary": obj.get("summary") or obj.get("name") or obj.get("title")
        or oid or "",
        "self": f"{API_BASE}/{_pluralize_for_ref(ref_type)}/{oid}",
        "html_url": obj.get("html_url")
        or f"{WEB_BASE}/{_pluralize_for_ref(ref_type)}/{oid}",
    }


def _pluralize_for_ref(ref_type: str) -> str:
    """Map e.g. 'service_reference' -> 'services'."""
    base = ref_type.replace("_reference", "")
    mapping = {
        "service": "services",
        "user": "users",
        "team": "teams",
        "escalation_policy": "escalation_policies",
        "schedule": "schedules",
        "incident": "incidents",
        "priority": "priorities",
        "vendor": "vendors",
    }
    return mapping.get(base, base + "s")


def _self_url(plural: str, oid: str) -> str:
    return f"{API_BASE}/{plural}/{oid}"


def _html_url(plural: str, oid: str) -> str:
    return f"{WEB_BASE}/{plural}/{oid}"


# ---------------------------------------------------------------------------
# Error envelopes — PD returns `{"error": {message, code, errors[]}}`.
# Common codes: 2001 invalid input, 2100 not found, 2101 unauthorized.
# ---------------------------------------------------------------------------

def _err(code: int, message: str, errors: list | None = None,
         http_status: int = None) -> dict:
    return {
        "error": {
            "message": message,
            "code": code,
            "errors": errors or [message],
        }
    }


def _not_found(what: str) -> dict:
    return _err(2100, f"Not Found", [f"{what} not found"])


def _invalid(message: str, errors: list | None = None) -> dict:
    return _err(2001, "Invalid Input Provided", errors or [message])


# ---------------------------------------------------------------------------
# Pagination — PD list envelope shape
# ---------------------------------------------------------------------------

def _paginate(items: list, limit: int, offset: int) -> tuple[list, dict]:
    """Slice + return the envelope fields (limit, offset, total, more)."""
    if limit is None or limit <= 0:
        limit = 25
    if limit > 100:
        limit = 100
    if offset is None or offset < 0:
        offset = 0
    page = items[offset: offset + limit]
    more = (offset + limit) < len(items)
    return page, {
        "limit": limit,
        "offset": offset,
        "total": None,   # PD only populates `total` when `total=true` query param
        "more": more,
    }


# ---------------------------------------------------------------------------
# Object enrichers — turn stored dicts into full PD JSON shapes
# ---------------------------------------------------------------------------

def _full_user(state: dict, u: dict) -> dict:
    if not u:
        return u
    uid = u["id"]
    teams = [_ref(state["teams"][tid], "team_reference")
             for tid in u.get("team_ids", []) if tid in state["teams"]]
    return {
        "id": uid,
        "type": "user",
        "summary": u.get("name", ""),
        "self": _self_url("users", uid),
        "html_url": _html_url("users", uid),
        "name": u.get("name", ""),
        "email": u.get("email", ""),
        "time_zone": u.get("time_zone", "UTC"),
        "color": u.get("color", "blue"),
        "role": u.get("role", "user"),
        "avatar_url": u.get("avatar_url",
                            f"{WEB_BASE}/avatars/{uid}.png"),
        "description": u.get("description"),
        "invitation_sent": bool(u.get("invitation_sent", False)),
        "job_title": u.get("job_title"),
        "teams": teams,
        "contact_methods": u.get("contact_methods", []),
        "notification_rules": u.get("notification_rules", []),
    }


def _full_team(state: dict, t: dict) -> dict:
    if not t:
        return t
    tid = t["id"]
    return {
        "id": tid,
        "type": "team",
        "summary": t.get("name", ""),
        "self": _self_url("teams", tid),
        "html_url": _html_url("teams", tid),
        "name": t.get("name", ""),
        "description": t.get("description", ""),
        "default_role": t.get("default_role", "manager"),
        "parent": t.get("parent"),
    }


def _full_service(state: dict, svc: dict) -> dict:
    if not svc:
        return svc
    sid = svc["id"]
    ep = state["escalation_policies"].get(svc.get("escalation_policy_id"))
    teams = [_ref(state["teams"][tid], "team_reference")
             for tid in svc.get("team_ids", []) if tid in state["teams"]]
    return {
        "id": sid,
        "type": "service",
        "summary": svc.get("name", ""),
        "self": _self_url("services", sid),
        "html_url": _html_url("services", sid),
        "name": svc.get("name", ""),
        "description": svc.get("description", ""),
        "auto_resolve_timeout": svc.get("auto_resolve_timeout"),
        "acknowledgement_timeout": svc.get("acknowledgement_timeout"),
        "created_at": svc.get("created_at"),
        "updated_at": svc.get("updated_at"),
        "status": svc.get("status", "active"),
        "alert_creation": svc.get("alert_creation",
                                  "create_alerts_and_incidents"),
        "escalation_policy": _ref(ep, "escalation_policy_reference")
        if ep else None,
        "teams": teams,
        "integrations": svc.get("integrations", []),
        "incident_urgency_rule": svc.get(
            "incident_urgency_rule",
            {"type": "constant", "urgency": "high"}),
    }


def _full_escalation_policy(state: dict, ep: dict) -> dict:
    if not ep:
        return ep
    eid = ep["id"]
    teams = [_ref(state["teams"][tid], "team_reference")
             for tid in ep.get("team_ids", []) if tid in state["teams"]]
    rules = []
    for rule in ep.get("escalation_rules", []):
        targets = []
        for tref in rule.get("targets", []):
            ttype = tref.get("type", "user_reference")
            tid = tref.get("id")
            obj = None
            if ttype == "user_reference" or ttype == "user":
                obj = state["users"].get(tid)
                if obj:
                    targets.append(_ref(obj, "user_reference"))
            elif ttype == "schedule_reference" or ttype == "schedule":
                obj = state["schedules"].get(tid)
                if obj:
                    targets.append(_ref(obj, "schedule_reference"))
            else:
                targets.append(tref)
        rules.append({
            "id": rule.get("id"),
            "escalation_delay_in_minutes": rule.get(
                "escalation_delay_in_minutes", 30),
            "targets": targets,
        })
    return {
        "id": eid,
        "type": "escalation_policy",
        "summary": ep.get("name", ""),
        "self": _self_url("escalation_policies", eid),
        "html_url": _html_url("escalation_policies", eid),
        "name": ep.get("name", ""),
        "description": ep.get("description", ""),
        "num_loops": ep.get("num_loops", 0),
        "on_call_handoff_notifications": ep.get(
            "on_call_handoff_notifications", "if_has_services"),
        "escalation_rules": rules,
        "services": [_ref(s, "service_reference")
                     for s in state["services"].values()
                     if s.get("escalation_policy_id") == eid],
        "teams": teams,
    }


def _full_schedule(state: dict, sch: dict) -> dict:
    if not sch:
        return sch
    sid = sch["id"]
    users = [_ref(state["users"][uid], "user_reference")
             for uid in sch.get("user_ids", []) if uid in state["users"]]
    teams = [_ref(state["teams"][tid], "team_reference")
             for tid in sch.get("team_ids", []) if tid in state["teams"]]
    return {
        "id": sid,
        "type": "schedule",
        "summary": sch.get("name", ""),
        "self": _self_url("schedules", sid),
        "html_url": _html_url("schedules", sid),
        "name": sch.get("name", ""),
        "description": sch.get("description", ""),
        "time_zone": sch.get("time_zone", "UTC"),
        "schedule_layers": sch.get("schedule_layers", []),
        "overrides_subschedule": sch.get(
            "overrides_subschedule",
            {"name": "Overrides", "rendered_schedule_entries": []}),
        "final_schedule": sch.get(
            "final_schedule",
            {"name": "Final Schedule", "rendered_schedule_entries": []}),
        "users": users,
        "teams": teams,
        "escalation_policies": [
            _ref(ep, "escalation_policy_reference")
            for ep in state["escalation_policies"].values()
            if any(sid == tgt.get("id")
                   for rule in ep.get("escalation_rules", [])
                   for tgt in rule.get("targets", [])
                   if tgt.get("type") in ("schedule_reference", "schedule"))
        ],
    }


def _full_incident(state: dict, inc: dict) -> dict:
    if not inc:
        return inc
    iid = inc["id"]
    svc = state["services"].get(inc.get("service_id"))
    ep = state["escalation_policies"].get(inc.get("escalation_policy_id"))
    assignments = []
    for a in inc.get("assignments", []):
        uid = a.get("assignee_id") if isinstance(a, dict) else a
        u = state["users"].get(uid)
        if u:
            assignments.append({
                "at": a.get("at", inc.get("created_at"))
                if isinstance(a, dict) else inc.get("created_at"),
                "assignee": _ref(u, "user_reference"),
            })
    acknowledgements = []
    for a in inc.get("acknowledgements", []):
        uid = a.get("acknowledger_id") if isinstance(a, dict) else a
        u = state["users"].get(uid)
        if u:
            acknowledgements.append({
                "at": a.get("at") if isinstance(a, dict) else inc.get(
                    "last_status_change_at"),
                "acknowledger": _ref(u, "user_reference"),
            })
    teams = [_ref(state["teams"][tid], "team_reference")
             for tid in inc.get("team_ids", []) if tid in state["teams"]]
    body = inc.get("body") or {
        "type": "incident_body",
        "details": inc.get("details", ""),
    }
    last_status_user_id = inc.get("last_status_change_by_id")
    last_status_user = (state["users"].get(last_status_user_id)
                        if last_status_user_id else None)
    return {
        "id": iid,
        "type": "incident",
        "summary": f"[#{inc.get('incident_number')}] {inc.get('title', '')}",
        "self": _self_url("incidents", iid),
        "html_url": _html_url("incidents", iid),
        "incident_number": inc.get("incident_number"),
        "title": inc.get("title", ""),
        "description": inc.get("description", inc.get("title", "")),
        "created_at": inc.get("created_at"),
        "updated_at": inc.get("updated_at", inc.get("created_at")),
        "status": inc.get("status", "triggered"),
        "incident_key": inc.get("incident_key"),
        "service": _ref(svc, "service_reference") if svc else None,
        "assignments": assignments,
        "assigned_via": inc.get("assigned_via", "escalation_policy"),
        "last_status_change_at": inc.get("last_status_change_at",
                                         inc.get("created_at")),
        "last_status_change_by": (_ref(last_status_user, "user_reference")
                                  if last_status_user
                                  else (_ref(svc, "service_reference")
                                        if svc else None)),
        "first_trigger_log_entry": inc.get("first_trigger_log_entry"),
        "escalation_policy": (_ref(ep, "escalation_policy_reference")
                              if ep else None),
        "teams": teams,
        "priority": inc.get("priority"),
        "urgency": inc.get("urgency", "high"),
        "resolve_reason": inc.get("resolve_reason"),
        "alert_counts": inc.get("alert_counts",
                                {"all": 0, "triggered": 0, "resolved": 0}),
        "body": body,
        "acknowledgements": acknowledgements,
        "is_mergeable": inc.get("is_mergeable", True),
    }


def _full_alert(state: dict, alert: dict) -> dict:
    if not alert:
        return alert
    aid = alert["id"]
    inc = state["incidents"].get(alert.get("incident_id"))
    svc = state["services"].get(alert.get("service_id"))
    return {
        "id": aid,
        "type": "alert",
        "summary": alert.get("summary", ""),
        "self": f"{API_BASE}/alerts/{aid}",
        "html_url": f"{WEB_BASE}/alerts/{aid}",
        "created_at": alert.get("created_at"),
        "status": alert.get("status", "triggered"),
        "alert_key": alert.get("alert_key", aid),
        "service": _ref(svc, "service_reference") if svc else None,
        "incident": _ref(inc, "incident_reference") if inc else None,
        "suppressed": bool(alert.get("suppressed", False)),
        "severity": alert.get("severity", "critical"),
        "integration": alert.get("integration"),
        "body": alert.get("body", {"type": "alert_body", "details": {}}),
    }


def _full_note(state: dict, note: dict) -> dict:
    if not note:
        return note
    u = state["users"].get(note.get("user_id"))
    return {
        "id": note["id"],
        "user": _ref(u, "user_reference") if u else None,
        "channel": note.get("channel", {"type": "web"}),
        "content": note.get("content", ""),
        "trimmed": False,
        "created_at": note.get("created_at"),
    }


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("pagerduty-mock")


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"triggered", "acknowledged", "resolved"}
_VALID_URGENCIES = {"high", "low"}


@mcp.tool(name="list_incidents")
def list_incidents(since: str = "",
                   until: str = "",
                   statuses: list | None = None,
                   incident_key: str = "",
                   service_ids: list | None = None,
                   team_ids: list | None = None,
                   user_ids: list | None = None,
                   urgencies: list | None = None,
                   sort_by: str = "created_at:desc",
                   limit: int = 25,
                   offset: int = 0) -> dict:
    """GET /incidents — list incidents, filterable by status, service,
    team, assigned user, urgency, and time range. Returns the standard
    PD list envelope `{"incidents":[...], "limit","offset","total","more"}`.
    """
    with _lock():
        s = _load_state()
        items = list(s["incidents"].values())
        if statuses:
            wanted = set(statuses)
            items = [i for i in items if i.get("status") in wanted]
        if incident_key:
            items = [i for i in items
                     if i.get("incident_key") == incident_key]
        if service_ids:
            ss = set(service_ids)
            items = [i for i in items if i.get("service_id") in ss]
        if team_ids:
            ts = set(team_ids)
            items = [i for i in items
                     if any(t in ts for t in i.get("team_ids", []))]
        if user_ids:
            us = set(user_ids)
            items = [i for i in items
                     if any((a.get("assignee_id") if isinstance(a, dict)
                             else a) in us
                            for a in i.get("assignments", []))]
        if urgencies:
            urg = set(urgencies)
            items = [i for i in items if i.get("urgency") in urg]
        if since:
            items = [i for i in items
                     if (i.get("created_at") or "") >= since]
        if until:
            items = [i for i in items
                     if (i.get("created_at") or "") <= until]
        # sort
        reverse = sort_by.endswith(":desc")
        key = sort_by.split(":", 1)[0] or "created_at"
        items.sort(key=lambda i: (i.get(key) or ""), reverse=reverse)
        page, env = _paginate(items, limit, offset)
        out = {
            "incidents": [_full_incident(s, i) for i in page],
            **env,
        }
        _record(s, "list_incidents", statuses=statuses,
                service_ids=service_ids, count=len(page))
        _save_state(s)
        return out


@mcp.tool(name="get_incident")
def get_incident(id: str) -> dict:
    """GET /incidents/{id} — retrieve a single incident."""
    with _lock():
        s = _load_state()
        inc = s["incidents"].get(id)
        _record(s, "get_incident", id=id,
                result="ok" if inc else "not_found")
        _save_state(s)
        if not inc:
            return _not_found(f"Incident {id}")
        return {"incident": _full_incident(s, inc)}


@mcp.tool(name="create_incident")
def create_incident(incident: dict,
                    from_email: str = "") -> dict:
    """POST /incidents — create an incident. `incident` must include:
        type ("incident"), title, service ({id,type:service_reference}).
    Optional: urgency ('high'|'low'), body, incident_key,
    priority ({id,type:priority_reference}), escalation_policy,
    assignments. `From` header is supplied via `from_email`.
    """
    with _lock():
        s = _load_state()
        if not isinstance(incident, dict):
            _record(s, "create_incident", result="invalid_payload")
            _save_state(s)
            return _invalid("incident must be an object")
        title = incident.get("title")
        if not title:
            _record(s, "create_incident", result="missing_title")
            _save_state(s)
            return _invalid("title is required",
                            ["title: must be present"])
        svc_ref = incident.get("service") or {}
        svc_id = svc_ref.get("id") if isinstance(svc_ref, dict) else None
        svc = s["services"].get(svc_id) if svc_id else None
        if not svc:
            _record(s, "create_incident", result="invalid_service",
                    service_id=svc_id)
            _save_state(s)
            return _invalid("invalid service",
                            [f"service: {svc_id} not found"])
        urgency = incident.get("urgency") or svc.get(
            "incident_urgency_rule", {}).get("urgency", "high")
        if urgency not in _VALID_URGENCIES:
            _record(s, "create_incident", result="invalid_urgency")
            _save_state(s)
            return _invalid("invalid urgency",
                            [f"urgency: must be one of "
                             f"{sorted(_VALID_URGENCIES)}"])
        # Resolve actor by email (PD's `From` header semantics)
        actor = None
        if from_email:
            for u in s["users"].values():
                if u.get("email") == from_email:
                    actor = u
                    break
        # Incident key idempotency
        ikey = incident.get("incident_key")
        if ikey:
            for existing in s["incidents"].values():
                if (existing.get("incident_key") == ikey
                        and existing.get("status") != "resolved"):
                    _record(s, "create_incident", result="dedup",
                            incident_id=existing["id"])
                    _save_state(s)
                    return {"incident": _full_incident(s, existing)}
        # Build the incident
        iid = _new_id(s)
        num = s["next_incident_number"]
        s["next_incident_number"] = num + 1
        now = _now_iso()
        # Determine assignments from escalation policy or explicit list
        assignments = []
        for a in incident.get("assignments", []) or []:
            assignee = a.get("assignee") if isinstance(a, dict) else None
            aid = assignee.get("id") if isinstance(assignee, dict) else None
            if aid and aid in s["users"]:
                assignments.append({"at": now, "assignee_id": aid})
        ep_ref = incident.get("escalation_policy") or {}
        ep_id = ep_ref.get("id") if isinstance(ep_ref, dict) else None
        if not ep_id:
            ep_id = svc.get("escalation_policy_id")
        if not assignments and ep_id and ep_id in s["escalation_policies"]:
            ep = s["escalation_policies"][ep_id]
            for rule in ep.get("escalation_rules", []):
                for tgt in rule.get("targets", []):
                    if tgt.get("type") in ("user_reference", "user"):
                        tid = tgt.get("id")
                        if tid in s["users"]:
                            assignments.append({"at": now,
                                                "assignee_id": tid})
                if assignments:
                    break
        body = incident.get("body") or {"type": "incident_body",
                                        "details": title}
        new_inc = {
            "id": iid,
            "incident_number": num,
            "title": title,
            "description": incident.get("description", title),
            "created_at": now,
            "updated_at": now,
            "status": "triggered",
            "incident_key": ikey or iid,
            "service_id": svc_id,
            "assignments": assignments,
            "assigned_via": ("direct_assignment"
                             if incident.get("assignments")
                             else "escalation_policy"),
            "last_status_change_at": now,
            "last_status_change_by_id": (actor or {}).get("id"),
            "first_trigger_log_entry": None,
            "escalation_policy_id": ep_id,
            "team_ids": [t.get("id") for t in incident.get("teams", []) or []
                         if isinstance(t, dict) and t.get("id")
                         in s["teams"]],
            "priority": incident.get("priority"),
            "urgency": urgency,
            "alert_counts": {"all": 0, "triggered": 0, "resolved": 0},
            "body": body,
            "acknowledgements": [],
            "is_mergeable": True,
        }
        s["incidents"][iid] = new_inc
        s["incident_notes"].setdefault(iid, [])
        s["incident_alerts"].setdefault(iid, [])
        s["incident_log_entries"].setdefault(iid, []).append({
            "id": _new_id(s, "R"),
            "type": "trigger_log_entry",
            "summary": f"Triggered through the API",
            "created_at": now,
            "agent": (_ref(actor, "user_reference") if actor
                      else _ref(svc, "service_reference")),
        })
        _record(s, "create_incident", incident_id=iid,
                title=title, service_id=svc_id, from_email=from_email)
        _save_state(s)
        return {"incident": _full_incident(s, new_inc)}


@mcp.tool(name="manage_incidents")
def manage_incidents(incidents: list,
                     from_email: str = "") -> dict:
    """PUT /incidents — bulk update incidents. Each list element is
    `{"id":"P...","type":"incident_reference","status":"acknowledged"|
    "resolved", "title"?, "priority"?, "escalation_level"?,
    "assignments"?, "escalation_policy"?, "resolution"?}`. Status
    transitions are the primary mechanism for ack/resolve.
    """
    with _lock():
        s = _load_state()
        if not isinstance(incidents, list) or not incidents:
            _record(s, "manage_incidents", result="invalid_payload")
            _save_state(s)
            return _invalid("incidents must be a non-empty array")
        actor = None
        if from_email:
            for u in s["users"].values():
                if u.get("email") == from_email:
                    actor = u
                    break
        updated_list = []
        errors = []
        for item in incidents:
            if not isinstance(item, dict) or not item.get("id"):
                errors.append({"error": "missing id"})
                continue
            iid = item["id"]
            inc = s["incidents"].get(iid)
            if not inc:
                errors.append({"id": iid, "error": "not_found"})
                continue
            now = _now_iso()
            new_status = item.get("status")
            if new_status:
                if new_status not in _VALID_STATUSES:
                    errors.append({"id": iid,
                                   "error": f"invalid status {new_status}"})
                    continue
                old_status = inc.get("status")
                if old_status == "resolved" and new_status != "resolved":
                    errors.append({"id": iid,
                                   "error": "cannot un-resolve incident"})
                    continue
                inc["status"] = new_status
                inc["last_status_change_at"] = now
                inc["last_status_change_by_id"] = (actor or {}).get("id")
                if new_status == "acknowledged" and actor:
                    inc.setdefault("acknowledgements", []).append({
                        "at": now, "acknowledger_id": actor["id"],
                    })
                if new_status == "resolved":
                    inc["resolved_at"] = now
                    if item.get("resolution"):
                        inc["resolve_reason"] = {
                            "type": "merge_resolve_reason",
                            "incident": None,
                        } if isinstance(item["resolution"], dict) else None
            if item.get("title"):
                inc["title"] = item["title"]
            if item.get("priority"):
                inc["priority"] = item["priority"]
            if item.get("urgency") in _VALID_URGENCIES:
                inc["urgency"] = item["urgency"]
            if item.get("escalation_policy"):
                ep_ref = item["escalation_policy"]
                ep_id = (ep_ref.get("id") if isinstance(ep_ref, dict)
                         else None)
                if ep_id and ep_id in s["escalation_policies"]:
                    inc["escalation_policy_id"] = ep_id
            if item.get("assignments"):
                new_assignments = []
                for a in item["assignments"]:
                    assignee = (a.get("assignee") if isinstance(a, dict)
                                else None)
                    aid = (assignee.get("id") if isinstance(assignee, dict)
                           else None)
                    if aid in s["users"]:
                        new_assignments.append({"at": now,
                                                "assignee_id": aid})
                inc["assignments"] = new_assignments
            inc["updated_at"] = now
            s["incident_log_entries"].setdefault(iid, []).append({
                "id": _new_id(s, "R"),
                "type": (f"{new_status}_log_entry"
                         if new_status else "annotate_log_entry"),
                "summary": f"Updated incident",
                "created_at": now,
                "agent": _ref(actor, "user_reference") if actor else None,
            })
            updated_list.append(inc)
        _record(s, "manage_incidents", count=len(updated_list),
                errors=len(errors), from_email=from_email)
        _save_state(s)
        return {
            "incidents": [_full_incident(s, i) for i in updated_list],
        }


@mcp.tool(name="create_incident_note")
def create_incident_note(id: str,
                         note: dict,
                         from_email: str = "") -> dict:
    """POST /incidents/{id}/notes — add a note to an incident.
    `note` is `{"content":"..."}`. Returns `{"note": {...}}`.
    """
    with _lock():
        s = _load_state()
        inc = s["incidents"].get(id)
        if not inc:
            _record(s, "create_incident_note", id=id, result="not_found")
            _save_state(s)
            return _not_found(f"Incident {id}")
        if not isinstance(note, dict) or not note.get("content"):
            _record(s, "create_incident_note", id=id,
                    result="invalid_payload")
            _save_state(s)
            return _invalid("note.content is required")
        actor = None
        if from_email:
            for u in s["users"].values():
                if u.get("email") == from_email:
                    actor = u
                    break
        nid = _new_id(s, "N")
        now = _now_iso()
        rec = {
            "id": nid,
            "user_id": (actor or {}).get("id"),
            "content": note["content"],
            "created_at": now,
            "channel": note.get("channel", {"type": "web"}),
        }
        s["incident_notes"].setdefault(id, []).append(rec)
        _record(s, "create_incident_note", id=id, note_id=nid)
        _save_state(s)
        return {"note": _full_note(s, rec)}


@mcp.tool(name="list_incident_alerts")
def list_incident_alerts(id: str,
                         statuses: list | None = None,
                         alert_key: str = "",
                         sort_by: str = "created_at:desc",
                         limit: int = 25,
                         offset: int = 0) -> dict:
    """GET /incidents/{id}/alerts — list alerts grouped under an
    incident. Returns `{"alerts":[...], limit, offset, total, more}`.
    """
    with _lock():
        s = _load_state()
        inc = s["incidents"].get(id)
        if not inc:
            _record(s, "list_incident_alerts", id=id, result="not_found")
            _save_state(s)
            return _not_found(f"Incident {id}")
        alerts = list(s["incident_alerts"].get(id, []))
        if statuses:
            wanted = set(statuses)
            alerts = [a for a in alerts if a.get("status") in wanted]
        if alert_key:
            alerts = [a for a in alerts
                      if a.get("alert_key") == alert_key]
        reverse = sort_by.endswith(":desc")
        key = sort_by.split(":", 1)[0] or "created_at"
        alerts.sort(key=lambda a: (a.get(key) or ""), reverse=reverse)
        page, env = _paginate(alerts, limit, offset)
        _record(s, "list_incident_alerts", id=id, count=len(page))
        _save_state(s)
        return {
            "alerts": [_full_alert(s, a) for a in page],
            **env,
        }


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@mcp.tool(name="list_services")
def list_services(query: str = "",
                  team_ids: list | None = None,
                  include: list | None = None,
                  sort_by: str = "name",
                  limit: int = 25,
                  offset: int = 0) -> dict:
    """GET /services — list services. Optional `query` is a
    case-insensitive substring match on the service name."""
    with _lock():
        s = _load_state()
        items = list(s["services"].values())
        if query:
            q = query.lower()
            items = [x for x in items if q in (x.get("name") or "").lower()]
        if team_ids:
            ts = set(team_ids)
            items = [x for x in items
                     if any(t in ts for t in x.get("team_ids", []))]
        reverse = sort_by.endswith(":desc")
        key = sort_by.split(":", 1)[0] or "name"
        items.sort(key=lambda x: (x.get(key) or ""), reverse=reverse)
        page, env = _paginate(items, limit, offset)
        _record(s, "list_services", query=query, count=len(page))
        _save_state(s)
        return {
            "services": [_full_service(s, x) for x in page],
            **env,
        }


@mcp.tool(name="get_service")
def get_service(id: str, include: list | None = None) -> dict:
    """GET /services/{id} — retrieve a service."""
    with _lock():
        s = _load_state()
        svc = s["services"].get(id)
        _record(s, "get_service", id=id,
                result="ok" if svc else "not_found")
        _save_state(s)
        if not svc:
            return _not_found(f"Service {id}")
        return {"service": _full_service(s, svc)}


@mcp.tool(name="create_service")
def create_service(service: dict) -> dict:
    """POST /services — create a service. `service` requires
    `name` and `escalation_policy` (a `{id,type:"escalation_policy_
    reference"}` ref). Optional: description, auto_resolve_timeout,
    acknowledgement_timeout, alert_creation."""
    with _lock():
        s = _load_state()
        if not isinstance(service, dict) or not service.get("name"):
            _record(s, "create_service", result="missing_name")
            _save_state(s)
            return _invalid("service.name is required")
        ep_ref = service.get("escalation_policy") or {}
        ep_id = ep_ref.get("id") if isinstance(ep_ref, dict) else None
        if not ep_id or ep_id not in s["escalation_policies"]:
            _record(s, "create_service",
                    result="invalid_escalation_policy")
            _save_state(s)
            return _invalid("escalation_policy is required",
                            [f"escalation_policy: {ep_id} not found"])
        sid = _new_id(s)
        now = _now_iso()
        svc = {
            "id": sid,
            "name": service["name"],
            "description": service.get("description", ""),
            "auto_resolve_timeout": service.get("auto_resolve_timeout"),
            "acknowledgement_timeout": service.get(
                "acknowledgement_timeout"),
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "alert_creation": service.get(
                "alert_creation", "create_alerts_and_incidents"),
            "escalation_policy_id": ep_id,
            "team_ids": [t.get("id") for t in service.get("teams", []) or []
                         if isinstance(t, dict) and t.get("id")
                         in s["teams"]],
            "integrations": service.get("integrations", []),
            "incident_urgency_rule": service.get(
                "incident_urgency_rule",
                {"type": "constant", "urgency": "high"}),
        }
        s["services"][sid] = svc
        _record(s, "create_service", id=sid, name=svc["name"])
        _save_state(s)
        return {"service": _full_service(s, svc)}


# ---------------------------------------------------------------------------
# Escalation policies
# ---------------------------------------------------------------------------

@mcp.tool(name="list_escalation_policies")
def list_escalation_policies(query: str = "",
                             user_ids: list | None = None,
                             team_ids: list | None = None,
                             include: list | None = None,
                             sort_by: str = "name",
                             limit: int = 25,
                             offset: int = 0) -> dict:
    """GET /escalation_policies — list escalation policies."""
    with _lock():
        s = _load_state()
        items = list(s["escalation_policies"].values())
        if query:
            q = query.lower()
            items = [x for x in items if q in (x.get("name") or "").lower()]
        if team_ids:
            ts = set(team_ids)
            items = [x for x in items
                     if any(t in ts for t in x.get("team_ids", []))]
        if user_ids:
            us = set(user_ids)
            def _has_user(ep):
                for rule in ep.get("escalation_rules", []):
                    for tgt in rule.get("targets", []):
                        if (tgt.get("type") in ("user_reference", "user")
                                and tgt.get("id") in us):
                            return True
                return False
            items = [x for x in items if _has_user(x)]
        reverse = sort_by.endswith(":desc")
        key = sort_by.split(":", 1)[0] or "name"
        items.sort(key=lambda x: (x.get(key) or ""), reverse=reverse)
        page, env = _paginate(items, limit, offset)
        _record(s, "list_escalation_policies", query=query,
                count=len(page))
        _save_state(s)
        return {
            "escalation_policies": [_full_escalation_policy(s, x)
                                    for x in page],
            **env,
        }


@mcp.tool(name="get_escalation_policy")
def get_escalation_policy(id: str,
                          include: list | None = None) -> dict:
    """GET /escalation_policies/{id} — retrieve an escalation policy."""
    with _lock():
        s = _load_state()
        ep = s["escalation_policies"].get(id)
        _record(s, "get_escalation_policy", id=id,
                result="ok" if ep else "not_found")
        _save_state(s)
        if not ep:
            return _not_found(f"Escalation policy {id}")
        return {"escalation_policy": _full_escalation_policy(s, ep)}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool(name="list_users")
def list_users(query: str = "",
               team_ids: list | None = None,
               include: list | None = None,
               limit: int = 25,
               offset: int = 0) -> dict:
    """GET /users — list users. `query` matches name OR email."""
    with _lock():
        s = _load_state()
        items = list(s["users"].values())
        if query:
            q = query.lower()
            items = [x for x in items
                     if q in (x.get("name") or "").lower()
                     or q in (x.get("email") or "").lower()]
        if team_ids:
            ts = set(team_ids)
            items = [x for x in items
                     if any(t in ts for t in x.get("team_ids", []))]
        items.sort(key=lambda x: (x.get("name") or "").lower())
        page, env = _paginate(items, limit, offset)
        _record(s, "list_users", query=query, count=len(page))
        _save_state(s)
        return {
            "users": [_full_user(s, x) for x in page],
            **env,
        }


@mcp.tool(name="get_user")
def get_user(id: str, include: list | None = None) -> dict:
    """GET /users/{id} — retrieve a user."""
    with _lock():
        s = _load_state()
        u = s["users"].get(id)
        _record(s, "get_user", id=id,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _not_found(f"User {id}")
        return {"user": _full_user(s, u)}


@mcp.tool(name="get_current_user")
def get_current_user(include: list | None = None) -> dict:
    """GET /users/me — retrieve the user the API token belongs to."""
    with _lock():
        s = _load_state()
        uid = s.get("self_id")
        u = s["users"].get(uid)
        _record(s, "get_current_user", id=uid,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _not_found("Current user")
        return {"user": _full_user(s, u)}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@mcp.tool(name="list_teams")
def list_teams(query: str = "",
               limit: int = 25,
               offset: int = 0) -> dict:
    """GET /teams — list teams."""
    with _lock():
        s = _load_state()
        items = list(s["teams"].values())
        if query:
            q = query.lower()
            items = [x for x in items if q in (x.get("name") or "").lower()]
        items.sort(key=lambda x: (x.get("name") or "").lower())
        page, env = _paginate(items, limit, offset)
        _record(s, "list_teams", query=query, count=len(page))
        _save_state(s)
        return {
            "teams": [_full_team(s, x) for x in page],
            **env,
        }


@mcp.tool(name="get_team")
def get_team(id: str) -> dict:
    """GET /teams/{id} — retrieve a team."""
    with _lock():
        s = _load_state()
        t = s["teams"].get(id)
        _record(s, "get_team", id=id,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _not_found(f"Team {id}")
        return {"team": _full_team(s, t)}


# ---------------------------------------------------------------------------
# Schedules + on-calls
# ---------------------------------------------------------------------------

@mcp.tool(name="list_schedules")
def list_schedules(query: str = "",
                   include: list | None = None,
                   limit: int = 25,
                   offset: int = 0) -> dict:
    """GET /schedules — list on-call schedules."""
    with _lock():
        s = _load_state()
        items = list(s["schedules"].values())
        if query:
            q = query.lower()
            items = [x for x in items if q in (x.get("name") or "").lower()]
        items.sort(key=lambda x: (x.get("name") or "").lower())
        page, env = _paginate(items, limit, offset)
        _record(s, "list_schedules", query=query, count=len(page))
        _save_state(s)
        return {
            "schedules": [_full_schedule(s, x) for x in page],
            **env,
        }


@mcp.tool(name="get_schedule")
def get_schedule(id: str,
                 since: str = "",
                 until: str = "",
                 time_zone: str = "UTC") -> dict:
    """GET /schedules/{id} — retrieve a schedule, optionally with a
    rendered window between `since` and `until`."""
    with _lock():
        s = _load_state()
        sch = s["schedules"].get(id)
        _record(s, "get_schedule", id=id,
                result="ok" if sch else "not_found")
        _save_state(s)
        if not sch:
            return _not_found(f"Schedule {id}")
        return {"schedule": _full_schedule(s, sch)}


@mcp.tool(name="list_on_calls")
def list_on_calls(time_zone: str = "UTC",
                  include: list | None = None,
                  user_ids: list | None = None,
                  escalation_policy_ids: list | None = None,
                  schedule_ids: list | None = None,
                  since: str = "",
                  until: str = "",
                  earliest: bool = False,
                  limit: int = 25,
                  offset: int = 0) -> dict:
    """GET /oncalls — list current/upcoming on-call assignments.
    Returns `{"oncalls": [...], ...}` where each oncall is
    `{escalation_policy, escalation_level, schedule, user, start, end}`.
    """
    with _lock():
        s = _load_state()
        oncalls = []
        for ep in s["escalation_policies"].values():
            if (escalation_policy_ids
                    and ep["id"] not in escalation_policy_ids):
                continue
            for rule in ep.get("escalation_rules", []):
                lvl = rule.get("escalation_level", 1)
                for tgt in rule.get("targets", []):
                    ttype = tgt.get("type")
                    if ttype in ("user_reference", "user"):
                        uid = tgt.get("id")
                        u = s["users"].get(uid)
                        if not u:
                            continue
                        if user_ids and uid not in user_ids:
                            continue
                        oncalls.append({
                            "escalation_policy": _ref(
                                ep, "escalation_policy_reference"),
                            "escalation_level": lvl,
                            "schedule": None,
                            "user": _ref(u, "user_reference"),
                            "start": None,
                            "end": None,
                        })
                    elif ttype in ("schedule_reference", "schedule"):
                        sid = tgt.get("id")
                        sch = s["schedules"].get(sid)
                        if not sch:
                            continue
                        if schedule_ids and sid not in schedule_ids:
                            continue
                        # Use the first user listed on the schedule as
                        # the "current" on-call. Real PD renders this
                        # from the schedule layers, but the mock keeps
                        # things deterministic.
                        for uid in sch.get("user_ids", []):
                            u = s["users"].get(uid)
                            if not u:
                                continue
                            if user_ids and uid not in user_ids:
                                continue
                            oncalls.append({
                                "escalation_policy": _ref(
                                    ep, "escalation_policy_reference"),
                                "escalation_level": lvl,
                                "schedule": _ref(sch, "schedule_reference"),
                                "user": _ref(u, "user_reference"),
                                "start": since or None,
                                "end": until or None,
                            })
                            if earliest:
                                break
        page, env = _paginate(oncalls, limit, offset)
        _record(s, "list_on_calls", count=len(page))
        _save_state(s)
        return {"oncalls": page, **env}


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(account: dict | None = None,
                    self_user: dict | None = None,
                    users: list | None = None,
                    teams: list | None = None,
                    services: list | None = None,
                    escalation_policies: list | None = None,
                    schedules: list | None = None,
                    incidents: list | None = None,
                    notes: list | None = None,
                    alerts: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed PD state with PagerDuty-ish dicts. Each item
    accepts an optional `id`; one will be generated if missing.

    - users: [{id?, name, email, time_zone?, role?, team_ids?: [tid]}]
    - teams: [{id?, name, description?}]
    - services: [{id?, name, description?, escalation_policy_id,
                  team_ids?: [tid]}]
    - escalation_policies: [{id?, name, description?, num_loops?,
        escalation_rules: [{escalation_delay_in_minutes,
        targets:[{id, type:"user_reference"|"schedule_reference"}]}]}]
    - schedules: [{id?, name, time_zone?, user_ids?: [uid]}]
    - incidents: [{id?, title, service_id, urgency?, status?,
                   assignments?: [{assignee_id}], priority?, body?}]
    - notes: [{incident_id, content, user_id?}]
    - alerts: [{incident_id, summary, status?, severity?, alert_key?,
                body?}]

    If `replace` is true, the entire state is reset first.
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if account:
            s["account"].update(account)
        if self_user:
            sid = self_user.get("id") or _new_id(s, "PUSER")
            s["self_id"] = sid
            existing = s["users"].get(sid, {})
            existing.update({
                "id": sid,
                "name": self_user.get("name", "Mock Bot"),
                "email": self_user.get("email", "bot@mock.pagerduty.com"),
                "role": self_user.get("role", "admin"),
                "time_zone": self_user.get("time_zone", "UTC"),
                "team_ids": self_user.get("team_ids", []),
            })
            s["users"][sid] = existing
        for t in teams or []:
            tid = t.get("id") or _new_id(s, "PT")
            s["teams"][tid] = {
                "id": tid,
                "name": t.get("name", tid),
                "description": t.get("description", ""),
                "default_role": t.get("default_role", "manager"),
                "parent": t.get("parent"),
            }
        for u in users or []:
            uid = u.get("id") or _new_id(s, "PU")
            s["users"][uid] = {
                "id": uid,
                "name": u.get("name", uid),
                "email": u.get("email", f"{uid.lower()}@mock.pagerduty.com"),
                "time_zone": u.get("time_zone", "UTC"),
                "color": u.get("color", "blue"),
                "role": u.get("role", "user"),
                "avatar_url": u.get("avatar_url",
                                    f"{WEB_BASE}/avatars/{uid}.png"),
                "description": u.get("description"),
                "invitation_sent": bool(u.get("invitation_sent", False)),
                "job_title": u.get("job_title"),
                "team_ids": [tid for tid in u.get("team_ids", [])
                             if tid in s["teams"]],
                "contact_methods": u.get("contact_methods", []),
                "notification_rules": u.get("notification_rules", []),
            }
        for ep in escalation_policies or []:
            eid = ep.get("id") or _new_id(s, "PE")
            s["escalation_policies"][eid] = {
                "id": eid,
                "name": ep.get("name", eid),
                "description": ep.get("description", ""),
                "num_loops": ep.get("num_loops", 0),
                "on_call_handoff_notifications": ep.get(
                    "on_call_handoff_notifications", "if_has_services"),
                "escalation_rules": ep.get("escalation_rules", []),
                "team_ids": [tid for tid in ep.get("team_ids", [])
                             if tid in s["teams"]],
            }
        for sch in schedules or []:
            sid = sch.get("id") or _new_id(s, "PS")
            s["schedules"][sid] = {
                "id": sid,
                "name": sch.get("name", sid),
                "description": sch.get("description", ""),
                "time_zone": sch.get("time_zone", "UTC"),
                "schedule_layers": sch.get("schedule_layers", []),
                "user_ids": [uid for uid in sch.get("user_ids", [])
                             if uid in s["users"]],
                "team_ids": [tid for tid in sch.get("team_ids", [])
                             if tid in s["teams"]],
                "final_schedule": sch.get(
                    "final_schedule",
                    {"name": "Final Schedule",
                     "rendered_schedule_entries": []}),
                "overrides_subschedule": sch.get(
                    "overrides_subschedule",
                    {"name": "Overrides",
                     "rendered_schedule_entries": []}),
            }
        for svc in services or []:
            sid = svc.get("id") or _new_id(s, "PSVC")
            ep_id = svc.get("escalation_policy_id")
            s["services"][sid] = {
                "id": sid,
                "name": svc.get("name", sid),
                "description": svc.get("description", ""),
                "auto_resolve_timeout": svc.get("auto_resolve_timeout"),
                "acknowledgement_timeout": svc.get(
                    "acknowledgement_timeout"),
                "created_at": svc.get("created_at", _now_iso()),
                "updated_at": svc.get("updated_at", _now_iso()),
                "status": svc.get("status", "active"),
                "alert_creation": svc.get(
                    "alert_creation", "create_alerts_and_incidents"),
                "escalation_policy_id": (ep_id if ep_id
                                         in s["escalation_policies"]
                                         else None),
                "team_ids": [tid for tid in svc.get("team_ids", [])
                             if tid in s["teams"]],
                "integrations": svc.get("integrations", []),
                "incident_urgency_rule": svc.get(
                    "incident_urgency_rule",
                    {"type": "constant", "urgency": "high"}),
            }
        for inc in incidents or []:
            iid = inc.get("id") or _new_id(s, "PI")
            num = inc.get("incident_number") or s["next_incident_number"]
            if num >= s["next_incident_number"]:
                s["next_incident_number"] = num + 1
            assignments = []
            for a in inc.get("assignments", []) or []:
                if isinstance(a, dict) and a.get("assignee_id"):
                    assignments.append({
                        "at": a.get("at", _now_iso()),
                        "assignee_id": a["assignee_id"],
                    })
                elif isinstance(a, str):
                    assignments.append({"at": _now_iso(),
                                        "assignee_id": a})
            svc_id = inc.get("service_id")
            s["incidents"][iid] = {
                "id": iid,
                "incident_number": num,
                "title": inc.get("title", iid),
                "description": inc.get("description", inc.get("title", "")),
                "created_at": inc.get("created_at", _now_iso()),
                "updated_at": inc.get("updated_at",
                                      inc.get("created_at", _now_iso())),
                "status": inc.get("status", "triggered"),
                "incident_key": inc.get("incident_key", iid),
                "service_id": (svc_id if svc_id in s["services"] else None),
                "assignments": assignments,
                "assigned_via": inc.get("assigned_via",
                                        "escalation_policy"),
                "last_status_change_at": inc.get(
                    "last_status_change_at",
                    inc.get("created_at", _now_iso())),
                "last_status_change_by_id": inc.get(
                    "last_status_change_by_id"),
                "first_trigger_log_entry": inc.get(
                    "first_trigger_log_entry"),
                "escalation_policy_id": inc.get(
                    "escalation_policy_id",
                    s["services"].get(svc_id, {}).get(
                        "escalation_policy_id") if svc_id else None),
                "team_ids": [tid for tid in inc.get("team_ids", [])
                             if tid in s["teams"]],
                "priority": inc.get("priority"),
                "urgency": inc.get("urgency", "high"),
                "alert_counts": inc.get(
                    "alert_counts",
                    {"all": 0, "triggered": 0, "resolved": 0}),
                "body": inc.get("body", {"type": "incident_body",
                                         "details": inc.get("title", "")}),
                "acknowledgements": inc.get("acknowledgements", []),
                "is_mergeable": inc.get("is_mergeable", True),
            }
            s["incident_notes"].setdefault(iid, [])
            s["incident_alerts"].setdefault(iid, [])
            s["incident_log_entries"].setdefault(iid, [])
        for n in notes or []:
            iid = n.get("incident_id")
            if iid not in s["incidents"]:
                continue
            nid = n.get("id") or _new_id(s, "PN")
            s["incident_notes"].setdefault(iid, []).append({
                "id": nid,
                "user_id": n.get("user_id"),
                "content": n.get("content", ""),
                "created_at": n.get("created_at", _now_iso()),
                "channel": n.get("channel", {"type": "web"}),
            })
        for a in alerts or []:
            iid = a.get("incident_id")
            if iid not in s["incidents"]:
                continue
            aid = a.get("id") or _new_id(s, "PA")
            s["incident_alerts"].setdefault(iid, []).append({
                "id": aid,
                "incident_id": iid,
                "service_id": s["incidents"][iid].get("service_id"),
                "summary": a.get("summary", ""),
                "status": a.get("status", "triggered"),
                "alert_key": a.get("alert_key", aid),
                "severity": a.get("severity", "critical"),
                "suppressed": bool(a.get("suppressed", False)),
                "created_at": a.get("created_at", _now_iso()),
                "integration": a.get("integration"),
                "body": a.get("body", {"type": "alert_body", "details": {}}),
            })
            # bump incident alert counts
            counts = s["incidents"][iid].setdefault(
                "alert_counts", {"all": 0, "triggered": 0, "resolved": 0})
            counts["all"] = counts.get("all", 0) + 1
            counts[a.get("status", "triggered")] = counts.get(
                a.get("status", "triggered"), 0) + 1
        _record(s, "debug_seed",
                counts={
                    "users": len(users or []),
                    "teams": len(teams or []),
                    "services": len(services or []),
                    "escalation_policies": len(escalation_policies or []),
                    "schedules": len(schedules or []),
                    "incidents": len(incidents or []),
                    "notes": len(notes or []),
                    "alerts": len(alerts or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "user_ids": list(s["users"].keys()),
            "team_ids": list(s["teams"].keys()),
            "service_ids": list(s["services"].keys()),
            "escalation_policy_ids": list(s["escalation_policies"].keys()),
            "schedule_ids": list(s["schedules"].keys()),
            "incident_ids": list(s["incidents"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
