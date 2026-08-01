Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

A music streaming platform wants to accurately identify the 1940s pop singles with the most sustained popularity during their classic oldies promotion campaign, in order to replicate marketing strategies and plan themed programs. You should use the original sheet data `Billboard Pop Chart by Year` on my google drive to complete the following analysis:

1. Calculate the total number of consecutive weeks each song stayed on the chart (defined as the longest streak of non-empty/non-black weekly rankings).
2. Strictly follow the format of the `music_analysis_result_example.xlsx` in the workspace, create one sheet for each year and fill them. The final resulted file should be named as `music_analysis_result.xlsx` with multiple sheets. The final leaderboard in each sheet should contain all songds and be sorted in descending order firstly by the "Longest Consecutive Top 3 Weeks", secondly by "Song" (In lexicographical order), then by "Artist" (In lexicographical order).



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
