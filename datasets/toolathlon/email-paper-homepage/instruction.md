Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Update my personal homepage according to the following rules:
- For papers currently marked as "preprint" or "under review" on my homepage, update their acceptance information according to my emails.
- For all accepted or published papers on my homepage, update the status of code open-sourcing. If there is a released repository on my GitHub for the corresponding paper, update it on my homepage.


## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `github`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools github                 # list tools with one-line summaries
mcp-tool schema github <tool_name>    # full argument schema for one tool
mcp-tool call github <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call github <tool_name> '{}'
```
