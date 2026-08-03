"""Confluence Cloud mock MCP server.

Mirrors the tool surface of the Confluence Cloud REST API v2
(https://developer.atlassian.com/cloud/confluence/rest/v2/intro/).
Tool names follow Atlassian's operation names (kebab-case) and
parameter shapes / response payloads follow the v2 JSON contracts:

  - List endpoints return {"results": [...], "_links": {...}}
  - Item endpoints return the object directly (page/space/etc.)
  - Errors return {"errors": [{"status":..., "code":"...", "title":"..."}]}
  - Page bodies use storage / atlas_doc_format / view representations

Backed by a single JSON state file (default
$CONFLUENCE_MOCK_STATE_DIR/state.json) that holds spaces, pages,
blog posts, comments, labels, and the rolling call log.

Tool inventory:

  Spaces:    get_spaces, get_space
  Pages:     get_pages, get_page_by_id, create_page, update_page,
             delete_page, get_page_children, get_page_versions
  Blog:      get_blog_posts, create_blog_post
  Comments:  get_page_footer_comments, create_footer_comment,
             get_page_inline_comments
  Labels:    get_page_labels, add_label_to_page
  Mock-only: mock_debug_state, mock_debug_seed

State persists to $CONFLUENCE_MOCK_STATE_DIR/state.json (default
~/.openclaw/confluence_mock). A file lock (`fcntl.flock`) makes
concurrent calls safe. Set `CONFLUENCE_MOCK_SEED_PATH` to preload
state on first start.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "CONFLUENCE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/confluence_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    bot_id = "557058:00000000-0000-0000-0000-000000000bot"
    return {
        "site": {
            "base_url": "https://mock.atlassian.net/wiki",
            "cloud_id": "00000000-0000-0000-0000-0000000000c0",
        },
        "self": {
            "accountId": bot_id,
            "accountType": "atlassian",
            "displayName": "Mock Bot",
            "email": "mockbot@example.com",
        },
        "users": {
            bot_id: {
                "accountId": bot_id,
                "accountType": "atlassian",
                "displayName": "Mock Bot",
                "email": "mockbot@example.com",
            },
        },
        "spaces": {},          # space_id (str) -> space dict
        "space_keys": {},      # key -> space_id
        "pages": {},           # page_id (str) -> page dict
        "blog_posts": {},      # id (str) -> blog post dict
        "comments": {},        # id (str) -> comment dict (footer + inline)
        "labels": {},          # label_id -> {"id","name","prefix"}
        "label_names": {},     # name -> label_id
        "page_versions": {},   # page_id -> list[version dict]
        "next_id": {
            "space": 100,
            "page": 1000,
            "blog": 5000,
            "comment": 9000,
            "label": 700,
            "version": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("CONFLUENCE_MOCK_SEED_PATH")
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
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next(state: dict, kind: str) -> str:
    n = state["next_id"][kind]
    state["next_id"][kind] = n + 1
    return str(n)


def _err(status: int, code: str, title: str, detail: str = "") -> dict:
    """Confluence v2-shaped error envelope.

    Example real response body:
      {"errors": [{"status": 404, "code": "NOT_FOUND",
                   "title": "Page not found", "detail": "..."}]}
    """
    e: dict[str, Any] = {"status": status, "code": code, "title": title}
    if detail:
        e["detail"] = detail
    return {"errors": [e]}


def _site_base(state: dict) -> str:
    return state.get("site", {}).get("base_url",
                                     "https://mock.atlassian.net/wiki")


def _coerce_body(body: Any, default_repr: str = "storage") -> dict | None:
    """Accept body as: None, str (treated as storage value),
    or a dict like {"representation":"storage"|"atlas_doc_format"|"view",
    "value": "..."}. Returns the canonical dict shape or None."""
    if body is None:
        return None
    if isinstance(body, str):
        return {"representation": default_repr, "value": body}
    if isinstance(body, dict):
        rep = body.get("representation") or default_repr
        val = body.get("value", "")
        # Confluence sometimes wraps in {"storage": {"value":...,
        # "representation":"storage"}} — accept that too.
        if not val:
            for k in ("storage", "atlas_doc_format", "view"):
                if isinstance(body.get(k), dict):
                    rep = k
                    val = body[k].get("value", "")
                    break
        return {"representation": rep, "value": val or ""}
    return {"representation": default_repr, "value": str(body)}


def _resolve_space(state: dict, ref: str) -> str | None:
    """Resolve a space ref (id or key) to a space_id present in state."""
    if not ref:
        return None
    if ref in state["spaces"]:
        return ref
    if ref in state["space_keys"]:
        return state["space_keys"][ref]
    # case-insensitive key fallback
    for k, sid in state["space_keys"].items():
        if k.lower() == ref.lower():
            return sid
    return None


def _page_links(state: dict, page_id: str) -> dict:
    base = _site_base(state)
    p = state["pages"].get(page_id) or state["blog_posts"].get(page_id)
    space_id = p.get("spaceId") if p else None
    space_key = ""
    if space_id and space_id in state["spaces"]:
        space_key = state["spaces"][space_id].get("key", "")
    title = (p.get("title", "") if p else "").replace(" ", "+")
    webui = (f"/spaces/{space_key}/pages/{page_id}/{title}"
             if space_key else f"/pages/{page_id}")
    edit = f"/pages/edit-v2.action?pageId={page_id}"
    return {
        "webui": webui,
        "editui": edit,
        "tinyui": f"/x/{page_id}",
    }


def _list_links(self_path: str, next_cursor: str | None) -> dict:
    out = {"self": self_path}
    if next_cursor:
        sep = "&" if "?" in self_path else "?"
        out["next"] = f"{self_path}{sep}cursor={next_cursor}"
    return out


def _paginate(items: list, cursor: str, limit: int) -> tuple[list, str | None]:
    """Cursor pagination — cursor encodes the id of the LAST item
    returned in the previous page (matches Confluence v2 convention of
    opaque cursors)."""
    if limit <= 0:
        limit = 25
    if limit > 250:
        limit = 250
    start = 0
    if cursor:
        for i, it in enumerate(items):
            ident = str(it.get("id", "")) if isinstance(it, dict) else ""
            if ident == cursor:
                start = i + 1
                break
    end = start + limit
    page = items[start:end]
    next_cursor: str | None = None
    if end < len(items) and page:
        last = page[-1]
        if isinstance(last, dict) and "id" in last:
            next_cursor = str(last["id"])
    return page, next_cursor


def _public_space(s: dict, sp: dict) -> dict:
    base = _site_base(s)
    return {
        "id": sp["id"],
        "key": sp.get("key", ""),
        "name": sp.get("name", ""),
        "type": sp.get("type", "global"),
        "status": sp.get("status", "current"),
        "authorId": sp.get("authorId", s["self"]["accountId"]),
        "createdAt": sp.get("createdAt", _now()),
        "homepageId": sp.get("homepageId"),
        "description": sp.get("description"),
        "icon": sp.get("icon"),
        "_links": {
            "webui": f"/spaces/{sp.get('key','')}",
            "base": base,
        },
    }


def _public_page(s: dict, p: dict,
                 body_format: str | None = None,
                 include_labels: bool = False) -> dict:
    out: dict[str, Any] = {
        "id": p["id"],
        "status": p.get("status", "current"),
        "title": p.get("title", ""),
        "spaceId": p.get("spaceId"),
        "parentId": p.get("parentId"),
        "parentType": p.get("parentType", "page" if p.get("parentId") else None),
        "position": p.get("position"),
        "authorId": p.get("authorId", s["self"]["accountId"]),
        "ownerId": p.get("ownerId", p.get("authorId", s["self"]["accountId"])),
        "lastOwnerId": p.get("lastOwnerId"),
        "createdAt": p.get("createdAt", _now()),
        "version": {
            "number": p.get("version", 1),
            "message": p.get("versionMessage", ""),
            "minorEdit": p.get("minorEdit", False),
            "authorId": p.get("lastEditorId",
                              p.get("authorId", s["self"]["accountId"])),
            "createdAt": p.get("lastEditedAt", p.get("createdAt", _now())),
        },
        "_links": _page_links(s, p["id"]),
    }
    body = p.get("body")
    if body_format and body:
        rep = body.get("representation", "storage")
        val = body.get("value", "")
        # v2 exposes body in the requested format keys
        if body_format == "storage":
            out["body"] = {"storage": {"representation": "storage",
                                       "value": val if rep == "storage" else val}}
        elif body_format == "atlas_doc_format":
            out["body"] = {"atlas_doc_format": {
                "representation": "atlas_doc_format", "value": val,
            }}
        elif body_format == "view":
            out["body"] = {"view": {"representation": "view", "value": val}}
        elif body_format == "anonymous_export_view":
            out["body"] = {"anonymous_export_view": {
                "representation": "anonymous_export_view", "value": val,
            }}
        else:
            out["body"] = {rep: {"representation": rep, "value": val}}
    if include_labels:
        ids = p.get("labels", [])
        out["labels"] = {
            "results": [_public_label(s["labels"][lid]) for lid in ids
                        if lid in s["labels"]],
            "_links": {},
        }
    return out


def _public_blog(s: dict, b: dict, body_format: str | None = None) -> dict:
    out: dict[str, Any] = {
        "id": b["id"],
        "status": b.get("status", "current"),
        "title": b.get("title", ""),
        "spaceId": b.get("spaceId"),
        "authorId": b.get("authorId", s["self"]["accountId"]),
        "createdAt": b.get("createdAt", _now()),
        "version": {
            "number": b.get("version", 1),
            "message": b.get("versionMessage", ""),
            "minorEdit": b.get("minorEdit", False),
            "authorId": b.get("lastEditorId",
                              b.get("authorId", s["self"]["accountId"])),
            "createdAt": b.get("lastEditedAt", b.get("createdAt", _now())),
        },
        "_links": _page_links(s, b["id"]),
    }
    body = b.get("body")
    if body_format and body:
        rep = body.get("representation", "storage")
        val = body.get("value", "")
        out["body"] = {body_format: {"representation": body_format,
                                     "value": val}} if body_format != rep \
            else {rep: {"representation": rep, "value": val}}
    return out


def _public_comment(s: dict, c: dict, body_format: str = "storage") -> dict:
    body = c.get("body") or {"representation": "storage", "value": ""}
    return {
        "id": c["id"],
        "status": c.get("status", "current"),
        "title": c.get("title", ""),
        "version": {
            "number": c.get("version", 1),
            "authorId": c.get("authorId", s["self"]["accountId"]),
            "createdAt": c.get("createdAt", _now()),
            "message": "",
            "minorEdit": False,
        },
        "pageId": c.get("pageId"),
        "blogPostId": c.get("blogPostId"),
        "parentCommentId": c.get("parentCommentId"),
        "body": {body_format: {"representation": body_format,
                               "value": body.get("value", "")}},
        "_links": {
            "webui": (f"/pages/{c.get('pageId')}?focusedCommentId={c['id']}"
                      if c.get("pageId") else f"/x/{c['id']}"),
        },
    }


def _public_label(lbl: dict) -> dict:
    return {
        "id": lbl["id"],
        "name": lbl.get("name", ""),
        "prefix": lbl.get("prefix", "global"),
    }


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("confluence-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------

@mcp.tool(name="get_spaces")
def get_spaces(ids: list[str] | None = None,
               keys: list[str] | None = None,
               type: str = "",
               status: str = "",
               labels: list[str] | None = None,
               sort: str = "",
               cursor: str = "",
               limit: int = 25) -> dict:
    """Confluence v2: GET /spaces — return all spaces, optionally
    filtered by ids/keys/type/status. Returns
    {"results": [Space], "_links": {"next": ..., "self": ...}}."""
    with _lock():
        s = _load_state()
        items = list(s["spaces"].values())
        if ids:
            wanted = {str(i) for i in ids}
            items = [sp for sp in items if str(sp.get("id")) in wanted]
        if keys:
            wanted_k = {str(k) for k in keys}
            items = [sp for sp in items if sp.get("key") in wanted_k]
        if type:
            items = [sp for sp in items if sp.get("type") == type]
        if status:
            items = [sp for sp in items if sp.get("status", "current") == status]
        # sort
        if sort:
            field = sort.lstrip("-")
            reverse = sort.startswith("-")
            items.sort(key=lambda sp: sp.get(field, ""), reverse=reverse)
        else:
            items.sort(key=lambda sp: sp.get("key", ""))
        page, next_cursor = _paginate(items, cursor, limit)
        results = [_public_space(s, sp) for sp in page]
        _record(s, "get_spaces", count=len(results),
                ids=ids, keys=keys, type=type, status=status)
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/spaces?limit={limit}", next_cursor),
        }


@mcp.tool(name="get_space")
def get_space(id: str,
              description_format: str = "",
              include_icon: bool = False) -> dict:
    """Confluence v2: GET /spaces/{id} — retrieve a single space.
    `id` accepts either the numeric space id or the space key."""
    with _lock():
        s = _load_state()
        sid = _resolve_space(s, id)
        if not sid:
            _record(s, "get_space", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No space found with id or key: {id}")
        sp = s["spaces"][sid]
        out = _public_space(s, sp)
        if description_format:
            desc_val = (sp.get("description") or {}).get("value", "") \
                if isinstance(sp.get("description"), dict) \
                else (sp.get("description") or "")
            out["description"] = {
                description_format: {
                    "representation": description_format,
                    "value": desc_val,
                }
            }
        if include_icon:
            out["icon"] = sp.get("icon") or {"path": "/images/logo/default-space-logo.svg",
                                             "apiDownloadLink": ""}
        _record(s, "get_space", id=id, space_id=sid)
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@mcp.tool(name="get_pages")
def get_pages(id: list[str] | None = None,
              space_id: list[str] | None = None,
              status: list[str] | None = None,
              title: str = "",
              body_format: str = "",
              cursor: str = "",
              limit: int = 25) -> dict:
    """Confluence v2: GET /pages — list pages, optionally filtered
    by id, spaceId, status, or title. `body_format` requests one of
    storage / atlas_doc_format / view. Returns
    {"results": [...], "_links": {...}}."""
    with _lock():
        s = _load_state()
        items = list(s["pages"].values())
        if id:
            wanted = {str(i) for i in id}
            items = [p for p in items if str(p.get("id")) in wanted]
        if space_id:
            # accept either space ids or space keys
            wanted_sids = set()
            for sref in space_id:
                sid = _resolve_space(s, sref)
                if sid:
                    wanted_sids.add(sid)
            items = [p for p in items if p.get("spaceId") in wanted_sids]
        if status:
            wanted_st = set(status)
            items = [p for p in items if p.get("status", "current") in wanted_st]
        else:
            items = [p for p in items if p.get("status", "current") == "current"]
        if title:
            items = [p for p in items if p.get("title") == title]
        items.sort(key=lambda p: str(p.get("id", "")))
        page, next_cursor = _paginate(items, cursor, limit)
        results = [_public_page(s, p, body_format=body_format or None)
                   for p in page]
        _record(s, "get_pages", count=len(results),
                space_id=space_id, status=status, title=title)
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages?limit={limit}", next_cursor),
        }


@mcp.tool(name="get_page_by_id")
def get_page_by_id(id: str,
                   body_format: str = "storage",
                   get_draft: bool = False,
                   version: int = 0,
                   include_labels: bool = False,
                   include_properties: bool = False,
                   include_operations: bool = False,
                   include_versions: bool = False) -> dict:
    """Confluence v2: GET /pages/{id} — retrieve a single page.

    `body_format` in {storage, atlas_doc_format, view,
    anonymous_export_view, ""}. `version` selects a historical
    version (0 = current)."""
    with _lock():
        s = _load_state()
        p = s["pages"].get(str(id))
        if not p:
            _record(s, "get_page_by_id", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        target = dict(p)
        if version and version > 0:
            vs = s.get("page_versions", {}).get(str(id), [])
            v = next((vv for vv in vs if vv.get("number") == version), None)
            if not v:
                _record(s, "get_page_by_id", id=id, version=version,
                        result="version_not_found")
                _save_state(s)
                return _err(404, "NOT_FOUND",
                            f"No version {version} for page: {id}")
            target["body"] = v.get("body", target.get("body"))
            target["title"] = v.get("title", target.get("title"))
            target["version"] = v.get("number", target.get("version", 1))
            target["lastEditedAt"] = v.get("createdAt",
                                           target.get("lastEditedAt"))
            target["lastEditorId"] = v.get("authorId",
                                           target.get("lastEditorId"))
        out = _public_page(s, target, body_format=body_format or None,
                           include_labels=include_labels)
        if include_versions:
            out["versions"] = {
                "results": s.get("page_versions", {}).get(str(id), []),
                "_links": {},
            }
        if include_properties:
            out["properties"] = {"results": p.get("properties", []),
                                 "_links": {}}
        if include_operations:
            out["operations"] = {
                "results": [
                    {"operation": "read", "targetType": "page"},
                    {"operation": "update", "targetType": "page"},
                    {"operation": "delete", "targetType": "page"},
                ],
                "_links": {},
            }
        _record(s, "get_page_by_id", id=id, body_format=body_format,
                version=version or None)
        _save_state(s)
        return out


@mcp.tool(name="create_page")
def create_page(spaceId: str,
                title: str,
                body: Any = None,
                parentId: str = "",
                status: str = "current",
                representation: str = "storage",
                root_level: bool = False) -> dict:
    """Confluence v2: POST /pages — create a new page.

    `spaceId` is a space id or key. `body` accepts a plain string
    (interpreted as `storage` XHTML) or a dict
    {"representation": "storage|atlas_doc_format|view", "value": "..."}.
    `parentId` sets a parent page (omit/empty + root_level=True to
    create at the space root).
    """
    with _lock():
        s = _load_state()
        sid = _resolve_space(s, spaceId)
        if not sid:
            _record(s, "create_page", spaceId=spaceId,
                    result="space_not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No space found with id or key: {spaceId}")
        pid = ""
        parent_type = None
        if parentId:
            parent = s["pages"].get(str(parentId))
            if not parent:
                _record(s, "create_page", parentId=parentId,
                        result="parent_not_found")
                _save_state(s)
                return _err(404, "NOT_FOUND",
                            f"No parent page found: {parentId}")
            if parent.get("spaceId") != sid:
                _record(s, "create_page", parentId=parentId,
                        result="parent_wrong_space")
                _save_state(s)
                return _err(400, "INVALID_REQUEST",
                            "Parent page is not in the target space")
            pid = str(parentId)
            parent_type = "page"
        elif not root_level:
            # default to space homepage (if set) when neither parent
            # nor root_level was provided
            homepage = s["spaces"][sid].get("homepageId")
            if homepage:
                pid = str(homepage)
                parent_type = "page"
        # title uniqueness within space (Confluence enforces this)
        for existing in s["pages"].values():
            if (existing.get("spaceId") == sid
                    and existing.get("title") == title
                    and existing.get("status", "current") == "current"):
                _record(s, "create_page", spaceId=spaceId, title=title,
                        result="title_conflict")
                _save_state(s)
                return _err(400, "TITLE_ALREADY_EXISTS",
                            f"A page with title {title!r} already exists "
                            f"in space {s['spaces'][sid].get('key','')}")
        new_id = _next(s, "page")
        coerced = _coerce_body(body, default_repr=representation or "storage")
        now = _now()
        page = {
            "id": new_id,
            "title": title,
            "spaceId": sid,
            "parentId": pid or None,
            "parentType": parent_type,
            "status": status if status in ("current", "draft") else "current",
            "authorId": s["self"]["accountId"],
            "ownerId": s["self"]["accountId"],
            "lastEditorId": s["self"]["accountId"],
            "createdAt": now,
            "lastEditedAt": now,
            "version": 1,
            "versionMessage": "",
            "minorEdit": False,
            "body": coerced,
            "labels": [],
            "properties": [],
        }
        s["pages"][new_id] = page
        s["page_versions"].setdefault(new_id, []).append({
            "number": 1,
            "title": title,
            "body": coerced,
            "authorId": s["self"]["accountId"],
            "createdAt": now,
            "minorEdit": False,
            "message": "",
        })
        # if this is the first page in the space, set it as homepage
        if not s["spaces"][sid].get("homepageId"):
            s["spaces"][sid]["homepageId"] = new_id
        _record(s, "create_page", id=new_id, spaceId=sid, title=title,
                parentId=pid or None)
        _save_state(s)
        return _public_page(s, page, body_format=coerced["representation"])


@mcp.tool(name="update_page")
def update_page(id: str,
                title: str = "",
                body: Any = None,
                status: str = "",
                version: dict | None = None,
                parentId: str = "",
                representation: str = "storage") -> dict:
    """Confluence v2: PUT /pages/{id} — update a page.

    Confluence requires the *new* version number (current + 1) under
    `version.number`; if omitted we auto-increment. Pass a string,
    a Confluence body dict, or omit body entirely.
    """
    with _lock():
        s = _load_state()
        p = s["pages"].get(str(id))
        if not p:
            _record(s, "update_page", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        new_version = p.get("version", 1) + 1
        if isinstance(version, dict) and version.get("number") is not None:
            requested = int(version["number"])
            if requested != new_version:
                _record(s, "update_page", id=id, requested=requested,
                        expected=new_version, result="version_conflict")
                _save_state(s)
                return _err(409, "VERSION_CONFLICT",
                            f"Version mismatch: expected {new_version}, "
                            f"got {requested}")
        if title:
            # title uniqueness in space (excluding self)
            for existing in s["pages"].values():
                if (existing["id"] != p["id"]
                        and existing.get("spaceId") == p.get("spaceId")
                        and existing.get("title") == title
                        and existing.get("status", "current") == "current"):
                    _record(s, "update_page", id=id, title=title,
                            result="title_conflict")
                    _save_state(s)
                    return _err(400, "TITLE_ALREADY_EXISTS",
                                f"A page with title {title!r} already "
                                f"exists in the space")
            p["title"] = title
        if body is not None:
            coerced = _coerce_body(body,
                                   default_repr=representation or "storage")
            p["body"] = coerced
        if status and status in ("current", "draft", "archived", "trashed"):
            p["status"] = status
        if parentId:
            parent = s["pages"].get(str(parentId))
            if not parent:
                _record(s, "update_page", id=id, parentId=parentId,
                        result="parent_not_found")
                _save_state(s)
                return _err(404, "NOT_FOUND",
                            f"No parent page found: {parentId}")
            p["parentId"] = str(parentId)
            p["parentType"] = "page"
        now = _now()
        p["version"] = new_version
        p["lastEditedAt"] = now
        p["lastEditorId"] = s["self"]["accountId"]
        if isinstance(version, dict):
            p["versionMessage"] = version.get("message", "") or ""
            p["minorEdit"] = bool(version.get("minorEdit"))
        s["page_versions"].setdefault(str(id), []).append({
            "number": new_version,
            "title": p["title"],
            "body": p.get("body"),
            "authorId": s["self"]["accountId"],
            "createdAt": now,
            "minorEdit": p.get("minorEdit", False),
            "message": p.get("versionMessage", ""),
        })
        _record(s, "update_page", id=id, version=new_version,
                title=p["title"])
        _save_state(s)
        body_fmt = (p.get("body") or {}).get("representation", "storage")
        return _public_page(s, p, body_format=body_fmt)


@mcp.tool(name="delete_page")
def delete_page(id: str, purge: bool = False, draft: bool = False) -> dict:
    """Confluence v2: DELETE /pages/{id} — move a page to trash (or
    purge it if `purge=True`). Returns an empty success envelope on
    success (matches the real API's 204 no-content semantics)."""
    with _lock():
        s = _load_state()
        p = s["pages"].get(str(id))
        if not p:
            _record(s, "delete_page", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        if purge:
            s["pages"].pop(str(id), None)
            s["page_versions"].pop(str(id), None)
            # remove from any homepage references
            for sp in s["spaces"].values():
                if sp.get("homepageId") == str(id):
                    sp["homepageId"] = None
            # reparent children to root (parentId=None)
            for q in s["pages"].values():
                if q.get("parentId") == str(id):
                    q["parentId"] = None
                    q["parentType"] = None
            _record(s, "delete_page", id=id, purged=True)
            _save_state(s)
            return {"deleted": True, "id": str(id), "purged": True}
        # soft delete: mark as trashed
        p["status"] = "trashed"
        p["lastEditedAt"] = _now()
        _record(s, "delete_page", id=id, purged=False)
        _save_state(s)
        return {"deleted": True, "id": str(id), "purged": False,
                "status": "trashed"}


@mcp.tool(name="get_page_children")
def get_page_children(id: str,
                      cursor: str = "",
                      limit: int = 25,
                      sort: str = "") -> dict:
    """Confluence v2: GET /pages/{id}/children — list direct child
    pages of a parent. Returns
    {"results": [...child page summary...], "_links": {...}}."""
    with _lock():
        s = _load_state()
        if str(id) not in s["pages"]:
            _record(s, "get_page_children", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No parent page found: {id}")
        children = [p for p in s["pages"].values()
                    if p.get("parentId") == str(id)
                    and p.get("status", "current") == "current"]
        if sort:
            field = sort.lstrip("-")
            reverse = sort.startswith("-")
            children.sort(key=lambda p: p.get(field, ""), reverse=reverse)
        else:
            children.sort(key=lambda p: int(p.get("position") or 0))
        page, next_cursor = _paginate(children, cursor, limit)
        # Confluence's children endpoint returns a slim representation
        # (no full body)
        results = []
        for p in page:
            results.append({
                "id": p["id"],
                "status": p.get("status", "current"),
                "title": p.get("title", ""),
                "spaceId": p.get("spaceId"),
                "parentId": p.get("parentId"),
                "parentType": p.get("parentType"),
                "position": p.get("position"),
                "authorId": p.get("authorId"),
                "ownerId": p.get("ownerId"),
                "lastOwnerId": p.get("lastOwnerId"),
                "createdAt": p.get("createdAt"),
                "childPosition": p.get("position"),
                "_links": _page_links(s, p["id"]),
            })
        _record(s, "get_page_children", id=id, count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/children?limit={limit}",
                next_cursor),
        }


@mcp.tool(name="get_page_versions")
def get_page_versions(id: str,
                      body_format: str = "",
                      cursor: str = "",
                      limit: int = 25,
                      sort: str = "-modified-date") -> dict:
    """Confluence v2: GET /pages/{id}/versions — list all historical
    versions of a page. Each entry is a version descriptor (number,
    authorId, createdAt, message, minorEdit)."""
    with _lock():
        s = _load_state()
        if str(id) not in s["pages"]:
            _record(s, "get_page_versions", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        versions = list(s.get("page_versions", {}).get(str(id), []))
        # default sort newest-first
        reverse = not sort.startswith("modified-date")
        versions.sort(key=lambda v: v.get("number", 0), reverse=reverse)
        page, next_cursor = _paginate(
            [{"id": str(v.get("number")), **v} for v in versions],
            cursor, limit)
        results = []
        for v in page:
            entry = {
                "number": v.get("number"),
                "authorId": v.get("authorId"),
                "createdAt": v.get("createdAt"),
                "message": v.get("message", ""),
                "minorEdit": v.get("minorEdit", False),
                "ncsStepVersion": None,
            }
            if body_format and v.get("body"):
                bv = v["body"].get("value", "")
                entry["body"] = {body_format: {"representation": body_format,
                                               "value": bv}}
            results.append(entry)
        _record(s, "get_page_versions", id=id, count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/versions?limit={limit}",
                next_cursor),
        }


# ---------------------------------------------------------------------------
# Blog posts
# ---------------------------------------------------------------------------

@mcp.tool(name="get_blog_posts")
def get_blog_posts(id: list[str] | None = None,
                   space_id: list[str] | None = None,
                   status: list[str] | None = None,
                   title: str = "",
                   body_format: str = "",
                   cursor: str = "",
                   limit: int = 25) -> dict:
    """Confluence v2: GET /blogposts — list blog posts, optionally
    filtered by id, spaceId, status, or title."""
    with _lock():
        s = _load_state()
        items = list(s["blog_posts"].values())
        if id:
            wanted = {str(i) for i in id}
            items = [b for b in items if str(b.get("id")) in wanted]
        if space_id:
            wanted_sids = set()
            for sref in space_id:
                sid = _resolve_space(s, sref)
                if sid:
                    wanted_sids.add(sid)
            items = [b for b in items if b.get("spaceId") in wanted_sids]
        if status:
            wanted_st = set(status)
            items = [b for b in items if b.get("status", "current") in wanted_st]
        else:
            items = [b for b in items if b.get("status", "current") == "current"]
        if title:
            items = [b for b in items if b.get("title") == title]
        items.sort(key=lambda b: str(b.get("id", "")))
        page, next_cursor = _paginate(items, cursor, limit)
        results = [_public_blog(s, b, body_format=body_format or None)
                   for b in page]
        _record(s, "get_blog_posts", count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/blogposts?limit={limit}", next_cursor),
        }


@mcp.tool(name="create_blog_post")
def create_blog_post(spaceId: str,
                     title: str,
                     body: Any = None,
                     status: str = "current",
                     representation: str = "storage") -> dict:
    """Confluence v2: POST /blogposts — create a new blog post in a
    space. Body shape matches `create_page`."""
    with _lock():
        s = _load_state()
        sid = _resolve_space(s, spaceId)
        if not sid:
            _record(s, "create_blog_post", spaceId=spaceId,
                    result="space_not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No space found with id or key: {spaceId}")
        new_id = _next(s, "blog")
        coerced = _coerce_body(body, default_repr=representation or "storage")
        now = _now()
        blog = {
            "id": new_id,
            "title": title,
            "spaceId": sid,
            "status": status if status in ("current", "draft") else "current",
            "authorId": s["self"]["accountId"],
            "lastEditorId": s["self"]["accountId"],
            "createdAt": now,
            "lastEditedAt": now,
            "version": 1,
            "versionMessage": "",
            "minorEdit": False,
            "body": coerced,
            "labels": [],
        }
        s["blog_posts"][new_id] = blog
        _record(s, "create_blog_post", id=new_id, spaceId=sid, title=title)
        _save_state(s)
        return _public_blog(s, blog, body_format=coerced["representation"])


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="get_page_footer_comments")
def get_page_footer_comments(id: str,
                             body_format: str = "storage",
                             cursor: str = "",
                             limit: int = 25,
                             sort: str = "") -> dict:
    """Confluence v2: GET /pages/{id}/footer-comments — list the
    footer (page-level) comments on a page."""
    with _lock():
        s = _load_state()
        if str(id) not in s["pages"]:
            _record(s, "get_page_footer_comments", id=id,
                    result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        comments = [c for c in s["comments"].values()
                    if c.get("pageId") == str(id)
                    and c.get("commentType", "footer") == "footer"
                    and c.get("status", "current") == "current"]
        comments.sort(key=lambda c: c.get("createdAt", ""))
        page, next_cursor = _paginate(comments, cursor, limit)
        results = [_public_comment(s, c, body_format=body_format or "storage")
                   for c in page]
        _record(s, "get_page_footer_comments", id=id, count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/footer-comments?limit={limit}",
                next_cursor),
        }


@mcp.tool(name="create_footer_comment")
def create_footer_comment(pageId: str = "",
                          blogPostId: str = "",
                          parentCommentId: str = "",
                          body: Any = None,
                          representation: str = "storage") -> dict:
    """Confluence v2: POST /footer-comments — create a footer
    (page-level) comment.

    Exactly one of `pageId`, `blogPostId`, or `parentCommentId` must
    be provided. `body` is the comment text (string treated as
    storage XHTML, or a Confluence body dict).
    """
    with _lock():
        s = _load_state()
        if not (pageId or blogPostId or parentCommentId):
            return _err(400, "INVALID_REQUEST",
                        "One of pageId, blogPostId, parentCommentId required")
        if pageId and str(pageId) not in s["pages"]:
            _record(s, "create_footer_comment", pageId=pageId,
                    result="page_not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {pageId}")
        if blogPostId and str(blogPostId) not in s["blog_posts"]:
            _record(s, "create_footer_comment", blogPostId=blogPostId,
                    result="blog_not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No blog post found with id: {blogPostId}")
        parent_page_id: str | None = None
        parent_blog_id: str | None = None
        if parentCommentId:
            parent_c = s["comments"].get(str(parentCommentId))
            if not parent_c:
                _record(s, "create_footer_comment",
                        parentCommentId=parentCommentId,
                        result="parent_comment_not_found")
                _save_state(s)
                return _err(404, "NOT_FOUND",
                            f"No parent comment: {parentCommentId}")
            parent_page_id = parent_c.get("pageId")
            parent_blog_id = parent_c.get("blogPostId")
        new_id = _next(s, "comment")
        coerced = _coerce_body(body, default_repr=representation or "storage")
        now = _now()
        c = {
            "id": new_id,
            "status": "current",
            "title": "",
            "pageId": pageId or parent_page_id,
            "blogPostId": blogPostId or parent_blog_id,
            "parentCommentId": parentCommentId or None,
            "commentType": "footer",
            "body": coerced,
            "version": 1,
            "authorId": s["self"]["accountId"],
            "createdAt": now,
        }
        s["comments"][new_id] = c
        _record(s, "create_footer_comment", id=new_id,
                pageId=c["pageId"], blogPostId=c["blogPostId"],
                parentCommentId=c["parentCommentId"])
        _save_state(s)
        return _public_comment(s, c, body_format=coerced["representation"])


@mcp.tool(name="get_page_inline_comments")
def get_page_inline_comments(id: str,
                             body_format: str = "storage",
                             cursor: str = "",
                             limit: int = 25,
                             sort: str = "",
                             status: list[str] | None = None) -> dict:
    """Confluence v2: GET /pages/{id}/inline-comments — list inline
    comments (comments anchored to highlighted text) on a page."""
    with _lock():
        s = _load_state()
        if str(id) not in s["pages"]:
            _record(s, "get_page_inline_comments", id=id,
                    result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        want_status = set(status) if status else {"open"}
        comments = [c for c in s["comments"].values()
                    if c.get("pageId") == str(id)
                    and c.get("commentType") == "inline"
                    and c.get("resolutionStatus", "open") in want_status]
        comments.sort(key=lambda c: c.get("createdAt", ""))
        page, next_cursor = _paginate(comments, cursor, limit)
        results = []
        for c in page:
            pub = _public_comment(s, c, body_format=body_format or "storage")
            pub["properties"] = {
                "inlineMarkerRef": c.get("inlineMarkerRef", ""),
                "inlineOriginalSelection": c.get("inlineOriginalSelection",
                                                 ""),
            }
            pub["resolutionLastModifierId"] = c.get(
                "resolutionLastModifierId")
            pub["resolutionLastModifiedAt"] = c.get(
                "resolutionLastModifiedAt")
            pub["resolutionStatus"] = c.get("resolutionStatus", "open")
            results.append(pub)
        _record(s, "get_page_inline_comments", id=id,
                count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/inline-comments?limit={limit}",
                next_cursor),
        }


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@mcp.tool(name="get_page_labels")
def get_page_labels(id: str,
                    prefix: str = "",
                    cursor: str = "",
                    limit: int = 25,
                    sort: str = "") -> dict:
    """Confluence v2: GET /pages/{id}/labels — list labels attached
    to a page."""
    with _lock():
        s = _load_state()
        p = s["pages"].get(str(id))
        if not p:
            _record(s, "get_page_labels", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        ids = p.get("labels", [])
        labels = [s["labels"][lid] for lid in ids if lid in s["labels"]]
        if prefix:
            labels = [l for l in labels if l.get("prefix") == prefix]
        labels.sort(key=lambda l: l.get("name", ""))
        page, next_cursor = _paginate(labels, cursor, limit)
        results = [_public_label(l) for l in page]
        _record(s, "get_page_labels", id=id, count=len(results))
        _save_state(s)
        return {
            "results": results,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/labels?limit={limit}",
                next_cursor),
        }


@mcp.tool(name="add_label_to_page")
def add_label_to_page(id: str,
                      labels: list[dict] | dict | str | list[str] = None,
                      prefix: str = "global") -> dict:
    """Confluence v1-style helper exposed in v2 mock:
    POST /content/{id}/label — add one or more labels to a page.

    `labels` accepts:
      - "tagname"
      - ["tag1", "tag2"]
      - {"name": "tag1", "prefix": "global"}
      - [{"name":"tag1", "prefix":"team"}, ...]
    """
    with _lock():
        s = _load_state()
        p = s["pages"].get(str(id))
        if not p:
            _record(s, "add_label_to_page", id=id, result="not_found")
            _save_state(s)
            return _err(404, "NOT_FOUND",
                        f"No page found with id: {id}")
        # normalize labels argument
        raw: list[dict] = []
        if labels is None:
            return _err(400, "INVALID_REQUEST", "labels required")
        if isinstance(labels, str):
            raw = [{"name": labels, "prefix": prefix}]
        elif isinstance(labels, dict):
            raw = [{"name": labels.get("name", ""),
                    "prefix": labels.get("prefix", prefix)}]
        elif isinstance(labels, list):
            for item in labels:
                if isinstance(item, str):
                    raw.append({"name": item, "prefix": prefix})
                elif isinstance(item, dict):
                    raw.append({"name": item.get("name", ""),
                                "prefix": item.get("prefix", prefix)})
        raw = [l for l in raw if l.get("name")]
        if not raw:
            return _err(400, "INVALID_REQUEST",
                        "labels must contain at least one name")
        existing_ids = set(p.get("labels", []))
        added_ids: list[str] = []
        for lbl in raw:
            key = f"{lbl['prefix']}:{lbl['name']}"
            lid = s["label_names"].get(key)
            if not lid:
                lid = _next(s, "label")
                s["labels"][lid] = {
                    "id": lid,
                    "name": lbl["name"],
                    "prefix": lbl["prefix"],
                }
                s["label_names"][key] = lid
            if lid not in existing_ids:
                existing_ids.add(lid)
                added_ids.append(lid)
        p["labels"] = sorted(existing_ids)
        _record(s, "add_label_to_page", id=id,
                added=[s["labels"][lid]["name"] for lid in added_ids])
        _save_state(s)
        all_labels = [_public_label(s["labels"][lid])
                      for lid in p["labels"]]
        return {
            "results": all_labels,
            "_links": _list_links(
                f"/wiki/api/v2/pages/{id}/labels", None),
        }


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state. Not part of the
    real Confluence REST surface; for verifier introspection."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(site: dict | None = None,
                    self_user: dict | None = None,
                    users: list | None = None,
                    spaces: list | None = None,
                    pages: list | None = None,
                    blog_posts: list | None = None,
                    comments: list | None = None,
                    labels: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed state with Confluence-shaped objects.

    Input shapes (all id fields optional — auto-generated if omitted):
      - spaces:  [{id?, key, name, type?, status?, homepageId?}]
      - pages:   [{id?, spaceId|spaceKey, title, body?, parentId?,
                    status?, labels?: ["name"|{"name","prefix"}]}]
      - blog_posts: [{id?, spaceId|spaceKey, title, body?, status?}]
      - comments: [{id?, pageId|blogPostId, body, parentCommentId?,
                    commentType?: "footer"|"inline",
                    inlineMarkerRef?, inlineOriginalSelection?}]
      - labels:   [{id?, name, prefix?}]   (workspace catalog only)

    If `replace=True`, the state is fully reset first."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if site:
            s["site"].update(site)
        if self_user:
            s["self"].update(self_user)
            aid = self_user.get("accountId")
            if aid and aid not in s["users"]:
                s["users"][aid] = {**s["self"]}
        for u in users or []:
            aid = u.get("accountId") or f"557058:{_next(s, 'page')}-user"
            s["users"][aid] = {
                "accountId": aid,
                "accountType": u.get("accountType", "atlassian"),
                "displayName": u.get("displayName", aid),
                "email": u.get("email", ""),
            }
        for sp in spaces or []:
            sid = sp.get("id") or _next(s, "space")
            key = sp.get("key") or f"SPACE{sid}"
            now = _now()
            s["spaces"][sid] = {
                "id": sid,
                "key": key,
                "name": sp.get("name", key),
                "type": sp.get("type", "global"),
                "status": sp.get("status", "current"),
                "authorId": sp.get("authorId", s["self"]["accountId"]),
                "createdAt": sp.get("createdAt", now),
                "homepageId": sp.get("homepageId"),
                "description": sp.get("description"),
                "icon": sp.get("icon"),
            }
            s["space_keys"][key] = sid
        for lbl in labels or []:
            lid = lbl.get("id") or _next(s, "label")
            name = lbl.get("name", lid)
            prefix = lbl.get("prefix", "global")
            s["labels"][lid] = {"id": lid, "name": name, "prefix": prefix}
            s["label_names"][f"{prefix}:{name}"] = lid
        for p in pages or []:
            pid = str(p.get("id") or _next(s, "page"))
            sid = (_resolve_space(s, p.get("spaceId", ""))
                   or _resolve_space(s, p.get("spaceKey", "")))
            if not sid:
                continue
            now = _now()
            coerced = _coerce_body(p.get("body"))
            # normalize label refs to ids
            label_ids: list[str] = []
            for lref in p.get("labels", []) or []:
                if isinstance(lref, str):
                    key = f"global:{lref}"
                    lid = s["label_names"].get(key)
                    if not lid:
                        lid = _next(s, "label")
                        s["labels"][lid] = {"id": lid, "name": lref,
                                            "prefix": "global"}
                        s["label_names"][key] = lid
                    label_ids.append(lid)
                elif isinstance(lref, dict):
                    name = lref.get("name", "")
                    prefix = lref.get("prefix", "global")
                    key = f"{prefix}:{name}"
                    lid = s["label_names"].get(key)
                    if not lid:
                        lid = _next(s, "label")
                        s["labels"][lid] = {"id": lid, "name": name,
                                            "prefix": prefix}
                        s["label_names"][key] = lid
                    label_ids.append(lid)
            page = {
                "id": pid,
                "title": p.get("title", f"Page {pid}"),
                "spaceId": sid,
                "parentId": (str(p["parentId"]) if p.get("parentId")
                             else None),
                "parentType": ("page" if p.get("parentId") else None),
                "status": p.get("status", "current"),
                "authorId": p.get("authorId", s["self"]["accountId"]),
                "ownerId": p.get("ownerId",
                                 p.get("authorId", s["self"]["accountId"])),
                "lastEditorId": p.get("lastEditorId",
                                      s["self"]["accountId"]),
                "createdAt": p.get("createdAt", now),
                "lastEditedAt": p.get("lastEditedAt", now),
                "version": p.get("version", 1),
                "versionMessage": p.get("versionMessage", ""),
                "minorEdit": False,
                "body": coerced,
                "labels": label_ids,
                "properties": p.get("properties", []),
                "position": p.get("position"),
            }
            s["pages"][pid] = page
            s["page_versions"].setdefault(pid, []).append({
                "number": page["version"],
                "title": page["title"],
                "body": coerced,
                "authorId": page["authorId"],
                "createdAt": page["createdAt"],
                "minorEdit": False,
                "message": "",
            })
            if not s["spaces"][sid].get("homepageId"):
                s["spaces"][sid]["homepageId"] = pid
        for b in blog_posts or []:
            bid = str(b.get("id") or _next(s, "blog"))
            sid = (_resolve_space(s, b.get("spaceId", ""))
                   or _resolve_space(s, b.get("spaceKey", "")))
            if not sid:
                continue
            now = _now()
            s["blog_posts"][bid] = {
                "id": bid,
                "title": b.get("title", f"Blog {bid}"),
                "spaceId": sid,
                "status": b.get("status", "current"),
                "authorId": b.get("authorId", s["self"]["accountId"]),
                "lastEditorId": b.get("lastEditorId",
                                      s["self"]["accountId"]),
                "createdAt": b.get("createdAt", now),
                "lastEditedAt": b.get("lastEditedAt", now),
                "version": b.get("version", 1),
                "versionMessage": "",
                "minorEdit": False,
                "body": _coerce_body(b.get("body")),
                "labels": [],
            }
        for c in comments or []:
            cid = str(c.get("id") or _next(s, "comment"))
            now = _now()
            entry = {
                "id": cid,
                "status": c.get("status", "current"),
                "title": c.get("title", ""),
                "pageId": str(c["pageId"]) if c.get("pageId") else None,
                "blogPostId": (str(c["blogPostId"])
                               if c.get("blogPostId") else None),
                "parentCommentId": (str(c["parentCommentId"])
                                    if c.get("parentCommentId") else None),
                "commentType": c.get("commentType", "footer"),
                "body": _coerce_body(c.get("body")),
                "version": c.get("version", 1),
                "authorId": c.get("authorId", s["self"]["accountId"]),
                "createdAt": c.get("createdAt", now),
            }
            if entry["commentType"] == "inline":
                entry["inlineMarkerRef"] = c.get("inlineMarkerRef", "")
                entry["inlineOriginalSelection"] = c.get(
                    "inlineOriginalSelection", "")
                entry["resolutionStatus"] = c.get("resolutionStatus", "open")
            s["comments"][cid] = entry
        _record(s, "debug_seed",
                counts={"spaces": len(spaces or []),
                        "pages": len(pages or []),
                        "blog_posts": len(blog_posts or []),
                        "comments": len(comments or []),
                        "labels": len(labels or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "space_ids": list(s["spaces"].keys()),
            "page_ids": list(s["pages"].keys()),
            "blog_post_ids": list(s["blog_posts"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
