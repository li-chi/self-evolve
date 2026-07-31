"""Mailgun mock MCP server.

Mirrors the Mailgun API v3 REST surface
(documentation.mailgun.com/docs/mailgun/api-reference/). Tools are
named after the REST operations; parameter names and response shapes
match what the real Mailgun HTTP API returns, so the mock is a
drop-in stand-in for a Mailgun MCP/REST client during rollouts. We
deliberately do NOT wrap the existing mailgun-integration CLI mock;
this is the JSON-shape REST surface.

Tools implemented:

  Messages
    send_message            (POST /v3/{domain}/messages)
    retrieve_stored_message (GET  /v3/domains/{domain}/messages/{storage_key})

  Domains
    list_domains            (GET    /v4/domains)
    get_domain              (GET    /v4/domains/{name})
    create_domain           (POST   /v4/domains)
    delete_domain           (DELETE /v3/domains/{name})

  Mailing lists
    list_mailing_lists      (GET    /v3/lists/pages)
    create_mailing_list     (POST   /v3/lists)
    list_list_members       (GET    /v3/lists/{address}/members/pages)
    add_list_member         (POST   /v3/lists/{address}/members)
    remove_list_member      (DELETE /v3/lists/{address}/members/{member})

  Events / stats
    list_events             (GET /v3/{domain}/events)
    get_stats               (GET /v3/{domain}/stats/total)

  Suppressions
    list_bounces            (GET    /v3/{domain}/bounces)
    add_bounce              (POST   /v3/{domain}/bounces)
    delete_bounce           (DELETE /v3/{domain}/bounces/{address})
    list_unsubscribes       (GET    /v3/{domain}/unsubscribes)
    list_complaints         (GET    /v3/{domain}/complaints)

  Tags
    list_tags               (GET /v3/{domain}/tags)
    get_tag                 (GET /v3/{domain}/tags/{tag})

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.

State lives at `$MAILGUN_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/mailgun_mock`). Optional `MAILGUN_MOCK_SEED_PATH`
preloads state when no state.json exists yet.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "MAILGUN_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/mailgun_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _now_rfc2822() -> str:
    # Mailgun's events use RFC2822-ish "Day, DD Mon YYYY HH:MM:SS +0000"
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )


def _now_unix() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _empty_state() -> dict:
    return {
        # Per-domain catalog: bounces/unsubscribes/complaints/events/tags/
        # stored messages live under each domain entry.
        "domains": {},
        # Mailing lists keyed by list address (e.g. "devs@example.com")
        "mailing_lists": {},
        # Global stored messages by storage key for retrieve_stored_message
        "stored_messages": {},
        # Counter for deterministic-ish message ids
        "next_id": {"message": 1, "event": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("MAILGUN_MOCK_SEED_PATH")
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

def _new_message_id(state: dict, domain: str) -> str:
    """Mailgun message ids look like
    `<20231101120000.abc123def456@example.com>` (RFC822 Message-ID)."""
    n = state["next_id"]["message"]
    state["next_id"]["message"] = n + 1
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%d%H%M%S"
    )
    rand = secrets.token_hex(6)
    return f"<{stamp}.{rand}@{domain or 'mock.mailgun.org'}>"


def _new_event_id() -> str:
    return secrets.token_hex(12)


def _storage_key(message_id: str) -> str:
    """Storage keys are opaque base64-ish blobs in Mailgun; mimic with a
    hex digest of the message id (matches `event.message.storage.key`)."""
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:48]


def _ensure_domain(state: dict, name: str) -> dict:
    d = state["domains"].get(name)
    if d is None:
        d = {
            "name": name,
            "created_at": _now_rfc2822(),
            "state": "active",
            "type": "custom",
            "smtp_login": f"postmaster@{name}",
            "smtp_password": secrets.token_hex(8),
            "spam_action": "disabled",
            "wildcard": False,
            "require_tls": False,
            "skip_verification": False,
            "web_prefix": "email",
            "web_scheme": "https",
            "bounces": {},          # address -> bounce record
            "unsubscribes": {},     # address -> unsubscribe record
            "complaints": {},       # address -> complaint record
            "events": [],           # newest-first list of event dicts
            "tags": {},             # tag -> {tag, description, first_seen, last_seen}
            "stored_messages": {},  # storage_key -> stored message blob
            "receiving_dns_records": [
                {"record_type": "MX", "priority": "10",
                 "value": f"mxa.mailgun.org", "valid": "valid"},
                {"record_type": "MX", "priority": "10",
                 "value": f"mxb.mailgun.org", "valid": "valid"},
            ],
            "sending_dns_records": [
                {"name": name, "record_type": "TXT",
                 "value": "v=spf1 include:mailgun.org ~all", "valid": "valid"},
                {"name": f"smtp._domainkey.{name}", "record_type": "TXT",
                 "value": "k=rsa; p=MOCKDKIMKEY", "valid": "valid"},
                {"name": f"email.{name}", "record_type": "CNAME",
                 "value": "mailgun.org", "valid": "valid"},
            ],
        }
        state["domains"][name] = d
    return d


def _domain_summary(d: dict) -> dict:
    """Shape returned inside `items` for list_domains and as `domain`
    inside get_domain."""
    return {
        "name": d["name"],
        "created_at": d.get("created_at", _now_rfc2822()),
        "state": d.get("state", "active"),
        "type": d.get("type", "custom"),
        "smtp_login": d.get("smtp_login", f"postmaster@{d['name']}"),
        "smtp_password": d.get("smtp_password", ""),
        "spam_action": d.get("spam_action", "disabled"),
        "wildcard": d.get("wildcard", False),
        "require_tls": d.get("require_tls", False),
        "skip_verification": d.get("skip_verification", False),
        "web_prefix": d.get("web_prefix", "email"),
        "web_scheme": d.get("web_scheme", "https"),
        "is_disabled": d.get("state") == "disabled",
    }


def _paginate_items(items: list, limit: int, skip: int) -> list:
    if limit <= 0:
        limit = 100
    if limit > 1000:
        limit = 1000
    if skip < 0:
        skip = 0
    return items[skip: skip + limit]


def _paging_urls(base: str, total: int, limit: int, skip: int) -> dict:
    """Mailgun list endpoints return a `paging` object with next/prev/
    first/last URLs. We don't host an HTTP server, so we just produce
    URL-ish strings that include the relevant cursors — verifiers can
    still parse them deterministically."""
    def _u(s: int) -> str:
        return f"https://api.mailgun.net/{base}?limit={limit}&skip={s}"
    last_skip = max(0, ((total - 1) // max(limit, 1)) * limit)
    next_skip = min(last_skip, skip + limit) if skip + limit < total else last_skip
    prev_skip = max(0, skip - limit)
    return {
        "first": _u(0),
        "previous": _u(prev_skip),
        "next": _u(next_skip),
        "last": _u(last_skip),
    }


def _addr_match(addr: str, query: str) -> bool:
    if not query:
        return True
    return query.lower() in addr.lower()


_RECIPIENT_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _parse_recipients(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        candidates = value
    else:
        candidates = [p.strip() for p in str(value).split(",")]
    out = []
    for c in candidates:
        c = c.strip()
        if not c:
            continue
        # Strip "Name <addr>" form
        m = re.search(r"<([^>]+)>", c)
        if m:
            c = m.group(1).strip()
        out.append(c)
    return out


def _list_address_valid(addr: str) -> bool:
    return bool(_RECIPIENT_RE.match(addr or ""))


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("mailgun-mock")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@mcp.tool(name="send_message")
def send_message(domain: str,
                 from_: str = "",
                 to: str | list[str] = "",
                 subject: str = "",
                 text: str = "",
                 html: str = "",
                 cc: str | list[str] = "",
                 bcc: str | list[str] = "",
                 template: str = "",
                 o_tag: str | list[str] = "",
                 o_tracking: str = "",
                 o_tracking_clicks: str = "",
                 o_tracking_opens: str = "",
                 o_testmode: str = "",
                 o_deliverytime: str = "",
                 h_reply_to: str = "",
                 v_my_var: str = "") -> dict:
    """Mailgun REST: POST /v3/{domain}/messages — send an email.

    Returns Mailgun's send acknowledgement:
        {"id": "<...@domain>", "message": "Queued. Thank you."}

    Parameter names mirror Mailgun's form fields: `from` (we use
    `from_` since `from` is a Python keyword), `to`, `subject`,
    `text`, `html`, `cc`, `bcc`. `template` selects a saved template
    by name. `o:*` options use Python-safe names (`o_tag`,
    `o_tracking`, ...) and any `h:Reply-To` / `v:*` custom vars use
    similar conventions (`h_reply_to`, `v_my_var`).
    """
    with _lock():
        s = _load_state()
        if not domain:
            _record(s, "send_message", result="missing_domain")
            _save_state(s)
            raise ValueError("'domain' is required")
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "send_message", domain=domain, result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        recipients = _parse_recipients(to)
        if not recipients:
            _record(s, "send_message", domain=domain, result="no_recipients")
            _save_state(s)
            raise ValueError("'to' is required")
        cc_list = _parse_recipients(cc)
        bcc_list = _parse_recipients(bcc)
        tags = ([o_tag] if isinstance(o_tag, str) and o_tag
                else list(o_tag) if isinstance(o_tag, list) else [])
        msg_id = _new_message_id(s, domain)
        key = _storage_key(msg_id)
        stored = {
            "Message-Id": msg_id,
            "From": from_,
            "To": ", ".join(recipients),
            "Cc": ", ".join(cc_list) if cc_list else "",
            "Bcc": ", ".join(bcc_list) if bcc_list else "",
            "Subject": subject,
            "body-plain": text,
            "body-html": html,
            "stripped-text": text,
            "stripped-html": html,
            "template": template,
            "tags": tags,
            "recipients": recipients,
            "received_at": _now_rfc2822(),
            "storage": {
                "key": key,
                "url": (f"https://storage-us-east4.api.mailgun.net/v3/domains/"
                        f"{domain}/messages/{key}"),
                "region": "us-east4",
                "env": "production",
            },
            "headers": {
                "Reply-To": h_reply_to,
            },
            "user-variables": ({"my-var": v_my_var} if v_my_var else {}),
            "options": {
                "tracking": o_tracking,
                "tracking-clicks": o_tracking_clicks,
                "tracking-opens": o_tracking_opens,
                "testmode": o_testmode,
                "deliverytime": o_deliverytime,
            },
        }
        d["stored_messages"][key] = stored
        s["stored_messages"][key] = stored
        # Synthesize an "accepted" event per recipient
        for rcpt in recipients:
            ev_id = _new_event_id()
            d["events"].insert(0, {
                "id": ev_id,
                "event": "accepted",
                "timestamp": _now_unix(),
                "recipient": rcpt,
                "recipient-domain": rcpt.split("@", 1)[-1],
                "message": {
                    "headers": {
                        "to": rcpt,
                        "message-id": msg_id.strip("<>"),
                        "from": from_,
                        "subject": subject,
                    },
                    "attachments": [],
                    "size": len(text) + len(html),
                },
                "storage": stored["storage"],
                "tags": tags,
                "user-variables": stored["user-variables"],
                "flags": {
                    "is-test-mode": o_testmode in ("yes", "true", "1"),
                    "is-routed": False,
                    "is-system-test": False,
                    "is-authenticated": True,
                },
                "method": "HTTP",
                "envelope": {
                    "sender": from_,
                    "sending-ip": "127.0.0.1",
                    "targets": rcpt,
                    "transport": "smtp",
                },
                "log-level": "info",
            })
        # Update tag bookkeeping
        for tag in tags:
            t = d["tags"].setdefault(tag, {
                "tag": tag,
                "description": "",
                "first-seen": _now_rfc2822(),
            })
            t["last-seen"] = _now_rfc2822()
        _record(s, "send_message", domain=domain, message_id=msg_id,
                recipients=recipients, subject=subject, template=template)
        _save_state(s)
        return {"id": msg_id, "message": "Queued. Thank you."}


@mcp.tool(name="retrieve_stored_message")
def retrieve_stored_message(domain: str, storage_key: str) -> dict:
    """Mailgun REST: GET /v3/domains/{domain}/messages/{storage_key}
    — retrieve a stored (received or sent) MIME message.

    Returns the Mailgun stored-message body with headers
    (`Message-Id`, `From`, `To`, `Subject`, `body-plain`,
    `body-html`, `recipients`, `stripped-text`, `stripped-html`,
    `storage`, ...).
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "retrieve_stored_message", domain=domain,
                    storage_key=storage_key, result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        msg = d["stored_messages"].get(storage_key) or \
            s["stored_messages"].get(storage_key)
        if not msg:
            _record(s, "retrieve_stored_message", domain=domain,
                    storage_key=storage_key, result="not_found")
            _save_state(s)
            raise ValueError(f"stored_message_not_found: {storage_key}")
        _record(s, "retrieve_stored_message", domain=domain,
                storage_key=storage_key)
        _save_state(s)
        return dict(msg)


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

