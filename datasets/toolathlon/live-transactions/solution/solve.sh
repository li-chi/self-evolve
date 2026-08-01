#!/bin/bash
# Oracle for live-transactions:
#   1. the consolidated investigation JSON in the workspace as T8492XJ3.json
#   2. the same file archived in the mcp-fraud-investigation-archive-* bucket
#   3. a CRITICAL structured alert in the Trading_Logging-* log bucket
# Both names carry a per-run suffix and are discovered by listing, exactly
# as the agent must discover them.
set -e
cp /solution/groundtruth_workspace/T8492XJ3_investigation_report.json /app/T8492XJ3.json

python3 - <<'PY'
import json, re, subprocess


def call(tool, args=None):
    r = subprocess.run(["mcp-tool", "call", "google-cloud", tool,
                        json.dumps(args or {})],
                       check=True, capture_output=True, text=True)
    return r.stdout


def find(pattern, text, what):
    m = re.search(pattern, text)
    if not m:
        raise SystemExit(f"could not resolve {what} from:\n{text}")
    return m.group(1)


bucket = find(r"(mcp-fraud-investigation-archive[\w.-]*)",
              call("storage_list_buckets"), "archive bucket")
log_bucket = find(r"Log Bucket: (Trading_Logging[\w.-]*)",
                  call("logging_list_logs"), "Trading_Logging bucket")

print(call("storage_upload_file", {
    "bucket_name": bucket,
    "source_file_path": "/app/T8492XJ3.json",
    "destination_blob_name": "T8492XJ3.json"}))

# logging_write_log takes a string message (the real server's signature) and
# FastMCP pre-parses JSON-looking strings, so a JSON document is rejected —
# upstream behaves the same way. The alert therefore goes out as text
# carrying the required alert_type / transaction_id / status values.
payload = ("alert_type=Fraud transaction_id=T8492XJ3 "
           "status=Pending_Investigation "
           f"archive_location=gs://{bucket}/T8492XJ3.json")
print(call("logging_write_log", {"log_name": log_bucket, "message": payload,
                                 "severity": "CRITICAL"}))
print(f"oracle: archived to {bucket}, CRITICAL alert in {log_bucket}")
PY
