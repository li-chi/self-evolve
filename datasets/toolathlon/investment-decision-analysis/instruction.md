Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

You are an investment analysis assistant. Please use Yahoo Finance to perform financial analysis. Create three separate spreadsheets(Note that these are not three sheets in one file) in the designated folder of Google Sheets with the spreadsheet names the same as the sheet names in the excel file `results.xlsx`. Note that I have filled some contents in the excel, for these parts you much copy them without any change and you just need to fill in the rest cells. Please keep 2 decimals for all floating numbers. Cumulative value assumes $10,000 invested at 2020 year-end. Do not include % mark in the cells as we already write in the headers.

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
