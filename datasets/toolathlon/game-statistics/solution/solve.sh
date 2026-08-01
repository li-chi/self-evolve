#!/bin/bash
# Oracle for game-statistics: end-of-day settlement, done in BigQuery
# through the same MCP surface the agent has.
#   1. leaderboard_YYYYMMDD — today's top 100 players by total score, ranked
#   2. player_historical_stats — one new row per player for today
set -e
python3 - <<'PY'
import json, subprocess


def bq(query):
    out = subprocess.run(
        ["mcp-tool", "call", "google-cloud", "bigquery_run_query",
         json.dumps({"query": query, "max_results": 5000})],
        check=True, capture_output=True, text=True).stdout
    if "Error executing BigQuery query" in out:
        raise SystemExit(f"query failed: {out}\n{query}")
    marker = "__FULL_RESULTS_JSON__"
    return json.loads(out.split(marker, 1)[1]) if marker in out else {"rows": []}


# The settlement day is the day the streamed scores belong to.
day = bq("SELECT DATE(timestamp) AS d, COUNT(*) AS n "
         "FROM `game_analytics.daily_scores_stream` "
         "GROUP BY d ORDER BY n DESC LIMIT 1")["rows"][0]["d"]
table = "leaderboard_" + day.replace("-", "")
print(f"settlement day {day} -> {table}")

bq(f"DROP TABLE IF EXISTS `game_analytics.{table}`")
bq(f"CREATE TABLE `game_analytics.{table}` "
   "(player_id STRING, total_score INT64, rank INT64)")
bq(f"""
INSERT INTO `game_analytics.{table}` (player_id, total_score, rank)
SELECT player_id, total_score,
       ROW_NUMBER() OVER (ORDER BY total_score DESC) AS rank
FROM (
    SELECT player_id,
           SUM(scores.online_score + scores.task_score) AS total_score
    FROM `game_analytics.daily_scores_stream`
    WHERE DATE(timestamp) = '{day}'
    GROUP BY player_id
    ORDER BY total_score DESC
    LIMIT 100
)
""")

# One historical row per player who played today.
bq(f"DELETE FROM `game_analytics.player_historical_stats` WHERE date = '{day}'")
bq(f"""
INSERT INTO `game_analytics.player_historical_stats`
    (player_id, player_region, date, total_online_score, total_task_score,
     total_score, game_count)
SELECT player_id,
       MIN(player_region) AS player_region,
       '{day}' AS date,
       SUM(scores.online_score) AS total_online_score,
       SUM(scores.task_score) AS total_task_score,
       SUM(scores.online_score + scores.task_score) AS total_score,
       COUNT(*) AS game_count
FROM `game_analytics.daily_scores_stream`
WHERE DATE(timestamp) = '{day}'
GROUP BY player_id
""")

n = bq(f"SELECT COUNT(*) AS n FROM `game_analytics.{table}`")["rows"][0]["n"]
h = bq("SELECT COUNT(*) AS n FROM `game_analytics.player_historical_stats` "
       f"WHERE date = '{day}'")["rows"][0]["n"]
print(f"oracle: leaderboard rows={n}, historical rows for {day}={h}")
PY
