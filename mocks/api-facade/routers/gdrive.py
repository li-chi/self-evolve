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


# ---------------------------------------------------------------------------
# Sheets federation. In Google, spreadsheets and their folders ARE Drive
# files; here they live in the google-sheets-mock state (so the agent's
# sheet MCP tools and gspread see them). The Drive surface upstream's
# drive_helper uses (find/create/clear folder, get name, copy into folder)
# is served from that state for folder/spreadsheet ids the drive mock does
# not own.
# ---------------------------------------------------------------------------

def _gs():
    import gsheets
    return gsheets.gs


def _sheet_as_drive_file(ss):
    return {"kind": "drive#file", "id": ss["spreadsheetId"],
            "name": ss.get("properties", {}).get("title", ""),
            "mimeType": SHEET_MIME,
            "parents": [ss.get("folder_id")] if ss.get("folder_id") else []}


def _folder_as_drive_file(f):
    return {"kind": "drive#file", "id": f["id"], "name": f.get("name", ""),
            "mimeType": FOLDER_MIME,
            "parents": [f.get("parent")] if f.get("parent") else []}


def _sheets_files(gstate):
    out = {}
    for f in gstate.get("folders", {}).values():
        out[f["id"]] = _folder_as_drive_file(f)
    for ss in gstate.get("spreadsheets", {}).values():
        out[ss["spreadsheetId"]] = _sheet_as_drive_file(ss)
    return out


def _q_matches(public, q):
    """Match the tiny Drive-q subset drive_helper uses:
    name='X' / mimeType='Y' / 'folderid' in parents, joined by and."""
    if not q:
        return True
    import re
    for clause in [c.strip() for c in q.split(" and ")]:
        m = re.match(r"name\s*=\s*'(.*)'$", clause)
        if m:
            if public["name"] != m.group(1):
                return False
            continue
        m = re.match(r"mimeType\s*=\s*'(.*)'$", clause)
        if m:
            if public["mimeType"] != m.group(1):
                return False
            continue
        m = re.match(r"'(.*)'\s+in\s+parents$", clause)
        if m:
            if m.group(1) not in (public.get("parents") or []):
                return False
            continue
        m = re.match(r"trashed\s*=\s*(true|false)$", clause)
        if m:
            if (m.group(1) == "true") != bool(public.get("trashed")):
                return False
            continue
        return False  # unknown clause: be conservative, no match
    return True


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
            gstate = _gs()._load_state()
            out += [p for p in _sheets_files(gstate).values()
                    if _q_matches(p, q)]
            return 200, {"kind": "drive#fileList", "files": out,
                         "incompleteSearch": False}

        if file_id is None and method == "POST":
            if body.get("mimeType") == FOLDER_MIME:
                # Folders live in the sheets-mock state so the agent's
                # sheet tools (list_folders, create_spreadsheet in folder)
                # see the same folder the grader created.
                gsmod = _gs()
                gstate = gsmod._load_state()
                n = gstate.setdefault("next_id", {}).get("folder", 1)
                gstate["next_id"]["folder"] = n + 1
                folder = {"id": f"folder_{n:04d}",
                          "name": body.get("name", "Untitled"),
                          "parent": (body.get("parents") or [None])[0]}
                gstate.setdefault("folders", {})[folder["id"]] = folder
                gsmod._save_state(gstate)
                return 200, _folder_as_drive_file(folder)
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
            gsmod = _gs()
            gstate = gsmod._load_state()
            ss = gstate.get("spreadsheets", {}).get(file_id)
            folder = gstate.get("folders", {}).get(file_id)

            if ss is not None or folder is not None:
                if len(parts) == 2 and method == "GET":
                    return 200, (_sheet_as_drive_file(ss) if ss
                                 else _folder_as_drive_file(folder))
                if len(parts) == 2 and method == "DELETE":
                    if ss is not None:
                        del gstate["spreadsheets"][file_id]
                    else:
                        del gstate["folders"][file_id]
                    gsmod._save_state(gstate)
                    return 204, {}
                if len(parts) == 2 and method in ("PATCH", "PUT"):
                    rec = ss if ss is not None else folder
                    if "name" in body:
                        if ss is not None:
                            rec.setdefault("properties", {})["title"] = \
                                body["name"]
                        else:
                            rec["name"] = body["name"]
                    if "parents" in body and body["parents"]:
                        if ss is not None:
                            rec["folder_id"] = body["parents"][0]
                        else:
                            rec["parent"] = body["parents"][0]
                    gsmod._save_state(gstate)
                    return 200, (_sheet_as_drive_file(ss) if ss
                                 else _folder_as_drive_file(folder))
                if (len(parts) == 3 and parts[2] == "permissions"
                        and method == "POST"):
                    perm = gd._make_permission(
                        perm_id=gd._gen_permission_id(state),
                        type_=body.get("type", "anyone"),
                        role=body.get("role", "reader"),
                        email_address=body.get("emailAddress", ""))
                    return 200, perm
                if (len(parts) == 3 and parts[2] == "copy"
                        and method == "POST" and ss is not None):
                    import copy as _copy
                    n = gstate.setdefault("next_id", {}).get("spreadsheet", 1)
                    gstate["next_id"]["spreadsheet"] = n + 1
                    new_id = f"sheet_{n:04d}"
                    new_ss = _copy.deepcopy(ss)
                    new_ss["spreadsheetId"] = new_id
                    new_ss["spreadsheetUrl"] = \
                        f"https://docs.google.com/spreadsheets/d/{new_id}/edit"
                    if body.get("name"):
                        new_ss.setdefault("properties", {})["title"] = \
                            body["name"]
                    new_ss["folder_id"] = (body.get("parents") or [None])[0]
                    gstate["spreadsheets"][new_id] = new_ss
                    gsmod._save_state(gstate)
                    return 200, _sheet_as_drive_file(new_ss)

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
