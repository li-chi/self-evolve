Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please fork https://github.com/academicpages/academicpages.github.io and rename it as `LJT-Homepage` on my github repo, and fill in it with the personal details, academic background, research experience, publications, skills, and contact information in memory to build my personal page. Do not add or modify any information beyond what is provided in the memory and do not add other pages. You should record publications in about section as well instead of only on the `publications` subpage.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `github`

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
