"""Google Sheets mock MCP server.

Mirrors `mcp-google-sheets` (xing5/mcp-google-sheets), which is what
Toolathlon uses as its `google_sheet` server. Every tool name and
parameter matches the official server; responses match the Google
Sheets API v4 response shapes that the official server returns.

In-memory model — one JSON file at $GSHEETS_MOCK_STATE_DIR/state.json:

  state = {
    "folders": {"<id>": {"id","name","parent"}},
    "spreadsheets": {
      "<id>": {
        "spreadsheetId",
        "properties": {"title","locale","autoRecalc","timeZone"},
        "spreadsheetUrl",
        "folder_id": "<id or null>",
        "sheets": [
          {"properties": {"sheetId","title","index","sheetType","gridProperties":{...}},
           "data": [[cell, cell, ...], ...]}    # parallel storage: values only
        ]
      }
    },
    "next_id": {"spreadsheet": N, "sheet": N, "folder": N},
    "calls": [...]
  }

The `data` 2D array is the source of truth for cell values; cells are
stored as either str/number/bool. Formulas (strings starting with `=`)
are stored verbatim and returned as such by both get_sheet_data and
get_sheet_formulas (the mock does not evaluate formulas).

Tools implemented (15, plus 2 debug):

  get_sheet_data, get_sheet_formulas, update_cells, batch_update_cells,
  add_rows, add_columns, list_sheets, copy_sheet, rename_sheet,
  create_sheet, create_spreadsheet, list_spreadsheets,
  search_spreadsheets, get_spreadsheet_info, find_in_spreadsheet,
  get_multiple_sheet_data, list_folders
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


def _state_path() -> str:
    state_dir = os.environ.get(
        "GSHEETS_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/gsheets_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _empty_state() -> dict:
    return {
        "folders": {},
        "spreadsheets": {},
        "next_id": {"spreadsheet": 1, "sheet": 1, "folder": 1},
        "calls": [],
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("GSHEETS_MOCK_SEED_PATH")
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


def _next_id(state: dict, kind: str) -> int:
    n = state["next_id"].get(kind, 1)
    state["next_id"][kind] = n + 1
    return n


def _gen_spreadsheet_id() -> str:
    import secrets
    return secrets.token_urlsafe(33)


# ---------------------------------------------------------------------------
# A1 notation parser
# ---------------------------------------------------------------------------

_A1_RE = re.compile(
    r"^(?:'((?:[^']|'')+)'|([A-Za-z0-9_]+))?"
    r"(?:!?([A-Z]+)?(\d+)?(?::([A-Z]+)?(\d+)?)?)?$"
)


def _col_to_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _idx_to_col(idx: int) -> str:
    letters = ""
    n = idx + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        letters = chr(ord("A") + r) + letters
    return letters


def _parse_range(s: str) -> tuple[str | None, int | None, int | None,
                                  int | None, int | None]:
    """Parse "Sheet!A1:C10" or "A1:C10" or "Sheet" → (sheet, r1, c1, r2, c2)."""
    sheet = None
    rng = s
    if "!" in s:
        sheet, rng = s.split("!", 1)
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    if not rng:
        return sheet, None, None, None, None
    parts = rng.split(":")
    a = parts[0]
    b = parts[1] if len(parts) > 1 else None
    return (sheet, *_split_a1(a), *_split_a1(b)) if b is not None \
        else (sheet, *_split_a1(a), *_split_a1(a))


def _split_a1(s: str | None) -> tuple[int | None, int | None]:
    if s is None:
        return None, None
    m = re.match(r"^([A-Za-z]*)(\d*)$", s)
    if not m:
        return None, None
    col_s, row_s = m.group(1), m.group(2)
    row = int(row_s) - 1 if row_s else None
    col = _col_to_idx(col_s) if col_s else None
    return row, col


def _find_sheet(ss: dict, name: str | None) -> dict | None:
    if name is None and ss["sheets"]:
        return ss["sheets"][0]
    for sh in ss["sheets"]:
        if sh["properties"]["title"] == name:
            return sh
    return None


def _ensure_size(data: list, rows: int, cols: int) -> None:
    while len(data) < rows:
        data.append([])
    for row in data:
        while len(row) < cols:
            row.append("")


def _read_range(sh: dict, r1: int | None, c1: int | None,
                r2: int | None, c2: int | None) -> list[list[Any]]:
    data = sh["data"]
    if not data:
        return []
    if r1 is None:
        r1, r2 = 0, len(data) - 1
    if c1 is None:
        c1, c2 = 0, max((len(row) for row in data), default=0) - 1
    if r2 is None:
        r2 = r1
    if c2 is None:
        c2 = c1
    out = []
    for r in range(r1, min(r2, len(data) - 1) + 1):
        row = data[r]
        out.append([row[c] if c < len(row) else ""
                    for c in range(c1, c2 + 1)])
    while out and all(_blank(v) for v in out[-1]):
        out.pop()
    if out:
        max_used = max((len(row) for row in out), default=0)
        out = [row[:max_used] for row in out]
    return out


def _blank(v: Any) -> bool:
    return v == "" or v is None


def _write_range(sh: dict, r1: int, c1: int,
                 values: list[list[Any]]) -> tuple[int, int]:
    rows = len(values)
    cols = max((len(row) for row in values), default=0)
    _ensure_size(sh["data"], r1 + rows, c1 + cols)
    for i, row in enumerate(values):
        for j, v in enumerate(row):
            sh["data"][r1 + i][c1 + j] = v
    sh["properties"].setdefault("gridProperties", {})
    gp = sh["properties"]["gridProperties"]
    gp["rowCount"] = max(gp.get("rowCount", 0), r1 + rows, 1000)
    gp["columnCount"] = max(gp.get("columnCount", 0), c1 + cols, 26)
    return rows, cols


def _new_sheet(state: dict, title: str, rows: int = 1000,
               cols: int = 26, index: int = 0) -> dict:
    sid = _next_id(state, "sheet")
    return {
        "properties": {
            "sheetId": sid, "title": title, "index": index,
            "sheetType": "GRID",
            "gridProperties": {"rowCount": rows, "columnCount": cols},
        },
        "data": [["" for _ in range(cols)] for _ in range(min(rows, 50))],
    }


mcp = FastMCP("google-sheets-mock")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@mcp.tool(name="get_sheet_data")
def get_sheet_data(spreadsheet_id: str, sheet: str,
                   range: str | None = None,
                   include_grid_data: bool = False) -> dict:
    """Get values (or grid data) from a sheet. Matches the
    `mcp-google-sheets` `get_sheet_data` tool.

    Returns (default, include_grid_data=False):
      {"spreadsheetId": "...",
       "valueRanges": [{"range": "Sheet!A1:C10", "values": [[...]]}]}

    With include_grid_data=True, returns the raw
    `spreadsheets.get(ranges=[...], includeGridData=True)` shape.
    """
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            _record(s, "get_sheet_data",
                    spreadsheet_id=spreadsheet_id, result="not_found")
            _save_state(s)
            return {"error": f"Requested entity was not found: {spreadsheet_id}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            _record(s, "get_sheet_data",
                    spreadsheet_id=spreadsheet_id, sheet=sheet,
                    result="sheet_not_found")
            _save_state(s)
            return {"error": f"Unable to parse range: {sheet}"}
        full_range = f"{sheet}!{range}" if range else sheet
        r1, c1, r2, c2 = (None, None, None, None)
        if range:
            _, r1, c1, r2, c2 = _parse_range(range)
        values = _read_range(sh, r1, c1, r2, c2)
        _record(s, "get_sheet_data", spreadsheet_id=spreadsheet_id,
                sheet=sheet, range=range,
                cells=sum(len(r) for r in values))
        _save_state(s)
        if include_grid_data:
            return {
                "spreadsheetId": spreadsheet_id,
                "properties": ss["properties"],
                "sheets": [{
                    "properties": sh["properties"],
                    "data": [{
                        "rowData": [{
                            "values": [{"formattedValue": str(v) if v != "" else "",
                                        "userEnteredValue":
                                            _user_entered(v)}
                                       for v in row],
                        } for row in values],
                    }],
                }],
                "spreadsheetUrl": ss["spreadsheetUrl"],
            }
        return {
            "spreadsheetId": spreadsheet_id,
            "valueRanges": [{"range": full_range, "values": values}],
        }


def _user_entered(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, (int, float)):
        return {"numberValue": float(v)}
    s = str(v)
    if s.startswith("="):
        return {"formulaValue": s}
    return {"stringValue": s}


@mcp.tool(name="get_sheet_formulas")
def get_sheet_formulas(spreadsheet_id: str, sheet: str,
                       range: str | None = None) -> list:
    """Get formulas (cells starting with `=`) from a sheet. Returns a
    2D array matching `mcp-google-sheets` `get_sheet_formulas`."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return [[]]
        sh = _find_sheet(ss, sheet)
        if not sh:
            return [[]]
        r1, c1, r2, c2 = (None, None, None, None)
        if range:
            _, r1, c1, r2, c2 = _parse_range(range)
        values = _read_range(sh, r1, c1, r2, c2)
        out = [[v if isinstance(v, str) and v.startswith("=") else ""
                for v in row] for row in values]
        _record(s, "get_sheet_formulas", spreadsheet_id=spreadsheet_id,
                sheet=sheet)
        _save_state(s)
        return out


