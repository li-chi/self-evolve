Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Conduct a detailed analysis of the NHL 2024–2025 schedule (in spreadsheet `NHL 2425 Schedule`) and calculate how many back-to-back sets each team will face this season. Please also break down, for each team, the number of occurrences in each of the four home/away configurations: Home–Away (HA), Away–Home (AH), Home–Home (HH), and Away–Away (AA). Organize the results into a newspreadsheet named `NHL-B2B-Analysis`. The table headers should exactly be: `Team,HA,AH,HH,AA,Total`.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google_sheet`

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
