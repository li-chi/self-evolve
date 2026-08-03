"""Jira Cloud mock MCP server.

Mirrors the tool surface of Atlassian Jira Cloud's REST API v3
(developer.atlassian.com/cloud/jira/platform/rest/v3/intro). Tool
names use the operationId-style names from the Jira REST v3 spec
(e.g. `getIssue`, `searchForIssuesUsingJql`, `doTransition`) so an
agent trained on the real API sees the same tool surface.

Responses follow Jira's REST JSON shapes — issues are
`{"id","key","self","fields":{...}}`, paginated results are
`{"startAt","maxResults","total","isLast","values":[...]}` (or
`{"issues":[...]}` for issue search). Errors are returned as Jira
error objects (`{"errorMessages":[...],"errors":{...},"status":404}`)
rather than raised, so the call trace looks like a real failed HTTP
response.

ADF (Atlassian Document Format): tools that take `description` /
`body` accept *either* a plain string (auto-wrapped into a minimal
ADF doc) or a full ADF document dict. The stored representation is
always ADF; the returned representation preserves whatever shape
was supplied at creation time.

State plumbing matches the slack-mock pattern: a single JSON state
file at `$JIRA_MOCK_STATE_DIR/state.json`, `fcntl.flock`-guarded,
optionally seeded from `$JIRA_MOCK_SEED_PATH`, and a `state["calls"]`
log appended on every tool call for verifier replay.

Tools implemented (real-API operationIds where they exist):

  Issue          getIssue, createIssue, editIssue, deleteIssue,
                 searchForIssuesUsingJql, getTransitions,
                 doTransition, assignIssue
  Comment        getComments, addComment
  Project        getProject, getAllProjects
  User           findUsers, getUser
  Meta           getIssueAllTypes, getStatuses, getPriorities

Plus mock-only helpers `mock_debug_state` and `mock_debug_seed`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP


JIRA_BASE_URL_DEFAULT = "https://mock.atlassian.net"
SELF_ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "JIRA_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/jira_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    """Jira format: 2024-01-15T10:30:00.000+0000"""
    now = datetime.datetime.now(datetime.timezone.utc)
    base = now.strftime("%Y-%m-%dT%H:%M:%S")
    ms = f"{now.microsecond // 1000:03d}"
    return f"{base}.{ms}+0000"


def _empty_state() -> dict:
    return {
        "base_url": JIRA_BASE_URL_DEFAULT,
        "self": {
            "accountId": SELF_ACCOUNT_ID,
            "accountType": "atlassian",
            "displayName": "Mock Bot",
            "emailAddress": "mockbot@example.com",
            "active": True,
        },
        # Catalog (typically seeded once, mutated only by debug_seed):
        "projects": {},        # key -> project object
        "users": {},           # accountId -> user object
        "issue_types": {},     # id -> issue type
        "statuses": {},        # id -> status
        "priorities": {},      # id -> priority
        "workflow": {},        # status_name -> [{"id","name","to"}]
        # Mutable runtime:
        "issues": {},          # key -> issue object (with fields)
        "comments": {},        # issue_key -> [comment dicts]
        "next_id": {
            "issue": 10000,
            "comment": 10000,
            "project": 10000,
            "user": 1,
            "issue_type": 10000,
            "status": 10000,
            "priority": 10000,
            "transition": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("JIRA_MOCK_SEED_PATH")
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
# Error shapes (Jira REST v3)
# ---------------------------------------------------------------------------

def _err(status: int, message: str,
         errors: dict | None = None) -> dict:
    """Return a Jira-shaped error object.

    Real Jira REST v3 errors look like:
        {"errorMessages":["Issue does not exist"],"errors":{},"status":404}
    Some endpoints also return per-field errors in `errors`.
    """
    return {
        "errorMessages": [message] if message else [],
        "errors": errors or {},
        "status": status,
    }


# ---------------------------------------------------------------------------
# ID + URL helpers
# ---------------------------------------------------------------------------

def _base_url(state: dict) -> str:
    return state.get("base_url", JIRA_BASE_URL_DEFAULT).rstrip("/")


def _new_issue_id(state: dict) -> str:
    n = state["next_id"]["issue"]
    state["next_id"]["issue"] = n + 1
    return str(n)


def _new_comment_id(state: dict) -> str:
    n = state["next_id"]["comment"]
    state["next_id"]["comment"] = n + 1
    return str(n)


def _new_project_id(state: dict) -> str:
    n = state["next_id"]["project"]
    state["next_id"]["project"] = n + 1
    return str(n)


def _new_account_id(state: dict) -> str:
    n = state["next_id"]["user"]
    state["next_id"]["user"] = n + 1
    return f"5b10ac8d82e05b22cc7d4{n:04d}"


def _new_issue_type_id(state: dict) -> str:
    n = state["next_id"]["issue_type"]
    state["next_id"]["issue_type"] = n + 1
    return str(n)


def _new_status_id(state: dict) -> str:
    n = state["next_id"]["status"]
    state["next_id"]["status"] = n + 1
    return str(n)


def _new_priority_id(state: dict) -> str:
    n = state["next_id"]["priority"]
    state["next_id"]["priority"] = n + 1
    return str(n)


# ---------------------------------------------------------------------------
# ADF (Atlassian Document Format)
# ---------------------------------------------------------------------------

def _to_adf(value: Any) -> dict | None:
    """Accept either a plain string or an ADF doc; return ADF (or None
    if value is None/empty)."""
    if value is None:
        return None
    if isinstance(value, dict):
        # Assume already ADF (or close enough).
        return value
    if isinstance(value, str):
        if not value:
            return None
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": value}]}
            ],
        }
    return None


def _adf_to_plain(adf: dict | None) -> str:
    """Best-effort plain-text extraction from ADF, for filtering."""
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                out.append(node["text"])
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for c in node:
                walk(c)
    walk(adf)
    return "".join(out)


# ---------------------------------------------------------------------------
# Object shaping
# ---------------------------------------------------------------------------

def _user_obj(state: dict, account_id: str) -> dict | None:
    u = state["users"].get(account_id)
    if not u:
        return None
    return {
        "self": f"{_base_url(state)}/rest/api/3/user?accountId={account_id}",
        "accountId": u["accountId"],
        "accountType": u.get("accountType", "atlassian"),
        "emailAddress": u.get("emailAddress"),
        "displayName": u.get("displayName", ""),
        "active": u.get("active", True),
        "timeZone": u.get("timeZone", "UTC"),
        "avatarUrls": u.get("avatarUrls", {}),
    }


def _project_obj(state: dict, project_key: str,
                 include_full: bool = True) -> dict | None:
    p = state["projects"].get(project_key)
    if not p:
        return None
    base = _base_url(state)
    out = {
        "self": f"{base}/rest/api/3/project/{p['id']}",
        "id": p["id"],
        "key": p["key"],
        "name": p.get("name", p["key"]),
        "projectTypeKey": p.get("projectTypeKey", "software"),
        "simplified": p.get("simplified", True),
        "style": p.get("style", "next-gen"),
        "isPrivate": p.get("isPrivate", False),
        "avatarUrls": p.get("avatarUrls", {}),
    }
    if include_full:
        lead = p.get("leadAccountId")
        if lead and lead in state["users"]:
            out["lead"] = _user_obj(state, lead)
        out["description"] = p.get("description", "")
        out["issueTypes"] = [_issue_type_obj(state, it)
                             for it in p.get("issueTypeIds", [])
                             if it in state["issue_types"]]
        out["url"] = f"{base}/projects/{p['key']}"
        out["projectCategory"] = p.get("projectCategory")
    return out


def _issue_type_obj(state: dict, type_id: str) -> dict | None:
    it = state["issue_types"].get(type_id)
    if not it:
        return None
    return {
        "self": f"{_base_url(state)}/rest/api/3/issuetype/{type_id}",
        "id": type_id,
        "name": it.get("name", ""),
        "description": it.get("description", ""),
        "iconUrl": it.get("iconUrl", ""),
        "subtask": it.get("subtask", False),
        "hierarchyLevel": it.get("hierarchyLevel", 0),
    }


def _status_obj(state: dict, status_id: str) -> dict | None:
    st = state["statuses"].get(status_id)
    if not st:
        return None
    cat = st.get("statusCategory", {})
    return {
        "self": f"{_base_url(state)}/rest/api/3/status/{status_id}",
        "id": status_id,
        "name": st.get("name", ""),
        "description": st.get("description", ""),
        "iconUrl": st.get("iconUrl", ""),
        "statusCategory": {
            "self": f"{_base_url(state)}/rest/api/3/statuscategory/"
                    f"{cat.get('id', 2)}",
            "id": cat.get("id", 2),
            "key": cat.get("key", "new"),
            "colorName": cat.get("colorName", "blue-gray"),
            "name": cat.get("name", "To Do"),
        },
    }


def _priority_obj(state: dict, priority_id: str) -> dict | None:
    pr = state["priorities"].get(priority_id)
    if not pr:
        return None
    return {
        "self": f"{_base_url(state)}/rest/api/3/priority/{priority_id}",
        "id": priority_id,
        "name": pr.get("name", ""),
        "description": pr.get("description", ""),
        "iconUrl": pr.get("iconUrl", ""),
        "statusColor": pr.get("statusColor", "#999999"),
    }


def _status_id_by_name(state: dict, name: str) -> str | None:
    for sid, st in state["statuses"].items():
        if (st.get("name") or "").lower() == name.lower():
            return sid
    return None


def _priority_id_by_name(state: dict, name: str) -> str | None:
    for pid, pr in state["priorities"].items():
        if (pr.get("name") or "").lower() == name.lower():
            return pid
    return None


def _issue_type_id_by_name(state: dict, name: str) -> str | None:
    for tid, it in state["issue_types"].items():
        if (it.get("name") or "").lower() == name.lower():
            return tid
    return None


def _issue_obj(state: dict, key: str,
               fields_filter: list[str] | None = None) -> dict | None:
    """Render a stored issue into a Jira REST v3 issue object."""
    iss = state["issues"].get(key)
    if not iss:
        return None
    stored = iss.get("fields", {})
    project_key = iss.get("project")
    issuetype_id = stored.get("issuetype_id")
    status_id = stored.get("status_id")
    priority_id = stored.get("priority_id")
    assignee_id = stored.get("assignee_id")
    reporter_id = stored.get("reporter_id")

    fields: dict[str, Any] = {
        "summary": stored.get("summary", ""),
        "description": stored.get("description"),
        "project": (_project_obj(state, project_key, include_full=False)
                    if project_key else None),
        "issuetype": (_issue_type_obj(state, issuetype_id)
                      if issuetype_id else None),
        "status": (_status_obj(state, status_id) if status_id else None),
        "priority": (_priority_obj(state, priority_id)
                     if priority_id else None),
        "assignee": (_user_obj(state, assignee_id)
                     if assignee_id else None),
        "reporter": (_user_obj(state, reporter_id)
                     if reporter_id else None),
        "creator": (_user_obj(state, iss.get("creator_id"))
                    if iss.get("creator_id") else None),
        "labels": list(stored.get("labels", [])),
        "components": list(stored.get("components", [])),
        "fixVersions": list(stored.get("fixVersions", [])),
        "versions": list(stored.get("versions", [])),
        "created": iss.get("created"),
        "updated": iss.get("updated"),
        "duedate": stored.get("duedate"),
        "resolution": stored.get("resolution"),
        "resolutiondate": stored.get("resolutiondate"),
        "parent": stored.get("parent"),
        "subtasks": stored.get("subtasks", []),
        "issuelinks": stored.get("issuelinks", []),
        "watches": {
            "self": (f"{_base_url(state)}/rest/api/3/issue/{key}/watchers"),
            "watchCount": stored.get("watchCount", 0),
            "isWatching": False,
        },
        "votes": {
            "self": f"{_base_url(state)}/rest/api/3/issue/{key}/votes",
            "votes": stored.get("votes", 0),
            "hasVoted": False,
        },
    }
    # Merge any pass-through custom fields (`customfield_*`).
    for k, v in stored.items():
        if k.startswith("customfield_"):
            fields[k] = v

    if fields_filter:
        wanted = set(fields_filter)
        # Jira accepts "*all", "*navigable", "-fieldname" etc — we
        # implement only positive names and "*all".
        if "*all" not in wanted:
            fields = {k: v for k, v in fields.items() if k in wanted}

    return {
        "expand": "renderedFields,names,schema,operations,editmeta,changelog",
        "id": iss["id"],
        "self": f"{_base_url(state)}/rest/api/3/issue/{iss['id']}",
        "key": key,
        "fields": fields,
    }


def _comment_obj(state: dict, c: dict) -> dict:
    """Render a stored comment dict into the REST v3 comment shape."""
    issue_key = c.get("issue_key", "")
    return {
        "self": (f"{_base_url(state)}/rest/api/3/issue/{issue_key}/comment/"
                 f"{c['id']}"),
        "id": c["id"],
        "author": (_user_obj(state, c["author_id"])
                   if c.get("author_id") else None),
        "body": c.get("body"),
        "updateAuthor": (_user_obj(state, c.get("update_author_id"))
                         if c.get("update_author_id") else None),
        "created": c.get("created"),
        "updated": c.get("updated"),
        "visibility": c.get("visibility"),
        "jsdPublic": c.get("jsdPublic", True),
    }


# ---------------------------------------------------------------------------
# JQL parser (subset)
# ---------------------------------------------------------------------------

# Real Jira JQL is rich; we handle the common shapes that show up in
# real Jira workflows + an `ORDER BY` suffix. Supported:
#   project = "KEY"   project = KEY    project in (A, B)
#   status = "Name"   status != "Name" status in (a, b)
#   priority = ...    assignee = "5b10..."   assignee = currentUser()
#   reporter = ...    issuetype = "Bug"      labels = "foo"
#   text ~ "needle"   summary ~ "..."        description ~ "..."
#   key = "PROJ-1"
# Joined by AND only (OR not implemented). ORDER BY <field> ASC|DESC.

_JQL_TOKEN_RE = re.compile(
    r'\s*('
    r'"(?:[^"\\]|\\.)*"|'      # double-quoted
    r"'(?:[^'\\]|\\.)*'|"      # single-quoted
    r'\([^)]*\)|'              # paren group
    r'[!<>=~]+|'               # operators
    r'\S+'                     # bare word
    r')'
)


def _jql_tokens(s: str) -> list[str]:
    return [m.group(1) for m in _JQL_TOKEN_RE.finditer(s)]


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
        return tok[1:-1]
    return tok


def _parse_list(tok: str) -> list[str]:
    inner = tok.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    parts = [p.strip() for p in inner.split(",")]
    return [_unquote(p) for p in parts if p]


def _parse_jql(jql: str) -> tuple[list[tuple[str, str, Any]],
                                  tuple[str, str] | None] | None:
    """Returns ([(field, op, value), ...], (order_field, dir) | None)
    or None if parsing fails. Supports the documented subset."""
    s = (jql or "").strip()
    if not s:
        return [], None
    order = None
    m = re.search(r'\s+order\s+by\s+(.+)$', s, re.IGNORECASE)
    if m:
        ob = m.group(1).strip()
        s = s[: m.start()].strip()
        # take just the first sort key
        first = ob.split(",", 1)[0].strip()
        parts = first.split()
        ob_field = parts[0].strip('"')
        ob_dir = "asc"
        if len(parts) > 1 and parts[1].lower() in ("asc", "desc"):
            ob_dir = parts[1].lower()
        order = (ob_field, ob_dir)
    clauses_text = re.split(r"\s+and\s+", s, flags=re.IGNORECASE)
    out: list[tuple[str, str, Any]] = []
    for c in clauses_text:
        if not c.strip():
            continue
        tokens = _jql_tokens(c)
        if len(tokens) < 3:
            return None
        field = tokens[0].lower().strip('"')
        # operator may be one of: =, !=, ~, !~, in, not in, is, is not
        op = tokens[1].lower()
        rest = tokens[2:]
        if op == "not" and rest and rest[0].lower() == "in":
            op = "not in"
            rest = rest[1:]
        if op == "is" and rest and rest[0].lower() == "not":
            op = "is not"
            rest = rest[1:]
        if op in ("in", "not in"):
            value = _parse_list(rest[0])
        else:
            value = _unquote(rest[0])
        out.append((field, op, value))
    return out, order


def _issue_field_for_jql(state: dict, key: str, field: str) -> Any:
    iss = state["issues"][key]
    f = iss.get("fields", {})
    if field in ("project",):
        return iss.get("project")
    if field == "key":
        return key
    if field == "status":
        sid = f.get("status_id")
        return state["statuses"].get(sid, {}).get("name") if sid else None
    if field == "priority":
        pid = f.get("priority_id")
        return state["priorities"].get(pid, {}).get("name") if pid else None
    if field in ("type", "issuetype"):
        tid = f.get("issuetype_id")
        return state["issue_types"].get(tid, {}).get("name") if tid else None
    if field == "assignee":
        return f.get("assignee_id")
    if field == "reporter":
        return f.get("reporter_id")
    if field in ("labels",):
        return list(f.get("labels", []))
    if field == "summary":
        return f.get("summary", "")
    if field == "description":
        return _adf_to_plain(f.get("description"))
    if field == "text":
        return " ".join([
            f.get("summary", "") or "",
            _adf_to_plain(f.get("description")),
        ])
    if field == "created":
        return iss.get("created", "")
    if field == "updated":
        return iss.get("updated", "")
    if field == "resolution":
        return f.get("resolution")
    return f.get(field)


def _jql_match(state: dict, key: str, clauses: list) -> bool:
    self_id = state["self"]["accountId"]
    for field, op, value in clauses:
        actual = _issue_field_for_jql(state, key, field)
        # currentUser() resolves to the bot
        if isinstance(value, str) and value.lower() in (
                "currentuser()", "currentuser ()"):
            value = self_id
        if op in ("=", "is"):
            if value == "empty" or value is None:
                if actual not in (None, "", []):
                    return False
            elif isinstance(actual, list):
                if value not in actual:
                    return False
            elif (actual or "") != value:
                return False
        elif op in ("!=", "is not"):
            if value == "empty":
                if actual in (None, "", []):
                    return False
            elif isinstance(actual, list):
                if value in actual:
                    return False
            elif (actual or "") == value:
                return False
        elif op == "~":
            text = (actual if isinstance(actual, str) else "") or ""
            if value.lower() not in text.lower():
                return False
        elif op == "!~":
            text = (actual if isinstance(actual, str) else "") or ""
            if value.lower() in text.lower():
                return False
        elif op == "in":
            wanted = [v.lower() if isinstance(v, str) else v for v in value]
            if isinstance(actual, list):
                low = [a.lower() if isinstance(a, str) else a for a in actual]
                if not any(w in low for w in wanted):
                    return False
            else:
                a = actual.lower() if isinstance(actual, str) else actual
                if a not in wanted:
                    return False
        elif op == "not in":
            wanted = [v.lower() if isinstance(v, str) else v for v in value]
            if isinstance(actual, list):
                low = [a.lower() if isinstance(a, str) else a for a in actual]
                if any(w in low for w in wanted):
                    return False
            else:
                a = actual.lower() if isinstance(actual, str) else actual
                if a in wanted:
                    return False
        else:
            # Unknown operator → fail closed.
            return False
    return True


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("jira-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Issue: getIssue
# ---------------------------------------------------------------------------

@mcp.tool(name="getIssue")
def get_issue(issueIdOrKey: str,
              fields: list[str] | None = None,
              expand: str | None = None,
              properties: list[str] | None = None) -> dict:
    """Jira REST: GET /rest/api/3/issue/{issueIdOrKey}

    Returns an issue object. `fields` is a list of field names to
    include (e.g. ["summary","status"]); omit or pass ["*all"] for
    everything. `expand` and `properties` are accepted but ignored
    (mock does not model rendered fields, changelogs, or entity
    properties).
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "getIssue", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, f"Issue does not exist or you do not have "
                             f"permission to see it.")
        obj = _issue_obj(s, key, fields_filter=fields)
        _record(s, "getIssue", issueIdOrKey=issueIdOrKey, key=key)
        _save_state(s)
        return obj


