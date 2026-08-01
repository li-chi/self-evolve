"""Route hardcoded API hosts to local mock facades.

Most Toolathlon graders and preprocess scripts talk to a public API whose
host is baked into the client (`https://api.github.com` in
`utils.app_specific.github`, `api.notion.com` inside `notion_client`,
`huggingface.co` inside `huggingface_hub`, and so on). The fidelity
contract says that code must run *unmodified*, so the substitution happens
below it: this module patches the three HTTP stacks those clients use and
rewrites the destination host.

    NETREDIRECT_MAP='{"api.github.com": "http://127.0.0.1:10101"}'

Anything not in the map is left alone, so live-web calls in the same
process (arXiv, yfinance) still go out to the real internet.

Installed as `sitecustomize.py` on PYTHONPATH, so it applies to every
Python process in the container without touching any task code.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

_MAP: dict = {}


def _load_map() -> dict:
    raw = os.environ.get("NETREDIRECT_MAP", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("netredirect: NETREDIRECT_MAP is not valid JSON", file=sys.stderr)
        return {}


def rewrite(url: str) -> str:
    """Map a URL onto its local facade, or return it unchanged."""
    if not _MAP or not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.hostname or ""
    target = _MAP.get(host)
    if target is None and host:
        # allow a "*.example.com" wildcard entry
        for pattern, value in _MAP.items():
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                target = value
                break
    if not target:
        return url
    t = urlsplit(target)
    # Preserve the original host so a facade can serve several APIs and
    # so signed/virtual-host logic still sees where the call was meant for.
    prefix = t.path.rstrip("/")
    return urlunsplit((t.scheme or "http", t.netloc,
                       prefix + parts.path, parts.query, parts.fragment))


def _patch_requests() -> None:
    try:
        import requests
    except ImportError:
        return
    original = requests.sessions.Session.request

    def request(self, method, url, *args, **kwargs):
        return original(self, method, rewrite(url), *args, **kwargs)

    requests.sessions.Session.request = request


def _patch_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return

    def _fix(request):
        new = rewrite(str(request.url))
        if new != str(request.url):
            request.url = httpx.URL(new)
        return request

    for client_cls, attr in ((httpx.Client, "send"), (httpx.AsyncClient, "send")):
        original = getattr(client_cls, attr)

        def make(orig):
            def send(self, request, *args, **kwargs):
                return orig(self, _fix(request), *args, **kwargs)
            return send

        setattr(client_cls, attr, make(original))


def _patch_httplib2() -> None:
    try:
        import httplib2
    except ImportError:
        return
    original = httplib2.Http.request

    def request(self, uri, *args, **kwargs):
        return original(self, rewrite(uri), *args, **kwargs)

    httplib2.Http.request = request


def _patch_urllib() -> None:
    try:
        import urllib.request as urlreq
    except ImportError:
        return
    original = urlreq.urlopen

    def urlopen(url, *args, **kwargs):
        if isinstance(url, str):
            url = rewrite(url)
        elif hasattr(url, "full_url"):
            url.full_url = rewrite(url.full_url)
        return original(url, *args, **kwargs)

    urlreq.urlopen = urlopen


_MAP = _load_map()
if _MAP:
    _patch_requests()
    _patch_httpx()
    _patch_httplib2()
    _patch_urllib()
    if os.environ.get("NETREDIRECT_DEBUG"):
        print(f"netredirect: active for {sorted(_MAP)}", file=sys.stderr)
