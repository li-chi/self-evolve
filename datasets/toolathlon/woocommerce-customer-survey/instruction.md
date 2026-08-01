Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Use the WooCommerce to check all order status updates for customers marked as "Completed", and send a Google Forms feedback questionnaire about their experience to the customers’ email addresses.
The requirements for constructing the questionnaire can be found in form_requirement.md in the workspace.
Also, store the Google Drive link corresponding to the Google Form (e.g., https://drive.google.com/open?id=...) in the workspace file drive_url.txt.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`
- `google_forms`

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
