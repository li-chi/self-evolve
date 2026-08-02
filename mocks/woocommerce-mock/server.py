"""WooCommerce mock MCP server.

Mirrors the tool surface of `@lockon0927/woocommerce-mcp` (Toolathlon's
official woocommerce server, source: github.com/lockon-n/woocommerce-mcp).
That server proxies the WooCommerce REST API v3, so every tool here
accepts the same arguments and returns the same JSON shape as the
underlying WC endpoint.

Implemented subset (24 tools, covers all 9 Toolathlon `woocommerce`
tasks):

  Products    woo_products_list, woo_products_get, woo_products_create,
              woo_products_update, woo_products_delete,
              woo_products_batch_update,
              woo_products_categories_list, woo_products_categories_create,
              woo_products_tags_list, woo_products_reviews_list,
              woo_products_variations_list
  Orders      woo_orders_list, woo_orders_get, woo_orders_create,
              woo_orders_update, woo_orders_delete,
              woo_orders_batch_update, woo_orders_notes_create,
              woo_orders_refunds_create
  Customers   woo_customers_list, woo_customers_get,
              woo_customers_create, woo_customers_update
  Reports     woo_reports_sales, woo_reports_top_sellers,
              woo_reports_low_stock

Skipped in v1 (not used by any Toolathlon task): coupons, shipping
zones, tax classes/rates, payment gateways, webhooks, settings,
system_status. Add as needed.

State is a single JSON file at `$WC_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/woocommerce_mock`). Every mutating call appends
to `state["calls"]` for verifier consumption.
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


PRODUCT_STATUSES = {"publish", "draft", "private", "pending"}
ORDER_STATUSES = {"pending", "processing", "on-hold", "completed",
                  "cancelled", "refunded", "failed", "trash"}
STOCK_STATUSES = {"instock", "outofstock", "onbackorder"}


def _state_path() -> str:
    state_dir = os.environ.get(
        "WC_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/woocommerce_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", ""))


def _empty_state() -> dict:
    return {
        "products": {},
        "orders": {},
        "customers": {},
        "categories": {},
        "tags": {},
        "reviews": {},
        "next_id": {"product": 1, "order": 1, "customer": 1,
                    "category": 1, "tag": 1, "review": 1, "note": 1,
                    "refund": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("WC_MOCK_SEED_PATH")
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


def _err(code: str, message: str, status: int = 400) -> dict:
    """WC REST error body shape: {code, message, data:{status}}."""
    return {"code": code, "message": message, "data": {"status": status}}


def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _paginate(items: list, page: int, per_page: int) -> list:
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 10), 1), 100)
    start = (page - 1) * per_page
    return items[start: start + per_page]


def _sort(items: list, orderby: str | None, order: str | None) -> list:
    if not orderby:
        return items
    reverse = (order or "desc").lower() == "desc"

    def key(it):
        v = it.get(orderby)
        return (v is None, v if v is not None else 0)

    return sorted(items, key=key, reverse=reverse)


mcp = FastMCP("woocommerce-mock")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def _new_product(state: dict, data: dict) -> dict:
    pid = _next_id(state, "product")
    now = _now()
    product = {
        "id": pid,
        "name": data.get("name", ""),
        "slug": data.get("slug") or _slugify(data.get("name", f"product-{pid}")),
        "permalink": f"https://shop.mock/?p={pid}",
        # WC REST v3 honours a caller-supplied date_created (upstream
        # preprocess backdates products this way); default to now.
        "date_created": data.get("date_created") or now,
        "date_modified": data.get("date_modified")
                         or data.get("date_created") or now,
        "type": data.get("type", "simple"),
        "status": data.get("status", "publish"),
        "featured": bool(data.get("featured", False)),
        "catalog_visibility": data.get("catalog_visibility", "visible"),
        "description": data.get("description", ""),
        "short_description": data.get("short_description", ""),
        "sku": data.get("sku", ""),
        "price": data.get("regular_price") or data.get("price") or "",
        "regular_price": data.get("regular_price", ""),
        "sale_price": data.get("sale_price", ""),
        "on_sale": bool(data.get("sale_price")),
        "purchasable": True,
        "total_sales": 0,
        "manage_stock": bool(data.get("manage_stock", False)),
        "stock_quantity": data.get("stock_quantity"),
        "stock_status": data.get("stock_status",
                                 "instock" if (data.get("stock_quantity") is None
                                               or (data.get("stock_quantity") or 0) > 0)
                                 else "outofstock"),
        "backorders": data.get("backorders", "no"),
        "weight": data.get("weight", ""),
        "dimensions": data.get("dimensions", {"length": "", "width": "", "height": ""}),
        "categories": data.get("categories", []),
        "tags": data.get("tags", []),
        "images": data.get("images", []),
        "attributes": data.get("attributes", []),
        "variations": data.get("variations", []),
        "average_rating": "0.00",
        "rating_count": 0,
        "meta_data": data.get("meta_data", []),
    }
    state["products"][str(pid)] = product
    return product


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-",
                  (s or "").lower()).strip("-") or "item"


@mcp.tool(name="woo_products_list")
def woo_products_list(perPage: int = 10, page: int = 1,
                      search: str | None = None,
                      status: str | None = None,
                      category: str | None = None,
                      tag: str | None = None,
                      sku: str | None = None,
                      featured: bool | None = None,
                      onSale: bool | None = None,
                      minPrice: str | None = None,
                      maxPrice: str | None = None,
                      stockStatus: str | None = None,
                      orderby: str | None = None,
                      order: str | None = None) -> list:
    """WC REST: GET /wp-json/wc/v3/products — list/search products."""
    with _lock():
        s = _load_state()
        items = list(s["products"].values())
        if search:
            q = search.lower()
            items = [i for i in items
                     if q in (i["name"] or "").lower()
                     or q in (i["sku"] or "").lower()
                     or q in (i["description"] or "").lower()]
        if status:
            items = [i for i in items if i["status"] == status]
        if sku:
            items = [i for i in items if i["sku"] == sku]
        if category:
            items = [i for i in items
                     if any(str(c.get("id")) == str(category)
                            for c in i.get("categories", []))]
        if tag:
            items = [i for i in items
                     if any(str(t.get("id")) == str(tag)
                            for t in i.get("tags", []))]
        if featured is not None:
            items = [i for i in items if bool(i["featured"]) == bool(featured)]
        if onSale is not None:
            items = [i for i in items if bool(i["on_sale"]) == bool(onSale)]
        if stockStatus:
            items = [i for i in items if i["stock_status"] == stockStatus]
        if minPrice:
            items = [i for i in items if _money(i["price"]) >= _money(minPrice)]
        if maxPrice:
            items = [i for i in items if _money(i["price"]) <= _money(maxPrice)]
        items = _sort(items, orderby, order)
        page_items = _paginate(items, page, perPage)
        _record(s, "products_list", count=len(page_items),
                search=search, status=status)
        _save_state(s)
        return page_items


def _money(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@mcp.tool(name="woo_products_get")
def woo_products_get(productId: int) -> dict:
    """WC REST: GET /wp-json/wc/v3/products/{id}."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(productId))
        _record(s, "products_get", product_id=productId,
                result="ok" if p else "not_found")
        _save_state(s)
        if not p:
            return _err("woocommerce_rest_product_invalid_id",
                        "Invalid ID.", status=404)
        return p


