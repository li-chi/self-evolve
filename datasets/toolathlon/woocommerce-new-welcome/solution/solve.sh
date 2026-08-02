#!/bin/bash
# Oracle: sync first-time customers to BigQuery and send welcome emails
# through the same tool surface the agent has. See solve.py.
set -e
/opt/toolathlon/.venv/bin/python /solution/solve.py
