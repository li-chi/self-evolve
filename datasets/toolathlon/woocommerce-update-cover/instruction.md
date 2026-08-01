Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Update the main product image based on WooCommerce order data, setting the image of the best-selling variation of each product as the main product image.

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