@mcp.tool(name="list_domains")
def list_domains(limit: int = 100,
                 skip: int = 0,
                 state_filter: str = "",
                 authority: str = "") -> dict:
    """Mailgun REST: GET /v4/domains — list sending domains on the
    account.

    Returns `{items: [...], total_count: N}` — Mailgun's v4 domains
    endpoint shape. `state_filter` filters by `active|unverified|disabled`.
    """
    with _lock():
        s = _load_state()
        items = [_domain_summary(d) for d in s["domains"].values()]
        if state_filter:
            items = [i for i in items if i.get("state") == state_filter]
        if authority:
            items = [i for i in items if i["name"].endswith(authority)]
        items.sort(key=lambda i: i["name"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        _record(s, "list_domains", count=len(page),
                state_filter=state_filter)
        _save_state(s)
        return {"items": page, "total_count": total}


@mcp.tool(name="get_domain")
def get_domain(name: str) -> dict:
    """Mailgun REST: GET /v4/domains/{name} — retrieve a single domain
    with its DNS records.

    Returns `{domain: {...}, receiving_dns_records: [...],
    sending_dns_records: [...]}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(name)
        if d is None:
            _record(s, "get_domain", name=name, result="not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {name}")
        _record(s, "get_domain", name=name)
        _save_state(s)
        return {
            "domain": _domain_summary(d),
            "receiving_dns_records": list(d.get("receiving_dns_records", [])),
            "sending_dns_records": list(d.get("sending_dns_records", [])),
        }


@mcp.tool(name="create_domain")
def create_domain(name: str,
                  smtp_password: str = "",
                  spam_action: str = "disabled",
                  wildcard: bool = False,
                  force_dkim_authority: bool = False,
                  dkim_key_size: int = 1024,
                  ips: str = "",
                  pool_id: str = "",
                  web_scheme: str = "https") -> dict:
    """Mailgun REST: POST /v4/domains — create a new sending domain.

    Returns Mailgun's create response shape: `{message, domain,
    receiving_dns_records, sending_dns_records}`.
    """
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_domain", result="missing_name")
            _save_state(s)
            raise ValueError("'name' is required")
        if name in s["domains"]:
            _record(s, "create_domain", name=name, result="already_exists")
            _save_state(s)
            raise ValueError(f"domain_already_exists: {name}")
        d = _ensure_domain(s, name)
        if smtp_password:
            d["smtp_password"] = smtp_password
        d["spam_action"] = spam_action
        d["wildcard"] = bool(wildcard)
        d["web_scheme"] = web_scheme
        _record(s, "create_domain", name=name)
        _save_state(s)
        return {
            "message": "Domain has been created",
            "domain": _domain_summary(d),
            "receiving_dns_records": list(d.get("receiving_dns_records", [])),
            "sending_dns_records": list(d.get("sending_dns_records", [])),
        }


@mcp.tool(name="delete_domain")
def delete_domain(name: str) -> dict:
    """Mailgun REST: DELETE /v3/domains/{name} — delete a sending
    domain. Returns `{message: "Domain has been deleted"}`."""
    with _lock():
        s = _load_state()
        if name not in s["domains"]:
            _record(s, "delete_domain", name=name, result="not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {name}")
        del s["domains"][name]
        _record(s, "delete_domain", name=name)
        _save_state(s)
        return {"message": "Domain has been deleted"}


# ---------------------------------------------------------------------------
# Mailing lists
# ---------------------------------------------------------------------------

def _list_summary(lst: dict) -> dict:
    return {
        "address": lst["address"],
        "name": lst.get("name", ""),
        "description": lst.get("description", ""),
        "access_level": lst.get("access_level", "readonly"),
        "reply_preference": lst.get("reply_preference", "list"),
        "created_at": lst.get("created_at", _now_rfc2822()),
        "members_count": len(lst.get("members", {})),
    }


@mcp.tool(name="list_mailing_lists")
def list_mailing_lists(limit: int = 100,
                       skip: int = 0,
                       address: str = "") -> dict:
    """Mailgun REST: GET /v3/lists/pages — list mailing lists on the
    account.

    Returns `{items: [...], paging: {first,last,next,previous}}`.
    """
    with _lock():
        s = _load_state()
        items = [_list_summary(lst) for lst in s["mailing_lists"].values()]
        if address:
            items = [i for i in items if _addr_match(i["address"], address)]
        items.sort(key=lambda i: i["address"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        paging = _paging_urls("v3/lists/pages", total, limit, skip)
        _record(s, "list_mailing_lists", count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="create_mailing_list")
def create_mailing_list(address: str,
                        name: str = "",
                        description: str = "",
                        access_level: str = "readonly",
                        reply_preference: str = "list") -> dict:
    """Mailgun REST: POST /v3/lists — create a new mailing list.

    `access_level` in {readonly, members, everyone}.
    `reply_preference` in {list, sender}.

    Returns `{message: "Mailing list has been created", list: {...}}`.
    """
    with _lock():
        s = _load_state()
        if not _list_address_valid(address):
            _record(s, "create_mailing_list", address=address,
                    result="invalid_address")
            _save_state(s)
            raise ValueError(f"invalid_list_address: {address}")
        if address in s["mailing_lists"]:
            _record(s, "create_mailing_list", address=address,
                    result="already_exists")
            _save_state(s)
            raise ValueError(f"list_already_exists: {address}")
        if access_level not in ("readonly", "members", "everyone"):
            raise ValueError(f"invalid_access_level: {access_level}")
        if reply_preference not in ("list", "sender"):
            raise ValueError(f"invalid_reply_preference: {reply_preference}")
        lst = {
            "address": address,
            "name": name,
            "description": description,
            "access_level": access_level,
            "reply_preference": reply_preference,
            "created_at": _now_rfc2822(),
            "members": {},
        }
        s["mailing_lists"][address] = lst
        _record(s, "create_mailing_list", address=address)
        _save_state(s)
        return {"message": "Mailing list has been created",
                "list": _list_summary(lst)}


@mcp.tool(name="list_list_members")
def list_list_members(address: str,
                      limit: int = 100,
                      skip: int = 0,
                      subscribed: str = "") -> dict:
    """Mailgun REST: GET /v3/lists/{address}/members/pages — list
    members of a mailing list.

    Returns `{items: [...], paging: {...}}`. `subscribed` filters by
    "yes"/"no" to match only (un)subscribed members.
    """
    with _lock():
        s = _load_state()
        lst = s["mailing_lists"].get(address)
        if lst is None:
            _record(s, "list_list_members", address=address,
                    result="list_not_found")
            _save_state(s)
            raise ValueError(f"list_not_found: {address}")
        members = list(lst.get("members", {}).values())
        if subscribed == "yes":
            members = [m for m in members if m.get("subscribed", True)]
        elif subscribed == "no":
            members = [m for m in members if not m.get("subscribed", True)]
        members.sort(key=lambda m: m["address"])
        total = len(members)
        page = _paginate_items(members, limit, skip)
        paging = _paging_urls(
            f"v3/lists/{quote(address, safe='@')}/members/pages",
            total, limit, skip)
        _record(s, "list_list_members", address=address, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="add_list_member")
def add_list_member(address: str,
                    member_address: str,
                    name: str = "",
                    vars: dict | str | None = None,
                    subscribed: bool = True,
                    upsert: bool = False) -> dict:
    """Mailgun REST: POST /v3/lists/{address}/members — add a member to
    a mailing list. `upsert` overwrites an existing entry.

    Returns `{message: "Mailing list member has been created",
    member: {...}}`.
    """
    with _lock():
        s = _load_state()
        lst = s["mailing_lists"].get(address)
        if lst is None:
            _record(s, "add_list_member", address=address,
                    result="list_not_found")
            _save_state(s)
            raise ValueError(f"list_not_found: {address}")
        if not _RECIPIENT_RE.match(member_address or ""):
            raise ValueError(f"invalid_member_address: {member_address}")
        if isinstance(vars, str):
            try:
                vars = json.loads(vars) if vars else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_vars_json: {exc}") from exc
        if vars is None:
            vars = {}
        if member_address in lst["members"] and not upsert:
            _record(s, "add_list_member", address=address,
                    member=member_address, result="already_exists")
            _save_state(s)
            raise ValueError(f"member_already_exists: {member_address}")
        member = {
            "address": member_address,
            "name": name,
            "vars": vars,
            "subscribed": bool(subscribed),
        }
        lst["members"][member_address] = member
        _record(s, "add_list_member", address=address, member=member_address)
        _save_state(s)
        return {"message": "Mailing list member has been created",
                "member": dict(member)}


@mcp.tool(name="remove_list_member")
def remove_list_member(address: str, member_address: str) -> dict:
    """Mailgun REST: DELETE /v3/lists/{address}/members/{member} —
    remove a member from a mailing list.

    Returns `{message: "Mailing list member has been deleted",
    member: {address: ...}}`.
    """
    with _lock():
        s = _load_state()
        lst = s["mailing_lists"].get(address)
        if lst is None:
            _record(s, "remove_list_member", address=address,
                    result="list_not_found")
            _save_state(s)
            raise ValueError(f"list_not_found: {address}")
        if member_address not in lst["members"]:
            _record(s, "remove_list_member", address=address,
                    member=member_address, result="not_found")
            _save_state(s)
            raise ValueError(f"member_not_found: {member_address}")
        del lst["members"][member_address]
        _record(s, "remove_list_member", address=address,
                member=member_address)
        _save_state(s)
        return {"message": "Mailing list member has been deleted",
                "member": {"address": member_address}}


# ---------------------------------------------------------------------------
# Events / stats
# ---------------------------------------------------------------------------

_EVENT_TYPES = {
    "accepted", "rejected", "delivered", "failed", "opened", "clicked",
    "unsubscribed", "complained", "stored", "list_member_uploaded",
}


@mcp.tool(name="list_events")
def list_events(domain: str,
                event: str = "",
                recipient: str = "",
                tags: str = "",
                begin: str = "",
                end: str = "",
                ascending: str = "no",
                limit: int = 100) -> dict:
    """Mailgun REST: GET /v3/{domain}/events — query the events log.

    Returns Mailgun's events page: `{items: [...], paging: {first,
    last, next, previous}}`. Event objects include `event`,
    `timestamp` (unix float), `recipient`, `message`, etc.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "list_events", domain=domain, result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        ev_filters = {e.strip() for e in (event or "").split(",") if e.strip()}
        if ev_filters and not (ev_filters <= _EVENT_TYPES):
            bad = ev_filters - _EVENT_TYPES
            raise ValueError(f"invalid_event_types: {sorted(bad)}")
        tag_filters = {t.strip() for t in (tags or "").split(",") if t.strip()}
        events = list(d.get("events", []))
        if ev_filters:
            events = [e for e in events if e.get("event") in ev_filters]
        if recipient:
            events = [e for e in events if e.get("recipient") == recipient]
        if tag_filters:
            events = [e for e in events
                      if tag_filters & set(e.get("tags", []))]
        # time filtering: begin/end are unix timestamps or RFC2822
        def _parse_t(v: str) -> float | None:
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                pass
            try:
                return datetime.datetime.strptime(
                    v, "%a, %d %b %Y %H:%M:%S %z").timestamp()
            except ValueError:
                return None
        b = _parse_t(begin)
        e = _parse_t(end)
        if b is not None:
            events = [ev for ev in events
                      if float(ev.get("timestamp", 0)) >= b]
        if e is not None:
            events = [ev for ev in events
                      if float(ev.get("timestamp", 0)) <= e]
        events.sort(key=lambda ev: float(ev.get("timestamp", 0)),
                    reverse=(ascending != "yes"))
        if limit <= 0 or limit > 300:
            limit = 100
        page = events[:limit]
        paging = _paging_urls(f"v3/{domain}/events", len(events), limit, 0)
        _record(s, "list_events", domain=domain, event=event,
                recipient=recipient, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="get_stats")
def get_stats(domain: str,
              event: str = "accepted,delivered,failed",
              start: str = "",
              end: str = "",
              resolution: str = "day",
              duration: str = "") -> dict:
    """Mailgun REST: GET /v3/{domain}/stats/total — aggregate event
    counters over time.

    Returns `{start, end, resolution, stats: [{time, <event>: {total,
    ...}}, ...]}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "get_stats", domain=domain, result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        events_requested = [e.strip() for e in (event or "").split(",")
                            if e.strip()]
        if resolution not in ("hour", "day", "month"):
            raise ValueError(f"invalid_resolution: {resolution}")
        # bucket events by the requested resolution
        fmt = {"hour": "%Y-%m-%dT%H:00:00.000Z",
               "day": "%Y-%m-%dT00:00:00.000Z",
               "month": "%Y-%m-01T00:00:00.000Z"}[resolution]
        buckets: dict[str, dict[str, int]] = {}
        for ev in d.get("events", []):
            etype = ev.get("event")
            if events_requested and etype not in events_requested:
                continue
            try:
                ts = float(ev.get("timestamp", 0))
            except (TypeError, ValueError):
                continue
            t = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc).strftime(fmt)
            buckets.setdefault(t, {}).setdefault(etype, 0)
            buckets[t][etype] += 1
        stats = []
        for t in sorted(buckets.keys()):
            row = {"time": t}
            for et in events_requested or list(_EVENT_TYPES):
                row[et] = {"total": buckets[t].get(et, 0)}
            stats.append(row)
        _record(s, "get_stats", domain=domain, event=event,
                resolution=resolution, count=len(stats))
        _save_state(s)
        return {
            "start": start or (stats[0]["time"] if stats else ""),
            "end": end or (stats[-1]["time"] if stats else ""),
            "resolution": resolution,
            "stats": stats,
        }


