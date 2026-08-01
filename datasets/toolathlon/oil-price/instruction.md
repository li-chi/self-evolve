Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

I have prepared two data tables in the Notion `Oil Price` page. Please fetch WTI and Brent monthly prices for the last 12 months from Yahoo Finance, analyze the WTI-Brent oil spread changes and calculate related indicators, implement a z-score-based spread trading strategy backtest, and return a summary report.

For detailed technical specifications, please refer to the `detail.md` file.



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
