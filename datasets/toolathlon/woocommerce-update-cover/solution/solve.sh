#!/bin/bash
# Oracle for woocommerce-update-cover: for each product, make the
# best-selling variation's image the product's featured (first) image.
# Sales are read back from the store's order history through the same
# woocommerce tool surface the agent has.
set -e
python3 - <<'PY'
import collections, json, subprocess


def call(tool, args):
    """Call a woocommerce tool and decode its result.

    A tool returning a list comes back as several content blocks, so the
    text is a run of concatenated JSON values rather than one array.
    """
    out = subprocess.run(["mcp-tool", "call", "woocommerce", tool,
                          json.dumps(args)],
                         check=True, capture_output=True, text=True).stdout
    decoder, values, idx = json.JSONDecoder(), [], 0
    text = out.strip()
    while idx < len(text):
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        values.append(value)
        idx = end
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
    if not values:
        raise SystemExit(f"{tool}: could not decode result:\n{out[:300]}")
    return values[0] if len(values) == 1 else values


products = call("woo_products_list", {"perPage": 100})
orders = call("woo_orders_list", {"perPage": 100})
products = products if isinstance(products, list) else [products]
orders = orders if isinstance(orders, list) else [orders]
if not products:
    raise SystemExit("no products in the store")

sold = collections.defaultdict(collections.Counter)
for order in orders:
    for line in order.get("line_items") or []:
        pid, vid = line.get("product_id"), line.get("variation_id")
        if pid and vid:
            sold[int(pid)][int(vid)] += int(line.get("quantity") or 0)

for product in products:
    pid = int(product["id"])
    counts = sold.get(pid)
    if not counts:
        continue
    top_vid = max(counts.items(), key=lambda kv: kv[1])[0]
    variations = call("woo_products_variations_list",
                      {"productId": pid, "perPage": 100})
    variations = variations if isinstance(variations, list) else [variations]
    variation = next((v for v in variations
                      if isinstance(v, dict) and int(v.get("id", 0)) == top_vid),
                     {})
    image = variation.get("image") or {}
    if not image.get("id"):
        print(f"product {pid}: top variation {top_vid} has no image, skipped")
        continue
    call("woo_products_update",
         {"productId": pid,
          "productData": {"images": [{"id": image["id"]}]}})
    print(f"product {pid}: featured image <- variation {top_vid} "
          f"image {image['id']} ({counts[top_vid]} sold)")
PY