@mcp.tool(name="woo_products_create")
def woo_products_create(productData: dict) -> dict:
    """WC REST: POST /wp-json/wc/v3/products — create a product."""
    with _lock():
        s = _load_state()
        if not productData.get("name"):
            return _err("rest_missing_callback_param",
                        "Missing parameter(s): name")
        if (st := productData.get("status")) and st not in PRODUCT_STATUSES:
            return _err("rest_invalid_param",
                        f"Invalid status: {st!r}")
        p = _new_product(s, productData)
        _record(s, "products_create", product_id=p["id"],
                name=p["name"], sku=p["sku"])
        _save_state(s)
        return p


@mcp.tool(name="woo_products_update")
def woo_products_update(productId: int, productData: dict) -> dict:
    """WC REST: PUT /wp-json/wc/v3/products/{id} — update a product."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(productId))
        if not p:
            _record(s, "products_update", product_id=productId,
                    result="not_found")
            _save_state(s)
            return _err("woocommerce_rest_product_invalid_id",
                        "Invalid ID.", status=404)
        for k, v in (productData or {}).items():
            if k == "regular_price":
                p["regular_price"] = v
                if not p.get("sale_price"):
                    p["price"] = v
            elif k == "sale_price":
                p["sale_price"] = v
                p["on_sale"] = bool(v)
                if v:
                    p["price"] = v
            elif k == "stock_quantity":
                p["stock_quantity"] = v
                p["stock_status"] = ("instock" if (v or 0) > 0
                                     else "outofstock")
            else:
                p[k] = v
        p["date_modified"] = _now()
        _record(s, "products_update", product_id=productId,
                fields=list((productData or {}).keys()))
        _save_state(s)
        return p


@mcp.tool(name="woo_products_delete")
def woo_products_delete(productId: int, force: bool = False) -> dict:
    """WC REST: DELETE /wp-json/wc/v3/products/{id}. `force=True`
    hard-deletes; otherwise the product is moved to trash."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(productId))
        if not p:
            _record(s, "products_delete", product_id=productId,
                    result="not_found")
            _save_state(s)
            return _err("woocommerce_rest_product_invalid_id",
                        "Invalid ID.", status=404)
        if force:
            del s["products"][str(productId)]
            p["deleted"] = True
        else:
            p["status"] = "trash"
        _record(s, "products_delete", product_id=productId, force=force)
        _save_state(s)
        return p


