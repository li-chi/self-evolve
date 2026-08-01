Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

As a member of the market analysis team, you are urgently tasked with analyzing the price differences between our core product line and our primary competitor, FutureGadget. We have scraped pricing information from our competitor's website (stored in PDF format), and we have also obtained our corresponding product prices from the internal file, internal_pricing_sheet.xlsx. Finally, please combine and process these two sets of data, calculate the price difference, and store the final comparison results (including product name, our price, competitor's price, and price difference) in BigQuery for management decision-making. Please refer to the workspace file requirements.md for storage format requirements.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google-cloud`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools google-cloud                 # list tools with one-line summaries
mcp-tool schema google-cloud <tool_name>    # full argument schema for one tool
mcp-tool call google-cloud <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call google-cloud bigquery_run_query '{"query": "SELECT * FROM `dataset.table` LIMIT 5"}'
```
