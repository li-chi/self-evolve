"""Zotero mock MCP server.

Mirrors the Zotero Web API v3 surface
(https://www.zotero.org/support/dev/web_api/v3/start). Operation
names, parameter names and response shapes match the real REST API
so this mock can stand in for a live Zotero server during rollouts.

Implemented tools (24 + 2 mock helpers):

  Items
    get_items, get_item, get_top_items, get_trash_items, create_items,
    update_item, delete_item, get_item_children
  Collections
    get_collections, get_collection, create_collection, update_collection,
    delete_collection, get_collection_items, get_top_collections
  Tags
    get_tags, get_item_tags
  Searches
    get_searches
  Groups
    get_groups, get_group

Mock-only helpers:
    mock_debug_state, mock_debug_seed

Object shapes follow Zotero's wrapper convention:

    {
      "key":     "ABCD1234",
      "version": 17,
      "library": {"type":"user","id":12345,"name":"Mock User",
                  "links":{"alternate":{"href":"...","type":"text/html"}}},
      "links":   {"self":{"href":"...","type":"application/json"},
                  "alternate":{"href":"...","type":"text/html"}},
      "meta":    {"creatorSummary":"Smith","parsedDate":"2024-01",
                  "numChildren":0},
      "data":    {"key":"ABCD1234","version":17,"itemType":"book",
                  "title":"...","creators":[...],"tags":[...],
                  "collections":[...],"relations":{},...}
    }

Errors are returned as `{"message": "..."}` dicts with an implied
HTTP status that the caller can inspect via the `status` field
(matching how the real API surfaces 400/403/404/409/412 etc.).

State lives at `$ZOTERO_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/zotero_mock`). Optional `ZOTERO_MOCK_SEED_PATH` preloads
state when no state.json exists. Every call (including reads)
appends to `state["calls"]` so verifiers can replay the trace.
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


ZOTERO_API_VERSION = 3


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "ZOTERO_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/zotero_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "api_version": ZOTERO_API_VERSION,
        "library": {
            "type": "user",
            "id": 12345,
            "name": "Mock User",
            "links": {
                "alternate": {
                    "href": "https://www.zotero.org/mockuser",
                    "type": "text/html",
                },
            },
        },
        "version": 0,
        "items": {},          # key -> item dict (Zotero "data" shape)
        "collections": {},    # key -> collection dict (Zotero "data" shape)
        "searches": {},       # key -> saved search dict
        "groups": {},         # group_id -> group dict
        "next_version": 1,
        "calls": [],
        "_rng_seed": 0,
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ZOTERO_MOCK_SEED_PATH")
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

_ALPHABET = string.ascii_uppercase + string.digits


def _new_key(state: dict, taken: set | None = None) -> str:
    """Generate a fresh 8-character Zotero key (alphanumeric, uppercase)."""
    taken = taken or set()
    # Deterministic-ish: use _rng_seed counter so seeded fixtures stay
    # reproducible across runs.
    seed = state.get("_rng_seed", 0)
    rng = random.Random(seed)
    while True:
        state["_rng_seed"] = seed + 1
        seed += 1
        key = "".join(rng.choices(_ALPHABET, k=8))
        if key not in taken:
            return key


def _bump_version(state: dict) -> int:
    v = state.get("next_version", 1)
    state["next_version"] = v + 1
    state["version"] = v
    return v


def _library_block(state: dict) -> dict:
    lib = state.get("library", {})
    base = (f"https://www.zotero.org/"
            f"{'users' if lib.get('type') == 'user' else 'groups'}/"
            f"{lib.get('id')}")
    return {
        "type": lib.get("type", "user"),
        "id": lib.get("id", 0),
        "name": lib.get("name", ""),
        "links": {
            "alternate": {
                "href": base,
                "type": "text/html",
            },
        },
    }


def _item_links(state: dict, key: str) -> dict:
    lib = state.get("library", {})
    prefix = (f"https://api.zotero.org/"
              f"{'users' if lib.get('type') == 'user' else 'groups'}/"
              f"{lib.get('id')}")
    return {
        "self": {
            "href": f"{prefix}/items/{key}",
            "type": "application/json",
        },
        "alternate": {
            "href": f"https://www.zotero.org/{lib.get('type','user')}s/"
                    f"{lib.get('id')}/items/{key}",
            "type": "text/html",
        },
    }


def _collection_links(state: dict, key: str) -> dict:
    lib = state.get("library", {})
    prefix = (f"https://api.zotero.org/"
              f"{'users' if lib.get('type') == 'user' else 'groups'}/"
              f"{lib.get('id')}")
    return {
        "self": {
            "href": f"{prefix}/collections/{key}",
            "type": "application/json",
        },
        "alternate": {
            "href": f"https://www.zotero.org/{lib.get('type','user')}s/"
                    f"{lib.get('id')}/collections/{key}",
            "type": "text/html",
        },
    }


def _creator_summary(creators: list) -> str:
    if not creators:
        return ""
    names = [c.get("lastName") or c.get("name") or ""
             for c in creators if isinstance(c, dict)]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def _parsed_date(date_str: str | None) -> str:
    """Best-effort 'parsedDate' YYYY[-MM[-DD]] from a free-text date."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    # Match "2024", "2024-01", "2024-01-15", "January 2024", "2024 Jan 15"
    import re
    m = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", s)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if d:
            return f"{y}-{mo}-{d}"
        if mo:
            return f"{y}-{mo}"
        return y
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        return m.group(0)
    return ""


