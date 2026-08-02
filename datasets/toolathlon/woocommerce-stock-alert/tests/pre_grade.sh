#!/bin/bash
# Restore files/sheet_id.txt for the grader: the id of the spreadsheet
# preprocess copied into the "woocommerce-stock-alert" Drive folder. It is
# a resource name (not an answer), re-read from the sheets mock's own
# state because the staged task tree is destroyed after init.
set -e
/opt/toolathlon/.venv/bin/python - <<'EOF'
import json, os
state_dir = os.environ.get("GSHEETS_MOCK_STATE_DIR",
                           "/var/lib/mock-state/gsheets")
with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as f:
    s = json.load(f)
folder = next((f_ for f_ in s.get("folders", {}).values()
               if f_.get("name") == "woocommerce-stock-alert"), None)
sheet = None
if folder:
    sheet = next((ss for ss in s.get("spreadsheets", {}).values()
                  if ss.get("folder_id") == folder["id"]), None)
if not sheet:
    raise SystemExit("no copied stock sheet found in mock state")
out = ("/tests/pkg/tasks/finalpool/woocommerce-stock-alert/files/"
       "sheet_id.txt")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    f.write(sheet["spreadsheetId"])
print("sheet_id.txt restored:", sheet["spreadsheetId"])
EOF
