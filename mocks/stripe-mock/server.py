"""Stripe mock MCP server.

Mirrors the Stripe REST API surface (docs.stripe.com/api) for the
resources most commonly exercised by agent tasks: Customers, Charges,
PaymentIntents, Refunds, Subscriptions, Products, Prices, Invoices,
Payouts, and Balance.

Tool names follow Stripe REST verb naming (e.g. `create_customer`,
`retrieve_payment_intent`, `confirm_payment_intent`,
`finalize_invoice`). Response shapes match Stripe JSON conventions:
each object carries `id`, `object`, `created` (epoch seconds),
`livemode`, and `metadata`; list responses are
`{"object": "list", "data": [...], "has_more": bool, "url": "/v1/..."}`;
errors are returned (not raised) as
`{"error": {"type": "invalid_request_error", "code": "resource_missing",
"message": "...", "param": "id"}}` so traces look like real failed
HTTP responses.

State lives at `$STRIPE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/stripe_mock`). Per-rollout isolation should clear the
state dir between rollouts. Optional `STRIPE_MOCK_SEED_PATH` preloads
state when no state.json exists.

Every call (including reads) appends to `state["calls"]` so verifiers
can replay the trace. File-locking via `fcntl.flock` makes concurrent
calls safe.

Amounts are integers in the smallest currency unit (cents for USD).
Stripe id prefixes used:
    cus_  Customers
    ch_   Charges
    pi_   PaymentIntents
    pm_   PaymentMethods
    re_   Refunds
    sub_  Subscriptions
    prod_ Products
    price_ Prices
    in_   Invoices
    il_   InvoiceItems / lines
    po_   Payouts
    txn_  BalanceTransactions
    evt_  Events
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import string
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "STRIPE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/stripe_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_epoch() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _empty_state() -> dict:
    return {
        "account": {
            "id": "acct_mock0000000001",
            "object": "account",
            "business_profile": {"name": "Mock Account"},
            "country": "US",
            "default_currency": "usd",
            "email": "mock@example.com",
            "type": "standard",
        },
        "balance": {
            "object": "balance",
            "available": [{"amount": 0, "currency": "usd",
                           "source_types": {"card": 0}}],
            "pending": [{"amount": 0, "currency": "usd",
                         "source_types": {"card": 0}}],
            "livemode": False,
        },
        "customers": {},
        "charges": {},
        "payment_intents": {},
        "refunds": {},
        "subscriptions": {},
        "products": {},
        "prices": {},
        "invoices": {},
        "payouts": {},
        "balance_transactions": {},
        "events": {},
        "next_seq": 1,
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("STRIPE_MOCK_SEED_PATH")
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
# ID generation
# ---------------------------------------------------------------------------

_ID_ALPHABET = string.ascii_letters + string.digits


def _rand_suffix(n: int = 24) -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(n))


def _new_id(prefix: str) -> str:
    # Stripe ids are <prefix>_<random>. Length varies by resource; 24
    # chars is a reasonable middle ground for mock ids.
    return f"{prefix}_{_rand_suffix(24)}"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _err(err_type: str, code: str, message: str,
         param: str | None = None,
         doc_url: str | None = None) -> dict:
    """Return a Stripe-shaped error envelope (matches real REST error
    body). Callers `return _err(...)` instead of raising, so failed
    calls appear as JSON in the trace."""
    body: dict[str, Any] = {
        "type": err_type,
        "code": code,
        "message": message,
    }
    if param:
        body["param"] = param
    if doc_url:
        body["doc_url"] = doc_url
    return {"error": body}


def _resource_missing(resource: str, rid: str, param: str = "id") -> dict:
    return _err(
        "invalid_request_error",
        "resource_missing",
        f"No such {resource}: '{rid}'",
        param=param,
    )


# ---------------------------------------------------------------------------
# List / pagination helpers
# ---------------------------------------------------------------------------

def _list_response(data: list[dict], has_more: bool, url: str) -> dict:
    return {
        "object": "list",
        "url": url,
        "has_more": bool(has_more),
        "data": data,
    }


def _paginate(items: list[dict], limit: int, starting_after: str | None,
              ending_before: str | None) -> tuple[list[dict], bool]:
    """Stripe-style cursor pagination: items are pre-sorted desc by
    `created` (newest first). `starting_after` returns items strictly
    after the given id in that ordering; `ending_before` returns items
    strictly before. `limit` is 1-100 (default 10)."""
    if limit is None:
        limit = 10
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    ids = [it.get("id") for it in items]
    start = 0
    end = len(items)
    if starting_after and starting_after in ids:
        start = ids.index(starting_after) + 1
    if ending_before and ending_before in ids:
        end = ids.index(ending_before)
    window = items[start:end]
    page = window[:limit]
    has_more = len(window) > limit
    return page, has_more


def _filter_by_created(items: list[dict], created: Any) -> list[dict]:
    """`created` may be an int (exact match) or a dict with gt/gte/lt/lte."""
    if created is None:
        return items
    if isinstance(created, int):
        return [it for it in items if it.get("created") == created]
    if isinstance(created, dict):
        out = []
        for it in items:
            c = it.get("created", 0)
            ok = True
            if "gt" in created and not c > int(created["gt"]):
                ok = False
            if "gte" in created and not c >= int(created["gte"]):
                ok = False
            if "lt" in created and not c < int(created["lt"]):
                ok = False
            if "lte" in created and not c <= int(created["lte"]):
                ok = False
            if ok:
                out.append(it)
        return out
    return items


def _sorted_desc(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (x.get("created", 0), x.get("id", "")),
                  reverse=True)


def _ensure_metadata(meta: Any) -> dict:
    if not isinstance(meta, dict):
        return {}
    # Stripe stores metadata as str->str. Coerce non-string scalars.
    out: dict[str, str] = {}
    for k, v in meta.items():
        if v is None:
            continue
        out[str(k)] = str(v)
    return out


def _apply_metadata_update(existing: dict, incoming: Any) -> dict:
    """Stripe: PATCH metadata merges keys; a key set to null/empty
    string deletes the key. For the mock we treat None as a delete and
    empty string as a delete (matches docs)."""
    if not isinstance(incoming, dict):
        return existing
    out = dict(existing or {})
    for k, v in incoming.items():
        if v is None or v == "":
            out.pop(str(k), None)
        else:
            out[str(k)] = str(v)
    return out


# ---------------------------------------------------------------------------
# Balance transaction helper
# ---------------------------------------------------------------------------

def _record_balance_transaction(state: dict, *, amount: int, currency: str,
                                source: str, type_: str,
                                fee: int = 0,
                                description: str = "",
                                status: str = "available") -> dict:
    txn_id = _new_id("txn")
    now = _now_epoch()
    txn = {
        "id": txn_id,
        "object": "balance_transaction",
        "amount": amount,
        "available_on": now,
        "created": now,
        "currency": currency,
        "description": description,
        "exchange_rate": None,
        "fee": fee,
        "fee_details": ([] if fee == 0 else
                        [{"amount": fee, "currency": currency,
                          "type": "stripe_fee",
                          "description": "Stripe processing fee"}]),
        "net": amount - fee,
        "reporting_category": type_,
        "source": source,
        "status": status,
        "type": type_,
    }
    state["balance_transactions"][txn_id] = txn
    # Update balance
    sign = 1 if amount >= 0 else -1
    for bucket in state["balance"]["available"]:
        if bucket["currency"] == currency:
            bucket["amount"] += amount - fee
            break
    else:
        state["balance"]["available"].append(
            {"amount": amount - fee, "currency": currency,
             "source_types": {"card": amount - fee}})
    _ = sign  # appease linters; sign is implicit in `amount`.
    return txn


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("stripe-mock")


# ===========================================================================
# Customers
# ===========================================================================

def _serialize_customer(c: dict) -> dict:
    return dict(c)


@mcp.tool(name="list_customers")
def list_customers(email: str | None = None,
                   created: Any = None,
                   limit: int = 10,
                   starting_after: str | None = None,
                   ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/customers — list customers. Filter by
    `email` (exact) or `created` (int or {gt,gte,lt,lte} dict).
    Cursor-paginate with `starting_after` / `ending_before` (max
    `limit` 100, default 10)."""
    with _lock():
        s = _load_state()
        items = list(s["customers"].values())
        if email:
            items = [c for c in items if c.get("email") == email]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_customers", count=len(page), email=email)
        _save_state(s)
        return _list_response(page, has_more, "/v1/customers")


