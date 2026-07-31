"""Cloudflare mock MCP server.

Mirrors the Cloudflare REST API v4 surface
(https://developers.cloudflare.com/api/) so that rollouts which would
otherwise hit the live Cloudflare control plane (Zones, DNS,
Workers, KV, R2, Pages, Page Rules, User/Account) can run
deterministically against an in-process mock.

Tool surface (29 + 2 mock helpers) — every tool returns the v4
envelope:

    {"success": true,
     "errors": [],
     "messages": [],
     "result": <payload>,
     "result_info": {...}}   # only on paginated lists

On failure:

    {"success": false,
     "errors": [{"code": 1003, "message": "..."}],
     "messages": [],
     "result": null}

The mock is intentionally *not* a wrapper around the cloudflare-mcp
CLI mock — it mirrors the REST surface directly.

Operations
  Zones:        list_zones, get_zone, create_zone, delete_zone
  DNS:          list_dns_records, get_dns_record, create_dns_record,
                update_dns_record, delete_dns_record
  Workers:      list_workers, get_worker_script, upload_worker_script,
                delete_worker_script
  Workers KV:   list_kv_namespaces, list_kv_keys, get_kv_value,
                write_kv_value, delete_kv_value
  R2:           list_r2_buckets, create_r2_bucket, delete_r2_bucket
  Pages:        list_pages_projects, get_pages_project,
                list_pages_deployments
  Page Rules:   list_page_rules, create_page_rule
  Cache:        purge_cache
  Account/User: get_user, list_accounts
  Mock helpers: mock_debug_state, mock_debug_seed

State lives at `$CLOUDFLARE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/cloudflare_mock`). On first start, if no state.json
exists and `CLOUDFLARE_MOCK_SEED_PATH` is set, the seed file is
loaded as the initial state. Every tool call (including reads)
appends an entry to `state["calls"]` so verifiers can replay the
trace.
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

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "CLOUDFLARE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/cloudflare_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _hex_id(seed: str | None = None) -> str:
    """Cloudflare ids are 32-char lowercase hex strings."""
    if seed:
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return secrets.token_hex(16)


def _empty_state() -> dict:
    bot_user_id = _hex_id("mock-user")
    bot_account_id = _hex_id("mock-account")
    return {
        "user": {
            "id": bot_user_id,
            "email": "mock@example.com",
            "first_name": "Mock",
            "last_name": "User",
            "username": "mockuser",
            "telephone": None,
            "country": "US",
            "zipcode": "00000",
            "created_on": _now_iso(),
            "modified_on": _now_iso(),
            "two_factor_authentication_enabled": False,
            "suspended": False,
        },
        "accounts": {
            bot_account_id: {
                "id": bot_account_id,
                "name": "Mock Account",
                "type": "standard",
                "settings": {
                    "enforce_twofactor": False,
                    "use_account_custom_ns_by_default": False,
                },
                "created_on": _now_iso(),
            },
        },
        "default_account_id": bot_account_id,
        "zones": {},                      # zone_id -> zone dict
        "dns_records": {},                # zone_id -> [record dict]
        "page_rules": {},                 # zone_id -> [page rule dict]
        "worker_scripts": {},             # account_id -> {script_name -> dict}
        "kv_namespaces": {},              # account_id -> {ns_id -> ns dict}
        "kv_values": {},                  # ns_id -> {key -> {value, metadata, expiration}}
        "r2_buckets": {},                 # account_id -> {bucket_name -> bucket dict}
        "pages_projects": {},             # account_id -> {project_name -> dict}
        "pages_deployments": {},          # project_name -> [deployment dict]
        "purges": [],                     # list of purge_cache calls
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("CLOUDFLARE_MOCK_SEED_PATH")
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
# v4 envelope
# ---------------------------------------------------------------------------

def _ok(result: Any, result_info: dict | None = None,
        messages: list | None = None) -> dict:
    env: dict[str, Any] = {
        "success": True,
        "errors": [],
        "messages": messages or [],
        "result": result,
    }
    if result_info is not None:
        env["result_info"] = result_info
    return env


def _err(code: int, message: str) -> dict:
    """Cloudflare v4 error envelope. Common codes:
       1001 invalid request
       1003 record not found / resource not found
       7003 missing required parameter
       9106 invalid zone
       10000 authentication error
    """
    return {
        "success": False,
        "errors": [{"code": code, "message": message}],
        "messages": [],
        "result": None,
    }


def _paginate(items: list, page: int, per_page: int) -> tuple[list, dict]:
    """Returns (page_slice, result_info) matching Cloudflare's v4
    pagination shape."""
    if page < 1:
        page = 1
    if per_page < 1:
        per_page = 20
    if per_page > 50000:
        per_page = 50000
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 0
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]
    info = {
        "page": page,
        "per_page": per_page,
        "count": len(page_items),
        "total_count": total,
        "total_pages": total_pages,
    }
    return page_items, info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DNS_TYPES = {"A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA",
              "PTR", "SPF", "URI"}
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_dns_content(rtype: str, content: str) -> str | None:
    if rtype == "A":
        if not _IPV4_RE.match(content or ""):
            return "A record content must be an IPv4 address"
        for part in content.split("."):
            try:
                if not (0 <= int(part) <= 255):
                    return "A record octet out of range"
            except ValueError:
                return "A record octet not numeric"
    elif rtype == "AAAA":
        if ":" not in (content or ""):
            return "AAAA record content must be an IPv6 address"
    elif rtype == "CNAME":
        if not content:
            return "CNAME record requires content"
    elif rtype == "MX":
        if not content:
            return "MX record requires content (mail host)"
    elif rtype == "TXT":
        if content is None:
            return "TXT record requires content"
    elif rtype == "NS":
        if not content:
            return "NS record requires content (nameserver)"
    elif rtype == "SRV":
        if not content:
            return "SRV record requires content"
    return None


def _normalize_dns_name(name: str, zone_name: str) -> str:
    if name == "@" or not name:
        return zone_name
    if name == zone_name or name.endswith("." + zone_name):
        return name
    return f"{name}.{zone_name}"


def _make_zone(name: str, account_id: str,
               jump_start: bool = True, paused: bool = False,
               ztype: str = "full") -> dict:
    zid = _hex_id(f"zone:{name}")
    now = _now_iso()
    return {
        "id": zid,
        "name": name,
        "status": "pending" if jump_start else "active",
        "paused": paused,
        "type": ztype,
        "development_mode": 0,
        "name_servers": [
            f"ns1.{name}",
            f"ns2.{name}",
        ],
        "original_name_servers": None,
        "original_registrar": None,
        "original_dnshost": None,
        "created_on": now,
        "modified_on": now,
        "activated_on": None if jump_start else now,
        "meta": {
            "step": 4,
            "wildcard_proxiable": False,
            "custom_certificate_quota": 0,
            "page_rule_quota": 3,
            "phishing_detected": False,
            "multiple_railguns_allowed": False,
        },
        "owner": {
            "id": None,
            "email": None,
            "type": "user",
        },
        "account": {
            "id": account_id,
            "name": "Mock Account",
        },
        "plan": {
            "id": "0feeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "name": "Free Website",
            "price": 0,
            "currency": "USD",
            "frequency": "",
            "legacy_id": "free",
            "is_subscribed": False,
            "can_subscribe": False,
        },
        "permissions": ["#zone:read", "#zone:edit"],
    }


def _make_dns_record(zone_id: str, zone_name: str, rtype: str, name: str,
                     content: str, ttl: int, proxied: bool,
                     priority: int | None, comment: str | None,
                     tags: list[str] | None) -> dict:
    rid = _hex_id(f"dns:{zone_id}:{rtype}:{name}:{content}:{secrets.token_hex(4)}")
    now = _now_iso()
    rec: dict[str, Any] = {
        "id": rid,
        "zone_id": zone_id,
        "zone_name": zone_name,
        "name": name,
        "type": rtype,
        "content": content,
        "proxiable": rtype in ("A", "AAAA", "CNAME"),
        "proxied": bool(proxied) if rtype in ("A", "AAAA", "CNAME") else False,
        "ttl": ttl if ttl is not None else 1,
        "locked": False,
        "meta": {
            "auto_added": False,
            "managed_by_apps": False,
            "managed_by_argo_tunnel": False,
            "source": "primary",
        },
        "comment": comment,
        "tags": list(tags or []),
        "created_on": now,
        "modified_on": now,
    }
    if rtype in ("MX", "SRV", "URI"):
        rec["priority"] = priority if priority is not None else 10
    return rec


def _resolve_zone(state: dict, ref: str) -> str | None:
    """Resolve a zone by id or name; return canonical zone_id."""
    if not ref:
        return None
    if ref in state["zones"]:
        return ref
    for zid, z in state["zones"].items():
        if z.get("name") == ref:
            return zid
    return None


def _default_account_id(state: dict, account_id: str | None) -> str | None:
    if account_id and account_id in state["accounts"]:
        return account_id
    if not account_id:
        return state.get("default_account_id")
    return None


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("cloudflare-mock")


# ===========================================================================
# Zones
# ===========================================================================

@mcp.tool(name="list_zones")
def list_zones(name: str = "",
               status: str = "",
               account_id: str = "",
               page: int = 1,
               per_page: int = 20,
               order: str = "name",
               direction: str = "asc",
               match: str = "all") -> dict:
    """Cloudflare v4: GET /zones — list zones in the account.

    Supports filtering by `name` (exact match by default), `status`
    (active/pending/initializing/moved/deleted/deactivated), and
    `account.id`. Paginated via `page` + `per_page`. Sort by
    `name|status|account.id|account.name` ascending or descending."""
    with _lock():
        s = _load_state()
        zones = list(s["zones"].values())
        if name:
            zones = [z for z in zones if z.get("name") == name]
        if status:
            zones = [z for z in zones if z.get("status") == status]
        if account_id:
            zones = [z for z in zones
                     if z.get("account", {}).get("id") == account_id]
        key_field = order if order in ("name", "status") else "name"
        zones.sort(key=lambda z: z.get(key_field, ""),
                   reverse=(direction == "desc"))
        page_items, info = _paginate(zones, page, per_page)
        _record(s, "list_zones", name=name, status=status,
                count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


@mcp.tool(name="get_zone")
def get_zone(zone_id: str) -> dict:
    """Cloudflare v4: GET /zones/{zone_id} — retrieve a zone by its
    32-char hex id (or by zone name as a convenience)."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "get_zone", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        _record(s, "get_zone", zone_id=zid)
        _save_state(s)
        return _ok(s["zones"][zid])


