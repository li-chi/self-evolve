# woocommerce-mock

Mock MCP server mirroring `@lockon0927/woocommerce-mcp` (Toolathlon's
official woocommerce server, source: `github.com/lockon-n/woocommerce-mcp`).
Every tool name and argument matches the official server, which is a
thin proxy over the WooCommerce REST API v3.

## Why a new mock (vs `internal/task-0076-wc/mcp_servers/woocommerce-mock`)

The task-0076-wc mock uses the **WC Store API** (cart / checkout /
storefront-style tools — `wc_store_cart_add_item`, `wc_store_checkout`,
…). That surface is **not** exposed by the official MCP server; it was
a custom per-task mock for cart-flow tasks. The shared `mcp_servers/`
mock implemented here mirrors the official **admin REST** surface
(`woo_products_*`, `woo_orders_*`, `woo_customers_*`, `woo_reports_*`)
that the 9 Toolathlon `woocommerce` tasks actually call.

## Implemented tools (24)

| group       | tool                                       | REST endpoint                                  |
|-------------|--------------------------------------------|-----------------------------------------------|
| Products    | `woo_products_list`                        | GET    /wc/v3/products                        |
|             | `woo_products_get`                         | GET    /wc/v3/products/{id}                   |
|             | `woo_products_create`                      | POST   /wc/v3/products                        |
|             | `woo_products_update`                      | PUT    /wc/v3/products/{id}                   |
|             | `woo_products_delete`                      | DELETE /wc/v3/products/{id}                   |
|             | `woo_products_batch_update`                | POST   /wc/v3/products/batch                  |
|             | `woo_products_categories_list`             | GET    /wc/v3/products/categories             |
|             | `woo_products_categories_create`           | POST   /wc/v3/products/categories             |
|             | `woo_products_tags_list`                   | GET    /wc/v3/products/tags                   |
|             | `woo_products_reviews_list`                | GET    /wc/v3/products/reviews                |
|             | `woo_products_variations_list`             | GET    /wc/v3/products/{id}/variations        |
| Orders      | `woo_orders_list`                          | GET    /wc/v3/orders                          |
|             | `woo_orders_get`                           | GET    /wc/v3/orders/{id}                     |
|             | `woo_orders_create`                        | POST   /wc/v3/orders                          |
|             | `woo_orders_update`                        | PUT    /wc/v3/orders/{id}                     |
|             | `woo_orders_delete`                        | DELETE /wc/v3/orders/{id}                     |
|             | `woo_orders_batch_update`                  | POST   /wc/v3/orders/batch                    |
|             | `woo_orders_notes_create`                  | POST   /wc/v3/orders/{id}/notes               |
|             | `woo_orders_refunds_create`                | POST   /wc/v3/orders/{id}/refunds             |
| Customers   | `woo_customers_list`                       | GET    /wc/v3/customers                       |
|             | `woo_customers_get`                        | GET    /wc/v3/customers/{id}                  |
|             | `woo_customers_create`                     | POST   /wc/v3/customers                       |
|             | `woo_customers_update`                     | PUT    /wc/v3/customers/{id}                  |
| Reports     | `woo_reports_sales`                        | GET    /wc/v3/reports/sales                   |
|             | `woo_reports_top_sellers`                  | GET    /wc/v3/reports/top_sellers             |
|             | `woo_reports_low_stock`                    | GET    /wc/v3/reports/low_in_stock            |

Plus `mock_debug_state` (mock-only, returns the persisted state dict).

## Skipped in v1

Not exercised by any Toolathlon task — add as needed:

- `woo_coupons_*`, `woo_shipping_zones_*`, `woo_shipping_zone_methods_*`,
  `woo_tax_classes_list`, `woo_tax_rates_*`, `woo_payment_gateways_*`,
  `woo_webhooks_*`, `woo_settings_*`, `woo_system_status`,
  `woo_system_tools_list`, `woo_system_tools_run`.

## State

`$WC_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/woocommerce/state.json` inside the
container). Layout:

```jsonc
{
  "products":  {"<id>": {...wc product object...}},
  "orders":    {"<id>": {...wc order object...}},
  "customers": {"<id>": {...wc customer object...}},
  "categories": {...}, "tags": {...}, "reviews": {...},
  "next_id": {"product": N, "order": N, "customer": N, ...},
  "calls": [{"op": "...", "ts": "...", ...}]
}
```

Errors return the WC REST body shape:
`{"code": "...", "message": "...", "data": {"status": 4xx}}`.

Set `WC_MOCK_SEED_PATH` to a JSON in the same shape to preload state
at first start (only if `state.json` does not yet exist).

## Behavior notes

- `woo_orders_create` decrements stock for products with `manage_stock=True`
  and bumps `total_sales`.
- `woo_orders_update` setting `status=completed` stamps `date_completed`.
- `woo_orders_refunds_create` records a refund and flips the order to
  `refunded`.
- `woo_reports_sales` / `top_sellers` aggregate over orders in
  (completed | processing) status only.
- `woo_reports_low_stock` returns products with `manage_stock=True` and
  `stock_quantity <= threshold` (default 2, matching WC's default).