def _num_children(state: dict, parent_key: str) -> int:
    return sum(1 for it in state["items"].values()
               if it.get("parentItem") == parent_key
               and not it.get("deleted"))


def _wrap_item(state: dict, data: dict) -> dict:
    """Wrap a raw `data` item into the Zotero JSON response envelope."""
    key = data.get("key")
    version = data.get("version", state.get("version", 0))
    creators = data.get("creators", []) or []
    meta = {
        "creatorSummary": _creator_summary(creators),
        "parsedDate": _parsed_date(data.get("date")),
        "numChildren": _num_children(state, key) if key else 0,
    }
    return {
        "key": key,
        "version": version,
        "library": _library_block(state),
        "links": _item_links(state, key),
        "meta": meta,
        "data": dict(data),
    }


def _wrap_collection(state: dict, data: dict) -> dict:
    key = data.get("key")
    version = data.get("version", state.get("version", 0))
    num_items = sum(1 for it in state["items"].values()
                    if key in (it.get("collections") or [])
                    and not it.get("deleted"))
    num_collections = sum(1 for c in state["collections"].values()
                          if c.get("parentCollection") == key)
    return {
        "key": key,
        "version": version,
        "library": _library_block(state),
        "links": _collection_links(state, key),
        "meta": {
            "numCollections": num_collections,
            "numItems": num_items,
        },
        "data": dict(data),
    }


def _err(message: str, status: int = 400) -> dict:
    """Return a Zotero-shaped error object. Matches how the real REST
    API surfaces errors via response body + status code."""
    return {"message": message, "status": status}


def _paginate(items: list, start: int, limit: int) -> list:
    if limit <= 0:
        limit = 25
    if limit > 100:
        limit = 100
    start = max(0, int(start or 0))
    return items[start: start + limit]


def _sort_items(items: list, sort: str, direction: str) -> list:
    """Zotero sort keys: dateAdded, dateModified, title, creator, date,
    itemType. Direction: asc | desc (default desc for date* keys)."""
    if not sort:
        sort = "dateModified"
    reverse = (direction or "").lower() == "desc"
    if not direction:
        reverse = sort in ("dateAdded", "dateModified", "date")

    def keyfn(d):
        data = d.get("data", d)
        if sort == "title":
            return (data.get("title") or "").lower()
        if sort == "creator":
            return _creator_summary(data.get("creators", []) or []).lower()
        if sort == "date":
            return _parsed_date(data.get("date"))
        if sort == "itemType":
            return data.get("itemType") or ""
        if sort == "dateAdded":
            return data.get("dateAdded") or ""
        if sort == "dateModified":
            return data.get("dateModified") or ""
        return data.get("key") or ""

    return sorted(items, key=keyfn, reverse=reverse)


def _matches_qmode(text: str, q: str, mode: str) -> bool:
    """`qmode` is `titleCreatorYear` (default) or `everything`."""
    if not q:
        return True
    return q.lower() in (text or "").lower()


def _item_search_text(data: dict, mode: str) -> str:
    parts = [data.get("title", "")]
    for c in data.get("creators", []) or []:
        if isinstance(c, dict):
            parts.append(c.get("lastName", ""))
            parts.append(c.get("firstName", ""))
            parts.append(c.get("name", ""))
    parts.append(_parsed_date(data.get("date")))
    if mode == "everything":
        parts.append(data.get("abstractNote", "") or "")
        parts.append(data.get("publicationTitle", "") or "")
        for t in data.get("tags", []) or []:
            if isinstance(t, dict):
                parts.append(t.get("tag", ""))
    return " ".join(p for p in parts if p)


