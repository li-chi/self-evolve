"""Google Drive v3 served from the google-drive-mock state.

`utils.app_specific.googlesheet.drive_helper` builds a Drive client with
`googleapiclient.discovery.build('drive', 'v3', ...)`, which talks to
www.googleapis.com over httplib2; netredirect points that host here. The
mock module owns the file objects and the Drive `q` query language, so a
folder the agent sees through its MCP tool is the folder the grader lists.
"""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

gd = _load_mock("google-drive-mock")

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _files(state):
    return state.setdefault("files", {})


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = gd._load_state()
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    # googleapiclient hits /drive/v3/files...; the discovery document is
    # fetched from the same host, so serve a minimal one too.
    if parts[:1] == ["discovery"]:
        return 200, {"kind": "discovery#restDescription", "name": "drive",
                     "version": "v3", "rootUrl": "https://www.googleapis.com/",
                     "servicePath": "drive/v3/", "resources": {}}
    if parts[:2] == ["drive", "v3"]:
        parts = parts[2:]

    if parts[:1] == ["files"]:
        file_id = parts[1] if len(parts) > 1 else None

        if file_id is None and method == "GET":
            q = query.get("q")
            out = [gd._public_file(f) for f in _files(state).values()
                   if not q or gd._file_matches_q(state, f, q)]
            return 200, {"kind": "drive#fileList", "files": out,
                         "incompleteSearch": False}

        if file_id is None and method == "POST":
            new = gd._new_file(
                file_id=gd._gen_file_id(),
                name=body.get("name", "Untitled"),
                mime_type=body.get("mimeType", "application/octet-stream"),
                parents=body.get("parents") or [gd._ROOT_FOLDER_ID],
                content=b"")
            _files(state)[new["id"]] = new
            gd._save_state(state)
            return 200, gd._public_file(new)

        target = _files(state).get(file_id)
        if target is None:
            return 404, {"error": {"code": 404, "message": "File not found",
                                   "errors": [{"reason": "notFound"}]}}

        if len(parts) == 2 and method == "GET":
            return 200, gd._public_file(target)
        if len(parts) == 2 and method in ("PATCH", "PUT"):
            for key in ("name", "parents", "trashed", "description"):
                if key in body:
                    target[key] = body[key]
            gd._save_state(state)
            return 200, gd._public_file(target)
        if len(parts) == 2 and method == "DELETE":
            del _files(state)[file_id]
            gd._save_state(state)
            return 204, {}

        if len(parts) == 3 and parts[2] == "copy" and method == "POST":
            import copy as _copy
            new = _copy.deepcopy(target)
            new["id"] = gd._gen_file_id()
            new["name"] = body.get("name", target.get("name"))
            if body.get("parents"):
                new["parents"] = body["parents"]
            _files(state)[new["id"]] = new
            gd._save_state(state)
            return 200, gd._public_file(new)

        if len(parts) == 3 and parts[2] == "permissions" and method == "POST":
            perm = gd._make_permission(
                perm_id=gd._gen_permission_id(state),
                type_=body.get("type", "anyone"),
                role=body.get("role", "reader"),
                email_address=body.get("emailAddress", ""))
            target.setdefault("_permissions", []).append(perm)
            gd._save_state(state)
            return 200, perm

    raise NotImplementedError(f"drive facade: {method} {path}")