# ---------------------------------------------------------------------------
# Suppressions: bounces / unsubscribes / complaints
# ---------------------------------------------------------------------------

@mcp.tool(name="list_bounces")
def list_bounces(domain: str, limit: int = 100, skip: int = 0,
                 address: str = "") -> dict:
    """Mailgun REST: GET /v3/{domain}/bounces — list hard-bounce
    suppression entries for a domain.

    Returns `{items: [{address, code, error, created_at}, ...],
    paging: {...}}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "list_bounces", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        items = list(d.get("bounces", {}).values())
        if address:
            items = [i for i in items if _addr_match(i["address"], address)]
        items.sort(key=lambda i: i["address"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        paging = _paging_urls(f"v3/{domain}/bounces", total, limit, skip)
        _record(s, "list_bounces", domain=domain, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="add_bounce")
def add_bounce(domain: str,
               address: str,
               code: str = "550",
               error: str = "") -> dict:
    """Mailgun REST: POST /v3/{domain}/bounces — add a permanent-bounce
    entry to the suppression list.

    Returns `{message: "Address has been added to the bounces table",
    address}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "add_bounce", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        if not _RECIPIENT_RE.match(address or ""):
            raise ValueError(f"invalid_address: {address}")
        entry = {
            "address": address,
            "code": str(code),
            "error": error,
            "created_at": _now_rfc2822(),
        }
        d["bounces"][address] = entry
        _record(s, "add_bounce", domain=domain, address=address, code=code)
        _save_state(s)
        return {"message": "Address has been added to the bounces table",
                "address": address}


@mcp.tool(name="delete_bounce")
def delete_bounce(domain: str, address: str) -> dict:
    """Mailgun REST: DELETE /v3/{domain}/bounces/{address} — remove a
    bounce suppression entry.

    Returns `{message: "Bounced address has been removed", address}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "delete_bounce", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        if address not in d.get("bounces", {}):
            _record(s, "delete_bounce", domain=domain, address=address,
                    result="not_found")
            _save_state(s)
            raise ValueError(f"bounce_not_found: {address}")
        del d["bounces"][address]
        _record(s, "delete_bounce", domain=domain, address=address)
        _save_state(s)
        return {"message": "Bounced address has been removed",
                "address": address}


