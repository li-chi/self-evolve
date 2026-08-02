#!/usr/bin/env python3
"""Oracle for woocommerce-stock-alert.

Performs the task through the same tool surface the agent has:
  - woocommerce MCP: find products whose stock_quantity is below their
    stock_threshold meta;
  - google_sheet MCP: upsert each low-stock product into the stock_sheet
    purchase requisition list (update the existing SKU row or append);
  - emails MCP: send one alert per low-stock product to the purchasing
    manager, following the workspace template.
"""

import json
import subprocess
import sys
from datetime import datetime


def mcp(server, tool, args):
    out = subprocess.run(
        ["mcp-tool", "call", server, tool, json.dumps(args)],
        capture_output=True, text=True, check=True).stdout
    docs, idx, dec = [], 0, json.JSONDecoder()
    s = out.strip()
    if not s:
        return []
    try:
        while idx < len(s):
            obj, end = dec.raw_decode(s, idx)
            docs.append(obj)
            idx = end
            while idx < len(s) and s[idx] in " \n\r\t":
                idx += 1
    except json.JSONDecodeError:
        return s
    return docs[0] if len(docs) == 1 else docs


def aslist(x):
    return [x] if isinstance(x, dict) else (x or [])


def meta_value(obj, key):
    for m in obj.get("meta_data") or []:
        if m.get("key") == key:
            return m.get("value")
    return None


# --- low-stock products ------------------------------------------------------
low = []
for p in aslist(mcp("woocommerce", "woo_products_list", {"perPage": 100})):
    threshold = meta_value(p, "stock_threshold")
    stock = p.get("stock_quantity")
    if threshold is None or stock is None:
        continue
    if float(stock) < float(threshold):
        supplier = meta_value(p, "supplier") or {}
        if isinstance(supplier, str):
            try:
                supplier = json.loads(supplier)
            except ValueError:
                supplier = {"name": supplier}
        low.append({
            "id": p["id"], "name": p.get("name", ""),
            "sku": p.get("sku", ""), "stock": int(stock),
            "threshold": int(float(threshold)),
            "supplier_name": supplier.get("name", ""),
            "supplier_id": supplier.get("supplier_id", ""),
            "supplier_contact": supplier.get("contact", ""),
        })
if not low:
    print("oracle: no low-stock products found")
    sys.exit(1)

# --- find the requisition spreadsheet ---------------------------------------
sheets = aslist(mcp("google_sheet", "list_spreadsheets", {}))
sid = None
for entry in sheets:
    info = mcp("google_sheet", "list_sheets", {"spreadsheet_id": entry["id"]})
    names = info if isinstance(info, list) else [info]
    if "stock_sheet" in [str(n) for n in names]:
        sid = entry["id"]
        break
if not sid:
    print("oracle: no spreadsheet with a stock_sheet worksheet found")
    sys.exit(1)

grid = mcp("google_sheet", "get_sheet_data",
           {"spreadsheet_id": sid, "sheet": "stock_sheet"})
values = grid["valueRanges"][0]["values"]
headers = [str(h).strip() for h in values[0]]
rows = [list(r) + [""] * (len(headers) - len(r)) for r in values[1:]]
sku_col = headers.index("SKU")
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

by_sku = {str(r[sku_col]).strip(): i for i, r in enumerate(rows)}
for p in low:
    record = {
        "Product ID": str(p["id"]), "Product Name": p["name"],
        "SKU": p["sku"], "Current Stock": p["stock"],
        "Safety Threshold": p["threshold"],
        "Supplier Name": p["supplier_name"],
        "Supplier ID": p["supplier_id"],
        "Supplier Contact": p["supplier_contact"],
        "Alert Time": now,
        "Suggested Order Quantity": str(max(
            p["threshold"] * 2 - p["stock"], p["threshold"] - p["stock"])),
    }
    row = [record.get(h, "") for h in headers]
    if p["sku"] in by_sku:
        rows[by_sku[p["sku"]]] = row
    else:
        by_sku[p["sku"]] = len(rows)
        rows.append(row)

end_col = chr(ord("A") + len(headers) - 1)
mcp("google_sheet", "update_cells",
    {"spreadsheet_id": sid, "sheet": "stock_sheet",
     "range": f"A2:{end_col}{len(rows) + 1}", "data": rows})
print(f"oracle: wrote {len(rows)} requisition rows to {sid}")

# --- alert emails ------------------------------------------------------------
with open("/app/purchasing_manager_email.txt", encoding="utf-8") as f:
    manager = f.read().strip()
sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"

for p in low:
    body = f"""Dear Purchasing Manager,

The system has detected that the following product's stock level is below the safety threshold. Please take immediate action:

## Product Information
- **Product Name**: {p['name']}
- **SKU**: {p['sku']}
- **Current Stock**: {p['stock']}
- **Safety Threshold**: {p['threshold']}
- **Supplier**: {p['supplier_name']}
- **Supplier Contact**: {p['supplier_contact']}

## Action Required
Please review the stock levels and consider placing a purchase order to replenish inventory.

For detailed information and to update the purchase requisition list, please visit the Google Sheets link: {sheet_url}

Best regards,
Stock Alert System
"""
    mcp("emails", "send_email",
        {"to": manager,
         "subject": f"[Stock Alert] {p['name']} Stock Below Safety Threshold",
         "body": body})

print(f"oracle: {len(low)} low-stock products, {len(low)} alerts to {manager}")