@mcp.tool(name="woo_products_batch_update")
def woo_products_batch_update(create: list | None = None,
                              update: list | None = None,
                              delete: list | None = None) -> dict:
    """WC REST: POST /wp-json/wc/v3/products/batch."""
    with _lock():
        s = _load_state()
        created = []
        for data in (create or []):
            if not data.get("name"):
                continue
            created.append(_new_product(s, data))
        updated = []
        for entry in (update or []):
            pid = entry.get("id")
            if pid is None:
                continue
            p = s["products"].get(str(pid))
            if not p:
                continue
            for k, v in entry.items():
                if k == "id":
                    continue
                p[k] = v
            p["date_modified"] = _now()
            updated.append(p)
        deleted = []
        for pid in (delete or []):
            p = s["products"].pop(str(pid), None)
            if p:
                p["deleted"] = True
                deleted.append(p)
        _record(s, "products_batch_update",
                created=len(created), updated=len(updated),
                deleted=len(deleted))
        _save_state(s)
        return {"create": created, "update": updated, "delete": deleted}


# Categories / Tags / Reviews / Variations -----------------------------------

@mcp.tool(name="woo_products_categories_list")
def woo_products_categories_list(perPage: int = 10, page: int = 1,
                                 search: str | None = None) -> list:
    """WC REST: GET /wp-json/wc/v3/products/categories."""
    with _lock():
        s = _load_state()
        items = list(s["categories"].values())
        if search:
            q = search.lower()
            items = [c for c in items if q in (c["name"] or "").lower()]
        page_items = _paginate(items, page, perPage)
        _record(s, "categories_list", count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="woo_products_categories_create")
def woo_products_categories_create(name: str,
                                   slug: str | None = None,
                                   parent: int = 0,
                                   description: str = "") -> dict:
    """WC REST: POST /wp-json/wc/v3/products/categories."""
    with _lock():
        s = _load_state()
        cid = _next_id(s, "category")
        cat = {"id": cid, "name": name,
               "slug": slug or _slugify(name),
               "parent": parent, "description": description, "count": 0}
        s["categories"][str(cid)] = cat
        _record(s, "categories_create", category_id=cid, name=name)
        _save_state(s)
        return cat


@mcp.tool(name="woo_products_tags_list")
def woo_products_tags_list(perPage: int = 10, page: int = 1) -> list:
    """WC REST: GET /wp-json/wc/v3/products/tags."""
    with _lock():
        s = _load_state()
        items = list(s["tags"].values())
        page_items = _paginate(items, page, perPage)
        _record(s, "tags_list", count=len(page_items))
        _save_state(s)
        return page_items


