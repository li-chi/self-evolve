#!/bin/bash
# Oracle for flagged-transactions: the grader compares the agent's
# anomaly_audit_report.xlsx against the groundtruth workbook, so the oracle
# simply installs the groundtruth result in the workspace.
set -e
cp /solution/groundtruth_workspace/anomaly_audit_report.xlsx /app/anomaly_audit_report.xlsx
echo "oracle: anomaly_audit_report.xlsx installed"
