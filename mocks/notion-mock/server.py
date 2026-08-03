"""Notion mock MCP server.

Exposes the same tool surface as @notionhq/notion-mcp-server. That
server is generated from Notion's OpenAPI spec at
github.com/makenotion/notion-mcp-server/scripts/notion-openapi.json,
so every tool here is named `API-<operationId>` and accepts the same
parameter shape as the real Notion REST API.

Backed by a single JSON state file (default
$NOTION_MOCK_STATE_DIR/state.json) that holds all Notion objects
(pages, blocks, databases, data sources, users, comments) plus a
call log used by the verifier.

Responses match Notion's REST shapes (`object`, `id`, `created_time`,
`last_edited_time`, `archived`, `parent`, `properties`, ...). Errors
are returned as Notion error objects, not raised, so the trace looks
like a real failed HTTP response:
    {"object":"error","status":404,"code":"object_not_found",
     "message":"..."}

The state file is seeded from $NOTION_MOCK_SEED_PATH at process
start if no state file exists yet (per-rollout isolation should
clear the state dir between rollouts).
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP


NOTION_VERSION = "2022-06-28"


def _state_path() -> str:
    state_dir = os.environ.get(
        "NOTION_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/notion_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _empty_state() -> dict:
    bot_id = "00000000-0000-0000-0000-00000000b07"
    return {
        "version": NOTION_VERSION,
        "self": {
            "object": "user",
            "id": bot_id,
            "name": "Mock Bot",
            "type": "bot",
            "bot": {"owner": {"type": "workspace", "workspace": True},
                    "workspace_name": "Mock Workspace"},
        },
        "users": {
            bot_id: {
                "object": "user", "id": bot_id, "name": "Mock Bot",
                "type": "bot",
                "bot": {"owner": {"type": "workspace", "workspace": True}},
            },
        },
        "objects": {},
        "comments": {},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("NOTION_MOCK_SEED_PATH")
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


def _err(status: int, code: str, message: str) -> dict:
    """Return a Notion-shaped error object (matches real REST error body)."""
    return {
        "object": "error",
        "status": status,
        "code": code,
        "message": message,
        "request_id": _new_id(),
    }


def _get_obj(state: dict, obj_id: str) -> dict | None:
    return state["objects"].get(obj_id)


def _strip(obj: dict) -> dict:
    """Return a shallow copy without our internal `children` list."""
    out = dict(obj)
    out.pop("_children", None)
    return out


def _touch(obj: dict) -> None:
    obj["last_edited_time"] = _now()
    obj["last_edited_by"] = {"object": "user",
                             "id": "00000000-0000-0000-0000-00000000b07"}


def _make_page(parent: dict, properties: dict | None,
               icon: dict | None, cover: dict | None) -> dict:
    pid = _new_id()
    now = _now()
    return {
        "object": "page",
        "id": pid,
        "created_time": now,
        "last_edited_time": now,
        "created_by": {"object": "user",
                       "id": "00000000-0000-0000-0000-00000000b07"},
        "last_edited_by": {"object": "user",
                           "id": "00000000-0000-0000-0000-00000000b07"},
        "cover": cover,
        "icon": icon,
        "parent": parent,
        "archived": False,
        "in_trash": False,
        "properties": properties or {},
        "url": f"https://www.notion.so/{pid.replace('-', '')}",
        "public_url": None,
        "_children": [],
    }


def _make_block(parent_id: str, block_type: str, payload: dict) -> dict:
    bid = _new_id()
    now = _now()
    block = {
        "object": "block",
        "id": bid,
        "parent": {"type": "block_id", "block_id": parent_id}
        if parent_id else {"type": "workspace", "workspace": True},
        "created_time": now,
        "last_edited_time": now,
        "created_by": {"object": "user",
                       "id": "00000000-0000-0000-0000-00000000b07"},
        "last_edited_by": {"object": "user",
                           "id": "00000000-0000-0000-0000-00000000b07"},
        "has_children": False,
        "archived": False,
        "in_trash": False,
        "type": block_type,
        block_type: payload,
        "_children": [],
    }
    return block


def _resolve_parent(state: dict, parent: dict) -> tuple[str | None, dict | None]:
    """Return (parent_id, parent_obj) for a Notion-shaped parent dict."""
    if not isinstance(parent, dict):
        return None, None
    if parent.get("type") == "page_id" or "page_id" in parent:
        pid = parent.get("page_id")
    elif parent.get("type") == "database_id" or "database_id" in parent:
        pid = parent.get("database_id")
    elif (parent.get("type") == "data_source_id"
          or "data_source_id" in parent):
        pid = parent.get("data_source_id")
    elif parent.get("type") == "block_id" or "block_id" in parent:
        pid = parent.get("block_id")
    elif parent.get("type") == "workspace":
        return None, None
    else:
        return None, None
    return pid, state["objects"].get(pid) if pid else None


mcp = FastMCP("notion-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool(name="API-get-self")
def api_get_self() -> dict:
    """Notion REST: GET /v1/users/me — retrieve the bot user
    associated with the current integration token."""
    with _lock():
        s = _load_state()
        _record(s, "get_self")
        _save_state(s)
        return dict(s["self"])


@mcp.tool(name="API-get-users")
def api_get_users(start_cursor: str | None = None,
                  page_size: int = 100) -> dict:
    """Notion REST: GET /v1/users — list all users in the workspace.
    Paginated via `start_cursor` + `page_size` (max 100)."""
    with _lock():
        s = _load_state()
        users = list(s["users"].values())
        users.sort(key=lambda u: u["id"])
        page_size = min(max(int(page_size or 100), 1), 100)
        start = 0
        if start_cursor:
            for i, u in enumerate(users):
                if u["id"] == start_cursor:
                    start = i
                    break
        page = users[start: start + page_size]
        next_cursor = (page[-1]["id"]
                       if len(users) > start + page_size and page else None)
        _record(s, "get_users", page_size=page_size,
                start_cursor=start_cursor)
        _save_state(s)
        return {
            "object": "list",
            "results": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "type": "user",
            "user": {},
        }


@mcp.tool(name="API-get-user")
def api_get_user(user_id: str) -> dict:
    """Notion REST: GET /v1/users/{user_id} — retrieve a single user."""
    with _lock():
        s = _load_state()
        u = s["users"].get(user_id)
        _record(s, "get_user", user_id=user_id,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            return _err(404, "object_not_found",
                        f"Could not find user with ID: {user_id}")
        return dict(u)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool(name="API-post-search")
def api_post_search(query: str | None = None,
                    sort: dict | None = None,
                    filter: dict | None = None,
                    start_cursor: str | None = None,
                    page_size: int = 100) -> dict:
    """Notion REST: POST /v1/search — search across pages and
    databases the integration has access to.

    `filter` may be {"value":"page"|"database","property":"object"}.
    `query` is a case-insensitive substring match against the object's
    title text. Returns a paginated list of page/database objects.
    """
    with _lock():
        s = _load_state()
        q = (query or "").lower().strip()
        kind = None
        if isinstance(filter, dict) and filter.get("property") == "object":
            kind = filter.get("value")
        results = []
        for obj in s["objects"].values():
            if obj.get("archived") or obj.get("in_trash"):
                continue
            if kind and obj.get("object") != kind:
                continue
            if obj.get("object") not in ("page", "database"):
                continue
            title = _extract_title(obj).lower()
            if q and q not in title:
                continue
            results.append(_strip(obj))
        results.sort(key=lambda o: o.get("last_edited_time") or "",
                     reverse=True)
        page_size = min(max(int(page_size or 100), 1), 100)
        start = 0
        if start_cursor:
            for i, o in enumerate(results):
                if o["id"] == start_cursor:
                    start = i
                    break
        page = results[start: start + page_size]
        next_cursor = (page[-1]["id"]
                       if len(results) > start + page_size and page else None)
        _record(s, "post_search", query=query, filter=filter,
                count=len(page))
        _save_state(s)
        return {
            "object": "list",
            "results": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "type": "page_or_database",
            "page_or_database": {},
        }


def _extract_title(obj: dict) -> str:
    """Pull a plain-text title out of a page/database properties dict."""
    props = obj.get("properties") or {}
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title":
            parts = v.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts
                           if isinstance(p, dict))
    if obj.get("object") == "database":
        title_parts = obj.get("title", [])
        return "".join(p.get("plain_text", "") for p in title_parts
                       if isinstance(p, dict))
    return ""


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@mcp.tool(name="API-post-page")
def api_post_page(parent: dict,
                  properties: dict | None = None,
                  children: list | None = None,
                  icon: dict | None = None,
                  cover: dict | None = None) -> dict:
    """Notion REST: POST /v1/pages — create a new page.

    `parent` must be one of: {type:"page_id", page_id}, {type:
    "database_id", database_id}, or {type:"data_source_id",
    data_source_id}. `properties` must match the parent database's
    schema if parent is a database. `children` is an optional initial
    block list (each element is a Notion block object with `type` +
    `<type>` payload).
    """
    with _lock():
        s = _load_state()
        parent_id, parent_obj = _resolve_parent(s, parent)
        if parent_id and not parent_obj:
            return _err(404, "object_not_found",
                        f"Could not find parent: {parent_id}")
        page = _make_page(parent or {"type": "workspace", "workspace": True},
                          properties, icon, cover)
        s["objects"][page["id"]] = page
        if parent_obj is not None:
            parent_obj.setdefault("_children", []).append(page["id"])
        for ch in children or []:
            block = _coerce_and_store_block(s, page["id"], ch)
            if isinstance(block, dict) and block.get("object") == "error":
                return block
        _record(s, "post_page", page_id=page["id"], parent=parent)
        _save_state(s)
        return _strip(page)


@mcp.tool(name="API-retrieve-a-page")
def api_retrieve_a_page(page_id: str,
                        filter_properties: list | None = None) -> dict:
    """Notion REST: GET /v1/pages/{page_id} — retrieve a page object,
    optionally limited to a subset of `filter_properties`."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, page_id)
        _record(s, "retrieve_page", page_id=page_id,
                result="ok" if obj and obj.get("object") == "page"
                else "not_found")
        _save_state(s)
        if not obj or obj.get("object") != "page":
            return _err(404, "object_not_found",
                        f"Could not find page with ID: {page_id}")
        out = _strip(obj)
        if filter_properties:
            out = dict(out)
            out["properties"] = {k: v for k, v in out["properties"].items()
                                 if k in filter_properties}
        return out