@mcp.tool(name="retrieve_customer")
def retrieve_customer(customer_id: str) -> dict:
    """Stripe REST: GET /v1/customers/{id} — retrieve a customer by id."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(customer_id)
        _record(s, "retrieve_customer", customer_id=customer_id,
                result="ok" if c else "missing")
        _save_state(s)
        if not c:
            return _resource_missing("customer", customer_id)
        return _serialize_customer(c)


@mcp.tool(name="create_customer")
def create_customer(email: str | None = None,
                    name: str | None = None,
                    description: str | None = None,
                    phone: str | None = None,
                    address: dict | None = None,
                    metadata: dict | None = None,
                    balance: int = 0,
                    currency: str | None = None,
                    payment_method: str | None = None,
                    invoice_prefix: str | None = None) -> dict:
    """Stripe REST: POST /v1/customers — create a customer object.
    All parameters are optional; Stripe will create a customer with
    no payment details if none are supplied."""
    with _lock():
        s = _load_state()
        cid = _new_id("cus")
        c = {
            "id": cid,
            "object": "customer",
            "address": address,
            "balance": int(balance or 0),
            "created": _now_epoch(),
            "currency": currency,
            "default_source": None,
            "delinquent": False,
            "description": description,
            "discount": None,
            "email": email,
            "invoice_prefix": invoice_prefix or _rand_suffix(8).upper(),
            "invoice_settings": {
                "custom_fields": None,
                "default_payment_method": payment_method,
                "footer": None,
                "rendering_options": None,
            },
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "name": name,
            "next_invoice_sequence": 1,
            "phone": phone,
            "preferred_locales": [],
            "shipping": None,
            "tax_exempt": "none",
            "test_clock": None,
        }
        s["customers"][cid] = c
        _record(s, "create_customer", customer_id=cid, email=email)
        _save_state(s)
        return _serialize_customer(c)


@mcp.tool(name="update_customer")
def update_customer(customer_id: str,
                    email: str | None = None,
                    name: str | None = None,
                    description: str | None = None,
                    phone: str | None = None,
                    address: dict | None = None,
                    metadata: dict | None = None,
                    balance: int | None = None,
                    default_source: str | None = None) -> dict:
    """Stripe REST: POST /v1/customers/{id} — update a customer.
    Only supplied fields are changed; `metadata` is merged key-wise."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(customer_id)
        if not c:
            _record(s, "update_customer", customer_id=customer_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("customer", customer_id)
        if email is not None:
            c["email"] = email
        if name is not None:
            c["name"] = name
        if description is not None:
            c["description"] = description
        if phone is not None:
            c["phone"] = phone
        if address is not None:
            c["address"] = address
        if balance is not None:
            c["balance"] = int(balance)
        if default_source is not None:
            c["default_source"] = default_source
        if metadata is not None:
            c["metadata"] = _apply_metadata_update(c.get("metadata", {}),
                                                  metadata)
        _record(s, "update_customer", customer_id=customer_id)
        _save_state(s)
        return _serialize_customer(c)


@mcp.tool(name="delete_customer")
def delete_customer(customer_id: str) -> dict:
    """Stripe REST: DELETE /v1/customers/{id} — permanently delete a
    customer. Returns `{id, object, deleted: true}` on success."""
    with _lock():
        s = _load_state()
        c = s["customers"].pop(customer_id, None)
        if not c:
            _record(s, "delete_customer", customer_id=customer_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("customer", customer_id)
        _record(s, "delete_customer", customer_id=customer_id)
        _save_state(s)
        return {"id": customer_id, "object": "customer", "deleted": True}


@mcp.tool(name="search_customers")
def search_customers(query: str, limit: int = 10,
                     page: str | None = None) -> dict:
    """Stripe REST: GET /v1/customers/search — search customers via a
    Stripe query string. The mock supports the common
    `field:"value"` clauses joined with AND, on fields:
    email, name, phone, description, and `metadata['key']`."""
    with _lock():
        s = _load_state()
        items = list(s["customers"].values())
        filtered = _search_filter(items, query, ["email", "name", "phone",
                                                 "description"])
        filtered = _sorted_desc(filtered)
        try:
            limit = max(1, min(int(limit or 10), 100))
        except (TypeError, ValueError):
            limit = 10
        start = 0
        if page:
            try:
                start = int(page)
            except (TypeError, ValueError):
                start = 0
        window = filtered[start: start + limit]
        next_page = (str(start + limit)
                     if start + limit < len(filtered) else None)
        _record(s, "search_customers", query=query, count=len(window))
        _save_state(s)
        return {
            "object": "search_result",
            "url": "/v1/customers/search",
            "has_more": next_page is not None,
            "next_page": next_page,
            "data": window,
        }


def _search_filter(items: list[dict], query: str,
                   fields: list[str]) -> list[dict]:
    """Tiny Stripe search-query parser. Supports
    `field:"value"` and `metadata['k']:"v"` joined by AND/whitespace."""
    if not query:
        return items
    parts = []
    # Split on AND (case-insensitive) and whitespace
    raw = query.replace(" AND ", " ").replace(" and ", " ")
    # Tokenize: capture field:"value" or field:'value' or metadata['k']:"v"
    import re
    clause_re = re.compile(
        r"""(metadata\[['"]?(?P<mk>[^'"\]]+)['"]?\]
             | (?P<field>[a-zA-Z_]+)
            )
            \s*:\s*
            (?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>\S+))""",
        re.VERBOSE,
    )
    for m in clause_re.finditer(raw):
        val = m.group("dq")
        if val is None:
            val = m.group("sq")
        if val is None:
            val = m.group("bare")
        if m.group("mk"):
            parts.append(("metadata", m.group("mk"), val))
        else:
            parts.append(("field", m.group("field"), val))
    out = []
    for it in items:
        ok = True
        for kind, key, val in parts:
            if kind == "metadata":
                mv = (it.get("metadata") or {}).get(key)
                if mv != val:
                    ok = False
                    break
            else:
                if key not in fields:
                    ok = False
                    break
                if (it.get(key) or "") != val:
                    ok = False
                    break
        if ok:
            out.append(it)
    return out


# ===========================================================================
# Charges
# ===========================================================================

def _serialize_charge(ch: dict) -> dict:
    return dict(ch)


@mcp.tool(name="list_charges")
def list_charges(customer: str | None = None,
                 payment_intent: str | None = None,
                 created: Any = None,
                 limit: int = 10,
                 starting_after: str | None = None,
                 ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/charges — list charges, most recent first.
    Filter by `customer`, `payment_intent`, or `created`."""
    with _lock():
        s = _load_state()
        items = list(s["charges"].values())
        if customer:
            items = [c for c in items if c.get("customer") == customer]
        if payment_intent:
            items = [c for c in items if c.get("payment_intent")
                     == payment_intent]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_charges", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/charges")


@mcp.tool(name="retrieve_charge")
def retrieve_charge(charge_id: str) -> dict:
    """Stripe REST: GET /v1/charges/{id} — retrieve a charge."""
    with _lock():
        s = _load_state()
        ch = s["charges"].get(charge_id)
        _record(s, "retrieve_charge", charge_id=charge_id,
                result="ok" if ch else "missing")
        _save_state(s)
        if not ch:
            return _resource_missing("charge", charge_id)
        return _serialize_charge(ch)


@mcp.tool(name="create_charge")
def create_charge(amount: int,
                  currency: str = "usd",
                  customer: str | None = None,
                  source: str | None = None,
                  description: str | None = None,
                  capture: bool = True,
                  metadata: dict | None = None,
                  receipt_email: str | None = None,
                  statement_descriptor: str | None = None) -> dict:
    """Stripe REST: POST /v1/charges — create a charge against a
    customer or payment source. `amount` is in the smallest currency
    unit (cents). When `capture=false` the charge is authorized but
    not captured (status=`succeeded`, captured=false in Stripe's
    legacy charge object — the mock represents this as
    captured=false and a positive `amount_capturable`)."""
    with _lock():
        s = _load_state()
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return _err("invalid_request_error", "parameter_invalid_integer",
                        "amount must be an integer", param="amount")
        if amount <= 0:
            return _err("invalid_request_error",
                        "parameter_invalid_integer",
                        "amount must be positive", param="amount")
        if customer and customer not in s["customers"]:
            return _resource_missing("customer", customer, param="customer")
        currency = (currency or "usd").lower()
        cid = _new_id("ch")
        now = _now_epoch()
        captured = bool(capture)
        ch = {
            "id": cid,
            "object": "charge",
            "amount": amount,
            "amount_captured": amount if captured else 0,
            "amount_refunded": 0,
            "application": None,
            "application_fee": None,
            "application_fee_amount": None,
            "balance_transaction": None,
            "billing_details": {
                "address": None, "email": receipt_email,
                "name": None, "phone": None,
            },
            "calculated_statement_descriptor": statement_descriptor,
            "captured": captured,
            "created": now,
            "currency": currency,
            "customer": customer,
            "description": description,
            "disputed": False,
            "failure_code": None,
            "failure_message": None,
            "fraud_details": {},
            "invoice": None,
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "outcome": {
                "network_status": "approved_by_network",
                "reason": None,
                "risk_level": "normal",
                "risk_score": 5,
                "seller_message": "Payment complete.",
                "type": "authorized",
            },
            "paid": captured,
            "payment_intent": None,
            "payment_method": source,
            "payment_method_details": {
                "card": {"brand": "visa", "exp_month": 12,
                         "exp_year": 2030, "last4": "4242",
                         "funding": "credit", "country": "US"},
                "type": "card",
            },
            "receipt_email": receipt_email,
            "receipt_number": None,
            "receipt_url": f"https://pay.stripe.com/receipts/{cid}",
            "refunded": False,
            "refunds": _list_response([], False,
                                      f"/v1/charges/{cid}/refunds"),
            "review": None,
            "shipping": None,
            "source": source,
            "source_transfer": None,
            "statement_descriptor": statement_descriptor,
            "statement_descriptor_suffix": None,
            "status": "succeeded" if captured else "succeeded",
            "amount_capturable": 0 if captured else amount,
            "transfer_data": None,
            "transfer_group": None,
        }
        s["charges"][cid] = ch
        if captured:
            fee = max(30, int(amount * 0.029))  # Stripe ~2.9% + 30¢ fee
            txn = _record_balance_transaction(
                s, amount=amount, currency=currency, source=cid,
                type_="charge", fee=fee,
                description=description or "")
            ch["balance_transaction"] = txn["id"]
        _record(s, "create_charge", charge_id=cid, amount=amount,
                currency=currency, captured=captured)
        _save_state(s)
        return _serialize_charge(ch)


@mcp.tool(name="capture_charge")
def capture_charge(charge_id: str,
                   amount: int | None = None,
                   receipt_email: str | None = None,
                   statement_descriptor: str | None = None) -> dict:
    """Stripe REST: POST /v1/charges/{id}/capture — capture a
    previously authorized but uncaptured charge. `amount` may be
    less than the original amount for partial capture."""
    with _lock():
        s = _load_state()
        ch = s["charges"].get(charge_id)
        if not ch:
            _record(s, "capture_charge", charge_id=charge_id, result="missing")
            _save_state(s)
            return _resource_missing("charge", charge_id)
        if ch.get("captured"):
            return _err("invalid_request_error", "charge_already_captured",
                        f"Charge {charge_id} has already been captured.")
        capture_amount = int(amount) if amount is not None else ch["amount"]
        if capture_amount > ch["amount"]:
            return _err("invalid_request_error",
                        "amount_too_large",
                        "Capture amount cannot exceed authorized amount",
                        param="amount")
        ch["captured"] = True
        ch["amount_captured"] = capture_amount
        ch["amount_capturable"] = 0
        ch["paid"] = True
        ch["status"] = "succeeded"
        if receipt_email is not None:
            ch["receipt_email"] = receipt_email
        if statement_descriptor is not None:
            ch["statement_descriptor"] = statement_descriptor
        fee = max(30, int(capture_amount * 0.029))
        txn = _record_balance_transaction(
            s, amount=capture_amount, currency=ch["currency"],
            source=ch["id"], type_="charge", fee=fee,
            description=ch.get("description") or "")
        ch["balance_transaction"] = txn["id"]
        _record(s, "capture_charge", charge_id=charge_id,
                amount=capture_amount)
        _save_state(s)
        return _serialize_charge(ch)


@mcp.tool(name="update_charge")
def update_charge(charge_id: str,
                  description: str | None = None,
                  metadata: dict | None = None,
                  receipt_email: str | None = None,
                  fraud_details: dict | None = None,
                  shipping: dict | None = None) -> dict:
    """Stripe REST: POST /v1/charges/{id} — update mutable fields on
    a charge."""
    with _lock():
        s = _load_state()
        ch = s["charges"].get(charge_id)
        if not ch:
            _record(s, "update_charge", charge_id=charge_id, result="missing")
            _save_state(s)
            return _resource_missing("charge", charge_id)
        if description is not None:
            ch["description"] = description
        if receipt_email is not None:
            ch["receipt_email"] = receipt_email
        if fraud_details is not None:
            ch["fraud_details"] = fraud_details
        if shipping is not None:
            ch["shipping"] = shipping
        if metadata is not None:
            ch["metadata"] = _apply_metadata_update(ch.get("metadata", {}),
                                                   metadata)
        _record(s, "update_charge", charge_id=charge_id)
        _save_state(s)
        return _serialize_charge(ch)


# ===========================================================================
# PaymentIntents
# ===========================================================================

def _serialize_pi(pi: dict) -> dict:
    return dict(pi)


@mcp.tool(name="list_payment_intents")
def list_payment_intents(customer: str | None = None,
                         created: Any = None,
                         limit: int = 10,
                         starting_after: str | None = None,
                         ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/payment_intents — list PaymentIntents,
    optionally filtered by `customer` or `created`."""
    with _lock():
        s = _load_state()
        items = list(s["payment_intents"].values())
        if customer:
            items = [p for p in items if p.get("customer") == customer]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_payment_intents", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/payment_intents")


@mcp.tool(name="retrieve_payment_intent")
def retrieve_payment_intent(payment_intent_id: str,
                            client_secret: str | None = None) -> dict:
    """Stripe REST: GET /v1/payment_intents/{id} — retrieve a
    PaymentIntent."""
    with _lock():
        s = _load_state()
        pi = s["payment_intents"].get(payment_intent_id)
        _record(s, "retrieve_payment_intent",
                payment_intent_id=payment_intent_id,
                result="ok" if pi else "missing")
        _save_state(s)
        if not pi:
            return _resource_missing("payment_intent", payment_intent_id)
        return _serialize_pi(pi)


@mcp.tool(name="create_payment_intent")
def create_payment_intent(amount: int,
                          currency: str = "usd",
                          customer: str | None = None,
                          payment_method: str | None = None,
                          payment_method_types: list | None = None,
                          confirm: bool = False,
                          capture_method: str = "automatic",
                          description: str | None = None,
                          metadata: dict | None = None,
                          receipt_email: str | None = None,
                          setup_future_usage: str | None = None,
                          statement_descriptor: str | None = None,
                          automatic_payment_methods: dict | None = None) -> dict:
    """Stripe REST: POST /v1/payment_intents — create a PaymentIntent.
    Set `confirm=true` to attempt confirmation in the same call.
    `capture_method` in {automatic, automatic_async, manual}."""
    with _lock():
        s = _load_state()
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return _err("invalid_request_error", "parameter_invalid_integer",
                        "amount must be an integer", param="amount")
        if amount <= 0:
            return _err("invalid_request_error",
                        "parameter_invalid_integer",
                        "amount must be positive", param="amount")
        if customer and customer not in s["customers"]:
            return _resource_missing("customer", customer, param="customer")
        if capture_method not in ("automatic", "automatic_async", "manual"):
            return _err("invalid_request_error",
                        "parameter_invalid_string_blank",
                        "invalid capture_method", param="capture_method")
        currency = (currency or "usd").lower()
        pid = _new_id("pi")
        now = _now_epoch()
        if not payment_method_types:
            payment_method_types = ["card"]
        status = "requires_payment_method"
        if payment_method:
            status = "requires_confirmation"
        pi = {
            "id": pid,
            "object": "payment_intent",
            "amount": amount,
            "amount_capturable": 0,
            "amount_received": 0,
            "application": None,
            "application_fee_amount": None,
            "automatic_payment_methods": automatic_payment_methods,
            "canceled_at": None,
            "cancellation_reason": None,
            "capture_method": capture_method,
            "client_secret": f"{pid}_secret_{_rand_suffix(16)}",
            "confirmation_method": "automatic",
            "created": now,
            "currency": currency,
            "customer": customer,
            "description": description,
            "invoice": None,
            "last_payment_error": None,
            "latest_charge": None,
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "next_action": None,
            "on_behalf_of": None,
            "payment_method": payment_method,
            "payment_method_options": {},
            "payment_method_types": payment_method_types,
            "processing": None,
            "receipt_email": receipt_email,
            "review": None,
            "setup_future_usage": setup_future_usage,
            "shipping": None,
            "statement_descriptor": statement_descriptor,
            "statement_descriptor_suffix": None,
            "status": status,
            "transfer_data": None,
            "transfer_group": None,
        }
        s["payment_intents"][pid] = pi
        _record(s, "create_payment_intent", payment_intent_id=pid,
                amount=amount, currency=currency, confirm=confirm)
        if confirm and payment_method:
            _confirm_pi(s, pi)
        _save_state(s)
        return _serialize_pi(pi)


def _confirm_pi(state: dict, pi: dict,
                payment_method: str | None = None) -> dict:
    """Confirm a PaymentIntent in-place. Returns the (possibly
    error) result dict; on success the pi is mutated and a charge is
    created when capture_method is automatic."""
    if payment_method:
        pi["payment_method"] = payment_method
    if not pi.get("payment_method"):
        return _err("invalid_request_error",
                    "payment_intent_unexpected_state",
                    "PaymentIntent has no payment method attached.",
                    param="payment_method")
    if pi["status"] not in ("requires_payment_method",
                            "requires_confirmation",
                            "requires_action",
                            "processing"):
        return _err("invalid_request_error",
                    "payment_intent_unexpected_state",
                    f"PaymentIntent in status '{pi['status']}' cannot "
                    f"be confirmed.")
    if pi["capture_method"] == "manual":
        pi["status"] = "requires_capture"
        pi["amount_capturable"] = pi["amount"]
    else:
        pi["status"] = "succeeded"
        pi["amount_received"] = pi["amount"]
        ch_id = _new_id("ch")
        now = _now_epoch()
        fee = max(30, int(pi["amount"] * 0.029))
        ch = {
            "id": ch_id,
            "object": "charge",
            "amount": pi["amount"],
            "amount_captured": pi["amount"],
            "amount_refunded": 0,
            "captured": True,
            "created": now,
            "currency": pi["currency"],
            "customer": pi.get("customer"),
            "description": pi.get("description"),
            "disputed": False,
            "invoice": pi.get("invoice"),
            "livemode": False,
            "metadata": dict(pi.get("metadata", {})),
            "paid": True,
            "payment_intent": pi["id"],
            "payment_method": pi.get("payment_method"),
            "payment_method_details": {
                "card": {"brand": "visa", "exp_month": 12,
                         "exp_year": 2030, "last4": "4242",
                         "funding": "credit", "country": "US"},
                "type": "card",
            },
            "receipt_email": pi.get("receipt_email"),
            "receipt_url": f"https://pay.stripe.com/receipts/{ch_id}",
            "refunded": False,
            "refunds": _list_response([], False,
                                      f"/v1/charges/{ch_id}/refunds"),
            "status": "succeeded",
        }
        txn = _record_balance_transaction(
            state, amount=pi["amount"], currency=pi["currency"],
            source=ch_id, type_="charge", fee=fee,
            description=pi.get("description") or "")
        ch["balance_transaction"] = txn["id"]
        state["charges"][ch_id] = ch
        pi["latest_charge"] = ch_id
    return pi


@mcp.tool(name="update_payment_intent")
def update_payment_intent(payment_intent_id: str,
                          amount: int | None = None,
                          currency: str | None = None,
                          customer: str | None = None,
                          description: str | None = None,
                          metadata: dict | None = None,
                          payment_method: str | None = None,
                          receipt_email: str | None = None,
                          setup_future_usage: str | None = None) -> dict:
    """Stripe REST: POST /v1/payment_intents/{id} — update mutable
    fields on a PaymentIntent before it is confirmed."""
    with _lock():
        s = _load_state()
        pi = s["payment_intents"].get(payment_intent_id)
        if not pi:
            _record(s, "update_payment_intent",
                    payment_intent_id=payment_intent_id, result="missing")
            _save_state(s)
            return _resource_missing("payment_intent", payment_intent_id)
        if pi["status"] in ("succeeded", "canceled"):
            return _err("invalid_request_error",
                        "payment_intent_unexpected_state",
                        f"PaymentIntent in status '{pi['status']}' "
                        f"cannot be updated.")
        if amount is not None:
            pi["amount"] = int(amount)
        if currency is not None:
            pi["currency"] = currency.lower()
        if customer is not None:
            pi["customer"] = customer
        if description is not None:
            pi["description"] = description
        if payment_method is not None:
            pi["payment_method"] = payment_method
            if pi["status"] == "requires_payment_method":
                pi["status"] = "requires_confirmation"
        if receipt_email is not None:
            pi["receipt_email"] = receipt_email
        if setup_future_usage is not None:
            pi["setup_future_usage"] = setup_future_usage
        if metadata is not None:
            pi["metadata"] = _apply_metadata_update(pi.get("metadata", {}),
                                                   metadata)
        _record(s, "update_payment_intent",
                payment_intent_id=payment_intent_id)
        _save_state(s)
        return _serialize_pi(pi)


@mcp.tool(name="confirm_payment_intent")
def confirm_payment_intent(payment_intent_id: str,
                           payment_method: str | None = None,
                           receipt_email: str | None = None,
                           return_url: str | None = None) -> dict:
    """Stripe REST: POST /v1/payment_intents/{id}/confirm — confirm
    a PaymentIntent, attempting to collect payment. If
    `capture_method=manual` the resulting status is
    `requires_capture`; otherwise `succeeded` (and a charge is
    created)."""
    with _lock():
        s = _load_state()
        pi = s["payment_intents"].get(payment_intent_id)
        if not pi:
            _record(s, "confirm_payment_intent",
                    payment_intent_id=payment_intent_id, result="missing")
            _save_state(s)
            return _resource_missing("payment_intent", payment_intent_id)
        if receipt_email is not None:
            pi["receipt_email"] = receipt_email
        result = _confirm_pi(s, pi, payment_method=payment_method)
        _record(s, "confirm_payment_intent",
                payment_intent_id=payment_intent_id,
                status=pi.get("status"))
        _save_state(s)
        if isinstance(result, dict) and "error" in result:
            return result
        return _serialize_pi(pi)


@mcp.tool(name="cancel_payment_intent")
def cancel_payment_intent(payment_intent_id: str,
                          cancellation_reason: str | None = None) -> dict:
    """Stripe REST: POST /v1/payment_intents/{id}/cancel — cancel a
    PaymentIntent. `cancellation_reason` in
    {duplicate, fraudulent, requested_by_customer, abandoned}."""
    with _lock():
        s = _load_state()
        pi = s["payment_intents"].get(payment_intent_id)
        if not pi:
            _record(s, "cancel_payment_intent",
                    payment_intent_id=payment_intent_id, result="missing")
            _save_state(s)
            return _resource_missing("payment_intent", payment_intent_id)
        if pi["status"] in ("succeeded", "canceled"):
            return _err("invalid_request_error",
                        "payment_intent_unexpected_state",
                        f"PaymentIntent in status '{pi['status']}' "
                        f"cannot be canceled.")
        pi["status"] = "canceled"
        pi["canceled_at"] = _now_epoch()
        pi["cancellation_reason"] = cancellation_reason
        _record(s, "cancel_payment_intent",
                payment_intent_id=payment_intent_id,
                reason=cancellation_reason)
        _save_state(s)
        return _serialize_pi(pi)


# ===========================================================================
# Refunds
# ===========================================================================

@mcp.tool(name="list_refunds")
def list_refunds(charge: str | None = None,
                 payment_intent: str | None = None,
                 created: Any = None,
                 limit: int = 10,
                 starting_after: str | None = None,
                 ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/refunds — list refunds."""
    with _lock():
        s = _load_state()
        items = list(s["refunds"].values())
        if charge:
            items = [r for r in items if r.get("charge") == charge]
        if payment_intent:
            items = [r for r in items if r.get("payment_intent")
                     == payment_intent]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_refunds", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/refunds")


@mcp.tool(name="create_refund")
def create_refund(charge: str | None = None,
                  payment_intent: str | None = None,
                  amount: int | None = None,
                  reason: str | None = None,
                  metadata: dict | None = None,
                  refund_application_fee: bool = False,
                  reverse_transfer: bool = False) -> dict:
    """Stripe REST: POST /v1/refunds — refund a charge or
    PaymentIntent. Supply either `charge` or `payment_intent` (not
    both). `amount` defaults to the remaining refundable amount.
    `reason` in {duplicate, fraudulent, requested_by_customer}."""
    with _lock():
        s = _load_state()
        if not charge and not payment_intent:
            return _err("invalid_request_error", "parameter_missing",
                        "Either charge or payment_intent is required.",
                        param="charge")
        if payment_intent and not charge:
            pi = s["payment_intents"].get(payment_intent)
            if not pi:
                return _resource_missing("payment_intent", payment_intent,
                                         param="payment_intent")
            charge = pi.get("latest_charge")
            if not charge:
                return _err("invalid_request_error",
                            "payment_intent_unexpected_state",
                            "PaymentIntent has no successful charge to "
                            "refund.")
        ch = s["charges"].get(charge)
        if not ch:
            return _resource_missing("charge", charge, param="charge")
        refundable = ch["amount"] - ch.get("amount_refunded", 0)
        if amount is None:
            amount = refundable
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return _err("invalid_request_error", "parameter_invalid_integer",
                        "amount must be an integer", param="amount")
        if amount <= 0 or amount > refundable:
            return _err("invalid_request_error", "amount_too_large",
                        "Refund amount exceeds remaining charge amount",
                        param="amount")
        if reason and reason not in ("duplicate", "fraudulent",
                                     "requested_by_customer"):
            return _err("invalid_request_error", "parameter_invalid_string",
                        "invalid reason", param="reason")
        rid = _new_id("re")
        now = _now_epoch()
        r = {
            "id": rid,
            "object": "refund",
            "amount": amount,
            "balance_transaction": None,
            "charge": ch["id"],
            "created": now,
            "currency": ch["currency"],
            "destination_details": {"card": {"reference_status": "pending",
                                             "reference_type": "acquirer_reference_number",
                                             "type": "refund"},
                                    "type": "card"},
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "payment_intent": ch.get("payment_intent"),
            "reason": reason,
            "receipt_number": None,
            "source_transfer_reversal": None,
            "status": "succeeded",
            "transfer_reversal": None,
        }
        s["refunds"][rid] = r
        ch["amount_refunded"] = ch.get("amount_refunded", 0) + amount
        ch["refunded"] = ch["amount_refunded"] >= ch["amount"]
        ch.setdefault("refunds", _list_response(
            [], False, f"/v1/charges/{ch['id']}/refunds"))
        ch["refunds"]["data"].insert(0, r)
        txn = _record_balance_transaction(
            s, amount=-amount, currency=ch["currency"], source=rid,
            type_="refund", description=f"REFUND FOR {ch['id']}")
        r["balance_transaction"] = txn["id"]
        _record(s, "create_refund", refund_id=rid, charge=ch["id"],
                amount=amount, reason=reason)
        _save_state(s)
        return dict(r)


@mcp.tool(name="retrieve_refund")
def retrieve_refund(refund_id: str) -> dict:
    """Stripe REST: GET /v1/refunds/{id} — retrieve a refund."""
    with _lock():
        s = _load_state()
        r = s["refunds"].get(refund_id)
        _record(s, "retrieve_refund", refund_id=refund_id,
                result="ok" if r else "missing")
        _save_state(s)
        if not r:
            return _resource_missing("refund", refund_id)
        return dict(r)


# ===========================================================================
# Products
# ===========================================================================

@mcp.tool(name="list_products")
def list_products(active: bool | None = None,
                  ids: list | None = None,
                  created: Any = None,
                  limit: int = 10,
                  starting_after: str | None = None,
                  ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/products — list products."""
    with _lock():
        s = _load_state()
        items = list(s["products"].values())
        if active is not None:
            items = [p for p in items if bool(p.get("active")) == bool(active)]
        if ids:
            ids_set = set(ids)
            items = [p for p in items if p.get("id") in ids_set]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_products", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/products")


@mcp.tool(name="retrieve_product")
def retrieve_product(product_id: str) -> dict:
    """Stripe REST: GET /v1/products/{id} — retrieve a product."""
    with _lock():
        s = _load_state()
        p = s["products"].get(product_id)
        _record(s, "retrieve_product", product_id=product_id,
                result="ok" if p else "missing")
        _save_state(s)
        if not p:
            return _resource_missing("product", product_id)
        return dict(p)


@mcp.tool(name="create_product")
def create_product(name: str,
                   id: str | None = None,
                   description: str | None = None,
                   active: bool = True,
                   metadata: dict | None = None,
                   images: list | None = None,
                   shippable: bool | None = None,
                   url: str | None = None,
                   tax_code: str | None = None,
                   unit_label: str | None = None,
                   default_price_data: dict | None = None) -> dict:
    """Stripe REST: POST /v1/products — create a product."""
    with _lock():
        s = _load_state()
        if not name:
            return _err("invalid_request_error", "parameter_missing",
                        "name is required", param="name")
        pid = id or _new_id("prod")
        if pid in s["products"]:
            return _err("invalid_request_error", "resource_already_exists",
                        f"Product already exists: '{pid}'", param="id")
        now = _now_epoch()
        p = {
            "id": pid,
            "object": "product",
            "active": bool(active),
            "attributes": [],
            "created": now,
            "default_price": None,
            "description": description,
            "features": [],
            "images": list(images or []),
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "name": name,
            "package_dimensions": None,
            "shippable": shippable,
            "statement_descriptor": None,
            "tax_code": tax_code,
            "type": "service",
            "unit_label": unit_label,
            "updated": now,
            "url": url,
        }
        s["products"][pid] = p
        _record(s, "create_product", product_id=pid)
        if default_price_data:
            price = _create_price_obj(s, product=pid,
                                      **{k: v for k, v in
                                         default_price_data.items()
                                         if k in ("currency", "unit_amount",
                                                  "recurring", "metadata",
                                                  "active", "nickname",
                                                  "tax_behavior")})
            p["default_price"] = price["id"]
        _save_state(s)
        return dict(p)


@mcp.tool(name="update_product")
def update_product(product_id: str,
                   name: str | None = None,
                   description: str | None = None,
                   active: bool | None = None,
                   metadata: dict | None = None,
                   default_price: str | None = None,
                   images: list | None = None,
                   url: str | None = None) -> dict:
    """Stripe REST: POST /v1/products/{id} — update a product."""
    with _lock():
        s = _load_state()
        p = s["products"].get(product_id)
        if not p:
            _record(s, "update_product", product_id=product_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("product", product_id)
        if name is not None:
            p["name"] = name
        if description is not None:
            p["description"] = description
        if active is not None:
            p["active"] = bool(active)
        if default_price is not None:
            p["default_price"] = default_price
        if images is not None:
            p["images"] = list(images)
        if url is not None:
            p["url"] = url
        if metadata is not None:
            p["metadata"] = _apply_metadata_update(p.get("metadata", {}),
                                                  metadata)
        p["updated"] = _now_epoch()
        _record(s, "update_product", product_id=product_id)
        _save_state(s)
        return dict(p)


# ===========================================================================
# Prices
# ===========================================================================

def _create_price_obj(state: dict, *, product: str,
                      currency: str = "usd",
                      unit_amount: int | None = None,
                      recurring: dict | None = None,
                      metadata: dict | None = None,
                      active: bool = True,
                      nickname: str | None = None,
                      tax_behavior: str | None = None,
                      lookup_key: str | None = None) -> dict:
    pid = _new_id("price")
    now = _now_epoch()
    p = {
        "id": pid,
        "object": "price",
        "active": bool(active),
        "billing_scheme": "per_unit",
        "created": now,
        "currency": (currency or "usd").lower(),
        "custom_unit_amount": None,
        "livemode": False,
        "lookup_key": lookup_key,
        "metadata": _ensure_metadata(metadata),
        "nickname": nickname,
        "product": product,
        "recurring": recurring,
        "tax_behavior": tax_behavior or "unspecified",
        "tiers_mode": None,
        "transform_quantity": None,
        "type": "recurring" if recurring else "one_time",
        "unit_amount": int(unit_amount) if unit_amount is not None else None,
        "unit_amount_decimal": (str(int(unit_amount))
                                if unit_amount is not None else None),
    }
    state["prices"][pid] = p
    return p


@mcp.tool(name="list_prices")
def list_prices(product: str | None = None,
                active: bool | None = None,
                currency: str | None = None,
                type: str | None = None,
                lookup_keys: list | None = None,
                created: Any = None,
                limit: int = 10,
                starting_after: str | None = None,
                ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/prices — list prices."""
    with _lock():
        s = _load_state()
        items = list(s["prices"].values())
        if product:
            items = [p for p in items if p.get("product") == product]
        if active is not None:
            items = [p for p in items if bool(p.get("active")) == bool(active)]
        if currency:
            items = [p for p in items
                     if (p.get("currency") or "").lower() == currency.lower()]
        if type:
            items = [p for p in items if p.get("type") == type]
        if lookup_keys:
            lk = set(lookup_keys)
            items = [p for p in items if p.get("lookup_key") in lk]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_prices", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/prices")


@mcp.tool(name="retrieve_price")
def retrieve_price(price_id: str) -> dict:
    """Stripe REST: GET /v1/prices/{id} — retrieve a price."""
    with _lock():
        s = _load_state()
        p = s["prices"].get(price_id)
        _record(s, "retrieve_price", price_id=price_id,
                result="ok" if p else "missing")
        _save_state(s)
        if not p:
            return _resource_missing("price", price_id)
        return dict(p)


@mcp.tool(name="create_price")
def create_price(product: str,
                 currency: str = "usd",
                 unit_amount: int | None = None,
                 recurring: dict | None = None,
                 active: bool = True,
                 metadata: dict | None = None,
                 nickname: str | None = None,
                 tax_behavior: str | None = None,
                 lookup_key: str | None = None) -> dict:
    """Stripe REST: POST /v1/prices — create a price for a product.
    `recurring` is a dict like {"interval":"month","interval_count":1}.
    Omit `recurring` for a one-time price."""
    with _lock():
        s = _load_state()
        if product not in s["products"]:
            return _resource_missing("product", product, param="product")
        if unit_amount is None:
            return _err("invalid_request_error", "parameter_missing",
                        "unit_amount is required", param="unit_amount")
        p = _create_price_obj(s, product=product, currency=currency,
                              unit_amount=unit_amount, recurring=recurring,
                              metadata=metadata, active=active,
                              nickname=nickname, tax_behavior=tax_behavior,
                              lookup_key=lookup_key)
        _record(s, "create_price", price_id=p["id"], product=product)
        _save_state(s)
        return dict(p)


# ===========================================================================
# Subscriptions
# ===========================================================================

def _add_months(epoch: int, months: int) -> int:
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    d = min(dt.day, 28)
    return int(datetime.datetime(y, m, d, dt.hour, dt.minute, dt.second,
                                 tzinfo=datetime.timezone.utc).timestamp())


def _period_end(start: int, recurring: dict | None) -> int:
    if not recurring:
        return _add_months(start, 1)
    interval = recurring.get("interval", "month")
    count = int(recurring.get("interval_count", 1) or 1)
    if interval == "day":
        return start + 86400 * count
    if interval == "week":
        return start + 7 * 86400 * count
    if interval == "month":
        return _add_months(start, count)
    if interval == "year":
        return _add_months(start, 12 * count)
    return _add_months(start, count)


def _serialize_sub(state: dict, sub: dict) -> dict:
    return dict(sub)


@mcp.tool(name="list_subscriptions")
def list_subscriptions(customer: str | None = None,
                       status: str | None = None,
                       price: str | None = None,
                       created: Any = None,
                       limit: int = 10,
                       starting_after: str | None = None,
                       ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/subscriptions — list subscriptions."""
    with _lock():
        s = _load_state()
        items = list(s["subscriptions"].values())
        if customer:
            items = [x for x in items if x.get("customer") == customer]
        if status and status != "all":
            items = [x for x in items if x.get("status") == status]
        if price:
            items = [x for x in items
                     if any(i.get("price", {}).get("id") == price
                            for i in x.get("items", {}).get("data", []))]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_subscriptions", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/subscriptions")


@mcp.tool(name="retrieve_subscription")
def retrieve_subscription(subscription_id: str) -> dict:
    """Stripe REST: GET /v1/subscriptions/{id} — retrieve a
    subscription."""
    with _lock():
        s = _load_state()
        sub = s["subscriptions"].get(subscription_id)
        _record(s, "retrieve_subscription",
                subscription_id=subscription_id,
                result="ok" if sub else "missing")
        _save_state(s)
        if not sub:
            return _resource_missing("subscription", subscription_id)
        return _serialize_sub(s, sub)


@mcp.tool(name="create_subscription")
def create_subscription(customer: str,
                        items: list,
                        metadata: dict | None = None,
                        default_payment_method: str | None = None,
                        collection_method: str = "charge_automatically",
                        days_until_due: int | None = None,
                        trial_period_days: int | None = None,
                        trial_end: int | None = None,
                        description: str | None = None,
                        cancel_at_period_end: bool = False) -> dict:
    """Stripe REST: POST /v1/subscriptions — create a subscription
    for a customer. `items` is a list of `{"price": "price_xxx",
    "quantity": N}` entries (quantity defaults to 1).
    `collection_method` in {charge_automatically, send_invoice}."""
    with _lock():
        s = _load_state()
        if customer not in s["customers"]:
            return _resource_missing("customer", customer, param="customer")
        if not items:
            return _err("invalid_request_error", "parameter_missing",
                        "items is required", param="items")
        sub_items = []
        currency = None
        first_recurring = None
        for idx, it in enumerate(items):
            price_id = it.get("price")
            if not price_id or price_id not in s["prices"]:
                return _resource_missing("price", price_id or "",
                                         param=f"items[{idx}][price]")
            pr = s["prices"][price_id]
            if not pr.get("recurring"):
                return _err("invalid_request_error",
                            "parameter_invalid_string",
                            "subscriptions require recurring prices",
                            param=f"items[{idx}][price]")
            qty = int(it.get("quantity", 1) or 1)
            si_id = _new_id("si")
            sub_items.append({
                "id": si_id,
                "object": "subscription_item",
                "billing_thresholds": None,
                "created": _now_epoch(),
                "metadata": _ensure_metadata(it.get("metadata")),
                "plan": pr,
                "price": pr,
                "quantity": qty,
                "subscription": None,  # filled in below
                "tax_rates": [],
            })
            if currency is None:
                currency = pr["currency"]
            if first_recurring is None:
                first_recurring = pr.get("recurring")
        now = _now_epoch()
        if trial_end is None and trial_period_days:
            trial_end = now + int(trial_period_days) * 86400
        period_start = now
        period_end = _period_end(period_start, first_recurring)
        status = "active"
        if trial_end and trial_end > now:
            status = "trialing"
        sub_id = _new_id("sub")
        for si in sub_items:
            si["subscription"] = sub_id
        sub = {
            "id": sub_id,
            "object": "subscription",
            "application": None,
            "automatic_tax": {"enabled": False},
            "billing_cycle_anchor": period_start,
            "cancel_at": None,
            "cancel_at_period_end": bool(cancel_at_period_end),
            "canceled_at": None,
            "cancellation_details": {"comment": None, "feedback": None,
                                     "reason": None},
            "collection_method": collection_method,
            "created": now,
            "currency": currency,
            "current_period_end": period_end,
            "current_period_start": period_start,
            "customer": customer,
            "days_until_due": days_until_due,
            "default_payment_method": default_payment_method,
            "default_source": None,
            "description": description,
            "discount": None,
            "ended_at": None,
            "items": _list_response(sub_items, False,
                                    f"/v1/subscription_items?subscription={sub_id}"),
            "latest_invoice": None,
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "next_pending_invoice_item_invoice": None,
            "on_behalf_of": None,
            "pause_collection": None,
            "payment_settings": {
                "payment_method_options": None,
                "payment_method_types": None,
                "save_default_payment_method": "off",
            },
            "pending_invoice_item_interval": None,
            "pending_setup_intent": None,
            "pending_update": None,
            "schedule": None,
            "start_date": now,
            "status": status,
            "test_clock": None,
            "transfer_data": None,
            "trial_end": trial_end,
            "trial_settings": {
                "end_behavior": {"missing_payment_method": "create_invoice"},
            },
            "trial_start": now if status == "trialing" else None,
        }
        s["subscriptions"][sub_id] = sub
        _record(s, "create_subscription", subscription_id=sub_id,
                customer=customer, item_count=len(sub_items))
        _save_state(s)
        return _serialize_sub(s, sub)


@mcp.tool(name="update_subscription")
def update_subscription(subscription_id: str,
                        metadata: dict | None = None,
                        cancel_at_period_end: bool | None = None,
                        default_payment_method: str | None = None,
                        items: list | None = None,
                        description: str | None = None,
                        collection_method: str | None = None,
                        days_until_due: int | None = None,
                        proration_behavior: str | None = None,
                        trial_end: int | None = None) -> dict:
    """Stripe REST: POST /v1/subscriptions/{id} — update a
    subscription. To change items, pass a new `items` list (the mock
    replaces items; real Stripe takes patch-style item updates)."""
    with _lock():
        s = _load_state()
        sub = s["subscriptions"].get(subscription_id)
        if not sub:
            _record(s, "update_subscription",
                    subscription_id=subscription_id, result="missing")
            _save_state(s)
            return _resource_missing("subscription", subscription_id)
        if cancel_at_period_end is not None:
            sub["cancel_at_period_end"] = bool(cancel_at_period_end)
        if default_payment_method is not None:
            sub["default_payment_method"] = default_payment_method
        if description is not None:
            sub["description"] = description
        if collection_method is not None:
            sub["collection_method"] = collection_method
        if days_until_due is not None:
            sub["days_until_due"] = int(days_until_due)
        if trial_end is not None:
            sub["trial_end"] = int(trial_end)
        if metadata is not None:
            sub["metadata"] = _apply_metadata_update(sub.get("metadata", {}),
                                                    metadata)
        if items:
            new_items = []
            for idx, it in enumerate(items):
                price_id = it.get("price")
                if not price_id or price_id not in s["prices"]:
                    return _resource_missing("price", price_id or "",
                                             param=f"items[{idx}][price]")
                pr = s["prices"][price_id]
                qty = int(it.get("quantity", 1) or 1)
                si_id = it.get("id") or _new_id("si")
                new_items.append({
                    "id": si_id,
                    "object": "subscription_item",
                    "created": _now_epoch(),
                    "metadata": _ensure_metadata(it.get("metadata")),
                    "plan": pr,
                    "price": pr,
                    "quantity": qty,
                    "subscription": sub["id"],
                    "tax_rates": [],
                })
            sub["items"] = _list_response(
                new_items, False,
                f"/v1/subscription_items?subscription={sub['id']}")
        _record(s, "update_subscription", subscription_id=subscription_id)
        _save_state(s)
        return _serialize_sub(s, sub)


@mcp.tool(name="cancel_subscription")
def cancel_subscription(subscription_id: str,
                        invoice_now: bool = False,
                        prorate: bool = False,
                        cancellation_details: dict | None = None) -> dict:
    """Stripe REST: DELETE /v1/subscriptions/{id} — cancel a
    subscription immediately. To cancel at period end, use
    `update_subscription` with `cancel_at_period_end=true`."""
    with _lock():
        s = _load_state()
        sub = s["subscriptions"].get(subscription_id)
        if not sub:
            _record(s, "cancel_subscription",
                    subscription_id=subscription_id, result="missing")
            _save_state(s)
            return _resource_missing("subscription", subscription_id)
        now = _now_epoch()
        sub["status"] = "canceled"
        sub["canceled_at"] = now
        sub["ended_at"] = now
        sub["cancel_at_period_end"] = False
        if cancellation_details:
            sub["cancellation_details"].update(cancellation_details)
        _record(s, "cancel_subscription", subscription_id=subscription_id)
        _save_state(s)
        return _serialize_sub(s, sub)


# ===========================================================================
# Invoices
# ===========================================================================

@mcp.tool(name="list_invoices")
def list_invoices(customer: str | None = None,
                  subscription: str | None = None,
                  status: str | None = None,
                  created: Any = None,
                  limit: int = 10,
                  starting_after: str | None = None,
                  ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/invoices — list invoices."""
    with _lock():
        s = _load_state()
        items = list(s["invoices"].values())
        if customer:
            items = [i for i in items if i.get("customer") == customer]
        if subscription:
            items = [i for i in items if i.get("subscription") == subscription]
        if status:
            items = [i for i in items if i.get("status") == status]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_invoices", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/invoices")


@mcp.tool(name="retrieve_invoice")
def retrieve_invoice(invoice_id: str) -> dict:
    """Stripe REST: GET /v1/invoices/{id} — retrieve an invoice."""
    with _lock():
        s = _load_state()
        inv = s["invoices"].get(invoice_id)
        _record(s, "retrieve_invoice", invoice_id=invoice_id,
                result="ok" if inv else "missing")
        _save_state(s)
        if not inv:
            return _resource_missing("invoice", invoice_id)
        return dict(inv)


@mcp.tool(name="create_invoice")
def create_invoice(customer: str,
                   subscription: str | None = None,
                   collection_method: str = "charge_automatically",
                   description: str | None = None,
                   metadata: dict | None = None,
                   days_until_due: int | None = None,
                   auto_advance: bool = False,
                   currency: str | None = None,
                   default_payment_method: str | None = None,
                   footer: str | None = None) -> dict:
    """Stripe REST: POST /v1/invoices — create a draft invoice for a
    customer (or out-of-band invoice for a subscription)."""
    with _lock():
        s = _load_state()
        if customer not in s["customers"]:
            return _resource_missing("customer", customer, param="customer")
        cust = s["customers"][customer]
        cur = (currency or cust.get("currency") or "usd").lower()
        iid = _new_id("in")
        now = _now_epoch()
        due_date = None
        if collection_method == "send_invoice":
            d = days_until_due if days_until_due is not None else 30
            due_date = now + int(d) * 86400
        inv = {
            "id": iid,
            "object": "invoice",
            "account_country": "US",
            "account_name": "Mock Account",
            "amount_due": 0,
            "amount_paid": 0,
            "amount_remaining": 0,
            "amount_shipping": 0,
            "application": None,
            "attempt_count": 0,
            "attempted": False,
            "auto_advance": bool(auto_advance),
            "automatic_tax": {"enabled": False, "status": None},
            "billing_reason": ("subscription_create" if subscription
                               else "manual"),
            "charge": None,
            "collection_method": collection_method,
            "created": now,
            "currency": cur,
            "custom_fields": None,
            "customer": customer,
            "customer_address": cust.get("address"),
            "customer_email": cust.get("email"),
            "customer_name": cust.get("name"),
            "customer_phone": cust.get("phone"),
            "customer_shipping": None,
            "customer_tax_exempt": cust.get("tax_exempt", "none"),
            "customer_tax_ids": [],
            "default_payment_method": default_payment_method,
            "default_source": None,
            "description": description,
            "discount": None,
            "discounts": [],
            "due_date": due_date,
            "ending_balance": None,
            "footer": footer,
            "from_invoice": None,
            "hosted_invoice_url": (f"https://invoice.stripe.com/i/"
                                   f"{iid}/{_rand_suffix(16)}"),
            "invoice_pdf": f"https://pay.stripe.com/invoice/{iid}/pdf",
            "issuer": {"type": "self"},
            "last_finalization_error": None,
            "latest_revision": None,
            "lines": _list_response([], False,
                                    f"/v1/invoices/{iid}/lines"),
            "livemode": False,
            "metadata": _ensure_metadata(metadata),
            "next_payment_attempt": None,
            "number": None,
            "on_behalf_of": None,
            "paid": False,
            "paid_out_of_band": False,
            "payment_intent": None,
            "payment_settings": {"default_mandate": None,
                                 "payment_method_options": None,
                                 "payment_method_types": None},
            "period_end": now,
            "period_start": now,
            "post_payment_credit_notes_amount": 0,
            "pre_payment_credit_notes_amount": 0,
            "quote": None,
            "receipt_number": None,
            "rendering": None,
            "shipping_cost": None,
            "shipping_details": None,
            "starting_balance": 0,
            "statement_descriptor": None,
            "status": "draft",
            "status_transitions": {
                "finalized_at": None,
                "marked_uncollectible_at": None,
                "paid_at": None,
                "voided_at": None,
            },
            "subscription": subscription,
            "subscription_details": ({"metadata": {}} if subscription
                                     else None),
            "subtotal": 0,
            "subtotal_excluding_tax": 0,
            "tax": None,
            "test_clock": None,
            "total": 0,
            "total_discount_amounts": [],
            "total_excluding_tax": 0,
            "total_tax_amounts": [],
            "transfer_data": None,
            "webhooks_delivered_at": None,
        }
        # If this is for a subscription, populate one line per item.
        if subscription and subscription in s["subscriptions"]:
            sub = s["subscriptions"][subscription]
            lines = []
            subtotal = 0
            for si in sub["items"]["data"]:
                pr = si["price"]
                qty = si.get("quantity", 1)
                amount = (pr.get("unit_amount") or 0) * qty
                subtotal += amount
                lines.append({
                    "id": _new_id("il"),
                    "object": "line_item",
                    "amount": amount,
                    "amount_excluding_tax": amount,
                    "currency": cur,
                    "description": (pr.get("nickname")
                                    or f"{qty} × Subscription"),
                    "discount_amounts": [],
                    "discountable": True,
                    "discounts": [],
                    "invoice_item": None,
                    "livemode": False,
                    "metadata": {},
                    "period": {"start": sub["current_period_start"],
                               "end": sub["current_period_end"]},
                    "plan": pr,
                    "price": pr,
                    "proration": False,
                    "proration_details": {"credited_items": None},
                    "quantity": qty,
                    "subscription": subscription,
                    "subscription_item": si["id"],
                    "tax_amounts": [],
                    "tax_rates": [],
                    "type": "subscription",
                    "unit_amount_excluding_tax": str(pr.get("unit_amount", 0)),
                })
            inv["lines"] = _list_response(lines, False,
                                          f"/v1/invoices/{iid}/lines")
            inv["subtotal"] = subtotal
            inv["subtotal_excluding_tax"] = subtotal
            inv["total"] = subtotal
            inv["total_excluding_tax"] = subtotal
            inv["amount_due"] = subtotal
            inv["amount_remaining"] = subtotal
        s["invoices"][iid] = inv
        _record(s, "create_invoice", invoice_id=iid, customer=customer,
                subscription=subscription)
        _save_state(s)
        return dict(inv)


def _next_invoice_number(state: dict, customer_id: str) -> str:
    cust = state["customers"].get(customer_id, {})
    prefix = cust.get("invoice_prefix") or "MOCK"
    seq = cust.get("next_invoice_sequence", 1)
    cust["next_invoice_sequence"] = seq + 1
    return f"{prefix}-{seq:04d}"


@mcp.tool(name="finalize_invoice")
def finalize_invoice(invoice_id: str,
                     auto_advance: bool | None = None) -> dict:
    """Stripe REST: POST /v1/invoices/{id}/finalize — finalize a
    draft invoice. Status transitions draft -> open."""
    with _lock():
        s = _load_state()
        inv = s["invoices"].get(invoice_id)
        if not inv:
            _record(s, "finalize_invoice", invoice_id=invoice_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("invoice", invoice_id)
        if inv["status"] != "draft":
            return _err("invalid_request_error", "invoice_not_editable",
                        f"Invoice in status '{inv['status']}' cannot be "
                        f"finalized.")
        if auto_advance is not None:
            inv["auto_advance"] = bool(auto_advance)
        inv["status"] = "open"
        inv["number"] = _next_invoice_number(s, inv["customer"])
        inv["status_transitions"]["finalized_at"] = _now_epoch()
        _record(s, "finalize_invoice", invoice_id=invoice_id)
        _save_state(s)
        return dict(inv)


@mcp.tool(name="pay_invoice")
def pay_invoice(invoice_id: str,
                payment_method: str | None = None,
                source: str | None = None,
                paid_out_of_band: bool = False,
                forgive: bool = False) -> dict:
    """Stripe REST: POST /v1/invoices/{id}/pay — attempt to pay an
    open invoice. Set `paid_out_of_band=true` to mark it paid
    without collecting payment."""
    with _lock():
        s = _load_state()
        inv = s["invoices"].get(invoice_id)
        if not inv:
            _record(s, "pay_invoice", invoice_id=invoice_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("invoice", invoice_id)
        if inv["status"] not in ("open", "draft"):
            return _err("invalid_request_error",
                        "invoice_not_payable",
                        f"Invoice in status '{inv['status']}' cannot be paid.")
        now = _now_epoch()
        if inv["status"] == "draft":
            inv["status"] = "open"
            inv["number"] = _next_invoice_number(s, inv["customer"])
            inv["status_transitions"]["finalized_at"] = now
        if forgive:
            inv["status"] = "paid"
            inv["paid"] = True
            inv["amount_paid"] = 0
            inv["amount_remaining"] = 0
            inv["status_transitions"]["paid_at"] = now
        elif paid_out_of_band:
            inv["status"] = "paid"
            inv["paid"] = True
            inv["paid_out_of_band"] = True
            inv["amount_paid"] = inv["amount_due"]
            inv["amount_remaining"] = 0
            inv["status_transitions"]["paid_at"] = now
        else:
            # Create a charge for the invoice amount.
            amount = inv["amount_due"]
            ch_id = _new_id("ch")
            fee = max(30, int(amount * 0.029)) if amount > 0 else 0
            ch = {
                "id": ch_id,
                "object": "charge",
                "amount": amount,
                "amount_captured": amount,
                "amount_refunded": 0,
                "captured": True,
                "created": now,
                "currency": inv["currency"],
                "customer": inv["customer"],
                "description": (f"Invoice {inv.get('number') or invoice_id}"),
                "invoice": invoice_id,
                "livemode": False,
                "paid": True,
                "payment_method": (payment_method or source
                                   or inv.get("default_payment_method")),
                "payment_intent": None,
                "receipt_url": f"https://pay.stripe.com/receipts/{ch_id}",
                "refunded": False,
                "refunds": _list_response(
                    [], False, f"/v1/charges/{ch_id}/refunds"),
                "status": "succeeded",
                "metadata": {},
            }
            txn = _record_balance_transaction(
                s, amount=amount, currency=inv["currency"],
                source=ch_id, type_="charge", fee=fee,
                description=ch["description"])
            ch["balance_transaction"] = txn["id"]
            s["charges"][ch_id] = ch
            inv["charge"] = ch_id
            inv["status"] = "paid"
            inv["paid"] = True
            inv["amount_paid"] = amount
            inv["amount_remaining"] = 0
            inv["status_transitions"]["paid_at"] = now
            inv["attempt_count"] += 1
            inv["attempted"] = True
        _record(s, "pay_invoice", invoice_id=invoice_id,
                paid_out_of_band=paid_out_of_band, forgive=forgive)
        _save_state(s)
        return dict(inv)


@mcp.tool(name="send_invoice")
def send_invoice(invoice_id: str) -> dict:
    """Stripe REST: POST /v1/invoices/{id}/send — email a finalized
    invoice to the customer (collection_method=send_invoice). The
    mock just marks `webhooks_delivered_at`."""
    with _lock():
        s = _load_state()
        inv = s["invoices"].get(invoice_id)
        if not inv:
            _record(s, "send_invoice", invoice_id=invoice_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("invoice", invoice_id)
        if inv["status"] != "open":
            return _err("invalid_request_error", "invoice_not_sendable",
                        f"Invoice in status '{inv['status']}' cannot be "
                        f"sent.")
        inv["webhooks_delivered_at"] = _now_epoch()
        _record(s, "send_invoice", invoice_id=invoice_id)
        _save_state(s)
        return dict(inv)


@mcp.tool(name="void_invoice")
def void_invoice(invoice_id: str) -> dict:
    """Stripe REST: POST /v1/invoices/{id}/void — void a finalized
    invoice. Only `open` invoices can be voided."""
    with _lock():
        s = _load_state()
        inv = s["invoices"].get(invoice_id)
        if not inv:
            _record(s, "void_invoice", invoice_id=invoice_id,
                    result="missing")
            _save_state(s)
            return _resource_missing("invoice", invoice_id)
        if inv["status"] != "open":
            return _err("invalid_request_error", "invoice_not_voidable",
                        f"Invoice in status '{inv['status']}' cannot be "
                        f"voided.")
        inv["status"] = "void"
        inv["status_transitions"]["voided_at"] = _now_epoch()
        inv["amount_remaining"] = 0
        _record(s, "void_invoice", invoice_id=invoice_id)
        _save_state(s)
        return dict(inv)


# ===========================================================================
# Payouts
# ===========================================================================

@mcp.tool(name="list_payouts")
def list_payouts(status: str | None = None,
                 destination: str | None = None,
                 created: Any = None,
                 arrival_date: Any = None,
                 limit: int = 10,
                 starting_after: str | None = None,
                 ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/payouts — list payouts."""
    with _lock():
        s = _load_state()
        items = list(s["payouts"].values())
        if status:
            items = [p for p in items if p.get("status") == status]
        if destination:
            items = [p for p in items if p.get("destination") == destination]
        items = _filter_by_created(items, created)
        if arrival_date is not None:
            # Reuse created-style filter on arrival_date
            def _arr(it: dict) -> dict:
                return {"created": it.get("arrival_date", 0)}
            items = [it for it in items
                     if _filter_by_created([_arr(it)], arrival_date)]
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_payouts", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/payouts")


@mcp.tool(name="retrieve_payout")
def retrieve_payout(payout_id: str) -> dict:
    """Stripe REST: GET /v1/payouts/{id} — retrieve a payout."""
    with _lock():
        s = _load_state()
        p = s["payouts"].get(payout_id)
        _record(s, "retrieve_payout", payout_id=payout_id,
                result="ok" if p else "missing")
        _save_state(s)
        if not p:
            return _resource_missing("payout", payout_id)
        return dict(p)


# ===========================================================================
# Balance
# ===========================================================================

@mcp.tool(name="retrieve_balance")
def retrieve_balance() -> dict:
    """Stripe REST: GET /v1/balance — retrieve the current account
    balance (available + pending), broken down by currency."""
    with _lock():
        s = _load_state()
        _record(s, "retrieve_balance")
        _save_state(s)
        return dict(s["balance"])


@mcp.tool(name="list_balance_transactions")
def list_balance_transactions(type: str | None = None,
                              currency: str | None = None,
                              source: str | None = None,
                              payout: str | None = None,
                              created: Any = None,
                              available_on: Any = None,
                              limit: int = 10,
                              starting_after: str | None = None,
                              ending_before: str | None = None) -> dict:
    """Stripe REST: GET /v1/balance_transactions — list
    balance-affecting transactions (charges, refunds, payouts, fees)."""
    with _lock():
        s = _load_state()
        items = list(s["balance_transactions"].values())
        if type:
            items = [t for t in items if t.get("type") == type]
        if currency:
            items = [t for t in items
                     if (t.get("currency") or "").lower() == currency.lower()]
        if source:
            items = [t for t in items if t.get("source") == source]
        if payout:
            items = [t for t in items if t.get("source") == payout]
        items = _filter_by_created(items, created)
        items = _sorted_desc(items)
        page, has_more = _paginate(items, limit, starting_after, ending_before)
        _record(s, "list_balance_transactions", count=len(page))
        _save_state(s)
        return _list_response(page, has_more, "/v1/balance_transactions")


# ===========================================================================
# Mock-only helpers
# ===========================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (verifier introspection)."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(account: dict | None = None,
                    balance: dict | None = None,
                    customers: list | None = None,
                    products: list | None = None,
                    prices: list | None = None,
                    charges: list | None = None,
                    payment_intents: list | None = None,
                    refunds: list | None = None,
                    subscriptions: list | None = None,
                    invoices: list | None = None,
                    payouts: list | None = None,
                    balance_transactions: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state with Stripe-shaped objects, bypassing
    validation. Each collection is a list of dicts; if an item lacks
    `id`, a new one is generated with the right prefix. If `replace`
    is true, state is fully reset before seeding."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if account:
            s["account"].update(account)
        if balance:
            s["balance"].update(balance)

        def _put(bucket: str, prefix: str, items: list | None,
                 obj_type: str) -> list[str]:
            ids = []
            for raw in (items or []):
                entry = dict(raw)
                entry.setdefault("object", obj_type)
                entry.setdefault("livemode", False)
                entry.setdefault("created", _now_epoch())
                if "metadata" in entry:
                    entry["metadata"] = _ensure_metadata(entry["metadata"])
                else:
                    entry["metadata"] = {}
                rid = entry.get("id") or _new_id(prefix)
                entry["id"] = rid
                s[bucket][rid] = entry
                ids.append(rid)
            return ids

        ids = {
            "customers": _put("customers", "cus", customers, "customer"),
            "products": _put("products", "prod", products, "product"),
            "prices": _put("prices", "price", prices, "price"),
            "charges": _put("charges", "ch", charges, "charge"),
            "payment_intents": _put("payment_intents", "pi", payment_intents,
                                    "payment_intent"),
            "refunds": _put("refunds", "re", refunds, "refund"),
            "subscriptions": _put("subscriptions", "sub", subscriptions,
                                  "subscription"),
            "invoices": _put("invoices", "in", invoices, "invoice"),
            "payouts": _put("payouts", "po", payouts, "payout"),
            "balance_transactions": _put("balance_transactions", "txn",
                                         balance_transactions,
                                         "balance_transaction"),
        }
        _record(s, "debug_seed",
                counts={k: len(v) for k, v in ids.items()},
                replace=replace)
        _save_state(s)
        return {"ok": True, "ids": ids}


if __name__ == "__main__":
    mcp.run()