@mcp.tool(name="woo_products_reviews_list")
def woo_products_reviews_list(product: int | None = None,
                              perPage: int = 10, page: int = 1) -> list:
    """WC REST: GET /wp-json/wc/v3/products/reviews."""
    with _lock():
        s = _load_state()
        items = list(s["reviews"].values())
        if product is not None:
            items = [r for r in items if r.get("product_id") == product]
        page_items = _paginate(items, page, perPage)
        _record(s, "reviews_list", count=len(page_items),
                product=product)
        _save_state(s)
        return page_items


@mcp.tool(name="woo_products_variations_list")
def woo_products_variations_list(productId: int,
                                 perPage: int = 10, page: int = 1) -> list:
    """WC REST: GET /wp-json/wc/v3/products/{productId}/variations."""
    with _lock():
        s = _load_state()
        p = s["products"].get(str(productId))
        if not p:
            _record(s, "variations_list", product_id=productId,
                    result="not_found")
            _save_state(s)
            return []
        items = list(p.get("variations", []))
        page_items = _paginate(items, page, perPage)
        _record(s, "variations_list", product_id=productId,
                count=len(page_items))
        _save_state(s)
        return page_items


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _new_order(state: dict, data: dict) -> dict:
    oid = _next_id(state, "order")
    now = _now()
    line_items = []
    total = 0.0
    for li in data.get("line_items", []) or []:
        pid = li.get("product_id")
        qty = int(li.get("quantity", 1))
        p = state["products"].get(str(pid)) if pid is not None else None
        unit = _money(li.get("price") or (p["price"] if p else "0"))
        sub = unit * qty
        line_items.append({
            "id": _next_id(state, "note"),
            "name": (p["name"] if p else li.get("name", "")),
            "product_id": pid, "variation_id": li.get("variation_id", 0),
            "quantity": qty, "sku": (p["sku"] if p else li.get("sku", "")),
            "price": unit, "subtotal": str(sub), "total": str(sub),
            "tax_class": "",
        })
        total += sub
        if p and p.get("manage_stock") and p.get("stock_quantity") is not None:
            p["stock_quantity"] = max(0, int(p["stock_quantity"]) - qty)
            p["stock_status"] = ("instock" if p["stock_quantity"] > 0
                                 else "outofstock")
            p["total_sales"] = int(p.get("total_sales", 0)) + qty
    order = {
        "id": oid,
        "parent_id": 0,
        "number": str(oid),
        "order_key": f"wc_order_mock{oid:05d}",
        "created_via": data.get("created_via", "rest-api"),
        "version": "9.0.0", "status": data.get("status", "pending"),
        "currency": data.get("currency", "USD"),
        # Honour caller-supplied dates (upstream preprocess backdates
        # historical orders); default to now.
        "date_created": data.get("date_created") or now,
        "date_modified": data.get("date_modified")
                         or data.get("date_created") or now,
        "discount_total": "0.00", "discount_tax": "0.00",
        "shipping_total": "0.00", "shipping_tax": "0.00",
        "cart_tax": "0.00", "total": f"{total:.2f}", "total_tax": "0.00",
        "prices_include_tax": False,
        "customer_id": data.get("customer_id", 0),
        "customer_ip_address": "", "customer_user_agent": "",
        "customer_note": data.get("customer_note", ""),
        "billing": data.get("billing", {}),
        "shipping": data.get("shipping", {}),
        "payment_method": data.get("payment_method", ""),
        "payment_method_title": data.get("payment_method_title", ""),
        "transaction_id": "",
        "date_paid": data.get("date_paid"),
        "date_completed": data.get("date_completed"),
        "cart_hash": "",
        "meta_data": data.get("meta_data", []),
        "line_items": line_items,
        "tax_lines": [], "shipping_lines": data.get("shipping_lines", []),
        "fee_lines": [], "coupon_lines": [], "refunds": [],
        "_notes": [],
    }
    state["orders"][str(oid)] = order
    return order


