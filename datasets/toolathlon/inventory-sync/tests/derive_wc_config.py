#!/usr/bin/env python3
"""Re-derive woocommerce_config.json for inventory-sync.

Upstream's preprocess writes woocommerce_config.json (site credentials +
the region -> local-product-id -> WC-product-id mapping) next to
token_key_session.py, where only the harness can see it. In the ported
layout preprocess runs *inside* the task container and that tree is
destroyed after init, so the file is deliberately not carried over.

The mapping is infrastructure, not the answer: preprocess created every
regional product with SKU "<REGIONPREFIX>_<local_id>" and a `region`
meta_data entry, so the identical mapping is recomputed here,
verifier-side, from the store's own product records.
"""

import json
import os
import sys

STATE = os.environ.get("WC_MOCK_STATE_DIR", "/var/lib/mock-state/woocommerce")
PKG = "/tests/pkg/tasks/finalpool/inventory-sync"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    PKG, "woocommerce_config.json")

sys.path.insert(0, PKG)
from token_key_session import all_token_key_session  # noqa: E402

with open(os.path.join(STATE, "state.json"), encoding="utf-8") as f:
    state = json.load(f)

product_mapping = {}
for p in state.get("products", {}).values():
    region = next((m.get("value") for m in (p.get("meta_data") or [])
                   if m.get("key") == "region"), None)
    sku = p.get("sku") or ""
    if not region or "_" not in sku:
        continue
    local_id = sku.split("_", 1)[1]
    product_mapping.setdefault(region, {})[local_id] = str(p["id"])

config = {
    "site_url": all_token_key_session.woocommerce_site_url,
    "consumer_key": all_token_key_session.woocommerce_api_key,
    "consumer_secret": all_token_key_session.woocommerce_api_secret,
    "product_mapping": product_mapping,
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

n = sum(len(v) for v in product_mapping.values())
print(f"derived product_mapping: {len(product_mapping)} regions, "
      f"{n} products -> {OUT}")
