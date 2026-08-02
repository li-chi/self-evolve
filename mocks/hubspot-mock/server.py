"""HubSpot CRM mock MCP server.

Mirrors the HubSpot CRM API v3 (https://developers.hubspot.com/docs/api/crm/).
Tool names and parameter shapes follow the per-objectType verb pattern
(`list_<type>`, `get_<type>`, `create_<type>`, `update_<type>`,
`archive_<type>`, `search_<type>`). Responses are HubSpot-shaped JSON:

    {"id": "12345678",
     "properties": {...},
     "createdAt": "ISO",
     "updatedAt": "ISO",
     "archived": false}

List responses wrap results:

    {"results": [...], "paging": {"next": {"after": "<cursor>"}}}

Errors are returned as HubSpot error objects (status="error",
correlationId, category) — NOT raised — so the trace looks like a
real failed REST call.

State lives at `$HUBSPOT_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/hubspot_mock`). Optional `HUBSPOT_MOCK_SEED_PATH`
preloads state when no state.json exists. Every call (including
reads) appends to `state["calls"]` so verifiers can replay traces.

Tool surface (per HubSpot CRM v3 object API):

  Contacts:    list_contacts, get_contact, create_contact,
               update_contact, archive_contact, search_contacts
  Companies:   list_companies, get_company, create_company,
               update_company
  Deals:       list_deals, get_deal, create_deal, update_deal
  Engagements: create_note, create_task, create_email
  Associations: create_association
  Pipelines:   list_pipelines, get_pipeline

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.
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


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "HUBSPOT_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/hubspot_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _correlation_id() -> str:
    return str(uuid.uuid4())


# Default HubSpot CRM deal pipeline stages (matches `default` pipeline).
_DEFAULT_DEAL_PIPELINE = {
    "id": "default",
    "label": "Sales Pipeline",
    "displayOrder": 0,
    "archived": False,
    "stages": [
        {"id": "appointmentscheduled", "label": "Appointment Scheduled",
         "displayOrder": 0, "metadata": {"isClosed": "false",
                                         "probability": "0.2"},
         "archived": False},
        {"id": "qualifiedtobuy", "label": "Qualified To Buy",
         "displayOrder": 1, "metadata": {"isClosed": "false",
                                         "probability": "0.4"},
         "archived": False},
        {"id": "presentationscheduled", "label": "Presentation Scheduled",
         "displayOrder": 2, "metadata": {"isClosed": "false",
                                         "probability": "0.6"},
         "archived": False},
        {"id": "decisionmakerboughtin", "label": "Decision Maker Bought-In",
         "displayOrder": 3, "metadata": {"isClosed": "false",
                                         "probability": "0.8"},
         "archived": False},
        {"id": "contractsent", "label": "Contract Sent",
         "displayOrder": 4, "metadata": {"isClosed": "false",
                                         "probability": "0.9"},
         "archived": False},
        {"id": "closedwon", "label": "Closed Won",
         "displayOrder": 5, "metadata": {"isClosed": "true",
                                         "probability": "1.0"},
         "archived": False},
        {"id": "closedlost", "label": "Closed Lost",
         "displayOrder": 6, "metadata": {"isClosed": "true",
                                         "probability": "0.0"},
         "archived": False},
    ],
    "createdAt": "2024-01-01T00:00:00.000Z",
    "updatedAt": "2024-01-01T00:00:00.000Z",
}


def _empty_state() -> dict:
    return {
        "portal": {"id": "12345678", "domain": "mock.hubspot.com"},
        "objects": {
            "contacts": {},   # id -> object
            "companies": {},
            "deals": {},
            "notes": {},
            "tasks": {},
            "emails": {},
        },
        "associations": [],   # list[{fromObjectType,fromId,toObjectType,toId,types:[...]}]
        "pipelines": {
            "deals": {"default": dict(_DEFAULT_DEAL_PIPELINE)},
            "tickets": {},
        },
        "next_id": {
            "contacts": 90000001,
            "companies": 90000001,
            "deals": 90000001,
            "notes": 90000001,
            "tasks": 90000001,
            "emails": 90000001,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("HUBSPOT_MOCK_SEED_PATH")
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
# HubSpot-shaped helpers
# ---------------------------------------------------------------------------

def _err(category: str, message: str,
         sub_category: str | None = None,
         status_code: int = 404) -> dict:
    """HubSpot-shaped error object. The real REST API returns
    {"status":"error","message":...,"correlationId":...,"category":...}
    on 4xx/5xx; we mirror that body shape exactly so traces look real."""
    body = {
        "status": "error",
        "message": message,
        "correlationId": _correlation_id(),
        "category": category,
    }
    if sub_category:
        body["subCategory"] = sub_category
    return body


def _next_id(state: dict, kind: str) -> str:
    n = state["next_id"][kind]
    state["next_id"][kind] = n + 1
    return str(n)


def _wrap(obj: dict, properties_with_history: list | None = None) -> dict:
    """Return the canonical object envelope HubSpot returns for a single
    record: id, properties, createdAt, updatedAt, archived. Skips
    internal underscore-prefixed keys."""
    out = {
        "id": obj["id"],
        "properties": {k: v for k, v in obj.get("properties", {}).items()
                       if not (isinstance(v, dict) and v.get("_internal"))},
        "createdAt": obj.get("createdAt"),
        "updatedAt": obj.get("updatedAt"),
        "archived": bool(obj.get("archived", False)),
    }
    if obj.get("archivedAt"):
        out["archivedAt"] = obj["archivedAt"]
    return out


def _filter_properties(obj: dict, properties: list | None) -> dict:
    """If `properties` is provided, restrict the object's `properties`
    map to that subset (matching HubSpot's `properties` query param)."""
    wrapped = _wrap(obj)
    if properties:
        wrapped["properties"] = {k: wrapped["properties"].get(k, "")
                                 for k in properties}
    return wrapped


def _coerce_properties(properties: dict | None) -> dict:
    """All HubSpot property values are stringified in REST responses."""
    if not properties:
        return {}
    out = {}
    for k, v in properties.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[k] = str(v)
        else:
            out[k] = str(v)
    return out


def _paginate(items: list, after: str | None, limit: int) -> tuple[list, str | None]:
    """Cursor pagination: `after` is the id immediately after which to
    resume (HubSpot returns paging.next.after = last item's id)."""
    if limit <= 0:
        limit = 10
    if limit > 100:
        limit = 100
    start = 0
    if after:
        for i, it in enumerate(items):
            if it.get("id") == after:
                start = i + 1
                break
    end = start + limit
    page = items[start:end]
    next_after = None
    if end < len(items) and page:
        next_after = page[-1]["id"]
    return page, next_after


def _list_envelope(results: list, next_after: str | None) -> dict:
    body: dict[str, Any] = {"results": results}
    if next_after:
        body["paging"] = {"next": {"after": next_after,
                                   "link": f"?after={next_after}"}}
    return body


def _now_obj(obj_id: str, properties: dict) -> dict:
    now = _now()
    return {
        "id": obj_id,
        "properties": properties,
        "createdAt": now,
        "updatedAt": now,
        "archived": False,
    }


def _touch(obj: dict) -> None:
    obj["updatedAt"] = _now()


# ---------------------------------------------------------------------------
# Generic CRUD (used by contacts/companies/deals)
# ---------------------------------------------------------------------------

_OBJECT_KINDS = {"contacts", "companies", "deals",
                 "notes", "tasks", "emails"}


def _generic_list(state: dict, kind: str,
                  limit: int, after: str | None,
                  properties: list | None,
                  archived: bool) -> dict:
    objs = list(state["objects"][kind].values())
    objs = [o for o in objs if bool(o.get("archived", False)) == archived]
    objs.sort(key=lambda o: int(o["id"]))
    page, next_after = _paginate(objs, after, limit)
    results = [_filter_properties(o, properties) for o in page]
    return _list_envelope(results, next_after)


def _generic_get(state: dict, kind: str, object_id: str,
                 properties: list | None,
                 archived: bool) -> dict:
    obj = state["objects"][kind].get(str(object_id))
    if not obj:
        return _err("OBJECT_NOT_FOUND",
                    f"resource not found for objectType={kind}, "
                    f"objectId={object_id}")
    if not archived and obj.get("archived"):
        return _err("OBJECT_NOT_FOUND",
                    f"resource not found for objectType={kind}, "
                    f"objectId={object_id}")
    return _filter_properties(obj, properties)


def _generic_create(state: dict, kind: str,
                    properties: dict | None,
                    associations: list | None = None) -> dict:
    oid = _next_id(state, kind)
    obj = _now_obj(oid, _coerce_properties(properties))
    state["objects"][kind][oid] = obj
    # process inline associations (HubSpot lets you create an object
    # with associations[].to.id + associations[].types[].associationTypeId)
    if associations:
        for assoc in associations:
            if not isinstance(assoc, dict):
                continue
            to = assoc.get("to") or {}
            to_id = str(to.get("id", ""))
            if not to_id:
                continue
            # association `types` carries category + typeId; we just record
            types = assoc.get("types") or []
            to_object_type = _infer_to_object_type(state, to_id, kind)
            if not to_object_type:
                continue
            state["associations"].append({
                "fromObjectType": kind,
                "fromObjectId": oid,
                "toObjectType": to_object_type,
                "toObjectId": to_id,
                "associationTypes": types,
                "createdAt": _now(),
            })
    return _wrap(obj)


def _infer_to_object_type(state: dict, to_id: str,
                          from_kind: str) -> str | None:
    """Inline association payloads don't always specify object type.
    Look across catalogs to find the id."""
    for kind in _OBJECT_KINDS:
        if to_id in state["objects"][kind]:
            return kind
    return None


def _generic_update(state: dict, kind: str, object_id: str,
                    properties: dict | None) -> dict:
    obj = state["objects"][kind].get(str(object_id))
    if not obj:
        return _err("OBJECT_NOT_FOUND",
                    f"resource not found for objectType={kind}, "
                    f"objectId={object_id}")
    obj.setdefault("properties", {}).update(_coerce_properties(properties))
    _touch(obj)
    return _wrap(obj)


def _generic_archive(state: dict, kind: str, object_id: str) -> dict | None:
    """HubSpot DELETE returns 204 (no body) on success; we return None
    to signal that to callers which translate to an empty object."""
    obj = state["objects"][kind].get(str(object_id))
    if not obj:
        return _err("OBJECT_NOT_FOUND",
                    f"resource not found for objectType={kind}, "
                    f"objectId={object_id}")
    obj["archived"] = True
    obj["archivedAt"] = _now()
    _touch(obj)
    return None


# ---------------------------------------------------------------------------
# Search filter evaluation (HubSpot v3 search endpoint shape)
# ---------------------------------------------------------------------------

_OPERATORS = {
    "EQ", "NEQ", "LT", "LTE", "GT", "GTE",
    "BETWEEN", "IN", "NOT_IN",
    "HAS_PROPERTY", "NOT_HAS_PROPERTY",
    "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN",
}


def _matches_filter(obj: dict, flt: dict) -> bool:
    prop = flt.get("propertyName")
    op = flt.get("operator", "EQ")
    val = flt.get("value")
    values = flt.get("values") or []
    high = flt.get("highValue")
    raw = obj.get("properties", {}).get(prop)
    if op == "HAS_PROPERTY":
        return raw not in (None, "")
    if op == "NOT_HAS_PROPERTY":
        return raw in (None, "")
    if raw is None:
        return False
    if op == "EQ":
        return str(raw) == str(val)
    if op == "NEQ":
        return str(raw) != str(val)
    if op == "CONTAINS_TOKEN":
        return val is not None and str(val).lower() in str(raw).lower()
    if op == "NOT_CONTAINS_TOKEN":
        return val is None or str(val).lower() not in str(raw).lower()
    if op == "IN":
        return str(raw) in {str(v) for v in values}
    if op == "NOT_IN":
        return str(raw) not in {str(v) for v in values}
    try:
        n = float(raw)
    except (TypeError, ValueError):
        # Fallback to string compare for non-numeric props
        s = str(raw)
        v = str(val) if val is not None else ""
        if op == "LT":
            return s < v
        if op == "LTE":
            return s <= v
        if op == "GT":
            return s > v
        if op == "GTE":
            return s >= v
        if op == "BETWEEN":
            return v <= s <= str(high)
        return False
    try:
        fv = float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return False
    if op == "LT":
        return n < fv
    if op == "LTE":
        return n <= fv
    if op == "GT":
        return n > fv
    if op == "GTE":
        return n >= fv
    if op == "BETWEEN":
        try:
            hv = float(high)
        except (TypeError, ValueError):
            return False
        return fv <= n <= hv
    return False


def _matches_filter_groups(obj: dict, filter_groups: list | None,
                            query: str | None) -> bool:
    """HubSpot search: filter_groups are OR'd, filters within a group
    are AND'd. `query` is a free-text substring match across all string
    property values."""
    if filter_groups:
        any_group = False
        for g in filter_groups:
            if not isinstance(g, dict):
                continue
            filters = g.get("filters", [])
            if all(_matches_filter(obj, f) for f in filters):
                any_group = True
                break
        if not any_group:
            return False
    if query:
        q = query.lower()
        hay = " ".join(str(v) for v in obj.get("properties", {}).values()
                       if v is not None).lower()
        if q not in hay:
            return False
    return True


def _apply_sorts(objs: list, sorts: list | None) -> list:
    if not sorts:
        return sorted(objs, key=lambda o: int(o["id"]))
    for sort in reversed(sorts):
        if isinstance(sort, str):
            # Shorthand "propertyName" — ascending
            prop = sort
            direction = "ASCENDING"
        elif isinstance(sort, dict):
            prop = sort.get("propertyName")
            direction = sort.get("direction", "ASCENDING")
        else:
            continue
        if not prop:
            continue
        objs = sorted(
            objs,
            key=lambda o: (str(o.get("properties", {}).get(prop, ""))),
            reverse=(direction.upper() == "DESCENDING"),
        )
    return objs


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("hubspot-mock")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@mcp.tool(name="list_contacts")
def list_contacts(limit: int = 10,
                  after: str | None = None,
                  properties: list | None = None,
                  archived: bool = False) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/contacts — list contacts.

    Args mirror the real query params: `limit` (1-100, default 10),
    `after` (cursor from `paging.next.after`), `properties` (subset of
    contact properties to return; defaults to the standard set),
    `archived` (return archived contacts instead of active ones).
    """
    with _lock():
        s = _load_state()
        body = _generic_list(s, "contacts", limit, after, properties,
                             archived)
        _record(s, "list_contacts", limit=limit, after=after,
                count=len(body["results"]))
        _save_state(s)
        return body


@mcp.tool(name="get_contact")
def get_contact(contact_id: str,
                properties: list | None = None,
                properties_with_history: list | None = None,
                associations: list | None = None,
                archived: bool = False,
                id_property: str | None = None) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/contacts/{contactId} —
    retrieve a single contact by id (or by `idProperty` like `email`
    when supplied)."""
    with _lock():
        s = _load_state()
        if id_property and id_property != "hs_object_id":
            match = next((o for o in s["objects"]["contacts"].values()
                          if (o.get("properties", {}).get(id_property, "")
                              .lower() == str(contact_id).lower())),
                         None)
            if not match:
                _record(s, "get_contact", contact_id=contact_id,
                        id_property=id_property, result="not_found")
                _save_state(s)
                return _err("OBJECT_NOT_FOUND",
                            f"resource not found for objectType=contacts, "
                            f"{id_property}={contact_id}")
            body = _filter_properties(match, properties)
        else:
            body = _generic_get(s, "contacts", contact_id, properties,
                                archived)
        if associations and isinstance(body, dict) and "id" in body:
            assoc_block = {}
            for to_type in associations:
                links = [a for a in s["associations"]
                         if a["fromObjectType"] == "contacts"
                         and a["fromObjectId"] == body["id"]
                         and a["toObjectType"] == to_type]
                assoc_block[to_type] = {
                    "results": [{"id": a["toObjectId"],
                                  "type": (a.get("associationTypes", [{}])[0]
                                            .get("name", f"contact_to_{to_type}"))}
                                 for a in links]
                }
            if assoc_block:
                body["associations"] = assoc_block
        _record(s, "get_contact", contact_id=contact_id,
                result="ok" if "id" in body else "not_found")
        _save_state(s)
        return body


@mcp.tool(name="create_contact")
def create_contact(properties: dict,
                   associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/contacts — create a new
    contact. `properties` is a flat key/value map (email, firstname,
    lastname, phone, company, jobtitle, lifecyclestage, etc).
    Returns the created object envelope or an error if a contact with
    the same `email` already exists."""
    with _lock():
        s = _load_state()
        email = (properties or {}).get("email", "").strip()
        if email:
            dupe = next((o for o in s["objects"]["contacts"].values()
                         if o.get("properties", {}).get("email", "").lower()
                         == email.lower() and not o.get("archived")),
                        None)
            if dupe:
                _record(s, "create_contact", result="duplicate",
                        existing_id=dupe["id"])
                _save_state(s)
                return _err(
                    "CONFLICT",
                    f"Contact already exists. Existing ID: {dupe['id']}",
                    sub_category="CONTACT_EXISTS")
        body = _generic_create(s, "contacts", properties, associations)
        _record(s, "create_contact", contact_id=body.get("id"),
                email=email or None)
        _save_state(s)
        return body


@mcp.tool(name="update_contact")
def update_contact(contact_id: str,
                   properties: dict,
                   id_property: str | None = None) -> dict:
    """HubSpot CRM v3: PATCH /crm/v3/objects/contacts/{contactId} —
    update a contact's properties. Only changed properties need be
    included; missing keys are left untouched."""
    with _lock():
        s = _load_state()
        target_id = str(contact_id)
        if id_property and id_property != "hs_object_id":
            match = next((o for o in s["objects"]["contacts"].values()
                          if (o.get("properties", {}).get(id_property, "")
                              .lower() == str(contact_id).lower())),
                         None)
            if not match:
                _record(s, "update_contact", contact_id=contact_id,
                        id_property=id_property, result="not_found")
                _save_state(s)
                return _err(
                    "OBJECT_NOT_FOUND",
                    f"resource not found for objectType=contacts, "
                    f"{id_property}={contact_id}")
            target_id = match["id"]
        body = _generic_update(s, "contacts", target_id, properties)
        _record(s, "update_contact", contact_id=target_id,
                property_keys=list((properties or {}).keys()),
                result="ok" if "id" in body else "not_found")
        _save_state(s)
        return body


@mcp.tool(name="archive_contact")
def archive_contact(contact_id: str) -> dict:
    """HubSpot CRM v3: DELETE /crm/v3/objects/contacts/{contactId} —
    archive (soft-delete) a contact. Returns an empty body on success
    (mirrors the real 204) or an error object on not-found."""
    with _lock():
        s = _load_state()
        err = _generic_archive(s, "contacts", contact_id)
        _record(s, "archive_contact", contact_id=contact_id,
                result="not_found" if err else "ok")
        _save_state(s)
        return err if err else {}


@mcp.tool(name="search_contacts")
def search_contacts(query: str | None = None,
                    filter_groups: list | None = None,
                    sorts: list | None = None,
                    properties: list | None = None,
                    limit: int = 10,
                    after: str | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/contacts/search — search
    contacts. `filter_groups` is a list of {"filters":[{propertyName,
    operator,value}]} groups (filters AND-ed within a group, groups
    OR-ed). `query` is a free-text substring match over all property
    values. `sorts` is a list of {"propertyName":...,"direction":
    "ASCENDING"|"DESCENDING"}."""
    with _lock():
        s = _load_state()
        objs = [o for o in s["objects"]["contacts"].values()
                if not o.get("archived")]
        objs = [o for o in objs
                if _matches_filter_groups(o, filter_groups, query)]
        objs = _apply_sorts(objs, sorts)
        page, next_after = _paginate(objs, after, limit)
        results = [_filter_properties(o, properties) for o in page]
        body = _list_envelope(results, next_after)
        body["total"] = len(objs)
        _record(s, "search_contacts", query=query,
                filter_groups=filter_groups, count=len(results))
        _save_state(s)
        return body


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

@mcp.tool(name="list_companies")
def list_companies(limit: int = 10,
                   after: str | None = None,
                   properties: list | None = None,
                   archived: bool = False) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/companies — list companies.
    Same args as `list_contacts`."""
    with _lock():
        s = _load_state()
        body = _generic_list(s, "companies", limit, after, properties,
                             archived)
        _record(s, "list_companies", limit=limit, after=after,
                count=len(body["results"]))
        _save_state(s)
        return body


@mcp.tool(name="get_company")
def get_company(company_id: str,
                properties: list | None = None,
                associations: list | None = None,
                archived: bool = False) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/companies/{companyId} —
    retrieve a single company."""
    with _lock():
        s = _load_state()
        body = _generic_get(s, "companies", company_id, properties,
                            archived)
        if associations and isinstance(body, dict) and "id" in body:
            assoc_block = {}
            for to_type in associations:
                links = [a for a in s["associations"]
                         if a["fromObjectType"] == "companies"
                         and a["fromObjectId"] == body["id"]
                         and a["toObjectType"] == to_type]
                assoc_block[to_type] = {
                    "results": [{"id": a["toObjectId"],
                                  "type": (a.get("associationTypes", [{}])[0]
                                            .get("name", f"company_to_{to_type}"))}
                                 for a in links]
                }
            if assoc_block:
                body["associations"] = assoc_block
        _record(s, "get_company", company_id=company_id,
                result="ok" if "id" in body else "not_found")
        _save_state(s)
        return body


@mcp.tool(name="create_company")
def create_company(properties: dict,
                   associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/companies — create a
    company. Common properties: name, domain, industry, city, country,
    numberofemployees, annualrevenue."""
    with _lock():
        s = _load_state()
        body = _generic_create(s, "companies", properties, associations)
        _record(s, "create_company", company_id=body.get("id"),
                name=(properties or {}).get("name"))
        _save_state(s)
        return body


@mcp.tool(name="update_company")
def update_company(company_id: str, properties: dict) -> dict:
    """HubSpot CRM v3: PATCH /crm/v3/objects/companies/{companyId} —
    update a company's properties."""
    with _lock():
        s = _load_state()
        body = _generic_update(s, "companies", company_id, properties)
        _record(s, "update_company", company_id=company_id,
                property_keys=list((properties or {}).keys()),
                result="ok" if "id" in body else "not_found")
        _save_state(s)
        return body


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------

@mcp.tool(name="list_deals")
def list_deals(limit: int = 10,
               after: str | None = None,
               properties: list | None = None,
               archived: bool = False) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/deals — list deals."""
    with _lock():
        s = _load_state()
        body = _generic_list(s, "deals", limit, after, properties,
                             archived)
        _record(s, "list_deals", limit=limit, after=after,
                count=len(body["results"]))
        _save_state(s)
        return body


@mcp.tool(name="get_deal")
def get_deal(deal_id: str,
             properties: list | None = None,
             associations: list | None = None,
             archived: bool = False) -> dict:
    """HubSpot CRM v3: GET /crm/v3/objects/deals/{dealId} — retrieve
    a single deal."""
    with _lock():
        s = _load_state()
        body = _generic_get(s, "deals", deal_id, properties, archived)
        if associations and isinstance(body, dict) and "id" in body:
            assoc_block = {}
            for to_type in associations:
                links = [a for a in s["associations"]
                         if a["fromObjectType"] == "deals"
                         and a["fromObjectId"] == body["id"]
                         and a["toObjectType"] == to_type]
                assoc_block[to_type] = {
                    "results": [{"id": a["toObjectId"],
                                  "type": (a.get("associationTypes", [{}])[0]
                                            .get("name", f"deal_to_{to_type}"))}
                                 for a in links]
                }
            if assoc_block:
                body["associations"] = assoc_block
        _record(s, "get_deal", deal_id=deal_id,
                result="ok" if "id" in body else "not_found")
        _save_state(s)
        return body


@mcp.tool(name="create_deal")
def create_deal(properties: dict,
                associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/deals — create a deal.

    Common properties: dealname, dealstage, pipeline (defaults to
    `default`), amount, closedate, hubspot_owner_id. If `dealstage` is
    set, it is validated against the deal's pipeline; unknown stages
    return a VALIDATION_ERROR."""
    with _lock():
        s = _load_state()
        props = dict(properties or {})
        pipeline_id = props.get("pipeline") or "default"
        props.setdefault("pipeline", pipeline_id)
        stage = props.get("dealstage")
        if stage:
            pipeline = s["pipelines"]["deals"].get(pipeline_id)
            if not pipeline:
                _record(s, "create_deal", result="unknown_pipeline",
                        pipeline=pipeline_id)
                _save_state(s)
                return _err("VALIDATION_ERROR",
                            f"unknown pipeline: {pipeline_id}",
                            sub_category="PROPERTY_VALUE_INVALID",
                            status_code=400)
            valid = {st["id"] for st in pipeline.get("stages", [])}
            if stage not in valid:
                _record(s, "create_deal", result="invalid_stage",
                        stage=stage)
                _save_state(s)
                return _err(
                    "VALIDATION_ERROR",
                    f"dealstage {stage!r} is not valid for pipeline "
                    f"{pipeline_id!r}",
                    sub_category="PROPERTY_VALUE_INVALID",
                    status_code=400)
        body = _generic_create(s, "deals", props, associations)
        _record(s, "create_deal", deal_id=body.get("id"),
                dealname=props.get("dealname"),
                dealstage=props.get("dealstage"))
        _save_state(s)
        return body


@mcp.tool(name="update_deal")
def update_deal(deal_id: str, properties: dict) -> dict:
    """HubSpot CRM v3: PATCH /crm/v3/objects/deals/{dealId} — update a
    deal. If `dealstage` is updated, it is validated against the deal's
    current `pipeline` (or the new pipeline if also being changed)."""
    with _lock():
        s = _load_state()
        obj = s["objects"]["deals"].get(str(deal_id))
        if not obj:
            _record(s, "update_deal", deal_id=deal_id, result="not_found")
            _save_state(s)
            return _err("OBJECT_NOT_FOUND",
                        f"resource not found for objectType=deals, "
                        f"objectId={deal_id}")
        new_props = _coerce_properties(properties)
        target_pipeline = (new_props.get("pipeline")
                           or obj.get("properties", {}).get("pipeline")
                           or "default")
        new_stage = new_props.get("dealstage")
        if new_stage:
            pipeline = s["pipelines"]["deals"].get(target_pipeline)
            if not pipeline:
                _record(s, "update_deal", deal_id=deal_id,
                        result="unknown_pipeline",
                        pipeline=target_pipeline)
                _save_state(s)
                return _err("VALIDATION_ERROR",
                            f"unknown pipeline: {target_pipeline}",
                            sub_category="PROPERTY_VALUE_INVALID",
                            status_code=400)
            valid = {st["id"] for st in pipeline.get("stages", [])}
            if new_stage not in valid:
                _record(s, "update_deal", deal_id=deal_id,
                        result="invalid_stage", stage=new_stage)
                _save_state(s)
                return _err(
                    "VALIDATION_ERROR",
                    f"dealstage {new_stage!r} is not valid for pipeline "
                    f"{target_pipeline!r}",
                    sub_category="PROPERTY_VALUE_INVALID",
                    status_code=400)
        obj.setdefault("properties", {}).update(new_props)
        _touch(obj)
        _record(s, "update_deal", deal_id=deal_id,
                property_keys=list(new_props.keys()))
        _save_state(s)
        return _wrap(obj)


# ---------------------------------------------------------------------------
# Engagements (notes, tasks, emails) — HubSpot CRM v3 modeled as objects
# ---------------------------------------------------------------------------

def _create_engagement(state: dict, kind: str,
                       properties: dict | None,
                       associations: list | None) -> dict:
    """Notes/tasks/emails are object types in v3. They live at
    /crm/v3/objects/{notes,tasks,emails} with the same envelope shape."""
    return _generic_create(state, kind, properties, associations)


@mcp.tool(name="create_note")
def create_note(properties: dict,
                associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/notes — create a note
    engagement. Common properties: hs_note_body, hs_timestamp,
    hubspot_owner_id. Inline `associations` link the note to a
    contact/company/deal via association type ids."""
    with _lock():
        s = _load_state()
        props = dict(properties or {})
        props.setdefault("hs_timestamp", _now())
        body = _create_engagement(s, "notes", props, associations)
        _record(s, "create_note", note_id=body.get("id"))
        _save_state(s)
        return body


@mcp.tool(name="create_task")
def create_task(properties: dict,
                associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/tasks — create a task
    engagement. Common properties: hs_task_subject, hs_task_body,
    hs_task_status (NOT_STARTED/IN_PROGRESS/WAITING/COMPLETED/DEFERRED),
    hs_task_type (TODO/CALL/EMAIL), hs_timestamp,
    hubspot_owner_id."""
    with _lock():
        s = _load_state()
        props = dict(properties or {})
        props.setdefault("hs_timestamp", _now())
        props.setdefault("hs_task_status", "NOT_STARTED")
        body = _create_engagement(s, "tasks", props, associations)
        _record(s, "create_task", task_id=body.get("id"),
                subject=props.get("hs_task_subject"))
        _save_state(s)
        return body


@mcp.tool(name="create_email")
def create_email(properties: dict,
                 associations: list | None = None) -> dict:
    """HubSpot CRM v3: POST /crm/v3/objects/emails — create an email
    engagement. Common properties: hs_email_subject, hs_email_text,
    hs_email_html, hs_email_direction (INCOMING_EMAIL/EMAIL),
    hs_email_status (SENT/BOUNCED), hs_timestamp,
    hubspot_owner_id."""
    with _lock():
        s = _load_state()
        props = dict(properties or {})
        props.setdefault("hs_timestamp", _now())
        body = _create_engagement(s, "emails", props, associations)
        _record(s, "create_email", email_id=body.get("id"),
                subject=props.get("hs_email_subject"))
        _save_state(s)
        return body


# ---------------------------------------------------------------------------
# Associations
# ---------------------------------------------------------------------------

@mcp.tool(name="create_association")
def create_association(from_object_type: str,
                       from_object_id: str,
                       to_object_type: str,
                       to_object_id: str,
                       association_types: list | None = None) -> dict:
    """HubSpot CRM v3: PUT
    /crm/v4/objects/{fromObjectType}/{fromObjectId}/associations/
    {toObjectType}/{toObjectId} — associate two CRM records.

    `association_types` is a list of {"associationCategory":
    "HUBSPOT_DEFINED"|"USER_DEFINED", "associationTypeId": int}. If
    omitted, the default category for the (fromType, toType) pair is
    used."""
    with _lock():
        s = _load_state()
        if (from_object_type not in _OBJECT_KINDS
                or to_object_type not in _OBJECT_KINDS):
            _record(s, "create_association",
                    result="unknown_object_type",
                    from_type=from_object_type, to_type=to_object_type)
            _save_state(s)
            return _err("VALIDATION_ERROR",
                        f"unsupported objectType in association: "
                        f"{from_object_type} -> {to_object_type}",
                        status_code=400)
        if str(from_object_id) not in s["objects"][from_object_type]:
            _record(s, "create_association", result="from_not_found",
                    from_id=from_object_id)
            _save_state(s)
            return _err("OBJECT_NOT_FOUND",
                        f"resource not found for objectType="
                        f"{from_object_type}, objectId={from_object_id}")
        if str(to_object_id) not in s["objects"][to_object_type]:
            _record(s, "create_association", result="to_not_found",
                    to_id=to_object_id)
            _save_state(s)
            return _err("OBJECT_NOT_FOUND",
                        f"resource not found for objectType="
                        f"{to_object_type}, objectId={to_object_id}")
        types = association_types or [{
            "associationCategory": "HUBSPOT_DEFINED",
            "associationTypeId": 1,
        }]
        # Dedupe identical edges
        existing = next(
            (a for a in s["associations"]
             if a["fromObjectType"] == from_object_type
             and a["fromObjectId"] == str(from_object_id)
             and a["toObjectType"] == to_object_type
             and a["toObjectId"] == str(to_object_id)),
            None,
        )
        if existing is None:
            s["associations"].append({
                "fromObjectType": from_object_type,
                "fromObjectId": str(from_object_id),
                "toObjectType": to_object_type,
                "toObjectId": str(to_object_id),
                "associationTypes": types,
                "createdAt": _now(),
            })
        _record(s, "create_association",
                from_type=from_object_type, from_id=from_object_id,
                to_type=to_object_type, to_id=to_object_id)
        _save_state(s)
        return {
            "fromObjectTypeId": from_object_type,
            "fromObjectId": str(from_object_id),
            "toObjectTypeId": to_object_type,
            "toObjectId": str(to_object_id),
            "labels": [t.get("associationCategory", "HUBSPOT_DEFINED")
                       for t in types],
        }


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

@mcp.tool(name="list_pipelines")
def list_pipelines(object_type: str = "deals") -> dict:
    """HubSpot CRM v3: GET /crm/v3/pipelines/{objectType} — list all
    pipelines for an object type (typically `deals` or `tickets`).
    Returns {"results":[pipeline,...]}."""
    with _lock():
        s = _load_state()
        pipelines = list(s["pipelines"].get(object_type, {}).values())
        pipelines.sort(key=lambda p: p.get("displayOrder", 0))
        _record(s, "list_pipelines", object_type=object_type,
                count=len(pipelines))
        _save_state(s)
        return {"results": pipelines}


@mcp.tool(name="get_pipeline")
def get_pipeline(object_type: str, pipeline_id: str) -> dict:
    """HubSpot CRM v3: GET /crm/v3/pipelines/{objectType}/{pipelineId}
    — retrieve a single pipeline and its stages."""
    with _lock():
        s = _load_state()
        pipeline = s["pipelines"].get(object_type, {}).get(pipeline_id)
        _record(s, "get_pipeline", object_type=object_type,
                pipeline_id=pipeline_id,
                result="ok" if pipeline else "not_found")
        _save_state(s)
        if not pipeline:
            return _err("OBJECT_NOT_FOUND",
                        f"resource not found for objectType={object_type}, "
                        f"pipelineId={pipeline_id}")
        return dict(pipeline)


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state. Used by verifiers
    and per-task setup to introspect the mock world."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(portal: dict | None = None,
                    contacts: list | None = None,
                    companies: list | None = None,
                    deals: list | None = None,
                    notes: list | None = None,
                    tasks: list | None = None,
                    emails: list | None = None,
                    pipelines: dict | None = None,
                    associations: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: bulk-seed HubSpot-shaped records into state. Each
    entry is a dict containing `properties` (and optionally `id`); ids
    are auto-allocated if missing.

    - `contacts`/`companies`/`deals`: [{"id"?, "properties": {...}}]
    - `notes`/`tasks`/`emails`: [{"id"?, "properties": {...}}]
    - `pipelines`: {"deals": {"<pipeline_id>": {...}}} replaces the
      pipeline dict for that object type.
    - `associations`: [{"fromObjectType","fromObjectId",
                        "toObjectType","toObjectId",
                        "associationTypes":[{...}]}]
    - `portal`: merge into state["portal"].
    - `replace`: reset state to empty before seeding (preserves the
      default deal pipeline)."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if portal:
            s["portal"].update(portal)
        for kind, seeds in (("contacts", contacts),
                            ("companies", companies),
                            ("deals", deals),
                            ("notes", notes),
                            ("tasks", tasks),
                            ("emails", emails)):
            for item in (seeds or []):
                oid = str(item.get("id") or _next_id(s, kind))
                props = _coerce_properties(item.get("properties", {}))
                if kind == "deals":
                    props.setdefault("pipeline", "default")
                now = _now()
                s["objects"][kind][oid] = {
                    "id": oid,
                    "properties": props,
                    "createdAt": item.get("createdAt", now),
                    "updatedAt": item.get("updatedAt", now),
                    "archived": bool(item.get("archived", False)),
                }
                # Keep next_id ahead of any explicitly-seeded id
                try:
                    next_n = int(oid) + 1
                    if next_n > s["next_id"][kind]:
                        s["next_id"][kind] = next_n
                except ValueError:
                    pass
        if pipelines:
            for obj_type, plines in pipelines.items():
                s["pipelines"].setdefault(obj_type, {}).update(plines or {})
        for a in (associations or []):
            if not isinstance(a, dict):
                continue
            s["associations"].append({
                "fromObjectType": a["fromObjectType"],
                "fromObjectId": str(a["fromObjectId"]),
                "toObjectType": a["toObjectType"],
                "toObjectId": str(a["toObjectId"]),
                "associationTypes": a.get("associationTypes")
                or [{"associationCategory": "HUBSPOT_DEFINED",
                     "associationTypeId": 1}],
                "createdAt": a.get("createdAt", _now()),
            })
        _record(s, "debug_seed", replace=replace,
                counts={
                    "contacts": len(contacts or []),
                    "companies": len(companies or []),
                    "deals": len(deals or []),
                    "notes": len(notes or []),
                    "tasks": len(tasks or []),
                    "emails": len(emails or []),
                    "associations": len(associations or []),
                })
        _save_state(s)
        return {
            "ok": True,
            "contact_ids": list(s["objects"]["contacts"].keys()),
            "company_ids": list(s["objects"]["companies"].keys()),
            "deal_ids": list(s["objects"]["deals"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