@mcp.tool(name="woo_orders_list")
def woo_orders_list(perPage: int = 10, page: int = 1,
                    search: str | None = None,
                    status: str | None = None,
                    customer: int | None = None,
                    product: int | None = None,
                    after: str | None = None,
                    before: str | None = None,
                    orderby: str | None = "date",
                    order: str | None = "desc") -> list:
    """WC REST: GET /wp-json/wc/v3/orders."""
    with _lock():
        s = _load_state()
        items = list(s["orders"].values())
        if status:
            items = [o for o in items if o["status"] == status]
        if customer is not None:
            items = [o for o in items if o["customer_id"] == customer]
        if product is not None:
            items = [o for o in items
                     if any(li.get("product_id") == product
                            for li in o["line_items"])]
        if after:
            items = [o for o in items if o["date_created"] >= after]
        if before:
            items = [o for o in items if o["date_created"] <= before]
        if search:
            q = search.lower()
            items = [o for o in items
                     if q in o["order_key"].lower()
                     or q in (o.get("billing", {}).get("first_name", "")
                              + " " + o.get("billing", {}).get("last_name", "")
                              ).lower()
                     or q in (o.get("billing", {}).get("email", "")).lower()]
        items = _sort(items, "date_created" if orderby == "date" else orderby,
                      order)
        page_items = _paginate(items, page, perPage)
        _record(s, "orders_list", count=len(page_items),
                status=status, customer=customer)
        _save_state(s)
        return page_items


@mcp.tool(name="woo_orders_get")
def woo_orders_get(orderId: int) -> dict:
    """WC REST: GET /wp-json/wc/v3/orders/{id}."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(orderId))
        _record(s, "orders_get", order_id=orderId,
                result="ok" if o else "not_found")
        _save_state(s)
        if not o:
            return _err("woocommerce_rest_shop_order_invalid_id",
                        "Invalid ID.", status=404)
        return o


@mcp.tool(name="woo_orders_create")
def woo_orders_create(orderData: dict) -> dict:
    """WC REST: POST /wp-json/wc/v3/orders."""
    with _lock():
        s = _load_state()
        st = orderData.get("status", "pending")
        if st not in ORDER_STATUSES:
            return _err("rest_invalid_param", f"Invalid status: {st!r}")
        o = _new_order(s, orderData)
        _record(s, "orders_create", order_id=o["id"], status=o["status"])
        _save_state(s)
        return o


@mcp.tool(name="woo_orders_update")
def woo_orders_update(orderId: int, orderData: dict) -> dict:
    """WC REST: PUT /wp-json/wc/v3/orders/{id}."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(orderId))
        if not o:
            _record(s, "orders_update", order_id=orderId, result="not_found")
            _save_state(s)
            return _err("woocommerce_rest_shop_order_invalid_id",
                        "Invalid ID.", status=404)
        for k, v in (orderData or {}).items():
            if k == "status" and v == "completed" and o["status"] != "completed":
                o["date_completed"] = _now()
            if k == "status" and v not in ORDER_STATUSES:
                return _err("rest_invalid_param", f"Invalid status: {v!r}")
            o[k] = v
        o["date_modified"] = _now()
        _record(s, "orders_update", order_id=orderId,
                fields=list((orderData or {}).keys()))
        _save_state(s)
        return o


@mcp.tool(name="woo_orders_delete")
def woo_orders_delete(orderId: int, force: bool = False) -> dict:
    """WC REST: DELETE /wp-json/wc/v3/orders/{id}."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(orderId))
        if not o:
            _record(s, "orders_delete", order_id=orderId, result="not_found")
            _save_state(s)
            return _err("woocommerce_rest_shop_order_invalid_id",
                        "Invalid ID.", status=404)
        if force:
            del s["orders"][str(orderId)]
            o["deleted"] = True
        else:
            o["status"] = "trash"
        _record(s, "orders_delete", order_id=orderId, force=force)
        _save_state(s)
        return o


@mcp.tool(name="woo_orders_batch_update")
def woo_orders_batch_update(create: list | None = None,
                            update: list | None = None,
                            delete: list | None = None) -> dict:
    """WC REST: POST /wp-json/wc/v3/orders/batch."""
    with _lock():
        s = _load_state()
        created = [_new_order(s, d) for d in (create or [])]
        updated = []
        for entry in (update or []):
            oid = entry.get("id")
            o = s["orders"].get(str(oid)) if oid else None
            if not o:
                continue
            for k, v in entry.items():
                if k == "id":
                    continue
                o[k] = v
            o["date_modified"] = _now()
            updated.append(o)
        deleted = []
        for oid in (delete or []):
            o = s["orders"].pop(str(oid), None)
            if o:
                o["deleted"] = True
                deleted.append(o)
        _record(s, "orders_batch_update",
                created=len(created), updated=len(updated),
                deleted=len(deleted))
        _save_state(s)
        return {"create": created, "update": updated, "delete": deleted}


@mcp.tool(name="woo_orders_notes_create")
def woo_orders_notes_create(orderId: int,
                            note: str,
                            customer_note: bool = False,
                            added_by_user: bool = False) -> dict:
    """WC REST: POST /wp-json/wc/v3/orders/{orderId}/notes."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(orderId))
        if not o:
            return _err("woocommerce_rest_shop_order_invalid_id",
                        "Invalid ID.", status=404)
        nid = _next_id(s, "note")
        n = {"id": nid, "author": "system",
             "date_created": _now(), "note": note,
             "customer_note": bool(customer_note),
             "added_by_user": bool(added_by_user)}
        o["_notes"].append(n)
        _record(s, "orders_notes_create", order_id=orderId, note_id=nid)
        _save_state(s)
        return n