@mcp.tool(name="get_multiple_sheet_data")
def get_multiple_sheet_data(queries: list[dict]) -> list[dict]:
    """Batch-read multiple (spreadsheet, sheet, range) queries.
    Each query: {"spreadsheet_id","sheet","range"}. Returns a list of
    {"spreadsheet_id","sheet","range","values"|"error"}."""
    out = []
    for q in queries or []:
        try:
            res = get_sheet_data(q["spreadsheet_id"], q["sheet"],
                                 q.get("range"))
            if "valueRanges" in res:
                out.append({**q, "values": res["valueRanges"][0]["values"]})
            else:
                out.append({**q, "error": res.get("error", "unknown error")})
        except Exception as e:
            out.append({**q, "error": str(e)})
    return out


@mcp.tool(name="list_sheets")
def list_sheets(spreadsheet_id: str) -> list[str]:
    """List sheet (tab) names in a spreadsheet."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        _record(s, "list_sheets", spreadsheet_id=spreadsheet_id,
                result="ok" if ss else "not_found")
        _save_state(s)
        if not ss:
            return []
        return [sh["properties"]["title"] for sh in ss["sheets"]]


@mcp.tool(name="get_spreadsheet_info")
def get_spreadsheet_info(spreadsheet_id: str) -> str:
    """Return a human-readable summary of the spreadsheet (title +
    sheets + row/col counts). Matches `mcp-google-sheets` shape (string)."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        _record(s, "get_spreadsheet_info",
                spreadsheet_id=spreadsheet_id,
                result="ok" if ss else "not_found")
        _save_state(s)
        if not ss:
            return f"Spreadsheet not found: {spreadsheet_id}"
        title = ss["properties"]["title"]
        lines = [f"Spreadsheet: {title}", f"ID: {spreadsheet_id}",
                 f"URL: {ss['spreadsheetUrl']}", "",
                 f"Sheets ({len(ss['sheets'])}):"]
        for sh in ss["sheets"]:
            p = sh["properties"]
            gp = p.get("gridProperties", {})
            lines.append(f"  - {p['title']} ({gp.get('rowCount',0)} rows × "
                         f"{gp.get('columnCount',0)} cols)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@mcp.tool(name="update_cells")
def update_cells(spreadsheet_id: str, sheet: str, range: str,
                 data: list[list[Any]]) -> dict:
    """Write values to a range. Returns the
    spreadsheets.values.update API response shape."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet_id}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            return {"error": f"sheet not found: {sheet}"}
        _, r1, c1, _, _ = _parse_range(range)
        if r1 is None or c1 is None:
            return {"error": f"invalid range: {range}"}
        rows, cols = _write_range(sh, r1, c1, data)
        full_range = f"{sheet}!{range}"
        _record(s, "update_cells", spreadsheet_id=spreadsheet_id,
                sheet=sheet, range=range, rows=rows, cols=cols)
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet_id,
            "updatedRange": full_range,
            "updatedRows": rows,
            "updatedColumns": cols,
            "updatedCells": rows * cols,
        }


@mcp.tool(name="batch_update_cells")
def batch_update_cells(spreadsheet_id: str, sheet: str,
                       ranges: dict[str, list[list[Any]]]) -> dict:
    """Batch update multiple ranges. Returns spreadsheets.values.batchUpdate
    response shape."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet_id}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            return {"error": f"sheet not found: {sheet}"}
        responses = []
        total_rows = total_cols = total_cells = 0
        for rng, data in (ranges or {}).items():
            _, r1, c1, _, _ = _parse_range(rng)
            if r1 is None or c1 is None:
                continue
            rows, cols = _write_range(sh, r1, c1, data)
            full_range = f"{sheet}!{rng}"
            responses.append({
                "spreadsheetId": spreadsheet_id,
                "updatedRange": full_range,
                "updatedRows": rows, "updatedColumns": cols,
                "updatedCells": rows * cols,
            })
            total_rows += rows
            total_cols += cols
            total_cells += rows * cols
        _record(s, "batch_update_cells",
                spreadsheet_id=spreadsheet_id, sheet=sheet,
                ranges=list((ranges or {}).keys()))
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet_id,
            "totalUpdatedRows": total_rows,
            "totalUpdatedColumns": total_cols,
            "totalUpdatedCells": total_cells,
            "totalUpdatedSheets": 1 if responses else 0,
            "responses": responses,
        }