@mcp.tool(name="list_unsubscribes")
def list_unsubscribes(domain: str, limit: int = 100, skip: int = 0,
                      address: str = "") -> dict:
    """Mailgun REST: GET /v3/{domain}/unsubscribes — list unsubscribe
    suppression entries.

    Returns `{items: [{address, tags, created_at}, ...],
    paging: {...}}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "list_unsubscribes", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        items = list(d.get("unsubscribes", {}).values())
        if address:
            items = [i for i in items if _addr_match(i["address"], address)]
        items.sort(key=lambda i: i["address"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        paging = _paging_urls(f"v3/{domain}/unsubscribes", total, limit, skip)
        _record(s, "list_unsubscribes", domain=domain, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="list_complaints")
def list_complaints(domain: str, limit: int = 100, skip: int = 0,
                    address: str = "") -> dict:
    """Mailgun REST: GET /v3/{domain}/complaints — list FBL/complaint
    suppression entries.

    Returns `{items: [{address, created_at}, ...], paging: {...}}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "list_complaints", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        items = list(d.get("complaints", {}).values())
        if address:
            items = [i for i in items if _addr_match(i["address"], address)]
        items.sort(key=lambda i: i["address"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        paging = _paging_urls(f"v3/{domain}/complaints", total, limit, skip)
        _record(s, "list_complaints", domain=domain, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@mcp.tool(name="list_tags")
def list_tags(domain: str, limit: int = 100, skip: int = 0) -> dict:
    """Mailgun REST: GET /v3/{domain}/tags — list message tags seen for
    a domain.

    Returns `{items: [{tag, description, first-seen, last-seen}, ...],
    paging: {...}}`.
    """
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "list_tags", domain=domain,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        items = list(d.get("tags", {}).values())
        items.sort(key=lambda t: t["tag"])
        total = len(items)
        page = _paginate_items(items, limit, skip)
        paging = _paging_urls(f"v3/{domain}/tags", total, limit, skip)
        _record(s, "list_tags", domain=domain, count=len(page))
        _save_state(s)
        return {"items": page, "paging": paging}


@mcp.tool(name="get_tag")
def get_tag(domain: str, tag: str) -> dict:
    """Mailgun REST: GET /v3/{domain}/tags/{tag} — retrieve a single
    tag's metadata."""
    with _lock():
        s = _load_state()
        d = s["domains"].get(domain)
        if d is None:
            _record(s, "get_tag", domain=domain, tag=tag,
                    result="domain_not_found")
            _save_state(s)
            raise ValueError(f"domain_not_found: {domain}")
        t = d.get("tags", {}).get(tag)
        if t is None:
            _record(s, "get_tag", domain=domain, tag=tag, result="not_found")
            _save_state(s)
            raise ValueError(f"tag_not_found: {tag}")
        _record(s, "get_tag", domain=domain, tag=tag)
        _save_state(s)
        return dict(t)


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (for verifier introspection)."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(domains: list | None = None,
                    mailing_lists: list | None = None,
                    bounces: list | None = None,
                    unsubscribes: list | None = None,
                    complaints: list | None = None,
                    events: list | None = None,
                    tags: list | None = None,
                    stored_messages: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. Each input collection holds Mailgun-shaped dicts.

    - `domains`: [{name, state?, type?, smtp_password?, spam_action?,
                   wildcard?, web_scheme?}]
    - `mailing_lists`: [{address, name?, description?, access_level?,
                         reply_preference?, members?: [{address, name?,
                         vars?, subscribed?}]}]
    - `bounces`: [{domain, address, code?, error?}]
    - `unsubscribes`: [{domain, address, tags?, created_at?}]
    - `complaints`: [{domain, address, created_at?}]
    - `events`: [{domain, event, recipient, message?, tags?, timestamp?}]
    - `tags`: [{domain, tag, description?, first-seen?, last-seen?}]
    - `stored_messages`: [{domain, storage_key?, message_id?, from?, to?,
                           subject?, body_plain?, body_html?}]

    If `replace` is true, the state is fully reset before seeding.
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        for d in domains or []:
            name = d.get("name")
            if not name:
                continue
            dom = _ensure_domain(s, name)
            for k in ("state", "type", "smtp_login", "smtp_password",
                      "spam_action", "wildcard", "require_tls",
                      "skip_verification", "web_prefix", "web_scheme",
                      "created_at"):
                if k in d:
                    dom[k] = d[k]
        for lst in mailing_lists or []:
            addr = lst.get("address")
            if not addr:
                continue
            entry = s["mailing_lists"].setdefault(addr, {
                "address": addr,
                "name": lst.get("name", ""),
                "description": lst.get("description", ""),
                "access_level": lst.get("access_level", "readonly"),
                "reply_preference": lst.get("reply_preference", "list"),
                "created_at": lst.get("created_at", _now_rfc2822()),
                "members": {},
            })
            for k in ("name", "description", "access_level",
                      "reply_preference", "created_at"):
                if k in lst:
                    entry[k] = lst[k]
            for m in lst.get("members") or []:
                ma = m.get("address")
                if not ma:
                    continue
                entry["members"][ma] = {
                    "address": ma,
                    "name": m.get("name", ""),
                    "vars": m.get("vars", {}),
                    "subscribed": bool(m.get("subscribed", True)),
                }
        for b in bounces or []:
            dom_name = b.get("domain")
            addr = b.get("address")
            if not (dom_name and addr):
                continue
            dom = _ensure_domain(s, dom_name)
            dom["bounces"][addr] = {
                "address": addr,
                "code": str(b.get("code", "550")),
                "error": b.get("error", ""),
                "created_at": b.get("created_at", _now_rfc2822()),
            }
        for u in unsubscribes or []:
            dom_name = u.get("domain")
            addr = u.get("address")
            if not (dom_name and addr):
                continue
            dom = _ensure_domain(s, dom_name)
            dom["unsubscribes"][addr] = {
                "address": addr,
                "tags": list(u.get("tags") or []),
                "created_at": u.get("created_at", _now_rfc2822()),
            }
        for c in complaints or []:
            dom_name = c.get("domain")
            addr = c.get("address")
            if not (dom_name and addr):
                continue
            dom = _ensure_domain(s, dom_name)
            dom["complaints"][addr] = {
                "address": addr,
                "created_at": c.get("created_at", _now_rfc2822()),
            }
        for ev in events or []:
            dom_name = ev.get("domain")
            if not dom_name:
                continue
            dom = _ensure_domain(s, dom_name)
            event_obj = {
                "id": ev.get("id") or _new_event_id(),
                "event": ev.get("event", "accepted"),
                "timestamp": float(ev.get("timestamp") or _now_unix()),
                "recipient": ev.get("recipient", ""),
                "recipient-domain": (ev.get("recipient", "").split("@", 1)[-1]
                                     if "@" in ev.get("recipient", "") else ""),
                "message": ev.get("message", {}),
                "tags": list(ev.get("tags") or []),
                "user-variables": ev.get("user-variables", {}),
                "log-level": ev.get("log-level", "info"),
            }
            dom["events"].insert(0, event_obj)
        for t in tags or []:
            dom_name = t.get("domain")
            tag = t.get("tag")
            if not (dom_name and tag):
                continue
            dom = _ensure_domain(s, dom_name)
            dom["tags"][tag] = {
                "tag": tag,
                "description": t.get("description", ""),
                "first-seen": t.get("first-seen", _now_rfc2822()),
                "last-seen": t.get("last-seen", _now_rfc2822()),
            }
        for sm in stored_messages or []:
            dom_name = sm.get("domain")
            if not dom_name:
                continue
            dom = _ensure_domain(s, dom_name)
            msg_id = sm.get("message_id") or _new_message_id(s, dom_name)
            key = sm.get("storage_key") or _storage_key(msg_id)
            stored = {
                "Message-Id": msg_id,
                "From": sm.get("from", ""),
                "To": sm.get("to", ""),
                "Subject": sm.get("subject", ""),
                "body-plain": sm.get("body_plain", ""),
                "body-html": sm.get("body_html", ""),
                "stripped-text": sm.get("body_plain", ""),
                "stripped-html": sm.get("body_html", ""),
                "recipients": _parse_recipients(sm.get("to", "")),
                "received_at": sm.get("received_at", _now_rfc2822()),
                "storage": {
                    "key": key,
                    "url": (f"https://storage-us-east4.api.mailgun.net/v3/"
                            f"domains/{dom_name}/messages/{key}"),
                    "region": "us-east4",
                    "env": "production",
                },
            }
            dom["stored_messages"][key] = stored
            s["stored_messages"][key] = stored
        _record(s, "debug_seed",
                counts={
                    "domains": len(domains or []),
                    "mailing_lists": len(mailing_lists or []),
                    "bounces": len(bounces or []),
                    "unsubscribes": len(unsubscribes or []),
                    "complaints": len(complaints or []),
                    "events": len(events or []),
                    "tags": len(tags or []),
                    "stored_messages": len(stored_messages or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "domains": list(s["domains"].keys()),
            "mailing_lists": list(s["mailing_lists"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
