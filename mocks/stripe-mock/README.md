# stripe-mock

Stripe-shaped mock MCP server. Mirrors the Stripe REST API surface
documented at <https://docs.stripe.com/api> for the core resources
agent tasks typically touch (Customers, Charges, PaymentIntents,
Refunds, Subscriptions, Products, Prices, Invoices, Payouts, Balance).

There is no upstream Stripe MCP server we're mocking — this is a
direct REST-shaped mock built from the public Stripe API docs.

## Tool surface

Tool names follow Stripe REST verb naming (no `API-` prefix; this
server is not derived from an OpenAPI spec). Responses match Stripe
JSON shapes: every object carries `id`, `object`, `created` (epoch
seconds), `livemode`, and `metadata`; list endpoints return
`{"object":"list","data":[...],"has_more":bool,"url":"/v1/..."}`;
errors are returned (not raised) as
`{"error":{"type":"invalid_request_error","code":"resource_missing",...}}`
so traces look like real failed HTTP responses.

| tool                        | REST endpoint                                          |
|-----------------------------|--------------------------------------------------------|
| `list_customers`            | GET    /v1/customers                                   |
| `retrieve_customer`         | GET    /v1/customers/{id}                              |
| `create_customer`           | POST   /v1/customers                                   |
| `update_customer`           | POST   /v1/customers/{id}                              |
| `delete_customer`           | DELETE /v1/customers/{id}                              |
| `search_customers`          | GET    /v1/customers/search                            |
| `list_charges`              | GET    /v1/charges                                     |
| `retrieve_charge`           | GET    /v1/charges/{id}                                |
| `create_charge`             | POST   /v1/charges                                     |
| `capture_charge`            | POST   /v1/charges/{id}/capture                        |
| `update_charge`             | POST   /v1/charges/{id}                                |
| `list_payment_intents`      | GET    /v1/payment_intents                             |
| `retrieve_payment_intent`   | GET    /v1/payment_intents/{id}                        |
| `create_payment_intent`     | POST   /v1/payment_intents                             |
| `update_payment_intent`     | POST   /v1/payment_intents/{id}                        |
| `confirm_payment_intent`    | POST   /v1/payment_intents/{id}/confirm                |
| `cancel_payment_intent`     | POST   /v1/payment_intents/{id}/cancel                 |
| `list_refunds`              | GET    /v1/refunds                                     |
| `create_refund`             | POST   /v1/refunds                                     |
| `retrieve_refund`           | GET    /v1/refunds/{id}                                |
| `list_subscriptions`        | GET    /v1/subscriptions                               |
| `retrieve_subscription`     | GET    /v1/subscriptions/{id}                          |
| `create_subscription`       | POST   /v1/subscriptions                               |
| `update_subscription`       | POST   /v1/subscriptions/{id}                          |
| `cancel_subscription`       | DELETE /v1/subscriptions/{id}                          |
| `list_products`             | GET    /v1/products                                    |
| `retrieve_product`          | GET    /v1/products/{id}                               |
| `create_product`            | POST   /v1/products                                    |
| `update_product`            | POST   /v1/products/{id}                               |
| `list_prices`               | GET    /v1/prices                                      |
| `retrieve_price`            | GET    /v1/prices/{id}                                 |
| `create_price`              | POST   /v1/prices                                      |
| `list_invoices`             | GET    /v1/invoices                                    |
| `retrieve_invoice`          | GET    /v1/invoices/{id}                               |
| `create_invoice`            | POST   /v1/invoices                                    |
| `finalize_invoice`          | POST   /v1/invoices/{id}/finalize                      |
| `pay_invoice`               | POST   /v1/invoices/{id}/pay                           |
| `send_invoice`              | POST   /v1/invoices/{id}/send                          |
| `void_invoice`              | POST   /v1/invoices/{id}/void                          |
| `list_payouts`              | GET    /v1/payouts                                     |
| `retrieve_payout`           | GET    /v1/payouts/{id}                                |
| `retrieve_balance`          | GET    /v1/balance                                     |
| `list_balance_transactions` | GET    /v1/balance_transactions                        |

Plus two mock-only debug tools used by per-task setup/verification:

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed` — bulk-insert Stripe-shaped objects bypassing
  validation, for fixture seeding.

## Conventions

- Amounts are integers in the **smallest currency unit** (cents for
  USD), matching Stripe.
- IDs use the canonical Stripe prefixes: `cus_`, `ch_`, `pi_`, `pm_`,
  `re_`, `sub_`, `si_`, `prod_`, `price_`, `in_`, `il_`, `po_`,
  `txn_`, `evt_`. Each is `<prefix>_<24-char base62>`.
- Pagination is Stripe-style cursor (`starting_after` / `ending_before`
  on object IDs) with `limit` 1–100 (default 10).
- `metadata` is `str -> str`; PATCH-style updates merge keys, with
  `null` or empty-string values deleting a key.
- `created` filters accept either an int or a `{gt,gte,lt,lte}` dict.
- Errors are JSON `{"error": {...}}` and never raised — failed calls
  show up in the trace as a normal tool return.

### Cross-resource behavior

- `create_charge` with `capture=false` leaves the charge with
  `captured=false`, `amount_capturable=amount`, `status=succeeded`;
  `capture_charge` then captures and writes the
  `balance_transaction`.
- `create_payment_intent` with `confirm=true` and a `payment_method`
  drives the PI through `succeeded` and synthesizes a charge whose
  `payment_intent` points back to the PI.
- `pay_invoice` (default path) synthesizes a charge, marks the
  invoice `paid`, and records a `balance_transaction`. With
  `paid_out_of_band=true` or `forgive=true` no charge is created.
- `create_refund` requires a successful `charge` (or a
  `payment_intent` with a `latest_charge`) and tops up
  `amount_refunded`; a negative `balance_transaction` is written.

## State

State lives in `$STRIPE_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/stripe/state.json` inside the container;
`~/.openclaw/stripe_mock/state.json` outside). Shape:

```jsonc
{
  "account": {"id": "acct_...", "object": "account", ...},
  "balance": {"object": "balance",
              "available": [{"amount": N, "currency": "usd", ...}],
              "pending":   [{"amount": N, "currency": "usd", ...}]},
  "customers":            {"cus_xxx": {...}},
  "charges":              {"ch_xxx":  {...}},
  "payment_intents":      {"pi_xxx":  {...}},
  "refunds":              {"re_xxx":  {...}},
  "subscriptions":        {"sub_xxx": {...}},
  "products":             {"prod_xxx":{...}},
  "prices":               {"price_xxx":{...}},
  "invoices":             {"in_xxx":  {...}},
  "payouts":              {"po_xxx":  {...}},
  "balance_transactions": {"txn_xxx": {...}},
  "events":               {"evt_xxx": {...}},
  "calls":                [{"op": "...", "ts": "...", ...}]
}
```

The `calls` log is what the verifier consumes — every tool (reads
included) appends an entry. File-locking via `fcntl.flock` makes
concurrent calls safe; per-rollout isolation should reset the state
dir between rollouts.

Seed a starting state by setting `STRIPE_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.

## Run

```bash
# local
STRIPE_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  stripe-mock:
    build:
      context: ../../mcp_servers/stripe-mock
      dockerfile: Dockerfile
    image: mcp-env/stripe-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      STRIPE_MOCK_STATE_DIR: /workspace/output/end_state/stripe
      STRIPE_MOCK_SEED_PATH: /workspace/input/stripe_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