def _resolve_issue_id(state: dict, ref: str) -> str | None:
    """Accept either an issue key (PROJ-123) or numeric id."""
    if not ref:
        return None
    if ref in state["issues"]:
        return ref
    for key, iss in state["issues"].items():
        if iss.get("id") == str(ref):
            return key
    return None


# ---------------------------------------------------------------------------
# Issue: createIssue
# ---------------------------------------------------------------------------

@mcp.tool(name="createIssue")
def create_issue(fields: dict,
                 update: dict | None = None) -> dict:
    """Jira REST: POST /rest/api/3/issue

    Body shape: {"fields": {"project":{"key":"PROJ"},
                           "summary":"...",
                           "issuetype":{"name":"Bug"}, ...},
                 "update": {...}}
    Returns {"id","key","self"}. Required fields: project (key or id),
    summary, issuetype (name or id). Optional: description, priority,
    labels, assignee (accountId), reporter, duedate, parent, custom
    fields (`customfield_*`).
    """
    with _lock():
        s = _load_state()
        errors: dict[str, str] = {}
        # Resolve project
        proj_in = (fields or {}).get("project") or {}
        project_key = None
        if isinstance(proj_in, dict):
            if proj_in.get("key"):
                project_key = proj_in["key"]
            elif proj_in.get("id"):
                for k, p in s["projects"].items():
                    if p["id"] == str(proj_in["id"]):
                        project_key = k
                        break
        if not project_key or project_key not in s["projects"]:
            errors["project"] = "project is required."
        # Summary
        summary = fields.get("summary", "")
        if not summary or not isinstance(summary, str):
            errors["summary"] = "You must specify a summary of the issue."
        # Issue type
        it_in = fields.get("issuetype") or {}
        issuetype_id = None
        if isinstance(it_in, dict):
            if it_in.get("id") and str(it_in["id"]) in s["issue_types"]:
                issuetype_id = str(it_in["id"])
            elif it_in.get("name"):
                issuetype_id = _issue_type_id_by_name(s, it_in["name"])
        if not issuetype_id:
            errors["issuetype"] = "issue type is required"
        if errors:
            _record(s, "createIssue", result="validation_error",
                    errors=list(errors.keys()))
            _save_state(s)
            return _err(400, "", errors=errors)

        # Default status = first one in workflow (project-defined or
        # global). For real Jira this is the workflow's initial status.
        proj = s["projects"][project_key]
        initial_status_id = (proj.get("initialStatusId")
                             or next(iter(s["statuses"]), None))

        # Priority
        pr_in = fields.get("priority") or {}
        priority_id = None
        if isinstance(pr_in, dict):
            if pr_in.get("id") and str(pr_in["id"]) in s["priorities"]:
                priority_id = str(pr_in["id"])
            elif pr_in.get("name"):
                priority_id = _priority_id_by_name(s, pr_in["name"])

        # Assignee
        assignee_id = _extract_account_id(fields.get("assignee"))
        if assignee_id and assignee_id not in s["users"]:
            return _err(400, "", errors={
                "assignee": (f"User '{assignee_id}' does not exist.")})

        reporter_id = (_extract_account_id(fields.get("reporter"))
                       or s["self"]["accountId"])

        # Issue id + key
        issue_id = _new_issue_id(s)
        proj["nextSeq"] = proj.get("nextSeq", 1)
        seq = proj["nextSeq"]
        proj["nextSeq"] = seq + 1
        key = f"{project_key}-{seq}"
        now = _now_iso()

        stored_fields: dict[str, Any] = {
            "summary": summary,
            "description": _to_adf(fields.get("description")),
            "issuetype_id": issuetype_id,
            "status_id": initial_status_id,
            "priority_id": priority_id,
            "assignee_id": assignee_id,
            "reporter_id": reporter_id,
            "labels": list(fields.get("labels") or []),
            "components": list(fields.get("components") or []),
            "fixVersions": list(fields.get("fixVersions") or []),
            "versions": list(fields.get("versions") or []),
            "duedate": fields.get("duedate"),
            "parent": fields.get("parent"),
            "subtasks": [],
            "issuelinks": [],
            "votes": 0,
            "watchCount": 0,
        }
        # Pass through any customfield_*
        for k, v in fields.items():
            if k.startswith("customfield_"):
                stored_fields[k] = v

        s["issues"][key] = {
            "id": issue_id,
            "key": key,
            "project": project_key,
            "creator_id": s["self"]["accountId"],
            "created": now,
            "updated": now,
            "fields": stored_fields,
        }
        s["comments"].setdefault(key, [])

        _record(s, "createIssue", key=key, project=project_key,
                issuetype=issuetype_id, summary=summary)
        _save_state(s)
        return {
            "id": issue_id,
            "key": key,
            "self": f"{_base_url(s)}/rest/api/3/issue/{issue_id}",
        }


