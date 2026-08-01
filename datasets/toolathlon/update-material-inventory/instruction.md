Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please help me monitor new paid orders in WooCommerce, retrieve the SKU and quantity of each finished product in the order, then, based on the Bill of Materials (BOM) recorded in Google Sheets, calculate the amount of raw materials that need to be consumed. Deduct the corresponding quantities from the raw material inventory table in Google Sheets, write the updated raw material inventory back to Google Sheets, and then, based on the updated raw material balances, recalculate the maximum producible quantities for all finished products. Finally, sync these maximum producible quantities to WooCommerce as the available stock for the products.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google_sheet`
- `woocommerce`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools google_sheet                 # list tools with one-line summaries
mcp-tool schema google_sheet <tool_name>    # full argument schema for one tool
mcp-tool call google_sheet <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call google_sheet <tool_name> '{}'
```
