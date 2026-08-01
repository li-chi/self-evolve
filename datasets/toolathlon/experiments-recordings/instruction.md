Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

In the table of the Notion page `mcp_experiments_recordings`, based on the historical experiments of W&B project `mbzuai-llm/Guru`, list the highest val-core acc mean@1/mean@k scores for each benchmark according to the table headers, and calculate and fill in the Best Step for that run (format: step(average acc)).

Instructions:

- If multiple runs have the same name, treat them as one run for combined statistics.
- The average score should only be calculated using the arithmetic mean of metrics available at that step; missing metrics are not included.
- Only operate on the target page under the specified parent page; do not change column names or order.



## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `notion`
- `wandb`

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
