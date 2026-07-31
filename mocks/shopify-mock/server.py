"""Shopify mock MCP server.

Mirrors the operation surface and JSON shapes of the Shopify Admin
REST API (https://shopify.dev/docs/api/admin-rest). Tools are named
after the REST resources/actions they correspond to (e.g.
`list_products` -> GET /admin/api/{version}/products.json,
`create_order` -> POST /admin/api/{version}/orders.json), and every
response payload uses the singular/plural envelope that the real
Admin API returns (`{"product": {...}}`, `{"products": [...]}`,
`{"count": N}`, etc.).

This is *not* a wrapper over the shopify-checkout CLI mock — it is a
standalone Admin REST mock, intended to back tasks that interact with
Shopify's `Product`, `Variant`, `Order`, `Customer`, `Collection`,
`InventoryLevel`, and `Shop` resources.

State is a single JSON file at `$SHOPIFY_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/shopify_mock`). Layout:

    {
      "shop": {...shop object...},
      "products":    {"<id>": {...product object...}},
      "variants":    {"<id>": {...variant object...}},
      "orders":      {"<id>": {...order object...}},
      "customers":   {"<id>": {...customer object...}},
      "collections": {"<id>": {...collection object...}},
      "inventory_items":  {"<id>": {...inventory item...}},
      "inventory_levels": {"<key>": {...level object...}},
      "next_id": {"product": N, "variant": N, ...},
      "calls": [{"op": "...", "ts": "...", ...}]
    }

Errors are returned (not raised) so the call log captures them just
like real HTTP failures. Two shapes are used to match Shopify:

  - Lookup / generic failure:
      {"errors": "Not Found"}
  - Validation failure:
      {"errors": {"title": ["can't be blank"]}}

Seed via `SHOPIFY_MOCK_SEED_PATH` (only loaded if no state.json
exists yet). Per-rollout isolation should clear the state dir
between rollouts.
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


SHOPIFY_API_VERSION = "2024-10"

PRODUCT_STATUSES = {"active", "archived", "draft"}
ORDER_FINANCIAL = {"pending", "authorized", "partially_paid", "paid",
                   "partially_refunded", "refunded", "voided"}
ORDER_FULFILLMENT = {None, "fulfilled", "partial", "restocked",
                     "unfulfilled"}


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "SHOPIFY_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/shopify_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    """Shopify uses ISO-8601 with timezone offset, e.g.
    '2024-10-15T11:23:45-04:00'. We emit UTC ('+00:00')."""
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"))


def _empty_state() -> dict:
    return {
        "shop": {
            "id": 1,
            "name": "Mock Shop",
            "email": "owner@mock.shop",
            "domain": "mock.myshopify.com",
            "myshopify_domain": "mock.myshopify.com",
            "shop_owner": "Mock Owner",
            "currency": "USD",
            "money_format": "${{amount}}",
            "money_with_currency_format": "${{amount}} USD",
            "weight_unit": "kg",
            "country_code": "US",
            "country_name": "United States",
            "timezone": "(GMT+00:00) UTC",
            "iana_timezone": "Etc/UTC",
            "primary_locale": "en",
            "plan_name": "basic",
            "created_at": _now(),
            "updated_at": _now(),
        },
        "products": {},
        "variants": {},          # variant_id -> variant dict (also embedded in product)
        "orders": {},
        "customers": {},
        "collections": {},
        "inventory_items": {},   # inventory_item_id -> {id, sku, tracked, ...}
        "inventory_levels": {},  # "{location_id}:{inventory_item_id}" -> level
        "locations": {},
        "next_id": {
            "product": 1_000_000_001,
            "variant": 2_000_000_001,
            "order": 3_000_000_001,
            "customer": 4_000_000_001,
            "collection": 5_000_000_001,
            "inventory_item": 6_000_000_001,
            "location": 7_000_000_001,
            "image": 8_000_000_001,
            "line_item": 9_000_000_001,
            "order_number": 1001,
        },
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("SHOPIFY_MOCK_SEED_PATH")
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


# ---------------------------------------------------------------------------
# Error helpers — match Shopify Admin REST error shapes
# ---------------------------------------------------------------------------

def _err_not_found() -> dict:
    """Shopify returns this on 404 lookups: {"errors": "Not Found"}."""
    return {"errors": "Not Found"}


def _err_string(message: str) -> dict:
    """Generic string error envelope used by Shopify for non-validation
    failures (e.g. {"errors": "Order has already been canceled."})."""
    return {"errors": message}


def _err_fields(field_errors: dict[str, list[str]]) -> dict:
    """Validation errors: {"errors": {"title": ["can't be blank"]}}."""
    return {"errors": field_errors}


# ---------------------------------------------------------------------------
# ID + utility helpers
# ---------------------------------------------------------------------------

def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _handle(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-",
                  (s or "").lower()).strip("-") or "item"


def _paginate(items: list, limit: int, page_info: str | None) -> list:
    """Shopify caps `limit` at 250 (default 50). `page_info` is an
    opaque cursor in the real API; here we treat it as a stringified
    integer offset for predictability."""
    limit = max(1, min(int(limit or 50), 250))
    start = 0
    if page_info:
        try:
            start = int(page_info)
        except (TypeError, ValueError):
            start = 0
    return items[start: start + limit]


def _money(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_iso(v: str | None) -> datetime.datetime | None:
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_by_ids(items: list, ids: str | None) -> list:
    """`?ids=632910392,921728736` — comma-separated id filter."""
    if not ids:
        return items
    want = {s.strip() for s in str(ids).split(",") if s.strip()}
    return [i for i in items if str(i.get("id")) in want]


def _filter_by_date(items: list, field: str,
                    min_v: str | None, max_v: str | None) -> list:
    if not (min_v or max_v):
        return items
    out = items
    if min_v:
        out = [i for i in out if (i.get(field) or "") >= min_v]
    if max_v:
        out = [i for i in out if (i.get(field) or "") <= max_v]
    return out


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------

def _new_variant(state: dict, product_id: int, data: dict,
                 position: int) -> dict:
    vid = _next_id(state, "variant")
    iid = _next_id(state, "inventory_item")
    now = _now()
    price = data.get("price")
    if price is None:
        price = "0.00"
    else:
        price = f"{_money(price):.2f}"
    variant = {
        "id": vid,
        "product_id": product_id,
        "title": data.get("title") or "Default Title",
        "price": price,
        "sku": data.get("sku", ""),
        "position": position,
        "inventory_policy": data.get("inventory_policy", "deny"),
        "compare_at_price": data.get("compare_at_price"),
        "fulfillment_service": data.get("fulfillment_service", "manual"),
        "inventory_management": data.get("inventory_management"),
        "option1": data.get("option1") or "Default Title",
        "option2": data.get("option2"),
        "option3": data.get("option3"),
        "created_at": now,
        "updated_at": now,
        "taxable": bool(data.get("taxable", True)),
        "barcode": data.get("barcode", ""),
        "grams": int(data.get("grams", 0) or 0),
        "image_id": data.get("image_id"),
        "weight": _money(data.get("weight", 0)),
        "weight_unit": data.get("weight_unit", "kg"),
        "inventory_item_id": iid,
        "inventory_quantity": int(data.get("inventory_quantity", 0) or 0),
        "old_inventory_quantity": int(data.get("inventory_quantity", 0) or 0),
        "requires_shipping": bool(data.get("requires_shipping", True)),
        "admin_graphql_api_id": f"gid://shopify/ProductVariant/{vid}",
    }
    state["variants"][str(vid)] = variant
    state["inventory_items"][str(iid)] = {
        "id": iid,
        "sku": variant["sku"],
        "tracked": variant["inventory_management"] == "shopify",
        "requires_shipping": variant["requires_shipping"],
        "created_at": now,
        "updated_at": now,
        "admin_graphql_api_id": f"gid://shopify/InventoryItem/{iid}",
    }
    return variant


def _new_product(state: dict, data: dict) -> dict:
    pid = _next_id(state, "product")
    now = _now()
    title = data.get("title", "")
    handle = data.get("handle") or _handle(title or f"product-{pid}")
    raw_variants = data.get("variants") or [{}]
    options_in = data.get("options")
    if options_in:
        options = [
            {"id": _next_id(state, "image"),  # synthetic option id
             "product_id": pid,
             "name": o.get("name", f"Option {i+1}"),
             "position": i + 1,
             "values": list(o.get("values") or [])}
            for i, o in enumerate(options_in)
        ]
    else:
        options = [{"id": _next_id(state, "image"),
                    "product_id": pid, "name": "Title",
                    "position": 1, "values": ["Default Title"]}]
    images_in = data.get("images") or []
    images = []
    for i, img in enumerate(images_in):
        iid = _next_id(state, "image")
        images.append({
            "id": iid, "product_id": pid, "position": i + 1,
            "created_at": now, "updated_at": now,
            "alt": img.get("alt"),
            "width": img.get("width", 0),
            "height": img.get("height", 0),
            "src": img.get("src", ""),
            "variant_ids": img.get("variant_ids", []),
            "admin_graphql_api_id": f"gid://shopify/ProductImage/{iid}",
        })
    product = {
        "id": pid,
        "title": title,
        "body_html": data.get("body_html", ""),
        "vendor": data.get("vendor", ""),
        "product_type": data.get("product_type", ""),
        "created_at": now,
        "handle": handle,
        "updated_at": now,
        "published_at": now if data.get("status", "active") == "active" else None,
        "template_suffix": data.get("template_suffix"),
        "status": data.get("status", "active"),
        "published_scope": data.get("published_scope", "web"),
        "tags": data.get("tags", ""),
        "admin_graphql_api_id": f"gid://shopify/Product/{pid}",
        "variants": [],
        "options": options,
        "images": images,
        "image": images[0] if images else None,
    }
    state["products"][str(pid)] = product
    for i, v in enumerate(raw_variants):
        product["variants"].append(_new_variant(state, pid, v, i + 1))
    return product


def _new_customer(state: dict, data: dict) -> dict:
    cid = _next_id(state, "customer")
    now = _now()
    cust = {
        "id": cid,
        "email": data.get("email"),
        "accepts_marketing": bool(data.get("accepts_marketing", False)),
        "created_at": now,
        "updated_at": now,
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "orders_count": 0,
        "state": data.get("state", "disabled"),
        "total_spent": "0.00",
        "last_order_id": None,
        "note": data.get("note"),
        "verified_email": bool(data.get("verified_email", True)),
        "multipass_identifier": None,
        "tax_exempt": False,
        "phone": data.get("phone"),
        "tags": data.get("tags", ""),
        "currency": "USD",
        "addresses": data.get("addresses", []),
        "default_address": (data.get("addresses") or [{}])[0]
                            if data.get("addresses") else None,
        "admin_graphql_api_id": f"gid://shopify/Customer/{cid}",
    }
    state["customers"][str(cid)] = cust
    return cust


def _new_order(state: dict, data: dict) -> dict:
    oid = _next_id(state, "order")
    onum = _next_id(state, "order_number")
    now = _now()
    line_items_out = []
    subtotal = 0.0
    for li in data.get("line_items", []) or []:
        lid = _next_id(state, "line_item")
        vid = li.get("variant_id")
        v = state["variants"].get(str(vid)) if vid else None
        p = None
        if v:
            p = state["products"].get(str(v.get("product_id")))
        qty = int(li.get("quantity", 1))
        price = li.get("price")
        if price is None and v:
            price = v.get("price")
        if price is None:
            price = "0.00"
        unit = _money(price)
        line_items_out.append({
            "id": lid,
            "variant_id": vid,
            "title": (li.get("title") or (p and p.get("title")) or ""),
            "quantity": qty,
            "sku": li.get("sku") or (v and v.get("sku")) or "",
            "variant_title": (v and v.get("title")) or None,
            "vendor": (p and p.get("vendor")) or None,
            "fulfillment_service": (v and v.get("fulfillment_service")) or "manual",
            "product_id": (v and v.get("product_id")) or li.get("product_id"),
            "requires_shipping": True,
            "taxable": True,
            "gift_card": False,
            "name": (li.get("title") or (p and p.get("title")) or ""),
            "variant_inventory_management": (v and v.get("inventory_management")) or None,
            "price": f"{unit:.2f}",
            "total_discount": "0.00",
            "fulfillment_status": None,
            "price_set": {"shop_money": {"amount": f"{unit:.2f}",
                                         "currency_code": "USD"}},
            "discount_allocations": [],
            "duties": [],
            "admin_graphql_api_id": f"gid://shopify/LineItem/{lid}",
            "tax_lines": [],
        })
        subtotal += unit * qty
        # Decrement inventory if tracked
        if v and v.get("inventory_management") == "shopify":
            v["inventory_quantity"] = int(v.get("inventory_quantity", 0)) - qty
            v["updated_at"] = now
    cust = None
    cid = data.get("customer_id")
    if cid is None and isinstance(data.get("customer"), dict):
        c_in = data["customer"]
        if c_in.get("id"):
            cust = state["customers"].get(str(c_in["id"]))
        elif c_in.get("email"):
            cust = next((c for c in state["customers"].values()
                         if c.get("email", "").lower()
                         == c_in["email"].lower()), None)
            if not cust:
                cust = _new_customer(state, c_in)
        if cust:
            cid = cust["id"]
    elif cid is not None:
        cust = state["customers"].get(str(cid))
    email = data.get("email") or (cust and cust.get("email"))
    total = f"{subtotal:.2f}"
    fin = data.get("financial_status", "pending")
    order = {
        "id": oid,
        "admin_graphql_api_id": f"gid://shopify/Order/{oid}",
        "app_id": None,
        "browser_ip": None,
        "buyer_accepts_marketing": False,
        "cancel_reason": None,
        "cancelled_at": None,
        "cart_token": None,
        "checkout_id": None,
        "checkout_token": None,
        "client_details": None,
        "closed_at": None,
        "confirmation_number": None,
        "confirmed": True,
        "contact_email": email,
        "created_at": now,
        "currency": data.get("currency", "USD"),
        "current_subtotal_price": total,
        "current_total_discounts": "0.00",
        "current_total_price": total,
        "current_total_tax": "0.00",
        "customer_locale": "en",
        "device_id": None,
        "discount_codes": [],
        "email": email,
        "estimated_taxes": False,
        "financial_status": fin,
        "fulfillment_status": data.get("fulfillment_status"),
        "gateway": "",
        "landing_site": None,
        "landing_site_ref": None,
        "location_id": None,
        "name": f"#{onum}",
        "note": data.get("note"),
        "note_attributes": data.get("note_attributes", []),
        "number": onum - 1000,
        "order_number": onum,
        "order_status_url": (f"https://{state['shop']['myshopify_domain']}/"
                             f"orders/{oid}"),
        "original_total_duties_set": None,
        "payment_gateway_names": data.get("payment_gateway_names", []),
        "phone": data.get("phone"),
        "presentment_currency": data.get("currency", "USD"),
        "processed_at": now,
        "processing_method": "direct",
        "reference": None,
        "referring_site": None,
        "source_identifier": None,
        "source_name": data.get("source_name", "web"),
        "source_url": None,
        "subtotal_price": total,
        "tags": data.get("tags", ""),
        "tax_lines": [],
        "taxes_included": False,
        "test": bool(data.get("test", False)),
        "token": f"mocktoken{oid:08d}",
        "total_discounts": "0.00",
        "total_line_items_price": total,
        "total_outstanding": "0.00",
        "total_price": total,
        "total_price_usd": total,
        "total_shipping_price_set": {"shop_money": {"amount": "0.00",
                                                    "currency_code": "USD"}},
        "total_tax": "0.00",
        "total_tip_received": "0.00",
        "total_weight": 0,
        "updated_at": now,
        "user_id": None,
        "billing_address": data.get("billing_address"),
        "customer": cust,
        "discount_applications": [],
        "fulfillments": [],
        "line_items": line_items_out,
        "payment_terms": None,
        "refunds": [],
        "shipping_address": data.get("shipping_address"),
        "shipping_lines": data.get("shipping_lines", []),
    }
    state["orders"][str(oid)] = order
    if cust:
        cust["orders_count"] = int(cust.get("orders_count", 0)) + 1
        cust["last_order_id"] = oid
        cust["last_order_name"] = order["name"]
        cust["total_spent"] = (
            f"{_money(cust.get('total_spent', '0')) + _money(total):.2f}")
        cust["updated_at"] = now
    return order


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("shopify-mock")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@mcp.tool(name="list_products")
def list_products(limit: int = 50,
                  page_info: str | None = None,
                  ids: str | None = None,
                  title: str | None = None,
                  vendor: str | None = None,
                  handle: str | None = None,
                  product_type: str | None = None,
                  status: str | None = None,
                  collection_id: int | None = None,
                  created_at_min: str | None = None,
                  created_at_max: str | None = None,
                  updated_at_min: str | None = None,
                  updated_at_max: str | None = None,
                  fields: str | None = None) -> dict:
    """GET /admin/api/{version}/products.json — list products.
    Returns {"products": [...]}.
    """
    with _lock():
        s = _load_state()
        items = list(s["products"].values())
        items = _filter_by_ids(items, ids)
        if title:
            items = [p for p in items if title.lower() in (p.get("title") or "").lower()]
        if vendor:
            items = [p for p in items if p.get("vendor") == vendor]
        if handle:
            items = [p for p in items if p.get("handle") == handle]
        if product_type:
            items = [p for p in items if p.get("product_type") == product_type]
        if status:
            wanted = {s.strip() for s in status.split(",") if s.strip()}
            items = [p for p in items if p.get("status") in wanted]
        if collection_id is not None:
            col = s["collections"].get(str(collection_id))
            if col is not None:
                pids = {int(pid) for pid in col.get("product_ids", [])}
                items = [p for p in items if int(p["id"]) in pids]
            else:
                items = []
        items = _filter_by_date(items, "created_at",
                                created_at_min, created_at_max)
        items = _filter_by_date(items, "updated_at",
                                updated_at_min, updated_at_max)
        items.sort(key=lambda p: int(p["id"]))
        page = _paginate(items, limit, page_info)
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            page = [{k: v for k, v in p.items() if k in keep} for p in page]
        _record(s, "list_products", count=len(page),
                title=title, vendor=vendor, status=status)
        _save_state(s)
        return {"products": page}


@mcp.tool(name="get_product")
def get_product(product_id: int, fields: str | None = None) -> dict:
    """GET /admin/api/{version}/products/{product_id}.json."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(product_id))
        _record(s, "get_product", product_id=product_id,
                result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            return _err_not_found()
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            p = {k: v for k, v in p.items() if k in keep}
        return {"product": p}


@mcp.tool(name="create_product")
def create_product(product: dict) -> dict:
    """POST /admin/api/{version}/products.json. Body: {"product": {...}}.
    Required: `title`. Validation errors come back as
    {"errors": {"title": ["can't be blank"]}}.
    """
    with _lock():
        s = _load_state()
        if not isinstance(product, dict):
            _record(s, "create_product", result="invalid")
            _save_state(s)
            return _err_fields({"product": ["must be a hash"]})
        if not product.get("title"):
            _record(s, "create_product", result="missing_title")
            _save_state(s)
            return _err_fields({"title": ["can't be blank"]})
        st = product.get("status", "active")
        if st not in PRODUCT_STATUSES:
            _record(s, "create_product", result="bad_status", status=st)
            _save_state(s)
            return _err_fields({"status": [f"is not included in the list"]})
        p = _new_product(s, product)
        _record(s, "create_product", product_id=p["id"], title=p["title"])
        _save_state(s)
        return {"product": p}


@mcp.tool(name="update_product")
def update_product(product_id: int, product: dict) -> dict:
    """PUT /admin/api/{version}/products/{product_id}.json.
    Body: {"product": {...}}. `id` in the body is ignored.
    """
    with _lock():
        s = _load_state()
        p = s["products"].get(str(product_id))
        if not p:
            _record(s, "update_product", product_id=product_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        data = dict(product or {})
        data.pop("id", None)
        if (st := data.get("status")) and st not in PRODUCT_STATUSES:
            _record(s, "update_product", product_id=product_id,
                    result="bad_status")
            _save_state(s)
            return _err_fields({"status": ["is not included in the list"]})
        # Variants update: if provided, replace by id-match (only top-level
        # fields, not creating new variants here).
        if "variants" in data:
            new_variants = []
            for v_in in data["variants"] or []:
                if v_in.get("id") and str(v_in["id"]) in s["variants"]:
                    v = s["variants"][str(v_in["id"])]
                    for k, val in v_in.items():
                        if k == "id":
                            continue
                        v[k] = val
                    v["updated_at"] = _now()
                    new_variants.append(v)
            if new_variants:
                p["variants"] = new_variants
            del data["variants"]
        if "tags" in data and isinstance(data["tags"], list):
            data["tags"] = ", ".join(str(t) for t in data["tags"])
        for k, v in data.items():
            p[k] = v
        p["updated_at"] = _now()
        if p.get("title") and not p.get("handle"):
            p["handle"] = _handle(p["title"])
        _record(s, "update_product", product_id=product_id,
                fields=list((product or {}).keys()))
        _save_state(s)
        return {"product": p}


@mcp.tool(name="delete_product")
def delete_product(product_id: int) -> dict:
    """DELETE /admin/api/{version}/products/{product_id}.json. The
    real Admin REST returns `{}` (empty hash) on success."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(product_id))
        if not p:
            _record(s, "delete_product", product_id=product_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        # Remove variants
        for v in p.get("variants", []):
            s["variants"].pop(str(v["id"]), None)
            iid = v.get("inventory_item_id")
            if iid:
                s["inventory_items"].pop(str(iid), None)
                for k in list(s["inventory_levels"].keys()):
                    if k.endswith(f":{iid}"):
                        s["inventory_levels"].pop(k, None)
        del s["products"][str(product_id)]
        _record(s, "delete_product", product_id=product_id)
        _save_state(s)
        return {}


@mcp.tool(name="count_products")
def count_products(vendor: str | None = None,
                   product_type: str | None = None,
                   collection_id: int | None = None,
                   status: str | None = None,
                   created_at_min: str | None = None,
                   created_at_max: str | None = None,
                   updated_at_min: str | None = None,
                   updated_at_max: str | None = None) -> dict:
    """GET /admin/api/{version}/products/count.json — {"count": N}."""
    with _lock():
        s = _load_state()
        items = list(s["products"].values())
        if vendor:
            items = [p for p in items if p.get("vendor") == vendor]
        if product_type:
            items = [p for p in items if p.get("product_type") == product_type]
        if status:
            wanted = {s.strip() for s in status.split(",") if s.strip()}
            items = [p for p in items if p.get("status") in wanted]
        if collection_id is not None:
            col = s["collections"].get(str(collection_id))
            pids = {int(pid) for pid in (col or {}).get("product_ids", [])}
            items = [p for p in items if int(p["id"]) in pids]
        items = _filter_by_date(items, "created_at",
                                created_at_min, created_at_max)
        items = _filter_by_date(items, "updated_at",
                                updated_at_min, updated_at_max)
        _record(s, "count_products", count=len(items))
        _save_state(s)
        return {"count": len(items)}


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

@mcp.tool(name="list_variants")
def list_variants(product_id: int,
                  limit: int = 50,
                  page_info: str | None = None,
                  fields: str | None = None) -> dict:
    """GET /admin/api/{version}/products/{product_id}/variants.json."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(product_id))
        if not p:
            _record(s, "list_variants", product_id=product_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        items = list(p.get("variants", []))
        items.sort(key=lambda v: int(v.get("position", 0)))
        page = _paginate(items, limit, page_info)
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            page = [{k: v for k, v in vv.items() if k in keep} for vv in page]
        _record(s, "list_variants", product_id=product_id, count=len(page))
        _save_state(s)
        return {"variants": page}


@mcp.tool(name="get_variant")
def get_variant(variant_id: int, fields: str | None = None) -> dict:
    """GET /admin/api/{version}/variants/{variant_id}.json."""
    with _lock():
        s = _load_state()
        v = s["variants"].get(str(variant_id))
        _record(s, "get_variant", variant_id=variant_id,
                result="ok" if v else "not_found")
        _save_state(s)
        if not v:
            return _err_not_found()
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            v = {k: val for k, val in v.items() if k in keep}
        return {"variant": v}


@mcp.tool(name="create_variant")
def create_variant(product_id: int, variant: dict) -> dict:
    """POST /admin/api/{version}/products/{product_id}/variants.json.
    Body: {"variant": {...}}.
    """
    with _lock():
        s = _load_state()
        p = s["products"].get(str(product_id))
        if not p:
            _record(s, "create_variant", product_id=product_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        if not isinstance(variant, dict):
            _record(s, "create_variant", product_id=product_id,
                    result="invalid")
            _save_state(s)
            return _err_fields({"variant": ["must be a hash"]})
        if not variant.get("option1") and not variant.get("title"):
            _record(s, "create_variant", product_id=product_id,
                    result="missing_option1")
            _save_state(s)
            return _err_fields({"option1": ["can't be blank"]})
        position = len(p.get("variants", [])) + 1
        v = _new_variant(s, p["id"], variant, position)
        p.setdefault("variants", []).append(v)
        p["updated_at"] = _now()
        _record(s, "create_variant", product_id=p["id"], variant_id=v["id"])
        _save_state(s)
        return {"variant": v}


@mcp.tool(name="update_variant")
def update_variant(variant_id: int, variant: dict) -> dict:
    """PUT /admin/api/{version}/variants/{variant_id}.json."""
    with _lock():
        s = _load_state()
        v = s["variants"].get(str(variant_id))
        if not v:
            _record(s, "update_variant", variant_id=variant_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        for k, val in (variant or {}).items():
            if k == "id":
                continue
            if k == "price" and val is not None:
                v["price"] = f"{_money(val):.2f}"
            else:
                v[k] = val
        v["updated_at"] = _now()
        # Mirror change into the product's embedded variants list
        p = s["products"].get(str(v["product_id"]))
        if p:
            for i, pv in enumerate(p.get("variants", [])):
                if int(pv["id"]) == int(v["id"]):
                    p["variants"][i] = v
                    break
            p["updated_at"] = v["updated_at"]
        _record(s, "update_variant", variant_id=variant_id,
                fields=list((variant or {}).keys()))
        _save_state(s)
        return {"variant": v}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

_ORDER_STATUS_VALUES = {"open", "closed", "cancelled", "any"}


@mcp.tool(name="list_orders")
def list_orders(limit: int = 50,
                page_info: str | None = None,
                ids: str | None = None,
                status: str = "open",
                financial_status: str | None = None,
                fulfillment_status: str | None = None,
                name: str | None = None,
                email: str | None = None,
                customer_id: int | None = None,
                created_at_min: str | None = None,
                created_at_max: str | None = None,
                updated_at_min: str | None = None,
                updated_at_max: str | None = None,
                processed_at_min: str | None = None,
                processed_at_max: str | None = None,
                fields: str | None = None) -> dict:
    """GET /admin/api/{version}/orders.json — list orders.

    `status` selects by lifecycle: open (default), closed, cancelled,
    any. `financial_status` and `fulfillment_status` are independent
    filters (Shopify treats "unfulfilled" as fulfillment_status IS
    NULL).
    """
    with _lock():
        s = _load_state()
        items = list(s["orders"].values())
        items = _filter_by_ids(items, ids)
        st = status or "open"
        if st not in _ORDER_STATUS_VALUES:
            _record(s, "list_orders", result="bad_status", status=st)
            _save_state(s)
            return _err_fields({"status": ["is not included in the list"]})
        if st != "any":
            if st == "open":
                items = [o for o in items
                         if not o.get("closed_at") and not o.get("cancelled_at")]
            elif st == "closed":
                items = [o for o in items if o.get("closed_at")]
            elif st == "cancelled":
                items = [o for o in items if o.get("cancelled_at")]
        if financial_status:
            wanted = {x.strip() for x in financial_status.split(",")
                      if x.strip()}
            items = [o for o in items if o.get("financial_status") in wanted]
        if fulfillment_status:
            wanted = {x.strip() for x in fulfillment_status.split(",")
                      if x.strip()}
            picked = []
            for o in items:
                fs = o.get("fulfillment_status")
                if "unfulfilled" in wanted and fs is None:
                    picked.append(o)
                elif fs in wanted:
                    picked.append(o)
            items = picked
        if name:
            items = [o for o in items if (o.get("name") or "") == name]
        if email:
            items = [o for o in items
                     if (o.get("email") or "").lower() == email.lower()]
        if customer_id is not None:
            items = [o for o in items
                     if (o.get("customer") or {}).get("id") == customer_id]
        items = _filter_by_date(items, "created_at",
                                created_at_min, created_at_max)
        items = _filter_by_date(items, "updated_at",
                                updated_at_min, updated_at_max)
        items = _filter_by_date(items, "processed_at",
                                processed_at_min, processed_at_max)
        items.sort(key=lambda o: int(o["id"]))
        page = _paginate(items, limit, page_info)
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            page = [{k: v for k, v in o.items() if k in keep} for o in page]
        _record(s, "list_orders", count=len(page), status=st,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status)
        _save_state(s)
        return {"orders": page}


@mcp.tool(name="get_order")
def get_order(order_id: int, fields: str | None = None) -> dict:
    """GET /admin/api/{version}/orders/{order_id}.json."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(order_id))
        _record(s, "get_order", order_id=order_id,
                result="ok" if o else "not_found")
        _save_state(s)
        if not o:
            return _err_not_found()
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            o = {k: v for k, v in o.items() if k in keep}
        return {"order": o}


@mcp.tool(name="create_order")
def create_order(order: dict) -> dict:
    """POST /admin/api/{version}/orders.json. Body: {"order": {...}}.

    `line_items` is required (Shopify requires at least one). Each
    line item should be {"variant_id": <id>, "quantity": <n>} (or
    can include a free-form `title` + `price` for custom items).
    """
    with _lock():
        s = _load_state()
        if not isinstance(order, dict):
            _record(s, "create_order", result="invalid")
            _save_state(s)
            return _err_fields({"order": ["must be a hash"]})
        if not order.get("line_items"):
            _record(s, "create_order", result="missing_line_items")
            _save_state(s)
            return _err_fields({"line_items":
                                ["must have at least one line item"]})
        fin = order.get("financial_status", "pending")
        if fin not in ORDER_FINANCIAL:
            _record(s, "create_order", result="bad_financial_status")
            _save_state(s)
            return _err_fields({"financial_status":
                                ["is not included in the list"]})
        o = _new_order(s, order)
        _record(s, "create_order", order_id=o["id"],
                financial_status=o["financial_status"],
                line_items=len(o["line_items"]))
        _save_state(s)
        return {"order": o}


@mcp.tool(name="update_order")
def update_order(order_id: int, order: dict) -> dict:
    """PUT /admin/api/{version}/orders/{order_id}.json. Body:
    {"order": {...}}. Common fields: `note`, `email`, `tags`,
    `note_attributes`, `shipping_address`, `billing_address`."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(order_id))
        if not o:
            _record(s, "update_order", order_id=order_id, result="not_found")
            _save_state(s)
            return _err_not_found()
        data = dict(order or {})
        data.pop("id", None)
        if (fin := data.get("financial_status")) and fin not in ORDER_FINANCIAL:
            _record(s, "update_order", order_id=order_id,
                    result="bad_financial_status")
            _save_state(s)
            return _err_fields({"financial_status":
                                ["is not included in the list"]})
        if "tags" in data and isinstance(data["tags"], list):
            data["tags"] = ", ".join(str(t) for t in data["tags"])
        for k, v in data.items():
            o[k] = v
        o["updated_at"] = _now()
        _record(s, "update_order", order_id=order_id,
                fields=list((order or {}).keys()))
        _save_state(s)
        return {"order": o}


@mcp.tool(name="cancel_order")
def cancel_order(order_id: int,
                 reason: str | None = None,
                 email: bool | None = None,
                 refund: dict | None = None,
                 restock: bool | None = None) -> dict:
    """POST /admin/api/{version}/orders/{order_id}/cancel.json.

    `reason` is one of {customer, declined, fraud, inventory, other};
    if invalid Shopify returns a 422. `restock` re-credits inventory
    for tracked variants. Cancelling an already-cancelled order
    returns {"errors": "Order has already been canceled."}."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(order_id))
        if not o:
            _record(s, "cancel_order", order_id=order_id, result="not_found")
            _save_state(s)
            return _err_not_found()
        if o.get("cancelled_at"):
            _record(s, "cancel_order", order_id=order_id,
                    result="already_cancelled")
            _save_state(s)
            return _err_string("Order has already been canceled.")
        valid_reasons = {"customer", "declined", "fraud", "inventory",
                         "other", None}
        if reason not in valid_reasons:
            _record(s, "cancel_order", order_id=order_id,
                    result="bad_reason")
            _save_state(s)
            return _err_fields({"reason":
                                ["is not included in the list"]})
        now = _now()
        o["cancelled_at"] = now
        o["cancel_reason"] = reason
        o["financial_status"] = ("refunded" if refund or
                                 o.get("financial_status") in ("paid",
                                                                "partially_paid")
                                 else o.get("financial_status"))
        o["updated_at"] = now
        if restock:
            for li in o.get("line_items", []):
                vid = li.get("variant_id")
                if not vid:
                    continue
                v = s["variants"].get(str(vid))
                if v and v.get("inventory_management") == "shopify":
                    v["inventory_quantity"] = (
                        int(v.get("inventory_quantity", 0))
                        + int(li.get("quantity", 0)))
                    v["updated_at"] = now
        _record(s, "cancel_order", order_id=order_id,
                reason=reason, restock=bool(restock))
        _save_state(s)
        return {"order": o}


@mcp.tool(name="close_order")
def close_order(order_id: int) -> dict:
    """POST /admin/api/{version}/orders/{order_id}/close.json.

    Marks an order as archived/closed. Idempotent — closing a
    closed order is a no-op and returns the order."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(order_id))
        if not o:
            _record(s, "close_order", order_id=order_id, result="not_found")
            _save_state(s)
            return _err_not_found()
        if not o.get("closed_at"):
            o["closed_at"] = _now()
            o["updated_at"] = o["closed_at"]
        _record(s, "close_order", order_id=order_id)
        _save_state(s)
        return {"order": o}


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@mcp.tool(name="list_customers")
def list_customers(limit: int = 50,
                   page_info: str | None = None,
                   ids: str | None = None,
                   query: str | None = None,
                   created_at_min: str | None = None,
                   created_at_max: str | None = None,
                   updated_at_min: str | None = None,
                   updated_at_max: str | None = None,
                   fields: str | None = None) -> dict:
    """GET /admin/api/{version}/customers.json. The real endpoint
    supports a Shopify-search `query` parameter
    (`first_name:Jane email:jane@example.com`); we simplify it to a
    free-text substring match over name + email."""
    with _lock():
        s = _load_state()
        items = list(s["customers"].values())
        items = _filter_by_ids(items, ids)
        if query:
            q = query.lower()
            items = [c for c in items
                     if q in (c.get("email") or "").lower()
                     or q in (c.get("first_name") or "").lower()
                     or q in (c.get("last_name") or "").lower()
                     or q in (c.get("phone") or "").lower()
                     or q in (c.get("tags") or "").lower()]
        items = _filter_by_date(items, "created_at",
                                created_at_min, created_at_max)
        items = _filter_by_date(items, "updated_at",
                                updated_at_min, updated_at_max)
        items.sort(key=lambda c: int(c["id"]))
        page = _paginate(items, limit, page_info)
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            page = [{k: v for k, v in c.items() if k in keep} for c in page]
        _record(s, "list_customers", count=len(page), query=query)
        _save_state(s)
        return {"customers": page}


@mcp.tool(name="get_customer")
def get_customer(customer_id: int, fields: str | None = None) -> dict:
    """GET /admin/api/{version}/customers/{customer_id}.json."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(str(customer_id))
        _record(s, "get_customer", customer_id=customer_id,
                result="ok" if c else "not_found")
        _save_state(s)
        if not c:
            return _err_not_found()
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            c = {k: v for k, v in c.items() if k in keep}
        return {"customer": c}


@mcp.tool(name="create_customer")
def create_customer(customer: dict) -> dict:
    """POST /admin/api/{version}/customers.json. Required: `email`
    OR `phone` (Shopify allows either). Email uniqueness is enforced
    with `{"email": ["has already been taken"]}`."""
    with _lock():
        s = _load_state()
        if not isinstance(customer, dict):
            _record(s, "create_customer", result="invalid")
            _save_state(s)
            return _err_fields({"customer": ["must be a hash"]})
        em = customer.get("email")
        ph = customer.get("phone")
        if not em and not ph:
            _record(s, "create_customer", result="missing_identifier")
            _save_state(s)
            return _err_fields({"base":
                                ["Email or phone is required"]})
        if em:
            for c in s["customers"].values():
                if (c.get("email") or "").lower() == em.lower():
                    _record(s, "create_customer", result="email_taken")
                    _save_state(s)
                    return _err_fields({"email":
                                        ["has already been taken"]})
        c = _new_customer(s, customer)
        _record(s, "create_customer", customer_id=c["id"], email=em)
        _save_state(s)
        return {"customer": c}


@mcp.tool(name="update_customer")
def update_customer(customer_id: int, customer: dict) -> dict:
    """PUT /admin/api/{version}/customers/{customer_id}.json."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(str(customer_id))
        if not c:
            _record(s, "update_customer", customer_id=customer_id,
                    result="not_found")
            _save_state(s)
            return _err_not_found()
        data = dict(customer or {})
        data.pop("id", None)
        new_email = data.get("email")
        if new_email and new_email.lower() != (c.get("email") or "").lower():
            for other in s["customers"].values():
                if (other["id"] != c["id"]
                        and (other.get("email") or "").lower()
                        == new_email.lower()):
                    _record(s, "update_customer", customer_id=customer_id,
                            result="email_taken")
                    _save_state(s)
                    return _err_fields({"email":
                                        ["has already been taken"]})
        if "tags" in data and isinstance(data["tags"], list):
            data["tags"] = ", ".join(str(t) for t in data["tags"])
        for k, v in data.items():
            c[k] = v
        c["updated_at"] = _now()
        _record(s, "update_customer", customer_id=customer_id,
                fields=list((customer or {}).keys()))
        _save_state(s)
        return {"customer": c}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _level_key(location_id: int, inventory_item_id: int) -> str:
    return f"{int(location_id)}:{int(inventory_item_id)}"


@mcp.tool(name="get_inventory_level")
def get_inventory_level(inventory_item_id: int,
                        location_id: int) -> dict:
    """GET /admin/api/{version}/inventory_levels.json
    ?inventory_item_ids={id}&location_ids={loc}.

    Mirrors the real endpoint by returning a list under
    `inventory_levels`. A missing level returns an empty list (not
    a 404), matching Shopify's behavior."""
    with _lock():
        s = _load_state()
        key = _level_key(location_id, inventory_item_id)
        lvl = s["inventory_levels"].get(key)
        levels = [lvl] if lvl else []
        _record(s, "get_inventory_level",
                inventory_item_id=inventory_item_id,
                location_id=location_id,
                result="ok" if lvl else "not_found")
        _save_state(s)
        return {"inventory_levels": levels}


@mcp.tool(name="adjust_inventory_level")
def adjust_inventory_level(inventory_item_id: int,
                           location_id: int,
                           available_adjustment: int) -> dict:
    """POST /admin/api/{version}/inventory_levels/adjust.json. Body:
    {"location_id": L, "inventory_item_id": I, "available_adjustment": N}.
    Returns {"inventory_level": {...}}.

    Creates the level row if it doesn't yet exist. Also keeps the
    related variant's `inventory_quantity` in sync.
    """
    with _lock():
        s = _load_state()
        if str(inventory_item_id) not in s["inventory_items"]:
            _record(s, "adjust_inventory_level",
                    inventory_item_id=inventory_item_id,
                    result="not_found")
            _save_state(s)
            return _err_fields({"inventory_item_id":
                                [f"doesn't exist"]})
        if str(location_id) not in s["locations"]:
            # Auto-provision: tests don't always seed locations
            s["locations"][str(location_id)] = {
                "id": int(location_id),
                "name": f"Location {location_id}",
                "active": True,
                "created_at": _now(),
                "updated_at": _now(),
            }
        key = _level_key(location_id, inventory_item_id)
        lvl = s["inventory_levels"].get(key)
        if not lvl:
            lvl = {
                "inventory_item_id": int(inventory_item_id),
                "location_id": int(location_id),
                "available": 0,
                "updated_at": _now(),
                "admin_graphql_api_id":
                    (f"gid://shopify/InventoryLevel/"
                     f"{location_id}?inventory_item_id={inventory_item_id}"),
            }
            s["inventory_levels"][key] = lvl
        lvl["available"] = int(lvl.get("available", 0)) + int(available_adjustment)
        lvl["updated_at"] = _now()
        # Sync the variant's denormalized count
        for v in s["variants"].values():
            if int(v.get("inventory_item_id", 0)) == int(inventory_item_id):
                v["inventory_quantity"] = (
                    int(v.get("inventory_quantity", 0))
                    + int(available_adjustment))
                v["updated_at"] = lvl["updated_at"]
                p = s["products"].get(str(v["product_id"]))
                if p:
                    for i, pv in enumerate(p.get("variants", [])):
                        if int(pv["id"]) == int(v["id"]):
                            p["variants"][i] = v
                            break
                break
        _record(s, "adjust_inventory_level",
                inventory_item_id=inventory_item_id,
                location_id=location_id,
                delta=available_adjustment,
                available=lvl["available"])
        _save_state(s)
        return {"inventory_level": lvl}


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@mcp.tool(name="list_collections")
def list_collections(limit: int = 50,
                     page_info: str | None = None,
                     ids: str | None = None,
                     title: str | None = None,
                     handle: str | None = None,
                     product_id: int | None = None,
                     fields: str | None = None) -> dict:
    """GET /admin/api/{version}/collections.json. The real Admin API
    splits collections into `custom_collections` and
    `smart_collections`; this mock returns the union under
    `collections` to keep the tool surface compact (callers can
    distinguish via the `collection_type` field on each item)."""
    with _lock():
        s = _load_state()
        items = list(s["collections"].values())
        items = _filter_by_ids(items, ids)
        if title:
            items = [c for c in items
                     if title.lower() in (c.get("title") or "").lower()]
        if handle:
            items = [c for c in items if c.get("handle") == handle]
        if product_id is not None:
            items = [c for c in items
                     if int(product_id) in [int(p) for p in
                                            c.get("product_ids", [])]]
        items.sort(key=lambda c: int(c["id"]))
        page = _paginate(items, limit, page_info)
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            page = [{k: v for k, v in c.items() if k in keep} for c in page]
        _record(s, "list_collections", count=len(page))
        _save_state(s)
        return {"collections": page}


@mcp.tool(name="get_collection")
def get_collection(collection_id: int,
                   fields: str | None = None) -> dict:
    """GET /admin/api/{version}/collections/{collection_id}.json."""
    with _lock():
        s = _load_state()
        c = s["collections"].get(str(collection_id))
        _record(s, "get_collection", collection_id=collection_id,
                result="ok" if c else "not_found")
        _save_state(s)
        if not c:
            return _err_not_found()
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            c = {k: v for k, v in c.items() if k in keep}
        return {"collection": c}


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------

@mcp.tool(name="get_shop")
def get_shop(fields: str | None = None) -> dict:
    """GET /admin/api/{version}/shop.json — returns the singleton
    Shop object for the authenticated store."""
    with _lock():
        s = _load_state()
        shop = dict(s["shop"])
        if fields:
            keep = {f.strip() for f in fields.split(",") if f.strip()}
            shop = {k: v for k, v in shop.items() if k in keep}
        _record(s, "get_shop")
        _save_state(s)
        return {"shop": shop}


# ---------------------------------------------------------------------------
# Mock-only debug helpers (not part of the real Admin REST surface)
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state dict — for verifier
    introspection. Not part of the real Shopify API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(shop: dict | None = None,
                    products: list | None = None,
                    customers: list | None = None,
                    orders: list | None = None,
                    collections: list | None = None,
                    locations: list | None = None,
                    inventory_levels: list | None = None,
                    replace: bool = False) -> dict:
    """Seed mock state. Each collection holds Shopify-shaped dicts:

      - `products`:        [{title, body_html?, vendor?, product_type?,
                             status?, tags?, options?, variants?, images?}]
      - `customers`:       [{email, first_name?, last_name?, phone?,
                             addresses?, tags?, ...}]
      - `orders`:          [{line_items: [{variant_id, quantity, ...}],
                             customer_id?|customer?, financial_status?,
                             email?, billing_address?, shipping_address?}]
      - `collections`:     [{title, handle?, body_html?, product_ids?,
                             collection_type?}]
      - `locations`:       [{id?, name, address1?, city?, country?}]
      - `inventory_levels`:[{inventory_item_id, location_id, available}]

    If `replace=True`, state is fully reset before seeding. Returns
    the assigned ids for each collection."""
    with _lock():
        s = _empty_state() if replace else _load_state()
        if shop:
            s["shop"].update(shop)
            s["shop"]["updated_at"] = _now()
        product_ids = []
        for p_in in products or []:
            p = _new_product(s, p_in)
            product_ids.append(p["id"])
        customer_ids = []
        for c_in in customers or []:
            # Allow caller to pre-assign id
            if c_in.get("id"):
                cid = int(c_in["id"])
                now = _now()
                cust = {
                    "id": cid,
                    "email": c_in.get("email"),
                    "first_name": c_in.get("first_name", ""),
                    "last_name": c_in.get("last_name", ""),
                    "phone": c_in.get("phone"),
                    "tags": c_in.get("tags", ""),
                    "accepts_marketing": bool(c_in.get("accepts_marketing")),
                    "state": c_in.get("state", "disabled"),
                    "orders_count": 0, "total_spent": "0.00",
                    "addresses": c_in.get("addresses", []),
                    "default_address": (c_in.get("addresses") or [None])[0],
                    "verified_email": True,
                    "created_at": now, "updated_at": now,
                    "admin_graphql_api_id": f"gid://shopify/Customer/{cid}",
                    "currency": "USD",
                }
                s["customers"][str(cid)] = cust
                if cid >= s["next_id"]["customer"]:
                    s["next_id"]["customer"] = cid + 1
                customer_ids.append(cid)
            else:
                customer_ids.append(_new_customer(s, c_in)["id"])
        collection_ids = []
        for c_in in collections or []:
            cid = c_in.get("id") or _next_id(s, "collection")
            now = _now()
            title = c_in.get("title", f"Collection {cid}")
            col = {
                "id": int(cid),
                "title": title,
                "handle": c_in.get("handle") or _handle(title),
                "body_html": c_in.get("body_html", ""),
                "published_at": now,
                "sort_order": c_in.get("sort_order", "best-selling"),
                "template_suffix": None,
                "products_count": len(c_in.get("product_ids", [])),
                "collection_type": c_in.get("collection_type", "custom"),
                "updated_at": now,
                "published_scope": "web",
                "product_ids": list(c_in.get("product_ids", [])),
                "admin_graphql_api_id": f"gid://shopify/Collection/{cid}",
            }
            s["collections"][str(cid)] = col
            collection_ids.append(int(cid))
        for loc in locations or []:
            lid = loc.get("id") or _next_id(s, "location")
            s["locations"][str(lid)] = {
                "id": int(lid),
                "name": loc.get("name", f"Location {lid}"),
                "address1": loc.get("address1", ""),
                "city": loc.get("city", ""),
                "country": loc.get("country", "US"),
                "country_code": loc.get("country_code", "US"),
                "active": loc.get("active", True),
                "created_at": _now(),
                "updated_at": _now(),
                "admin_graphql_api_id": f"gid://shopify/Location/{lid}",
            }
        for ilv in inventory_levels or []:
            iid = ilv["inventory_item_id"]
            loc = ilv["location_id"]
            key = _level_key(loc, iid)
            s["inventory_levels"][key] = {
                "inventory_item_id": int(iid),
                "location_id": int(loc),
                "available": int(ilv.get("available", 0)),
                "updated_at": _now(),
                "admin_graphql_api_id":
                    (f"gid://shopify/InventoryLevel/"
                     f"{loc}?inventory_item_id={iid}"),
            }
        order_ids = []
        for o_in in orders or []:
            order_ids.append(_new_order(s, o_in)["id"])
        _record(s, "debug_seed",
                counts={"products": len(products or []),
                        "customers": len(customers or []),
                        "orders": len(orders or []),
                        "collections": len(collections or []),
                        "locations": len(locations or []),
                        "inventory_levels": len(inventory_levels or [])},
                replace=replace)
        _save_state(s)
        return {"ok": True,
                "product_ids": product_ids,
                "customer_ids": customer_ids,
                "order_ids": order_ids,
                "collection_ids": collection_ids}


if __name__ == "__main__":
    mcp.run()