@mcp.tool(name="API-patch-page")
def api_patch_page(page_id: str,
                   properties: dict | None = None,
                   archived: bool | None = None,
                   in_trash: bool | None = None,
                   icon: dict | None = None,
                   cover: dict | None = None) -> dict:
    """Notion REST: PATCH /v1/pages/{page_id} — update page
    properties, archive, or trash the page."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, page_id)
        if not obj or obj.get("object") != "page":
            _record(s, "patch_page", page_id=page_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find page with ID: {page_id}")
        if properties:
            obj.setdefault("properties", {}).update(properties)
        if archived is not None:
            obj["archived"] = bool(archived)
        if in_trash is not None:
            obj["in_trash"] = bool(in_trash)
        if icon is not None:
            obj["icon"] = icon
        if cover is not None:
            obj["cover"] = cover
        _touch(obj)
        _record(s, "patch_page", page_id=page_id, archived=archived,
                in_trash=in_trash, property_keys=list((properties or {}).keys()))
        _save_state(s)
        return _strip(obj)


@mcp.tool(name="API-retrieve-a-page-property")
def api_retrieve_a_page_property(page_id: str, property_id: str,
                                 page_size: int = 100,
                                 start_cursor: str | None = None) -> dict:
    """Notion REST: GET /v1/pages/{page_id}/properties/{property_id}
    — retrieve a single property value."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, page_id)
        if not obj or obj.get("object") != "page":
            _record(s, "retrieve_page_property", page_id=page_id,
                    property_id=property_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find page with ID: {page_id}")
        props = obj.get("properties", {})
        prop = props.get(property_id)
        if prop is None:
            for v in props.values():
                if isinstance(v, dict) and v.get("id") == property_id:
                    prop = v
                    break
        _record(s, "retrieve_page_property", page_id=page_id,
                property_id=property_id,
                result="ok" if prop else "not_found")
        _save_state(s)
        if prop is None:
            return _err(404, "object_not_found",
                        f"Could not find property: {property_id}")
        return dict(prop)


@mcp.tool(name="API-move-page")
def api_move_page(page_id: str, parent: dict) -> dict:
    """Notion REST: POST /v1/pages/{page_id}/move — move a page
    under a new parent (page or workspace)."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, page_id)
        if not obj or obj.get("object") != "page":
            _record(s, "move_page", page_id=page_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find page with ID: {page_id}")
        new_parent_id, new_parent_obj = _resolve_parent(s, parent)
        if new_parent_id and not new_parent_obj:
            _record(s, "move_page", page_id=page_id,
                    result="parent_not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find parent: {new_parent_id}")
        old_parent_id, old_parent_obj = _resolve_parent(s,
                                                       obj.get("parent", {}))
        if old_parent_obj is not None:
            ch = old_parent_obj.get("_children", [])
            if page_id in ch:
                ch.remove(page_id)
        obj["parent"] = parent
        if new_parent_obj is not None:
            new_parent_obj.setdefault("_children", []).append(page_id)
        _touch(obj)
        _record(s, "move_page", page_id=page_id, new_parent=parent)
        _save_state(s)
        return _strip(obj)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def _coerce_and_store_block(state: dict, parent_id: str,
                            block_in: dict) -> dict:
    """Validate a user-supplied block dict, persist it, return it."""
    if not isinstance(block_in, dict):
        return _err(400, "validation_error",
                    "block payload must be an object")
    btype = block_in.get("type")
    if not btype:
        for k in block_in:
            if k not in ("object", "type", "id", "parent",
                         "created_time", "last_edited_time",
                         "has_children", "archived"):
                btype = k
                break
    if not btype:
        return _err(400, "validation_error",
                    "block requires a `type` and a `<type>` payload")
    payload = block_in.get(btype, {})
    block = _make_block(parent_id, btype, payload)
    state["objects"][block["id"]] = block
    parent = state["objects"].get(parent_id)
    if parent is not None:
        parent.setdefault("_children", []).append(block["id"])
        if parent.get("object") == "block":
            parent["has_children"] = True
    return block


@mcp.tool(name="API-patch-block-children")
def api_patch_block_children(block_id: str,
                             children: list,
                             after: str | None = None) -> dict:
    """Notion REST: PATCH /v1/blocks/{block_id}/children — append
    block children to a parent (page or block). `after` inserts the
    new blocks after a specific existing child id."""
    with _lock():
        s = _load_state()
        parent = _get_obj(s, block_id)
        if not parent:
            _record(s, "patch_block_children", block_id=block_id,
                    result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find block with ID: {block_id}")
        created = []
        for ch in (children or []):
            block = _coerce_and_store_block(s, block_id, ch)
            if isinstance(block, dict) and block.get("object") == "error":
                return block
            created.append(block["id"])
        if after:
            ids = parent.get("_children", [])
            new = [i for i in ids if i not in created]
            try:
                idx = new.index(after) + 1
            except ValueError:
                idx = len(new)
            parent["_children"] = new[:idx] + created + new[idx:]
        _touch(parent)
        _record(s, "patch_block_children", block_id=block_id,
                count=len(created))
        _save_state(s)
        return {
            "object": "list",
            "results": [_strip(s["objects"][i]) for i in created],
            "next_cursor": None,
            "has_more": False,
            "type": "block",
            "block": {},
        }


@mcp.tool(name="API-get-block-children")
def api_get_block_children(block_id: str,
                           start_cursor: str | None = None,
                           page_size: int = 100) -> dict:
    """Notion REST: GET /v1/blocks/{block_id}/children — list a
    parent block (or page)'s children. Paginated."""
    with _lock():
        s = _load_state()
        parent = _get_obj(s, block_id)
        if not parent:
            _record(s, "get_block_children", block_id=block_id,
                    result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find block with ID: {block_id}")
        ids = [i for i in parent.get("_children", [])
               if s["objects"].get(i, {}).get("object") == "block"
               and not s["objects"][i].get("archived")]
        page_size = min(max(int(page_size or 100), 1), 100)
        start = 0
        if start_cursor:
            try:
                start = ids.index(start_cursor)
            except ValueError:
                start = 0
        page_ids = ids[start: start + page_size]
        next_cursor = (page_ids[-1]
                       if len(ids) > start + page_size and page_ids else None)
        _record(s, "get_block_children", block_id=block_id,
                count=len(page_ids))
        _save_state(s)
        return {
            "object": "list",
            "results": [_strip(s["objects"][i]) for i in page_ids],
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "type": "block",
            "block": {},
        }


@mcp.tool(name="API-retrieve-a-block")
def api_retrieve_a_block(block_id: str) -> dict:
    """Notion REST: GET /v1/blocks/{block_id} — retrieve a single
    block."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, block_id)
        _record(s, "retrieve_block", block_id=block_id,
                result="ok" if obj and obj.get("object") == "block"
                else "not_found")
        _save_state(s)
        if not obj or obj.get("object") != "block":
            return _err(404, "object_not_found",
                        f"Could not find block with ID: {block_id}")
        return _strip(obj)


@mcp.tool(name="API-update-a-block")
def api_update_a_block(block_id: str,
                       archived: bool | None = None,
                       **block_payload: Any) -> dict:
    """Notion REST: PATCH /v1/blocks/{block_id} — update a block's
    type-specific payload or archive flag. Extra kwargs are merged
    into the block's type payload (e.g. paragraph={"rich_text":[...]})."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, block_id)
        if not obj or obj.get("object") != "block":
            _record(s, "update_block", block_id=block_id,
                    result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find block with ID: {block_id}")
        if archived is not None:
            obj["archived"] = bool(archived)
        btype = obj["type"]
        for k, v in block_payload.items():
            if k == btype and isinstance(v, dict):
                obj[btype] = {**obj.get(btype, {}), **v}
        _touch(obj)
        _record(s, "update_block", block_id=block_id, archived=archived,
                payload_keys=list(block_payload.keys()))
        _save_state(s)
        return _strip(obj)


@mcp.tool(name="API-delete-a-block")
def api_delete_a_block(block_id: str) -> dict:
    """Notion REST: DELETE /v1/blocks/{block_id} — set the block's
    `archived` (in-trash) flag. Returns the updated block."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, block_id)
        if not obj or obj.get("object") != "block":
            _record(s, "delete_block", block_id=block_id,
                    result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find block with ID: {block_id}")
        obj["archived"] = True
        obj["in_trash"] = True
        _touch(obj)
        _record(s, "delete_block", block_id=block_id)
        _save_state(s)
        return _strip(obj)


# ---------------------------------------------------------------------------
# Databases & Data sources
# ---------------------------------------------------------------------------

@mcp.tool(name="API-retrieve-a-database")
def api_retrieve_a_database(database_id: str) -> dict:
    """Notion REST: GET /v1/databases/{database_id} — retrieve a
    database object. Includes `data_sources` array of attached
    data-source descriptors."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, database_id)
        _record(s, "retrieve_database", database_id=database_id,
                result="ok" if obj and obj.get("object") == "database"
                else "not_found")
        _save_state(s)
        if not obj or obj.get("object") != "database":
            return _err(404, "object_not_found",
                        f"Could not find database with ID: {database_id}")
        return _strip(obj)


@mcp.tool(name="API-retrieve-a-data-source")
def api_retrieve_a_data_source(data_source_id: str) -> dict:
    """Notion REST: GET /v1/data_sources/{data_source_id} — retrieve a
    data-source schema. A data source is the new (2025-09) layer
    holding the actual property schema for a database."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, data_source_id)
        _record(s, "retrieve_data_source", data_source_id=data_source_id,
                result="ok" if obj and obj.get("object") == "data_source"
                else "not_found")
        _save_state(s)
        if not obj or obj.get("object") != "data_source":
            return _err(404, "object_not_found",
                        f"Could not find data source with ID: {data_source_id}")
        return _strip(obj)


@mcp.tool(name="API-create-a-data-source")
def api_create_a_data_source(parent: dict,
                             properties: dict,
                             title: list | None = None,
                             icon: dict | None = None) -> dict:
    """Notion REST: POST /v1/data_sources — create a new data source
    under a database parent ({type:"database_id", database_id})."""
    with _lock():
        s = _load_state()
        parent_id, parent_obj = _resolve_parent(s, parent)
        if not parent_obj or parent_obj.get("object") != "database":
            _record(s, "create_data_source", result="parent_invalid")
            _save_state(s)
            return _err(400, "validation_error",
                        "parent must reference an existing database_id")
        dsid = _new_id()
        now = _now()
        ds = {
            "object": "data_source",
            "id": dsid,
            "created_time": now,
            "last_edited_time": now,
            "title": title or [],
            "icon": icon,
            "parent": {"type": "database_id", "database_id": parent_id},
            "properties": properties or {},
            "archived": False,
            "_children": [],
        }
        s["objects"][dsid] = ds
        parent_obj.setdefault("data_sources", []).append(
            {"id": dsid, "name": _plain(title)})
        _record(s, "create_data_source", data_source_id=dsid,
                database_id=parent_id)
        _save_state(s)
        return _strip(ds)


@mcp.tool(name="API-update-a-data-source")
def api_update_a_data_source(data_source_id: str,
                             properties: dict | None = None,
                             title: list | None = None,
                             archived: bool | None = None) -> dict:
    """Notion REST: PATCH /v1/data_sources/{data_source_id} — update
    a data source's property schema, title, or archive flag."""
    with _lock():
        s = _load_state()
        obj = _get_obj(s, data_source_id)
        if not obj or obj.get("object") != "data_source":
            _record(s, "update_data_source",
                    data_source_id=data_source_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find data source with ID: {data_source_id}")
        if properties:
            obj.setdefault("properties", {}).update(properties)
        if title is not None:
            obj["title"] = title
        if archived is not None:
            obj["archived"] = bool(archived)
        _touch(obj)
        _record(s, "update_data_source",
                data_source_id=data_source_id,
                property_keys=list((properties or {}).keys()))
        _save_state(s)
        return _strip(obj)


@mcp.tool(name="API-query-data-source")
def api_query_data_source(data_source_id: str,
                          filter: dict | None = None,
                          sorts: list | None = None,
                          start_cursor: str | None = None,
                          page_size: int = 100,
                          filter_properties: list | None = None) -> dict:
    """Notion REST: POST /v1/data_sources/{data_source_id}/query —
    list pages belonging to a data source, optionally filtered/sorted.

    Filter support is a *subset* of the real API: equals, contains,
    is_empty, is_not_empty on top-level property filters (single
    condition; AND/OR not implemented). Unsupported filters fall back
    to returning every row.
    """
    with _lock():
        s = _load_state()
        ds = _get_obj(s, data_source_id)
        if not ds or ds.get("object") != "data_source":
            _record(s, "query_data_source",
                    data_source_id=data_source_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find data source with ID: {data_source_id}")
        rows = [s["objects"][i] for i in ds.get("_children", [])
                if s["objects"].get(i, {}).get("object") == "page"
                and not s["objects"][i].get("archived")]
        rows = _apply_filter(rows, filter)
        rows = _apply_sorts(rows, sorts or [])
        page_size = min(max(int(page_size or 100), 1), 100)
        start = 0
        if start_cursor:
            for i, r in enumerate(rows):
                if r["id"] == start_cursor:
                    start = i
                    break
        page = rows[start: start + page_size]
        next_cursor = (page[-1]["id"]
                       if len(rows) > start + page_size and page else None)
        if filter_properties:
            page = [{**_strip(r),
                     "properties": {k: v for k, v in r["properties"].items()
                                    if k in filter_properties}}
                    for r in page]
        else:
            page = [_strip(r) for r in page]
        _record(s, "query_data_source",
                data_source_id=data_source_id, filter=filter,
                count=len(page))
        _save_state(s)
        return {
            "object": "list",
            "results": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "type": "page_or_data_source",
            "page_or_data_source": {},
        }


@mcp.tool(name="API-list-data-source-templates")
def api_list_data_source_templates(data_source_id: str,
                                   start_cursor: str | None = None,
                                   page_size: int = 100) -> dict:
    """Notion REST: GET /v1/data_sources/{data_source_id}/templates
    — list template pages defined on the data source. The mock
    returns an empty list (templates not modeled)."""
    with _lock():
        s = _load_state()
        ds = _get_obj(s, data_source_id)
        if not ds or ds.get("object") != "data_source":
            _record(s, "list_templates",
                    data_source_id=data_source_id, result="not_found")
            _save_state(s)
            return _err(404, "object_not_found",
                        f"Could not find data source with ID: {data_source_id}")
        _record(s, "list_templates", data_source_id=data_source_id)
        _save_state(s)
        return {"object": "list", "results": [],
                "next_cursor": None, "has_more": False,
                "type": "page", "page": {}}


def _apply_filter(rows: list, flt: dict | None) -> list:
    if not flt or not isinstance(flt, dict):
        return rows
    prop_name = flt.get("property")
    if not prop_name:
        return rows
    cond = {k: v for k, v in flt.items() if k != "property"}
    if not cond:
        return rows
    kind, body = next(iter(cond.items()))
    if not isinstance(body, dict):
        return rows
    out = []
    for r in rows:
        prop = (r.get("properties") or {}).get(prop_name)
        if prop is None:
            continue
        if _filter_match(prop, kind, body):
            out.append(r)
    return out


def _filter_match(prop: dict, kind: str, body: dict) -> bool:
    val = _plain_property(prop)
    if "equals" in body:
        return val == body["equals"]
    if "does_not_equal" in body:
        return val != body["does_not_equal"]
    if "contains" in body:
        return isinstance(val, str) and body["contains"] in val
    if "does_not_contain" in body:
        return not (isinstance(val, str) and body["does_not_contain"] in val)
    if "is_empty" in body and body["is_empty"]:
        return not val
    if "is_not_empty" in body and body["is_not_empty"]:
        return bool(val)
    if "starts_with" in body:
        return isinstance(val, str) and val.startswith(body["starts_with"])
    if "ends_with" in body:
        return isinstance(val, str) and val.endswith(body["ends_with"])
    if "greater_than" in body:
        try:
            return float(val) > float(body["greater_than"])
        except (TypeError, ValueError):
            return False
    if "less_than" in body:
        try:
            return float(val) < float(body["less_than"])
        except (TypeError, ValueError):
            return False
    return True


def _plain_property(prop: dict) -> Any:
    t = prop.get("type")
    if t == "title" or t == "rich_text":
        return "".join(p.get("plain_text", "")
                       for p in prop.get(t, []) if isinstance(p, dict))
    if t in ("number", "checkbox", "url", "email", "phone_number"):
        return prop.get(t)
    if t == "select":
        sel = prop.get("select")
        return sel.get("name") if isinstance(sel, dict) else None
    if t == "multi_select":
        return [s.get("name") for s in prop.get("multi_select", [])
                if isinstance(s, dict)]
    if t == "status":
        st = prop.get("status")
        return st.get("name") if isinstance(st, dict) else None
    if t == "date":
        d = prop.get("date")
        return d.get("start") if isinstance(d, dict) else None
    if t == "people":
        return [p.get("id") for p in prop.get("people", [])
                if isinstance(p, dict)]
    if t == "relation":
        return [r.get("id") for r in prop.get("relation", [])
                if isinstance(r, dict)]
    return prop.get(t)


def _apply_sorts(rows: list, sorts: list) -> list:
    if not sorts:
        return rows
    for sort in reversed(sorts):
        prop = sort.get("property")
        direction = sort.get("direction", "ascending")
        ts = sort.get("timestamp")
        if ts in ("created_time", "last_edited_time"):
            key = lambda r, t=ts: r.get(t) or ""
        elif prop:
            key = lambda r, p=prop: (_plain_property(
                (r.get("properties") or {}).get(p, {"type": "title"})) or "")
        else:
            continue
        rows = sorted(rows, key=key,
                      reverse=(direction == "descending"))
    return rows


def _plain(rich_text: list | None) -> str:
    if not rich_text:
        return ""
    return "".join(p.get("plain_text", "") for p in rich_text
                   if isinstance(p, dict))


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="API-retrieve-a-comment")
def api_retrieve_a_comment(block_id: str,
                           start_cursor: str | None = None,
                           page_size: int = 100) -> dict:
    """Notion REST: GET /v1/comments?block_id=... — list comments on
    a block or page."""
    with _lock():
        s = _load_state()
        comments = [c for c in s["comments"].values()
                    if c.get("parent", {}).get("page_id") == block_id
                    or c.get("parent", {}).get("block_id") == block_id]
        comments.sort(key=lambda c: c["created_time"])
        page_size = min(max(int(page_size or 100), 1), 100)
        start = 0
        if start_cursor:
            for i, c in enumerate(comments):
                if c["id"] == start_cursor:
                    start = i
                    break
        page = comments[start: start + page_size]
        next_cursor = (page[-1]["id"]
                       if len(comments) > start + page_size and page else None)
        _record(s, "retrieve_comment", block_id=block_id,
                count=len(page))
        _save_state(s)
        return {"object": "list", "results": page,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
                "type": "comment", "comment": {}}


@mcp.tool(name="API-create-a-comment")
def api_create_a_comment(parent: dict | None = None,
                         discussion_id: str | None = None,
                         rich_text: list | None = None,
                         display_name: dict | None = None) -> dict:
    """Notion REST: POST /v1/comments — create a comment on a page
    (via parent={type:"page_id",page_id}) or reply to a discussion
    (via discussion_id)."""
    with _lock():
        s = _load_state()
        if not rich_text:
            return _err(400, "validation_error",
                        "rich_text required")
        cid = _new_id()
        now = _now()
        comment = {
            "object": "comment",
            "id": cid,
            "parent": parent or {},
            "discussion_id": discussion_id or _new_id(),
            "created_time": now,
            "last_edited_time": now,
            "created_by": {"object": "user",
                           "id": "00000000-0000-0000-0000-00000000b07"},
            "rich_text": rich_text,
            "display_name": display_name,
        }
        s["comments"][cid] = comment
        _record(s, "create_comment", comment_id=cid, parent=parent,
                discussion_id=comment["discussion_id"])
        _save_state(s)
        return dict(comment)


# ---------------------------------------------------------------------------
# Debug helpers (not part of the Notion REST surface)
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not exposed by the
    real Notion server; use for inspection/verification."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed_object")
def mock_debug_seed_object(obj: dict) -> dict:
    """Mock-only: directly insert a Notion-shaped object (page,
    database, data_source, or block) into the state, bypassing
    validation. Used by per-task preprocessing to seed fixtures."""
    with _lock():
        s = _load_state()
        if not isinstance(obj, dict) or "id" not in obj or "object" not in obj:
            return _err(400, "validation_error",
                        "obj must have `id` and `object`")
        s["objects"][obj["id"]] = obj
        _record(s, "debug_seed", object_id=obj["id"],
                kind=obj["object"])
        _save_state(s)
        return _strip(obj)


if __name__ == "__main__":
    mcp.run()