def _extract_account_id(field: Any) -> str | None:
    if not field:
        return None
    if isinstance(field, dict):
        return field.get("accountId")
    if isinstance(field, str):
        return field
    return None


# ---------------------------------------------------------------------------
# Issue: editIssue
# ---------------------------------------------------------------------------

@mcp.tool(name="editIssue")
def edit_issue(issueIdOrKey: str,
               fields: dict | None = None,
               update: dict | None = None,
               notifyUsers: bool = True) -> dict:
    """Jira REST: PUT /rest/api/3/issue/{issueIdOrKey}

    Edits an issue. Body shape mirrors createIssue: provide a `fields`
    dict to overwrite the named fields, and/or an `update` dict whose
    values are arrays of {add|set|remove|edit: value} operations
    (mock implements `set`, `add`, `remove`, `edit`).
    Returns 204 (empty body) on success — we return an empty dict
    `{}` to match.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "editIssue", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        iss = s["issues"][key]
        sf = iss["fields"]
        f = fields or {}

        if "summary" in f:
            sf["summary"] = f["summary"]
        if "description" in f:
            sf["description"] = _to_adf(f["description"])
        if "labels" in f:
            sf["labels"] = list(f["labels"] or [])
        if "duedate" in f:
            sf["duedate"] = f["duedate"]
        if "priority" in f:
            pr = f["priority"] or {}
            pid = None
            if isinstance(pr, dict):
                if pr.get("id") and str(pr["id"]) in s["priorities"]:
                    pid = str(pr["id"])
                elif pr.get("name"):
                    pid = _priority_id_by_name(s, pr["name"])
            sf["priority_id"] = pid
        if "assignee" in f:
            aid = _extract_account_id(f.get("assignee"))
            if aid == "-1":
                # Jira convention: -1 = default assignee. Mock keeps it
                # unassigned for simplicity.
                aid = None
            if aid and aid not in s["users"]:
                _record(s, "editIssue", issueIdOrKey=key,
                        result="invalid_assignee")
                _save_state(s)
                return _err(400, "", errors={
                    "assignee": f"User '{aid}' does not exist."})
            sf["assignee_id"] = aid
        if "issuetype" in f:
            it_in = f["issuetype"] or {}
            tid = None
            if isinstance(it_in, dict):
                if it_in.get("id") and str(it_in["id"]) in s["issue_types"]:
                    tid = str(it_in["id"])
                elif it_in.get("name"):
                    tid = _issue_type_id_by_name(s, it_in["name"])
            if tid:
                sf["issuetype_id"] = tid
        for k, v in f.items():
            if k.startswith("customfield_"):
                sf[k] = v

        # update ops
        for field_name, ops in (update or {}).items():
            if not isinstance(ops, list):
                continue
            for op in ops:
                if not isinstance(op, dict):
                    continue
                if "set" in op:
                    if field_name == "summary":
                        sf["summary"] = op["set"]
                    elif field_name == "labels":
                        sf["labels"] = list(op["set"] or [])
                    elif field_name == "description":
                        sf["description"] = _to_adf(op["set"])
                    elif field_name.startswith("customfield_"):
                        sf[field_name] = op["set"]
                elif "add" in op:
                    if field_name == "labels":
                        sf.setdefault("labels", [])
                        if op["add"] not in sf["labels"]:
                            sf["labels"].append(op["add"])
                elif "remove" in op:
                    if field_name == "labels":
                        sf.setdefault("labels", [])
                        if op["remove"] in sf["labels"]:
                            sf["labels"].remove(op["remove"])

        iss["updated"] = _now_iso()
        _record(s, "editIssue", issueIdOrKey=key,
                field_keys=list((fields or {}).keys()),
                update_keys=list((update or {}).keys()),
                notifyUsers=notifyUsers)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Issue: deleteIssue
# ---------------------------------------------------------------------------

@mcp.tool(name="deleteIssue")
def delete_issue(issueIdOrKey: str,
                 deleteSubtasks: bool = False) -> dict:
    """Jira REST: DELETE /rest/api/3/issue/{issueIdOrKey}

    Deletes the issue. `deleteSubtasks` defaults to false; with
    subtasks present and the flag unset, real Jira returns 400. The
    mock honours that same gating. Returns 204 (empty dict) on
    success.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "deleteIssue", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        iss = s["issues"][key]
        subtasks = iss["fields"].get("subtasks") or []
        if subtasks and not deleteSubtasks:
            _record(s, "deleteIssue", issueIdOrKey=key,
                    result="has_subtasks")
            _save_state(s)
            return _err(400, "The issue has subtasks. You must use the "
                             "deleteSubtasks parameter to delete it.")
        del s["issues"][key]
        s["comments"].pop(key, None)
        _record(s, "deleteIssue", issueIdOrKey=key,
                deleteSubtasks=deleteSubtasks)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Issue: searchForIssuesUsingJql
