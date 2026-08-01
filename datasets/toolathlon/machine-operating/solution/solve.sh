#!/bin/bash
# Oracle for machine-operating: write the anomaly report into the workspace
# and upload it to the iot_anomaly_reports-* bucket. The bucket name carries
# a per-run suffix, so it is discovered by listing buckets — exactly what the
# agent has to do.
set -e
cp /solution/groundtruth_workspace/anomaly_report.csv /app/anomaly_report.csv

python3 - <<'PY_INNER'
import json, re, subprocess


def call(tool, args):
    r = subprocess.run(["mcp-tool", "call", "google-cloud", tool,
                        json.dumps(args)], check=True, capture_output=True,
                       text=True)
    return r.stdout


listing = call("storage_list_buckets", {})
match = re.search(r"(iot_anomaly_reports[\w.-]*)", listing)
if not match:
    raise SystemExit(f"no iot_anomaly_reports bucket found in:\n{listing}")
bucket = match.group(1)

print(call("storage_upload_file", {
    "bucket_name": bucket,
    "source_file_path": "/app/anomaly_report.csv",
    "destination_blob_name": "anomaly_report.csv"}))
print(f"oracle: anomaly_report.csv uploaded to {bucket}")
PY_INNER
