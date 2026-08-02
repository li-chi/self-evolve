#!/usr/bin/env python3
"""WooCommerce REST API served from the woocommerce-mock state.

Both clients in a Toolathlon woocommerce task take a *URL*:

  * the agent's MCP server (`@lockon0927/woocommerce-mcp`) reads
    WORDPRESS_SITE_URL / WOOCOMMERCE_CONSUMER_KEY / _SECRET
  * upstream preprocess and graders use
    `utils.app_specific.woocommerce.WooCommerceClient(site_url, ...)`

so the substitution happens at the HTTP layer: this serves
/wp-json/wc/v3/... (and the handful of /wp-json/wp/v2/... routes used) out
of the same state.json the MCP mock reads and writes. Neither client is
modified, and both see one store.

    rest_facade.py [--port 10003] [--prefix /store100]
                   [--state-dir /var/lib/mock-state/woocommerce]

Auth is accepted but not enforced: upstream passes the per-task consumer
key/secret, and the mock has a single store, so there is nothing to
authorise between.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as wcmock  # the MCP mock: single source of state + behaviour

PREFIX = ""
PORT = 10003
_LOCK = threading.RLock()

# collection name -> (state key, id counter key)
COLLECTIONS = {
    "products": ("products", "product"),
    "products/attributes": ("attributes", "attribute"),
    "products/shipping_classes": ("shipping_classes", "shipping_class"),
    "coupons": ("coupons", "coupon"),
    "orders": ("orders", "order"),
    "customers": ("customers", "customer"),
    "products/categories": ("categories", "category"),
    "products/tags": ("tags", "tag"),
    "products/reviews": ("reviews", "review"),
}


def _load():
    return wcmock._load_state()


def _save(state):
    # Keep every bucket the MCP mock's own handlers assume present: the two
    # views share one file, and a store first written through REST must not
    # be missing keys the tool side reads (e.g. the "calls" log).
    for bucket in ("products", "orders", "customers", "categories", "tags",
                   "reviews"):
        state.setdefault(bucket, {})
    state.setdefault("next_id", {})
    state.setdefault("calls", [])
    wcmock._save_state(state)


def _match_filters(item: dict, params: dict) -> bool:
    """Apply the query filters WooCommerce supports and clients rely on."""
    for key, values in params.items():
        value = values[0]
        if key in ("page", "per_page", "offset", "order", "orderby",
                   "context", "_fields", "consumer_key", "consumer_secret"):
            continue
        if key == "search":
            # WooCommerce searches the text fields, not the whole record —
            # a naive JSON dump matches key names ("shipping_required"
            # contains "red") and would return everything.
            haystack = " ".join(
                str(item.get(f, "")) for f in
                ("name", "sku", "slug", "description", "short_description")
            ).lower()
            if value.lower() not in haystack:
                return False
        elif key == "include":
            if str(item.get("id")) not in value.split(","):
                return False
        elif key == "exclude":
            if str(item.get("id")) in value.split(","):
                return False
        elif key in ("sku", "slug", "status", "type", "stock_status",
                     "customer", "email"):
            if key == "status" and value.lower() == "any":
                continue  # WC REST: status=any means no status filter
            if str(item.get(key, "")).lower() != value.lower():
                return False
        elif key == "category":
            cats = [str(c.get("id")) for c in item.get("categories", [])]
            if value not in cats:
                return False
        elif key == "after":
            if str(item.get("date_created", "")) < value:
                return False
        elif key == "before":
            if str(item.get("date_created", "")) > value:
                return False
    return True


def _paginate(items: list, params: dict) -> list:
    per_page = int(params.get("per_page", ["10"])[0])
    page = int(params.get("page", ["1"])[0])
    offset = int(params.get("offset", [str((page - 1) * per_page)])[0])
    orderby = params.get("orderby", [None])[0]
    if orderby and items and orderby in items[0]:
        reverse = params.get("order", ["desc"])[0].lower() == "desc"
        items = sorted(items, key=lambda x: str(x.get(orderby, "")),
                       reverse=reverse)
    return items[offset:offset + per_page]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the task logs readable
        pass

    # -- plumbing ---------------------------------------------------------

    def _wp_route(self):
        """Return the /wp-json/wp/v2/<...> tail, or None."""
        parsed = urlparse(self.path)
        path = re.sub(r"^/store\d+", "", parsed.path)
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):]
        base = "/wp-json/wp/v2/"
        if path.startswith(base):
            return path[len(base):].strip("/"), parse_qs(parsed.query)
        return None, parse_qs(parsed.query)

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Upstream hosts one store per task under /storeNN on a single port
        # (http://localhost:10003/store85/...), so accept any store prefix
        # rather than pinning one per task.
        path = re.sub(r"^/store\d+", "", path)
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):]
        for base in ("/wp-json/wc/v3/", "/wp-json/wc/v2/", "/wp-json/wp/v2/"):
            if path.startswith(base):
                return path[len(base):].strip("/"), parse_qs(parsed.query)
        return None, parse_qs(parsed.query)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _reply(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-WP-Total", str(
            len(payload) if isinstance(payload, list) else 1))
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, message="resource does not exist"):
        self._reply({"code": "woocommerce_rest_not_found",
                     "message": message, "data": {"status": 404}}, 404)

    # -- verbs ------------------------------------------------------------

    def _terms(self, endpoint: str):
        """Match a nested collection (attribute terms, product variations)."""
        return _nested_match(endpoint)

    # -- WordPress admin surface -------------------------------------------
    #
    # Media upload goes through a cookie session: the client GETs
    # /wp-login.php for a nonce, POSTs credentials, then asks
    # admin-ajax.php for a REST nonce. The mock has a single store and no
    # users to distinguish, so it accepts the login and issues a cookie.

    def _wp_admin(self):
        path = re.sub(r"^/store\d+", "", urlparse(self.path).path)
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):]
        return path

    def _serve_login_page(self) -> None:
        page = ('<!DOCTYPE html><html><body><form name="loginform" '
                'action="wp-login.php" method="post">'
                '<input type="hidden" name="_wpnonce" value="mocknonce123" />'
                '</form></body></html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _serve_login_post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(302)
        self.send_header("Location", "/wp-admin/")
        self.send_header("Set-Cookie",
                         "wordpress_logged_in_mock=mock; path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_text(self, text: str) -> None:
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        admin = self._wp_admin()
        if admin.endswith("/wp-login.php") or admin == "/wp-login.php":
            return self._serve_login_page()
        if admin.startswith("/wp-admin/admin-ajax.php"):
            return self._serve_text("mockrestnonce")
        if admin.startswith("/wp-admin"):
            return self._serve_text("ok")
        wp, wp_params = self._wp_route()
        if wp is not None and wp.split("/")[0] == "media":
            return self._media_get(wp, wp_params)
        if wp is not None and wp.split("/")[0] == "posts":
            return self._posts_get(wp, wp_params)
        endpoint, params = self._route()
        if endpoint is None:
            return self._not_found("unknown API root")
        with _LOCK:
            state = _load()
        m, bname, _counter = self._terms(endpoint)
        if m:
            with _LOCK:
                bucket = _terms_bucket(_load(), bname, m.group(1))
            if m.group(2):
                item = bucket.get(m.group(2))
                return self._reply(item) if item else self._not_found()
            return self._reply(_paginate(
                [t for t in bucket.values() if _match_filters(t, params)],
                params))
        coll, ident = _split(endpoint)
        if coll not in COLLECTIONS:
            if endpoint in ("", "system_status", "data"):
                return self._reply({"environment": {"version": "9.0.0"}})
            if coll == "shipping/zones":
                # a fresh WC install has only the default zone (id 0);
                # clients enumerate zones to clean them up
                return self._reply([{"id": 0,
                                     "name": "Locations not covered by "
                                             "your other zones",
                                     "order": 0}])
            if coll in ("taxes", "products/attributes", "webhooks"):
                # empty on a fresh store; clients enumerate to clean up
                return self._reply([])
            return self._not_found(f"no collection {coll}")
        key = COLLECTIONS[coll][0]
        bucket = state.get(key, {})
        if ident:
            item = bucket.get(str(ident))
            return self._reply(item) if item else self._not_found()
        items = [i for i in bucket.values() if _match_filters(i, params)]
        return self._reply(_paginate(items, params))

    def do_POST(self):
        admin = self._wp_admin()
        if admin.endswith("/wp-login.php"):
            return self._serve_login_post()
        wp, _wp_params = self._wp_route()
        if wp is not None and wp.split("/")[0] == "media":
            return self._media_upload()
        if wp is not None and wp.split("/")[0] == "posts":
            return self._posts_create()
        endpoint, _params = self._route()
        if endpoint is None:
            return self._not_found("unknown API root")
        body = self._body()
        coll, ident = _split(endpoint)

        if coll.endswith("batch") or endpoint.endswith("/batch"):
            return self._batch(endpoint[: -len("/batch")], body)

        m, bname, counter = self._terms(endpoint)
        if m:
            with _LOCK:
                state = _load()
                bucket = _terms_bucket(state, bname, m.group(1))
                if m.group(2):                       # POST to a child = update
                    item = bucket.get(m.group(2))
                    if not item:
                        return self._not_found()
                    item.update(body)
                else:
                    new_id = state.setdefault("next_id", {}).get(counter, 1)
                    state["next_id"][counter] = new_id + 1
                    item = dict(body)
                    item["id"] = new_id
                    item.setdefault("slug", re.sub(
                        r"[^a-z0-9]+", "-", str(body.get("name", "")).lower()))
                    bucket[str(new_id)] = item
                _save(state)
            return self._reply(item, 201)

        with _LOCK:
            state = _load()
            if coll not in COLLECTIONS:
                return self._not_found(f"no collection {coll}")
            key, counter = COLLECTIONS[coll]
            if ident:                      # POST to an item = update
                item = state.get(key, {}).get(str(ident))
                if not item:
                    return self._not_found()
                item.update(body)
                _save(state)
                return self._reply(item)
            item = _create(state, key, counter, body)
            _save(state)
        return self._reply(item, 201)

    def do_PUT(self):
        endpoint, _params = self._route()
        body = self._body()
        m, bname, _counter = self._terms(endpoint or "")
        if m and m.group(2):
            with _LOCK:
                state = _load()
                item = _terms_bucket(state, bname, m.group(1)).get(m.group(2))
                if item is None:
                    return self._not_found()
                item.update(body)
                _save(state)
            return self._reply(item)
        coll, ident = _split(endpoint or "")
        if endpoint and endpoint.endswith("/batch"):
            return self._batch(endpoint[: -len("/batch")], body)
        with _LOCK:
            state = _load()
            if coll not in COLLECTIONS or not ident:
                return self._not_found()
            item = state.get(COLLECTIONS[coll][0], {}).get(str(ident))
            if not item:
                return self._not_found()
            item.update(body)
            _save(state)
        return self._reply(item)

    def do_DELETE(self):
        wp, _wp_params = self._wp_route()
        if wp is not None and wp.split("/")[0] == "media":
            return self._media_delete(wp)
        endpoint, _params = self._route()
        coll, ident = _split(endpoint or "")
        m, bname, _counter = self._terms(endpoint or "")
        if m and m.group(2):
            with _LOCK:
                state = _load()
                item = _terms_bucket(state, bname, m.group(1)).pop(
                    m.group(2), None)
                if item is None:
                    return self._not_found()
                _save(state)
            return self._reply(item)
        with _LOCK:
            state = _load()
            if coll not in COLLECTIONS or not ident:
                return self._not_found()
            item = state.get(COLLECTIONS[coll][0], {}).pop(str(ident), None)
            if item is None:
                return self._not_found()
            _save(state)
        return self._reply(item)

    # -- WordPress media library ------------------------------------------
    #
    # WooCommerce product images are WordPress attachments: preprocess
    # uploads JPEGs with a raw body plus Content-Disposition, then attaches
    # the returned media ids to products. The bytes are kept on disk beside
    # the state file so an agent can fetch source_url.

    def _media_get(self, wp: str, params: dict) -> None:
        parts = wp.split("/")
        with _LOCK:
            state = _load()
        media = state.get("media", {})
        if len(parts) > 1 and parts[1].isdigit():
            item = media.get(parts[1])
            return self._reply(item) if item else self._not_found()
        return self._reply(_paginate(list(media.values()), params))

    def _media_upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        content = self.rfile.read(length) if length else b""
        disposition = self.headers.get("Content-Disposition", "")
        m = re.search(r'filename="?([^";]+)"?', disposition)
        filename = m.group(1) if m else "upload.bin"
        with _LOCK:
            state = _load()
            new_id = state.setdefault("next_id", {}).get("media", 1)
            state["next_id"]["media"] = new_id + 1
            media_dir = os.path.join(
                os.environ.get("WC_MOCK_STATE_DIR", "."), "media")
            os.makedirs(media_dir, exist_ok=True)
            disk = os.path.join(media_dir, f"{new_id}-{filename}")
            with open(disk, "wb") as f:
                f.write(content)
            item = {
                "id": new_id,
                "title": {"rendered": filename},
                "slug": re.sub(r"[^a-z0-9]+", "-", filename.lower()),
                "media_type": "image",
                "mime_type": self.headers.get("Content-Type", "image/jpeg"),
                "source_url": f"http://127.0.0.1:{PORT}/wp-content/uploads/"
                              f"{new_id}-{filename}",
                "alt_text": "",
                "file_path": disk,
            }
            state.setdefault("media", {})[str(new_id)] = item
            _save(state)
        self._reply(item, 201)

    def _media_delete(self, wp: str) -> None:
        parts = wp.split("/")
        if len(parts) < 2 or not parts[1].isdigit():
            return self._not_found()
        with _LOCK:
            state = _load()
            item = state.get("media", {}).pop(parts[1], None)
            if item is None:
                return self._not_found()
            _save(state)
        self._reply({"deleted": True, "previous": item})

    # -- WordPress posts (wp/v2/posts): minimal blog surface ---------------

    def _posts_get(self, wp: str, params: dict) -> None:
        parts = wp.split("/")
        with _LOCK:
            state = _load()
            posts = state.setdefault("posts", {})
            if len(parts) > 1:
                p = posts.get(parts[1])
                return self._reply(p) if p else self._not_found()
            items = sorted(posts.values(),
                           key=lambda p: p.get("date", ""), reverse=True)
            return self._reply(_paginate(items, params))

    def _posts_create(self) -> None:
        body = self._body()
        with _LOCK:
            state = _load()
            posts = state.setdefault("posts", {})
            pid = state.setdefault("next_id", {}).get("post", 1)
            state["next_id"]["post"] = pid + 1
            now = wcmock._now()
            post = {"id": pid, "date": now, "date_gmt": now,
                    "status": body.get("status", "publish"),
                    "title": {"rendered": body.get("title", "")},
                    "content": {"rendered": body.get("content", "")},
                    "excerpt": {"rendered": body.get("excerpt", "")}}
            posts[str(pid)] = post
            _save(state)
        return self._reply(post, status=201)

    def _batch(self, coll: str, body: dict) -> None:
        """WooCommerce batch endpoint: {create:[], update:[], delete:[]}."""
        coll = coll.strip("/")
        if coll not in COLLECTIONS:
            return self._not_found(f"no collection {coll}")
        key, counter = COLLECTIONS[coll]
        out = {"create": [], "update": [], "delete": []}
        with _LOCK:
            state = _load()
            bucket = state.setdefault(key, {})
            for data in body.get("create", []) or []:
                out["create"].append(_create(state, key, counter, data))
            for data in body.get("update", []) or []:
                item = bucket.get(str(data.get("id")))
                if item is None:
                    out["update"].append(
                        {"id": data.get("id"),
                         "error": {"code": "woocommerce_rest_not_found"}})
                    continue
                patch = {k: v for k, v in data.items() if k != "id"}
                item.update(patch)
                out["update"].append(item)
            for ident in body.get("delete", []) or []:
                item = bucket.pop(str(ident), None)
                out["delete"].append(item or {"id": ident})
            _save(state)
        self._reply(out)


# nested collections: /products/attributes/<id>/terms and
# /products/<id>/variations behave like collections owned by a parent.
NESTED = [
    (re.compile(r"^products/attributes/(\d+)/terms(?:/(\d+))?$"),
     "attribute_terms", "term"),
    (re.compile(r"^products/(\d+)/variations(?:/(\d+))?$"),
     "variations", "variation"),
]


def _nested_match(endpoint: str):
    for rx, bucket, counter in NESTED:
        m = rx.match(endpoint or "")
        if m:
            return m, bucket, counter
    return None, None, None


def _terms_bucket(state: dict, bucket: str, parent_id: str) -> dict:
    """Child records of a parent, as a {id: record} map.

    Variations are the exception: the MCP mock reads them from the parent
    product's own "variations" LIST, so the facade writes them there and
    presents a map view over it — one storage, two shapes.
    """
    if bucket == "variations":
        product = state.setdefault("products", {}).setdefault(
            str(parent_id), {"id": int(parent_id)})
        return _ListAsMap(product.setdefault("variations", []))
    return state.setdefault(bucket, {}).setdefault(str(parent_id), {})


class _ListAsMap(dict):
    """Dict view over a list of records keyed by str(record["id"])."""

    def __init__(self, backing: list):
        super().__init__({str(r.get("id")): r for r in backing})
        self._backing = backing

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        for i, r in enumerate(self._backing):
            if str(r.get("id")) == str(key):
                self._backing[i] = value
                return
        self._backing.append(value)

    def pop(self, key, default=None):
        item = super().pop(key, default)
        self._backing[:] = [r for r in self._backing
                            if str(r.get("id")) != str(key)]
        return item


def _split(endpoint: str):
    """Split 'products/12' or 'products/categories/3' into (collection, id)."""
    parts = [p for p in endpoint.split("/") if p]
    if not parts:
        return "", None
    if parts[-1].isdigit():
        return "/".join(parts[:-1]), parts[-1]
    return "/".join(parts), None


def _create(state: dict, key: str, counter: str, data: dict) -> dict:
    """Create through the MCP mock's own constructors where they exist, so
    REST-created and tool-created records are shaped identically."""
    # The mock's constructors assume a fully-formed state dict; a store
    # seeded only through REST may not have every bucket yet.
    for bucket in ("products", "orders", "customers", "categories", "tags",
                   "reviews"):
        state.setdefault(bucket, {})
    state.setdefault("next_id", {})
    if key == "products" and hasattr(wcmock, "_new_product"):
        return wcmock._new_product(state, data)
    if key == "orders" and hasattr(wcmock, "_new_order"):
        return wcmock._new_order(state, data)
    new_id = state.setdefault("next_id", {}).get(counter, 1)
    state["next_id"][counter] = new_id + 1
    item = dict(data)
    item["id"] = new_id
    state.setdefault(key, {})[str(new_id)] = item
    return item


def main() -> None:
    global PREFIX, PORT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=10003)
    ap.add_argument("--prefix", default="",
                    help="extra path prefix; /storeNNN is always accepted")
    ap.add_argument("--state-dir",
                    default="/var/lib/mock-state/woocommerce")
    args = ap.parse_args()

    PREFIX = args.prefix.rstrip("/")
    PORT = args.port
    os.environ.setdefault("WC_MOCK_STATE_DIR", args.state_dir)
    os.makedirs(args.state_dir, exist_ok=True)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[woocommerce-rest] http://127.0.0.1:{args.port}{PREFIX}"
          f"/store*/wp-json/wc/v3  state={args.state_dir}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
