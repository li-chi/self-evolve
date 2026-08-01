Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Help me automatically update the two existing tables in the PURCHASE_INVOICE database with all the received receipts in my workspace. For receipts that are not fully paid, send an email to the relevant purchasing manager with the subject "Process Outstanding Invoices". The email body should include all the filenames that the manager still needs to process. Also, set a column description for outstanding_flag precisely as: "0=Paid, 1=Outstanding".

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `snowflake`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools snowflake                 # list tools with one-line summaries
mcp-tool schema snowflake <tool_name>    # full argument schema for one tool
mcp-tool call snowflake <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call snowflake <tool_name> '{}'
```
