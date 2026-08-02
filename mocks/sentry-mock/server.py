"""Sentry mock MCP server.

Mirrors Sentry's REST API v0 (`/api/0/...`,
https://docs.sentry.io/api/). Tools are named after the real Sentry
endpoint operations (list_projects, list_project_issues,
update_issue, ...) and accept/return the same JSON shapes the
upstream Sentry API serves so an agent trained against real Sentry
sees the same interface.

Resource hierarchy mirrors Sentry's:
  organization (slug, e.g. "mockcorp")
    └─ team (slug, e.g. "engineering")
    └─ project (slug, e.g. "backend") — belongs to org, owned by team
        └─ issue (id "123456", shortId "BE-1")
            └─ event (id 32-hex)
            └─ comment
        └─ alert rule
        └─ environment
    └─ release (version, e.g. "1.0.0")
    └─ member

Tool surface (matches Sentry API v0 operations 1:1, plus a few
convenience wrappers around `update_issue`):

  Organizations / teams / members / projects:
    list_organizations, get_organization, list_projects, get_project,
    create_project, list_teams, get_team, list_organization_members

  Issues:
    list_project_issues, list_organization_issues, get_issue,
    update_issue, delete_issue, resolve_issue, assign_issue,
    ignore_issue

  Events:
    list_issue_events, get_latest_event_for_issue, get_event

  Comments:
    list_issue_comments, create_issue_comment

  Releases:
    list_releases, get_release, create_release

  Environments + alerts:
    list_environments, list_project_alerts, create_project_alert

Plus mock-only debug helpers: mock_debug_state, mock_debug_seed_*.

State lives at `$SENTRY_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/sentry_mock`). Per-rollout isolation should clear the
state dir between rollouts. Optional `SENTRY_MOCK_SEED_PATH`
preloads state when no state.json exists yet.

State shape:

  state = {
    "organizations":  {slug: <Organization>},
    "teams":          {(org_slug, team_slug): <Team>},
    "projects":       {(org_slug, project_slug): <Project>},
    "members":        {(org_slug, member_id): <Member>},
    "issues":         {issue_id: <Issue>},
    "events":         {event_id: <Event>},
    "issue_events":   {issue_id: [event_id, ...]},
    "releases":       {(org_slug, version): <Release>},
    "comments":       {issue_id: [<Comment>, ...]},
    "alert_rules":    {(org_slug, project_slug, rule_id): <AlertRule>},
    "environments":   {(org_slug, project_slug): [name, ...]},
    "next_id": {"issue": 1, "comment": 1, "member": 1, "team": 1,
                 "project": 1, "alert_rule": 1, "short_id": {}, ...},
    "calls": [...]
  }

Sentry conventions mirrored verbatim:

  - Slugs:    lowercase, hyphen-separated  (`^[a-z0-9-]+$`)
  - Issue id: numeric string  ("123456")
  - shortId:  `<PREFIX>-<N>` where PREFIX is a 2-4 char uppercase
              abbreviation of the project slug ("frontend" → "FE-1",
              "ios-app" → "IA-1")
  - Event id: 32 hex chars
  - Timestamps: ISO 8601 with `Z` suffix ("2024-01-01T00:00:00Z")

Intentionally NOT supported (out of scope for this mock):

  - Event ingestion (Sentry SDK envelope) endpoint
  - Source maps + symbolication
  - Performance: transactions, spans, profiling, replay, monitors
  - Alerts wizard (rule conditions evaluator), notification actions
  - Codeowners, integration installs, dashboards

Every call (reads + writes) appends to `state["calls"]` so verifiers
can replay the trace.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


WEB_BASE = "https://mock.sentry.io"
API_BASE = "https://mock.sentry.io/api/0"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "SENTRY_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/sentry_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    """Build empty state, seeded with one default org / team / project.

    Mirrors `mock_seed.sentry.empty_state` so an unseeded fresh server
    still has a usable mockcorp/engineering/backend shell.
    """
    org_slug = "mockcorp"
    team_slug = "engineering"
    project_slug = "backend"
    now = "2024-01-01T00:00:00Z"
    state: dict = {
        "organizations": {},
        "teams": {},
        "projects": {},
        "members": {},
        "issues": {},
        "events": {},
        "issue_events": {},
        "releases": {},
        "comments": {},
        "alert_rules": {},
        "environments": {},
        "next_id": {
            "issue": 1,
            "comment": 1,
            "member": 1,
            "team": 1,
            "project": 1,
            "alert_rule": 1,
            "release": 1,
            "organization": 1,
            "short_id": {},
        },
        "calls": [],
    }
    org = {
        "id": "1",
        "slug": org_slug,
        "name": "MockCorp",
        "status": {"id": "active", "name": "active"},
        "dateCreated": now,
        "isEarlyAdopter": False,
        "require2FA": False,
        "avatar": {"avatarType": "letter_avatar", "avatarUuid": None},
        "features": [],
    }
    state["organizations"][org_slug] = org
    state["next_id"]["organization"] = 2
    team = {
        "id": "1",
        "slug": team_slug,
        "name": "Engineering",
        "dateCreated": now,
        "isMember": True,
        "hasAccess": True,
        "isPending": False,
        "memberCount": 0,
        "avatar": {"avatarType": "letter_avatar", "avatarUuid": None},
        "orgSlug": org_slug,
    }
    state["teams"][f"{org_slug}::{team_slug}"] = team
    state["next_id"]["team"] = 2
    project = _new_project_record(
        pid="1", slug=project_slug, name="backend",
        platform="python", org_slug=org_slug, team_slugs=[team_slug],
        date_created=now,
    )
    state["projects"][f"{org_slug}::{project_slug}"] = project
    state["next_id"]["project"] = 2
    state["next_id"]["short_id"][f"{org_slug}::{project_slug}"] = 1
    state["environments"][f"{org_slug}::{project_slug}"] = []
    return state


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("SENTRY_MOCK_SEED_PATH")
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
# ID + slug helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("_", "-").replace(" ", "-")
    s = _SLUG_RE.sub("", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "item"


def _next_id(state: dict, kind: str) -> str:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return str(n)


def _project_prefix(slug: str) -> str:
    """Generate Sentry's shortId prefix from a project slug.

    Sentry derives the prefix from the slug: take the first letter of
    each hyphen-delimited segment, uppercase, pad/clip to 2-4 chars.
    "frontend" -> "FE" (first two letters), "ios-app" -> "IA",
    "data-pipeline-v2" -> "DPV".
    """
    if not slug:
        return "PRJ"
    parts = [p for p in slug.split("-") if p]
    if len(parts) >= 2:
        letters = "".join(p[0] for p in parts[:4])
        return letters.upper()
    # single-segment: take first two letters
    return (parts[0][:2] or "PR").upper()


def _next_short_id(state: dict, org_slug: str, project_slug: str) -> str:
    key = f"{org_slug}::{project_slug}"
    n = state["next_id"]["short_id"].get(key, 1)
    state["next_id"]["short_id"][key] = n + 1
    return f"{_project_prefix(project_slug)}-{n}"


def _gen_event_id() -> str:
    return secrets.token_hex(16)


def _gen_share_id() -> str:
    return secrets.token_hex(7)


# ---------------------------------------------------------------------------
# Object constructors / enrichers
# ---------------------------------------------------------------------------

VALID_LEVELS = {"debug", "info", "warning", "error", "fatal"}
VALID_STATUSES = {"resolved", "resolvedInNextRelease", "unresolved",
                  "ignored", "reprocessing"}
VALID_PLATFORMS = {
    "python", "javascript", "javascript-react", "javascript-vue",
    "javascript-node", "node", "ruby", "rails", "go", "java",
    "java-android", "csharp", "php", "rust", "cocoa", "cocoa-swift",
    "objective-c", "react-native", "electron", "flutter", "other",
}


def _new_project_record(*, pid: str, slug: str, name: str, platform: str,
                        org_slug: str, team_slugs: list[str],
                        date_created: str) -> dict:
    return {
        "id": pid,
        "slug": slug,
        "name": name,
        "platform": platform,
        "isPublic": False,
        "isBookmarked": False,
        "color": "#3fbf7f",
        "dateCreated": date_created,
        "firstEvent": None,
        "hasAccess": True,
        "hasMinifiedStackTrace": False,
        "hasMonitors": False,
        "hasProfiles": False,
        "hasReplays": False,
        "hasSessions": False,
        "isInternal": False,
        "isMember": True,
        "features": [],
        "status": "active",
        "platforms": [platform] if platform else [],
        "latestRelease": None,
        "orgSlug": org_slug,
        "teamSlugs": list(team_slugs),
    }


def _public_project(state: dict, p: dict) -> dict:
    """Strip internal fields and embed teams/organization refs."""
    if not p:
        return p
    out = {k: v for k, v in p.items()
           if k not in {"orgSlug", "teamSlugs"}}
    org = state["organizations"].get(p.get("orgSlug"))
    if org:
        out["organization"] = _short_org(org)
    teams = []
    for ts in p.get("teamSlugs", []):
        t = state["teams"].get(f"{p.get('orgSlug')}::{ts}")
        if t:
            teams.append(_short_team(t))
    out["teams"] = teams
    return out


def _short_org(org: dict) -> dict:
    return {
        "id": org["id"], "slug": org["slug"], "name": org["name"],
        "status": org.get("status", {"id": "active", "name": "active"}),
    }


def _short_team(team: dict) -> dict:
    return {
        "id": team["id"], "slug": team["slug"], "name": team["name"],
    }


def _short_project(p: dict) -> dict:
    return {
        "id": p["id"], "slug": p["slug"], "name": p["name"],
        "platform": p.get("platform"),
    }


def _public_org(org: dict) -> dict:
    return {k: v for k, v in (org or {}).items()
            if k not in {"_internal"}}


def _public_team(state: dict, t: dict) -> dict:
    if not t:
        return t
    return {k: v for k, v in t.items() if k != "orgSlug"}


def _public_issue(state: dict, issue: dict) -> dict:
    """Return the issue dict in the shape Sentry serves."""
    if not issue:
        return issue
    iid = issue["id"]
    proj = state["projects"].get(
        f"{issue.get('orgSlug')}::{issue.get('projectSlug')}")
    comments = state["comments"].get(iid, [])
    fr_ver = issue.get("firstReleaseVersion")
    lr_ver = issue.get("lastReleaseVersion")
    fr = (state["releases"].get(f"{issue.get('orgSlug')}::{fr_ver}")
          if fr_ver else None)
    lr = (state["releases"].get(f"{issue.get('orgSlug')}::{lr_ver}")
          if lr_ver else None)
    out = {
        "id": iid,
        "shareId": issue.get("shareId"),
        "shortId": issue.get("shortId"),
        "title": issue.get("title", ""),
        "culprit": issue.get("culprit", ""),
        "permalink": (f"{WEB_BASE}/organizations/"
                      f"{issue.get('orgSlug')}/issues/{iid}/"),
        "logger": issue.get("logger"),
        "level": issue.get("level", "error"),
        "status": issue.get("status", "unresolved"),
        "statusDetails": issue.get("statusDetails", {}),
        "isPublic": bool(issue.get("isPublic", False)),
        "platform": issue.get("platform", "python"),
        "project": _short_project(proj) if proj else None,
        "type": issue.get("type", "error"),
        "metadata": issue.get("metadata", {}),
        "numComments": len(comments),
        "assignedTo": issue.get("assignedTo"),
        "isBookmarked": bool(issue.get("isBookmarked", False)),
        "isSubscribed": bool(issue.get("isSubscribed", True)),
        "hasSeen": bool(issue.get("hasSeen", False)),
        "annotations": issue.get("annotations", []),
        "isUnhandled": bool(issue.get("isUnhandled", True)),
        "count": str(issue.get("count", 1)),
        "userCount": int(issue.get("userCount", 1)),
        "firstSeen": issue.get("firstSeen"),
        "lastSeen": issue.get("lastSeen"),
        "stats": issue.get("stats", {"24h": []}),
        "firstRelease": _short_release(fr) if fr else None,
        "lastRelease": _short_release(lr) if lr else None,
    }
    if "tags" in issue:
        out["tags"] = issue["tags"]
    return out


def _public_event(state: dict, event: dict) -> dict:
    if not event:
        return event
    eid = event["id"]
    return {
        "id": eid,
        "eventID": eid,
        "groupID": event.get("issueId"),
        "tags": event.get("tags", []),
        "contexts": event.get("contexts", {}),
        "message": event.get("message", ""),
        "title": event.get("title", event.get("message", "")),
        "location": event.get("location"),
        "culprit": event.get("culprit"),
        "level": event.get("level", "error"),
        "platform": event.get("platform", "python"),
        "dateCreated": event.get("dateCreated"),
        "dateReceived": event.get("dateReceived",
                                   event.get("dateCreated")),
        "fingerprints": event.get("fingerprints", ["{{ default }}"]),
        "user": event.get("user"),
        "entries": event.get("entries", []),
        "release": event.get("release"),
        "environment": event.get("environment"),
        "sdk": event.get("sdk", {"name": "sentry.python", "version": "1.0.0"}),
    }


def _short_release(r: dict) -> dict:
    return {
        "version": r["version"],
        "shortVersion": r.get("shortVersion", r["version"]),
        "ref": r.get("ref"),
        "url": r.get("url"),
        "dateReleased": r.get("dateReleased"),
        "dateCreated": r.get("dateCreated"),
    }


def _public_release(state: dict, r: dict) -> dict:
    if not r:
        return r
    return {
        "version": r["version"],
        "shortVersion": r.get("shortVersion", r["version"]),
        "ref": r.get("ref"),
        "url": r.get("url"),
        "dateReleased": r.get("dateReleased"),
        "dateCreated": r.get("dateCreated"),
        "data": r.get("data", {}),
        "newGroups": int(r.get("newGroups", 0)),
        "owner": r.get("owner"),
        "commitCount": int(r.get("commitCount", 0)),
        "lastCommit": r.get("lastCommit"),
        "deployCount": int(r.get("deployCount", 0)),
        "lastDeploy": r.get("lastDeploy"),
        "authors": r.get("authors", []),
        "projects": [
            _short_project(state["projects"].get(f"{r.get('orgSlug')}::{ps}"))
            for ps in r.get("projectSlugs", [])
            if state["projects"].get(f"{r.get('orgSlug')}::{ps}")
        ],
        "firstEvent": r.get("firstEvent"),
        "lastEvent": r.get("lastEvent"),
    }


def _public_member(m: dict) -> dict:
    if not m:
        return m
    return {k: v for k, v in m.items() if k != "orgSlug"}


def _public_alert(rule: dict) -> dict:
    if not rule:
        return rule
    return {k: v for k, v in rule.items()
            if k not in {"orgSlug", "projectSlug"}}


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------

def _err(detail: str, status: int = 400) -> dict:
    return {"detail": detail, "status": status}


def _not_found(what: str) -> dict:
    return _err(f"The requested resource does not exist ({what})",
                status=404)


def _invalid(detail: str) -> dict:
    return _err(detail, status=400)


# ---------------------------------------------------------------------------
# Query DSL + period parsing
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)


def _parse_period_seconds(period: str | None) -> int | None:
    """Parse Sentry-style statsPeriod (`24h`, `7d`, `14d`, ...) into
    seconds. Returns None if input is empty/unrecognized."""
    if not period:
        return None
    m = _PERIOD_RE.match(period.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return n * mult


def _iso_to_dt(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_query(query: str | None) -> dict:
    """Parse Sentry's search-query DSL into a structured filter dict.

    Supported facets:
        is:resolved|unresolved|ignored
        is:assigned|unassigned
        level:<level>
        assigned:me|<username>
        release:<version>
        environment:<env>
        event.type:error
        has:<tag>
        <tag>:<value>     (e.g. browser:Chrome, transaction:/foo)
        <free-text>       (matches title/culprit/message)

    Returns:
        {
            "is": set[str], "level": set[str], "assigned": set[str],
            "release": set[str], "environment": set[str],
            "event.type": set[str], "has": set[str],
            "tags": list[(key, value)],
            "free_text": list[str],
        }
    """
    out = {
        "is": set(),
        "level": set(),
        "assigned": set(),
        "release": set(),
        "environment": set(),
        "event.type": set(),
        "has": set(),
        "tags": [],
        "free_text": [],
    }
    if not query:
        return out
    # Split on whitespace, respect "quoted phrases"
    tokens = re.findall(r'"[^"]*"|\S+', query)
    for raw in tokens:
        tok = raw.strip('"')
        if not tok:
            continue
        if ":" in tok:
            key, _, val = tok.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in ("is", "level", "release", "environment",
                       "event.type", "has"):
                out[key].add(val)
            elif key == "assigned" or key == "assigned_or_suggested":
                out["assigned"].add(val)
            elif key:
                out["tags"].append((key, val))
        else:
            out["free_text"].append(tok.lower())
    return out


def _issue_matches_query(issue: dict, parsed: dict, *,
                         viewer_username: str | None = None) -> bool:
    # is: facets
    for flag in parsed["is"]:
        if flag == "resolved":
            if issue.get("status") != "resolved":
                return False
        elif flag == "unresolved":
            if issue.get("status") != "unresolved":
                return False
        elif flag == "ignored":
            if issue.get("status") != "ignored":
                return False
        elif flag == "assigned":
            if not issue.get("assignedTo"):
                return False
        elif flag == "unassigned":
            if issue.get("assignedTo"):
                return False
    # level
    if parsed["level"]:
        if issue.get("level") not in parsed["level"]:
            return False
    # assigned
    for who in parsed["assigned"]:
        target = who
        if target == "me" and viewer_username:
            target = viewer_username
        cur = issue.get("assignedTo") or ""
        if isinstance(cur, dict):
            cur = cur.get("name") or cur.get("username") or ""
        if cur != target:
            return False
    # release
    if parsed["release"]:
        cur = (issue.get("lastReleaseVersion")
               or issue.get("firstReleaseVersion"))
        if cur not in parsed["release"]:
            return False
    # environment
    if parsed["environment"]:
        envs = set(issue.get("environments", []))
        if not (envs & parsed["environment"]):
            return False
    # event.type
    if parsed["event.type"]:
        if issue.get("type") not in parsed["event.type"]:
            return False
    # has:<tag>
    if parsed["has"]:
        tags = {t.get("key") for t in issue.get("tags", [])
                if isinstance(t, dict)}
        if not parsed["has"].issubset(tags):
            return False
    # arbitrary tag:value pairs
    for key, val in parsed["tags"]:
        found = False
        for t in issue.get("tags", []):
            if (isinstance(t, dict) and t.get("key") == key
                    and t.get("value") == val):
                found = True
                break
        if not found:
            return False
    # free text matches title/culprit
    if parsed["free_text"]:
        hay = " ".join([
            issue.get("title", ""),
            issue.get("culprit", ""),
            (issue.get("metadata") or {}).get("value", ""),
        ]).lower()
        for needle in parsed["free_text"]:
            if needle not in hay:
                return False
    return True


def _filter_by_stats_period(items: list[dict], period: str | None,
                            field: str = "lastSeen") -> list[dict]:
    secs = _parse_period_seconds(period)
    if secs is None:
        return items
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(seconds=secs)
    kept = []
    for it in items:
        ts = _iso_to_dt(it.get(field))
        if ts is None:
            kept.append(it)
        elif ts >= cutoff:
            kept.append(it)
    return kept


def _sort_issues(items: list[dict], sort: str) -> list[dict]:
    sort = (sort or "date").lower()
    if sort == "new":
        return sorted(items, key=lambda i: i.get("firstSeen") or "",
                      reverse=True)
    if sort == "freq":
        return sorted(items, key=lambda i: int(i.get("count", 0) or 0),
                      reverse=True)
    if sort == "user":
        return sorted(items, key=lambda i: int(i.get("userCount", 0) or 0),
                      reverse=True)
    if sort == "priority":
        order = {"fatal": 0, "error": 1, "warning": 2, "info": 3, "debug": 4}
        return sorted(items, key=lambda i: order.get(
            i.get("level", "error"), 9))
    # "date" (default): lastSeen desc
    return sorted(items, key=lambda i: i.get("lastSeen") or "", reverse=True)


def _paginate(items: list, cursor: str | None, limit: int) -> tuple[list, dict]:
    """Cursor pagination shaped like Sentry's `cursor=<offset>:0:0` token.

    The mock degrades the cursor to a simple integer offset so the
    agent can ignore the inner structure and still page deterministically.
    """
    if limit is None or limit <= 0:
        limit = 100
    if limit > 100:
        limit = 100
    offset = 0
    if cursor:
        # Sentry cursor format: "<offset>:0:0" — we extract the first
        # integer and treat it as offset.
        m = re.match(r"^(-?\d+)", str(cursor))
        if m:
            offset = max(0, int(m.group(1)))
    page = items[offset: offset + limit]
    next_offset = offset + limit
    has_more = next_offset < len(items)
    env = {
        "next_cursor": f"{next_offset}:0:0" if has_more else None,
        "has_more": has_more,
    }
    return page, env


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("sentry-mock")


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------

@mcp.tool(name="list_organizations")
def list_organizations() -> list:
    """GET /api/0/organizations/ — list all organizations the API user
    has access to. Returns a list of Organization objects."""
    with _lock():
        s = _load_state()
        items = sorted(s["organizations"].values(),
                       key=lambda o: o.get("slug", ""))
        _record(s, "list_organizations", count=len(items))
        _save_state(s)
        return [_public_org(o) for o in items]


@mcp.tool(name="get_organization")
def get_organization(organizationSlug: str) -> dict:
    """GET /api/0/organizations/{organization_slug}/ — retrieve a
    single organization by slug."""
    with _lock():
        s = _load_state()
        org = s["organizations"].get(organizationSlug)
        _record(s, "get_organization", organization_slug=organizationSlug,
                result="ok" if org else "not_found")
        _save_state(s)
        if not org:
            return _not_found(f"organization {organizationSlug}")
        return _public_org(org)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@mcp.tool(name="list_projects")
def list_projects(organizationSlug: str) -> list:
    """GET /api/0/organizations/{organization_slug}/projects/ — list
    all projects under an organization."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "list_projects", organization_slug=organizationSlug,
                    result="org_not_found")
            _save_state(s)
            return []
        items = [p for k, p in s["projects"].items()
                 if k.startswith(f"{organizationSlug}::")]
        items.sort(key=lambda p: p.get("slug", ""))
        _record(s, "list_projects", organization_slug=organizationSlug,
                count=len(items))
        _save_state(s)
        return [_public_project(s, p) for p in items]


