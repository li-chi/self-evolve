#!/usr/bin/env python3
"""Oracle for update-material-inventory.

Performs the task through the same tool surface the agent has:
  - woocommerce MCP: read the paid orders and each product's SKU;
  - google_sheet MCP: read the BOM and Material_Inventory worksheets,
    deduct the consumed material quantities, write the balances back;
  - woocommerce MCP: set each product's stock to its recomputed maximum
    producible quantity.

Upstream's orders are randomised per run, so everything is computed from
the live service state — the same definition the grader re-derives.
"""

import json
import subprocess
import sys


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


with open("/app/config.json", encoding="utf-8") as f:
    config = json.load(f)
sid = config["spreadsheet_id"]
bom_sheet = config.get("bom_sheet_name", "BOM")
inv_sheet = config.get("inventory_sheet_name", "Material_Inventory")

# --- orders: quantities per SKU ---------------------------------------------
products = aslist(mcp("woocommerce", "woo_products_list",
                      {"perPage": 100}))
sku_by_pid = {str(p["id"]): p.get("sku", "") for p in products}

orders = aslist(mcp("woocommerce", "woo_orders_list", {"perPage": 100}))
totals = {}
for o in orders:
    for line in o.get("line_items", []) or []:
        sku = sku_by_pid.get(str(line.get("product_id")), "")
        if sku:
            totals[sku] = totals.get(sku, 0) + int(line.get("quantity") or 0)
if not totals:
    print("oracle: no orders found")
    sys.exit(1)

# --- BOM + inventory from the sheets ----------------------------------------
bom_grid = mcp("google_sheet", "get_sheet_data",
               {"spreadsheet_id": sid, "sheet": bom_sheet})
inv_grid = mcp("google_sheet", "get_sheet_data",
               {"spreadsheet_id": sid, "sheet": inv_sheet})
bom_rows = bom_grid["valueRanges"][0]["values"][1:]
inv_rows = inv_grid["valueRanges"][0]["values"]

bom = {}
for row in bom_rows:
    if len(row) >= 5 and str(row[0]).strip():
        sku, mat, unit = str(row[0]).strip(), str(row[2]).strip(), row[4]
        bom.setdefault(sku, {})[mat] = float(unit)

consumption = {}
for sku, qty in totals.items():
    for mat, unit in bom.get(sku, {}).items():
        consumption[mat] = consumption.get(mat, 0) + qty * unit

# --- write balances back (column C = current stock) --------------------------
final_materials = {}
for i, row in enumerate(inv_rows):
    if i == 0 or not row or not str(row[0]).strip():
        continue
    mat = str(row[0]).strip()
    balance = round(float(row[2]) - consumption.get(mat, 0), 6)
    final_materials[mat] = balance
    mcp("google_sheet", "update_cells",
        {"spreadsheet_id": sid, "sheet": inv_sheet,
         "range": f"C{i + 1}", "data": [[balance]]})

# --- max producible -> WooCommerce stock -------------------------------------
for sku, wc in config.get("product_mapping", {}).items():
    materials = bom.get(sku, {})
    counts = [int(final_materials.get(m, 0) // unit)
              for m, unit in materials.items()]
    qty = min(counts) if counts else 0
    mcp("woocommerce", "woo_products_update",
        {"productId": int(wc["woocommerce_id"]),
         "productData": {"manage_stock": True, "stock_quantity": qty}})
    print(f"oracle: {sku} -> stock {qty}")

print(f"oracle: orders {totals}; updated {len(final_materials)} materials")
