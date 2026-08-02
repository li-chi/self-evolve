#!/usr/bin/env python3
"""Oracle for inventory-sync.

Does exactly what the task asks the agent to do, through the same tool
surface the agent has (mcp-tool -> the woocommerce MCP server): read each
warehouse SQLite database, aggregate quantities per region and product,
and write the totals to the matching regional WooCommerce products.

The region/product identification mirrors what the store itself exposes:
every regional product carries SKU "<REGIONPREFIX>_<local_id>" and a
`region` meta_data entry (created that way by upstream preprocess).
"""

import json
import sqlite3
import subprocess
import sys

CITIES = {  # city db name -> region, same mapping the instruction gives
    "new_york": "East",
    "boston": "East",
    "dallas": "South",
    "houston": "South",
    "los_angeles": "West",
    "san_francisco": "West",
}


def mcp(tool, args):
    out = subprocess.run(
        ["mcp-tool", "call", "woocommerce", tool, json.dumps(args)],
        capture_output=True, text=True, check=True).stdout
    # A list result arrives as one JSON document per content item,
    # newline-joined by mcp-tool — parse them all.
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
        return s  # plain-text tool result (e.g. send_email confirmation)
    return docs[0] if len(docs) == 1 else docs


# 1. Aggregate local inventory per region/product (validator's definition:
#    sum of inventory.quantity over the region's city databases).
totals = {}  # region -> local product id -> qty
for city, region in CITIES.items():
    db = f"/app/warehouse/warehouse_{city}.db"
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT p.product_id, i.quantity FROM inventory i "
        "JOIN products p ON i.product_id = p.product_id").fetchall()
    conn.close()
    for pid, qty in rows:
        totals.setdefault(region, {})[pid] = \
            totals.get(region, {}).get(pid, 0) + qty

# 2. Map regional store products by SKU/meta region.
products = []
page = 1
while True:
    batch = mcp("woo_products_list", {"perPage": 100, "page": page})
    if isinstance(batch, dict):
        batch = [batch]
    if not batch:
        break
    products.extend(batch)
    if len(batch) < 100:
        break
    page += 1

updated = 0
for p in products:
    region = next((m.get("value") for m in (p.get("meta_data") or [])
                   if m.get("key") == "region"), None)
    sku = p.get("sku") or ""
    if not region or "_" not in sku:
        continue
    local_id = sku.split("_", 1)[1]
    qty = totals.get(region, {}).get(local_id)
    if qty is None:
        continue
    if p.get("stock_quantity") != qty:
        mcp("woo_products_update",
            {"productId": p["id"],
             "productData": {"manage_stock": True, "stock_quantity": qty}})
    updated += 1

print(f"oracle: synced {updated} regional products")
if updated == 0:
    sys.exit(1)