@mcp.tool(name="get_project")
def get_project(organizationSlug: str, projectSlug: str) -> dict:
    """GET /api/0/projects/{organization_slug}/{project_slug}/ —
    retrieve one project."""
    with _lock():
        s = _load_state()
        p = s["projects"].get(f"{organizationSlug}::{projectSlug}")
        _record(s, "get_project", organization_slug=organizationSlug,
                project_slug=projectSlug,
                result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            return _not_found(f"project {organizationSlug}/{projectSlug}")
        return _public_project(s, p)


@mcp.tool(name="create_project")
def create_project(organizationSlug: str, teamSlug: str, name: str,
                   slug: str | None = None,
                   platform: str = "python") -> dict:
    """POST /api/0/teams/{organization_slug}/{team_slug}/projects/ —
    create a new project under a team.

    `slug` is auto-derived from `name` when omitted. `platform` must
    be one of Sentry's supported platforms (e.g. `python`,
    `javascript-react`, `node`, `go`).
    """
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "create_project", result="org_not_found",
                    organization_slug=organizationSlug)
            _save_state(s)
            return _not_found(f"organization {organizationSlug}")
        if f"{organizationSlug}::{teamSlug}" not in s["teams"]:
            _record(s, "create_project", result="team_not_found",
                    organization_slug=organizationSlug, team_slug=teamSlug)
            _save_state(s)
            return _not_found(f"team {organizationSlug}/{teamSlug}")
        if not name:
            _record(s, "create_project", result="missing_name")
            _save_state(s)
            return _invalid("name is required")
        proj_slug = _slugify(slug or name)
        key = f"{organizationSlug}::{proj_slug}"
        if key in s["projects"]:
            _record(s, "create_project", result="slug_taken",
                    project_slug=proj_slug)
            _save_state(s)
            return _invalid(f"slug already in use: {proj_slug}")
        if platform and platform not in VALID_PLATFORMS:
            # Sentry does enforce a platform whitelist; mirror it.
            _record(s, "create_project", result="invalid_platform",
                    platform=platform)
            _save_state(s)
            return _invalid(f"invalid platform: {platform}")
        pid = _next_id(s, "project")
        now = _now_iso()
        project = _new_project_record(
            pid=pid, slug=proj_slug, name=name,
            platform=platform or "other",
            org_slug=organizationSlug, team_slugs=[teamSlug],
            date_created=now,
        )
        s["projects"][key] = project
        s["next_id"]["short_id"][key] = 1
        s["environments"][key] = []
        _record(s, "create_project", id=pid, slug=proj_slug,
                organization_slug=organizationSlug, team_slug=teamSlug,
                platform=platform)
        _save_state(s)
        return _public_project(s, project)