# ---------------------------------------------------------------------------

@mcp.tool(name="searchForIssuesUsingJql")
def search_for_issues_using_jql(jql: str = "",
                                startAt: int = 0,
                                maxResults: int = 50,
                                fields: list[str] | None = None,
                                expand: str | None = None,
                                fieldsByKeys: bool = False) -> dict:
    """Jira REST: POST /rest/api/3/search (JQL)

    Returns {"startAt","maxResults","total","issues":[...]}. JQL
    support is a documented subset: `field OP value` joined by AND,
    operators `=`, `!=`, `~`, `!~`, `in`, `not in`, `is`, `is not`;
    fields `project`, `key`, `status`, `priority`, `issuetype`,
    `assignee`, `reporter`, `labels`, `summary`, `description`,
    `text`, `created`, `updated`, `resolution`, plus any
    `customfield_*`. `ORDER BY` accepted with single sort key. OR
    and complex grouping are NOT supported.
    """
    with _lock():
        s = _load_state()
        parsed = _parse_jql(jql)
        if parsed is None:
            _record(s, "searchForIssuesUsingJql", jql=jql,
                    result="invalid_jql")
            _save_state(s)
            return _err(400, f"Error in the JQL Query: {jql!r}.")
        clauses, order = parsed
        matches: list[str] = []
        for key in s["issues"].keys():
            if _jql_match(s, key, clauses):
                matches.append(key)
        if order:
            field, direction = order
            matches.sort(
                key=lambda k: (_issue_field_for_jql(s, k, field.lower()) or ""),
                reverse=(direction == "desc"),
            )
        else:
            matches.sort()

        startAt = max(0, int(startAt or 0))
        maxResults = max(0, min(int(maxResults or 50), 100))
        page_keys = matches[startAt: startAt + maxResults]
        issues_out = [_issue_obj(s, k, fields_filter=fields)
                      for k in page_keys]
        _record(s, "searchForIssuesUsingJql", jql=jql,
                total=len(matches), startAt=startAt,
                maxResults=maxResults, count=len(page_keys))
        _save_state(s)
        return {
            "expand": "names,schema",
            "startAt": startAt,
            "maxResults": maxResults,
            "total": len(matches),
            "issues": issues_out,
        }


