"""Google Drive mock MCP server.

Mirrors the Google Drive API v3 surface (`drive.files.*`,
`drive.permissions.*`, `drive.drives.*`, `drive.about.*`). Tool
names follow the de-facto MCP naming derived from Drive method
names (`files.list` -> `list_files`, `permissions.create` ->
`create_permission`, etc.). Response shapes match Drive v3
resources verbatim:

  File
    {id, name, mimeType, parents, kind: "drive#file",
     createdTime, modifiedTime, size, md5Checksum,
     webViewLink, webContentLink, iconLink, thumbnailLink,
     owners: [{kind:"drive#user", displayName, emailAddress}],
     permissions: [...], trashed, version, description,
     starred, properties, appProperties}
  Permission
    {id, type, role, emailAddress?, displayName?, domain?,
     kind: "drive#permission"}
  Drive (shared drive)
    {id, name, kind: "drive#drive", createdTime}

List responses:
    files.list        -> {kind: "drive#fileList", incompleteSearch,
                          files: [File], nextPageToken?}
    permissions.list  -> {kind: "drive#permissionList",
                          permissions: [Permission], nextPageToken?}
    drives.list       -> {kind: "drive#driveList",
                          drives: [Drive], nextPageToken?}

Upstream tool surface (13):

  list_files, get_file, create_file, update_file, delete_file,
  copy_file, export_file, download_file,
  list_permissions, create_permission, delete_permission,
  list_drives, get_about

Plus mock-only debug tools used by per-task setup/verification:

  mock_debug_state, mock_debug_seed_file, mock_debug_seed_folder,
  mock_debug_seed_permission

State — one JSON file at $GDRIVE_MOCK_STATE_DIR/state.json:

  state = {
    "about": {"user": {...}, "storageQuota": {...}, ...},
    "files": {
      "<fileId>": {<File resource>, "_content": "<base64>"?,
                   "_text": "<plain text>"?}
    },
    "permissions": {
      "<fileId>": {"<permissionId>": <Permission resource>}
    },
    "drives": {
      "<driveId>": <Drive resource>
    },
    "next_id": {"permission": 1},
    "calls": [...]
  }

Folders are files with mimeType `application/vnd.google-apps.folder`.
Trash is a `trashed: true` flag on the file (no separate trash bin).
Google-native types (docs/sheets/slides) store their human-readable
text in the `_text` field; `export_file` formats that text into the
requested export mimeType.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import string
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "GDRIVE_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gdrive_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


_FOLDER_MIME = "application/vnd.google-apps.folder"
_DOC_MIME = "application/vnd.google-apps.document"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_SLIDES_MIME = "application/vnd.google-apps.presentation"
_NATIVE_MIMES = {_DOC_MIME, _SHEET_MIME, _SLIDES_MIME, _FOLDER_MIME}

_ROOT_FOLDER_ID = "root"


def _default_about() -> dict:
    return {
        "kind": "drive#about",
        "user": {
            "kind": "drive#user",
            "displayName": "Mock User",
            "emailAddress": "me@gdrive.mock",
            "permissionId": "00000000000000000001",
            "me": True,
        },
        "storageQuota": {
            "limit": "16106127360",       # 15 GiB
            "usage": "0",
            "usageInDrive": "0",
            "usageInDriveTrash": "0",
        },
        "maxImportSizes": {},
        "maxUploadSize": "5242880000000",
        "appInstalled": True,
        "canCreateDrives": True,
    }


def _empty_state() -> dict:
    root = {
        "id": _ROOT_FOLDER_ID,
        "name": "My Drive",
        "mimeType": _FOLDER_MIME,
        "parents": [],
        "kind": "drive#file",
        "createdTime": "2024-01-01T00:00:00.000Z",
        "modifiedTime": "2024-01-01T00:00:00.000Z",
        "size": "0",
        "md5Checksum": "",
        "webViewLink": f"https://drive.google.com/drive/folders/{_ROOT_FOLDER_ID}",
        "webContentLink": "",
        "iconLink": ("https://drive-thirdparty.googleusercontent.com/"
                      "16/type/application/vnd.google-apps.folder"),
        "thumbnailLink": "",
        "owners": [{
            "kind": "drive#user",
            "displayName": "Mock User",
            "emailAddress": "me@gdrive.mock",
        }],
        "trashed": False,
        "version": "1",
        "description": "",
        "starred": False,
        "properties": {},
        "appProperties": {},
    }
    return {
        "about": _default_about(),
        "files": {_ROOT_FOLDER_ID: root},
        "permissions": {_ROOT_FOLDER_ID: {}},
        "drives": {},
        "next_id": {"permission": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GDRIVE_MOCK_SEED_PATH")
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
# ID generation
# ---------------------------------------------------------------------------

_FILE_ID_ALPHABET = string.ascii_letters + string.digits + "_-"


def _gen_file_id() -> str:
    """Drive file IDs are 33-char URL-safe (letters + digits + `_-`)."""
    return "".join(secrets.choice(_FILE_ID_ALPHABET) for _ in range(33))


def _gen_permission_id(state: dict) -> str:
    """Permission IDs are numeric strings (e.g. `01234567890123456789`)."""
    n = int(state["next_id"].get("permission", 1))
    state["next_id"]["permission"] = n + 1
    return f"{n:020d}"


def _gen_drive_id() -> str:
    """Shared-drive ids share the file-id shape."""
    return "0A" + "".join(secrets.choice(_FILE_ID_ALPHABET) for _ in range(31))


# ---------------------------------------------------------------------------
# File / permission builders
# ---------------------------------------------------------------------------

def _b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def _b64d(s: str) -> bytes:
    if not s:
        return b""
    return base64.b64decode(s)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _is_native(mime: str) -> bool:
    return mime in _NATIVE_MIMES


def _icon_link(mime: str) -> str:
    return (f"https://drive-thirdparty.googleusercontent.com/16/type/{mime}")


def _web_view_link(file_id: str, mime: str) -> str:
    if mime == _FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{file_id}"
    if mime == _DOC_MIME:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if mime == _SHEET_MIME:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if mime == _SLIDES_MIME:
        return f"https://docs.google.com/presentation/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"


def _web_content_link(file_id: str, mime: str, name: str) -> str:
    if mime in (_FOLDER_MIME, _DOC_MIME, _SHEET_MIME, _SLIDES_MIME):
        return ""
    return (f"https://drive.google.com/uc?id={file_id}&export=download")


def _new_file(*, file_id: str, name: str, mime_type: str,
              parents: list[str] | None,
              content: bytes | str | None = None,
              text: str | None = None,
              description: str = "",
              properties: dict | None = None,
              app_properties: dict | None = None,
              owner_email: str = "me@gdrive.mock",
              owner_name: str = "Mock User",
              created_time: str | None = None,
              modified_time: str | None = None,
              starred: bool = False,
              trashed: bool = False) -> dict:
    ts = created_time or _now_iso()
    mts = modified_time or ts
    parents_list = list(parents) if parents else [_ROOT_FOLDER_ID]
    out: dict[str, Any] = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "parents": parents_list,
        "kind": "drive#file",
        "createdTime": ts,
        "modifiedTime": mts,
        "webViewLink": _web_view_link(file_id, mime_type),
        "webContentLink": _web_content_link(file_id, mime_type, name),
        "iconLink": _icon_link(mime_type),
        "thumbnailLink": "",
        "owners": [{
            "kind": "drive#user",
            "displayName": owner_name,
            "emailAddress": owner_email,
        }],
        "trashed": bool(trashed),
        "version": "1",
        "description": description or "",
        "starred": bool(starred),
        "properties": dict(properties or {}),
        "appProperties": dict(app_properties or {}),
    }
    if mime_type == _FOLDER_MIME:
        out["size"] = "0"
        out["md5Checksum"] = ""
    elif _is_native(mime_type):
        # Native docs/sheets/slides — store editable text, no md5.
        out["_text"] = text if text is not None else (
            content.decode("utf-8", errors="replace")
            if isinstance(content, bytes)
            else (content or ""))
        # Drive omits `size` for native docs in the real API; mirror that.
        out["size"] = "0"
        out["md5Checksum"] = ""
    else:
        # Binary / text file with bytes content.
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif content is None:
            data = b""
        else:
            data = bytes(content)
        out["_content"] = _b64(data)
        out["size"] = str(len(data))
        out["md5Checksum"] = _md5(data)
    return out


def _public_file(f: dict, *, include_permissions: list[dict] | None = None) -> dict:
    """Strip private mock fields before returning to the caller."""
    out = {k: v for k, v in f.items() if not k.startswith("_")}
    if include_permissions is not None:
        out["permissions"] = list(include_permissions)
    return out


def _make_permission(*, perm_id: str, type_: str, role: str,
                     email_address: str = "",
                     display_name: str = "",
                     domain: str = "") -> dict:
    out: dict[str, Any] = {
        "id": perm_id,
        "type": type_,
        "role": role,
        "kind": "drive#permission",
    }
    if type_ == "user" or type_ == "group":
        if email_address:
            out["emailAddress"] = email_address
        if display_name:
            out["displayName"] = display_name
    elif type_ == "domain":
        if domain:
            out["domain"] = domain
        if display_name:
            out["displayName"] = display_name
    elif type_ == "anyone":
        # No emailAddress / domain.
        pass
    return out


# ---------------------------------------------------------------------------
# `q=` query parser
# ---------------------------------------------------------------------------

# Single-clause regexes. We AND-combine on ` and `.

_NAME_EQ = re.compile(r"^\s*name\s*=\s*'([^']*)'\s*$", re.IGNORECASE)
_NAME_CONTAINS = re.compile(
    r"^\s*name\s+contains\s+'([^']*)'\s*$", re.IGNORECASE)
_MIME_EQ = re.compile(
    r"^\s*mimeType\s*=\s*'([^']*)'\s*$", re.IGNORECASE)
_MIME_NEQ = re.compile(
    r"^\s*mimeType\s*!=\s*'([^']*)'\s*$", re.IGNORECASE)
_MIME_CONTAINS = re.compile(
    r"^\s*mimeType\s+contains\s+'([^']*)'\s*$", re.IGNORECASE)
_IN_PARENTS = re.compile(
    r"^\s*'([^']*)'\s+in\s+parents\s*$", re.IGNORECASE)
_TRASHED_EQ = re.compile(
    r"^\s*trashed\s*=\s*(true|false)\s*$", re.IGNORECASE)
_STARRED_EQ = re.compile(
    r"^\s*starred\s*=\s*(true|false)\s*$", re.IGNORECASE)
_MODTIME_GT = re.compile(
    r"^\s*modifiedTime\s*>\s*'([^']*)'\s*$", re.IGNORECASE)
_MODTIME_LT = re.compile(
    r"^\s*modifiedTime\s*<\s*'([^']*)'\s*$", re.IGNORECASE)
_IN_WRITERS = re.compile(
    r"^\s*'([^']*)'\s+in\s+writers\s*$", re.IGNORECASE)
_IN_READERS = re.compile(
    r"^\s*'([^']*)'\s+in\s+readers\s*$", re.IGNORECASE)
_IN_OWNERS = re.compile(
    r"^\s*'([^']*)'\s+in\s+owners\s*$", re.IGNORECASE)
_FULLTEXT_CONTAINS = re.compile(
    r"^\s*fullText\s+contains\s+'([^']*)'\s*$", re.IGNORECASE)


def _split_q(q: str) -> list[str]:
    """Split `q` on the ` and ` connector, respecting single quotes."""
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    i = 0
    while i < len(q):
        ch = q[i]
        if ch == "'":
            in_quote = not in_quote
            cur.append(ch)
            i += 1
            continue
        if not in_quote and q[i:i + 5].lower() == " and ":
            out.append("".join(cur).strip())
            cur = []
            i += 5
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return [c for c in out if c]


def _file_matches_clause(state: dict, f: dict, clause: str) -> bool:
    c = clause.strip()
    if not c:
        return True
    perms = state.get("permissions", {}).get(f["id"], {}).values()

    def _emails_with_role(roles: set[str]) -> set[str]:
        return {p.get("emailAddress", "").lower()
                for p in perms
                if p.get("role") in roles and p.get("emailAddress")}

    def _owner_emails() -> set[str]:
        return {o.get("emailAddress", "").lower()
                for o in (f.get("owners") or [])}

    m = _NAME_EQ.match(c)
    if m:
        return f.get("name", "") == m.group(1)
    m = _NAME_CONTAINS.match(c)
    if m:
        return m.group(1).lower() in f.get("name", "").lower()
    m = _MIME_EQ.match(c)
    if m:
        return f.get("mimeType", "") == m.group(1)
    m = _MIME_NEQ.match(c)
    if m:
        return f.get("mimeType", "") != m.group(1)
    m = _MIME_CONTAINS.match(c)
    if m:
        return m.group(1).lower() in f.get("mimeType", "").lower()
    m = _IN_PARENTS.match(c)
    if m:
        return m.group(1) in (f.get("parents") or [])
    m = _TRASHED_EQ.match(c)
    if m:
        want = m.group(1).lower() == "true"
        return bool(f.get("trashed", False)) == want
    m = _STARRED_EQ.match(c)
    if m:
        want = m.group(1).lower() == "true"
        return bool(f.get("starred", False)) == want
    m = _MODTIME_GT.match(c)
    if m:
        return f.get("modifiedTime", "") > m.group(1)
    m = _MODTIME_LT.match(c)
    if m:
        return f.get("modifiedTime", "") < m.group(1)
    m = _IN_WRITERS.match(c)
    if m:
        e = m.group(1).lower()
        return e in _emails_with_role({"writer", "owner",
                                          "organizer", "fileOrganizer"})
    m = _IN_READERS.match(c)
    if m:
        e = m.group(1).lower()
        # In Drive, readers includes everyone with any read access.
        return e in (_emails_with_role({"reader", "commenter", "writer",
                                          "owner", "organizer",
                                          "fileOrganizer"})
                     | _owner_emails())
    m = _IN_OWNERS.match(c)
    if m:
        e = m.group(1).lower()
        return e in _owner_emails()
    m = _FULLTEXT_CONTAINS.match(c)
    if m:
        needle = m.group(1).lower()
        hay = " ".join([
            f.get("name", ""),
            f.get("description", ""),
            f.get("_text", "") or "",
        ]).lower()
        if needle in hay:
            return True
        # Plain-text files: peek into content.
        if f.get("_content"):
            try:
                txt = _b64d(f["_content"]).decode(
                    "utf-8", errors="replace").lower()
                if needle in txt:
                    return True
            except Exception:
                pass
        return False
    # Unknown clause → conservatively no match. Real API would 400.
    return False


def _file_matches_q(state: dict, f: dict, q: str) -> bool:
    if not q:
        return True
    for clause in _split_q(q):
        if not _file_matches_clause(state, f, clause):
            return False
    return True


# ---------------------------------------------------------------------------
# Export rendering
# ---------------------------------------------------------------------------

def _export_native(f: dict, mime_type: str) -> tuple[bytes, str]:
    """Render a native doc/sheet/slides to a target export mimeType.

    Returns (bytes, effective_mime). The mock makes no attempt to emit
    a real PDF/docx/xlsx/pptx — it emits a deterministic text blob
    framed by the requested mime so verifiers and agents can round-trip
    a recognizable payload.
    """
    text = f.get("_text", "") or ""
    name = f.get("name", "")
    fmt = (mime_type or "").lower()
    if fmt in ("text/plain",):
        return text.encode("utf-8"), "text/plain"
    if fmt in ("text/html",):
        body = (f"<html><head><title>{name}</title></head>"
                f"<body><pre>{text}</pre></body></html>")
        return body.encode("utf-8"), "text/html"
    if fmt in ("text/markdown", "text/x-markdown"):
        body = f"# {name}\n\n{text}\n"
        return body.encode("utf-8"), "text/markdown"
    if fmt in ("application/pdf",):
        # Stub PDF — header + plaintext body. Not a valid PDF on
        # purpose; the goal is round-trippable bytes the verifier can
        # match against.
        body = f"%PDF-1.4\n% {name}\n{text}\n%%EOF\n"
        return body.encode("utf-8"), "application/pdf"
    if fmt in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        body = f"DOCX:{name}\n{text}"
        return body.encode("utf-8"), fmt
    if fmt in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    ):
        body = f"sheet:{name}\n{text}"
        return body.encode("utf-8"), fmt
    if fmt in (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ):
        body = f"PPTX:{name}\n{text}"
        return body.encode("utf-8"), fmt
    # Fallback: text/plain
    return text.encode("utf-8"), "text/plain"


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("google-drive-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



_MAX_PAGE_SIZE = 1000
_DEFAULT_PAGE_SIZE = 100


def _paginate(rows: list[dict], page_size: int | None,
              page_token: str | None) -> tuple[list[dict], str | None]:
    try:
        offset = int(page_token) if page_token else 0
    except (TypeError, ValueError):
        offset = 0
    n = int(page_size) if page_size else _DEFAULT_PAGE_SIZE
    n = max(1, min(n, _MAX_PAGE_SIZE))
    page = rows[offset:offset + n]
    nxt = str(offset + n) if offset + n < len(rows) else None
    return page, nxt


def _order_files(rows: list[dict], order_by: str | None) -> list[dict]:
    """Drive `orderBy` accepts comma-separated keys with optional `desc`.

    Supported keys: name, modifiedTime, createdTime, starred, folder,
    quotaBytesUsed. Unknown keys are ignored.
    """
    if not order_by:
        # Default order: name asc.
        return sorted(rows, key=lambda f: (f.get("name") or "").lower())
    keys = [k.strip() for k in order_by.split(",") if k.strip()]
    # Apply in reverse so the first key is the primary sort.
    out = list(rows)
    for k in reversed(keys):
        desc = k.lower().endswith(" desc")
        field = k[:-5].strip() if desc else k.strip()
        if field == "folder":
            out.sort(key=lambda f: (f.get("mimeType") != _FOLDER_MIME),
                     reverse=desc)
        elif field == "name":
            out.sort(key=lambda f: (f.get("name") or "").lower(),
                     reverse=desc)
        elif field == "modifiedTime":
            out.sort(key=lambda f: f.get("modifiedTime") or "",
                     reverse=desc)
        elif field == "createdTime":
            out.sort(key=lambda f: f.get("createdTime") or "",
                     reverse=desc)
        elif field == "starred":
            out.sort(key=lambda f: bool(f.get("starred")),
                     reverse=not desc)
        elif field == "quotaBytesUsed":
            out.sort(key=lambda f: int(f.get("size") or "0"),
                     reverse=desc)
    return out


def _attach_permissions(state: dict, f: dict) -> list[dict]:
    return list(state.get("permissions", {}).get(f["id"], {}).values())


# ===========================================================================
# Files
# ===========================================================================

@mcp.tool(name="list_files")
def list_files(q: str = "",
               pageSize: int = _DEFAULT_PAGE_SIZE,
               pageToken: str | None = None,
               orderBy: str | None = None,
               spaces: str = "drive",
               corpora: str | None = None,
               fields: str | None = None,
               includeItemsFromAllDrives: bool = False,
               driveId: str | None = None,
               supportsAllDrives: bool = False) -> dict:
    """Drive API v3: `files.list` — GET /drive/v3/files.

    Returns a `drive#fileList` with the matched files (default
    excludes trashed files unless `q` specifies `trashed = true`).
    """
    with _lock():
        s = _load_state()
        rows = [f for fid, f in s["files"].items() if fid != _ROOT_FOLDER_ID]
        # Apply driveId / spaces filtering (the mock keeps everything in
        # the user's My Drive; shared-drive items are tagged with a
        # `_driveId` private field by the seeder).
        if driveId:
            rows = [f for f in rows if f.get("_driveId") == driveId]
        # Default behavior: drop trashed files unless `q` asks for them.
        if not q or "trashed" not in q.lower():
            rows = [f for f in rows if not f.get("trashed")]
        if q:
            rows = [f for f in rows if _file_matches_q(s, f, q)]
        rows = _order_files(rows, orderBy)
        page, nxt = _paginate(rows, pageSize, pageToken)
        files_out = [_public_file(f,
                                    include_permissions=_attach_permissions(s, f))
                      for f in page]
        result: dict[str, Any] = {
            "kind": "drive#fileList",
            "incompleteSearch": False,
            "files": files_out,
        }
        if nxt:
            result["nextPageToken"] = nxt
        _record(s, "list_files", q=q, count=len(files_out), total=len(rows))
        _save_state(s)
        return result


@mcp.tool(name="get_file")
def get_file(fileId: str, fields: str | None = None,
             supportsAllDrives: bool = False) -> dict:
    """Drive API v3: `files.get` — GET /drive/v3/files/{fileId}.

    Returns the File resource. `fields` is accepted for API parity
    but the mock always returns the full resource.
    """
    with _lock():
        s = _load_state()
        f = s["files"].get(fileId)
        if not f:
            _record(s, "get_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        _record(s, "get_file", file_id=fileId)
        _save_state(s)
        return _public_file(f, include_permissions=_attach_permissions(s, f))


@mcp.tool(name="create_file")
def create_file(name: str,
                mimeType: str = "application/octet-stream",
                parents: list[str] | None = None,
                description: str = "",
                properties: dict | None = None,
                appProperties: dict | None = None,
                content: str | None = None) -> dict:
    """Drive API v3: `files.create` — POST /drive/v3/files.

    Creates a new File. `content` is a string (utf-8) for text files,
    a base64-encoded blob for binary files, or omitted for empty /
    Google-native files. Returns the created File resource.
    """
    with _lock():
        s = _load_state()
        # Resolve parent. Default to root.
        parents_list = list(parents) if parents else [_ROOT_FOLDER_ID]
        for pid in parents_list:
            if pid != _ROOT_FOLDER_ID and pid not in s["files"]:
                _record(s, "create_file", result="parent_not_found",
                        parent=pid)
                _save_state(s)
                return {
                    "error": {
                        "code": 404,
                        "message": f"Parent folder not found: {pid}",
                        "status": "NOT_FOUND",
                    }
                }
        fid = _gen_file_id()
        # If the parent is a known mimeType-folder, fine; if the caller
        # passes binary `content` for a native mimeType, we treat it as
        # text (the agent typically supplies plain text for native docs).
        if _is_native(mimeType) and mimeType != _FOLDER_MIME:
            text = content or ""
            f = _new_file(file_id=fid, name=name, mime_type=mimeType,
                            parents=parents_list,
                            text=text,
                            description=description,
                            properties=properties,
                            app_properties=appProperties)
        else:
            # Non-native: `content` is a utf-8 string (small text files)
            # OR a base64 string when the caller wants binary bytes.
            payload: bytes
            if content is None:
                payload = b""
            else:
                # Try base64 first (binary protocol per the real API);
                # fall back to utf-8 if it doesn't decode cleanly.
                try:
                    payload = base64.b64decode(content, validate=True)
                except Exception:
                    payload = content.encode("utf-8")
            f = _new_file(file_id=fid, name=name, mime_type=mimeType,
                            parents=parents_list,
                            content=payload,
                            description=description,
                            properties=properties,
                            app_properties=appProperties)
        s["files"][fid] = f
        s["permissions"].setdefault(fid, {})
        # Owner gets an implicit `owner` permission.
        owner_perm_id = _gen_permission_id(s)
        s["permissions"][fid][owner_perm_id] = _make_permission(
            perm_id=owner_perm_id, type_="user", role="owner",
            email_address=s["about"]["user"]["emailAddress"],
            display_name=s["about"]["user"]["displayName"],
        )
        _record(s, "create_file", file_id=fid, name=name,
                mime_type=mimeType, parents=parents_list)
        _save_state(s)
        return _public_file(f,
                             include_permissions=_attach_permissions(s, f))


@mcp.tool(name="update_file")
def update_file(fileId: str,
                name: str | None = None,
                mimeType: str | None = None,
                addParents: list[str] | None = None,
                removeParents: list[str] | None = None,
                description: str | None = None,
                properties: dict | None = None,
                appProperties: dict | None = None,
                content: str | None = None,
                starred: bool | None = None,
                trashed: bool | None = None) -> dict:
    """Drive API v3: `files.update` — PATCH /drive/v3/files/{fileId}.

    Updates metadata (name/parents/description/properties/starred/
    trashed) and optionally replaces the file content. Returns the
    updated File resource.
    """
    with _lock():
        s = _load_state()
        f = s["files"].get(fileId)
        if not f:
            _record(s, "update_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        if name is not None:
            f["name"] = name
        if description is not None:
            f["description"] = description
        if mimeType is not None:
            f["mimeType"] = mimeType
            f["iconLink"] = _icon_link(mimeType)
            f["webViewLink"] = _web_view_link(fileId, mimeType)
            f["webContentLink"] = _web_content_link(fileId, mimeType,
                                                      f.get("name", ""))
        if properties is not None:
            f["properties"] = dict(properties)
        if appProperties is not None:
            f["appProperties"] = dict(appProperties)
        if starred is not None:
            f["starred"] = bool(starred)
        if trashed is not None:
            f["trashed"] = bool(trashed)
        # Parent mutations.
        if addParents or removeParents:
            cur = list(f.get("parents") or [])
            for pid in (addParents or []):
                if pid != _ROOT_FOLDER_ID and pid not in s["files"]:
                    continue
                if pid not in cur:
                    cur.append(pid)
            for pid in (removeParents or []):
                if pid in cur:
                    cur.remove(pid)
            if not cur:
                cur = [_ROOT_FOLDER_ID]
            f["parents"] = cur
        # Content mutation.
        if content is not None:
            if _is_native(f.get("mimeType", "")) and f.get("mimeType") != _FOLDER_MIME:
                f["_text"] = content
            else:
                try:
                    data = base64.b64decode(content, validate=True)
                except Exception:
                    data = content.encode("utf-8")
                f["_content"] = _b64(data)
                f["size"] = str(len(data))
                f["md5Checksum"] = _md5(data)
        # Bump modifiedTime + version.
        f["modifiedTime"] = _now_iso()
        try:
            f["version"] = str(int(f.get("version") or "1") + 1)
        except (TypeError, ValueError):
            f["version"] = "2"
        _record(s, "update_file", file_id=fileId)
        _save_state(s)
        return _public_file(f,
                             include_permissions=_attach_permissions(s, f))


@mcp.tool(name="delete_file")
def delete_file(fileId: str, supportsAllDrives: bool = False) -> dict:
    """Drive API v3: `files.delete` — DELETE /drive/v3/files/{fileId}.

    Permanently deletes the file (and any cached permissions). The
    real API returns 204 No Content; the mock returns an empty dict
    on success.
    """
    with _lock():
        s = _load_state()
        if fileId == _ROOT_FOLDER_ID:
            _record(s, "delete_file", file_id=fileId, result="root_protected")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": "Cannot delete root folder.",
                    "status": "INVALID_ARGUMENT",
                }
            }
        if fileId not in s["files"]:
            _record(s, "delete_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        del s["files"][fileId]
        s["permissions"].pop(fileId, None)
        # Cascade: orphaned children re-parent to root.
        for other in s["files"].values():
            parents = other.get("parents") or []
            if fileId in parents:
                new_parents = [p for p in parents if p != fileId]
                if not new_parents:
                    new_parents = [_ROOT_FOLDER_ID]
                other["parents"] = new_parents
        _record(s, "delete_file", file_id=fileId)
        _save_state(s)
        return {}


@mcp.tool(name="copy_file")
def copy_file(fileId: str,
              name: str | None = None,
              parents: list[str] | None = None) -> dict:
    """Drive API v3: `files.copy` — POST /drive/v3/files/{fileId}/copy.

    Creates a copy of the source file. `name` defaults to "Copy of
    <original name>". `parents` defaults to the source's parents.
    Returns the new File resource.
    """
    with _lock():
        s = _load_state()
        src = s["files"].get(fileId)
        if not src:
            _record(s, "copy_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        new_id = _gen_file_id()
        new_name = name if name is not None else (
            f"Copy of {src.get('name', '')}")
        new_parents = list(parents) if parents else list(src.get("parents")
                                                             or [_ROOT_FOLDER_ID])
        copy = dict(src)
        copy["id"] = new_id
        copy["name"] = new_name
        copy["parents"] = new_parents
        copy["createdTime"] = _now_iso()
        copy["modifiedTime"] = copy["createdTime"]
        copy["version"] = "1"
        copy["webViewLink"] = _web_view_link(new_id, src.get("mimeType", ""))
        copy["webContentLink"] = _web_content_link(
            new_id, src.get("mimeType", ""), new_name)
        # Carry over content bytes / text but as independent values.
        if "_content" in src:
            copy["_content"] = src["_content"]
        if "_text" in src:
            copy["_text"] = src["_text"]
        s["files"][new_id] = copy
        s["permissions"].setdefault(new_id, {})
        owner_perm_id = _gen_permission_id(s)
        s["permissions"][new_id][owner_perm_id] = _make_permission(
            perm_id=owner_perm_id, type_="user", role="owner",
            email_address=s["about"]["user"]["emailAddress"],
            display_name=s["about"]["user"]["displayName"],
        )
        _record(s, "copy_file", source_id=fileId, file_id=new_id,
                name=new_name)
        _save_state(s)
        return _public_file(copy,
                             include_permissions=_attach_permissions(s, copy))


@mcp.tool(name="export_file")
def export_file(fileId: str, mimeType: str) -> dict:
    """Drive API v3: `files.export` —
    GET /drive/v3/files/{fileId}/export.

    Exports a Google-native file (doc/sheet/slides) to the requested
    mimeType. Real API returns raw bytes; the mock returns
    `{fileId, mimeType, content}` where `content` is a utf-8 string
    (or base64 if the export type is binary-ish; the mock keeps text
    when possible for ease of verification).
    """
    with _lock():
        s = _load_state()
        f = s["files"].get(fileId)
        if not f:
            _record(s, "export_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        if not _is_native(f.get("mimeType", "")):
            _record(s, "export_file", file_id=fileId,
                    result="not_native_doc")
            _save_state(s)
            return {
                "error": {
                    "code": 403,
                    "message":
                        "Export only supported for Google Docs Editors files.",
                    "status": "PERMISSION_DENIED",
                }
            }
        data, effective_mime = _export_native(f, mimeType)
        # Try to keep utf-8 text inline; otherwise base64.
        try:
            text = data.decode("utf-8")
            content_field: str = text
        except UnicodeDecodeError:
            content_field = _b64(data)
        _record(s, "export_file", file_id=fileId, mime=mimeType)
        _save_state(s)
        return {
            "fileId": fileId,
            "mimeType": effective_mime,
            "content": content_field,
        }


@mcp.tool(name="download_file")
def download_file(fileId: str) -> dict:
    """Non-standard MCP wrapper around `files.get?alt=media`.

    Real Drive returns binary HTTP; MCP cannot return raw bytes, so
    this wrapper returns `{fileId, mimeType, content, sizeBytes}`
    where `content` is a utf-8 string when decodable, else base64.
    """
    with _lock():
        s = _load_state()
        f = s["files"].get(fileId)
        if not f:
            _record(s, "download_file", file_id=fileId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        mime = f.get("mimeType", "")
        if mime == _FOLDER_MIME:
            _record(s, "download_file", file_id=fileId,
                    result="cannot_download_folder")
            _save_state(s)
            return {
                "error": {
                    "code": 403,
                    "message": "Cannot download a folder.",
                    "status": "PERMISSION_DENIED",
                }
            }
        if _is_native(mime):
            # Native doc download is exposed as plain text export.
            text = f.get("_text", "") or ""
            data = text.encode("utf-8")
            _record(s, "download_file", file_id=fileId, native=True)
            _save_state(s)
            return {
                "fileId": fileId,
                "mimeType": "text/plain",
                "content": text,
                "sizeBytes": len(data),
            }
        data = _b64d(f.get("_content", ""))
        try:
            text = data.decode("utf-8")
            content_field: str = text
        except UnicodeDecodeError:
            content_field = _b64(data)
        _record(s, "download_file", file_id=fileId, bytes=len(data))
        _save_state(s)
        return {
            "fileId": fileId,
            "mimeType": mime,
            "content": content_field,
            "sizeBytes": len(data),
        }


# ===========================================================================
# Permissions
# ===========================================================================

@mcp.tool(name="list_permissions")
def list_permissions(fileId: str,
                     pageSize: int = _DEFAULT_PAGE_SIZE,
                     pageToken: str | None = None,
                     fields: str | None = None) -> dict:
    """Drive API v3: `permissions.list` —
    GET /drive/v3/files/{fileId}/permissions."""
    with _lock():
        s = _load_state()
        if fileId not in s["files"]:
            _record(s, "list_permissions", file_id=fileId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        perms = list(s["permissions"].get(fileId, {}).values())
        # Stable order: owners first, then by id.
        role_order = {"owner": 0, "organizer": 1, "fileOrganizer": 2,
                        "writer": 3, "commenter": 4, "reader": 5}
        perms.sort(key=lambda p: (role_order.get(p.get("role"), 99),
                                    p.get("id", "")))
        page, nxt = _paginate(perms, pageSize, pageToken)
        result: dict[str, Any] = {
            "kind": "drive#permissionList",
            "permissions": list(page),
        }
        if nxt:
            result["nextPageToken"] = nxt
        _record(s, "list_permissions", file_id=fileId,
                count=len(page), total=len(perms))
        _save_state(s)
        return result


@mcp.tool(name="create_permission")
def create_permission(fileId: str,
                      role: str,
                      type: str,
                      emailAddress: str | None = None,
                      domain: str | None = None,
                      displayName: str | None = None) -> dict:
    """Drive API v3: `permissions.create` —
    POST /drive/v3/files/{fileId}/permissions.

    `role` is one of owner|organizer|fileOrganizer|writer|
    commenter|reader. `type` is one of user|group|domain|anyone.
    Returns the created Permission resource.
    """
    with _lock():
        s = _load_state()
        if fileId not in s["files"]:
            _record(s, "create_permission", file_id=fileId,
                    result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        if role not in {"owner", "organizer", "fileOrganizer",
                          "writer", "commenter", "reader"}:
            _record(s, "create_permission", file_id=fileId,
                    result="bad_role", role=role)
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": f"Invalid role: {role}",
                    "status": "INVALID_ARGUMENT",
                }
            }
        if type not in {"user", "group", "domain", "anyone"}:
            _record(s, "create_permission", file_id=fileId,
                    result="bad_type", type=type)
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": f"Invalid type: {type}",
                    "status": "INVALID_ARGUMENT",
                }
            }
        if type in ("user", "group") and not emailAddress:
            _record(s, "create_permission", file_id=fileId,
                    result="missing_email")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": "emailAddress is required for user/group.",
                    "status": "INVALID_ARGUMENT",
                }
            }
        if type == "domain" and not domain:
            _record(s, "create_permission", file_id=fileId,
                    result="missing_domain")
            _save_state(s)
            return {
                "error": {
                    "code": 400,
                    "message": "domain is required for domain permissions.",
                    "status": "INVALID_ARGUMENT",
                }
            }
        pid = _gen_permission_id(s)
        perm = _make_permission(
            perm_id=pid, type_=type, role=role,
            email_address=emailAddress or "",
            domain=domain or "",
            display_name=displayName or "",
        )
        s["permissions"].setdefault(fileId, {})[pid] = perm
        _record(s, "create_permission", file_id=fileId,
                permission_id=pid, role=role, type=type)
        _save_state(s)
        return dict(perm)


@mcp.tool(name="delete_permission")
def delete_permission(fileId: str, permissionId: str) -> dict:
    """Drive API v3: `permissions.delete` —
    DELETE /drive/v3/files/{fileId}/permissions/{permissionId}.

    Returns an empty dict on success."""
    with _lock():
        s = _load_state()
        if fileId not in s["files"]:
            _record(s, "delete_permission", file_id=fileId,
                    result="file_not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"File not found: {fileId}",
                    "status": "NOT_FOUND",
                }
            }
        perms = s["permissions"].get(fileId, {})
        if permissionId not in perms:
            _record(s, "delete_permission", file_id=fileId,
                    permission_id=permissionId, result="not_found")
            _save_state(s)
            return {
                "error": {
                    "code": 404,
                    "message": f"Permission not found: {permissionId}",
                    "status": "NOT_FOUND",
                }
            }
        del perms[permissionId]
        _record(s, "delete_permission", file_id=fileId,
                permission_id=permissionId)
        _save_state(s)
        return {}


# ===========================================================================
# Drives + About
# ===========================================================================

@mcp.tool(name="list_drives")
def list_drives(pageSize: int = 10,
                pageToken: str | None = None,
                q: str | None = None) -> dict:
    """Drive API v3: `drives.list` — GET /drive/v3/drives.

    Returns the shared drives the user can see. Empty in a fresh
    mock state until `mock_debug_state` / a seeder adds drives.
    """
    with _lock():
        s = _load_state()
        rows = list(s.get("drives", {}).values())
        if q:
            # Minimal: only `name = '...'`
            m = _NAME_EQ.match(q)
            if m:
                rows = [d for d in rows if d.get("name") == m.group(1)]
        rows.sort(key=lambda d: d.get("name") or "")
        page, nxt = _paginate(rows, pageSize, pageToken)
        result: dict[str, Any] = {
            "kind": "drive#driveList",
            "drives": list(page),
        }
        if nxt:
            result["nextPageToken"] = nxt
        _record(s, "list_drives", count=len(page))
        _save_state(s)
        return result


@mcp.tool(name="get_about")
def get_about(fields: str | None = None) -> dict:
    """Drive API v3: `about.get` — GET /drive/v3/about.

    Returns user + storage-quota info."""
    with _lock():
        s = _load_state()
        _record(s, "get_about")
        _save_state(s)
        return dict(s.get("about") or _default_about())


# ===========================================================================
# Mock-only debug helpers
# ===========================================================================

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not part of the real
    Drive API surface."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed_file")
def mock_debug_seed_file(fileId: str | None = None,
                         name: str = "",
                         mimeType: str = "text/plain",
                         parents: list[str] | None = None,
                         content: str = "",
                         description: str = "",
                         starred: bool = False,
                         trashed: bool = False,
                         createdTime: str | None = None,
                         modifiedTime: str | None = None,
                         properties: dict | None = None,
                         appProperties: dict | None = None,
                         ownerEmail: str | None = None,
                         ownerName: str | None = None) -> dict:
    """Mock-only: insert a fully-formed File fixture (non-folder).

    For native types (doc/sheet/slides) `content` becomes the
    editable text. For other types `content` is treated as utf-8
    text bytes (use base64 in callers that need binary)."""
    with _lock():
        s = _load_state()
        fid = fileId or _gen_file_id()
        if mimeType == _FOLDER_MIME:
            f = _new_file(file_id=fid, name=name, mime_type=mimeType,
                            parents=parents, description=description,
                            created_time=createdTime,
                            modified_time=modifiedTime,
                            starred=starred, trashed=trashed,
                            properties=properties,
                            app_properties=appProperties,
                            owner_email=ownerEmail
                              or s["about"]["user"]["emailAddress"],
                            owner_name=ownerName
                              or s["about"]["user"]["displayName"])
        elif _is_native(mimeType):
            f = _new_file(file_id=fid, name=name, mime_type=mimeType,
                            parents=parents, text=content,
                            description=description,
                            created_time=createdTime,
                            modified_time=modifiedTime,
                            starred=starred, trashed=trashed,
                            properties=properties,
                            app_properties=appProperties,
                            owner_email=ownerEmail
                              or s["about"]["user"]["emailAddress"],
                            owner_name=ownerName
                              or s["about"]["user"]["displayName"])
        else:
            f = _new_file(file_id=fid, name=name, mime_type=mimeType,
                            parents=parents,
                            content=(content or "").encode("utf-8"),
                            description=description,
                            created_time=createdTime,
                            modified_time=modifiedTime,
                            starred=starred, trashed=trashed,
                            properties=properties,
                            app_properties=appProperties,
                            owner_email=ownerEmail
                              or s["about"]["user"]["emailAddress"],
                            owner_name=ownerName
                              or s["about"]["user"]["displayName"])
        s["files"][fid] = f
        s["permissions"].setdefault(fid, {})
        owner_perm_id = _gen_permission_id(s)
        s["permissions"][fid][owner_perm_id] = _make_permission(
            perm_id=owner_perm_id, type_="user", role="owner",
            email_address=f["owners"][0]["emailAddress"],
            display_name=f["owners"][0]["displayName"],
        )
        _record(s, "debug_seed_file", file_id=fid, name=name,
                mime_type=mimeType)
        _save_state(s)
        return _public_file(f,
                             include_permissions=_attach_permissions(s, f))


@_debug_tool(name="mock_debug_seed_folder")
def mock_debug_seed_folder(folderId: str | None = None,
                           name: str = "",
                           parents: list[str] | None = None,
                           description: str = "",
                           starred: bool = False,
                           trashed: bool = False,
                           createdTime: str | None = None,
                           modifiedTime: str | None = None) -> dict:
    """Mock-only: insert a folder fixture."""
    return mock_debug_seed_file(
        fileId=folderId, name=name, mimeType=_FOLDER_MIME,
        parents=parents, description=description,
        starred=starred, trashed=trashed,
        createdTime=createdTime, modifiedTime=modifiedTime,
    )


@_debug_tool(name="mock_debug_seed_permission")
def mock_debug_seed_permission(fileId: str,
                               type: str,
                               role: str,
                               emailAddress: str | None = None,
                               domain: str | None = None,
                               displayName: str | None = None,
                               permissionId: str | None = None) -> dict:
    """Mock-only: insert a Permission fixture on `fileId`."""
    with _lock():
        s = _load_state()
        if fileId not in s["files"]:
            _record(s, "debug_seed_permission", file_id=fileId,
                    result="file_not_found")
            _save_state(s)
            return {"error": f"File not found: {fileId}"}
        pid = permissionId or _gen_permission_id(s)
        perm = _make_permission(
            perm_id=pid, type_=type, role=role,
            email_address=emailAddress or "",
            domain=domain or "",
            display_name=displayName or "",
        )
        s["permissions"].setdefault(fileId, {})[pid] = perm
        _record(s, "debug_seed_permission", file_id=fileId,
                permission_id=pid, role=role, type=type)
        _save_state(s)
        return dict(perm)


if __name__ == "__main__":
    mcp.run()
