Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

As an operator of the WooCommerce e-commerce platform, you need to send emails to customers about new product reservations and discount reminders. Specifically, this includes sending new products scheduled to be released within the next 30 days to customers who have subscribed to the automatic reservation reminder service (i.e. the potential meta data "subscription_preferences" includes "new_product_alerts" as True, default is False), as well as sending discount product reminder emails to all customers.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools woocommerce                 # list tools with one-line summaries
mcp-tool schema woocommerce <tool_name>    # full argument schema for one tool
mcp-tool call woocommerce <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call woocommerce <tool_name> '{}'
```
