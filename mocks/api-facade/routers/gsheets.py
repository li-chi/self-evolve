"""Google Sheets v4 served from the google-sheets-mock state.

`gspread` (used by upstream graders through
`utils.app_specific.googlesheet.drive_helper`) talks to
sheets.googleapis.com with `requests`; netredirect points that host here.
Cell storage and A1 parsing come from the mock module, so the agent's MCP
tools and the grader's gspread client read the same grid.
"""

from __future__ import annotations

import sys

from mockmod import load as _load_mock  # noqa: E402

gs = _load_mock("google-sheets-mock")


def _spreadsheet(state, sid):
    return state.get("spreadsheets", {}).get(sid)


def _grid(sheet):
    return sheet.setdefault("data", [])


def _values(sheet, rng: str | None):
    """Return the A1 range's values, trimming trailing empty rows/cols."""
    r1, c1, r2, c2 = 0, 0, None, None
    if rng and "!" in rng:
        rng = rng.split("!", 1)[1]
    if rng:
        parsed = gs._parse_range(rng)
        _sheet_name, r1, c1, r2, c2 = (parsed if len(parsed) == 5
                                       else (None, *parsed))
        r1 = (r1 or 1) - 1
        c1 = (c1 or 1) - 1
    rows = _grid(sheet)[r1:(r2 if r2 else None)]
    out = []
    for row in rows:
        out.append([("" if v is None else v)
                    for v in row[c1:(c2 if c2 else None)]])
    while out and not any(str(v).strip() for v in out[-1]):
        out.pop()
    return [[*r] for r in out]


def handle(method: str, path: str, query: dict, body, headers: dict):
    state = gs._load_state()
    body = body if isinstance(body, dict) else {}
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["v4"]:
        parts = parts[1:]
    if parts[:1] != ["spreadsheets"]:
        raise NotImplementedError(f"sheets facade: {method} {path}")

    # POST /v4/spreadsheets  -> create
    if len(parts) == 1 and method == "POST":
        title = ((body.get("properties") or {}).get("title")
                 or "Untitled spreadsheet")
        sid = f"mock-sheet-{gs._next_id(state, 'spreadsheet')}"
        sheets = [gs._new_sheet(state, s.get("properties", {}).get(
            "title", "Sheet1"), index=i)
            for i, s in enumerate(body.get("sheets") or [{}])]
        ss = {"spreadsheetId": sid,
              "properties": {"title": title},
              "sheets": sheets,
              "spreadsheetUrl":
                  f"https://docs.google.com/spreadsheets/d/{sid}/edit"}
        state.setdefault("spreadsheets", {})[sid] = ss
        gs._save_state(state)
        return 200, ss

    sid = parts[1] if len(parts) > 1 else None
    ss = _spreadsheet(state, sid)
    if ss is None:
        return 404, {"error": {"code": 404, "status": "NOT_FOUND",
                               "message": f"Requested entity was not found: {sid}"}}
    tail = parts[2:]

    if not tail and method == "GET":
        return 200, ss

    if tail[:1] == ["values"]:
        rng = "/".join(tail[1:])
        if "!" in rng:
            name, cells = rng.split("!")[0], rng
        elif gs._find_sheet(ss, rng):
            # A1 notation allows a bare sheet name as the whole range
            name, cells = rng, None
        else:
            name, cells = None, rng
        sheet = gs._find_sheet(ss, name) or ss["sheets"][0]
        if method == "GET":
            return 200, {"range": rng, "majorDimension": "ROWS",
                         "values": _values(sheet, cells)}
        if method in ("PUT", "POST"):
            rows = body.get("values") or []
            r1, c1 = 1, 1
            cell = cells.split("!")[-1] if cells else ""
            cell = cell.split(":")[0]
            if cell and cell[0].isalpha():
                parsed = gs._split_a1(cell)
                r1, c1 = (parsed[0] or 1), (parsed[1] or 1)
            gs._write_range(sheet, r1, c1, rows)
            gs._save_state(state)
            return 200, {"spreadsheetId": sid, "updatedRange": rng,
                         "updatedRows": len(rows),
                         "updatedColumns": max((len(r) for r in rows),
                                               default=0),
                         "updatedCells": sum(len(r) for r in rows)}

    if tail == [":batchUpdate"] or tail == ["batchUpdate"] or \
            (tail and tail[-1].endswith("batchUpdate")):
        for request in body.get("requests") or []:
            if "addSheet" in request:
                props = request["addSheet"].get("properties", {})
                ss["sheets"].append(gs._new_sheet(
                    state, props.get("title", "Sheet"),
                    index=len(ss["sheets"])))
            elif "updateSpreadsheetProperties" in request:
                ss["properties"].update(
                    request["updateSpreadsheetProperties"].get("properties", {}))
        gs._save_state(state)
        return 200, {"spreadsheetId": sid, "replies": []}

    raise NotImplementedError(f"sheets facade: {method} {path}")