# ---------------------------------------------------------------------------
# Teams + members
# ---------------------------------------------------------------------------

@mcp.tool(name="list_teams")
def list_teams(organizationSlug: str) -> list:
    """GET /api/0/organizations/{organization_slug}/teams/ — list
    all teams in the organization."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "list_teams", result="org_not_found",
                    organization_slug=organizationSlug)
            _save_state(s)
            return []
        items = [t for k, t in s["teams"].items()
                 if k.startswith(f"{organizationSlug}::")]
        items.sort(key=lambda t: t.get("slug", ""))
        _record(s, "list_teams", organization_slug=organizationSlug,
                count=len(items))
        _save_state(s)
        return [_public_team(s, t) for t in items]


@mcp.tool(name="get_team")
def get_team(organizationSlug: str, teamSlug: str) -> dict:
    """GET /api/0/teams/{organization_slug}/{team_slug}/ — retrieve a
    single team."""
    with _lock():
        s = _load_state()
        t = s["teams"].get(f"{organizationSlug}::{teamSlug}")
        _record(s, "get_team", organization_slug=organizationSlug,
                team_slug=teamSlug, result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return _not_found(f"team {organizationSlug}/{teamSlug}")
        return _public_team(s, t)


@mcp.tool(name="list_organization_members")
def list_organization_members(organizationSlug: str) -> list:
    """GET /api/0/organizations/{organization_slug}/members/ — list
    all members of the organization."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "list_organization_members", result="org_not_found")
            _save_state(s)
            return []
        items = [m for k, m in s["members"].items()
                 if k.startswith(f"{organizationSlug}::")]
        items.sort(key=lambda m: m.get("email", ""))
        _record(s, "list_organization_members",
                organization_slug=organizationSlug, count=len(items))
        _save_state(s)
        return [_public_member(m) for m in items]


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def _viewer_username(state: dict) -> str | None:
    """Return the API user's username for `assigned:me` resolution."""
    # The default viewer in this mock is "mockbot"; let scenarios
    # override via the `viewer` state key.
    viewer = state.get("viewer") or {}
    return viewer.get("username") or "mockbot"