@mcp.tool(name="create_zone")
def create_zone(name: str,
                account: dict | None = None,
                jump_start: bool = True,
                type: str = "full") -> dict:
    """Cloudflare v4: POST /zones — create a new zone.

    `account` is `{"id": "<32-hex>"}`. `type` in {"full","partial",
    "secondary"}. Returns the new zone object with status="pending"
    when jump_start is true, "active" otherwise."""
    with _lock():
        s = _load_state()
        if not name or "." not in name:
            _record(s, "create_zone", name=name, result="invalid_name")
            _save_state(s)
            return _err(1097, "Invalid zone name")
        if type not in ("full", "partial", "secondary"):
            _record(s, "create_zone", name=name, result="invalid_type")
            _save_state(s)
            return _err(1004, f"Invalid type: {type}")
        for z in s["zones"].values():
            if z.get("name") == name:
                _record(s, "create_zone", name=name, result="duplicate")
                _save_state(s)
                return _err(1061, f"Zone already exists: {name}")
        acct_id = None
        if isinstance(account, dict) and account.get("id"):
            acct_id = account["id"]
        acct_id = _default_account_id(s, acct_id)
        if not acct_id:
            _record(s, "create_zone", name=name, result="bad_account")
            _save_state(s)
            return _err(1010, "Invalid account.id")
        zone = _make_zone(name, acct_id, jump_start=jump_start, ztype=type)
        s["zones"][zone["id"]] = zone
        s["dns_records"].setdefault(zone["id"], [])
        s["page_rules"].setdefault(zone["id"], [])
        _record(s, "create_zone", zone_id=zone["id"], name=name,
                account_id=acct_id, type=type, jump_start=jump_start)
        _save_state(s)
        return _ok(zone)


