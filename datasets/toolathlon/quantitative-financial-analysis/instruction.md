Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please create a Google Sheet named '2025_Market_Data' with a worksheet titled 'Jun-Jul_2025', retrieve daily stock data for Apple, Tesla, Nvidia, and Meta for each trading day in June and July 2025, automatically excluding non-trading days, and record all these data in the 'Jun-Jul_2025' worksheet following the existing examples in example.csv. I want the original prices rather than adjusted prices that account for stock splits and dividends. Once you have finished recording the data for each trading day in the worksheet, write the line 'Google Sheet : {url}' on the 'Quant Research' Notion page, and add a comment at the top of that page stating exactly this: "Monthly market data is ready. The reporting team can view it directly"

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google_sheet`
- `notion`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools google_sheet                 # list tools with one-line summaries
mcp-tool schema google_sheet <tool_name>    # full argument schema for one tool
mcp-tool call google_sheet <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call google_sheet <tool_name> '{}'
```