def _list_issues_for_org(state: dict, org_slug: str,
                         project_slugs: list[str] | None,
                         query: str, stats_period: str | None,
                         environment: str | None,
                         sort: str) -> list[dict]:
    items = []
    for iss in state["issues"].values():
        if iss.get("orgSlug") != org_slug:
            continue
        if project_slugs:
            if iss.get("projectSlug") not in project_slugs:
                continue
        if environment:
            envs = iss.get("environments", [])
            if environment not in envs:
                continue
        items.append(iss)
    parsed = _parse_query(query)
    vname = _viewer_username(state)
    items = [i for i in items
             if _issue_matches_query(i, parsed, viewer_username=vname)]
    items = _filter_by_stats_period(items, stats_period)
    items = _sort_issues(items, sort)
    return items


@mcp.tool(name="list_project_issues")
def list_project_issues(organizationSlug: str, projectSlug: str,
                        query: str = "",
                        statsPeriod: str = "",
                        environment: str = "",
                        sort: str = "date",
                        cursor: str = "",
                        limit: int = 100) -> list:
    """GET /api/0/projects/{organization_slug}/{project_slug}/issues/

    Filter with Sentry's search-query DSL (e.g. `is:unresolved
    level:error`). `statsPeriod` accepts `24h`, `7d`, `14d`, `30d`,
    `90d`. `sort` is one of `new|priority|date|freq|user`.
    """
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "list_project_issues", result="project_not_found",
                    organization_slug=organizationSlug,
                    project_slug=projectSlug)
            _save_state(s)
            return []
        items = _list_issues_for_org(
            s, organizationSlug, [projectSlug],
            query, statsPeriod or None,
            environment or None, sort,
        )
        page, env = _paginate(items, cursor or None, limit)
        _record(s, "list_project_issues",
                organization_slug=organizationSlug,
                project_slug=projectSlug,
                query=query, sort=sort, count=len(page))
        _save_state(s)
        return [_public_issue(s, i) for i in page]


@mcp.tool(name="list_organization_issues")
def list_organization_issues(organizationSlug: str,
                             query: str = "",
                             statsPeriod: str = "",
                             environment: str = "",
                             sort: str = "date",
                             project: list | None = None,
                             cursor: str = "",
                             limit: int = 100) -> list:
    """GET /api/0/organizations/{organization_slug}/issues/

    Same surface as list_project_issues but spans all projects in
    the org. `project` is a list of project slugs to restrict to.
    """
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "list_organization_issues", result="org_not_found")
            _save_state(s)
            return []
        items = _list_issues_for_org(
            s, organizationSlug, list(project) if project else None,
            query, statsPeriod or None,
            environment or None, sort,
        )
        page, env = _paginate(items, cursor or None, limit)
        _record(s, "list_organization_issues",
                organization_slug=organizationSlug,
                query=query, sort=sort, count=len(page))
        _save_state(s)
        return [_public_issue(s, i) for i in page]


