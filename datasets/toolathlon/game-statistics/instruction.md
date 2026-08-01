Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

The end-of-day settlement time for the game has arrived, and we need to process the player score data for the current day in the `daily_scores_stream` table. First, generate the final leaderboard for the day by selecting the top 100 players with the highest scores and storing the results in an independent table named after the day's date, `leaderboard_YYYYMMDD`(Must include player_id, total_score and rank fields). Second, for long-term player behavior analysis, update the statistical data for all players on the day into the master table `player_historical_stats`, inserting a new record for each player containing their ID, the day's date, the day's total score, and the number of games played on the day.

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
