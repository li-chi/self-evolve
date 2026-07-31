"""Zendesk Support mock MCP server.

Mirrors the Zendesk Support REST API v2
(developer.zendesk.com/api-reference/ticketing/introduction/). Each
MCP tool maps to a top-level Support v2 endpoint; parameter shapes
match the documented query / request body, and responses use the
Zendesk envelope conventions verbatim:

  - single resource          -> {"<type>": {...}}    (e.g. {"ticket": {...}})
  - collection of resources  -> {"<type>s": [...]}   (e.g. {"tickets": [...]})
  - paginated collections    -> {"<type>s": [...], "next_page": "...",
                                 "previous_page": "...", "count": N}

URLs are emitted as `https://mockcorp.zendesk.com/api/v2/<type>/{id}.json`.
Timestamps are ISO 8601 with `Z` suffix (e.g. "2024-01-01T00:00:00Z").
IDs are positive monotonically-increasing integers per resource type.

State plumbing mirrors slack/jira/linear mocks: a single JSON file at
`$ZENDESK_MOCK_STATE_DIR/state.json` (default `~/.openclaw/zendesk_mock`),
`fcntl.flock`-guarded, atomic save, optional preseed via
`$ZENDESK_MOCK_SEED_PATH`. Every call appends to `state["calls"]` so
verifiers can replay the trace.

Tool surface (Support v2 endpoint mapping in parens):

  Tickets       list_tickets                 GET /api/v2/tickets.json
                get_ticket                   GET /api/v2/tickets/{id}.json
                create_ticket                POST /api/v2/tickets.json
                update_ticket                PUT /api/v2/tickets/{id}.json
                delete_ticket                DELETE /api/v2/tickets/{id}.json
                list_ticket_comments         GET /api/v2/tickets/{id}/comments.json
                search_tickets               GET /api/v2/search.json?query=type:ticket+...
                incremental_tickets          GET /api/v2/incremental/tickets.json
  Users         list_users, get_user, create_user, update_user, search_users
                  GET/POST/PUT /api/v2/users(.json|/{id}.json|/search.json)
  Orgs          list_organizations, get_organization, create_organization
                  GET/POST /api/v2/organizations(.json|/{id}.json)
  Groups        list_groups, create_group, add_user_to_group
                  GET/POST /api/v2/groups(.json|...)
                  POST /api/v2/group_memberships.json
  Macros        list_macros                  GET /api/v2/macros.json
                apply_macro_to_ticket        (convenience: PUT ticket with macro actions)
  Misc          list_satisfaction_ratings    GET /api/v2/satisfaction_ratings.json
                list_ticket_fields           GET /api/v2/ticket_fields.json
                list_views                   GET /api/v2/views.json
                count_tickets_in_view        GET /api/v2/views/{id}/count.json

Plus mock-only debug tools (not in the real Zendesk surface):

  mock_debug_state, mock_debug_seed_ticket, mock_debug_seed_user,
  mock_debug_seed_organization, mock_debug_seed_group,
  mock_debug_seed_macro, mock_debug_seed_view

Search query DSL handled (Zendesk Support search syntax, subset):
  type:ticket  status:<value>  priority:<value>  assignee:none|<id>|<email>
  requester:<id>|<email>  tags:<tag>  group:<id>
  created>YYYY-MM-DD  created<YYYY-MM-DD  updated>...  updated<...
  free-text terms match subject/description (case-insensitive).
  Multiple terms AND-combine.

Intentionally NOT modelled: webhooks, OAuth, side conversations, the
Zendesk Apps Framework, Chat / Talk / Sell / Guide products, attachments
upload pipeline (placeholder list only), live notifications.
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


ZENDESK_SUBDOMAIN = "mockcorp"
ZENDESK_BASE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "ZENDESK_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/zendesk_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def _empty_state() -> dict:
    # Pre-seed a single admin user, default group, and default org so
    # that an "empty" state still answers the real-world expectations
    # of created_by/assignee/etc. (a real Zendesk account always has at
    # least one admin user and one default group on signup).
    admin_id = 1
    org_id = 1
    group_id = 1
    now = _now()
    return {
        "subdomain": ZENDESK_SUBDOMAIN,
        "tickets": {
            # str(id) -> ticket dict
        },
        "users": {
            str(admin_id): {
                "id": admin_id,
                "url": f"{ZENDESK_BASE_URL}/api/v2/users/{admin_id}.json",
                "name": "Mock Admin",
                "email": "admin@mockcorp.com",
                "role": "admin",
                "verified": True,
                "active": True,
                "organization_id": org_id,
                "phone": None,
                "tags": [],
                "external_id": None,
                "time_zone": "UTC",
                "iana_time_zone": "Etc/UTC",
                "locale": "en-US",
                "locale_id": 1,
                "alias": None,
                "details": "",
                "notes": "",
                "signature": None,
                "moderator": False,
                "ticket_restriction": None,
                "only_private_comments": False,
                "restricted_agent": False,
                "suspended": False,
                "default_group_id": group_id,
                "shared": False,
                "shared_agent": False,
                "last_login_at": now,
                "two_factor_auth_enabled": False,
                "user_fields": {},
                "photo": None,
                "shared_phone_number": None,
                "report_csv": False,
                "role_type": None,
                "custom_role_id": None,
                "created_at": now,
                "updated_at": now,
            },
        },
        "organizations": {
            str(org_id): {
                "id": org_id,
                "url": (f"{ZENDESK_BASE_URL}/api/v2/organizations/"
                        f"{org_id}.json"),
                "name": "Mock Corp",
                "domain_names": ["mockcorp.com"],
                "details": "",
                "notes": "",
                "group_id": group_id,
                "shared_tickets": False,
                "shared_comments": False,
                "tags": [],
                "organization_fields": {},
                "external_id": None,
                "created_at": now,
                "updated_at": now,
            },
        },
        "groups": {
            str(group_id): {
                "id": group_id,
                "url": (f"{ZENDESK_BASE_URL}/api/v2/groups/{group_id}.json"),
                "name": "Support",
                "description": "Default support group",
                "default": True,
                "deleted": False,
                "created_at": now,
                "updated_at": now,
            },
        },
        "group_memberships": {
            # str(id) -> {id, user_id, group_id, default, created_at, updated_at}
        },
        "macros": {
            # str(id) -> macro
        },
        "ticket_fields": {
            # str(id) -> ticket field
        },
        "views": {
            # str(id) -> view
        },
        "comments": {
            # str(ticket_id) -> [comment dicts]
        },
        "audits": {
            # str(ticket_id) -> [audit dicts]
        },
        "satisfaction_ratings": {
            # str(id) -> rating
        },
        "self_user_id": admin_id,
        "next_id": {
            "ticket": 1,
            "user": admin_id + 1,
            "organization": org_id + 1,
            "group": group_id + 1,
            "group_membership": 1,
            "macro": 1,
            "ticket_field": 1,
            "view": 1,
            "comment": 1,
            "audit": 1,
            "satisfaction_rating": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ZENDESK_MOCK_SEED_PATH")
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


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _ticket_url(ticket_id: int) -> str:
    return f"{ZENDESK_BASE_URL}/api/v2/tickets/{ticket_id}.json"


def _user_url(user_id: int) -> str:
    return f"{ZENDESK_BASE_URL}/api/v2/users/{user_id}.json"


def _org_url(org_id: int) -> str:
    return f"{ZENDESK_BASE_URL}/api/v2/organizations/{org_id}.json"


def _group_url(group_id: int) -> str:
    return f"{ZENDESK_BASE_URL}/api/v2/groups/{group_id}.json"


def _comment_url(ticket_id: int, comment_id: int) -> str:
    return (f"{ZENDESK_BASE_URL}/api/v2/tickets/{ticket_id}/comments/"
            f"{comment_id}.json")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _user_by_email(state: dict, email: str) -> dict | None:
    if not email:
        return None
    target = email.strip().lower()
    for u in state["users"].values():
        if (u.get("email") or "").strip().lower() == target:
            return u
    return None


def _resolve_user_ref(state: dict, ref: Any) -> int | None:
    """Resolve `none` / numeric id / email to a user id, or None."""
    if ref is None:
        return None
    if isinstance(ref, int):
        return ref if str(ref) in state["users"] else None
    if isinstance(ref, str):
        low = ref.strip().lower()
        if low in ("", "none", "null"):
            return None
        if low.isdigit():
            return int(low) if low in state["users"] else None
        u = _user_by_email(state, ref)
        return u["id"] if u else None
    return None


# ---------------------------------------------------------------------------
# Object shaping
# ---------------------------------------------------------------------------

_VALID_STATUSES = ("new", "open", "pending", "hold", "solved", "closed")
_VALID_PRIORITIES = ("low", "normal", "high", "urgent")
_VALID_TYPES = ("problem", "incident", "question", "task")
_VALID_ROLES = ("end-user", "agent", "admin")


def _normalize_comment_input(c: Any) -> dict | None:
    """Normalize a comment payload from create/update body to the
    stored comment shape."""
    if c is None:
        return None
    if isinstance(c, str):
        return {"body": c, "html_body": None, "public": True,
                "type": "Comment"}
    if isinstance(c, dict):
        body = c.get("body") or c.get("value") or ""
        html_body = c.get("html_body")
        public = c.get("public", True)
        return {
            "body": str(body),
            "html_body": html_body,
            "public": bool(public),
            "type": c.get("type") or "Comment",
            "author_id": c.get("author_id"),
            "uploads": list(c.get("uploads") or []),
        }
    return None


def _new_ticket(state: dict, *,
                tid: int,
                subject: str,
                description: str,
                priority: str | None,
                status: str | None,
                ttype: str | None,
                requester_id: int,
                submitter_id: int,
                assignee_id: int | None,
                group_id: int | None,
                organization_id: int | None,
                tags: list[str],
                external_id: str | None,
                custom_fields: list[dict],
                via_channel: str = "api",
                now: str | None = None) -> dict:
    now = now or _now()
    return {
        "id": tid,
        "url": _ticket_url(tid),
        "external_id": external_id,
        "type": ttype,
        "subject": subject,
        "raw_subject": subject,
        "description": description,
        "priority": priority,
        "status": status or "new",
        "recipient": None,
        "requester_id": requester_id,
        "submitter_id": submitter_id,
        "assignee_id": assignee_id,
        "organization_id": organization_id,
        "group_id": group_id,
        "collaborator_ids": [],
        "follower_ids": [],
        "email_cc_ids": [],
        "forum_topic_id": None,
        "problem_id": None,
        "has_incidents": False,
        "due_at": None,
        "tags": list(tags or []),
        "via": {"channel": via_channel,
                "source": {"from": {}, "to": {}, "rel": None}},
        "custom_fields": list(custom_fields or []),
        "satisfaction_rating": None,
        "sharing_agreement_ids": [],
        "fields": list(custom_fields or []),
        "followup_ids": [],
        "ticket_form_id": None,
        "brand_id": None,
        "allow_channelback": False,
        "allow_attachments": True,
        "from_messaging_channel": False,
        "created_at": now,
        "updated_at": now,
    }


def _new_comment(state: dict, *, cid: int, ticket_id: int,
                 author_id: int, body: str,
                 html_body: str | None = None,
                 public: bool = True,
                 type_: str = "Comment",
                 via_channel: str = "api",
                 audit_id: int | None = None,
                 now: str | None = None) -> dict:
    now = now or _now()
    if html_body is None:
        # Real API always echoes an html_body — minimal wrap.
        html_body = (f"<div class=\"zd-comment\">"
                     f"<p dir=\"auto\">{body}</p></div>")
    plain_body = body
    return {
        "id": cid,
        "url": _comment_url(ticket_id, cid),
        "type": type_,
        "ticket_id": ticket_id,
        "author_id": author_id,
        "body": body,
        "html_body": html_body,
        "plain_body": plain_body,
        "public": bool(public),
        "audit_id": audit_id,
        "via": {"channel": via_channel,
                "source": {"from": {}, "to": {}, "rel": None}},
        "attachments": [],
        "metadata": {"system": {}, "custom": {}},
        "created_at": now,
    }


def _new_audit(state: dict, *, aid: int, ticket_id: int, author_id: int,
               events: list[dict], now: str | None = None) -> dict:
    now = now or _now()
    return {
        "id": aid,
        "ticket_id": ticket_id,
        "author_id": author_id,
        "created_at": now,
        "via": {"channel": "api"},
        "events": list(events or []),
        "metadata": {"system": {}, "custom": {}},
    }


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

def _paginate(items: list, page: int, per_page: int,
              base_url: str) -> dict:
    """Zendesk classic pagination shape — returns the page + next_page,
    previous_page, count for envelopes that include them."""
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 100), 100))
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    sub = items[start:end]
    sep = "&" if "?" in base_url else "?"
    nxt = (f"{base_url}{sep}page={page + 1}&per_page={per_page}"
           if end < total else None)
    prv = (f"{base_url}{sep}page={page - 1}&per_page={per_page}"
           if page > 1 else None)
    return {"items": sub, "next_page": nxt, "previous_page": prv,
            "count": total, "page": page, "per_page": per_page}


def _sort_records(records: list[dict], sort_by: str | None,
                  sort_order: str | None) -> list[dict]:
    key = sort_by or "created_at"
    if key not in ("created_at", "updated_at", "priority", "status",
                   "id", "subject", "ticket_type"):
        key = "created_at"
    reverse = (sort_order or "asc").lower() == "desc"
    # Priority/status need a stable ordering by canonical rank.
    if key == "priority":
        rank = {None: 0, "low": 1, "normal": 2, "high": 3, "urgent": 4}
        return sorted(records, key=lambda r: rank.get(r.get("priority"), 0),
                      reverse=reverse)
    if key == "status":
        rank = {s: i for i, s in enumerate(_VALID_STATUSES)}
        return sorted(records, key=lambda r: rank.get(r.get("status"), 0),
                      reverse=reverse)
    if key == "ticket_type":
        rank = {None: 0, **{t: i + 1 for i, t in enumerate(_VALID_TYPES)}}
        return sorted(records, key=lambda r: rank.get(r.get("type"), 0),
                      reverse=reverse)
    return sorted(records, key=lambda r: (r.get(key) or ""),
                  reverse=reverse)


# ---------------------------------------------------------------------------
# Search DSL parser (Zendesk Support query syntax — documented subset)
# ---------------------------------------------------------------------------

_QUERY_TOKEN_RE = re.compile(
    r'\s*('
    r'"(?:[^"\\]|\\.)*"|'        # double-quoted
    r"'(?:[^'\\]|\\.)*'|"        # single-quoted
    r'\S+'                       # bare term
    r')'
)


def _parse_query(query: str) -> list[tuple[str, str, str]]:
    """Parse a Zendesk Support search query into a list of
    (field, op, value) clauses. `field == ""` means a free-text
    body/subject match. Operators: `:`, `>`, `<`, `>=`, `<=`."""
    if not query:
        return []
    clauses: list[tuple[str, str, str]] = []
    for m in _QUERY_TOKEN_RE.finditer(query):
        tok = m.group(1)
        if not tok:
            continue
        # Strip surrounding quotes
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
            tok = tok[1:-1]
            clauses.append(("", ":", tok))
            continue
        # Match `field:value`, `field>value`, etc.
        m2 = re.match(r'^([a-zA-Z_]+)(>=|<=|>|<|:)(.*)$', tok)
        if m2:
            field = m2.group(1).lower()
            op = m2.group(2)
            value = m2.group(3)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            clauses.append((field, op, value))
        else:
            clauses.append(("", ":", tok))
    return clauses


def _date_lte(a: str, b: str) -> bool:
    """Compare 'YYYY-MM-DD' (or longer ISO prefix) lexicographically."""
    return (a or "") <= (b or "")


def _ticket_matches_query(state: dict, t: dict,
                          clauses: list[tuple[str, str, str]]) -> bool:
    if not clauses:
        return True
    for field, op, value in clauses:
        if field == "":
            needle = value.lower()
            hay = " ".join([
                (t.get("subject") or "").lower(),
                (t.get("description") or "").lower(),
            ])
            if needle not in hay:
                return False
            continue
        if field == "type":
            if value == "ticket":
                continue
            # ticket type field
            if (t.get("type") or "") != value:
                return False
            continue
        if field == "status":
            if (t.get("status") or "") != value:
                return False
            continue
        if field == "priority":
            if (t.get("priority") or "") != value:
                return False
            continue
        if field == "assignee":
            if value.lower() == "none":
                if t.get("assignee_id") is not None:
                    return False
            else:
                resolved = _resolve_user_ref(state, value)
                if resolved != t.get("assignee_id"):
                    return False
            continue
        if field == "requester":
            resolved = _resolve_user_ref(state, value)
            if resolved != t.get("requester_id"):
                return False
            continue
        if field == "submitter":
            resolved = _resolve_user_ref(state, value)
            if resolved != t.get("submitter_id"):
                return False
            continue
        if field in ("group", "group_id"):
            try:
                gid = int(value)
            except (TypeError, ValueError):
                return False
            if t.get("group_id") != gid:
                return False
            continue
        if field in ("organization", "organization_id"):
            try:
                oid = int(value)
            except (TypeError, ValueError):
                return False
            if t.get("organization_id") != oid:
                return False
            continue
        if field == "tags":
            if value not in (t.get("tags") or []):
                return False
            continue
        if field == "created":
            created = t.get("created_at") or ""
            if op == ">" and not (created > value):
                return False
            if op == "<" and not (created < value):
                return False
            if op == ">=" and not _date_lte(value, created):
                return False
            if op == "<=" and not _date_lte(created, value):
                return False
            if op == ":" and not created.startswith(value):
                return False
            continue
        if field == "updated":
            updated = t.get("updated_at") or ""
            if op == ">" and not (updated > value):
                return False
            if op == "<" and not (updated < value):
                return False
            if op == ">=" and not _date_lte(value, updated):
                return False
            if op == "<=" and not _date_lte(updated, value):
                return False
            if op == ":" and not updated.startswith(value):
                return False
            continue
        # Unknown field — be permissive: free-text fallback on subject.
        if value.lower() not in (t.get("subject") or "").lower():
            return False
    return True


# ---------------------------------------------------------------------------
# Macro application
# ---------------------------------------------------------------------------

def _apply_macro_actions(state: dict, ticket: dict, macro: dict,
                          author_id: int) -> list[dict]:
    """Apply macro `actions` to ticket in-place. Returns the list of
    events for the audit."""
    events: list[dict] = []
    for action in macro.get("actions") or []:
        field = (action.get("field") or "").lower()
        value = action.get("value")
        if field == "status" and value in _VALID_STATUSES:
            old = ticket.get("status")
            ticket["status"] = value
            events.append({"type": "Change", "field_name": "status",
                           "previous_value": old, "value": value})
        elif field == "priority" and value in _VALID_PRIORITIES:
            old = ticket.get("priority")
            ticket["priority"] = value
            events.append({"type": "Change", "field_name": "priority",
                           "previous_value": old, "value": value})
        elif field == "type" and value in _VALID_TYPES:
            old = ticket.get("type")
            ticket["type"] = value
            events.append({"type": "Change", "field_name": "type",
                           "previous_value": old, "value": value})
        elif field == "current_tags":
            # Real macros split tags by spaces; mock accepts list or string.
            new_tags = (value if isinstance(value, list)
                        else (value or "").split())
            existing = list(ticket.get("tags") or [])
            for tg in new_tags:
                if tg and tg not in existing:
                    existing.append(tg)
            ticket["tags"] = existing
            events.append({"type": "Change", "field_name": "tags",
                           "value": existing})
        elif field == "remove_tags":
            rm_tags = (value if isinstance(value, list)
                       else (value or "").split())
            existing = [t for t in (ticket.get("tags") or [])
                        if t not in rm_tags]
            ticket["tags"] = existing
            events.append({"type": "Change", "field_name": "tags",
                           "value": existing})
        elif field == "assignee_id":
            uid = _resolve_user_ref(state, value)
            ticket["assignee_id"] = uid
            events.append({"type": "Change", "field_name": "assignee_id",
                           "value": uid})
        elif field == "group_id":
            try:
                gid = int(value) if value is not None else None
            except (TypeError, ValueError):
                gid = None
            ticket["group_id"] = gid
            events.append({"type": "Change", "field_name": "group_id",
                           "value": gid})
        elif field == "comment_value":
            body = value if isinstance(value, str) else (
                "\n".join(value) if isinstance(value, list) else "")
            if body:
                cid = _next_id(state, "comment")
                aid = _next_id(state, "audit")
                c = _new_comment(state, cid=cid, ticket_id=ticket["id"],
                                 author_id=author_id, body=body,
                                 public=True, audit_id=aid)
                state["comments"].setdefault(str(ticket["id"]), []).append(c)
                events.append({"type": "Comment", "id": cid,
                               "public": True, "body": body})
        elif field == "comment_value_html":
            body = value if isinstance(value, str) else ""
            if body:
                cid = _next_id(state, "comment")
                aid = _next_id(state, "audit")
                c = _new_comment(state, cid=cid, ticket_id=ticket["id"],
                                 author_id=author_id, body=body,
                                 html_body=body, public=True, audit_id=aid)
                state["comments"].setdefault(str(ticket["id"]), []).append(c)
                events.append({"type": "Comment", "id": cid,
                               "public": True, "html_body": body})
    return events


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("zendesk-mock")


# ===========================================================================
# Tickets
# ===========================================================================

@mcp.tool(name="list_tickets")
def list_tickets(page: int = 1, per_page: int = 100,
                 sort_by: str | None = None,
                 sort_order: str | None = None) -> dict:
    """Zendesk Support: GET /api/v2/tickets.json

    Returns a paginated list of all tickets visible to the caller.
    Envelope: `{tickets, next_page, previous_page, count}`. Pagination
    is page-based; `per_page` is capped at 100 (matching the real
    API). `sort_by` accepts `created_at`, `updated_at`, `priority`,
    `status`, `id`, `subject`, `ticket_type`."""
    with _lock():
        s = _load_state()
        rows = list(s["tickets"].values())
        rows = _sort_records(rows, sort_by, sort_order)
        base = f"{ZENDESK_BASE_URL}/api/v2/tickets.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_tickets", page=p["page"], per_page=p["per_page"],
                count=p["count"])
        _save_state(s)
        return {
            "tickets": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="get_ticket")
def get_ticket(ticketId: int) -> dict:
    """Zendesk Support: GET /api/v2/tickets/{id}.json

    Returns `{"ticket": {...}}` or a Zendesk-shaped 404 error
    `{"error": "RecordNotFound", "description": "Not found"}`."""
    with _lock():
        s = _load_state()
        t = s["tickets"].get(str(ticketId))
        _record(s, "get_ticket", ticketId=ticketId,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            return {"error": "RecordNotFound",
                    "description": f"Not found - ticket {ticketId}"}
        return {"ticket": t}


@mcp.tool(name="create_ticket")
def create_ticket(subject: str,
                  comment: dict | str | None = None,
                  requester_id: int | None = None,
                  assignee_id: int | None = None,
                  group_id: int | None = None,
                  priority: str | None = None,
                  status: str | None = None,
                  tags: list[str] | None = None,
                  type: str | None = None,
                  external_id: str | None = None,
                  custom_fields: list[dict] | None = None) -> dict:
    """Zendesk Support: POST /api/v2/tickets.json

    Request body wraps a `ticket` object. The mock accepts the
    ticket fields as positional kwargs to match the wrapped shape.
    `comment` is the initial public comment — either a plain string
    (auto-wrapped) or a dict like `{"body": "...", "public": True,
    "html_body": "..."}`. Returns `{"ticket": {...}, "audit": {...}}`
    matching the real API.

    Validation: priority must be one of low|normal|high|urgent;
    status must be one of new|open|pending|hold|solved|closed; type
    must be one of problem|incident|question|task. requester_id must
    exist (defaults to the authenticated bot if omitted).
    """
    with _lock():
        s = _load_state()
        if priority is not None and priority not in _VALID_PRIORITIES:
            _record(s, "create_ticket", result="invalid_priority")
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"priority must be one of "
                                    f"{list(_VALID_PRIORITIES)}")}
        if status is not None and status not in _VALID_STATUSES:
            _record(s, "create_ticket", result="invalid_status")
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"status must be one of "
                                    f"{list(_VALID_STATUSES)}")}
        if type is not None and type not in _VALID_TYPES:
            _record(s, "create_ticket", result="invalid_type")
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"type must be one of "
                                    f"{list(_VALID_TYPES)}")}
        # Resolve requester
        req_id = requester_id or s.get("self_user_id") or 1
        if str(req_id) not in s["users"]:
            _record(s, "create_ticket", result="invalid_requester",
                    requester_id=req_id)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"requester {req_id} does not exist"}
        if assignee_id is not None and str(assignee_id) not in s["users"]:
            _record(s, "create_ticket", result="invalid_assignee",
                    assignee_id=assignee_id)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"assignee {assignee_id} does not exist"}
        if group_id is not None and str(group_id) not in s["groups"]:
            _record(s, "create_ticket", result="invalid_group",
                    group_id=group_id)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"group {group_id} does not exist"}

        c = _normalize_comment_input(comment)
        description = (c or {}).get("body", "") if c else ""

        tid = _next_id(s, "ticket")
        now = _now()
        requester = s["users"][str(req_id)]
        org_id = requester.get("organization_id")

        t = _new_ticket(s, tid=tid, subject=subject or "",
                        description=description,
                        priority=priority,
                        status=status,
                        ttype=type,
                        requester_id=req_id,
                        submitter_id=s.get("self_user_id") or req_id,
                        assignee_id=assignee_id,
                        group_id=group_id,
                        organization_id=org_id,
                        tags=list(tags or []),
                        external_id=external_id,
                        custom_fields=list(custom_fields or []),
                        now=now)
        s["tickets"][str(tid)] = t

        aid = _next_id(s, "audit")
        events: list[dict] = [{"type": "Create", "field_name": "subject",
                               "value": t["subject"]}]
        if c:
            cid = _next_id(s, "comment")
            comment_obj = _new_comment(
                s, cid=cid, ticket_id=tid,
                author_id=c.get("author_id") or req_id,
                body=c["body"], html_body=c.get("html_body"),
                public=bool(c.get("public", True)),
                type_=c.get("type") or "Comment",
                audit_id=aid, now=now)
            s["comments"].setdefault(str(tid), []).append(comment_obj)
            events.append({"type": "Comment", "id": cid,
                           "public": comment_obj["public"],
                           "body": comment_obj["body"]})
        audit = _new_audit(s, aid=aid, ticket_id=tid,
                           author_id=s.get("self_user_id") or req_id,
                           events=events, now=now)
        s["audits"].setdefault(str(tid), []).append(audit)
        _record(s, "create_ticket", ticket_id=tid, subject=subject,
                requester_id=req_id, status=t["status"])
        _save_state(s)
        return {"ticket": t, "audit": audit}


@mcp.tool(name="update_ticket")
def update_ticket(ticketId: int,
                  subject: str | None = None,
                  comment: dict | str | None = None,
                  assignee_id: Any = "__unset__",
                  group_id: Any = "__unset__",
                  priority: str | None = None,
                  status: str | None = None,
                  type: str | None = None,
                  tags: list[str] | None = None,
                  additional_tags: list[str] | None = None,
                  remove_tags: list[str] | None = None,
                  custom_fields: list[dict] | None = None,
                  external_id: str | None = None) -> dict:
    """Zendesk Support: PUT /api/v2/tickets/{id}.json

    Request body wraps a `ticket` object. Supports overwrite (`tags`),
    additive (`additional_tags`), and subtractive (`remove_tags`)
    tag operations as documented. `comment` appends a comment when
    supplied. Returns `{"ticket": {...}, "audit": {...}}`.
    """
    with _lock():
        s = _load_state()
        t = s["tickets"].get(str(ticketId))
        if not t:
            _record(s, "update_ticket", ticketId=ticketId, result="not_found")
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - ticket {ticketId}"}
        events: list[dict] = []
        if priority is not None:
            if priority not in _VALID_PRIORITIES:
                _record(s, "update_ticket", ticketId=ticketId,
                        result="invalid_priority")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"priority must be one of "
                                        f"{list(_VALID_PRIORITIES)}")}
            events.append({"type": "Change", "field_name": "priority",
                           "previous_value": t.get("priority"),
                           "value": priority})
            t["priority"] = priority
        if status is not None:
            if status not in _VALID_STATUSES:
                _record(s, "update_ticket", ticketId=ticketId,
                        result="invalid_status")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"status must be one of "
                                        f"{list(_VALID_STATUSES)}")}
            events.append({"type": "Change", "field_name": "status",
                           "previous_value": t.get("status"),
                           "value": status})
            t["status"] = status
        if type is not None:
            if type not in _VALID_TYPES:
                _record(s, "update_ticket", ticketId=ticketId,
                        result="invalid_type")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"type must be one of "
                                        f"{list(_VALID_TYPES)}")}
            events.append({"type": "Change", "field_name": "type",
                           "previous_value": t.get("type"), "value": type})
            t["type"] = type
        if subject is not None:
            events.append({"type": "Change", "field_name": "subject",
                           "previous_value": t.get("subject"),
                           "value": subject})
            t["subject"] = subject
            t["raw_subject"] = subject
        if assignee_id != "__unset__":
            if assignee_id is not None and str(assignee_id) not in s["users"]:
                _record(s, "update_ticket", ticketId=ticketId,
                        result="invalid_assignee")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"assignee {assignee_id} does not "
                                        f"exist")}
            events.append({"type": "Change", "field_name": "assignee_id",
                           "previous_value": t.get("assignee_id"),
                           "value": assignee_id})
            t["assignee_id"] = assignee_id
        if group_id != "__unset__":
            if group_id is not None and str(group_id) not in s["groups"]:
                _record(s, "update_ticket", ticketId=ticketId,
                        result="invalid_group")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": f"group {group_id} does not exist"}
            events.append({"type": "Change", "field_name": "group_id",
                           "previous_value": t.get("group_id"),
                           "value": group_id})
            t["group_id"] = group_id
        # Tag operations
        if tags is not None:
            events.append({"type": "Change", "field_name": "tags",
                           "previous_value": list(t.get("tags") or []),
                           "value": list(tags)})
            t["tags"] = list(tags)
        if additional_tags:
            existing = list(t.get("tags") or [])
            for tg in additional_tags:
                if tg and tg not in existing:
                    existing.append(tg)
            events.append({"type": "Change", "field_name": "tags",
                           "value": existing})
            t["tags"] = existing
        if remove_tags:
            existing = [tg for tg in (t.get("tags") or [])
                        if tg not in remove_tags]
            events.append({"type": "Change", "field_name": "tags",
                           "value": existing})
            t["tags"] = existing
        if custom_fields is not None:
            t["custom_fields"] = list(custom_fields)
            t["fields"] = list(custom_fields)
            events.append({"type": "Change", "field_name": "custom_fields",
                           "value": list(custom_fields)})
        if external_id is not None:
            events.append({"type": "Change", "field_name": "external_id",
                           "previous_value": t.get("external_id"),
                           "value": external_id})
            t["external_id"] = external_id

        now = _now()
        aid = _next_id(s, "audit")
        # Append comment if supplied
        c = _normalize_comment_input(comment)
        if c and c.get("body"):
            cid = _next_id(s, "comment")
            author_id = (c.get("author_id")
                         or s.get("self_user_id")
                         or t.get("requester_id"))
            comment_obj = _new_comment(
                s, cid=cid, ticket_id=ticketId, author_id=author_id,
                body=c["body"], html_body=c.get("html_body"),
                public=bool(c.get("public", True)),
                type_=c.get("type") or "Comment",
                audit_id=aid, now=now)
            s["comments"].setdefault(str(ticketId), []).append(comment_obj)
            events.append({"type": "Comment", "id": cid,
                           "public": comment_obj["public"],
                           "body": comment_obj["body"]})

        t["updated_at"] = now
        audit = _new_audit(s, aid=aid, ticket_id=ticketId,
                           author_id=s.get("self_user_id")
                                     or t.get("requester_id"),
                           events=events, now=now)
        s["audits"].setdefault(str(ticketId), []).append(audit)
        _record(s, "update_ticket", ticketId=ticketId,
                event_fields=[e.get("field_name") for e in events
                              if "field_name" in e])
        _save_state(s)
        return {"ticket": t, "audit": audit}


@mcp.tool(name="delete_ticket")
def delete_ticket(ticketId: int) -> dict:
    """Zendesk Support: DELETE /api/v2/tickets/{id}.json

    Permanently deletes the ticket and its comments. Real API
    returns 204 No Content; the mock returns an empty dict on
    success.
    """
    with _lock():
        s = _load_state()
        if str(ticketId) not in s["tickets"]:
            _record(s, "delete_ticket", ticketId=ticketId,
                    result="not_found")
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - ticket {ticketId}"}
        del s["tickets"][str(ticketId)]
        s["comments"].pop(str(ticketId), None)
        s["audits"].pop(str(ticketId), None)
        _record(s, "delete_ticket", ticketId=ticketId)
        _save_state(s)
        return {}


@mcp.tool(name="list_ticket_comments")
def list_ticket_comments(ticketId: int,
                         page: int = 1, per_page: int = 100,
                         sort_order: str | None = None) -> dict:
    """Zendesk Support: GET /api/v2/tickets/{id}/comments.json

    Returns `{comments, next_page, previous_page, count}`. Comments
    are ordered by `created_at` ascending by default.
    """
    with _lock():
        s = _load_state()
        if str(ticketId) not in s["tickets"]:
            _record(s, "list_ticket_comments", ticketId=ticketId,
                    result="not_found")
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - ticket {ticketId}"}
        rows = list(s["comments"].get(str(ticketId), []))
        rev = (sort_order or "asc").lower() == "desc"
        rows.sort(key=lambda c: c.get("created_at") or "", reverse=rev)
        base = (f"{ZENDESK_BASE_URL}/api/v2/tickets/{ticketId}/"
                f"comments.json")
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_ticket_comments", ticketId=ticketId,
                count=p["count"])
        _save_state(s)
        return {
            "comments": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="search_tickets")
def search_tickets(query: str = "type:ticket",
                   sort_by: str | None = None,
                   sort_order: str | None = None,
                   page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/search.json?query=...

    Implements the Support search syntax (subset): `type:ticket`,
    `status:<v>`, `priority:<v>`, `assignee:none|<id>|<email>`,
    `requester:<id>|<email>`, `tags:<tag>`, `group:<id>`,
    `created>YYYY-MM-DD` / `created<YYYY-MM-DD` (also `>=`, `<=`,
    `:` for prefix), `updated>...`, and free-text terms (matched
    against subject and description). Multiple terms AND-combine.
    Returns `{results, facets, next_page, previous_page, count}`.
    """
    with _lock():
        s = _load_state()
        clauses = _parse_query(query or "")
        # If `type:ticket` not specified, default to ticket search.
        rows = list(s["tickets"].values())
        rows = [t for t in rows
                if _ticket_matches_query(s, t, clauses)]
        rows = _sort_records(rows, sort_by, sort_order)
        base = (f"{ZENDESK_BASE_URL}/api/v2/search.json?"
                f"query={query or ''}")
        p = _paginate(rows, page, per_page, base)
        # Real Zendesk returns each result with a `result_type` flag.
        results = [{**t, "result_type": "ticket"} for t in p["items"]]
        _record(s, "search_tickets", query=query,
                count=p["count"])
        _save_state(s)
        return {
            "results": results,
            "facets": None,
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="incremental_tickets")
def incremental_tickets(startTime: int = 0,
                        per_page: int = 1000) -> dict:
    """Zendesk Support: GET /api/v2/incremental/tickets.json?start_time=N

    Returns tickets updated after the Unix epoch `start_time`,
    sorted by `updated_at` ascending. Envelope mirrors the real
    incremental export endpoint:
      `{tickets, next_page, end_time, after_url, after_cursor,
        before_cursor, count, end_of_stream}`.
    """
    with _lock():
        s = _load_state()
        per_page = max(1, min(int(per_page or 1000), 1000))
        try:
            start = int(startTime or 0)
        except (TypeError, ValueError):
            start = 0
        start_iso = (datetime.datetime.fromtimestamp(
            start, tz=datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
        rows = [t for t in s["tickets"].values()
                if (t.get("updated_at") or "") > start_iso]
        rows.sort(key=lambda t: t.get("updated_at") or "")
        page = rows[:per_page]
        end_iso = (page[-1].get("updated_at") if page else start_iso)
        end_ts = int(datetime.datetime.strptime(
            end_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp())
        end_of_stream = len(rows) <= per_page
        after_url = (None if end_of_stream else
                     (f"{ZENDESK_BASE_URL}/api/v2/incremental/"
                      f"tickets.json?start_time={end_ts}"))
        _record(s, "incremental_tickets", start_time=start,
                count=len(page), end_of_stream=end_of_stream)
        _save_state(s)
        return {
            "tickets": page,
            "next_page": after_url,
            "end_time": end_ts,
            "after_url": after_url,
            "after_cursor": str(end_ts) if after_url else None,
            "before_cursor": str(start) if start else None,
            "count": len(page),
            "end_of_stream": end_of_stream,
        }


# ===========================================================================
# Users
# ===========================================================================

@mcp.tool(name="list_users")
def list_users(role: str | None = None,
               page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/users.json

    Returns `{users, next_page, previous_page, count}`. Optional
    `role` filter: `end-user`, `agent`, or `admin`.
    """
    with _lock():
        s = _load_state()
        rows = list(s["users"].values())
        if role:
            if role not in _VALID_ROLES:
                _record(s, "list_users", result="invalid_role", role=role)
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"role must be one of "
                                        f"{list(_VALID_ROLES)}")}
            rows = [u for u in rows if u.get("role") == role]
        rows.sort(key=lambda u: u.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/users.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_users", count=p["count"], role=role)
        _save_state(s)
        return {
            "users": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="get_user")
def get_user(userId: int) -> dict:
    """Zendesk Support: GET /api/v2/users/{id}.json

    Returns `{"user": {...}}` or a 404 error envelope.
    """
    with _lock():
        s = _load_state()
        u = s["users"].get(str(userId))
        _record(s, "get_user", userId=userId,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return {"error": "RecordNotFound",
                    "description": f"Not found - user {userId}"}
        return {"user": u}


@mcp.tool(name="create_user")
def create_user(name: str,
                email: str | None = None,
                role: str = "end-user",
                phone: str | None = None,
                organization_id: int | None = None,
                tags: list[str] | None = None,
                verified: bool = False,
                external_id: str | None = None) -> dict:
    """Zendesk Support: POST /api/v2/users.json

    Request body wraps a `user` object. Returns `{"user": {...}}`.
    Validates `role` (end-user|agent|admin) and rejects duplicate
    emails — the real API responds 422 `RecordInvalid` if a verified
    email is already in use.
    """
    with _lock():
        s = _load_state()
        if role not in _VALID_ROLES:
            _record(s, "create_user", result="invalid_role")
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"role must be one of "
                                    f"{list(_VALID_ROLES)}")}
        if email and _user_by_email(s, email):
            _record(s, "create_user", result="duplicate_email", email=email)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"email {email} already in use"}
        if (organization_id is not None
                and str(organization_id) not in s["organizations"]):
            _record(s, "create_user", result="invalid_org",
                    organization_id=organization_id)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"organization {organization_id} does "
                                    f"not exist")}
        uid = _next_id(s, "user")
        now = _now()
        user = {
            "id": uid,
            "url": _user_url(uid),
            "name": name,
            "email": email,
            "role": role,
            "verified": bool(verified),
            "active": True,
            "organization_id": organization_id,
            "phone": phone,
            "tags": list(tags or []),
            "external_id": external_id,
            "time_zone": "UTC",
            "iana_time_zone": "Etc/UTC",
            "locale": "en-US",
            "locale_id": 1,
            "alias": None,
            "details": "",
            "notes": "",
            "signature": None,
            "moderator": False,
            "ticket_restriction": None,
            "only_private_comments": False,
            "restricted_agent": role == "end-user",
            "suspended": False,
            "default_group_id": None,
            "shared": False,
            "shared_agent": False,
            "last_login_at": None,
            "two_factor_auth_enabled": False,
            "user_fields": {},
            "photo": None,
            "shared_phone_number": None,
            "report_csv": False,
            "role_type": None,
            "custom_role_id": None,
            "created_at": now,
            "updated_at": now,
        }
        s["users"][str(uid)] = user
        _record(s, "create_user", user_id=uid, role=role, email=email)
        _save_state(s)
        return {"user": user}


@mcp.tool(name="update_user")
def update_user(userId: int,
                name: str | None = None,
                email: str | None = None,
                role: str | None = None,
                phone: str | None = None,
                organization_id: Any = "__unset__",
                tags: list[str] | None = None,
                verified: bool | None = None,
                external_id: str | None = None) -> dict:
    """Zendesk Support: PUT /api/v2/users/{id}.json

    Returns `{"user": {...}}` reflecting the updated record.
    """
    with _lock():
        s = _load_state()
        u = s["users"].get(str(userId))
        if not u:
            _record(s, "update_user", userId=userId, result="not_found")
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - user {userId}"}
        if role is not None and role not in _VALID_ROLES:
            _record(s, "update_user", userId=userId, result="invalid_role")
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": (f"role must be one of "
                                    f"{list(_VALID_ROLES)}")}
        if name is not None:
            u["name"] = name
        if email is not None:
            other = _user_by_email(s, email)
            if other and other.get("id") != userId:
                _record(s, "update_user", userId=userId,
                        result="duplicate_email")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": f"email {email} already in use"}
            u["email"] = email
        if role is not None:
            u["role"] = role
        if phone is not None:
            u["phone"] = phone
        if organization_id != "__unset__":
            if (organization_id is not None
                    and str(organization_id) not in s["organizations"]):
                _record(s, "update_user", userId=userId,
                        result="invalid_org")
                _save_state(s)
                return {"error": "RecordInvalid",
                        "description": (f"organization {organization_id} "
                                        f"does not exist")}
            u["organization_id"] = organization_id
        if tags is not None:
            u["tags"] = list(tags)
        if verified is not None:
            u["verified"] = bool(verified)
        if external_id is not None:
            u["external_id"] = external_id
        u["updated_at"] = _now()
        _record(s, "update_user", userId=userId)
        _save_state(s)
        return {"user": u}


@mcp.tool(name="search_users")
def search_users(query: str = "",
                 page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/users/search.json?query=...

    Substring match against name and email (case-insensitive).
    Returns `{users, next_page, previous_page, count}`.
    """
    with _lock():
        s = _load_state()
        q = (query or "").lower().strip()
        rows = list(s["users"].values())
        if q:
            rows = [u for u in rows
                    if q in (u.get("name") or "").lower()
                    or q in (u.get("email") or "").lower()]
        rows.sort(key=lambda u: u.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/users/search.json?query={query}"
        p = _paginate(rows, page, per_page, base)
        _record(s, "search_users", query=query, count=p["count"])
        _save_state(s)
        return {
            "users": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


# ===========================================================================
# Organizations
# ===========================================================================

@mcp.tool(name="list_organizations")
def list_organizations(page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/organizations.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["organizations"].values(),
                      key=lambda o: o.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/organizations.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_organizations", count=p["count"])
        _save_state(s)
        return {
            "organizations": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="get_organization")
def get_organization(organizationId: int) -> dict:
    """Zendesk Support: GET /api/v2/organizations/{id}.json"""
    with _lock():
        s = _load_state()
        o = s["organizations"].get(str(organizationId))
        _record(s, "get_organization", organizationId=organizationId,
                result="ok" if o else "not_found")
        _save_state(s)
        if not o:
            return {"error": "RecordNotFound",
                    "description": (f"Not found - organization "
                                    f"{organizationId}")}
        return {"organization": o}


@mcp.tool(name="create_organization")
def create_organization(name: str,
                        tags: list[str] | None = None,
                        domain_names: list[str] | None = None,
                        details: str | None = None,
                        notes: str | None = None,
                        external_id: str | None = None) -> dict:
    """Zendesk Support: POST /api/v2/organizations.json

    Returns `{"organization": {...}}`.
    """
    with _lock():
        s = _load_state()
        oid = _next_id(s, "organization")
        now = _now()
        org = {
            "id": oid,
            "url": _org_url(oid),
            "name": name,
            "domain_names": list(domain_names or []),
            "details": details or "",
            "notes": notes or "",
            "group_id": None,
            "shared_tickets": False,
            "shared_comments": False,
            "tags": list(tags or []),
            "organization_fields": {},
            "external_id": external_id,
            "created_at": now,
            "updated_at": now,
        }
        s["organizations"][str(oid)] = org
        _record(s, "create_organization", organization_id=oid, name=name)
        _save_state(s)
        return {"organization": org}


# ===========================================================================
# Groups + memberships
# ===========================================================================

@mcp.tool(name="list_groups")
def list_groups(page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/groups.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["groups"].values(),
                      key=lambda g: g.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/groups.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_groups", count=p["count"])
        _save_state(s)
        return {
            "groups": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="create_group")
def create_group(name: str, description: str | None = None) -> dict:
    """Zendesk Support: POST /api/v2/groups.json

    Returns `{"group": {...}}`.
    """
    with _lock():
        s = _load_state()
        gid = _next_id(s, "group")
        now = _now()
        g = {
            "id": gid,
            "url": _group_url(gid),
            "name": name,
            "description": description or "",
            "default": False,
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        s["groups"][str(gid)] = g
        _record(s, "create_group", group_id=gid, name=name)
        _save_state(s)
        return {"group": g}


@mcp.tool(name="add_user_to_group")
def add_user_to_group(userId: int, groupId: int) -> dict:
    """Zendesk Support: POST /api/v2/group_memberships.json

    Returns `{"group_membership": {...}}`. The real endpoint takes a
    body `{"group_membership": {"user_id": ..., "group_id": ...}}`;
    the mock lifts user_id/group_id to top-level params for ergonomics.
    """
    with _lock():
        s = _load_state()
        if str(userId) not in s["users"]:
            _record(s, "add_user_to_group", result="user_not_found",
                    userId=userId)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"user {userId} does not exist"}
        if str(groupId) not in s["groups"]:
            _record(s, "add_user_to_group", result="group_not_found",
                    groupId=groupId)
            _save_state(s)
            return {"error": "RecordInvalid",
                    "description": f"group {groupId} does not exist"}
        mid = _next_id(s, "group_membership")
        now = _now()
        membership = {
            "id": mid,
            "user_id": userId,
            "group_id": groupId,
            "default": False,
            "created_at": now,
            "updated_at": now,
            "url": (f"{ZENDESK_BASE_URL}/api/v2/group_memberships/"
                    f"{mid}.json"),
        }
        s["group_memberships"][str(mid)] = membership
        _record(s, "add_user_to_group", membership_id=mid,
                userId=userId, groupId=groupId)
        _save_state(s)
        return {"group_membership": membership}


# ===========================================================================
# Macros
# ===========================================================================

@mcp.tool(name="list_macros")
def list_macros(page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/macros.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["macros"].values(),
                      key=lambda m: m.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/macros.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_macros", count=p["count"])
        _save_state(s)
        return {
            "macros": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="apply_macro_to_ticket")
def apply_macro_to_ticket(ticketId: int, macroId: int) -> dict:
    """Convenience tool (not a 1:1 Zendesk endpoint): apply a macro's
    actions to a ticket and create the audit + any comment the macro
    embeds. Returns `{"ticket": {...}, "audit": {...},
    "applied_actions": [...]}`.

    The real Zendesk surface has two related endpoints — GET
    `/api/v2/macros/{id}/apply.json` (preview only, no mutation) and
    the macro-applied UI path which calls the standard ticket
    PUT with the macro's actions. The mock wraps both into one tool
    that actually mutates the ticket.
    """
    with _lock():
        s = _load_state()
        t = s["tickets"].get(str(ticketId))
        if not t:
            _record(s, "apply_macro_to_ticket", result="ticket_not_found",
                    ticketId=ticketId)
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - ticket {ticketId}"}
        m = s["macros"].get(str(macroId))
        if not m:
            _record(s, "apply_macro_to_ticket", result="macro_not_found",
                    macroId=macroId)
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - macro {macroId}"}
        author_id = s.get("self_user_id") or t.get("requester_id") or 1
        now = _now()
        events = _apply_macro_actions(s, t, m, author_id=author_id)
        t["updated_at"] = now
        aid = _next_id(s, "audit")
        audit = _new_audit(s, aid=aid, ticket_id=ticketId,
                           author_id=author_id, events=events, now=now)
        s["audits"].setdefault(str(ticketId), []).append(audit)
        _record(s, "apply_macro_to_ticket", ticketId=ticketId,
                macroId=macroId, applied=len(events))
        _save_state(s)
        return {"ticket": t, "audit": audit,
                "applied_actions": list(m.get("actions") or [])}


# ===========================================================================
# Misc
# ===========================================================================

@mcp.tool(name="list_satisfaction_ratings")
def list_satisfaction_ratings(page: int = 1, per_page: int = 100) -> dict:
    """Zendesk Support: GET /api/v2/satisfaction_ratings.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["satisfaction_ratings"].values(),
                      key=lambda r: r.get("id", 0))
        base = f"{ZENDESK_BASE_URL}/api/v2/satisfaction_ratings.json"
        p = _paginate(rows, page, per_page, base)
        _record(s, "list_satisfaction_ratings", count=p["count"])
        _save_state(s)
        return {
            "satisfaction_ratings": p["items"],
            "next_page": p["next_page"],
            "previous_page": p["previous_page"],
            "count": p["count"],
        }


@mcp.tool(name="list_ticket_fields")
def list_ticket_fields() -> dict:
    """Zendesk Support: GET /api/v2/ticket_fields.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["ticket_fields"].values(),
                      key=lambda f: f.get("id", 0))
        _record(s, "list_ticket_fields", count=len(rows))
        _save_state(s)
        return {"ticket_fields": rows}


@mcp.tool(name="list_views")
def list_views() -> dict:
    """Zendesk Support: GET /api/v2/views.json"""
    with _lock():
        s = _load_state()
        rows = sorted(s["views"].values(),
                      key=lambda v: v.get("id", 0))
        _record(s, "list_views", count=len(rows))
        _save_state(s)
        return {"views": rows}


@mcp.tool(name="count_tickets_in_view")
def count_tickets_in_view(viewId: int) -> dict:
    """Zendesk Support: GET /api/v2/views/{id}/count.json

    Returns `{"view_count": {"view_id", "url", "value", "pretty",
    "fresh"}}`. The mock counts tickets whose stored fields satisfy
    each clause in the view's `conditions.all` list (field + operator
    + value, mirroring the public condition DSL: `status`, `priority`,
    `assignee_id`, `group_id`, `tags`).
    """
    with _lock():
        s = _load_state()
        v = s["views"].get(str(viewId))
        if not v:
            _record(s, "count_tickets_in_view", viewId=viewId,
                    result="not_found")
            _save_state(s)
            return {"error": "RecordNotFound",
                    "description": f"Not found - view {viewId}"}
        conditions = v.get("conditions") or {}
        all_cl = conditions.get("all") or []

        def _match(t: dict) -> bool:
            for c in all_cl:
                field = (c.get("field") or "").lower()
                op = (c.get("operator") or "is").lower()
                value = c.get("value")
                if field == "status":
                    if op == "is" and t.get("status") != value:
                        return False
                    if op == "is_not" and t.get("status") == value:
                        return False
                elif field == "priority":
                    if op == "is" and t.get("priority") != value:
                        return False
                    if op == "is_not" and t.get("priority") == value:
                        return False
                elif field in ("assignee_id", "group_id"):
                    try:
                        target = int(value) if value is not None else None
                    except (TypeError, ValueError):
                        target = None
                    if op == "is" and t.get(field) != target:
                        return False
                    if op == "is_not" and t.get(field) == target:
                        return False
                elif field == "current_tags":
                    if op == "includes" and value not in (t.get("tags") or []):
                        return False
                    if op == "not_includes" and value in (t.get("tags") or []):
                        return False
            return True

        n = sum(1 for t in s["tickets"].values() if _match(t))
        out = {
            "view_count": {
                "view_id": viewId,
                "url": (f"{ZENDESK_BASE_URL}/api/v2/views/{viewId}/"
                        f"count.json"),
                "value": n,
                "pretty": str(n),
                "fresh": True,
            }
        }
        _record(s, "count_tickets_in_view", viewId=viewId, count=n)
        _save_state(s)
        return out


# ===========================================================================
# Mock-only debug tools
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection). Not part of the real Zendesk surface."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_ticket")
def mock_debug_seed_ticket(subject: str,
                           description: str = "",
                           id: int | None = None,
                           status: str = "open",
                           priority: str | None = None,
                           type: str | None = None,
                           requester_id: int | None = None,
                           assignee_id: int | None = None,
                           group_id: int | None = None,
                           organization_id: int | None = None,
                           tags: list[str] | None = None,
                           external_id: str | None = None,
                           custom_fields: list[dict] | None = None,
                           comments: list[dict] | None = None,
                           created_at: str | None = None,
                           updated_at: str | None = None) -> dict:
    """Mock-only: seed one fully-formed ticket (+ optional comments).
    `comments` is a list of `{body, public?, author_id?, type?,
    html_body?}` dicts; they are appended in order."""
    with _lock():
        s = _load_state()
        tid = id if id is not None else _next_id(s, "ticket")
        # advance next_id to stay strictly past tid
        if tid >= s["next_id"]["ticket"]:
            s["next_id"]["ticket"] = tid + 1
        req_id = requester_id or s.get("self_user_id") or 1
        now = created_at or _now()
        t = _new_ticket(s, tid=tid, subject=subject,
                        description=description,
                        priority=priority,
                        status=status,
                        ttype=type,
                        requester_id=req_id,
                        submitter_id=req_id,
                        assignee_id=assignee_id,
                        group_id=group_id,
                        organization_id=organization_id,
                        tags=list(tags or []),
                        external_id=external_id,
                        custom_fields=list(custom_fields or []),
                        now=now)
        if updated_at:
            t["updated_at"] = updated_at
        s["tickets"][str(tid)] = t
        for c in comments or []:
            cid = _next_id(s, "comment")
            aid = _next_id(s, "audit")
            author_id = c.get("author_id") or req_id
            cm = _new_comment(s, cid=cid, ticket_id=tid,
                              author_id=author_id,
                              body=c.get("body", ""),
                              html_body=c.get("html_body"),
                              public=bool(c.get("public", True)),
                              type_=c.get("type") or "Comment",
                              audit_id=aid, now=c.get("created_at") or now)
            s["comments"].setdefault(str(tid), []).append(cm)
        _record(s, "debug_seed_ticket", ticket_id=tid,
                n_comments=len(comments or []))
        _save_state(s)
        return t


@mcp.tool(name="mock_debug_seed_user")
def mock_debug_seed_user(name: str,
                         email: str | None = None,
                         id: int | None = None,
                         role: str = "end-user",
                         organization_id: int | None = None,
                         phone: str | None = None,
                         tags: list[str] | None = None,
                         verified: bool = False,
                         external_id: str | None = None) -> dict:
    """Mock-only: seed one user fixture."""
    with _lock():
        s = _load_state()
        uid = id if id is not None else _next_id(s, "user")
        if uid >= s["next_id"]["user"]:
            s["next_id"]["user"] = uid + 1
        now = _now()
        u = {
            "id": uid,
            "url": _user_url(uid),
            "name": name,
            "email": email,
            "role": role,
            "verified": bool(verified),
            "active": True,
            "organization_id": organization_id,
            "phone": phone,
            "tags": list(tags or []),
            "external_id": external_id,
            "time_zone": "UTC",
            "iana_time_zone": "Etc/UTC",
            "locale": "en-US",
            "locale_id": 1,
            "alias": None,
            "details": "",
            "notes": "",
            "signature": None,
            "moderator": False,
            "ticket_restriction": None,
            "only_private_comments": False,
            "restricted_agent": role == "end-user",
            "suspended": False,
            "default_group_id": None,
            "shared": False,
            "shared_agent": False,
            "last_login_at": None,
            "two_factor_auth_enabled": False,
            "user_fields": {},
            "photo": None,
            "shared_phone_number": None,
            "report_csv": False,
            "role_type": None,
            "custom_role_id": None,
            "created_at": now,
            "updated_at": now,
        }
        s["users"][str(uid)] = u
        _record(s, "debug_seed_user", user_id=uid, role=role)
        _save_state(s)
        return u


@mcp.tool(name="mock_debug_seed_organization")
def mock_debug_seed_organization(name: str,
                                 id: int | None = None,
                                 domain_names: list[str] | None = None,
                                 tags: list[str] | None = None,
                                 details: str = "",
                                 notes: str = "",
                                 external_id: str | None = None) -> dict:
    """Mock-only: seed one organization fixture."""
    with _lock():
        s = _load_state()
        oid = id if id is not None else _next_id(s, "organization")
        if oid >= s["next_id"]["organization"]:
            s["next_id"]["organization"] = oid + 1
        now = _now()
        org = {
            "id": oid,
            "url": _org_url(oid),
            "name": name,
            "domain_names": list(domain_names or []),
            "details": details,
            "notes": notes,
            "group_id": None,
            "shared_tickets": False,
            "shared_comments": False,
            "tags": list(tags or []),
            "organization_fields": {},
            "external_id": external_id,
            "created_at": now,
            "updated_at": now,
        }
        s["organizations"][str(oid)] = org
        _record(s, "debug_seed_organization", organization_id=oid)
        _save_state(s)
        return org


@mcp.tool(name="mock_debug_seed_group")
def mock_debug_seed_group(name: str,
                          id: int | None = None,
                          description: str = "",
                          default: bool = False) -> dict:
    """Mock-only: seed one group fixture."""
    with _lock():
        s = _load_state()
        gid = id if id is not None else _next_id(s, "group")
        if gid >= s["next_id"]["group"]:
            s["next_id"]["group"] = gid + 1
        now = _now()
        g = {
            "id": gid,
            "url": _group_url(gid),
            "name": name,
            "description": description,
            "default": bool(default),
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        s["groups"][str(gid)] = g
        _record(s, "debug_seed_group", group_id=gid)
        _save_state(s)
        return g


@mcp.tool(name="mock_debug_seed_macro")
def mock_debug_seed_macro(title: str,
                          actions: list[dict],
                          id: int | None = None,
                          description: str | None = None,
                          active: bool = True) -> dict:
    """Mock-only: seed one macro fixture.

    `actions` is a list of `{"field": "<name>", "value": <value>}`
    using the Zendesk macro action vocabulary (status, priority,
    type, current_tags, remove_tags, assignee_id, group_id,
    comment_value, comment_value_html)."""
    with _lock():
        s = _load_state()
        mid = id if id is not None else _next_id(s, "macro")
        if mid >= s["next_id"]["macro"]:
            s["next_id"]["macro"] = mid + 1
        now = _now()
        m = {
            "id": mid,
            "url": (f"{ZENDESK_BASE_URL}/api/v2/macros/{mid}.json"),
            "title": title,
            "active": bool(active),
            "actions": list(actions or []),
            "description": description,
            "position": mid,
            "restriction": None,
            "created_at": now,
            "updated_at": now,
        }
        s["macros"][str(mid)] = m
        _record(s, "debug_seed_macro", macro_id=mid)
        _save_state(s)
        return m


@mcp.tool(name="mock_debug_seed_view")
def mock_debug_seed_view(title: str,
                         conditions: dict | None = None,
                         id: int | None = None,
                         description: str | None = None,
                         active: bool = True) -> dict:
    """Mock-only: seed one view fixture.

    `conditions` shape mirrors Zendesk Support: `{"all": [{"field",
    "operator", "value"}, ...], "any": [...]}`. The mock honours
    `all` clauses in `count_tickets_in_view`."""
    with _lock():
        s = _load_state()
        vid = id if id is not None else _next_id(s, "view")
        if vid >= s["next_id"]["view"]:
            s["next_id"]["view"] = vid + 1
        now = _now()
        v = {
            "id": vid,
            "url": (f"{ZENDESK_BASE_URL}/api/v2/views/{vid}.json"),
            "title": title,
            "description": description,
            "active": bool(active),
            "conditions": conditions or {"all": [], "any": []},
            "execution": {"columns": [], "group_by": None,
                          "group_order": "asc", "sort_by": "created_at",
                          "sort_order": "desc"},
            "position": vid,
            "restriction": None,
            "watchable": False,
            "created_at": now,
            "updated_at": now,
        }
        s["views"][str(vid)] = v
        _record(s, "debug_seed_view", view_id=vid)
        _save_state(s)
        return v


if __name__ == "__main__":
    mcp.run()
