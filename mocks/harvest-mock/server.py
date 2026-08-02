"""Harvest mock MCP server.

Mirrors the Harvest REST API v2 surface (help.getharvest.com/api-v2/).
Each tool corresponds to one Harvest endpoint, takes the same query
parameters / body fields the real API accepts, and returns Harvest's
JSON response shapes (with `id` as integer, list-envelope pagination,
nested `client`/`project`/`task`/`user` sub-objects, etc.).

Tool surface (28 tools, all under the Harvest v2 namespace, plus 2
mock-only debug helpers):

  Clients
    list_clients, get_client, create_client, update_client, delete_client
  Projects
    list_projects, get_project, create_project, update_project,
    delete_project
  Tasks
    list_tasks, get_task, create_task, update_task, delete_task
  Time Entries
    list_time_entries, get_time_entry, create_time_entry,
    update_time_entry, delete_time_entry, restart_time_entry,
    stop_time_entry
  Users
    list_users, get_current_user, get_user
  Invoices
    list_invoices, get_invoice, create_invoice
  Expenses
    list_expenses, create_expense
  Assignments
    list_project_user_assignments, list_task_assignments

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed`.

State lives at `$HARVEST_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/harvest_mock`). `HARVEST_MOCK_SEED_PATH` preloads state
when no state.json exists. Every call (reads included) appends to
`state["calls"]` so verifiers can replay the trace. File locking via
`fcntl.flock` makes concurrent calls safe.

Errors follow Harvest conventions:
  - 404 / generic: {"message": "Not Found"} (raised as ValueError so
    FastMCP surfaces it cleanly to the client trace)
  - Validation: {"errors": [{"resource": "...", "message": "..."}]}
List responses follow Harvest's envelope:
  {"<resource_plural>": [...], "per_page": 100, "total_pages": N,
   "total_entries": M, "next_page": ..., "previous_page": ...,
   "page": 1, "links": {...}}
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
        "HARVEST_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/harvest_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _empty_state() -> dict:
    return {
        "account": {
            "id": 1000001,
            "name": "Mock Harvest Account",
            "currency": "USD",
        },
        "self": {
            "id": 9000001,
            "first_name": "Mock",
            "last_name": "User",
            "email": "mock@example.com",
            "is_admin": True,
            "is_active": True,
        },
        "users": {},          # id (int) -> user dict
        "clients": {},        # id (int) -> client dict
        "projects": {},       # id (int) -> project dict
        "tasks": {},          # id (int) -> task dict
        "time_entries": {},   # id (int) -> time entry dict
        "invoices": {},       # id (int) -> invoice dict
        "expenses": {},       # id (int) -> expense dict
        "project_user_assignments": {},  # id -> assignment dict
        "task_assignments": {},          # id -> assignment dict
        "next_id": {
            "client": 1, "project": 1, "task": 1,
            "user": 1, "time_entry": 1, "invoice": 1,
            "expense": 1, "project_user_assignment": 1,
            "task_assignment": 1, "line_item": 1,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("HARVEST_MOCK_SEED_PATH")
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
# Helpers
# ---------------------------------------------------------------------------

def _next_id(state: dict, kind: str) -> int:
    """Mint a new integer id of the requested kind. Harvest ids are
    7-10 digit ints; we offset per-kind so collisions are obvious."""
    base = {
        "client": 4000000, "project": 5000000, "task": 6000000,
        "user": 9000000, "time_entry": 1000000, "invoice": 2000000,
        "expense": 3000000, "project_user_assignment": 7000000,
        "task_assignment": 8000000, "line_item": 1500000,
    }.get(kind, 0)
    n = state["next_id"].setdefault(kind, 1)
    state["next_id"][kind] = n + 1
    return base + n


def _err_not_found(resource: str = "Record") -> ValueError:
    return ValueError(json.dumps({"message": "Not Found"}))


def _err_validation(resource: str, message: str) -> ValueError:
    return ValueError(json.dumps({
        "errors": [{"resource": resource, "message": message}],
    }))


def _client_summary(state: dict, client_id: int | None) -> dict | None:
    if client_id is None:
        return None
    c = state["clients"].get(str(client_id)) or state["clients"].get(client_id)
    if not c:
        return None
    return {"id": c["id"], "name": c.get("name", "")}


def _project_summary(state: dict, project_id: int | None) -> dict | None:
    if project_id is None:
        return None
    p = (state["projects"].get(str(project_id))
         or state["projects"].get(project_id))
    if not p:
        return None
    return {"id": p["id"], "name": p.get("name", ""),
            "code": p.get("code", "")}


def _task_summary(state: dict, task_id: int | None) -> dict | None:
    if task_id is None:
        return None
    t = state["tasks"].get(str(task_id)) or state["tasks"].get(task_id)
    if not t:
        return None
    return {"id": t["id"], "name": t.get("name", "")}


def _user_summary(state: dict, user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    u = state["users"].get(str(user_id)) or state["users"].get(user_id)
    if not u:
        return None
    name = f"{u.get('first_name','')} {u.get('last_name','')}".strip()
    return {"id": u["id"], "name": name}


def _lookup(state: dict, bucket: str, obj_id: int | str) -> dict | None:
    """Lookup an object by id, tolerating int/str keys (state file
    persists ints as JSON object keys, which become strings)."""
    coll = state.get(bucket, {})
    return coll.get(str(obj_id)) or coll.get(obj_id)


def _store(state: dict, bucket: str, obj: dict) -> None:
    state.setdefault(bucket, {})[str(obj["id"])] = obj


def _delete(state: dict, bucket: str, obj_id: int | str) -> bool:
    coll = state.get(bucket, {})
    key = str(obj_id) if str(obj_id) in coll else (
        obj_id if obj_id in coll else None)
    if key is None:
        return False
    del coll[key]
    return True


def _values(state: dict, bucket: str) -> list[dict]:
    return list(state.get(bucket, {}).values())


def _bool_to_iso(b: bool | str | None) -> bool | None:
    """Harvest query params for booleans are 'true'/'false' strings.
    Accept both."""
    if b is None or b == "":
        return None
    if isinstance(b, bool):
        return b
    s = str(b).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _filter_updated_since(items: list[dict], updated_since: str) -> list[dict]:
    if not updated_since:
        return items
    return [it for it in items
            if (it.get("updated_at") or "") >= updated_since]


def _filter_date_range(items: list[dict], from_date: str, to_date: str,
                       field: str = "spent_date") -> list[dict]:
    out = items
    if from_date:
        out = [it for it in out if (it.get(field) or "") >= from_date]
    if to_date:
        out = [it for it in out if (it.get(field) or "") <= to_date]
    return out


def _paginate(items: list[dict], page: int, per_page: int,
              resource_plural: str) -> dict:
    """Harvest pagination envelope. `page` is 1-indexed; `per_page`
    defaults to 100 (max 2000 per docs, we cap at 2000)."""
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page or 100)
    except (TypeError, ValueError):
        per_page = 100
    per_page = min(max(per_page, 1), 2000)
    total = len(items)
    total_pages = max((total + per_page - 1) // per_page, 1)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]
    next_page = page + 1 if page < total_pages else None
    prev_page = page - 1 if page > 1 else None
    base = f"https://api.harvestapp.com/v2/{resource_plural}"
    links = {
        "first": f"{base}?page=1&per_page={per_page}",
        "last": f"{base}?page={total_pages}&per_page={per_page}",
        "next": (f"{base}?page={next_page}&per_page={per_page}"
                 if next_page else None),
        "previous": (f"{base}?page={prev_page}&per_page={per_page}"
                     if prev_page else None),
    }
    return {
        resource_plural: page_items,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_entries": total,
        "next_page": next_page,
        "previous_page": prev_page,
        "page": page,
        "links": links,
    }


# ---------------------------------------------------------------------------
# Object constructors (return Harvest-shaped dicts)
# ---------------------------------------------------------------------------

def _make_client(state: dict, name: str, is_active: bool = True,
                 address: str = "", currency: str = "") -> dict:
    cid = _next_id(state, "client")
    now = _now_iso()
    return {
        "id": cid,
        "name": name,
        "is_active": is_active,
        "address": address,
        "statement_key": "",
        "currency": currency or state["account"].get("currency", "USD"),
        "created_at": now,
        "updated_at": now,
    }


def _make_project(state: dict, client_id: int, name: str,
                  code: str = "", is_active: bool = True,
                  is_billable: bool = True, is_fixed_fee: bool = False,
                  bill_by: str = "Project",
                  hourly_rate: float | None = None,
                  budget: float | None = None,
                  budget_by: str = "project",
                  budget_is_monthly: bool = False,
                  notify_when_over_budget: bool = False,
                  over_budget_notification_percentage: float = 80.0,
                  show_budget_to_all: bool = False,
                  cost_budget: float | None = None,
                  cost_budget_include_expenses: bool = False,
                  fee: float | None = None,
                  notes: str = "",
                  starts_on: str | None = None,
                  ends_on: str | None = None) -> dict:
    pid = _next_id(state, "project")
    now = _now_iso()
    return {
        "id": pid,
        "name": name,
        "code": code,
        "is_active": is_active,
        "is_billable": is_billable,
        "is_fixed_fee": is_fixed_fee,
        "bill_by": bill_by,
        "hourly_rate": hourly_rate,
        "budget": budget,
        "budget_by": budget_by,
        "budget_is_monthly": budget_is_monthly,
        "notify_when_over_budget": notify_when_over_budget,
        "over_budget_notification_percentage":
            over_budget_notification_percentage,
        "over_budget_notification_date": None,
        "show_budget_to_all": show_budget_to_all,
        "cost_budget": cost_budget,
        "cost_budget_include_expenses": cost_budget_include_expenses,
        "fee": fee,
        "notes": notes,
        "starts_on": starts_on,
        "ends_on": ends_on,
        "client": _client_summary(state, client_id),
        "created_at": now,
        "updated_at": now,
    }


def _make_task(state: dict, name: str, billable_by_default: bool = True,
               default_hourly_rate: float | None = None,
               is_default: bool = False, is_active: bool = True) -> dict:
    tid = _next_id(state, "task")
    now = _now_iso()
    return {
        "id": tid,
        "name": name,
        "billable_by_default": billable_by_default,
        "default_hourly_rate": default_hourly_rate,
        "is_default": is_default,
        "is_active": is_active,
        "created_at": now,
        "updated_at": now,
    }


def _make_user(state: dict, first_name: str, last_name: str, email: str,
               is_active: bool = True, is_admin: bool = False,
               is_project_manager: bool = False,
               telephone: str = "", timezone: str = "Eastern Time (US & Canada)",
               weekly_capacity: int = 126000,
               default_hourly_rate: float | None = None,
               cost_rate: float | None = None,
               roles: list[str] | None = None) -> dict:
    uid = _next_id(state, "user")
    now = _now_iso()
    return {
        "id": uid,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "telephone": telephone,
        "timezone": timezone,
        "has_access_to_all_future_projects": False,
        "is_contractor": False,
        "is_active": is_active,
        "is_admin": is_admin,
        "is_project_manager": is_project_manager,
        "can_see_rates": is_admin,
        "can_create_projects": is_admin,
        "can_create_invoices": is_admin,
        "weekly_capacity": weekly_capacity,
        "default_hourly_rate": default_hourly_rate,
        "cost_rate": cost_rate,
        "roles": roles or [],
        "avatar_url": "",
        "created_at": now,
        "updated_at": now,
    }


def _make_time_entry(state: dict, *, user_id: int, project_id: int,
                     task_id: int, spent_date: str,
                     hours: float | None = None,
                     notes: str = "", billable: bool | None = None,
                     budgeted: bool = False,
                     billable_rate: float | None = None,
                     cost_rate: float | None = None,
                     is_running: bool = False,
                     timer_started_at: str | None = None,
                     started_time: str | None = None,
                     ended_time: str | None = None) -> dict:
    eid = _next_id(state, "time_entry")
    now = _now_iso()
    proj = _lookup(state, "projects", project_id) or {}
    task = _lookup(state, "tasks", task_id) or {}
    client_id = (proj.get("client") or {}).get("id")
    if billable is None:
        billable = bool(task.get("billable_by_default", True)
                        and proj.get("is_billable", True))
    return {
        "id": eid,
        "spent_date": spent_date,
        "user": _user_summary(state, user_id),
        "user_assignment": None,
        "client": _client_summary(state, client_id),
        "project": _project_summary(state, project_id),
        "task": _task_summary(state, task_id),
        "task_assignment": None,
        "invoice": None,
        "hours": float(hours) if hours is not None else 0.0,
        "hours_without_timer": float(hours) if hours is not None else 0.0,
        "rounded_hours": (round(float(hours) * 4) / 4
                          if hours is not None else 0.0),
        "notes": notes,
        "is_locked": False,
        "locked_reason": None,
        "is_closed": False,
        "is_billed": False,
        "timer_started_at": timer_started_at,
        "started_time": started_time,
        "ended_time": ended_time,
        "is_running": is_running,
        "billable": bool(billable),
        "budgeted": bool(budgeted),
        "billable_rate": billable_rate,
        "cost_rate": cost_rate,
        "created_at": now,
        "updated_at": now,
    }


def _make_invoice(state: dict, *, client_id: int,
                  subject: str = "", purchase_order: str = "",
                  notes: str = "", currency: str | None = None,
                  issue_date: str | None = None,
                  due_date: str | None = None,
                  payment_term: str = "upon receipt",
                  line_items: list[dict] | None = None,
                  tax: float | None = None, tax2: float | None = None,
                  discount: float | None = None) -> dict:
    iid = _next_id(state, "invoice")
    now = _now_iso()
    li_out = []
    amount_total = 0.0
    for li in (line_items or []):
        liid = _next_id(state, "line_item")
        qty = float(li.get("quantity") or 1)
        rate = float(li.get("unit_price") or 0)
        amt = qty * rate
        amount_total += amt
        li_out.append({
            "id": liid,
            "kind": li.get("kind", "Service"),
            "description": li.get("description", ""),
            "quantity": qty,
            "unit_price": rate,
            "amount": amt,
            "taxed": bool(li.get("taxed", False)),
            "taxed2": bool(li.get("taxed2", False)),
            "project": _project_summary(state, li.get("project_id")),
        })
    tax_amount = (amount_total * (tax or 0) / 100.0) if tax else 0.0
    tax2_amount = (amount_total * (tax2 or 0) / 100.0) if tax2 else 0.0
    discount_amount = ((amount_total * (discount or 0) / 100.0)
                       if discount else 0.0)
    total = amount_total + tax_amount + tax2_amount - discount_amount
    return {
        "id": iid,
        "client": _client_summary(state, client_id),
        "line_items": li_out,
        "estimate": None,
        "retainer": None,
        "creator": _user_summary(state, state["self"]["id"]),
        "client_key": f"key{iid}",
        "number": f"{iid}",
        "purchase_order": purchase_order,
        "amount": amount_total,
        "due_amount": total,
        "tax": tax,
        "tax_amount": tax_amount,
        "tax2": tax2,
        "tax2_amount": tax2_amount,
        "discount": discount,
        "discount_amount": discount_amount,
        "subject": subject,
        "notes": notes,
        "currency": (currency
                     or state["account"].get("currency", "USD")),
        "state": "draft",
        "period_start": None,
        "period_end": None,
        "issue_date": issue_date or _today(),
        "due_date": due_date,
        "payment_term": payment_term,
        "sent_at": None,
        "paid_at": None,
        "paid_date": None,
        "closed_at": None,
        "recurring_invoice_id": None,
        "created_at": now,
        "updated_at": now,
    }


def _make_expense(state: dict, *, user_id: int, project_id: int,
                  expense_category_id: int | None = None,
                  spent_date: str | None = None,
                  units: float | None = None,
                  total_cost: float | None = None,
                  notes: str = "", billable: bool = True,
                  receipt: dict | None = None) -> dict:
    xid = _next_id(state, "expense")
    now = _now_iso()
    proj = _lookup(state, "projects", project_id) or {}
    client_id = (proj.get("client") or {}).get("id")
    return {
        "id": xid,
        "spent_date": spent_date or _today(),
        "user": _user_summary(state, user_id),
        "user_assignment": None,
        "project": _project_summary(state, project_id),
        "expense_category": ({"id": expense_category_id,
                              "name": ""}
                             if expense_category_id else None),
        "client": _client_summary(state, client_id),
        "invoice": None,
        "notes": notes,
        "billable": bool(billable),
        "receipt": receipt,
        "units": units,
        "total_cost": total_cost,
        "is_closed": False,
        "is_locked": False,
        "is_billed": False,
        "locked_reason": None,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("harvest-mock")


# ---------------------------------------------------------------------------
# Clients — /v1/clients
# ---------------------------------------------------------------------------

@mcp.tool(name="list_clients")
def list_clients(is_active: bool | str | None = None,
                 updated_since: str = "",
                 page: int = 1,
                 per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/clients. Returns the paginated list
    envelope: {"clients":[...], "per_page", "total_pages",
    "total_entries", "next_page", "previous_page", "page", "links"}."""
    with _lock():
        s = _load_state()
        items = _values(s, "clients")
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [c for c in items if bool(c.get("is_active")) == active]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda c: c["id"])
        env = _paginate(items, page, per_page, "clients")
        _record(s, "list_clients", count=len(env["clients"]),
                is_active=is_active, updated_since=updated_since,
                page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_client")
def get_client(client_id: int) -> dict:
    """Harvest REST: GET /v2/clients/{client_id}."""
    with _lock():
        s = _load_state()
        c = _lookup(s, "clients", client_id)
        _record(s, "get_client", client_id=client_id,
                result="ok" if c else "not_found")
        _save_state(s)
        if not c:
            raise _err_not_found("Client")
        return dict(c)


@mcp.tool(name="create_client")
def create_client(name: str,
                  is_active: bool = True,
                  address: str = "",
                  currency: str = "") -> dict:
    """Harvest REST: POST /v2/clients. `name` is required."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_client", result="validation")
            _save_state(s)
            raise _err_validation("Client", "Name can't be blank")
        c = _make_client(s, name=name, is_active=is_active,
                         address=address, currency=currency)
        _store(s, "clients", c)
        _record(s, "create_client", client_id=c["id"], name=name)
        _save_state(s)
        return c


@mcp.tool(name="update_client")
def update_client(client_id: int,
                  name: str | None = None,
                  is_active: bool | None = None,
                  address: str | None = None,
                  currency: str | None = None) -> dict:
    """Harvest REST: PATCH /v2/clients/{client_id}."""
    with _lock():
        s = _load_state()
        c = _lookup(s, "clients", client_id)
        if not c:
            _record(s, "update_client", client_id=client_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("Client")
        if name is not None:
            c["name"] = name
        if is_active is not None:
            c["is_active"] = bool(is_active)
        if address is not None:
            c["address"] = address
        if currency is not None:
            c["currency"] = currency
        c["updated_at"] = _now_iso()
        _record(s, "update_client", client_id=client_id)
        _save_state(s)
        return c


@mcp.tool(name="delete_client")
def delete_client(client_id: int) -> dict:
    """Harvest REST: DELETE /v2/clients/{client_id}. Returns 200 with
    no body on success — we return {} to match."""
    with _lock():
        s = _load_state()
        ok = _delete(s, "clients", client_id)
        _record(s, "delete_client", client_id=client_id,
                result="ok" if ok else "not_found")
        _save_state(s)
        if not ok:
            raise _err_not_found("Client")
        return {}


# ---------------------------------------------------------------------------
# Projects — /v1/projects
# ---------------------------------------------------------------------------

@mcp.tool(name="list_projects")
def list_projects(is_active: bool | str | None = None,
                  client_id: int | None = None,
                  updated_since: str = "",
                  page: int = 1,
                  per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/projects."""
    with _lock():
        s = _load_state()
        items = _values(s, "projects")
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [p for p in items if bool(p.get("is_active")) == active]
        if client_id is not None:
            items = [p for p in items
                     if (p.get("client") or {}).get("id") == int(client_id)]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda p: p["id"])
        env = _paginate(items, page, per_page, "projects")
        _record(s, "list_projects", count=len(env["projects"]),
                client_id=client_id, is_active=is_active, page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_project")
def get_project(project_id: int) -> dict:
    """Harvest REST: GET /v2/projects/{project_id}."""
    with _lock():
        s = _load_state()
        p = _lookup(s, "projects", project_id)
        _record(s, "get_project", project_id=project_id,
                result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            raise _err_not_found("Project")
        return dict(p)


@mcp.tool(name="create_project")
def create_project(client_id: int, name: str,
                   code: str = "",
                   is_active: bool = True,
                   is_billable: bool = True,
                   is_fixed_fee: bool = False,
                   bill_by: str = "Project",
                   hourly_rate: float | None = None,
                   budget: float | None = None,
                   budget_by: str = "project",
                   budget_is_monthly: bool = False,
                   notify_when_over_budget: bool = False,
                   over_budget_notification_percentage: float = 80.0,
                   show_budget_to_all: bool = False,
                   cost_budget: float | None = None,
                   cost_budget_include_expenses: bool = False,
                   fee: float | None = None,
                   notes: str = "",
                   starts_on: str | None = None,
                   ends_on: str | None = None) -> dict:
    """Harvest REST: POST /v2/projects. `client_id`, `name`, `is_billable`,
    and `bill_by` are required by the real API; we default everything
    sensible. `bill_by` is one of {Project, Tasks, People, none}."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_project", result="validation_name")
            _save_state(s)
            raise _err_validation("Project", "Name can't be blank")
        if not _lookup(s, "clients", client_id):
            _record(s, "create_project", result="bad_client",
                    client_id=client_id)
            _save_state(s)
            raise _err_validation("Project",
                                  f"Client {client_id} does not exist")
        p = _make_project(s, client_id=int(client_id), name=name,
                          code=code, is_active=is_active,
                          is_billable=is_billable,
                          is_fixed_fee=is_fixed_fee, bill_by=bill_by,
                          hourly_rate=hourly_rate, budget=budget,
                          budget_by=budget_by,
                          budget_is_monthly=budget_is_monthly,
                          notify_when_over_budget=notify_when_over_budget,
                          over_budget_notification_percentage=
                          over_budget_notification_percentage,
                          show_budget_to_all=show_budget_to_all,
                          cost_budget=cost_budget,
                          cost_budget_include_expenses=
                          cost_budget_include_expenses,
                          fee=fee, notes=notes, starts_on=starts_on,
                          ends_on=ends_on)
        _store(s, "projects", p)
        _record(s, "create_project", project_id=p["id"],
                client_id=client_id, name=name)
        _save_state(s)
        return p


_PROJECT_FIELDS = {
    "name", "code", "is_active", "is_billable", "is_fixed_fee", "bill_by",
    "hourly_rate", "budget", "budget_by", "budget_is_monthly",
    "notify_when_over_budget", "over_budget_notification_percentage",
    "show_budget_to_all", "cost_budget", "cost_budget_include_expenses",
    "fee", "notes", "starts_on", "ends_on",
}


@mcp.tool(name="update_project")
def update_project(project_id: int, **fields: Any) -> dict:
    """Harvest REST: PATCH /v2/projects/{project_id}. Accepts any
    subset of mutable project fields (name, code, is_active,
    is_billable, is_fixed_fee, bill_by, hourly_rate, budget,
    budget_by, budget_is_monthly, notify_when_over_budget,
    over_budget_notification_percentage, show_budget_to_all,
    cost_budget, cost_budget_include_expenses, fee, notes,
    starts_on, ends_on) plus `client_id` to reassign the client."""
    with _lock():
        s = _load_state()
        p = _lookup(s, "projects", project_id)
        if not p:
            _record(s, "update_project", project_id=project_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("Project")
        for k, v in fields.items():
            if k in _PROJECT_FIELDS:
                p[k] = v
            elif k == "client_id" and v is not None:
                summary = _client_summary(s, int(v))
                if not summary:
                    raise _err_validation("Project",
                                          f"Client {v} does not exist")
                p["client"] = summary
        p["updated_at"] = _now_iso()
        _record(s, "update_project", project_id=project_id,
                fields=list(fields.keys()))
        _save_state(s)
        return p


@mcp.tool(name="delete_project")
def delete_project(project_id: int) -> dict:
    """Harvest REST: DELETE /v2/projects/{project_id}. The real API
    only allows deleting if there are no associated time entries or
    expenses; otherwise it returns 422. We enforce that too."""
    with _lock():
        s = _load_state()
        p = _lookup(s, "projects", project_id)
        if not p:
            _record(s, "delete_project", project_id=project_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("Project")
        for te in _values(s, "time_entries"):
            if (te.get("project") or {}).get("id") == int(project_id):
                _record(s, "delete_project", project_id=project_id,
                        result="has_time_entries")
                _save_state(s)
                raise _err_validation(
                    "Project",
                    "Can't be deleted: project has time entries")
        _delete(s, "projects", project_id)
        _record(s, "delete_project", project_id=project_id)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Tasks — /v1/tasks
# ---------------------------------------------------------------------------

@mcp.tool(name="list_tasks")
def list_tasks(is_active: bool | str | None = None,
               updated_since: str = "",
               page: int = 1,
               per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/tasks."""
    with _lock():
        s = _load_state()
        items = _values(s, "tasks")
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [t for t in items if bool(t.get("is_active")) == active]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda t: t["id"])
        env = _paginate(items, page, per_page, "tasks")
        _record(s, "list_tasks", count=len(env["tasks"]), page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_task")
def get_task(task_id: int) -> dict:
    """Harvest REST: GET /v2/tasks/{task_id}."""
    with _lock():
        s = _load_state()
        t = _lookup(s, "tasks", task_id)
        _record(s, "get_task", task_id=task_id,
                result="ok" if t else "not_found")
        _save_state(s)
        if not t:
            raise _err_not_found("Task")
        return dict(t)


@mcp.tool(name="create_task")
def create_task(name: str,
                billable_by_default: bool = True,
                default_hourly_rate: float | None = None,
                is_default: bool = False,
                is_active: bool = True) -> dict:
    """Harvest REST: POST /v2/tasks."""
    with _lock():
        s = _load_state()
        if not name:
            _record(s, "create_task", result="validation")
            _save_state(s)
            raise _err_validation("Task", "Name can't be blank")
        t = _make_task(s, name=name,
                       billable_by_default=billable_by_default,
                       default_hourly_rate=default_hourly_rate,
                       is_default=is_default, is_active=is_active)
        _store(s, "tasks", t)
        _record(s, "create_task", task_id=t["id"], name=name)
        _save_state(s)
        return t


@mcp.tool(name="update_task")
def update_task(task_id: int,
                name: str | None = None,
                billable_by_default: bool | None = None,
                default_hourly_rate: float | None = None,
                is_default: bool | None = None,
                is_active: bool | None = None) -> dict:
    """Harvest REST: PATCH /v2/tasks/{task_id}."""
    with _lock():
        s = _load_state()
        t = _lookup(s, "tasks", task_id)
        if not t:
            _record(s, "update_task", task_id=task_id, result="not_found")
            _save_state(s)
            raise _err_not_found("Task")
        if name is not None:
            t["name"] = name
        if billable_by_default is not None:
            t["billable_by_default"] = bool(billable_by_default)
        if default_hourly_rate is not None:
            t["default_hourly_rate"] = default_hourly_rate
        if is_default is not None:
            t["is_default"] = bool(is_default)
        if is_active is not None:
            t["is_active"] = bool(is_active)
        t["updated_at"] = _now_iso()
        _record(s, "update_task", task_id=task_id)
        _save_state(s)
        return t


@mcp.tool(name="delete_task")
def delete_task(task_id: int) -> dict:
    """Harvest REST: DELETE /v2/tasks/{task_id}. Disallows deletion if
    the task is referenced by any time entry (matches real API 422)."""
    with _lock():
        s = _load_state()
        t = _lookup(s, "tasks", task_id)
        if not t:
            _record(s, "delete_task", task_id=task_id, result="not_found")
            _save_state(s)
            raise _err_not_found("Task")
        for te in _values(s, "time_entries"):
            if (te.get("task") or {}).get("id") == int(task_id):
                _record(s, "delete_task", task_id=task_id,
                        result="has_time_entries")
                _save_state(s)
                raise _err_validation(
                    "Task",
                    "Can't be deleted: task has time entries")
        _delete(s, "tasks", task_id)
        _record(s, "delete_task", task_id=task_id)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Time Entries — /v1/time_entries
# ---------------------------------------------------------------------------

@mcp.tool(name="list_time_entries")
def list_time_entries(user_id: int | None = None,
                      client_id: int | None = None,
                      project_id: int | None = None,
                      task_id: int | None = None,
                      is_billed: bool | str | None = None,
                      is_running: bool | str | None = None,
                      updated_since: str = "",
                      from_: str = "",
                      to: str = "",
                      page: int = 1,
                      per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/time_entries. Filters: user_id, client_id,
    project_id, task_id, is_billed, is_running, updated_since, from, to.
    NOTE: `from_` is the Python-safe name for the Harvest `from` query
    param (also accepted via `from`)."""
    with _lock():
        s = _load_state()
        items = _values(s, "time_entries")
        if user_id is not None:
            items = [e for e in items
                     if (e.get("user") or {}).get("id") == int(user_id)]
        if client_id is not None:
            items = [e for e in items
                     if (e.get("client") or {}).get("id") == int(client_id)]
        if project_id is not None:
            items = [e for e in items
                     if (e.get("project") or {}).get("id") == int(project_id)]
        if task_id is not None:
            items = [e for e in items
                     if (e.get("task") or {}).get("id") == int(task_id)]
        billed = _bool_to_iso(is_billed)
        if billed is not None:
            items = [e for e in items if bool(e.get("is_billed")) == billed]
        running = _bool_to_iso(is_running)
        if running is not None:
            items = [e for e in items if bool(e.get("is_running")) == running]
        items = _filter_updated_since(items, updated_since)
        items = _filter_date_range(items, from_, to, "spent_date")
        items.sort(key=lambda e: (e.get("spent_date") or "", e["id"]),
                   reverse=True)
        env = _paginate(items, page, per_page, "time_entries")
        _record(s, "list_time_entries",
                count=len(env["time_entries"]), user_id=user_id,
                project_id=project_id, task_id=task_id, page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_time_entry")
def get_time_entry(time_entry_id: int) -> dict:
    """Harvest REST: GET /v2/time_entries/{time_entry_id}."""
    with _lock():
        s = _load_state()
        e = _lookup(s, "time_entries", time_entry_id)
        _record(s, "get_time_entry", time_entry_id=time_entry_id,
                result="ok" if e else "not_found")
        _save_state(s)
        if not e:
            raise _err_not_found("TimeEntry")
        return dict(e)


@mcp.tool(name="create_time_entry")
def create_time_entry(project_id: int,
                      task_id: int,
                      spent_date: str,
                      user_id: int | None = None,
                      hours: float | None = None,
                      notes: str = "",
                      external_reference: dict | None = None,
                      started_time: str | None = None,
                      ended_time: str | None = None) -> dict:
    """Harvest REST: POST /v2/time_entries. Defaults `user_id` to the
    authenticated user. If `hours` is omitted the entry is created
    running (matches Harvest's duration-vs-timer logic). If
    `started_time` is given without `ended_time` the entry is also
    created running."""
    with _lock():
        s = _load_state()
        if not _lookup(s, "projects", project_id):
            _record(s, "create_time_entry", result="bad_project",
                    project_id=project_id)
            _save_state(s)
            raise _err_validation("TimeEntry",
                                  f"Project {project_id} does not exist")
        if not _lookup(s, "tasks", task_id):
            _record(s, "create_time_entry", result="bad_task",
                    task_id=task_id)
            _save_state(s)
            raise _err_validation("TimeEntry",
                                  f"Task {task_id} does not exist")
        uid = int(user_id) if user_id is not None else s["self"]["id"]
        # If no hours and started_time without ended_time, treat as running.
        is_running = (hours is None
                      and (started_time is None or ended_time is None))
        timer_started_at = _now_iso() if is_running else None
        e = _make_time_entry(s, user_id=uid, project_id=int(project_id),
                             task_id=int(task_id), spent_date=spent_date,
                             hours=hours, notes=notes,
                             is_running=is_running,
                             timer_started_at=timer_started_at,
                             started_time=started_time,
                             ended_time=ended_time)
        if external_reference:
            e["external_reference"] = external_reference
        _store(s, "time_entries", e)
        _record(s, "create_time_entry", time_entry_id=e["id"],
                project_id=project_id, task_id=task_id,
                user_id=uid, hours=hours, is_running=is_running)
        _save_state(s)
        return e


@mcp.tool(name="update_time_entry")
def update_time_entry(time_entry_id: int,
                      project_id: int | None = None,
                      task_id: int | None = None,
                      spent_date: str | None = None,
                      hours: float | None = None,
                      notes: str | None = None,
                      started_time: str | None = None,
                      ended_time: str | None = None) -> dict:
    """Harvest REST: PATCH /v2/time_entries/{time_entry_id}."""
    with _lock():
        s = _load_state()
        e = _lookup(s, "time_entries", time_entry_id)
        if not e:
            _record(s, "update_time_entry", time_entry_id=time_entry_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("TimeEntry")
        if project_id is not None:
            summary = _project_summary(s, int(project_id))
            if not summary:
                raise _err_validation("TimeEntry",
                                      f"Project {project_id} does not exist")
            e["project"] = summary
            proj = _lookup(s, "projects", project_id) or {}
            cid = (proj.get("client") or {}).get("id")
            e["client"] = _client_summary(s, cid)
        if task_id is not None:
            summary = _task_summary(s, int(task_id))
            if not summary:
                raise _err_validation("TimeEntry",
                                      f"Task {task_id} does not exist")
            e["task"] = summary
        if spent_date is not None:
            e["spent_date"] = spent_date
        if hours is not None:
            e["hours"] = float(hours)
            e["hours_without_timer"] = float(hours)
            e["rounded_hours"] = round(float(hours) * 4) / 4
        if notes is not None:
            e["notes"] = notes
        if started_time is not None:
            e["started_time"] = started_time
        if ended_time is not None:
            e["ended_time"] = ended_time
        e["updated_at"] = _now_iso()
        _record(s, "update_time_entry", time_entry_id=time_entry_id)
        _save_state(s)
        return e


@mcp.tool(name="delete_time_entry")
def delete_time_entry(time_entry_id: int) -> dict:
    """Harvest REST: DELETE /v2/time_entries/{time_entry_id}."""
    with _lock():
        s = _load_state()
        ok = _delete(s, "time_entries", time_entry_id)
        _record(s, "delete_time_entry", time_entry_id=time_entry_id,
                result="ok" if ok else "not_found")
        _save_state(s)
        if not ok:
            raise _err_not_found("TimeEntry")
        return {}


@mcp.tool(name="restart_time_entry")
def restart_time_entry(time_entry_id: int) -> dict:
    """Harvest REST: PATCH /v2/time_entries/{time_entry_id}/restart.
    Restarts a stopped timer. Returns 422 if the entry is already
    running."""
    with _lock():
        s = _load_state()
        e = _lookup(s, "time_entries", time_entry_id)
        if not e:
            _record(s, "restart_time_entry", time_entry_id=time_entry_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("TimeEntry")
        if e.get("is_running"):
            _record(s, "restart_time_entry", time_entry_id=time_entry_id,
                    result="already_running")
            _save_state(s)
            raise _err_validation("TimeEntry",
                                  "Time entry is already running")
        e["is_running"] = True
        e["timer_started_at"] = _now_iso()
        e["ended_time"] = None
        e["updated_at"] = _now_iso()
        _record(s, "restart_time_entry", time_entry_id=time_entry_id)
        _save_state(s)
        return e


@mcp.tool(name="stop_time_entry")
def stop_time_entry(time_entry_id: int) -> dict:
    """Harvest REST: PATCH /v2/time_entries/{time_entry_id}/stop.
    Stops a running timer; returns 422 if it's not running."""
    with _lock():
        s = _load_state()
        e = _lookup(s, "time_entries", time_entry_id)
        if not e:
            _record(s, "stop_time_entry", time_entry_id=time_entry_id,
                    result="not_found")
            _save_state(s)
            raise _err_not_found("TimeEntry")
        if not e.get("is_running"):
            _record(s, "stop_time_entry", time_entry_id=time_entry_id,
                    result="not_running")
            _save_state(s)
            raise _err_validation("TimeEntry",
                                  "Time entry is not running")
        # Accumulate elapsed time into hours (best-effort; mock uses
        # 1.0h placeholder if no started_at recorded).
        started = e.get("timer_started_at")
        if started:
            try:
                t0 = datetime.datetime.fromisoformat(
                    started.replace("Z", "+00:00"))
                t1 = datetime.datetime.now(datetime.timezone.utc)
                delta_h = max((t1 - t0).total_seconds() / 3600.0, 0.0)
            except ValueError:
                delta_h = 0.0
        else:
            delta_h = 0.0
        e["hours"] = round(float(e.get("hours") or 0.0) + delta_h, 4)
        e["hours_without_timer"] = e["hours"]
        e["rounded_hours"] = round(e["hours"] * 4) / 4
        e["is_running"] = False
        e["timer_started_at"] = None
        e["ended_time"] = _now_iso()
        e["updated_at"] = _now_iso()
        _record(s, "stop_time_entry", time_entry_id=time_entry_id,
                hours=e["hours"])
        _save_state(s)
        return e


# ---------------------------------------------------------------------------
# Users — /v1/users
# ---------------------------------------------------------------------------

@mcp.tool(name="list_users")
def list_users(is_active: bool | str | None = None,
               updated_since: str = "",
               page: int = 1,
               per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/users."""
    with _lock():
        s = _load_state()
        items = _values(s, "users")
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [u for u in items if bool(u.get("is_active")) == active]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda u: u["id"])
        env = _paginate(items, page, per_page, "users")
        _record(s, "list_users", count=len(env["users"]), page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_current_user")
def get_current_user() -> dict:
    """Harvest REST: GET /v2/users/me. Returns the authenticated user."""
    with _lock():
        s = _load_state()
        me_id = s["self"]["id"]
        u = _lookup(s, "users", me_id)
        _record(s, "get_current_user", user_id=me_id)
        _save_state(s)
        if u:
            return dict(u)
        return dict(s["self"])


@mcp.tool(name="get_user")
def get_user(user_id: int) -> dict:
    """Harvest REST: GET /v2/users/{user_id}."""
    with _lock():
        s = _load_state()
        u = _lookup(s, "users", user_id)
        _record(s, "get_user", user_id=user_id,
                result="ok" if u else "not_found")
        _save_state(s)
        if not u:
            raise _err_not_found("User")
        return dict(u)


# ---------------------------------------------------------------------------
# Invoices — /v1/invoices
# ---------------------------------------------------------------------------

@mcp.tool(name="list_invoices")
def list_invoices(client_id: int | None = None,
                  project_id: int | None = None,
                  state: str = "",
                  updated_since: str = "",
                  from_: str = "",
                  to: str = "",
                  page: int = 1,
                  per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/invoices. Filters: client_id, project_id,
    state (draft, open, paid, closed), updated_since, from, to (on
    issue_date)."""
    with _lock():
        s = _load_state()
        items = _values(s, "invoices")
        if client_id is not None:
            items = [i for i in items
                     if (i.get("client") or {}).get("id") == int(client_id)]
        if project_id is not None:
            items = [i for i in items
                     if any((li.get("project") or {}).get("id") == int(project_id)
                            for li in i.get("line_items", []))]
        if state:
            items = [i for i in items if i.get("state") == state]
        items = _filter_updated_since(items, updated_since)
        items = _filter_date_range(items, from_, to, "issue_date")
        items.sort(key=lambda i: i["id"], reverse=True)
        env = _paginate(items, page, per_page, "invoices")
        _record(s, "list_invoices", count=len(env["invoices"]),
                client_id=client_id, state=state, page=page)
        _save_state(s)
        return env


@mcp.tool(name="get_invoice")
def get_invoice(invoice_id: int) -> dict:
    """Harvest REST: GET /v2/invoices/{invoice_id}."""
    with _lock():
        s = _load_state()
        i = _lookup(s, "invoices", invoice_id)
        _record(s, "get_invoice", invoice_id=invoice_id,
                result="ok" if i else "not_found")
        _save_state(s)
        if not i:
            raise _err_not_found("Invoice")
        return dict(i)


@mcp.tool(name="create_invoice")
def create_invoice(client_id: int,
                   subject: str = "",
                   purchase_order: str = "",
                   notes: str = "",
                   currency: str | None = None,
                   issue_date: str | None = None,
                   due_date: str | None = None,
                   payment_term: str = "upon receipt",
                   line_items: list[dict] | None = None,
                   tax: float | None = None,
                   tax2: float | None = None,
                   discount: float | None = None) -> dict:
    """Harvest REST: POST /v2/invoices. `line_items` is a list of
    {kind, description, quantity, unit_price, taxed, taxed2,
    project_id}."""
    with _lock():
        s = _load_state()
        if not _lookup(s, "clients", client_id):
            _record(s, "create_invoice", result="bad_client",
                    client_id=client_id)
            _save_state(s)
            raise _err_validation("Invoice",
                                  f"Client {client_id} does not exist")
        inv = _make_invoice(s, client_id=int(client_id), subject=subject,
                            purchase_order=purchase_order, notes=notes,
                            currency=currency, issue_date=issue_date,
                            due_date=due_date, payment_term=payment_term,
                            line_items=line_items, tax=tax, tax2=tax2,
                            discount=discount)
        _store(s, "invoices", inv)
        _record(s, "create_invoice", invoice_id=inv["id"],
                client_id=client_id,
                line_items=len(inv["line_items"]))
        _save_state(s)
        return inv


# ---------------------------------------------------------------------------
# Expenses — /v1/expenses
# ---------------------------------------------------------------------------

@mcp.tool(name="list_expenses")
def list_expenses(user_id: int | None = None,
                  client_id: int | None = None,
                  project_id: int | None = None,
                  is_billed: bool | str | None = None,
                  updated_since: str = "",
                  from_: str = "",
                  to: str = "",
                  page: int = 1,
                  per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/expenses."""
    with _lock():
        s = _load_state()
        items = _values(s, "expenses")
        if user_id is not None:
            items = [x for x in items
                     if (x.get("user") or {}).get("id") == int(user_id)]
        if client_id is not None:
            items = [x for x in items
                     if (x.get("client") or {}).get("id") == int(client_id)]
        if project_id is not None:
            items = [x for x in items
                     if (x.get("project") or {}).get("id") == int(project_id)]
        billed = _bool_to_iso(is_billed)
        if billed is not None:
            items = [x for x in items if bool(x.get("is_billed")) == billed]
        items = _filter_updated_since(items, updated_since)
        items = _filter_date_range(items, from_, to, "spent_date")
        items.sort(key=lambda x: (x.get("spent_date") or "", x["id"]),
                   reverse=True)
        env = _paginate(items, page, per_page, "expenses")
        _record(s, "list_expenses", count=len(env["expenses"]),
                project_id=project_id, page=page)
        _save_state(s)
        return env


@mcp.tool(name="create_expense")
def create_expense(project_id: int,
                   spent_date: str,
                   user_id: int | None = None,
                   expense_category_id: int | None = None,
                   units: float | None = None,
                   total_cost: float | None = None,
                   notes: str = "",
                   billable: bool = True,
                   receipt: dict | None = None) -> dict:
    """Harvest REST: POST /v2/expenses."""
    with _lock():
        s = _load_state()
        if not _lookup(s, "projects", project_id):
            _record(s, "create_expense", result="bad_project",
                    project_id=project_id)
            _save_state(s)
            raise _err_validation("Expense",
                                  f"Project {project_id} does not exist")
        uid = int(user_id) if user_id is not None else s["self"]["id"]
        x = _make_expense(s, user_id=uid, project_id=int(project_id),
                          expense_category_id=expense_category_id,
                          spent_date=spent_date, units=units,
                          total_cost=total_cost, notes=notes,
                          billable=billable, receipt=receipt)
        _store(s, "expenses", x)
        _record(s, "create_expense", expense_id=x["id"],
                project_id=project_id, total_cost=total_cost)
        _save_state(s)
        return x


# ---------------------------------------------------------------------------
# Project / Task Assignments
# ---------------------------------------------------------------------------

@mcp.tool(name="list_project_user_assignments")
def list_project_user_assignments(project_id: int,
                                  user_id: int | None = None,
                                  is_active: bool | str | None = None,
                                  updated_since: str = "",
                                  page: int = 1,
                                  per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/projects/{project_id}/user_assignments.
    Lists user assignments for a project. Response key is
    `user_assignments`."""
    with _lock():
        s = _load_state()
        if not _lookup(s, "projects", project_id):
            _record(s, "list_project_user_assignments",
                    project_id=project_id, result="project_not_found")
            _save_state(s)
            raise _err_not_found("Project")
        items = [a for a in _values(s, "project_user_assignments")
                 if (a.get("project") or {}).get("id") == int(project_id)]
        if user_id is not None:
            items = [a for a in items
                     if (a.get("user") or {}).get("id") == int(user_id)]
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [a for a in items if bool(a.get("is_active")) == active]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda a: a["id"])
        env = _paginate(items, page, per_page, "user_assignments")
        _record(s, "list_project_user_assignments",
                project_id=project_id,
                count=len(env["user_assignments"]), page=page)
        _save_state(s)
        return env


@mcp.tool(name="list_task_assignments")
def list_task_assignments(project_id: int | None = None,
                          is_active: bool | str | None = None,
                          updated_since: str = "",
                          page: int = 1,
                          per_page: int = 100) -> dict:
    """Harvest REST: GET /v2/task_assignments (or, if `project_id` is
    given, GET /v2/projects/{project_id}/task_assignments). Response
    key is `task_assignments`."""
    with _lock():
        s = _load_state()
        items = _values(s, "task_assignments")
        if project_id is not None:
            if not _lookup(s, "projects", project_id):
                _record(s, "list_task_assignments",
                        project_id=project_id, result="project_not_found")
                _save_state(s)
                raise _err_not_found("Project")
            items = [a for a in items
                     if (a.get("project") or {}).get("id") == int(project_id)]
        active = _bool_to_iso(is_active)
        if active is not None:
            items = [a for a in items if bool(a.get("is_active")) == active]
        items = _filter_updated_since(items, updated_since)
        items.sort(key=lambda a: a["id"])
        env = _paginate(items, page, per_page, "task_assignments")
        _record(s, "list_task_assignments",
                count=len(env["task_assignments"]),
                project_id=project_id, page=page)
        _save_state(s)
        return env


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the full persisted state. Not part of the
    Harvest API surface; use for inspection/verification."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(account: dict | None = None,
                    self_user: dict | None = None,
                    users: list | None = None,
                    clients: list | None = None,
                    projects: list | None = None,
                    tasks: list | None = None,
                    time_entries: list | None = None,
                    invoices: list | None = None,
                    expenses: list | None = None,
                    project_user_assignments: list | None = None,
                    task_assignments: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: bulk-seed Harvest-shaped state. Each list contains
    dicts in the same shape the real API returns (numeric `id`,
    Harvest field names). Unspecified ids are minted, missing fields
    are filled with sensible defaults via the same `_make_*`
    constructors used at runtime.

    - `clients`: [{name, is_active?, address?, currency?}]
    - `projects`: [{client_id, name, code?, is_active?, is_billable?,
                    bill_by?, budget?, budget_by?, ...}]
    - `tasks`: [{name, billable_by_default?, default_hourly_rate?,
                 is_default?, is_active?}]
    - `users`: [{first_name, last_name, email, is_admin?, ...}]
    - `time_entries`: [{user_id, project_id, task_id, spent_date,
                        hours?, notes?, billable?, ...}]
    - `invoices`: [{client_id, subject?, line_items?, ...}]
    - `expenses`: [{project_id, spent_date, total_cost?, ...}]
    - `project_user_assignments`: [{project_id, user_id, is_active?,
                                    is_project_manager?, hourly_rate?,
                                    budget?}]
    - `task_assignments`: [{project_id, task_id, is_active?, billable?,
                            hourly_rate?, budget?}]

    If `replace` is true, the state is fully reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if account:
            s["account"].update(account)
        if self_user:
            s["self"].update(self_user)
        for u in users or []:
            uid = u.get("id")
            built = _make_user(s,
                               first_name=u.get("first_name", ""),
                               last_name=u.get("last_name", ""),
                               email=u.get("email", ""),
                               is_active=u.get("is_active", True),
                               is_admin=u.get("is_admin", False),
                               is_project_manager=u.get("is_project_manager",
                                                        False),
                               telephone=u.get("telephone", ""),
                               timezone=u.get("timezone",
                                              "Eastern Time (US & Canada)"),
                               weekly_capacity=u.get("weekly_capacity", 126000),
                               default_hourly_rate=u.get("default_hourly_rate"),
                               cost_rate=u.get("cost_rate"),
                               roles=u.get("roles") or [])
            if uid is not None:
                built["id"] = int(uid)
            _store(s, "users", built)
        for c in clients or []:
            cid = c.get("id")
            built = _make_client(s, name=c.get("name", ""),
                                 is_active=c.get("is_active", True),
                                 address=c.get("address", ""),
                                 currency=c.get("currency", ""))
            if cid is not None:
                built["id"] = int(cid)
            _store(s, "clients", built)
        for p in projects or []:
            pid = p.get("id")
            client_id = p.get("client_id") or (p.get("client") or {}).get("id")
            built = _make_project(s, client_id=int(client_id),
                                  name=p.get("name", ""),
                                  code=p.get("code", ""),
                                  is_active=p.get("is_active", True),
                                  is_billable=p.get("is_billable", True),
                                  is_fixed_fee=p.get("is_fixed_fee", False),
                                  bill_by=p.get("bill_by", "Project"),
                                  hourly_rate=p.get("hourly_rate"),
                                  budget=p.get("budget"),
                                  budget_by=p.get("budget_by", "project"),
                                  budget_is_monthly=p.get("budget_is_monthly",
                                                          False),
                                  notify_when_over_budget=
                                  p.get("notify_when_over_budget", False),
                                  over_budget_notification_percentage=
                                  p.get("over_budget_notification_percentage",
                                        80.0),
                                  show_budget_to_all=
                                  p.get("show_budget_to_all", False),
                                  cost_budget=p.get("cost_budget"),
                                  cost_budget_include_expenses=
                                  p.get("cost_budget_include_expenses", False),
                                  fee=p.get("fee"),
                                  notes=p.get("notes", ""),
                                  starts_on=p.get("starts_on"),
                                  ends_on=p.get("ends_on"))
            if pid is not None:
                built["id"] = int(pid)
            _store(s, "projects", built)
        for t in tasks or []:
            tid = t.get("id")
            built = _make_task(s, name=t.get("name", ""),
                               billable_by_default=
                               t.get("billable_by_default", True),
                               default_hourly_rate=
                               t.get("default_hourly_rate"),
                               is_default=t.get("is_default", False),
                               is_active=t.get("is_active", True))
            if tid is not None:
                built["id"] = int(tid)
            _store(s, "tasks", built)
        for te in time_entries or []:
            teid = te.get("id")
            built = _make_time_entry(s,
                                     user_id=int(te.get("user_id")
                                                 or s["self"]["id"]),
                                     project_id=int(te["project_id"]),
                                     task_id=int(te["task_id"]),
                                     spent_date=te.get("spent_date",
                                                       _today()),
                                     hours=te.get("hours"),
                                     notes=te.get("notes", ""),
                                     billable=te.get("billable"),
                                     budgeted=te.get("budgeted", False),
                                     billable_rate=te.get("billable_rate"),
                                     cost_rate=te.get("cost_rate"),
                                     is_running=te.get("is_running", False),
                                     timer_started_at=te.get(
                                         "timer_started_at"),
                                     started_time=te.get("started_time"),
                                     ended_time=te.get("ended_time"))
            if teid is not None:
                built["id"] = int(teid)
            for k in ("is_billed", "is_locked", "invoice"):
                if k in te:
                    built[k] = te[k]
            _store(s, "time_entries", built)
        for inv in invoices or []:
            iid = inv.get("id")
            built = _make_invoice(s,
                                  client_id=int(inv["client_id"]),
                                  subject=inv.get("subject", ""),
                                  purchase_order=inv.get("purchase_order",
                                                         ""),
                                  notes=inv.get("notes", ""),
                                  currency=inv.get("currency"),
                                  issue_date=inv.get("issue_date"),
                                  due_date=inv.get("due_date"),
                                  payment_term=inv.get("payment_term",
                                                       "upon receipt"),
                                  line_items=inv.get("line_items") or [],
                                  tax=inv.get("tax"),
                                  tax2=inv.get("tax2"),
                                  discount=inv.get("discount"))
            if iid is not None:
                built["id"] = int(iid)
            if "state" in inv:
                built["state"] = inv["state"]
            _store(s, "invoices", built)
        for x in expenses or []:
            xid = x.get("id")
            built = _make_expense(s,
                                  user_id=int(x.get("user_id")
                                              or s["self"]["id"]),
                                  project_id=int(x["project_id"]),
                                  expense_category_id=
                                  x.get("expense_category_id"),
                                  spent_date=x.get("spent_date", _today()),
                                  units=x.get("units"),
                                  total_cost=x.get("total_cost"),
                                  notes=x.get("notes", ""),
                                  billable=x.get("billable", True),
                                  receipt=x.get("receipt"))
            if xid is not None:
                built["id"] = int(xid)
            _store(s, "expenses", built)
        for a in project_user_assignments or []:
            aid = a.get("id") or _next_id(s, "project_user_assignment")
            entry = {
                "id": int(aid),
                "is_project_manager": a.get("is_project_manager", False),
                "is_active": a.get("is_active", True),
                "use_default_rates": a.get("use_default_rates", True),
                "budget": a.get("budget"),
                "hourly_rate": a.get("hourly_rate"),
                "project": _project_summary(s, int(a["project_id"])),
                "user": _user_summary(s, int(a["user_id"])),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            s.setdefault("project_user_assignments", {})[str(entry["id"])] = entry
        for a in task_assignments or []:
            aid = a.get("id") or _next_id(s, "task_assignment")
            entry = {
                "id": int(aid),
                "billable": a.get("billable", True),
                "is_active": a.get("is_active", True),
                "hourly_rate": a.get("hourly_rate"),
                "budget": a.get("budget"),
                "project": _project_summary(s, int(a["project_id"])),
                "task": _task_summary(s, int(a["task_id"])),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            s.setdefault("task_assignments", {})[str(entry["id"])] = entry
        _record(s, "debug_seed",
                counts={
                    "users": len(users or []),
                    "clients": len(clients or []),
                    "projects": len(projects or []),
                    "tasks": len(tasks or []),
                    "time_entries": len(time_entries or []),
                    "invoices": len(invoices or []),
                    "expenses": len(expenses or []),
                    "project_user_assignments":
                        len(project_user_assignments or []),
                    "task_assignments": len(task_assignments or []),
                },
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "client_ids": [c["id"] for c in s["clients"].values()],
            "project_ids": [p["id"] for p in s["projects"].values()],
            "task_ids": [t["id"] for t in s["tasks"].values()],
            "user_ids": [u["id"] for u in s["users"].values()],
        }


if __name__ == "__main__":
    mcp.run()
