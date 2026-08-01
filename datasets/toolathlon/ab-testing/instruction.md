Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

The A/B test for our new homepage has concluded, and the raw clickstream data has been stored in the `ab_testing` dataset in BigQuery. Analyze the data and fill the `record.csv`, determine which version ('A' or 'B') has the highest overall conversion rate, i.e., the overall conversion rate is defined as the arithmetic mean of the per-scenario conversion rates. If version B outperforms, immediately create a new Cloud Storage bucket whose name is prefixed with `promo-assets-for-b` for the full promotion, and you do not need to write any log entry in this process. If version A wins or the results are a tie, no bucket creation is required, but a log entry with the message `{'status': 'AB_Test_Concluded', 'winner': 'A', 'action': 'No_Change'}` must be written to the existing log bucket whose name is prefixed with `abtesting_logging`. During the entire process, there is no need to consider any log entries in the log bucket that existed before the task was executed.

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