@mcp.tool(name="add_rows")
def add_rows(spreadsheet_id: str, sheet: str, count: int,
             start_row: int | None = None) -> dict:
    """Insert `count` rows at `start_row` (0-based). Returns the
    raw batchUpdate result."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet_id}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            return {"error": f"Sheet '{sheet}' not found"}
        start = start_row if start_row is not None else 0
        cols = max((len(r) for r in sh["data"]), default=0) or \
            sh["properties"]["gridProperties"].get("columnCount", 26)
        for _ in range(int(count)):
            sh["data"].insert(start, ["" for _ in range(cols)])
        gp = sh["properties"].setdefault("gridProperties", {})
        gp["rowCount"] = gp.get("rowCount", 1000) + int(count)
        _record(s, "add_rows", spreadsheet_id=spreadsheet_id,
                sheet=sheet, count=count, start_row=start)
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet_id,
            "replies": [{}],
            "_inserted": {"sheet": sheet, "count": count,
                          "start_row": start},
        }


@mcp.tool(name="add_columns")
def add_columns(spreadsheet_id: str, sheet: str, count: int,
                start_column: int | None = None) -> dict:
    """Insert `count` columns at `start_column` (0-based)."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet_id}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            return {"error": f"Sheet '{sheet}' not found"}
        start = start_column if start_column is not None else 0
        for row in sh["data"]:
            for _ in range(int(count)):
                row.insert(start, "")
        gp = sh["properties"].setdefault("gridProperties", {})
        gp["columnCount"] = gp.get("columnCount", 26) + int(count)
        _record(s, "add_columns", spreadsheet_id=spreadsheet_id,
                sheet=sheet, count=count, start_column=start)
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet_id,
            "replies": [{}],
            "_inserted": {"sheet": sheet, "count": count,
                          "start_column": start},
        }


