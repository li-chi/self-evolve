"""Google OAuth2 token endpoint stub.

`google.oauth2.credentials.Credentials.refresh()` posts to
oauth2.googleapis.com before any API call. There is no identity to prove
against a local mock, so the exchange always succeeds with a placeholder
token — the API routers do not check it.
"""

from __future__ import annotations


def handle(method: str, path: str, query: dict, body, headers: dict):
    if path.rstrip("/").endswith("token") or path == "/":
        return 200, {"access_token": "mock-access-token",
                     "expires_in": 3600,
                     "refresh_token": "mock-refresh-token",
                     "scope": "https://www.googleapis.com/auth/drive",
                     "token_type": "Bearer",
                     "id_token": "mock-id-token"}
    if "revoke" in path:
        return 200, {}
    if "tokeninfo" in path:
        return 200, {"aud": "mock", "scope": "", "expires_in": 3600}
    raise NotImplementedError(f"goauth: {method} {path}")