@mcp.tool(name="get_issue")
def get_issue(organizationSlug: str, issueId: str) -> dict:
    """GET /api/0/organizations/{organization_slug}/issues/{issue_id}/
    Returns the full Issue object including metadata, tags, stats,
    firstRelease and lastRelease."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "get_issue", id=issueId, result="not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        _record(s, "get_issue", id=issueId, result="ok")
        _save_state(s)
        return _public_issue(s, iss)


def _apply_status_details(iss: dict, status: str,
                          status_details: dict | None) -> None:
    sd = status_details or {}
    iss["statusDetails"] = {}
    if status == "resolved":
        if sd.get("inNextRelease"):
            iss["status"] = "resolvedInNextRelease"
            iss["statusDetails"] = {"inNextRelease": True}
        elif sd.get("inRelease"):
            iss["statusDetails"] = {"inRelease": sd["inRelease"]}
        elif sd.get("inCommit"):
            iss["statusDetails"] = {"inCommit": sd["inCommit"]}
    elif status == "ignored":
        for key in ("ignoreDuration", "ignoreCount", "ignoreUserCount",
                    "ignoreWindow", "ignoreUserWindow", "ignoreUntil"):
            if key in sd:
                iss["statusDetails"][key] = sd[key]


def _update_issue_inner(state: dict, iss: dict, *,
                         status: str | None,
                         status_details: dict | None,
                         assigned_to: Any,
                         has_seen: bool | None,
                         is_bookmarked: bool | None,
                         is_subscribed: bool | None,
                         is_public: bool | None) -> dict:
    if status is not None:
        if status not in VALID_STATUSES:
            return _invalid(
                f"status must be one of {sorted(VALID_STATUSES)}")
        iss["status"] = (
            "resolvedInNextRelease"
            if status == "resolved" and (status_details or {}).get(
                "inNextRelease") else status)
        _apply_status_details(iss, status, status_details)
    elif status_details is not None:
        _apply_status_details(iss, iss.get("status", "unresolved"),
                              status_details)
    if assigned_to is not None:
        if assigned_to == "" or assigned_to is False:
            iss["assignedTo"] = None
        elif isinstance(assigned_to, str):
            if assigned_to.startswith("team:"):
                team_slug = assigned_to.split(":", 1)[1]
                t = state["teams"].get(
                    f"{iss.get('orgSlug')}::{team_slug}")
                if t:
                    iss["assignedTo"] = {
                        "type": "team", "id": t["id"],
                        "name": t["name"], "slug": t["slug"],
                    }
                else:
                    return _invalid(f"team not found: {team_slug}")
            else:
                # username (or username equivalent) lookup
                member = None
                for m in state["members"].values():
                    if m.get("orgSlug") != iss.get("orgSlug"):
                        continue
                    if (m.get("username") == assigned_to
                            or m.get("email") == assigned_to):
                        member = m
                        break
                if member:
                    iss["assignedTo"] = {
                        "type": "user", "id": member["id"],
                        "name": member.get("name"),
                        "username": member.get("username"),
                        "email": member.get("email"),
                    }
                else:
                    # Sentry allows unknown usernames at the API; mirror
                    # that by storing the raw string-shape.
                    iss["assignedTo"] = {
                        "type": "user", "username": assigned_to,
                        "name": assigned_to,
                    }
        elif isinstance(assigned_to, dict):
            iss["assignedTo"] = assigned_to
    if has_seen is not None:
        iss["hasSeen"] = bool(has_seen)
    if is_bookmarked is not None:
        iss["isBookmarked"] = bool(is_bookmarked)
    if is_subscribed is not None:
        iss["isSubscribed"] = bool(is_subscribed)
    if is_public is not None:
        iss["isPublic"] = bool(is_public)
    return {}


@mcp.tool(name="update_issue")
def update_issue(organizationSlug: str, issueId: str,
                 status: str | None = None,
                 statusDetails: dict | None = None,
                 assignedTo: Any = None,
                 hasSeen: bool | None = None,
                 isBookmarked: bool | None = None,
                 isSubscribed: bool | None = None,
                 isPublic: bool | None = None) -> dict:
    """PUT /api/0/organizations/{organization_slug}/issues/{issue_id}/

    `status` is one of resolved | resolvedInNextRelease | unresolved
    | ignored | reprocessing. `statusDetails` carries `{inNextRelease:
    True}`, `{inRelease: "1.0.0"}`, `{ignoreDuration: <minutes>}`, etc.
    `assignedTo` is a username, `team:<slug>`, or null to unassign.
    """
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "update_issue", id=issueId, result="not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        err = _update_issue_inner(
            s, iss, status=status, status_details=statusDetails,
            assigned_to=assignedTo, has_seen=hasSeen,
            is_bookmarked=isBookmarked, is_subscribed=isSubscribed,
            is_public=isPublic,
        )
        if err:
            _record(s, "update_issue", id=issueId, result="invalid",
                    detail=err)
            _save_state(s)
            return err
        _record(s, "update_issue", id=issueId,
                status=status, assigned_to=assignedTo)
        _save_state(s)
        return _public_issue(s, iss)


@mcp.tool(name="delete_issue")
def delete_issue(organizationSlug: str, issueId: str) -> dict:
    """DELETE /api/0/organizations/{organization_slug}/issues/{issue_id}/
    Returns 204-style success. Issue + its events + comments are
    dropped from state."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "delete_issue", id=issueId, result="not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        # remove events + comments
        for eid in list(s["issue_events"].get(str(issueId), [])):
            s["events"].pop(eid, None)
        s["issue_events"].pop(str(issueId), None)
        s["comments"].pop(str(issueId), None)
        s["issues"].pop(str(issueId), None)
        _record(s, "delete_issue", id=issueId, result="ok")
        _save_state(s)
        return {"ok": True}


@mcp.tool(name="resolve_issue")
def resolve_issue(organizationSlug: str, issueId: str,
                  inNextRelease: bool = False,
                  inRelease: str | None = None) -> dict:
    """Convenience wrapper: PUT update_issue with status=resolved.

    Pass `inNextRelease=True` for `resolvedInNextRelease`, or
    `inRelease="<version>"` to scope the resolution to a release."""
    sd: dict = {}
    if inNextRelease:
        sd["inNextRelease"] = True
    if inRelease:
        sd["inRelease"] = inRelease
    return update_issue(
        organizationSlug=organizationSlug, issueId=issueId,
        status="resolved", statusDetails=sd or None,
    )


@mcp.tool(name="assign_issue")
def assign_issue(organizationSlug: str, issueId: str,
                 assignee: str) -> dict:
    """Convenience wrapper: PUT update_issue with `assignedTo`.
    `assignee` is a username, or `team:<slug>` to assign to a team."""
    return update_issue(
        organizationSlug=organizationSlug, issueId=issueId,
        assignedTo=assignee,
    )