@mcp.tool(name="woo_orders_refunds_create")
def woo_orders_refunds_create(orderId: int,
                              amount: str | float | None = None,
                              reason: str = "",
                              line_items: list | None = None) -> dict:
    """WC REST: POST /wp-json/wc/v3/orders/{orderId}/refunds."""
    with _lock():
        s = _load_state()
        o = s["orders"].get(str(orderId))
        if not o:
            return _err("woocommerce_rest_shop_order_invalid_id",
                        "Invalid ID.", status=404)
        amt = f"{_money(amount if amount is not None else o['total']):.2f}"
        rid = _next_id(s, "refund")
        refund = {"id": rid, "date_created": _now(),
                  "amount": amt, "reason": reason,
                  "refunded_by": 1, "refunded_payment": False,
                  "line_items": line_items or []}
        o["refunds"].append({"id": rid, "reason": reason, "total": f"-{amt}"})
        o["status"] = "refunded"
        _record(s, "orders_refunds_create", order_id=orderId,
                refund_id=rid, amount=amt)
        _save_state(s)
        return refund


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def _new_customer(state: dict, data: dict) -> dict:
    cid = _next_id(state, "customer")
    now = _now()
    cust = {
        "id": cid,
        "date_created": now, "date_modified": now,
        "email": data.get("email", ""),
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "role": "customer",
        "username": data.get("username") or (data.get("email", "")).split("@")[0],
        "billing": data.get("billing", {}),
        "shipping": data.get("shipping", {}),
        "is_paying_customer": False,
        "avatar_url": "",
        "meta_data": data.get("meta_data", []),
    }
    state["customers"][str(cid)] = cust
    return cust


@mcp.tool(name="woo_customers_list")
def woo_customers_list(perPage: int = 10, page: int = 1,
                       search: str | None = None,
                       email: str | None = None,
                       role: str | None = None,
                       orderby: str | None = "id",
                       order: str | None = "asc") -> list:
    """WC REST: GET /wp-json/wc/v3/customers."""
    with _lock():
        s = _load_state()
        items = list(s["customers"].values())
        if email:
            items = [c for c in items if c["email"] == email]
        if role:
            items = [c for c in items if c["role"] == role]
        if search:
            q = search.lower()
            items = [c for c in items
                     if q in c["email"].lower()
                     or q in (c["first_name"] + " " + c["last_name"]).lower()
                     or q in c["username"].lower()]
        items = _sort(items, orderby, order)
        page_items = _paginate(items, page, perPage)
        _record(s, "customers_list", count=len(page_items),
                search=search, email=email)
        _save_state(s)
        return page_items