# ---------------------------------------------------------------------------
# Sheet management
# ---------------------------------------------------------------------------

@mcp.tool(name="create_sheet")
def create_sheet(spreadsheet_id: str, title: str,
                 rows: int = 1000, cols: int = 26) -> dict:
    """Create a new sheet (tab) within an existing spreadsheet."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet_id}"}
        if any(sh["properties"]["title"] == title for sh in ss["sheets"]):
            return {"error": f"sheet '{title}' already exists"}
        new = _new_sheet(s, title, rows=rows, cols=cols,
                         index=len(ss["sheets"]))
        ss["sheets"].append(new)
        _record(s, "create_sheet", spreadsheet_id=spreadsheet_id,
                title=title, sheet_id=new["properties"]["sheetId"])
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet_id,
            "replies": [{"addSheet": {"properties": new["properties"]}}],
        }


@mcp.tool(name="copy_sheet")
def copy_sheet(src_spreadsheet: str, src_sheet: str,
               dst_spreadsheet: str, dst_sheet: str) -> dict:
    """Copy a sheet from one spreadsheet to another, renaming on copy."""
    with _lock():
        s = _load_state()
        src_ss = s["spreadsheets"].get(src_spreadsheet)
        dst_ss = s["spreadsheets"].get(dst_spreadsheet)
        if not src_ss:
            return {"error": f"Source sheet '{src_sheet}' not found"}
        if not dst_ss:
            return {"error": f"dst spreadsheet not found: {dst_spreadsheet}"}
        src = _find_sheet(src_ss, src_sheet)
        if not src:
            return {"error": f"Source sheet '{src_sheet}' not found"}
        if any(sh["properties"]["title"] == dst_sheet for sh in dst_ss["sheets"]):
            new_title = f"{dst_sheet} (copy)"
        else:
            new_title = dst_sheet
        new_sid = _next_id(s, "sheet")
        copy = {
            "properties": {
                "sheetId": new_sid, "title": new_title,
                "index": len(dst_ss["sheets"]),
                "sheetType": src["properties"].get("sheetType", "GRID"),
                "gridProperties": dict(src["properties"].get(
                    "gridProperties", {})),
            },
            "data": [list(row) for row in src["data"]],
        }
        dst_ss["sheets"].append(copy)
        _record(s, "copy_sheet", src_spreadsheet=src_spreadsheet,
                src_sheet=src_sheet, dst_spreadsheet=dst_spreadsheet,
                dst_sheet=new_title)
        _save_state(s)
        return {
            "copy": {"sheetId": new_sid, "title": new_title,
                     "index": copy["properties"]["index"]},
            **({"rename": {"replies": [{"updateSheetProperties": {}}]}}
               if new_title != dst_sheet else {}),
        }


@mcp.tool(name="rename_sheet")
def rename_sheet(spreadsheet: str, sheet: str, new_name: str) -> dict:
    """Rename a sheet within a spreadsheet."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet)
        if not ss:
            return {"error": f"spreadsheet not found: {spreadsheet}"}
        sh = _find_sheet(ss, sheet)
        if not sh:
            return {"error": f"Sheet '{sheet}' not found"}
        sh["properties"]["title"] = new_name
        _record(s, "rename_sheet", spreadsheet_id=spreadsheet,
                old=sheet, new=new_name)
        _save_state(s)
        return {
            "spreadsheetId": spreadsheet,
            "replies": [{"updateSheetProperties": {
                "properties": dict(sh["properties"]),
                "fields": "title"}}],
        }