# ---------------------------------------------------------------------------
# Issue: getTransitions
# ---------------------------------------------------------------------------

@mcp.tool(name="getTransitions")
def get_transitions(issueIdOrKey: str,
                    expand: str | None = None,
                    transitionId: str | None = None,
                    skipRemoteOnlyCondition: bool = False,
                    includeUnavailableTransitions: bool = False,
                    sortByOpsBarAndStatus: bool = False) -> dict:
    """Jira REST: GET /rest/api/3/issue/{issueIdOrKey}/transitions

    Returns {"expand": "...", "transitions": [{"id","name","to":{
    "id","name",...}, "isAvailable","hasScreen","isInitial",
    "isGlobal","isConditional"}]}. Transitions are derived from the
    seeded workflow: state["workflow"][current_status_name] -> list
    of {"id","name","to"} entries.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "getTransitions", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        iss = s["issues"][key]
        sid = iss["fields"].get("status_id")
        current = s["statuses"].get(sid, {}).get("name", "")
        wf = s.get("workflow", {})
        avail = wf.get(current, [])
        if transitionId:
            avail = [t for t in avail if str(t.get("id")) == str(transitionId)]
        out = []
        for t in avail:
            to_name = t.get("to")
            to_sid = _status_id_by_name(s, to_name) if to_name else None
            to_obj = _status_obj(s, to_sid) if to_sid else {
                "name": to_name or "", "id": ""}
            out.append({
                "id": str(t.get("id")),
                "name": t.get("name", ""),
                "to": to_obj,
                "hasScreen": False,
                "isGlobal": False,
                "isInitial": False,
                "isAvailable": True,
                "isConditional": False,
                "isLooped": False,
            })
        _record(s, "getTransitions", issueIdOrKey=key,
                current_status=current, count=len(out))
        _save_state(s)
        return {"expand": "transitions", "transitions": out}


# ---------------------------------------------------------------------------
# Issue: doTransition
# ---------------------------------------------------------------------------

@mcp.tool(name="doTransition")
def do_transition(issueIdOrKey: str,
                  transition: dict,
                  fields: dict | None = None,
                  update: dict | None = None,
                  historyMetadata: dict | None = None) -> dict:
    """Jira REST: POST /rest/api/3/issue/{issueIdOrKey}/transitions

    Body: `{"transition": {"id": "<transitionId>"}, "fields": {...},
    "update": {...}}`. Returns 204 (empty dict) on success, error
    object otherwise.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "doTransition", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        if not isinstance(transition, dict) or not transition.get("id"):
            _record(s, "doTransition", issueIdOrKey=key,
                    result="missing_transition_id")
            _save_state(s)
            return _err(400, "Transition id is required.")
        tid = str(transition["id"])
        iss = s["issues"][key]
        sid = iss["fields"].get("status_id")
        current = s["statuses"].get(sid, {}).get("name", "")
        wf = s.get("workflow", {})
        avail = wf.get(current, [])
        match = next((t for t in avail if str(t.get("id")) == tid), None)
        if not match:
            _record(s, "doTransition", issueIdOrKey=key,
                    transition=tid, result="invalid_transition")
            _save_state(s)
            return _err(400, f"It is not possible to perform this "
                             f"transition on the specified issue.")
        new_status_id = _status_id_by_name(s, match.get("to", ""))
        if not new_status_id:
            return _err(400, "Transition target status not found.")
        iss["fields"]["status_id"] = new_status_id
        iss["updated"] = _now_iso()
        # Optional field updates accompanying the transition
        if fields:
            # Delegate to editIssue's field setting logic by mutating
            # in place. Only the common ones — keep simple.
            if "resolution" in fields:
                res = fields["resolution"]
                if isinstance(res, dict):
                    iss["fields"]["resolution"] = res
                else:
                    iss["fields"]["resolution"] = {"name": str(res)}
                iss["fields"]["resolutiondate"] = _now_iso()
        _record(s, "doTransition", issueIdOrKey=key,
                transition_id=tid, to_status=match.get("to"))
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Issue: assignIssue
# ---------------------------------------------------------------------------

