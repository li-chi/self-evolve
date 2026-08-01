Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

A production line in the factory streams real-time data through IoT sensors into the live_sensor table of the machine_operating dataset in BigQuery. The normal operating parameter ranges (minimum/maximum values) for each machine’s sensors are defined in a configuration file named machine_operating_parameters.xlsx. Please query the live_sensor table in the machine_operating dataset in BigQuery for sensor data between 11:30 and 12:30 on August 19, 2025, and, using the parameter ranges from the Excel file, identify all readings that fall outside their normal ranges (you may choose any efficient method for comparison). Compile the final anomaly report with the fields `timestamp`, `machine_id`, `sensor_type`, `reading`, (the format of these data should be exactly the same as in the BigQuery table) and `normal_range` (min - max)  into a file named "anomaly_report.csv". Save the file in the workspace, and upload this file to the existing cloud storage bucket whose name starts with the prefix "iot_anomaly_reports" in your cloud space.

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
