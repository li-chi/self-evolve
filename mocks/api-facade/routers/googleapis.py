"""www.googleapis.com — one host, several APIs.

Drive, Calendar and the Sheets fallback all live under this host, so the
router dispatches on the API path segment and hands off to the service
router. Keeping one entry per host means a task that uses Drive *and*
Calendar does not have to choose which one owns the redirect.
"""

from __future__ import annotations

import gcalendar
import gdrive
import gforms
import gsheets


def handle(method: str, path: str, query: dict, body, headers: dict):
    head = [p for p in path.split("/") if p][:1]
    first = head[0] if head else ""
    if first == "drive":
        return gdrive.handle(method, path, query, body, headers)
    if first == "calendar":
        return gcalendar.handle(method, path, query, body, headers)
    if first == "forms" or first == "v1":
        return gforms.handle(method, path, query, body, headers)
    if first in ("sheets", "v4"):
        return gsheets.handle(method, path, query, body, headers)
    if first == "discovery":
        return gdrive.handle(method, path, query, body, headers)
    if first == "oauth2":
        import goauth
        return goauth.handle(method, path, query, body, headers)
    raise NotImplementedError(f"googleapis: {method} {path}")
