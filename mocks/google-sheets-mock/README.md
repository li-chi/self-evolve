# google-sheets-mock

Mock MCP server mirroring [`xing5/mcp-google-sheets`](https://github.com/xing5/mcp-google-sheets),
which is what Toolathlon uses as its `google_sheet` server. Every
tool name and parameter shape matches the official server; responses
match the Google Sheets API v4 / Drive API v3 shapes that
`mcp-google-sheets` returns.

## Tool surface (15)

Reads:

| tool                          | maps to                                                          |
|-------------------------------|------------------------------------------------------------------|
| `get_sheet_data`              | `spreadsheets.values.get` (or `spreadsheets.get` if `include_grid_data=True`) |
| `get_sheet_formulas`          | `spreadsheets.values.get` with `valueRenderOption=FORMULA`       |
| `get_multiple_sheet_data`     | many `spreadsheets.values.get`                                   |
| `list_sheets`                 | `spreadsheets.get` (titles only)                                 |
| `get_spreadsheet_info`        | `spreadsheets.get` (human-readable summary)                      |
| `find_in_spreadsheet`         | scan cells (no real API, matches the official server's helper)   |

Writes:

| tool                          | maps to                                                          |
|-------------------------------|------------------------------------------------------------------|
| `update_cells`                | `spreadsheets.values.update`                                     |
| `batch_update_cells`          | `spreadsheets.values.batchUpdate`                                |
| `add_rows`                    | `spreadsheets.batchUpdate` (`insertDimension`)                   |
| `add_columns`                 | `spreadsheets.batchUpdate` (`insertDimension`)                   |

Sheet / spreadsheet management:

| tool                          | maps to                                                          |
|-------------------------------|------------------------------------------------------------------|
| `create_sheet`                | `spreadsheets.batchUpdate` (`addSheet`)                          |
| `copy_sheet`                  | `spreadsheets.sheets.copyTo`                                     |
| `rename_sheet`                | `spreadsheets.batchUpdate` (`updateSheetProperties`)             |
| `create_spreadsheet`          | `spreadsheets.create`                                            |
| `list_spreadsheets`           | Drive `files.list` (mimeType=spreadsheet)                        |
| `search_spreadsheets`         | Drive `files.list` (name substring)                              |
| `list_folders`                | Drive `files.list` (mimeType=folder)                             |

Plus two mock-only debug tools used by per-task setup/verification:

- `mock_debug_state` — return the full persisted state.
- `mock_debug_seed_spreadsheet(id, title, sheets=[{title, data}],
  folder_id=None)` — insert a complete spreadsheet fixture.

## Skipped in v1

- `share_spreadsheet` (no permissions model in the mock)
- `batch_update` (raw passthrough — not used by Toolathlon tasks)
- `add_chart` (chart objects not modeled)
- `get_multiple_spreadsheet_summary`

Add as needed.

## Behavior notes

- Cell values are stored as raw Python str/number/bool. Formulas
  (strings starting with `=`) are stored verbatim and **not
  evaluated**. `get_sheet_data` returns the raw value (including the
  formula text); `get_sheet_formulas` returns only the formula text.
  Tasks that depend on Sheets-side formula evaluation will need an
  upgrade.
- `update_cells` auto-grows the sheet's row/column count as needed.
- `add_rows` / `add_columns` insert blanks at the requested 0-based
  index.
- Range parser supports A1 notation `Sheet!A1:C10`, `A1:C10`,
  `Sheet`. Unbounded ranges (`A:A`, `1:1`) are accepted; trailing
  blank rows are trimmed from `get_sheet_data` results to match real
  API behavior.

## State

`$GSHEETS_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/gsheets/state.json` inside the container).

```jsonc
{
  "folders": {"<id>": {"id","name","parent"}},
  "spreadsheets": {
    "<id>": {
      "spreadsheetId", "properties": {"title", ...},
      "spreadsheetUrl", "folder_id",
      "sheets": [
        {"properties": {"sheetId","title","index","sheetType",
                        "gridProperties":{"rowCount","columnCount"}},
         "data": [[cell, ...], ...]}
      ]
    }
  },
  "next_id": {"spreadsheet": N, "sheet": N, "folder": N},
  "calls": [{"op", "ts", ...}]
}
```

Seed via `GSHEETS_MOCK_SEED_PATH` (loaded once if no `state.json` exists).