@mcp.tool(name="delete_zone")
def delete_zone(zone_id: str) -> dict:
    """Cloudflare v4: DELETE /zones/{zone_id} — delete a zone.
    Returns the deleted zone's id in `result`."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "delete_zone", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        del s["zones"][zid]
        s["dns_records"].pop(zid, None)
        s["page_rules"].pop(zid, None)
        _record(s, "delete_zone", zone_id=zid)
        _save_state(s)
        return _ok({"id": zid})


@mcp.tool(name="purge_cache")
def purge_cache(zone_id: str,
                purge_everything: bool = False,
                files: list | None = None,
                tags: list | None = None,
                hosts: list | None = None,
                prefixes: list | None = None) -> dict:
    """Cloudflare v4: POST /zones/{zone_id}/purge_cache — purge cached
    resources from a zone. Provide `purge_everything=true` OR one of
    `files`, `tags`, `hosts`, `prefixes`. Returns `{"id": "<zone_id>"}`
    on success."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "purge_cache", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        body_has = any([purge_everything, files, tags, hosts, prefixes])
        if not body_has:
            _record(s, "purge_cache", zone_id=zid, result="no_target")
            _save_state(s)
            return _err(1007,
                        "Either purge_everything or at least one of "
                        "files/tags/hosts/prefixes is required")
        purge = {
            "zone_id": zid,
            "ts": _now_iso(),
            "purge_everything": bool(purge_everything),
            "files": list(files or []),
            "tags": list(tags or []),
            "hosts": list(hosts or []),
            "prefixes": list(prefixes or []),
        }
        s["purges"].append(purge)
        _record(s, "purge_cache", zone_id=zid,
                purge_everything=purge_everything,
                files_count=len(files or []),
                tags_count=len(tags or []),
                hosts_count=len(hosts or []),
                prefixes_count=len(prefixes or []))
        _save_state(s)
        return _ok({"id": zid})


# ===========================================================================
# DNS records
# ===========================================================================

@mcp.tool(name="list_dns_records")
def list_dns_records(zone_id: str,
                     type: str = "",
                     name: str = "",
                     content: str = "",
                     proxied: bool | None = None,
                     page: int = 1,
                     per_page: int = 100,
                     order: str = "type",
                     direction: str = "asc",
                     match: str = "all") -> dict:
    """Cloudflare v4: GET /zones/{zone_id}/dns_records — list DNS
    records for a zone, optionally filtered by `type`, `name`,
    `content`, `proxied`. Paginated. `order` in
    {type,name,content,ttl,proxied}."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "list_dns_records", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        records = list(s["dns_records"].get(zid, []))
        zone_name = s["zones"][zid].get("name", "")
        if type:
            records = [r for r in records if r.get("type") == type]
        if name:
            full = _normalize_dns_name(name, zone_name)
            records = [r for r in records
                       if r.get("name") == full or r.get("name") == name]
        if content:
            records = [r for r in records if r.get("content") == content]
        if proxied is not None:
            records = [r for r in records
                       if bool(r.get("proxied")) == bool(proxied)]
        if order in ("type", "name", "content", "ttl", "proxied"):
            records.sort(key=lambda r: r.get(order) or "",
                         reverse=(direction == "desc"))
        page_items, info = _paginate(records, page, per_page)
        _record(s, "list_dns_records", zone_id=zid, type=type, name=name,
                count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


@mcp.tool(name="get_dns_record")
def get_dns_record(zone_id: str, dns_record_id: str) -> dict:
    """Cloudflare v4: GET /zones/{zone_id}/dns_records/{dns_record_id}
    — retrieve a single DNS record by id."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "get_dns_record", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        rec = next((r for r in s["dns_records"].get(zid, [])
                    if r.get("id") == dns_record_id), None)
        _record(s, "get_dns_record", zone_id=zid,
                dns_record_id=dns_record_id,
                result="ok" if rec else "not_found")
        _save_state(s)
        if not rec:
            return _err(81044, f"DNS record not found: {dns_record_id}")
        return _ok(rec)


@mcp.tool(name="create_dns_record")
def create_dns_record(zone_id: str,
                      type: str,
                      name: str,
                      content: str,
                      ttl: int = 1,
                      proxied: bool = False,
                      priority: int | None = None,
                      comment: str | None = None,
                      tags: list | None = None) -> dict:
    """Cloudflare v4: POST /zones/{zone_id}/dns_records — create a
    new DNS record.

    `type` in {A, AAAA, CNAME, MX, TXT, NS, SRV, CAA, PTR, SPF, URI}.
    `name` may be `@` for the apex; `ttl=1` means automatic. `proxied`
    only honored for A/AAAA/CNAME. `priority` is required for MX/SRV.
    """
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "create_dns_record", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        rtype = (type or "").upper()
        if rtype not in _DNS_TYPES:
            _record(s, "create_dns_record", zone_id=zid, result="bad_type")
            _save_state(s)
            return _err(9005, f"Invalid DNS record type: {type}")
        err = _validate_dns_content(rtype, content)
        if err:
            _record(s, "create_dns_record", zone_id=zid,
                    result="bad_content")
            _save_state(s)
            return _err(9020, err)
        if ttl is not None and ttl != 1 and not (60 <= ttl <= 86400):
            _record(s, "create_dns_record", zone_id=zid, result="bad_ttl")
            _save_state(s)
            return _err(9007,
                        "ttl must be 1 (automatic) or between 60 and 86400")
        if rtype in ("MX", "SRV") and priority is None:
            _record(s, "create_dns_record", zone_id=zid,
                    result="missing_priority")
            _save_state(s)
            return _err(7003, f"{rtype} record requires priority")
        zone_name = s["zones"][zid].get("name", "")
        full = _normalize_dns_name(name, zone_name)
        rec = _make_dns_record(zid, zone_name, rtype, full, content,
                               ttl, proxied, priority, comment, tags)
        s["dns_records"].setdefault(zid, []).append(rec)
        _record(s, "create_dns_record", zone_id=zid,
                dns_record_id=rec["id"], type=rtype, name=full)
        _save_state(s)
        return _ok(rec)


