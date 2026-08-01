Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Identify the tickets in the database that have exceeded the initial response time according to the relevant documentation, and send reminder emails—based on the templates mentioned in the manual—to the respective responsible managers, as well as apology emails to all involved users.

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
