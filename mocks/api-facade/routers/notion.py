"""Notion API v1 served from the notion-mock state.

`utils.app_specific.notion.ops` (preprocess and graders) calls
api.notion.com with `requests`, and `notion_client` — used by a few task
graders — reaches the same host over httpx. netredirect points both here,
and the mock module supplies the object shapes so a page the agent creates
through its MCP tool is the same record the grader reads back.
"""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

nt = _load_mock("notion-mock")


def _paginate(items: list, query: dict):
    size = int(query.get("page_size") or 100)
    return {"object": "list", "results": items[:size],
            "next_cursor": None, "has_more": len(items) > size}


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = nt._load_state()
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "v1":
        parts = parts[1:]

    if parts == ["users", "me"] and method == "GET":
        return 200, state["self"]

    if parts == ["users"] and method == "GET":
        return 200, _paginate(list(state.get("users", {}).values()), query)

    if len(parts) == 2 and parts[0] == "users" and method == "GET":
        user = state.get("users", {}).get(parts[1])
        return (200, user) if user else (404, nt._err(404, "object_not_found",
                                                      "user not found"))

    # search
    if parts == ["search"] and method == "POST":
        q = (body.get("query") or "").lower()
        filt = (body.get("filter") or {}).get("value")
        out = []
        for obj in state.get("objects", {}).values():
            if filt and obj.get("object") != filt:
                continue
            if q and q not in nt._extract_title(obj).lower():
                continue
            out.append(nt._strip(obj))
        return 200, _paginate(out, {**query, **body})

    # pages
    if parts and parts[0] == "pages":
        if len(parts) == 1 and method == "POST":
            page = nt._make_page(body.get("parent") or {},
                                 body.get("properties"),
                                 body.get("icon"), body.get("cover"))
            state["objects"][page["id"]] = page
            for child in body.get("children") or []:
                nt._coerce_and_store_block(state, page["id"], child)
            nt._save_state(state)
            return 200, nt._strip(page)
        if len(parts) >= 2:
            page = nt._get_obj(state, parts[1])
            if not page:
                return 404, nt._err(404, "object_not_found", "page not found")
            if len(parts) == 2 and method == "GET":
                return 200, nt._strip(page)
            if len(parts) == 2 and method == "PATCH":
                if "properties" in body:
                    page.setdefault("properties", {}).update(body["properties"])
                for field in ("archived", "in_trash", "icon", "cover"):
                    if field in body:
                        page[field] = body[field]
                nt._touch(page)
                nt._save_state(state)
                return 200, nt._strip(page)
            if len(parts) == 4 and parts[2] == "properties" and method == "GET":
                prop = page.get("properties", {}).get(parts[3])
                if prop is None:
                    return 404, nt._err(404, "object_not_found",
                                        "property not found")
                return 200, prop

    # blocks
    if parts and parts[0] == "blocks" and len(parts) >= 2:
        block_id = parts[1]
        if len(parts) == 3 and parts[2] == "children":
            if method == "GET":
                kids = [nt._strip(b) for b in state.get("objects", {}).values()
                        if b.get("object") == "block"
                        and b.get("parent", {}).get("block_id") == block_id
                        or (b.get("object") == "block"
                            and b.get("parent", {}).get("page_id") == block_id)]
                kids.sort(key=lambda b: b.get("_order", 0))
                return 200, _paginate(kids, query)
            if method == "PATCH":
                created = []
                for child in body.get("children") or []:
                    created.append(nt._coerce_and_store_block(
                        state, block_id, child))
                nt._save_state(state)
                return 200, _paginate([nt._strip(b) for b in created], query)
        if len(parts) == 2:
            block = nt._get_obj(state, block_id)
            if method == "GET":
                if not block:
                    return 404, nt._err(404, "object_not_found",
                                        "block not found")
                return 200, nt._strip(block)
            if method == "DELETE":
                if not block:
                    return 404, nt._err(404, "object_not_found",
                                        "block not found")
                block["archived"] = True
                block["in_trash"] = True
                nt._touch(block)
                nt._save_state(state)
                return 200, nt._strip(block)
            if method == "PATCH":
                if not block:
                    return 404, nt._err(404, "object_not_found",
                                        "block not found")
                block.update({k: v for k, v in body.items()
                              if k not in ("object", "id")})
                nt._touch(block)
                nt._save_state(state)
                return 200, nt._strip(block)

    # databases
    if parts and parts[0] == "databases":
        if len(parts) == 1 and method == "POST":
            db = nt._make_page(body.get("parent") or {},
                               body.get("properties"),
                               body.get("icon"), body.get("cover"))
            db["object"] = "database"
            db["title"] = body.get("title") or []
            state["objects"][db["id"]] = db
            nt._save_state(state)
            return 200, nt._strip(db)
        db = nt._get_obj(state, parts[1]) if len(parts) >= 2 else None
        if len(parts) == 2 and method == "GET":
            if not db:
                return 404, nt._err(404, "object_not_found",
                                    "database not found")
            return 200, nt._strip(db)
        if len(parts) == 3 and parts[2] == "query" and method == "POST":
            if not db:
                return 404, nt._err(404, "object_not_found",
                                    "database not found")
            rows = [nt._strip(o) for o in state.get("objects", {}).values()
                    if o.get("object") == "page"
                    and o.get("parent", {}).get("database_id") == parts[1]]
            return 200, _paginate(rows, {**query, **body})

    raise NotImplementedError(f"notion facade: {method} {path}")
