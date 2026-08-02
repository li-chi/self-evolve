Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please check all customers who have completed their first order in our store in the past 7 days and immediately sync their information (name, email address, etc.) from WooCommerce to our core customer relationship database (BigQuery). At the same time, send a welcome email to each of the above customers. Please follow the email format in the template (`welcome_email_template.md`).

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`
- `google-cloud`
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
