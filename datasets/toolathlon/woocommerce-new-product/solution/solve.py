#!/usr/bin/env python3
"""Oracle for woocommerce-new-product.

Performs the task through the same tool surface the agent has:
  - woocommerce MCP: find upcoming (draft/pending) products and sale
    products, and read customers + their subscription preferences;
  - emails MCP: send the pre-order announcement to new-product
    subscribers and the discount reminder to every customer.

Selection criteria mirror the task text (and the grader's reading of it):
new product = status draft/pending with launch_date within the next 30
days (missing/unparseable launch_date counts as upcoming); sale product =
sale_price < regular_price; discount mail goes to all customers.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta


def mcp(server, tool, args):
    out = subprocess.run(
        ["mcp-tool", "call", server, tool, json.dumps(args)],
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


def paged(server, tool):
    items, page = [], 1
    while True:
        batch = mcp(server, tool, {"perPage": 100, "page": page})
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def meta_value(obj, key):
    for m in obj.get("meta_data") or []:
        if m.get("key") == key:
            return m.get("value")
    return None


now = datetime.now()
horizon = now + timedelta(days=30)

# --- products -------------------------------------------------------------
new_products, sale_products = [], []
for status in ("publish", "draft", "pending"):
    batch = mcp("woocommerce", "woo_products_list",
                {"perPage": 100, "status": status})
    if isinstance(batch, dict):
        batch = [batch]
    for p in batch:
        if p.get("status") in ("draft", "pending"):
            launch = meta_value(p, "launch_date")
            upcoming = True
            if launch:
                try:
                    upcoming = now <= datetime.strptime(
                        launch, "%Y-%m-%d") <= horizon
                except ValueError:
                    upcoming = True
            if upcoming:
                new_products.append({
                    "id": p["id"], "name": p.get("name"),
                    "status": p.get("status"), "launch_date": launch})
        sale, regular = p.get("sale_price"), p.get("regular_price")
        try:
            if sale and regular and float(sale) < float(regular):
                sale_products.append({
                    "id": p["id"], "name": p.get("name"),
                    "regular_price": regular, "sale_price": sale})
        except ValueError:
            pass

# --- customers ------------------------------------------------------------
customers = paged("woocommerce", "woo_customers_list")
subscribers, everyone = [], []
for c in customers:
    addr = c.get("email")
    if not addr:
        continue
    everyone.append(addr)
    prefs = meta_value(c, "subscription_preferences") or {}
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except ValueError:
            prefs = {}
    if prefs.get("new_product_alerts", False):
        subscribers.append(addr)

# --- send -----------------------------------------------------------------
log_lines = []


def send(to, subject, body):
    mcp("emails", "send_email", {"to": to, "subject": subject, "body": body})
    log_lines.append(f"[{datetime.now().isoformat()}] {to} — {subject}")


new_list = "\n".join(
    f"- {p['name']} (launch: {p.get('launch_date') or 'to be announced'})"
    for p in new_products)
sale_list = "\n".join(
    f"- {p['name']}: {p['regular_price']} -> {p['sale_price']}"
    for p in sale_products)

for addr in subscribers:
    send(addr, "New Product Pre-order: upcoming launches in the next 30 days",
         "Hello,\n\nThe following new products are launching within the "
         f"next 30 days and are open for reservation:\n{new_list}\n\n"
         "Reply to this email to reserve yours.\n")

for addr in everyone:
    send(addr, "Discount Sale: special offers on selected products",
         "Hello,\n\nThe following products are currently discounted:\n"
         f"{sale_list}\n\nDon't miss out!\n")

# --- report ---------------------------------------------------------------
report = {
    "new_products": new_products,
    "sale_products": sale_products,
    "appointment_emails": {"sent": subscribers},
    "discount_emails": {"sent": everyone},
    "summary": {
        "total_emails_sent": len(subscribers) + len(everyone),
        "appointment_emails_sent": len(subscribers),
        "discount_emails_sent": len(everyone),
    },
}
with open("/app/email_report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
with open("/app/sent_emails.log", "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines) + "\n")

print(f"oracle: {len(new_products)} new products, {len(sale_products)} sale "
      f"products, {len(subscribers)} appointment + {len(everyone)} discount "
      "emails sent")
if not everyone or not new_products or not sale_products:
    sys.exit(1)
