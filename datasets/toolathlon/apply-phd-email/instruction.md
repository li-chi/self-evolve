Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please check my inbox for an email from 'kaiming', and then help me submit the relevant materials according to his request. All materials are in the workspace, and the email subject should exactly be `PhD Application Materials Submission (Student ID: {studentid})`. My personal information is in the memory.

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
