# shopify-mock

Mock MCP server mirroring the **Shopify Admin REST API**
(<https://shopify.dev/docs/api/admin-rest>). Tool names and JSON
payload shapes match the official endpoints, so it's a drop-in
stand-in for tasks that exercise Shopify's `Product`, `Variant`,
`Order`, `Customer`, `Collection`, `InventoryLevel`, and `Shop`
resources.

This is a **separate surface** from the `shopify-checkout` CLI mock
in `terminal-tool-use/mocks/shopify-checkout` — that mock implements
an x402-style checkout flow on a fixed catalog, not the Admin API.
This server does not wrap it.

## Implemented tools (25)

| group        | tool                       | REST endpoint                                                      |
|--------------|----------------------------|---------------------------------------------------------------------|
| Products     | `list_products`            | GET    /admin/api/{ver}/products.json                              |
|              | `get_product`              | GET    /admin/api/{ver}/products/{id}.json                         |
|              | `create_product`           | POST   /admin/api/{ver}/products.json                              |
|              | `update_product`           | PUT    /admin/api/{ver}/products/{id}.json                         |
|              | `delete_product`           | DELETE /admin/api/{ver}/products/{id}.json                         |
|              | `count_products`           | GET    /admin/api/{ver}/products/count.json                        |
| Variants     | `list_variants`            | GET    /admin/api/{ver}/products/{id}/variants.json                |
|              | `get_variant`              | GET    /admin/api/{ver}/variants/{id}.json                         |
|              | `create_variant`           | POST   /admin/api/{ver}/products/{id}/variants.json                |
|              | `update_variant`           | PUT    /admin/api/{ver}/variants/{id}.json                         |
| Orders       | `list_orders`              | GET    /admin/api/{ver}/orders.json                                |
|              | `get_order`                | GET    /admin/api/{ver}/orders/{id}.json                           |
|              | `create_order`             | POST   /admin/api/{ver}/orders.json                                |
|              | `update_order`             | PUT    /admin/api/{ver}/orders/{id}.json                           |
|              | `cancel_order`             | POST   /admin/api/{ver}/orders/{id}/cancel.json                    |
|              | `close_order`              | POST   /admin/api/{ver}/orders/{id}/close.json                     |
| Customers    | `list_customers`           | GET    /admin/api/{ver}/customers.json                             |
|              | `get_customer`             | GET    /admin/api/{ver}/customers/{id}.json                        |
|              | `create_customer`          | POST   /admin/api/{ver}/customers.json                             |
|              | `update_customer`          | PUT    /admin/api/{ver}/customers/{id}.json                        |
| Inventory    | `get_inventory_level`      | GET    /admin/api/{ver}/inventory_levels.json                      |
|              | `adjust_inventory_level`   | POST   /admin/api/{ver}/inventory_levels/adjust.json               |
| Collections  | `list_collections`         | GET    /admin/api/{ver}/collections.json                           |
|              | `get_collection`           | GET    /admin/api/{ver}/collections/{id}.json                      |
| Shop         | `get_shop`                 | GET    /admin/api/{ver}/shop.json                                  |

Plus mock-only helpers `mock_debug_state` and `mock_debug_seed`.

## Response shapes

Every response uses the singular/plural envelope returned by Shopify:

- Singletons:  `{"product": {...}}`, `{"order": {...}}`, `{"shop": {...}}`
- Lists:       `{"products": [...]}`, `{"orders": [...]}`
- Counts:      `{"count": N}`
- Delete:      `{}`
- Errors (lookup): `{"errors": "Not Found"}`
- Errors (generic): `{"errors": "Order has already been canceled."}`
- Errors (validation): `{"errors": {"title": ["can't be blank"]}}`

IDs are positive 64-bit-style integers (e.g. `1000000001`).
Resources include the canonical Shopify fields: `id`, `created_at`,
`updated_at`, `admin_graphql_api_id`, etc. Orders include `name`
(`"#1001"`), `order_number`, `financial_status`, `fulfillment_status`,
`line_items`, `email`, `customer`.

## State

`$SHOPIFY_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/shopify/state.json` inside the
container; `~/.openclaw/shopify_mock` locally). Layout:

```jsonc
{
  "shop":        {...shop object...},
  "products":    {"<id>": {...}},
  "variants":    {"<id>": {...}},
  "orders":      {"<id>": {...}},
  "customers":   {"<id>": {...}},
  "collections": {"<id>": {...}},
  "inventory_items":  {"<id>": {...}},
  "inventory_levels": {"<loc>:<item>": {...}},
  "locations":   {"<id>": {...}},
  "next_id":     {"product": N, "order": N, ...},
  "calls":       [{"op": "...", "ts": "...", ...}]
}
```

Every call (including reads and failures) appends an entry to
`state["calls"]` for verifier consumption.

Set `SHOPIFY_MOCK_SEED_PATH` to a JSON in this layout to preload
state at first start (only loaded when `state.json` does not yet
exist).

## Behavior notes

- `create_order` decrements `inventory_quantity` for variants with
  `inventory_management == "shopify"`.
- `cancel_order` with `restock=True` re-credits inventory for tracked
  variants; cancelling a cancelled order returns the
  `"Order has already been canceled."` error.
- `close_order` is idempotent (re-closing returns the order).
- `adjust_inventory_level` auto-provisions the inventory level row if
  it doesn't yet exist and keeps the variant's denormalized
  `inventory_quantity` in sync.
- `list_collections` returns custom + smart collections combined
  under `collections` (each item carries `collection_type`).
- `create_customer` requires `email` OR `phone`; duplicate emails
  return `{"email": ["has already been taken"]}`.
- `create_order` auto-creates a customer from an embedded
  `customer: {email,...}` if one isn't already on file.

## Seeding via `mock_debug_seed`

```python
mock_debug_seed(
    shop={"name": "Acme", "currency": "USD"},
    products=[{
        "title": "Burton Custom Snowboard",
        "vendor": "Burton",
        "product_type": "Snowboard",
        "tags": "Snow, Sport",
        "options": [{"name": "Size", "values": ["155", "159"]}],
        "variants": [
            {"option1": "155", "price": "549.99", "sku": "BURTON-155",
             "inventory_management": "shopify", "inventory_quantity": 10},
            {"option1": "159", "price": "569.99", "sku": "BURTON-159",
             "inventory_management": "shopify", "inventory_quantity": 6},
        ],
    }],
    customers=[{"email": "jane@example.com",
                "first_name": "Jane", "last_name": "Doe"}],
    locations=[{"id": 7000000001, "name": "Main Warehouse",
                "country": "US"}],
    replace=True,
)
```