def _make_item(state: dict, payload: dict) -> dict:
    """Build a fresh `data` item with key, version, timestamps."""
    taken = set(state["items"].keys())
    key = payload.get("key") or _new_key(state, taken)
    version = _bump_version(state)
    now = _now_iso()
    data = {
        "key": key,
        "version": version,
        "itemType": payload.get("itemType", "document"),
        "title": payload.get("title", ""),
        "creators": payload.get("creators", []) or [],
        "abstractNote": payload.get("abstractNote", ""),
        "date": payload.get("date", ""),
        "url": payload.get("url", ""),
        "DOI": payload.get("DOI", ""),
        "ISBN": payload.get("ISBN", ""),
        "publicationTitle": payload.get("publicationTitle", ""),
        "publisher": payload.get("publisher", ""),
        "tags": payload.get("tags", []) or [],
        "collections": payload.get("collections", []) or [],
        "relations": payload.get("relations", {}) or {},
        "dateAdded": payload.get("dateAdded", now),
        "dateModified": payload.get("dateModified", now),
        "deleted": False,
    }
    # Pass through any extra fields the caller provided.
    for k, v in payload.items():
        if k not in data:
            data[k] = v
    if payload.get("parentItem"):
        data["parentItem"] = payload["parentItem"]
    return data


def _make_collection(state: dict, payload: dict) -> dict:
    taken = set(state["collections"].keys())
    key = payload.get("key") or _new_key(state, taken)
    version = _bump_version(state)
    return {
        "key": key,
        "version": version,
        "name": payload.get("name", ""),
        "parentCollection": payload.get("parentCollection", False),
        "relations": payload.get("relations", {}) or {},
    }


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("zotero-mock")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@mcp.tool(name="get_items")
def get_items(itemType: str = "",
              tag: str = "",
              q: str = "",
              qmode: str = "titleCreatorYear",
              sort: str = "dateModified",
              direction: str = "",
              start: int = 0,
              limit: int = 25,
              include_trashed: bool = False) -> dict:
    """Zotero Web API v3: GET /users/{id}/items — list ALL items in the
    library (including children, excluding trash unless requested).

    `q` filters by title/creator/date (qmode=titleCreatorYear) or
    everything (qmode=everything). `tag` restricts to items carrying
    the tag. Returns `{total, start, limit, items: [wrapped item, ...]}`
    where each item follows Zotero's `{key, version, library, links,
    meta, data}` envelope.
    """
    with _lock():
        s = _load_state()
        wrapped = []
        for it in s["items"].values():
            if it.get("deleted") and not include_trashed:
                continue
            if itemType and it.get("itemType") != itemType:
                continue
            if tag:
                tag_names = [t.get("tag") if isinstance(t, dict) else t
                             for t in (it.get("tags") or [])]
                if tag not in tag_names:
                    continue
            if q and not _matches_qmode(_item_search_text(it, qmode),
                                        q, qmode):
                continue
            wrapped.append(_wrap_item(s, it))
        wrapped = _sort_items(wrapped, sort, direction)
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_items", q=q, tag=tag, itemType=itemType,
                count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "items": page,
        }


@mcp.tool(name="get_item")
def get_item(itemKey: str,
             include: str = "data") -> dict:
    """Zotero Web API v3: GET /users/{id}/items/{itemKey} — retrieve
    a single item. `include` is a comma-separated list with `data`
    (default), `bib`, and/or `citation`."""
    with _lock():
        s = _load_state()
        it = s["items"].get(itemKey)
        if not it:
            _record(s, "get_item", itemKey=itemKey, result="not_found")
            _save_state(s)
            return _err(f"Item not found: {itemKey}", status=404)
        wrapped = _wrap_item(s, it)
        wanted = {w.strip() for w in (include or "data").split(",")}
        if "bib" in wanted:
            title = it.get("title") or ""
            authors = _creator_summary(it.get("creators") or [])
            year = _parsed_date(it.get("date"))[:4]
            wrapped["bib"] = (f"<div class=\"csl-entry\">{authors} "
                              f"({year}). {title}.</div>")
        if "citation" in wanted:
            wrapped["citation"] = (
                f"<span>({_creator_summary(it.get('creators') or [])} "
                f"{_parsed_date(it.get('date'))[:4]})</span>")
        _record(s, "get_item", itemKey=itemKey)
        _save_state(s)
        return wrapped


@mcp.tool(name="get_top_items")
def get_top_items(itemType: str = "",
                  tag: str = "",
                  q: str = "",
                  qmode: str = "titleCreatorYear",
                  sort: str = "dateModified",
                  direction: str = "",
                  start: int = 0,
                  limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/items/top — list top-level
    items only (excludes child attachments/notes). Same filter/sort
    parameters as `get_items`."""
    with _lock():
        s = _load_state()
        wrapped = []
        for it in s["items"].values():
            if it.get("deleted"):
                continue
            if it.get("parentItem"):
                continue
            if itemType and it.get("itemType") != itemType:
                continue
            if tag:
                tag_names = [t.get("tag") if isinstance(t, dict) else t
                             for t in (it.get("tags") or [])]
                if tag not in tag_names:
                    continue
            if q and not _matches_qmode(_item_search_text(it, qmode),
                                        q, qmode):
                continue
            wrapped.append(_wrap_item(s, it))
        wrapped = _sort_items(wrapped, sort, direction)
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_top_items", q=q, count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "items": page,
        }


@mcp.tool(name="get_trash_items")
def get_trash_items(start: int = 0, limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/items/trash — list items in
    the trash."""
    with _lock():
        s = _load_state()
        wrapped = [_wrap_item(s, it) for it in s["items"].values()
                   if it.get("deleted")]
        wrapped = _sort_items(wrapped, "dateModified", "desc")
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_trash_items", count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "items": page,
        }


