Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

I am a WooCommerce store owner, and my shop has warehouses in the following cities: New York, Boston, Dallas, LA, San Francisco, Houston. Please help me: Check each warehouse SQLite database (located in the warehouse directory in the workspace) for the latest product inventory list (identified by Product ID) that has been uploaded but not yet updated in the online WooCommerce store. Use the WooCommerce MCP server to synchronize the inventory updates to the WooCommerce online store according to the city-to-region mapping. 

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`

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
