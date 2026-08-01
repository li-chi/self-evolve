Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please help me check which products are in my shop, filter out those that have been in stock for more than 90 days and have sold fewer than 10 units in the past 30 days. Move them to a product category named "Outlet/Clearance" ("/" is part of the name). Also, send an email to each of the subscribed customers.

You can find the email template under the workspace (please sort the products by stock-in time from earliest to latest; if the stock-in time is the same, then sort by discount ratio from small to large). The subscriber information is in subscriber.json.

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