@mcp.tool(name="assignIssue")
def assign_issue(issueIdOrKey: str, accountId: str | None = None) -> dict:
    """Jira REST: PUT /rest/api/3/issue/{issueIdOrKey}/assignee

    Body: `{"accountId":"<accountId>"}`. Special values:
      - `"-1"`  → default assignee (mock leaves it as null)
      - `null` → unassign
    Returns 204 (empty dict) on success.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "assignIssue", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        if accountId in (None, "-1"):
            s["issues"][key]["fields"]["assignee_id"] = None
        else:
            if accountId not in s["users"]:
                _record(s, "assignIssue", issueIdOrKey=key,
                        accountId=accountId, result="user_not_found")
                _save_state(s)
                return _err(400, "", errors={
                    "accountId": f"User '{accountId}' does not exist."})
            s["issues"][key]["fields"]["assignee_id"] = accountId
        s["issues"][key]["updated"] = _now_iso()
        _record(s, "assignIssue", issueIdOrKey=key, accountId=accountId)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="getComments")
def get_comments(issueIdOrKey: str,
                 startAt: int = 0,
                 maxResults: int = 50,
                 orderBy: str | None = None,
                 expand: str | None = None) -> dict:
    """Jira REST: GET /rest/api/3/issue/{issueIdOrKey}/comment

    Returns {"startAt","maxResults","total","comments":[...]}.
    `orderBy` of `"created"` or `"-created"` controls direction
    (defaults to ascending by created time).
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "getComments", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        comments = list(s["comments"].get(key, []))
        reverse = (orderBy or "").startswith("-")
        comments.sort(key=lambda c: c.get("created", ""), reverse=reverse)
        startAt = max(0, int(startAt or 0))
        maxResults = max(0, min(int(maxResults or 50), 100))
        page = comments[startAt: startAt + maxResults]
        out = [_comment_obj(s, c) for c in page]
        _record(s, "getComments", issueIdOrKey=key,
                count=len(out), total=len(comments))
        _save_state(s)
        return {
            "startAt": startAt,
            "maxResults": maxResults,
            "total": len(comments),
            "comments": out,
        }


@mcp.tool(name="addComment")
def add_comment(issueIdOrKey: str,
                body: Any,
                visibility: dict | None = None,
                properties: list | None = None) -> dict:
    """Jira REST: POST /rest/api/3/issue/{issueIdOrKey}/comment

    `body` may be a plain string (auto-wrapped into ADF) or a full
    ADF document. Returns the created comment object.
    """
    with _lock():
        s = _load_state()
        key = _resolve_issue_id(s, issueIdOrKey)
        if not key:
            _record(s, "addComment", issueIdOrKey=issueIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "Issue does not exist or you do not have "
                             "permission to see it.")
        cid = _new_comment_id(s)
        now = _now_iso()
        c = {
            "id": cid,
            "issue_key": key,
            "author_id": s["self"]["accountId"],
            "update_author_id": s["self"]["accountId"],
            "body": _to_adf(body),
            "created": now,
            "updated": now,
            "visibility": visibility,
            "jsdPublic": True,
        }
        s["comments"].setdefault(key, []).append(c)
        s["issues"][key]["updated"] = now
        _record(s, "addComment", issueIdOrKey=key, comment_id=cid)
        _save_state(s)
        return _comment_obj(s, c)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@mcp.tool(name="getProject")
