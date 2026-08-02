#!/usr/bin/env python3
"""Oracle for filter-low-selling-products.

Performs the task through the same tool surface the agent has:
  - woocommerce MCP: find low-selling products (in stock > 90 days and
    < 10 units sold in the past 30 days, per the product's
    sales_last_30_days meta), create the "Outlet/Clearance" category and
    add those products to it;
  - emails MCP: send each subscriber the promotional email built from the
    workspace template, products sorted by stock-in time earliest->latest
    (ties: discount ratio ascending) — the grader compares the body
    verbatim (normalized).
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
        return s  # plain-text tool result (e.g. send_email confirmation)
    return docs[0] if len(docs) == 1 else docs


def aslist(x):
    return [x] if isinstance(x, dict) else (x or [])


# --- find low-selling products (grader's definition) -----------------------
now = datetime.now()
products, page = [], 1
while True:
    batch = aslist(mcp("woocommerce", "woo_products_list",
                       {"perPage": 100, "page": page}))
    if not batch:
        break
    products.extend(batch)
    if len(batch) < 100:
        break
    page += 1

low, rest = [], []
for p in products:
    created = p.get("date_created") or ""
    if not created:
        continue
    days = (now - datetime.fromisoformat(
        created.replace("Z", "+00:00")).replace(tzinfo=None)).days
    sales = 0
    for m in p.get("meta_data") or []:
        if m.get("key") in ("sales_last_30_days", "_sales_last_30_days"):
            try:
                sales = int(m.get("value", 0))
                break
            except (TypeError, ValueError):
                continue
    regular = float(p.get("regular_price") or 0)
    sale = float(p.get("sale_price") or 0) or regular
    item = {
        "product": p,
        "days_in_stock": days,
        "discount_ratio": (sale / regular) if regular > 0 else 1.0,
        "regular_price": regular,
        "sale_price": sale,
    }
    (low if days > 90 and sales < 10 else rest).append(item)

# earliest stock-in first == most days in stock first; ties by discount asc
low.sort(key=lambda x: (-x["days_in_stock"], x["discount_ratio"]))
if not low:
    print("oracle: no low-selling products found")
    sys.exit(1)

# --- category move ----------------------------------------------------------
cats = aslist(mcp("woocommerce", "woo_products_categories_list",
                  {"perPage": 100}))
outlet = next((c for c in cats if c.get("name") == "Outlet/Clearance"), None)
if outlet is None:
    outlet = mcp("woocommerce", "woo_products_categories_create",
                 {"name": "Outlet/Clearance"})
outlet_id = outlet["id"]

for item in low:
    p = item["product"]
    existing = [{"id": c["id"]} for c in (p.get("categories") or [])]
    if not any(c["id"] == outlet_id for c in existing):
        mcp("woocommerce", "woo_products_update",
            {"productId": p["id"],
             "productData": {"categories": existing + [{"id": outlet_id}]}})

# --- subscriber emails ------------------------------------------------------
with open("/app/subscriber.json", encoding="utf-8") as f:
    subscribers = json.load(f).get("subscriber_list", [])
with open("/app/email_template.txt", encoding="utf-8") as f:
    template = f.read()

placeholder = (
    "[Product Name 1] - Original Price: [Original Price] - Promotional Price: [Promotional Price]\n"
    "[Product Name 2] - Original Price: [Original Price] - Promotional Price: [Promotional Price]\n"
    "[Product Name 3] - Original Price: [Original Price] - Promotional Price: [Promotional Price]"
)
product_lines = "\n".join(
    f"{i['product'].get('name', '')} - Original Price: ${i['regular_price']:.2f}"
    f" - Promotional Price: ${i['sale_price']:.2f}" for i in low)

for sub in subscribers:
    body = template.replace("{customer_fullname}", sub.get("name", ""))
    body = body.replace(placeholder, product_lines)
    mcp("emails", "send_email",
        {"to": sub["email"],
         "subject": "Special Clearance Promotion",
         "body": body})

print(f"oracle: {len(low)} products moved to Outlet/Clearance, "
      f"{len(subscribers)} promo emails sent")
