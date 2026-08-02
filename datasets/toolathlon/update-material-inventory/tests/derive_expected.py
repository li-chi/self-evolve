#!/usr/bin/env python3
"""Re-derive expected_results.json for update-material-inventory.

Upstream preprocess simulates a RANDOM set of paid orders and recomputes
groundtruth_workspace/expected_results.json from them. In the ported
layout that recomputed file would sit inside the agent's container, so it
is deliberately not carried over.

The same numbers are recomputed here, verifier-side, from the store's own
end state: order quantities per SKU come out of the woocommerce mock's
orders, and the static inputs (initial inventories, BOM) come from the
SHIPPED expected_results.json — the same definition upstream's
calculate_expected_results.py uses.
"""

import json
import os
import sys

STATE = os.environ.get("WC_MOCK_STATE_DIR", "/var/lib/mock-state/woocommerce")
GT = ("/tests/pkg/tasks/finalpool/update-material-inventory/"
      "groundtruth_workspace/expected_results.json")

with open(GT, encoding="utf-8") as f:
    base = json.load(f)
with open(os.path.join(STATE, "state.json"), encoding="utf-8") as f:
    wc = json.load(f)

sku_by_pid = {str(p["id"]): p.get("sku", "") for p in wc["products"].values()}

totals = {}
for order in wc.get("orders", {}).values():
    for line in order.get("line_items", []) or []:
        sku = sku_by_pid.get(str(line.get("product_id")), "")
        if sku:
            totals[sku] = totals.get(sku, 0) + int(line.get("quantity") or 0)

material_inventory = base["initial_inventories"]["material_inventory"]
bom = base["bom_data"]

consumption = {}
for sku, qty in totals.items():
    for material, unit in bom.get(sku, {}).items():
        consumption[material] = consumption.get(material, 0) + qty * unit

final_materials = {m: round(v - consumption.get(m, 0), 6)
                   for m, v in material_inventory.items()}

max_producible = {}
for sku, materials in bom.items():
    counts = [int(final_materials.get(m, 0) // unit)
              for m, unit in materials.items()]
    max_producible[sku] = min(counts) if counts else 0

base["test_orders_summary"] = {"total_quantities_ordered": totals}
base["expected_material_consumption"] = consumption
base["expected_final_inventories"] = {
    "woocommerce_inventory": max_producible,
    "google_sheets_material_inventory": final_materials,
}

with open(GT, "w", encoding="utf-8") as f:
    json.dump(base, f, indent=2, ensure_ascii=False)
print(f"derived expected_results: orders {totals}, "
      f"max producible {max_producible}")
