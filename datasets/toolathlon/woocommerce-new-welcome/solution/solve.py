#!/usr/bin/env python3
"""Oracle for woocommerce-new-welcome.

Performs the task through the same tool surface the agent has:
  - woocommerce MCP: read the store's orders, find customers whose first
    (and only) order was completed within the past 7 days;
  - google-cloud MCP: sync those customers into the
    woocommerce_crm.customers BigQuery table (insert or update), marking
    welcome_email_sent/welcome_email_date;
  - emails MCP: send each of them the welcome email in the workspace
    template's format (subject "Welcome to ...! Exclusive offers await
    you", body with order id/amount/date).
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta


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
        return s  # plain-text tool result
    return docs[0] if len(docs) == 1 else docs


def aslist(x):
    return [x] if isinstance(x, dict) else (x or [])


def sql(q):
    r = mcp("google-cloud", "bigquery_run_query", {"query": q})
    if isinstance(r, str):
        try:
            return json.loads(r)
        except json.JSONDecodeError:
            return r
    return r


def esc(v):
    return str(v).replace("'", "''")


# --- collect orders ---------------------------------------------------------
orders, page = [], 1
while True:
    batch = aslist(mcp("woocommerce", "woo_orders_list",
                       {"perPage": 100, "page": page}))
    if not batch:
        break
    orders.extend(batch)
    if len(batch) < 100:
        break
    page += 1

by_email = {}
for o in orders:
    email = (o.get("billing") or {}).get("email", "").lower()
    if email:
        by_email.setdefault(email, []).append(o)

now = datetime.now()
week_ago = now - timedelta(days=7)
first_timers = []  # (order, email)
for email, os_ in by_email.items():
    if len(os_) != 1:
        continue
    o = os_[0]
    if o.get("status") != "completed":
        continue
    created = (o.get("date_created") or "").replace("Z", "")
    try:
        when = datetime.fromisoformat(created).replace(tzinfo=None)
    except ValueError:
        continue
    if when >= week_ago:
        first_timers.append(o)

if not first_timers:
    print("oracle: no first-time customers found in the past 7 days")
    sys.exit(1)

# --- sync to BigQuery + welcome emails --------------------------------------
TABLE = "mcp-bench0606.woocommerce_crm.customers"
existing = sql(f"SELECT id, email FROM `{TABLE}`")
rows = existing if isinstance(existing, list) else \
    (existing.get("rows", []) if isinstance(existing, dict) else [])
known = {str(r.get("email", "")).lower() for r in rows if isinstance(r, dict)}
max_id = 0
for r in rows:
    if isinstance(r, dict):
        try:
            max_id = max(max_id, int(r.get("id") or 0))
        except (TypeError, ValueError):
            pass

today = now.strftime("%Y-%m-%d")
for o in first_timers:
    b = o.get("billing") or {}
    email = b.get("email", "")
    first, last = b.get("first_name", ""), b.get("last_name", "")
    phone = b.get("phone", "")
    wc_id = o.get("customer_id") or 0
    if email.lower() in known:
        sql(f"UPDATE `{TABLE}` SET welcome_email_sent = TRUE, "
            f"welcome_email_date = '{today}' WHERE email = '{esc(email)}'")
    else:
        max_id += 1
        sql(f"INSERT INTO `{TABLE}` (id, woocommerce_id, email, first_name, "
            f"last_name, phone, welcome_email_sent, welcome_email_date) "
            f"VALUES ({max_id}, {int(wc_id)}, '{esc(email)}', "
            f"'{esc(first)}', '{esc(last)}', '{esc(phone)}', TRUE, "
            f"'{today}')")

    created = (o.get("date_created") or "")[:10]
    body = f"""Dear {first} {last},

Thank you for placing your first order with us! As a new customer, we've prepared exclusive offers for you:

New Customer Exclusive Benefits
- 10% Off Coupon Code: WELCOME10 (valid on your next order)
- Free Shipping: Enjoy with orders over $50
- Double Points: Double your points on all orders within your first month

Your First Order Information
- Order ID: {o.get('id')}
- Order Amount: ${o.get('total')}
- Order Date: {created}

Recommended for You
Based on your purchase history, you may also like our seasonal picks.

Need help?
- Customer Service Email: support@example.com
- Customer Service Phone: 1-800-EXAMPLE

Thank you again for your trust and support!

Best wishes,
Store88 Team
"""
    mcp("emails", "send_email",
        {"to": email,
         "subject": "Welcome to Store88! Exclusive offers await you",
         "body": body})

print(f"oracle: synced + welcomed {len(first_timers)} first-time customers")
