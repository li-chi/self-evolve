Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Check my emails from the past day. If any mention the COML conference main-track camera-ready deadline, schedule a calendar reminder for me three hours before that deadline.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google_calendar`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools google_calendar                 # list tools with one-line summaries
mcp-tool schema google_calendar <tool_name>    # full argument schema for one tool
mcp-tool call google_calendar <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call google_calendar <tool_name> '{}'
```