@mcp.tool(name="ignore_issue")
def ignore_issue(organizationSlug: str, issueId: str,
                 ignoreDuration: int | None = None) -> dict:
    """Convenience wrapper: PUT update_issue with status=ignored.
    `ignoreDuration` is the snooze duration in minutes."""
    sd: dict = {}
    if ignoreDuration is not None:
        sd["ignoreDuration"] = int(ignoreDuration)
    return update_issue(
        organizationSlug=organizationSlug, issueId=issueId,
        status="ignored", statusDetails=sd or None,
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@mcp.tool(name="list_issue_events")
def list_issue_events(organizationSlug: str, issueId: str,
                      cursor: str = "",
                      limit: int = 100) -> list:
    """GET /api/0/organizations/{organization_slug}/issues/{issue_id}/events/
    Returns the sampled list of events grouped under the issue."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "list_issue_events", id=issueId,
                    result="not_found")
            _save_state(s)
            return []
        eids = list(s["issue_events"].get(str(issueId), []))
        events = [s["events"][e] for e in eids if e in s["events"]]
        events.sort(key=lambda e: e.get("dateCreated") or "",
                    reverse=True)
        page, env = _paginate(events, cursor or None, limit)
        _record(s, "list_issue_events", id=issueId, count=len(page))
        _save_state(s)
        return [_public_event(s, e) for e in page]


@mcp.tool(name="get_latest_event_for_issue")
def get_latest_event_for_issue(organizationSlug: str,
                               issueId: str) -> dict:
    """GET /api/0/organizations/{organization_slug}/issues/{issue_id}/events/latest/
    Returns the most recent event for the issue (by `dateCreated`)."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "get_latest_event_for_issue", id=issueId,
                    result="not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        eids = list(s["issue_events"].get(str(issueId), []))
        events = [s["events"][e] for e in eids if e in s["events"]]
        if not events:
            _record(s, "get_latest_event_for_issue", id=issueId,
                    result="no_events")
            _save_state(s)
            return _not_found(f"events for issue {issueId}")
        events.sort(key=lambda e: e.get("dateCreated") or "",
                    reverse=True)
        _record(s, "get_latest_event_for_issue", id=issueId,
                event_id=events[0]["id"])
        _save_state(s)
        return _public_event(s, events[0])


@mcp.tool(name="get_event")
def get_event(organizationSlug: str, projectSlug: str,
              eventId: str) -> dict:
    """GET /api/0/projects/{organization_slug}/{project_slug}/events/{event_id}/
    Retrieve a single event by its 32-hex id."""
    with _lock():
        s = _load_state()
        e = s["events"].get(eventId)
        if not e or e.get("orgSlug") != organizationSlug \
                or e.get("projectSlug") != projectSlug:
            _record(s, "get_event", id=eventId, result="not_found")
            _save_state(s)
            return _not_found(f"event {eventId}")
        _record(s, "get_event", id=eventId, result="ok")
        _save_state(s)
        return _public_event(s, e)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="list_issue_comments")
def list_issue_comments(organizationSlug: str, issueId: str) -> list:
    """GET /api/0/organizations/{organization_slug}/issues/{issue_id}/comments/
    List notes (comments) attached to an issue, oldest first."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "list_issue_comments", id=issueId,
                    result="not_found")
            _save_state(s)
            return []
        items = list(s["comments"].get(str(issueId), []))
        _record(s, "list_issue_comments", id=issueId, count=len(items))
        _save_state(s)
        return list(items)


@mcp.tool(name="create_issue_comment")
def create_issue_comment(organizationSlug: str, issueId: str,
                         text: str) -> dict:
    """POST /api/0/organizations/{organization_slug}/issues/{issue_id}/comments/
    Add a comment (note) to an issue. Body field is `data.text` in the
    real API; this mock accepts `text` directly."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "create_issue_comment", id=issueId,
                    result="not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        if not text:
            _record(s, "create_issue_comment", id=issueId,
                    result="missing_text")
            _save_state(s)
            return _invalid("data.text is required")
        cid = _next_id(s, "comment")
        now = _now_iso()
        viewer = s.get("viewer") or {"username": "mockbot",
                                       "name": "Mock Bot",
                                       "id": "0"}
        comment = {
            "id": cid,
            "user": {"id": viewer.get("id"),
                     "name": viewer.get("name"),
                     "username": viewer.get("username"),
                     "email": viewer.get("email")},
            "data": {"text": text},
            "type": "note",
            "dateCreated": now,
        }
        s["comments"].setdefault(str(issueId), []).append(comment)
        _record(s, "create_issue_comment", id=issueId, comment_id=cid)
        _save_state(s)
        return comment


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

@mcp.tool(name="list_releases")
def list_releases(organizationSlug: str,
                  query: str = "",
                  project: list | None = None) -> list:
    """GET /api/0/organizations/{organization_slug}/releases/
    Filter by `query` (substring of version) and `project` (list of
    project slugs)."""
    with _lock():
        s = _load_state()
        items = [r for k, r in s["releases"].items()
                 if k.startswith(f"{organizationSlug}::")]
        if query:
            q = query.lower()
            items = [r for r in items
                     if q in (r.get("version") or "").lower()]
        if project:
            wanted = set(project)
            items = [r for r in items
                     if wanted & set(r.get("projectSlugs", []))]
        items.sort(key=lambda r: r.get("dateCreated") or "",
                   reverse=True)
        _record(s, "list_releases",
                organization_slug=organizationSlug, count=len(items))
        _save_state(s)
        return [_public_release(s, r) for r in items]


@mcp.tool(name="get_release")
def get_release(organizationSlug: str, version: str) -> dict:
    """GET /api/0/organizations/{organization_slug}/releases/{version}/
    Retrieve a single release by version."""
    with _lock():
        s = _load_state()
        r = s["releases"].get(f"{organizationSlug}::{version}")
        _record(s, "get_release", organization_slug=organizationSlug,
                version=version, result="ok" if r else "not_found")
        _save_state(s)
        if not r:
            return _not_found(f"release {version}")
        return _public_release(s, r)