@mcp.tool(name="update_dns_record")
def update_dns_record(zone_id: str,
                      dns_record_id: str,
                      type: str | None = None,
                      name: str | None = None,
                      content: str | None = None,
                      ttl: int | None = None,
                      proxied: bool | None = None,
                      priority: int | None = None,
                      comment: str | None = None,
                      tags: list | None = None) -> dict:
    """Cloudflare v4: PATCH /zones/{zone_id}/dns_records/{dns_record_id}
    — partial update of a DNS record. Only provided fields are
    modified. Returns the updated record."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "update_dns_record", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        rec = next((r for r in s["dns_records"].get(zid, [])
                    if r.get("id") == dns_record_id), None)
        if not rec:
            _record(s, "update_dns_record", zone_id=zid,
                    dns_record_id=dns_record_id, result="not_found")
            _save_state(s)
            return _err(81044, f"DNS record not found: {dns_record_id}")
        rtype = (type or rec["type"]).upper()
        if type is not None and rtype not in _DNS_TYPES:
            _record(s, "update_dns_record", zone_id=zid, result="bad_type")
            _save_state(s)
            return _err(9005, f"Invalid DNS record type: {type}")
        new_content = content if content is not None else rec["content"]
        err = _validate_dns_content(rtype, new_content)
        if err:
            _record(s, "update_dns_record", zone_id=zid,
                    result="bad_content")
            _save_state(s)
            return _err(9020, err)
        if ttl is not None and ttl != 1 and not (60 <= ttl <= 86400):
            _record(s, "update_dns_record", zone_id=zid, result="bad_ttl")
            _save_state(s)
            return _err(9007,
                        "ttl must be 1 (automatic) or between 60 and 86400")
        zone_name = s["zones"][zid].get("name", "")
        if name is not None:
            rec["name"] = _normalize_dns_name(name, zone_name)
        if type is not None:
            rec["type"] = rtype
            rec["proxiable"] = rtype in ("A", "AAAA", "CNAME")
        if content is not None:
            rec["content"] = content
        if ttl is not None:
            rec["ttl"] = ttl
        if proxied is not None:
            if rec["type"] in ("A", "AAAA", "CNAME"):
                rec["proxied"] = bool(proxied)
            else:
                rec["proxied"] = False
        if priority is not None and rec["type"] in ("MX", "SRV", "URI"):
            rec["priority"] = priority
        if comment is not None:
            rec["comment"] = comment
        if tags is not None:
            rec["tags"] = list(tags)
        rec["modified_on"] = _now_iso()
        _record(s, "update_dns_record", zone_id=zid,
                dns_record_id=dns_record_id)
        _save_state(s)
        return _ok(rec)


@mcp.tool(name="delete_dns_record")
def delete_dns_record(zone_id: str, dns_record_id: str) -> dict:
    """Cloudflare v4: DELETE /zones/{zone_id}/dns_records/{dns_record_id}
    — delete a DNS record. Returns `{"id": "<deleted_id>"}`."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "delete_dns_record", zone=zone_id, result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        records = s["dns_records"].get(zid, [])
        target = next((r for r in records
                       if r.get("id") == dns_record_id), None)
        if not target:
            _record(s, "delete_dns_record", zone_id=zid,
                    dns_record_id=dns_record_id, result="not_found")
            _save_state(s)
            return _err(81044, f"DNS record not found: {dns_record_id}")
        records.remove(target)
        _record(s, "delete_dns_record", zone_id=zid,
                dns_record_id=dns_record_id)
        _save_state(s)
        return _ok({"id": dns_record_id})


# ===========================================================================
# Workers
# ===========================================================================