@mcp.tool(name="get_item_children")
def get_item_children(itemKey: str,
                      start: int = 0,
                      limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/items/{itemKey}/children —
    list children (attachments / notes) of a parent item."""
    with _lock():
        s = _load_state()
        parent = s["items"].get(itemKey)
        if not parent:
            _record(s, "get_item_children", itemKey=itemKey,
                    result="not_found")
            _save_state(s)
            return _err(f"Item not found: {itemKey}", status=404)
        kids = [_wrap_item(s, it) for it in s["items"].values()
                if it.get("parentItem") == itemKey
                and not it.get("deleted")]
        kids = _sort_items(kids, "dateAdded", "asc")
        total = len(kids)
        page = _paginate(kids, start, limit)
        _record(s, "get_item_children", itemKey=itemKey, count=len(page))
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "items": page,
        }


@mcp.tool(name="create_items")
def create_items(items: list) -> dict:
    """Zotero Web API v3: POST /users/{id}/items — create one or more
    items. Each entry is a `data` dict (no envelope) containing
    `itemType`, `title`, `creators`, etc.

    Returns `{successful: {<idx>: <wrapped item>}, success: {<idx>:
    <key>}, failed: {<idx>: {message,code}}, unchanged: {}}` — the
    exact shape the real API uses for batch writes."""
    with _lock():
        s = _load_state()
        successful: dict[str, Any] = {}
        success: dict[str, Any] = {}
        failed: dict[str, Any] = {}
        for idx, payload in enumerate(items or []):
            idx_str = str(idx)
            if not isinstance(payload, dict):
                failed[idx_str] = {"code": 400,
                                   "message": "item must be an object"}
                continue
            if not payload.get("itemType"):
                failed[idx_str] = {"code": 400,
                                   "message": "itemType is required"}
                continue
            parent = payload.get("parentItem")
            if parent and parent not in s["items"]:
                failed[idx_str] = {"code": 404,
                                   "message": f"parent not found: {parent}"}
                continue
            # Validate referenced collections exist.
            bad_col = [c for c in (payload.get("collections") or [])
                       if c not in s["collections"]]
            if bad_col:
                failed[idx_str] = {"code": 409,
                                   "message": f"unknown collections: "
                                              f"{','.join(bad_col)}"}
                continue
            data = _make_item(s, payload)
            s["items"][data["key"]] = data
            success[idx_str] = data["key"]
            successful[idx_str] = _wrap_item(s, data)
        _record(s, "create_items",
                count=len(success), failed=len(failed))
        _save_state(s)
        return {
            "successful": successful,
            "success": success,
            "unchanged": {},
            "failed": failed,
        }


@mcp.tool(name="update_item")
def update_item(itemKey: str,
                data: dict,
                if_unmodified_since_version: int | None = None) -> dict:
    """Zotero Web API v3: PATCH /users/{id}/items/{itemKey} — partial
    update of an item's `data` fields. Optional
    `if_unmodified_since_version` enforces optimistic concurrency
    (412 Precondition Failed on mismatch)."""
    with _lock():
        s = _load_state()
        it = s["items"].get(itemKey)
        if not it:
            _record(s, "update_item", itemKey=itemKey, result="not_found")
            _save_state(s)
            return _err(f"Item not found: {itemKey}", status=404)
        if (if_unmodified_since_version is not None
                and int(if_unmodified_since_version) != int(it.get("version", 0))):
            _record(s, "update_item", itemKey=itemKey,
                    result="version_conflict",
                    expected=if_unmodified_since_version,
                    actual=it.get("version"))
            _save_state(s)
            return _err("Item has been modified since specified version",
                        status=412)
        if not isinstance(data, dict):
            return _err("data must be an object", status=400)
        # Cannot change key via PATCH.
        data = {k: v for k, v in data.items() if k != "key"}
        # Validate collection refs if changing them.
        if "collections" in data:
            bad = [c for c in (data["collections"] or [])
                   if c not in s["collections"]]
            if bad:
                _record(s, "update_item", itemKey=itemKey,
                        result="bad_collections")
                _save_state(s)
                return _err(f"unknown collections: {','.join(bad)}",
                            status=409)
        it.update(data)
        it["version"] = _bump_version(s)
        it["dateModified"] = _now_iso()
        _record(s, "update_item", itemKey=itemKey,
                fields=list(data.keys()))
        _save_state(s)
        return _wrap_item(s, it)


@mcp.tool(name="delete_item")
def delete_item(itemKey: str,
                permanent: bool = False,
                if_unmodified_since_version: int | None = None) -> dict:
    """Zotero Web API v3: DELETE /users/{id}/items/{itemKey} — move
    the item to trash (`permanent=False`, default) or remove it
    entirely (`permanent=True`). Returns `{key, deleted, permanent}`."""
    with _lock():
        s = _load_state()
        it = s["items"].get(itemKey)
        if not it:
            _record(s, "delete_item", itemKey=itemKey, result="not_found")
            _save_state(s)
            return _err(f"Item not found: {itemKey}", status=404)
        if (if_unmodified_since_version is not None
                and int(if_unmodified_since_version) != int(it.get("version", 0))):
            _record(s, "delete_item", itemKey=itemKey,
                    result="version_conflict")
            _save_state(s)
            return _err("Item has been modified since specified version",
                        status=412)
        if permanent:
            s["items"].pop(itemKey, None)
            # Cascade-remove children.
            for k, child in list(s["items"].items()):
                if child.get("parentItem") == itemKey:
                    s["items"].pop(k, None)
        else:
            it["deleted"] = True
            it["version"] = _bump_version(s)
            it["dateModified"] = _now_iso()
        _record(s, "delete_item", itemKey=itemKey,
                permanent=bool(permanent))
        _save_state(s)
        return {"key": itemKey, "deleted": True,
                "permanent": bool(permanent)}


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@mcp.tool(name="get_collections")
def get_collections(q: str = "",
                    sort: str = "title",
                    direction: str = "asc",
                    start: int = 0,
                    limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/collections — list ALL
    collections in the library."""
    with _lock():
        s = _load_state()
        wrapped = []
        for c in s["collections"].values():
            if q and q.lower() not in (c.get("name") or "").lower():
                continue
            wrapped.append(_wrap_collection(s, c))
        reverse = (direction or "").lower() == "desc"

        def kf(d):
            data = d.get("data", d)
            return (data.get("name") if sort == "title" else
                    data.get("key", "")).lower()
        wrapped.sort(key=kf, reverse=reverse)
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_collections", q=q, count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "collections": page,
        }