@mcp.tool(name="create_release")
def create_release(organizationSlug: str, version: str,
                   projects: list,
                   ref: str | None = None,
                   url: str | None = None,
                   dateReleased: str | None = None,
                   commits: list | None = None) -> dict:
    """POST /api/0/organizations/{organization_slug}/releases/
    `projects` is the list of project slugs the release belongs to.
    `commits` (optional) is recorded but not parsed."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "create_release", result="org_not_found")
            _save_state(s)
            return _not_found(f"organization {organizationSlug}")
        if not version:
            _record(s, "create_release", result="missing_version")
            _save_state(s)
            return _invalid("version is required")
        key = f"{organizationSlug}::{version}"
        if key in s["releases"]:
            _record(s, "create_release", result="already_exists",
                    version=version)
            _save_state(s)
            return _public_release(s, s["releases"][key])
        # Validate project slugs exist under the org
        proj_slugs = []
        for ps in projects or []:
            if f"{organizationSlug}::{ps}" in s["projects"]:
                proj_slugs.append(ps)
        now = _now_iso()
        rec = {
            "version": version,
            "shortVersion": version[:12],
            "ref": ref,
            "url": url,
            "dateReleased": dateReleased,
            "dateCreated": now,
            "data": {},
            "newGroups": 0,
            "owner": None,
            "commitCount": len(commits or []),
            "lastCommit": (commits or [None])[-1]
            if commits else None,
            "deployCount": 0,
            "lastDeploy": None,
            "authors": [],
            "projectSlugs": proj_slugs,
            "firstEvent": None,
            "lastEvent": None,
            "orgSlug": organizationSlug,
        }
        s["releases"][key] = rec
        # Update each project's latestRelease
        for ps in proj_slugs:
            p = s["projects"].get(f"{organizationSlug}::{ps}")
            if p is not None:
                p["latestRelease"] = _short_release(rec)
        _record(s, "create_release",
                organization_slug=organizationSlug, version=version,
                projects=proj_slugs)
        _save_state(s)
        return _public_release(s, rec)


# ---------------------------------------------------------------------------
# Environments + alerts
# ---------------------------------------------------------------------------

@mcp.tool(name="list_environments")
def list_environments(organizationSlug: str, projectSlug: str) -> list:
    """GET /api/0/projects/{organization_slug}/{project_slug}/environments/
    Returns environment objects (`name`, `isHidden`)."""
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "list_environments", result="project_not_found")
            _save_state(s)
            return []
        items = list(
            s["environments"].get(
                f"{organizationSlug}::{projectSlug}", []))
        out = [{"name": name, "isHidden": False} for name in items]
        _record(s, "list_environments",
                organization_slug=organizationSlug,
                project_slug=projectSlug, count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="list_project_alerts")
def list_project_alerts(organizationSlug: str, projectSlug: str) -> list:
    """GET /api/0/projects/{organization_slug}/{project_slug}/alert-rules/
    List metric alert rules attached to the project."""
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "list_project_alerts", result="project_not_found")
            _save_state(s)
            return []
        items = [r for k, r in s["alert_rules"].items()
                 if k.startswith(f"{organizationSlug}::{projectSlug}::")]
        items.sort(key=lambda r: r.get("dateCreated") or "")
        _record(s, "list_project_alerts",
                organization_slug=organizationSlug,
                project_slug=projectSlug, count=len(items))
        _save_state(s)
        return [_public_alert(r) for r in items]


@mcp.tool(name="create_project_alert")
def create_project_alert(organizationSlug: str, projectSlug: str,
                         name: str,
                         aggregate: str = "count()",
                         dataset: str = "events",
                         query: str = "",
                         timeWindow: int = 60,
                         triggers: list | None = None,
                         projects: list | None = None) -> dict:
    """POST /api/0/projects/{organization_slug}/{project_slug}/alert-rules/

    `triggers` is the list of warning/critical threshold objects; the
    mock stores them verbatim. `dataset` is typically `events`,
    `transactions`, or `sessions`.
    """
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "create_project_alert", result="project_not_found")
            _save_state(s)
            return _not_found(f"project {organizationSlug}/{projectSlug}")
        if not name:
            _record(s, "create_project_alert", result="missing_name")
            _save_state(s)
            return _invalid("name is required")
        rid = _next_id(s, "alert_rule")
        now = _now_iso()
        rule = {
            "id": rid,
            "name": name,
            "aggregate": aggregate,
            "dataset": dataset,
            "query": query,
            "timeWindow": int(timeWindow),
            "triggers": list(triggers or []),
            "projects": list(projects or [projectSlug]),
            "dateCreated": now,
            "orgSlug": organizationSlug,
            "projectSlug": projectSlug,
            "status": "active",
        }
        s["alert_rules"][f"{organizationSlug}::{projectSlug}::{rid}"] = rule
        _record(s, "create_project_alert", id=rid, name=name,
                organization_slug=organizationSlug,
                project_slug=projectSlug)
        _save_state(s)
        return _public_alert(rule)


@mcp.tool(name="create_issue")
def create_issue(organizationSlug: str, projectSlug: str, title: str,
                 culprit: str | None = None,
                 level: str = "error",
                 status: str = "unresolved",
                 platform: str = "python",
                 count: int = 1,
                 userCount: int = 1,
                 tags: list | None = None,
                 metadata: dict | None = None,
                 assignedTo: Any = None,
                 environments: list | None = None) -> dict:
    """Create (file) a new Issue under the given org/project — e.g. to
    record a detected breach/alert. `title` is the issue headline.
    Returns the new Issue."""
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "create_issue", result="project_not_found")
            _save_state(s)
            return _not_found(f"project {organizationSlug}/{projectSlug}")
        if not title:
            _record(s, "create_issue", result="missing_title")
            _save_state(s)
            return _invalid("title is required")
        iid = _next_id(s, "issue")
        short = _next_short_id(s, organizationSlug, projectSlug)
        now = _now_iso()
        iss = {
            "id": iid,
            "shareId": _gen_share_id(),
            "shortId": short,
            "title": title,
            "culprit": culprit or "",
            "logger": None,
            "level": level if level in VALID_LEVELS else "error",
            "status": status if status in VALID_STATUSES else "unresolved",
            "statusDetails": {},
            "isPublic": False,
            "platform": platform,
            "type": "error",
            "metadata": metadata or {"type": title.split(":")[0]
                                      if ":" in title else "Error",
                                      "value": title},
            "assignedTo": assignedTo,
            "isBookmarked": False,
            "isSubscribed": True,
            "hasSeen": False,
            "annotations": [],
            "isUnhandled": True,
            "count": int(count),
            "userCount": int(userCount),
            "firstSeen": now,
            "lastSeen": now,
            "stats": {"24h": []},
            "tags": list(tags or []),
            "environments": list(environments or []),
            "orgSlug": organizationSlug,
            "projectSlug": projectSlug,
            "firstReleaseVersion": None,
            "lastReleaseVersion": None,
        }
        s["issues"][iid] = iss
        s["issue_events"].setdefault(iid, [])
        s["comments"].setdefault(iid, [])
        ekey = f"{organizationSlug}::{projectSlug}"
        for e in environments or []:
            if e and e not in s["environments"].setdefault(ekey, []):
                s["environments"][ekey].append(e)
        _record(s, "create_issue", id=iid, short_id=short,
                organization_slug=organizationSlug, project_slug=projectSlug)
        _save_state(s)
        return _public_issue(s, iss)


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_organization")
def mock_debug_seed_organization(slug: str, name: str | None = None,
                                 features: list | None = None) -> dict:
    """Mock-only: insert (or upsert) an organization."""
    with _lock():
        s = _load_state()
        existing = s["organizations"].get(slug, {})
        org = {
            "id": existing.get("id") or _next_id(s, "organization"),
            "slug": slug,
            "name": name or existing.get("name") or slug,
            "status": {"id": "active", "name": "active"},
            "dateCreated": existing.get("dateCreated") or _now_iso(),
            "isEarlyAdopter": existing.get("isEarlyAdopter", False),
            "require2FA": existing.get("require2FA", False),
            "avatar": existing.get("avatar",
                                    {"avatarType": "letter_avatar",
                                     "avatarUuid": None}),
            "features": list(features or existing.get("features", [])),
        }
        s["organizations"][slug] = org
        _record(s, "debug_seed_organization", slug=slug)
        _save_state(s)
        return _public_org(org)


@mcp.tool(name="mock_debug_seed_team")
def mock_debug_seed_team(organizationSlug: str, slug: str,
                         name: str | None = None) -> dict:
    """Mock-only: insert (or upsert) a team under an organization."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "debug_seed_team", result="org_not_found")
            _save_state(s)
            return _not_found(f"organization {organizationSlug}")
        key = f"{organizationSlug}::{slug}"
        existing = s["teams"].get(key, {})
        team = {
            "id": existing.get("id") or _next_id(s, "team"),
            "slug": slug,
            "name": name or existing.get("name") or slug,
            "dateCreated": existing.get("dateCreated") or _now_iso(),
            "isMember": True,
            "hasAccess": True,
            "isPending": False,
            "memberCount": existing.get("memberCount", 0),
            "avatar": {"avatarType": "letter_avatar", "avatarUuid": None},
            "orgSlug": organizationSlug,
        }
        s["teams"][key] = team
        _record(s, "debug_seed_team",
                organization_slug=organizationSlug, slug=slug)
        _save_state(s)
        return _public_team(s, team)


