Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

You need to read the inventory levels of WooCommerce products, check the current stock quantity (stock_quantity) for each product against the safety threshold (stock_threshold), identify all products with stock below the threshold, and automatically update a Google Sheets purchase requisition list named stock_sheet. For each low-stock product, record it in Google Sheets and send an email notification to the purchasing manager (the email address is in purchasing_manager_email.txt). You need to find all low-stock products, record them and send emails. The email template can be found in stock_alert_email_template.md.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`
- `google_sheet`
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
