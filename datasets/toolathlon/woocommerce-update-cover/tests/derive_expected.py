#!/usr/bin/env python3
"""Re-derive expected_results.json for woocommerce-update-cover.

Upstream's preprocess computes the answer from the orders it just created
and drops it in groundtruth_workspace, where only the harness can see it.
In the ported layout preprocess runs *inside* the task container, so that
file would sit next to the agent — it is deliberately not carried over.

Instead the same quantity is recomputed here, verifier-side, from the
store's own order history: per product, the variation with the highest sold
quantity, and that variation's image id. This is the definition upstream's
`get_expected_results` uses; it just reads the orders back out of the store
rather than remembering them in-process.
"""

import collections
import json
import os
import sys

STATE = os.environ.get("WC_MOCK_STATE_DIR", "/var/lib/mock-state/woocommerce")
OUT = sys.argv[1] if len(sys.argv) > 1 else "expected_results.json"

with open(os.path.join(STATE, "state.json"), encoding="utf-8") as f:
    state = json.load(f)

products = state.get("products", {})
# Variations live on their parent product record, as the store's own tools
# read them.
variations = {pid: {str(v.get("id")): v for v in (p.get("variations") or [])}
              for pid, p in products.items()}

# quantity sold per (product, variation)
sold = collections.defaultdict(lambda: collections.Counter())
for order in state.get("orders", {}).values():
    for line in order.get("line_items", []) or []:
        pid = str(line.get("product_id") or "")
        vid = line.get("variation_id")
        if not pid or not vid:
            continue
        sold[pid][str(vid)] += int(line.get("quantity") or 0)

expected = {}
for pid, product in products.items():
    counts = sold.get(pid)
    if not counts:
        continue
    top_vid, top_qty = max(counts.items(), key=lambda kv: (kv[1], -int(kv[0])))
    var = variations.get(pid, {}).get(top_vid, {})
    images = var.get("image") or {}
    image_id = images.get("id") if isinstance(images, dict) else None
    expected[pid] = {
        "product_name": product.get("name"),
        "expected_top_variation_id": int(top_vid),
        "expected_featured_image_id": image_id,
        "expected_sales_quantity": top_qty,
        "all_variations_sales": [
            {"variation_id": int(v), "sales": q}
            for v, q in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    }

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({"expected_updates": expected}, f, indent=2)
print(f"derived expected results for {len(expected)} products -> {OUT}")
