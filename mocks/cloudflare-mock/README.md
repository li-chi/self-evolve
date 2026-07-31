# cloudflare-mock

Mock MCP server mirroring the **Cloudflare API v4**
(https://developers.cloudflare.com/api/). Cloudflare is **Tier A**:
real accounts don't parallelize across rollouts, so we run an
in-process deterministic mock.

The mock mirrors the REST surface directly — it is **not** a wrapper
around the `cloudflare-mcp` CLI mock under
`terminal-tool-use/mocks/cloudflare-mcp/`. Tool names follow the
endpoint operation (e.g. `list_dns_records`, `create_dns_record`)
rather than path templates, and every response is wrapped in the
Cloudflare v4 envelope.

## Implemented tools (29 + 2 mock helpers)

| group         | tool                              |
|---------------|-----------------------------------|
| Zones         | `list_zones`                      |
|               | `get_zone`                        |
|               | `create_zone`                     |
|               | `delete_zone`                     |
|               | `purge_cache`                     |
| DNS           | `list_dns_records`                |
|               | `get_dns_record`                  |
|               | `create_dns_record`               |
|               | `update_dns_record`               |
|               | `delete_dns_record`               |
| Workers       | `list_workers`                    |
|               | `get_worker_script`               |
|               | `upload_worker_script`            |
|               | `delete_worker_script`            |
| Workers KV    | `list_kv_namespaces`              |
|               | `list_kv_keys`                    |
|               | `get_kv_value`                    |
|               | `write_kv_value`                  |
|               | `delete_kv_value`                 |
| R2            | `list_r2_buckets`                 |
|               | `create_r2_bucket`                |
|               | `delete_r2_bucket`                |
| Pages         | `list_pages_projects`             |
|               | `get_pages_project`               |
|               | `list_pages_deployments`          |
| Page Rules    | `list_page_rules`                 |
|               | `create_page_rule`                |
| User/Account  | `get_user`                        |
|               | `list_accounts`                   |
| Mock-only     | `mock_debug_state`                |
|               | `mock_debug_seed`                 |

Parameter names match the v4 REST API request body / query string
fields. Responses are the v4 envelope:

```jsonc
// success
{
  "success": true,
  "errors": [],
  "messages": [],
  "result": { /* payload */ },
  "result_info": { /* paginated lists only */ }
}

// failure
{
  "success": false,
  "errors": [{"code": 1003, "message": "..."}],
  "messages": [],
  "result": null
}
```

## Object shapes

### Zone
```jsonc
{
  "id": "<32-hex>",
  "name": "example.com",
  "status": "active",          // active|pending|initializing|moved|deleted|deactivated
  "paused": false,
  "type": "full",              // full|partial|secondary
  "development_mode": 0,
  "name_servers": ["ns1...", "ns2..."],
  "original_name_servers": null,
  "created_on": "...Z",
  "modified_on": "...Z",
  "activated_on": "...Z",
  "meta": {...},
  "account": {"id": "<32-hex>", "name": "..."},
  "plan": {"id":"...","name":"Free Website","legacy_id":"free",...}
}
```

### DNS record
```jsonc
{
  "id": "<32-hex>",
  "zone_id": "<32-hex>",
  "zone_name": "example.com",
  "name": "www.example.com",   // apex stored as the bare zone name
  "type": "A",                 // A|AAAA|CNAME|MX|TXT|NS|SRV|CAA|PTR|SPF|URI
  "content": "203.0.113.10",
  "proxiable": true,           // true for A|AAAA|CNAME
  "proxied": false,
  "ttl": 1,                    // 1 = automatic; otherwise 60..86400
  "priority": 10,              // only on MX|SRV|URI
  "locked": false,
  "meta": {...},
  "comment": null,
  "tags": [],
  "created_on": "...Z",
  "modified_on": "...Z"
}
```

## State

Single JSON file at `$CLOUDFLARE_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/cloudflare_mock`). Layout:

```jsonc
{
  "user":              {"id","email","first_name","last_name",...},
  "accounts":          {"<acct_id>": {"id","name","type","settings"}},
  "default_account_id":"<acct_id>",
  "zones":             {"<zone_id>": {Zone}},
  "dns_records":       {"<zone_id>": [DNSRecord]},
  "page_rules":        {"<zone_id>": [PageRule]},
  "worker_scripts":    {"<acct_id>": {"<name>": {Script}}},
  "kv_namespaces":     {"<acct_id>": {"<ns_id>": {"id","title"}}},
  "kv_values":         {"<ns_id>":   {"<key>": {"value","metadata","expiration"}}},
  "r2_buckets":        {"<acct_id>": {"<name>": {Bucket}}},
  "pages_projects":    {"<acct_id>": {"<name>": {Project}}},
  "pages_deployments": {"<project_name>": [Deployment]},
  "purges":            [{"zone_id","files","tags",...}],
  "calls":             [{"op","ts",...}]
}
```

Set `CLOUDFLARE_MOCK_SEED_PATH` to a JSON file in the same shape to
preload state on first start (only when `state.json` does not yet
exist; per-rollout isolation should clear the state dir between
rollouts). Per-task fixtures are typically loaded via the
`mock_debug_seed` tool instead.

## Behavior notes / known mock-vs-real gaps

- Ids: zone, DNS-record, worker-script, namespace, page-rule, and
  account ids are 32-char lowercase hex strings, derived
  deterministically from the resource name (`sha256(...)[:32]`) so
  re-running the same seed produces stable ids.
- Pagination uses Cloudflare's `page` + `per_page` convention (and
  `cursor` for KV keys). `result_info` carries `count`,
  `total_count`, `total_pages`, `page`, `per_page`.
- Auth: no `Authorization` header is checked — every call succeeds
  as if from a token with full scope.
- `purge_cache` records the request in `state["purges"]` and returns
  `{"id": "<zone_id>"}`; nothing is actually cached.
- KV `get_kv_value` wraps the value as `{"value","metadata"}` in the
  v4 envelope rather than streaming the raw body (which the real
  endpoint does); this keeps the tool surface uniform.
- DNS validation: A requires IPv4, AAAA requires `:`, MX/SRV require
  `priority`. CAA/PTR/SPF/URI are accepted with minimal validation.
- Worker scripts are stored with their body inline; `list_workers`
  strips the body and `get_worker_script` includes it (the real API
  splits these across two endpoints).
- R2 `location_hint` is stored as-is (no per-region routing modeled).
- Pages projects/deployments are fully static — there's no real
  build pipeline; deployments must be seeded via `mock_debug_seed`.
- Page rules are stored in insertion order; priorities are not
  re-balanced after create.

## Env

| var                          | default                          | purpose                                |
|------------------------------|----------------------------------|----------------------------------------|
| `CLOUDFLARE_MOCK_STATE_DIR`  | `~/.openclaw/cloudflare_mock`    | state.json directory                   |
| `CLOUDFLARE_MOCK_SEED_PATH`  | unset                            | preload state.json on first start      |

The Dockerfile sets
`CLOUDFLARE_MOCK_STATE_DIR=/workspace/output/end_state/cloudflare` to
match the openclaw rollout layout.