# ---------------------------------------------------------------------------
# Spreadsheet (file) management — Drive-shaped operations
# ---------------------------------------------------------------------------

@mcp.tool(name="create_spreadsheet")
def create_spreadsheet(title: str, folder_id: str | None = None) -> dict:
    """Create a new spreadsheet. If `folder_id` is provided the
    spreadsheet is placed in that Drive folder."""
    with _lock():
        s = _load_state()
        sid = _gen_spreadsheet_id()
        first = _new_sheet(s, "Sheet1")
        ss = {
            "spreadsheetId": sid,
            "properties": {"title": title, "locale": "en_US",
                           "autoRecalc": "ON_CHANGE",
                           "timeZone": "America/Los_Angeles"},
            "spreadsheetUrl":
                f"https://docs.google.com/spreadsheets/d/{sid}/edit",
            "folder_id": folder_id,
            "sheets": [first],
        }
        s["spreadsheets"][sid] = ss
        _record(s, "create_spreadsheet", spreadsheet_id=sid,
                title=title, folder_id=folder_id)
        _save_state(s)
        return {"spreadsheetId": sid, "title": title,
                "spreadsheetUrl": ss["spreadsheetUrl"]}


@mcp.tool(name="list_spreadsheets")
def list_spreadsheets(folder_id: str | None = None) -> list[dict]:
    """List spreadsheets visible to the user. Filter by `folder_id`
    when set."""
    with _lock():
        s = _load_state()
        items = []
        for sid, ss in s["spreadsheets"].items():
            if folder_id is not None and ss.get("folder_id") != folder_id:
                continue
            items.append({"id": sid, "name": ss["properties"]["title"]})
        _record(s, "list_spreadsheets", folder_id=folder_id,
                count=len(items))
        _save_state(s)
        return items


