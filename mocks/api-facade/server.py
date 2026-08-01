#!/usr/bin/env python3
"""One HTTP server that impersonates the public APIs Toolathlon calls.

Upstream preprocess and graders talk to hardcoded hosts (api.github.com,
api.notion.com, huggingface.co, sheets.googleapis.com, ...). The fidelity
contract keeps that code unmodified, so `mocks/netredirect` rewrites those
hosts onto this server and each request lands in the router for that
service:

    https://api.github.com/user
      -> http://127.0.0.1:10200/__svc/github/user
      -> routers/github.py  handle("GET", "/user", ...)

Every router reads and writes the SAME state.json its mock MCP server uses,
so the agent's tools and the harness's client library are two views of one
account.

    api_facade.py [--port 10200]

A router returns (status, payload). Unimplemented routes return 501 with a
loud message rather than a plausible-looking empty result: a silent wrong
answer here would change a grade.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "routers"))

PREFIX = "/__svc/"
_ROUTERS: dict = {}
_LOCK = threading.RLock()


def get_router(name: str):
    if name not in _ROUTERS:
        try:
            _ROUTERS[name] = importlib.import_module(f"routers.{name}")
        except ImportError as e:
            _ROUTERS[name] = e
    router = _ROUTERS[name]
    if isinstance(router, Exception):
        raise router
    return router


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("API_FACADE_DEBUG"):
            sys.stderr.write("[api-facade] " + fmt % args + "\n")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith(PREFIX):
            return self._send(404, {"message": f"no service in {path}"})
        rest = path[len(PREFIX):]
        service, _, tail = rest.partition("/")
        tail = "/" + unquote(tail)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query["__raw_query"] = parsed.query

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw

        try:
            router = get_router(service)
        except Exception as e:  # noqa: BLE001
            return self._send(501, {"message": f"no router for {service}: {e}"})

        try:
            with _LOCK:
                status, payload = router.handle(
                    method, tail, query, body, dict(self.headers))
        except NotImplementedError as e:
            sys.stderr.write(f"[api-facade] 501 {service} {method} {tail}: {e}\n")
            return self._send(501, {"message": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"message": f"{type(e).__name__}: {e}"})
        return self._send(status, payload)

    def _send(self, status: int, payload) -> None:
        if isinstance(payload, (bytes, bytearray)):
            data, ctype = bytes(payload), "application/octet-stream"
        elif isinstance(payload, str):
            data, ctype = payload.encode(), "text/plain; charset=utf-8"
        else:
            data = json.dumps(payload, default=str).encode()
            ctype = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=10200)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[api-facade] listening on 127.0.0.1:{args.port}{PREFIX}<service>",
          flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
