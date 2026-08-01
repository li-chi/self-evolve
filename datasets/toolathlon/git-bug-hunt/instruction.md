Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

In the `LUFFY` Git repository within the workspace, we've identified a serious performance issue introduced by a commit containing the variable 'remove_caching_layer'. You need to find the earliest commit that introduced this variable, get the author's name and email for that commit, and write an email to the author. The subject of the email should be '[URGENT] Performance Issue Investigation Regarding Your Commit'. The body of the email should include the commit hash and the full commit message of the discovered commit, formatted according to the `template.txt` file in the workspace.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools emails                 # list tools with one-line summaries
mcp-tool schema emails <tool_name>    # full argument schema for one tool
mcp-tool call emails <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call emails list_emails '{"folder": "INBOX", "limit": 20}'
```