def get_project(projectIdOrKey: str,
                expand: str | None = None,
                properties: list | None = None) -> dict:
    """Jira REST: GET /rest/api/3/project/{projectIdOrKey}

    Returns a full project object including `lead`, `issueTypes`,
    and metadata.
    """
    with _lock():
        s = _load_state()
        key = _resolve_project(s, projectIdOrKey)
        if not key:
            _record(s, "getProject", projectIdOrKey=projectIdOrKey,
                    result="not_found")
            _save_state(s)
            return _err(404, "No project could be found with key or id "
                             f"'{projectIdOrKey}'.")
        obj = _project_obj(s, key, include_full=True)
        _record(s, "getProject", projectIdOrKey=projectIdOrKey, key=key)
        _save_state(s)
        return obj


def _resolve_project(state: dict, ref: str) -> str | None:
    if not ref:
        return None
    if ref in state["projects"]:
        return ref
    for k, p in state["projects"].items():
        if p["id"] == str(ref):
            return k
    return None


@mcp.tool(name="getAllProjects")
def get_all_projects(expand: str | None = None,
                     recent: int | None = None,
                     properties: list | None = None) -> list:
    """Jira REST: GET /rest/api/3/project

    Returns a JSON array (not paginated) of project objects accessible
    to the user. `recent` and `expand` are accepted but ignored.
    """
    with _lock():
        s = _load_state()
        out = [_project_obj(s, k, include_full=False)
               for k in sorted(s["projects"].keys())]
        if recent:
            out = out[: int(recent)]
        _record(s, "getAllProjects", count=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool(name="findUsers")
def find_users(query: str | None = None,
               accountId: str | None = None,
               username: str | None = None,
               startAt: int = 0,
               maxResults: int = 50,
               property: str | None = None) -> list:
    """Jira REST: GET /rest/api/3/user/search

    Returns a JSON array of matching user objects. `query` is a
    case-insensitive substring match against displayName or email
    (real Jira's behaviour when GDPR strict mode is on). If
    `accountId` is supplied, returns exactly that user.
    """
    with _lock():
        s = _load_state()
        results: list[dict] = []
        if accountId:
            u = _user_obj(s, accountId)
            if u:
                results = [u]
        else:
            q = (query or "").lower().strip()
            for aid, u in s["users"].items():
                hay = " ".join([
                    u.get("displayName", "") or "",
                    u.get("emailAddress", "") or "",
                    u.get("accountId", "") or "",
                ]).lower()
                if not q or q in hay:
                    obj = _user_obj(s, aid)
                    if obj:
                        results.append(obj)
        startAt = max(0, int(startAt or 0))
        maxResults = max(0, min(int(maxResults or 50), 1000))
        page = results[startAt: startAt + maxResults]
        _record(s, "findUsers", query=query, accountId=accountId,
                count=len(page))
        _save_state(s)
        return page


@mcp.tool(name="getUser")
def get_user(accountId: str,
             expand: str | None = None) -> dict:
    """Jira REST: GET /rest/api/3/user?accountId=...

    Returns a single user object or 404 error if not found.
    """
    with _lock():
        s = _load_state()
        u = _user_obj(s, accountId)
        _record(s, "getUser", accountId=accountId,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _err(404, "The user named in the query parameters "
                             "does not exist.")
        return u


# ---------------------------------------------------------------------------
# Issue types, statuses, priorities
# ---------------------------------------------------------------------------

@mcp.tool(name="getIssueAllTypes")
def get_issue_all_types() -> list:
    """Jira REST: GET /rest/api/3/issuetype

    Returns the full list of issue type objects defined on the
    instance (Story, Bug, Task, Epic, Subtask, ...).
    """
    with _lock():
        s = _load_state()
        out = [_issue_type_obj(s, tid) for tid in s["issue_types"]]
        out = [o for o in out if o]
        _record(s, "getIssueAllTypes", count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="getStatuses")
def get_statuses() -> list:
    """Jira REST: GET /rest/api/3/status

    Returns the full list of status objects defined on the instance
    (To Do, In Progress, Done, ...).
    """
    with _lock():
        s = _load_state()
        out = [_status_obj(s, sid) for sid in s["statuses"]]
        out = [o for o in out if o]
        _record(s, "getStatuses", count=len(out))
        _save_state(s)
        return out


@mcp.tool(name="getPriorities")
def get_priorities() -> list:
    """Jira REST: GET /rest/api/3/priority

    Returns the full list of priority objects defined on the instance
    (Highest, High, Medium, Low, Lowest, ...).
    """
    with _lock():
        s = _load_state()
        out = [_priority_obj(s, pid) for pid in s["priorities"]]
        out = [o for o in out if o]
        _record(s, "getPriorities", count=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection). Not part of the real Jira surface."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(base_url: str | None = None,
                    self_user: dict | None = None,
                    users: list | None = None,
                    projects: list | None = None,
                    issue_types: list | None = None,
                    statuses: list | None = None,
                    priorities: list | None = None,
                    workflow: dict | None = None,
                    issues: list | None = None,
                    comments: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed Jira state with catalog + runtime data.

    Each input is a list of Jira-shaped dicts (id is auto-assigned
    if omitted). Fields recognised per kind:

      users:       [{accountId?, displayName, emailAddress?, active?}]
      projects:    [{id?, key, name?, projectTypeKey?,
                     leadAccountId?, issueTypeIds?: [id],
                     initialStatusId?: id}]
      issue_types: [{id?, name, description?, subtask?}]
      statuses:    [{id?, name, statusCategory?:{id,key,name,colorName}}]
      priorities:  [{id?, name, description?}]
      workflow:    {"<status_name>": [{"id","name","to"}]}
      issues:      [{key?, project, summary, description?,
                     issuetype, status?, priority?, assignee?,
                     reporter?, labels?, ...}]
      comments:    [{issueKey, body, author?: accountId}]

    When `replace=True` the entire state is reset first.
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if base_url:
            s["base_url"] = base_url
        if self_user:
            s["self"].update(self_user)
        for u in users or []:
            aid = u.get("accountId") or _new_account_id(s)
            s["users"][aid] = {
                "accountId": aid,
                "accountType": u.get("accountType", "atlassian"),
                "displayName": u.get("displayName", aid),
                "emailAddress": u.get("emailAddress"),
                "active": u.get("active", True),
                "timeZone": u.get("timeZone", "UTC"),
                "avatarUrls": u.get("avatarUrls", {}),
            }
        for it in issue_types or []:
            tid = str(it.get("id") or _new_issue_type_id(s))
            s["issue_types"][tid] = {
                "id": tid,
                "name": it.get("name", tid),
                "description": it.get("description", ""),
                "iconUrl": it.get("iconUrl", ""),
                "subtask": bool(it.get("subtask", False)),
                "hierarchyLevel": it.get("hierarchyLevel", 0),
            }
        for st in statuses or []:
            sid = str(st.get("id") or _new_status_id(s))
            s["statuses"][sid] = {
                "id": sid,
                "name": st.get("name", sid),
                "description": st.get("description", ""),
                "iconUrl": st.get("iconUrl", ""),
                "statusCategory": st.get("statusCategory", {
                    "id": 2, "key": "new", "colorName": "blue-gray",
                    "name": "To Do",
                }),
            }
        for pr in priorities or []:
            pid = str(pr.get("id") or _new_priority_id(s))
            s["priorities"][pid] = {
                "id": pid,
                "name": pr.get("name", pid),
                "description": pr.get("description", ""),
                "iconUrl": pr.get("iconUrl", ""),
                "statusColor": pr.get("statusColor", "#999999"),
            }
        for p in projects or []:
            pid = str(p.get("id") or _new_project_id(s))
            key = p.get("key") or f"PROJ{pid}"
            s["projects"][key] = {
                "id": pid,
                "key": key,
                "name": p.get("name", key),
                "projectTypeKey": p.get("projectTypeKey", "software"),
                "simplified": p.get("simplified", True),
                "style": p.get("style", "next-gen"),
                "isPrivate": p.get("isPrivate", False),
                "description": p.get("description", ""),
                "avatarUrls": p.get("avatarUrls", {}),
                "leadAccountId": p.get("leadAccountId"),
                "issueTypeIds": [str(t) for t in (p.get("issueTypeIds") or [])],
                "initialStatusId": (str(p["initialStatusId"])
                                    if p.get("initialStatusId") else None),
                "nextSeq": int(p.get("nextSeq", 1)),
            }
        if workflow:
            s["workflow"].update(workflow)
        for iss_in in issues or []:
            proj_key = iss_in.get("project")
            if proj_key not in s["projects"]:
                continue
            proj = s["projects"][proj_key]
            iid = str(iss_in.get("id") or _new_issue_id(s))
            key = iss_in.get("key")
            if not key:
                seq = proj.get("nextSeq", 1)
                proj["nextSeq"] = seq + 1
                key = f"{proj_key}-{seq}"
            issuetype_id = None
            it_in = iss_in.get("issuetype")
            if isinstance(it_in, dict):
                if it_in.get("id"):
                    issuetype_id = str(it_in["id"])
                elif it_in.get("name"):
                    issuetype_id = _issue_type_id_by_name(s, it_in["name"])
            elif isinstance(it_in, str):
                issuetype_id = (it_in if it_in in s["issue_types"]
                                else _issue_type_id_by_name(s, it_in))
            status_id = None
            st_in = iss_in.get("status")
            if isinstance(st_in, dict):
                status_id = (str(st_in["id"]) if st_in.get("id")
                             else _status_id_by_name(s, st_in.get("name", "")))
            elif isinstance(st_in, str):
                status_id = (st_in if st_in in s["statuses"]
                             else _status_id_by_name(s, st_in))
            priority_id = None
            pr_in = iss_in.get("priority")
            if isinstance(pr_in, dict):
                priority_id = (str(pr_in["id"]) if pr_in.get("id")
                               else _priority_id_by_name(
                                   s, pr_in.get("name", "")))
            elif isinstance(pr_in, str):
                priority_id = (pr_in if pr_in in s["priorities"]
                               else _priority_id_by_name(s, pr_in))
            assignee_id = _extract_account_id(iss_in.get("assignee"))
            reporter_id = (_extract_account_id(iss_in.get("reporter"))
                           or s["self"]["accountId"])
            now = _now_iso()
            stored: dict[str, Any] = {
                "summary": iss_in.get("summary", ""),
                "description": _to_adf(iss_in.get("description")),
                "issuetype_id": issuetype_id,
                "status_id": status_id or proj.get("initialStatusId"),
                "priority_id": priority_id,
                "assignee_id": assignee_id,
                "reporter_id": reporter_id,
                "labels": list(iss_in.get("labels") or []),
                "components": list(iss_in.get("components") or []),
                "fixVersions": list(iss_in.get("fixVersions") or []),
                "versions": list(iss_in.get("versions") or []),
                "duedate": iss_in.get("duedate"),
                "parent": iss_in.get("parent"),
                "subtasks": iss_in.get("subtasks") or [],
                "issuelinks": iss_in.get("issuelinks") or [],
                "votes": int(iss_in.get("votes", 0)),
                "watchCount": int(iss_in.get("watchCount", 0)),
            }
            for k, v in iss_in.items():
                if k.startswith("customfield_"):
                    stored[k] = v
            s["issues"][key] = {
                "id": iid,
                "key": key,
                "project": proj_key,
                "creator_id": (_extract_account_id(iss_in.get("creator"))
                               or s["self"]["accountId"]),
                "created": iss_in.get("created", now),
                "updated": iss_in.get("updated", now),
                "fields": stored,
            }
            s["comments"].setdefault(key, [])
        for c in comments or []:
            key = c.get("issueKey")
            if key not in s["issues"]:
                continue
            cid = str(c.get("id") or _new_comment_id(s))
            now = _now_iso()
            entry = {
                "id": cid,
                "issue_key": key,
                "author_id": c.get("author") or s["self"]["accountId"],
                "update_author_id": (c.get("updateAuthor")
                                     or c.get("author")
                                     or s["self"]["accountId"]),
                "body": _to_adf(c.get("body")),
                "created": c.get("created", now),
                "updated": c.get("updated", now),
                "visibility": c.get("visibility"),
                "jsdPublic": c.get("jsdPublic", True),
            }
            s["comments"].setdefault(key, []).append(entry)
        _record(s, "debug_seed",
                counts={
                    "users": len(users or []),
                    "projects": len(projects or []),
                    "issue_types": len(issue_types or []),
                    "statuses": len(statuses or []),
                    "priorities": len(priorities or []),
                    "issues": len(issues or []),
                    "comments": len(comments or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "issue_keys": list(s["issues"].keys()),
            "project_keys": list(s["projects"].keys()),
            "user_ids": list(s["users"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