@mcp.tool(name="mock_debug_seed_project")
def mock_debug_seed_project(organizationSlug: str, slug: str,
                            name: str | None = None,
                            platform: str = "python",
                            teamSlug: str = "engineering") -> dict:
    """Mock-only: insert (or upsert) a project. Creates the team if
    missing."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "debug_seed_project", result="org_not_found")
            _save_state(s)
            return _not_found(f"organization {organizationSlug}")
        # ensure team
        tkey = f"{organizationSlug}::{teamSlug}"
        if tkey not in s["teams"]:
            s["teams"][tkey] = {
                "id": _next_id(s, "team"), "slug": teamSlug,
                "name": teamSlug.capitalize(), "dateCreated": _now_iso(),
                "isMember": True, "hasAccess": True, "isPending": False,
                "memberCount": 0,
                "avatar": {"avatarType": "letter_avatar",
                            "avatarUuid": None},
                "orgSlug": organizationSlug,
            }
        key = f"{organizationSlug}::{slug}"
        existing = s["projects"].get(key, {})
        project = _new_project_record(
            pid=existing.get("id") or _next_id(s, "project"),
            slug=slug, name=name or existing.get("name") or slug,
            platform=platform,
            org_slug=organizationSlug, team_slugs=[teamSlug],
            date_created=existing.get("dateCreated") or _now_iso(),
        )
        s["projects"][key] = project
        s["next_id"]["short_id"].setdefault(key, 1)
        s["environments"].setdefault(key, [])
        _record(s, "debug_seed_project",
                organization_slug=organizationSlug, slug=slug)
        _save_state(s)
        return _public_project(s, project)


@mcp.tool(name="mock_debug_seed_issue")
def mock_debug_seed_issue(organizationSlug: str, projectSlug: str,
                          title: str,
                          culprit: str | None = None,
                          level: str = "error",
                          status: str = "unresolved",
                          platform: str = "python",
                          count: int = 1,
                          userCount: int = 1,
                          firstSeen: str | None = None,
                          lastSeen: str | None = None,
                          tags: list | None = None,
                          metadata: dict | None = None,
                          assignedTo: Any = None,
                          environments: list | None = None) -> dict:
    """Mock-only: insert an Issue under the given org/project. Returns
    the new Issue."""
    with _lock():
        s = _load_state()
        if f"{organizationSlug}::{projectSlug}" not in s["projects"]:
            _record(s, "debug_seed_issue", result="project_not_found")
            _save_state(s)
            return _not_found(f"project {organizationSlug}/{projectSlug}")
        iid = _next_id(s, "issue")
        short = _next_short_id(s, organizationSlug, projectSlug)
        now = _now_iso()
        iss = {
            "id": iid,
            "shareId": _gen_share_id(),
            "shortId": short,
            "title": title,
            "culprit": culprit or "",
            "logger": None,
            "level": level if level in VALID_LEVELS else "error",
            "status": status if status in VALID_STATUSES else "unresolved",
            "statusDetails": {},
            "isPublic": False,
            "platform": platform,
            "type": "error",
            "metadata": metadata or {"type": title.split(":")[0]
                                      if ":" in title else "Error",
                                      "value": title},
            "assignedTo": assignedTo,
            "isBookmarked": False,
            "isSubscribed": True,
            "hasSeen": False,
            "annotations": [],
            "isUnhandled": True,
            "count": int(count),
            "userCount": int(userCount),
            "firstSeen": firstSeen or now,
            "lastSeen": lastSeen or now,
            "stats": {"24h": []},
            "tags": list(tags or []),
            "environments": list(environments or []),
            "orgSlug": organizationSlug,
            "projectSlug": projectSlug,
            "firstReleaseVersion": None,
            "lastReleaseVersion": None,
        }
        s["issues"][iid] = iss
        s["issue_events"].setdefault(iid, [])
        s["comments"].setdefault(iid, [])
        # record envs on the project
        ekey = f"{organizationSlug}::{projectSlug}"
        for e in environments or []:
            if e and e not in s["environments"].setdefault(ekey, []):
                s["environments"][ekey].append(e)
        _record(s, "debug_seed_issue", id=iid, short_id=short)
        _save_state(s)
        return _public_issue(s, iss)


@mcp.tool(name="mock_debug_seed_event")
def mock_debug_seed_event(organizationSlug: str, projectSlug: str,
                          issueId: str,
                          message: str,
                          level: str = "error",
                          platform: str = "python",
                          exceptionType: str | None = None,
                          exceptionValue: str | None = None,
                          environment: str | None = None,
                          release: str | None = None,
                          tags: list | None = None) -> dict:
    """Mock-only: insert an Event under an existing Issue."""
    with _lock():
        s = _load_state()
        iss = s["issues"].get(str(issueId))
        if not iss or iss.get("orgSlug") != organizationSlug:
            _record(s, "debug_seed_event", result="issue_not_found")
            _save_state(s)
            return _not_found(f"issue {issueId}")
        eid = _gen_event_id()
        now = _now_iso()
        entries = []
        if exceptionType or exceptionValue:
            entries.append({
                "type": "exception",
                "data": {
                    "values": [{
                        "type": exceptionType or "Exception",
                        "value": exceptionValue or message,
                        "stacktrace": {"frames": []},
                    }],
                },
            })
        ev = {
            "id": eid,
            "issueId": str(issueId),
            "orgSlug": organizationSlug,
            "projectSlug": projectSlug,
            "message": message,
            "title": message,
            "level": level if level in VALID_LEVELS else "error",
            "platform": platform,
            "dateCreated": now,
            "dateReceived": now,
            "fingerprints": ["{{ default }}"],
            "user": None,
            "entries": entries,
            "tags": list(tags or []),
            "contexts": {},
            "release": release,
            "environment": environment,
            "sdk": {"name": f"sentry.{platform}", "version": "1.0.0"},
        }
        s["events"][eid] = ev
        s["issue_events"].setdefault(str(issueId), []).append(eid)
        # touch issue lastSeen
        iss["lastSeen"] = now
        iss["count"] = int(iss.get("count", 0) or 0) + 1
        if environment:
            envs = iss.setdefault("environments", [])
            if environment not in envs:
                envs.append(environment)
            ekey = f"{organizationSlug}::{projectSlug}"
            if environment not in s["environments"].setdefault(ekey, []):
                s["environments"][ekey].append(environment)
        _record(s, "debug_seed_event", id=eid, issue_id=issueId)
        _save_state(s)
        return _public_event(s, ev)


@mcp.tool(name="mock_debug_seed_release")
def mock_debug_seed_release(organizationSlug: str, version: str,
                            projects: list | None = None,
                            dateReleased: str | None = None,
                            newGroups: int = 0) -> dict:
    """Mock-only: insert a Release."""
    with _lock():
        s = _load_state()
        if organizationSlug not in s["organizations"]:
            _record(s, "debug_seed_release", result="org_not_found")
            _save_state(s)
            return _not_found(f"organization {organizationSlug}")
        key = f"{organizationSlug}::{version}"
        proj_slugs = []
        for ps in projects or []:
            if f"{organizationSlug}::{ps}" in s["projects"]:
                proj_slugs.append(ps)
        now = _now_iso()
        rec = {
            "version": version,
            "shortVersion": version[:12],
            "ref": None, "url": None,
            "dateReleased": dateReleased,
            "dateCreated": now,
            "data": {},
            "newGroups": int(newGroups),
            "owner": None,
            "commitCount": 0,
            "lastCommit": None,
            "deployCount": 0,
            "lastDeploy": None,
            "authors": [],
            "projectSlugs": proj_slugs,
            "firstEvent": None,
            "lastEvent": None,
            "orgSlug": organizationSlug,
        }
        s["releases"][key] = rec
        for ps in proj_slugs:
            p = s["projects"].get(f"{organizationSlug}::{ps}")
            if p is not None:
                p["latestRelease"] = _short_release(rec)
        _record(s, "debug_seed_release", version=version)
        _save_state(s)
        return _public_release(s, rec)


if __name__ == "__main__":
    mcp.run()
