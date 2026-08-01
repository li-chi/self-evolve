#!/bin/bash
# Oracle for ab-testing: does what a perfect agent does, through the same
# tool surface the agent has.
#
#   1. record.csv  <- the groundtruth per-scenario conversion rates
#   2. B wins overall (74.121% vs 73.840%), so create a bucket prefixed
#      `promo-assets-for-b` via the google-cloud MCP server
#   3. write NO log entry (the task says a log entry is only for an A win,
#      and the grader fails if the abtesting_logging bucket gained entries)
set -e

cp /solution/groundtruth_workspace/expected_ratio.csv /app/record.csv

mcp-tool call google-cloud storage_create_bucket \
  '{"bucket_name": "promo-assets-for-b-promotion", "location": "US"}'

echo "oracle: record.csv filled and promo bucket created"