@mcp.tool(name="get_top_collections")
def get_top_collections(start: int = 0, limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/collections/top — list
    top-level collections only (no nested children)."""
    with _lock():
        s = _load_state()
        wrapped = [_wrap_collection(s, c) for c in s["collections"].values()
                   if not c.get("parentCollection")]
        wrapped.sort(key=lambda d: (d["data"].get("name") or "").lower())
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_top_collections", count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "collections": page,
        }


@mcp.tool(name="get_collection")
def get_collection(collectionKey: str) -> dict:
    """Zotero Web API v3: GET /users/{id}/collections/{collectionKey}
    — retrieve one collection (with `numItems` + `numCollections`)."""
    with _lock():
        s = _load_state()
        c = s["collections"].get(collectionKey)
        if not c:
            _record(s, "get_collection", collectionKey=collectionKey,
                    result="not_found")
            _save_state(s)
            return _err(f"Collection not found: {collectionKey}",
                        status=404)
        _record(s, "get_collection", collectionKey=collectionKey)
        _save_state(s)
        return _wrap_collection(s, c)


@mcp.tool(name="get_collection_items")
def get_collection_items(collectionKey: str,
                         itemType: str = "",
                         tag: str = "",
                         q: str = "",
                         qmode: str = "titleCreatorYear",
                         sort: str = "dateModified",
                         direction: str = "",
                         start: int = 0,
                         limit: int = 25,
                         top: bool = False) -> dict:
    """Zotero Web API v3: GET /users/{id}/collections/{collectionKey}/items
    — list items in a collection. `top=true` restricts to top-level
    items only (matches /collections/{key}/items/top)."""
    with _lock():
        s = _load_state()
        c = s["collections"].get(collectionKey)
        if not c:
            _record(s, "get_collection_items",
                    collectionKey=collectionKey, result="not_found")
            _save_state(s)
            return _err(f"Collection not found: {collectionKey}",
                        status=404)
        wrapped = []
        for it in s["items"].values():
            if it.get("deleted"):
                continue
            if collectionKey not in (it.get("collections") or []):
                continue
            if top and it.get("parentItem"):
                continue
            if itemType and it.get("itemType") != itemType:
                continue
            if tag:
                tag_names = [t.get("tag") if isinstance(t, dict) else t
                             for t in (it.get("tags") or [])]
                if tag not in tag_names:
                    continue
            if q and not _matches_qmode(_item_search_text(it, qmode),
                                        q, qmode):
                continue
            wrapped.append(_wrap_item(s, it))
        wrapped = _sort_items(wrapped, sort, direction)
        total = len(wrapped)
        page = _paginate(wrapped, start, limit)
        _record(s, "get_collection_items",
                collectionKey=collectionKey, count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "items": page,
        }


@mcp.tool(name="create_collection")
def create_collection(name: str,
                      parentCollection: str | bool = False,
                      relations: dict | None = None) -> dict:
    """Zotero Web API v3: POST /users/{id}/collections — create a new
    collection. `parentCollection` is either another collection key
    or `false` for a top-level collection."""
    with _lock():
        s = _load_state()
        if not name or not name.strip():
            return _err("name is required", status=400)
        if (parentCollection and parentCollection is not False
                and parentCollection not in s["collections"]):
            _record(s, "create_collection",
                    result="bad_parent", parent=parentCollection)
            _save_state(s)
            return _err(f"parent collection not found: {parentCollection}",
                        status=409)
        data = _make_collection(s, {"name": name,
                                    "parentCollection": parentCollection
                                    if parentCollection else False,
                                    "relations": relations or {}})
        s["collections"][data["key"]] = data
        _record(s, "create_collection", collectionKey=data["key"],
                name=name)
        _save_state(s)
        return _wrap_collection(s, data)


@mcp.tool(name="update_collection")
def update_collection(collectionKey: str,
                      data: dict,
                      if_unmodified_since_version: int | None = None) -> dict:
    """Zotero Web API v3: PATCH /users/{id}/collections/{collectionKey}
    — partial update of a collection (`name`, `parentCollection`)."""
    with _lock():
        s = _load_state()
        c = s["collections"].get(collectionKey)
        if not c:
            _record(s, "update_collection",
                    collectionKey=collectionKey, result="not_found")
            _save_state(s)
            return _err(f"Collection not found: {collectionKey}",
                        status=404)
        if (if_unmodified_since_version is not None
                and int(if_unmodified_since_version) != int(c.get("version", 0))):
            _record(s, "update_collection",
                    collectionKey=collectionKey,
                    result="version_conflict")
            _save_state(s)
            return _err("Collection has been modified since specified "
                        "version", status=412)
        if not isinstance(data, dict):
            return _err("data must be an object", status=400)
        data = {k: v for k, v in data.items() if k != "key"}
        if ("parentCollection" in data and data["parentCollection"]
                and data["parentCollection"] not in s["collections"]):
            return _err(f"parent collection not found: "
                        f"{data['parentCollection']}", status=409)
        c.update(data)
        c["version"] = _bump_version(s)
        _record(s, "update_collection",
                collectionKey=collectionKey, fields=list(data.keys()))
        _save_state(s)
        return _wrap_collection(s, c)


@mcp.tool(name="delete_collection")
def delete_collection(collectionKey: str,
                      if_unmodified_since_version: int | None = None) -> dict:
    """Zotero Web API v3: DELETE /users/{id}/collections/{collectionKey}
    — remove a collection. Items in it are not deleted; the collection
    reference is simply stripped from each item's `collections` list."""
    with _lock():
        s = _load_state()
        c = s["collections"].get(collectionKey)
        if not c:
            _record(s, "delete_collection",
                    collectionKey=collectionKey, result="not_found")
            _save_state(s)
            return _err(f"Collection not found: {collectionKey}",
                        status=404)
        if (if_unmodified_since_version is not None
                and int(if_unmodified_since_version) != int(c.get("version", 0))):
            _record(s, "delete_collection",
                    collectionKey=collectionKey,
                    result="version_conflict")
            _save_state(s)
            return _err("Collection has been modified since specified "
                        "version", status=412)
        s["collections"].pop(collectionKey, None)
        # Reparent children to top-level.
        for cc in s["collections"].values():
            if cc.get("parentCollection") == collectionKey:
                cc["parentCollection"] = False
                cc["version"] = _bump_version(s)
        # Strip the collection from items.
        for it in s["items"].values():
            cols = it.get("collections") or []
            if collectionKey in cols:
                it["collections"] = [c for c in cols if c != collectionKey]
                it["version"] = _bump_version(s)
                it["dateModified"] = _now_iso()
        _record(s, "delete_collection", collectionKey=collectionKey)
        _save_state(s)
        return {"key": collectionKey, "deleted": True}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@mcp.tool(name="get_tags")
def get_tags(q: str = "",
             qmode: str = "contains",
             start: int = 0,
             limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/tags — list distinct tags
    across the library, with `numItems` count for each. `qmode` is
    `contains` (default) or `startsWith`."""
    with _lock():
        s = _load_state()
        counts: dict[str, int] = {}
        types: dict[str, int] = {}
        for it in s["items"].values():
            if it.get("deleted"):
                continue
            for t in it.get("tags") or []:
                name = t.get("tag") if isinstance(t, dict) else t
                ttype = (t.get("type", 0) if isinstance(t, dict) else 0)
                if not name:
                    continue
                if q:
                    if qmode == "startsWith":
                        if not name.lower().startswith(q.lower()):
                            continue
                    elif q.lower() not in name.lower():
                        continue
                counts[name] = counts.get(name, 0) + 1
                types[name] = ttype
        rows = [{"tag": name,
                 "links": {
                     "self": {
                         "href": (f"https://api.zotero.org/"
                                  f"users/{s['library'].get('id')}/"
                                  f"tags/{name}"),
                         "type": "application/atom+xml",
                     },
                 },
                 "meta": {"type": types.get(name, 0),
                          "numItems": counts[name]}}
                for name in sorted(counts.keys(), key=str.lower)]
        total = len(rows)
        page = _paginate(rows, start, limit)
        _record(s, "get_tags", q=q, count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "tags": page,
        }


@mcp.tool(name="get_item_tags")
def get_item_tags(itemKey: str) -> dict:
    """Zotero Web API v3: GET /users/{id}/items/{itemKey}/tags — list
    tags belonging to a single item."""
    with _lock():
        s = _load_state()
        it = s["items"].get(itemKey)
        if not it:
            _record(s, "get_item_tags", itemKey=itemKey,
                    result="not_found")
            _save_state(s)
            return _err(f"Item not found: {itemKey}", status=404)
        rows = []
        for t in it.get("tags") or []:
            name = t.get("tag") if isinstance(t, dict) else t
            ttype = (t.get("type", 0) if isinstance(t, dict) else 0)
            if not name:
                continue
            rows.append({
                "tag": name,
                "links": {
                    "self": {
                        "href": (f"https://api.zotero.org/"
                                 f"users/{s['library'].get('id')}/"
                                 f"tags/{name}"),
                        "type": "application/atom+xml",
                    },
                },
                "meta": {"type": ttype, "numItems": 1},
            })
        _record(s, "get_item_tags", itemKey=itemKey, count=len(rows))
        _save_state(s)
        return {
            "total": len(rows),
            "start": 0,
            "limit": len(rows),
            "tags": rows,
        }


# ---------------------------------------------------------------------------
# Saved Searches
# ---------------------------------------------------------------------------

@mcp.tool(name="get_searches")
def get_searches(start: int = 0, limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/searches — list saved
    searches in the library."""
    with _lock():
        s = _load_state()
        lib = s["library"]
        prefix = (f"https://api.zotero.org/"
                  f"{'users' if lib.get('type') == 'user' else 'groups'}/"
                  f"{lib.get('id')}")
        rows = []
        for sk, search in s["searches"].items():
            rows.append({
                "key": sk,
                "version": search.get("version", 0),
                "library": _library_block(s),
                "links": {
                    "self": {
                        "href": f"{prefix}/searches/{sk}",
                        "type": "application/json",
                    },
                },
                "meta": {},
                "data": dict(search),
            })
        rows.sort(key=lambda r: (r["data"].get("name") or "").lower())
        total = len(rows)
        page = _paginate(rows, start, limit)
        _record(s, "get_searches", count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "searches": page,
        }


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@mcp.tool(name="get_groups")
def get_groups(start: int = 0, limit: int = 25) -> dict:
    """Zotero Web API v3: GET /users/{id}/groups — list groups the
    user belongs to."""
    with _lock():
        s = _load_state()
        rows = []
        for gid, g in s["groups"].items():
            rows.append({
                "id": int(gid) if str(gid).isdigit() else gid,
                "version": g.get("version", 0),
                "links": {
                    "self": {
                        "href": f"https://api.zotero.org/groups/{gid}",
                        "type": "application/json",
                    },
                    "alternate": {
                        "href": f"https://www.zotero.org/groups/{gid}",
                        "type": "text/html",
                    },
                },
                "meta": {
                    "created": g.get("created", _now_iso()),
                    "lastModified": g.get("lastModified", _now_iso()),
                    "numItems": g.get("numItems", 0),
                },
                "data": {
                    "id": int(gid) if str(gid).isdigit() else gid,
                    "version": g.get("version", 0),
                    "name": g.get("name", ""),
                    "owner": g.get("owner", 0),
                    "type": g.get("type", "Private"),
                    "description": g.get("description", ""),
                    "url": g.get("url", ""),
                    "hasImage": bool(g.get("hasImage", False)),
                    "libraryEditing": g.get("libraryEditing", "members"),
                    "libraryReading": g.get("libraryReading", "members"),
                    "fileEditing": g.get("fileEditing", "members"),
                    "members": list(g.get("members") or []),
                    "admins": list(g.get("admins") or []),
                },
            })
        rows.sort(key=lambda r: (r["data"].get("name") or "").lower())
        total = len(rows)
        page = _paginate(rows, start, limit)
        _record(s, "get_groups", count=len(page), total=total)
        _save_state(s)
        return {
            "total": total,
            "start": int(start or 0),
            "limit": int(limit or 25),
            "groups": page,
        }


@mcp.tool(name="get_group")
def get_group(groupID: str) -> dict:
    """Zotero Web API v3: GET /groups/{groupID} — retrieve a single
    group."""
    with _lock():
        s = _load_state()
        g = s["groups"].get(str(groupID))
        if not g:
            _record(s, "get_group", groupID=groupID, result="not_found")
            _save_state(s)
            return _err(f"Group not found: {groupID}", status=404)
        _record(s, "get_group", groupID=groupID)
        _save_state(s)
        gid = str(groupID)
        return {
            "id": int(gid) if gid.isdigit() else gid,
            "version": g.get("version", 0),
            "links": {
                "self": {
                    "href": f"https://api.zotero.org/groups/{gid}",
                    "type": "application/json",
                },
                "alternate": {
                    "href": f"https://www.zotero.org/groups/{gid}",
                    "type": "text/html",
                },
            },
            "meta": {
                "created": g.get("created", _now_iso()),
                "lastModified": g.get("lastModified", _now_iso()),
                "numItems": g.get("numItems", 0),
            },
            "data": {
                "id": int(gid) if gid.isdigit() else gid,
                "version": g.get("version", 0),
                "name": g.get("name", ""),
                "owner": g.get("owner", 0),
                "type": g.get("type", "Private"),
                "description": g.get("description", ""),
                "url": g.get("url", ""),
                "hasImage": bool(g.get("hasImage", False)),
                "libraryEditing": g.get("libraryEditing", "members"),
                "libraryReading": g.get("libraryReading", "members"),
                "fileEditing": g.get("fileEditing", "members"),
                "members": list(g.get("members") or []),
                "admins": list(g.get("admins") or []),
            },
        }


# ---------------------------------------------------------------------------
# Mock-only helpers (not part of the real surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state dict (for verifier
    introspection). Not exposed by the real Zotero API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(library: dict | None = None,
                    items: list | None = None,
                    collections: list | None = None,
                    searches: list | None = None,
                    groups: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: bulk-seed library state. Each `items` entry is a
    `data`-style dict; missing keys are filled in (`key`, `version`,
    `dateAdded`, `dateModified`). Pass `replace=True` to fully reset
    state before seeding.

    - `library`: {"type":"user"|"group","id":int,"name":str}
    - `items`: [{key?, itemType, title, creators, date, tags,
                 collections, parentItem?, deleted?, ...}]
    - `collections`: [{key?, name, parentCollection?}]
    - `searches`: [{key?, name, conditions:[...]}]
    - `groups`: [{id, name, owner, type, members, admins, ...}]
    """
    with _lock():
        s = _empty_state() if replace else _load_state()
        if library:
            s["library"].update(library)
        for c in collections or []:
            data = _make_collection(s, c)
            s["collections"][data["key"]] = data
        for it in items or []:
            data = _make_item(s, it)
            s["items"][data["key"]] = data
        for sr in searches or []:
            taken = set(s["searches"].keys())
            sk = sr.get("key") or _new_key(s, taken)
            s["searches"][sk] = {
                "key": sk,
                "version": _bump_version(s),
                "name": sr.get("name", ""),
                "conditions": sr.get("conditions", []) or [],
            }
        for g in groups or []:
            gid = str(g.get("id") or _new_key(s, set()))
            s["groups"][gid] = {
                "version": g.get("version", _bump_version(s)),
                "name": g.get("name", ""),
                "owner": g.get("owner", 0),
                "type": g.get("type", "Private"),
                "description": g.get("description", ""),
                "url": g.get("url", ""),
                "hasImage": bool(g.get("hasImage", False)),
                "libraryEditing": g.get("libraryEditing", "members"),
                "libraryReading": g.get("libraryReading", "members"),
                "fileEditing": g.get("fileEditing", "members"),
                "members": list(g.get("members") or []),
                "admins": list(g.get("admins") or []),
                "created": g.get("created", _now_iso()),
                "lastModified": g.get("lastModified", _now_iso()),
                "numItems": g.get("numItems", 0),
            }
        _record(s, "debug_seed",
                counts={"items": len(items or []),
                        "collections": len(collections or []),
                        "searches": len(searches or []),
                        "groups": len(groups or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "library": s["library"],
            "item_keys": list(s["items"].keys()),
            "collection_keys": list(s["collections"].keys()),
            "search_keys": list(s["searches"].keys()),
            "group_ids": list(s["groups"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
