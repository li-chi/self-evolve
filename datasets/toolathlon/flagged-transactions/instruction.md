Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Perform anomaly detection on high-net-worth clients’ transactions: Extract the 2025 transactions of clients in `high_value_clients.csv` from BigQuery  `all_transactions.recordings` and mark the abnormal transactions with `amount > mean + 3*std` for each client, and fill them into  `anomaly_audit_report.xlsx`, the result should be sorted by the "transaction_id" (remove the sample data  
before adding results).



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
