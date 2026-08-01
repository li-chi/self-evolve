Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

You need to update a notion page named `Colley Whisson` based on the information provided in the `colley_whisson.docx` document.

Please update the following sections based on the information in the docx document. Pay attention that you must include all details:

- About Me section
- Paintings section
- Workshop section
- Prizes section
- Exhibitions section


## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `notion`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools notion                 # list tools with one-line summaries
mcp-tool schema notion <tool_name>    # full argument schema for one tool
mcp-tool call notion <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call notion <tool_name> '{}'
```
