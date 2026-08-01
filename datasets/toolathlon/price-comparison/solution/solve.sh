#!/bin/bash
# Oracle for price-comparison: publish the groundtruth comparison into
# BigQuery at bigquery_pricing_analysis.analysis with the four columns the
# requirements (and the grader) specify.
set -e
python3 - <<'PY'
import csv, json, subprocess

GT = ("/solution/groundtruth_workspace/"
      "competitive_pricing_analysis_ground_truth.csv")


def bq(query):
    subprocess.run(["mcp-tool", "call", "google-cloud", "bigquery_run_query",
                    json.dumps({"query": query})], check=True)


subprocess.run(["mcp-tool", "call", "google-cloud", "bigquery_create_dataset",
                json.dumps({"dataset_id": "bigquery_pricing_analysis",
                            "location": "US"})], check=True)
bq("DROP TABLE IF EXISTS `bigquery_pricing_analysis.analysis`")
bq("CREATE TABLE `bigquery_pricing_analysis.analysis` ("
   "`Product Name` STRING, `Our Price` FLOAT64, "
   "`Competitor Price` FLOAT64, `Price Difference` FLOAT64)")

with open(GT, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))

values = ", ".join(
    "('{}', {}, {}, {})".format(
        r["Product Name"].replace("'", "''"),
        float(r["Our Price"]), float(r["Competitor Price"]),
        float(r["Price Difference"]))
    for r in rows)
bq(f"INSERT INTO `bigquery_pricing_analysis.analysis` VALUES {values}")
print(f"oracle: {len(rows)} product comparisons published to BigQuery")
PY