@mcp.tool(name="search_spreadsheets")
def search_spreadsheets(query: str,
                        folder_id: str | None = None) -> list[dict]:
    """Search spreadsheets by name substring (case-insensitive)."""
    with _lock():
        s = _load_state()
        q = (query or "").lower()
        items = []
        for sid, ss in s["spreadsheets"].items():
            if folder_id is not None and ss.get("folder_id") != folder_id:
                continue
            if q and q not in ss["properties"]["title"].lower():
                continue
            items.append({"id": sid, "name": ss["properties"]["title"]})
        _record(s, "search_spreadsheets", query=query,
                count=len(items))
        _save_state(s)
        return items


@mcp.tool(name="list_folders")
def list_folders(parent_folder_id: str | None = None) -> list[dict]:
    """List Drive folders, optionally under `parent_folder_id`."""
    with _lock():
        s = _load_state()
        items = []
        for fid, f in s["folders"].items():
            if parent_folder_id is not None and f.get("parent") != parent_folder_id:
                continue
            items.append({"id": fid, "name": f["name"]})
        _record(s, "list_folders", parent_folder_id=parent_folder_id)
        _save_state(s)
        return items


@mcp.tool(name="find_in_spreadsheet")
def find_in_spreadsheet(spreadsheet_id: str, query: str,
                        sheet: str | None = None,
                        match_case: bool = False,
                        match_entire_cell: bool = False) -> list[dict]:
    """Find cells containing `query`. Returns
    [{"sheet","row","col","a1","value"}]."""
    with _lock():
        s = _load_state()
        ss = s["spreadsheets"].get(spreadsheet_id)
        if not ss:
            return []
        out = []
        sheets = ([sh for sh in ss["sheets"]
                   if sh["properties"]["title"] == sheet]
                  if sheet else ss["sheets"])
        for sh in sheets:
            for r, row in enumerate(sh["data"]):
                for c, v in enumerate(row):
                    if v == "" or v is None:
                        continue
                    sv = str(v)
                    cv, qv = (sv, query) if match_case \
                        else (sv.lower(), (query or "").lower())
                    matched = (cv == qv if match_entire_cell
                               else qv in cv)
                    if matched:
                        out.append({
                            "sheet": sh["properties"]["title"],
                            "row": r, "col": c,
                            "a1": f"{_idx_to_col(c)}{r+1}",
                            "value": v,
                        })
        _record(s, "find_in_spreadsheet",
                spreadsheet_id=spreadsheet_id, query=query,
                hits=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not in mcp-google-sheets."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_spreadsheet")
def mock_debug_seed_spreadsheet(spreadsheet_id: str, title: str,
                                sheets: list[dict],
                                folder_id: str | None = None) -> dict:
    """Mock-only: insert a fully-formed spreadsheet for fixtures.

    `sheets` is a list of {"title": "...", "data": [[...]]} dicts.
    """
    with _lock():
        s = _load_state()
        sh_list = []
        for i, sh in enumerate(sheets or []):
            sid = _next_id(s, "sheet")
            sh_list.append({
                "properties": {
                    "sheetId": sid,
                    "title": sh.get("title", f"Sheet{i+1}"),
                    "index": i, "sheetType": "GRID",
                    "gridProperties": {
                        "rowCount": max(len(sh.get("data", [])), 1000),
                        "columnCount": max(
                            (len(r) for r in sh.get("data", [])),
                            default=26),
                    },
                },
                "data": [list(row) for row in sh.get("data", [])],
            })
        s["spreadsheets"][spreadsheet_id] = {
            "spreadsheetId": spreadsheet_id,
            "properties": {"title": title, "locale": "en_US",
                           "autoRecalc": "ON_CHANGE",
                           "timeZone": "America/Los_Angeles"},
            "spreadsheetUrl":
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            "folder_id": folder_id,
            "sheets": sh_list,
        }
        _record(s, "debug_seed_spreadsheet",
                spreadsheet_id=spreadsheet_id, title=title,
                tabs=[sh["properties"]["title"] for sh in sh_list])
        _save_state(s)
        return {"spreadsheetId": spreadsheet_id,
                "title": title,
                "sheets": [sh["properties"]["title"] for sh in sh_list]}


if __name__ == "__main__":
    mcp.run()