@mcp.tool(name="woo_customers_get")
def woo_customers_get(customerId: int) -> dict:
    """WC REST: GET /wp-json/wc/v3/customers/{id}."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(str(customerId))
        _record(s, "customers_get", customer_id=customerId,
                result="ok" if c else "not_found")
        _save_state(s)
        if not c:
            return _err("woocommerce_rest_invalid_id",
                        "Invalid resource ID.", status=404)
        return c


@mcp.tool(name="woo_customers_create")
def woo_customers_create(customerData: dict) -> dict:
    """WC REST: POST /wp-json/wc/v3/customers."""
    with _lock():
        s = _load_state()
        em = customerData.get("email")
        if not em:
            return _err("rest_missing_callback_param",
                        "Missing parameter(s): email")
        for c in s["customers"].values():
            if c["email"].lower() == em.lower():
                return _err("registration-error-email-exists",
                            "An account is already registered with your email address.",
                            status=400)
        c = _new_customer(s, customerData)
        _record(s, "customers_create", customer_id=c["id"], email=c["email"])
        _save_state(s)
        return c


@mcp.tool(name="woo_customers_update")
def woo_customers_update(customerId: int, customerData: dict) -> dict:
    """WC REST: PUT /wp-json/wc/v3/customers/{id}."""
    with _lock():
        s = _load_state()
        c = s["customers"].get(str(customerId))
        if not c:
            _record(s, "customers_update", customer_id=customerId,
                    result="not_found")
            _save_state(s)
            return _err("woocommerce_rest_invalid_id",
                        "Invalid resource ID.", status=404)
        for k, v in (customerData or {}).items():
            c[k] = v
        c["date_modified"] = _now()
        _record(s, "customers_update", customer_id=customerId,
                fields=list((customerData or {}).keys()))
        _save_state(s)
        return c


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@mcp.tool(name="woo_reports_sales")
def woo_reports_sales(period: str | None = None,
                      date_min: str | None = None,
                      date_max: str | None = None) -> list:
    """WC REST: GET /wp-json/wc/v3/reports/sales."""
    with _lock():
        s = _load_state()
        orders = [o for o in s["orders"].values()
                  if o["status"] in ("completed", "processing")]
        if date_min:
            orders = [o for o in orders if o["date_created"] >= date_min]
        if date_max:
            orders = [o for o in orders if o["date_created"] <= date_max]
        total = sum(_money(o["total"]) for o in orders)
        items_sold = sum(int(li["quantity"]) for o in orders
                         for li in o["line_items"])
        report = [{
            "total_sales": f"{total:.2f}",
            "net_sales": f"{total:.2f}",
            "average_sales": f"{(total/len(orders) if orders else 0):.2f}",
            "total_orders": len(orders),
            "total_items": items_sold,
            "total_tax": "0.00",
            "total_shipping": "0.00",
            "total_refunds": 0,
            "total_discount": "0.00",
            "totals_grouped_by": period or "day",
            "totals": {},
        }]
        _record(s, "reports_sales",
                total=f"{total:.2f}", orders=len(orders))
        _save_state(s)
        return report


@mcp.tool(name="woo_reports_top_sellers")
def woo_reports_top_sellers(period: str | None = None,
                            date_min: str | None = None,
                            date_max: str | None = None) -> list:
    """WC REST: GET /wp-json/wc/v3/reports/top_sellers."""
    with _lock():
        s = _load_state()
        orders = [o for o in s["orders"].values()
                  if o["status"] in ("completed", "processing")]
        if date_min:
            orders = [o for o in orders if o["date_created"] >= date_min]
        if date_max:
            orders = [o for o in orders if o["date_created"] <= date_max]
        agg: dict[int, dict] = {}
        for o in orders:
            for li in o["line_items"]:
                pid = li.get("product_id")
                if pid is None:
                    continue
                a = agg.setdefault(pid, {"product_id": pid,
                                         "name": li.get("name", ""),
                                         "quantity": 0})
                a["quantity"] += int(li["quantity"])
        out = sorted(agg.values(), key=lambda x: x["quantity"],
                     reverse=True)
        _record(s, "reports_top_sellers", n=len(out))
        _save_state(s)
        return out


@mcp.tool(name="woo_reports_low_stock")
def woo_reports_low_stock(threshold: int = 2) -> list:
    """WC REST: GET /wp-json/wc/v3/reports/low_in_stock."""
    with _lock():
        s = _load_state()
        out = [p for p in s["products"].values()
               if p.get("manage_stock")
               and p.get("stock_quantity") is not None
               and int(p["stock_quantity"]) <= int(threshold)]
        _record(s, "reports_low_stock", threshold=threshold,
                count=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not in the real WC API."""
    with _lock():
        return _load_state()


if __name__ == "__main__":
    mcp.run()
