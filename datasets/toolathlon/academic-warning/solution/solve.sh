#!/bin/bash
# Oracle for academic-warning:
#   1. bad_student.csv = every student whose latest score dropped >25%
#   2. one CRITICAL log per student whose drop exceeded 45%, naming the
#      student and their id, in the pre-existing exam_log-* bucket
set -e
LOG_BUCKET="$(cat /solution/groundtruth_workspace/log_bucket_name.txt)"
cp /solution/groundtruth_workspace/expected_alerts.csv /app/bad_student.csv

python3 - "$LOG_BUCKET" <<'PY'
import csv, json, subprocess, sys

log_bucket = sys.argv[1]
with open("/app/bad_student.csv", encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f))

critical = [r for r in rows if float(r["drop_ratio"]) > 0.45]
for r in critical:
    message = (f"CRITICAL academic warning: student {r['name']} "
               f"(ID: {r['student_id']}) scored {r['score']} against a "
               f"historical average of {r['hist_avg']} "
               f"(drop {float(r['drop_ratio']) * 100:.1f}%). "
               f"Counselor notification required.")
    subprocess.run(
        ["mcp-tool", "call", "google-cloud", "logging_write_log",
         json.dumps({"log_name": log_bucket, "message": message,
                     "severity": "CRITICAL"})],
        check=True)
print(f"oracle: {len(rows)} students listed, {len(critical)} CRITICAL logs")
PY