@mcp.tool(name="list_workers")
def list_workers(account_id: str = "") -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/workers/scripts —
    list Worker scripts under an account."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id or None)
        if not aid:
            _record(s, "list_workers", account=account_id,
                    result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        scripts = list(s["worker_scripts"].get(aid, {}).values())
        # The real surface returns ScriptResponse without the script
        # body — we strip `body` here too.
        result = [{k: v for k, v in sc.items() if k != "body"}
                  for sc in scripts]
        _record(s, "list_workers", account_id=aid, count=len(result))
        _save_state(s)
        return _ok(result)


@mcp.tool(name="get_worker_script")
def get_worker_script(account_id: str, script_name: str) -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/workers/scripts/{script_name}
    — retrieve a worker script's metadata and source body.

    The real API returns the JS body as `text/javascript`; the mock
    returns the script object with the `body` field inline for
    convenience."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "get_worker_script", account=account_id,
                    script=script_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        sc = s["worker_scripts"].get(aid, {}).get(script_name)
        if not sc:
            _record(s, "get_worker_script", account_id=aid,
                    script=script_name, result="not_found")
            _save_state(s)
            return _err(10007, f"Worker script not found: {script_name}")
        _record(s, "get_worker_script", account_id=aid,
                script=script_name)
        _save_state(s)
        return _ok(sc)


@mcp.tool(name="upload_worker_script")
def upload_worker_script(account_id: str,
                         script_name: str,
                         script: str = "",
                         body: str = "",
                         metadata: dict | None = None,
                         compatibility_date: str | None = None,
                         compatibility_flags: list | None = None) -> dict:
    """Cloudflare v4: PUT /accounts/{account_id}/workers/scripts/{script_name}
    — upload (create or replace) a Worker script.

    Pass either `script` or `body` for the JS source (they alias).
    `metadata` is the upload-form metadata blob
    ({"main_module": "...", "bindings": [...]})."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "upload_worker_script", account=account_id,
                    script=script_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if not _NAME_RE.match(script_name or ""):
            _record(s, "upload_worker_script", account_id=aid,
                    script=script_name, result="bad_name")
            _save_state(s)
            return _err(10021,
                        "Worker script name must be lowercase, 3-63 chars, "
                        "alphanumeric and hyphens only")
        source = script or body or ""
        scripts = s["worker_scripts"].setdefault(aid, {})
        existed = script_name in scripts
        sid = (scripts[script_name]["id"] if existed
               else _hex_id(f"worker:{aid}:{script_name}"))
        now = _now_iso()
        record = {
            "id": sid,
            "etag": hashlib.md5(source.encode("utf-8")).hexdigest(),
            "size": len(source.encode("utf-8")),
            "modified_on": now,
            "created_on": (scripts[script_name]["created_on"]
                           if existed else now),
            "usage_model": "bundled",
            "handlers": ["fetch"],
            "compatibility_date": compatibility_date,
            "compatibility_flags": list(compatibility_flags or []),
            "logpush": False,
            "tail_consumers": None,
            "placement_mode": None,
            "metadata": metadata or {},
            "body": source,
        }
        scripts[script_name] = record
        _record(s, "upload_worker_script", account_id=aid,
                script=script_name, replaced=existed,
                size=record["size"])
        _save_state(s)
        return _ok({k: v for k, v in record.items() if k != "body"})


@mcp.tool(name="delete_worker_script")
def delete_worker_script(account_id: str, script_name: str,
                         force: bool = False) -> dict:
    """Cloudflare v4: DELETE /accounts/{account_id}/workers/scripts/{script_name}
    — delete a Worker script. `force=true` ignores referenced-by
    checks (the mock has none, so always succeeds)."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "delete_worker_script", account=account_id,
                    script=script_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        scripts = s["worker_scripts"].get(aid, {})
        if script_name not in scripts:
            _record(s, "delete_worker_script", account_id=aid,
                    script=script_name, result="not_found")
            _save_state(s)
            return _err(10007, f"Worker script not found: {script_name}")
        del scripts[script_name]
        _record(s, "delete_worker_script", account_id=aid,
                script=script_name, force=force)
        _save_state(s)
        return _ok(None)


# ===========================================================================
# Workers KV
# ===========================================================================

@mcp.tool(name="list_kv_namespaces")
def list_kv_namespaces(account_id: str = "",
                       page: int = 1,
                       per_page: int = 100,
                       order: str = "title",
                       direction: str = "asc") -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/storage/kv/namespaces
    — list Workers KV namespaces for an account. Paginated."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id or None)
        if not aid:
            _record(s, "list_kv_namespaces", account=account_id,
                    result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        namespaces = list(s["kv_namespaces"].get(aid, {}).values())
        if order in ("title", "id"):
            namespaces.sort(key=lambda n: n.get(order) or "",
                            reverse=(direction == "desc"))
        page_items, info = _paginate(namespaces, page, per_page)
        _record(s, "list_kv_namespaces", account_id=aid,
                count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


@mcp.tool(name="list_kv_keys")
def list_kv_keys(account_id: str,
                 namespace_id: str,
                 prefix: str = "",
                 limit: int = 1000,
                 cursor: str = "") -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/keys
    — list keys in a KV namespace. Optional `prefix` filter, `limit`
    up to 1000, and `cursor` for continuation. Returns
    `result_info` with `cursor` to resume."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "list_kv_keys", account=account_id,
                    namespace=namespace_id, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if namespace_id not in s["kv_namespaces"].get(aid, {}):
            _record(s, "list_kv_keys", account_id=aid,
                    namespace=namespace_id, result="not_found")
            _save_state(s)
            return _err(10013, f"KV namespace not found: {namespace_id}")
        entries = s["kv_values"].get(namespace_id, {})
        keys = sorted(entries.keys())
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        start = 0
        if cursor:
            try:
                start = keys.index(cursor) + 1
            except ValueError:
                start = 0
        limit = max(1, min(int(limit or 1000), 1000))
        page = keys[start: start + limit]
        next_cursor = page[-1] if (start + limit) < len(keys) and page else ""
        out = []
        for k in page:
            e = entries[k]
            row: dict[str, Any] = {"name": k}
            if e.get("expiration") is not None:
                row["expiration"] = e["expiration"]
            if e.get("metadata"):
                row["metadata"] = e["metadata"]
            out.append(row)
        info = {"count": len(out), "cursor": next_cursor}
        _record(s, "list_kv_keys", account_id=aid,
                namespace=namespace_id, count=len(out))
        _save_state(s)
        return _ok(out, info)


@mcp.tool(name="get_kv_value")
def get_kv_value(account_id: str,
                 namespace_id: str,
                 key_name: str) -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key_name}
    — read a single KV value. Returns `{"value": "<string>",
    "metadata": {...}}`. (The real endpoint returns the value body
    directly; the mock wraps it in the v4 envelope for uniformity.)"""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "get_kv_value", account=account_id,
                    namespace=namespace_id, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if namespace_id not in s["kv_namespaces"].get(aid, {}):
            _record(s, "get_kv_value", account_id=aid,
                    namespace=namespace_id, result="not_found")
            _save_state(s)
            return _err(10013, f"KV namespace not found: {namespace_id}")
        entries = s["kv_values"].get(namespace_id, {})
        entry = entries.get(key_name)
        if entry is None:
            _record(s, "get_kv_value", account_id=aid,
                    namespace=namespace_id, key=key_name,
                    result="not_found")
            _save_state(s)
            return _err(10009, f"Key not found: {key_name}")
        _record(s, "get_kv_value", account_id=aid,
                namespace=namespace_id, key=key_name)
        _save_state(s)
        return _ok({
            "value": entry.get("value", ""),
            "metadata": entry.get("metadata") or {},
        })


@mcp.tool(name="write_kv_value")
def write_kv_value(account_id: str,
                   namespace_id: str,
                   key_name: str,
                   value: str,
                   metadata: dict | None = None,
                   expiration: int | None = None,
                   expiration_ttl: int | None = None) -> dict:
    """Cloudflare v4: PUT /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key_name}
    — write a single KV value. `expiration` is a unix epoch seconds
    timestamp; `expiration_ttl` is seconds from now. `metadata` is an
    arbitrary JSON object."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "write_kv_value", account=account_id,
                    namespace=namespace_id, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if namespace_id not in s["kv_namespaces"].get(aid, {}):
            _record(s, "write_kv_value", account_id=aid,
                    namespace=namespace_id, result="not_found")
            _save_state(s)
            return _err(10013, f"KV namespace not found: {namespace_id}")
        exp = expiration
        if exp is None and expiration_ttl is not None:
            exp = int(datetime.datetime.now(datetime.timezone.utc)
                      .timestamp()) + int(expiration_ttl)
        entries = s["kv_values"].setdefault(namespace_id, {})
        entries[key_name] = {
            "value": value,
            "metadata": metadata or {},
            "expiration": exp,
        }
        _record(s, "write_kv_value", account_id=aid,
                namespace=namespace_id, key=key_name,
                size=len(value or ""))
        _save_state(s)
        return _ok(None)


@mcp.tool(name="delete_kv_value")
def delete_kv_value(account_id: str,
                    namespace_id: str,
                    key_name: str) -> dict:
    """Cloudflare v4: DELETE /accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key_name}
    — delete a single KV key/value pair."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "delete_kv_value", account=account_id,
                    namespace=namespace_id, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if namespace_id not in s["kv_namespaces"].get(aid, {}):
            _record(s, "delete_kv_value", account_id=aid,
                    namespace=namespace_id, result="not_found")
            _save_state(s)
            return _err(10013, f"KV namespace not found: {namespace_id}")
        entries = s["kv_values"].get(namespace_id, {})
        if key_name not in entries:
            _record(s, "delete_kv_value", account_id=aid,
                    namespace=namespace_id, key=key_name,
                    result="not_found")
            _save_state(s)
            return _err(10009, f"Key not found: {key_name}")
        del entries[key_name]
        _record(s, "delete_kv_value", account_id=aid,
                namespace=namespace_id, key=key_name)
        _save_state(s)
        return _ok(None)


# ===========================================================================
# R2
# ===========================================================================

@mcp.tool(name="list_r2_buckets")
def list_r2_buckets(account_id: str = "",
                    name_contains: str = "",
                    per_page: int = 20,
                    cursor: str = "",
                    order: str = "name",
                    direction: str = "asc") -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/r2/buckets — list R2
    buckets. Optional `name_contains` substring filter. Returns
    `{"buckets": [...]}` in `result`."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id or None)
        if not aid:
            _record(s, "list_r2_buckets", account=account_id,
                    result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        buckets = list(s["r2_buckets"].get(aid, {}).values())
        if name_contains:
            buckets = [b for b in buckets
                       if name_contains in (b.get("name") or "")]
        if order in ("name", "creation_date"):
            field = "created_on" if order == "creation_date" else "name"
            buckets.sort(key=lambda b: b.get(field) or "",
                         reverse=(direction == "desc"))
        _record(s, "list_r2_buckets", account_id=aid, count=len(buckets))
        _save_state(s)
        return _ok({"buckets": buckets})


@mcp.tool(name="create_r2_bucket")
def create_r2_bucket(account_id: str,
                     name: str,
                     location_hint: str = "",
                     storage_class: str = "Standard") -> dict:
    """Cloudflare v4: POST /accounts/{account_id}/r2/buckets — create
    a new R2 bucket. `location_hint` in {"apac","eeur","enam","weur",
    "wnam"} (case-insensitive). `storage_class` in {"Standard",
    "InfrequentAccess"}."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "create_r2_bucket", account=account_id,
                    bucket=name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if not _NAME_RE.match(name or ""):
            _record(s, "create_r2_bucket", account_id=aid, bucket=name,
                    result="bad_name")
            _save_state(s)
            return _err(10004,
                        "R2 bucket name must be lowercase, 3-63 chars, "
                        "alphanumeric and hyphens only")
        buckets = s["r2_buckets"].setdefault(aid, {})
        if name in buckets:
            _record(s, "create_r2_bucket", account_id=aid, bucket=name,
                    result="duplicate")
            _save_state(s)
            return _err(10006, f"R2 bucket already exists: {name}")
        if storage_class not in ("Standard", "InfrequentAccess"):
            _record(s, "create_r2_bucket", account_id=aid, bucket=name,
                    result="bad_storage_class")
            _save_state(s)
            return _err(10008,
                        f"Invalid storage class: {storage_class}")
        bucket = {
            "name": name,
            "location": (location_hint or "").lower() or "apac",
            "creation_date": _now_iso(),
            "created_on": _now_iso(),
            "storage_class": storage_class,
        }
        buckets[name] = bucket
        _record(s, "create_r2_bucket", account_id=aid, bucket=name,
                location=bucket["location"],
                storage_class=storage_class)
        _save_state(s)
        return _ok(bucket)


@mcp.tool(name="delete_r2_bucket")
def delete_r2_bucket(account_id: str, bucket_name: str) -> dict:
    """Cloudflare v4: DELETE /accounts/{account_id}/r2/buckets/{bucket_name}
    — delete an R2 bucket."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "delete_r2_bucket", account=account_id,
                    bucket=bucket_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        buckets = s["r2_buckets"].get(aid, {})
        if bucket_name not in buckets:
            _record(s, "delete_r2_bucket", account_id=aid,
                    bucket=bucket_name, result="not_found")
            _save_state(s)
            return _err(10006, f"R2 bucket not found: {bucket_name}")
        del buckets[bucket_name]
        _record(s, "delete_r2_bucket", account_id=aid,
                bucket=bucket_name)
        _save_state(s)
        return _ok(None)


# ===========================================================================
# Pages
# ===========================================================================

@mcp.tool(name="list_pages_projects")
def list_pages_projects(account_id: str = "",
                        page: int = 1,
                        per_page: int = 25) -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/pages/projects —
    list Cloudflare Pages projects in an account. Paginated."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id or None)
        if not aid:
            _record(s, "list_pages_projects", account=account_id,
                    result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        projects = list(s["pages_projects"].get(aid, {}).values())
        projects.sort(key=lambda p: p.get("name") or "")
        page_items, info = _paginate(projects, page, per_page)
        _record(s, "list_pages_projects", account_id=aid,
                count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


@mcp.tool(name="get_pages_project")
def get_pages_project(account_id: str, project_name: str) -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/pages/projects/{project_name}
    — retrieve a Pages project."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "get_pages_project", account=account_id,
                    project=project_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        proj = s["pages_projects"].get(aid, {}).get(project_name)
        if not proj:
            _record(s, "get_pages_project", account_id=aid,
                    project=project_name, result="not_found")
            _save_state(s)
            return _err(8000007, f"Pages project not found: {project_name}")
        _record(s, "get_pages_project", account_id=aid,
                project=project_name)
        _save_state(s)
        return _ok(proj)


@mcp.tool(name="list_pages_deployments")
def list_pages_deployments(account_id: str,
                           project_name: str,
                           env: str = "",
                           page: int = 1,
                           per_page: int = 25) -> dict:
    """Cloudflare v4: GET /accounts/{account_id}/pages/projects/{project_name}/deployments
    — list deployments for a Pages project. Optional `env` filter
    in {"production","preview"}."""
    with _lock():
        s = _load_state()
        aid = _default_account_id(s, account_id)
        if not aid:
            _record(s, "list_pages_deployments", account=account_id,
                    project=project_name, result="bad_account")
            _save_state(s)
            return _err(1003, f"Invalid account: {account_id}")
        if project_name not in s["pages_projects"].get(aid, {}):
            _record(s, "list_pages_deployments", account_id=aid,
                    project=project_name, result="not_found")
            _save_state(s)
            return _err(8000007, f"Pages project not found: {project_name}")
        deployments = list(s["pages_deployments"].get(project_name, []))
        if env:
            deployments = [d for d in deployments
                           if d.get("environment") == env]
        deployments.sort(key=lambda d: d.get("created_on") or "",
                         reverse=True)
        page_items, info = _paginate(deployments, page, per_page)
        _record(s, "list_pages_deployments", account_id=aid,
                project=project_name, count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


# ===========================================================================
# Page Rules
# ===========================================================================

@mcp.tool(name="list_page_rules")
def list_page_rules(zone_id: str,
                    status: str = "",
                    order: str = "priority",
                    direction: str = "desc",
                    match: str = "all") -> dict:
    """Cloudflare v4: GET /zones/{zone_id}/pagerules — list page rules
    for a zone. Optional `status` filter ("active"|"disabled")."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "list_page_rules", zone=zone_id,
                    result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        rules = list(s["page_rules"].get(zid, []))
        if status:
            rules = [r for r in rules if r.get("status") == status]
        if order in ("priority", "status"):
            rules.sort(key=lambda r: r.get(order) or 0,
                       reverse=(direction == "desc"))
        _record(s, "list_page_rules", zone_id=zid, count=len(rules))
        _save_state(s)
        return _ok(rules)


@mcp.tool(name="create_page_rule")
def create_page_rule(zone_id: str,
                     targets: list,
                     actions: list,
                     priority: int = 1,
                     status: str = "active") -> dict:
    """Cloudflare v4: POST /zones/{zone_id}/pagerules — create a page
    rule.

    `targets` is a list of target dicts (typically
    `[{"target":"url","constraint":{"operator":"matches",
    "value":"https://example.com/*"}}]`). `actions` is a list of
    action dicts (e.g. `[{"id":"always_use_https"}]`)."""
    with _lock():
        s = _load_state()
        zid = _resolve_zone(s, zone_id)
        if not zid:
            _record(s, "create_page_rule", zone=zone_id,
                    result="not_found")
            _save_state(s)
            return _err(1003, f"Invalid zone identifier: {zone_id}")
        if not isinstance(targets, list) or not targets:
            _record(s, "create_page_rule", zone_id=zid,
                    result="bad_targets")
            _save_state(s)
            return _err(1004, "targets must be a non-empty list")
        if not isinstance(actions, list) or not actions:
            _record(s, "create_page_rule", zone_id=zid,
                    result="bad_actions")
            _save_state(s)
            return _err(1004, "actions must be a non-empty list")
        if status not in ("active", "disabled"):
            _record(s, "create_page_rule", zone_id=zid,
                    result="bad_status")
            _save_state(s)
            return _err(1004, f"Invalid status: {status}")
        now = _now_iso()
        rule_id = _hex_id(f"pagerule:{zid}:{secrets.token_hex(4)}")
        rule = {
            "id": rule_id,
            "targets": targets,
            "actions": actions,
            "priority": int(priority),
            "status": status,
            "created_on": now,
            "modified_on": now,
        }
        s["page_rules"].setdefault(zid, []).append(rule)
        _record(s, "create_page_rule", zone_id=zid,
                rule_id=rule_id, status=status, priority=priority)
        _save_state(s)
        return _ok(rule)


# ===========================================================================
# User / Account
# ===========================================================================

@mcp.tool(name="get_user")
def get_user() -> dict:
    """Cloudflare v4: GET /user — return the authenticated user
    object."""
    with _lock():
        s = _load_state()
        _record(s, "get_user")
        _save_state(s)
        return _ok(s["user"])


@mcp.tool(name="list_accounts")
def list_accounts(name: str = "",
                  page: int = 1,
                  per_page: int = 20,
                  direction: str = "desc") -> dict:
    """Cloudflare v4: GET /accounts — list accounts the authenticated
    user has access to. Optional `name` filter (exact match)."""
    with _lock():
        s = _load_state()
        accounts = list(s["accounts"].values())
        if name:
            accounts = [a for a in accounts if a.get("name") == name]
        accounts.sort(key=lambda a: a.get("name") or "",
                      reverse=(direction == "desc"))
        page_items, info = _paginate(accounts, page, per_page)
        _record(s, "list_accounts", count=len(page_items))
        _save_state(s)
        return _ok(page_items, info)


# ===========================================================================
# Mock-only helpers
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state (for verifier
    introspection). Not part of the real Cloudflare API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(user: dict | None = None,
                    accounts: list | None = None,
                    default_account_id: str | None = None,
                    zones: list | None = None,
                    dns_records: list | None = None,
                    worker_scripts: list | None = None,
                    kv_namespaces: list | None = None,
                    kv_values: list | None = None,
                    r2_buckets: list | None = None,
                    pages_projects: list | None = None,
                    pages_deployments: list | None = None,
                    page_rules: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed the persisted state with Cloudflare-shaped
    fixtures. If `replace` is true the state is fully reset first.

    Shapes (all keys optional, mock fills defaults):

    - user: {id?, email, first_name, last_name, ...}
    - accounts: [{id?, name, type?}]
    - zones: [{id?, name, status?, account_id?, paused?}]
    - dns_records: [{zone, type, name, content, ttl?, proxied?,
                     priority?, comment?, tags?}]
    - worker_scripts: [{account_id?, name, body|script, metadata?,
                        compatibility_date?, compatibility_flags?}]
    - kv_namespaces: [{account_id?, id?, title}]
    - kv_values: [{namespace_id, key, value, metadata?, expiration?}]
    - r2_buckets: [{account_id?, name, location?, storage_class?}]
    - pages_projects: [{account_id?, name, subdomain?, domains?,
                        production_branch?, source?}]
    - pages_deployments: [{project_name, environment?, url?, source?}]
    - page_rules: [{zone, targets, actions, priority?, status?}]
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if user:
            s["user"].update(user)
        for a in accounts or []:
            aid = a.get("id") or _hex_id(f"account:{a.get('name','')}")
            s["accounts"][aid] = {
                "id": aid,
                "name": a.get("name", "Mock Account"),
                "type": a.get("type", "standard"),
                "settings": a.get("settings",
                                  {"enforce_twofactor": False,
                                   "use_account_custom_ns_by_default":
                                       False}),
                "created_on": a.get("created_on", _now_iso()),
            }
        if default_account_id and default_account_id in s["accounts"]:
            s["default_account_id"] = default_account_id
        elif accounts and not s.get("default_account_id"):
            s["default_account_id"] = next(iter(s["accounts"].keys()))
        for z in zones or []:
            zid = z.get("id") or _hex_id(f"zone:{z.get('name','')}")
            acct_id = (z.get("account_id")
                       or s.get("default_account_id"))
            now = _now_iso()
            zone_obj = _make_zone(z.get("name", "example.com"),
                                  acct_id or "",
                                  jump_start=(z.get("status", "active")
                                              == "pending"),
                                  ztype=z.get("type", "full"))
            zone_obj["id"] = zid
            if "status" in z:
                zone_obj["status"] = z["status"]
            if "paused" in z:
                zone_obj["paused"] = bool(z["paused"])
            if "development_mode" in z:
                zone_obj["development_mode"] = int(z["development_mode"])
            if "name_servers" in z:
                zone_obj["name_servers"] = list(z["name_servers"])
            if "plan" in z:
                zone_obj["plan"].update(z["plan"])
            s["zones"][zid] = zone_obj
            s["dns_records"].setdefault(zid, [])
            s["page_rules"].setdefault(zid, [])
        for r in dns_records or []:
            zref = r.get("zone") or r.get("zone_id") or r.get("zone_name")
            zid = _resolve_zone(s, zref or "")
            if not zid:
                continue
            zone_name = s["zones"][zid].get("name", "")
            rtype = (r.get("type") or "A").upper()
            full = _normalize_dns_name(r.get("name", "@"), zone_name)
            rec = _make_dns_record(zid, zone_name, rtype, full,
                                   r.get("content", ""),
                                   r.get("ttl", 1),
                                   r.get("proxied", False),
                                   r.get("priority"),
                                   r.get("comment"),
                                   r.get("tags"))
            if r.get("id"):
                rec["id"] = r["id"]
            s["dns_records"].setdefault(zid, []).append(rec)
        for w in worker_scripts or []:
            aid = (w.get("account_id")
                   or s.get("default_account_id"))
            if not aid:
                continue
            scripts = s["worker_scripts"].setdefault(aid, {})
            name = w.get("name") or w.get("script_name") or ""
            if not name:
                continue
            source = w.get("body") or w.get("script") or ""
            scripts[name] = {
                "id": w.get("id") or _hex_id(f"worker:{aid}:{name}"),
                "etag": hashlib.md5(source.encode("utf-8")).hexdigest(),
                "size": len(source.encode("utf-8")),
                "modified_on": _now_iso(),
                "created_on": w.get("created_on", _now_iso()),
                "usage_model": w.get("usage_model", "bundled"),
                "handlers": w.get("handlers", ["fetch"]),
                "compatibility_date": w.get("compatibility_date"),
                "compatibility_flags": list(
                    w.get("compatibility_flags") or []),
                "logpush": bool(w.get("logpush", False)),
                "tail_consumers": w.get("tail_consumers"),
                "placement_mode": w.get("placement_mode"),
                "metadata": w.get("metadata", {}),
                "body": source,
            }
        for n in kv_namespaces or []:
            aid = (n.get("account_id")
                   or s.get("default_account_id"))
            if not aid:
                continue
            nid = n.get("id") or _hex_id(
                f"kv:{aid}:{n.get('title','')}")
            s["kv_namespaces"].setdefault(aid, {})[nid] = {
                "id": nid,
                "title": n.get("title", nid),
                "supports_url_encoding": bool(
                    n.get("supports_url_encoding", True)),
            }
            s["kv_values"].setdefault(nid, {})
        for v in kv_values or []:
            nsid = v.get("namespace_id")
            if not nsid:
                continue
            s["kv_values"].setdefault(nsid, {})[v["key"]] = {
                "value": v.get("value", ""),
                "metadata": v.get("metadata") or {},
                "expiration": v.get("expiration"),
            }
        for b in r2_buckets or []:
            aid = (b.get("account_id")
                   or s.get("default_account_id"))
            if not aid:
                continue
            name = b.get("name", "")
            if not name:
                continue
            s["r2_buckets"].setdefault(aid, {})[name] = {
                "name": name,
                "location": b.get("location", "apac"),
                "creation_date": b.get("creation_date", _now_iso()),
                "created_on": b.get("created_on", _now_iso()),
                "storage_class": b.get("storage_class", "Standard"),
            }
        for p in pages_projects or []:
            aid = (p.get("account_id")
                   or s.get("default_account_id"))
            if not aid:
                continue
            name = p.get("name", "")
            if not name:
                continue
            now = _now_iso()
            s["pages_projects"].setdefault(aid, {})[name] = {
                "id": p.get("id") or _hex_id(f"pages:{aid}:{name}"),
                "name": name,
                "subdomain": p.get("subdomain", f"{name}.pages.dev"),
                "domains": list(p.get("domains") or []),
                "source": p.get("source"),
                "build_config": p.get("build_config", {
                    "build_command": "",
                    "destination_dir": "",
                    "root_dir": "",
                    "web_analytics_tag": None,
                    "web_analytics_token": None,
                }),
                "deployment_configs": p.get("deployment_configs", {
                    "production": {}, "preview": {}}),
                "production_branch": p.get("production_branch", "main"),
                "canonical_deployment": p.get("canonical_deployment"),
                "latest_deployment": p.get("latest_deployment"),
                "created_on": p.get("created_on", now),
            }
        for d in pages_deployments or []:
            proj = d.get("project_name") or d.get("project")
            if not proj:
                continue
            now = _now_iso()
            entry = {
                "id": d.get("id") or _hex_id(f"deploy:{proj}:{secrets.token_hex(4)}"),
                "short_id": d.get("short_id", secrets.token_hex(4)),
                "project_id": d.get("project_id"),
                "project_name": proj,
                "environment": d.get("environment", "production"),
                "url": d.get("url", f"https://{proj}.pages.dev"),
                "created_on": d.get("created_on", now),
                "modified_on": d.get("modified_on", now),
                "latest_stage": d.get("latest_stage", {
                    "name": "deploy",
                    "started_on": now,
                    "ended_on": now,
                    "status": "success",
                }),
                "deployment_trigger": d.get("deployment_trigger", {
                    "type": "ad_hoc",
                    "metadata": {},
                }),
                "stages": d.get("stages", []),
                "build_config": d.get("build_config", {}),
                "source": d.get("source"),
                "aliases": list(d.get("aliases") or []),
                "is_skipped": bool(d.get("is_skipped", False)),
            }
            s["pages_deployments"].setdefault(proj, []).append(entry)
        for pr in page_rules or []:
            zref = pr.get("zone") or pr.get("zone_id") or pr.get("zone_name")
            zid = _resolve_zone(s, zref or "")
            if not zid:
                continue
            now = _now_iso()
            rule = {
                "id": pr.get("id") or _hex_id(
                    f"pagerule:{zid}:{secrets.token_hex(4)}"),
                "targets": pr.get("targets", []),
                "actions": pr.get("actions", []),
                "priority": int(pr.get("priority", 1)),
                "status": pr.get("status", "active"),
                "created_on": pr.get("created_on", now),
                "modified_on": pr.get("modified_on", now),
            }
            s["page_rules"].setdefault(zid, []).append(rule)
        _record(s, "debug_seed",
                counts={
                    "accounts": len(accounts or []),
                    "zones": len(zones or []),
                    "dns_records": len(dns_records or []),
                    "worker_scripts": len(worker_scripts or []),
                    "kv_namespaces": len(kv_namespaces or []),
                    "kv_values": len(kv_values or []),
                    "r2_buckets": len(r2_buckets or []),
                    "pages_projects": len(pages_projects or []),
                    "pages_deployments": len(pages_deployments or []),
                    "page_rules": len(page_rules or []),
                },
                replace=replace)
        _save_state(s)
        return _ok({
            "account_ids": list(s["accounts"].keys()),
            "zone_ids": list(s["zones"].keys()),
            "default_account_id": s.get("default_account_id"),
        })


if __name__ == "__main__":
    mcp.run()
